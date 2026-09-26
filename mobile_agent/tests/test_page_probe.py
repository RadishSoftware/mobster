"""Answers already in a page's off-screen text end the run without scrolling to them. Offline."""

import unittest

from mobile_agent.agent import Agent
from mobile_agent.drivers import Driver
from mobile_agent.extraction import Evidence
from mobile_agent.models import Decision
from mobile_agent.state import Element, OffscreenNode, Snapshot
from mobile_agent.task_policy import ActionSupport, OutputSupport, StopGate

SCHEMA = {"type": "object", "properties": {"first_ascent": {"type": "string"}},
          "required": ["first_ascent"], "additionalProperties": False}
GOAL = "According to this article's infobox, on what date was Mount Everest first climbed?"


def page():
    filler = [OffscreenNode(f"paragraph {index}", "StaticText", (0, 1.2 + index * .05, 1, .04), web=True,
                            locator=f"/web/p[{index}]") for index in range(30)]
    infobox = [OffscreenNode("First ascent", "StaticText", (0, 3.0, .4, .04), web=True, locator="/web/th"),
               OffscreenNode("29 May 1953", "StaticText", (.5, 3.0, .4, .04), web=True, locator="/web/td")]
    return Snapshot([Element("0", "Mount Everest", "StaticText", (0, .2, 1, .05))], "Mount Everest", 393, 852,
                    "wda", bundle_id="com.apple.mobilesafari", offscreen=tuple(filler + infobox))


class Safari(Driver):
    can_type = False

    def __init__(self):
        self.actions = []

    def observe(self, timeout=10):
        return page()

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        self.actions.append(operation)

    def close(self):
        pass


class Model:
    """Jev stand-in: always wants to scroll; selects and verifies the infobox date."""

    def __init__(self, support=OutputSupport.SUPPORTED):
        self.support = support

    def decide(self, snapshot, goal, history, **kwargs):
        return Decision("SWIPE_UP", None, .9, .1, .01, "test", 0, {}, StopGate.CONTINUE)

    def verify_action(self, *args, **kwargs):
        return ActionSupport.ALLOWED

    def side_channel(self, name="prefetch"):
        return None

    def select_fields(self, goal, schema, evidence, timeout=20, *, http=None):
        entry = next(e for e in evidence["entries"] if e["text"] == "29 May 1953")
        return {"data": {"first_ascent": "29 May 1953"},
                "citations": [{"path": "/first_ascent", "evidence_id": entry["id"], "quote": "29 May 1953"}]}

    def verify_output(self, goal, candidate, evidence, timeout=20, **kwargs):
        return self.support


class PageProbeTests(unittest.TestCase):
    def run_agent(self, model):
        phone, events = Safari(), []
        result = Agent(phone, model, emit=events.append, settle_seconds=0, max_steps=4).run(
            GOAL, execute=True, output_schema=SCHEMA, output_format="json")
        return result, phone, [event["event"] for event in events]

    def test_offscreen_text_is_citable_evidence_with_its_row(self):
        evidence = Evidence()
        evidence.add(page(), 0)
        entry = next(e for e in evidence.public()["entries"] if e["text"] == "29 May 1953")
        self.assertTrue(entry["offscreen"])
        self.assertIn("First ascent", entry["row_context"])
        self.assertFalse(any(e.get("offscreen") for e in evidence.decision_context(page())["entries"]))

    def test_a_verified_answer_ends_the_run_before_any_scroll(self):
        result, phone, events = self.run_agent(Model())
        self.assertEqual(result["data"], {"first_ascent": "29 May 1953"})
        self.assertEqual(phone.actions, [])
        self.assertIn("page_probe_answered", events)

    def test_an_unsupported_answer_leaves_the_run_to_navigate(self):
        result, phone, events = self.run_agent(Model(OutputSupport.UNCLEAR))
        self.assertIn("page_probe_unsupported", events)
        self.assertIn("SWIPE_UP", phone.actions)


if __name__ == "__main__":
    unittest.main()
