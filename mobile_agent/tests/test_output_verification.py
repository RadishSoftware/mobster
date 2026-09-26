"""Answer acceptance is separate from literal copying; no real provider or device calls."""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from mobile_agent.agent import Agent
from mobile_agent.demo import DemoDriver, DemoHelper, DemoModel, screen
from mobile_agent.extraction import Evidence, InsufficientEvidence, bounded_json
from mobile_agent.models import Jev
from mobile_agent.task_policy import OutputIntent, OutputSupport, StopGate


TITLE = "Coffee brewing guide"
STRING = {"type": "string"}
CANDIDATE = {"data": TITLE, "citations": [{"path": "", "evidence_id": "e0", "quote": TITLE}]}


def observed_evidence():
    evidence = Evidence()
    evidence.add(screen("results"), 1)
    return evidence.public()


def response(choice="supported", **changes):
    answer = {"type": "choice", "choice": choice, "confidence": 1,
              "probabilities": {item.value: int(item.value == choice) for item in OutputSupport}, **changes}
    return {"model": "jev-test", "answers": {"output_support": answer},
            "usage": {"input_tokens": 100, "output_tokens": 10}}


CONDITIONAL = "Read the actual title unless a sign-in wall appears"


class OutputVerificationContractTests(unittest.TestCase):
    def model(self, reply=None):
        events = []
        model = Jev("offline-only", on_inference=events.append)
        model.http.request = Mock(return_value=reply if reply is not None else response())
        return model, events

    def test_one_overall_choice_contains_original_request_candidate_and_full_context(self):
        model, events = self.model()
        observed = observed_evidence()
        observed["truncated"] = True
        current = screen("results").public()
        self.assertIs(model.verify_output("Read the actual title", CANDIDATE, observed, timeout=3,
                                         current_screen=current), OutputSupport.SUPPORTED)
        model.http.request.assert_called_once()
        method, path, body, timeout = model.http.request.call_args.args
        self.assertEqual((method, path, timeout), ("POST", "/systemone", 3))
        self.assertEqual(body["state"], {"original_request": "Read the actual title", "candidate": CANDIDATE,
                                        "evidence": observed, "current_screen": current})
        # One overall choice, plus per-claim Nouls riding in the same call.
        self.assertEqual(set(body["questions"]), {"output_support", "claim_final_state", "claim_entity", "claim_field"})
        self.assertEqual(body["questions"]["output_support"]["type"], "choice")
        self.assertTrue(all(body["questions"][k]["type"] == "noul"
                            for k in ("claim_final_state", "claim_entity", "claim_field")))
        self.assertEqual({item["purpose"] for item in events}, {"verification"})
        self.assertEqual(events[-1]["usage"]["total_tokens"], 110)
        self.assertNotIn("candidate", str(events))
        self.assertNotIn(TITLE, str(events))

    def test_native_evidence_exports_json_arrays_without_mutating_internal_rectangles(self):
        evidence = Evidence()
        evidence.add(screen("results"), 1)
        exported = evidence.public()
        self.assertIsInstance(evidence.entries[0]["rect"], tuple)
        self.assertIsInstance(exported["entries"][0]["rect"], list)
        self.assertEqual(bounded_json(exported), exported)
        exported["entries"][0]["rect"][0] = .9
        self.assertNotEqual(evidence.entries[0]["rect"][0], .9)

    def test_supported_unsupported_and_unclear_are_explicit_typed_choices(self):
        for expected in OutputSupport:
            with self.subTest(expected=expected):
                model, _events = self.model(response(expected.value))
                self.assertIs(model.verify_output("Read title", CANDIDATE, observed_evidence(),
                                                 current_screen=screen("results").public()), expected)

    def test_missing_malformed_nonfinite_or_tied_support_fails_closed(self):
        invalid = [{}, {"answers": {}}, {"answers": {"output_support": None}},
                   response(type="noul"), response(choice="invented"), response(confidence=True),
                   response(confidence=float("nan")), response(probabilities={"supported": 1}),
                   response(probabilities={"supported": 1, "unsupported": 0, "unclear": 0, "extra": 0}),
                   response(probabilities={"supported": .5, "unsupported": .5, "unclear": 0}),
                   response(probabilities={"supported": .5, "unsupported": 0, "unclear": .5}),
                   response(probabilities={"supported": float("nan"), "unsupported": 0, "unclear": 0})]
        for reply in invalid:
            with self.subTest(reply=reply):
                model, _events = self.model(reply)
                self.assertIs(model.verify_output("Read title", CANDIDATE, observed_evidence(),
                                                 current_screen=screen("results").public()), OutputSupport.UNCLEAR)

    def test_invalid_input_is_rejected_before_network(self):
        model, _events = self.model()
        for goal, candidate, evidence in (("", CANDIDATE, observed_evidence()),
                ("x" * 12001, CANDIDATE, observed_evidence()), ("Read title", {"data": TITLE}, observed_evidence()),
                ("Read title", {**CANDIDATE, "citations": None}, observed_evidence()),
                ("Read title", CANDIDATE, {"entries": "bad"}),
                ("Read title", {**CANDIDATE, "data": "x" * 50000}, observed_evidence())):
            with self.subTest(goal=goal[:30]), self.assertRaises(ValueError):
                model.verify_output(goal, candidate, evidence, current_screen=screen("results").public())
        model.http.request.assert_not_called()

    def test_verification_uses_same_unique_call_counter_as_decisions(self):
        model, events = self.model()
        model.calls = 2
        model.verify_output("Read title", CANDIDATE, observed_evidence(), current_screen=screen("results").public())
        self.assertEqual([item["call_id"] for item in events], ["jev:3", "jev:3"])
        self.assertEqual(model.calls, 3)

    def test_provider_failure_emits_only_bounded_telemetry_and_is_never_retried(self):
        model, events = self.model()
        model.http.request.side_effect = ConnectionError("private answer or secret")
        with self.assertRaises(ConnectionError):
            model.verify_output("Read title", CANDIDATE, observed_evidence(), current_screen=screen("results").public())
        self.assertFalse(events[-1]["success"])
        self.assertEqual(events[-1]["purpose"], "verification")
        self.assertNotIn("private", str(events))
        model.http.request.assert_called_once()

    def test_full_request_verifier_separates_current_state_from_historical_facts(self):
        model, _events = self.model(response("unsupported"))
        current = screen("results").public()
        evidence = observed_evidence()
        evidence["entries"].append({"id": "e1", "text": "Home", "step": 0, "last_seen_step": 0})
        goal = "Read the title, then return Home and report the title."
        self.assertIs(model.verify_output(goal, CANDIDATE, evidence, current_screen=current), OutputSupport.UNSUPPORTED)
        payload = model.http.request.call_args.args[2]
        self.assertEqual(payload["state"]["current_screen"], current)
        self.assertNotIn("Home", payload["state"]["current_screen"]["text"])
        rules = payload["questions"]["output_support"]["instructions"]
        self.assertIn("FULL fulfillment", rules)
        self.assertIn("not replace or waive", rules)
        self.assertIn("never by a prior screen", rules)

    def test_missing_or_malformed_final_screen_never_reaches_provider(self):
        model, _events = self.model()
        for current in (None, {}, {"source": "x", "elements": [], "text": []}):
            with self.subTest(current=current), self.assertRaises(ValueError):
                model.verify_output("Read title", CANDIDATE, observed_evidence(), current_screen=current)
        model.http.request.assert_not_called()


