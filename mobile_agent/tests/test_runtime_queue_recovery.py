"""A waiting task never owns the phone, and unfinished task intent survives history trimming."""

from unittest.mock import patch

from mobile_agent.journal import Journal
from mobile_agent.tests.test_fleet import B_URL, SETTINGS, SIM_B
from mobile_agent.tests.test_phone_queue import Base
from mobile_agent.wda import watchdog as wda_watchdog
from mobile_agent.wda.watchdog import ActionTracker


class QueueRecoveryTests(Base):
    def test_active_run_survives_more_than_a_hundred_completed_runs(self):
        runtime = self.runtime()
        active, replayed = runtime.create("settings", "Keep the phone", "live", "active-retained", device=SIM_B)
        self.assertFalse(replayed)
        for index in range(101):
            runtime.compose_result(app=SETTINGS, goal=f"Completed {index}", summary={"status": "completed"},
                                   idempotency_key=None)
        self.assertIn(active.id, runtime.runs)
        self.assertIn(active.id, [item["id"] for item in runtime.journal.load()])
        self.assertEqual(runtime.journal.lookup("active-retained")[0], active.id)
        self.assertIs(runtime.stop_run(active.id), active)
        self.assertLessEqual(len(runtime.journal.load()), 102)  # insertion prunes before the newest run finishes

    def test_old_rows_without_a_trustworthy_terminal_marker_are_not_trimmed(self):
        journal = Journal()
        self.addCleanup(journal.close)
        for identifier, marker in (("missing", None), ("invalid", "yesterday")):
            metadata = {"id": identifier}
            if marker is not None:
                metadata["finishedAt"] = marker
            journal.create(metadata)
        for index in range(101):
            journal.create({"id": f"done-{index}", "finishedAt": index + 1})
        identifiers = {row[0] for row in journal.connection.execute("SELECT id FROM runs")}
        self.assertTrue({"missing", "invalid", "done-100"} <= identifiers)
        self.assertNotIn("done-0", identifiers)

    def test_expired_head_cannot_start_when_phone_frees_before_its_timer(self):
        runtime = self.runtime()
        holder = runtime.create("settings", "Hold", "live", device=SIM_B)
        old = runtime.create("settings", "Expired", "live", device=SIM_B, queue=True)
        self.now[0] += 20 * 60
        young = runtime.create("settings", "Still waiting", "live", device=SIM_B, queue=True)
        self.now[0] += 11 * 60
        self.end(runtime, holder)  # deliberately do not call sweep_queues
        self.assertEqual((old.status, old.summary["stop_code"]), ("error", "queue_expired"))
        self.assertFalse(old.summary["actions_may_have_run"])
        self.assertNotIn("dequeued", [event["event"] for event in old.events])
        self.assertEqual(list(runtime.active_runs), [young.id])
        self.assertEqual(runtime.queues.total(), 0)

    def test_a_queued_request_does_not_replace_or_clear_the_watchdog_holder(self):
        tracker = ActionTracker()
        with patch.object(wda_watchdog, "tracker", tracker):
            runtime = self.runtime()
            holder = runtime.create("settings", "Hold", "live", device=SIM_B)
            waiting = runtime.create("settings", "Wait", "live", device=SIM_B, queue=True)
            self.assertEqual(tracker.runs[B_URL], holder.id)
            token = tracker.begin(B_URL, "POST", "/wda/keys")
            tracker.end(token, "unknown")
            runtime.stop_run(waiting.id)
            tracker.run_ended(B_URL, waiting.id)
            self.assertEqual(tracker.runs[B_URL], holder.id)
            self.assertEqual(len(tracker.unknown_outcomes(B_URL)), 1)
            self.assertEqual(tracker.admit_restart(B_URL), "unknown_outcome")
            tracker.run_ended(B_URL, holder.id)
            self.assertEqual(tracker.unknown_outcomes(B_URL), [])

    def test_dequeued_request_takes_watchdog_ownership_only_after_lease(self):
        tracker = ActionTracker()
        with patch.object(wda_watchdog, "tracker", tracker):
            runtime = self.runtime()
            holder = runtime.create("settings", "Hold", "live", device=SIM_B)
            waiting = runtime.create("settings", "Wait", "live", device=SIM_B, queue=True)
            self.assertEqual(tracker.runs[B_URL], holder.id)
            tracker.run_ended(B_URL, holder.id)
            self.end(runtime, holder)
            self.assertEqual(tracker.runs[B_URL], waiting.id)
            self.assertEqual(list(runtime.active_runs), [waiting.id])

    def test_restart_blocked_at_dispatch_releases_queued_lease_without_an_action(self):
        tracker = ActionTracker()
        with patch.object(wda_watchdog, "tracker", tracker):
            runtime = self.runtime()
            holder = runtime.create("settings", "Hold", "live", device=SIM_B)
            waiting = runtime.create("settings", "Wait", "live", device=SIM_B, queue=True)
            tracker.run_ended(B_URL, holder.id)
            self.assertIsNone(tracker.admit_restart(B_URL))
            self.end(runtime, holder)
            self.assertEqual(waiting.status, "error")
            self.assertEqual(waiting.summary["stop_code"], "device_unavailable")
            self.assertFalse(waiting.summary["actions_may_have_run"])
            self.assertIsNone(waiting.lease)
            self.assertEqual(runtime.active_runs, {})
            self.assertEqual(tracker.runs, {})

    def test_worker_start_failure_retires_only_the_lease_holder(self):
        tracker = ActionTracker()
        with patch.object(wda_watchdog, "tracker", tracker):
            runtime = self.runtime()
            holder = runtime.create("settings", "Hold", "live", device=SIM_B)
            waiting = runtime.create("settings", "Wait", "live", device=SIM_B, queue=True)
            tracker.run_ended(B_URL, holder.id)
            with patch("mobile_agent.runtime_queue.threading.Thread", side_effect=RuntimeError("no worker")):
                self.end(runtime, holder)
            self.assertEqual(waiting.summary["stop_code"], "start_failed")
            self.assertIsNone(waiting.lease)
            self.assertEqual(tracker.runs, {})
