"""Offline usage-accounting tests; never send provider or device requests."""

from pathlib import Path
from datetime import date
import tempfile
import unittest

from ..costs import estimate_cost, planning_estimate, google_model_rate, helper_model_rates, path_cost_totals
from ..journal import Journal, JournalError
from ..server import Run


APP = {"id": "settings", "name": "Settings", "bundleId": "com.apple.Preferences"}


class CostTests(unittest.TestCase):
    def test_jev_input_cost_output_free(self):
        value = estimate_cost("typesafe", "jev-1.13.0", {"input_tokens": 5000, "output_tokens": 250})
        self.assertEqual(value["cost_nanodollars"], 210000)
        self.assertEqual(value["estimated_usd"], .00021)
        self.assertEqual(value["cost_basis"], "published_rate")

    def test_google_cache_and_reasoning_rates(self):
        value = estimate_cost("google", "gemini-3.5-flash-lite", {
            "input_tokens": 1000, "output_tokens": 100, "reasoning_tokens": 50,
            "cached_input_tokens": 200, "total_tokens": 1150}, "global")
        self.assertEqual(value["cost_nanodollars"], 621000)

    def test_flash_estimate_excludes_credit_promotion_and_counts_reasoning_and_cache(self):
        value = estimate_cost("google", "gemini-3.7-flash", {
            "input_tokens": 1000, "output_tokens": 100, "reasoning_tokens": 50,
            "cached_input_tokens": 200, "total_tokens": 1150}, "global")
        self.assertEqual(value["cost_nanodollars"], 2_355_000)

    def test_pro_context_boundary_prices_entire_request_not_only_excess(self):
        for incoming, expected in ((200_000, 43_600_000), (200_001, 86_300_400)):
            value = estimate_cost("google", "gemini-3.1-pro-preview", {
                "input_tokens": incoming, "cached_input_tokens": incoming - 1000,
                "output_tokens": 100, "reasoning_tokens": 50, "total_tokens": incoming + 150}, "global")
            # Cached input is still part of the request context size.
            self.assertEqual(value["cost_nanodollars"], expected)

    def test_rate_catalog_keeps_base_and_long_context_rates_separate(self):
        rate = google_model_rate("gemini-3.1-pro-preview", "global")
        self.assertEqual(rate["input_usd_per_million"], 2)
        self.assertEqual(rate["output_usd_per_million"], 12)
        self.assertEqual(rate["context_tiers"][0], {"above_input_tokens": 200_000,
            "input_usd_per_million": 4, "output_usd_per_million": 18, "cached_input_usd_per_million": .4})
        self.assertEqual(planning_estimate("jev-latest", "gemini-3.1-pro-preview", "global")["rates"]["google"], rate)

    def test_promotion_expiry_and_regular_rates_are_explicit(self):
        promo = google_model_rate("gemini-3.7-flash", "global", today=date(2026, 12, 31))
        self.assertEqual(promo["input_usd_per_million"], 1.5)
        self.assertEqual(promo["promotion"]["input_usd_per_million"], .75)
        self.assertIn("credits", promo["promotion"]["description"])
        future = google_model_rate("gemini-3.7-flash", "global", today=date(2027, 1, 1))
        self.assertNotIn("promotion", future)
        self.assertEqual(future["input_usd_per_million"], 1.5)

    def test_unknown_model_or_region_has_no_catalog_rate(self):
        self.assertEqual(helper_model_rates(["gemini-future"], "global"), {})
        self.assertIsNone(google_model_rate("gemini-3.7-flash", "us-central1"))

    def test_invalid_counts_remain_unknown_for_every_supported_google_model(self):
        for model in ("gemini-3.5-flash-lite", "gemini-3.7-flash", "gemini-3.1-pro-preview"):
            for extra in ({"total_tokens": 1101}, {"cached_input_tokens": 1001},
                          {"tool_input_tokens": 1}, {"input_tokens": True}, {"output_tokens": -1}):
                self.assertIsNone(estimate_cost("google", model, {
                    "input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100, **extra}, "global")["estimated_usd"])

    def test_unknown_rates_counts_or_regions_are_not_free(self):
        for provider, model, usage, region in (
            ("typesafe", "jev-future", {"input_tokens": 10}, None),
            ("google", "gemini-3.5-flash-lite", {"input_tokens": 10}, "global"),
            ("google", "gemini-3.5-flash-lite", {"input_tokens": 10, "output_tokens": 1, "total_tokens": 15}, "global"),
            ("google", "gemini-3.5-flash-lite", {"input_tokens": 10, "output_tokens": 1, "total_tokens": 11}, "us-central1"),
            ("typesafe", "jev-latest", {"input_tokens": True}, None),
        ):
            self.assertIsNone(estimate_cost(provider, model, usage, region)["estimated_usd"])

    def test_explicit_zero_is_measured_zero(self):
        self.assertEqual(estimate_cost("typesafe", "jev-latest", {"input_tokens": 0})["estimated_usd"], 0)

    def test_planning_range_discloses_scenario_and_unknown_models(self):
        scenario = planning_estimate("jev-latest", "gemini-3.5-flash-lite", "global")
        self.assertEqual(scenario["estimated_min_usd"], .00375)
        self.assertEqual(scenario["estimated_max_usd"], .0123)
        self.assertEqual(scenario["basis"], "planning_scenario")
        self.assertFalse(scenario["is_spending_cap"])
        self.assertIsNone(planning_estimate("jev-latest", "unknown")["estimated_min_usd"])

    def test_planning_cost_accounts_for_draft_and_schema_with_disclosed_approximation(self):
        value = planning_estimate("jev-latest", "gemini-3.5-flash-lite", "global",
                                  goal="Read", output_schema={"type": "object"})
        self.assertEqual(value["assumptions"]["estimated_prompt_tokens"], 1)
        self.assertGreater(value["assumptions"]["estimated_schema_tokens"], 0)
        self.assertGreater(value["estimated_min_usd"], .00375)
        self.assertIn("not provider tokenization", value["assumptions"]["token_estimation"])

    def test_path_cost_totals_match_inference_events(self):
        jev_usage = {"input_tokens": 5000, "output_tokens": 250, "total_tokens": 5250}
        helper_usage = {"input_tokens": 1000, "output_tokens": 100,
                        "reasoning_tokens": 50, "cached_input_tokens": 200, "total_tokens": 1150}
        jev_price = estimate_cost("typesafe", "jev-1.13.0", jev_usage)
        helper_price = estimate_cost("google", "gemini-3.5-flash-lite", helper_usage, "global")
        events = [
            {"event": "inference_started", "call_id": "jev:1", "provider": "typesafe",
             "model": "jev-1.13.0", "purpose": "decision"},
            {"event": "inference_finished", "call_id": "jev:1", "provider": "typesafe",
             "model": "jev-1.13.0", "purpose": "decision", "success": True,
             "latency_ms": 139.03, "usage": jev_usage, **jev_price},
            {"event": "inference_started", "call_id": "helper:1", "provider": "google",
             "model": "gemini-3.5-flash-lite", "purpose": "recovery"},
            {"event": "inference_finished", "call_id": "helper:1", "provider": "google",
             "model": "gemini-3.5-flash-lite", "purpose": "recovery", "success": True,
             "latency_ms": 592.0, "usage": helper_usage, **helper_price},
        ]
        totals = path_cost_totals(events)
        self.assertEqual(totals["cost_basis"], "published_rate")
        self.assertEqual(totals["system_one"]["calls"], 1)
        self.assertEqual(totals["system_one"]["total_tokens"], 5250)
        self.assertEqual(totals["system_one"]["cost_nanodollars"], jev_price["cost_nanodollars"])
        self.assertEqual(totals["system_two"]["calls"], 1)
        self.assertEqual(totals["system_two"]["cost_nanodollars"], helper_price["cost_nanodollars"])
        self.assertEqual(totals["combined"]["calls"], 2)
        self.assertEqual(totals["combined"]["cost_nanodollars"],
                         jev_price["cost_nanodollars"] + helper_price["cost_nanodollars"])
        self.assertEqual(totals["combined"]["estimated_usd"],
                         jev_price["estimated_usd"] + helper_price["estimated_usd"])
        self.assertEqual(totals["combined"]["total_tokens"], 5250 + 1150)
        # Budget isolation: helper spend is never folded into the Jev path.
        self.assertLess(totals["system_one"]["estimated_usd"], totals["combined"]["estimated_usd"])
        self.assertGreater(totals["system_two"]["estimated_usd"], 0)

    def test_path_cost_totals_keep_unpriced_and_unreported_calls_unknown(self):
        totals = path_cost_totals([
            {"event": "inference_finished", "call_id": "jev:1", "provider": "typesafe",
             "latency_ms": 10, "success": False, "usage": {}},
            {"event": "inference_finished", "call_id": "helper:1", "provider": "google",
             "latency_ms": 10, "success": True},
        ])
        self.assertIsNone(totals["system_one"]["estimated_usd"])
        self.assertIsNone(totals["system_two"]["total_tokens"])
        self.assertIsNone(totals["combined"]["estimated_usd"])
        self.assertEqual(totals["system_one"]["failed"], 1)
        empty = path_cost_totals([])
        self.assertEqual(empty["combined"]["calls"], 0)
        self.assertIsNone(empty["system_one"]["latency_ms"])


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.journal = Journal()
        self.addCleanup(self.journal.close)

    def run_record(self, journal=None):
        journal = journal or self.journal
        run = Run(APP, "A private task never copied into the usage ledger", "live", journal=journal)
        journal.create(run.metadata())
        return run

    def call(self, run, call_id="jev:1", finish=True):
        identity = {"call_id": call_id, "provider": "typesafe", "model": "jev-1.13.0", "purpose": "decision"}
        run.emit({"event": "inference_started", **identity})
        if finish:
            usage = {"input_tokens": 5000, "output_tokens": 250, "total_tokens": 5250}
            run.emit({"event": "inference_finished", **identity, "usage": usage,
                      **estimate_cost("typesafe", identity["model"], usage), "success": True})

    def test_measured_calls_group_and_exact_derived_total(self):
        run = self.run_record()
        self.call(run)
        result = self.journal.usage()
        self.assertEqual(result["totals"]["calls"], 1)
        self.assertEqual(result["totals"]["reported_calls"], 1)
        self.assertEqual(result["totals"]["total_tokens"], 5250)
        self.assertEqual(result["totals"]["estimated_usd"], .00021)
        self.assertNotIn("private task", str(result))

    def test_pending_terminal_and_unknown_usage_are_distinct(self):
        run = self.run_record()
        self.call(run, finish=False)
        self.assertEqual(self.journal.usage()["totals"]["in_flight_calls"], 1)
        run.finish({"status": "interrupted", "reason": "Service restarted"})
        totals = self.journal.usage()["totals"]
        self.assertEqual(totals["in_flight_calls"], 0)
        self.assertEqual(totals["unreported_calls"], 1)
        self.assertIsNone(totals["estimated_usd"])
        self.assertIsNone(totals["total_tokens"])

    def test_duplicate_completion_rolls_back_event_and_count(self):
        run = self.run_record()
        self.call(run)
        count = len(run.events)
        with self.assertRaises(JournalError):
            run.emit({**run.events[-1]})
        self.assertEqual(len(run.events), count)
        self.assertEqual(len(self.journal.load()[0]["events"]), count)
        self.assertEqual(self.journal.usage()["totals"]["calls"], 1)

    def test_usage_survives_run_retention(self):
        run = self.run_record()
        self.call(run)
        run.finish({"status": "completed_unverified"})
        for _ in range(100):
            self.run_record()
        self.assertNotIn(run.id, [r["id"] for r in self.journal.load()])
        self.assertEqual(self.journal.usage()["totals"]["calls"], 1)

    def test_restart_preserves_prices_and_tracking_start(self):
        with tempfile.TemporaryDirectory(prefix="mobster-usage-test-") as folder:
            path = Path(folder) / "runs.sqlite3"
            before = Journal(path)
            run = self.run_record(before)
            self.call(run)
            expected = before.usage()
            before.close()
            after = Journal(path)
            try:
                actual = after.usage()
                self.assertEqual(actual["tracked_since"], expected["tracked_since"])
                self.assertEqual(actual["totals"], expected["totals"])
            finally:
                after.close()

    def test_per_run_filter_and_empty_totals(self):
        first, second = self.run_record(), self.run_record()
        self.call(first)
        self.call(second)
        self.assertEqual(self.journal.usage(first.id)["totals"]["calls"], 1)
        empty = self.journal.usage("missing")["totals"]
        self.assertEqual(empty["calls"], 0)
        self.assertIsNone(empty["total_tokens"])
        self.assertIsNone(empty["estimated_usd"])
