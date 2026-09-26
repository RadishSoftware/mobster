import copy
import math
import unittest
from unittest.mock import Mock, patch

from ..benchmarks import (Sample, aggregate, analyze_model_samples, analyze_runs,
                         distribution, fetch_runs, hybrid_report, run_samples, unwrap_runs)
from ..inference import request_inference


def fixture():
    return {"id": "0123456789ab", "appId": "settings", "mode": "live", "status": "blocked",
            "createdAt": 1000, "finishedAt": 1800, "summary": {"elapsed_ms": 400},
            "events": [
                {"event": "app_launch_started", "timestamp": 1010},
                {"event": "app_launch_acknowledged", "timestamp": 1100},
                {"event": "observation", "timestamp": 1120},
                {"event": "decision", "timestamp": 1250, "step": 0, "source": "wda",
                 "latency_ms": 130, "observe_ms": 20},
                {"event": "action_started", "timestamp": 1270, "step": 0},
                {"event": "action_acknowledged", "timestamp": 1280, "act_ms": 10},
                {"event": "observation_after_action", "timestamp": 1300, "settle_ms": 20},
                {"event": "decision", "timestamp": 1400, "step": 1, "source": "wda",
                 "latency_ms": 80, "observe_ms": 20},
                {"event": "result", "timestamp": 1500}]}


def provider_events(provider, call_id, purpose, ms=100, failed=False):
    """Exercise the production emitter without network access or real clocks."""
    events = []
    http = Mock(location="global")
    http.request.return_value = {"model": "jev-test" if provider == "typesafe" else "gemini-test"}
    if failed:
        http.request.side_effect = TimeoutError("private failure detail")
    with patch("mobile_agent.inference.time.monotonic", side_effect=[10, 10 + ms / 1000]):
        try:
            request_inference(http, "/unused", {}, 1, emit=events.append, provider=provider,
                              call_id=call_id, model=http.request.return_value["model"], purpose=purpose)
        except TimeoutError:
            if not failed:
                raise
    return events


