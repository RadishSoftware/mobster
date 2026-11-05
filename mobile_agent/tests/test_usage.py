"""Offline usage-accounting tests; never send provider or device requests."""

import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from ..journal import Journal, JournalError
from ..run import Run

APP = {"id": "settings", "name": "Settings", "bundleId": "com.apple.Preferences"}


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.journal = Journal()
        self.addCleanup(self.journal.close)

    def test_month_boundary_uses_the_offset_at_the_start_of_the_month(self):
        from ..usage import month_start
        try:
            with mock.patch.dict(os.environ, {"TZ": "America/Los_Angeles"}):
                time.tzset()
                for now, expected in (("2026-03-20T12:00:00-07:00", "2026-03-01T00:00:00-08:00"),
                                      ("2026-11-20T12:00:00-08:00", "2026-11-01T00:00:00-07:00")):
                    with self.subTest(now=now):
                        self.assertEqual(month_start(datetime.fromisoformat(now).timestamp()),
                                         datetime.fromisoformat(expected).timestamp() * 1000)
        finally:
            time.tzset()

    def run_record(self, journal=None):
        journal = journal or self.journal
        run = Run(APP, "A private task never copied into the usage ledger", "live", journal=journal)
        journal.create(run.metadata())
        return run

    def call(self, run, call_id="jev:1", finish=True):
        identity = {"call_id": call_id, "provider": "anthropic", "model": "claude-sonnet-5-5", "purpose": "decision"}
        run.emit({"event": "inference_started", **identity})
        if finish:
            usage = {"input_tokens": 5000, "output_tokens": 250, "total_tokens": 5250}
            run.emit({"event": "inference_finished", **identity, "usage": usage,
                      "cost_nanodollars": 210_000, "estimated_usd": .00021, "cost_basis": "published_rate",
                      "success": True})

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
        for _ in range(101):
            later = self.run_record()
            later.finish({"status": "completed_unverified"})
        self.assertFalse(run.id in {row["id"] for row in self.journal.load()},
                         f"finished run {run.id} remained in history")
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


