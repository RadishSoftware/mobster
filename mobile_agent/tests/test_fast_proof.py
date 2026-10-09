"""The Fast engine as the run view reads it: approvals that name the act, plain-words steps, proof and outcome.

Offline: fixture drivers and models stand in for the phone and Jev.
"""

import io
import unittest
from dataclasses import replace

from mobile_agent import fast_proof
from mobile_agent.agent import Agent
from mobile_agent.demo import DemoDriver, DemoHelper, DemoModel
from mobile_agent.drivers import Driver
from mobile_agent.models import Decision
from mobile_agent.state import Element, Snapshot
from mobile_agent.task_policy import ActionSupport, OutputIntent, RiskTier, StopGate


def snap(elements, bundle="com.apple.reminders"):
    return Snapshot(elements, "\n".join(e.label or e.value for e in elements), 402, 874, "synthetic_fixture",
                    bundle_id=bundle)


class RemindersDriver(Driver):
    """A list with New Reminder; the form's Title field; Done saves the row."""
    can_type = True

    def __init__(self):
        self.stage, self.title, self.actions = "list", "", []

    def observe(self, timeout=10):
        if self.stage == "list":
            rows = [Element("r", self.title, "StaticText", (.05, .3, .9, .05))] if self.title else []
            return snap([Element("0", "New Reminder", "Button", (.05, .9, .4, .05)), *rows])
        field = Element("0", "Title", "TextField", (.05, .2, .9, .05), True, value=self.typed,
                        actions=("TAP", "TYPE"))
        return snap([field, Element("1", "Done", "Button", (.8, .06, .15, .05))])

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        self.actions.append((operation, target.label if target else None))
        if self.stage == "list" and operation == "TAP":
            self.stage, self.typed = "form", ""
        elif self.stage == "form" and operation == "TYPE":
            self.typed = text
        elif self.stage == "form" and operation == "TAP":
            self.stage, self.title = "list", self.typed

    def close(self):
        pass


def decision(operation, target=None, *, text=None, goal=.01, risk=.01, tier=RiskTier.NAVIGATION.value, intent=None):
    return Decision(operation, target, .98, goal, .01, "fixture", 0, {}, StopGate.CONTINUE, intent,
                    risk_tier=tier, side_effect_risk=risk, text=text)


class RemindersModel:
    """Jev as the audit saw it: the New Reminder tap scored as a likely side effect."""

    def __init__(self):
        self.checks = 0

    def verify_action(self, snapshot, goal, operation, target, history, **kwargs):
        self.checks += 1
        return ActionSupport.ALLOWED

    def decide(self, snapshot, goal, history, **kwargs):
        intent = OutputIntent.ACTION_ONLY if kwargs.get("classify_output") else None
        labels = {e.label: e for e in snapshot.elements}
        if "New Reminder" in labels and any(e.role == "StaticText" for e in snapshot.elements):
            return decision("DONE", goal=.99, intent=intent)
        if "New Reminder" in labels:
            return decision("TAP", "0", risk=.95, tier=RiskTier.SIDE_EFFECT.value, intent=intent)
        if not labels["Title"].value:
            return decision("TYPE", "0", text="Buy oat milk", tier=RiskTier.TYPE.value, intent=intent)
        return decision("TAP", "1", risk=.4, tier=RiskTier.SIDE_EFFECT.value, intent=intent)


class MessagesDriver(Driver):
    """A thread with a composer and Send; a sent message shows as a row."""
    can_type = True

    def __init__(self):
        self.typed, self.sent, self.actions = "", [], []

    def observe(self, timeout=10):
        rows = [Element(f"m{i}", text, "StaticText", (.3, .2 + i * .06, .6, .05)) for i, text in enumerate(self.sent)]
        composer = Element("c", "Message", "TextField", (.05, .88, .7, .05), True, value=self.typed,
                           actions=("TAP", "TYPE"))
        header = [Element("n", "Messages", "NavigationBar", (0, .06, 1, .11)),
                  Element("b", "Messages", "Button", (.04, .06, .1, .05)),
                  Element("p", "Contact photo", "Button", (.43, .06, .15, .07)),
                  Element("h", "Sam", "Button", (.26, .13, .48, .04))]
        return snap([*header, *rows, composer, Element("s", "Send", "Button", (.8, .88, .15, .05))],
                    "com.apple.MobileSMS")

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        self.actions.append((operation, target.label if target else None))
        if operation == "TYPE":
            self.typed = text
        elif operation == "TAP" and target.label == "Send":
            self.sent.append(self.typed)
            self.typed = ""

    def close(self):
        pass


