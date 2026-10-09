"""Restart a simulator's WebDriverAgent runner the way it was started: headless, by xcodebuild.

A simulator's runner is ``xcodebuild test-without-building -xctestrun <built runner> -destination
id=<UDID>`` with its ports in TEST_RUNNER_USE_PORT / TEST_RUNNER_MJPEG_SERVER_PORT. When the runner
dies (iOSWorld mem-046: a reset connection, then refused ones, in 2 of 4 runs on CityRide, 25 Sep)
nothing started it again and the task ended. ``SimulatorWDA`` is ``WDA.restarter``: it stops the
xcodebuild that drives this one simulator (never another's), starts it again, and returns once the
runner answers /status. Simulators only: the USB runner has its own supervisor (device_manager).
"""

import json
import os
import pathlib
import signal
import subprocess
import time
import urllib.request

READY_POLL_SECONDS = .5
STOP_GRACE_SECONDS = 1  # the runner is dead or wedged: its xcodebuild is not waited on long


def runner_pids(udid, ps_output=None):
    """PIDs of the xcodebuild test runs whose destination is this simulator."""
    if ps_output is None:
        ps_output = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True,
                                   timeout=10).stdout
    pids = []
    for line in ps_output.splitlines():
        pid, _, command = line.strip().partition(" ")
        if ("xcodebuild" in command and "test-without-building" in command and f"id={udid}" in command
                and pid.isdigit()):
            pids.append(int(pid))
    return pids


def ready(url, timeout=2):
    """True when the runner at ``url`` answers /status as ready."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/status", timeout=timeout) as response:
            value = json.load(response).get("value") or {}
        return value.get("ready", True) is not False
    except (OSError, ValueError, AttributeError):
        return False


class SimulatorWDA:
    """``WDA.restarter`` for one simulator's runner (see the module docstring)."""

    def __init__(self, udid, port, mjpeg_port, xctestrun, *, log_path=None, cwd=None):
        if not pathlib.Path(xctestrun).is_file():
            raise FileNotFoundError(f"no xctestrun at {xctestrun}")
        self.udid, self.port, self.mjpeg_port = udid, int(port), int(mjpeg_port)
        self.xctestrun, self.log_path = str(xctestrun), log_path
        self.cwd = cwd or str(pathlib.Path(xctestrun).resolve().parent)
        self.url = f"http://127.0.0.1:{self.port}"
        self.restarts = 0

    def command(self):
        return ["xcodebuild", "test-without-building", "-xctestrun", self.xctestrun,
                "-destination", f"id={self.udid}"]

    def stop(self):
        """Stop this simulator's runner (its xcodebuild), gracefully then by force."""
        pids = runner_pids(self.udid)
        for pid in pids:
            _signal(pid, signal.SIGTERM)
        end = time.monotonic() + STOP_GRACE_SECONDS
        while time.monotonic() < end and any(_alive(pid) for pid in pids):
            time.sleep(.2)
        for pid in pids:
            if _alive(pid):
                _signal(pid, signal.SIGKILL)
        return pids

    def start(self):
        env = {**os.environ, "TEST_RUNNER_USE_PORT": str(self.port),
               "TEST_RUNNER_MJPEG_SERVER_PORT": str(self.mjpeg_port)}
        log = open(self.log_path, "ab") if self.log_path else subprocess.DEVNULL
        try:
            # Its own session: not stopped with this process, as the runner started by hand is not.
            return subprocess.Popen(self.command(), cwd=self.cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
        finally:
            if log is not subprocess.DEVNULL:
                log.close()

    def __call__(self, timeout=90):
        """Restart the runner; returns once it answers /status, or raises TimeoutError."""
        started = time.monotonic()
        self.stop()
        self.timing = {"stop_ms": round((time.monotonic() - started) * 1000)}
        self.start()
        self.restarts += 1
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if ready(self.url):
                self.timing["ready_ms"] = round((time.monotonic() - started) * 1000)
                return True
            time.sleep(READY_POLL_SECONDS)
        raise TimeoutError(f"WDA on port {self.port} did not come back in {timeout:.0f} s")


def _alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _signal(pid, number):
    """Signal one runner: its process group only when it leads its own (as ``start`` makes it). Runners
    started by hand from one shell share that shell's group with other simulators' runners (four did,
    26 Sep), and those must never be touched."""
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, number)
        else:
            os.kill(pid, number)
    except OSError:
        pass
