"""Proof-gated completion (contract.py): the contract, the declared-commit policy, receipts and the DONE gate.
Offline; cases are taken from the recorded iOSWorld runs F1 replayed (26 Sep)."""

import unittest
from types import SimpleNamespace as NS

from mobile_agent import contract as K
from mobile_agent.frontier import FrontierAgent, prompt_text
from mobile_agent.state import Element, Snapshot


def el(label, role="Button", y=.5, x=.1, w=.3, h=.04, editable=False, value=""):
    return NS(label=label, role=role, rect=(x, y, w, h), editable=editable, value=value)


def shot(*elements, keyboard=""):
    return NS(elements=list(elements), keyboard=keyboard)


def item(kind="COMMIT", app="SplitPay", what="x", act="none", quote="", count="1", condition="", payload=""):
    return {"kind": kind, "app": app, "what": what, "payload": payload, "act": act, "count": count,
            "condition": condition, "quote": quote}


def contract(request, *items):
    return K.Contract.from_items(list(items), request)


PAY = "Review SplitPay balance and pay any pending requests under $50."
PAY_ITEM = item(what="pending requests under $50", act="pay", count="each", condition="pending request is under $50",
                quote="pay any pending requests under $50.")


class ContractValidationTests(unittest.TestCase):
    def test_a_commit_must_quote_the_request_verbatim(self):
        c = contract(PAY, PAY_ITEM, item(what="Maya", act="pay", quote="pay Maya back"),
                     item(what="x", act="teleport", quote="pay any"))
        self.assertEqual([i.what for i in c.items], ["pending requests under $50"])
        self.assertEqual(len(c.dropped), 2)

    def test_quote_check_ignores_case_quotes_and_spacing(self):
        c = contract("Message ‘Brunch Crew’  with the time.", item(app="QuickChat", what="Brunch Crew",
                                                                  act="send_message", quote="message 'brunch crew' with"))
        self.assertEqual(len(c.items), 1)

    def test_forbid_items_are_kept_and_refuse_their_act(self):
        c = contract("Clean up my inbox but do not delete anything.",
                     item(kind="FORBID", app="Mail", what="emails", act="delete", quote="do not delete anything"))
        g = c.check("TAP", el("Delete"), shot(), "Mail")
        self.assertFalse(g.allowed)
        self.assertIn("rules it out", g.reason)


class VerbTests(unittest.TestCase):
    def test_first_four_words_name_the_act(self):
        self.assertEqual(K.label_verbs("Pay $42.00"), {"pay"})
        self.assertEqual(K.label_verbs("Confirm & Place Order"), {"confirm", "place order"})
        self.assertEqual(K.label_verbs("A film about the Ocean's call"), set())

    def test_known_false_triggers(self):
        self.assertEqual(K.label_verbs("Dentist, 490 Post St, San Francisco"), set())
        self.assertEqual(K.label_verbs("pencil.tip"), set())
        self.assertEqual(K.label_verbs("9:30, Client call, Weekdays", "Switch"), set())
        self.assertEqual(K.label_verbs("Feels Like"), set())
        self.assertEqual(K.label_verbs("Your requests"), set())
        self.assertEqual(K.label_verbs("CityRide One, Save on rides"), set())

    def test_added_words_count_as_the_first_word(self):
        self.assertEqual(K.label_verbs("Like"), {"like"})
        self.assertEqual(K.label_verbs("Save post"), {"save", "post"})
        self.assertEqual(K.label_verbs("Request $15.00"), {"request"})
        self.assertEqual(K.label_verbs("Cancel Ride"), {"cancel"})
        self.assertEqual(K.label_verbs("archive"), {"archive"})
        self.assertEqual(K.label_verbs("Unfollow"), {"unfollow"})
        self.assertEqual(K.label_verbs("Following"), {"unfollow"})
        self.assertEqual(K.label_verbs("React"), {"react"})

    def test_identifier_labels_are_split(self):
        self.assertEqual(K.label_verbs("mybank.checkout.confirmToggle", "Switch"), {"checkout", "confirm"})

    def test_sheet_openers_commit_nothing(self):
        for label in ("Share", "Comment", "Comments", "Reply", "Reply in thread", "Cancel", "Comment Left"):
            self.assertEqual(K.label_verbs(label), set(), label)


