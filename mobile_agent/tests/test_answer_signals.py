"""Multi-signal answer acceptance. Offline."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from mobile_agent.agent import Agent
from mobile_agent.models import Decision
from mobile_agent.state import Element, Snapshot
from mobile_agent.task_policy import OutputSupport, StopGate, agreement_accepts

SCHEMA = {"type": "object", "properties": {"birth_year": {"type": "string"}},
          "required": ["birth_year"], "additionalProperties": False}
HIGH = {"claim_final_state": .9, "claim_entity": .85, "claim_field": .9,
        "p_supported": .45, "p_unsupported": .5, "p_unclear": .05}
SCREEN = Snapshot([Element("0", "Gustave Eiffel", "StaticText", (0, .1, 1, .05)),
                   Element("1", "15 December 1832 – 27 December 1923) was a French", "StaticText",
                           (0, .3, 1, .05))], "", 400, 800, "wda")
ANSWER = {"data": {"birth_year": "1832"}, "citations": [
    {"path": "/birth_year", "evidence_id": "e1", "quote": "15 December 1832 – 27 December 1923) was a French"}]}


class Verifier:
    """A model whose verdict is split but whose per-claim signals are given."""
    def __init__(self, signals):
        self.signals, self.calls = signals, 0
        self.decide = Mock(return_value=Decision("DONE", None, .97, .9, .05, "t", 0, {}, StopGate.CONTINUE))
        self.select_fields = Mock(return_value=ANSWER)

    def verify_output(self, *args, **kwargs):
        return self.verify_output_signals(*args, **kwargs)[0]

    def verify_output_signals(self, *args, **kwargs):
        self.calls += 1
        return OutputSupport.UNSUPPORTED, dict(self.signals)


class Helper:
    def __init__(self, answer):
        self.calls, self.answer = 0, answer

    def extract(self, *args, **kwargs):
        self.calls += 1
        return self.answer


def run(signals, helper_answer):
    driver = SimpleNamespace(can_type=False, observe=lambda timeout=10: SCREEN, execute=Mock())
    model, events = Verifier(signals), []
    result = Agent(driver, model, Helper(helper_answer), emit=events.append).run(
        "Report the year Gustave Eiffel was born", execute=True, output_schema=SCHEMA, output_format="json")
    return result, events


class AgreementTests(unittest.TestCase):
    def test_policy(self):
        # Calibrated (task_policy.CALIBRATED_CLAIM_FLOORS): the field and entity claims decide.
        self.assertTrue(agreement_accepts(HIGH))
        self.assertTrue(agreement_accepts({**HIGH, "p_supported": .2}))
        self.assertFalse(agreement_accepts({**HIGH, "claim_entity": .5}))
        self.assertFalse(agreement_accepts({**HIGH, "claim_field": .4, "p_supported": .2}))
        self.assertFalse(agreement_accepts({**HIGH, "claim_field": None, "p_supported": .2}))

    def test_high_calibrated_claims_accept_a_split_verdict(self):
        result, events = run(HIGH, ANSWER)
        self.assertEqual(result["data"], {"birth_year": "1832"})
        signal = next(e for e in events if e["event"] == "answer_signals")
        self.assertEqual(signal["accepted_by"], "claims_calibrated")
        self.assertNotIn("candidate", signal)

    def test_disagreeing_extractors_with_weak_field_claims_are_not_accepted(self):
        other = {"data": {"birth_year": "1923"}, "citations": [{**ANSWER["citations"][0]}]}
        result, _ = run({**HIGH, "claim_field": .4, "p_supported": .2}, other)
        self.assertIsNone(result["data"])

    def test_low_claims_are_not_accepted_even_when_extractors_agree(self):
        result, _ = run({**HIGH, "claim_entity": .3}, ANSWER)
        self.assertIsNone(result["data"])


if __name__ == "__main__":
    unittest.main()