class BenchmarkTests(unittest.TestCase):
    def test_prospective_action_verification_has_its_own_provider_stage(self):
        run = fixture()
        run["events"] = provider_events("typesafe", "jev:2", "action_verification", 120)
        samples, warnings = run_samples(run)
        self.assertEqual([(s.stage, s.ms) for s in samples if s.clock == "provider_request"],
                         [("jev_action_verification", 120)])
        self.assertFalse(warnings)

    def test_terminal_verification_is_separate_from_decision_and_not_double_counted(self):
        run = fixture()
        verify = provider_events("typesafe", "jev:3", "verification", 210)
        run["events"] = provider_events("typesafe", "jev:1", "decision", 180) + verify + [verify[-1]]
        samples, warnings = run_samples(run)
        self.assertEqual([(s.stage, s.ms, s.phase) for s in samples if s.clock == "provider_request"],
                         [("jev_request", 180, "first_in_provider"),
                          ("jev_verification", 210, "subsequent_in_provider")])
        self.assertEqual([item["issue"] for item in warnings], ["duplicate inference finish ignored"])

    def test_failed_verification_retains_actual_provider_timing(self):
        run = fixture()
        run["events"] = provider_events("typesafe", "jev:3", "verification", 30, failed=True)
        samples, warnings = run_samples(run)
        self.assertEqual([(s.stage, s.ms, s.outcome) for s in samples if s.clock == "provider_request"],
                         [("jev_verification", 30, "request_failed")])
        self.assertFalse(warnings)

    def test_unfinished_or_missing_verification_duration_is_never_invented(self):
        for unfinished in (True, False):
            run = fixture()
            events = provider_events("typesafe", "jev:3", "verification")
            if unfinished:
                events = events[:1]
            else:
                events[-1].pop("latency_ms")
            run["events"] = events
            samples, warnings = run_samples(run)
            self.assertFalse(any(s.stage == "jev_verification" for s in samples))
            self.assertEqual(warnings[0]["stage"], "jev_verification")

    def test_canonical_google_timings_cover_every_helper_purpose(self):
        run = fixture()
        run["events"] = [event for index, purpose in enumerate(("planning", "text", "recovery", "extraction"))
                         for event in provider_events("google", f"helper:{index + 1}", purpose, 100 + index)]
        samples, warnings = run_samples(run)
        provider = [sample for sample in samples if sample.clock == "provider_request"]
        self.assertEqual([(s.stage, s.ms) for s in provider],
                         [("helper_planning", 100), ("helper_text", 101),
                          ("helper_recovery", 102), ("helper_extraction", 103)])
        self.assertEqual({s.provider for s in provider}, {"google"})
        self.assertEqual({s.model for s in provider}, {"gemini-test"})
        self.assertEqual({s.outcome for s in provider}, {"response_received"})
        self.assertEqual([s.phase for s in provider],
                         ["first_in_provider"] + ["subsequent_in_provider"] * 3)
        self.assertFalse(warnings)

    def test_provider_request_and_framework_wall_are_not_conflated(self):
        run = fixture()
        run["events"][3:3] = provider_events("typesafe", "jev:1", "decision", 90)
        run["events"] += provider_events("typesafe", "jev:2", "decision", 40)
        run["events"].append({"event": "completion_check", "latency_ms": 50})
        samples, _ = run_samples(run)
        requests = [s for s in samples if s.clock == "provider_request"]
        self.assertEqual([(s.stage, s.provider, s.ms) for s in requests],
                         [("jev_request", "typesafe", 90), ("jev_request", "typesafe", 40)])
        wrappers = [s for s in samples if s.clock == "framework_wall"]
        self.assertEqual([s.ms for s in wrappers], [130, 80, 50])
        self.assertFalse(any("overhead" in s.stage for s in samples))
        groups = aggregate(samples)
        self.assertTrue(all(row["clock"] == "provider_request" for row in groups
                            if row["provider"] == "typesafe"))

    def test_duplicate_provider_finish_does_not_count_twice(self):
        run = fixture()
        events = provider_events("google", "helper:1", "planning")
        run["events"] = events + [copy.deepcopy(events[-1]),
                                 {"event": "helper", "purpose": "planning", "latency_ms": 999}]
        samples, warnings = run_samples(run)
        self.assertEqual([s.ms for s in samples if s.stage == "helper_planning"], [100])
        self.assertTrue(any(w["issue"] == "duplicate inference finish ignored" for w in warnings))

    def test_provider_failures_stay_separate_even_when_run_status_matches(self):
        run = fixture()
        run["status"] = "completed_unverified"
        run["events"] = (provider_events("google", "helper:1", "planning") +
                         provider_events("google", "helper:2", "recovery", 5, failed=True) +
                         provider_events("google", "helper:3", "recovery", 200))
        result = analyze_runs([run])
        recovery = [row for row in result["groups"] if row["stage"] == "helper_recovery"]
        self.assertEqual({(row["outcome"], row["median_ms"]) for row in recovery},
                         {("request_failed", 5), ("response_received", 200)})
        self.assertNotIn("private failure detail", str(result))

    def test_cancelled_inflight_call_has_no_invented_duration(self):
        run = fixture()
        run["status"] = "cancelled"
        events = provider_events("google", "helper:1", "planning")
        events[0]["timestamp"] = 1100
        run["events"] = events[:1]
        samples, warnings = run_samples(run)
        self.assertFalse(any(s.clock == "provider_request" for s in samples))
        self.assertTrue(any(w["issue"] == "inference has no finish event; duration unknown" for w in warnings))
        run["events"] = events
        samples, warnings = run_samples(run)
        self.assertEqual([(s.status, s.ms) for s in samples if s.clock == "provider_request"],
                         [("cancelled", 100)])
        self.assertFalse(warnings)

    def test_missing_provider_clock_cannot_fall_back_to_annotation_or_wall_gap(self):
        for invalid in (None, True, -1, math.nan, math.inf, "100"):
            with self.subTest(latency=invalid):
                run = fixture()
                events = provider_events("google", "helper:1", "planning")
                events[0]["timestamp"], events[1]["timestamp"] = 1000, 1500
                events[1]["latency_ms"] = invalid
                run["events"] = events + [{"event": "helper", "purpose": "planning", "latency_ms": 999}]
                samples, warnings = run_samples(run)
                self.assertFalse(any(s.stage == "helper_planning" for s in samples))
                self.assertTrue(any(w.get("stage") == "helper_planning" for w in warnings))

    def test_legacy_explicit_helper_clock_is_retained_without_inference_events(self):
        run = fixture()
        run["events"] += [{"event": "helper", "purpose": "planning", "latency_ms": 75},
                          {"event": "helper", "purpose": "recovery", "calls": 2}]
        samples, _ = run_samples(run)
        helpers = [s for s in samples if s.stage.startswith("helper_")]
        self.assertEqual([(s.stage, s.ms, s.clock) for s in helpers],
                         [("helper_planning", 75, "legacy_helper")])

    def test_provider_model_and_outcome_are_aggregation_boundaries(self):
        baseline = dict(run_id="r", app="settings", status="completed_unverified", stage="helper_text",
                        ms=10, clock="provider_request", provider="google", model="gemini-test",
                        outcome="response_received")
        samples = [Sample(**baseline), Sample(**{**baseline, "model": "gemini-other"}),
                   Sample(**{**baseline, "provider": "helper"}),
                   Sample(**{**baseline, "outcome": "request_failed"})]
        self.assertEqual(len(aggregate(samples)), 4)

    def test_provider_missing_outcome_or_changed_identity_is_diagnosed(self):
        run = fixture()
        events = provider_events("google", "helper:1", "planning")
        del events[-1]["success"]
        run["events"] = events
        samples, warnings = run_samples(run)
        self.assertFalse(any(s.clock == "provider_request" for s in samples))
        self.assertTrue(any(w["issue"] == "missing or invalid provider outcome" for w in warnings))
        events[-1].update(success=True, purpose="extraction")
        samples, warnings = run_samples(run)
        self.assertFalse(any(s.clock == "provider_request" for s in samples))
        self.assertTrue(any(w["issue"] == "inference identity changed within one call" for w in warnings))

    def test_zero_failure_duration_and_non_google_helper_are_retained(self):
        run = fixture()
        run["events"] = (provider_events("typesafe", "jev:1", "decision", 0, failed=True) +
                         provider_events("helper", "helper:1", "text", 25))
        samples, warnings = run_samples(run)
        self.assertEqual([(s.provider, s.ms, s.outcome) for s in samples if s.clock == "provider_request"],
                         [("typesafe", 0, "request_failed"), ("helper", 25, "response_received")])
        self.assertFalse(warnings)

    def test_actual_response_model_can_differ_from_requested_alias(self):
        run = fixture()
        run["events"] = provider_events("typesafe", "jev:1", "decision")
        run["events"][0]["model"] = "jev-latest"
        samples, warnings = run_samples(run)
        self.assertEqual([s.model for s in samples if s.clock == "provider_request"], ["jev-test"])
        self.assertFalse(warnings)

    def test_nearest_rank_and_even_median(self):
        result = distribution(range(1, 21))
        self.assertEqual((result["median_ms"], result["p95_ms"]), (10.5, 19))
        self.assertFalse(result["small_sample"])

    def test_single_sample_is_not_claimed_reliable(self):
        self.assertTrue(distribution([10])["small_sample"])

    def test_bad_latency_rejected(self):
        for values in ([], [True], [-1], [math.inf], [math.nan], ["12"]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                distribution(values)

    def test_explicit_and_gap_timings(self):
        samples, warnings = run_samples(fixture())
        timings = {sample.stage: sample.ms for sample in samples}
        self.assertEqual(timings["task_wall"], 800)
        self.assertEqual(timings["agent_loop"], 400)
        self.assertEqual(timings["app_launch_ack"], 90)
        self.assertEqual(timings["pre_action_gap"], 20)
        self.assertEqual(timings["post_result_tail"], 300)
        self.assertEqual(timings["before_first_observation"], 120)
        self.assertFalse(warnings)

    def test_first_call_is_not_called_cold(self):
        samples, _ = run_samples(fixture())
        self.assertEqual([s.phase for s in samples if s.stage == "jev_decision"],
                         ["first_in_run", "subsequent_in_run"])

    def test_no_observation_source_guessing(self):
        run = fixture()
        del run["events"][3]["source"]
        samples, _ = run_samples(run)
        self.assertTrue(any(sample.stage == "observe" for sample in samples))

    def test_fast_failure_cannot_mix_with_pass(self):
        groups = aggregate([Sample("1", "a", "error", "task", 1),
                            Sample("2", "a", "completed_unverified", "task", 1000)])
        self.assertEqual(len(groups), 2)
        self.assertEqual({row["median_ms"] for row in groups}, {1, 1000})

    def test_demos_are_excluded(self):
        run = fixture()
        run["mode"] = "demo"
        result = analyze_runs([run])
        self.assertEqual(result["live_runs"], 0)
        self.assertEqual(result["excluded_non_live_runs"], 1)
        self.assertEqual(result["groups"], [])

    def test_no_fabricated_independent_success(self):
        run = fixture()
        run["status"] = "completed_unverified"
        self.assertEqual(analyze_runs([run])["independently_verified_runs"], 0)

    def test_duplicate_runs_rejected(self):
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            analyze_runs([fixture(), fixture()])

    def test_metadata_only_not_misreported_as_fast_empty_run(self):
        run = fixture()
        run.update(events=[], eventCount=10)
        with self.assertRaisesRegex(ValueError, "metadata"):
            analyze_runs([run])

    def test_malformed_shapes_rejected(self):
        for value in (None, [None], [{"id": 1}], [{"id": "a", "mode": "live", "events": [None]}]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                analyze_runs(value)

    def test_bad_clock_is_reported_not_clamped_to_zero(self):
        run = fixture()
        run["finishedAt"] = 900
        samples, warnings = run_samples(run)
        self.assertFalse(any(sample.stage == "task_wall" for sample in samples))
        self.assertTrue(any(warning.get("stage") == "task_wall" for warning in warnings))

    def test_clock_reversal_is_visible(self):
        run = fixture()
        run["events"][3]["timestamp"] = 100
        _, warnings = run_samples(run)
        self.assertTrue(any(warning["issue"] == "wall clock moved backwards" for warning in warnings))

    def test_inflight_wall_duration_not_invented(self):
        run = fixture()
        run["finishedAt"] = None
        samples, _ = run_samples(run)
        self.assertFalse(any(sample.stage in {"task_wall", "post_result_tail"} for sample in samples))

    def test_private_text_is_not_exported(self):
        run = fixture()
        run["goal"] = "SECRET USER GOAL"
        run["events"][2]["text"] = "SECRET APP CONTENT"
        before = copy.deepcopy(run)
        result = str(analyze_runs([run]))
        self.assertNotIn("SECRET", result)
        self.assertEqual(run, before)

    def test_model_failures_and_call_order_separate(self):
        records = [{"sample": {"model": "gemini", "case": "typing", "ms": ms, "passed": passed}}
                   for ms, passed in [(10, False), (200, True), (300, True)]]
        records.append({"summary": []})
        result = analyze_model_samples(records)
        self.assertEqual(len(result["groups"]), 2)
        self.assertEqual(result["groups"][0]["status"], "failed")
        self.assertEqual(result["groups"][1]["median_ms"], 250)
        self.assertEqual(result["groups"][1]["phase"], "subsequent_in_model")

    def test_invalid_model_record_rejected(self):
        with self.assertRaises(ValueError):
            analyze_model_samples([{"sample": {"model": "m", "ms": 1, "passed": "false", "case": "a"}}])

    def test_export_shapes(self):
        run = fixture()
        for value in (run, [run], {"run": run}, {"runs": [run]}):
            self.assertEqual(unwrap_runs(value), [run])

    def test_remote_or_credential_api_rejected_before_network(self):
        with patch("mobile_agent.benchmarks.build_opener") as network:
            for url in ("https://example.com/api", "http://localhost/api/runs",
                        "http://key@localhost/api", "http://127.0.0.1/api?token=secret"):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    fetch_runs(url)
            network.assert_not_called()

    def test_hybrid_report_splits_jev_and_helper_and_never_invents_accuracy(self):
        run = fixture()
        run["summary"] = {"elapsed_ms": 400, "decisions": 2, "model_claimed_complete": True,
                          "independently_verified": False}
        run["events"] = (
            provider_events("typesafe", "jev:1", "decision", 139) +
            provider_events("typesafe", "jev:2", "action_verification", 80) +
            provider_events("google", "helper:1", "recovery", 592) +
            [{"event": "decision", "latency_ms": 120, "fanout_questions": 7, "step": 0},
             {"event": "result", "timestamp": 1500}])
        report = hybrid_report([run])
        self.assertEqual(report["kind"], "hybrid_report")
        self.assertEqual(report["tasks"], 1)
        self.assertEqual(report["decisions_per_task"], 2)
        self.assertEqual(report["jev_calls_per_task"], 2)
        self.assertEqual(report["helper_calls_per_task"], 1)
        self.assertEqual(report["system_one"]["calls"], 2)
        self.assertEqual(report["system_two"]["calls"], 1)
        self.assertEqual(report["model_claimed_complete_runs"], 1)
        self.assertEqual(report["independently_verified_runs"], 0)
        self.assertEqual(report["fanout"], {"batches": 1, "questions": 7, "questions_per_batch": 7.0})
        self.assertIsNotNone(report["decision_latency_ms"])
        self.assertEqual(report["decision_latency_ms"]["n"], 1)
        self.assertTrue(any("not billing or task accuracy" in c for c in report["caveats"]))
        self.assertNotIn("SECRET", str(report))

    def test_hybrid_report_cost_per_task_and_missing_usage_stay_explicit(self):
        runs = []
        for index in range(2):
            run = fixture()
            run["id"] = f"{index:012x}"
            run["summary"] = {"decisions": 1}
            run["events"] = (provider_events("typesafe", f"jev:{index + 1}", "decision", 100 + index)
                             + [{"event": "decision", "latency_ms": 100 + index, "step": 0}])
            runs.append(run)
        report = hybrid_report(runs)
        self.assertEqual(report["tasks"], 2)
        self.assertEqual(report["decisions_per_task"], 1)
        self.assertEqual(report["jev_calls_per_task"], 1)
        self.assertEqual(report["system_two"]["calls"], 0)
        self.assertIsNone(report["cost_per_task_usd"])  # provider_events omit usage/price
        self.assertIsNone(report["combined"]["estimated_usd"])
        self.assertEqual(report["decision_latency_ms"]["n"], 2)
        self.assertEqual(report["decision_latency_ms"]["median_ms"], 100.5)
        with self.assertRaises(ValueError):
            hybrid_report([{"id": "x"}] * 200)

    def test_hybrid_report_excludes_non_live_runs_like_latency_analysis(self):
        live, demo = fixture(), fixture()
        demo["mode"] = "demo"
        demo["id"] = "ffffffffffff"
        report = hybrid_report([live, demo])
        self.assertEqual(report["tasks"], 1)
        self.assertEqual(report["excluded_non_live_runs"], 1)


if __name__ == "__main__":
    unittest.main()
