"""Automatic result shapes remain literal-grounded and completion-gated; no provider calls."""

from dataclasses import replace
import json
import unittest
from unittest.mock import Mock, patch

from mobile_agent.agent import Agent
from mobile_agent.demo import DemoDriver, DemoHelper, DemoModel, screen
from mobile_agent.extraction import InsufficientEvidence
from mobile_agent.models import Helper
from mobile_agent.task_policy import OutputSupport
from mobile_agent.output_contract import validate_automatic_schema, validate_output


STRING = {"type": "string"}
ROW = {"type": "object", "properties": {"title": STRING}, "required": ["title"], "additionalProperties": False}
ROWS = {"type": "array", "items": ROW, "maxItems": 10}
EVIDENCE = {"entries": [{"id": "e0", "text": "Coffee brewing guide", "field": "label", "role": "Button", "last_seen_step": 0}], "latest_step": 0}


def answer(data="Coffee brewing guide", schema=STRING, path=""):
    return {"schema": schema, "data": data, "citations": [{"path": path, "evidence_id": "e0", "quote": "Coffee brewing guide"}]}


class AutomaticHelperTests(unittest.TestCase):
    def helper(self, content, finish_reason="stop"):
        with patch.dict("os.environ", {"TEXT_MODEL_PROVIDER": "openrouter", "TEXT_MODEL_API_KEY": "test-only", "TEXT_MODEL": "test-only"}):
            helper = Helper()
        helper.http.request = Mock(return_value={"choices": [{"message": {"content": json.dumps(content)}, "finish_reason": finish_reason}], "usage": {}})
        return helper

    def test_text_literal_uses_one_terminal_call_with_exact_evidence_citation(self):
        helper = self.helper(answer())
        result = helper.extract_auto(EVIDENCE, "Read the visible title", "text")
        self.assertEqual(result, answer())
        self.assertEqual(helper.calls, 1)
        helper.http.request.assert_called_once()
        body = helper.http.request.call_args.args[2]
        self.assertEqual(json.loads(body["messages"][1]["content"])["format"], "text")
        self.assertIn("A field label is not its value", body["messages"][0]["content"])

    def test_csv_accepts_model_selected_flat_rows_with_relative_citations(self):
        content = answer([{"title": "Coffee brewing guide"}], ROWS, "/0/title")
        helper = self.helper(content)
        self.assertEqual(helper.extract_auto(EVIDENCE, "Read titles", "csv"), content)
        self.assertEqual(helper.calls, 1)

    def test_fabricated_fact_is_rejected_even_with_real_quote(self):
        helper = self.helper(answer("Invented video"))
        with self.assertRaisesRegex(ValueError, "literal-evidence"):
            helper.extract_auto(EVIDENCE, "Read title", "text")

    def test_invented_quote_and_wrong_pointer_cannot_ground_an_answer(self):
        for citation in ({"path": "", "evidence_id": "e0", "quote": "Invented video"},
                         {"path": "/data", "evidence_id": "e0", "quote": "Coffee brewing guide"}):
            helper = self.helper({**answer(), "citations": [citation]})
            with self.subTest(citation=citation), self.assertRaises(ValueError):
                helper.extract_auto(EVIDENCE, "Read title", "text")

    def test_explicit_abstention_is_distinct_from_malformed_or_partial_output(self):
        helper = self.helper({"schema": None, "data": None, "citations": []})
        with self.assertRaises(InsufficientEvidence):
            helper.extract_auto(EVIDENCE, "Read a hidden value", "text")
        for content in ({"schema": None, "data": "Coffee brewing guide", "citations": []},
                        {**answer(), "extra": "not allowed"},
                        {"schema": STRING, "data": None, "citations": []}):
            with self.subTest(content=content), self.assertRaises(ValueError) as failure:
                self.helper(content).extract_auto(EVIDENCE, "Read", "text")
            self.assertNotIsInstance(failure.exception, InsufficientEvidence)

    def test_truncated_response_and_oversized_schema_fail_closed(self):
        with self.assertRaises(ValueError):
            self.helper(answer(), "length").extract_auto(EVIDENCE, "Read", "text")
        enormous = {"type": "object", "properties": {f"field{i}": STRING for i in range(41)}, "additionalProperties": False}
        with self.assertRaises(ValueError):
            self.helper(answer({}, enormous)).extract_auto(EVIDENCE, "Read", "json")

    def test_invalid_format_is_rejected_before_spending_helper_call(self):
        helper = self.helper(answer())
        for output_format in ("html", "auto", [], None):
            with self.subTest(output_format=output_format), self.assertRaises(ValueError):
                helper.extract_auto(EVIDENCE, "Read", output_format)
        self.assertEqual(helper.calls, 0)
        helper.http.request.assert_not_called()

    def test_extract_prompt_requires_literal_reading_and_abstention(self):
        helper = self.helper(answer())
        helper.extract_auto(EVIDENCE, "Read title", "text")
        body = helper.http.request.call_args.args[2]
        prompt = body["messages"][0]["content"]
        self.assertIn("1.2K stays", prompt)
        self.assertIn("Prefer abstention over any guess", prompt)
        self.assertIn("One field per claim", prompt)


