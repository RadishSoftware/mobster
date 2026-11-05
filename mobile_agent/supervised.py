"""One external process kept alive until stopped, and the process groups it and the build steps run in."""

import atexit
import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from .env_file import open_private, private_dir

RESTART_DELAY = 2.0
MAX_RESTART_DELAY = 30.0
# Inside the Mac app's SIDECAR_GRACE (desktop/src-tauri/src/sidecar.rs): an agent killed first leaves them running.
TERM_GRACE = 3.0
HEALTHY_RUN_SECONDS = 60.0
HOLD_POLL = 2.0


class Supervised:
    def __init__(self, name, command, log_path, env=None, gate=None, hold=None):
        # gate(): before each relaunch, a release callable or None to wait; hold(): a reason not to launch yet or None.
        self.name, self.command, self.log_path, self.env, self.gate, self.hold = (
            name, command, log_path, env, gate, hold)
        self.held = None
        self.launched_at = None
        self.process = None
        self.stopping = threading.Event()
        self.thread = None
        self.restarts = 0
        self.failures = 0
        self.last_exit = None
        self.pid_path = Path(str(log_path) + ".pid")
        # A caller waits on these events themselves, not on a clock that says they should have happened.
        self.launches = 0
        self.progress = threading.Condition()

    def wait_for(self, condition, timeout=None):
        # timeout guards against a hung supervisor; it never decides a result.
        with self.progress:
            return self.progress.wait_for(lambda: condition(self), timeout)

    def _progressed(self):
        with self.progress:
            self.progress.notify_all()

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    @property
    def alive(self):
        process = self.process
        return process is not None and process.poll() is None

    def start(self):
        if self.running:
            return
        self.stopping.clear()
        self.thread = threading.Thread(target=self._loop, name=f"mobster-{self.name}", daemon=True)
        self.thread.start()

    def _admitted(self):
        while not self.stopping.is_set():
            release = self.gate() if self.gate is not None else (lambda: None)
            if release is not None:
                return release
            self.stopping.wait(RESTART_DELAY)
        return None

    def _hold_reason(self):
        try:
            reason = self.hold() if self.hold is not None else None
        except Exception:  # noqa: BLE001
            return None
        return reason if isinstance(reason, str) and reason else None

    def _clear_to_launch(self):
        while not self.stopping.is_set():
            self.held = self._hold_reason()
            if self.held is None:
                return True
            self.stopping.wait(HOLD_POLL)
        return False

    def _loop(self):
        private_dir(Path(self.log_path).parent)
        reap_leftover(self.pid_path)
        relaunch = False
        while not self.stopping.is_set():
            if not self._clear_to_launch():
                return
            release = None
            if relaunch:
                release = self._admitted()
                if release is None:
                    return
            relaunch = True
            with open_private(self.log_path, "ab") as log:
                try:
                    self.process = subprocess.Popen(self.command, stdout=log, stderr=subprocess.STDOUT,
                                                    stdin=subprocess.DEVNULL, start_new_session=True, env=self.env)
                except OSError as error:
                    if release is not None:
                        release()
                    self.last_exit = f"could not start: {error}"
                    self.stopping.wait(RESTART_DELAY * 5)
                    continue
                started = self.launched_at = time.monotonic()
                record(self.pid_path, self.process.pid, self.command)
                if release is not None:
                    release()
                self.launches += 1               # counted once the child is recorded and the gate released
                self._progressed()
                while self.process.poll() is None and not self.stopping.is_set():
                    self.stopping.wait(.5)
                if self.stopping.is_set():
                    self._terminate()
                    return
                self.last_exit, self.process = self.process.returncode, None
                forget(self.pid_path)
                self.restarts += 1
                self._progressed()
            # Back off while it keeps failing fast (an unplugged or locked phone).
            self.failures = 0 if time.monotonic() - started >= HEALTHY_RUN_SECONDS else self.failures + 1
            self.stopping.wait(min(MAX_RESTART_DELAY, RESTART_DELAY * 2 ** max(0, self.failures - 1)))

    def _terminate(self):
        process = self.process
        if process is not None:
            stop_group(process, TERM_GRACE)
            # Reaped, so its pid can be reused: a later stop must not signal that group.
            self.process = None
            forget(self.pid_path)

    def signal_stop(self):
        self.stopping.set()
        self._progressed()
        process = self.process
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass

    def stop(self):
        self.signal_stop()
        if self.thread is not None:
            self.thread.join(timeout=TERM_GRACE + 3)
        self._terminate()


# A first build takes about 20 minutes; a step past this is stuck.
BUILD_STEP_LIMIT = 45 * 60
# Each build step runs in its own process group, which quitting Mobster would not reach: the exit hook stops them.
BUILD_STEPS = set()


def stop_group(process, grace=2.0):
    # Waits on the whole group: a child that ignores SIGTERM outlives a shell that exits on it.
    _end_group(process.pid, grace)
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def record(pid_path, pid, command):
    try:
        with open_private(pid_path, "w") as stream:
            stream.write(json.dumps({"pid": pid, "command": " ".join(map(str, command))}))
    except OSError:
        pass


def forget(pid_path):
    try:
        Path(pid_path).unlink()
    except OSError:
        pass


def command_of(pid):
    try:
        out = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def reap_leftover(pid_path, grace=TERM_GRACE):
    # Two runners on one phone end each other's sessions. Only the exact recorded command stops, never a reused pid.
    try:
        note = json.loads(Path(pid_path).read_text())
    except (OSError, ValueError):
        return False
    forget(pid_path)
    pid, command = (note.get("pid"), note.get("command")) if isinstance(note, dict) else (None, None)
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1 or pid == os.getpid() or not command:
        return False
    if command_of(pid) != command:
        return False
    _end_group(pid, grace)
    return True


def _end_group(pgid, grace):
    for number, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 2.0)):
        deadline = time.monotonic() + wait
        alive = _signal_group(pgid, number)
        while alive and time.monotonic() < deadline:
            time.sleep(.05)
            alive = _signal_group(pgid, 0)
        if not alive:
            break


def _signal_group(pgid, number):
    # macOS answers EPERM, not ESRCH, while the group holds only zombies.
    try:
        os.killpg(pgid, number)
    except (ProcessLookupError, PermissionError):
        return False
    return True


@atexit.register
def _stop_steps():
    for process in list(BUILD_STEPS):
        stop_group(process)
