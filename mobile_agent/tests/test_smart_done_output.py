import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from jsonschema import Draft202012Validator

from mobile_agent import engines, smart_result
from mobile_agent.frontier import output_instruction
from mobile_agent.tests.seam_tasks import SmartBase
from mobile_agent.tests.test_engines import Script
from mobile_agent.tests.test_longform import FEED_GOAL, Navigator
from mobile_agent.tests.test_longform import BaseServer as LongBase

TABLE = {"columns": ["name", "count"], "rows": [["Sam", 2], ["Alex", 1]], "single": False}
RECORDS = [{"name": "Sam", "count": 2}, {"name": "Alex", "count": 1}]
CUSTOM = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"],
          "additionalProperties": False}


class Client:
    model = "configured-test-model"

    def __init__(self, reply=TABLE):
        self.reply, self.calls = reply, []

    def complete(self, messages, schema, **kwargs):
        self.calls.append((messages, schema))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply, {}


def summarize(client, data_json, fmt="json", schema=None, status="completed", **kwargs):
    return smart_result.summarize({"status": status, "answer": "Sam has 2 and Alex has 1.", "data_json": data_json},
                             SimpleNamespace(client=client, _contract=None), None,
                             request="Report the names and counts", output_format=fmt, output_schema=schema, **kwargs)


class DoneOutputTests(unittest.TestCase):
    def test_valid_automatic_exports_remove_one_call_and_validate_actual_records(self):
        for fmt in smart_result.AUTOMATIC_FORMATS:
            with self.subTest(fmt=fmt):
                baseline, done = Client(), Client()
                before = summarize(baseline, None, fmt)
                after = summarize(done, json.dumps(TABLE), fmt)
                self.assertEqual((len(baseline.calls), len(done.calls)), (1, 0))
                self.assertEqual(after["data"], RECORDS)
                self.assertEqual(after["data"], before["data"])
                self.assertEqual(after["resolved_output_format"], fmt)
                self.assertEqual(after["schema_source"], "automatic")
                Draft202012Validator(after["output_schema"]).validate(after["data"])

    def test_empty_list_is_valid_and_single_record_keeps_shape(self):
        for table, expected in (({**TABLE, "rows": []}, []),
                                ({**TABLE, "rows": [["Sam", 2]], "single": True}, {"name": "Sam", "count": 2})):
            client = Client()
            result = summarize(client, json.dumps(table))
            self.assertEqual(result["data"], expected)
            self.assertEqual(client.calls, [])
            self.assertTrue(result["schema_validated"])

    def test_missing_invalid_and_misaligned_done_table_use_same_client_fallback_once(self):
        for value in (None, "not JSON", "null", "{}", json.dumps({**TABLE, "rows": [["Sam"]]}),
                      json.dumps({**TABLE, "columns": ["name", "name"]}),
                      json.dumps({**TABLE, "rows": [[{}, 2]]})):
            with self.subTest(value=value):
                client = Client()
                result = summarize(client, value)
                self.assertEqual(len(client.calls), 1)
                self.assertIs(client.calls[0][1], smart_result.TABLE)
                self.assertEqual(result["data"], RECORDS)
                self.assertEqual(result["model"], client.model)

    def test_failed_fallback_and_blocked_or_denied_runs_preserve_existing_output(self):
        client = Client(RuntimeError("offline provider failure"))
        result = summarize(client, "invalid", "csv")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual((result["resolved_output_format"], result["data_status"]), ("text", "observed"))
        self.assertIn("offline provider failure", result["structure_error"])
        for status in ("blocked", "approval_denied", "stopped"):
            client = Client()
            result = summarize(client, json.dumps(TABLE), status=status)
            self.assertEqual(client.calls, [])
            self.assertNotIn("schema_validated", result)

    def test_custom_schema_wins_and_existing_null_semantics_are_preserved(self):
        client = Client()
        result = summarize(client, '{"name":"Sam"}', schema=CUSTOM)
        self.assertEqual(result["data"], {"name": "Sam"})
        self.assertEqual(result["schema_source"], "custom")
        self.assertEqual(client.calls, [])
        client = Client({"data": None})
        result = summarize(client, "null", schema={"type": "null"})
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result["data_status"], "schema_validation_failed")

    def test_longform_export_uses_final_report_not_earlier_done_data(self):
        client = Client({**TABLE, "rows": [["Report row", 9]]})
        result = summarize(client, json.dumps(TABLE), report={"tldr": "Final report", "partial": False},
                           markdown="Report row has 9.")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result["data"], [{"name": "Report row", "count": 9}])
        self.assertIn("Report row has 9.", client.calls[0][0][1]["content"][0]["text"])


