"""Exact saved-suite comparisons preserve both verdicts and refuse missing provenance."""

import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

from mobile_agent.testkit import capsule, comparison, reports

BASE = "20261024-120000-abcd"
CANDIDATE = "20261024-120001-abcd"


def suite(suite_id, status):
    reason = {"class": "assertion", "message": "not yet"} if status == "failed" else None
    return {"schema": reports.SCHEMA, "suite": suite_id, "finishedAt": 2,
            "exitCode": 1 if status == "failed" else 0, "options": {"strict": False},
            "executionPolicy": {"retries": 0, "repeat": 1, "replay": "fresh", "keyless": False,
                                "assertTimeout": 5, "mobster": "test"},
            "devices": [{"key": "sim:iPhone 17 Pro@iOS 26.4", "name": "private phone"}],
            "checks": [{"file": ".mobster/checks/example.yaml", "checkSpecDigest": "a" * 64,
                        "baselineDigest": "none", "fixtureDigest": "b" * 64,
                        "results": [{"deviceKey": "sim:iPhone 17 Pro@iOS 26.4", "status": status,
                                     "reason": reason, "environment": {"kind": "simulator",
                                     "model": "iPhone 17 Pro", "runtime": "iOS 26.4"},
                                     "quarantined": False, "flaky": False, "replayed": False,
                                     "healed": [], "assertions": [], "appDigest": ("c" if status == "failed"
                                                                           else "d") * 64}]}]}


