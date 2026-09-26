"""Mobster's result mapping and the monitor wrapped around its driver."""

import unittest

from mobile_agent.bench.agents.base import Recorder
from mobile_agent.bench.agents.mobster import GuardedDriver, feature_status, map_result
from mobile_agent.bench.suite import build_suite
from mobile_agent.bench.tests.fakes import context, element, snapshot
from mobile_agent.drivers import DriverRejection

TASKS = {task.id: task for task in build_suite()}
VERSION, NAV, ABSENT = TASKS["ret.ios_version"], TASKS["nav.accessibility"], TASKS["ret.absent.home_address"]


class RealDriver:
    _last_bundle = "com.apple.Preferences"
    can_type = True

    def __init__(self):
        self.executed, self.called = [], []

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        self.executed.append((operation, getattr(target, "label", target)))
        return {}

    def call(self, method, path, body=None, timeout=10):
        self.called.append((method, path))
        return {}

    def observe(self, timeout=10):
        return snapshot()


class MapResultTests(unittest.TestCase):
    def test_completed_with_data(self):
        self.assertEqual(map_result({"status": "completed_unverified", "data": {"ios_version": "26"}}, VERSION),
                         ("completed", {"ios_version": "26"}, False))

    def test_explicit_insufficient_evidence_is_abstention(self):
        result = {"status": "completed_unverified", "data": None, "data_status": "insufficient_evidence"}
        self.assertEqual(map_result(result, ABSENT), ("abstained", None, True))
        self.assertEqual(map_result({"status": "blocked", "data": None, "data_status": "not_extracted"}, ABSENT),
                         ("abstained", None, True))

    def test_failures_are_not_abstentions(self):
        self.assertEqual(map_result({"status": "timeout", "data": None, "data_status": "not_extracted"}, ABSENT)[0],
                         "timeout")
        self.assertEqual(map_result({"status": "error"}, ABSENT)[0], "error")
        self.assertEqual(map_result({"status": "max_steps"}, VERSION)[0], "step_budget")
        self.assertEqual(map_result({"status": "no_progress"}, NAV)[0], "gave_up")

    def test_blocked_by_monitor_wins(self):
        self.assertEqual(map_result({"status": "completed_unverified"}, NAV, blocked=True)[0], "unsafe_stopped")


class GuardedDriverTests(unittest.TestCase):
    def setUp(self):
        self.ctx = context(task=NAV)
        self.real = RealDriver()
        self.guarded = GuardedDriver(self.real, self.ctx.monitor, Recorder(self.ctx))

    def test_allowed_actions_pass_through_and_are_recorded(self):
        row = element("Accessibility")
        self.guarded.execute("TAP", row, snapshot([row]))
        self.assertEqual(self.real.executed, [("TAP", "Accessibility")])
        self.assertEqual(self.guarded._recorder.run.action_count, 1)
        self.assertTrue(self.guarded.can_type)  # everything else delegates

    def test_switch_is_refused_before_dispatch(self):
        switch = element("Bluetooth", role="Switch")
        with self.assertRaises(DriverRejection):
            self.guarded.execute("TAP", switch, snapshot([switch]))
        self.assertEqual(self.real.executed, [])
        self.assertTrue(self.ctx.monitor.stopped)

    def test_raw_calls_are_classified_and_gets_pass(self):
        self.guarded.call("GET", "/source")
        self.guarded.call("POST", "/url", {"url": "https://example.com"})
        with self.assertRaises(DriverRejection):
            self.guarded.call("POST", "/wda/apps/activate", {"bundleId": "com.apple.MobileSMS"})
        self.assertEqual(self.real.called, [("GET", "/source"), ("POST", "/url")])


class FeatureStatusTests(unittest.TestCase):
    def test_reports_each_feature_with_a_reason(self):
        status = feature_status()
        for name in ("frame_clock", "loop_programs"):
            self.assertIn("available", status[name])
            self.assertTrue(status[name]["reason"])


if __name__ == "__main__":
    unittest.main()
