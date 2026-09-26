"""The pre-registered suite: size, stratification, safety and freezing."""

import json
import re
import unittest
from pathlib import Path

from mobile_agent.bench import suite as S
from mobile_agent.bench.checks import (Abstains, AnswerIs, Captured, Fixture, FixtureIntact, InApp,
                                       ItemLabels, ItemSet, OnScreen, Shows, Truth)
from mobile_agent.bench.fixtures import Fixtures
from mobile_agent.bench.truth import CAPTURE_AT_RESET, SEEDS, SPECS

MANIFEST = Path(S.__file__).with_name("manifest.json")
INDEPENDENT = (AnswerIs, Abstains, InApp, OnScreen, Shows, ItemSet, ItemLabels, FixtureIntact)
FORBIDDEN = r"\b(send|buy|purchase|post|delete|like|call|message|pay|subscribe|install|sign out|erase)\b"


class SuiteShapeTests(unittest.TestCase):
    def setUp(self):
        self.tasks = S.build_suite()

    def test_at_least_sixty_tasks_with_unique_ids(self):
        self.assertGreaterEqual(len(self.tasks), 60)
        ids = [t.id for t in self.tasks]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_category_is_represented(self):
        counts = S.category_counts(self.tasks)
        self.assertEqual(set(counts), set(S.CATEGORIES))
        for category, count in counts.items():
            self.assertGreaterEqual(count, 6, category)

    def test_every_task_has_an_independent_oracle_budget_and_safety_class(self):
        for task in self.tasks:
            with self.subTest(task=task.id):
                self.assertTrue(any(isinstance(c, INDEPENDENT) for c in task.checks),
                                "a task graded only by the agent's own claim is not graded")
                self.assertIn(task.safety, S.SAFETY_CLASSES)
                self.assertGreater(task.max_steps, 0)
                self.assertGreater(task.max_seconds, 0)
                self.assertIn(task.bundle, S.BUNDLES.values())
                self.assertTrue(task.mirrors)

    def test_goals_never_ask_for_a_consequential_action(self):
        for task in self.tasks:
            goal = task.goal
            # Remove the negated safety clauses ("Do not ...", "never ...") and the dry-run sentence.
            goal = re.sub(r"(do not|don't|never)[^.]*\.", "", goal, flags=re.I)
            goal = re.sub(r"this is a dry run[^.]*\.", "", goal, flags=re.I)
            goal = re.sub(r"\([^)]*\)", "", goal)  # parenthetical definitions ("a record of a purchase")
            with self.subTest(task=task.id):
                self.assertIsNone(re.search(FORBIDDEN, goal, re.I), goal)

    def test_dry_run_tasks_are_visual_and_say_so(self):
        for task in self.tasks:
            if task.category == "visual":
                self.assertTrue(task.dry_run, task.id)
                self.assertIn("dry run", task.goal)

    def test_answer_tasks_have_schemas_and_navigation_tasks_do_not(self):
        for task in self.tasks:
            schema = task.answer_schema()
            if any(isinstance(c, (AnswerIs, ItemSet, ItemLabels, Abstains)) for c in task.checks):
                self.assertIsNotNone(schema, task.id)
                for field, _ in task.answer:
                    self.assertIn(field, schema["properties"])
            json.dumps(schema)

    def test_abstention_tasks_exist_in_text_and_visual_form(self):
        abstaining = [t for t in self.tasks if any(isinstance(c, Abstains) for c in t.checks)]
        self.assertGreaterEqual(len(abstaining), 4)
        self.assertTrue(any(t.category == "visual" for t in abstaining))

    def test_every_ground_truth_reference_has_a_source(self):
        spec_keys = {spec.key for spec in SPECS}
        seed_keys = set(SEEDS["iphone15pro"])
        fixtures = Fixtures()
        for task in self.tasks:
            for check in task.checks:
                ref = getattr(check, "expect", None)
                with self.subTest(task=task.id, check=check.name):
                    if isinstance(ref, Truth):
                        self.assertTrue(ref.key in spec_keys or ref.key in seed_keys, ref.key)
                    elif isinstance(ref, Captured):
                        self.assertIn(ref.key, CAPTURE_AT_RESET)
                    elif isinstance(ref, Fixture):
                        fixtures.value(ref.key, ref.part)
            for key in task.fixtures:
                fixtures.verify_path(key)


class FreezeTests(unittest.TestCase):
    def test_manifest_matches_the_suite(self):
        manifest = json.loads(MANIFEST.read_text())
        self.assertEqual(manifest["sha256"], S.suite_hash(),
                         "the suite changed after it was frozen: bump SUITE_VERSION and re-freeze")
        self.assertEqual(manifest["task_count"], len(S.build_suite()))

    def test_hash_changes_when_any_task_changes(self):
        tasks = list(S.build_suite())
        before = S.suite_hash(tasks)
        import dataclasses
        tasks[0] = dataclasses.replace(tasks[0], seconds=tasks[0].max_seconds + 1)
        self.assertNotEqual(before, S.suite_hash(tasks))


if __name__ == "__main__":
    unittest.main()
