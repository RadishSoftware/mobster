"""Ask before acting: consequential actions pause for the user's answer. Offline."""

from dataclasses import replace
import threading
import time
import unittest
import unittest.mock

from mobile_agent.agent import Agent
from mobile_agent import demo
from mobile_agent.demo import DemoDriver, DemoHelper, DemoModel
from mobile_agent.server import Run
from mobile_agent.state import Element, Snapshot
from mobile_agent.task_policy import (ActionSupport, RiskTier, action_risk_tier, approval_kind, approval_subject,
                                     approval_title, commit_act, needs_approval)


class RiskyModel(DemoModel):
    """The demo flow, with every tap scored as a likely side effect."""

    def decide(self, snapshot, goal, history, **kwargs):
        decision = super().decide(snapshot, goal, history, **kwargs)
        return replace(decision, side_effect_risk=.95) if decision.operation == "TAP" else decision


class CommitDriver(DemoDriver):
    """The demo flow, where the first control the model taps is labelled "Send" (a commit)."""

    def observe(self, timeout=10):
        snapshot = demo.screen(self.stage)
        if self.stage != "home":
            return snapshot
        return Snapshot([Element("0", "Send", "Button", (.8, .06, .1, .05)), *snapshot.elements[1:]],
                        snapshot.text.replace("Search", "Send"), snapshot.width, snapshot.height, snapshot.source)


class RowDriver(DemoDriver):
    """The demo flow, where the first thing the model taps is ``element`` (a reminder's row, a Call button),
    shown with the elements ``around`` it (the Button a WebKit text sits in)."""

    def __init__(self, element, *around):
        super().__init__()
        self.element, self.around = element, around

    def observe(self, timeout=10):
        snapshot = demo.screen(self.stage)
        if self.stage != "home":
            return snapshot
        return Snapshot([self.element, *self.around, *snapshot.elements[1:]],
                        snapshot.text.replace("Search", self.element.label),
                        snapshot.width, snapshot.height, snapshot.source)


class ConfidentModel(DemoModel):
    """The demo flow with every tap scored as a side effect but taken outright: no demotion to WAIT."""

    def decide(self, snapshot, goal, history, **kwargs):
        decision = super().decide(snapshot, goal, history, **kwargs)
        return (replace(decision, risk_tier=RiskTier.SIDE_EFFECT.value, side_effect_risk=.95)
                if decision.operation == "TAP" else decision)


