"""A malformed helper response is retried; an abstention and a wrong answer are not.

Also pins the per-field claim check (output-field-eval:16-18): a literal bound
to the wrong semantic field is rejected without an LLM oracle.
"""

import unittest
from unittest.mock import Mock

from ..agent import Agent
from ..demo import DemoDriver, DemoHelper, DemoModel, screen
from ..extraction import InsufficientEvidence, check_field_claims, validate_extraction
from ..task_policy import OutputSupport

STRING = {"type": "string"}
ANSWER = {"data": "Coffee brewing guide",
          "citations": [{"path": "", "evidence_id": "e0", "quote": "Coffee brewing guide"}]}


class ExtractionRetryTests(unittest.TestCase):
    def build(self, responses, **kwargs):
        driver = DemoDriver()
        driver.stage = "results"
        helper = DemoHelper()
        helper.calls = 0
        self.attempts = []

        def extract(evidence, goal, schema, **_kwargs):
            helper.calls += 1
            self.attempts.append(len(self.attempts))
            outcome = responses[min(len(self.attempts) - 1, len(responses) - 1)]
            if isinstance(outcome, Exception):
                raise outcome
            # Citations must resolve against the real evidence ids.
            first = evidence["entries"][0]["id"]
            return {"data": outcome, "citations": [{"path": "", "evidence_id": first,
                                                    "quote": evidence["entries"][0]["text"]}]}

        helper.extract = Mock(side_effect=extract)
        model = Mock()
        model.decide.return_value = DemoModel().decide(screen("results"), "", [])
        model.verify_output.return_value = OutputSupport.SUPPORTED
        self.events = []
        return Agent(driver, model, helper, emit=self.events.append, **kwargs)

    def run_task(self, agent):
        return agent.run("Read the title", execute=True, output_schema=STRING)

    def test_an_unusable_response_is_retried_once_and_then_succeeds(self):
        agent = self.build([ValueError("malformed"), "Coffee brewing guide"])
        result = self.run_task(agent)
        self.assertEqual(result["data_status"], "extracted")
        self.assertEqual(len(self.attempts), 2)
        retry, = [e for e in self.events if e["event"] == "extraction_retry"]
        self.assertEqual(retry["reason"], "unusable_response")

    def test_an_abstention_is_never_retried(self):
        agent = self.build([InsufficientEvidence("absent")])
        result = self.run_task(agent)
        self.assertEqual(result["data_status"], "insufficient_evidence")
        self.assertEqual(len(self.attempts), 1)
        self.assertEqual([e for e in self.events if e["event"] == "extraction_retry"], [])

    def test_retries_are_bounded_and_still_fail_closed(self):
        agent = self.build([ValueError("malformed")], max_extraction_retries=2)
        result = self.run_task(agent)
        self.assertEqual(result["data_status"], "extraction_failed")
        self.assertIsNone(result["data"])
        self.assertEqual(len(self.attempts), 3)

    def test_retry_never_exceeds_the_helper_budget(self):
        agent = self.build([ValueError("malformed")], max_extraction_retries=5, max_helper_calls=1)
        result = self.run_task(agent)
        self.assertEqual(result["data_status"], "extraction_failed")
        self.assertEqual(len(self.attempts), 1)

    def test_disabling_retries_preserves_the_previous_behaviour(self):
        agent = self.build([ValueError("malformed"), "Coffee brewing guide"], max_extraction_retries=0)
        self.assertEqual(self.run_task(agent)["data_status"], "extraction_failed")
        self.assertEqual(len(self.attempts), 1)

    def test_the_allowance_is_validated(self):
        for invalid in (-1, 11, 1.5, "1"):
            with self.assertRaises(ValueError):
                Agent(DemoDriver(), DemoModel(), DemoHelper(), max_extraction_retries=invalid)


MODEL_SCHEMA = {"type": "object", "additionalProperties": False,
                "properties": {"model_name": {"type": ["string", "null"]},
                               "model_number": {"type": ["string", "null"]}},
                "required": ["model_name", "model_number"]}


def about_model(label="Model Number", value="VM0001LL/A", *, superseded=False, group=None):
    """One coherent label/value observation binding, as Evidence.decision_context emits."""
    return [
        {"id": "e0", "text": label, "label_context": label, "field": "label", "role": "StaticText",
         "step": 0, "last_seen_step": 0, "group": group if group is not None else "g0",
         "superseded": superseded},
        {"id": "e1", "text": value, "label_context": label, "field": "value", "role": "StaticText",
         "step": 0, "last_seen_step": 0, "group": group if group is not None else "g0",
         "superseded": superseded},
    ]


