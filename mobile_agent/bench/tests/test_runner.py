"""Runner hygiene with fakes: plan, reset, health, resume, budgets, records."""

import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from mobile_agent.bench.agents.base import AgentRun
from mobile_agent.bench.fixtures import Fixtures
from mobile_agent.bench.phone import Health
from mobile_agent.bench.runner import INFRA, SKIPPED, RunHalted, Runner, load_records, make_plan
from mobile_agent.bench.suite import build_suite
from mobile_agent.bench.tests.fakes import FakePhone, FakeProbe, element, snapshot
from mobile_agent.bench.truth import TruthStore

TASKS = {task.id: task for task in build_suite()}


class ScriptedAgent:
    kind = "baseline"

    def __init__(self, name, status="completed", answer=None, available=(True, "ok"), sleep=0):
        self.name, self.status, self.answer, self._available = name, status, answer, available
        self.runs = []

    def config(self):
        return {"agent": self.name}

    def available(self):
        return self._available

    def run(self, task, ctx):
        self.runs.append(task.id)
        return AgentRun(status=self.status, answer=self.answer, decision_steps=2, action_count=2,
                        model_calls=2, cost_usd=.01)


@contextmanager
def no_lease(url):
    yield


def runner(tmp, agents, tasks, *, health=None, probe=None, repeats=2, truth=None):
    probe = probe or FakeProbe(start=snapshot([element("Bluetooth", value="On")], title="Settings"),
                               final=snapshot(title="Accessibility"))
    return Runner(tasks=tasks, agents=agents, out_dir=Path(tmp) / "run", wda_url="http://127.0.0.1:8100",
                  device="iphone15pro", truth=truth or TruthStore("iphone15pro", Path(tmp) / "truth.json"),
                  fixtures=Fixtures(), repeats=repeats, seed=7, yield_to=None,
                  probe_factory=lambda url: probe, phone_factory=lambda url, session: FakePhone(),
                  health=health or (lambda url: Health(True, False, "s")), lease_factory=no_lease,
                  log=lambda *a: None, sleep=lambda s: None)


class PlanTests(unittest.TestCase):
    def test_every_triple_once_and_seeded(self):
        plan = make_plan(["a", "b", "c"], ["x", "y"], 3, seed=1)
        self.assertEqual(len(plan), 18)
        self.assertEqual(len({attempt.key for attempt in plan}), 18)
        self.assertEqual(plan, make_plan(["a", "b", "c"], ["x", "y"], 3, seed=1))
        orders = {tuple(a.task for a in plan if a.repeat == r and a.agent == "x") for r in range(3)}
        self.assertGreater(len(orders), 1, "task order is shuffled per repeat")


class RunnerTests(unittest.TestCase):
    def test_grades_records_and_resumes(self):
        tasks = [TASKS["nav.accessibility"], TASKS["state.bluetooth"]]
        with tempfile.TemporaryDirectory() as tmp:
            agent = ScriptedAgent("x", answer={"bluetooth": "on"})
            r = runner(tmp, {"x": agent}, tasks)
            r.run("hash")
            records = load_records(r.records_path)
            self.assertEqual(len(records), 4)
            by_task = {rec["task"]: rec for rec in records}
            # nav: title read by the probe; bluetooth: truth captured at reset from the start screen.
            self.assertEqual(by_task["state.bluetooth"]["captured"], {"settings.bluetooth": "On"})
            self.assertEqual(by_task["state.bluetooth"]["verdict"], "pass")
            self.assertIn(by_task["nav.accessibility"]["verdict"], ("pass", "fail"))
            # Resume: nothing left to do.
            again = runner(tmp, {"x": ScriptedAgent("x")}, tasks)
            again.run("hash")
            self.assertEqual(len(load_records(r.records_path)), 4)
            with self.assertRaises(SystemExit):
                runner(tmp, {"x": agent}, tasks).run("different-hash")

    def test_unavailable_agent_is_skipped_not_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = runner(tmp, {"x": ScriptedAgent("x", available=(False, "no model"))}, [TASKS["web.heading"]],
                       repeats=1)
            r.run("hash")
            self.assertEqual(load_records(r.records_path)[0]["verdict"], SKIPPED)

    def test_locked_phone_halts(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = runner(tmp, {"x": ScriptedAgent("x")}, [TASKS["web.heading"]],
                       health=lambda url: Health(False, True, reason="phone is locked"))
            with self.assertRaises(RunHalted):
                r.run("hash")

    def test_infrastructure_failures_are_retried_on_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = runner(tmp, {"x": ScriptedAgent("x")}, [TASKS["web.heading"]], repeats=1,
                       probe=FakeProbe(fail_reset=True))
            r.halt_after_failures = 5
            r.run("hash")
            self.assertEqual(load_records(r.records_path)[0]["verdict"], INFRA)
            ok = runner(tmp, {"x": ScriptedAgent("x", answer={"heading": "Example Domain"})},
                        [TASKS["web.heading"]], repeats=1,
                        probe=FakeProbe(final=snapshot(bundle="com.apple.mobilesafari")))
            ok.run("hash")
            verdicts = [rec["verdict"] for rec in load_records(ok.records_path)]
            self.assertEqual(verdicts, [INFRA, "pass"])

    def test_late_answers_are_timeouts(self):
        task = TASKS["web.heading"]
        clock = iter([0, 0, 10, 10 + task.max_seconds + 5, 10 + task.max_seconds + 5, 999, 999, 999])
        with tempfile.TemporaryDirectory() as tmp:
            r = runner(tmp, {"x": ScriptedAgent("x", answer={"heading": "Example Domain"})}, [task], repeats=1,
                       probe=FakeProbe(final=snapshot(bundle="com.apple.mobilesafari")))
            r.clock = lambda: next(clock)
            r.run("hash")
            record = load_records(r.records_path)[0]
            self.assertEqual(record["status"], "timeout")
            self.assertEqual(record["verdict"], "fail")

    def test_missing_fixture_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = runner(tmp, {"x": ScriptedAgent("x")}, [TASKS["ret.note_code"]], repeats=1)
            r.fixture_status["notes.bench"] = (False, "missing")
            r.run("hash")
            self.assertEqual(load_records(r.records_path)[0]["verdict"], SKIPPED)

    def test_plan_header_records_seed_and_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = runner(tmp, {"x": ScriptedAgent("x")}, [TASKS["web.heading"]], repeats=1)
            r.run("abc")
            header = json.loads(r.plan_path.read_text())
            self.assertEqual((header["suite_hash"], header["seed"]), ("abc", 7))


class YieldTests(unittest.TestCase):
    def test_app_not_running_means_idle(self):
        from mobile_agent.bench.runner import app_idle
        self.assertTrue(app_idle("http://127.0.0.1:9", wait_seconds=0, sleep=lambda s: None))


if __name__ == "__main__":
    unittest.main()
