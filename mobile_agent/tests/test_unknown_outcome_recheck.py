import unittest

from mobile_agent.effect_ledger import EffectLedger
from mobile_agent.frontier import UNKNOWN_REASON, FrontierAgent
from mobile_agent.frontier.safety import outcome_unknown
from mobile_agent.state import Element
from mobile_agent.tests import test_frontier as F
from mobile_agent.transport import WDA_ERRORS, NotSent, StaleSession, TransportError, answer_error
from mobile_agent.wda.guards import DriverRejection

NEXT = Element("1", "Next", "Button", (.6, .9, .3, .05))
SEND = Element("1", "Send", "Button", (.8, .9, .15, .05))


def timing_out(screens, failures=1, lands=False):
    driver = F.Driver(screens)
    real, sent = driver.execute, []

    def execute(operation, target, snapshot, text=None, timeout=10):
        sent.append((operation, getattr(target, "label", target), text))
        if len(sent) <= failures:
            if lands:
                driver.index += 1
            raise TimeoutError("Operation deadline exceeded")
        return real(operation, target, snapshot, text, timeout)

    driver.execute = execute
    return driver, sent


def run(driver, request, steps, **kwargs):
    events = []
    result = FrontierAgent(driver, F.Script(list(steps) + [("DONE", None, None)] * 3), emit=events.append,
                           screenshots=False, settle_seconds=0, **kwargs).run(request)
    return result, events


def prompts(events):
    return [e["text"] for e in events if e["event"] == "frontier_prompt"]


def answered(status, name=None):
    return TransportError(f"HTTP {status}; request not retried", status=status, wda_error=name)


def rejecting(screens, error, lands=False):
    driver = F.Driver(screens)
    real, sent = driver.execute, []

    def execute(operation, target, snapshot, text=None, timeout=10):
        sent.append((operation, getattr(target, "label", target), text))
        if len(sent) == 1:
            if lands:
                driver.index += 1
            raise error
        return real(operation, target, snapshot, text, timeout)

    driver.execute = execute
    return driver, sent


