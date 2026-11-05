"""A Network X-ray the Mac app runs. A job stops its capture tool on Stop, on shutdown and on any error."""

import logging
import threading
import time
import uuid

from ..api_errors import APIError
from ..harness_api import STAY_ON_MAC
from . import analyze, attribution, capture, store, summary, target

log = logging.getLogger("mobster.xray")
MAX_JOBS = 20
DEFAULT_SECONDS, MIN_SECONDS, MAX_SECONDS = 60, 10, 600
LIVE_EVERY = 2.0
SHUTDOWN_WAIT = 0.5
STATES = ("recording", "analyzing", "done", "failed", "stopped")
ACTIVE = ("recording", "analyzing")
BUSY = "Mobster is already recording this iPhone's network traffic. Wait for it to finish, or stop it."
APP_NAME = "Network X-ray"


class Job:
    def __init__(self, phone, seconds, keep=False):
        self.id = uuid.uuid4().hex[:12]
        self.phone, self.seconds, self.keep = phone, seconds, keep
        self.state = "recording"
        self.started = time.monotonic()
        self.ended = None
        self.live = summary.live({"apps": [], "local": {"packets": 0, "bytes": 0}, "capture": {"packets": 0}})
        self.error = None
        self.run_id = None
        self.early = False
        self.stop_requested = threading.Event()
        self.recording = None
        self.thread = None
        self.names = attribution.Names()
        self.folder = None
        self.lock = threading.Lock()

    def view(self):
        """GET /api/xray/network/{jobId}."""
        with self.lock:
            elapsed = (self.ended or time.monotonic()) - self.started
            out = {"state": self.state, "elapsed": round(elapsed, 1), "seconds": self.seconds,
                   "device": {"id": self.phone.id, "name": self.phone.name}, "live": self.live}
            if self.run_id:
                out["runId"] = self.run_id
                out["early"] = self.early
            if self.error:
                out["error"] = dict(self.error)
            return out

    def _set(self, state, **fields):
        with self.lock:
            self.state = state
            if state not in ACTIVE:
                self.ended = time.monotonic()
            for key, value in fields.items():
                setattr(self, key, value)