class AutomaticShapeTests(unittest.TestCase):
    def test_null_request_schema_selects_automatic_but_null_resolved_schema_is_invalid(self):
        self.assertIsNone(validate_output("text", None))
        self.assertIsNone(validate_output("csv", None))
        for output_format in ("text", "csv", "json", "yaml", "markdown"):
            with self.subTest(output_format=output_format), self.assertRaises(ValueError):
                validate_automatic_schema(output_format, None)

    def test_text_custom_schema_stays_rejected_but_automatic_literal_shape_is_allowed(self):
        with self.assertRaises(ValueError):
            validate_output("text", STRING)
        self.assertEqual(validate_automatic_schema("text", STRING), STRING)
        self.assertEqual(validate_automatic_schema("text", ROW), ROW)
        with self.assertRaises(ValueError):
            validate_automatic_schema("text", ROWS)
        self.assertEqual(validate_automatic_schema("csv", ROWS), ROWS)

    def test_text_and_csv_reject_nested_automatic_values(self):
        nested = {"type": "object", "properties": {"nested": ROW}, "additionalProperties": False}
        for output_format in ("text", "csv"):
            with self.subTest(output_format=output_format), self.assertRaises(ValueError):
                validate_automatic_schema(output_format, nested)
        self.assertEqual(validate_automatic_schema("json", nested), nested)


