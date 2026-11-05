"""WebDriverAgent on Mobster's simulators: the pinned source, one build per Xcode, and one runner per simulator."""

import atexit
import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import NamedTuple

from ..devices import LOOPBACK, wda_url
from ..wda import gate as wda_gate
from ..wda import patches as wda_patches
from ..wda import source as wda_source
from ..wda.patches import pin_loopback, xctestrun_targets
from .models import SimError
from .simctl import run as run_command

COMMIT8 = wda_source.COMMIT[:8]
PATCH_DIGEST = hashlib.sha256(repr(wda_patches.active_patches()).encode()).hexdigest()[:8]   # the flagged routes too
BUILD_TIMEOUT = 1200
CLONE_TIMEOUT = 600
READY_TIMEOUT = 240  # a busy Mac took over 120 s to start a runner (smoke, 28 Sep, load average 780)
READY_POLL = .5
BUILD_LOCK_TIMEOUT = 1500
MJPEG_WAIT = 5.0  # the MJPEG socket opens a moment after /status answers
HTTP_TIMEOUT = 10.0
DEVICE_PATH = re.compile(r"/CoreSimulator/Devices/([0-9A-Fa-f-]{36})/")
HEARTBEAT_SECONDS = 30.0
STOP_GRACE_SECONDS = 1  # the runner is dead or wedged: its xcodebuild is not waited on long


def elapsed_words(seconds):
    """"45 s", "2 min 05 s"."""
    seconds = int(max(0, seconds))
    return f"{seconds} s" if seconds < 60 else f"{seconds // 60} min {seconds % 60:02d} s"