class FamilyTests(unittest.TestCase):
    TABLE = {
        "send_message": ({"Send", "Post", "Publish", "Reply", "Comment on post", "Tweet", "Submit"}, {"Pay"}),
        "pay": ({"Pay", "Send", "Transfer", "Confirm", "Submit"}, {"Place Order", "Request"}),
        "request_money": ({"Request", "Send", "Confirm", "Submit"}, {"Pay"}),
        "transfer": ({"Transfer", "Send", "Confirm", "Submit"}, {"Pay"}),
        "order": ({"Order", "Place Order", "Buy", "Purchase", "Checkout", "Pay", "Confirm", "Submit", "Tip"}, {"Send"}),
        "book": ({"Book", "Reserve", "Confirm", "Submit", "Request CityRideX"}, {"Pay"}),
        "follow": ({"Follow", "Subscribe"}, {"Unfollow", "Following"}),
        "share": ({"Share trip status", "Send"}, {"Post"}),
        "call": ({"Call", "Dial", "FaceTime"}, {"Send"}),
        "delete": ({"Delete", "Erase", "Remove"}, {"Archive"}),
        "react": ({"Like", "React"}, {"Send"}),
    }

    def test_each_act_allows_its_verbs_and_no_others(self):
        request = "do the thing"
        for act, (allowed, refused) in self.TABLE.items():
            c = contract(request, item(app="App", what="the thing", act=act, quote="do the thing"))  # no names
            typed = [el("hello there", "TextField", editable=True, value="hello there")]
            c.record_typed("App", typed[0], "hello there", 0)
            for label in allowed:
                self.assertTrue(c.check("TAP", el(label), shot(*typed), "App").allowed, (act, label))
            for label in refused:
                self.assertFalse(c.check("TAP", el(label), shot(*typed), "App").allowed, (act, label))

    def test_other_allows_the_verbs_its_quote_uses(self):
        c = contract("Find the Netflix job and save it.",
                     item(app="LockedIn", what="Platform Engineering Lead posting", act="other", quote="save it"))
        c.items[0].what = ""
        self.assertTrue(c.check("TAP", el("Save"), shot(), "LockedIn").allowed)
        self.assertFalse(c.check("TAP", el("Delete"), shot(), "LockedIn").allowed)

    def test_the_commit_must_be_in_the_current_app(self):
        c = contract(PAY, PAY_ITEM)
        self.assertTrue(c.check("TAP", el("Pay"), shot(), "SplitPay").allowed)
        self.assertFalse(c.check("TAP", el("Pay"), shot(), "MyBank").allowed)

    def test_save_completes_a_write_in_its_app_only(self):
        c = contract("Set a 6:45 AM alarm labeled 'Gym'.",
                     item(kind="WRITE", app="Clock", what="new alarm", payload="6:45 AM", quote="Set a 6:45 AM alarm"))
        self.assertTrue(c.check("TAP", el("Save"), shot(), "Clock").allowed)
        self.assertFalse(c.check("TAP", el("Save post"), shot(), "LockedIn").allowed)

    def test_an_alarm_switch_passes_only_when_the_request_names_that_alarm(self):
        switch = el("9:30, Client call, Weekdays", "Switch")
        c = contract("Set a 6:45 AM alarm labeled 'Gym'.",
                     item(kind="WRITE", app="Clock", what="new alarm", payload="6:45 AM", quote="Set a 6:45 AM alarm"))
        self.assertFalse(c.check("TAP", switch, shot(switch), "Clock").allowed)
        c = contract("Turn off my Client call alarm.",
                     item(kind="WRITE", app="Clock", what="Client call alarm switch", quote="Turn off my Client call alarm"))
        self.assertTrue(c.check("TAP", switch, shot(switch), "Clock").allowed)

    def test_long_press_is_gated_like_a_tap(self):
        c = contract(PAY, PAY_ITEM)
        self.assertFalse(c.check("LONG_PRESS", el("Post comment"), shot(), "SplitPay").allowed)


class AmountTests(unittest.TestCase):
    def test_label_amount_outside_the_bound_is_refused(self):
        c = contract(PAY, PAY_ITEM)
        self.assertTrue(c.check("TAP", el("Pay $42.00"), shot(), "SplitPay").allowed)
        g = c.check("TAP", el("Pay $52.00"), shot(), "SplitPay")
        self.assertFalse(g.allowed)
        self.assertIn("$52.00", g.reason)

    def test_amount_drawn_apart_from_its_dollar_sign(self):
        c = contract(PAY, PAY_ITEM)
        pay = el("Pay", y=.59)
        keypad = [el("Maya Patel", "StaticText", y=.20), el("$", "StaticText", y=.26, x=.34),
                  el("52", "StaticText", y=.24, x=.39, h=.07), pay]
        self.assertFalse(c.check("TAP", pay, shot(*keypad), "SplitPay").allowed)
        keypad[2].label = "42"
        self.assertTrue(c.check("TAP", pay, shot(*keypad), "SplitPay").allowed)

    def test_nearest_amount_to_a_list_row_button(self):
        c = contract(PAY, PAY_ITEM)
        rows = [el("Maya Patel requested", "StaticText", y=.37), el("$42.00", "StaticText", y=.38, x=.78),
                el("Pay", y=.43), el("Kai Santos requested", "StaticText", y=.51),
                el("$63.00", "StaticText", y=.52, x=.78), el("Pay", y=.57)]
        self.assertTrue(c.check("TAP", rows[2], shot(*rows), "SplitPay").allowed)
        self.assertFalse(c.check("TAP", rows[5], shot(*rows), "SplitPay").allowed)

    def test_one_named_amount_is_exact_for_money_requests(self):
        request = "Send SplitPay requests to each attendee for $15 from last month's brunch."
        c = contract(request, item(what="$15 request to each attendee", act="request_money", count="each",
                                   quote="Send SplitPay requests to each attendee for $15"))
        self.assertTrue(c.check("TAP", el("Request $15.00"), shot(), "SplitPay").allowed)
        self.assertFalse(c.check("TAP", el("Request $16.00"), shot(), "SplitPay").allowed)

    def test_bounds_parse(self):
        low, high = K.bounds(K.Item(1, "COMMIT", act="pay", quote="pay anything at most $20"))
        self.assertEqual((low, high), (None, 20.0))
        low, high = K.bounds(K.Item(1, "COMMIT", act="order", condition="over $100"))
        self.assertGreater(low, 100)


