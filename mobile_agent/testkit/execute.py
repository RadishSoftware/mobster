"""One attempt of one check on one target: a verify run (verify/runner.py), exactly as `mobster verify` makes it,
plus what a suite needs around it: a stop that reaches runs on other threads, a simulator manager that allows
``--parallel`` simulators of one device type, and optional screen recordings.

A Smart step's approver is verify's ``check_approver``: on a real device it refuses every send, buy, post or delete,
on a simulator every purchase. It is never a person's approval (``run.request_approval``).
"""

from pathlib import Path
import signal
import subprocess
import threading

from ..verify.runner import CouldntRun, VerifyRun, resolve_mode

VIDEO_STOP_WAIT = 10.0


class Attempts:
    """Runs attempts. ``simulators()`` gives the shared simulator manager (made on first use); ``stop`` is the
    suite's stop flag; ``abort_all`` stops every run in flight."""

    def __init__(self, *, runs_dir, keyless=False, key=None, assert_timeout=5, parallel=1, progress=None,
                 stop=None, video_dir=None, simulators=None, devices=None, run_class=VerifyRun, popen=None):
        self.runs_dir = Path(runs_dir)
        self.keyless, self.key, self.assert_timeout = keyless, key, assert_timeout
        self.parallel = max(1, int(parallel))
        self.progress = progress or (lambda line: None)
        self.stop = stop or threading.Event()
        self.video_dir = Path(video_dir) if video_dir else None
        self._simulators = simulators
        self._devices = devices or _device_manager
        self.run_class = run_class
        self.popen = popen or subprocess.Popen
        self._live = set()
        self._lock = threading.Lock()

    # -- managers ----------------------------------------------------------------------------------------------

    def simulators(self):
        with self._lock:
            if self._simulators is None:
                self._simulators = suite_simulators(self.parallel, self.progress)
            return self._simulators

    def manager_for(self, record):
        """The manager a run uses: the shared simulator manager, pinned to a Mobster simulator a record names, or a
        WdaDevice for a real device."""
        if record is None:
            return self.simulators()
        if record.get("kind") == "simulator":
            from ..device_targets import PinnedSimulator
            return PinnedSimulator(self.simulators(), record["udid"])
        return self._devices(record)

    # -- one attempt -------------------------------------------------------------------------------------------

    def __call__(self, check, record, *, label, video_name=None):
        """The verify result of one attempt of ``check`` (on ``record``'s device, or a simulator)."""
        manager = self.manager_for(record)
        video = None
        if self.video_dir is not None and video_name and (record is None or record.get("kind") == "simulator"):
            video = self.video_dir / f"{video_name}.mp4"
            manager = Recording(manager, video, popen=self.popen, log=self.progress)
        mode = "launch" if self.keyless else resolve_mode(check, "auto")
        run = self.run_class(check, mode=mode, runs_dir=self.runs_dir, manager=manager,
                             progress=lambda line: self.progress(f"{label}: {line}"), key=self.key,
                             assert_timeout=self.assert_timeout)
        with self._lock:
            self._live.add(run)
        try:
            result = self._run(run, check, mode)
        finally:
            with self._lock:
                self._live.discard(run)
        if video is not None and video.is_file() and video.stat().st_size > 0:
            result["video"] = str(video)
        return result

    def _run(self, run, check, mode):
        if self.stop.is_set():
            return run.abort("stopped", "The suite was stopped before this ran.")
        if check.steps and mode == "launch":
            return run.abort("usage", "--keyless never calls a model, so a check with steps can't run.",
                             "Drop --keyless, or give the check no steps (launch-only).")
        if mode == "smart":
            from ..engines import smart_key
            from ..verify.runner import no_smart_key
            run.key = self.key or smart_key()
            if not run.key:
                why, fix = no_smart_key()
                return run.abort("usage", f"This check's steps need Smart, and {why}.", fix)
        try:
            run.prepare()
            if mode == "smart":
                run.run_smart(self.stop.is_set)
        except CouldntRun as error:
            if run.error is None:
                run._fail(error)
        except Exception as error:  # the run's own failure, never the suite's
            run.fail_internal(error)
        if self.stop.is_set() and run.result is None and run.error is None:
            run._fail(CouldntRun("stopped", "The suite was stopped."))
        return run.finish()

    def abort_all(self, message="The suite was stopped."):
        """Stop every run in flight: one still preparing stops at its next phase, a Smart run at its next step."""
        self.stop.set()
        with self._lock:
            live = list(self._live)
        for run in live:
            threading.Thread(target=_abort, args=(run, message), daemon=True, name="mobster-test-abort").start()


def _abort(run, message):
    try:
        if run.state == "preparing":
            run.abort("stopped", message)
    except Exception:
        pass


def _device_manager(record):
    from ..device_targets import WdaDevice
    return WdaDevice(record)


def suite_simulators(parallel, progress):
    """The simulator manager for a suite: MOBSTER_MAX_SIMS, raised to ``parallel`` so that many simulators of one
    device type can run checks at once. Each run holds its simulator's lease, so two runs never share one."""
    from ..sim import SimulatorManager

    class SuiteSimulators(SimulatorManager):
        @property
        def max_sims(self):
            return max(SimulatorManager.max_sims.fget(self), parallel)

    return SuiteSimulators(progress=progress)


class Recording:
    """A simulator manager that records the screen while each lease is held: `xcrun simctl io <udid> recordVideo`
    from acquire until the lease is released (SIGINT finishes the file). Everything else is the manager's."""

    def __init__(self, manager, path, *, popen=subprocess.Popen, log=None):
        self._manager, self.path, self._popen = manager, Path(path), popen
        self._log = log or (lambda line: None)

    def __getattr__(self, name):
        return getattr(self._manager, name)

    def acquire(self, *args, **kwargs):
        lease = self._manager.acquire(*args, **kwargs)
        process = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            process = self._popen(video_argv(lease.target.udid, self.path), stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except Exception as error:
            self._log(f"no screen recording ({type(error).__name__})")
        return RecordedLease(lease, process)


class RecordedLease:
    def __init__(self, lease, process):
        self._lease, self._process = lease, process
        self.target = lease.target

    def __getattr__(self, name):
        return getattr(self._lease, name)

    def release(self):
        process, self._process = self._process, None
        if process is not None:
            stop_recording(process)
        self._lease.release()


def video_argv(udid, path):
    return ["xcrun", "simctl", "io", str(udid), "recordVideo", "--codec=h264", "--force", str(path)]


def stop_recording(process, wait=VIDEO_STOP_WAIT):
    """SIGINT, which makes simctl finish the movie; a kill after ``wait`` seconds."""
    try:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=wait)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    except Exception:
        pass


__all__ = ["Attempts", "Recording", "suite_simulators", "video_argv", "stop_recording"]
