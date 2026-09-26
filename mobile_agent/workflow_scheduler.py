"""One bounded scheduler worker, started only by the actual API serve lifecycle."""

import threading

class WorkflowScheduler:
    def __init__(self, workflows):
        self.workflows = workflows
        self.stop = threading.Event()
        self.thread = None

    def start(self):
        if self.thread is not None or self.stop.is_set():
            raise RuntimeError("A scheduler instance can start only once")
        self.workflows.recover()
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
                self.workflows.scheduler_error = "Scheduled tasks stopped because durable scheduling is unavailable. Restart after resolving the storage or schedule error."
                self.stop.set()

    def close(self, timeout=25):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout)
            return not self.thread.is_alive()
        return True
