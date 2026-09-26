"""Answer verification receives compact, bounded evidence. Offline."""

import json
import unittest

from mobile_agent.extraction import VERIFICATION_EVIDENCE_BYTES, Evidence
from mobile_agent.state import Element, Snapshot


def screen(step_labels, source="wda"):
    elements = [Element(str(i), label, "StaticText", (0, min(.9, i / 300), .5, .01), value=label)
                for i, label in enumerate(step_labels)]
    return Snapshot(elements, "\n".join(step_labels), 400, 800, source)


class VerificationEvidenceTests(unittest.TestCase):
    def test_static_text_value_equal_to_label_is_one_entry(self):
        evidence = Evidence()
        evidence.add(screen(["iOS Version"]), 0)
        self.assertEqual([e["field"] for e in evidence.entries], ["label"])

    def test_cited_entries_survive_the_budget_and_newest_come_next(self):
        evidence = Evidence()
        for step in range(6):
            evidence.add(screen([f"step {step} row {i} " + "x" * 60 for i in range(60)]), step)
        cited = evidence.entries[0]["id"]
        compact = evidence.for_verification([{"evidence_id": cited, "path": "/a", "quote": "q"}])
        self.assertLessEqual(len(json.dumps(compact["entries"]).encode()), VERIFICATION_EVIDENCE_BYTES + 200)
        ids = {entry["id"] for entry in compact["entries"]}
        self.assertIn(cited, ids)
        self.assertIn(evidence.entries[-1]["id"], ids)
        self.assertTrue(compact["truncated"])

    def test_compact_entries_keep_what_the_verifier_reads(self):
        evidence = Evidence()
        evidence.add(Snapshot([Element("0", "iOS Version, 26.0.1", "Cell", (0, .3, 1, .05), value="26.0.1")],
                              "", 400, 800, "wda"), 2)
        entries = evidence.for_verification([])["entries"]
        self.assertEqual({(e["text"], e.get("field", "label")) for e in entries},
                         {("iOS Version, 26.0.1", "label"), ("26.0.1", "value")})
        value = next(e for e in entries if e["text"] == "26.0.1")
        self.assertEqual((value["label_context"], value["row"], value["step"]), ("iOS Version, 26.0.1", 30, 2))


if __name__ == "__main__":
    unittest.main()


class RowContextTests(unittest.TestCase):
    def test_table_values_carry_their_row_label(self):
        from mobile_agent.extraction import row_contexts
        cells = [Element("0", "Height", "StaticText", (.05, .40, .2, .02)),
                 Element("1", "330", "StaticText", (.45, .40, .08, .02)),
                 Element("2", "m (1,083", "StaticText", (.54, .401, .15, .02)),
                 Element("3", "Observation deck", "StaticText", (.05, .45, .3, .02)),
                 Element("4", "276", "StaticText", (.45, .45, .08, .02))]
        rows = row_contexts(cells)
        self.assertEqual(rows["1"], "Height 330 m (1,083")
        self.assertEqual(rows["4"], "Observation deck 276")

    def test_row_context_reaches_the_verifier(self):
        evidence = Evidence()
        evidence.add(Snapshot([Element("0", "Height", "StaticText", (.05, .4, .2, .02)),
                               Element("1", "330", "StaticText", (.45, .4, .08, .02))], "", 400, 800, "wda"), 0)
        entries = evidence.for_verification([])["entries"]
        self.assertEqual(next(e for e in entries if e["text"] == "330")["row_context"], "Height 330")


class EvictionTests(unittest.TestCase):
    def test_a_long_scroll_keeps_its_newest_screens(self):
        evidence = Evidence()
        for step in range(30):
            evidence.add(screen([f"page {step} line {i} " + "y" * 80 for i in range(40)]), step)
        texts = [entry["text"] for entry in evidence.entries]
        self.assertTrue(any(text.startswith("page 29 ") for text in texts))
        self.assertFalse(any(text.startswith("page 0 ") for text in texts))
        self.assertTrue(evidence.truncated)
        self.assertEqual(len({entry["id"] for entry in evidence.entries}), len(evidence.entries))