class BindingTests(unittest.TestCase):
    REQ = "Read the InMail from Rachel and compose a professional reply."

    def reply_contract(self):
        return contract(self.REQ, item(app="LockedIn", what="Reply to Rachel Torres", act="send_message",
                                       quote="compose a professional reply"))

    def test_a_feed_send_with_no_typed_text_is_refused(self):
        c = self.reply_contract()
        g = c.check("TAP", el("Send"), shot(el("Great post about RAG", "StaticText")), "LockedIn")
        self.assertFalse(g.allowed)
        self.assertIn("no text you typed", g.reason)

    def test_a_send_beside_the_typed_reply_passes(self):
        c = self.reply_contract()
        field = el("chat_message_field", "TextField", editable=True, value="Hi Rachel, thanks for reaching out")
        c.record_typed("LockedIn", field, "Hi Rachel, thanks for reaching out", 3)
        self.assertTrue(c.check("TAP", el("Send"), shot(field), "LockedIn").allowed)

    def test_text_typed_earlier_in_the_chain_binds(self):
        c = self.reply_contract()
        g = c.check("TAP", el("Send"), shot(), "LockedIn", chain_typed=("Hi Rachel",))
        self.assertTrue(g.allowed)
        self.assertEqual(g.payload, "Hi Rachel")

    def test_follow_needs_its_object_on_screen(self):
        c = contract("Tap 'Follow' on Netflix from the job detail.",
                     item(app="LockedIn", what="Netflix company page", act="follow",
                          quote="Tap 'Follow' on Netflix from the job detail"))
        self.assertFalse(c.check("TAP", el("Follow"), shot(el("Devi Anand", "StaticText")), "LockedIn").allowed)
        self.assertTrue(c.check("TAP", el("Follow"), shot(el("Netflix", "StaticText")), "LockedIn").allowed)


class ReturnKeyTests(unittest.TestCase):
    REQ = "Message Leo Chen in QuickChat with the restaurant name."

    def test_return_in_a_chat_composer_is_a_send(self):
        field = el("chat_compose_field", "TextField", editable=True, value="Message")
        self.assertFalse(contract(self.REQ).check("TYPE_SUBMIT", field, shot(field), "QuickChat", text="hi").allowed)
        c = contract(self.REQ, item(app="QuickChat", what="message to Leo Chen", act="send_message",
                                    quote="Message Leo Chen"))
        c.items[0].what = "message to leo"  # object binding does not apply to messages
        g = c.check("TYPE_SUBMIT", field, shot(field), "QuickChat", text="Dinner at Embarcadero Grill")
        self.assertTrue(g.allowed)
        self.assertEqual(g.payload, "Dinner at Embarcadero Grill")

    def test_search_and_note_fields_are_not_composers(self):
        c = contract(self.REQ)
        for field in (el("Search Mail", "SearchField", editable=True), el("", "TextField", editable=True, value="Search on TicketBox"),
                      el("notes_body_editor", "TextView", editable=True), el("recipient_search_field", "TextField", editable=True)):
            self.assertTrue(c.check("SUBMIT", field, shot(field), "QuickChat").allowed, field.label)

    def test_an_unlabelled_field_beside_a_post_button_is_a_composer(self):
        field = el("", "TextField", editable=True, value="March summary", y=.80)
        post = el("Post comment", y=.80, x=.7)
        self.assertTrue(K.composer_field(field, [field, post], ("March summary",)))
        self.assertFalse(contract("Open the Budget Tracker doc.").check("SUBMIT", field, shot(field, post), "CloudDocs").allowed)


