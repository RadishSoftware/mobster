"""Schedule and persistence checks using only fake runtimes and temporary databases."""

from datetime import datetime, timezone
import io
import json
from email.message import Message
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from mobile_agent.api_errors import APIError
from mobile_agent.journal import Journal, JournalError
from mobile_agent.schedule import next_occurrences, preview, validate_cron
from mobile_agent.server import make_handler
from mobile_agent.workflows import Workflows
from mobile_agent.workflow_scheduler import WorkflowScheduler


class FakeRuntime:
    def __init__(self, journal):
        self.journal, self.lock, self.runs = journal, threading.RLock(), {}
        self.calls, self.failure = [], None
        self.config = SimpleNamespace(port=8765)

    def create(self, app_id, goal, mode, key, **output):
        if self.failure:
            raise self.failure
        prior = self.journal.lookup(key)
        if prior:
            return self.runs[prior[0]], True
        self.calls.append((app_id, goal, mode, key, output))
        identifier = f"{len(self.calls):012x}"
        run = SimpleNamespace(id=identifier, status="running", finished_at=None)
        run.public = lambda: {"id": identifier, "status": run.status}
        self.journal.create({"id": identifier, "status": "running", "finishedAt": None}, key, "test")
        self.runs[identifier] = run
        return run, False


