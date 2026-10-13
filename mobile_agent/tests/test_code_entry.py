"""A one-time code asks first in every mode ("Enter this code?"), and only a code does. MobsterBench Real, 11 Oct, on a
real iPhone: a Clock timer's wheel (one digit, no label) asked in 2 of 3 timer tasks, and a locker code read from the
person's own note, typed into a message, asked too. A code is a field that names one or that the app marks as one,
digits on a screen asking for one, or digits from a row that reads like a code sent to the person. Offline."""

import os
import unittest
from unittest.mock import patch

from mobile_agent import permissions
from mobile_agent.frontier import FrontierAgent, prompt_text
from mobile_agent.permissions.rules import Action, code_entry, looks_like_otp
from mobile_agent.state import Element, Snapshot

CLEAN = {k: v for k, v in os.environ.items() if not k.startswith(("MOBSTER_", "OPENAI", "TEXT_MODEL", "TYPESAFE"))}
COMPOSER = "Message, Text Message, SMS"


def typing(text, label="", role="TextField", screen="", context="", hint="", origins=None, op="TYPE"):
    return Action(op=op, label=label, role=role, text=text, screen=screen, context=context, hint=hint, origins=origins)


class CodeEntryRuleTests(unittest.TestCase):
    def test_a_timer_wheel_is_never_a_code_box(self):
        for digits in ("0", "7", "12", "1200"):
            self.assertFalse(code_entry(typing(digits, role="PickerWheel", op="SET_TEXT")), digits)
        self.assertFalse(code_entry(typing("25", label="Minutes", op="SET_TEXT")))

    def test_a_locker_code_from_the_persons_note_typed_into_a_message_does_not_ask(self):
        note = ("MobsterBench Note, modified September 24, 2026, Locker code: 4817, In folder Notes",)
        self.assertFalse(code_entry(typing("4817", label=COMPOSER, screen="New Message", origins=note)))
        self.assertFalse(code_entry(typing("4817", label=COMPOSER, origins=())))  # from the request itself

    def test_a_code_a_message_sent_still_asks_wherever_it_is_typed(self):
        for row in ("4817 is your Pixgram verification code", "Your Pixgram code: 4817. Do not share it.",
                    "G-4817 is your Google verification code", "Use 4817 to sign in to Pixgram"):
            self.assertTrue(code_entry(typing("4817", label=COMPOSER, origins=(row,))), row)
        # One box of six, the digits from that message.
        self.assertTrue(code_entry(typing("4", origins=("481723 is your Pixgram code",))))

    def test_a_verification_field_or_screen_asks_whatever_the_digits_came_from(self):
        self.assertTrue(code_entry(typing("482913", label="Verification code", origins=())))
        self.assertTrue(code_entry(typing("482913", hint="oneTimeCode", origins=())))
        self.assertTrue(code_entry(typing("482913", screen="Verify it's you",
                                          context="Enter the code we sent to •••• 42", origins=())))
        self.assertTrue(code_entry(typing("4", context="Enter the 6-digit code we sent to your phone", origins=())))
        self.assertTrue(code_entry(typing("482913", label="Code", origins=())))

    def test_where_the_digits_came_from_is_unknown_it_fails_closed_as_before(self):
        self.assertTrue(code_entry(typing("4817", label=COMPOSER)))  # origins None: Fast, AI plans, MCP
        self.assertTrue(code_entry(typing("4")))  # a bare box
        self.assertFalse(code_entry(typing("4817", label="Quantity")))
        self.assertFalse(code_entry(typing("4817", role="SearchField")))

    def test_what_reads_like_a_code_sent(self):
        self.assertTrue(looks_like_otp("482913 is your Pixgram verification code"))
        self.assertTrue(looks_like_otp("Your Pixgram login code is 482913"))
        self.assertFalse(looks_like_otp("Locker code: 4817"))
        self.assertFalse(looks_like_otp("Order 48172 has shipped"))
        self.assertFalse(looks_like_otp("Gym opens at 0600, door 4817"))


class Phone:
    def __init__(self, screens):
        self.screens, self.index, self.actions = list(screens), 0, []

    def observe(self, timeout=10):
        return self.screens[min(self.index, len(self.screens) - 1)]

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        self.actions.append((operation, getattr(target, "label", target), text))
        self.index += 1

    def write_text(self, element, snapshot, text, **kwargs):
        self.actions.append(("TYPE", element.label, text))
        self.index += 1
        return None


class Model:
    def __init__(self, items, steps):
        self.items, self.steps, self.prompts = items, list(steps), []
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "calls": 0}

    def complete(self, messages, schema, timeout=60, **kwargs):
        if "items" in schema["properties"]:
            return {"items": self.items}, {}
        self.usage["calls"] += 1
        prompt = prompt_text(messages)
        self.prompts.append(prompt)
        rows = prompt.split("Screen elements:\n", 1)[1].splitlines()
        op, label, text = self.steps.pop(0) if self.steps else ("DONE", None, None)
        target = next((r.split()[0] for r in rows if label and f'"{label}"' in r), None)
        return {"thought": "", "plan": "", "notes_add": [], "checklist_updates": [],
                "actions": [{"operation": op, "target": target, "target_label": None, "text": text, "app": None}],
                "answer": "Done." if op == "DONE" else None}, {}


def screen(*elements, bundle="com.apple.MobileSMS"):
    return Snapshot(list(elements), "\n".join(e.label for e in elements), 400, 800, "synthetic_fixture",
                    bundle_id=bundle)


class FrontierCodeCardTests(unittest.TestCase):
    """Smart's loop: where the digits came from, read from the screens the run saw."""

    REQUEST = "Text Jordan Lee the locker code from my note."
    ITEMS = [{"kind": "COMMIT", "app": "Messages", "what": "message to Jordan Lee", "payload": "", "act": "send_message",
              "count": "1", "condition": "", "quote": "Text Jordan Lee"}]

    def run_with(self, source_row):
        source = screen(Element("1", source_row, "StaticText", (.05, .3, .9, .03)),
                        Element("2", "Messages", "Button", (.4, .9, .2, .05)))
        compose = screen(Element("3", "Jordan Lee", "StaticText", (.3, .08, .4, .03)),
                         Element("4", COMPOSER, "TextField", (.2, .57, .6, .05), editable=True,
                                 actions=("TAP", "TYPE", "TYPE_SUBMIT", "SUBMIT")))
        phone, asked = Phone([source, compose, compose, compose]), []
        model = Model(self.ITEMS, [("TAP", "Messages", None), ("TYPE", COMPOSER, "4817")])
        gate = permissions.PermissionGate(permissions.ASK_IMPORTANT)
        with patch.dict(os.environ, dict(CLEAN), clear=True):
            FrontierAgent(phone, model, apps={"com.apple.MobileSMS": "Messages"}, emit=lambda e: None,
                          screenshots=False, settle_seconds=0, permissions=gate,
                          approve=lambda request: asked.append(request) or "denied").run(self.REQUEST)
        return phone, [a for a in asked if a.get("act") == "code"]

    def test_digits_from_the_persons_note_are_typed_without_a_code_card(self):
        phone, cards = self.run_with("Locker code: 4817")
        self.assertEqual(cards, [])
        self.assertIn(("TYPE", COMPOSER, "4817"), phone.actions)

    def test_digits_from_a_code_message_ask_first(self):
        phone, cards = self.run_with("4817 is your Pixgram verification code")
        self.assertEqual([c["title"] for c in cards], ["Enter this code?"])
        self.assertNotIn(("TYPE", COMPOSER, "4817"), phone.actions)


if __name__ == "__main__":
    unittest.main()
