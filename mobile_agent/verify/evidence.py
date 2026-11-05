"""Failure evidence: what a run keeps to say why a check went red, not just that it did."""

import json
import os
import plistlib
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .. import secret_filter
from . import crashes

LOG_FILE = "logs/app.log"
CRASH_DIR = "crashes"
LOG_CAP_BYTES = 1_000_000
TAIL_LINES = 2_000
LINE_LIMIT = 2_000
# The log process is reaped on a thread of its own: waiting for `simctl spawn` cost a check 0.35 s (16 Oct).
DRAIN_WAIT = .3
WINDOW_MS = 10_000
WINDOW_LINES = 60
STOP_WAIT = 3.0
# ReportCrash wrote a simulator's report 1-4 min after the crash under load (16 Oct); `mobster test` looks again.
EXIT_WAIT = 2.0
REPORT_WAIT = {"simulator": 8.0, "iphone": 15.0}
CRASH_POLL = .5
# A phone's clock and the Mac's can disagree by a little.
CLOCK_SLACK = 5.0
# SpringBoard's line when an app's process ends: "[app<id>:pid] Process exited: ... domain:signal(2) code:SIGTRAP(5)".
# A crash is a signal other than a kill, a watchdog or jetsam; Mobster's own relaunch shows as a force-quit.
EXITED = re.compile(r"\[app<(?P<bundle>[^>(]+)(?:\([^)]*\))?>:(?P<pid>\d+)\] Process exited: .*?"
                    r"domain:(?P<domain>[\w-]+)\(\d+\) code:(?P<code>[\w-]+)(?:\((?P<number>[^)]*)\))?")
NOT_CRASHES = frozenset({"SIGKILL", "SIGTERM", "SIGINT", "SIGHUP", "SIGQUIT"})
LEVELS = {"default": "notice", "notice": "notice", "info": "info", "debug": "debug", "error": "error",
          "fault": "fault", "warning": "error", "critical": "fault"}


@dataclass
class Line:
    at_ms: int
    level: str
    process: str
    text: str
    own: bool = False  # from the app's own code (a subsystem not Apple's), which the window keeps first
    driver: bool = False  # XCTest's own lines in the app's process (com.apple.dt.*): Mobster's driver, not the app
    apple: bool = False  # an Apple framework's subsystem (com.apple.*), even inside the app's process: accessibility

    def render(self):
        return f"{self.at_ms / 1000:9.3f}  {self.level:<6} {self.process}: {self.text}"

    def to_dict(self):
        return {"atMs": self.at_ms, "level": self.level, "process": self.process, "text": self.text}


@dataclass
class Exit:
    """An app process ending, from SpringBoard's log."""
    at_ms: int
    pid: int
    domain: str
    code: str

    @property
    def crashed(self):
        if self.domain == "signal":
            return self.code not in NOT_CRASHES
        if self.domain == "jetsam":
            return True
        return self.domain == "frontboard" and "8badf00d" in self.code.lower()

    @property
    def why(self):
        """'SIGTRAP', 'killed for using too much memory (jetsam)', 'the watchdog (0x8badf00d)'."""
        if self.domain == "jetsam":
            return "killed for using too much memory (jetsam)"
        if "8badf00d" in self.code.lower():
            return "killed by the watchdog (0x8badf00d)"
        return self.code


def exit_event(text, bundle_id):
    """The Exit in a SpringBoard line about ``bundle_id``'s process (at_ms 0), or None."""
    match = EXITED.search(text or "")
    if not match or match.group("bundle") != bundle_id:
        return None
    code = match.group("code")
    if match.group("domain") == "frontboard" and match.group("number"):
        code = match.group("number")
    return Exit(0, int(match.group("pid")), match.group("domain"), code)


