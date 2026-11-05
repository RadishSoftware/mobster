"""Semantic context survives extraction; the request-bound EffectLedger never dispatches one effect twice."""

import unittest

from ..effect_ledger import EffectLedger
from ..state import Element, Snapshot


def screen():
    return Snapshot([Element("0", "Search", "Button", (.8, .06, .1, .05)),
                     Element("1", "Profile", "Button", (.8, .92, .15, .05))],
                    "For You\nSearch\nProfile", 402, 874, "synthetic_fixture")


class EffectLedgerTests(unittest.TestCase):
    def test_duplicate_add_refused(self):
        ledger = EffectLedger()
        self.assertFalse(ledger.would_duplicate("TAP", "Add"))
        ledger.record_outcome("TAP", "Add", outcome="acknowledged")
        self.assertTrue(ledger.would_duplicate("TAP", "Add"))

    def test_unknown_prior_outcome_blocks_retry(self):
        ledger = EffectLedger()
        ledger.record_outcome("TAP", "Save", outcome="unknown")
        self.assertTrue(ledger.would_duplicate("TAP", "Save"))

    def test_only_a_pre_dispatch_refusal_or_a_settled_look_releases_the_slot(self):
        for outcome in ("not_dispatched", "failed_pre_dispatch", "observed_absent", "resolved"):
            with self.subTest(outcome=outcome):
                ledger = EffectLedger()
                ledger.record_outcome("TAP", "Save", outcome=outcome)
                self.assertFalse(ledger.would_duplicate("TAP", "Save"))
        for outcome in ("acknowledged", "observed_change", "observed_unchanged", "unknown", "unproven"):
            with self.subTest(outcome=outcome):
                ledger = EffectLedger()
                ledger.record_outcome("TAP", "Save", outcome=outcome)
                self.assertTrue(ledger.would_duplicate("TAP", "Save"))

    def test_a_later_refusal_never_releases_an_attempted_effect(self):
        for prior in ("acknowledged", "unknown", "unproven", "observed_change", "observed_unchanged"):
            for refused in ("not_dispatched", "failed_pre_dispatch"):
                with self.subTest(prior=prior, refused=refused):
                    ledger = EffectLedger()
                    ledger.record_outcome("TAP", "Save", outcome=prior)
                    ledger.record_outcome("TAP", "Save", outcome=refused)
                    self.assertTrue(ledger.would_duplicate("TAP", "Save"))

    def test_a_pre_dispatch_refusal_still_releases_a_slot_nothing_was_sent_through(self):
        ledger = EffectLedger()
        for refused in ("not_dispatched", "failed_pre_dispatch"):
            ledger.record_outcome("TAP", "Save", outcome=refused)
            self.assertFalse(ledger.would_duplicate("TAP", "Save"))
        ledger.record_outcome("TAP", "Save", outcome="unknown")
        self.assertTrue(ledger.would_duplicate("TAP", "Save"))

    def test_distinct_fields_allowed(self):
        ledger = EffectLedger()
        ledger.record_outcome("TYPE", "field-a", "Sample", outcome="acknowledged")
        self.assertTrue(ledger.would_duplicate("TYPE", "field-a", "Sample"))
        self.assertFalse(ledger.would_duplicate("TYPE", "field-b", "Sample"))
        ledger.record_outcome("TYPE", "field-b", "Sample", outcome="observed_change")
        self.assertFalse(ledger.would_duplicate("INCREMENT", "stepper", None))

    def test_unproven_visual_outcome_blocks_retry(self):
        ledger = EffectLedger()
        ledger.record_outcome("TAP", "Save", outcome="unproven")
        self.assertTrue(ledger.would_duplicate("TAP", "Save"))
        self.assertFalse(ledger.would_duplicate("TAP", "Cancel"))

    def test_unfamiliar_outcome_never_releases_an_effect(self):
        ledger = EffectLedger()
        ledger.record_outcome("TAP", "Save", outcome="new_driver_outcome")
        self.assertTrue(ledger.would_duplicate("TAP", "Save"))
        self.assertEqual(ledger.held(lambda _key, _record: True), [("TAP", "Save", None)])
        ledger.record_outcome("TAP", "Save", outcome="new_driver_outcome")
        self.assertTrue(ledger.would_duplicate("TAP", "Save"))

    def test_type_same_text_refused(self):
        ledger = EffectLedger()
        ledger.record_outcome("TYPE", "Name", "Sample", outcome="acknowledged")
        self.assertTrue(ledger.would_duplicate("TYPE", "Name", "Sample"))
        self.assertFalse(ledger.would_duplicate("TYPE", "Name", "Other"))
        ledger.record_outcome("INCREMENT", "qty", None, outcome="observed_change")
        self.assertTrue(ledger.would_duplicate("INCREMENT", "qty", None))

    def test_an_explicit_effect_key_blocks_a_duplicate_within_a_run(self):
        effects = EffectLedger()
        effects.record_outcome("TAP", "like", outcome="unknown", effect=("LOOP", "x", 0, "like"))
        self.assertTrue(effects.would_duplicate("TAP", "like", effect=("LOOP", "x", 0, "like")))

    def test_per_run_not_global(self):
        first, second = EffectLedger(), EffectLedger()
        first.record_outcome("TAP", "Add", outcome="acknowledged")
        self.assertTrue(first.would_duplicate("TAP", "Add"))
        self.assertFalse(second.would_duplicate("TAP", "Add"))
        self.assertFalse(EffectLedger().would_duplicate("TAP", "Add"))
        self.assertFalse(first.would_duplicate("SWIPE_UP", "scroll"))
        self.assertFalse(first.would_duplicate("WAIT", None))
        first.record_outcome("SWIPE_UP", "scroll", outcome="acknowledged")
        self.assertFalse(first.would_duplicate("SWIPE_UP", "scroll"))


if __name__ == '__main__':
    unittest.main()
