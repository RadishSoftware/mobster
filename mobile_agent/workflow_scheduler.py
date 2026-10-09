"""One bounded scheduler worker, started only by the actual API serve lifecycle.

It also keeps the Mac awake for schedules (KeepAwake): from 10 minutes before a scheduled task is due until its
run ends, it holds `caffeinate -i -w <this process>`, so idle sleep can't skip it. caffeinate exits by itself
when Mobster does. MOBSTER_KEEP_AWAKE=0 turns it off.
"""

import os
import shutil
import subprocess
import sys
import threading
import time

AWAKE_LEAD_MS = 10 * 60 * 1000
# How often the scheduler looks again at whether the Mac must stay awake.
AWAKE_CHECK_SECONDS = 10
CAFFEINATE = "/usr/bin/caffeinate"


class KeepAwake:
    """Holds one `caffeinate -i -w <pid>` while ``hold()`` was called last, none after ``release()``."""

    def __init__(self, *, spawn=None, enabled=None, pid=None, platform=sys.platform, exists=None):
        if enabled is None:
            enabled = os.environ.get("MOBSTER_KEEP_AWAKE", "1").strip().lower() not in ("0", "off", "false", "no")
        exists = exists or (lambda path: shutil.which(path) is not None)
        self.enabled = bool(enabled) and platform == "darwin" and exists(CAFFEINATE)
        self.spawn = spawn or (lambda argv: subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                             stderr=subprocess.DEVNULL, close_fds=True))
        self.pid = pid or os.getpid()
        self.process = None

    @property
    def held(self):
        return self.process is not None and self.process.poll() is None

    def hold(self):
        if not self.enabled or self.held:
            return
        try:
            self.process = self.spawn([CAFFEINATE, "-i", "-w", str(self.pid)])
        except OSError:
            self.process = None

    def release(self):
        process, self.process = self.process, None
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=2)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass


def awake_needed(workflows, now_ms, lead_ms=AWAKE_LEAD_MS):
    """Whether a scheduled task is due within ``lead_ms`` or a saved task's run is still going."""
    for workflow in workflows:
        due = workflow.get("nextAt")
        if workflow.get("enabled") and isinstance(due, (int, float)) and due - now_ms <= lead_ms:
            return True
        if workflow.get("lastOutcome") in ("claimed", "started"):
            return True
    return False


class WorkflowScheduler:
    def __init__(self, workflows, keep_awake=None, clock=time.monotonic):
        self.workflows = workflows
        self.stop = threading.Event()
        self.thread = None
        self.keep_awake = keep_awake if keep_awake is not None else KeepAwake()
        self.clock = clock
        self._awake_checked = None

    def start(self):
        if self.thread is not None or self.stop.is_set():
            raise RuntimeError("A scheduler instance can start only once")
        try:
            self.workflows.recover()
        except Exception:
            # Scheduling fails closed as in _work, but Mobster still starts: runs, setup and this error
            # (GET /workflows) stay reachable instead of the whole agent exiting.
            self._fail()
            return
        thread = threading.Thread(target=self._work, name="mobster-workflows", daemon=True)
        thread.start()
        self.thread = thread

    def _work(self):
        while not self.stop.wait(1):
            try:
                self.workflows.tick()
            except Exception:
                # Fail closed, surfaced by GET /workflows. Never retry a failed
                # durability boundary or run against a silently broken schedule.
                self._fail()
                continue
            self._stay_awake()
        self.keep_awake.release()

    def _stay_awake(self):
        """Hold or release the Mac's keep-awake, at most every AWAKE_CHECK_SECONDS. Never raises."""
        now = self.clock()
        if self._awake_checked is not None and now - self._awake_checked < AWAKE_CHECK_SECONDS:
            return
        self._awake_checked = now
        try:
            if awake_needed(self.workflows.list(), self.workflows.now()):
                self.keep_awake.hold()
            else:
                self.keep_awake.release()
        except Exception:
            pass

    def _fail(self):
        self.workflows.scheduler_error = "Scheduled tasks stopped because durable scheduling is unavailable. Restart after resolving the storage or schedule error."
        self.stop.set()

    def close(self, timeout=25):
        self.stop.set()
        try:
            if self.thread:
                self.thread.join(timeout)
                return not self.thread.is_alive()
            return True
        finally:
            self.keep_awake.release()
