"""The run executor: one cancel token and a bounded pool of named workers per run. Offline, no sleeps."""

import threading
import unittest
from unittest.mock import patch

from mobile_agent.run_executor import Cancelled, CancelToken, RunExecutor, State
from mobile_agent.tests.timing import bound


def names(executor, state):
    return [worker.name for worker in executor.workers() if worker.state is state]


class CancelTokenTests(unittest.TestCase):
    def test_a_child_follows_its_parent_and_never_leads_it(self):
        parent = CancelToken()
        child, alone = CancelToken(parent), CancelToken(parent)
        alone.set()
        self.assertFalse(parent.is_set())
        parent.set()
        self.assertTrue(child.is_set() and child.wait(0))
        self.assertTrue(CancelToken(parent).is_set())  # made after the parent was set

    def test_repeated_set_calls_each_hook_once(self):
        token, calls = CancelToken(), []
        token.on_cancel(lambda: calls.append("early"))
        token.on_cancel(lambda: 1 / 0)  # a failing hook never reaches whoever pressed Stop
        with self.assertLogs("mobster.run_executor", "ERROR"):
            token.set()
        token.set()
        token.on_cancel(lambda: calls.append("late"))  # already set: runs at once
        self.assertEqual(calls, ["early", "late"])


class RunExecutorTests(unittest.TestCase):
    def test_cancel_before_submit_refuses_and_runs_nothing(self):
        executor, ran = RunExecutor(), []
        executor.cancel()
        with self.assertRaises(Cancelled):
            executor.submit("contract", ran.append, 1)
        self.assertEqual((ran, executor.workers()), ([], ()))
        self.assertTrue(executor.join(0))

    def test_cancel_during_work_sets_the_token_the_worker_sees(self):
        executor, started = RunExecutor(), threading.Event()

        def work():
            started.set()
            return executor.token.wait(bound(5)), threading.current_thread().name

        future = executor.submit("verify", work)
        self.assertTrue(started.wait(bound(5)))
        executor.cancel()
        self.assertEqual(future.result(bound(5)), (True, "verify"))
        self.assertTrue(executor.join(bound(5)))
        self.assertEqual(names(executor, State.DONE), ["verify"])

    def test_join_after_cancel_waits_for_running_work_drops_queued_work_and_names_stragglers(self):
        executor, release, ran = RunExecutor(max_workers=1), threading.Event(), []
        executor.submit("stuck", lambda: (ran.append("stuck"), release.wait(bound(5))))
        queued = executor.submit("prefetch", ran.append, "prefetch")
        self.assertEqual(names(executor, State.QUEUED), ["prefetch"])  # the pool is bounded
        executor.cancel()
        self.assertTrue(queued.cancelled())
        self.assertFalse(executor.join(0.01))
        self.assertEqual(names(executor, State.RUNNING), ["stuck"])
        release.set()
        self.assertTrue(executor.join(bound(5)))
        self.assertEqual(ran, ["stuck"])
        self.assertEqual([(w.name, w.state) for w in executor.workers()],
                         [("prefetch", State.CANCELLED), ("stuck", State.DONE)])

    def test_a_queued_worker_starts_when_a_running_one_finishes(self):
        executor, release = RunExecutor(max_workers=1), threading.Event()
        first = executor.submit("first", release.wait, bound(5))
        second = executor.submit("second", lambda: threading.current_thread().name)
        self.assertFalse(second.done())
        release.set()
        self.assertEqual((first.result(bound(5)), second.result(bound(5))), (True, "second"))
        self.assertTrue(executor.join(bound(5)))

    def test_stop_on_the_parent_cancels_the_executor(self):
        stop = CancelToken()
        executor = RunExecutor(stop)
        future = executor.submit("checkpoint", executor.token.wait, bound(5))
        stop.set()
        self.assertTrue(future.result(bound(5)))
        with self.assertRaises(Cancelled):
            executor.submit("late", print)
        executor.cancel()  # repeated cancel is idempotent
        executor.cancel()
        self.assertTrue(executor.join(bound(5)))

    def test_cancelling_the_executor_leaves_the_parent_alone(self):
        stop = CancelToken()
        RunExecutor(stop).cancel()  # a run's helpers end with it; that is not the person pressing Stop
        self.assertFalse(stop.is_set())

    def test_a_worker_that_raises_never_raises_into_the_caller(self):
        executor = RunExecutor()
        with self.assertLogs("mobster.run_executor", "WARNING"):
            future = executor.submit("compile", lambda: 1 / 0)
            self.assertTrue(executor.join(bound(5)))
        self.assertIsInstance(future.exception(), ZeroDivisionError)
        self.assertEqual(executor.workers()[0].error, "ZeroDivisionError")
        self.assertEqual(names(executor, State.FAILED), ["compile"])
        self.assertEqual(executor.submit("next", int, "7").result(bound(5)), 7)  # the pool keeps working

    def test_a_worker_cannot_wait_for_itself(self):
        executor = RunExecutor()
        inside = executor.submit("listeners", lambda: (executor.in_worker(), executor.join(bound(5))))
        self.assertEqual(inside.result(bound(5)), (True, False))
        self.assertFalse(executor.in_worker())

    def test_a_thread_that_cannot_start_leaves_nothing_behind(self):
        executor = RunExecutor()
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("can't start new thread")):
            with self.assertRaisesRegex(RuntimeError, "new thread"):
                executor.submit("listeners", print)
        self.assertEqual(executor.workers(), ())
        self.assertEqual(executor.submit("listeners", int, "1").result(bound(5)), 1)


if __name__ == "__main__":
    unittest.main()
