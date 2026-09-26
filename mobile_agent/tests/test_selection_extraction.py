"""Extraction as literal selection. Offline: the Jev call is replaced."""

import unittest
from unittest.mock import patch

from mobile_agent.extraction import Evidence, InsufficientEvidence, validate_extraction
from mobile_agent.models import (Jev, SelectionUnsure, literal_candidates, selectable_schema)
from mobile_agent.state import Element, Snapshot

SCHEMA = {"type": "object", "properties": {"model_number": {"type": "string"}},
          "required": ["model_number"], "additionalProperties": False}


def about_evidence():
    evidence = Evidence()
    evidence.add(Snapshot([
        Element("0", "Model Number, MTQM3LL/A", "Cell", (0, .3, 1, .05)),
        Element("1", "iOS Version, 26.0.1", "Cell", (0, .4, 1, .05)),
    ], "", 400, 800, "wda"), 3)
    return evidence.public()


def choice(options, pick, confidence=.95):
    probabilities = {key: 0.0 for key in options}
    probabilities[pick] = 1.0
    return {"type": "choice", "choice": pick, "confidence": confidence, "probabilities": probabilities}


class SelectionTests(unittest.TestCase):
    def jev(self):
        with patch.dict("os.environ", {"TYPESAFE_API_KEY": "test"}):
            return Jev()

    def run_select(self, pick_literal=None, confidence=.95, tie=False):
        jev, evidence = self.jev(), about_evidence()
        candidates = literal_candidates(evidence)
        def respond(http, path, body, timeout, **kwargs):
            options = body["questions"]["field_0"]["criteria"]
            pick = "none" if pick_literal is None else next(
                f"c{i}" for i, (literal, _) in enumerate(candidates) if literal == pick_literal)
            answer = choice(options, pick, confidence)
            if tie:
                other = next(key for key in options if key != pick)
                answer["probabilities"][pick] = answer["probabilities"][other] = .5
            return {"answers": {"field_0": answer}}
        with patch("mobile_agent.models.request_inference", side_effect=respond):
            return jev.select_fields("Report the Model Number", SCHEMA, evidence), evidence

    def test_flat_string_schemas_only(self):
        self.assertTrue(selectable_schema(SCHEMA))
        for schema in ({"type": "string"}, {"type": "object", "properties": {"n": {"type": "integer"}}},
                       {"type": "object", "properties": {"n": {"type": "string", "pattern": "^a$"}}},
                       {"type": "object", "properties": {}}):
            self.assertFalse(selectable_schema(schema), schema)
        # An answer set ("Answer 'on' or 'off'") stays selectable: the pick is still an
        # observed literal, and the schema then also holds it to the set.
        self.assertTrue(selectable_schema({"type": "object", "properties": {
            "n": {"type": "string", "enum": ["on", "off"]}}}))

    def test_combined_labels_offer_their_parts(self):
        literals = [literal for literal, _ in literal_candidates(about_evidence())]
        self.assertIn("MTQM3LL/A", literals)
        self.assertIn("Model Number, MTQM3LL/A", literals)

    def test_selected_part_is_a_valid_grounded_extraction(self):
        result, evidence = self.run_select("MTQM3LL/A")
        self.assertEqual(result["data"], {"model_number": "MTQM3LL/A"})
        self.assertEqual(result["citations"][0]["quote"], "Model Number, MTQM3LL/A")
        validate_extraction(result, SCHEMA, evidence)

    def test_none_defers_to_the_generative_extractor(self):
        with self.assertRaises(SelectionUnsure):
            self.run_select(None)

    def test_facts_inside_sentences_are_offered(self):
        evidence = Evidence()
        evidence.add(Snapshot([Element("0", ", whose company designed and built the tower from 1887 to 1889.",
                                       "StaticText", (0, .3, 1, .05)),
                               Element("1", "Born 31 January 1956 in Haarlem; height 330 m", "StaticText",
                                       (0, .4, 1, .05))], "", 400, 800, "wda"), 0)
        literals = {literal for literal, _ in literal_candidates(evidence.public())}
        self.assertTrue({"1887", "1889", "31 January 1956", "1956", "330 m"} <= literals, literals)

    def test_low_confidence_or_tie_defers_to_the_helper(self):
        with self.assertRaises(SelectionUnsure):
            self.run_select("MTQM3LL/A", confidence=.4)
        with self.assertRaises(SelectionUnsure):
            self.run_select("MTQM3LL/A", tie=True)


class PrefetchTests(unittest.TestCase):
    def setup(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from mobile_agent.models import Decision
        from mobile_agent.task_policy import OutputSupport, StopGate
        screen = Snapshot([Element("0", "Model Number, MTQM3LL/A", "Cell", (0, .3, 1, .05))], "", 400, 800, "wda")
        driver = SimpleNamespace(can_type=False, observe=Mock(return_value=screen), execute=Mock())
        model = Mock(spec=["decide", "select_fields", "verify_output", "side_channel", "verify_action"])
        model.decide.return_value = Decision("DONE", None, .99, .99, 0, "offline", 0, {}, StopGate.CONTINUE)
        side = object()
        model.side_channel.return_value = side
        model.select_fields.return_value = {"data": {"model_number": "MTQM3LL/A"}, "citations": [
            {"path": "/model_number", "evidence_id": "e0", "quote": "Model Number, MTQM3LL/A"}]}
        model.verify_output.return_value = OutputSupport.SUPPORTED
        return driver, model, side

    def test_answer_is_selected_and_verified_during_the_completion_check(self):
        from mobile_agent.agent import Agent
        driver, model, side = self.setup()
        result = Agent(driver, model).run("Report the Model Number", execute=True, output_schema=SCHEMA,
                                          output_format="json")
        self.assertEqual(result["data"], {"model_number": "MTQM3LL/A"})
        self.assertEqual(model.select_fields.call_count, 1)
        self.assertEqual(model.verify_output.call_count, 1)
        self.assertIs(model.select_fields.call_args.kwargs["http"], side)
        self.assertIs(model.verify_output.call_args.kwargs["http"], side)

    def test_prefetch_for_a_different_screen_is_never_used(self):
        from concurrent.futures import Future
        from types import SimpleNamespace
        from mobile_agent.agent import Agent
        future = Future()
        future.set_result(("stale", "stale"))
        state = SimpleNamespace(prefetch=("other-fingerprint", future), budget=lambda: 5)
        screen = Snapshot([], "", 400, 800, "wda")
        self.assertIsNone(Agent.__new__(Agent)._take_prefetch(state, screen))
        self.assertIsNone(state.prefetch)


if __name__ == "__main__":
    unittest.main()
