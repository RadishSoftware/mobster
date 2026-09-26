"""Offline precision-boundary tests; no provider or device calls."""
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from mobile_agent.agent import Agent, same_observed_element
from mobile_agent.models import Decision, Jev
from mobile_agent.task_policy import (ACTION_CONFIDENCE_FLOOR, ACTION_SUPPORT_INSTRUCTIONS, ActionSupport,
                                      RiskTier, StopGate)
from mobile_agent.state import Element, Snapshot

# Synthetic fixtures, also used by a paid evaluation that is not shipped.
TEMPORAL = {"source": "host_request_clock_not_verified_device_clock",
            "request_time": "2026-09-19T17:37:00-07:00", "timezone_name": "PDT"}


def form(values, *, commit="Done", text="Edit item"):
    elements = [Element(str(index), label, role, (.1, .1 + index * .1, .6, .08),
                        role == "TextField", value=value,
                        actions=("TYPE",) if role == "TextField" else ("INCREMENT", "DECREMENT"))
                for index, (label, value, role) in enumerate(values)]
    elements.append(Element("commit", commit, "Button", (.8, .02, .15, .05)))
    return Snapshot(elements, text, 430, 932, "synthetic_fixture")


def clock_form(hour="12", minute="00", period="PM"):
    return form([("", hour + " o’clock", "UIAccessibilityPickerComponent"),
                 ("", minute + " minutes", "UIAccessibilityPickerComponent"),
                 ("", period, "UIAccessibilityPickerComponent")], text="Add Alarm\nRepeat: Never")


def decision(operation="TAP", target="commit"):
    return Decision(operation, target, .3, .04, .1, "offline-precision", 0, {}, StopGate.CONTINUE)


def response(support="allowed", **changes):
    return {"model": "jev-test", "answers": {"action_support": {
        "type": "choice", "choice": support, "confidence": 1,
        "probabilities": {value.value: int(value.value == support) for value in ActionSupport}, **changes}}}