class AppendingMessagesDriver(MessagesDriver):
    """As the phone does it: TYPE adds keystrokes to what the composer already holds; clear_text empties it."""

    def __init__(self):
        super().__init__()
        self.cleared = 0

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        if operation == "TYPE":
            self.actions.append((operation, target.label))
            self.typed += text
            return
        super().execute(operation, target, snapshot, text, timeout)

    def clear_text(self, target, timeout=10):
        assert target.label == "Message"
        self.cleared += 1
        self.typed = ""


class MessagesModel:
    """Types the message the request (or the user's redirect) names, then taps Send."""

    def verify_action(self, *args, **kwargs):
        return ActionSupport.ALLOWED

    def decide(self, snapshot, goal, history, **kwargs):
        intent = OutputIntent.ACTION_ONLY if kwargs.get("classify_output") else None
        wanted = "I'm ten minutes late" if "ten minutes" in goal else "I'm running late"
        composer = next(e for e in snapshot.elements if e.id == "c")
        if wanted in [e.label for e in snapshot.elements if e.role == "StaticText"]:
            return decision("DONE", goal=.99, intent=intent)
        if composer.value != wanted:
            return decision("TYPE", "c", text=wanted, tier=RiskTier.TYPE.value, intent=intent)
        return decision("TAP", "s", risk=.9, tier=RiskTier.SIDE_EFFECT.value, intent=intent)



def jpeg():
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (590, 1278), (40, 40, 40)).save(out, "JPEG")
    return out.getvalue()


class FakeClock:
    """A healthy FrameClock whose video holds one JPEG frame."""

    def __init__(self):
        frame = ("seq", "image/jpeg", jpeg())
        self.video = type("Video", (), {"latest": staticmethod(lambda: frame)})()

    def still_for(self):
        return 1.0