class FailedAnswers(unittest.TestCase):
    """WDA's failed answer can follow part of an action, so it's an unknown outcome and held like one."""

    def test_a_failed_answer_is_unknown_on_an_action_that_held_back(self):
        for error in (answered(400, "invalid element state"), answered(500), answered(404),
                      answered(404, "stale element reference"), answered(500, "unknown error"),
                      answered(400, "timeout"),
                      TransportError("WDA rejected command; not retried", status=200,
                                     wda_error="invalid element state")):
            for op in ("TAP", "LONG_PRESS", "TYPE", "TYPE_SUBMIT", "SUBMIT"):
                self.assertTrue(outcome_unknown(error, op), (op, str(error), error.wda_error))

    def test_a_name_that_shows_wda_refused_first_stays_known(self):
        for name in ("invalid argument", "invalid selector", "invalid coordinates", "unknown command", "unknown method",
                     "unsupported operation", "no such alert", "no such element"):
            self.assertFalse(outcome_unknown(answered(WDA_ERRORS[name], name), "TAP"), name)

    def test_that_name_with_any_other_status_is_unknown(self):
        """WDA pairs each name with one status: another pairing isn't WDA's refusal, so it proves nothing."""
        for name in ("invalid argument", "no such element", "unsupported operation"):
            for status in (200, 400, 404, 500):
                if status != WDA_ERRORS[name]:
                    self.assertTrue(outcome_unknown(answered(status, name), "TAP"), (name, status))

    def test_typing_is_several_requests_so_no_name_clears_it(self):
        for op in ("TYPE", "TYPE_SUBMIT", "SET_TEXT"):
            self.assertTrue(outcome_unknown(answered(400, "invalid argument"), op), op)

    def test_only_wdas_own_names_are_kept(self):
        from mobile_agent.transport import error_name
        self.assertEqual(error_name({"error": "invalid argument"}), "invalid argument")
        for other in ("Jane Doe", "jane doe", "Invalid Argument", "invalid  argument", "invalid argument ", "", None, 7,
                      "invalid argument\u00a0"):
            self.assertIsNone(error_name({"error": other}), other)

    def test_an_action_that_doesnt_hold_back_keeps_its_reading(self):
        for op in ("LAUNCH_APP", "SWIPE_UP", None):
            self.assertFalse(outcome_unknown(answered(400, "invalid element state"), op), op)

    def test_errors_with_no_answer(self):
        self.assertTrue(outcome_unknown(TransportError("HTTP 400; WDA rejected command; not retried"), "TAP"))
        self.assertFalse(outcome_unknown(NotSent("HTTP ConnectionRefusedError; nothing sent; not retried"), "TAP"))
        self.assertFalse(outcome_unknown(StaleSession("HTTP 404 StaleSession: invalid session id; nothing sent"),
                                         "TAP"))
        self.assertTrue(outcome_unknown(TransportError("HTTP TimeoutError; request outcome unknown; not retried"),
                                        "TAP"))
        self.assertTrue(outcome_unknown(TimeoutError("late"), "SWIPE_UP"))
        self.assertTrue(outcome_unknown(DriverRejection("native_exception_outcome_unknown"), "TAP"))

    def test_only_a_short_name_comes_through_from_wdas_answer(self):
        self.assertEqual(answer_error(b'{"value": {"error": "stale element reference", "message": "private text"}}'),
                         "stale element reference")
        for raw in (b"not json", b'{"value": "x"}', b'{"value": {"error": 7}}', b'{"value": {"error": "<script>"}}',
                    b'{"value": {"error": "' + b"a" * 65 + b'"}}', b"[]"):
            self.assertIsNone(answer_error(raw), raw)

    def test_a_tap_that_answered_an_error_and_landed_isnt_sent_again(self):
        off = Element("1", "Airplane Mode", "Switch", (.1, .3, .8, .05), value="0")
        on = Element("1", "Airplane Mode", "Switch", (.1, .3, .8, .05), value="1")
        driver, sent = rejecting([F.screen(off)] + [F.screen(on)] * 8, answered(500), lands=True)
        _, events = run(driver, "Turn on Airplane Mode.", [("TAP", "Airplane Mode", None)] * 2)
        self.assertEqual(sent, [("TAP", "Airplane Mode", None)])
        self.assertIn("outcome_unknown", [e.get("label") for e in events if e["event"] == "frontier_refused"])

    def test_typing_that_answered_an_error_and_partly_landed_isnt_typed_again(self):
        actions = ("TAP", "TYPE", "TYPE_SUBMIT")
        empty = Element("1", "Note", "TextField", (.1, .3, .8, .05), True, value="", actions=actions)
        partial = Element("1", "Note", "TextField", (.1, .3, .8, .05), True, value="eggs an", actions=actions)
        driver, sent = rejecting([F.screen(empty)] + [F.screen(partial)] * 8, answered(400, "invalid element state"),
                                 lands=True)
        _, events = run(driver, "Write eggs and milk.", [("TYPE", "Note", "eggs and milk")] * 2)
        self.assertEqual(sent, [("TYPE", "Note", "eggs and milk")])
        self.assertIn("outcome_unknown", [e.get("label") for e in events if e["event"] == "frontier_refused"])

    def test_a_consequential_tap_that_answered_an_error_never_goes_again_on_its_own(self):
        driver, sent = rejecting([F.screen(SEND)] * 8, answered(404))
        run(driver, "Send it.", [("TAP", "Send", None)] * 2)
        self.assertEqual(sent, [("TAP", "Send", None)])

    def test_a_refusal_wda_named_first_can_be_tried_again(self):
        driver, sent = rejecting([F.screen(NEXT)] * 8, answered(400, "invalid argument"))
        _, events = run(driver, "Go to the next step.", [("TAP", "Next", None)] * 2)
        self.assertEqual(sent, [("TAP", "Next", None)] * 2)
        self.assertNotIn("outcome_unknown", [e.get("label") for e in events if e["event"] == "frontier_refused"])
        self.assertEqual([e["outcome"] for e in events if e["event"] == "frontier_failed"], ["failed"])