class PolicyTests(unittest.TestCase):
    def test_commit_controls_and_side_effect_submits_ask(self):
        ask = lambda op, label, tier=RiskTier.NAVIGATION.value, risk=0.: needs_approval(
            op, label, risk_tier=tier, side_effect_risk=risk)
        for label in ("Send", "Buy now", "Place Order", "Delete Message", "Post", "Pay with Apple Pay", "Follow"):
            self.assertTrue(ask("TAP", label), label)
        self.assertTrue(ask("SUBMIT", "Message", RiskTier.SIDE_EFFECT.value))

    def test_rides_trash_clearing_and_other_commits_ask(self):
        # The PM probe of 26 Sep: each of these went through unasked when only COMMIT_CONTROL counted.
        side = RiskTier.SIDE_EFFECT.value
        for label in ("Request Lyft", "Request UberX", "Trash", "Move to Trash", "Clear All", "Clear", "Accept",
                      "Decline", "Get", "Rent", "Unsubscribe", "Block Caller", "Block this Caller",
                      "Leave Conversation", "Join", "Archive", "Report Junk", "Cancel Ride"):
            self.assertEqual(approval_kind("TAP", label, risk_tier=side), "commit", label)
            self.assertTrue(needs_approval("TAP", label, risk_tier=RiskTier.NAVIGATION.value), label)
            # Phone's "Block this Caller" and Settings' "Clear" are rows (Cell), and a row asks just the same.
            self.assertEqual(approval_kind("TAP", label, risk_tier=side, role="Cell"), "commit", label)
        self.assertEqual(approval_title(commit_act("Request Lyft")), "Send this request?")
        self.assertEqual(approval_title(commit_act("Move to Trash")), "Move this to the trash?")
        self.assertEqual(approval_title(commit_act("Clear All")), "Clear these?")
        self.assertEqual(approval_title(commit_act("Cancel Ride")), "Cancel this ride?")

    def test_form_openers_and_look_alikes_do_not_ask(self):
        side = RiskTier.SIDE_EFFECT.value
        for label in ("New Reminder", "Add", "Create", "Compose", "Edit", "New Message", "Get Directions",
                      "Get Started", "Clear text", "Request Desktop Website", "Cancel", "Save", "Done"):
            self.assertIsNone(approval_kind("TAP", label, risk_tier=side), label)
        # The request is read for the act only through COMMIT_CONTROL: "get" there is no App Store Get.
        self.assertEqual(commit_act("Return", "Get the iOS version"), "submit")

    def test_a_high_side_effect_score_alone_no_longer_asks(self):
        # The audit's "Tap New Reminder" approval: opening a form commits nothing.
        for label in ("New Reminder", "Continue", "Add List"):
            self.assertFalse(needs_approval("TAP", label, risk_tier=RiskTier.SIDE_EFFECT.value, side_effect_risk=.95))
            self.assertIsNone(approval_kind("TAP", label, risk_tier=RiskTier.SIDE_EFFECT.value))

    def test_an_uncertain_step_asks_as_unsure_and_a_commit_stays_a_commit(self):
        side = RiskTier.SIDE_EFFECT.value
        self.assertEqual(approval_kind("TAP", "New Reminder", risk_tier=side, uncertain=True), "unsure")
        self.assertEqual(approval_kind("TYPE", "Title", risk_tier=side, uncertain=True), "unsure")
        self.assertEqual(approval_kind("TAP", "Send", risk_tier=side, uncertain=True), "commit")
        self.assertIsNone(approval_kind("SWIPE_UP", "Send", risk_tier=side))

    def test_a_commit_word_in_content_is_not_a_commit(self):
        """QA-2: tapping the reminder "Call the dentist" asked "Place this call?". A row, text or a field shows
        its own words; only a control's label says what the tap does."""
        side = RiskTier.SIDE_EFFECT.value
        for label, role in (("Call the dentist, Incomplete", "Cell"), ("Call the dentist", "Cell"),
                            ("Call the dentist", "StaticText"), ("Sam, Send me the photos, 9:41 AM", "Cell"),
                            ("Order more coffee filters", "TextField"), ("Call the dentist, Incomplete", "Button")):
            self.assertIsNone(approval_kind("TAP", label, risk_tier=side, role=role), (label, role))
            self.assertNotEqual(approval_title(commit_act(label, "Delete the reminder Call the dentist", role)),
                                "Place this call?", label)
        # A control acts on its label, and content that reads like a control's own label ("Delete Contact") too.
        for label, role, title in (("Call", "Button", "Place this call?"), ("call", "Button", "Place this call?"),
                                   ("Send", "Button", "Send this message?"), ("Call Sam", "Button", "Place this call?"),
                                   ("Place Order", "Cell", "Place this order?"), ("Delete Contact", "Cell", "Delete this?"),
                                   ("Pay with Apple Pay", "Button", "Make this payment?"), ("Call", "Cell", "Place this call?")):
            self.assertEqual(approval_kind("TAP", label, risk_tier=side, role=role), "commit", label)
            self.assertEqual(approval_title(commit_act(label, "", role)), title, label)
        # Without a role (older callers), a label counts as a control's, as before.
        self.assertEqual(approval_kind("TAP", "Call the dentist", risk_tier=side), "commit")

    def test_a_field_submit_and_a_destructive_row_still_ask(self):
        """Review of #41: the role only tells a tap on content from a tap on a control. Return in a field does
        what the field's label says, and a settings row that erases or blocks asks at any length."""
        goal = "Reply to Sam that I'm on my way"
        tier = action_risk_tier("TYPE_SUBMIT", "Reply to Sam", goal).value
        self.assertEqual(tier, RiskTier.TYPE.value)
        for role in ("TextField", "TextView", "SearchField", None):
            self.assertEqual(approval_kind("TYPE_SUBMIT", "Reply to Sam", risk_tier=tier, role=role), "commit", role)
            self.assertEqual(approval_kind("SUBMIT", "Reply to Sam", risk_tier=tier, role=role), "commit", role)
            self.assertEqual(approval_title(commit_act("Reply to Sam", goal, role, "TYPE_SUBMIT")), "Send this reply?")
        # A tap on the same field only focuses it.
        self.assertIsNone(approval_kind("TAP", "Reply to Sam", risk_tier=tier, role="TextField"))
        for label, title in (("Block this Caller", "Block this contact?"), ("Clear History and Website Data", "Clear these?"),
                             ("Erase All Content and Settings", "Erase this?"), ("Remove This Card", "Remove this?"),
                             ("Delete All Recordings", "Delete this?"), ("Reset All Settings", "Reset this?"),
                             ("Report This Message As Junk", "Report this?"), ("Move to Trash", "Move this to the trash?")):
            for tier in (RiskTier.NAVIGATION.value, RiskTier.SIDE_EFFECT.value):
                self.assertEqual(approval_kind("TAP", label, risk_tier=tier, role="Cell"), "commit", label)
                self.assertTrue(needs_approval("TAP", label, risk_tier=tier, role="Cell"), label)
            self.assertEqual(approval_title(commit_act(label, "", "Cell")), title, label)
        # A reminder's row, a row iOS joins with commas, and a call to someone in a row's words still don't.
        for label in ("Call the dentist, Incomplete", "Call the dentist", "Delete old photos, Incomplete",
                      "Pay the rent on Friday, Incomplete", "Sam, Block party on Saturday, 9:41 AM",
                      "Book club notes, Yesterday, No additional text"):
            self.assertIsNone(approval_kind("TAP", label, risk_tier=RiskTier.SIDE_EFFECT.value, role="Cell"), label)

    def test_money_and_message_rows_ask_at_any_length(self):
        """Review 2 of #41: rows and text that pay, order, send or transfer went through unasked when only
        deletions counted past two words. Every act but a call, a reply, a comment or a follow now counts in
        content at any length, and each asks what it did on main."""
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
        # A plain row that reads like a payment asks too: a spurious ask is the safe failure. The same words as a
        # reminder's title sit inside its row, which does not ask (approval_subject).
        self.assertEqual(approval_kind("TAP", "Pay the rent on Friday", risk_tier=side, role="Cell"), "commit")
        # A call, a reply, a comment or a follow in a row's words is someone to reach, not a control.
        for label in ("Call the dentist", "Dial the office line", "FaceTime with the family", "Reply to Sam's note",
                      "Comment on the draft later", "Follow up with Dana"):
            for role in ("Cell", "StaticText"):
                self.assertIsNone(approval_kind("TAP", label, risk_tier=side, role=role), (label, role))
        for label in ("Call Sam", "Reply All", "Follow"):
            self.assertEqual(approval_kind("TAP", label, risk_tier=side, role="Cell"), "commit", label)
        # A tap on a field only focuses it, whatever its label.
        for label in ("Send", "Comment", "Pay", "Transfer amount", "Order more coffee filters"):
            for role in ("TextField", "TextView", "SecureTextField", "SearchField"):
                self.assertIsNone(approval_kind("TAP", label, risk_tier=side, role=role), (label, role))

    def test_text_on_a_control_reads_as_the_control(self):
        """Review 2 of #41: WebKit gives a checkout <button> as a Button with a StaticText of its label inside."""
        text = Element("0", "Place your order", "StaticText", (.3, .72, .4, .03))
        button = Element("9", "Place your order", "Button", (.05, .71, .9, .05))
        form = Element("8", "Your cart", "Link", (0, .6, 1, .3))
        self.assertIs(approval_subject([text, form, button], text), button)
        # Of several controls under the tap, the one with the same label, else the innermost.
        inner = Element("7", "Details", "Button", (.28, .715, .45, .04))
        self.assertIs(approval_subject([text, inner, button], text), button)
        other = Element("0", "Total $42.00", "StaticText", (.3, .72, .4, .03))
        self.assertIs(approval_subject([other, form, button], other), button)
        # A control that names no commit leaves the text its own reading; a bar or a pane holds, never names.
        pay = Element("0", "Confirm and pay", "StaticText", (.3, .72, .4, .03))
        self.assertIs(approval_subject([pay, form], pay), pay)
        bar = Element("7", "Send Money", "NavigationBar", (0, .6, 1, .3))
        self.assertIs(approval_subject([other, bar], other), other)
        # An unlabelled button is named by the text on it; its icon reads as the button.
        bare = Element("6", "", "Button", (.05, .71, .9, .05))
        subject = approval_subject([pay, bare], pay)
        self.assertEqual((subject.label, subject.role), ("Confirm and pay", "Button"))
        icon = Element("0", "arrow.up.circle.fill", "Image", (.86, .9, .06, .03))
        send = Element("5", "Send", "Button", (.85, .89, .08, .05))
        self.assertIs(approval_subject([icon, send], icon), send)
        # A reminder's title inside its row is the reminder, not a payment.
        title = Element("0", "Pay the rent on Friday", "StaticText", (.1, .51, .5, .03))
        row = Element("4", "Pay the rent on Friday, Incomplete", "Cell", (0, .5, 1, .06))
        self.assertIs(approval_subject([title, row], title), row)
        self.assertIsNone(approval_kind("TAP", row.label, risk_tier=RiskTier.SIDE_EFFECT.value, role=row.role))
        # A control, a field and a Settings row stand for themselves.
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
        ask = lambda op, label, tier=RiskTier.NAVIGATION.value, risk=0.: needs_approval(
            op, label, risk_tier=tier, side_effect_risk=risk)
        for label in ("General", "About", "Done", "Back", "Wi-Fi", "Search"):
            self.assertFalse(ask("TAP", label), label)
        self.assertFalse(ask("SUBMIT", "Address", RiskTier.TYPE.value))
        self.assertFalse(ask("TYPE", "Send a message", RiskTier.SIDE_EFFECT.value, .99))
        self.assertFalse(ask("SWIPE_UP", "Send", risk=.99))
        self.assertFalse(ask("TAP", "Continue", risk=.5))