class FastApprovalTests(unittest.TestCase):
    def test_tapping_new_reminder_never_asks_and_the_reminder_is_saved(self):
        driver, requests, events = RemindersDriver(), [], []
        result = Agent(driver, RemindersModel(), DemoHelper(), approve=lambda r: requests.append(r) or "approved",
                       emit=events.append).run("Add a reminder to buy oat milk", execute=True)
        self.assertEqual(requests, [])
        self.assertEqual(driver.actions, [("TAP", "New Reminder"), ("TYPE", "Title"), ("TAP", "Done")])
        self.assertEqual(result["status"], "completed_unverified")
        # The saved row reads back what was typed: that is the proof, so the run is done.
        self.assertEqual(result["outcome"], "done")
        self.assertEqual(result["proof"][0]["quote"], "Buy oat milk")
        self.assertEqual([e["text"] for e in events if e["event"] == "step"],
                         ["Tapped New Reminder", "Typed into Title", "Tapped Done", "Found Buy oat milk"])

    def test_send_in_a_message_task_asks_once_with_the_exact_text(self):
        driver, requests = MessagesDriver(), []
        result = Agent(driver, MessagesModel(), DemoHelper(),
                       approve=lambda r: requests.append(r) or "approved").run("Text Sam I'm running late", execute=True)
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual((request["kind"], request["act"], request["title"], request["text"]),
                         ("commit", "send", "Send this message to Sam?", "I'm running late"))
        self.assertEqual(request["target"], {"x": .8, "y": .88, "w": .15, "h": .05})
        self.assertEqual(driver.sent, ["I'm running late"])
        self.assertEqual(result["outcome"], "done")

    def test_a_redirect_declines_the_send_and_continues_with_the_instruction(self):
        driver, requests, events = MessagesDriver(), [], []
        answers = iter(["redirected:say ten minutes late", "approved"])
        result = Agent(driver, MessagesModel(), DemoHelper(), emit=events.append,
                       approve=lambda r: requests.append(r) or next(answers)).run("Text Sam I'm running late",
                                                                                   execute=True)
        self.assertEqual([r["text"] for r in requests], ["I'm running late", "I'm ten minutes late"])
        self.assertEqual(driver.sent, ["I'm ten minutes late"])
        self.assertEqual(result["status"], "completed_unverified")
        redirected = [e for e in events if e["event"] == "approval_redirected"]
        self.assertEqual(len(redirected), 1)
        self.assertNotIn("ten minutes", str(redirected))  # the user's words stay out of the event log

    def test_a_redirect_replaces_text_the_composer_already_holds(self):
        # The live failure (sim 4, 26 Sep): the composer still held the declined words, so typing the
        # new ones would have appended; the action check refused it twice and the run ended blocked.
        class Checker(MessagesModel):
            def verify_action(self, snapshot, goal, operation, target, history, text=None, **kwargs):
                if operation == "TYPE" and target.value:
                    return ActionSupport.MISMATCH  # appending to other text is not what was asked
                return ActionSupport.ALLOWED

        driver, requests, events = AppendingMessagesDriver(), [], []
        answers = iter(["redirected:say ten minutes late", "approved"])
        result = Agent(driver, Checker(), DemoHelper(), emit=events.append,
                       approve=lambda r: requests.append(r) or next(answers)).run("Text Sam I'm running late",
                                                                                   execute=True)
        self.assertEqual([r["text"] for r in requests], ["I'm running late", "I'm ten minutes late"])
        self.assertEqual([r["title"] for r in requests], ["Send this message to Sam?"] * 2)
        self.assertEqual(driver.cleared, 1)
        self.assertEqual(driver.sent, ["I'm ten minutes late"])
        self.assertEqual((result["status"], result["outcome"]), ("completed_unverified", "done"))
        self.assertEqual(result["proof"][0]["quote"], "I'm ten minutes late")
        self.assertIn("Cleared Message", [e["text"] for e in events if e["event"] == "step"])
        self.assertTrue(next(e for e in events if e["event"] == "approval_redirected")["cleared"])

    def test_after_a_redirect_a_refused_retype_goes_to_the_user_not_to_couldnt_finish(self):
        # Live (27 Sep): the thread already showed the new words once, and the check refused typing them.
        class Refuses(MessagesModel):
            def verify_action(self, snapshot, goal, operation, target, history, text=None, **kwargs):
                composer = next(e for e in snapshot.elements if e.id == "c")
                return (ActionSupport.MISMATCH if "ten minutes" in (text or "") or "ten minutes" in composer.value
                        else ActionSupport.ALLOWED)

        driver, requests, events = AppendingMessagesDriver(), [], []
        answers = iter(["redirected:say ten minutes late", "approved", "approved"])
        result = Agent(driver, Refuses(), DemoHelper(), emit=events.append,
                       approve=lambda r: requests.append(r) or next(answers)).run("Text Sam I'm running late",
                                                                                   execute=True)
        self.assertEqual([(r["kind"], r["operation"], r["text"]) for r in requests],
                         [("commit", "TAP", "I'm running late"), ("unsure", "TYPE", "I'm ten minutes late"),
                          ("commit", "TAP", "I'm ten minutes late")])
        self.assertEqual(driver.sent, ["I'm ten minutes late"])
        self.assertEqual(result["outcome"], "done")
        self.assertEqual([e["operation"] for e in events if e["event"] == "redirect_mismatch_put_to_user"], ["TYPE", "TAP"])

    def test_a_message_the_person_approved_that_shows_as_sent_finishes_the_task(self):
        # Live (27 Sep): after the approved Send, Jev only ever proposed WAIT, and the run ended no_progress.
        class NeverDone(MessagesModel):
            def decide(self, snapshot, goal, history, **kwargs):
                made = super().decide(snapshot, goal, history, **kwargs)
                return decision("WAIT", intent=made.output_intent) if made.operation == "DONE" else made

        driver, events = MessagesDriver(), []
        result = Agent(driver, NeverDone(), DemoHelper(), emit=events.append,
                       approve=lambda r: "approved").run("Text Sam I'm running late", execute=True)
        self.assertEqual(driver.sent, ["I'm running late"])
        self.assertEqual((result["status"], result["outcome"]), ("completed_unverified", "done"))
        self.assertEqual(result["answer"], "Sent “I'm running late” to Sam.")
        self.assertIn("approved_message_sent", [e["event"] for e in events])

        class Unsendable(MessagesDriver):
            def execute(self, operation, target, snapshot, text=None, timeout=10):
                if operation == "TAP" and target.label == "Send":
                    self.actions.append((operation, "Send"))
                    return  # the tap lands, nothing goes out: the words stay in the composer
                super().execute(operation, target, snapshot, text, timeout)

        # A request that may go on after the message ("then ...") is not finished by the message alone.
        result = Agent(MessagesDriver(), NeverDone(), DemoHelper(), max_steps=6, approve=lambda r: "approved").run(
            "Text Sam I'm running late, then delete the draft and open Mail", execute=True)
        self.assertNotEqual(result["outcome"], "done")

        result = Agent(Unsendable(), NeverDone(), DemoHelper(), max_steps=6,
                       approve=lambda r: "approved").run("Text Sam I'm running late", execute=True)
        self.assertNotEqual(result["outcome"], "done")

    def test_a_redirected_action_proposed_again_is_asked_again_not_refused(self):
        class Stubborn(MessagesModel):
            def decide(self, snapshot, goal, history, **kwargs):
                return super().decide(snapshot, goal.split("\n")[0], history, **kwargs)

        answers = iter(["redirected:say ten minutes late", "denied"])
        requests = []
        result = Agent(MessagesDriver(), Stubborn(), DemoHelper(),
                       approve=lambda r: requests.append(r) or next(answers)).run("Text Sam I'm running late", execute=True)
        self.assertEqual(len(requests), 2)
        self.assertEqual(result["status"], "approval_denied")

    def test_an_empty_redirect_is_a_decline(self):
        driver = MessagesDriver()
        result = Agent(driver, MessagesModel(), DemoHelper(), approve=lambda r: "redirected:  ").run(
            "Text Sam I'm running late", execute=True)
        self.assertEqual((result["status"], result["outcome"], driver.sent), ("approval_denied", "declined", []))

    def test_an_uncertain_step_asks_as_unsure(self):
        class Unsure(RemindersModel):
            def verify_action(self, *args, **kwargs):
                return ActionSupport.UNCLEAR

        requests = []
        Agent(RemindersDriver(), Unsure(), DemoHelper(), approve=lambda r: requests.append(r) or "approved").run(
            "Look at my reminders", execute=True)
        self.assertEqual(requests[0]["kind"], "unsure")
        self.assertEqual(requests[0]["label"], "New Reminder")
        self.assertIsNone(requests[0]["act"])