class PlanUsageTests(unittest.TestCase):
    def setUp(self):
        self.journal = Journal()
        self.addCleanup(self.journal.close)

    def run_record(self, journal=None):
        journal = journal or self.journal
        run = Run(APP, "Task", "live", journal=journal)
        journal.create(run.metadata())
        return run

    def agent_call(self, run, call_id, billing="plan", model="claude-sonnet-5-5", provider="claude_code"):
        identity = {"call_id": call_id, "provider": provider, "model": model, "purpose": "agent"}
        run.emit({"event": "inference_started", **identity})
        plan = billing == "plan"
        run.emit({"event": "inference_finished", **identity, "success": True, "billing": billing,
                  "usage": {"input_tokens": 20440, "output_tokens": 350, "total_tokens": 20790,
                            "cache_read_tokens": 18000, "cache_write_tokens": 2400},
                  "cost_nanodollars": 0 if plan else 41_200_000, "estimated_usd": 0.0 if plan else 0.0412,
                  "cost_basis": "plan" if plan else "published_rate",
                  "api_equivalent_nanodollars": 41_200_000})

    def smart_call(self, run):
        identity = {"call_id": "jev:1", "provider": "anthropic", "model": "claude-sonnet-5-5", "purpose": "decision"}
        run.emit({"event": "inference_started", **identity})
        run.emit({"event": "inference_finished", **identity, "success": True, "cost_basis": "published_rate",
                  "usage": {"input_tokens": 5000, "output_tokens": 250, "total_tokens": 5250},
                  "cost_nanodollars": 210_000, "estimated_usd": .00021})

    def test_a_plan_row_sums_tokens_cache_and_api_equivalent_and_costs_nothing(self):
        run = self.run_record()
        self.agent_call(run, "claude_code:1")
        self.agent_call(run, "claude_code:2")
        self.smart_call(run)
        result = self.journal.usage()
        row = next(p for p in result["providers"] if p["provider"] == "claude_code")
        self.assertEqual((row["billing"], row["calls"], row["input_tokens"], row["total_tokens"],
                          row["cache_read_tokens"], row["cache_write_tokens"]), ("plan", 2, 40880, 41580, 36000, 4800))
        self.assertAlmostEqual(row["api_equivalent_usd"], 0.0824)
        self.assertEqual(row["estimated_usd"], 0.0)
        totals = result["totals"]
        self.assertEqual((totals["plan_tokens"], totals["cache_read_tokens"]), (41580, 36000))
        self.assertAlmostEqual(totals["estimated_usd"], 0.00021)  # Smart's call only: the plan adds no spend
        week = self.journal.plan_since(0)
        self.assertEqual(week["tokens"], 41580)
        self.assertAlmostEqual(week["apiEquivalentUsd"], 0.0824)
        self.assertAlmostEqual(self.journal.spend_since(0), 0.00021)

    def test_an_api_key_row_is_spend_and_not_plan_tokens(self):
        run = self.run_record()
        self.agent_call(run, "claude_code:1", billing="api_key")
        result = self.journal.usage()
        self.assertEqual(result["providers"][0]["billing"], "api_key")
        self.assertAlmostEqual(result["totals"]["estimated_usd"], 0.0412)
        self.assertIsNone(result["totals"]["plan_tokens"])
        self.assertEqual(self.journal.plan_since(0), {"tokens": None, "apiEquivalentUsd": None})

    def test_a_ledger_from_before_the_new_columns_is_migrated_in_place(self):
        with tempfile.TemporaryDirectory(prefix="mobster-usage-test-") as folder:
            path = Path(folder) / "runs.sqlite3"
            old = sqlite3.connect(path)
            old.executescript("""
                CREATE TABLE usage_tracking (id INTEGER PRIMARY KEY CHECK(id=1), since REAL NOT NULL);
                CREATE TABLE usage_calls (
                    run_id TEXT NOT NULL, call_id TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
                    started_at REAL NOT NULL, finished_at REAL,
                    input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER,
                    cost_nanodollars INTEGER, PRIMARY KEY(run_id,call_id));
                INSERT INTO usage_tracking VALUES (1, 1000);
                INSERT INTO usage_calls VALUES ('old-run','jev:1','anthropic','claude-sonnet-5-5',2000,2001,
                                                5000,250,5250,210000);""")
            old.commit()
            old.close()
            journal = Journal(path)
            try:
                columns = {row[1] for row in journal.connection.execute("PRAGMA table_info(usage_calls)")}
                self.assertTrue({"cache_read_tokens", "cache_write_tokens", "billing",
                                 "api_equivalent_nanodollars"} <= columns)
                row = journal.usage()["providers"][0]
                self.assertEqual((row["total_tokens"], row["estimated_usd"]), (5250, .00021))
                self.assertEqual((row["billing"], row["cache_read_tokens"], row["api_equivalent_usd"]),
                                 (None, None, None))
                run = self.run_record(journal)
                self.agent_call(run, "claude_code:1")
                self.assertEqual(journal.usage()["totals"]["plan_tokens"], 20790)
            finally:
                journal.close()

    def test_the_newest_limit_per_agent_is_kept_and_a_bad_one_is_dropped_without_failing_the_event(self):
        run = self.run_record()
        window = {"five_hour": {"utilization": 0.37, "resetsAt": 1792030800.0}}
        run.emit({"event": "agent_limits", "agent": "claude_code",
                  "limits": {"status": "allowed", "type": "five_hour", "resetsAt": 1.0, "windows": window}})
        run.emit({"event": "agent_limits", "agent": "claude_code",
                  "limits": {"status": "allowed_warning", "type": "five_hour", "resetsAt": 2.0, "windows": window}})
        run.emit({"event": "agent_limits", "agent": "codex",
                  "limits": {"status": "allowed", "type": None, "resetsAt": None, "windows": {}}})
        count = len(run.events)
        for bad in ({"event": "agent_limits", "agent": "claude_code", "limits": {"status": "fine"}},
                    {"event": "agent_limits", "agent": "nobody", "limits": {"status": "allowed"}},
                    {"event": "agent_limits", "agent": "claude_code", "limits": "x"}):
            run.emit(bad)
        self.assertEqual(len(run.events), count + 3)
        limits = self.journal.usage()["limits"]
        self.assertEqual(sorted(limits), ["claude_code", "codex"])
        self.assertEqual((limits["claude_code"]["status"], limits["claude_code"]["resetsAt"]), ("allowed_warning", 2.0))
        self.assertIn("at", limits["claude_code"])
        self.assertEqual(self.journal.usage(run.id)["limits"], {})
