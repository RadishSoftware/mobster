import http.client
import io
import json
import os
import tempfile
import threading
import time
import unittest
import unittest.mock
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from mobile_agent.frontier import FrontierAgent
from mobile_agent.run import Run
from mobile_agent.server import BoundedServer, Runtime, make_handler, may_have_acted
from mobile_agent.state import Element
from mobile_agent.task_policy import (
    RiskTier,
    action_risk_tier,
    approval_kind,
    approval_subject,
    approval_title,
    commit_act,
)
from mobile_agent.tests import test_frontier as F
from mobile_agent.tests.fakes import Clock
from mobile_agent.wda.frame_store import FrameStore


class PolicyTests(unittest.TestCase):
    def test_commit_controls_and_side_effect_submits_ask(self):
        ask = lambda op, label, tier=RiskTier.NAVIGATION.value: approval_kind(
            op, label, risk_tier=tier) == "commit"
        for label in ("Send", "Buy now", "Place Order", "Delete Message", "Post", "Pay with Apple Pay", "Follow"):
            self.assertTrue(ask("TAP", label), label)
        self.assertTrue(ask("SUBMIT", "Message", RiskTier.SIDE_EFFECT.value))

    def test_rides_trash_clearing_and_other_commits_ask(self):
        side = RiskTier.SIDE_EFFECT.value
        for label in ("Request Lyft", "Request UberX", "Trash", "Move to Trash", "Clear All", "Clear", "Accept",
                      "Decline", "Get", "Rent", "Unsubscribe", "Block Caller", "Block this Caller",
                      "Leave Conversation", "Join", "Archive", "Report Junk", "Cancel Ride"):
            self.assertEqual(approval_kind("TAP", label, risk_tier=side), "commit", label)
            self.assertTrue(approval_kind("TAP", label, risk_tier=RiskTier.NAVIGATION.value) == "commit", label)
            self.assertEqual(approval_kind("TAP", label, risk_tier=side, role="Cell"), "commit", label)
        self.assertEqual(approval_title(commit_act("Request Lyft")), "Send this request?")
        self.assertEqual(approval_title(commit_act("Move to Trash")), "Move this to the trash?")
        self.assertEqual(approval_title(commit_act("Clear All")), "Clear these?")
        self.assertEqual(approval_title(commit_act("Cancel Ride")), "Cancel this ride?")

    def test_form_openers_and_look_alikes_do_not_ask(self):
        side = RiskTier.SIDE_EFFECT.value
        for label in ("New Reminder", "Add", "Create", "Compose", "Edit", "New Message", "Get Directions",
                      "Get Started", "Clear text", "Request Desktop Website", "Cancel", "Save", "Done", "Continue",
                      "Add List"):
            self.assertIsNone(approval_kind("TAP", label, risk_tier=side), label)
        self.assertEqual(commit_act("Return", "Get the iOS version"), "submit")

    def test_an_uncertain_step_asks_as_unsure_and_a_commit_stays_a_commit(self):
        side = RiskTier.SIDE_EFFECT.value
        self.assertEqual(approval_kind("TAP", "New Reminder", risk_tier=side, uncertain=True), "unsure")
        self.assertEqual(approval_kind("TYPE", "Title", risk_tier=side, uncertain=True), "unsure")
        self.assertEqual(approval_kind("TAP", "Send", risk_tier=side, uncertain=True), "commit")
        self.assertIsNone(approval_kind("SWIPE_UP", "Send", risk_tier=side))

    def test_a_commit_word_in_content_is_not_a_commit(self):
        side = RiskTier.SIDE_EFFECT.value
        for label, role in (("Call the dentist, Incomplete", "Cell"), ("Call the dentist", "Cell"),
                            ("Call the dentist", "StaticText"), ("Sam, Send me the photos, 9:41 AM", "Cell"),
                            ("Order more coffee filters", "TextField"), ("Call the dentist, Incomplete", "Button")):
            self.assertIsNone(approval_kind("TAP", label, risk_tier=side, role=role), (label, role))
            self.assertNotEqual(approval_title(commit_act(label, "Delete the reminder Call the dentist", role)),
                                "Place this call?", label)
        for label, role, title in (("Call", "Button", "Place this call?"), ("call", "Button", "Place this call?"),
                                   ("Send", "Button", "Send this message?"), ("Call Sam", "Button", "Place this call?"),
                                   ("Place Order", "Cell", "Place this order?"),
                                   ("Delete Contact", "Cell", "Delete this?"),
                                   ("Pay with Apple Pay", "Button", "Make this payment?"),
                                   ("Call", "Cell", "Place this call?")):
            self.assertEqual(approval_kind("TAP", label, risk_tier=side, role=role), "commit", label)
            self.assertEqual(approval_title(commit_act(label, "", role)), title, label)
        self.assertEqual(approval_kind("TAP", "Call the dentist", risk_tier=side), "commit")

    def test_a_field_submit_and_a_destructive_row_still_ask(self):
        goal = "Reply to Sam that I'm on my way"
        tier = action_risk_tier("TYPE_SUBMIT", "Reply to Sam", goal).value
        self.assertEqual(tier, RiskTier.TYPE.value)
        for role in ("TextField", "TextView", "SearchField", None):
            self.assertEqual(approval_kind("TYPE_SUBMIT", "Reply to Sam", risk_tier=tier, role=role), "commit", role)
            self.assertEqual(approval_kind("SUBMIT", "Reply to Sam", risk_tier=tier, role=role), "commit", role)
            self.assertEqual(approval_title(commit_act("Reply to Sam", goal, role, "TYPE_SUBMIT")), "Send this reply?")
        self.assertIsNone(approval_kind("TAP", "Reply to Sam", risk_tier=tier, role="TextField"))
        for label, title in (("Block this Caller", "Block this contact?"),
                             ("Clear History and Website Data", "Clear these?"),
                             ("Erase All Content and Settings", "Erase this?"), ("Remove This Card", "Remove this?"),
                             ("Delete All Recordings", "Delete this?"), ("Reset All Settings", "Reset this?"),
                             ("Report This Message As Junk", "Report this?"),
                             ("Move to Trash", "Move this to the trash?")):
            for tier in (RiskTier.NAVIGATION.value, RiskTier.SIDE_EFFECT.value):
                self.assertEqual(approval_kind("TAP", label, risk_tier=tier, role="Cell"), "commit", label)
            self.assertEqual(approval_title(commit_act(label, "", "Cell")), title, label)
        for label in ("Call the dentist, Incomplete", "Call the dentist", "Delete old photos, Incomplete",
                      "Pay the rent on Friday, Incomplete", "Sam, Block party on Saturday, 9:41 AM",
                      "Book club notes, Yesterday, No additional text"):
            self.assertIsNone(approval_kind("TAP", label, risk_tier=RiskTier.SIDE_EFFECT.value, role="Cell"), label)

    def test_money_and_message_rows_ask_at_any_length(self):
        side = RiskTier.SIDE_EFFECT.value
        for label, role, title in (("Place your order", "StaticText", "Place this order?"),
                                   ("Confirm and pay", "StaticText", "Confirm this?"),
                                   ("Send My Current Location", "Cell", "Send this message?"),
                                   ("Transfer to Checking", "Cell", "Make this transfer?"),
                                   ("Buy with Apple Pay", "StaticText", "Buy this?"),
                                   ("Pay $42.00 now", "Cell", "Make this payment?"),
                                   ("Post to your story", "Cell", "Post this?"),
                                   ("Share with everyone nearby", "Cell", "Share this?"),
                                   ("Subscribe for $4.99 a month", "StaticText", "Subscribe?"),
                                   ("Book this table for two", "Cell", "Make this booking?"),
                                   ("Submit your application now", "StaticText", "Submit this?"),
                                   ("Donate to the fund", "Image", "Make this donation?"),
                                   ("Mark as Junk", "Cell", "Mark this as junk?")):
            for tier in (RiskTier.NAVIGATION.value, side):
                self.assertEqual(approval_kind("TAP", label, risk_tier=tier, role=role), "commit", label)
            self.assertEqual(approval_title(commit_act(label, "", role)), title, label)
        self.assertEqual(approval_kind("TAP", "Pay the rent on Friday", risk_tier=side, role="Cell"), "commit")
        for label in ("Call the dentist", "Dial the office line", "FaceTime with the family", "Reply to Sam's note",
                      "Comment on the draft later", "Follow up with Dana"):
            for role in ("Cell", "StaticText"):
                self.assertIsNone(approval_kind("TAP", label, risk_tier=side, role=role), (label, role))
        for label in ("Call Sam", "Reply All", "Follow"):
            self.assertEqual(approval_kind("TAP", label, risk_tier=side, role="Cell"), "commit", label)
        for label in ("Send", "Comment", "Pay", "Transfer amount", "Order more coffee filters"):
            for role in ("TextField", "TextView", "SecureTextField", "SearchField"):
                self.assertIsNone(approval_kind("TAP", label, risk_tier=side, role=role), (label, role))

    def test_text_on_a_control_reads_as_the_control(self):
        text = Element("0", "Place your order", "StaticText", (.3, .72, .4, .03))
        button = Element("9", "Place your order", "Button", (.05, .71, .9, .05))
        form = Element("8", "Your cart", "Link", (0, .6, 1, .3))
        self.assertIs(approval_subject([text, form, button], text), button)
        inner = Element("7", "Details", "Button", (.28, .715, .45, .04))
        self.assertIs(approval_subject([text, inner, button], text), button)
        other = Element("0", "Total $42.00", "StaticText", (.3, .72, .4, .03))
        self.assertIs(approval_subject([other, form, button], other), button)
        pay = Element("0", "Confirm and pay", "StaticText", (.3, .72, .4, .03))
        self.assertIs(approval_subject([pay, form], pay), pay)
        bar = Element("7", "Send Money", "NavigationBar", (0, .6, 1, .3))
        self.assertIs(approval_subject([other, bar], other), other)
        bare = Element("6", "", "Button", (.05, .71, .9, .05))
        subject = approval_subject([pay, bare], pay)
        self.assertEqual((subject.label, subject.role), ("Confirm and pay", "Button"))
        icon = Element("0", "arrow.up.circle.fill", "Image", (.86, .9, .06, .03))
        send = Element("5", "Send", "Button", (.85, .89, .08, .05))
        self.assertIs(approval_subject([icon, send], icon), send)
        title = Element("0", "Pay the rent on Friday", "StaticText", (.1, .51, .5, .03))
        row = Element("4", "Pay the rent on Friday, Incomplete", "Cell", (0, .5, 1, .06))
        self.assertIs(approval_subject([title, row], title), row)
        self.assertIsNone(approval_kind("TAP", row.label, risk_tier=RiskTier.SIDE_EFFECT.value, role=row.role))
        erase = Element("0", "Erase All Content and Settings", "Cell", (0, .5, 1, .06))
        for element in (button, Element("3", "Send", "TextField", (0, .7, 1, .1), True), erase):
            self.assertIs(approval_subject([element, form], element), element)

    def test_the_card_names_the_act(self):
        self.assertEqual(commit_act("Send"), "send")
        self.assertEqual(commit_act("Place Order · $42.00"), "place order")
        self.assertEqual(commit_act("Message", "Post this to my feed"), "post")
        self.assertEqual(commit_act("Return", "Text Sam I'm late"), "submit")
        self.assertEqual(approval_title("send"), "Send this message?")
        self.assertEqual(approval_title("delete"), "Delete this?")
        self.assertEqual(approval_title(None, unsure=True), "Not sure about this step")

    def test_navigation_typing_and_scrolling_never_ask(self):
        ask = lambda op, label, tier=RiskTier.NAVIGATION.value: approval_kind(
            op, label, risk_tier=tier) == "commit"
        for label in ("General", "About", "Done", "Back", "Wi-Fi", "Search"):
            self.assertFalse(ask("TAP", label), label)
        self.assertFalse(ask("SUBMIT", "Address", RiskTier.TYPE.value))
        self.assertFalse(ask("TYPE", "Send a message", RiskTier.SIDE_EFFECT.value))
        self.assertFalse(ask("SWIPE_UP", "Send"))
        self.assertFalse(ask("TAP", "Continue"))


