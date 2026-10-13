"""iOSWorld's official pass rule, the paired A/B comparison and the pooled estimate. Offline."""

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import tempfile
import unittest

from mobile_agent.bench import iosworld
from mobile_agent.bench.iosworld_stats import (commit_requested, compare, estimate, format_compare, load_runs,
                                               official_pass, read_prompts, read_worker_logs, report_lines)


def evaluation(*satisfied, success=None, score=None):
    satisfied = list(satisfied)
    return {"success": all(satisfied) if success is None else success,
            "score": (sum(satisfied) / len(satisfied) if satisfied else 0.0) if score is None else score,
            "rubric_results": [{"criterion": f"criterion {i}", "satisfied": s} for i, s in enumerate(satisfied)]}


def prompt(*labels):
    rows = "\n".join(f'e{i + 1} Button "{label}" @1,1,1,1' for i, label in enumerate(labels))
    return json.dumps({"event": "frontier_prompt", "text": f"Request: x\n\nScreen elements:\n{rows}\n"})


def write_task(root, run, number, task, *, category="multi_app", goal="", ev=None, status="completed",
               reason="", events=(), prompts=(), calls=None, model="gpt-5.6-sol"):
    folder = pathlib.Path(root) / run / f"{number:03d}-{task}"
    folder.mkdir(parents=True, exist_ok=True)
    record = {"task": task, "goal": goal, "apps": ["a", "b"], "category": category, "difficulty": "medium",
              "status": "ok", "agent_answer": "done",
              "mobster": {"status": status, "reason": reason, "model": model, "policy": "frontier",
                          "model_calls": calls if calls is not None else len(prompts), "agent_seconds": 10.0,
                          "cost_usd": 0.1, "events": list(events)}}
    if ev is not None:
        record["evaluation"] = ev
    (folder / "task.json").write_text(json.dumps(record))
    (folder / "trajectory.json").write_text("[]")
    if prompts:
        (folder / "prompts.jsonl").write_text("\n".join(prompts) + "\n")
    return folder


