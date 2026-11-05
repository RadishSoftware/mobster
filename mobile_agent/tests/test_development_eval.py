"""Offline scorer refusals; no simulator, model, build, or network."""

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mobile_agent.evals.development.score import InvalidRun, score

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


class DevelopmentScoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = self.root / "manifest.json"
        self.case = {"id": "bug.one", "family": "automation", "report": "A visible bug",
                     "fixture": "local fixture", "check": "check.yaml", "checkSha256": SHA_C,
                     "bugCheck": "bug", "regressionChecks": ["invariant"],
                     "criticalChecks": ["safety"], "maxAttempts": 2}
        self.write_manifest([self.case])
        self.claims = self.header()
        self.oracle = self.header()
        self.oracle.update(baselineBuildSha256=SHA_A, fixedBuildSha256=SHA_B)

    def write_manifest(self, cases):
        self.manifest.write_text(json.dumps({"schema": 1, "name": "fixture", "cases": cases}))
        self.manifest_sha = hashlib.sha256(self.manifest.read_bytes()).hexdigest()

    def header(self):
        return {"schema": 1, "manifestSha256": self.manifest_sha, "runId": "run-1",
                "configSha256": SHA_D, "attempts": []}

    def check(self, name, ok, suffix):
        name_on_disk = f"{suffix}-{name}.json"
        content = json.dumps({"independent": name, "outcome": ok}).encode()
        (self.root / name_on_disk).write_bytes(content)
        return {"id": name, "ok": ok, "evidence": name_on_disk,
                "sha256": hashlib.sha256(content).hexdigest()}

    def arm(self, build, bug_ok, suffix, *, critical=True, reached=True):
        return {"buildSha256": build, "startStateSha256": SHA_D,
                "setupSha256": SHA_A, "assertionSha256": SHA_C, "reached": reached,
                "checks": [self.check("bug", bug_ok, suffix),
                           self.check("invariant", True, suffix),
                           self.check("safety", critical, suffix)]}

    def attempt(self, ordinal=1, baseline_verdict="bug", fixed_verdict="fixed", *, critical=True):
        self.claims["attempts"].append({"caseId": "bug.one", "attempt": ordinal,
                                         "baselineVerdict": baseline_verdict, "fixedVerdict": fixed_verdict})
        self.oracle["attempts"].append({"caseId": "bug.one", "attempt": ordinal,
                                         "baseline": self.arm(SHA_A, False, f"b{ordinal}", critical=critical),
                                         "fixed": self.arm(SHA_B, True, f"f{ordinal}", critical=critical)})

    def scored(self):
        (self.root / "claims.json").write_text(json.dumps(self.claims))
        (self.root / "oracle.json").write_text(json.dumps(self.oracle))
        return score(self.manifest, self.root)

    def test_missing_registered_case_stays_in_denominator(self):
        extra = dict(self.case, id="bug.two", family="reliability")
        self.write_manifest([self.case, extra])
        self.claims = self.header()
        self.oracle = self.header() | {"baselineBuildSha256": SHA_A, "fixedBuildSha256": SHA_B}
        self.attempt()
        result = self.scored()
        self.assertEqual(result["registered"], 2)
        self.assertEqual(result["cases"][1]["eventual"], "unreached")
        self.assertEqual(result["cases"][0]["verifiedFix"], True)

    def test_oracle_exposes_bug_but_agent_calls_it_clean(self):
        self.attempt(baseline_verdict="clean", fixed_verdict="broken")
        result = self.scored()
        self.assertEqual(result["cases"][0]["eventual"], "survived")
        self.assertEqual(result["falseSuccess"], 1)
        self.assertEqual(result["fixedArmFalsePositive"], 1)
        self.assertFalse(result["cases"][0]["verifiedFix"])

    def test_claim_without_oracle_is_inconclusive_and_unsupported(self):
        self.claims["attempts"].append({"caseId": "bug.one", "attempt": 1,
                                         "baselineVerdict": "clean", "fixedVerdict": "fixed"})
        result = self.scored()
        self.assertEqual(result["cases"][0]["eventual"], "inconclusive")
        self.assertEqual(result["unsupportedSuccess"], 2)
        self.assertEqual(result["falseSuccess"], 0)

    def test_later_detection_is_recovery_but_unknown_critical_vetoes_fix(self):
        self.attempt(1, baseline_verdict="unknown")
        self.attempt(2)
        self.oracle["attempts"][0]["fixed"]["checks"][2]["sha256"] = SHA_A
        result = self.scored()
        case = result["cases"][0]
        self.assertEqual(case["firstAttempt"], "survived")
        self.assertEqual(case["eventual"], "detected")
        self.assertTrue(case["recovered"])
        self.assertFalse(case["verifiedFix"])
        self.assertTrue(case["criticalEvidenceVeto"])

    def test_missing_later_oracle_also_vetoes_earlier_fix(self):
        self.attempt()
        self.claims["attempts"].append({"caseId": "bug.one", "attempt": 2,
                                         "baselineVerdict": "bug", "fixedVerdict": "unknown"})
        self.assertFalse(self.scored()["cases"][0]["verifiedFix"])

    def test_later_fixed_build_failure_overrides_earlier_proof(self):
        self.attempt(1)
        self.attempt(2)
        second = self.oracle["attempts"][1]["fixed"]["checks"][0]
        second["ok"] = False
        content = json.dumps({"independent": "bug", "outcome": False}).encode()
        (self.root / second["evidence"]).write_bytes(content)
        second["sha256"] = hashlib.sha256(content).hexdigest()
        result = self.scored()
        self.assertFalse(result["cases"][0]["verifiedFix"])
        self.assertTrue(result["cases"][0]["verificationEvidenceVeto"])
        self.assertEqual(result["falseSuccess"], 1)

    def test_run_and_build_binding_refusals(self):
        self.attempt()
        self.oracle["fixedBuildSha256"] = SHA_A
        with self.assertRaisesRegex(InvalidRun, "distinct"):
            self.scored()
        self.oracle["fixedBuildSha256"] = SHA_B
        self.claims["runId"] = "other-run"
        with self.assertRaisesRegex(InvalidRun, "different runs"):
            self.scored()

    def test_attempt_union_is_contiguous_bounded_and_unique(self):
        self.claims["attempts"].append({"caseId": "bug.one", "attempt": 2,
                                         "baselineVerdict": "bug", "fixedVerdict": "fixed"})
        with self.assertRaisesRegex(InvalidRun, "non-contiguous"):
            self.scored()
        self.claims["attempts"][0]["attempt"] = 3
        with self.assertRaisesRegex(InvalidRun, "unknown case or attempt"):
            self.scored()
        self.claims["attempts"][0]["attempt"] = 1
        self.claims["attempts"].append(dict(self.claims["attempts"][0]))
        with self.assertRaisesRegex(InvalidRun, "duplicate attempt"):
            self.scored()

    def test_changed_assertion_or_setup_cannot_verify_fix(self):
        self.attempt()
        self.oracle["attempts"][0]["fixed"]["assertionSha256"] = SHA_B
        self.assertFalse(self.scored()["cases"][0]["verifiedFix"])
        self.oracle["attempts"][0]["fixed"]["assertionSha256"] = SHA_C
        self.oracle["attempts"][0]["fixed"]["setupSha256"] = SHA_B
        self.assertFalse(self.scored()["cases"][0]["verifiedFix"])

    def test_tampered_evidence_never_becomes_a_negative_oracle(self):
        self.attempt()
        self.oracle["attempts"][0]["baseline"]["checks"][0]["sha256"] = SHA_B
        result = self.scored()
        self.assertEqual(result["cases"][0]["eventual"], "inconclusive")
        self.assertFalse(result["cases"][0]["verifiedFix"])

    def test_agent_repro_needs_exact_artifact_and_harness_replay(self):
        self.attempt()
        content = b"open paywall; count plans\n"
        (self.root / "repro.txt").write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        self.claims["attempts"][0]["repro"] = {"path": "repro.txt", "sha256": digest}
        self.oracle["attempts"][0]["replayedReproSha256"] = digest
        result = self.scored()
        self.assertTrue(result["cases"][0]["reproProven"])
        self.assertTrue(result["cases"][0]["verifiedFix"])
        self.oracle["attempts"][0]["replayedReproSha256"] = SHA_A
        result = self.scored()
        self.assertFalse(result["cases"][0]["reproProven"])
        self.assertTrue(result["cases"][0]["verifiedFix"])
        self.oracle["attempts"][0]["replayedReproSha256"] = digest
        (self.root / "repro.txt").write_text("changed")
        self.assertFalse(self.scored()["cases"][0]["reproProven"])

    def test_public_scorer_requires_an_explicit_manifest(self):
        self.attempt()
        self.scored()
        public = self.root / "public"
        package = public / "mobile_agent" / "evals" / "development"
        package.mkdir(parents=True)
        for directory in (public / "mobile_agent", public / "mobile_agent" / "evals", package):
            (directory / "__init__.py").write_text("")
        shutil.copyfile(Path(__file__).parents[1] / "evals" / "development" / "score.py",
                        package / "score.py")
        command = [sys.executable, "-S", "-m", "mobile_agent.evals.development.score"]
        absent = subprocess.run([*command, str(self.root)], cwd=public,
                                capture_output=True, text=True, timeout=10)
        self.assertNotEqual(absent.returncode, 0)
        self.assertIn("--manifest", absent.stderr)
        present = subprocess.run([*command, "--manifest", str(self.manifest), str(self.root)],
                                 cwd=public, capture_output=True, text=True, timeout=10)
        self.assertEqual(present.returncode, 0, present.stderr)
        self.assertEqual(json.loads(present.stdout)["registered"], 1)


if __name__ == "__main__":
    unittest.main()