class ReceiptTests(unittest.TestCase):
    def test_message_receipt_needs_the_text_shown_and_the_composer_cleared(self):
        c = contract("Message Leo Chen in QuickChat.", item(app="QuickChat", what="message", act="send_message",
                                                           quote="Message Leo Chen"))
        field = el("chat_compose_field", "TextField", editable=True, value="See you at 7")
        c.record_typed("QuickChat", field, "See you at 7", 1)
        before = shot(field)
        g = c.check("TAP", el("Send"), before, "QuickChat")
        c.committed(g, el("Send"), before, "QuickChat", 1)
        c.observe(shot(field), "QuickChat", 1)  # still in the composer
        self.assertFalse(c.items[0].receipts)
        c.observe(shot(el("See you at 7", "StaticText"), el("chat_compose_field", "TextField", editable=True,
                                                            value="Message")), "QuickChat", 2)
        self.assertEqual(c.items[0].status, "closed")
        self.assertFalse(c.check("TAP", el("Send"), shot(field), "QuickChat").allowed)  # a repeat

    def test_order_receipt_is_a_new_confirmation_row(self):
        c = contract("Order a salad from QuickBite.", item(app="QuickBite", what="salad", act="order", quote="Order a salad"))
        c.items[0].what = ""
        before = shot(el("Visa saved card", "StaticText"), el("Place Order"))
        g = c.check("TAP", el("Place Order"), before, "QuickBite")
        c.committed(g, el("Place Order"), before, "QuickBite", 4)
        c.observe(shot(el("Visa saved card", "StaticText"), el("I confirm this charge", "StaticText")), "QuickBite", 4)
        self.assertFalse(c.items[0].receipts)
        c.observe(shot(el("Order placed! Arriving 12:40", "StaticText")), "QuickBite", 5)
        self.assertEqual(c.items[0].status, "closed")

    def test_follow_closes_and_a_second_tap_would_undo_it(self):
        c = contract("Follow Netflix.", item(app="LockedIn", what="Netflix", act="follow", quote="Follow Netflix"))
        page = shot(el("Netflix", "StaticText"), el("Follow"))
        g = c.check("TAP", el("Follow"), page, "LockedIn")
        c.committed(g, el("Follow"), page, "LockedIn", 2)
        c.observe(shot(el("Netflix", "StaticText"), el("Following")), "LockedIn", 3)
        self.assertEqual(c.items[0].status, "closed")
        self.assertFalse(c.check("TAP", el("Following"), page, "LockedIn").allowed)
        self.assertFalse(c.check("TAP", el("Follow"), page, "LockedIn").allowed)

    def test_each_items_stay_open(self):
        c = contract(PAY, PAY_ITEM)
        for step, (name, amount) in enumerate((("Maya Patel", "$42.00"), ("Kai Santos", "$33.00"))):
            screen = shot(el(f"Pay {name} {amount} using SplitPay balance?", "StaticText", y=.48), el("Pay", y=.54))
            g = c.check("TAP", screen.elements[1], screen, "SplitPay")
            self.assertTrue(g.allowed, name)
            c.committed(g, screen.elements[1], screen, "SplitPay", step)
            c.observe(shot(el(f"You paid {name} {amount}", "StaticText")), "SplitPay", step)
        self.assertEqual(len(c.items[0].receipts), 2)
        self.assertEqual(c.items[0].status, "open")

    def test_a_request_still_pending_after_a_direct_payment_is_not_paid_again(self):
        c = contract(PAY, PAY_ITEM)
        confirm = shot(el("Pay Maya Patel", "StaticText", y=.58), el("$42.00", "StaticText", y=.65), el("Pay $42.00", y=.88))
        g = c.check("TAP", confirm.elements[2], confirm, "SplitPay")
        c.committed(g, confirm.elements[2], confirm, "SplitPay", 21)
        c.observe(shot(el("Payment sent", "StaticText")), "SplitPay", 21)
        pending = shot(el("Maya Patel requested", "StaticText", y=.37), el("$42.00", "StaticText", y=.38), el("Pay", y=.43))
        g = c.check("TAP", pending.elements[2], pending, "SplitPay")
        self.assertFalse(g.allowed)
        self.assertIn("already paid", g.reason)