class FrameTests(unittest.TestCase):
    def run_demo(self, capture):
        driver, events = DemoDriver(), []
        driver.frame_clock = FakeClock()
        agent = Agent(driver, DemoModel(), DemoHelper(), emit=events.append)
        agent.capture_frames = capture
        agent.run("Search coffee", execute=True)
        return [e for e in events if e["event"] == "step"], events

    def test_frames_ride_on_steps_only_when_the_server_asks(self):
        steps, events = self.run_demo(True)
        self.assertTrue(steps)
        for step in steps:
            self.assertIsInstance(step["_frame"], bytes)
            self.assertTrue(step["_frame"].startswith(b"\xff\xd8"))
            from PIL import Image
            self.assertLessEqual(max(Image.open(io.BytesIO(step["_frame"])).size), fast_proof.FRAME_SIDE)
        steps, events = self.run_demo(False)
        self.assertTrue(steps)
        self.assertFalse(any("_frame" in event for event in events))

    def test_an_action_whose_settle_timed_out_is_still_a_step(self):
        class Flaky(DemoDriver):
            failed = False

            def observe(self, timeout=10):
                if self.actions and not self.failed:
                    self.failed = True
                    raise TimeoutError("settle read timed out")
                return super().observe(timeout)

        events = []
        Agent(Flaky(), DemoModel(), DemoHelper(), emit=events.append).run("Search coffee", execute=True)
        names = [e["event"] for e in events]
        self.assertIn("step_interrupted", names)
        steps = [e for e in events if e["event"] == "step"]
        self.assertEqual(steps[0]["text"], "Tapped Search")
        self.assertLess(names.index("step"), names.index("step_interrupted"))

    def test_steps_are_numbered_and_in_plain_words(self):
        steps, _ = self.run_demo(False)
        self.assertEqual([s["n"] for s in steps], list(range(1, len(steps) + 1)))
        self.assertEqual(steps[0]["text"], "Tapped Search")
        self.assertEqual(steps[1]["text"], "Typed into Search")