class UnknownOutcomeRecheckTests(unittest.TestCase):
    def test_a_timed_out_next_is_sent_once_more_when_the_screen_is_unchanged(self):
        driver, sent = timing_out([F.screen(NEXT, Element("2", "Step 1 of 3", "StaticText", (.1, .1, .8, .05)))] * 8)
        _, events = run(driver, "Go to the next step.", [("TAP", "Next", None), ("TAP", "Next", None)])
        self.assertEqual(sent, [("TAP", "Next", None)] * 2)
        self.assertEqual([e["reason"] for e in events if e["event"] == "frontier_retry"], ["outcome_unknown_unchanged"])
        self.assertIn("The screen looks unchanged, so it probably didn't happen", prompts(events)[1])

    def test_a_timed_out_tap_is_not_sent_again_when_the_control_changed(self):
        off = Element("1", "Airplane Mode", "Switch", (.1, .3, .8, .05), value="0")
        on = Element("1", "Airplane Mode", "Switch", (.1, .3, .8, .05), value="1")
        driver, sent = timing_out([F.screen(off)] + [F.screen(on)] * 8, lands=True)
        _, events = run(driver, "Turn on Airplane Mode.", [("TAP", "Airplane Mode", None)] * 2)
        self.assertEqual(sent, [("TAP", "Airplane Mode", None)])
        self.assertIn("outcome_unknown", [e.get("label") for e in events if e["event"] == "frontier_refused"])
        self.assertIn("took effect", prompts(events)[2])

    def test_a_new_sheet_over_the_same_screen_counts_as_changed(self):
        sheet = F.screen(NEXT, Element("3", "Allow notifications?", "Alert", (.1, .4, .8, .2)))
        driver, sent = timing_out([F.screen(NEXT)] + [sheet] * 8, lands=True)
        run(driver, "Go to the next step.", [("TAP", "Next", None), ("TAP", "Next", None)])
        self.assertEqual(sent, [("TAP", "Next", None)])

    def test_a_retry_whose_outcome_is_unknown_too_is_not_sent_a_third_time(self):
        driver, sent = timing_out([F.screen(NEXT)] * 8, failures=2)
        run(driver, "Go to the next step.", [("TAP", "Next", None)] * 3)
        self.assertEqual(sent, [("TAP", "Next", None)] * 2)

    def test_after_an_answered_retry_a_later_unknown_gets_its_own_retry(self):
        steps = [F.screen(NEXT, Element("2", f"Step {n} of 3", "StaticText", (.1, .1, .8, .05))) for n in (1, 2, 3)]
        driver = F.Driver(steps)
        real, sent = driver.execute, []

        def execute(operation, target, snapshot, text=None, timeout=10):
            sent.append((operation, target.label, text))
            if len(sent) in (1, 3):
                raise TimeoutError("Operation deadline exceeded")
            return real(operation, target, snapshot, text, timeout)

        driver.execute = execute
        run(driver, "Go through the steps.", [("TAP", "Next", None)] * 4)
        self.assertEqual(sent, [("TAP", "Next", None)] * 4)
        self.assertEqual(driver.index, 2)

    def test_a_timed_out_send_asks_again_instead_of_retrying(self):
        driver, sent = timing_out([F.screen(SEND, bundle="com.apple.MobileSMS")] * 8)
        asked, answers = [], ["approved", "denied"]
        result, _ = run(driver, "Send the message.", [("TAP", "Send", None), ("TAP", "Send", None)],
                        approve=lambda request: asked.append(request) or answers.pop(0))
        self.assertEqual(sent, [("TAP", "Send", None)])
        self.assertEqual((asked[-1]["title"], asked[-1]["reason"]), ("Send again?", UNKNOWN_REASON))
        self.assertEqual(result["status"], "approval_denied")

    def test_a_timed_out_send_goes_again_only_on_the_persons_yes(self):
        driver, sent = timing_out([F.screen(SEND, bundle="com.apple.MobileSMS")] * 8)
        asked = []
        run(driver, "Send the message.", [("TAP", "Send", None), ("TAP", "Send", None)],
            approve=lambda request: asked.append(request) or "approved")
        self.assertEqual(sent, [("TAP", "Send", None)] * 2)
        self.assertEqual(asked[-1]["reason"], UNKNOWN_REASON)

    def test_a_timed_out_send_is_refused_when_nobody_can_answer(self):
        driver, sent = timing_out([F.screen(SEND, bundle="com.apple.MobileSMS")] * 8)
        _, events = run(driver, "Send the message.", [("TAP", "Send", None), ("TAP", "Send", None)])
        self.assertEqual(sent, [("TAP", "Send", None)])
        self.assertIn("nobody can give it here", prompts(events)[2])

    def test_a_tap_on_a_form_holding_typed_text_asks_again(self):
        email = Element("0", "Email", "TextField", (.1, .2, .8, .05), True, value="sam@example.com",
                        actions=("TAP", "TYPE", "TYPE_SUBMIT"))
        driver, sent = timing_out([F.screen(email, NEXT)] * 8)
        asked = []
        run(driver, "Go to the next step.", [("TAP", "Next", None), ("TAP", "Next", None)],
            approve=lambda request: asked.append(request) or "denied")
        self.assertEqual(sent, [("TAP", "Next", None)])
        self.assertEqual([r["reason"] for r in asked], [UNKNOWN_REASON])

    def test_typed_text_is_not_typed_again_when_the_field_now_holds_it(self):
        empty = Element("0", "Message", "TextField", (.1, .8, .7, .05), True, actions=("TAP", "TYPE", "TYPE_SUBMIT"))
        holding = Element("0", "Message", "TextField", (.1, .8, .7, .05), True, value="On my way",
                          actions=("TAP", "TYPE", "TYPE_SUBMIT"))
        driver, sent = timing_out([F.screen(empty)] + [F.screen(holding)] * 8, lands=True)
        run(driver, "Type On my way in the message field.", [("TYPE", "Message", "On my way"),
                                                            ("TYPE", "Message", "On my way")])
        self.assertEqual(sent, [("TYPE", "Message", "On my way")])

    def test_typing_that_left_the_field_as_it_was_is_typed_once_more(self):
        empty = Element("0", "Message", "TextField", (.1, .8, .7, .05), True, actions=("TAP", "TYPE", "TYPE_SUBMIT"))
        driver, sent = timing_out([F.screen(empty)] * 8)
        run(driver, "Type On my way in the message field.", [("TYPE", "Message", "On my way")] * 2)
        self.assertEqual(sent, [("TYPE", "Message", "On my way")] * 2)

    def test_a_refusal_before_dispatch_still_frees_the_action_without_a_look(self):
        driver = F.Driver([F.screen(SEND, bundle="com.apple.MobileSMS")] * 8)
        real, sent = driver.execute, []

        def execute(operation, target, snapshot, text=None, timeout=10):
            sent.append(operation)
            if len(sent) == 1:
                raise DriverRejection("stale_revision")
            return real(operation, target, snapshot, text, timeout)

        driver.execute = execute
        run(driver, "Send the message.", [("TAP", "Send", None), ("TAP", "Send", None)])
        self.assertEqual(sent, ["TAP", "TAP"])