class ActionVerificationContractTests(unittest.TestCase):
    def test_host_clock_is_not_authority_for_cross_midnight_device_dates(self):
        self.assertIn("NOT authority for the device", ACTION_SUPPORT_INSTRUCTIONS)
        self.assertIn("host/device midnight", ACTION_SUPPORT_INSTRUCTIONS)
        self.assertIn("observed device date/time context", ACTION_SUPPORT_INSTRUCTIONS)

    def model(self, answer=None):
        events = []
        model = Jev("offline-only", on_inference=events.append)
        model.http.request = Mock(return_value=answer if answer is not None else response())
        return model, events

    def test_exact_target_values_and_temporal_source_are_bound_in_separate_request(self):
        model, events = self.model()
        snapshot = clock_form(minute="37")
        self.assertIs(model.verify_action(snapshot, "Set noon", "TAP", snapshot.elements[-1], [],
                                         temporal_context=TEMPORAL, timeout=3), ActionSupport.ALLOWED)
        method, route, body, timeout = model.http.request.call_args.args
        self.assertEqual((method, route, timeout), ("POST", "/systemone", 3))
        state = body["state"]
        self.assertEqual(state["original_request"], "Set noon")
        self.assertEqual(state["proposed_action"], {"operation": "TAP", "target": "3", "text": None})
        self.assertEqual(state["current_screen"]["elements"][1]["value"], "37 minutes")
        self.assertEqual(state["temporal_context"], TEMPORAL)
        self.assertEqual(set(body["questions"]), {"action_support"})
        self.assertEqual(events[-1]["purpose"], "action_verification")
        self.assertNotIn("37 minutes", str(events))

    def test_type_guard_has_actual_append_and_existing_value(self):
        model, _ = self.model()
        snapshot = form([("Name", "Sam", "TextField")])
        model.verify_action(snapshot, "Name it Sample", "TYPE", snapshot.elements[0], [], text="ple")
        state = model.http.request.call_args.args[2]["state"]
        self.assertEqual(state["proposed_action"]["text"], "ple")
        self.assertEqual(state["current_screen"]["elements"][0]["value"], "Sam")

    def test_known_duplicate_append_is_rejected_without_provider_opinion(self):
        model, events = self.model()
        snapshot = form([("Name", "Sample", "TextField")])
        self.assertIs(model.verify_action(snapshot, "Name it Sample", "TYPE", snapshot.elements[0], [], text="Sample"),
                      ActionSupport.MISMATCH)
        model.http.request.assert_not_called()
        self.assertEqual(events, [])

    def test_unknown_or_unobserved_prior_dispatch_abstains_without_provider(self):
        for outcome in ("unknown", "acknowledged"):
            model, events = self.model()
            snapshot = clock_form()
            history = [{"operation": "TAP", "label": "Done", "outcome": outcome, "changed": None}]
            self.assertIs(model.verify_action(snapshot, "Set noon", "TAP", snapshot.elements[-1], history),
                          ActionSupport.UNCLEAR)
            model.http.request.assert_not_called()
            self.assertEqual(events, [])

    def test_history_is_bounded_and_omission_is_explicit(self):
        model, _ = self.model()
        snapshot = clock_form()
        model.verify_action(snapshot, "Set noon", "TAP", snapshot.elements[-1],
                            [{"operation": "TAP", "label": str(index)} for index in range(12)])
        state = model.http.request.call_args.args[2]["state"]
        self.assertTrue(state["history_truncated"])
        self.assertEqual([row["label"] for row in state["recent_actions"]], list(map(str, range(4, 12))))

    def test_missing_malformed_nonfinite_or_tied_verdict_never_allows(self):
        answers = [{}, {"answers": {}}, response(type="noul"), response(choice="invented"),
                   response(confidence=True), response(confidence=float("nan")),
                   response(probabilities={"allowed": 1}),
                   response(probabilities={"allowed": .5, "mismatch": .5, "unclear": 0}),
                   response(probabilities={"allowed": .5, "mismatch": 0, "unclear": .5})]
        for answer in answers:
            with self.subTest(answer=answer):
                model, _ = self.model(answer)
                snapshot = clock_form()
                self.assertIs(model.verify_action(snapshot, "Set noon", "TAP", snapshot.elements[-1], []),
                              ActionSupport.UNCLEAR)

    def test_bad_operation_target_text_or_clock_fails_before_provider(self):
        model, _ = self.model()
        snapshot = clock_form()
        attempts = [dict(operation="INCREMENT"), dict(target=replace(snapshot.elements[-1], label="Other")),
                    dict(text="unexpected"), dict(temporal_context={**TEMPORAL, "request_time": "2026-09-19T12:00:00"})]
        for params in attempts:
            options = dict(snapshot=snapshot, goal="Set noon", operation="TAP", target=snapshot.elements[-1], history=[])
            options.update(params)
            with self.subTest(params=params), self.assertRaises(ValueError):
                model.verify_action(**options)
        model.http.request.assert_not_called()

    def test_unlabeled_adjustable_criteria_keep_distinct_values_roles_and_actions(self):
        model, _ = self.model()
        def reply(_method, _route, body, _timeout):
            choices = {"operation": "INCREMENT", "increment_target": "1", "decrement_target": "1",
                       "tap_target": "3", "stop_gate": "continue"}
            answers = {}
            for name, question in body["questions"].items():
                selected = choices.get(name)
                answers[name] = ({"type": "noul", "noul": .1} if question["type"] == "noul" else
                    {"type": "choice", "choice": selected, "confidence": 1,
                     "probabilities": {key: int(key == selected) for key in question["criteria"]}})
            return {"model": "test", "answers": answers}
        model.http.request.side_effect = reply
        chosen = model.decide(clock_form(minute="37"), "Set noon", [])
        self.assertEqual(chosen.target, "1")
        criteria = model.http.request.call_args.args[2]["questions"]["increment_target"]["criteria"]
        self.assertIn("none", criteria)
        element_values = [json.loads(value) for key, value in criteria.items() if key != "none"]
        self.assertEqual([value["value"] for value in element_values], ["12 o’clock", "37 minutes", "PM"])
        self.assertTrue(all(value["actions"] == ["INCREMENT", "DECREMENT"] for value in element_values))

    def test_verify_action_confidence_below_tier_floor_is_unclear_not_allowed(self):
        floor = ACTION_CONFIDENCE_FLOOR[RiskTier.SIDE_EFFECT]
        model, _ = self.model(response("allowed", confidence=floor - .05))
        snapshot = form([("Recipient", "demo@example.org", "TextField")], commit="Send")
        self.assertIs(model.verify_action(snapshot, "Send the message Hello", "TAP",
                                          snapshot.elements[-1], []), ActionSupport.UNCLEAR)
        model, _ = self.model(response("allowed", confidence=floor))
        self.assertIs(model.verify_action(snapshot, "Send the message Hello", "TAP",
                                          snapshot.elements[-1], []), ActionSupport.ALLOWED)

    def test_verify_action_compacts_empty_element_fields_but_keeps_bound_values(self):
        model, _ = self.model()
        snapshot = clock_form(minute="37")
        model.verify_action(snapshot, "Set noon", "TAP", snapshot.elements[-1], [])
        screen = model.http.request.call_args.args[2]["state"]["current_screen"]
        self.assertEqual(screen["elements"][1]["value"], "37 minutes")
        self.assertNotIn("value", screen["elements"][-1])  # commit button has empty value
        self.assertNotIn("visibility_verified", screen)
        self.assertEqual(model.http.request.call_args.args[2]["state"]["proposed_action"],
                         {"operation": "TAP", "target": "3", "text": None})


