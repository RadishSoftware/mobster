"""Who a message goes to is the request's person, picked by name, never an address made of the name. MobsterBench Real,
12 Oct, on a real iPhone: Messages' contact search showed no suggestions yet, Return in the To field turned the typed
name into an address of its own (the name squashed into one lowercase word), and the message was about to go there.
The card named that address, so the person would have declined, but the agent shouldn't pick it. Offline."""

import os
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from mobile_agent import contract as K
from mobile_agent.frontier import FrontierAgent, prompt_text
from mobile_agent.state import Element, Snapshot

REQUEST = "Text Jordan Lee: Dinner at 7 works for me"
ITEMS = [{"kind": "COMMIT", "app": "Messages", "what": "Jordan Lee", "payload": "Dinner at 7 works for me",
          "act": "send_message", "count": "1", "condition": "", "quote": "Text Jordan Lee"}]
CLEAN = {k: v for k, v in os.environ.items() if not k.startswith(("MOBSTER_", "OPENAI", "TEXT_MODEL", "TYPESAFE"))}


def el(label, role="Button", x=.1, y=.5, w=.8, h=.05, editable=False, value=""):
    return NS(label=label, role=role, rect=(x, y, w, h), editable=editable, value=value, placeholder="")


def to_field(value):
    return el("To:", "TextField", x=.08, y=.16, w=.85, h=.06, editable=True, value=value)


def shot(*elements):
    return NS(elements=list(elements), keyboard="")


def contract(request=REQUEST, what="Jordan Lee"):
    items = [dict(ITEMS[0], what=what, quote=f"Text {what}")]
    return K.Contract.from_items(items, request.replace("Jordan Lee", what))


class ReturnInTheToFieldTests(unittest.TestCase):
    def test_return_on_a_typed_name_is_refused(self):
        c = contract()
        field = to_field("Jordan Lee")
        gate = c.check("SUBMIT", field, shot(field), "Messages")
        self.assertFalse(gate.allowed)
        self.assertIn("an address of its own", gate.reason)
        self.assertFalse(c.check("TYPE_SUBMIT", to_field(""), shot(to_field("")), "Messages", text="Jordan Lee").allowed)

    def test_return_on_a_phone_number_or_an_email_goes_ahead(self):
        c = contract()
        for typed in ("+1 (555) 010-0000", "jordan@example.com"):
            field = to_field(typed)
            self.assertTrue(c.check("SUBMIT", field, shot(field), "Messages").allowed, typed)

    def test_without_a_message_to_send_nothing_changes(self):
        read = K.Contract.from_items([{"kind": "READ", "app": "Messages", "what": "thread with Jordan Lee",
                                       "payload": "", "act": "none", "count": "", "condition": "",
                                       "quote": "Read my thread with Jordan Lee"}], "Read my thread with Jordan Lee")
        field = to_field("Jordan Lee")
        self.assertTrue(read.check("SUBMIT", field, shot(field), "Messages").allowed)


class SuggestionPickTests(unittest.TestCase):
    def picker(self, *rows, typed="Jordan Lee"):
        return shot(to_field(typed), *[el(label, "Cell", y=.25 + .07 * i) for i, label in enumerate(rows)])

    def test_the_exact_name_wins_over_a_handle_or_a_longer_name(self):
        c = contract()
        screen = self.picker("jordanlee@example.com, iMessage", "Jordan Leeds, mobile", "Jordan Lee, (555) 010-0000")
        for wrong in screen.elements[1:3]:
            gate = c.check("TAP", wrong, screen, "Messages")
            self.assertFalse(gate.allowed, wrong.label)
            self.assertIn("'Jordan Lee' is. TAP that row", gate.reason)
        self.assertTrue(c.check("TAP", screen.elements[3], screen, "Messages").allowed)

    def test_two_close_rows_and_no_exact_one_ask_instead_of_guessing(self):
        c = contract()
        screen = self.picker("Jordan Leeds, mobile", "jordanlee@example.com, iMessage")
        gate = c.check("TAP", screen.elements[1], screen, "Messages")
        self.assertFalse(gate.allowed)
        self.assertIn("ASK_USER", gate.reason)

    def test_one_close_row_is_picked(self):
        c = contract(what="Sam")
        screen = self.picker("Sam Rivera, mobile", "Add Contact", typed="Sam")
        self.assertTrue(c.check("TAP", screen.elements[1], screen, "Messages").allowed)

    def test_rows_with_no_typed_name_are_not_a_pick(self):
        c = contract()
        screen = shot(to_field(""), el("Jordan Leeds, mobile", "Cell"), el("Jordan Lee, mobile", "Cell", y=.6))
        self.assertTrue(c.check("TAP", screen.elements[1], screen, "Messages").allowed)