@contextlib.contextmanager
def heartbeat(progress, what, interval=None, clock=time.monotonic):
    interval = HEARTBEAT_SECONDS if interval is None else interval
    done = threading.Event()
    started = clock()

    def beat():
        while not done.wait(interval):
            try:
                progress(f"Still {what} ({elapsed_words(clock() - started)} so far)")
            except Exception:
                return
    thread = threading.Thread(target=beat, name="mobster-heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        done.set()


# Every build or clone ``run_logged`` has running, across WebDriverAgent instances. Each runs in its own process
# group (start_new_session), so neither ctrl+c nor the end of this process reaches it: the exit hook below kills
# what is left, which covers a build on a daemon thread when the interpreter exits under it.
_RUNNING = set()


@atexit.register
def _kill_running():
    for process in list(_RUNNING):
        _kill_group(process)
    _RUNNING.clear()


# The simulator runners this process started (xcodebuild test-without-building), each in its own session so a
# crash or ctrl+c of a command leaves the warm simulator's runner for the next command. A process that owns them for
# its whole life (`mobster serve`) calls stop_started() as it closes, or the runner outlives it and boots the
# simulator again after `simctl shutdown`.
_STARTED = set()


def stop_started():
    """Stop every runner this process started and that is still running (their process groups)."""
    for process in list(_STARTED):
        _STARTED.discard(process)
        if process.poll() is None:
            _kill_group(process)


def source_dir(data_dir):
    return Path(data_dir) / "wda" / f"src-{COMMIT8}"


def build_dir(data_dir, xcode_build):
    return Path(data_dir) / "wda" / f"build-{xcode_build}-{COMMIT8}-{PATCH_DIGEST}"


def parse_build_dir(name):
    """(Xcode build, commit8, patch digest) from a build folder's name, or None."""
    match = re.fullmatch(r"build-([^-]+)-([0-9a-f]{8})-([0-9a-f]{8})", name)
    return match.groups() if match else None


def http_json(method, url, body=None, timeout=HTTP_TIMEOUT):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data is not None else {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        try:
            status, raw = exc.code, exc.read()
        finally:
            exc.close()
    except (OSError, ValueError):
        return None, None
    try:
        payload = json.loads(raw.decode("utf-8", "replace") or "null")
    except ValueError:
        payload = None
    return status, (payload.get("value") if isinstance(payload, dict) else payload)


def find_xctestrun(build):
    """The simulator test plan a finished build leaves, or None."""
    products = Path(build) / "Build" / "Products"
    try:
        plans = sorted(products.glob("*iphonesimulator*.xctestrun"), key=lambda path: path.stat().st_mtime)
    except OSError:
        return None
    return plans[-1] if plans else None


# -- lsof and ps -------------------------------------------------------------------------------------------

class Listener(NamedTuple):
    command: str
    pid: int
    host: str   # "127.0.0.1", "*", "::1", "192.168.1.4"
    port: int


def parse_lsof(text):
    """Listening sockets from `lsof -nP -iTCP:<port> -sTCP:LISTEN` (its default table)."""
    listeners = []
    for line in (text or "").splitlines():
        tokens = line.split()
        if len(tokens) < 9 or tokens[0] == "COMMAND" or not tokens[1].isdigit():
            continue
        name = tokens[-2] if tokens[-1] == "(LISTEN)" else tokens[-1]
        host, _, port = name.rpartition(":")
        if not port.isdigit():
            continue
        host = host[1:-1] if host.startswith("[") and host.endswith("]") else host
        listeners.append(Listener(tokens[0], int(tokens[1]), host or "*", int(port)))
    return listeners


def loopback_only(listeners):
    """True when something listens, and only on 127.0.0.1."""
    return bool(listeners) and all(listener.host == LOOPBACK for listener in listeners)


def device_of(command):
    """The UDID a simulator process runs in, from its executable's path, or None."""
    match = DEVICE_PATH.search(command or "")
    return match.group(1).upper() if match else None


def runner_commands(udid, ps_output):
    """[(pid, command)] of the xcodebuild runs driving this simulator's runner."""
    runners = []
    for line in (ps_output or "").splitlines():
        pid, _, command = line.strip().partition(" ")
        if ("xcodebuild" in command and "test-without-building" in command and f"id={udid}" in command
                and pid.isdigit()):
            runners.append((int(pid), command.strip()))
    return runners


def runner_pids(udid, ps_output=None):
    """PIDs of the xcodebuild test runs whose destination is this simulator (`ps` is read unless given)."""
    if ps_output is None:
        ps_output = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True,
                                   timeout=10).stdout
    return [pid for pid, _ in runner_commands(udid, ps_output)]


def runner_command(xcodebuild, xctestrun, udid):
    """The command that runs a built runner on one simulator."""
    return [xcodebuild, "test-without-building", "-xctestrun", str(xctestrun), "-destination", f"id={udid}"]


def runner_env(base, port, mjpeg):
    """``base`` plus the runner's ports, 127.0.0.1 only whatever the test plan says, and the actuation gate."""
    return {**base, "TEST_RUNNER_USE_PORT": str(port), "TEST_RUNNER_MJPEG_SERVER_PORT": str(mjpeg),
            "TEST_RUNNER_USE_IP": LOOPBACK, **wda_gate.runner_env("simulator")}


def ready(url, timeout=2):
    """True when the runner at ``url`` answers /status as ready."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/status", timeout=timeout) as response:
            value = json.load(response).get("value") or {}
        return value.get("ready", True) is not False
    except (OSError, ValueError, AttributeError):
        return False


def _alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _signal(pid, number):
    # a runner started by hand shares its shell's process group with other simulators' runners (26 Sep)
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, number)
        else:
            os.kill(pid, number)
    except OSError:
        pass


def terminate(pids, clock=time.monotonic, sleep=time.sleep):
    """SIGTERM these runners, give them STOP_GRACE_SECONDS, then SIGKILL what is left; returns ``pids``."""
    for pid in pids:
        _signal(pid, signal.SIGTERM)
    end = clock() + STOP_GRACE_SECONDS
    while clock() < end and any(_alive(pid) for pid in pids):
        sleep(.2)
    for pid in pids:
        if _alive(pid):
            _signal(pid, signal.SIGKILL)
    return pids


def xctestrun_of(command):
    """The test plan an xcodebuild runner was started with (paths may hold spaces)."""
    match = re.search(r" -xctestrun (.+?\.xctestrun)(?= |$)", command or "")
    return match.group(1) if match else None


REUSE, RESTART, MOVE, START = "reuse", "restart", "move", "start"
UNCHECKED = "unchecked"  # the owner lookup (lsof) was skipped: this simulator's own runner answers


def runner_plan(*, udid, listening, owner, ready, runners, xctestrun):
    current = any(xctestrun_of(command) == str(xctestrun) for _, command in runners)
    if listening and ready and current and owner in (udid, UNCHECKED):
        return REUSE
    # something else listens on the port: take another pair, never adopt a stranger's server
    if listening and owner not in (udid, UNCHECKED):
        return MOVE
    if listening:
        return RESTART
    return RESTART if runners else START


class WebDriverAgent:
    """Build, start, stop and check simulator runners."""

    SEAMS = ("run", "ready", "listening", "popen", "sleep", "clock", "http")

    def __init__(self, data_dir, *, log=None, progress=None):
        from .registry import listening
        self.data_dir = Path(data_dir)
        self.logs = self.data_dir / "logs"
        self.log = log or (lambda message: None)
        self.progress = progress or (lambda message: None)
        self.run = run_command
        self.ready = ready
        self.listening = listening
        self.popen = subprocess.Popen
        self.sleep = time.sleep
        self.clock = time.monotonic
        self.http = http_json
        self._running = set()

    def ps(self):
        return self.run(["/bin/ps", "-axo", "pid=,command="], timeout=10).out

    def lsof(self, port):
        return self.run(["/usr/sbin/lsof", "-nP", f"-iTCP:{int(port)}", "-sTCP:LISTEN"], timeout=10).out

    # -- source and build ----------------------------------------------------------------------------------

    @contextlib.contextmanager
    def build_lock(self):
        """One build or clone at a time across processes; the second waits and then finds it built."""
        folder = self.data_dir / "wda"
        folder.mkdir(parents=True, exist_ok=True)
        fd = os.open(folder / "build.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            end, told = self.clock() + BUILD_LOCK_TIMEOUT, False
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN) or self.clock() >= end:
                        raise SimError("busy", "Another Mobster process is still building WebDriverAgent.",
                                       "Wait for it to finish, then try again.") from None
                    if not told:
                        self.progress("Waiting for another Mobster process to finish building WebDriverAgent")
                        told = True
                    self.sleep(1)
            yield
        finally:
            os.close(fd)

    def ensure_source(self, tools):
        """The pinned clone, patched. Cloned into a temporary folder and renamed, so a half clone never counts."""
        source = source_dir(self.data_dir)
        if not tools.git:
            raise SimError("environment", "git isn't installed, and building WebDriverAgent needs it.",
                           "Install Xcode, which includes git.")
        if source.is_dir() and wda_source.is_pinned(tools.git, source):
            self._patch(source)
            return source
        if source.exists():
            shutil.rmtree(source)
        temporary = source.with_name(f".{source.name}-{os.getpid()}")
        if temporary.exists():
            shutil.rmtree(temporary)
        self.progress(f"Downloading WebDriverAgent {wda_source.REF}")
        with heartbeat(self.progress, "downloading WebDriverAgent"):
            result = self.run(wda_source.clone_command(tools.git, temporary), timeout=CLONE_TIMEOUT, env=tools.env)
        if not result.ok:
            shutil.rmtree(temporary, ignore_errors=True)
            raise SimError("wda", f"Couldn't download WebDriverAgent {wda_source.REF} from GitHub: "
                                  f"{result.last_line() or 'git failed'}",
                           "Check this Mac's network connection, then try again.")
        try:
            wda_source.verify(tools.git, temporary)
        except RuntimeError as exc:
            shutil.rmtree(temporary, ignore_errors=True)
            raise SimError("wda", str(exc), "Try again; if it repeats, the release was moved upstream.") from None
        os.replace(temporary, source)
        self._patch(source)
        return source

    def _patch(self, source):
        try:
            wda_patches.patch_wda(source)
        except (OSError, RuntimeError):
            raise SimError("wda", "WebDriverAgent's source doesn't take Mobster's loopback patches.",
                           f"Delete {source} and try again.") from None

    def ensure_build(self, tools):
        build = build_dir(self.data_dir, tools.build)
        plan = find_xctestrun(build)
        if plan and plan_is_pinned(plan):
            return plan
        with self.build_lock():
            plan = find_xctestrun(build)
            if plan:
                # a process that died between xcodebuild writing the plan and the pin left it listening everywhere
                if not plan_is_pinned(plan):
                    pin_loopback(plan.parent)
                    self.log(f"pinned the cached test plan to {LOOPBACK}: {plan}")
                return plan
            source = self.ensure_source(tools)
            self.logs.mkdir(parents=True, exist_ok=True)
            log = self.logs / "wda-build.log"
            self.progress(f"Building WebDriverAgent for Xcode {tools.version}, once per Xcode version: usually 2 to "
                          f"5 minutes (log: {log})")
            command = [tools.xcodebuild, "build-for-testing", "-project", str(source / "WebDriverAgent.xcodeproj"),
                       "-scheme", "WebDriverAgentRunner", "-destination", "generic/platform=iOS Simulator",
                       "-derivedDataPath", str(build), "CODE_SIGNING_ALLOWED=NO"]
            started = self.clock()
            with heartbeat(self.progress, "building WebDriverAgent"):
                code = self.run_logged(command, log, BUILD_TIMEOUT, tools.env)
            plan = find_xctestrun(build)
            if code != 0 or not plan:
                raise SimError("wda", f"WebDriverAgent didn't build: {last_error(log) or 'xcodebuild failed'}",
                               f"See {log}.")
            pin_loopback(build / "Build" / "Products")
            self.log(f"built WebDriverAgent in {self.clock() - started:.1f} s: {plan}")
            return plan

    def run_logged(self, command, log, timeout, env):
        with open(log, "ab") as stream:
            stream.write(f"\n$ {' '.join(map(str, command))}\n".encode())
            stream.flush()
            try:
                process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT,
                                           stdin=subprocess.DEVNULL, env=env, start_new_session=True)
            except OSError as exc:
                stream.write(f"\nerror: {exc}\n".encode())
                return None
            self._running.add(process)
            _RUNNING.add(process)
            try:
                return process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_group(process)
                stream.write(f"\nerror: did not finish in {timeout} s\n".encode())
                return None
            except BaseException:
                _kill_group(process)
                raise
            finally:
                self._running.discard(process)
                _RUNNING.discard(process)

    def cancel(self):
        """Stop a build running in another thread (the acquire that started it failed or was interrupted)."""
        for process in list(self._running):
            _kill_group(process)

    # -- runners -------------------------------------------------------------------------------------------

    def runner_log(self, udid):
        return self.logs / f"wda-{udid}.log"

    def owner(self, port):
        """The UDID of the simulator whose process listens on ``port``, or None."""
        pids = {listener.pid for listener in parse_lsof(self.lsof(port))}
        for pid in sorted(pids):
            command = self.run(["/bin/ps", "-o", "command=", "-p", str(pid)], timeout=10).out
            udid = device_of(command)
            if udid:
                return udid
        return None

    def plan(self, udid, port, mjpeg, xctestrun):
        url = wda_url(port)
        runners = runner_commands(udid, self.ps())
        current = any(xctestrun_of(command) == str(xctestrun) for _, command in runners)
        busy_ports = [number for number in (port, mjpeg) if self.listening(number)]
        ready = port in busy_ports and self.ready(url)
        owner = None
        if busy_ports and ready and current:
            owner = UNCHECKED
        elif busy_ports:
            owners = {self.owner(number) for number in busy_ports}
            owner = udid if owners == {udid} else next(iter(owners - {udid}))  # a stranger on either port
        return runner_plan(udid=udid, listening=bool(busy_ports), owner=owner, ready=ready, runners=runners,
                           xctestrun=xctestrun)

    def start(self, udid, port, mjpeg, xctestrun, tools):
        """Start the runner in its own session, output to logs/wda-<udid>.log."""
        self.logs.mkdir(parents=True, exist_ok=True)
        env = runner_env(tools.env, port, mjpeg)
        gate = env[wda_gate.RUNNER_VARIABLE]
        command = runner_command(tools.xcodebuild, xctestrun, udid)
        with open(self.runner_log(udid), "ab") as stream:
            stream.write(f"\n$ TEST_RUNNER_USE_PORT={port} TEST_RUNNER_MJPEG_SERVER_PORT={mjpeg} "
                         f"TEST_RUNNER_USE_IP={LOOPBACK} {wda_gate.RUNNER_VARIABLE}={gate} "
                         f"{' '.join(command)}\n".encode())
            stream.flush()
            process = self.popen(command, cwd=str(Path(xctestrun).parent), env=env, stdout=stream,
                                 stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        _STARTED.add(process)
        return process

    def wait_ready(self, udid, port, process=None, timeout=READY_TIMEOUT):
        with heartbeat(self.progress, f"waiting for WebDriverAgent to answer (log: {self.runner_log(udid)})"):
            return self._wait_ready(udid, port, process, timeout)

    def _wait_ready(self, udid, port, process, timeout):
        url = wda_url(port)
        end = self.clock() + timeout
        while self.clock() < end:
            if self.ready(url):
                return
            if process is not None and process.poll() is not None:
                raise SimError("wda", "WebDriverAgent stopped before it was ready: "
                                      f"{last_error(self.runner_log(udid)) or 'xcodebuild exited'}",
                               f"See {self.runner_log(udid)}.")
            self.sleep(READY_POLL)
        raise SimError("wda", f"WebDriverAgent didn't answer on {url} within {timeout:g} s, which happens when "
                              "this Mac is busy.",
                       f"Run the command again. If it keeps happening, see {self.runner_log(udid)}.")

    def stop(self, udid):
        """Stop this simulator's runner and only it: SIGTERM, a short wait, SIGKILL."""
        return terminate(runner_pids(udid, self.ps()), self.clock, self.sleep)

    def listeners(self, port):
        return parse_lsof(self.lsof(port))

    # -- system alerts (sessionless) -----------------------------------------------------------------------

    def read_alert(self, url):
        status, value = self.http("GET", f"{url}/alert/text")
        return status is not None, (value if status == 200 and isinstance(value, str) and value else None)

    def alert_text(self, url):
        """The text of the alert on screen, or None when there is none or the runner didn't answer."""
        return self.read_alert(url)[1]

    def alert_action(self, url, action, name=None):
        if action not in ("accept", "dismiss"):
            raise ValueError(f"alert action must be accept or dismiss, not {action!r}")
        status, _ = self.http("POST", f"{url}/alert/{action}", {"name": name} if name else {})
        return status == 200  # even when nothing changed: the caller reads the alert again

    def check_loopback(self, port, mjpeg):
        api = self.listeners(port)
        end = self.clock() + MJPEG_WAIT
        video = self.listeners(mjpeg)
        while not video and self.clock() < end:
            self.sleep(.25)
            video = self.listeners(mjpeg)
        return loopback_only(api) and loopback_only(video), {port: api, mjpeg: video}


def _kill_group(process):
    """SIGTERM the process's group, then SIGKILL it if it is still there after two seconds."""
    for number, grace in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 0)):
        try:
            os.killpg(process.pid, number)
        except OSError:
            return
        try:
            process.wait(timeout=grace or None)
            return
        except subprocess.TimeoutExpired:
            continue