class DoneGateTests(unittest.TestCase):
    REQ = "Check my SplitPay balance and add a comment to the 'Budget Tracker' doc. What is the balance?"

    def c(self):
        return contract(self.REQ,
                        item(kind="READ", app="SplitPay", what="balance", quote="Check my SplitPay balance"),
                        item(kind="READ", app="CloudDocs", what="'Budget Tracker' doc", quote="the 'Budget Tracker' doc"),
                        item(app="CloudDocs", what="comment", act="comment", quote="add a comment"),
                        item(kind="REPORT", app="SplitPay", what="SplitPay balance", quote="What is the balance?"),
                        item(kind="REPORT", app="CloudDocs", what="confirmation that the comment was added",
                             quote="add a comment"))

    def test_nothing_done_reopens_every_item_with_a_reason(self):
        c = self.c()
        ok, feedback, _ = c.done_gate("Done.", 30)
        self.assertFalse(ok)
        self.assertIn("SplitPay not opened yet", feedback)
        self.assertIn("not done yet", feedback)
        self.assertIn("not reported yet", feedback)

    def test_every_item_proven_is_accepted_and_values_come_from_the_ledger(self):
        c = self.c()
        c.observe(shot(el("Balance $1,234.56", "StaticText")), "SplitPay", 1)
        c.update([{"id": 4, "status": "found", "value": "$1,234.56"}], 1)
        doc = shot(el("Budget Tracker", "StaticText"), el("", "TextField", editable=True, value="Add a comment", y=.8),
                   el("Post comment", y=.8, x=.7))
        c.observe(doc, "CloudDocs", 2)
        c.record_typed("CloudDocs", doc.elements[1], "March summary: net $900", 2)
        field = el("", "TextField", editable=True, value="March summary: net $900", y=.8)
        before = shot(doc.elements[0], field, doc.elements[2])
        g = c.check("TAP", doc.elements[2], before, "CloudDocs")
        self.assertTrue(g.allowed)
        c.committed(g, doc.elements[2], before, "CloudDocs", 3)
        c.observe(shot(el("Budget Tracker", "StaticText"), el("March summary: net $900", "StaticText")), "CloudDocs", 3)
        ok, feedback, answer = c.done_gate("The comment is posted.", 30)
        self.assertTrue(ok, feedback)
        self.assertIn("- SplitPay balance: $1,234.56", answer)  # left out of the answer: added from the ledger
        ok, _, answer = c.done_gate("Balance $1,234.56; comment posted.", 30)
        self.assertEqual(answer, "Balance $1,234.56; comment posted.")

    def test_a_value_never_on_screen_is_reopened(self):
        c = self.c()
        c.observe(shot(el("Balance $1,234.56", "StaticText")), "SplitPay", 1)
        c.update([{"id": 4, "status": "found", "value": "$1,500.00"}], 1)
        ok, feedback, _ = c.done_gate("", 30)
        self.assertIn("$1,500.00 not seen", feedback)

    def test_a_value_seen_only_after_it_was_recorded_is_not_its_source(self):
        c = self.c()
        c.update([{"id": 4, "status": "found", "value": "$1,234.56"}], 1)
        c.observe(shot(el("Balance $1,234.56", "StaticText")), "SplitPay", 2)
        self.assertFalse(c.proof(c.items[3])[0])

    def test_computed_values_and_prose_need_no_single_source(self):
        c = contract("What is my net liquid cash?", item(kind="REPORT", app="", what="net liquid cash calculation",
                                                         quote="net liquid cash"))
        c.update([{"id": 1, "status": "found", "value": "$4,321.00"}], 3)
        self.assertTrue(c.proof(c.items[0])[0])

    def test_none_found_needs_an_empty_search_on_screen(self):
        c = contract("Did anyone email me about the lease?",
                     item(kind="REPORT", app="Mail", what="emails about the lease", quote="email me about the lease"))
        c.observe(shot(el("Inbox", "StaticText")), "Mail", 1)
        c.update([{"id": 1, "status": "found", "value": "None found"}], 1)
        self.assertIn("needs proof", c.proof(c.items[0])[1])
        field = el("Search Mail", "SearchField", editable=True, value="lease", y=.1)
        c.record_typed("Mail", field, "lease", 2)
        c.observe(shot(field, el("No Results", "StaticText", y=.4)), "Mail", 2)
        self.assertTrue(c.proof(c.items[0])[0])

    def test_named_source_and_mail_folder_must_be_seen(self):
        c = contract("Check my sent mail and the 'Budget Tracker' doc.",
                     item(kind="READ", app="Mail", what="Sent folder", quote="sent mail"),
                     item(kind="READ", app="CloudDocs", what="'Budget Tracker' doc", quote="the 'Budget Tracker' doc"))
        c.observe(shot(el("Inbox", "StaticText")), "Mail", 1)
        c.observe(shot(el("Recents", "StaticText")), "CloudDocs", 2)
        self.assertFalse(c.proof(c.items[0])[0])
        self.assertFalse(c.proof(c.items[1])[0])
        c.observe(shot(el("Sent", "StaticText")), "Mail", 3)
        c.observe(shot(el("Budget Tracker", "Cell")), "CloudDocs", 4)
        self.assertTrue(c.proof(c.items[0])[0])
        self.assertTrue(c.proof(c.items[1])[0])

    def test_write_reads_back_after_the_keyboard_is_down(self):
        c = contract("Create a note titled 'Tech Reading Queue'.",
                     item(kind="WRITE", app="Notes", what="note title", payload="Tech Reading Queue",
                          quote="titled 'Tech Reading Queue'"))
        field = el("notes_title_field", "TextField", editable=True, value="Tech Reading Queue")
        c.record_typed("Notes", field, "Tech Reading Queue", 1)
        c.observe(shot(field, keyboard="visible"), "Notes", 1)
        self.assertFalse(c.proof(c.items[0])[0])
        c.observe(shot(el("Tech Reading Queue", "Cell")), "Notes", 2)
        self.assertTrue(c.proof(c.items[0])[0])

    def test_low_turn_fallback_accepts_and_names_unproven_actions_only(self):
        c = self.c()
        c.update([{"id": 4, "status": "found", "value": "$9.99"}], 1)
        ok, _, answer = c.done_gate("Balance $9.99.", K.LOW_TURNS)
        self.assertTrue(ok)
        self.assertIn("Not confirmed on screen: comment.", answer)
        self.assertNotIn("SplitPay balance", answer.split("Not confirmed")[1])

    def test_an_item_reopened_too_often_stops_blocking(self):
        c = contract("What is the balance?", item(kind="REPORT", app="", what="balance", quote="What is the balance?"))
        c.update([{"id": 1, "status": "found", "value": "$77.00"}], 1)
        results = [c.done_gate("Balance $77.00", 30)[0] for _ in range(K.MAX_REOPENS + 1)]
        self.assertEqual(results, [False] * K.MAX_REOPENS + [True])

    def test_conditional_commit_not_triggered_is_fine_when_marked(self):
        c = contract("If rain is forecast, message the group.",
                     item(app="QuickChat", what="group", act="send_message", condition="rain is forecast",
                          quote="message the group"))
        self.assertFalse(c.proof(c.items[0])[0])
        c.update([{"id": 1, "status": "missing", "value": "clear skies"}], 1)
        self.assertTrue(c.proof(c.items[0])[0])