class SendToAMadeAddressTests(unittest.TestCase):
    def send(self, to_value):
        c = contract()
        composer = el("Message", "TextField", y=.57, w=.6, editable=True, value="Dinner at 7 works for me")
        send = el("Send", "Button", x=.84, y=.58, w=.1, h=.03)
        c.record_typed("Messages", composer, "Dinner at 7 works for me", 1)
        return c.check("TAP", send, shot(to_field(to_value), composer, send), "Messages")

    def test_the_name_squashed_into_an_address_is_refused_at_send(self):
        gate = self.send("To:￼jordanlee")
        self.assertFalse(gate.allowed)
        self.assertIn("'jordanlee', an address made of the name", gate.reason)

    def test_the_contact_or_another_person_goes_to_the_card(self):
        self.assertTrue(self.send("To:￼Jordan Lee").allowed)
        self.assertTrue(self.send("To:￼Sam Rivera").allowed)  # the card names both; the person decides


class LoopTests(unittest.TestCase):
    """Smart's loop on the recorded shape: no suggestions yet, then Return in the To field."""

    def test_return_on_the_typed_name_is_refused_and_the_model_told_to_pick_the_row(self):
        to = Element("7", "To:", "TextField", (.08, .16, .85, .06), editable=True, value="Jordan Lee",
                     actions=("TAP", "TYPE", "TYPE_SUBMIT", "SUBMIT"))
        sheet = Snapshot([Element("1", "New Message", "Button", (.32, .1, .37, .02)), to],
                         "", 400, 800, "synthetic_fixture", bundle_id="com.apple.MobileSMS")

        class Phone:
            def __init__(self):
                self.actions = []

            def observe(self, timeout=10):
                return sheet

            def execute(self, operation, target, snapshot, text=None, timeout=10):
                self.actions.append((operation, getattr(target, "label", target)))

        class Model:
            def __init__(self):
                self.prompts, self.usage = [], {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0,
                                                "calls": 0}

            def complete(self, messages, schema, timeout=60, **kwargs):
                if "items" in schema["properties"]:
                    return {"items": ITEMS}, {}
                self.usage["calls"] += 1
                self.prompts.append(prompt_text(messages))
                first = self.usage["calls"] == 1
                action = ({"operation": "SUBMIT", "target": "e2", "target_label": None, "text": None, "app": None}
                          if first else {"operation": "BLOCKED", "target": None, "target_label": None, "text": None,
                                         "app": None})
                return {"thought": "", "plan": "", "notes_add": [], "checklist_updates": [], "actions": [action],
                        "answer": None if first else "No suggestion."}, {}

        phone, model = Phone(), Model()
        with patch.dict(os.environ, dict(CLEAN), clear=True):
            FrontierAgent(phone, model, apps={"com.apple.MobileSMS": "Messages"}, emit=lambda e: None,
                          screenshots=False, settle_seconds=0).run(REQUEST)
        self.assertEqual(phone.actions, [])
        self.assertIn("TAP 'Jordan Lee' in the suggestions", "\n".join(model.prompts))


if __name__ == "__main__":
    unittest.main()