class FieldClaimTests(unittest.TestCase):
    """Local structural/lexical per-field binding check; never an LLM oracle."""

    def claim(self, data, entries, schema=MODEL_SCHEMA):
        citations = []
        for path, value in (
                ("/model_name", (data or {}).get("model_name")),
                ("/model_number", (data or {}).get("model_number"))):
            if value is None:
                continue
            for entry in entries:
                if entry["text"] == value:
                    citations.append({"path": path, "evidence_id": entry["id"], "quote": entry["text"]})
                    break
        return {"data": data, "citations": citations}, {"entries": entries}

    def test_model_name_vs_model_number_swap_rejected(self):
        # The Model Number literal bound as model_name is the measured 3/3 failure.
        data = {"model_name": "VM0001LL/A", "model_number": None}
        candidate, evidence = self.claim(data, about_model())
        with self.assertRaises(ValueError):
            check_field_claims(candidate, evidence)
        with self.assertRaises(ValueError):
            validate_extraction(candidate, MODEL_SCHEMA, evidence)
        # The inverse swap is equally refused: a Model Name binding claimed as model_number.
        swapped = {"model_name": None, "model_number": "Alpha"}
        candidate, evidence = self.claim(swapped, about_model("Model Name", "Alpha"))
        with self.assertRaises(ValueError):
            check_field_claims(candidate, evidence)

    def test_correct_binding_accepted(self):
        data = {"model_name": None, "model_number": "VM0001LL/A"}
        candidate, evidence = self.claim(data, about_model())
        self.assertEqual(check_field_claims(candidate, evidence), None)
        self.assertEqual(validate_extraction(candidate, MODEL_SCHEMA, evidence)["data"], data)
        named = {"model_name": "Alpha", "model_number": None}
        candidate, evidence = self.claim(named, about_model("Model Name", "Alpha"))
        self.assertEqual(check_field_claims(candidate, evidence), None)
        # Abstention stays empty and never fails the claim check.
        abstain = {"data": None, "citations": []}
        self.assertIsNone(check_field_claims(abstain, evidence))
        nullable = {"type": ["object", "null"], "additionalProperties": False,
                    "properties": MODEL_SCHEMA["properties"]}
        self.assertIsNone(validate_extraction(abstain, nullable, evidence)["data"])

    def test_missing_label_context_rejected(self):
        unlabeled = [{"id": "e1", "text": "VM0001LL/A", "label_context": "", "field": "value",
                      "role": "StaticText", "step": 0, "last_seen_step": 0}]
        data = {"model_name": None, "model_number": "VM0001LL/A"}
        candidate, evidence = self.claim(data, unlabeled)
        with self.assertRaises(ValueError):
            check_field_claims(candidate, evidence)
        with self.assertRaises(ValueError):
            validate_extraction(candidate, MODEL_SCHEMA, evidence)

    def test_superseded_group_rejected(self):
        data = {"model_name": None, "model_number": "VM0001LL/A"}
        candidate, evidence = self.claim(data, about_model(superseded=True))
        with self.assertRaises(ValueError):
            check_field_claims(candidate, evidence)
        # A later value in the same observation group supersedes this binding.
        stale = about_model(value="OLD0001LL/A", group="g0")
        stale[1] = {**stale[1], "id": "e2", "text": "VM0001LL/A"}
        fresh = {**stale[1], "id": "e3", "text": "NEW0002LL/A", "last_seen_step": 3}
        candidate, evidence = self.claim({"model_name": None, "model_number": "VM0001LL/A"},
                                         [stale[0], stale[1], fresh])
        with self.assertRaises(ValueError):
            check_field_claims(candidate, evidence)

    def test_units_transform_1_2K_stays_literal(self):
        # 1.2K -> 1200 is a forbidden transform; the observed literal must ship.
        evidence = {"entries": [{"id": "e0", "text": "Followers, 1.2K", "field": "label",
                                 "role": "StaticText", "step": 1, "last_seen_step": 1}]}
        schema = {"type": "string"}
        with self.assertRaises(ValueError):
            validate_extraction({"data": "1200",
                                 "citations": [{"path": "", "evidence_id": "e0", "quote": "1.2K"}]},
                                schema, evidence)
        with self.assertRaises(ValueError):
            validate_extraction({"data": 1200,
                                 "citations": [{"path": "", "evidence_id": "e0", "quote": "Followers, 1.2K"}]},
                                {"type": "integer"}, evidence)
        self.assertEqual(validate_extraction({"data": "1.2K",
                                              "citations": [{"path": "", "evidence_id": "e0",
                                                             "quote": "Followers, 1.2K"}]},
                                             schema, evidence)["data"], "1.2K")

    def test_abstention_on_empty_evidence(self):
        nullable = {"type": ["string", "null"]}
        empty = {"entries": [], "latest_step": 0}
        self.assertIsNone(validate_extraction({"data": None, "citations": []}, nullable, empty)["data"])
        self.assertIsNone(check_field_claims({"data": None, "citations": []}, empty))
        # A concrete claim without evidence still fails closed.
        with self.assertRaises(ValueError):
            validate_extraction({"data": "ghost", "citations": []}, {"type": "string"}, empty)

    def test_label_text_is_not_a_value_for_its_own_field(self):
        # The model_label_missing success-path variant: with no value row observed,
        # filling model_name with its own label text must refuse, never ship.
        entries = about_model()
        entries.append({"id": "e2", "text": "Model Name", "label_context": "Model Name",
                        "field": "label", "role": "StaticText", "step": 0,
                        "last_seen_step": 0, "group": "g1"})
        data = {"model_name": "Model Name", "model_number": "VM0001LL/A"}
        candidate, evidence = self.claim(data, entries)
        with self.assertRaises(ValueError):
            check_field_claims(candidate, evidence)
        with self.assertRaises(ValueError):
            validate_extraction(candidate, MODEL_SCHEMA, evidence)
        # A field whose name asks for the label itself keeps the quote exemption.
        label_schema = {"type": "object", "additionalProperties": False,
                        "properties": {"field_label": {"type": "string"}}, "required": ["field_label"]}
        quoted = {"data": {"field_label": "Model Name"},
                  "citations": [{"path": "/field_label", "evidence_id": "e2", "quote": "Model Name"}]}
        self.assertEqual(validate_extraction(quoted, label_schema, evidence)["data"],
                         {"field_label": "Model Name"})
        # Root scalars quoting a label or message stay allowed.
        root = {"data": "Model Name",
                "citations": [{"path": "", "evidence_id": "e2", "quote": "Model Name"}]}
        self.assertEqual(validate_extraction(root, {"type": "string"}, evidence)["data"],
                         "Model Name")
        # A content element is its own context: a result-card label whose text is
        # unrelated to the field name stays shippable (title <- "Coffee brewing guide").
        card = {"entries": [{"id": "e0", "text": "Coffee brewing guide",
                             "label_context": "Coffee brewing guide", "field": "label",
                             "role": "Button", "step": 0, "last_seen_step": 0}]}
        titled = {"data": {"title": "Coffee brewing guide"},
                  "citations": [{"path": "/title", "evidence_id": "e0", "quote": "Coffee brewing guide"}]}
        title_schema = {"type": "object", "additionalProperties": False,
                        "properties": {"title": {"type": "string"}}, "required": ["title"]}
        self.assertEqual(validate_extraction(titled, title_schema, card)["data"],
                         {"title": "Coffee brewing guide"})

    def test_absence_marker_is_not_a_concrete_code_value(self):
        # "Recovery code hidden" may be quoted as content, never shipped as the code.
        entries = [{"id": "e0", "text": "Settings", "field": "label", "role": "StaticText",
                    "step": 1, "last_seen_step": 1},
                   {"id": "e1", "text": "Recovery code hidden", "label_context": "Recovery code",
                    "field": "value", "role": "StaticText", "step": 1, "last_seen_step": 1}]
        evidence = {"entries": entries}
        schema = {"type": "object", "additionalProperties": False,
                  "properties": {"recovery_code": {"type": "string"}}, "required": ["recovery_code"]}
        bad = {"data": {"recovery_code": "Recovery code hidden"},
               "citations": [{"path": "/recovery_code", "evidence_id": "e1", "quote": "Recovery code hidden"}]}
        with self.assertRaises(ValueError):
            check_field_claims(bad, evidence)
        with self.assertRaises(ValueError):
            validate_extraction(bad, schema, evidence)
        # Quoting the availability message itself stays allowed (content-as-label).
        quote_schema = {"type": "string"}
        quoted = {"data": "Recovery code hidden",
                  "citations": [{"path": "", "evidence_id": "e1", "quote": "Recovery code hidden"}]}
        self.assertEqual(validate_extraction(quoted, quote_schema, evidence)["data"],
                         "Recovery code hidden")

    def test_apple_ibrand_tokens_are_not_a_wrong_field_conflict(self):
        # Live harness regression (settings.about.version 0/3, 2026-09-21): SwiftUI
        # packs name+value into one label ("iOS Version, 26.6.1"). The camel splitter
        # turned "iOS" into {i, os}, so ios_version vs the row looked like a
        # divergent-head conflict and the correct answer was rejected.
        entries = [{"id": "e0", "text": "iOS Version, 26.6.1", "label_context": "iOS Version, 26.6.1",
                    "field": "label", "role": "SwiftUI.AccessibilityNode", "step": 5,
                    "last_seen_step": 5}]
        evidence = {"entries": entries}
        schema = {"type": "object", "additionalProperties": False,
                  "properties": {"ios_version": {"type": "string"}}, "required": ["ios_version"]}
        good = {"data": {"ios_version": "26.6.1"},
                "citations": [{"path": "/ios_version", "evidence_id": "e0", "quote": "26.6.1"}]}
        self.assertEqual(validate_extraction(good, schema, evidence)["data"],
                         {"ios_version": "26.6.1"})
        # The packed label itself is still refused as the shipped value.
        packed = {"data": {"ios_version": "iOS Version, 26.6.1"},
                  "citations": [{"path": "/ios_version", "evidence_id": "e0",
                                 "quote": "iOS Version, 26.6.1"}]}
        with self.assertRaises(ValueError):
            validate_extraction(packed, schema, evidence)
        # Tokenizer contract: i-brands stay whole, ordinary camelCase still splits.
        from mobile_agent.extraction import _tokens
        self.assertEqual(_tokens("iPhone 16 Plus"), {"iphone", "16", "plus"})
        self.assertEqual(_tokens("camelCaseWord"), {"camel", "case", "word"})


if __name__ == "__main__":
    unittest.main()