class OutputRuntimeTests(SmartBase):
    def test_runtime_forwards_custom_and_automatic_schemas_into_actual_done_turn(self):
        class ExportScript(Script):
            def __init__(self, value):
                super().__init__([[('DONE', None, None)]] * 3, [], answer="Sam has 2 and Alex has 1.")
                self.value, self.fallbacks, self.decision_schemas = value, 0, []
            def complete(self, messages, schema, timeout=60):
                if schema is smart_result.TABLE or set(schema.get("properties", {})) == {"data"}:
                    self.fallbacks += 1
                    return TABLE, {}
                out, usage = super().complete(messages, schema, timeout)
                if "actions" in schema["properties"]:
                    self.decision_schemas.append(schema)
                    if "data_json" in schema["properties"]:
                        out["data_json"] = json.dumps(self.value)
                return out, usage

        for fmt, schema, value, expected in (("csv", None, TABLE, RECORDS), ("json", None, TABLE, RECORDS),
                                             ("yaml", None, TABLE, RECORDS),
                                             ("json", CUSTOM, {"name": "Sam"}, {"name": "Sam"})):
            with self.subTest(fmt=fmt, custom=schema is not None):
                runtime = self.runtime()
                run = runtime.create("messages", "Report the names and counts", "live", output_format=fmt,
                                     output_schema=schema)
                script = ExportScript(value)
                self.work(runtime, run, script)
                self.assertEqual(run.summary["data"], expected, run.summary)
                self.assertTrue(run.summary["schema_validated"])
                self.assertEqual(script.fallbacks, 0)
                self.assertTrue(all("data_json" in shape["required"] for shape in script.decision_schemas))
                self.assertIn(output_instruction(run.output_schema if schema is not None
                                                 else smart_result.TABLE), script.prompts[0])


class LongOutputRuntimeTests(LongBase):
    def check_navigation_schema(self, custom):
        decisions, formatting, built = [], [], []
        class ExportNavigator(Navigator):
            def complete(self, messages, schema, timeout=60, **kwargs):
                if schema is smart_result.TABLE:
                    formatting.append(messages)
                    return TABLE, {}
                if set(schema.get("properties", {})) == {"data"}:
                    formatting.append(messages)
                    return {"data": {"name": "Sam"}}, {}
                if "actions" in schema.get("properties", {}):
                    decisions.append(schema)
                return super().complete(messages, schema, timeout=timeout, **kwargs)

        original = engines.build_frontier
        def build(*args, **kwargs):
            built.append(kwargs)
            return original(*args, **kwargs)
        with patch.object(engines, "acquire_client", side_effect=lambda key: ExportNavigator(self.prompts)), \
                patch.object(engines, "build_frontier", side_effect=build):
            status, data, _ = self.call("POST", "/api/longform/plan", {"goal": FEED_GOAL})
            self.assertEqual(status, 200)
            body = {"goal": FEED_GOAL, "engine": "smart", "longform": {"planId": data["plan"]["id"]},
                    "outputFormat": "json" if custom else "csv"}
            if custom:
                body["outputSchema"] = CUSTOM
            status, data, _ = self.call("POST", "/api/runs", body)
            self.assertEqual(status, 201, data)
            run = self.finished(self.runtime.runs[data["run"]["id"]])
        self.assertEqual(len(built), 1)
        self.assertIsNotNone(built[0]["longform"])
        self.assertIsNone(built[0]["output_schema"])
        self.assertTrue(decisions)
        self.assertTrue(all("data_json" not in schema["properties"] for schema in decisions))
        self.assertTrue(all("also put the answer in data_json" not in prompt for prompt in self.prompts))
        self.assertEqual(len(formatting), 1)
        self.assertIn(run.summary["markdown"], formatting[0][1]["content"][0]["text"])
        self.assertEqual(run.summary["data"], {"name": "Sam"} if custom else RECORDS)
        self.assertTrue(run.summary["schema_validated"])

    def test_longform_automatic_export_only_structures_final_report(self):
        self.check_navigation_schema(False)

    def test_longform_custom_schema_only_structures_final_report(self):
        self.check_navigation_schema(True)
