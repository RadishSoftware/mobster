"""A long report never quietly drops observations when chunk summaries fail."""

import math
import unittest
from types import SimpleNamespace

from mobile_agent.longform import costs, synthesis
from mobile_agent.longform.budget import Budget
from mobile_agent.longform.notebook import Item, Notebook
from mobile_agent.longform.session import LongSession


class Client:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []
        self.model = "claude-sonnet-5-5"

    def complete(self, messages, schema, **kwargs):
        self.calls.append(messages)
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return output, {}


def book_with(count):
    book = Notebook("a" * 12, "Compare the listings", "browse")
    for n in range(1, count + 1):
        item = book.add_item(Item(kind="row", identity=f"row-{n}", source={"name": f"Place {n}"},
                                  text={"details": [f"price {n}"]}))
        book.update_item(item.id, extract={"summary": f"Result {n}", "quality": 1})
    return book


def session(book, *, digest=None, synth=None, fallback=None):
    budget = Budget("time", 10, None, True, 600, "results", "10 minutes", "row")
    models = SimpleNamespace(digest=digest, synth=synth, synth_fallback=fallback, extract=None)
    return LongSession(book.run_id, book.request, {"kind": book.kind}, budget, models=models, notebook=book)


def theme(name, first, last):
    return {"name": name, "summary": "Distinct results", "item_ids": [f"i{n}" for n in range(first, last + 1)]}


def prompt(messages):
    return messages[1]["content"][0]["text"]


class CoverageTests(unittest.TestCase):
    def test_failed_late_chunk_and_missing_digest_keep_unrepresented_items(self):
        mapped = Client({"chunk": {"themes": [theme("early", 1, 60)]}},
                        {"chunk": {"themes": [theme("middle", 61, 120)]}}, RuntimeError("offline"))
        book = book_with(150)
        text = prompt(synthesis.messages_for(session(book, digest=mapped)))
        self.assertIn("i150 | Place 150", text)
        self.assertIn("Items (70):", text)
        self.assertEqual(len(mapped.calls), 3)
        self.assertIn("Items (150):", prompt(synthesis.messages_for(session(book))))

    def test_malformed_partial_and_foreign_theme_ids_do_not_claim_coverage(self):
        book = book_with(150)
        malformed = Client({"chunk": {"themes": [None, {"name": "x", "summary": "", "item_ids": ["i1"]},
                                                {"name": "wrong", "summary": "x", "item_ids": "i2"},
                                                {"name": "valid", "summary": "x",
                                                 "item_ids": ["i50", "i61", 5, "i50"]}]}},
                           {"chunk": {"themes": {"name": "wrong"}}},
                           {"chunk": {"themes": []}})
        text = prompt(synthesis.messages_for(session(book, digest=malformed)))
        self.assertIn("- valid: x [i50]", text)
        self.assertNotIn("- wrong:", text)
        self.assertIn("Items (149):", text)
        self.assertIn("i61 | Place 61", text)

    def test_valid_whole_chunk_ids_remain_visible_and_success_stays_small(self):
        book = book_with(150)
        mapped = Client(*({"chunk": {"themes": [theme("group", first, min(first + 59, 150))]}}
                          for first in (1, 61, 121)))
        text = prompt(synthesis.messages_for(session(book, digest=mapped)))
        self.assertIn("i120]", text)  # the end of a 60-ID list was previously cut at 300 characters
        self.assertIn("Items (40):", text)
        self.assertNotIn("i150 | Place 150", text)

    def test_theme_cap_does_not_treat_undisplayed_ids_as_covered(self):
        book = book_with(150)
        first_chunk = [theme(f"group {n}", n, n) for n in range(1, 8)]
        first_chunk.append(theme("most", 8, 59))
        first_chunk.append(theme("undisplayed", 60, 60))
        mapped = Client({"chunk": {"themes": first_chunk}},
                        {"chunk": {"themes": [theme("middle", 61, 120)]}},
                        {"chunk": {"themes": [theme("last", 121, 150)]}})
        text = prompt(synthesis.messages_for(session(book, digest=mapped)))
        self.assertIn("Items (41):", text)
        self.assertIn("i60 | Place 60", text)
        self.assertNotIn("- undisplayed:", text)

    def test_500_raw_items_fit_and_501_refuse_both_report_clients_with_a_gap(self):
        self.assertIn("Items (500):", prompt(synthesis.messages_for(session(book_with(500)))))
        primary, fallback = Client(), Client()
        long = session(book_with(501), synth=primary, fallback=fallback)
        report, _ = synthesis.synthesize(long)
        self.assertEqual((primary.calls, fallback.calls), ([], []))
        self.assertTrue(report["fallback"])
        self.assertIn("500-item synthesis limit", report["gaps"][0])
        self.assertEqual(len(long.notebook.kept_items()), 501)

    def test_generic_extracted_and_screen_details_reach_the_report_prompt_with_caps(self):
        book = Notebook("a" * 12, "Compare prices", "browse")
        extracted = book.add_item(Item(kind="row", source={"name": "One"}, text={"details": ["old price"]}))
        book.update_item(extracted.id, extract={"summary": "Listing", "details": ["$42", "4.8 stars"]})
        captured = book.add_item(Item(kind="row", source={"name": "Two"},
                                      text={"details": [f"detail {n}" for n in range(1, 10)]}))
        lines = prompt(synthesis.messages_for(session(book)))
        self.assertIn("details: $42; 4.8 stars", lines)
        self.assertIn("details: detail 1", lines)
        self.assertIn("detail 8", lines)
        self.assertNotIn("detail 9", lines)
        self.assertEqual(captured.extract, None)

    def test_reserve_covers_the_bounded_raw_fallback_plus_every_map_chunk(self):
        model = "claude-sonnet-5-5"
        for count in (121, 500, 501):
            with self.subTest(count=count):
                expected = (costs.SYNTH_BASE[model] + min(count, 500) * costs.SYNTH_PER_ITEM[model]
                            + math.ceil(count / synthesis.CHUNK) * costs.DIGEST_USD * 4)
                self.assertEqual(costs.synth_usd(model, count), round(expected, 5))
        self.assertGreater(costs.synth_usd(model, 500), costs.synth_usd(model, 120))


if __name__ == "__main__":
    unittest.main()
