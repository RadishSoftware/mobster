"""'How many' answers: two listings of cited items must agree; code counts them. Offline."""

import unittest

from mobile_agent.agent import Agent
from mobile_agent.drivers import Driver
from mobile_agent.models import Decision
from mobile_agent.state import Element, Snapshot
from mobile_agent.task_policy import ActionSupport, OutputSupport, StopGate

SCHEMA = {"type": "object", "properties": {"count": {"type": "string"}}, "required": ["count"],
          "additionalProperties": False}
ROWS = ["Water the plants", "Return library books", "Pick up dry cleaning"]


class Phone(Driver):
    can_type = False

    def observe(self, timeout=10):
        elements = [Element("0", "MobsterBench", "NavigationBar", (0, .05, 1, .05))]
        elements += [Element(str(i + 1), row, "TextField", (.1, .2 + i * .06, .8, .05), value=row)
                     for i, row in enumerate(ROWS)]
        return Snapshot(elements, "MobsterBench\n" + "\n".join(ROWS), 393, 852, "synthetic_fixture",
                        bundle_id="com.apple.reminders")

    def execute(self, *args, **kwargs):
        raise AssertionError("no action expected")

    def close(self):
        pass


class Model:
    def __init__(self, support):
        self.support = support

    def decide(self, snapshot, goal, history, **kwargs):
        return Decision("DONE", None, .95, .95, .01, "test", 0, {}, StopGate.CONTINUE)

    def verify_action(self, *args, **kwargs):
        return ActionSupport.ALLOWED

    def verify_output(self, goal, candidate, evidence, timeout=20, **kwargs):
        return self.support


class Helper:
    def __init__(self, second_drops_one=False):
        self.calls, self.second_drops_one = 0, second_drops_one

    def extract(self, evidence, goal, schema, timeout=20):
        self.calls += 1
        entries = [e for e in evidence["entries"] if e["text"] in ROWS and e["field"] == "label"]
        if self.second_drops_one and self.calls == 2:
            entries = entries[:-1]
        return {"data": {"items": [e["text"] for e in entries]},
                "citations": [{"path": f"/items/{i}", "evidence_id": e["id"], "quote": e["text"]}
                              for i, e in enumerate(entries)]}


class CountedAnswerTests(unittest.TestCase):
    def run_agent(self, support, helper=None):
        return Agent(Phone(), Model(support), helper or Helper(), settle_seconds=0).run(
            "In Reminders: Report how many incomplete reminders the list MobsterBench has.", execute=True,
            output_schema=SCHEMA, output_format="json")

    def test_the_count_is_the_number_of_cited_items(self):
        result = self.run_agent(OutputSupport.SUPPORTED)
        self.assertEqual(result["data"], {"count": "3"})
        self.assertEqual(len(result["citations"]), 3)

    def test_listings_that_disagree_give_no_count(self):
        result = self.run_agent(OutputSupport.UNSUPPORTED, Helper(second_drops_one=True))
        self.assertNotEqual(result["data"], {"count": "3"})
        self.assertNotEqual(result["data"], {"count": "2"})


if __name__ == "__main__":
    unittest.main()