class Board:
    """The runtime's Network X-ray jobs."""

    def __init__(self, runtime, *, recorder=capture.Recording, namer=target.phone_names, folder=store.new_folder,
                 every=LIVE_EVERY):
        self.runtime, self.recorder, self.namer, self.new_folder, self.every = runtime, recorder, namer, folder, every
        self.jobs = {}
        self.keys = {}
        self.lock = threading.Lock()
        self.closed = False

    def start(self, phone, seconds, *, keep=False, key=None):
        """A new Job recording ``phone``. APIError 409 xray_busy while another capture of that phone runs."""
        with self.lock:
            if self.closed:
                raise APIError("The service is shutting down", 503, "shutting_down")
            if key and key in self.keys and self.keys[key] in self.jobs:
                return self.jobs[self.keys[key]]
            if any(job.phone.udid == phone.udid and job.state in ACTIVE for job in self.jobs.values()):
                raise APIError(BUSY, 409, "xray_busy")
            job = Job(phone, seconds, keep)
            try:
                job.folder = self.new_folder()
                job.recording = self.recorder(phone.udid, seconds, store.capture_path(job.folder),
                                              network=phone.network)
            except capture.CaptureError as error:
                self._discard(job)
                raise APIError(str(error), 503, "xray_unavailable") from None
            except Exception:
                self._discard(job)
                raise
            self.jobs[job.id] = job
            if key:
                self.keys[key] = job.id
            self._trim()
        job.thread = threading.Thread(target=self._work, args=(job,), name=f"mobster-xray-{job.id}", daemon=True)
        job.thread.start()
        return job

    def get(self, job_id):
        with self.lock:
            return self.jobs.get(job_id)

    def stop(self, job_id):
        """Ask a recording job to end early; its analysis and its task still happen."""
        job = self.get(job_id)
        if job is None:
            raise APIError("There is no such X-ray", 404, "xray_not_found")
        if job.state == "recording":
            job.stop_requested.set()
        return job

    def close(self):
        """Shutdown: stop every capture tool, wait for the threads, and drop the jobs."""
        with self.lock:
            self.closed = True
            jobs = list(self.jobs.values())
        for job in jobs:
            job.stop_requested.set()
            if job.recording is not None:
                job.recording.stop(wait=SHUTDOWN_WAIT)   # a service closes in about 2 s: kill what doesn't flush
        for job in jobs:
            if job.thread is not None and job.thread is not threading.current_thread():
                job.thread.join(timeout=SHUTDOWN_WAIT)
        with self.lock:
            self.jobs.clear()
            self.keys.clear()

    def _trim(self):
        """Keep the last MAX_JOBS jobs; an active one is never dropped."""
        finished = [job for job in self.jobs.values() if job.state not in ACTIVE]
        for job in finished[:max(0, len(self.jobs) - MAX_JOBS)]:
            del self.jobs[job.id]
        self.keys = {key: job_id for key, job_id in self.keys.items() if job_id in self.jobs}

    @staticmethod
    def _discard(job):
        if job.folder is not None:
            store.discard_capture(job.folder)
            try:
                job.folder.rmdir()
            except OSError:
                pass

    def _read_names(self, job):
        try:
            job.names = self.namer(job.phone)
        except Exception:    # no app list: the report names processes
            job.names = attribution.Names()

    def _work(self, job):
        namer = threading.Thread(target=self._read_names, args=(job,), name=f"mobster-xray-names-{job.id}", daemon=True)
        namer.start()
        try:
            self._record(job)
            if self.closed:
                job._set("stopped")
                return
            job._set("analyzing")
            job.recording.stop()
            try:
                job.recording.finish()
            except capture.CaptureError:
                if not job.stop_requested.is_set():
                    raise
                job._set("stopped")      # Stop before the phone sent anything: there is nothing to report
                return
            namer.join(timeout=30)
            result, _paths = store.conclude(job.folder, job.names, keep=job.keep)
            run = self._compose(job, result)
            job.live = summary.live(result)
            job._set("done", run_id=run.id, early=job.stop_requested.is_set())
        except capture.CaptureError as error:
            job._set("failed", error={"message": str(error), "code": "xray_capture"})
        except APIError as error:
            job._set("failed", error={"message": str(error), "code": error.code})
        except Exception as error:   # noqa: BLE001 - a job ends in a state; the cause goes to the log by type only
            log.warning("network x-ray failed (%s)", type(error).__name__)
            job._set("failed", error={"message": "Mobster couldn't read the capture, so nothing was saved. Try again.",
                                      "code": "xray_failed"})
        finally:
            job.recording.stop()
            if not job.keep:
                store.discard_capture(job.folder)

    def _record(self, job):
        """Record until the time is up, Stop, shutdown or the tool exits, reading the growing file every few seconds."""
        deadline = job.started + job.seconds
        path = store.capture_path(job.folder)
        pause = self.every
        while job.recording.running() and time.monotonic() < deadline:
            if job.stop_requested.wait(min(pause, max(0.0, deadline - time.monotonic()))):
                return
            if path.exists() and path.stat().st_size:
                began = time.monotonic()
                try:
                    job.live = summary.live(analyze.analyze_file(path, names=job.names))
                except (OSError, ValueError):    # a block still being written: the next reading has it
                    pass
                pause = max(self.every, 2 * (time.monotonic() - began))   # a long capture is read less often

    def _compose(self, job, result):
        from ..runtime_create import ANY_APP
        paths_stamp = job.folder.name
        text = summary.answer(result)
        proof = {"quote": "Read your iPhone's network traffic: hostnames, sizes and times. Nothing was decrypted.",
                 "app": APP_NAME}
        blocks = summary.blocks(result, paths_stamp, job.keep)
        envelope = {"v": 1, "blocks": blocks}
        done = {"event": "result", "status": "completed", "outcome": "done", "answer": text, "reason": None,
                "independently_verified": True, "actions": 0, "decisions": 0, "elapsed_ms": 0, "model": None,
                "proof": [proof], "blocks": envelope}
        return self.runtime.compose_result(app=dict(ANY_APP),
                                           goal=f"Network X-ray of {job.phone.name} for {job.seconds} seconds",
                                           summary=done, idempotency_key=f"xray-network-{job.id}", origin="app",
                                           extras={STAY_ON_MAC: True},
                                           device=(job.phone.id, job.phone.name))
