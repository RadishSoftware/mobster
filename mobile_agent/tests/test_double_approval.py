"""O1, one message gets one approval: the Kate Bell take (5 Oct, Messages on an iOS 26.4 simulator) replayed
offline. The run asked "Send this message to Kate Bell?" twice: before typing (a type-and-Return action, with
an empty composer) and again at Send, because iOS 26 Messages' Return added a line instead of sending.

Now a composer TYPE_SUBMIT types without asking, asks once with the text in the composer, then presses Return;
if Return did not send, the Send tap for the same item, text, thread and app uses that approval once."""

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from mobile_agent import engines
from mobile_agent.frontier import prompt_text
from mobile_agent.state import Element, Snapshot

FIXTURES = Path(__file__).parent / "fixtures" / "frontier"
CLEAN = {k: v for k, v in os.environ.items() if not k.startswith(("MOBSTER_", "OPENAI", "TEXT_MODEL", "TYPESAFE"))}
QUICK = __import__("dataclasses").replace(engines.SMART_CONFIG, settle_seconds=0, screenshots=False)


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def snapshots(data):
    """{name: Snapshot} from a fixture's screens."""
    out = {}
    for name, rows in data["screens"].items():
        elements = [Element(r["id"], r["label"], r["role"], tuple(r["rect"]), editable=r.get("editable", False),
                            value=r.get("value", ""), actions=tuple(r.get("actions", ("TAP",))), locator=f"/{r['id']}")
                    for r in rows]
        keyboard = "visible" if any(e.role == "Key" for e in elements) else ""
        out[name] = Snapshot(elements, "\n".join(e.label for e in elements), 400, 800, "synthetic_fixture",
                             bundle_id=data["bundle"], keyboard=keyboard)
    return out


class Messages:
    """A Messages thread: Return adds a line (it never sends), Send sends. ``sent`` is the screen after Send."""

    def __init__(self, screens, sent="sent", newline="newline", start="list"):
        self.screens, self.state, self.sent, self.newline = screens, start, sent, newline
        self.actions = []

    def observe(self, timeout=10):
        return self.screens[self.state]

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        label = getattr(target, "label", target)
        self.actions.append((operation, label, text))
        if operation == "TAP" and str(label).startswith("+1 (555)"):
            self.state = "thread"
        elif operation in ("TYPE", "TYPE_SUBMIT") and label == "Message":
            self.state = self.newline if operation == "TYPE_SUBMIT" else "typed"
        elif operation == "SUBMIT" and label == "Message":
            self.state = self.newline
        elif operation == "TAP" and label == "Send":
            self.state = self.sent


class Model:
    """Answers each decision from a list of chunks [(op, label, text)]; the contract call returns ``items``."""

    def __init__(self, steps, items, answer="Sent to Kate Bell: Running 10 minutes late, save me a seat!"):
        self.steps, self.items, self.answer, self.prompts = list(steps), items, answer, []
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "calls": 0}
        self.model = "claude-sonnet-5-5"

    def complete(self, messages, schema, timeout=60, **kwargs):
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
        return {"thought": "", "plan": "", "notes_add": [], "checklist_updates": [], "actions": actions,
                "answer": self.answer if chunk[-1][0] == "DONE" else None}, {}


def run(driver, steps, approve, data):
    out = []
    model = Model(steps, data["items"])
    events = engines.SmartEvents(out.append, driver=driver, frame=lambda: None, apps={data["bundle"]: "Messages"})
    with patch.dict(os.environ, dict(CLEAN), clear=True):
        agent = engines.build_frontier(driver, model, apps=events.apps, emit=events, config=QUICK,
                                       approve=events.approving(approve), skills=())
        events.agent = agent
        result = agent.run(data["request"])
        events.finish()
        summary = engines.summarize(result, agent, events, request=data["request"])
    return result, summary, out, model