def _predicate_text(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


class SimulatorTools:
    """A simulator's evidence: its unified log and the Mac's crash reports for its processes."""

    kind = "simulator"

    def __init__(self, udid, *, popen=subprocess.Popen, run=subprocess.run, reports=None):
        self.udid = udid
        self.popen, self.run = popen, run
        self.reports = Path(reports) if reports else crashes.DIAGNOSTIC_REPORTS

    def executable(self, app):
        return getattr(app, "executable", None)

    def log_argv(self, executable, bundle_id):
        """The app's own log, and SpringBoard's line when its process exits."""
        predicate = (f"process == {_predicate_text(executable)} OR (process == \"SpringBoard\" AND "
                     f"eventMessage CONTAINS \"Process exited\" AND "
                     f"eventMessage CONTAINS {_predicate_text(bundle_id)})")
        return ["/usr/bin/xcrun", "simctl", "spawn", self.udid, "log", "stream", "--style", "ndjson", "--level",
                "info", "--predicate", predicate]

    def parse(self, raw, arrival, t0_wall):
        try:
            event = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(event, dict) or event.get("eventType") not in (None, "logEvent", "activityCreateEvent"):
            return None
        stamp = _log_time(event.get("timestamp")) or arrival
        process = Path(str(event.get("processImagePath") or "")).name or "app"
        where = "/".join(filter(None, [event.get("subsystem"), event.get("category")]))
        text = str(event.get("eventMessage") or "")
        if where:
            text = f"[{where}] {text}"
        level = LEVELS.get(str(event.get("messageType") or "default").lower(), "notice")
        subsystem = str(event.get("subsystem") or "")
        own = bool(subsystem) and not subsystem.startswith("com.apple.")
        return Line(round((stamp - t0_wall) * 1000), level, process, text, own, subsystem.startswith("com.apple.dt."),
                    subsystem.startswith("com.apple."))

    def crash_reports(self, executable, since):
        """The crash reports that may be this run's: the Mac's own files, read in place (no copy)."""
        return crashes.candidates(self.reports, executable, since - CLOCK_SLACK), None


class PhoneTools:
    # idevicecrashreport --keep copies the phone's crash reports and never removes them from the phone.
    kind = "iphone"

    def __init__(self, udid, *, network=False, popen=subprocess.Popen, run=subprocess.run, tool=None):
        self.udid, self.network = udid, network
        self.popen, self.run = popen, run
        if tool is None:
            from ..mac_tools import tool
        self._tool = tool

    def _base(self, name):
        path = self._tool(name)
        if not path:
            raise LookupError(f"{name} isn't installed")
        return [path, "-u", self.udid] + (["-n"] if self.network else [])

    def executable(self, app):
        """The app's CFBundleExecutable, read from the phone (``ideviceinstaller list -b``); None when it can't."""
        known = getattr(app, "executable", None)
        if known:
            return known
        try:
            done = self.run(self._base("ideviceinstaller") + ["list", "--all", "--xml", "-b", app.bundle_id,
                                                               "-a", "CFBundleIdentifier", "-a", "CFBundleExecutable"],
                            capture_output=True, timeout=20, stdin=subprocess.DEVNULL)
            items = plistlib.loads(done.stdout) if done.returncode == 0 and done.stdout else []
        except (OSError, subprocess.SubprocessError, LookupError, ValueError, plistlib.InvalidFileException):
            return None
        for item in items if isinstance(items, list) else ():
            if isinstance(item, dict) and item.get("CFBundleIdentifier") == app.bundle_id:
                name = item.get("CFBundleExecutable")
                return name if isinstance(name, str) and name else None
        return None

    def log_argv(self, executable, bundle_id):
        """The app's process and SpringBoard's (for the line when the app exits; the rest of SpringBoard's is dropped as
        it is read)."""
        return self._base("idevicesyslog") + ["--no-colors", "-p", f"{executable}|SpringBoard"]

    SYSLOG = re.compile(r"^\w{3} +\d+ [\d:.]+ \S+ (?P<process>[^\[(\s]+)(?:\([^)]*\))?\[(?P<pid>\d+)\] "
                        r"<(?P<level>\w+)>: ?(?P<text>.*)$")

    def parse(self, raw, arrival, t0_wall):
        # Stamped when the Mac read it: the phone's clock isn't the run's.
        text = raw.rstrip("\n")
        if not text.strip() or text.startswith("[connected") or text.startswith("[disconnected"):
            return None
        match = self.SYSLOG.match(text)
        at = round((arrival - t0_wall) * 1000)
        if not match:
            return Line(at, "", "", text.strip())
        return Line(at, LEVELS.get(match.group("level").lower(), "notice"), match.group("process"),
                    match.group("text"))

    def crash_reports(self, executable, since):
        """Copies of the phone's crash reports for ``executable`` (``idevicecrashreport --keep --filter``) in a
        temporary folder the caller removes."""
        folder = tempfile.mkdtemp(prefix="mobster-crashes-")
        try:
            self.run(self._base("idevicecrashreport") + ["--keep", "--filter", executable, folder],
                     capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError, LookupError):
            return [], folder
        found = []
        for path in Path(folder).rglob("*.ips"):
            if path.name.startswith(f"{executable}-"):
                found.append(path)
        return sorted(found, reverse=True), folder


def _log_time(text):
    if not isinstance(text, str):
        return None
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d %H:%M:%S.%f%z").timestamp()
    except ValueError:
        return None


class LogCapture:
    """One log process, read on its own thread into ``logs/app.log`` (capped) and kept in memory for the window."""

    def __init__(self, tools, executable, bundle_id, path, *, t0_wall, clock=time.time, on_exit=None):
        self.tools, self.executable, self.bundle_id, self.path = tools, executable, bundle_id, Path(path)
        self.t0_wall, self.clock = t0_wall, clock
        self.on_exit = on_exit or (lambda event: None)
        self.lines = []           # what the file holds, in order
        self.tail = deque(maxlen=TAIL_LINES)
        self.dropped = 0
        self.bytes = 0
        self.error = None
        self._process = self._thread = None
        self._handle = None
        self._lock = threading.Lock()
        self._stopped = False
        self._last = None         # the entry a continuation line belongs to

    def start(self):
        argv = self.tools.log_argv(self.executable, self.bundle_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.path, "w", encoding="utf-8")
        try:
            self._process = self.tools.popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                             stderr=subprocess.DEVNULL, start_new_session=True, text=True,
                                             encoding="utf-8", errors="replace", bufsize=1)
        except BaseException:
            self._handle.close()
            self._handle = None
            raise
        self._thread = threading.Thread(target=self._read, name="mobster-evidence-log", daemon=True)
        self._thread.start()

    def _read(self):
        try:
            for raw in self._process.stdout:
                line = self.tools.parse(raw, self.clock(), self.t0_wall)
                if line is not None:
                    self._take(line)
        except (OSError, ValueError, AttributeError) as error:
            self.error = type(error).__name__

    def _take(self, line):
        if not line.level and not line.process:  # the next line of the entry before
            if self._last is None:
                return
            line = Line(self._last.at_ms, self._last.level, self._last.process, line.text)
        elif line.process == "SpringBoard":
            event = exit_event(line.text, self.bundle_id)
            self._last = None
            if event is None:
                return
            event.at_ms = line.at_ms
            self.on_exit(event)
            # SpringBoard's own words hold "code:SIGTRAP(5)", which the secret filter would take for a code.
            line = Line(line.at_ms, line.level, "SpringBoard", f"{self.bundle_id} process {event.pid} exited: "
                        f"{event.domain} {event.code}" + (" (a crash)" if event.crashed else ""))
        self._last = line if line.process != "SpringBoard" else None
        line.text = secret_filter.redact(line.text[:LINE_LIMIT])
        with self._lock:
            if self._stopped:
                return
            size = len(line.render().encode("utf-8")) + 1
            if self.bytes + size <= LOG_CAP_BYTES and not self.tail:
                self.lines.append(line)
                self.bytes += size
                self._handle.write(line.render() + "\n")
            else:
                if len(self.tail) == self.tail.maxlen:
                    self.dropped += 1
                self.tail.append(line)

    def stop(self):
        """End the log process and finish the file (idempotent): the head, a note of what was dropped, the tail."""
        process, self._process = self._process, None
        if process is not None:
            _signal(process)
            threading.Thread(target=_end, args=(process,), name="mobster-evidence-reap", daemon=True).start()
        if self._thread is not None:
            self._thread.join(DRAIN_WAIT)
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            handle, self._handle = self._handle, None
            if handle is None:
                return
            try:
                if self.dropped:
                    handle.write(f"[{self.dropped} lines dropped: the log passed {LOG_CAP_BYTES // 1000} kB; the last "
                                 f"{len(self.tail)} follow]\n")
                for line in self.tail:
                    text = line.render()
                    handle.write(text + "\n")
                    self.bytes += len(text.encode("utf-8")) + 1
            finally:
                handle.close()

    @property
    def count(self):
        with self._lock:
            return len(self.lines) + len(self.tail) + self.dropped

    def all(self):
        with self._lock:
            return list(self.lines) + list(self.tail)

    def window(self, center_ms, half=WINDOW_MS, limit=WINDOW_LINES):
        near = [line for line in self.all() if abs(line.at_ms - center_ms) <= half and not line.driver]
        if len(near) > limit:
            def rank(i):
                line = near[i]
                return (0 if line.own or line.level in ("error", "fault") or line.process == "SpringBoard" else 1,
                        abs(line.at_ms - center_ms))
            keep = sorted(range(len(near)), key=rank)[:limit]
            near = [near[i] for i in sorted(keep)]
        return near