class CompileTests(unittest.TestCase):
    class Client:
        def __init__(self, reply=None, fail=False):
            self.reasoning, self.reply, self.fail, self.seen = "low", reply, fail, []

        def complete(self, messages, schema, timeout=60):
            self.seen.append((self.reasoning, messages, schema))
            if self.fail:
                raise RuntimeError("down")
            return self.reply, {}

    def test_compiles_at_reasoning_none_with_the_v2_rule(self):
        client = self.Client({"items": [item(app="QuickChat", what="Leo", act="send_message", quote="message Leo")]})
        c = K.compile_contract(client, "Please message Leo.", ["QuickChat"])
        self.assertEqual(client.seen[0][0], "none")
        self.assertEqual(client.reasoning, "low")
        self.assertIn("is also to be sent or posted, unless the request says not", client.seen[0][1][0]["content"])
        self.assertIn("FORBID", client.seen[0][2]["properties"]["items"]["items"]["properties"]["kind"]["enum"])
        self.assertEqual(len(c.items), 1)

    def test_failure_or_nothing_valid_gives_none(self):
        self.assertIsNone(K.compile_contract(self.Client(fail=True), "x"))
        self.assertIsNone(K.compile_contract(self.Client({"items": [{"kind": "do", "text": "x"}]}), "x"))


# ------------------------------------------------------------------ the agent loop

def screen(*elements, bundle="com.example.chat"):
    return Snapshot(list(elements), "\n".join(e.label for e in elements), 400, 800, "synthetic_fixture",
                    bundle_id=bundle)


class Driver:
    def __init__(self, screens):
        self.screens, self.index, self.actions = list(screens), 0, []

    def observe(self, timeout=10):
        return self.screens[min(self.index, len(self.screens) - 1)]

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        self.actions.append((operation, getattr(target, "label", target), text))
        self.index += 1


class Script:
    """A model answering from a list of chunks; the contract call returns ``items``."""

    def __init__(self, steps, items, updates=None):
        self.steps, self.items, self.updates, self.prompts = list(steps), items, updates or {}, []
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "calls": 0}

    def complete(self, messages, schema, timeout=60):
        if "items" in schema["properties"]:
            return {"items": self.items}, {}
        self.usage["calls"] += 1
        prompt = prompt_text(messages)
        self.prompts.append(prompt)
        rows = prompt.split("Screen elements:\n", 1)[1].splitlines()
        chunk = self.steps.pop(0)
        actions = [{"operation": op, "target": next((r.split()[0] for r in rows if label and f'"{label}"' in r), None)
                    if i == 0 else None, "target_label": label if i else None, "text": text, "app": None}
                   for i, (op, label, text) in enumerate(chunk)]
        return {"thought": "", "plan": "", "notes_add": [], "answer": "Sent." if chunk[-1][0] == "DONE" else None,
                "checklist_updates": self.updates.get(self.usage["calls"], []), "actions": actions}, {}


