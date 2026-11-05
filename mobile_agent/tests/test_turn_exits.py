import os
import unittest
from unittest.mock import patch

from mobile_agent.agent_hooks import GuardVerdict
from mobile_agent.frontier import KEYPAD_REFUSAL, PENDING_REASON, RESUME_AFTER_SECONDS, FrontierAgent, prompt_text
from mobile_agent.permissions import ASK_IMPORTANT, PermissionGate, policy
from mobile_agent.state import Element
from mobile_agent.tests import test_frontier as F
from mobile_agent.tests import test_frontier_act as D
from mobile_agent.tests import test_frontier_lock_loop as L
from mobile_agent.tests import test_frontier_skills as S
from mobile_agent.tests import test_native_capabilities as N
from mobile_agent.tests import test_point_tap as P
from mobile_agent.tests import test_unknown_outcome_recheck as U
from mobile_agent.tests.fakes import Clock
from mobile_agent.wda import system_dialogs as sd

CLEAN = P.CLEAN


class Model:

    def __init__(self, *turns, items=(), answer="Done."):
        self.turns, self.items, self.answer, self.prompts = list(turns), list(items), answer, []
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "calls": 0}

    def complete(self, messages, schema, timeout=60, **kwargs):
        if "items" in schema["properties"]:
            return {"items": [dict(i) for i in self.items]}, {}
        self.usage["calls"] += 1
        prompt = prompt_text(messages)
        self.prompts.append(prompt)
        rows = prompt.split("Screen elements:\n", 1)[1].splitlines() if "Screen elements:\n" in prompt else []
        turn = self.turns.pop(0) if len(self.turns) > 1 else self.turns[0]
        actions = []
        for index, step in enumerate(turn):
            if isinstance(step, dict):
                actions.append({"operation": "TAP", "target": None, "target_label": None, "text": None, "app": None,
                                **step})
                continue
            op, label, text = step
            target = next((r.split()[0] for r in rows if label and f'"{label}"' in r), None)
            actions.append({"operation": op, "target": target if index == 0 else None,
                            "target_label": label if index else (label if target is None else None), "text": text,
                            "app": None})
        ends = (turn[-1]["operation"] if isinstance(turn[-1], dict) else turn[-1][0]) in ("DONE", "BLOCKED")
        return {"thought": "", "plan": "", "notes_add": [], "checklist_updates": [], "actions": actions,
                "answer": self.answer if ends else None}, {}


def commit(act, quote, what="the draft"):
    return {"kind": "COMMIT", "app": "Mail", "what": what, "payload": "", "act": act, "count": "1", "condition": "",
            "quote": quote}


def run(driver, model, request, **kwargs):
    events = []
    kwargs.setdefault("settle_seconds", 0)
    with patch.dict(os.environ, dict(CLEAN), clear=True):
        result = FrontierAgent(driver, model, emit=events.append, screenshots=False, **kwargs).run(request)
    return result, events


def refused(events):
    return [e.get("label") for e in events if e["event"] == "frontier_refused"]


SAVE = Element("1", "Save", "Button", (.6, .9, .3, .05))
SAVE_ITEM = commit("other", "Save the draft")
MAIL = {"com.example.mail": "Mail"}


class Dropping(F.Driver):

    capabilities = F.Driver.capabilities | {"recover"}

    def __init__(self, before, after):
        super().__init__([before] * 8)
        self.after, self.sent, self.recovered = after, [], 0

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        self.sent.append((operation, getattr(target, "label", target)))
        raise RuntimeError("ConnectionRefusedError: the runner went away")

    def recover(self, timeout=None):
        self.recovered += 1
        self.screens = [self.after] * 8


