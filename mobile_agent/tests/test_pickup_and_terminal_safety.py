"""Durable pickup boundaries, using only an in-memory journal."""

import threading
import unittest
from types import SimpleNamespace

from mobile_agent.api_errors import APIError
from mobile_agent.harness import checkpoints
from mobile_agent.journal import Journal
from mobile_agent.run import Run

APP = {"id": "com.example.app", "name": "Example", "bundleId": "com.example.app"}


class JournalRunTests(unittest.TestCase):
    def setUp(self):
        self.journal = Journal(None)
        self.addCleanup(self.journal.close)
        with self.journal.transaction() as connection:
            checkpoints.migrate(connection, 0)
        self.run = Run(dict(APP), "Open Example", "live", journal=self.journal, engine="smart", origin="app")
        self.journal.create(self.run.metadata())
        self.created = []
        self.runtime = SimpleNamespace(lock=threading.RLock(), runs={self.run.id: self.run}, journal=self.journal,
                                       create=self._create)

    def _create(self, *args, **kwargs):
        self.created.append((args, kwargs))
        return "created"

    def checkpoint(self):
        self.assertTrue(checkpoints.save(self.journal, self.run.id, {"step": 1, "history": ["0: read"]}))

    def test_unacknowledged_gesture_refuses_each_pickup_admission_before_phone_use(self):
        self.run.emit({"event": "action_started", "operation": "DRAG_XY", "target": "com.example.app"})
        self.run.finish({"event": "result", "status": "interrupted", "answer": None})
        self.checkpoint()
        with self.assertRaises(APIError) as failure:
            checkpoints.check(self.runtime, self.run.id)
        self.assertEqual(failure.exception.code, "pickup_outcome_unknown")
        with self.assertRaises(APIError):
            checkpoints.pick_up(self.runtime, self.run.id)
        self.assertEqual(self.created, [])
        emitted = []
        context = SimpleNamespace(extras={"resumeFrom": self.run.id}, runtime=self.runtime,
                                  run_id="b" * 12, emit=emitted.append)
        result = checkpoints.pre_run(context, None, None)
        self.assertEqual(result.result["status"], "blocked")
        self.assertEqual(result.result["stop_code"], "pickup_outcome_unknown")
        self.assertEqual(self.created, [])

    def test_terminal_unknown_refuses_even_after_a_gesture_acknowledgment(self):
        self.run.emit({"event": "action_started", "operation": "DRAG_XY"})
        self.run.emit({"event": "action_acknowledged", "operation": "DRAG_XY"})
        self.run.finish({"event": "result", "status": "stopped", "code": "unknown_outcome"})
        self.checkpoint()
        with self.assertRaises(APIError) as failure:
            checkpoints.check(self.runtime, self.run.id)
        self.assertEqual(failure.exception.code, "pickup_outcome_unknown")

    def test_known_completed_and_not_dispatched_actions_allow_safe_pickup(self):
        self.run.emit({"event": "app_launch_started", "app": "Example"})
        self.run.emit({"event": "app_launch_acknowledged", "app": "Example"})
        self.run.emit({"event": "action_started", "operation": "DRAG_XY"})
        self.run.emit({"event": "action_acknowledged", "operation": "DRAG_XY"})
        self.run.emit({"event": "action_started", "operation": "SWIPE_UP"})
        self.run.emit({"event": "action_not_dispatched", "operation": "SWIPE_UP"})
        self.run.finish({"event": "result", "status": "interrupted", "answer": None})
        self.checkpoint()
        self.assertIs(checkpoints.check(self.runtime, self.run.id), self.run)
        self.assertEqual(checkpoints.pick_up(self.runtime, self.run.id), ("created", False))
        self.assertEqual(len(self.created), 1)

    def test_unacknowledged_app_launch_refuses_pickup(self):
        self.run.emit({"event": "app_launch_started", "app": "Example"})
        self.run.finish({"event": "result", "status": "interrupted"})
        self.checkpoint()
        with self.assertRaises(APIError) as failure:
            checkpoints.check(self.runtime, self.run.id)
        self.assertEqual(failure.exception.code, "pickup_outcome_unknown")

    def test_a_failed_terminal_persistence_record_is_not_a_pickup_proof(self):
        self.run.finish({"event": "result", "status": "error", "persistence_failed": True})
        self.checkpoint()
        with self.assertRaises(APIError) as failure:
            checkpoints.check(self.runtime, self.run.id)
        self.assertEqual(failure.exception.code, "pickup_outcome_unknown")


if __name__ == "__main__":
    unittest.main()