class AgentApprovalTests(unittest.TestCase):
    def run_with(self, answer):
        driver, requests = DemoDriver(), []

        def approve(request):
            requests.append(request)
            return answer

        driver = CommitDriver()
        result = Agent(driver, RiskyModel(), DemoHelper(), approve=approve).run("Search coffee", execute=True)
        return result, driver, requests

    def test_a_declined_action_is_never_dispatched(self):
        result, driver, requests = self.run_with("denied")
        self.assertEqual(result["status"], "approval_denied")
        self.assertEqual(result["outcome"], "declined")
        self.assertEqual(driver.actions, [])
        self.assertEqual(requests[0]["operation"], "TAP")
        self.assertEqual(requests[0]["label"], "Send")
        self.assertEqual((requests[0]["kind"], requests[0]["act"], requests[0]["title"]),
                         ("commit", "send", "Send this message?"))
        self.assertEqual(requests[0]["target"], {"x": .8, "y": .06, "w": .1, "h": .05})

    def test_no_answer_ends_the_task_without_acting(self):
        result, driver, _ = self.run_with("timeout")
        self.assertEqual(result["status"], "approval_timeout")
        self.assertEqual(driver.actions, [])

    def test_an_approved_action_runs(self):
        result, driver, requests = self.run_with("approved")
        self.assertEqual(driver.actions[0], "TAP")
        self.assertEqual(len(requests), 1)

    def test_stopping_while_waiting_stops_the_task(self):
        result, driver, _ = self.run_with("stopped")
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(driver.actions, [])

    def test_waiting_for_the_user_does_not_spend_the_run_deadline(self):
        driver = DemoDriver()

        def slow(_request):
            time.sleep(1.2)
            return "approved"

        result = Agent(driver, RiskyModel(), DemoHelper(), approve=slow, max_seconds=1).run(
            "Search coffee", execute=True)
        self.assertNotEqual(result["status"], "timeout")
        self.assertEqual(driver.actions[0], "TAP")

    def test_without_a_handler_nothing_asks(self):
        driver = CommitDriver()
        Agent(driver, RiskyModel(), DemoHelper()).run("Search coffee", execute=True)
        self.assertEqual(driver.actions[0], "TAP")

    def test_a_risky_tap_on_a_control_that_commits_nothing_does_not_ask(self):
        driver, requests = DemoDriver(), []
        result = Agent(driver, RiskyModel(), DemoHelper(), approve=lambda r: requests.append(r) or "approved").run(
            "Search coffee", execute=True)
        self.assertEqual((requests, driver.actions[0], result["status"]), ([], "TAP", "completed_unverified"))