class FrontierApprovalTests(unittest.TestCase):
    SEND = Element("1", "Send", "Button", (.8, .06, .1, .05))

    def run_frontier(self, request, *elements, answer="approved", cancelled=None, max_seconds=900, approve=True,
                     first=None):
        asked = []

        def ask(request_):
            asked.append(request_)
            return answer(request_) if callable(answer) else answer
        label = first or elements[0].label
        driver = F.Driver([F.screen(*elements, bundle="com.example.app")] * 8)
        script = F.Script([("TAP", label, None), ("DONE", None, None), ("DONE", None, None), ("DONE", None, None)])
        result = FrontierAgent(driver, script, emit=lambda e: None, screenshots=False, settle_seconds=0,
                               approve=ask if approve else None, cancelled=cancelled,
                               max_seconds=max_seconds).run(request)
        return result, driver, asked

    def taps(self, driver, label):
        return [a for a in driver.actions if a[:2] == ("TAP", label)]

    def test_no_answer_ends_the_task_without_acting(self):
        result, driver, asked = self.run_frontier("Send Sam the photos", self.SEND, answer="timeout")
        self.assertEqual(result["status"], "approval_timeout")
        self.assertEqual(driver.actions, [])
        self.assertEqual((asked[0]["kind"], asked[0]["label"], asked[0]["title"]), ("commit", "Send", "Tap 'Send'?"))
        self.assertEqual(asked[0]["target"], {"x": .8, "y": .06, "w": .1, "h": .05})
        result, driver, _ = self.run_frontier("Send Sam the photos", self.SEND, answer="approved")
        self.assertEqual(self.taps(driver, "Send"), [("TAP", "Send", None)])

    def test_stopping_while_waiting_stops_the_task(self):
        result, driver, _ = self.run_frontier("Send Sam the photos", self.SEND, answer="stopped")
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(driver.actions, [])

    def test_waiting_for_the_user_does_not_spend_the_run_deadline(self):
        clock = Clock()

        def slow(_request):
            clock.now += 600
            return "approved"
        with patch("time.monotonic", clock):
            result, driver, _ = self.run_frontier("Send Sam the photos", self.SEND, answer=slow, max_seconds=300)
        self.assertEqual(self.taps(driver, "Send"), [("TAP", "Send", None)])
        self.assertNotEqual(result["status"], "timeout")

    def test_a_commit_the_request_does_not_ask_for_is_never_put_to_the_user_or_tapped(self):
        cases = ((Element("0", "Call", "Button", (.4, .8, .2, .08)),),
                 (Element("0", "Place your order", "StaticText", (.3, .72, .4, .03)),
                  Element("9", "Place your order", "Button", (.05, .71, .9, .05))),
                 (Element("0", "Confirm and pay", "StaticText", (.3, .72, .4, .03)),),
                 (Element("0", "Send My Current Location", "Cell", (0, .5, 1, .05)),),
                 (Element("0", "Transfer to Checking", "Cell", (0, .5, 1, .05)),))
        for elements in cases:
            with self.subTest(label=elements[0].label):
                result, driver, asked = self.run_frontier("Search coffee", *elements)
                self.assertEqual((asked, driver.actions), ([], []))

    def test_a_requested_commit_is_put_to_the_user_and_a_no_never_taps(self):
        cases = (("Place my order", (Element("9", "Place your order", "Button", (.05, .71, .9, .05)),)),
                 ("Transfer $20 to checking", (Element("0", "Transfer to Checking", "Cell", (0, .5, 1, .05)),)))
        for request, elements in cases:
            with self.subTest(request=request):
                result, driver, asked = self.run_frontier(request, *elements, answer="denied")
                self.assertEqual([(r["kind"], r["label"]) for r in asked], [("commit", elements[0].label)])
                self.assertEqual((result["status"], driver.actions), ("approval_denied", []))
                result, driver, _ = self.run_frontier(request, *elements, answer="approved")
                self.assertEqual(self.taps(driver, elements[0].label), [("TAP", elements[0].label, None)])

    SHEET = (Element("1", "Send", "Button", (.85, .12, .1, .04)),
             Element("2", "Cancel", "Button", (.03, .06, .15, .04)),
             Element("3", "To: Bench Tester", "StaticText", (.05, .12, .6, .04)))

    def decline_on(self, *elements, answer="denied", cancelled=None):
        events, asked = [], []
        driver = F.Driver([F.screen(*elements, bundle="com.apple.MobileSMS")] * 8)
        script = F.Script([("TAP", "Send", None), ("DONE", None, None), ("DONE", None, None)])
        result = FrontierAgent(driver, script, emit=events.append, screenshots=False, settle_seconds=0,
                               approve=lambda request_: asked.append(request_) or answer,
                               cancelled=cancelled).run("Share the note with Bench Tester in Messages")
        return result, driver, [e for e in events if e.get("event") == "frontier_cleanup"]

    def test_a_declined_share_closes_its_sheet_with_cancel_and_journals_it_first(self):
        result, driver, cleanup = self.decline_on(*self.SHEET)
        self.assertEqual(result["status"], "approval_denied")
        self.assertEqual(driver.actions, [("TAP", "Cancel", None)], "Cancel only: Send is never tapped")
        self.assertEqual([e["state"] for e in cleanup], ["sending", "sent"])

    def test_no_cancel_two_closers_an_approval_or_stop_leave_the_screen_as_it_is(self):
        _, driver, cleanup = self.decline_on(self.SHEET[0], self.SHEET[2])
        self.assertEqual((driver.actions, cleanup), ([], []))
        two = (*self.SHEET, Element("4", "Close", "Button", (.85, .5, .1, .04)))
        _, driver, cleanup = self.decline_on(*two)
        self.assertEqual((driver.actions, cleanup), ([], []))
        _, driver, _ = self.decline_on(*self.SHEET, answer="timeout")
        self.assertEqual(driver.actions, [])
        _, driver, _ = self.decline_on(*self.SHEET, answer="approved")
        self.assertEqual(driver.actions, [("TAP", "Send", None)])
        _, driver, cleanup = self.decline_on(*self.SHEET, cancelled=lambda: True)
        self.assertEqual(self.taps(driver, "Cancel"), [])

    def test_the_cleanup_counts_as_an_action_after_a_restart(self):
        self.assertTrue(may_have_acted({"event": "frontier_cleanup", "state": "sending"}))


