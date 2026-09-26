"""Calibration math. Offline."""

import unittest

from mobile_agent.evals.calibrate import answer_label, auroc, candidate_label, expected_digests, threshold_table


class CalibrateTests(unittest.TestCase):
    def test_auroc(self):
        self.assertEqual(auroc([(.9, True), (.1, False)]), 1.0)
        self.assertEqual(auroc([(.1, True), (.9, False)]), 0.0)
        self.assertEqual(auroc([(.5, True), (.5, False)]), .5)
        self.assertIsNone(auroc([(.5, True)]))

    def test_threshold_table(self):
        rows = threshold_table([(.9, True), (.6, False), (.2, True)], thresholds=(.5,))
        self.assertEqual(rows, [(.5, 2, .5, .5)])

    def test_answer_label_uses_only_answer_oracles(self):
        record = {"oracles": [{"oracle": "ReturnedValue", "ok": True}, {"oracle": "StatusIn", "ok": False}]}
        self.assertIs(answer_label(record), True)
        self.assertIsNone(answer_label({"oracles": [{"oracle": "StatusIn", "ok": True}]}))


class CandidateLabelTests(unittest.TestCase):
    def test_candidates_are_labelled_by_ground_truth_digest(self):
        from mobile_agent.agent import answer_digest
        digests = expected_digests("iphone15pro")
        right = {"candidate_sha": answer_digest({"birth_year": "1956"})}
        wrong = {"candidate_sha": answer_digest({"birth_year": "1957"})}
        record = {"task": "web.type_url"}
        self.assertIs(candidate_label(record, right, digests), True)
        self.assertIs(candidate_label(record, wrong, digests), False)
        self.assertIsNone(candidate_label({"task": "web.read.scroll"}, right, digests))