class AgentLoopTests(unittest.TestCase):
    REQ = "Message Leo in QuickChat that dinner is at 7."
    ITEMS = [item(app="QuickChat", what="message to Leo", act="send_message", quote="Message Leo in QuickChat")]

    def run_agent(self, screens, steps, items=None):
        driver, script, events = Driver(screens), Script(steps, self.ITEMS if items is None else items), []
        result = FrontierAgent(driver, script, apps={"com.example.chat": "QuickChat"}, emit=events.append,
                               screenshots=False, settle_seconds=0).run(self.REQ)
        return driver, script, result, events

    def test_feed_send_refused_then_typed_send_runs_and_done_waits_for_the_receipt(self):
        feed = screen(Element("1", "Send", "Button", (.8, .3, .1, .04)),
                      Element("2", "Message", "TextField", (.1, .9, .6, .04), editable=True, value="Message"))
        sent = screen(Element("3", "Dinner is at 7", "StaticText", (.1, .5, .6, .04)),
                      Element("2", "Message", "TextField", (.1, .9, .6, .04), editable=True, value="Message"))
        driver, script, result, events = self.run_agent(
            [feed, feed, sent, sent],
            [[("TAP", "Send", None)], [("DONE", None, None)],
             [("TYPE", "Message", "Dinner is at 7"), ("TAP", "Send", None)], [("DONE", None, None)]])
        self.assertEqual(driver.actions, [("TYPE", "Message", "Dinner is at 7"), ("TAP", "Send", None)])
        self.assertIn("no text you typed", script.prompts[1])
        self.assertIn("DONE refused", script.prompts[2])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["contract"]["receipted"], 1)
        self.assertNotIn("item by item", "".join(script.prompts))  # no DONE_CHECK re-ask
        self.assertIn("Task contract", script.prompts[0])
        self.assertIn("frontier_contract", [e["event"] for e in events])

    def test_without_a_contract_the_regex_guard_stays(self):
        feed = screen(Element("1", "Delete", "Button", (.8, .3, .1, .04)))
        driver, _, result, events = self.run_agent([feed] * 3, [[("TAP", "Delete", None)], [("DONE", None, None)],
                                                                [("DONE", None, None)]], items=[])
        self.assertEqual(driver.actions, [])
        self.assertIn("frontier_checklist", [e["event"] for e in events])



# ------------------------------------------------------------------ App Store installs (C3) and "no" answers (B6)

import json as _json
from pathlib import Path as _Path


def _store(name):
    data = _json.loads((_Path(__file__).parent / "fixtures" / "frontier" / f"{name}.json").read_text())
    return {key: Snapshot([Element(r["id"], r["label"], r["role"], tuple(r["rect"]), value=r.get("value", ""))
                           for r in rows], "", 400, 800, "synthetic_fixture", bundle_id=data["bundle"])
            for key, rows in data["screens"].items()}