def controlled(doc):
    check = doc["checks"][0]
    cell = check["results"][0]
    failed = cell["status"] == "failed"
    check["baselineDigest"] = "2" * 64  # media or condition assets, without a visual baseline
    check["visualBaselineSha256"] = None
    check["businessBindingSha256"] = "e" * 64
    proof = {"bindingSha256": check["businessBindingSha256"], "setup": "seeded", "status": "observed",
             "cleanup": "removed", "generation": 1, "sourceSha256": ("f" if failed else "1") * 64,
             "assertionFields": ["enabled"], "passedFields": [] if failed else ["enabled"],
             "failedFields": ["enabled"] if failed else []}
    cell.update(attempts=1, business=proof, reason={"class": "business"} if failed else None,
                runs=[{"business": deepcopy(proof), "verdict": cell["status"],
                       "class": "business" if failed else None}])
    return doc


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.results = Path(temporary.name).resolve() / ".mobster" / "test-results"
        self.results.mkdir(parents=True)
        self.base = suite(BASE, "failed")
        self.candidate = suite(CANDIDATE, "passed")
        self.save(self.base)
        self.save(self.candidate)

    def save(self, doc):
        folder = self.results / doc["suite"]
        folder.mkdir(exist_ok=True)
        reports.write_json(doc, folder / "results.json")
        (folder / "junit.xml").write_text("<xml/>", encoding="utf-8")
        (folder / "index.html").write_text("<html/>", encoding="utf-8")
        capsule.capture(folder, doc)

    def test_exact_pair_reports_observed_transition_without_claiming_a_fix(self):
        result = comparison.compare_suites(self.results, BASE, CANDIDATE)
        self.assertEqual((result["baseVerdict"], result["candidateVerdict"]), (1, 0))
        self.assertTrue(result["comparable"])
        row = result["cases"][0]
        self.assertEqual(row["observedTransition"], "failed_to_passed")
        self.assertFalse(result["verifiedFix"] or row["verifiedFix"])
        self.assertEqual(row["proofReasons"], ["controlled_fixture_unproven"])
        self.assertNotIn("example.yaml", str(result))

    def test_changed_check_fixture_environment_and_policy_are_incomparable(self):
        for path, value, reason in (("checkSpecDigest", "0" * 64, "check_changed"),
                                    ("fixtureDigest", "0" * 64, "fixture_changed")):
            changed = deepcopy(self.candidate)
            changed["checks"][0][path] = value
            with self.subTest(reason=reason):
                self.assertIn(reason, comparison.compare(self.base, changed)["cases"][0]["reasons"])
        changed = deepcopy(self.candidate)
        changed["checks"][0]["results"][0]["environment"]["runtime"] = "iOS 25.0"
        self.assertIn("environment_changed", comparison.compare(self.base, changed)["cases"][0]["reasons"])
        changed = deepcopy(self.candidate)
        changed["executionPolicy"]["repeat"] = 2
        result = comparison.compare(self.base, changed)
        self.assertIn("execution_policy_changed", result["reasons"])
        self.assertFalse(result["cases"][0]["comparable"])

    def test_missing_or_tampered_inventory_refuses_exact_saved_pair(self):
        folder = self.results / BASE
        (folder / "evidence-inventory.json").unlink()
        with self.assertRaises(comparison.ComparisonError):
            comparison.compare_suites(self.results, BASE, CANDIDATE)
        capsule.capture(folder, self.base)
        (folder / "results.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(comparison.ComparisonError):
            comparison.compare_suites(self.results, BASE, CANDIDATE)

    def test_duplicate_or_unstable_case_and_scenario_refuse(self):
        duplicate = deepcopy(self.base)
        duplicate["checks"].append(deepcopy(duplicate["checks"][0]))
        result = comparison.compare(duplicate, self.candidate)
        self.assertFalse(result["comparable"])
        self.assertIn("duplicate_case_target", result["cases"][0]["reasons"])
        unstable = deepcopy(self.base)
        unstable["checks"][0]["results"][0]["flaky"] = True
        self.assertIn("verdict_not_stable", comparison.compare(unstable, self.candidate)["cases"][0]["reasons"])
        scenario = deepcopy(self.base)
        scenario["scenario"] = {"manifestDigest": "e" * 64}
        self.assertIn("scenario_not_comparable", comparison.compare(scenario, self.candidate)["reasons"])

    def test_an_unpinned_app_is_the_one_reason_a_complete_proof_is_not_a_fix(self):
        complete = controlled(deepcopy(self.base)), controlled(deepcopy(self.candidate))
        self.assertTrue(comparison.compare(*complete)["verifiedFix"])
        for sides in ((0,), (1,), (0, 1)):
            with self.subTest(sides=sides):
                docs = deepcopy(complete)
                for side in sides:
                    docs[side]["checks"][0]["results"][0]["appDigest"] = None
                result = comparison.compare(*docs)
                row = result["cases"][0]
                self.assertFalse(result["verifiedFix"] or row["verifiedFix"])
                self.assertEqual(row["proofReasons"], ["app_artifact_unknown"])
                self.assertEqual(row["observedTransition"], "failed_to_passed")

    def test_complete_controlled_app_state_proof_is_narrow(self):
        before = controlled(deepcopy(self.base))
        after = controlled(deepcopy(self.candidate))
        self.save(before)
        self.save(after)
        result = comparison.compare_suites(self.results, BASE, CANDIDATE)
        self.assertTrue(result["verifiedFix"])
        self.assertEqual(result["proof"]["scope"], "controlled_app_state")
        self.assertTrue(result["cases"][0]["verifiedFix"])

        changes = (
            ("unbound", lambda old, new: old["checks"][0].update(businessBindingSha256=None)),
            ("overlap", lambda old, new: old["checks"][0]["results"][0]["business"]
             ["passedFields"].append("enabled")),
            ("same snapshot", lambda old, new: new["checks"][0]["results"][0]["business"]
             .update(sourceSha256="f" * 64)),
            ("unknown reason", lambda old, new: new["checks"][0]["results"][0]
             .update(reason={"class": "unknown_outcome"})),
            ("AX failed", lambda old, new: new["checks"][0]["results"][0]
             .update(assertions=[{"index": 0, "text": "button", "ok": False}])),
            ("real phone", lambda old, new: (old["checks"][0]["results"][0]
             ["environment"].update(kind="iphone"), new["checks"][0]["results"][0]
             ["environment"].update(kind="iphone"))),
            ("repeat", lambda old, new: old["checks"][0]["results"][0].update(attempts=2)),
            ("review finding", lambda old, new: new["checks"][0]["results"][0]
             .update(findings=[{"level": "review"}])),
            ("uncertain condition", lambda old, new: new["checks"][0]["results"][0]
             .update(conditions={"applied": [], "skipped": [], "cleanup": "needs_reconciliation"})),
            ("visual required", lambda old, new: (old["checks"][0].update(visualBaselineSha256="3" * 64),
             new["checks"][0].update(visualBaselineSha256="3" * 64))),
            ("contradictory visual", lambda old, new: new["checks"][0]["results"][0]
             .update(visual={"ok": False})),
        )
        for label, change in changes:
            with self.subTest(label=label):
                old, new = deepcopy(before), deepcopy(after)
                change(old, new)
                for doc in (old, new):
                    cell = doc["checks"][0]["results"][0]
                    cell["runs"][0]["business"] = deepcopy(cell["business"])
                self.assertFalse(comparison.compare(old, new)["verifiedFix"])
        repeated_base, repeated_candidate = deepcopy(before), deepcopy(after)
        repeated_base["executionPolicy"]["repeat"] = 2
        repeated_candidate["executionPolicy"]["repeat"] = 2
        self.assertFalse(comparison.compare(repeated_base, repeated_candidate)["verifiedFix"])

    def test_one_proven_case_does_not_verify_an_unproven_second_failure(self):
        before = controlled(deepcopy(self.base))
        after = controlled(deepcopy(self.candidate))
        for doc, source in ((before, self.base), (after, self.candidate)):
            second = deepcopy(source["checks"][0])
            second["file"] = ".mobster/checks/other.yaml"
            doc["checks"].append(second)
        result = comparison.compare(before, after)
        self.assertTrue(result["cases"][0]["verifiedFix"])
        self.assertFalse(result["cases"][1]["verifiedFix"])
        self.assertFalse(result["verifiedFix"])
