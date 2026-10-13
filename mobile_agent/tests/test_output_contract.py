"""Presentation choices cannot weaken extraction or replay safety."""

import unittest

from ..output_contract import validate_automatic_schema, validate_format, validate_output


ROW = {"type": "object", "additionalProperties": False,
       "properties": {"name": {"type": "string"}, "count": {"type": ["integer", "null"]}}}
TABLE = {"type": "array", "items": ROW, "maxItems": 20}


class OutputContractTests(unittest.TestCase):
    def test_auto_is_a_request_policy_not_a_result_format(self):
        self.assertIsNone(validate_output("auto", None))
        with self.assertRaises(ValueError):
            validate_output("auto", ROW)
        with self.assertRaises(ValueError):
            validate_format("auto")
        with self.assertRaises(ValueError):
            validate_automatic_schema("auto", ROW)

    def test_non_tabular_formats_allow_optional_schema(self):
        self.assertIsNone(validate_output("text", None))
        self.assertIsNone(validate_output("csv", None))
        with self.assertRaises(ValueError):
            validate_output("text", ROW)
        for output_format in ("json", "yaml", "markdown"):
            self.assertIsNone(validate_output(output_format, None))
            self.assertEqual(validate_output(output_format, TABLE), TABLE)

    def test_csv_accepts_flat_row_or_bounded_table(self):
        for schema in (ROW, TABLE):
            self.assertEqual(validate_output("csv", schema), schema)

    def test_csv_rejects_ambiguous_or_nested_columns(self):
        cases = [{"type": "string"},
                 {"type": "object", "properties": {}, "additionalProperties": False},
                 {**ROW, "properties": {"nested": ROW}},
                 {**ROW, "properties": {"nested": TABLE}},
                 {**TABLE, "items": {"type": "string"}},
                 {**ROW, "type": ["object", "null"]}]
        for schema in cases:
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                validate_output("csv", schema)

    def test_all_formats_keep_canonical_schema_validation(self):
        for output_format in ("json", "yaml", "csv", "markdown"):
            with self.subTest(output_format=output_format), self.assertRaises(ValueError):
                validate_output(output_format, {"type": "array", "items": ROW})

    def test_invalid_formats_are_rejected(self):
        for value in ("JSON", "xml", "", None, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_output(value, ROW)