class SafetyExitTests(unittest.TestCase):
    def test_a_dropped_connection_that_comes_back_without_the_control_is_not_sent_again(self):
        before = F.screen(Element("1", "Archive", "Button", (.4, .1, .2, .05)))
        driver = Dropping(before, F.screen(Element("2", "Inbox", "StaticText", (.1, .1, .5, .05))))
        model = Model([("TAP", "Archive", None)], [("DONE", None, None)])
        _, events = run(driver, model, "Archive the QuickBite receipt in Mail.")
        self.assertEqual((driver.sent, driver.recovered), ([("TAP", "Archive")], 1))
        self.assertEqual([(e["event"], e["outcome"]) for e in events if e["event"] == "frontier_failed"],
                         [("frontier_failed", "unknown")])
        self.assertIn("Feedback on your last action: The connection to the phone dropped during this action and was "
                      "restored: it may or may not have happened.", model.prompts[1])
        self.assertIn("TAP '' failed (RuntimeError)", model.prompts[1])

    def test_an_unplugged_phone_stops_the_run_at_once_and_the_action_is_not_sent_again(self):
        driver = Dropping(F.screen(Element("1", "Archive", "Button", (.4, .1, .2, .05))), F.screen())
        guard, model = L.Guard(GuardVerdict("ready"), attached=False), Model([("TAP", "Archive", None)])
        result, _ = run(driver, model, "Archive the QuickBite receipt in Mail.", guard=guard)
        self.assertEqual((result["status"], result["code"]), ("blocked", "unplugged"))
        self.assertEqual(result["reason"], "Your iPhone was unplugged. Plug it back in and try again.")
        self.assertEqual((driver.sent, driver.recovered, model.usage["calls"]), ([("TAP", "Archive")], 0, 1))

    def test_a_cable_plugged_back_in_resumes_the_run_and_the_action_is_handled_as_unknown(self):
        # A task a person started: the guard waits for the cable (await_return), the run attaches to the restarted
        # runner and goes on in the same task. The action whose answer was lost is not sent blind: the run looks first.
        class Waiting(L.Guard):
            def __init__(self):
                super().__init__(GuardVerdict("ready"), attached=False)
                self.waited = 0

            def await_return(self, driver, kind="unplugged", midrun=True):
                self.waited += 1
                self._attached = True
                return GuardVerdict("ready")
        archive = Element("1", "Archive", "Button", (.4, .1, .2, .05))
        driver = Dropping(F.screen(archive), F.screen(Element("2", "Inbox", "StaticText", (.1, .1, .5, .05))))
        guard, model = Waiting(), Model([("TAP", "Archive", None)], [("DONE", None, None)])
        result, events = run(driver, model, "Archive the QuickBite receipt in Mail.", guard=guard)
        self.assertEqual((guard.waited, driver.recovered), (1, 1))
        self.assertNotEqual(result.get("code"), "unplugged")
        self.assertEqual(driver.sent, [("TAP", "Archive")])       # not sent again: its control is gone
        self.assertGreaterEqual(model.usage["calls"], 2)            # the same task went on to its next turn
        self.assertTrue(any(e["event"] == "frontier_wda_recovery" and e["ok"] for e in events))

    def test_an_approved_action_is_not_sent_when_the_phone_unlocked_on_a_different_screen(self):
        # The person took longer than RESUME_AFTER_SECONDS to answer; the guard unlocked the phone, and the control the
        # card asked about is no longer on screen.
        row = Element("0", "Block this Caller", "Cell", (0, .6, .995, .05))
        driver = F.Driver([F.screen(row, bundle="com.apple.mobilephone")] * 10)
        clock, guard, asked = Clock(), L.Guard(GuardVerdict("unlocked")), []

        def asker(request_):
            asked.append(request_)
            clock.now += RESUME_AFTER_SECONDS + 1
            driver.screens = [F.screen(Element("5", "Recents", "Button", (.1, .9, .2, .05)),
                                       bundle="com.apple.mobilephone")] * 10
            return "approved"
        model = Model([("TAP", "Block this Caller", None)], [("DONE", None, None)])
        with patch("time.monotonic", clock):
            run(driver, model, "Block this caller", approve=asker, guard=guard, apps={"com.apple.mobilephone": "Phone"},
                permissions=PermissionGate(ASK_IMPORTANT, apps=policy.ALLOW_ALL))
        self.assertEqual(([r["label"] for r in asked], driver.actions), (["Block this Caller"], []))
        self.assertIn("resume", guard.causes)
        self.assertIn("Feedback on your last action: The screen changed while Mobster waited for your OK: look again.",
                      model.prompts[1])
    def test_a_declared_commit_whose_first_send_timed_out_is_not_sent_again_with_nobody_to_ask(self):
        # The control is neutral, so the earlier attempt would be retried; the task's own contract calls it a commit.
        driver, sent = U.timing_out([F.screen(SAVE)] * 8)
        model = Model([("TAP", "Save", None)], [("TAP", "Save", None)], [("DONE", None, None)], items=[SAVE_ITEM])
        _, events = run(driver, model, "Save the draft.", apps=MAIL)
        self.assertEqual(sent, [("TAP", "Save", None)])
        self.assertEqual(refused(events), ["outcome_unknown"])
        self.assertIn("Feedback on your last action: Refused: your earlier TAP 'Save' has an unknown outcome: it may "
                      "have happened. Sending it again needs the user's OK, and nobody can give it here.",
                      model.prompts[2])
        self.assertIn("TAP 'Save' refused (its earlier outcome was unknown)", model.prompts[2])

    def test_another_control_for_an_act_whose_outcome_is_unknown_is_refused_with_nobody_to_ask(self):
        other = Element("2", "Save a copy", "Button", (.6, .8, .3, .05))
        driver, sent = U.timing_out([F.screen(SAVE, other)] * 8)
        model = Model([("TAP", "Save", None)], [("TAP", "Save a copy", None)], [("DONE", None, None)],
                      items=[SAVE_ITEM])
        _, events = run(driver, model, "Save the draft.", apps=MAIL)
        self.assertEqual(sent, [("TAP", "Save", None)])
        self.assertEqual(refused(events), ["outcome_unknown"])
        self.assertIn("Feedback on your last action: Refused: an earlier other for this request has an unknown "
                      "outcome: it may have happened. Doing it again needs the user's OK, and nobody can give it here.",
                      model.prompts[2])
        self.assertIn("TAP 'Save a copy' refused (an earlier attempt's outcome is unknown)", model.prompts[2])

    def test_an_approved_point_tap_is_not_sent_when_stop_came_while_asking(self):
        stop = []

        def asker(request_):
            stop.append(True)  # the person pressed Stop, then answered
            return "approved"
        phone, model, events = P.Phone([P.feed()] * 4), P.Model([dict(P.LIKE_ITEM)]), []
        with patch.dict(os.environ, dict(CLEAN), clear=True):
            result = FrontierAgent(phone, model, apps={"com.example.pixgram": "Pixgram"}, emit=events.append,
                                   screenshots=False, settle_seconds=0, approve=asker,
                                   cancelled=lambda: bool(stop)).run(P.LIKE)
        self.assertEqual((result["status"], phone.taps), ("stopped", []))
        self.assertEqual(model.usage["calls"], 1)


