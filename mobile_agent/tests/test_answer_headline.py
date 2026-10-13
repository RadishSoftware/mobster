"""The answer's headline states the value the request asked for (9 Oct: "The locker code is 4817." led a run whose
answer was 4,017)."""

import unittest
from types import SimpleNamespace

from mobile_agent.engines import lead_sentence, summarize

SCHEMA = {"type": "object", "properties": {"result": {"type": "string"}}, "required": ["result"],
          "additionalProperties": False}


class Headline(unittest.TestCase):
    def test_a_later_sentence_with_the_value_leads(self):
        self.assertEqual(lead_sentence("The locker code is 4817. Calculator shows 4817 minus 800 = 4017.",
                                       {"result": "4,017"}), "Calculator shows 4817 minus 800 = 4017.")

    def test_a_first_sentence_that_states_it_stays(self):
        self.assertIsNone(lead_sentence("4817 minus 800 is 4,017. The code was 4817.", {"result": "4017"}))
        self.assertIsNone(lead_sentence("Opened it.", {}))

    def test_summarize_puts_the_value_on_top(self):
        agent = SimpleNamespace(_contract=None, client=SimpleNamespace(model="claude-haiku-5-5", model_failed=False))
        outcome = {"status": "completed", "answer": "The locker code is 4817. Calculator shows 4817 minus 800 = 4017.",
                   "data_json": '{"result": "4017"}', "steps": 3, "actions": 9, "elapsed": 30.0}
        summary = summarize(outcome, agent, None, request="Subtract 800 from the code", output_schema=SCHEMA)
        self.assertEqual(summary["data"], {"result": "4017"})
        self.assertEqual(summary["answer"], "Calculator shows 4817 minus 800 = 4017.")


if __name__ == "__main__":
    unittest.main()