def _signal(process):
    """SIGTERM to the log process's group (it was started in a session of its own)."""
    try:
        if process.poll() is None:
            try:
                if not isinstance(process, subprocess.Popen):
                    raise TypeError("not a process this module started")
                os.killpg(process.pid, signal.SIGTERM)
            except (OSError, TypeError):
                process.terminate()
    except Exception:
        pass


def _end(process):
    """Reap the log process, killing it when it hasn't ended STOP_WAIT seconds after SIGTERM."""
    try:
        try:
            process.wait(timeout=STOP_WAIT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=STOP_WAIT)
    except Exception:
        pass


class Evidence:
    """The evidence of one run."""

    def __init__(self, tools, app, run_dir, *, t0_wall, project_root=None, log=None, clock=time.time,
                 sleep=time.sleep):
        self.tools, self.app, self.run_dir = tools, app, Path(run_dir)
        self.t0_wall, self.root = t0_wall, project_root
        self._log = log or (lambda line: None)
        self.clock, self.sleep = clock, sleep
        self.executable = None
        self.capture = None
        self.note = None          # why there's no log, when there isn't one
        self.pids = set()         # the app's processes the run saw
        self.exits = []           # Exit events from SpringBoard's log
        self.crash_summary = None
        self._looked = False
        self._starter = None
        self._closed = False      # stop() came: a log the starter hasn't started yet never starts
        self._lock = threading.Lock()

    @property
    def kind(self):
        return self.tools.kind

    def start(self):
        """Start the log on a thread of its own, so the launch never waits for it (an iPhone's executable name is one
        ideviceinstaller call)."""
        self._starter = threading.Thread(target=self._start, name="mobster-evidence-start", daemon=True)
        self._starter.start()

    def _start(self):
        try:
            self.executable = self.tools.executable(self.app)
        except Exception as error:
            self._log(f"evidence: no executable name ({type(error).__name__})")
        if not self.executable:
            self.note = "No log: Mobster couldn't read the app's executable name."
            return
        capture = LogCapture(self.tools, self.executable, self.app.bundle_id, self.run_dir / LOG_FILE,
                             t0_wall=self.t0_wall, clock=self.clock, on_exit=self._exited)
        with self._lock:
            if self._closed:
                return
            try:
                capture.start()
            except Exception as error:
                self.note = "No log: " + ("the log tool isn't installed." if isinstance(error, LookupError)
                                          else f"the log didn't start ({type(error).__name__}).")
                self._log(f"evidence: no log ({type(error).__name__}: {error})")
                return
            self.capture = capture

    def _exited(self, event):
        with self._lock:
            self.exits.append(event)
        if event.crashed:
            self.saw(event.pid)
            self._log(f"evidence: {self.app.bundle_id} process {event.pid} exited: {event.why}")

    def saw(self, pid):
        """A process of the app the run saw (WebDriverAgent's foreground app): its crash is this run's."""
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
            with self._lock:
                self.pids.add(pid)

    def _ready(self, wait=STOP_WAIT * 4):
        if self._starter is not None:
            self._starter.join(wait)

    def crashed(self):
        """The first crashed Exit the log showed, or None."""
        with self._lock:
            return next((event for event in self.exits if event.crashed), None)

    def crash(self, *, suspect=False):
        """This run's crash summary, or None. Looks once; with ``suspect`` it waits EXIT_WAIT, then REPORT_WAIT."""
        with self._lock:
            if self._looked:
                return self.crash_summary
        self._ready()
        exited = self.crashed()
        if exited is None and suspect and self.capture is not None:
            deadline = self.clock() + EXIT_WAIT
            while exited is None and self.clock() < deadline:
                self.sleep(CRASH_POLL)
                exited = self.crashed()
        summary = None
        executable = self.executable or getattr(self.app, "executable", None)
        if exited is not None or (suspect and self.capture is None):
            wait = REPORT_WAIT.get(self.kind, 8.0) if exited is not None else 0
            summary = self._report(executable, self.clock() + wait) if executable else None
            if summary is None and exited is not None:
                summary = {"type": None, "signal": exited.why, "subtype": None, "message": None, "thread": None,
                           "process": executable, "pid": exited.pid, "frames": [], "symbolicated": False,
                           "atMs": exited.at_ms, "report": None}
            elif summary is not None and exited is not None and not summary.get("signal"):
                summary["signal"] = exited.why
        with self._lock:
            self._looked = True
            self.crash_summary = summary
        return summary

    def _report(self, executable, deadline):
        """The crash report's summary, looking until ``deadline``; None when it hasn't appeared."""
        while True:
            report = self._find(executable)
            if report is not None:
                return self._summarize(report, executable)
            if self.clock() >= deadline:
                return None
            self.sleep(CRASH_POLL)

    def _summarize(self, report, executable):
        try:
            dsyms = crashes.find_dsyms(getattr(self.app, "path", None), self.root)
            summary = crashes.summary(report, executable=executable, dsyms=dsyms, run=self.tools.run)
            copy = crashes.write_copy(report, self.run_dir / CRASH_DIR)
        except Exception as error:
            self._log(f"evidence: the crash report couldn't be read ({type(error).__name__}: {error})")
            return None
        summary["report"] = copy.relative_to(self.run_dir).as_posix()
        summary["atMs"] = round((report.wall - self.t0_wall) * 1000)
        self._log(f"evidence: crash report {report.path.name} ({summary.get('type')})")
        return summary

    def _find(self, executable):
        try:
            paths, scratch = self.tools.crash_reports(executable, self.t0_wall)
        except Exception as error:
            self._log(f"evidence: no crash reports ({type(error).__name__})")
            return None
        try:
            with self._lock:
                pids = set(self.pids)
            udid = getattr(self.tools, "udid", None) if self.kind == "simulator" else None
            for path in paths:
                report = crashes.read(path)
                if report is not None and crashes.belongs(
                        report, bundle_id=self.app.bundle_id, executable=executable,
                        since=self.t0_wall - (CLOCK_SLACK if self.kind == "iphone" else 0),
                        until=self.clock() + CLOCK_SLACK, pids=pids, udid=udid):
                    return report
            return None
        finally:
            if scratch:
                shutil.rmtree(scratch, ignore_errors=True)

    def stop(self):
        """Stop the log (idempotent). A log still starting never starts."""
        self._ready(STOP_WAIT)
        with self._lock:
            self._closed = True
            capture = self.capture
        if capture is not None:
            capture.stop()

    def lines(self):
        """The captured log as dicts for the per-step proof (verify/proof.py)."""
        with self._lock:
            capture = self.capture
        return [{"atMs": line.at_ms, "level": line.level, "process": line.process, "text": line.text, "own": line.own,
                 "driver": line.driver} for line in capture.all()] if capture is not None else []

    def summary(self, *, center_ms=None, steps=(), failed=False):
        """results.json's ``evidence``: the log file, and for a run that didn't pass, the window around the failure, the
        step it came in and the crash."""
        with self._lock:
            capture, crash = self.capture, self.crash_summary
        out = {"source": self.kind, "startedAt": round(self.t0_wall * 1000)}
        if capture is not None:
            out["log"] = {"path": LOG_FILE, "lines": capture.count, "bytes": capture.bytes, "dropped": capture.dropped}
        elif self.note:
            out["note"] = self.note
        if crash is not None:
            out["crash"] = dict(crash)
        if not failed and crash is None:
            return out
        center = crash.get("atMs") if crash else center_ms
        if center is None:
            return out
        out["atMs"] = center
        step = failing_step(steps, center)
        if step is not None:
            out["step"] = step
        if capture is not None:
            # "app": the app under test's own line (its process, not an Apple framework's subsystem). The Mac app shows
            # those first and folds the rest (accessibility, system daemons) behind Show system log (ICP-3).
            executable = getattr(capture, "executable", None)
            out["window"] = {"fromMs": center - WINDOW_MS, "toMs": center + WINDOW_MS,
                             "lines": [{**line.to_dict(), "app": bool(executable) and line.process == executable
                                        and not line.apple} for line in capture.window(center)]}
        return out


