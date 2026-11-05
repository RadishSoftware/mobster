"""The report is computed from records, and its statistics hold."""

import json
import tempfile
import unittest
from pathlib import Path

from mobile_agent.bench.metrics import bootstrap_ci, flaky_tasks, pass_hat_k, percentile, quantile, wilson
from mobile_agent.bench.report import render
from mobile_agent.bench.suite import build_suite


class MetricsTests(unittest.TestCase):
    def test_wilson_known_values(self):
        low, high = wilson(8, 10)
        self.assertAlmostEqual(low, .4902, places=3)
        self.assertAlmostEqual(high, .9433, places=3)
        low, high = wilson(0, 10)
        self.assertEqual(low, 0)
        self.assertAlmostEqual(high, .2775, places=3)
        self.assertTrue(all(v != v for v in wilson(0, 0)))

    def test_percentile(self):
        self.assertEqual(percentile([1, 2, 3, 4], 50), 2.5)
        self.assertEqual(percentile([5], 90), 5)
        self.assertIsNone(percentile([None], 50))

    def test_quantile(self):
        self.assertEqual(quantile([4, 1, 3, 2], .5), 2.5)
        self.assertEqual(quantile([5], .9), 5)
        self.assertAlmostEqual(quantile([1, 2, 3, 10], .9), 7.9)
        self.assertIsNone(quantile([], .5))

    def test_pass_hat_k_and_flaky(self):
        outcomes = {"a": [True, True, True], "b": [True, False, False], "c": [False, False, False]}
        self.assertAlmostEqual(pass_hat_k(outcomes, 1), (1 + 1 / 3 + 0) / 3)
        self.assertAlmostEqual(pass_hat_k(outcomes, 3), 1 / 3)
        self.assertEqual(flaky_tasks(outcomes), ["b"])

    def test_bootstrap_is_deterministic(self):
        values = [0, 0, 1, 1, 1, .5]
        self.assertEqual(bootstrap_ci(values, resamples=500), bootstrap_ci(values, resamples=500))
        mean, low, high = bootstrap_ci(values, resamples=2000)
        self.assertLessEqual(low, mean)
        self.assertLessEqual(mean, high)


TASKS = [task for task in build_suite()][:20]


def records(agent, pass_rate, ms, *, unsafe=0, repeats=3):
    rows = []
    for index, task in enumerate(TASKS):
        for repeat in range(repeats):
            passed = (index + repeat) % 100 < pass_rate * 100 if pass_rate < 1 else True
            passed = passed if pass_rate > 0 else False
            rows.append({"key": f"{repeat}:{task.id}:{agent}", "repeat": repeat, "task": task.id,
                         "category": task.category, "agent": agent, "verdict": "pass" if passed else "fail",
                         "agent_ms": ms, "wall_ms": ms, "decision_steps": 4, "first_action_ms": 500,
                         "model_calls": 5, "cost_usd": .002, "unpriced_calls": 0,
                         "unsafe": [{"level": "unsafe", "op": "TAP", "label": "Send", "reason": "x"}] if unsafe else [],
                         "unintended": [], "checks": []})
    return rows


def write_run(tmp, rows):
    run = Path(tmp) / "run"
    run.mkdir()
    (run / "plan.json").write_text(json.dumps({"suite_hash": "f" * 64, "seed": 1, "repeats": 3,
                                               "device": "iphone15pro", "tasks": [t.id for t in TASKS],
                                               "agents": {"mobster-smart": {"config": {}},
                                                          "mobster-smart-codex": {"config": {}}}}))
    (run / "records.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return run


class RenderTests(unittest.TestCase):
    def test_render_creates_output_parents_for_an_external_run(self):
        with tempfile.TemporaryDirectory() as external, tempfile.TemporaryDirectory() as checkout:
            run = write_run(external, records("mobster", 1, 5000))
            out = Path(checkout) / "research" / "reports" / "bench.md"
            self.assertFalse(out.parent.exists())

            text = render(run, out)

            self.assertEqual(out.read_text(), text)
            self.assertIn("## Success rate", text)
            self.assertTrue((run / "records.jsonl").exists())

    def test_render_has_every_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = records("mobster-smart", 1, 5000) + records("mobster-smart-codex", .5, 20000, unsafe=1)
            rows.append({"key": "x", "repeat": 0, "task": TASKS[0].id, "category": TASKS[0].category,
                         "agent": "mobster-smart-cc", "verdict": "skipped", "error": "agent unavailable"})
            out = Path(tmp) / "bench.md"
            text = render(write_run(tmp, rows), out, today="2026-09-23")
            for heading in ("## Success rate", "### By category", "## Speed and cost", "## Safety",
                            "## Abstention and visual", "## Per task", "## Attempt accounting"):
                self.assertIn(heading, text)
            self.assertTrue(out.exists())
            self.assertIn("| mobster-smart-cc | 0 | 0 | 0 | 1 | 0 |", text)


if __name__ == "__main__":
    unittest.main()
