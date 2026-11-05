"""The dashboard's event stream from a Smart run: step text and the plan line (smart_events.py)."""

import unittest

from mobile_agent import contract as K
from mobile_agent import contract_asks as KA
from mobile_agent import contract_screen as KS
from mobile_agent import smart_events
from mobile_agent.tests.test_engines import item


class StepTextTests(unittest.TestCase):
    def test_steps_say_what_was_done(self):
        self.assertEqual(smart_events.step_text("TYPE", "Title", text="buy oat milk", field="field"),
                         "Typed “buy oat milk” in Title")
        self.assertEqual(smart_events.step_text("TYPE", "chat_compose_field", text="I'm late", field="composer"),
                         "Wrote the message")
        self.assertEqual(smart_events.step_text("TYPE", "Add a comment…", text="Nice", field="composer", again=True),
                         "Rewrote the comment")
        self.assertEqual(smart_events.step_text("TYPE_SUBMIT", "Search", text="Zara", field="search"),
                         "Searched for “Zara”")
        self.assertEqual(smart_events.step_text("SET_TEXT", "Title", text="", field="field"), "Cleared Title")
        self.assertEqual(smart_events.step_text("SET_TEXT", "chat_compose_field", text="", field="composer"),
                         "Cleared the message")
        self.assertEqual(smart_events.step_text("TYPE", "Notes", text="x" * 80, field="field"), "Typed “" + "x" * 39
                         + "…” in Notes")
        self.assertEqual(smart_events.step_text("SCROLL_TO", None, text="Zara Okonkwo"), "Scrolled to Zara Okonkwo")
        self.assertEqual(smart_events.step_text("READ_LIST", None, where="Reminders"), "Read the Reminders list")
        self.assertEqual(smart_events.step_text("TAP", "searchButton"), "Tapped search button")

    def test_steps_name_what_was_swiped_and_what_an_icon_does(self):
        self.assertEqual(smart_events.step_text("SWIPE_LEFT", "Buy oat milk, Incomplete"),
                         "Swiped left on Buy oat milk")
        self.assertEqual(smart_events.step_text("SWIPE_RIGHT", "Call Mom, Completed"), "Swiped right on Call Mom")
        self.assertEqual(smart_events.step_text("SWIPE_LEFT", None), "Swiped left")
        self.assertEqual(smart_events.step_text("SWIPE_UP", "Reminders list"), "Scrolled down")
        self.assertEqual(smart_events.step_text("TAP", "Buy oat milk, Incomplete"), "Tapped Buy oat milk")
        self.assertEqual(smart_events.step_text("TAP", "circle"), "Tapped the selection circle")
        self.assertEqual(smart_events.step_text("TAP", "ellipsis"), "Tapped More")
        self.assertEqual(smart_events.step_text("TAP", "ellipsis.circle"), "Tapped More")
        self.assertEqual(smart_events.step_text("TAP", "checkmark"), "Tapped Done")
        self.assertEqual(smart_events.step_text("TAP", "trash"), "Tapped Delete")
        self.assertEqual(smart_events.step_text("TAP", "square.and.pencil"), "Tapped Compose")
        self.assertEqual(smart_events.step_text("TAP", "Circle K"), "Tapped Circle K")  # a real name stays
        self.assertEqual(smart_events.step_text("DISMISS", None), "Closed a pop-up")

    def feed(self, *events):
        out = []
        smart = smart_events.SmartEvents(out.append, frame=lambda: b"jpeg")
        for event in events:
            smart(event)
        smart.finish()
        return [e["text"] for e in out if e["event"] == "step"]

    def test_typing_one_field_twice_in_a_row_is_one_step(self):
        decide = lambda *ops, label=None: {"event": "frontier_decision", "step": 0, "ops": list(ops),
                                           "operation": ops[0], "target_label": label}
        act = lambda op, label, text=None, index=0, field="field": {
            "event": "frontier_action", "step": 0, "index": index, "operation": op, "target_label": label,
            "text": text, "field": field if op in smart_events.TYPING else None}
        self.assertEqual(self.feed(decide("TYPE", label="Title"), act("TYPE", "Title", "buy milk"),
                                   decide("SET_TEXT", "TAP", label="Title"), act("SET_TEXT", "Title", "buy oat milk"),
                                   act("TAP", "Save", index=1)),
                         ["Typed “buy oat milk” in Title", "Tapped Save"])
        self.assertEqual(self.feed(decide("TYPE", "TYPE", label="Title"), act("TYPE", "Title", "a"),
                                   act("TYPE", "Notes", "b", index=1)),
                         ["Typed “a” in Title", "Typed “b” in Notes"])
        self.assertEqual(self.feed(decide("TYPE", label="Title"), act("TYPE", "Title", "a"),
                                   decide("TAP", label="Save"), act("TAP", "Save")),
                         ["Typed “a” in Title", "Tapped Save"])
        self.assertEqual(self.feed(decide("READ_LIST"), {"event": "frontier_action", "step": 0, "index": 0,
                                                         "operation": "READ_LIST", "target_label": None,
                                                         "app_name": "Reminders"}),
                         ["Read the Reminders list"])

    def test_a_step_carries_the_box_of_the_element_it_acted_on_and_nothing_when_unknown(self):
        out = []
        smart = smart_events.SmartEvents(out.append, frame=lambda: b"jpeg")
        act = lambda op, label, **extra: {"event": "frontier_action", "step": 0, "index": 0, "operation": op,
                                          "target_label": label, "text": None, "field": None, **extra}
        smart(act("TAP", "Reply", element_rect=[0.1, 0.5, 0.4, 0.05]))
        smart(act("TAP_XY", None))
        smart(act("TAP", "Share"))
        smart(act("TYPE", "Message", text="hi", field="composer", element_rect=[0.05, 0.9, 0.8, 0.04]))
        smart.finish()
        self.assertEqual([e.get("focus") for e in out if e["event"] == "step"],
                         [[0.1, 0.5, 0.4, 0.05], None, None, [0.05, 0.9, 0.8, 0.04]])

    def test_a_press_s_step_carries_the_loop_s_commit_test_and_every_action_s_step_whether_it_changed(self):
        out = []
        smart = smart_events.SmartEvents(out.append, frame=lambda: b"jpeg")
        act = lambda op, label, **extra: {"event": "frontier_action", "step": 0, "index": 0, "operation": op,
                                          "target_label": label, "text": None, "field": None, **extra}
        smart(act("TAP", "Alex Rivera", consequential=False, changed=True))
        smart(act("TAP", "$4.99", consequential=True, changed=True))
        smart(act("LONG_PRESS", "Sam Lee", consequential=False, changed=False))
        smart(act("TAP", "Back", changed=True))  # an engine that doesn't say: no commit
        smart(act("SWIPE_UP", None, consequential=False, changed=True))  # only a press carries it
        smart(act("TYPE", "Title", text="a", field="field", changed=False))
        smart(act("TYPE_SUBMIT", "Title", text="ab", field="field", changed=True))
        smart({"event": "tool_step", "op": "READ_INBOX", "text": "Read 10 DMs in Instagram"})
        smart(act("READ_INBOX", None, changed=True))
        smart.finish()
        self.assertEqual([(e["text"], e.get("commit"), e.get("changed")) for e in out if e["event"] == "step"],
                         [("Tapped Alex Rivera", False, True), ("Tapped $4.99", True, True),
                          ("Pressed and held Sam Lee", False, False), ("Tapped Back", None, True),
                          ("Scrolled down", None, True), ("Typed “ab” in Title and pressed Return", None, True),
                          ("Read 10 DMs in Instagram", None, None)])

    def test_each_model_turn_says_its_thought_after_the_last_turns_steps(self):
        out = []
        smart = smart_events.SmartEvents(out.append, frame=lambda: b"jpeg")
        decide = lambda step, op, label, **extra: {"event": "frontier_decision", "step": step, "ops": [op],
                                                   "operation": op, "target_label": label, **extra}
        smart(decide(0, "TYPE", "Title", thought="Type the reminder into Title.", latency_ms=1840.4))
        smart({"event": "frontier_action", "step": 0, "index": 0, "operation": "TYPE", "target_label": "Title",
               "text": "buy milk", "field": "field"})
        smart(decide(1, "TAP", "Save", thought="Save it.", latency_ms=920))
        smart.finish()
        said = [(e["event"], e.get("step"), e.get("text"), e.get("ms")) for e in out
                if e["event"] in ("thought", "step")]
        self.assertEqual(said, [("thought", 0, "Type the reminder into Title.", 1840),
                                ("step", 0, "Typed “buy milk” in Title", None),
                                ("thought", 1, "Save it.", 920)])

    def test_a_thought_masks_secrets_and_a_saved_programs_turn_has_none(self):
        out = []
        smart = smart_events.SmartEvents(out.append, frame=lambda: b"jpeg")
        smart({"event": "frontier_decision", "step": 0, "ops": ["TYPE"], "operation": "TYPE", "latency_ms": 10,
               "thought": "Paste the key sk-ant-api03-" + "x" * 40 + " into  the\nfield"})
        smart({"event": "frontier_decision", "step": 1, "ops": ["TAP"], "operation": "TAP", "latency_ms": 2,
               "thought": "saved program: tap Save", "source": "program"})
        smart({"event": "frontier_decision", "step": 2, "ops": ["DONE"], "operation": "DONE", "thought": ""})
        thoughts = [e for e in out if e["event"] == "thought"]
        self.assertEqual(len(thoughts), 2)
        self.assertNotIn("sk-ant", thoughts[0]["text"])
        self.assertTrue(thoughts[0]["text"].startswith("Paste the key ")
                        and thoughts[0]["text"].endswith(" into the field"))
        self.assertEqual((thoughts[1]["step"], thoughts[1]["text"], thoughts[1]["ms"]), (2, None, None))

    def test_a_thought_says_on_screen_for_the_element_ids_the_model_saw(self):
        out = []
        smart = smart_events.SmartEvents(out.append, frame=lambda: b"jpeg")
        for step, said in enumerate(("Address visible in element e32.", "Tap Send (e7) to finish.",
                                     "Compare e3 and e4.")):
            smart({"event": "frontier_decision", "step": step, "ops": ["TAP"], "operation": "TAP", "thought": said})
        self.assertEqual([e["text"] for e in out if e["event"] == "thought"],
                         ["Address visible on screen.", "Tap Send to finish.", "Compare it and it."])


