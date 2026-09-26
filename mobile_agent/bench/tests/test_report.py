"""The report is computed from records, and the verdict follows the pre-registered rule."""

import json
import tempfile
import unittest
from pathlib import Path

from mobile_agent.bench.report import compare, render, verdict_lines
from mobile_agent.bench.suite import build_suite

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
                                               "agents": {"mobster": {"config": {}}, "gemini-cu": {"config": {}}}}))
    (run / "records.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return run


class CompareTests(unittest.TestCase):
    def test_faster_and_as_accurate_is_sota(self):
        rows = records("mobster", 1, 5000) + records("gemini-cu", 1, 20000)
        c = compare([r for r in rows], "mobster", "gemini-cu", resamples=500)
        self.assertEqual(c["accuracy"], "not worse")
        self.assertEqual(c["speed"], "faster")
        self.assertIn("state of the art", verdict_lines({"gemini-cu": c}, 0, True)[0])

    def test_less_accurate_is_not_sota_and_losses_are_listed(self):
        rows = records("mobster", 0, 5000) + records("gemini-cu", 1, 20000)
        c = compare(rows, "mobster", "gemini-cu", resamples=500)
        self.assertEqual(c["accuracy"], "worse")
        self.assertEqual(len(c["losing_tasks"]), len(TASKS))
        self.assertIn("NOT", verdict_lines({"gemini-cu": c}, 0, True)[0])

    def test_unsafe_mobster_disqualifies(self):
        rows = records("mobster", 1, 5000) + records("gemini-cu", 1, 20000)
        c = compare(rows, "mobster", "gemini-cu", resamples=500)
        self.assertIn("NOT", verdict_lines({"gemini-cu": c}, 1, True)[0])


class RenderTests(unittest.TestCase):
    def test_render_has_every_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = records("mobster", 1, 5000) + records("gemini-cu", .5, 20000, unsafe=1)
            rows.append({"key": "x", "repeat": 0, "task": TASKS[0].id, "category": TASKS[0].category,
                         "agent": "som-pro", "verdict": "skipped", "error": "agent unavailable"})
            out = Path(tmp) / "bench.md"
            text = render(write_run(tmp, rows), out, today="2026-09-23")
            for heading in ("## Success rate", "### By category", "## Speed and cost", "## Safety",
                            "## Abstention and visual", "## Per task", "## Verdict", "### Where Mobster loses",
                            "## Attempt accounting"):
                self.assertIn(heading, text)
            self.assertTrue(out.exists())
            self.assertIn("| som-pro | 0 | 0 | 0 | 1 | 0 |", text)


if __name__ == "__main__":
    unittest.main()