class OfficialRuleTests(unittest.TestCase):
    def test_every_criterion_must_be_satisfied(self):
        self.assertTrue(official_pass(evaluation(True, True)))
        # The judge's own flag says pass; one criterion failed: iOSWorld counts a fail.
        self.assertFalse(official_pass(evaluation(True, False, success=True)))
        # The flag says fail; every criterion held: iOSWorld counts a pass.
        self.assertTrue(official_pass(evaluation(True, True, success=False)))

    def test_an_empty_rubric_fails_and_no_rubric_falls_back_to_the_flag(self):
        self.assertFalse(official_pass({"success": True, "rubric_results": []}))
        self.assertTrue(official_pass({"success": True}))
        self.assertFalse(official_pass({"success": False}))
        self.assertFalse(official_pass(None))

    def test_matches_iosworlds_own_aggregate(self):
        repo = pathlib.Path(os.environ.get("IOSWORLD_REPO") or
                            pathlib.Path(__file__).resolve().parents[2].parent / "iOSWorld")
        script = repo / "scripts" / "judge_trajectories.py"
        if not script.exists():
            self.skipTest("no iOSWorld checkout (set IOSWORLD_REPO)")
        spec = importlib.util.spec_from_file_location("judge_trajectories_under_test", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cases = [evaluation(True, True), evaluation(True, False, success=True), evaluation(True, True, success=False),
                 {"success": True, "rubric_results": []}, {"success": True}, {"success": False, "score": 0.4}]
        for case in cases:
            agg = module._aggregate([{"status": "ok", "evaluation": case}])
            self.assertEqual(agg["passes"], int(official_pass(case)), case)


class CommitTests(unittest.TestCase):
    def test_requested_commits(self):
        self.assertTrue(commit_requested("Post comment", "Open the doc and add a comment noting my summary."))
        # An identifier label: its checkout completes the order the request asks for.
        self.assertTrue(commit_requested("mybank.checkout.confirmToggle", "Order a high-protein recovery meal."))
        self.assertTrue(commit_requested("I confirm this charge", "Order a meal on QuickBite."))
        self.assertTrue(commit_requested("Pay", "pay any pending requests under $50"))
        self.assertTrue(commit_requested("Confirm Reservation", "make a reservation for 6 people"))
        self.assertTrue(commit_requested("Follow", "tap 'Follow' on Netflix"))

    def test_unrequested_commits(self):
        self.assertFalse(commit_requested("Reset", "Find a restaurant and post the name in #general."))
        self.assertFalse(commit_requested("Delete", "Summarize it all in a new Notes note."))
        self.assertFalse(commit_requested("Reserve", "Check StayFinder for the listing details."))
        # "confirm" in a request that asks for nothing to commit is a report verb, not a commit.
        self.assertFalse(commit_requested("Confirm", "Check my balance and confirm the note was created."))

    def test_non_commits_and_hand_labels(self):
        self.assertIsNone(commit_requested("Save", "anything"))
        self.assertIsNone(commit_requested("Share", "anything"))  # opening a share sheet commits nothing
        labels = {"multi-001": {"Reset": True}}
        self.assertTrue(commit_requested("Reset", "post the name", labels, "multi-001"))
        self.assertFalse(commit_requested("Reset", "post the name", labels, "multi-002"))


class TaskRunTests(unittest.TestCase):
    def test_mechanism_counts(self):
        with tempfile.TemporaryDirectory() as root:
            events = [{"event": "frontier_refused", "step": 3, "label": "Post comment"},
                      {"event": "frontier_refused", "step": 4, "label": "Post comment"},
                      {"event": "frontier_refused", "step": 5, "label": "Reset"},
                      {"event": "frontier_refused", "step": 6, "label": "loop"},
                      {"event": "frontier_action", "step": 7, "operation": "LONG_PRESS", "target_label": "Post comment"},
                      {"event": "frontier_action", "step": 8, "operation": "TAP", "target_label": "Delete"},
                      {"event": "frontier_action", "step": 8, "operation": "TAP", "target_label": "Budget Tracker"},
                      {"event": "frontier_chunk_stop", "step": 9, "reason": "verify_before_done"},
                      {"event": "frontier_chunk_stop", "step": 10, "reason": "verify_before_done"},
                      {"event": "frontier_chunk_stop", "step": 10, "reason": "no_change"}]
            prompts = [prompt("Home"), prompt("Docs"), prompt("Docs"), prompt("Home"), prompt("Docs")]
            write_task(root, "arm", 1, "multi-083", goal="Open the doc and add a comment.",
                       ev=evaluation(True, False, success=True), events=events, prompts=prompts, calls=6)
            [run] = load_runs([os.path.join(root, "arm")])
        self.assertEqual(len(run.guard_refusals), 3)
        self.assertEqual(run.required_refusals, 2)
        self.assertEqual(run.commits, ["Post comment", "Delete"])
        self.assertEqual(run.unrequested_commits, 1)
        self.assertEqual(run.done_deferrals, 2)
        self.assertTrue(run.false_claim)  # said DONE, failed a criterion
        self.assertTrue(run.disagrees)  # the judge's flag said pass
        self.assertEqual((run.calls, run.turns, run.revisits, run.stays), (6, 5, 2, 1))

    def test_wda_crash_and_calls_from_prompts(self):
        with tempfile.TemporaryDirectory() as root:
            write_task(root, "arm", 127, "mem-046", category="memory", ev=evaluation(False), status="error",
                       reason="TransportError: HTTP ConnectionRefusedError; request outcome unknown",
                       prompts=[prompt("A")] * 4)
            (pathlib.Path(root) / "arm" / "127-mem-046" / "task.json").write_text(json.dumps(
                {**json.loads((pathlib.Path(root) / "arm" / "127-mem-046" / "task.json").read_text()),
                 "mobster": {"status": "error", "reason": "TransportError: HTTP ConnectionRefusedError",
                             "model_calls": None}}))
            [run] = load_runs([os.path.join(root, "arm")])
        self.assertTrue(run.wda_crash)
        self.assertFalse(run.false_claim)
        self.assertEqual(run.calls, 4)

    def test_unjudged_runs_do_not_count(self):
        with tempfile.TemporaryDirectory() as root:
            write_task(root, "arm", 1, "t-001")
            [run] = load_runs([os.path.join(root, "arm")])
            self.assertFalse(run.judged)
            self.assertIn("judged 0", report_lines([run])[0])

    def test_model_filter(self):
        with tempfile.TemporaryDirectory() as root:
            write_task(root, "arm", 1, "t-001", ev=evaluation(True), model="gpt-5.6-sol")
            write_task(root, "arm", 2, "t-002", ev=evaluation(True), model="gpt-5.6-terra")
            self.assertEqual([r.task for r in load_runs([os.path.join(root, "arm")], model="gpt-5.6-sol")], ["t-001"])

    def test_prompts_and_worker_logs(self):
        with tempfile.TemporaryDirectory() as root:
            path = pathlib.Path(root) / "prompts.jsonl"
            path.write_text("\n".join([prompt("A"), "not json", prompt("B"), prompt("A"), prompt("A")]))
            self.assertEqual(read_prompts(path), (4, 1, 1))
            (pathlib.Path(root) / "run").mkdir()
            (pathlib.Path(root) / "run.worker0.log").write_text(
                "multi-001                NOT RUN (reset failed: x)\nmulti-002                completed  1.0s\n")
            (pathlib.Path(root) / "run.worker1.log").write_text("Traceback (most recent call last):\n  boom\n")
            self.assertEqual(read_worker_logs(pathlib.Path(root) / "run"), {"not_run": ["multi-001"], "tracebacks": 1})


class ReportTests(unittest.TestCase):
    def test_report_counts_the_official_rule_and_shows_the_flag(self):
        with tempfile.TemporaryDirectory() as root:
            write_task(root, "arm", 1, "t-001", ev=evaluation(True, False, success=True))
            write_task(root, "arm", 2, "t-002", ev=evaluation(True, True, success=False))
            write_task(root, "arm", 3, "t-003", ev=evaluation(True, True))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                iosworld.main(["report", "--run", os.path.join(root, "arm")])
        text = out.getvalue()
        self.assertIn("official passes 2 (66.7%)", text)
        self.assertIn("judge success flag 2/3; disagrees with the official rule on 2", text)
        self.assertIn("t-001 (official fail, flag pass", text)
        self.assertIn("multi_app 2/3", text)


def arms(root):
    """Arm A passes t1 twice and t2 once; arm B passes t1 once and t2 twice: a tie at 3/4."""
    plan = {"a1": {"t1": True, "t2": True}, "a2": {"t1": True, "t2": False},
            "b1": {"t1": True, "t2": True}, "b2": {"t1": False, "t2": True}}
    for run, tasks in plan.items():
        for number, (task, passed) in enumerate(sorted(tasks.items()), 1):
            ev = evaluation(True, True) if passed else evaluation(True, False, success=run == "b2")
            write_task(root, run, number, task, ev=ev,
                       events=[{"event": "frontier_chunk_stop", "reason": "verify_before_done"}] * (run[0] == "a"))
    return [os.path.join(root, r) for r in ("a1", "a2")], [os.path.join(root, r) for r in ("b1", "b2")]


class CompareTests(unittest.TestCase):
    def test_paired_comparison(self):
        with tempfile.TemporaryDirectory() as root:
            a, b = arms(root)
            result = compare(load_runs(a), load_runs(b), n_boot=2000, seed=1)
            again = compare(load_runs(a), load_runs(b), n_boot=2000, seed=1)
        self.assertEqual((result["a"]["official"], result["b"]["official"]), (3, 3))
        self.assertEqual(result["b"]["success"], 4)  # b2's failed t1 still had the flag set
        self.assertEqual(result["paired_tasks"], 2)
        self.assertEqual(result["pass_diff"], 0.0)
        # Per-task differences -0.5 and +0.5: SE = stdev / sqrt(2) = 0.5.
        self.assertAlmostEqual(result["pass_diff_se"], 0.5)
        self.assertAlmostEqual(result["rubric_diff"], 0.0)
        self.assertEqual(result["pass_diff_ci"], again["pass_diff_ci"])  # seeded
        lo, hi = result["pass_diff_ci"]
        self.assertTrue(-0.5 <= lo <= 0 <= hi <= 0.5)
        self.assertEqual((result["a"]["agreeing_tasks"], result["a"]["repeated_tasks"]), (1, 2))
        self.assertEqual(result["a"]["mechanisms"]["done_deferrals"]["total"], 4)
        self.assertEqual(result["b"]["mechanisms"]["done_deferrals"]["total"], 0)
        self.assertEqual(len(result["b"]["disagreements"]), 1)
        text = format_compare(result)
        self.assertIn("official passes (every criterion)", text)
        self.assertIn("pass rate    +0.0 points, SE 50.0", text)

    def test_cli(self):
        with tempfile.TemporaryDirectory() as root:
            a, b = arms(root)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                iosworld.main(["compare", "--a", *a, "--b", os.path.join(root, "b*"), "--bootstrap", "200"])
            self.assertIn("paired over 2 tasks, B - A", out.getvalue())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                iosworld.main(["compare", "--a", *a, "--b", *b, "--bootstrap", "200", "--json"])
            self.assertEqual(json.loads(out.getvalue())["paired_tasks"], 2)


class EstimateTests(unittest.TestCase):
    def test_reweights_to_the_full_mix(self):
        with tempfile.TemporaryDirectory() as root:
            # single: 1 task, 2 runs, both pass; multi: 2 tasks, one passes 1 of 3 runs, one 0 of 1; memory: none.
            write_task(root, "r", 1, "s-001", category="single_app", ev=evaluation(True))
            write_task(root, "q", 1, "s-001", category="single_app", ev=evaluation(True))
            for run, passed in (("r", True), ("q", False), ("p", False)):
                write_task(root, run, 2, "m-001", ev=evaluation(passed))
            write_task(root, "r", 3, "m-002", ev=evaluation(False))
            runs = load_runs([os.path.join(root, d) for d in ("r", "q", "p")])
        by_task = estimate(runs, mix={"single_app": 1, "multi_app": 3}, n_boot=500)
        by_run = estimate(runs, mix={"single_app": 1, "multi_app": 3}, weighting="run", n_boot=500)
        # Task-weighted multi: mean(1/3, 0) = 1/6; run-weighted multi: 1/4.
        self.assertAlmostEqual(by_task["estimate"], (1 + 3 * (1 / 6)) / 4)
        self.assertAlmostEqual(by_run["estimate"], (1 + 3 * .25) / 4)
        lo, hi = by_task["ci"]
        self.assertTrue(0 <= lo <= by_task["estimate"] <= hi <= 1)
        self.assertEqual(by_task["categories"]["multi_app"]["runs"], 4)

    def test_missing_category_is_reported(self):
        with tempfile.TemporaryDirectory() as root:
            write_task(root, "r", 1, "s-001", category="single_app", ev=evaluation(True))
            result = estimate(load_runs([os.path.join(root, "r")]), n_boot=100)
        self.assertEqual(result["estimate"], 1.0)
        self.assertEqual(result["missing"], ["multi_app", "memory"])

    def test_cli_filters_by_model(self):
        with tempfile.TemporaryDirectory() as root:
            write_task(root, "r", 1, "s-001", category="single_app", ev=evaluation(True), model="m1")
            write_task(root, "r", 2, "s-002", category="single_app", ev=evaluation(False), model="m2")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                iosworld.main(["estimate", os.path.join(root, "*"), "--model", "m1", "--weighting", "task",
                               "--bootstrap", "100"])
            self.assertIn("1 judged runs over 1 tasks, 1 official passes", out.getvalue())


if __name__ == "__main__":
    unittest.main()