def page(n, *extra):
    return F.screen(NEXT, Element("2", f"Step {n} of 3", "StaticText", (.1, .1, .8, .05)), *extra)


class PerScreenTests(unittest.TestCase):

    def test_the_next_page_s_next_runs_after_page_one_s_next_timed_out_but_happened(self):
        driver, sent = timing_out([page(1), page(2), page(3)], lands=True)
        _, events = run(driver, "Go through the steps.", [("TAP", "Next", None)] * 2)
        self.assertEqual(sent, [("TAP", "Next", None)] * 2)
        self.assertEqual(driver.index, 2)
        self.assertNotIn("outcome_unknown", [e.get("label") for e in events if e["event"] == "frontier_refused"])
        self.assertIn("The screen changed after it, so Mobster counts it as done", prompts(events)[1])

    def test_page_one_s_next_is_still_refused_when_that_screen_comes_back(self):
        back = Element("3", "Back", "Button", (.05, .05, .2, .05))
        driver, sent = timing_out([page(1), page(2, back), page(1)] + [page(1)] * 6, lands=True)
        _, events = run(driver, "Go through the steps.",
                        [("TAP", "Next", None), ("TAP", "Back", None), ("TAP", "Next", None)])
        self.assertEqual(sent, [("TAP", "Next", None), ("TAP", "Back", None)])
        self.assertIn("outcome_unknown", [e.get("label") for e in events if e["event"] == "frontier_refused"])
        self.assertIn("took effect", prompts(events)[3])

    def test_typing_stays_held_on_the_field_when_the_screen_changes_around_it(self):
        started = Element("0", "Message", "TextField", (.1, .8, .7, .05), True, value="See you",
                          actions=("TAP", "TYPE", "TYPE_SUBMIT"))
        holding = Element("0", "Message", "TextField", (.1, .8, .7, .05), True, value="See you soon",
                          actions=("TAP", "TYPE", "TYPE_SUBMIT"))
        before = F.screen(started, Element("4", "7 of 160", "StaticText", (.1, .75, .3, .03)))
        after = F.screen(holding, Element("4", "12 of 160", "StaticText", (.1, .75, .3, .03)))
        driver, sent = timing_out([before] + [after] * 8, lands=True)
        run(driver, "Add soon to the message.", [("TYPE", "Message", "soon"), ("TYPE", "Message", "soon")])
        self.assertEqual(sent, [("TYPE", "Message", "soon")])


