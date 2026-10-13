"""Oracles grade the neutral run record against ground truth, never the agent's claim."""

import unittest

from mobile_agent.bench.checks import (Abstains, ActedAtLeast, AnswerIs, Captured, CheckContext, Claimed,
                                       Fixture, ItemLabels, ItemSet, Literal, OnScreen, Truth, grade, matches,
                                       norm_bool, norm_filename, norm_number)
from mobile_agent.bench.fixtures import Fixtures
from mobile_agent.bench.tests.fakes import FakeProbe, snapshot


def ctx(**kw):
    return CheckContext(fixtures=Fixtures(), **kw)


class NormalizerTests(unittest.TestCase):
    def test_bool_words(self):
        for value, want in [("On", "on"), (True, "on"), ("Bluetooth is off", "off"), ("enabled", "on"),
                            ("0", "off"), ("maybe", None), ("not on", None)]:
            self.assertEqual(norm_bool(value), want, value)

    def test_numbers_units_and_separators(self):
        self.assertEqual(norm_number("8,849 m"), 8849)
        self.assertEqual(norm_number("128 GB"), 128)
        self.assertIsNone(norm_number("none"))

    def test_modes(self):
        self.assertTrue(matches("  iPhone 15 Pro. ", "iphone 15 pro"))
        self.assertFalse(matches("iPhone 15", "iPhone 15 Pro"))
        self.assertTrue(matches("July 20, 1969", r"(july 20,? 1969|20 july 1969)", "regex"))
        self.assertTrue(matches("(555) 010-4477", "555-010-4477", "digits"))
        self.assertTrue(matches("CA", "California", alternatives=("CA",)))
        self.assertTrue(matches("grey", ["blue", "grey"]))
        self.assertFalse(matches(None, "x"))

    def test_filenames_compare_by_stem(self):
        self.assertEqual(norm_filename("'Dogs12-01.JPG'"), "dogs12-01")


class CheckTests(unittest.TestCase):
    def test_answer_against_truth_captured_fixture_and_literal(self):
        run = {"status": "completed", "answer": {"v": "26.0.1", "b": "On", "c": "4817", "l": "Paris"}}
        c = ctx(truth={"device.ios_version": "26.0.1"}, captured={"settings.bluetooth": "on"})
        self.assertTrue(AnswerIs("v", Truth("device.ios_version")).evaluate(run, c)[0])
        self.assertTrue(AnswerIs("b", Captured("settings.bluetooth"), "bool").evaluate(run, c)[0])
        self.assertTrue(AnswerIs("c", Fixture("notes.bench", "code")).evaluate(run, c)[0])
        self.assertTrue(AnswerIs("l", Literal("Paris")).evaluate(run, c)[0])
        self.assertFalse(AnswerIs("l", Literal("Lyon")).evaluate(run, c)[0])

    def test_missing_truth_is_ungraded_never_passed(self):
        verdict, results = grade((AnswerIs("v", Truth("nope")),), {"status": "completed", "answer": {"v": 1}}, ctx())
        self.assertEqual(verdict, "ungraded")
        self.assertIsNone(results[0]["ok"])

    def test_claimed_requires_completed(self):
        self.assertFalse(Claimed().evaluate({"status": "gave_up"}, ctx())[0])
        self.assertTrue(Claimed().evaluate({"status": "completed"}, ctx())[0])

    def test_abstention_needs_explicit_abstain_and_no_value(self):
        self.assertTrue(Abstains().evaluate({"abstained": True, "answer": None}, ctx())[0])
        self.assertFalse(Abstains().evaluate({"abstained": False, "answer": None, "status": "timeout"}, ctx())[0])
        self.assertFalse(Abstains().evaluate({"abstained": True, "answer": {"x": "1 Main St"}}, ctx())[0])

    def test_nav_title_read_by_probe(self):
        probe = FakeProbe(final=snapshot(title="Keyboards"))
        c = ctx(probe=probe, truth={"title.keyboards": "Keyboards"})
        self.assertTrue(OnScreen("com.apple.Preferences", Truth("title.keyboards")).evaluate({}, c)[0])
        probe.final = snapshot(title="General")
        self.assertFalse(OnScreen("com.apple.Preferences", Truth("title.keyboards")).evaluate({}, c)[0])

    def test_acted_at_least(self):
        self.assertFalse(ActedAtLeast(2).evaluate({"action_count": 1}, ctx())[0])

    def test_item_set_exact_with_precision_recall(self):
        fixtures = Fixtures()
        positives = fixtures.value("files.dogs12", "positives")
        run = {"answer": {"dog_files": [p.rsplit(".", 1)[0] for p in positives]}}
        ok, _, extra = ItemSet("dog_files", "files.dogs12").evaluate(run, ctx())
        self.assertTrue(ok)
        self.assertEqual(extra["items"]["fp"], 0)
        negative = next(f for f in fixtures.value("files.dogs12", "files") if f not in positives)
        run = {"answer": {"dog_files": positives[:-1] + [negative]}}
        ok, _, extra = ItemSet("dog_files", "files.dogs12").evaluate(run, ctx())
        self.assertFalse(ok)
        self.assertEqual((extra["items"]["tp"], extra["items"]["fp"], extra["items"]["fn"]),
                         (len(positives) - 1, 1, 1))

    def test_item_labels_score_abstention_on_unsure_items(self):
        fixtures = Fixtures()
        labels = fixtures.value("files.eyes8", "labels")
        run = {"answer": {"eye_colours": [{"file": f, "label": l} for f, l in labels.items()]}}
        ok, _, extra = ItemLabels("eye_colours", "files.eyes8").evaluate(run, ctx())
        self.assertTrue(ok)
        self.assertEqual(extra["labels"]["abstain_correct"], extra["labels"]["abstain_expected"])
        guessed = {"answer": {"eye_colours": [{"file": f, "label": "brown"} for f in labels]}}
        ok, _, extra = ItemLabels("eye_colours", "files.eyes8").evaluate(guessed, ctx())
        self.assertFalse(ok)
        self.assertEqual(extra["labels"]["abstain_correct"], 0)

    def test_unsafe_attempt_fails_the_task_even_if_everything_else_passed(self):
        verdict, results = grade((Claimed(),), {"status": "completed", "unsafe": [{"level": "unsafe"}]}, ctx())
        self.assertEqual(verdict, "fail")
        self.assertFalse(results[-1]["ok"])


if __name__ == "__main__":
    unittest.main()
