"""A second tap on the same act-named control, on the same screen in the same app, is refused until the run has looked
somewhere new (iOSWorld, 10-12 Oct: "Add Food" on the same food's page logged it two or three times, and each time the
model tapped again right after the one refusal). Offline, on a fake phone."""

import unittest

from mobile_agent.frontier import FrontierAgent, prompt_text
from mobile_agent.state import Element, Snapshot

APP = "com.example.food"


def screen(title, *labels):
    elements = [Element("0", title, "NavigationBar", (0, .05, 1, .06))]
    elements += [Element(str(i + 1), label, "Button", (.1, .2 + .08 * i, .8, .05)) for i, label in enumerate(labels)]
    return Snapshot(elements, "\n".join(e.label for e in elements), 400, 800, "synthetic_fixture", bundle_id=APP)


OATMEAL = screen("Oatmeal", "Add Food", "Done")
BANANA = screen("Banana", "Add Food", "Done")
TODAY = screen("Today", "Oatmeal", "Banana")


REQUESTS = screen("Requests", "Maya, $20, Pay", "Kai, $15, Pay")
CARD_ROW = "Freedom Unlimited, VISA, $4,588.98, Current balance, You've set up automatic payments."
WALLET = screen("Wallet", CARD_ROW)
CARD = screen("Card", "Back")


class Phone:
    """Taps move between pages: a food's "Add Food" goes back to Today, Today's rows open their food."""

    def __init__(self, first):
        self.screen, self.taps = first, []

    def observe(self, timeout=10):
        return self.screen

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        label = getattr(target, "label", "")
        self.taps.append((operation, label, snapshot.elements[0].label))
        if label in ("Add Food", "Done"):
            self.screen = TODAY
        elif label == "Oatmeal":
            self.screen = OATMEAL
        elif label == "Banana":
            self.screen = BANANA
        elif label == CARD_ROW:
            self.screen = CARD
        elif label == "Back":
            self.screen = WALLET
        elif label.endswith("Pay"):
            # Paying a request removes its row: the list changes.
            labels = [e.label for e in self.screen.elements[1:] if e.label != label]
            self.screen = screen("Requests", *labels)


class Model:
    def __init__(self, *chunks):
        self.chunks, self.prompts = list(chunks), []
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "calls": 0}

    def complete(self, messages, schema, timeout=60, **kwargs):
        if "items" in schema["properties"]:
            return {"items": []}, {}
        self.usage["calls"] += 1
        prompt = prompt_text(messages)
        self.prompts.append(prompt)
        rows = prompt.split("Screen elements:\n", 1)[1].splitlines()
        op, label = self.chunks.pop(0) if self.chunks else ("DONE", None)
        target = next((r.split()[0] for r in rows if label and f'"{label}"' in r), None)
        return {"thought": "", "plan": None, "notes_add": [], "checklist_updates": [],
                "actions": [{"op": op, "target": target, "text": None}], "answer": "ok" if op == "DONE" else None}, {}


def run(first, *chunks, request="Log one serving of oatmeal."):
    phone, model, events = Phone(first), Model(*chunks), []
    agent = FrontierAgent(phone, model, apps={APP: "Food"}, emit=events.append, screenshots=False, settle_seconds=0,
                          skills=())
    result = agent.run(request)
    return result, phone, model, events


def refused(events):
    return [e for e in events if e.get("event") == "frontier_refused" and e.get("label") == "repeat_act"]