def last_error(log, window=65536):
    """The last `error:` line of a log (or its last line), for a message."""
    try:
        with open(log, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - window))
            lines = stream.read().decode(errors="replace").splitlines()
    except OSError:
        return ""
    errors = [line.strip() for line in lines if "error:" in line.lower()]
    tail = errors or [line.strip() for line in lines if line.strip()]
    return tail[-1][:300] if tail else ""


class SimulatorWDA:
    """The Smart loop's WebDriverAgent restarter for one simulator, which never stops another simulator's runner."""

    READY_POLL_SECONDS = .5

    def __init__(self, udid, port, mjpeg_port, xctestrun, *, log_path=None, cwd=None, tools=None):
        if not Path(xctestrun).is_file():
            raise FileNotFoundError(f"no xctestrun at {xctestrun}")
        self.udid, self.port, self.mjpeg_port = udid, int(port), int(mjpeg_port)
        self.xctestrun, self.log_path = str(xctestrun), log_path
        self.cwd = cwd or str(Path(xctestrun).resolve().parent)
        self.url = wda_url(self.port)
        self.tools = tools
        self.restarts = 0

    def command(self):
        return runner_command(self.tools.xcodebuild if self.tools else "xcodebuild", self.xctestrun, self.udid)

    def environment(self):
        return runner_env(self.tools.env if self.tools else os.environ, self.port, self.mjpeg_port)

    def stop(self):
        """Stop this simulator's runner (its xcodebuild), gracefully then by force."""
        return terminate(runner_pids(self.udid))

    def start(self):
        log = open(self.log_path, "ab") if self.log_path else subprocess.DEVNULL
        try:
            # Its own session: ctrl+c does not reach it. stop_started() does, for a process that owns it.
            process = subprocess.Popen(self.command(), cwd=self.cwd, env=self.environment(), stdout=log,
                                       stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
            _STARTED.add(process)
            return process
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
            time.sleep(self.READY_POLL_SECONDS)
        raise TimeoutError(f"WDA on port {self.port} did not come back in {timeout:.0f} s")


def plan_is_pinned(path):
    """Every target of a test plan has USE_IP=127.0.0.1."""
    import plistlib
    try:
        with open(path, "rb") as stream:
            plan = plistlib.load(stream)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return False
    targets = xctestrun_targets(plan)
    return bool(targets) and all(
        isinstance(target.get("EnvironmentVariables"), dict)
        and target["EnvironmentVariables"].get("USE_IP") == LOOPBACK
        and (not isinstance(target.get("TestingEnvironmentVariables"), dict)
             or target["TestingEnvironmentVariables"].get("USE_IP", LOOPBACK) == LOOPBACK)
        for target in targets)
