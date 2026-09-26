"""Closed-set answers: the request's own options, cited but judged, never copied. Offline."""

import unittest

from mobile_agent.extraction import Evidence, closed_answers, validate_extraction
from mobile_agent.state import Element, Snapshot

WIFI = {"type": "object", "properties": {"wifi": {"type": "string"}}, "required": ["wifi"],
        "additionalProperties": False}


def evidence():
    store = Evidence()
    store.add(Snapshot([Element("0", "Wi-Fi, Home Network", "Button", (0, .3, 1, .06))], "Wi-Fi Home Network",
                       393, 852, "wda"), 0)
    return store.public()


class ClosedAnswerTests(unittest.TestCase):
    def test_the_request_names_the_answer_set(self):
        schema = closed_answers("In Settings, is Wi-Fi turned on or off? Answer 'on' or 'off'.", WIFI)
        self.assertEqual(schema["properties"]["wifi"]["enum"], ["on", "off"])
        self.assertIs(closed_answers("Report the Wi-Fi network name.", WIFI), WIFI)

    def test_a_closed_value_needs_a_real_citation_not_a_literal(self):
        schema, observed = closed_answers("Is it on? Answer 'on' or 'off'.", WIFI), evidence()
        entry = observed["entries"][0]
        ok = {"data": {"wifi": "on"},
              "citations": [{"path": "/wifi", "evidence_id": entry["id"], "quote": "Home Network"}]}
        self.assertEqual(validate_extraction(ok, schema, observed)["data"], {"wifi": "on"})
        for bad in ({"data": {"wifi": "maybe"}, "citations": ok["citations"]},        # outside the set
                    {"data": {"wifi": "on"}, "citations": []},                        # no evidence
                    {"data": {"wifi": "on"}, "citations": [{"path": "/wifi", "evidence_id": entry["id"],
                                                            "quote": "invented"}]}):  # quote not observed
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_extraction(bad, schema, observed)

    def test_without_an_answer_set_the_literal_rule_stands(self):
        observed = evidence()
        entry = observed["entries"][0]
        with self.assertRaises(ValueError):
            validate_extraction({"data": {"wifi": "on"}, "citations": [
                {"path": "/wifi", "evidence_id": entry["id"], "quote": "Home Network"}]}, WIFI, observed)


if __name__ == "__main__":
    unittest.main()


class CalibratedClaimTests(unittest.TestCase):
    def test_field_and_entity_claims_accept_and_weak_ones_do_not(self):
        from mobile_agent.task_policy import agreement_reason
        strong = {"claim_final_state": .45, "claim_entity": .95, "claim_field": .92,
                  "p_supported": .01, "p_unsupported": .9, "p_unclear": .07}
        self.assertEqual(agreement_reason(strong), "claims_calibrated")
        for weaker in ({**strong, "claim_field": .34}, {**strong, "claim_entity": .6},
                       {**strong, "p_unclear": .3}, {**strong, "claim_field": None}):
            with self.subTest(weaker=weaker):
                self.assertIsNone(agreement_reason(weaker))
