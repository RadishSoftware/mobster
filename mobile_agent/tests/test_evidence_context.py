"""Semantic context survives extraction; observation waits stay inside run budgets.

Also pins the durable request-bound EffectLedger (precision-guard-audit:50).
"""

from dataclasses import replace
import unittest
from unittest.mock import Mock

from ..agent import Agent
from ..demo import DemoDriver, DemoModel, screen
from ..effect_ledger import EffectLedger
from ..extraction import Evidence, pack_extraction_context


class EvidenceContextTests(unittest.TestCase):
    def test_label_value_remain_grouped_with_role_and_position(self):
        current = screen()
        current.elements = [replace(current.elements[0], label='Page title', value='About', role='Heading')]
        evidence = Evidence()
        evidence.add(current, 3)
        label, value = evidence.public()['entries']
        self.assertEqual((label['field'], value['field']), ('label', 'value'))
        self.assertEqual(label['element_id'], value['element_id'])
        self.assertEqual(label['role'], 'Heading')
        self.assertEqual(label['rect'], list(current.elements[0].rect))
        self.assertEqual(value['text'], 'About')
        self.assertEqual(evidence.public()['latest_step'], 3)

    def test_reobservation_updates_recency_without_duplicating_evidence(self):
        current = screen()
        evidence = Evidence()
        evidence.add(current, 0)
        original = evidence.public()
        current.elements = [replace(current.elements[0], rect=(.1, .2, .3, .2))]
        evidence.add(current, 2)
        latest = evidence.public()
        self.assertEqual(latest['entries'][0]['id'], original['entries'][0]['id'])
        self.assertEqual(latest['entries'][0]['last_seen_step'], 2)
        self.assertEqual(original['entries'][0]['last_seen_step'], 0)
        self.assertEqual(latest['entries'][0]['rect'], [.1, .2, .3, .2])
        self.assertEqual(latest['entries'][1]['last_seen_step'], 0)

    def test_settle_window_is_distinct_from_bounded_response_deadline(self):
        for run_budget in (.2, 5):
            with self.subTest(run_budget=run_budget):
                driver = DemoDriver()
                driver.wait_for_change = Mock(side_effect=lambda *args, **kwargs: driver.observe())
                Agent(driver, DemoModel(), max_steps=1, max_seconds=run_budget,
                      settle_seconds=.6).run('Open Search', execute=True)
                arguments = driver.wait_for_change.call_args.kwargs
                self.assertEqual(arguments['wait_seconds'], .6)
                self.assertLessEqual(arguments['timeout'], run_budget)
                self.assertLessEqual(arguments['timeout'], 1.6)
                if run_budget > 1.6:
                    self.assertGreater(arguments['timeout'], .6)


class PackExtractionContextTests(unittest.TestCase):
    """Field-token packer keeps needed history and drops unrelated noise."""

    def entries(self):
        return [
            {"id": "e0", "text": "Model Name", "label_context": "Model Name", "field": "label",
             "element_id": "m", "bundle_id": "b", "source": "ax", "step": 0, "last_seen_step": 0, "role": "StaticText"},
            {"id": "e1", "text": "iPhone 16 Plus", "label_context": "Model Name", "field": "value",
             "element_id": "m", "bundle_id": "b", "source": "ax", "step": 0, "last_seen_step": 0, "role": "StaticText"},
            {"id": "e2", "text": "iOS Version", "label_context": "iOS Version", "field": "label",
             "element_id": "i", "bundle_id": "b", "source": "ax", "step": 0, "last_seen_step": 0, "role": "StaticText"},
            {"id": "e3", "text": "26.6.1", "label_context": "iOS Version", "field": "value",
             "element_id": "i", "bundle_id": "b", "source": "ax", "step": 0, "last_seen_step": 0, "role": "StaticText"},
            {"id": "e4", "text": "Unrelated Banner", "label_context": "", "field": "label",
             "element_id": "u", "bundle_id": "b", "source": "ax", "step": 0, "last_seen_step": 0, "role": "StaticText"},
        ]

    def test_field_token_packer_keeps_needed_history_entries(self):
        evidence = {"entries": self.entries(), "latest_step": 0}
        schema = {"type": "object", "properties": {"model_name": {"type": "string"}}}
        packed = pack_extraction_context(evidence, "What is the model name?", schema)
        kept = {entry["id"] for entry in packed["entries"]}
        self.assertIn("e0", kept)
        self.assertIn("e1", kept)
        self.assertIn("e2", kept)  # label/value pair travels with its group peer
        self.assertIn("e3", kept)
        self.assertNotIn("e4", kept)
        self.assertLess(len(packed["entries"]), len(evidence["entries"]))

    def test_packer_preserves_multi_screen_facts_for_requested_field(self):
        entries = self.entries()
        entries.append({"id": "e9", "text": "iPhone 16 Plus", "label_context": "Model Name",
                        "field": "value", "element_id": "m", "bundle_id": "b", "source": "ax",
                        "step": 0, "last_seen_step": 5, "role": "StaticText"})
        evidence = {"entries": entries, "latest_step": 5}
        packed = pack_extraction_context(evidence, "Return the model name",
                                         {"type": "object", "properties": {"model_name": {"type": "string"}}})
        kept = {entry["id"] for entry in packed["entries"]}
        self.assertIn("e9", kept)
        self.assertIn("e1", kept)