class AgentOutputVerificationTests(unittest.TestCase):
    def test_requested_answer_failure_cannot_keep_completed_status(self):
        for failure, expected in ((TimeoutError(), "extraction_timeout"),
                                  (InsufficientEvidence(), "insufficient_evidence"),
                                  (ValueError(), "extraction_failed")):
            with self.subTest(expected=expected):
                driver, helper, model = self.ready()
                helper.extract_auto.side_effect = failure
                result = self.run_answer(driver, helper, model)
                self.assertEqual(result["status"], "completion_not_confirmed")
                self.assertEqual(result["data_status"], expected)
                self.assert_no_answer(result)
                model.verify_output.assert_not_called()

    def test_missing_or_exhausted_helper_cannot_complete_a_requested_answer(self):
        for budget in (None, 0):
            with self.subTest(budget=budget):
                driver, helper, model = self.ready()
                result = self.run_answer(driver, None if budget is None else helper, model,
                                         max_helper_calls=4 if budget is None else budget)
                self.assertEqual(result["status"], "completion_not_confirmed")
                self.assertIn(result["data_status"], {"helper_unavailable", "helper_budget_exhausted"})
                self.assert_no_answer(result)
                model.verify_output.assert_not_called()

    def ready(self, support=OutputSupport.SUPPORTED):
        driver, helper = DemoDriver(), DemoHelper()
        driver.stage = "results"
        helper.calls = 0
        def extract(*_args, **_kwargs):
            helper.calls += 1
            return {"schema": STRING, **CANDIDATE}
        helper.extract_auto = Mock(side_effect=extract)
        helper.extract = Mock(return_value=CANDIDATE)
        helper.ask = Mock(side_effect=AssertionError("No action helper belongs on this answer path"))
        model = Mock(spec=Jev)
        model.decide.return_value = replace(DemoModel().decide(screen("results"), "", []), output_intent=OutputIntent.TEXT)
        model.verify_output.return_value = support
        return driver, helper, model

    def run_answer(self, driver, helper, model, goal="Read the actual title", **kwargs):
        return Agent(driver, model, helper, **kwargs).run(goal, execute=True, output_format="auto")

    def assert_no_answer(self, result):
        self.assertIsNone(result["data"])
        self.assertEqual(result["citations"], [])
        self.assertFalse(result["schema_validated"])
        self.assertFalse(result["model_claimed_complete"])

    def test_verified_answer_publishes_once_after_both_done_checks_and_literal_validation(self):
        driver, helper, model = self.ready()
        def verify(goal, candidate, evidence, **_kwargs):
            self.assertEqual(model.decide.call_count, 2)
            self.assertEqual(helper.calls, 1)
            self.assertEqual(goal, "Read the actual title")
            self.assertEqual(candidate, CANDIDATE)
            self.assertEqual(evidence["entries"][0]["text"], TITLE)
            return OutputSupport.SUPPORTED
        model.verify_output.side_effect = verify
        result = self.run_answer(driver, helper, model)
        self.assertEqual(result["data"], TITLE)
        self.assertEqual(result["output_support"], "supported")
        self.assertEqual(result["status"], "completed_unverified")
        self.assertFalse(result["independently_verified"])
        model.verify_output.assert_called_once()
        self.assertEqual(driver.actions, [])

    def test_unsupported_unclear_or_untyped_answer_is_not_published_as_completion(self):
        for support in (OutputSupport.UNSUPPORTED, OutputSupport.UNCLEAR, "supported", True, None):
            with self.subTest(support=support):
                driver, helper, model = self.ready(support)
                events = []
                result = self.run_answer(driver, helper, model, emit=events.append)
                self.assertEqual(result["status"], "completion_not_confirmed")
                self.assertEqual(result["data_status"], "unsupported_answer")
                self.assertIsNone(result["output_schema"])
                self.assert_no_answer(result)
                self.assertTrue(all("candidate" not in event for event in events))
                model.verify_output.assert_called_once()

    def test_missing_verifier_fails_closed(self):
        driver, helper, model = self.ready()
        legacy_model = SimpleNamespace(decide=model.decide)
        result = self.run_answer(driver, helper, legacy_model)
        self.assertEqual(result["status"], "completion_not_confirmed")
        self.assertEqual(result["data_status"], "unsupported_answer")
        self.assert_no_answer(result)

    def test_invalid_literal_candidate_never_reaches_semantic_verifier(self):
        driver, helper, model = self.ready()
        helper.extract_auto.side_effect = None
        helper.extract_auto.return_value = {"schema": STRING, **CANDIDATE, "data": "Invented title"}
        result = self.run_answer(driver, helper, model)
        self.assertEqual(result["data_status"], "extraction_failed")
        self.assertIsNone(result["data"])
        model.verify_output.assert_not_called()

    def test_action_only_uses_no_extractor_or_terminal_verifier(self):
        driver, helper, model = self.ready()
        model.decide.return_value = replace(model.decide.return_value, output_intent=OutputIntent.ACTION_ONLY)
        result = self.run_answer(driver, helper, model)
        self.assertEqual(result["data_status"], "not_requested")
        self.assertEqual(helper.calls, 0)
        self.assertIsNone(result["output_support"])
        helper.extract_auto.assert_not_called()
        model.verify_output.assert_not_called()

    def test_stop_or_unclear_condition_prevents_extraction_and_terminal_verification(self):
        for gate in (StopGate.STOP, StopGate.UNCLEAR):
            driver, helper, model = self.ready()
            model.decide.return_value = replace(model.decide.return_value, stop_gate=gate)
            result = self.run_answer(driver, helper, model, goal=CONDITIONAL)
            self.assert_no_answer(result)
            helper.extract_auto.assert_not_called()
            model.verify_output.assert_not_called()

    def test_stop_without_any_stated_condition_does_not_end_the_run(self):
        driver, helper, model = self.ready()
        done = model.decide.return_value
        model.decide.side_effect = [done, replace(done, stop_gate=StopGate.STOP)]
        result = self.run_answer(driver, helper, model)
        self.assertNotEqual(result["status"], "user_condition_met")
        model.verify_output.assert_called_once()

    def test_a_prohibition_read_as_a_stop_does_not_keep_a_question_from_its_answer(self):
        # ret.note_code (diag-15): "Only look; do not edit" gated DONE on the open note as STOP.
        driver, helper, model = self.ready()
        model.decide.return_value = replace(model.decide.return_value, stop_gate=StopGate.STOP)
        result = self.run_answer(driver, helper, model, goal="Read the actual title. Only look; do not edit it.")
        self.assertNotEqual(result["status"], "user_condition_met")
        model.verify_output.assert_called_once()

    def test_failed_completion_check_prevents_extraction_and_verification(self):
        driver, helper, model = self.ready()
        model.decide.side_effect = [model.decide.return_value,
                                   replace(model.decide.return_value, operation="WAIT")]
        result = self.run_answer(driver, helper, model)
        self.assertEqual(result["status"], "completion_not_confirmed")
        helper.extract_auto.assert_not_called()
        model.verify_output.assert_not_called()

    def test_answer_acceptance_uses_grounding_not_uncalibrated_navigation_probability(self):
        for probability in (.1, .79, .89):
            for support in OutputSupport:
                with self.subTest(probability=probability, support=support):
                    driver, helper, model = self.ready(support)
                    model.decide.return_value = replace(model.decide.return_value, goal_probability=probability)
                    result = self.run_answer(driver, helper, model)
                    self.assertEqual(model.decide.call_count, 2)
                    helper.extract_auto.assert_called_once()
                    model.verify_output.assert_called_once()
                    self.assertEqual(driver.actions, [])
                    if support is OutputSupport.SUPPORTED:
                        self.assertEqual(result["data"], TITLE)
                        self.assertEqual(result["data_status"], "extracted")
                    else:
                        self.assert_no_answer(result)
                        self.assertEqual(result["data_status"], "unsupported_answer")

    def test_answer_path_does_not_relax_action_only_or_intermediate_navigation_gate(self):
        for intermediate in (False, True):
            with self.subTest(intermediate=intermediate):
                driver, helper, model = self.ready()
                # Below the calibrated completion floor (task_policy.COMPLETION_GOAL_FLOOR).
                model.decide.return_value = replace(model.decide.return_value, goal_probability=.4,
                    output_intent=OutputIntent.TEXT if intermediate else OutputIntent.ACTION_ONLY)
                result = Agent(driver, model, helper).run("Read title", execute=True, output_format="auto",
                    subgoals=["Open the page"] if intermediate else None)
                self.assertEqual(result["status"], "inconsistent_completion")
                helper.extract_auto.assert_not_called()
                model.verify_output.assert_not_called()

    def test_answer_path_still_rejects_blocked_watcher_on_either_observation(self):
        for second in (False, True):
            with self.subTest(second=second):
                driver, helper, model = self.ready()
                done = replace(model.decide.return_value, goal_probability=.79)
                blocked = replace(done, blocked_probability=.99)
                model.decide.side_effect = [done, blocked] if second else [blocked]
                result = self.run_answer(driver, helper, model)
                self.assertEqual(result["status"], "completion_not_confirmed" if second else "inconsistent_completion")
                helper.extract_auto.assert_not_called()
                model.verify_output.assert_not_called()

    def test_answer_path_still_honors_second_observation_stop_guard(self):
        for guard in (StopGate.STOP, StopGate.UNCLEAR):
            with self.subTest(guard=guard):
                driver, helper, model = self.ready()
                done = replace(model.decide.return_value, goal_probability=.79)
                model.decide.side_effect = [done, replace(done, stop_gate=guard)]
                result = self.run_answer(driver, helper, model, goal=CONDITIONAL)
                self.assert_no_answer(result)
                helper.extract_auto.assert_not_called()
                model.verify_output.assert_not_called()

    def test_explicit_custom_schema_still_requires_semantic_acceptance(self):
        driver, helper, model = self.ready(OutputSupport.UNSUPPORTED)
        result = Agent(driver, model, helper).run("Read title", execute=True, output_schema=STRING, output_format="json")
        self.assertEqual(result["data_status"], "unsupported_answer")
        self.assertEqual(result["output_schema"], STRING)
        self.assert_no_answer(result)
        helper.extract_auto.assert_not_called()
        model.verify_output.assert_called_once()

    def test_correct_fact_cannot_complete_wrong_final_state_even_with_explicit_output_format(self):
        for goal in ("Read the title, return Home, then report the title.",
                     "Return to Home. Do not remain on the results page."):
            with self.subTest(goal=goal):
                driver, helper, model = self.ready()
                model.decide.return_value = replace(model.decide.return_value, goal_probability=.2)
                def verify(original, candidate, evidence, *, current_screen, **_kwargs):
                    self.assertEqual(original, goal)
                    self.assertEqual(candidate["data"], TITLE)
                    self.assertEqual(current_screen, driver.observe().public())
                    self.assertNotIn("Home", current_screen["text"])
                    return OutputSupport.UNSUPPORTED
                model.verify_output.side_effect = verify
                result = Agent(driver, model, helper).run(goal, execute=True, output_format="text")
                self.assertEqual(result["data_status"], "unsupported_answer")
                self.assert_no_answer(result)
                self.assertEqual(driver.actions, [])

    def test_preview_never_extracts_or_verifies(self):
        driver, helper, model = self.ready()
        result = Agent(driver, model, helper).run("Read title", output_format="text")
        self.assertEqual(result["status"], "preview")
        helper.extract_auto.assert_not_called()
        model.verify_output.assert_not_called()

    def test_provider_error_or_timeout_rejects_completion_without_retry(self):
        for failure, status in ((TimeoutError(), "verification_timeout"), (ConnectionError(), "verification_failed")):
            driver, helper, model = self.ready()
            model.verify_output.side_effect = failure
            result = self.run_answer(driver, helper, model)
            self.assertEqual(result["status"], "completion_not_confirmed")
            self.assertEqual(result["data_status"], status)
            self.assert_no_answer(result)
            model.verify_output.assert_called_once()

    def test_verification_wall_budget_overrun_cannot_publish_answer(self):
        driver, helper, model = self.ready()
        now = [0]
        def verify(*_args, **_kwargs):
            now[0] = 4
            return OutputSupport.SUPPORTED
        model.verify_output.side_effect = verify
        with patch("mobile_agent.agent.time.monotonic", side_effect=lambda: now[0]):
            result = self.run_answer(driver, helper, model, max_seconds=3)
        self.assertEqual(result["data_status"], "verification_timeout")
        self.assert_no_answer(result)

    def test_cancellation_after_verification_suppresses_the_answer(self):
        driver, helper, model = self.ready()
        result = self.run_answer(driver, helper, model, cancelled=lambda: model.verify_output.called)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["data_status"], "not_extracted")
        self.assert_no_answer(result)

    def test_cancellation_during_extraction_never_spends_verification_call(self):
        driver, helper, model = self.ready()
        result = self.run_answer(driver, helper, model, cancelled=lambda: helper.extract_auto.called)
        self.assertEqual(result["status"], "stopped")
        model.verify_output.assert_not_called()
        self.assert_no_answer(result)


if __name__ == "__main__":
    unittest.main()
