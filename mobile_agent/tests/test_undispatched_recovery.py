"""A refusal that dispatched nothing is recoverable; an unknown outcome is not."""

import unittest

from ..agent import Agent
from ..demo import DemoDriver, DemoHelper, DemoModel
from ..drivers import DriverRejection


class UndispatchedRecoveryTests(unittest.TestCase):
    def agent_for(self, driver, **kwargs):
        return Agent(driver, DemoModel(), DemoHelper(), emit=self.events.append, **kwargs)

    def setUp(self):
        self.events = []

    def failing_driver(self, code, times=1):
        driver = DemoDriver()
        real = driver.execute
        calls = {"n": 0}

        def execute(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] <= times:
                raise DriverRejection(code)
            return real(*args, **kwargs)

        driver.execute = execute
        self.calls = calls
        return driver

    def test_a_pre_dispatch_refusal_is_observed_again_rather_than_ending_the_run(self):
        driver = self.failing_driver("stale_revision")
        result = self.agent_for(driver).run("Search for coffee", execute=True,
                                            expected_text="Coffee brewing guide")
        self.assertEqual(result["status"], "expected_text_visible")
        event, = [e for e in self.events if e["event"] == "action_not_dispatched"]
        self.assertEqual((event["native_code"], event["dispatched"]), ("stale_revision", False))
        # A request that never reached app code is not an attempted action.
        self.assertEqual(result["attempted_actions"], len(driver.actions))

    def test_every_pre_dispatch_code_recovers(self):
        for code in sorted(DriverRejection.PRE_DISPATCH):
            with self.subTest(code=code):
                self.events = []
                result = self.agent_for(self.failing_driver(code)).run(
                    "Search for coffee", execute=True, expected_text="Coffee brewing guide")
                self.assertEqual(result["status"], "expected_text_visible")

    def test_an_unknown_outcome_still_ends_the_run_without_repeating_the_action(self):
        for code in ("native_exception_outcome_unknown", "main_thread_deadline_expired_outcome_unknown",
                     "physical_tap_outcome_unknown"):
            with self.subTest(code=code):
                self.events = []
                driver = self.failing_driver(code)
                result = self.agent_for(driver).run("Search for coffee", execute=True)
                self.assertEqual(result["status"], "error")
                self.assertIn("an in-flight action may have run", result["reason"])
                self.assertEqual(self.calls["n"], 1)
                self.assertEqual([e for e in self.events if e["event"] == "action_not_dispatched"], [])

    def test_repeated_refusals_are_bounded_and_then_fail_closed(self):
        driver = self.failing_driver("stale_revision", times=99)
        result = self.agent_for(driver, max_undispatched=2).run("Search for coffee", execute=True)
        self.assertEqual(result["status"], "error")
        self.assertIn("stale_revision", result["reason"])
        self.assertIn("no automatic retry", result["reason"])
        self.assertEqual(len(driver.actions), 0)
        self.assertEqual(len([e for e in self.events if e["event"] == "action_not_dispatched"]), 3)

    def test_the_allowance_is_validated(self):
        for invalid in (-1, 101, 1.5, "4"):
            with self.assertRaises(ValueError):
                Agent(DemoDriver(), DemoModel(), DemoHelper(), max_undispatched=invalid)


if __name__ == "__main__":
    unittest.main()