class CronTests(unittest.TestCase):
    def test_standard_fields_and_timezone_preview(self):
        expression = validate_cron("0 9 * * MON-FRI", "America/Los_Angeles")
        self.assertEqual(expression, "0 9 * * mon-fri")
        now = datetime(2026, 9, 19, tzinfo=timezone.utc).timestamp() * 1000
        values = preview({"cron": expression, "timezone": "America/Los_Angeles"}, now)["next"]
        self.assertEqual(len(values), 3)
        local = [datetime.fromtimestamp(value / 1000, ZoneInfo("America/Los_Angeles")) for value in values]
        self.assertEqual([(item.weekday(), item.hour) for item in local], [(0, 9), (1, 9), (2, 9)])

    def test_rejects_invalid_high_frequency_and_extensions(self):
        for value in ("* * * * *", "*/5 * * * *", "0,1 9 * * *", "0 0 31 2 *", "0 0 * * * *", "@daily", "H 0 * * *", "0 0 L * *", "0 0 * * 1#2", "0 0 1W * *", "0 9 * * nonsense", "0 0 * * * 0 2027"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_cron(value, "UTC")
        with self.assertRaises(ValueError):
            validate_cron("0 9 * * *", "not/a/timezone")

    def test_rate_proof_includes_midnight_gap(self):
        with self.assertRaisesRegex(ValueError, "five minutes"):
            validate_cron("0,59 0,23 * * *", "UTC")
        self.assertEqual(validate_cron("*/30 * * * *", "UTC"), "*/30 * * * *")

    def test_dst_occurrences_are_distinct_monotonic_utc_instants(self):
        now = datetime(2026, 10, 31, 12, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp() * 1000
        result = next_occurrences("30 1 * * *", "America/Los_Angeles", now, 3)
        local = [datetime.fromtimestamp(value / 1000, ZoneInfo("America/Los_Angeles")) for value in result]
        self.assertEqual([item.fold for item in local[:2]], [0, 1])
        self.assertEqual(result[1] - result[0], 3600_000)
        self.assertTrue(all(b > a for a, b in zip(result, result[1:])))

    def test_sparse_leap_day_is_bounded_and_supported(self):
        expression = validate_cron("0 9 29 2 *", "UTC")
        now = datetime(2026, 9, 19, tzinfo=timezone.utc).timestamp() * 1000
        result = next_occurrences(expression, "UTC", now)[0]
        self.assertEqual(datetime.fromtimestamp(result / 1000, timezone.utc).year, 2028)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mobster-workflows-test-")
        self.journal = Journal(Path(self.directory.name) / "runs.sqlite3")
        self.runtime = FakeRuntime(self.journal)
        self.time = datetime(2026, 9, 19, tzinfo=timezone.utc).timestamp()
        self.workflows = Workflows(self.runtime, lambda: self.time)
        self.runtime.workflows = self.workflows

    def tearDown(self):
        self.journal.close()
        self.directory.cleanup()

    def saved(self, **changes):
        return self.workflows.create({"name": "Read About", "appId": "settings", "goal": "Read iOS version", **changes})

    def scheduled(self, expression="*/30 * * * *"):
        workflow = self.saved()
        return self.workflows.update(workflow["id"], {"revision": 1, "cron": expression, "timezone": "UTC", "enabled": True})

    def due(self, workflow, late=0):
        self.time = workflow["nextAt"] / 1000 + late
        self.workflows.tick()
        return self.workflows.get(workflow["id"])

    def test_create_is_manual_paused_text_and_persistent(self):
        workflow = self.saved()
        self.assertEqual((workflow["enabled"], workflow["cron"], workflow["nextAt"], workflow["outputFormat"]), (False, None, None, "auto"))
        self.workflows.tick()
        self.assertEqual(self.runtime.calls, [])
        restored = Workflows(self.runtime, lambda: self.time)
        self.assertEqual(restored.list(), [workflow])

    def test_helper_model_is_saved_and_dispatched_without_following_later_default(self):
        with patch.dict("os.environ", {"TEXT_MODEL_PROVIDER": "vertex", "TEXT_MODEL": "gemini-3.5-flash-lite"}):
            workflow = self.saved()
        self.assertEqual(workflow["helperModel"], "gemini-3.5-flash-lite")
        with patch.dict("os.environ", {"TEXT_MODEL_PROVIDER": "vertex", "TEXT_MODEL": "gemini-3.7-flash"}):
            self.workflows.run(workflow["id"], "model-selected")
        self.assertEqual(self.runtime.calls[0][-1]["helper_model"], "gemini-3.5-flash-lite")
        self.assertEqual(Workflows(self.runtime).get(workflow["id"])["helperModel"], "gemini-3.5-flash-lite")

    def test_helper_model_update_changes_future_dispatch_only_and_requires_revision(self):
        with patch.dict("os.environ", {"TEXT_MODEL_PROVIDER": "vertex", "TEXT_MODEL": "gemini-3.5-flash-lite"}):
            workflow = self.saved()
            first, _ = self.workflows.run(workflow["id"], "first-model")
            updated = self.workflows.update(workflow["id"], {"revision": 1, "helperModel": "gemini-3.7-flash"})
            replay, reused = self.workflows.run(workflow["id"], "first-model")
            self.assertIs(first, replay)
            self.assertTrue(reused)
            self.workflows.run(workflow["id"], "second-model")
            with self.assertRaises(APIError):
                self.workflows.update(workflow["id"], {"revision": 1, "helperModel": "gemini-3.1-pro-preview"})
        self.assertEqual(updated["helperModel"], "gemini-3.7-flash")
        self.assertEqual([call[-1]["helper_model"] for call in self.runtime.calls],
                         ["gemini-3.5-flash-lite", "gemini-3.7-flash"])

    def test_invalid_saved_helper_model_is_rejected_without_persisting(self):
        with patch.dict("os.environ", {"TEXT_MODEL_PROVIDER": "vertex", "TEXT_MODEL": "gemini-3.5-flash-lite"}):
            with self.assertRaises(ValueError):
                self.saved(helperModel="gemini-unconfigured")
        self.assertEqual(self.workflows.list(), [])

    def test_schema_validation_precedes_any_storage_or_dispatch(self):
        with self.assertRaises(ValueError):
            self.saved(outputSchema={"type": "object"})
        with self.assertRaises(ValueError):
            self.saved(appId="arbitrary-private-app")
        self.assertEqual(self.workflows.list(), [])
        self.assertEqual(self.runtime.calls, [])

    def test_updates_require_revision_and_pause_clears_future_slot(self):
        workflow = self.scheduled()
        with self.assertRaises(APIError) as error:
            self.workflows.update(workflow["id"], {"revision": 1, "enabled": False})
        self.assertEqual(error.exception.code, "revision_conflict")
        updated = self.workflows.update(workflow["id"], {"revision": 2, "enabled": False})
        self.assertEqual((updated["enabled"], updated["nextAt"], updated["revision"]), (False, None, 3))
        self.time = workflow["nextAt"] / 1000
        self.workflows.tick()
        self.assertEqual(self.runtime.calls, [])

    def test_rename_preserves_scheduled_instant(self):
        workflow = self.scheduled()
        self.time += 17
        renamed = self.workflows.update(workflow["id"], {"revision": 2, "name": "Version report"})
        self.assertEqual(renamed["nextAt"], workflow["nextAt"])

    def test_manual_replay_uses_same_canonical_run_and_no_duplicate_action(self):
        workflow = self.saved()
        run, replayed = self.workflows.run(workflow["id"], "request-1")
        again, duplicate = self.workflows.run(workflow["id"], "request-1")
        self.assertFalse(replayed)
        self.assertTrue(duplicate)
        self.assertIs(run, again)
        self.assertEqual(len(self.runtime.calls), 1)
        self.assertEqual(self.runtime.calls[0][-1], {"output_schema": None, "output_format": "auto", "helper_model": workflow["helperModel"]})

    def test_manual_requires_request_key(self):
        workflow = self.saved()
        for key in (None, "", "bad key", "x" * 129):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.workflows.run(workflow["id"], key)
        self.assertEqual(self.runtime.calls, [])

    def test_due_claim_advances_atomically_before_runtime_create(self):
        workflow = self.scheduled()
        create = self.runtime.create
        def observe(*args, **kwargs):
            durable = self.workflows.get(workflow["id"])
            self.assertGreater(durable["nextAt"], workflow["nextAt"])
            self.assertEqual(durable["lastOutcome"], "claimed")
            return create(*args, **kwargs)
        self.runtime.create = observe
        updated = self.due(workflow)
        self.workflows.tick()
        self.assertEqual(len(self.runtime.calls), 1)
        self.assertEqual(updated["lastOutcome"], "started")
        self.assertEqual(updated["lastRunId"], self.runtime.calls and "000000000001")

    def test_busy_and_offline_slots_are_skipped_without_catchup(self):
        for code, expected in (("run_active", "skipped_busy"), ("device_busy", "skipped_busy"), ("device_unavailable", "skipped_offline")):
            workflow = self.scheduled()
            self.runtime.failure = APIError("Unavailable", code=code)
            updated = self.due(workflow)
            self.assertEqual(updated["lastOutcome"], expected)
            self.runtime.failure = None
            self.workflows.tick()
            self.assertEqual(self.runtime.calls, [])

    def test_late_tick_skips_missed_work_without_burst(self):
        workflow = self.scheduled()
        updated = self.due(workflow, late=7200)
        self.assertEqual(updated["lastOutcome"], "skipped_missed")
        self.assertGreater(updated["nextAt"], self.time * 1000)
        self.assertEqual(self.runtime.calls, [])

    def test_slow_first_dispatch_does_not_authorize_a_late_second_workflow(self):
        a, b = self.scheduled(), self.scheduled()
        create = self.runtime.create
        def slow_create(*args, **kwargs):
            run = create(*args, **kwargs)
            self.time += 61
            return run
        self.runtime.create = slow_create
        self.due(a)
        self.assertEqual(len(self.runtime.calls), 1)
        self.assertCountEqual([self.workflows.get(w["id"])["lastOutcome"] for w in (a, b)], ["started", "skipped_missed"])

    def test_restart_skips_even_recently_missed_slot(self):
        workflow = self.scheduled()
        self.time = workflow["nextAt"] / 1000 + 1
        self.workflows.recover()
        self.workflows.tick()
        self.assertEqual(self.workflows.get(workflow["id"])["lastOutcome"], "skipped_missed")
        self.assertEqual(self.runtime.calls, [])

    def test_uncertain_claim_is_not_replayed_after_recovery(self):
        workflow = self.saved()
        with patch.object(self.workflows, "_dispatch", side_effect=RuntimeError("simulated crash")):
            with self.assertRaises(RuntimeError):
                self.workflows.run(workflow["id"], "interrupted-request")
        self.workflows.recover()
        with self.assertRaises(APIError) as error:
            self.workflows.run(workflow["id"], "interrupted-request")
        self.assertEqual(error.exception.code, "workflow_dispatch_not_repeated")
        self.assertEqual(self.runtime.calls, [])

    def test_crash_after_run_commit_links_existing_run_without_reexecution(self):
        workflow = self.saved()
        with patch.object(self.workflows, "_outcome", side_effect=JournalError("commit failed")):
            with self.assertRaises(JournalError):
                self.workflows.run(workflow["id"], "commit-gap")
        self.assertEqual(len(self.runtime.calls), 1)
        self.workflows.recover()
        existing, replayed = self.workflows.run(workflow["id"], "commit-gap")
        self.assertTrue(replayed)
        self.assertEqual(existing.id, "000000000001")
        self.assertEqual(len(self.runtime.calls), 1)

    def test_pre_dispatch_persistence_failure_cannot_start_run(self):
        workflow = self.saved()
        with patch.object(self.journal, "transaction", side_effect=JournalError("unavailable")):
            with self.assertRaises(JournalError):
                self.workflows.run(workflow["id"], "blocked")
        self.assertEqual(self.runtime.calls, [])

    def test_completed_run_updates_outcome_without_repeating(self):
        workflow = self.saved()
        run, _ = self.workflows.run(workflow["id"], "done")
        run.status, run.finished_at = "expected_text_visible", self.time
        result = self.workflows.list()[0]
        self.assertEqual(result["lastOutcome"], "expected_text_visible")
        self.assertEqual(result["lastRunId"], run.id)
        self.assertEqual(len(self.runtime.calls), 1)

    def test_older_completion_does_not_overwrite_more_recent_skipped_attempt(self):
        workflow = self.saved()
        run, _ = self.workflows.run(workflow["id"], "first")
        self.runtime.failure = APIError("Busy", code="run_active")
        with self.assertRaises(APIError):
            self.workflows.run(workflow["id"], "second")
        run.status, run.finished_at = "expected_text_visible", self.time
        result = self.workflows.list()[0]
        self.assertEqual(result["lastOutcome"], "skipped_busy")
        self.assertEqual(result["lastRunId"], run.id)

    def test_global_daily_budget_is_enforced_across_workflows(self):
        self.workflows.daily_limit = 2
        a, b, c = [self.scheduled("0 9 * * *") for _ in range(3)]
        self.due(a)
        self.assertEqual(len(self.runtime.calls), 2)
        outcomes = [self.workflows.get(w["id"])["lastOutcome"] for w in (a, b, c)]
        self.assertEqual(outcomes.count("skipped_daily_limit"), 1)

    def test_concurrent_tick_claims_same_slot_once(self):
        workflow = self.scheduled()
        self.time = workflow["nextAt"] / 1000
        threads = [threading.Thread(target=self.workflows.tick) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(self.runtime.calls), 1)

    def test_scheduler_lifecycle_has_no_implicit_worker(self):
        scheduler = WorkflowScheduler(self.workflows)
        self.assertIsNone(scheduler.thread)
        self.assertTrue(scheduler.close())
        self.assertEqual(self.runtime.calls, [])

    def test_unexpected_scheduler_failure_stops_and_surfaces_error(self):
        scheduler = WorkflowScheduler(self.workflows)
        with patch.object(scheduler.stop, "wait", side_effect=[False, True]), patch.object(self.workflows, "tick", side_effect=RuntimeError("unexpected failure")):
            scheduler._work()
        self.assertTrue(scheduler.stop.is_set())
        self.assertIn("Scheduled tasks stopped", self.workflows.scheduler_error)
        self.assertEqual(self.runtime.calls, [])

    def test_failed_scheduler_thread_start_can_close_without_joining_unstarted_thread(self):
        scheduler = WorkflowScheduler(self.workflows)
        with patch("mobile_agent.workflow_scheduler.threading.Thread.start", side_effect=RuntimeError("resource limit")):
            with self.assertRaises(RuntimeError):
                scheduler.start()
        self.assertTrue(scheduler.close())
        self.assertIsNone(scheduler.thread)
        self.assertEqual(self.runtime.calls, [])

    def test_closed_scheduler_cannot_be_started(self):
        scheduler = WorkflowScheduler(self.workflows)
        scheduler.close()
        with self.assertRaises(RuntimeError):
            scheduler.start()

    def test_expired_dispatch_does_not_remain_permanently_started(self):
        workflow = self.saved()
        run, _ = self.workflows.run(workflow["id"], "old")
        del self.runtime.runs[run.id]
        self.assertEqual(self.workflows.list()[0]["lastOutcome"], "run_expired")
        with self.assertRaises(APIError) as error:
            self.workflows.run(workflow["id"], "old")
        self.assertEqual((error.exception.status, error.exception.code), (410, "run_expired"))
        self.assertEqual(len(self.runtime.calls), 1)

    def test_canonical_start_failure_keeps_the_existing_run_reference(self):
        workflow = self.saved()
        create = self.runtime.create
        def failed_start(*args, **kwargs):
            run, _ = create(*args, **kwargs)
            run.status, run.finished_at = "interrupted", self.time
            raise APIError("Worker start failed", 503, "start_failed")
        self.runtime.create = failed_start
        with self.assertRaises(APIError):
            self.workflows.run(workflow["id"], "failed-start")
        self.workflows.recover()
        run, replayed = self.workflows.run(workflow["id"], "failed-start")
        self.assertTrue(replayed)
        self.assertEqual(run.status, "interrupted")
        self.assertEqual(self.workflows.get(workflow["id"])["lastRunId"], run.id)
        self.assertEqual(len(self.runtime.calls), 1)

    def request(self, path, body, key=None):
        handler = object.__new__(make_handler(self.runtime))
        raw = json.dumps(body).encode()
        handler.headers = Message()
        handler.headers["Host"] = "127.0.0.1:8765"
        handler.headers["Content-Type"] = "application/json"
        handler.headers["Content-Length"] = str(len(raw))
        if key:
            handler.headers["Idempotency-Key"] = key
        handler.path, handler.rfile, handler.wfile = path, io.BytesIO(raw), io.BytesIO()
        statuses = []
        handler.send_response = statuses.append
        handler.send_header = lambda *_: None
        handler.end_headers = lambda: None
        handler.do_POST()
        return statuses[-1], json.loads(handler.wfile.getvalue())

    def test_http_workflow_contract_and_preview_never_dispatch(self):
        status, data = self.request("/api/workflows", {"name": "Version", "appId": "settings", "goal": "Read version"})
        self.assertEqual(status, 201)
        self.assertFalse(data["workflow"]["enabled"])
        status, data = self.request("/api/workflows/preview", {"cron": "0 9 * * *", "timezone": "UTC"})
        self.assertEqual(status, 200)
        self.assertEqual(len(data["next"]), 3)
        self.assertEqual(self.runtime.calls, [])

    def test_http_manual_run_is_explicit_idempotent_and_revision_safe(self):
        workflow = self.saved()
        path = f"/api/workflows/{workflow['id']}"
        status, data = self.request(path, {"revision": 99, "enabled": False})
        self.assertEqual((status, data["code"]), (409, "revision_conflict"))
        status, _ = self.request(path + "/run", {})
        self.assertEqual(status, 400)
        status, data = self.request(path + "/run", {}, "explicit")
        self.assertEqual(status, 201)
        status, duplicate = self.request(path + "/run", {}, "explicit")
        self.assertEqual(status, 200)
        self.assertEqual(data["run"]["id"], duplicate["run"]["id"])
        self.assertTrue(duplicate["replayed"])
        self.assertEqual(len(self.runtime.calls), 1)

    def test_http_post_start_persistence_failure_never_claims_no_task_started(self):
        workflow = self.saved()
        path = f"/api/workflows/{workflow['id']}/run"
        with patch.object(self.workflows, "_outcome", side_effect=JournalError("commit failed")):
            status, response = self.request(path, {}, "uncertain-http")
        self.assertEqual(status, 503)
        self.assertEqual(response["code"], "workflow_dispatch_uncertain")
        self.assertEqual(response["activeRunId"], "000000000001")
        self.assertIn("may already have started", response["error"])
        self.assertNotIn("No new task", response["error"])
        self.assertEqual(len(self.runtime.calls), 1)
        status, _ = self.request(path, {}, "uncertain-http")
        self.assertEqual(status, 409)
        self.assertEqual(len(self.runtime.calls), 1)
        self.workflows.recover()
        status, replay = self.request(path, {}, "uncertain-http")
        self.assertEqual(status, 200)
        self.assertEqual(replay["run"]["id"], response["activeRunId"])
        self.assertEqual(len(self.runtime.calls), 1)


if __name__ == "__main__":
    unittest.main()
