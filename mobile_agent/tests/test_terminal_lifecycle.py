"""Terminal journal limits and atomic publication; no device or provider calls."""

import io
from email.message import Message
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ..journal import EVENT_LIMIT, RUN_BYTE_LIMIT, Journal, JournalError, encode
from ..server import Run, Runtime, make_handler


class TerminalLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mobster-terminal-test-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "runs.sqlite3"
        self.journal = Journal(self.path)
        self.addCleanup(self.journal.close)
        self.run = Run({"id": "settings", "name": "Settings", "bundleId": "com.apple.Preferences"},
                       "Read the current page", "live", status="running", journal=self.journal)
        self.journal.create(self.run.metadata())
        self.run.emit({"event": "action_started", "operation": "TAP", "target": "observed-control"})

    def restart(self):
        self.journal.close()
        with patch("mobile_agent.server.resolve_wda_session", side_effect=AssertionError("No device calls")), \
                patch("mobile_agent.server.build_models", side_effect=AssertionError("No provider calls")):
            runtime = Runtime(SimpleNamespace(state_db=str(self.path)))
        self.addCleanup(runtime.close)
        return runtime

    def exhaust_events(self):
        while len(self.run.events) < EVENT_LIMIT:
            self.run.emit({"event": "observation"})

    def exhaust_bytes(self):
        used = self.journal.connection.execute("SELECT bytes FROM runs").fetchone()[0]
        while used < RUN_BYTE_LIMIT:
            event = {"event": "observation", "seq": len(self.run.events), "timestamp": 1, "text": ""}
            size = min(1_000_000, RUN_BYTE_LIMIT - used)
            event["text"] = "x" * (size - len(encode(event).encode()))
            self.journal.append(self.run.metadata(), event)
            self.run.events.append(event)
            used += size

    def assert_history_intact(self, run, prior_count):
        self.assertEqual(len(run.events), prior_count + 1)
        self.assertEqual([event["seq"] for event in run.events], list(range(prior_count + 1)))
        self.assertEqual(run.events[0]["event"], "action_started")
        self.assertEqual(run.events[-1]["event"], "run_finished")
        self.assertNotIn("persistence_failed", run.events[-1])
        self.assertIsNotNone(run.finished_at)

    def test_event_limit_keeps_reserved_terminal_capacity(self):
        self.exhaust_events()
        with self.assertRaises(JournalError):
            self.run.emit({"event": "observation"})
        self.assertEqual(len(self.run.events), EVENT_LIMIT)
        self.run.finish({"status": "error", "reason": "Event budget reached"})
        restored = self.restart().runs[self.run.id]
        self.assert_history_intact(restored, EVENT_LIMIT)
        self.assertEqual(restored.status, "error")

    def test_event_limit_crash_recovers_without_replaying_intent(self):
        self.exhaust_events()
        restored = self.restart().runs[self.run.id]
        self.assert_history_intact(restored, EVENT_LIMIT)
        self.assertEqual(restored.status, "interrupted")
        self.assertTrue(restored.summary["actions_may_have_run"])
        self.assertEqual(restored.summary["actions"], 0)

    def test_byte_limit_keeps_reserved_terminal_capacity(self):
        self.exhaust_bytes()
        count = len(self.run.events)
        with self.assertRaises(JournalError):
            self.run.emit({"event": "observation"})
        self.run.finish({"status": "error", "reason": "Byte budget reached"})
        restored = self.restart().runs[self.run.id]
        self.assert_history_intact(restored, count)
        self.assertEqual(restored.status, "error")

    def test_byte_limit_crash_recovers_without_losing_history(self):
        self.exhaust_bytes()
        count = len(self.run.events)
        restored = self.restart().runs[self.run.id]
        self.assert_history_intact(restored, count)
        self.assertEqual(restored.status, "interrupted")

    def test_failed_nonterminal_event_does_not_create_a_sequence_gap(self):
        with patch.object(self.journal, "append", side_effect=JournalError("Storage unavailable")):
            with self.assertRaises(JournalError):
                self.run.emit({"event": "action_acknowledged"})
        self.assertEqual(len(self.run.events), 1)
        self.run.finish({"status": "error", "reason": "Action outcome unknown"})
        self.assert_history_intact(self.restart().runs[self.run.id], 1)

    def test_failed_terminal_commit_is_explicit_and_rolls_back_both_writes(self):
        # Fail the metadata update after INSERT, proving the event is rolled back too.
        self.journal.connection.executescript("""
            CREATE TEMP TRIGGER reject_final BEFORE UPDATE ON runs BEGIN
                SELECT RAISE(ABORT, 'test storage failure');
            END;
        """)
        self.run.finish({"status": "error", "reason": "Task stopped"})
        self.assertTrue(self.run.summary["persistence_failed"])
        self.assertTrue(self.run.events[-1]["persistence_failed"])
        saved = self.journal.load()[0]
        self.assertIsNone(saved["finishedAt"])
        self.assertEqual(len(saved["events"]), 1)
        restored = self.restart().runs[self.run.id]
        self.assert_history_intact(restored, 1)
        self.assertEqual(restored.status, "interrupted")

    def test_finish_is_idempotent_and_no_event_can_follow_saved_terminal(self):
        self.run.finish({"status": "blocked"})
        self.run.finish({"status": "error"})
        self.assertEqual(self.run.status, "blocked")
        self.assertEqual(len(self.run.events), 2)
        with self.assertRaises(JournalError):
            self.run.emit({"event": "action_started"})
        self.assert_history_intact(self.restart().runs[self.run.id], 1)

    def test_recovery_storage_failure_stays_fail_closed_and_preserves_intent(self):
        self.journal.close()
        with patch.object(Journal, "finish", side_effect=JournalError("Storage unavailable")):
            with self.assertRaisesRegex(JournalError, "recovery could not be saved"):
                Runtime(SimpleNamespace(state_db=str(self.path)))
        # Failed initialization released the journal lease without discarding the pending intent.
        restored = self.restart().runs[self.run.id]
        self.assert_history_intact(restored, 1)
        self.assertTrue(restored.summary["actions_may_have_run"])

    def test_sse_observes_terminal_status_and_event_in_one_transition(self):
        entered, release = threading.Event(), threading.Event()
        original = self.journal.finish

        def slow_commit(metadata, event):
            entered.set()
            if not release.wait(2):
                raise AssertionError("Test did not release the commit")
            original(metadata, event)

        runtime = SimpleNamespace(config=SimpleNamespace(port=8765), runs={self.run.id: self.run},
                                  streams=threading.BoundedSemaphore(1))
        handler = object.__new__(make_handler(runtime))
        handler.path = f"/api/runs/{self.run.id}/events"
        handler.headers = Message()
        handler.headers["Host"] = "127.0.0.1:8765"
        handler.wfile = io.BytesIO()
        handler.send_response = lambda *_: None
        handler.send_header = lambda *_: None
        handler.end_headers = lambda: None
        with patch.object(self.journal, "finish", side_effect=slow_commit):
            finisher = threading.Thread(target=self.run.finish, args=({"status": "blocked"},))
            reader = threading.Thread(target=handler.do_GET)
            finisher.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertIsNone(self.run.finished_at)
                self.assertEqual(self.run.status, "running")
                acquired = self.run.condition.acquire(blocking=False)
                if acquired:
                    self.run.condition.release()
                self.assertFalse(acquired)
                reader.start()
            finally:
                release.set()
                finisher.join(2)
                if reader.ident is not None:
                    reader.join(2)
        self.assertFalse(finisher.is_alive())
        self.assertFalse(reader.is_alive())
        self.assertIn(b'"event": "run_finished"', handler.wfile.getvalue())
        self.assertIn(b'"status": "blocked"', handler.wfile.getvalue())
        self.assertTrue(runtime.streams.acquire(blocking=False))


if __name__ == "__main__":
    unittest.main()