class RunApprovalTests(unittest.TestCase):
    def run_obj(self):
        return Run({"id": "a", "name": "Messages", "bundleId": "com.apple.MobileSMS"}, "Text Alex hi", "live")

    def ask_in_background(self, run, timeout=5):
        answers = []
        thread = threading.Thread(target=lambda: answers.append(run.request_approval(
            {"step": 3, "operation": "TYPE_SUBMIT", "label": "Message", "text": "hi"}, timeout=timeout)))
        thread.start()
        for _ in range(100):
            if run.public()["approval"]:
                break
            time.sleep(.01)
        return thread, answers

    def test_the_pending_request_is_visible_and_answered_once(self):
        run = self.run_obj()
        thread, answers = self.ask_in_background(run)
        pending = run.public()["approval"]
        self.assertEqual((pending["label"], pending["text"], pending["app"]), ("Message", "hi", "Messages"))
        self.assertFalse(run.answer_approval("wrong", True))
        self.assertTrue(run.answer_approval(pending["id"], True))
        self.assertFalse(run.answer_approval(pending["id"], False))
        thread.join(2)
        self.assertEqual(answers, ["approved"])
        self.assertIsNone(run.public()["approval"])

    def test_a_point_taps_crop_is_a_run_frame_on_its_card(self):
        picture = io.BytesIO()
        Image.new("RGB", (360, 156), (20, 20, 20)).save(picture, "JPEG")
        picture = picture.getvalue()
        run = self.run_obj()
        run.frames = FrameStore()
        self.addCleanup(run.frames.close)
        answers = []
        thread = threading.Thread(target=lambda: answers.append(run.request_approval(
            {"step": 2, "operation": "TAP_XY", "label": "", "title": "Tap “Close” at this spot?", "act": "other",
             "crop": picture}, timeout=5)))
        thread.start()
        for _ in range(100):
            if run.public()["approval"]:
                break
            time.sleep(.01)
        pending = run.public()["approval"]
        path = pending["crop"]
        self.assertRegex(path, r"^/api/runs/[a-f0-9]{12}/frames/f[a-f0-9]{10}$")
        self.assertEqual(run.frames.get(run.id, path.rsplit("/", 1)[1]), picture)
        run.answer_approval(pending["id"], False)
        thread.join(2)
        requested = next(e for e in run.events if e["event"] == "approval_requested")
        self.assertEqual(requested["crop"], path)
        thread, _ = self.ask_in_background(run)
        self.assertNotIn("crop", run.public()["approval"])
        run.answer_approval(run.public()["approval"]["id"], False)
        thread.join(2)

    def test_the_text_to_send_never_enters_the_event_log(self):
        run = self.run_obj()
        thread, _ = self.ask_in_background(run)
        run.answer_approval(run.public()["approval"]["id"], False)
        thread.join(2)
        events = [e for e in run.events if e["event"].startswith("approval_")]
        self.assertEqual([e["event"] for e in events], ["approval_requested", "approval_resolved"])
        self.assertEqual(events[1]["decision"], "denied")
        self.assertTrue(events[0]["text_present"])
        self.assertNotIn("hi", str([{k: v for k, v in e.items() if k not in {"label", "event"}} for e in events]))

    def test_timeout_and_stop(self):
        run = self.run_obj()
        self.assertEqual(run.request_approval({"operation": "TAP", "label": "Send"}, timeout=.05), "timeout")
        thread, answers = self.ask_in_background(run)
        run.stop.set()
        with run.condition:
            run.condition.notify_all()
        thread.join(3)
        self.assertEqual(answers, ["stopped"])


class ApprovalRouteTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("MOBSTER_ASK_BEFORE_ACTING", None)
        os.environ.pop("MOBSTER_BYPASS_CHECKS", None)
        self.env_file = os.path.join(tempfile.mkdtemp(), "agent.env")
        runtime = object.__new__(Runtime)
        runtime.config, runtime.setup = SimpleNamespace(port=8765, env_file=self.env_file), None
        runtime.runs, runtime.lock = {}, threading.Lock()
        self.run = RunApprovalTests.run_obj(self)
        runtime.runs[self.run.id] = self.run
        self.server = BoundedServer(("127.0.0.1", 0), make_handler(runtime))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        payload = json.dumps(body) if body is not None else None
        connection.request(method, path, payload, {"Host": "127.0.0.1:8765", "Content-Type": "application/json"})
        response = connection.getresponse()
        data = json.loads(response.read())
        connection.close()
        return response.status, data

    def safety(self, reply):
        status, body = reply
        return status, {key: body[key] for key in ("askBeforeActing",)} if status == 200 else body

    def test_ask_before_acting_defaults_on_and_persists_when_turned_off(self):
        self.assertEqual(self.safety(self.call("GET", "/api/settings")),
                         (200, {"askBeforeActing": True}))
        self.assertEqual(self.safety(self.call("POST", "/api/settings", {"askBeforeActing": False})),
                         (200, {"askBeforeActing": False}))
        with open(self.env_file) as stream:
            self.assertIn("MOBSTER_ASK_BEFORE_ACTING=0", stream.read())
        self.assertEqual(self.call("POST", "/api/settings", {"askBeforeActing": "no"})[0], 400)
        self.assertEqual(self.call("POST", "/api/settings", {"other": True})[0], 400)

    def test_the_checks_bypass_cannot_be_turned_on(self):
        for value in (True, False, 1):
            self.assertEqual(self.call("POST", "/api/settings", {"bypassChecks": value})[0], 400)
        self.assertNotIn("bypassChecks", self.call("GET", "/api/settings")[1])
        if os.path.exists(self.env_file):
            with open(self.env_file) as stream:
                self.assertNotIn("MOBSTER_BYPASS_CHECKS", stream.read())

    def test_answering_a_pending_request(self):
        thread, answers = RunApprovalTests.ask_in_background(self, self.run)
        pending = self.run.public()["approval"]
        path = f"/api/runs/{self.run.id}/approval"
        self.assertEqual(self.call("POST", path, {"id": pending["id"]})[0], 400)
        status, body = self.call("POST", path, {"id": pending["id"], "approve": True})
        self.assertEqual(status, 200)
        thread.join(2)
        self.assertEqual(answers, ["approved"])
        self.assertEqual(self.call("POST", path, {"id": pending["id"], "approve": True})[0], 409)


