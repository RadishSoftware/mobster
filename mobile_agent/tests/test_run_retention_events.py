"""History retention and event publication preserve their durable ordering."""

import threading
import time
import unittest
from unittest.mock import Mock

from mobile_agent.journal import JournalError
from mobile_agent.run import Run
from mobile_agent.runtime_create import TaskStartMixin
from mobile_agent.tests.timing import bound

APP = {"id": "settings", "name": "Settings"}


class ResultHistoryTests(unittest.TestCase):
    def runtime(self):
        runtime = TaskStartMixin()
        runtime.plan_gate = None
        runtime.closing = False
        runtime.lock = threading.Lock()
        runtime.runs = {"active": object(), "expired": object()}
        runtime.journal = Mock()
        runtime.journal.lookup.return_value = None
        runtime.journal.create.return_value = ["expired"]
        runtime.frames = Mock(spec=["delete"])
        runtime.monthly_limit = Mock(return_value=None)
        runtime.publish = Mock()
        runtime.finished = Mock()
        return runtime

    def test_result_paths_prune_only_expired_history_and_frames(self):
        summary = {"status": "completed"}
        for method, argument in (("compose_result", {"summary": summary}),
                                 ("model_result", {"call": lambda emit: summary})):
            with self.subTest(method=method):
                runtime = self.runtime()
                active = runtime.runs["active"]
                run = getattr(runtime, method)(app=APP, goal="Result", idempotency_key="result", **argument)
                self.assertEqual(runtime.runs, {"active": active, run.id: run})
                runtime.frames.delete.assert_called_once_with("expired")
                self.assertEqual(run.status, "completed")

    def test_journal_refusal_does_not_prune_history_or_call_the_model(self):
        for method in ("compose_result", "model_result"):
            with self.subTest(method=method):
                runtime = self.runtime()
                before = dict(runtime.runs)
                call = Mock()
                argument = {"call": call} if method == "model_result" else {"summary": {"status": "completed"}}
                runtime.journal.create.side_effect = JournalError("disk unavailable")
                with self.assertRaises(JournalError):
                    getattr(runtime, method)(app=APP, goal="Result", idempotency_key="result", **argument)
                self.assertEqual(runtime.runs, before)
                runtime.frames.delete.assert_not_called()
                runtime.publish.assert_not_called()
                call.assert_not_called()


class RunEventOrderingTests(unittest.TestCase):
    def test_concurrent_events_keep_journal_history_and_listeners_in_order(self):
        journal, listeners = Mock(), Mock()
        saved = []

        def append(metadata, event):
            time.sleep(0)  # Yield inside persistence so a missing serialization lock is observable.
            saved.append(dict(event))

        journal.append.side_effect = append
        run = Run(APP, "Read", "live", journal=journal, listeners=listeners)
        start = threading.Barrier(3, timeout=bound(2))
        failures = []

        def emit():
            try:
                start.wait()
                for _ in range(10):
                    run.emit({"event": "observed"})
            except Exception as error:
                failures.append(error)

        threads = [threading.Thread(target=emit, daemon=True) for _ in range(2)]
        for thread in threads:
            thread.start()
        try:
            start.wait()
        finally:
            for thread in threads:
                thread.join(bound(2))
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        self.assertEqual([event["seq"] for event in saved], list(range(20)))
        self.assertEqual(saved, run.events)
        self.assertEqual([call.args[2] for call in listeners.post.call_args_list], saved)

    def test_journal_refusal_publishes_no_event_and_keeps_sequence_contiguous(self):
        journal, listeners = Mock(), Mock()
        run = Run(APP, "Read", "live", journal=journal, listeners=listeners)
        journal.append.side_effect = JournalError("disk unavailable")
        with self.assertRaises(JournalError):
            run.emit({"event": "action_started"})
        self.assertEqual(run.events, [])
        listeners.post.assert_not_called()
        journal.append.side_effect = None
        run.emit({"event": "observed"})
        self.assertEqual(run.events[0]["seq"], 0)
        listeners.post.assert_called_once_with("on_event", run, run.events[0])