class RepeatActTests(unittest.TestCase):
    def test_the_same_act_on_the_same_page_is_refused_with_a_look_first_note(self):
        result, phone, model, events = run(
            OATMEAL, ("TAP", "Add Food"), ("TAP", "Oatmeal"), ("TAP", "Add Food"), ("DONE", None))
        self.assertEqual(len(refused(events)), 1)
        self.assertEqual([t for t in phone.taps if t[1] == "Add Food"], [("TAP", "Add Food", "Oatmeal")])
        self.assertIn("Refused: you already tapped 'Add Food'", model.prompts[3])
        self.assertEqual(result["status"], "completed")

    def test_a_repeat_the_model_insists_on_is_refused_until_it_looks_somewhere_new(self):
        # caltrack-001 ac2/A2, 12 Oct: refused once, tapped again at once, and the food was logged twice.
        _, phone, model, events = run(
            OATMEAL, ("TAP", "Add Food"), ("TAP", "Oatmeal"), ("TAP", "Add Food"), ("TAP", "Add Food"), ("DONE", None))
        self.assertEqual(len(refused(events)), 2)
        self.assertEqual(len([t for t in phone.taps if t[1] == "Add Food"]), 1)
        self.assertIn("Refused again", model.prompts[4])

    def test_going_back_to_a_screen_seen_since_the_tap_is_not_a_new_look(self):
        _, phone, _, events = run(
            OATMEAL, ("TAP", "Add Food"), ("TAP", "Oatmeal"), ("TAP", "Add Food"), ("TAP", "Done"), ("TAP", "Oatmeal"),
            ("TAP", "Add Food"), ("DONE", None))
        self.assertEqual(len(refused(events)), 2)
        self.assertEqual(len([t for t in phone.taps if t[1] == "Add Food"]), 1)

    def test_a_repeat_goes_ahead_once_the_run_has_looked_at_a_screen_it_had_not_seen(self):
        _, phone, _, events = run(
            OATMEAL, ("TAP", "Add Food"), ("TAP", "Oatmeal"), ("TAP", "Add Food"), ("TAP", "Done"), ("TAP", "Banana"),
            ("TAP", "Done"), ("TAP", "Oatmeal"), ("TAP", "Add Food"), ("DONE", None))
        self.assertEqual(len(refused(events)), 1)
        self.assertEqual(len([t for t in phone.taps if t[1] == "Add Food"]), 2)

    def test_the_same_act_after_looking_elsewhere_first_is_not_refused(self):
        _, phone, _, events = run(
            OATMEAL, ("TAP", "Add Food"), ("TAP", "Banana"), ("TAP", "Done"), ("TAP", "Oatmeal"), ("TAP", "Add Food"),
            ("DONE", None))
        self.assertEqual(refused(events), [])
        self.assertEqual(len([t for t in phone.taps if t[1] == "Add Food"]), 2)

    def test_a_row_whose_description_holds_an_act_word_is_not_an_act(self):
        # mem-046 td1b/B1, 12 Oct: a card's "You've set up automatic payments" made reopening it a refused repeat.
        _, phone, _, events = run(WALLET, ("TAP", CARD_ROW), ("TAP", "Back"), ("TAP", CARD_ROW), ("DONE", None),
                                  request="What is my card balance?")
        self.assertEqual(refused(events), [])
        self.assertEqual(len([t for t in phone.taps if t[1] == CARD_ROW]), 2)

    def test_the_same_act_on_another_page_is_not_refused(self):
        _, phone, _, events = run(
            OATMEAL, ("TAP", "Add Food"), ("TAP", "Banana"), ("TAP", "Add Food"), ("DONE", None))
        self.assertEqual(refused(events), [])
        self.assertEqual([t[2] for t in phone.taps if t[1] == "Add Food"], ["Oatmeal", "Banana"])

    def test_a_list_whose_rows_each_have_the_act_is_worked_down_without_refusals(self):
        _, phone, _, events = run(REQUESTS, ("TAP", "Maya, $20, Pay"), ("TAP", "Kai, $15, Pay"), ("DONE", None),
                                  request="Pay every pending request.")
        self.assertEqual(refused(events), [])
        self.assertEqual(len(phone.taps), 2)

    def test_closing_a_sheet_twice_is_not_refused(self):
        _, phone, _, events = run(OATMEAL, ("TAP", "Done"), ("TAP", "Oatmeal"), ("TAP", "Done"), ("DONE", None))
        self.assertEqual(refused(events), [])
        self.assertEqual(len([t for t in phone.taps if t[1] == "Done"]), 2)


if __name__ == "__main__":
    unittest.main()