class EffectLedgerTests(unittest.TestCase):
    """Persistent side-effect actions are not dispatched twice for one logical effect."""

    def test_duplicate_add_refused(self):
        ledger = EffectLedger()
        self.assertFalse(ledger.would_duplicate("TAP", "Add"))
        ledger.record_intent("TAP", "Add")
        ledger.record_outcome("TAP", "Add", outcome="acknowledged")
        self.assertTrue(ledger.would_duplicate("TAP", "Add"))
        # A second intent never clears the acknowledged block.
        ledger.record_intent("TAP", "Add")
        self.assertTrue(ledger.would_duplicate("TAP", "Add"))

    def test_unknown_prior_outcome_blocks_retry(self):
        ledger = EffectLedger()
        ledger.record_intent("TAP", "Save")
        ledger.record_outcome("TAP", "Save", outcome="unknown")
        self.assertTrue(ledger.would_duplicate("TAP", "Save"))
        # In-flight (intent, outcome not yet known) is also ambiguous: never auto-retry.
        other = EffectLedger()
        other.record_intent("TAP", "Save")
        self.assertTrue(other.would_duplicate("TAP", "Save"))
        # Only a pre-dispatch refusal releases the slot.
        released = EffectLedger()
        released.record_intent("TAP", "Save")
        released.record_outcome("TAP", "Save", outcome="not_dispatched")
        self.assertFalse(released.would_duplicate("TAP", "Save"))

    def test_distinct_fields_allowed(self):
        ledger = EffectLedger()
        ledger.record_intent("TYPE", "field-a", "Sample")
        ledger.record_outcome("TYPE", "field-a", "Sample", outcome="acknowledged")
        self.assertTrue(ledger.would_duplicate("TYPE", "field-a", "Sample"))
        self.assertFalse(ledger.would_duplicate("TYPE", "field-b", "Sample"))
        ledger.record_intent("TYPE", "field-b", "Sample")
        ledger.record_outcome("TYPE", "field-b", "Sample", outcome="observed_change")
        self.assertFalse(ledger.would_duplicate("INCREMENT", "stepper", None))

    def test_type_same_text_refused(self):
        ledger = EffectLedger()
        ledger.record_intent("TYPE", "Name", "Sample")
        ledger.record_outcome("TYPE", "Name", "Sample", outcome="acknowledged")
        self.assertTrue(ledger.would_duplicate("TYPE", "Name", "Sample"))
        # A different append on the same field is a different logical effect.
        self.assertFalse(ledger.would_duplicate("TYPE", "Name", "Other"))
        # Incrementing the same stepper twice is also a duplicate under this key.
        ledger.record_intent("INCREMENT", "qty", None)
        ledger.record_outcome("INCREMENT", "qty", None, outcome="observed_change")
        self.assertTrue(ledger.would_duplicate("INCREMENT", "qty", None))

    def test_per_run_not_global(self):
        first, second = EffectLedger(), EffectLedger()
        first.record_intent("TAP", "Add")
        first.record_outcome("TAP", "Add", outcome="acknowledged")
        self.assertTrue(first.would_duplicate("TAP", "Add"))
        self.assertFalse(second.would_duplicate("TAP", "Add"))
        second.record_intent("TAP", "Add")
        self.assertTrue(second.would_duplicate("TAP", "Add"))
        self.assertFalse(EffectLedger().would_duplicate("TAP", "Add"))
        # Non-persistent operations never participate in duplicate blocking.
        self.assertFalse(first.would_duplicate("SWIPE_UP", "scroll"))
        self.assertFalse(first.would_duplicate("WAIT", None))
        first.record_intent("SWIPE_UP", "scroll")
        self.assertFalse(first.would_duplicate("SWIPE_UP", "scroll"))


if __name__ == '__main__':
    unittest.main()
