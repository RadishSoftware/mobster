"""A refusal that dispatched nothing is recoverable; an unknown outcome is never sent again by the loop."""

import unittest

from ..frontier import SAME_FAILURES_TO_STOP, FrontierAgent
from ..state import Element
from ..wda.guards import DriverRejection
from .test_frontier import Driver, Script, screen

ARCHIVE = "Archive the QuickBite receipt in Mail."


class UndispatchedRecoveryTests(unittest.TestCase):
    def run_failing(self, code, steps, times=1):
        """The model's ``steps`` on a Mail screen whose first ``times`` dispatches the driver refuses with ``code``."""
        row = screen(Element("1", "Archive", "Button", (.4, .1, .2, .05)),
                     Element("2", "QuickBite receipt", "StaticText", (.1, .3, .5, .05)))
        driver = Driver([row] * 12)
        real, sent = driver.execute, []

        def execute(operation, target, snapshot, text=None, timeout=10):
            sent.append(operation)
            if len(sent) <= times:
                raise DriverRejection(code)
            return real(operation, target, snapshot, text, timeout)

        driver.execute = execute
        events = []
        result = FrontierAgent(driver, Script(steps), emit=events.append, screenshots=False,
                               settle_seconds=0).run(ARCHIVE)
        return result, driver, sent, events

    def test_a_pre_dispatch_refusal_is_observed_again_rather_than_ending_the_run(self):
        for code in sorted(DriverRejection.PRE_DISPATCH):
            with self.subTest(code=code):
                result, driver, sent, events = self.run_failing(
                    code, [("TAP", "Archive", None), ("TAP", "Archive", None), ("DONE", None, None),
                           ("DONE", None, None)])
                failed, = [e for e in events if e["event"] == "frontier_failed"]
                self.assertIn(code, failed["error"])
                self.assertEqual(driver.actions, [("TAP", "Archive", None)])
                self.assertEqual(result["status"], "completed")

    def test_an_unknown_outcome_is_never_sent_again_by_the_loop_itself(self):
        for code in ("native_exception_outcome_unknown", "main_thread_deadline_expired_outcome_unknown",
                     "physical_tap_outcome_unknown"):
            with self.subTest(code=code):
                result, driver, sent, events = self.run_failing(
                    code, [("TAP", "Archive", None), ("DONE", None, None), ("DONE", None, None)])
                self.assertEqual(sent, ["TAP"])
                self.assertEqual(driver.actions, [])
                self.assertEqual([e["event"] for e in events].count("frontier_failed"), 1)

    def test_repeated_refusals_are_bounded_and_then_fail_closed(self):
        result, driver, sent, _ = self.run_failing("stale_revision", [("TAP", "Archive", None)] * 6
                                                   + [("DONE", None, None)] * 2, times=99)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(len(sent), SAME_FAILURES_TO_STOP)
        self.assertEqual(driver.actions, [])


if __name__ == "__main__":
    unittest.main()
