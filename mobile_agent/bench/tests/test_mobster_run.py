"""The Mobster adapter drives the REAL Agent class end to end on the offline demo fixture."""

import unittest
from unittest import mock

from mobile_agent.bench.agents.mobster import MobsterAgent
from mobile_agent.bench.suite import build_suite
from mobile_agent.bench.tests.fakes import context
from mobile_agent.demo import DemoDriver, DemoHelper, DemoModel, screen
from mobile_agent.state import Element, Snapshot

TASKS = {task.id: task for task in build_suite()}


class WdaLikeDemo(DemoDriver):
    _last_bundle = "com.apple.Preferences"

    def __init__(self, home=None):
        super().__init__()
        self.home = home
        self.calls = []

    def observe(self, timeout=10):
        if self.home is not None and self.stage == "home":
            return self.home
        return screen(self.stage)

    def call(self, method, path, body=None, timeout=10):
        self.calls.append((method, path))
        return {}


class Lease:
    @classmethod
    def device(cls, identity):
        return cls()

    def close(self):
        pass


def run(task, driver):
    agent = MobsterAgent()
    with mock.patch("mobile_agent.compose.build_target_driver", return_value=driver), \
            mock.patch("mobile_agent.compose.build_models", return_value=(DemoModel(), DemoHelper())), \
            mock.patch("mobile_agent.journal.Lease", Lease):
        return agent.run(task, context(task=task, seconds=30))


class MobsterRunTests(unittest.TestCase):
    def test_real_agent_completes_and_is_measured(self):
        task = TASKS["nav.accessibility"]
        driver = WdaLikeDemo()
        result = run(task, driver)
        self.assertEqual(result.status, "completed", result.reason)
        self.assertEqual(driver.actions, ["TAP", "TYPE"])
        self.assertEqual(result.action_count, 2)
        self.assertGreaterEqual(result.decision_steps, 2)
        self.assertIsNotNone(result.first_action_ms)
        self.assertEqual(result.detail["mobster_status"], "completed_unverified")
        self.assertIn(("POST", "/wda/apps/activate"), driver.calls)

    def test_a_switch_tap_is_refused_and_the_run_stops(self):
        task = TASKS["nav.accessibility"]
        home = Snapshot([Element("0", "Airplane Mode", "Switch", (.8, .06, .1, .05))], "Airplane Mode",
                        402, 874, "synthetic_fixture")
        driver = WdaLikeDemo(home)
        result = run(task, driver)
        self.assertEqual(result.status, "unsafe_stopped")
        self.assertEqual(driver.actions, [])


if __name__ == "__main__":
    unittest.main()