class StopRaceAndSleepTests(unittest.TestCase):
    run_obj = RunApprovalTests.run_obj
    ask_in_background = RunApprovalTests.ask_in_background

    def test_after_stop_an_approval_is_refused_and_says_the_task_was_stopped(self):
        run = self.run_obj()
        thread, answers = self.ask_in_background(run)
        pending = run.public()["approval"]
        with run.condition:
            run.stop.set()
            self.assertFalse(run.answer_approval(pending["id"], True))
            self.assertEqual(run.approval_decision(pending["id"]), "stopped")
            run.condition.notify_all()
        thread.join(3)
        self.assertEqual(answers, ["stopped"])
        self.assertEqual(run.approval_decision(pending["id"]), "stopped")

    def test_an_approval_that_lands_a_moment_before_stop_is_not_acted_on(self):
        run = self.run_obj()
        thread, answers = self.ask_in_background(run)
        pending = run.public()["approval"]
        with run.condition:
            self.assertTrue(run.answer_approval(pending["id"], True))
            run.stop.set()
        thread.join(3)
        self.assertEqual(answers, ["stopped"])

    def test_without_stop_the_same_approval_goes_through(self):
        run = self.run_obj()
        thread, answers = self.ask_in_background(run)
        pending = run.public()["approval"]
        self.assertTrue(run.answer_approval(pending["id"], True))
        thread.join(3)
        self.assertEqual(answers, ["approved"])
        self.assertEqual(run.approval_decision(pending["id"]), "approved")

    def test_a_request_ends_once_the_wall_clock_passes_its_deadline_as_after_a_sleeping_mac(self):
        run = self.run_obj()
        real, ahead = time.time, [0.0]
        with unittest.mock.patch("mobile_agent.server.time.time", lambda: real() + ahead[0]):
            thread, answers = self.ask_in_background(run, timeout=600)
            pending = run.public()["approval"]
            started = time.monotonic()
            ahead[0] = 601.0
            thread.join(5)
        self.assertEqual(answers, ["timeout"])
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertFalse(run.answer_approval(pending["id"], True))
        self.assertEqual(run.approval_decision(pending["id"]), "timeout")

    def test_before_its_deadline_a_request_still_waits_for_the_person(self):
        run = self.run_obj()
        real, ahead = time.time, [0.0]
        with unittest.mock.patch("mobile_agent.server.time.time", lambda: real() + ahead[0]):
            thread, answers = self.ask_in_background(run, timeout=600)
            ahead[0] = 500.0
            time.sleep(1.2)
            self.assertEqual(answers, [])
            self.assertTrue(run.answer_approval(run.public()["approval"]["id"], True))
            thread.join(3)
        self.assertEqual(answers, ["approved"])

    def test_an_expired_card_refuses_an_answer_before_its_waiter_wakes(self):
        for clock in ("wall", "monotonic"):
            with self.subTest(clock=clock):
                run = self.run_obj()
                thread, answers = self.ask_in_background(run, timeout=600)
                with run.condition:
                    pending = run.approval
                    if clock == "wall":
                        pending["expiresAt"] = (time.time() - 1) * 1000
                    else:
                        run.approval_deadline = time.monotonic() - 1
                    self.assertFalse(run.answer_approval(pending["id"], True))
                    self.assertEqual(run.approval_decision(pending["id"]), "timeout")
                thread.join(3)
                self.assertEqual(answers, ["timeout"])

    def test_mobsters_agent_never_acts_on_an_approval_once_stopped(self):
        stop = threading.Event()

        def approve(_request):
            stop.set()
            return "approved"

        send = Element("1", "Send", "Button", (.8, .06, .1, .05))
        driver = F.Driver([F.screen(send)] * 4)
        result = FrontierAgent(driver, F.Script([("TAP", "Send", None), ("DONE", None, None)]), screenshots=False,
                               settle_seconds=0, approve=approve, cancelled=stop.is_set).run("Send Sam the photos")
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(driver.actions, [])


