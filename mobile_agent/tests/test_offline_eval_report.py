"""Offline report accounting from fake artifacts; no agent, device, model or network."""

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mobile_agent.evals.offline_report import BENCH, InvalidEvidence, automation, render, testing

A, B, C, D = (letter * 64 for letter in "abcd")


class OfflineReportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def bench(self, *, suite="v1", frozen=True):
        run = self.root / "bench"
        run.mkdir(exist_ok=True)
        manifest = json.loads((BENCH / "manifest.json").read_text())
        tasks = manifest["tasks"][:2]
        attempts = [{"repeat": repeat, "order": repeat * 2 + index, "task": task, "agent": "mobster"}
                    for repeat in range(2) for index, task in enumerate(tasks)]
        plan = {"run_id": "bench-1", "suite": suite, "suite_hash": manifest["sha256"],
                "manifest_frozen": frozen, "tasks": tasks, "agents": {"mobster": {"config": {"model": "test-model"}}},
                "repeats": 2, "attempts": attempts, "device": "<UDID>", "truth_digest": A,
                "git": {"commit": "abc123", "mobile_agent_dirty": False}}
        (run / "plan.json").write_text(json.dumps(plan))
        def row(index, verdict, **extra):
            attempt = attempts[index]
            return {**attempt, "key": f"{attempt['repeat']}:{attempt['task']}:mobster",
                    "verdict": verdict, **extra}
        records = [row(0, "infra", agent_ms=100, cost_usd=.05),
                   row(0, "pass", status="completed", config={"model": "resolved-a"},
                       agent_ms=1000, cost_usd=.10, unpriced_calls=0, unsafe=[], unintended=[]),
                   row(2, "fail", status="completed", config={"model": "resolved-b"},
                       agent_ms=2000, cost_usd=None, unpriced_calls=1,
                       unsafe=[{"op": "TAP"}], unintended=[]),
                   row(3, "ungraded", status="completed", agent_ms=500)]
        (run / "records.jsonl").write_text("".join(json.dumps(item) + "\n" for item in records))
        return run, plan, records

    def development(self):
        run = self.root / "development"
        run.mkdir()
        manifest = {"schema": 1, "cases": [{"id": "bug.one", "family": "automation",
                    "checkSha256": C, "bugCheck": "bug", "regressionChecks": ["invariant"],
                    "criticalChecks": ["safety"], "maxAttempts": 2}]}
        path = self.root / "manifest.json"
        path.write_text(json.dumps(manifest))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        def check(name, ok, label):
            artifact = f"{label}-{name}.json"
            raw = json.dumps({"id": name, "ok": ok}).encode()
            (run / artifact).write_bytes(raw)
            return {"id": name, "ok": ok, "evidence": artifact,
                    "sha256": hashlib.sha256(raw).hexdigest()}
        def arm(build, bug, label):
            return {"buildSha256": build, "startStateSha256": D, "setupSha256": A,
                    "assertionSha256": C, "reached": True,
                    "checks": [check("bug", bug, label), check("invariant", True, label),
                               check("safety", True, label)]}
        header = {"schema": 1, "manifestSha256": digest, "runId": "paired-1", "configSha256": D}
        claims = {**header, "attempts": [{"caseId": "bug.one", "attempt": 1, "baselineVerdict": "clean",
                                           "fixedVerdict": "unknown"},
                                          {"caseId": "bug.one", "attempt": 2, "baselineVerdict": "bug",
                                           "fixedVerdict": "fixed"}]}
        oracle = {**header, "baselineBuildSha256": A, "fixedBuildSha256": B,
                  "attempts": [{"caseId": "bug.one", "attempt": n,
                                "baseline": arm(A, False, f"b{n}"), "fixed": arm(B, True, f"f{n}")}
                               for n in (1, 2)]}
        (run / "claims.json").write_text(json.dumps(claims))
        (run / "oracle.json").write_text(json.dumps(oracle))
        return run, path

    def test_bench_retries_do_not_inflate_planned_success_and_missing_stays_in_denominator(self):
        run, _, _ = self.bench()
        report = automation(run)
        agent = report["agents"]["mobster"]
        self.assertEqual((agent["planned"], agent["recordRows"], agent["infraRetries"]), (4, 4, 1))
        self.assertEqual(agent["outcomes"], {"pass": 1, "missing": 1, "fail": 1, "ungraded": 1})
        self.assertEqual(agent["plannedSuccess"], {"pass": 1, "denominator": 4})
        self.assertEqual(agent["gradedSuccess"], {"pass": 1, "denominator": 2})
        self.assertEqual(agent["firstRepeat"], {"pass": 1, "missing": 1})
        self.assertEqual(agent["laterIndependentRepeats"], {"fail": 1, "ungraded": 1})
        metrics = agent["metrics"]
        self.assertEqual((metrics["agentMsSubtotal"], metrics["costMeasured"]), (3600, 2))
        self.assertAlmostEqual(metrics["pricedUsdSubtotal"], .15)
        self.assertEqual((metrics["blockedInterventions"], metrics["interventionMeasured"],
                          metrics["unsupportedCompletionClaims"]), (1, 2, 2))
        self.assertEqual((metrics["failedCompletionClaims"], metrics["ungradedCompletionClaims"]), (1, 1))
        observed = agent["observedConfigs"]
        self.assertEqual(observed["models"], ["resolved-a", "resolved-b"])
        self.assertEqual((observed["measured"], observed["terminalRows"], observed["configurationVariation"]),
                         (2, 3, True))
        self.assertEqual(report["provenance"]["registeredCorpus"], "v1")
        self.assertEqual(report["provenance"]["independence"], "unverified")

    def test_duplicate_terminal_and_mismatched_frozen_manifest_refuse(self):
        run, plan, records = self.bench()
        with (run / "records.jsonl").open("a") as out:
            out.write(json.dumps(records[1]) + "\n")
        with self.assertRaisesRegex(InvalidEvidence, "duplicate terminal"):
            automation(run)
        (run / "records.jsonl").write_text(json.dumps(records[0]) + "\n")
        plan["suite_hash"] = B
        (run / "plan.json").write_text(json.dumps(plan))
        with self.assertRaisesRegex(InvalidEvidence, "claims a frozen manifest"):
            automation(run)

    def test_unverified_corpus_and_torn_tail_are_disclosed(self):
        run, plan, records = self.bench(suite="training", frozen=True)
        (run / "records.jsonl").write_text(json.dumps(records[0]) + "\n{" )
        report = automation(run)
        self.assertTrue(report["tornTail"])
        self.assertIsNone(report["provenance"]["registeredCorpus"])
        self.assertEqual(report["agents"]["mobster"]["outcomes"], {"infra": 1, "missing": 3})
        self.assertIn("unverified", render([run]))

    def test_missing_records_and_omitted_plan_slot_keep_the_denominator(self):
        run, plan, _ = self.bench()
        (run / "records.jsonl").unlink()
        report = automation(run)
        self.assertEqual(report["agents"]["mobster"]["outcomes"], {"missing": 4})
        self.assertIsNone(report["provenance"]["recordsSha256"])
        plan["attempts"].pop()
        (run / "plan.json").write_text(json.dumps(plan))
        with self.assertRaisesRegex(InvalidEvidence, "omits a task/agent/repeat slot"):
            automation(run)

    def test_offline_import_does_not_load_live_agents(self):
        result = subprocess.run([sys.executable, "-c", "import sys; import mobile_agent.evals.offline_report; "
                                 "assert 'mobile_agent.bench.agents' not in sys.modules"],
                                cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_paired_scorer_stays_in_test_stratum_and_recovers_only_on_second_attempt(self):
        bench, _, _ = self.bench()
        run, manifest = self.development()
        paired = testing(run, manifest)
        case = paired["score"]["cases"][0]
        self.assertEqual((case["firstAttempt"], case["eventual"], case["recovered"]),
                         ("survived", "detected", True))
        self.assertEqual(paired["score"]["falseSuccess"], 1)
        self.assertEqual((paired["time"], paired["cost"], paired["interventions"]),
                         ("not recorded", "not recorded", "not recorded"))
        text = render([bench], [run], development_manifest=manifest)
        self.assertIn("## Automate: bench-1", text)
        self.assertIn("## Test: paired-1", text)
        self.assertIn("First and later repeats are independent", text)
        self.assertIn("recovered", text)
        self.assertIn("observed models", text)
        with self.assertRaisesRegex(InvalidEvidence, "duplicate automation run"):
            render([bench, bench])
        with self.assertRaisesRegex(InvalidEvidence, "require --development-manifest"):
            render([], [run])


if __name__ == "__main__":
    unittest.main()