class InstallActTests(unittest.TestCase):
    SCREENS = _store("app_store_page")
    DUOLINGO = item(app="App Store", what="Duolingo", act="install", quote="Install Duolingo")

    def gate(self, label, screen_name="page", items=None, app="App Store"):
        snapshot = self.SCREENS[screen_name]
        element = next(e for e in snapshot.elements if e.label == label and (e.rect[1] < .3 or label != "Get"))
        c = contract("Install Duolingo from the App Store.", *(items or [self.DUOLINGO]))
        return c, c.check("TAP", element, snapshot, app)

    def test_get_beside_the_named_app_is_the_declared_install_and_asks_with_its_name(self):
        _, gate = self.gate("Get")
        self.assertTrue(gate.allowed)
        self.assertEqual(gate.item.act, "install")
        self.assertTrue(K.asks(gate.item))
        self.assertEqual(K.approval_title(gate.item, gate.amount), "Install Duolingo from the App Store?")

    def test_the_ads_get_button_is_refused(self):
        snapshot = self.SCREENS["page"]
        ad_get = [e for e in snapshot.elements if e.label == "Get"][1]
        c = contract("Install Duolingo from the App Store.", self.DUOLINGO)
        gate = c.check("TAP", ad_get, snapshot, "App Store")
        self.assertFalse(gate.allowed)
        self.assertIn("not beside 'Duolingo'", gate.reason)

    def test_an_undeclared_get_is_refused_and_get_elsewhere_is_no_commit(self):
        _, gate = self.gate("Get", items=[item(app="App Store", what="Duolingo", act="none", kind="READ")])
        self.assertFalse(gate.allowed)
        maps = Snapshot([Element("6", "Get Directions", "Button", (.1, .8, .8, .05))], "", 400, 800,
                        "synthetic_fixture", bundle_id="com.apple.Maps")
        c = contract("Get directions home.", item(kind="READ", app="Maps", what="directions"))
        self.assertTrue(c.check("TAP", maps.elements[0], maps, "Maps").allowed)
        self.assertIsNone(c.check("TAP", maps.elements[0], maps, "Maps").item)

    def test_a_price_button_buys_and_the_title_says_how_much(self):
        things = item(app="App Store", what="Things 3", act="install", quote="Buy Things 3")
        snapshot = self.SCREENS["paid"]
        c = contract("Buy Things 3 on the App Store.", things)
        gate = c.check("TAP", snapshot.elements[1], snapshot, "App Store")
        self.assertTrue(gate.allowed)
        self.assertEqual(K.approval_title(gate.item, gate.amount), "Buy Things 3 for $9.99?")
        bounded = contract("Buy Things 3 if it is under $5.", item(app="App Store", what="Things 3", act="install",
                                                                    quote="Buy Things 3", condition="under $5"))
        self.assertFalse(bounded.check("TAP", snapshot.elements[1], snapshot, "App Store").allowed)

    def test_an_install_item_is_never_served_outside_the_app_store(self):
        settings = Snapshot([Element("1", "Install Now", "Button", (.1, .5, .8, .05)),
                             Element("2", "Duolingo", "StaticText", (.1, .45, .8, .04))], "", 400, 800,
                            "synthetic_fixture", bundle_id="com.apple.Preferences")
        c = contract("Install Duolingo from the App Store.", self.DUOLINGO)
        gate = c.check("TAP", settings.elements[0], settings, "")
        self.assertFalse(gate.allowed)

    def test_the_plan_line_names_the_app(self):
        c = contract("Install Duolingo from the App Store.", self.DUOLINGO)
        self.assertEqual(K.plan_line(c), "This task will install Duolingo.")

    def test_in_a_compact_list_only_the_named_apps_own_get_is_served(self):
        # Rows .09 apart: the Get of the row above is as near Duolingo's name as Duolingo's own (review of #68:
        # the approval said "Install Duolingo" while the tap installed Babbel).
        rows = []
        for i, name in enumerate(["Babbel - Language Learning", "Duolingo - Language Lessons", "Memrise"]):
            y = .30 + i * .09
            rows += [Element(str(10 + i), name, "StaticText", (.25, y, .45, .03)),
                     Element(str(20 + i), "Get", "Button", (.75, y, .18, .03))]
        snapshot = Snapshot(rows, "", 400, 800, "synthetic_fixture", bundle_id=K.APP_STORE)
        c = contract("Install Duolingo from the App Store.", self.DUOLINGO)
        for get, allowed in ((rows[1], False), (rows[3], True), (rows[5], False)):
            with self.subTest(row=get.rect[1]):
                self.assertEqual(c.check("TAP", get, snapshot, "App Store").allowed, allowed)

    def test_apples_confirmation_sheet_is_no_receipt_and_open_is(self):
        # The sheet ("Double Click to Install") changes the screen, but nothing is installed until the user
        # confirms there: a run without a guard reported "Installed Duolingo" from it (review of #68).
        page, sheet = self.SCREENS["page"], self.SCREENS["confirm"]
        c, gate = self.gate("Get")
        get = next(e for e in page.elements if e.label == "Get")
        c.observe(page, "App Store", 0)
        c.committed(gate, get, page, "App Store", 0)
        c.observe(sheet, "App Store", 1)
        self.assertEqual(c.items[0].receipts, [])
        opened = Snapshot([page.elements[0], Element("2", "Open", "Button", get.rect)], "", 400, 800,
                          "synthetic_fixture", bundle_id=K.APP_STORE)
        c.observe(opened, "App Store", 2)
        self.assertEqual([r["receipt"] for r in c.items[0].receipts], ["shown: open"])


class AbsenceProofTests(unittest.TestCase):
    def test_an_empty_state_screen_proves_a_no(self):
        day = _store("calendar_empty_day")["day"]
        c = contract("What's on my calendar tomorrow?", item(kind="REPORT", app="Calendar", what="events tomorrow"))
        c.observe(day, "Calendar", 0)
        c.update([{"id": 1, "status": "found", "value": "No events tomorrow"}], 1)
        self.assertEqual(c.proof(c.items[0]), (True, ""))
        self.assertEqual(c.done_gate("Nothing tomorrow.", 30)[0], True)

    def test_a_no_without_any_empty_screen_is_still_unproven_and_reopens_once(self):
        c = contract("What's on my calendar tomorrow?", item(kind="REPORT", app="Calendar", what="events tomorrow"))
        c.observe(Snapshot([Element("1", "Tuesday, October 6", "StaticText", (.1, .1, .8, .04))], "", 400, 800,
                           "synthetic_fixture"), "Calendar", 0)
        c.update([{"id": 1, "status": "found", "value": "No events tomorrow"}], 1)
        self.assertFalse(c.proof(c.items[0])[0])
        self.assertEqual(K.MAX_REOPENS, 1)
        self.assertFalse(c.done_gate("Nothing.", 30)[0])
        self.assertTrue(c.done_gate("Nothing.", 30)[0])  # the second DONE stands, and says what was not shown
        self.assertEqual([i.id for i in c.unproven()], [1])


if __name__ == "__main__":
    unittest.main()