class SureSendModel(DemoModel):
    """The demo flow, where Jev is fairly sure of every tap but the side-effect floor gates it to WAIT."""

    def decide(self, snapshot, goal, history, **kwargs):
        decision = super().decide(snapshot, goal, history, **kwargs)
        if decision.operation != "TAP":
            return decision
        return replace(decision, operation="WAIT", target=None, confidence=.7, demoted_from="TAP",
                       risk_tier=RiskTier.SIDE_EFFECT.value, side_effect_risk=.95,
                       approvable_target=decision.target)


class UnsureCheckModel(RiskyModel):
    """The action checker cannot establish any action's effect."""

    def verify_action(self, *args, **kwargs):
        return ActionSupport.UNCLEAR


class HesitantActionTests(unittest.TestCase):
    """A step the model or its checker hesitates on goes to the user, or through with bypass."""

    def run_with(self, model, approve=None, bypass=False, driver=None):
        driver, requests, events = driver or DemoDriver(), [], []

        def handler(request):
            requests.append(request)
            return approve

        result = Agent(driver, model, DemoHelper(), approve=handler if approve else None, bypass=bypass,
                       emit=lambda event: events.append(event["event"])).run("Search coffee", execute=True)
        return result, driver, requests, events

    def test_a_fairly_sure_commit_is_put_to_the_user_instead_of_waiting(self):
        _, driver, requests, events = self.run_with(SureSendModel(), approve="approved", driver=CommitDriver())
        self.assertEqual(driver.actions[0], "TAP")
        self.assertEqual((requests[0]["operation"], requests[0]["kind"]), ("TAP", "commit"))
        self.assertIn("demotion_lifted", events)

    def test_declining_it_ends_the_task_without_acting(self):
        result, driver, _, _ = self.run_with(SureSendModel(), approve="denied", driver=CommitDriver())
        self.assertEqual((result["status"], driver.actions), ("approval_denied", []))

    def test_a_reminder_row_named_call_is_not_put_to_the_user_as_a_call(self):
        """QA-2 live (f27d229): "Delete the reminder Call the dentist" asked "Place this call?" at the row."""
        row = Element("0", "Call the dentist, Incomplete", "Cell", (0, .19, .995, .046))
        result, driver, requests, events = self.run_with(SureSendModel(), approve="approved", driver=RowDriver(row))
        self.assertEqual([r["title"] for r in requests if r["kind"] == "commit"], [])
        self.assertEqual(driver.actions[0], "TAP")
        # The Phone app's Call button still asks, and says what it does.
        call = Element("0", "Call", "Button", (.4, .8, .2, .08))
        result, driver, requests, _ = self.run_with(SureSendModel(), approve="denied", driver=RowDriver(call))
        self.assertEqual((requests[0]["kind"], requests[0]["act"], requests[0]["title"]), ("commit", "call", "Place this call?"))
        self.assertEqual((result["status"], driver.actions), ("approval_denied", []))

    def test_checkout_text_inside_its_button_is_put_to_the_user(self):
        """Review 2 of #41: Fast tapped the StaticText of WebKit's "Place your order" button and the order went
        through unasked, sure (demoted, then lifted) or confident alike. The text reads as its button."""
        text = Element("0", "Place your order", "StaticText", (.3, .72, .4, .03))
        button = Element("9", "Place your order", "Button", (.05, .71, .9, .05))
        for model in (SureSendModel(), ConfidentModel()):
            result, driver, requests, _ = self.run_with(model, approve="denied", driver=RowDriver(text, button))
            self.assertEqual([(r["kind"], r["act"], r["title"], r["role"]) for r in requests],
                             [("commit", "order", "Place this order?", "Button")], type(model).__name__)
            self.assertEqual((result["status"], driver.actions), ("approval_denied", []), type(model).__name__)

    def test_money_and_message_rows_are_put_to_the_user(self):
        """Review 2 of #41: each of these dispatched with no approval request on 26242af."""
        for element, title in ((Element("0", "Confirm and pay", "StaticText", (.3, .72, .4, .03)), "Confirm this?"),
                               (Element("0", "Send My Current Location", "Cell", (0, .5, 1, .05)), "Send this message?"),
                               (Element("0", "Transfer to Checking", "Cell", (0, .5, 1, .05)), "Make this transfer?")):
            result, driver, requests, _ = self.run_with(SureSendModel(), approve="denied", driver=RowDriver(element))
            self.assertEqual([(r["kind"], r["title"]) for r in requests], [("commit", title)], element.label)
            self.assertEqual((result["status"], driver.actions), ("approval_denied", []), element.label)

    def test_a_reminders_title_inside_its_row_is_not_put_to_the_user(self):
        """The title "Pay the rent on Friday" sits inside its reminder's row: tapping it pays nothing."""
        title = Element("0", "Pay the rent on Friday", "StaticText", (.1, .51, .5, .03))
        row = Element("9", "Pay the rent on Friday, Incomplete", "Cell", (0, .5, .995, .06))
        _, driver, requests, _ = self.run_with(SureSendModel(), approve="approved", driver=RowDriver(title, row))
        self.assertEqual([r["title"] for r in requests if r["kind"] == "commit"], [])
        self.assertEqual(driver.actions[0], "TAP")

    def test_a_row_that_blocks_a_caller_is_put_to_the_user(self):
        """Review of #41: Phone's "Block this Caller" is a Cell of three words, and it asks before the tap."""
        row = Element("0", "Block this Caller", "Cell", (0, .6, .995, .05))
        result, driver, requests, _ = self.run_with(SureSendModel(), approve="denied", driver=RowDriver(row))
        self.assertEqual((requests[0]["kind"], requests[0]["act"], requests[0]["title"]),
                         ("commit", "block", "Block this contact?"))
        self.assertEqual((result["status"], driver.actions), ("approval_denied", []))

    def test_a_fairly_sure_tap_that_commits_nothing_goes_to_the_action_check_not_the_user(self):
        _, driver, requests, events = self.run_with(SureSendModel(), approve="approved")
        self.assertEqual((driver.actions[0], requests), ("TAP", []))
        self.assertIn("demotion_lifted", events)
        self.assertIn("action_check", events)

    def test_with_nobody_to_ask_and_no_bypass_it_still_waits(self):
        # It waits out the run budget: without the stub, the wait backoff sleeps 54 s of it.
        with unittest.mock.patch("mobile_agent.agent.time.sleep"):
            _, driver, _, events = self.run_with(SureSendModel())
        self.assertEqual(driver.actions, [])
        self.assertNotIn("demotion_lifted", events)

    def test_bypass_takes_it_without_asking(self):
        _, driver, requests, events = self.run_with(SureSendModel(), bypass=True)
        self.assertEqual(driver.actions[0], "TAP")
        self.assertEqual(requests, [])
        self.assertIn("demotion_lifted", events)

    def test_an_unclear_check_is_put_to_the_user(self):
        _, driver, requests, events = self.run_with(UnsureCheckModel(), approve="approved")
        self.assertEqual(driver.actions[0], "TAP")
        self.assertIn("unclear_put_to_user", events)
        self.assertEqual((requests[0]["operation"], requests[0]["kind"]), ("TAP", "unsure"))
        self.assertEqual(requests[0]["title"], "Not sure about this step")

    def test_stopping_at_an_unsure_step_stops_the_task_without_acting(self):
        result, driver, _, _ = self.run_with(UnsureCheckModel(), approve="denied")
        self.assertEqual((result["status"], result["outcome"], driver.actions), ("stopped", "stopped", []))

    def test_bypass_lets_an_unclear_check_through(self):
        _, driver, requests, events = self.run_with(UnsureCheckModel(), bypass=True)
        self.assertEqual(driver.actions[0], "TAP")
        self.assertIn("unclear_bypassed", events)
        self.assertEqual(requests, [])

    def test_without_either_an_unclear_check_still_stops_the_task(self):
        result, driver, _, _ = self.run_with(UnsureCheckModel())
        self.assertEqual((result["status"], driver.actions), ("needs_clarification", []))


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