class PlanLineTests(unittest.TestCase):
    def test_plan_lines(self):
        c = K.Contract.from_items([item(what="Sam"), item(kind="COMMIT", app="Reminders", what="reminder", act="save",
                                                          quote="Text Sam")], "Text Sam")
        self.assertEqual(KA.plan_line(c), "This task will send 1 message to Sam.")
        self.assertIsNone(KA.plan_line(K.Contract.from_items([item("REPORT", what="weather", act="none")], "x")))
        self.assertIsNone(KA.plan_line(None))
        pay = K.Contract.from_items([item(app="Venmo", what="Maya", act="pay", quote="pay Maya $12")], "pay Maya $12")
        self.assertEqual(KA.plan_line(pay), "This task will make 1 payment to Maya.")
        self.assertEqual(KA.approval_title(pay.items[0], 12), "Pay $12.00 to Maya?")
        follow = K.Contract.from_items([item(app="X", what="@nasa", act="follow", quote="follow")], "follow @nasa")
        self.assertEqual(KA.approval_title(follow.items[0]), "Follow @nasa?")
        self.assertFalse(KA.asks(KS.Item(1, "COMMIT", act="save")))
        self.assertTrue(KA.asks(KS.Item(1, "COMMIT", act="delete")))
        self.assertFalse(KA.asks(KS.Item(1, "WRITE")))
