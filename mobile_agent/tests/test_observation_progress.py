"""Offline stability and cross-screen memory regressions; no provider/device calls."""

import json
import unittest


from mobile_agent.demo import screen
from mobile_agent.extraction import Evidence
from mobile_agent.state import Element, Snapshot
from mobile_agent.tests.test_task_policy import fake_jev


def value_screen(value, label="Balance", identifier="private-native-id"):
    return Snapshot([Element(identifier, label, "StaticText", (.1, .1, .8, .1),
                             value=value, actions=())], label + " " + value,
                    400, 800, "synthetic_fixture", bundle_id="test.app")


class DecisionMemoryTests(unittest.TestCase):
    def test_unchanged_current_facts_are_not_duplicated_in_memory(self):
        evidence, snapshot = Evidence(), value_screen("100")
        evidence.add(snapshot, 0)
        self.assertEqual(evidence.decision_context(snapshot), {"entries": [], "truncated": False})

    def test_changed_value_retains_label_context_and_is_marked_superseded(self):
        evidence = Evidence()
        evidence.add(value_screen("100"), 0)
        current = value_screen("20")
        evidence.add(current, 1)
        context = evidence.decision_context(current)
        self.assertEqual(len(context["entries"]), 1)
        fact = context["entries"][0]
        self.assertEqual((fact["text"], fact["label_context"], fact["field"]), ("100", "Balance", "value"))
        self.assertTrue(fact["superseded"])
        self.assertEqual(fact["last_seen_step"], 0)
        self.assertNotIn("private-native-id", json.dumps(context))
        self.assertNotIn("rect", fact)

    def test_two_rows_keep_values_with_their_own_labels_and_groups(self):
        evidence = Evidence()
        evidence.add(value_screen("100", "Balance", "balance"), 0)
        evidence.add(value_screen("20", "Limit", "limit"), 1)
        facts = evidence.decision_context(screen())["entries"]
        values = [fact for fact in facts if fact["field"] == "value"]
        self.assertEqual({(fact["label_context"], fact["text"]) for fact in values}, {("Balance", "100"), ("Limit", "20")})
        self.assertEqual(len({fact["group"] for fact in values}), 2)
        self.assertTrue(all(not fact["superseded"] for fact in values))

    def test_reused_element_with_new_label_does_not_relabel_old_value(self):
        evidence = Evidence()
        evidence.add(value_screen("100", "Balance"), 0)
        current = value_screen("100", "Limit")
        evidence.add(current, 1)
        prior = next(fact for fact in evidence.decision_context(current)["entries"] if fact["field"] == "value")
        self.assertEqual(prior["label_context"], "Balance")
        self.assertTrue(prior["superseded"])

    def test_memory_is_bounded_and_signals_omission_including_unicode(self):
        evidence = Evidence()
        for step in range(100):
            evidence.add(value_screen("京" * 400, f"Row {step}", f"node-{step}"), step)
        context = evidence.decision_context(screen())
        self.assertLessEqual(len(context["entries"]), 64)
        self.assertLess(len(json.dumps(context).encode()), 18000)
        self.assertTrue(context["truncated"])
        model = fake_jev()
        model.decide(screen(), "Read the collected values", [], evidence_history=context)

    def test_history_is_context_only_and_never_an_action_target(self):
        evidence = Evidence()
        evidence.add(value_screen("100", "Delete everything", "retired-target"), 0)
        model = fake_jev()
        model.decide(screen(), "Read the balance", [], evidence_history=evidence.decision_context(screen()))
        body = model.http.request.call_args.args[2]
        self.assertIn("observation_history", body["state"])
        self.assertNotIn("retired-target", json.dumps(body))
        self.assertNotIn("Delete everything", json.dumps(body["questions"]["tap_target"]))

    def test_empty_history_does_not_enlarge_the_provider_payload(self):
        model = fake_jev()
        model.decide(screen(), "Open Search", [], evidence_history={"entries": [], "truncated": False})
        self.assertNotIn("observation_history", model.http.request.call_args.args[2]["state"])


if __name__ == "__main__":
    unittest.main()
