"""No provider/device calls: unchanged WAIT is recoverable, never inferred success."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from mobile_agent.agent import Agent
from mobile_agent.models import Decision
from mobile_agent.state import Element, Snapshot
from mobile_agent.task_policy import ActionSupport, OutputIntent, StopGate


BLANK = Snapshot([Element("result", "Edit field", "SwiftUI.AccessibilityNode",
                          (.1, .1, .8, .1), actions=())],
                 "Last Expression\nEdit field", 430, 932, "synthetic_fixture")


def decision(operation="WAIT", gate=StopGate.CONTINUE):
    return Decision(operation, None, .5, .3, .2, "offline-wait", 0, {}, gate, OutputIntent.TEXT)


class WaitRecoveryTests(unittest.TestCase):
    def ready(self):
        driver = SimpleNamespace(can_type=False, observe=Mock(return_value=BLANK), execute=Mock())
        model = Mock()
        model.decide.return_value = decision()
        helper = SimpleNamespace(calls=0)
        def recover(*_args, **_kwargs):
            helper.calls += 1
            return "The result is not exposed; do not invent it."
        helper.ask = Mock(side_effect=recover)
        return driver, model, helper

    def run_without_sleep(self, driver, model, helper=None, *, max_steps=8,
                          goal="Find the result of 5 x 5 in Calculator", **kwargs):
        with patch("mobile_agent.agent.time.sleep"):
            return Agent(driver, model, helper, max_steps=max_steps, settle_seconds=0, **kwargs).run(
                goal, execute=True, output_format="auto")

    def test_missing_result_recovers_once_instead_of_bypassing_helper_for_26_waits(self):
        driver, model, helper = self.ready()
        model.decide.side_effect = [decision(), decision(), decision(), decision("BLOCKED")]
        events = []
        result = self.run_without_sleep(driver, model, helper, emit=events.append)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["data_status"], "not_extracted")
        self.assertIsNone(result["data"])
        self.assertEqual(helper.calls, 1)
        helper.ask.assert_called_once()
        self.assertEqual(model.decide.call_args.kwargs["hint"], "The result is not exposed; do not invent it.")
        self.assertTrue(any(e.get("reason") == "unchanged_wait" for e in events))
        driver.execute.assert_not_called()

    def test_same_plateau_spends_only_one_recovery_and_retains_run_bound(self):
        driver, model, helper = self.ready()
        result = self.run_without_sleep(driver, model, helper)
        self.assertEqual(result["status"], "max_steps")
        self.assertEqual(model.decide.call_count, 8)
        self.assertEqual(helper.calls, 1)
        self.assertIsNone(result["data"])

    def test_new_content_resets_unchanged_wait_streak(self):
        driver, model, helper = self.ready()
        updated = replace(BLANK, text="Loading another page")
        driver.observe.side_effect = [BLANK, BLANK, updated, updated]
        result = self.run_without_sleep(driver, model, helper, max_steps=4)
        self.assertEqual(result["status"], "max_steps")
        helper.ask.assert_not_called()

    def test_stop_condition_precedes_plateau_recovery(self):
        driver, model, helper = self.ready()
        model.decide.side_effect = [decision(), decision(), decision(gate=StopGate.STOP)]
        result = self.run_without_sleep(driver, model, helper,
                                        goal="Find the result of 5 x 5 in Calculator; stop if it asks to sign in")
        self.assertEqual(result["status"], "user_condition_met")
        helper.ask.assert_not_called()

    def test_idle_plateau_ends_as_a_stall_after_recovery(self):
        driver, model, helper = self.ready()
        model.decide.return_value = replace(decision(), screen_has_loading=.04)
        result = self.run_without_sleep(driver, model, helper, max_steps=30)
        self.assertEqual(result["status"], "no_progress")
        self.assertEqual(helper.ask.call_count, 1)
        self.assertLess(model.decide.call_count, 10)

    def test_loading_plateau_keeps_waiting(self):
        driver, model, helper = self.ready()
        model.decide.return_value = replace(decision(), screen_has_loading=.9)
        result = self.run_without_sleep(driver, model, helper, max_steps=12)
        self.assertEqual(result["status"], "max_steps")

    def test_recovery_hints_are_capped_to_keep_budget_for_text_and_answers(self):
        from mobile_agent.agent import MAX_RECOVERIES
        driver, model, helper = self.ready()
        model.decide.return_value = decision("BLOCKED")
        self.run_without_sleep(driver, model, helper, max_steps=10, max_helper_calls=10)
        self.assertLessEqual(helper.ask.call_count, MAX_RECOVERIES)

    def test_changed_guard_is_not_used_for_plateau_helper(self):
        driver, model, helper = self.ready()
        updated = replace(BLANK, revision="changed")
        driver.observe.side_effect = [BLANK, BLANK, BLANK, updated]
        result = self.run_without_sleep(driver, model, helper, max_steps=3)
        self.assertEqual(result["status"], "max_steps")
        helper.ask.assert_not_called()

    def test_reserved_answer_helper_budget_is_not_consumed(self):
        driver, model, helper = self.ready()
        model.decide.side_effect = [decision(), decision(), decision(), decision("BLOCKED")]
        result = self.run_without_sleep(driver, model, helper, max_helper_calls=1)
        self.assertEqual(result["status"], "blocked")
        helper.ask.assert_not_called()
        self.assertIn("do not compute or invent", model.decide.call_args.kwargs["hint"])

    def test_invalid_recovery_hint_cannot_become_authority(self):
        driver, model, helper = self.ready()
        helper.ask.side_effect = None
        helper.ask.return_value = "x" * 1001
        result = self.run_without_sleep(driver, model, helper)
        self.assertEqual(result["status"], "error")
        self.assertEqual(model.decide.call_count, 3)
        driver.execute.assert_not_called()

    def test_wait_plateau_is_ax_identity_only_and_never_spends_a_visual_capture(self):
        # WAIT recovery is about an unchanged AX plateau. A configured visual
        # reader must not be consulted on that path: cost stays post-no-op only.
        driver, model, helper = self.ready()
        reads = []

        def visual(snapshot, *, timeout):
            reads.append(True)
            raise AssertionError("WAIT must not capture the drawn screen")

        model.decide.side_effect = [decision(), decision(), decision(), decision("BLOCKED")]
        result = self.run_without_sleep(driver, model, helper, visual=visual)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(reads, [])
        self.assertEqual(helper.calls, 1)

    def test_missing_visual_identity_does_not_unblock_a_true_no_op_replay(self):
        # The no-progress guard still holds when a capture is unavailable:
        # fail closed, never treat an absent fingerprint as proof of change.
        # A failed capture records the AX-only pair, so the second tap is refused.
        driver, model, helper = self.ready()
        screen = Snapshot([Element("result", "Edit field", "SwiftUI.AccessibilityNode",
                                   (.1, .1, .8, .1), actions=("TAP",))],
                          "Last Expression\nEdit field", 430, 932, "synthetic_fixture")
        driver.observe = Mock(return_value=screen)
        model.verify_action = Mock(return_value=ActionSupport.ALLOWED)
        model.decide.side_effect = [
            Decision("TAP", "result", .99, .01, .01, "offline-wait", 0, {},
                     StopGate.CONTINUE, OutputIntent.ACTION_ONLY),
            Decision("TAP", "result", .99, .01, .01, "offline-wait", 0, {},
                     StopGate.CONTINUE, OutputIntent.ACTION_ONLY),
        ]
        reads = []

        def visual(snapshot, *, timeout):
            reads.append(True)
            raise RuntimeError("owned_capture_unavailable")

        result = self.run_without_sleep(driver, model, helper, visual=visual, max_steps=4)
        self.assertEqual(driver.execute.call_count, 1)
        self.assertEqual(result["status"], "no_progress")
        self.assertTrue(reads)


if __name__ == "__main__":
    unittest.main()