class AutomaticAgentTests(unittest.TestCase):
    def ready(self, content=None):
        driver = DemoDriver()
        driver.stage = "results"
        helper = DemoHelper()
        helper.calls = 0
        def extract(*_args, **_kwargs):
            helper.calls += 1
            return content if content is not None else answer()
        helper.extract_auto = Mock(side_effect=extract)
        helper.plan = Mock(side_effect=AssertionError("No planner belongs on the direct task path"))
        return driver, helper

    def test_terminal_auto_result_has_resolved_schema_and_one_helper_call_after_two_done_checks(self):
        driver, helper = self.ready()
        model = Mock()
        model.verify_output.return_value = OutputSupport.SUPPORTED
        done = DemoModel().decide(screen("results"), "", [])
        def decide(*_args, **_kwargs):
            helper.extract_auto.assert_not_called()
            return done
        model.decide.side_effect = decide
        events = []
        result = Agent(driver, model, helper, emit=events.append).run("Read title", execute=True, output_format="text")
        self.assertEqual(model.decide.call_count, 2)
        self.assertEqual(result["status"], "completed_unverified")
        self.assertEqual(result["data"], "Coffee brewing guide")
        self.assertEqual(result["data_status"], "extracted")
        self.assertEqual(result["schema_source"], "automatic")
        self.assertEqual(result["output_schema"], STRING)
        self.assertTrue(result["schema_validated"])
        self.assertFalse(result["independently_verified"])
        self.assertEqual(result["helper_calls"], 1)
        helper.plan.assert_not_called()
        self.assertEqual(driver.actions, [])

    def test_agent_revalidates_automatic_schema_and_grounding_from_helper_boundary(self):
        for content in (answer("Invented title"), answer("Coffee brewing guide", {"type": "object", "additionalProperties": True}), {"data": "Coffee brewing guide", "citations": []}):
            driver, helper = self.ready(content)
            result = Agent(driver, DemoModel(), helper).run("Read title", execute=True, output_format="text")
            self.assertEqual(result["data_status"], "extraction_failed")
            self.assertIsNone(result["data"])
            self.assertFalse(result["schema_validated"])

    def test_no_automatic_extractor_before_completion_gate_passes(self):
        done = DemoModel().decide(screen("results"), "", [])
        for decisions, expected in (([replace(done, blocked_probability=.99)], "inconsistent_completion"),
                                    ([done, replace(done, operation="WAIT")], "completion_not_confirmed")):
            driver, helper = self.ready()
            model = Mock()
            model.decide.side_effect = decisions
            result = Agent(driver, model, helper).run("Read title", execute=True, output_format="text")
            self.assertEqual(result["status"], expected)
            self.assertEqual(result["data_status"], "not_extracted")
            self.assertIsNone(result["data"])
            helper.extract_auto.assert_not_called()

    def test_preview_does_not_extract(self):
        driver, helper = self.ready()
        result = Agent(driver, DemoModel(), helper).run("Read title", output_format="text")
        self.assertEqual(result["data_status"], "preview")
        helper.extract_auto.assert_not_called()

    def test_abstention_and_budget_exhaustion_do_not_claim_a_schema_validated_result(self):
        driver, helper = self.ready()
        helper.extract_auto.side_effect = InsufficientEvidence("not present")
        result = Agent(driver, DemoModel(), helper).run("Read hidden field", execute=True, output_format="csv")
        self.assertEqual(result["data_status"], "insufficient_evidence")
        self.assertIsNone(result["data"])
        self.assertFalse(result["schema_validated"])
        driver, helper = self.ready()
        result = Agent(driver, DemoModel(), helper, max_helper_calls=0).run("Read title", execute=True, output_format="text")
        self.assertEqual(result["data_status"], "helper_budget_exhausted")
        helper.extract_auto.assert_not_called()

    def test_custom_schema_keeps_custom_provenance(self):
        driver, helper = self.ready()
        helper.extract = Mock(return_value={"data": "Coffee brewing guide", "citations": answer()["citations"]})
        result = Agent(driver, DemoModel(), helper).run("Read title", execute=True, output_schema=STRING, output_format="json")
        self.assertEqual(result["schema_source"], "custom")
        self.assertEqual(result["output_schema"], STRING)
        self.assertEqual(result["data_status"], "extracted")
        helper.extract_auto.assert_not_called()

    def test_automatic_field_name_misbinding_fails_closed_not_shipped(self):
        # model_number literal claimed as model_name is extraction_failed / insufficient_evidence.
        from mobile_agent.extraction import validate_extraction
        schema = {"type": "object", "additionalProperties": False,
                  "properties": {"model_name": {"type": "string"}}}
        evidence = {"entries": [
            {"id": "e0", "text": "Model Number", "label_context": "Model Number", "field": "label",
             "role": "StaticText", "last_seen_step": 0},
            {"id": "e1", "text": "VM0001LL/A", "label_context": "Model Number", "field": "value",
             "role": "StaticText", "last_seen_step": 0}]}
        bad = {"data": {"model_name": "VM0001LL/A"},
               "citations": [{"path": "/model_name", "evidence_id": "e1", "quote": "VM0001LL/A"}]}
        with self.assertRaises(ValueError):
            validate_extraction(bad, schema, evidence)
        good = {"data": {"model_name": "VM0001LL/A"},
                "citations": [{"path": "/model_name", "evidence_id": "e1", "quote": "VM0001LL/A"}]}
        good_schema = {"type": "object", "additionalProperties": False,
                       "properties": {"model_name": {"type": "string"}}}
        good_evidence = {"entries": [
            {"id": "e1", "text": "VM0001LL/A", "label_context": "Model Name", "field": "value",
             "role": "StaticText", "last_seen_step": 0}]}
        self.assertEqual(validate_extraction(good, good_schema, good_evidence)["data"],
                         good["data"])

    def test_units_transform_rejected_and_literal_1_2K_ships(self):
        from mobile_agent.extraction import validate_extraction
        evidence = {"entries": [{"id": "e0", "text": "Followers, 1.2K", "field": "label",
                                 "role": "StaticText", "last_seen_step": 1}]}
        schema = {"type": "string"}
        with self.assertRaises(ValueError):
            validate_extraction({"data": "1200",
                                 "citations": [{"path": "", "evidence_id": "e0", "quote": "1.2K"}]},
                                schema, evidence)
        self.assertEqual(validate_extraction({"data": "1.2K",
                                              "citations": [{"path": "", "evidence_id": "e0",
                                                             "quote": "Followers, 1.2K"}]},
                                             schema, evidence)["data"], "1.2K")


if __name__ == "__main__":
    unittest.main()
