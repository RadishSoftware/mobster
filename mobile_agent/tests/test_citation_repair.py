"""A helper's citation slips are undone before strict validation; its values never are. Offline."""

import unittest

from mobile_agent.extraction import repair_citations, validate_extraction

SCHEMA = {"type": "object", "properties": {"year": {"type": "string"}}, "required": ["year"],
          "additionalProperties": False}
EVIDENCE = {"entries": [
    {"id": "e73", "text": "The tower is named after the engineer"},
    {"id": "e74", "text": "Gustave Eiffel"},
    {"id": "e75", "text": "Eiffel"},
    {"id": "e76", "text": ", whose company designed and built the tower from 1887 to 1889."},
    {"id": "e90", "text": "Tallest in the world from 1889 to 1930"},
]}


class CitationRepairTests(unittest.TestCase):
    def test_a_quote_cited_at_a_neighbouring_entry_is_repointed(self):
        result = {"data": {"year": "1889"}, "citations": [{"path": "/year", "evidence_id": "e74", "quote": "1889"}]}
        with self.assertRaises(ValueError):
            validate_extraction(result, SCHEMA, EVIDENCE)
        repaired = validate_extraction(repair_citations(result, EVIDENCE), SCHEMA, EVIDENCE)
        self.assertEqual(repaired["citations"][0]["evidence_id"], "e76")

    def test_citations_keyed_by_field_become_a_list(self):
        result = {"data": {"year": "1889"}, "citations": {"year": {
            "path": "/year", "evidence_id": "e76", "quote": ", whose company designed and built the tower from "
                                                           "1887 to 1889."}}}
        self.assertEqual(validate_extraction(repair_citations(result, EVIDENCE), SCHEMA, EVIDENCE)["data"],
                         {"year": "1889"})

    def test_a_quote_far_from_the_cited_entry_is_left_to_fail(self):
        result = {"data": {"year": "1930"}, "citations": [{"path": "/year", "evidence_id": "e73", "quote": "1930"}]}
        with self.assertRaises(ValueError):
            validate_extraction(repair_citations(result, EVIDENCE), SCHEMA, EVIDENCE)

    def test_a_value_absent_from_the_evidence_is_never_repaired_into_one(self):
        result = {"data": {"year": "1890"}, "citations": [{"path": "/year", "evidence_id": "e74", "quote": "1890"}]}
        with self.assertRaises(ValueError):
            validate_extraction(repair_citations(result, EVIDENCE), SCHEMA, EVIDENCE)


if __name__ == "__main__":
    unittest.main()