def failing_step(steps, at_ms):
    """The step the failure came in: the first one recorded at or after ``at_ms``, else the last."""
    # A step is recorded once its action and settle end, so a crash its tap causes comes before the step's own time.
    timed = [(step.get("at_ms"), step.get("index")) for step in steps or ()
             if isinstance(step.get("at_ms"), (int, float))]
    for at, index in timed:
        if at >= at_ms:
            return index
    return timed[-1][1] if timed else None


def for_run(manager, target, app, run_dir, *, t0_wall, project_root=None, log=None):
    """The run's Evidence, or None when its manager offers none (a WebDriverAgent address, a fake)."""
    offer = getattr(manager, "evidence_tools", None)
    if not callable(offer):
        return None
    try:
        tools = offer(target)
    except Exception:
        return None
    if tools is None:
        return None
    return Evidence(tools, app, run_dir, t0_wall=t0_wall, project_root=project_root, log=log)


# `mobster test` waits this long, once, at the end of a suite, for crash reports macOS hadn't written when their run
# ended. A Mac under load wrote them 1-4 min after the crash (16 Oct); an idle one, within seconds.
LATE_WAIT = 60.0
LATE_POLL = 1.0


def late_reports(results, *, progress=None, budget=LATE_WAIT, clock=time.time, sleep=time.sleep, tools_for=None):
    pending = [cell for check in results.get("checks") or () for cell in check.get("results") or ()
               if _waiting(cell.get("evidence"))]
    if not pending:
        return 0
    progress = progress or (lambda line: None)
    count = len(pending)
    progress(f"Waiting up to {budget:.0f} s for {count} crash report{'' if count == 1 else 's'}: macOS writes "
             "them after the crash")
    deadline, found = clock() + budget, 0
    while pending:
        for cell in list(pending):
            try:
                attached = _attach(cell, tools_for=tools_for or _tools_for)
            except Exception:
                attached = None
            if attached is not False and attached is not None:
                pending.remove(cell)
                found += 1
            elif attached is False:
                pending.remove(cell)  # its run folder is gone or unreadable: nothing to wait for
        if not pending or clock() >= deadline:
            break
        sleep(LATE_POLL)
    if pending:
        progress(f"{len(pending)} crash report{'' if len(pending) == 1 else 's'} didn't arrive in time; macOS keeps "
                 "them in ~/Library/Logs/DiagnosticReports")
    return found