class OtherExitTests(unittest.TestCase):
    def test_a_run_whose_clock_runs_out_during_an_action_ends_with_a_timeout_before_the_next_decision(self):
        clock, archive = Clock(), Element("1", "Archive", "Button", (.4, .1, .2, .05))
        driver = F.Driver([F.screen(archive), F.screen(Element("2", "Inbox", "StaticText", (.1, .1, .5, .05)))] * 4)
        real = driver.execute

        def slow(operation, target, snapshot, text=None, timeout=10):
            clock.now += 10_000  # the phone took longer than the run's whole budget
            return real(operation, target, snapshot, text, timeout)
        driver.execute = slow
        model = Model([("TAP", "Archive", None)], [("DONE", None, None)])
        with patch("time.monotonic", clock):
            result, _ = run(driver, model, "Archive the QuickBite receipt in Mail.", max_seconds=300)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(driver.actions, [("TAP", "Archive", None)])  # the second call only writes the report
        self.assertEqual(result["answer"], "Done.")

    def test_a_system_dialog_nobody_answers_ends_the_run_before_the_model_is_asked(self):
        phone, model = N.AlertPhone(N.NOTIFY), Model([("DONE", None, None)])
        screen = F.screen(Element("1", "Lesson 1", "Button", (.1, .4, .8, .06)), bundle="com.duolingo")
        phone.observe = phone.observe_ready = lambda timeout=10: screen
        result, events = run(phone, model, "Start lesson 1.", apps={"com.duolingo": "Duolingo"}, dialogs=True,
                             permission_mode=sd.ASK_IMPORTANT, dialog_approve=lambda request: "timeout")
        self.assertEqual(result["status"], "approval_timeout")
        self.assertEqual((result["reason"], model.usage["calls"], phone.pressed()),
                         ("Nobody answered iOS's question, so it is still on your iPhone", 0, []))
        self.assertIn("system_dialog_shown", [e["event"] for e in events])

    def test_a_point_tap_the_person_redirects_is_not_tapped_and_the_instruction_is_read_next(self):
        phone, model, asked, _ = P.run(answer="redirected:Open the Search tab instead")
        self.assertEqual((phone.taps, len(asked)), ([], 1))
        self.assertIn(
            "The user declined the tap at that spot and asked instead: 'Open the Search tab instead'. Do that "
                      "now.", model.prompts[1])
        self.assertIn("TAP_XY 'Close' declined; user redirected", model.prompts[1])
        self.assertIn("The user's later instruction: Open the Search tab instead", model.prompts[1])

    def test_a_skill_the_person_redirects_is_not_performed_and_the_instruction_is_read_next(self):
        order = []
        _, model, _, _ = S.run(order, approve=lambda request: "redirected:Use the code from the second text")
        self.assertEqual([o[0] for o in order], ["prepare"])
        self.assertIn("The user declined USE_CODE and asked instead: 'Use the code from the second text'. Do that now.",
                      model.prompts[1])
        self.assertIn("USE_CODE declined; user redirected", model.prompts[1])

    def test_the_first_tap_on_the_passcode_keypad_is_refused_and_the_guard_asked_once(self):
        phone, guard = L.Phone(first=L.KEYPAD), L.Guard(GuardVerdict("ready"))
        model = L.Model([("TAP", "1", None)], [("BLOCKED", None, None)], answer="Locked.")
        frontier, events = L.agent(phone, model, guard)
        result = frontier.run("Open Reminders.")
        self.assertEqual(phone.actions, [])
        self.assertEqual(guard.causes, ["lock_suspected"])
        self.assertIn(KEYPAD_REFUSAL, model.prompts[1])
        self.assertIn("T0: TAP refused (passcode screen)", model.prompts[1])
        self.assertEqual(refused(events), ["passcode_keypad"])
        self.assertEqual(result["status"], "blocked")

    def test_a_swipe_on_a_row_that_is_not_on_screen_stops_the_sequence(self):
        phone = F.Driver([F.screen(Element("1", "Inbox", "StaticText", (.1, .1, .5, .04)))] * 4)
        model = Model([("SWIPE_LEFT", "Ghost row", None)], [("DONE", None, None)])
        _, events = run(phone, model, "Show the delete button on Ghost row.")
        self.assertEqual(phone.actions, [])
        self.assertEqual([e["reason"] for e in events if e["event"] == "frontier_chunk_stop"][0], "target_missing")
        self.assertIn("SWIPE_LEFT target 'Ghost row' not on screen; stopped the sequence", model.prompts[1])

    def test_typing_nothing_is_refused_with_the_sentence_that_names_set_text(self):
        field = Element("1", "Email", "TextField", (.1, .2, .8, .05), True, actions=("TAP", "TYPE", "TYPE_SUBMIT"))
        phone = F.Driver([F.screen(field)] * 4)
        model = Model([("TYPE", "Email", "")], [("DONE", None, None)])
        run(phone, model, "Fill in the email field.")
        self.assertEqual(phone.actions, [])
        self.assertIn('Feedback on your last action: TYPE needs text. SET_TEXT with text "" empties a field.',
                      model.prompts[1])

    def test_dismiss_is_refused_once_every_point_was_tried_on_an_unchanged_screen(self):
        sheet = F.screen(Element("1", "Options", "Sheet", (.1, .3, .8, .4)))
        phone = F.Driver([sheet] * 12)
        model = Model(*[[("DISMISS", None, None)]] * 8, [("DONE", None, None)])
        _, events = run(phone, model, "Close the options sheet.", max_steps=20)
        self.assertEqual([a[0] for a in phone.actions].count("TAP_POINT"), 7)
        self.assertIn("dismiss_exhausted", refused(events))
        self.assertTrue(any("Refused: tapping outside was tried everywhere and closed nothing. Use the overlay's own "
                            "controls (Close, Cancel, Done, Back) or choose an item in it." in p
                            for p in model.prompts))
        self.assertTrue(any("DISMISS refused (tried every point)" in p for p in model.prompts))

    def test_a_tap_whose_target_is_gone_once_the_results_settled_stops_the_sequence(self):
        row = Element("r", "Bench Tester", "Cell", (0, .3, 1, .06))

        class Fading(D.ReadsDriver):
            shown = 0

            def observe(self, timeout=10):
                if not self.actions:
                    return D.contacts(typed="")
                self.shown += 1
                return D.contacts(row) if self.shown <= 1 else D.contacts()

        driver, events = Fading([D.contacts()]), []
        script = F.Script([[("TYPE_SUBMIT", "Search", "Bench"), ("TAP", "Bench Tester", None)], ("DONE", None, None),
                           ("DONE", None, None)])
        with patch("time.sleep"), patch("mobile_agent.frontier.act.SUBMIT_SETTLE_SECONDS", 0.0):
            FrontierAgent(driver, script, emit=events.append, screenshots=False, settle_seconds=0).run(
                "In Contacts, open Bench Tester and report the company.")
        self.assertNotIn(("TAP", "Bench Tester", None), driver.actions)
        self.assertIn(("target_missing", "Bench Tester"), [(e["reason"], e.get("wanted")) for e in events
                                                          if e["event"] == "frontier_chunk_stop"])
        self.assertTrue(any("TAP target 'Bench Tester' gone once the results settled; stopped the sequence"
                            in e["text"] for e in events if e["event"] == "frontier_prompt"))

    def test_a_declared_commit_that_changed_nothing_asks_again_with_the_pending_warning(self):
        driver = F.Driver([F.screen(SAVE)] * 8)
        gate, asked = P.Gate(), []
        model = Model([("TAP", "Save", None)], [("TAP", "Save", None)], [("DONE", None, None)], items=[SAVE_ITEM])
        run(driver, model, "Save the draft.", apps=MAIL, permissions=gate,
            approve=lambda request: asked.append(request) or "denied")
        self.assertEqual([r["reason"] for r in asked], [PENDING_REASON])
        self.assertEqual(driver.actions, [("TAP", "Save", None)])  # the first went out; the second waited for a yes

    def test_a_pending_commit_with_no_one_to_ask_is_refused_not_sent_again(self):
        driver = F.Driver([F.screen(SAVE)] * 8)
        model = Model([("TAP", "Save", None)], [("TAP", "Save", None)], [("DONE", None, None)], items=[SAVE_ITEM])
        _, events = run(driver, model, "Save the draft.", apps=MAIL, permissions=P.Gate())
        self.assertEqual(driver.actions, [("TAP", "Save", None)])
        self.assertEqual(refused(events), ["needs_approval"])

    def test_a_swipe_that_removed_its_row_from_the_list_ends_the_run_blocked(self):
        row = Element("3", "Receipt from QuickBite", "Cell", (0, .3, 1, .08))
        other = Element("4", "Newsletter", "Cell", (0, .4, 1, .08))
        driver = F.Driver([F.screen(row, other, bundle="com.example.mail")]
                          + [F.screen(other, bundle="com.example.mail")] * 8)
        model = Model([("SWIPE_LEFT", "Receipt from QuickBite", None)], [("DONE", None, None)])
        result, events = run(driver, model, "Look at the Receipt from QuickBite row.", apps=MAIL)
        self.assertEqual((result["status"], result["code"]), ("blocked", "unexpected_change"))
        self.assertIn("removed it from the list in Mail", result["reason"])
        self.assertIn("row_gone", refused(events))

    def test_a_swipe_whose_row_is_still_there_goes_on(self):
        row = Element("3", "Receipt from QuickBite", "Cell", (0, .3, 1, .08))
        driver = F.Driver([F.screen(row, bundle="com.example.mail")] * 8)
        model = Model([("SWIPE_LEFT", "Receipt from QuickBite", None)], [("DONE", None, None)])
        result, events = run(driver, model, "Look at the Receipt from QuickBite row.", apps=MAIL)
        self.assertNotIn("row_gone", refused(events))
        self.assertNotEqual(result.get("code"), "unexpected_change")


if __name__ == "__main__":
    unittest.main()