class KateBellReplayTests(unittest.TestCase):
    DATA = fixture("kate_bell_thread")
    TEXT = DATA["text"]
    STEPS = [[("TAP", "+1 (555) 564-8583, 12/31/00 ", None)], [("TYPE_SUBMIT", "Message", TEXT)],
             [("TAP", "Send", None)], [("DONE", None, None)], [("DONE", None, None)], [("DONE", None, None)]]

    def replay(self, sent="sent", newline="newline"):
        driver = Messages(snapshots(self.DATA), sent=sent, newline=newline)
        asked = []

        def approve(request):
            asked.append({**request, "composer": next(e.value for e in driver.observe().elements
                                                      if e.label == "Message" and e.editable)})
            return "approved"
        result, summary, out, model = run(driver, list(self.STEPS), approve, self.DATA)
        return driver, asked, result, summary, out

    def test_one_approval_after_typing_with_the_text_in_the_composer_and_the_run_completes(self):
        driver, asked, result, summary, out = self.replay()
        self.assertEqual(len(asked), 1)
        request = asked[0]
        self.assertEqual(request["operation"], "SUBMIT")
        self.assertEqual(request["text"], self.TEXT)
        self.assertEqual(request["composer"], self.TEXT)  # the user sees the message before approving it
        self.assertEqual(request["title"], "Send this message to +1 (555) 564-8583 (Kate Bell in your request)?")
        self.assertEqual(driver.actions, [("TAP", "+1 (555) 564-8583, 12/31/00 ", None), ("TYPE", "Message", self.TEXT),
                                          ("SUBMIT", "Message", None), ("TAP", "Send", None)])
        kinds = [e["event"] for e in out]
        self.assertEqual(kinds.count("frontier_approval"), 2)  # asked once, reused once
        reused = [e for e in out if e["event"] == "frontier_approval" and e.get("reused")]
        self.assertEqual(len(reused), 1)
        # The approval was asked after the typing step was shown, never before it.
        self.assertLess([e.get("text") for e in out if e["event"] == "step"].index("Wrote the message"),
                        len([e for e in out if e["event"] == "step"]))
        self.assertEqual((result["status"], summary["status"], summary["outcome"]), ("completed", "completed", "done"))

    def test_a_bubble_that_is_only_a_text_view_with_delivered_under_it_is_proof(self):
        _, asked, result, summary, _ = self.replay(sent="sent_text_view_only")
        self.assertEqual(len(asked), 1)
        self.assertEqual(result["contract"]["receipted"], 1)  # the send (its text is proven through it)
        self.assertEqual((summary["outcome"], summary["reason"]), ("done", None))

    def test_a_wrong_text_bubble_still_ends_check(self):
        _, asked, result, summary, _ = self.replay(sent="sent_wrong_text")
        self.assertEqual(len(asked), 1)
        self.assertEqual((summary["status"], summary["outcome"]), ("completion_not_confirmed", "check"))

    def test_a_different_thread_asks_again(self):
        _, asked, result, _, out = self.replay(newline="other_thread_newline")
        self.assertEqual(len(asked), 2)  # the Send tap is in another thread: a new question
        self.assertEqual(asked[1]["operation"], "TAP")
        self.assertEqual(asked[1]["title"], "Send this message to Sam Rivera (Kate Bell in your request)?")
        self.assertFalse(any(e.get("reused") for e in out if e["event"] == "frontier_approval"))

    def test_a_denied_send_types_but_never_presses_return(self):
        driver = Messages(snapshots(self.DATA))
        result, _, _, _ = run(driver, list(self.STEPS), lambda request: "denied", self.DATA)
        self.assertEqual(result["status"], "approval_denied")
        self.assertNotIn("SUBMIT", [a[0] for a in driver.actions])
        self.assertNotIn(("TAP", "Send", None), driver.actions)

    def test_new_typing_after_the_approval_asks_again(self):
        """The reused approval needs the very text approved: typing again (even the same words) asks again."""
        driver = Messages(snapshots(self.DATA))
        asked = []
        steps = [[("TAP", "+1 (555) 564-8583, 12/31/00 ", None)], [("TYPE_SUBMIT", "Message", self.TEXT)],
                 [("SET_TEXT", "Message", self.TEXT), ("TAP", "Send", None)], [("DONE", None, None)],
                 [("DONE", None, None)], [("DONE", None, None)]]
        run(driver, steps, lambda request: asked.append(request) or "approved", self.DATA)
        self.assertEqual(len(asked), 2)

    def test_without_ask_before_acting_nothing_is_split(self):
        driver = Messages(snapshots(self.DATA))
        out = []
        model = Model(list(self.STEPS), self.DATA["items"])
        events = engines.SmartEvents(out.append, driver=driver, frame=lambda: None, apps={"com.apple.MobileSMS": "Messages"})
        with patch.dict(os.environ, dict(CLEAN), clear=True):
            agent = engines.build_frontier(driver, model, apps=events.apps, emit=events, config=QUICK, skills=())
            events.agent = agent
            agent.run(self.DATA["request"])
        self.assertIn(("TYPE_SUBMIT", "Message", self.TEXT), driver.actions)