class ProofTests(unittest.TestCase):
    def test_a_finished_task_without_proof_is_check_not_done(self):
        class Elsewhere(DemoModel):
            pass

        driver = DemoDriver()
        result = Agent(driver, Elsewhere(), type("Helper", (DemoHelper,), {"ask": lambda self, *a, **k: "zzz"})()).run(
            "Search zzz", execute=True)
        self.assertEqual(result["status"], "completed_unverified")
        self.assertEqual((result["proof"], result["outcome"]), ([], "check"))

    def screen(self, goal, elements, typed=(), bundle="com.apple.reminders"):
        class State:
            step_paths = {}

        proof = fast_proof.screen_proof(State(), goal, list(typed), snap(elements, bundle), 3)
        return proof, fast_proof.outcome("completed_unverified", proof)

    def test_an_unsaved_reminder_is_check(self):
        # PM probe case 1: the title still in the editor; the nav title "Reminders" is no proof.
        proof, outcome = self.screen("Add a reminder called Pick up dry cleaning", [
            Element("n", "Reminders", "NavigationBar", (0, .06, 1, .05)),
            Element("t", "Title", "TextField", (.05, .2, .9, .05), True, value="Pick up dry cleaning"),
            Element("d", "Done", "Button", (.8, .06, .15, .05)),
            Element("b", "New Reminder", "Button", (.05, .9, .4, .05))], ["Pick up dry cleaning"])
        self.assertEqual((proof, outcome), ([], "check"))

    def test_a_saved_reminder_is_done_with_a_sentence(self):
        proof, outcome = self.screen("Add a reminder called Pick up dry cleaning", [
            Element("n", "Reminders", "NavigationBar", (0, .06, 1, .05)),
            Element("r", "Pick up dry cleaning", "StaticText", (.1, .3, .8, .05)),
            Element("b", "New Reminder", "Button", (.05, .9, .4, .05))], ["Pick up dry cleaning"])
        self.assertEqual((proof[0]["quote"], outcome), ("Pick up dry cleaning", "done"))
        self.assertEqual(fast_proof.done_sentence(proof, "Reminders"), "“Pick up dry cleaning” now shows in Reminders.")

    def test_dark_mode_left_off_is_check_and_turned_on_is_done(self):
        # PM probe case 2: "Dark Mode: Off" proved "Turn on Dark Mode".
        def dark(value):
            return self.screen("Turn on Dark Mode", [
                Element("n", "Display & Brightness", "NavigationBar", (0, .06, 1, .05)),
                Element("s", "Dark Mode", "Switch", (.05, .3, .9, .05), value=value)], bundle="com.apple.Preferences")

        self.assertEqual(dark("0"), ([], "check"))
        proof, outcome = dark("1")
        self.assertEqual((proof[0]["quote"], outcome), ("Dark Mode: On", "done"))
        self.assertEqual(fast_proof.done_sentence(proof, "Settings"), "Dark Mode is on in Settings.")
        # A request that names no direction proves nothing by a switch either way.
        proof, outcome = self.screen("Look at Dark Mode", [Element("s", "Dark Mode", "Switch", (.05, .3, .9, .05), value="1")])
        self.assertEqual(outcome, "check")
        self.assertEqual(fast_proof.requested_switch("Turn Wi-Fi off"), "0")

    def test_an_unsent_message_is_check(self):
        # PM probe case 3: the words still in the composer, the recipient's name as the header.
        proof, outcome = self.screen("Text Sam I'm running late", [
            Element("n", "Sam", "StaticText", (0, .06, 1, .05)),
            Element("m", "Message", "TextField", (.05, .8, .7, .05), True, value="I'm running late"),
            Element("s", "Send", "Button", (.8, .8, .1, .05))], ["Sam", "I'm running late"], "com.apple.MobileSMS")
        self.assertEqual((proof, outcome), ([], "check"))
        # Only the recipient reading back (the header) is no proof of a sent message either.
        proof, outcome = self.screen("Text Sam I'm running late", [
            Element("n", "Sam", "NavigationBar", (0, .06, 1, .05)),
            Element("m", "Message", "TextField", (.05, .8, .7, .05), True, value="")],
            ["Sam", "I'm running late"], "com.apple.MobileSMS")
        self.assertEqual((proof, outcome), ([], "check"))

    def test_a_sent_message_is_done_though_its_bubble_is_an_editable_text_view(self):
        # Live Messages on sim 4: a sent bubble is an editable TextView holding the words; its cell is not.
        composer = ("", "Message", "I am running ten minutes late")
        sent = [Element("m", "Message", "TextField", (.2, .58, .66, .05), True, value="iMessage"),
                Element("c", "Your iMessage, I am running ten minutes late, 12:03 AM", "Cell", (0, .34, 1, .05)),
                Element("b", "CKBalloonTextView", "TextView", (.37, .34, .57, .05), True,
                        value="I am running ten minutes late")]
        proof, outcome = self.screen("Send the message I am running ten minutes late", sent, [composer],
                                     "com.apple.MobileSMS")
        self.assertEqual((proof[0]["quote"], outcome), ("I am running ten minutes late", "done"))
        # The same words still in the composer (an earlier bubble says them too): not sent, so check.
        unsent = [replace(sent[0], value="I am running ten minutes late"), *sent[1:]]
        proof, outcome = self.screen("Send the message I am running ten minutes late", unsent, [composer],
                                     "com.apple.MobileSMS")
        self.assertEqual((proof, outcome), ([], "check"))

    def test_a_words_match_alone_is_never_proof(self):
        proof, outcome = self.screen("Open the Reminders app and look at Groceries", [
            Element("n", "Reminders", "NavigationBar", (0, .06, 1, .05)),
            Element("g", "Groceries", "Button", (.05, .3, .9, .05))])
        self.assertEqual((proof, outcome), ([], "check"))

    def test_the_recipient_comes_from_the_conversation_header(self):
        header = [Element("n", "Messages", "NavigationBar", (0, .06, 1, .11)),
                  Element("b", "Messages", "Button", (.04, .06, .1, .05)),
                  Element("p", "Contact photo", "Button", (.43, .06, .15, .07)),
                  Element("h", "+1 (555) 564-8583", "Button", (.26, .13, .48, .04))]
        send = Element("s", "Send", "Button", (.85, .59, .1, .03))
        under = [Element("e", "iMessage  Encrypted", "StaticText", (.04, .19, .92, .04)),
                 Element("d", "Today 12:00 AM", "StaticText", (.04, .24, .92, .01))]
        self.assertEqual(fast_proof.recipient(snap([*header, *under, send], "com.apple.MobileSMS"), send),
                         "+1 (555) 564-8583")
        to = Element("t", "To:", "TextField", (.1, .12, .8, .04), True, value="Zara Okonkwo")
        self.assertEqual(fast_proof.recipient(snap([to, send]), send), "Zara Okonkwo")
        # Two names at the top: it does not guess.
        two = [*header, Element("x", "Alex", "Button", (.3, .1, .3, .03))]
        self.assertIsNone(fast_proof.recipient(snap([*two, send]), send))

    def test_cited_answers_become_proof_with_their_place(self):
        class State:
            step_paths = {0: "General", 2: "General › About"}

        entries = [{"id": "e1", "text": "26.4", "label_context": "iOS Version", "field": "value",
                    "bundle_id": "com.apple.Preferences", "step": 3, "last_seen_step": 3}]
        proof = fast_proof.cited_proof(State(), entries, [{"path": "/", "evidence_id": "e1", "quote": "26.4"}])
        self.assertEqual(proof, [{"quote": "iOS Version 26.4", "app": "com.apple.Preferences",
                                  "screen": "General › About", "step": 3}])

    def test_a_bare_value_is_quoted_with_the_label_on_its_row(self):
        class State:
            step_paths = {}

        row = "iOS Version, 26.4 iOS Version 26.4"
        entries = [{"id": "e9", "text": "iOS Version", "label_context": "iOS Version", "field": "label",
                    "bundle_id": "b", "row_context": row, "rect": [.09, .23, .2, .03], "last_seen_step": 2},
                   {"id": "e10", "text": "26.4", "label_context": "26.4", "field": "label", "bundle_id": "b",
                    "row_context": row, "rect": [.77, .23, .09, .03], "last_seen_step": 2}]
        proof = fast_proof.cited_proof(State(), entries, [{"evidence_id": "e10"}])
        self.assertEqual(proof[0]["quote"], "iOS Version 26.4")

    def test_the_result_carries_its_cost_when_calls_were_priced(self):
        from mobile_agent.costs import SpendLedger
        ledger = SpendLedger()
        ledger.add_event({"event": "inference_finished", "cost_nanodollars": 1_500_000, "cost_basis": "published_rate",
                          "estimated_usd": .0015, "success": True})
        result = Agent(DemoDriver(), DemoModel(), DemoHelper(), spend_ledger=ledger).run("Search coffee", execute=True)
        self.assertEqual(result["costUsd"], ledger.cost_nanodollars / 1e9)
        self.assertNotIn("costUsd", Agent(DemoDriver(), DemoModel(), DemoHelper()).run("Search coffee", execute=True))

    def test_outcomes(self):
        self.assertEqual(fast_proof.outcome("completed_unverified", [{"quote": "x"}]), "done")
        self.assertEqual(fast_proof.outcome("completed_unverified", []), "check")
        self.assertEqual(fast_proof.outcome("completion_not_confirmed", [{"quote": "x"}]), "check")
        self.assertEqual(fast_proof.outcome("approval_denied", []), "declined")
        self.assertEqual(fast_proof.outcome("stopped", []), "stopped")
        for status in ("error", "blocked", "max_steps", "timeout", "approval_timeout", "no_progress"):
            self.assertEqual(fast_proof.outcome(status, []), "couldnt_finish")

    def test_answers_read_as_a_sentence_for_text_and_auto(self):
        self.assertEqual(fast_proof.answer_sentence({"main_heading": "Example Domain"}, "text"),
                         "The main heading is Example Domain.")
        self.assertEqual(fast_proof.answer_sentence({"ios_version": "26.4"}, None), "The iOS version is 26.4.")
        self.assertEqual(fast_proof.answer_sentence("  Example   Domain ", "text"), "Example Domain")
        self.assertEqual(fast_proof.answer_sentence({"a": "1", "b": "2"}, "text"), "A: 1; B: 2.")
        self.assertIsNone(fast_proof.answer_sentence({"ios_version": "26.4"}, "json"))
        self.assertIsNone(fast_proof.answer_sentence({"rows": [{"a": 1}]}, "text"))

    def test_step_words(self):
        self.assertEqual(fast_proof.step_text("SWIPE_UP", "List"), "Scrolled down")
        self.assertEqual(fast_proof.step_text("TAP", ""), "Tapped an item on screen")
        self.assertEqual(fast_proof.step_text("TYPE", "To:"), "Typed into To")
        self.assertEqual(fast_proof.step_text("LAUNCH_APP", "com.apple.Maps"), "Opened another app")
        self.assertTrue(fast_proof.step_text("TAP", "x" * 200).endswith("…"))


if __name__ == "__main__":
    unittest.main()
