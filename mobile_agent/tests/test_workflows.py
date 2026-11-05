import io
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from mobile_agent.api_errors import APIError
from mobile_agent.journal import Journal, JournalError, encode
from mobile_agent.server import make_handler
from mobile_agent.workflows import records as batch
from mobile_agent.workflows.cron import local_timezone, next_occurrences, preview, validate_cron
from mobile_agent.workflows.scheduler import WorkflowScheduler
from mobile_agent.workflows.service import Workflows


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
        for value in ("* * * * *", "*/5 * * * *", "0,1 9 * * *", "0 0 31 2 *", "0 0 * * * *", "@daily", "H 0 * * *",
                      "0 0 L * *", "0 0 * * 1#2", "0 0 1W * *", "0 9 * * nonsense", "0 0 * * * 0 2027"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_cron(value, "UTC")
        with self.assertRaises(ValueError):
            validate_cron("0 9 * * *", "not/a/timezone")

    def test_rate_proof_includes_midnight_gap(self):
        with self.assertRaisesRegex(ValueError, "five minutes"):
            validate_cron("0,59 0,23 * * *", "UTC")
        self.assertEqual(validate_cron("*/30 * * * *", "UTC"), "*/30 * * * *")

    def test_dst_occurrences_are_distinct_monotonic_utc_instants(self):
        now = datetime(2026, 11, 1, 0, 45, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp() * 1000
        for expression in ("30 * * * *", "30 */1 * * *"):
            with self.subTest(expression=expression):
                result = next_occurrences(expression, "America/Los_Angeles", now, 3)
                local = [datetime.fromtimestamp(value / 1000, ZoneInfo("America/Los_Angeles")) for value in result]
                self.assertEqual([(item.hour, item.minute, item.fold)
                                  for item in local], [(1, 30, 0), (1, 30, 1), (2, 30, 0)])
                self.assertEqual(result[1] - result[0], 3600_000)
                self.assertTrue(all(b > a for a, b in zip(result, result[1:])))

    def test_a_schedule_at_fixed_hours_runs_once_when_the_clock_goes_back(self):
        zone = ZoneInfo("America/Los_Angeles")
        now = datetime(2026, 10, 31, 12, tzinfo=zone).timestamp() * 1000
        result = next_occurrences("30 1 * * *", "America/Los_Angeles", now, 3)
        local = [datetime.fromtimestamp(value / 1000, zone) for value in result]
        self.assertEqual([(item.day, item.hour, item.minute, item.fold) for item in local],
                         [(1, 1, 30, 0), (2, 1, 30, 0), (3, 1, 30, 0)])
        between = datetime(2026, 11, 1, 1, 40, tzinfo=zone).timestamp() * 1000
        after = datetime.fromtimestamp(next_occurrences("30 1 * * *", "America/Los_Angeles", between)[0] / 1000, zone)
        self.assertEqual((after.day, after.hour, after.minute, after.fold), (2, 1, 30, 0))
        result = next_occurrences("0 1-2 * * *", "America/Los_Angeles", now, 3)
        local = [datetime.fromtimestamp(value / 1000, zone) for value in result]
        self.assertEqual([(item.day, item.hour, item.fold) for item in local], [(1, 1, 0), (1, 2, 0), (2, 1, 0)])

    def test_sparse_leap_day_is_bounded_and_supported(self):
        expression = validate_cron("0 9 29 2 *", "UTC")
        now = datetime(2026, 9, 19, tzinfo=timezone.utc).timestamp() * 1000
        result = next_occurrences(expression, "UTC", now)[0]
        self.assertEqual(datetime.fromtimestamp(result / 1000, timezone.utc).year, 2028)


class WorkflowCase(unittest.TestCase):
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
        return self.workflows.update(workflow["id"],
                                     {"revision": 1, "cron": expression, "timezone": "UTC", "enabled": True})

    def due(self, workflow, late=0):
        self.time = workflow["nextAt"] / 1000 + late
        self.workflows.tick()
        return self.workflows.get(workflow["id"])

    def request(self, path, body=None, key=None, method="POST"):
        handler = object.__new__(make_handler(self.runtime))
        raw = b"" if body is None else json.dumps(body).encode()
        handler.headers = Message()
        handler.headers["Host"] = "127.0.0.1:8765"
        if body is not None:
            handler.headers["Content-Type"] = "application/json"
            handler.headers["Content-Length"] = str(len(raw))
        if key:
            handler.headers["Idempotency-Key"] = key
        handler.path, handler.rfile, handler.wfile = path, io.BytesIO(raw), io.BytesIO()
        statuses = []
        handler.send_response = statuses.append
        handler.send_header = lambda *_: None
        handler.end_headers = lambda: None
        getattr(handler, f"do_{method}")()
        return statuses[-1], json.loads(handler.wfile.getvalue())


class WorkflowTests(WorkflowCase):
    def test_create_is_manual_paused_text_and_persistent(self):
        workflow = self.saved()
        self.assertEqual((workflow["enabled"], workflow["cron"], workflow["nextAt"], workflow["outputFormat"]),
                         (False, None, None, "auto"))
        self.workflows.tick()
        self.assertEqual(self.runtime.calls, [])
        restored = Workflows(self.runtime, lambda: self.time)
        self.assertEqual(restored.list(), [workflow])

    def test_a_task_saved_for_quick_mode_opens_and_runs_on_mobsters_agent(self):
        record = {"id": "0123456789ab", "name": "Read About", "appId": "settings", "appName": "Settings",
                  "goal": "Read iOS version", "outputSchema": None, "outputFormat": "auto",
                  "helperModel": "gemini-3.5-flash-lite", "engine": "fast", "device": None, "cron": None,
                  "timezone": "UTC", "enabled": False, "revision": 3, "createdAt": 1, "updatedAt": 1, "nextAt": None,
                  "lastRunId": None, "lastOutcome": None, "version": 2, "sameConversation": False,
                  "homeThreadId": None, "lastThreadId": None}
        with self.journal.transaction() as connection:
            connection.execute("INSERT INTO workflows VALUES (?,?)", (record["id"], encode(record)))
        opened = Workflows(self.runtime, lambda: self.time).get(record["id"])
        self.assertEqual((opened["engine"], opened["revision"]), ("smart", 3))
        self.assertNotIn("helperModel", opened)
        self.workflows.run(record["id"], "quick-mode-task")
        self.assertEqual(self.runtime.calls[-1][-1],
                         {"output_schema": None, "output_format": "auto", "engine": "smart", "origin": "workflow",
                          "_workflow_id": record["id"]})

    def test_a_saved_task_runs_on_the_ready_coding_agent_when_mobsters_agent_has_no_key(self):
        record = self.saved()
        self.runtime.plan_agent = lambda: "claude_code"
        self.workflows.run(record["id"], "plan-only")
        self.assertEqual(self.runtime.calls[-1][-1]["engine"], "claude_code")
        chosen = self.workflows.create({"name": "On smart", "appId": "settings", "goal": "Read iOS version",
                                        "engine": "smart"})
        self.workflows.run(chosen["id"], "keeps-its-engine")
        self.assertEqual(self.runtime.calls[-1][-1]["engine"], "smart")
        self.runtime.plan_agent = lambda: None
        self.workflows.run(record["id"], "no-agent")
        self.assertNotIn("engine", self.runtime.calls[-1][-1])

    def test_quick_modes_helper_model_is_not_a_saved_task_field(self):
        with self.assertRaises(ValueError):
            self.saved(helperModel="gemini-3.5-flash-lite")
        workflow = self.saved()
        with self.assertRaises(ValueError):
            self.workflows.update(workflow["id"], {"revision": 1, "helperModel": "gemini-3.7-flash"})
        self.assertEqual(self.workflows.list(), [workflow])

    def test_schema_validation_precedes_any_storage_or_dispatch(self):
        with self.assertRaises(ValueError):
            self.saved(outputSchema={"type": "object"})
        with self.assertRaises(ValueError):
            self.saved(appId="arbitrary-private-app")
        with self.assertRaises(ValueError) as caught:
            self.saved(appId="instagram-not-here", appName="Not here")
        self.assertIn("isn't on this Mac's list of apps yet", str(caught.exception))
        self.assertIn("choose Any app", str(caught.exception))
        self.assertNotIn("catalog", str(caught.exception))
        self.assertEqual(self.workflows.list(), [])
        # An app Mobster has a pack for may be named before a phone lists it (ICP-10): an unknown id still may not.
        self.assertEqual(self.saved(appId="com.burbn.instagram", appName="Instagram")["appId"], "com.burbn.instagram")
        self.assertEqual(self.runtime.calls, [])

    def test_invalid_engine_shapes_are_rejected_before_saving(self):
        for engine in ({}, [], True, 42, "turbo"):
            with self.subTest(engine=engine), self.assertRaisesRegex(ValueError, "engine"):
                self.saved(engine=engine)
        self.assertEqual(self.workflows.list(), [])

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
        self.assertEqual(self.runtime.calls[0][-1],
                         {"output_schema": None, "output_format": "auto", "origin": "workflow",
                          "_workflow_id": workflow["id"]})

    def test_workflow_identity_requires_a_durable_request_for_that_workflow(self):
        workflow, other = self.saved(), self.saved(name="Another")
        self.workflows.run(workflow["id"], "identity")
        key = self.runtime.calls[-1][3]
        self.assertTrue(self.workflows.owns_run_request(workflow["id"], key))
        self.assertFalse(self.workflows.owns_run_request(other["id"], key))
        self.assertFalse(self.workflows.owns_run_request(workflow["id"], key + ":unclaimed"))

        batch_key = f"workflow:{other['id']}:call:child"
        record = batch.new_record(other, batch_key, None, int(self.time * 1000))
        self.workflows.batches.add(record)
        node_key = f"{batch_key}:g{record['id']}:1"
        self.assertFalse(self.workflows.owns_run_request(other["id"], node_key))
        self.workflows.batches.save_node(record["id"], {"instance": "step-1", "seq": 1, "key": node_key})
        self.assertTrue(self.workflows.owns_run_request(other["id"], node_key))
        self.assertFalse(self.workflows.owns_run_request(workflow["id"], node_key))

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

    def test_a_refused_slot_says_why_and_is_skipped_without_catchup(self):
        for code, expected in (("run_active", "skipped_busy"), ("device_busy", "skipped_busy"),
                               ("device_unavailable", "skipped_offline"),
                               ("monthly_limit_reached", "skipped_monthly_limit"),
                               ("usage_unavailable", "dispatch_failed")):
            workflow = self.scheduled()
            self.runtime.failure = APIError("Refused", code=code)
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
        self.assertCountEqual([self.workflows.get(w["id"])["lastOutcome"]
                               for w in (a, b)], ["started", "skipped_missed"])

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

    def test_unexpected_scheduler_failure_stops_and_surfaces_error(self):
        scheduler = WorkflowScheduler(self.workflows)
        with patch.object(scheduler.stop, "wait", side_effect=[False, True]), \
                patch.object(self.workflows, "tick", side_effect=RuntimeError("unexpected failure")):
            scheduler._work()
        self.assertTrue(scheduler.stop.is_set())
        self.assertIn("Scheduled tasks stopped", self.workflows.scheduler_error)
        self.assertEqual(self.runtime.calls, [])

    def test_failed_scheduler_thread_start_can_close_without_joining_unstarted_thread(self):
        scheduler = WorkflowScheduler(self.workflows)
        with patch("mobile_agent.workflows.scheduler.threading.Thread.start",
                   side_effect=RuntimeError("resource limit")):
            with self.assertRaises(RuntimeError):
                scheduler.start()
        self.assertTrue(scheduler.close())
        self.assertIsNone(scheduler.thread)
        self.assertEqual(self.runtime.calls, [])

    def test_a_scheduler_starts_no_worker_until_started_and_never_after_close(self):
        scheduler = WorkflowScheduler(self.workflows)
        self.assertIsNone(scheduler.thread)
        self.assertTrue(scheduler.close())
        with self.assertRaises(RuntimeError):
            scheduler.start()
        self.assertEqual(self.runtime.calls, [])

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
        with self.assertRaises(APIError) as failed:
            self.workflows.run(workflow["id"], "failed-start")
        self.assertEqual(failed.exception.code, "start_failed")
        self.assertNotIn("started", failed.exception.details)   # its task was recorded: the outcome isn't known
        self.workflows.recover()
        run, replayed = self.workflows.run(workflow["id"], "failed-start")
        self.assertTrue(replayed)
        self.assertEqual(run.status, "interrupted")
        self.assertEqual(self.workflows.get(workflow["id"])["lastRunId"], run.id)
        self.assertEqual(len(self.runtime.calls), 1)

    def test_a_run_refused_before_its_task_is_recorded_says_nothing_started(self):
        workflow = self.saved()
        path = f"/api/workflows/{workflow['id']}/run"
        self.runtime.failure = APIError("Mobster's helper on your iPhone is restarting. No action was taken. Try "
                                        "again in a moment.", 503, "device_unavailable")
        status, data = self.request(path, {}, "helper-restarting")
        self.assertEqual((status, data["code"], data.get("started")), (503, "device_unavailable", False))
        self.assertEqual(self.workflows.get(workflow["id"])["lastOutcome"], "skipped_offline")
        self.runtime.failure = None
        status, data = self.request(path, {}, "started-before")
        self.assertEqual(status, 201, data)
        self.runtime.closing = True
        status, data = self.request(path, {}, "shutting-down")
        self.assertEqual((status, data["code"], data.get("started")), (503, "shutting_down", False))
        for key in ("started-before", "helper-restarting"):   # a key it has seen: never "nothing started"
            with self.subTest(key=key):
                status, data = self.request(path, {}, key)
                self.assertEqual((status, data["code"]), (503, "shutting_down"))
                self.assertNotIn("started", data)
        self.assertEqual(len(self.runtime.calls), 1)

    def test_a_local_error_after_canonical_creation_keeps_the_run_without_repeating_it(self):
        workflow = self.saved()
        create = self.runtime.create
        for error in (ValueError("Worker start failed"), JournalError("Worker storage failed")):
            with self.subTest(error=type(error).__name__):
                def failed_start(*args, **kwargs):
                    create(*args, **kwargs)
                    raise error
                self.runtime.create = failed_start
                key = type(error).__name__
                with self.assertRaises(type(error)):
                    self.workflows.run(workflow["id"], key)
                run, replayed = self.workflows.run(workflow["id"], key)
                self.assertTrue(replayed)
                self.assertEqual(self.workflows.get(workflow["id"])["lastRunId"], run.id)
        self.assertEqual(len(self.runtime.calls), 2)

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


BUILDER_CRONS = Path(__file__).resolve().parents[2] / "dashboard" / "src" / "test" / "schedule-crons.json"


class LocalTimeTests(WorkflowCase):
    def test_the_local_zone_comes_from_tz_then_etc_localtime(self):
        self.assertEqual(local_timezone({"TZ": "America/Chicago"}, "/nonexistent"), "America/Chicago")
        self.assertEqual(local_timezone({"TZ": ":Europe/Paris"}, "/nonexistent"), "Europe/Paris")
        with tempfile.TemporaryDirectory() as directory:
            zoneinfo = Path(directory) / "var" / "db" / "timezone" / "zoneinfo" / "Asia"
            zoneinfo.mkdir(parents=True)
            (zoneinfo / "Tokyo").write_bytes(b"")
            link = Path(directory) / "localtime"
            link.symlink_to(zoneinfo / "Tokyo")
            self.assertEqual(local_timezone({}, str(link)), "Asia/Tokyo")
            self.assertEqual(local_timezone({"TZ": "Not/AZone"}, str(link)), "Asia/Tokyo")
        self.assertEqual(local_timezone({}, "/nonexistent"), "UTC")

    def test_a_new_workflow_uses_the_macs_iana_zone(self):
        with patch.dict(os.environ, {"TZ": "America/Los_Angeles"}):
            workflow = self.saved()
        self.assertEqual(workflow["timezone"], "America/Los_Angeles")
        ZoneInfo(workflow["timezone"])
        self.assertEqual(self.saved(timezone="Europe/Berlin")["timezone"], "Europe/Berlin")

    def test_existing_workflows_keep_their_zone(self):
        with patch.dict(os.environ, {"TZ": "UTC"}):
            workflow = self.saved()
        with patch.dict(os.environ, {"TZ": "America/New_York"}):
            renamed = self.workflows.update(workflow["id"], {"revision": 1, "name": "Renamed"})
            self.assertEqual(Workflows(self.runtime, lambda: self.time).get(workflow["id"])["timezone"], "UTC")
        self.assertEqual(renamed["timezone"], "UTC")


class ScheduleBuilderTests(WorkflowCase):
    def cases(self):
        if not BUILDER_CRONS.exists():
            self.skipTest("the dashboard is not part of this checkout")
        return json.loads(BUILDER_CRONS.read_text())["cases"]

    def test_builder_crons_round_trip_through_the_service(self):
        for case in self.cases():
            with self.subTest(cron=case["cron"]):
                self.assertEqual(validate_cron(case["cron"], "America/Los_Angeles"), case["cron"])
                workflow = self.saved(cron=case["cron"], timezone="America/Los_Angeles", enabled=True)
                stored = self.workflows.get(workflow["id"])
                self.assertEqual((stored["cron"], stored["timezone"], stored["enabled"]),
                                 (case["cron"], "America/Los_Angeles", True))
                self.assertEqual(stored["nextAt"],
                                 next_occurrences(case["cron"], "America/Los_Angeles", round(self.time * 1000))[0])
                updated = self.workflows.update(workflow["id"], {"revision": 1, "cron": case["cron"], "enabled": False})
                self.assertEqual((updated["cron"], updated["nextAt"]), (case["cron"], None))

    def test_a_schedule_saved_at_creation_is_validated_like_an_update(self):
        with self.assertRaises(ValueError):
            self.saved(cron="* * * * *", timezone="UTC", enabled=True)
        with self.assertRaises(ValueError):
            self.saved(enabled=True)
        self.assertEqual(self.workflows.list(), [])
        self.assertEqual(self.runtime.calls, [])


class EngineTests(WorkflowCase):
    def test_engine_update_changes_later_runs(self):
        workflow = self.saved(engine="smart")
        self.assertEqual(Workflows(self.runtime, lambda: self.time).get(workflow["id"])["engine"], "smart")
        updated = self.workflows.update(workflow["id"], {"revision": 1, "engine": "fast"})
        self.assertEqual(updated["engine"], "smart")
        self.workflows.run(workflow["id"], "fast")
        self.assertEqual(self.runtime.calls[-1][-1]["engine"], "smart")
        cleared = self.workflows.update(workflow["id"], {"revision": 2, "engine": None})
        self.workflows.run(workflow["id"], "default")
        self.assertIsNone(cleared["engine"])
        self.assertNotIn("engine", self.runtime.calls[-1][-1])

    def test_a_task_starts_without_an_app_on_the_home_screen(self):
        workflow = self.workflows.create({"name": "Pizza", "goal": "Find a pizza place that is open now",
                                          "engine": "smart"})
        self.assertIsNone(workflow["appId"])
        self.workflows.run(workflow["id"], "home-screen")
        self.assertIsNone(self.runtime.calls[-1][0])
        plain = self.workflows.create({"name": "Pizza", "goal": "Find pizza", "appId": "any"})
        self.assertEqual((plain["appId"], plain["engine"]), (None, None))
        self.workflows.run(plain["id"], "home-screen-plain")
        self.assertIsNone(self.runtime.calls[-1][0])

    def test_http_new_workflow_with_schedule_and_engine(self):
        body = {"name": "Morning meeting", "goal": "Tell me my first meeting", "engine": "smart",
                "cron": "0 8 * * 1-5", "timezone": "America/Los_Angeles", "enabled": True,
                "outputFormat": "auto", "outputSchema": None}
        status, data = self.request("/api/workflows", body)
        self.assertEqual(status, 201)
        workflow = data["workflow"]
        self.assertEqual((workflow["appId"], workflow["engine"], workflow["cron"], workflow["enabled"]),
                         (None, "smart", "0 8 * * 1-5", True))
        self.assertIsNotNone(workflow["nextAt"])
        self.assertEqual(self.runtime.calls, [])


class EditAndDeleteTests(WorkflowCase):
    def dispatches(self, identifier):
        with self.journal.lock:
            return self.journal.connection.execute("SELECT count(*) FROM workflow_dispatches WHERE workflow_id=?",
                                                   (identifier,)).fetchone()[0]

    def test_delete_removes_the_workflow_and_its_dispatch_rows(self):
        workflow = self.saved()
        run, _ = self.workflows.run(workflow["id"], "before-delete")
        self.assertEqual(self.dispatches(workflow["id"]), 1)
        self.assertEqual(self.workflows.delete(workflow["id"]), workflow["id"])
        self.assertEqual(self.dispatches(workflow["id"]), 0)
        self.assertEqual(self.workflows.list(), [])
        with self.assertRaises(APIError) as missing:
            self.workflows.get(workflow["id"])
        self.assertEqual(missing.exception.status, 404)
        self.assertIn(run.id, self.runtime.runs)
        self.assertEqual(Workflows(self.runtime, lambda: self.time).list(), [])

    def test_a_deleted_schedule_never_fires(self):
        workflow = self.scheduled()
        self.workflows.delete(workflow["id"])
        self.time = workflow["nextAt"] / 1000
        self.workflows.tick()
        self.assertEqual(self.runtime.calls, [])

    def test_http_delete_route(self):
        workflow = self.saved()
        status, data = self.request(f"/api/workflows/{workflow['id']}", method="DELETE")
        self.assertEqual((status, data), (200, {"deleted": workflow["id"]}))
        status, data = self.request(f"/api/workflows/{workflow['id']}", method="DELETE")
        self.assertEqual((status, data["code"]), (404, "not_found"))
        status, _ = self.request("/api/workflows/not-an-id", method="DELETE")
        self.assertEqual(status, 404)
        self.assertEqual(self.runtime.calls, [])

    def test_update_changes_the_goal_for_later_runs(self):
        workflow = self.saved()
        updated = self.workflows.update(workflow["id"], {"revision": 1, "goal": "  Read the model name  "})
        self.assertEqual((updated["goal"], updated["revision"]), ("Read the model name", 2))
        self.workflows.run(workflow["id"], "edited")
        self.assertEqual(self.runtime.calls[-1][1], "Read the model name")

    def test_an_empty_or_oversized_goal_is_rejected_without_saving(self):
        workflow = self.saved()
        for goal in ("", "   ", "x" * 4001, None, 7):
            with self.subTest(goal=goal), self.assertRaisesRegex(ValueError, "1–4000"):
                self.workflows.update(workflow["id"], {"revision": 1, "goal": goal})
        self.assertEqual(self.workflows.get(workflow["id"]), workflow)

    def test_update_changes_the_app_and_checks_it(self):
        workflow = self.saved()
        updated = self.workflows.update(workflow["id"], {"revision": 1, "appId": "notes"})
        self.assertEqual((updated["appId"], updated["appName"]), ("notes", "Notes"))
        with self.assertRaisesRegex(ValueError, "choose Any app"):
            self.workflows.update(workflow["id"], {"revision": 2, "appId": "not-an-app"})
        home = self.workflows.update(workflow["id"], {"revision": 2, "appId": None})
        self.assertEqual((home["appId"], home["appName"], home["engine"]), (None, None, None))

    def test_http_update_accepts_goal_and_app(self):
        workflow = self.saved()
        status, data = self.request(f"/api/workflows/{workflow['id']}",
                                    {"revision": 1, "goal": "Read the build number", "appId": "notes"})
        self.assertEqual(status, 200)
        self.assertEqual((data["workflow"]["goal"], data["workflow"]["appId"]), ("Read the build number", "notes"))
        status, data = self.request(f"/api/workflows/{workflow['id']}", {"revision": 2, "goal": ""})
        self.assertEqual(status, 400)

    def test_app_name_is_filled_on_create_and_kept_for_phone_apps(self):
        self.assertEqual(self.saved()["appName"], "Settings")
        self.runtime.apps = lambda: [{"id": "com.strava.stravaride", "name": "Strava"}]
        strava = self.saved(appId="com.strava.stravaride")
        self.assertEqual(strava["appName"], "Strava")
        self.runtime.apps = lambda: []
        self.assertEqual(self.workflows.get(strava["id"])["appName"], "Strava")
        renamed = self.workflows.update(strava["id"], {"revision": 1, "name": "Weekly runs"})
        self.assertEqual(renamed["appName"], "Strava")
        self.assertEqual(self.saved(appName="Settings app")["appName"], "Settings app")
        with self.assertRaises(ValueError):
            self.saved(appName="")
        home = self.workflows.create({"name": "Pizza", "goal": "Find pizza", "engine": "smart", "appName": "Ignored"})
        self.assertIsNone(home["appName"])

    def test_rows_saved_before_app_names_still_load(self):
        workflow = self.saved()
        legacy = {key: value for key, value in workflow.items() if key != "appName"}
        with self.journal.transaction() as connection:
            connection.execute("UPDATE workflows SET data=? WHERE id=?", (json.dumps(legacy), workflow["id"]))
        self.assertNotIn("appName", self.workflows.get(workflow["id"]))
        renamed = self.workflows.update(workflow["id"], {"revision": 1, "name": "Renamed"})
        self.assertEqual(renamed["name"], "Renamed")
        self.workflows.run(workflow["id"], "legacy")
        self.assertEqual(len(self.runtime.calls), 1)


if __name__ == "__main__":
    unittest.main()