def _waiting(evidence):
    crash = evidence.get("crash") if isinstance(evidence, dict) else None
    return bool(crash) and not crash.get("report") and bool(evidence.get("runDir"))


def _tools_for(source, udid):
    if source == "simulator":
        return SimulatorTools(udid)
    if source == "iphone":
        return PhoneTools(udid)
    return None


def _attach(cell, *, tools_for):
    """One crash report, looked for once: True when found and written, None when not there yet, False when the run can't
    take one."""
    from types import SimpleNamespace

    from .checks import project_root
    from .crashes import headline
    from .report import write_report
    evidence = cell["evidence"]
    run_dir = Path(evidence["runDir"])
    try:
        result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    stored = result.get("evidence") or {}
    crash, app, device = stored.get("crash") or {}, result.get("app") or {}, result.get("device") or {}
    tools = tools_for(stored.get("source"), device.get("udid"))
    if tools is None or not stored.get("startedAt") or not crash.get("process"):
        return False
    lookup = Evidence(tools, SimpleNamespace(bundle_id=app.get("bundle_id"), executable=crash["process"],
                                             path=app.get("path")),
                      run_dir, t0_wall=stored["startedAt"] / 1000, project_root=project_root(run_dir))
    lookup.executable = crash["process"]
    if isinstance(crash.get("pid"), int):
        lookup.saw(crash["pid"])
    summary = lookup._report(crash["process"], deadline=0)
    if summary is None:
        return None
    if not summary.get("signal"):
        summary["signal"] = crash.get("signal")
    stored["crash"] = summary
    # The report's own time is the crash's: SpringBoard logs the exit seconds later.
    stored["atMs"] = summary["atMs"]
    step = failing_step(result.get("steps"), summary["atMs"])
    if step is not None:
        stored["step"] = step
    message = headline(summary, app.get("name") or app.get("bundle_id") or "The app")
    if (result.get("reason") or {}).get("class") == "app_crashed":
        result["reason"]["message"] = message
        result["summary"] = message
    result["evidence"] = stored
    from ..paths import write_text_atomic
    write_text_atomic(run_dir / "result.json", json.dumps(result, indent=1, ensure_ascii=False, allow_nan=False) + "\n")
    write_report(run_dir, result)
    evidence["crash"] = summary
    evidence["atMs"] = stored["atMs"]
    if "step" in stored:
        evidence["step"] = stored["step"]
    if (cell.get("reason") or {}).get("class") == "app_crashed":
        cell["reason"]["message"] = message
    for run in cell.get("runs") or ():
        if run.get("runDir") == str(run_dir) and run.get("class") == "app_crashed":
            run["message"] = message
    return True


def log_text(lines):
    """Window lines as plain text, for JUnit's <system-out>."""
    return "\n".join(Line(item.get("atMs") or 0, item.get("level") or "", item.get("process") or "",
                          item.get("text") or "").render() for item in lines or ())


__all__ = ["Evidence", "LogCapture", "Line", "Exit", "SimulatorTools", "PhoneTools", "for_run", "failing_step",
           "exit_event", "late_reports", "log_text", "WINDOW_MS", "LOG_FILE"]