if __name__ == "__main__":
    unittest.main()


class ApprovalRouteTests(unittest.TestCase):
    def setUp(self):
        import http.client, json, os, tempfile
        from types import SimpleNamespace
        from unittest.mock import patch
        from mobile_agent.server import BoundedServer, Runtime, make_handler
        self.http, self.json = http.client, json
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
        connection = self.http.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        payload = self.json.dumps(body) if body is not None else None
        connection.request(method, path, payload, {"Host": "127.0.0.1:8765", "Content-Type": "application/json"})
        response = connection.getresponse()
        data = self.json.loads(response.read())
        connection.close()
        return response.status, data

    def safety(self, reply):
        status, body = reply
        return status, {key: body[key] for key in ("askBeforeActing", "bypassChecks")} if status == 200 else body

    def test_ask_before_acting_defaults_on_and_persists_when_turned_off(self):
        self.assertEqual(self.safety(self.call("GET", "/api/settings")),
                         (200, {"askBeforeActing": True, "bypassChecks": False}))
        self.assertEqual(self.safety(self.call("POST", "/api/settings", {"askBeforeActing": False})),
                         (200, {"askBeforeActing": False, "bypassChecks": False}))
        with open(self.env_file) as stream:
            self.assertIn("MOBSTER_ASK_BEFORE_ACTING=0", stream.read())
        self.assertEqual(self.call("POST", "/api/settings", {"askBeforeActing": "no"})[0], 400)
        self.assertEqual(self.call("POST", "/api/settings", {"other": True})[0], 400)

    def test_bypass_defaults_off_and_persists_when_turned_on(self):
        self.assertEqual(self.safety(self.call("POST", "/api/settings", {"bypassChecks": True})),
                         (200, {"askBeforeActing": True, "bypassChecks": True}))
        with open(self.env_file) as stream:
            self.assertIn("MOBSTER_BYPASS_CHECKS=1", stream.read())
        self.assertEqual(self.call("POST", "/api/settings", {"bypassChecks": 1})[0], 400)

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