class AgentActionVerificationTests(unittest.TestCase):
    def ready(self, snapshot=None):
        snapshot = snapshot or clock_form(minute="37")
        current = [snapshot]
        driver = SimpleNamespace(can_type=True, observe=Mock(side_effect=lambda **_: current[0]), execute=Mock())
        model = Mock()
        model.decide.return_value = decision()
        model.verify_action.return_value = ActionSupport.ALLOWED
        return driver, model, current

    def test_rejected_1237_commit_stays_rejected_even_if_next_verdict_would_allow(self):
        driver, model, _ = self.ready()
        model.verify_action.side_effect = [ActionSupport.MISMATCH, ActionSupport.ALLOWED]
        result = Agent(driver, model, max_steps=3, settle_seconds=0).run("Set exactly 12pm", execute=True)
        self.assertEqual(result["status"], "blocked")
        driver.execute.assert_not_called()
        model.verify_action.assert_called_once()
        self.assertEqual(result["attempted_actions"], 0)

    def test_correct_noon_commit_positive_is_dispatched_once(self):
        driver, model, _ = self.ready(clock_form())
        result = Agent(driver, model, max_steps=1, settle_seconds=0).run("Set exactly noon", execute=True)
        driver.execute.assert_called_once()
        self.assertEqual(result["attempted_actions"], 1)
        self.assertEqual(result["status"], "max_steps")  # Not claiming success from admission alone.

    def test_mismatch_can_adjust_a_component_without_saving_approximation(self):
        driver, model, _ = self.ready()
        model.decide.side_effect = [decision(), decision("DECREMENT", "1")]
        model.verify_action.return_value = ActionSupport.MISMATCH
        result = Agent(driver, model, max_steps=2, settle_seconds=0).run("Set exactly noon", execute=True)
        self.assertEqual(result["status"], "max_steps")
        self.assertEqual(driver.execute.call_args.args[0], "DECREMENT")
        driver.execute.assert_called_once()
        model.verify_action.assert_called_once()

    def test_unclear_or_untyped_verdict_stops_without_dispatch(self):
        for verdict in (ActionSupport.UNCLEAR, "allowed", None):
            driver, model, _ = self.ready()
            model.verify_action.return_value = verdict
            result = Agent(driver, model, settle_seconds=0).run("Set noon", execute=True)
            self.assertEqual(result["status"], "needs_clarification")
            self.assertIn("earlier actions may have completed", result["reason"])
            driver.execute.assert_not_called()

    def test_revision_value_or_identity_change_during_guard_discards_action(self):
        for mutation in (lambda s: replace(s, revision="new"),
                         lambda s: clock_form(minute="00"),
                         lambda s: replace(s, bundle_id="other.app")):
            driver, model, current = self.ready()
            def verify(*_args, **_kwargs):
                current[0] = mutation(current[0])
                return ActionSupport.ALLOWED
            model.verify_action.side_effect = verify
            result = Agent(driver, model, max_steps=1).run("Set noon", execute=True)
            self.assertEqual(result["status"], "max_steps")
            driver.execute.assert_not_called()

    def test_cancel_or_deadline_after_verifier_cannot_dispatch(self):
        driver, model, _ = self.ready()
        cancelled = [False]
        def verify(*_args, **_kwargs):
            cancelled[0] = True
            return ActionSupport.ALLOWED
        model.verify_action.side_effect = verify
        result = Agent(driver, model, cancelled=lambda: cancelled[0]).run("Set noon", execute=True)
        self.assertEqual(result["status"], "stopped")
        driver.execute.assert_not_called()
        driver, model, _ = self.ready()
        clock = [0]
        def late(*_args, **_kwargs):
            clock[0] = 2
            return ActionSupport.ALLOWED
        model.verify_action.side_effect = late
        with patch("mobile_agent.agent.time.monotonic", side_effect=lambda: clock[0]):
            result = Agent(driver, model, max_seconds=1).run("Set noon", execute=True)
        self.assertEqual(result["status"], "timeout")
        driver.execute.assert_not_called()

    def test_type_verification_uses_actual_helper_text(self):
        driver, model, current = self.ready(form([("Name", "Sam", "TextField")]))
        model.decide.return_value = decision("TYPE", "0")
        helper = SimpleNamespace(calls=0, ask=Mock(return_value="ple"))
        Agent(driver, model, helper, max_steps=1, settle_seconds=0).run("Name it Sample", execute=True)
        self.assertEqual(model.verify_action.call_args.kwargs["text"], "ple")
        self.assertEqual(driver.execute.call_args.kwargs["text"], "ple")

    def test_coupled_picker_rollover_is_recorded_with_stable_request_time(self):
        driver, model, current = self.ready(clock_form(hour="11", period="PM"))
        model.decide.side_effect = [decision("INCREMENT", "0"), decision("INCREMENT", "2")]
        def execute(*_args, **_kwargs):
            current[0] = clock_form(hour="12", period="AM")
        driver.execute.side_effect = execute
        Agent(driver, model, max_steps=2, settle_seconds=0).run("Set noon", execute=True)
        history = model.decide.call_args_list[1].args[2]
        self.assertEqual(history[0]["target_value_before"], "11 o’clock")
        self.assertEqual(history[0]["target_value_after"], "12 o’clock")
        self.assertIn({"label": "", "role": "UIAccessibilityPickerComponent", "before": "PM", "after": "AM"}, history[0]["field_changes"])
        first, second = [call.kwargs["temporal_context"] for call in model.decide.call_args_list]
        self.assertEqual(first, second)
        self.assertEqual(first["source"], TEMPORAL["source"])
        model.verify_action.assert_not_called()  # Native adjustable hot path remains one decision.

    def test_recycled_ids_do_not_invent_value_transitions(self):
        before = form([("Recipient", "demo@example.org", "TextField")])
        for after in (form([("Amount", "100", "TextField")]),
                      replace(before, bundle_id="another.app"),
                      replace(before, source="ocr")):
            self.assertIsNone(same_observed_element(before, after, before.elements[0]))
        ocr = replace(before, source="ocr")
        self.assertIsNone(same_observed_element(ocr, ocr, ocr.elements[0]))


if __name__ == "__main__":
    unittest.main()