class SecondAnswerRouteTests(unittest.TestCase):
    setUp, call = ApprovalRouteTests.setUp, ApprovalRouteTests.call

    def answer_twice(self, first, second):
        thread, answers = RunApprovalTests.ask_in_background(self, self.run)
        pending = self.run.public()["approval"]
        path = f"/api/runs/{self.run.id}/approval"
        self.assertEqual(self.call("POST", path, {"id": pending["id"], **first})[0], 200)
        thread.join(2)
        return self.call("POST", path, {"id": pending["id"], **second}), answers

    def test_a_decline_after_a_send_says_it_was_already_approved(self):
        (status, body), answers = self.answer_twice({"approve": True}, {"approve": False})
        self.assertEqual(answers, ["approved"])
        self.assertEqual((status, body["code"], body["decision"]), (409, "approval_not_pending", "approved"))
        self.assertEqual(body["error"], "This was already approved, so Mobster is going ahead.")

    def test_a_send_after_a_decline_says_it_was_already_declined(self):
        (status, body), answers = self.answer_twice({"approve": False}, {"approve": True})
        self.assertEqual(answers, ["denied"])
        self.assertEqual((status, body["decision"]), (409, "denied"))
        self.assertEqual(body["error"], "This was already declined, so Mobster won't do it.")

    def test_an_id_this_run_never_asked_keeps_the_plain_refusal(self):
        status, body = self.call("POST", f"/api/runs/{self.run.id}/approval", {"id": "ffffffffffff", "approve": True})
        self.assertEqual((status, body["code"]), (409, "approval_not_pending"))
        self.assertNotIn("decision", body)
        self.assertEqual(body["error"], "This approval request is no longer waiting")


if __name__ == "__main__":
    unittest.main()
