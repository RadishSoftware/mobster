"""One owner for the threads a run starts: a cancel token and a bounded pool of named workers."""

import logging
import threading
import time
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass
from enum import StrEnum

log = logging.getLogger("mobster.run_executor")

KEPT = 32  # finished workers ``workers()`` still names, newest last


class CancelToken:
    """Set once, never cleared. ``CancelToken(parent)`` is set when its parent is, and can be set alone. ``set``,
    ``is_set`` and ``wait`` are threading.Event's, so a run's Stop (``Run.stop``) is one: every ``run.stop.set()``
    reaches the tokens and executors made from it."""

    def __init__(self, parent=None):
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks = []
        if parent is not None:
            parent.on_cancel(self.set)

    def set(self):
        with self._lock:
            if self._event.is_set():
                return
            self._event.set()
            callbacks, self._callbacks = self._callbacks, []
        for fn in callbacks:
            _call(fn)

    def is_set(self):
        return self._event.is_set()

    def wait(self, timeout=None):
        return self._event.wait(timeout)

    def on_cancel(self, fn):
        """Call ``fn()`` once when the token is set, or now when it already is. It runs on the thread that sets the
        token, inside whatever locks that thread holds (Runtime.stop_run holds the runtime's and the run's), so it
        must be short and never wait."""
        with self._lock:
            if not self._event.is_set():
                self._callbacks.append(fn)
                return
        _call(fn)


def _call(fn):
    try:
        fn()
    except Exception:  # noqa: BLE001 -- a hook never reaches whoever pressed Stop
        log.exception("a cancel hook failed")


class Cancelled(RuntimeError):
    """``RunExecutor.submit`` after its token was set: the run is stopping, so nothing new starts."""


class State(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Worker:
    name: str
    state: State
    error: str = ""  # a failed worker's exception type


class _Job:
    __slots__ = ("name", "fn", "args", "future", "state", "thread")

    def __init__(self, name, fn, args):
        self.name, self.fn, self.args = name, fn, args
        self.future, self.state, self.thread = Future(), State.QUEUED, None


class RunExecutor:
    """The threads one run starts. At most ``max_workers`` named daemon workers run at once and the rest wait their
    turn. They all read one ``token``, a child of ``parent`` (the run's Stop): ``cancel()`` sets it, drops the work
    still waiting and refuses more, and ``join()`` waits for the work already running."""

    def __init__(self, parent=None, *, max_workers=4):
        if max_workers < 1:
            raise ValueError("A run executor needs at least one worker")
        self.token = CancelToken(parent)
        self._max = max_workers
        self._condition = threading.Condition()
        self._live = []  # queued and running jobs, oldest first
        self._finished = deque(maxlen=KEPT)
        self.token.on_cancel(self._drop_queued)

    def submit(self, name, fn, *args):
        """Run ``fn(*args)`` on a daemon thread named ``name`` and return its Future. What ``fn`` raises never
        reaches the caller: the Future holds it and ``workers()`` names the worker failed. Raises Cancelled once the
        token is set, and the thread's own error when no thread can be started."""
        job = _Job(name, fn, args)
        with self._condition:
            if self.token.is_set():
                raise Cancelled(f"{name} was not started: this run is stopping")
            self._live.append(job)
            if self._running() < self._max:
                try:
                    self._start(job)
                except BaseException:
                    self._live.remove(job)
                    raise
        return job.future

    def cancel(self):
        self.token.set()

    def join(self, timeout=None):
        """Wait until every worker has finished. False when one still runs after ``timeout`` (``workers()`` names
        it), and at once on one of this executor's own workers, which can't wait for itself."""
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._condition:
            while self._live:
                left = None if deadline is None else deadline - time.monotonic()
                if (left is not None and left <= 0) or self._owns(threading.current_thread()):
                    return False
                self._condition.wait(left)
            return True

    def in_worker(self):
        """True on one of this executor's worker threads."""
        with self._condition:
            return self._owns(threading.current_thread())

    def workers(self):
        """The last KEPT finished workers, then every queued and running one, oldest first."""
        with self._condition:
            return (*self._finished, *(Worker(job.name, job.state) for job in self._live))

    def _running(self):
        return sum(job.state is State.RUNNING for job in self._live)

    def _owns(self, thread):
        return any(job.thread is thread for job in self._live)

    def _start(self, job):
        job.state = State.RUNNING
        job.thread = threading.Thread(target=self._work, args=(job,), name=job.name, daemon=True)
        try:
            job.thread.start()
        except BaseException:
            job.state, job.thread = State.QUEUED, None
            raise

    def _work(self, job):
        state, error = State.CANCELLED, ""
        if job.future.set_running_or_notify_cancel():
            try:
                value = job.fn(*job.args)
            except BaseException as failure:  # noqa: BLE001 -- kept on its Future, never raised into the submitter
                log.warning("run worker %s failed", job.name, exc_info=True)
                job.future.set_exception(failure)
                state, error = State.FAILED, type(failure).__name__
            else:
                job.future.set_result(value)
                state = State.DONE
        with self._condition:
            self._retire(job, state, error)
            self._start_queued()
            self._condition.notify_all()

    def _start_queued(self):
        for job in [job for job in self._live if job.state is State.QUEUED]:
            if self.token.is_set() or self._running() >= self._max:
                return
            try:
                self._start(job)
            except Exception as failure:  # noqa: BLE001 -- no thread to be had: this job fails, the next still waits
                log.warning("run worker %s could not start", job.name, exc_info=True)
                if job.future.set_running_or_notify_cancel():
                    job.future.set_exception(failure)
                self._retire(job, State.FAILED, type(failure).__name__)

    def _drop_queued(self):
        with self._condition:
            for job in [job for job in self._live if job.state is State.QUEUED]:
                job.future.cancel()
                self._retire(job, State.CANCELLED)
            self._condition.notify_all()

    def _retire(self, job, state, error=""):
        """Move ``job`` to the finished list, dropping what it held: a parked worker must not keep a run alive."""
        self._live.remove(job)
        job.fn = job.args = job.thread = None
        self._finished.append(Worker(job.name, state, error))