if __name__ == "__main__":
    unittest.main()


def compose_sheet_data(to_value):
    """The New Message sheet as iOS 26 reads it (MobsterBench Real, 11 Oct): the bar named by its class, "New
    Message" centred in it, a To field holding each token as an attachment mark, the token a button on its row."""
    def rows(message):
        out = [{"id": "1", "label": "CKComposeChat", "role": "NavigationBar", "rect": [0, .09, 1, .06]},
               {"id": "2", "label": "New Message", "role": "Button", "rect": [.32, .10, .37, .02]},
               {"id": "3", "label": "Cancel", "role": "Button", "rect": [.85, .09, .09, .04]},
               {"id": "7", "label": "To:", "role": "TextField", "rect": [.08, .16, .85, .06], "editable": True,
                "value": to_value, "actions": ["TAP", "TYPE"]},
               {"id": "9", "label": "Add Contact", "role": "Button", "rect": [.85, .17, .08, .04]},
               {"id": "5", "label": "Message", "role": "TextField", "rect": [.2, .57, .64, .05], "editable": True,
                "value": message, "actions": ["TAP", "TYPE", "TYPE_SUBMIT", "SUBMIT"]},
               {"id": "6", "label": "Send", "role": "Button", "rect": [.84, .58, .1, .03]}]
        name = to_value.rsplit("\ufffc", 1)[-1] if "\ufffc" in to_value else ""
        if name:
            out.append({"id": "8", "label": f"{name}, +15550100000", "role": "Button", "rect": [.13, .18, .27, .03]})
        return out
    text = "Running late"
    return {"bundle": "com.apple.MobileSMS", "request": f"Text Jordan Lee: {text}", "text": text,
            "items": [{"kind": "COMMIT", "app": "Messages", "what": "text message to Jordan Lee", "payload": text,
                       "act": "send_message", "count": "1", "condition": "", "quote": "Text Jordan Lee"}],
            "screens": {"list": rows(""), "typed": rows(text)}}


class ComposeSheetCardTests(unittest.TestCase):
    """The card on the New Message sheet names the person in its To field, never the sheet's class name: every
    MobsterBench Real Messages card read "Send this message to CKComposeChat (Bench Tester in your request)?"."""

    def card(self, to_value):
        data = compose_sheet_data(to_value)
        driver = Messages(snapshots(data))
        asked = []
        steps = [[("TYPE", "Message", data["text"])], [("TAP", "Send", None)]] + [[("DONE", None, None)]] * 3
        result, _, _, _ = run(driver, steps, lambda request: asked.append(request) or "denied", data)
        self.assertEqual(result["status"], "approval_denied")
        self.assertNotIn(("TAP", "Send", None), driver.actions)
        self.assertEqual([r["text"] for r in asked], [data["text"]])  # what goes out is shown as before
        return asked[0]["title"]

    def test_the_card_names_the_to_fields_person(self):
        self.assertEqual(self.card("To:\ufffcJordan Lee"), "Send this message to Jordan Lee?")

    def test_with_no_one_on_screen_the_card_keeps_the_request_as_the_requests(self):
        self.assertEqual(self.card(""), "Send this message to the person you named (Jordan Lee)?")