class LedgerSettleTests(unittest.TestCase):
    def test_held_lists_only_blocking_effects_the_match_accepts(self):
        ledger = EffectLedger()
        ledger.record_outcome("TAP", None, outcome="unknown", effect=("app", "TAP", "Next", "a"), context={"pairs": 1})
        ledger.record_outcome("TAP", None, outcome="not_dispatched", effect=("app", "TAP", "Next", "b"))
        self.assertEqual(ledger.held(lambda key, record: key[1] == "TAP"), [("app", "TAP", "Next", "a")])
        self.assertEqual(ledger.held(lambda key, record: record["context"]["pairs"] == 2), [])

    def test_settle_and_resolve(self):
        ledger, key = EffectLedger(), ("app", "TAP", "Next", None)
        ledger.record_outcome("TAP", None, outcome="unknown", effect=key, context={"screen": "a"})
        self.assertTrue(ledger.would_duplicate("TAP", None, effect=key))
        self.assertEqual(ledger.lookup("TAP", None, effect=key)["context"], {"screen": "a"})
        self.assertEqual(ledger.settle("TAP", None, effect=key, happened=False), "observed_absent")
        self.assertFalse(ledger.would_duplicate("TAP", None, effect=key))
        ledger.record_outcome("TAP", None, outcome="unknown", effect=key)
        self.assertEqual(ledger.settle("TAP", None, effect=key, happened=False), "unknown")
        self.assertTrue(ledger.would_duplicate("TAP", None, effect=key))
        ledger.resolve("TAP", None, effect=key)
        self.assertFalse(ledger.would_duplicate("TAP", None, effect=key))

    def test_a_settled_effect_stays_blocked(self):
        ledger, key = EffectLedger(), ("app", "TAP", "Next", None)
        ledger.record_outcome("TAP", None, outcome="unknown", effect=key)
        self.assertEqual(ledger.settle("TAP", None, effect=key, happened=True), "observed_change")
        self.assertEqual(ledger.settle("TAP", None, effect=key, happened=False), "observed_change")
        self.assertTrue(ledger.would_duplicate("TAP", None, effect=key))


if __name__ == "__main__":
    unittest.main()
