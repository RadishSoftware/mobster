"""Scheduled health checks: a running Mobster (`mobster serve`, the Mac app) runs a project's checks on a schedule."""

import json
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..env_file import writer
from ..paths import dev_data_dir, write_text_atomic
from ..workflows import cron

FILE = "schedule.yaml"
STATE = "schedule.json"
DEVICES = "test-schedule-devices.json"
OFF = "test-schedule-off.json"
SCHEMA = "mobster.test-schedule/1"
KEEP_RUNS = 20
CHECK_SECONDS = 30
LATE_MS = 120_000          # a slot this much late (the Mac slept) isn't run
MIN_EVERY_S, MAX_EVERY_S = 300, 7 * 86400
MAX_SCHEDULES = 20
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,59}")
EVERY = re.compile(r"(?:every\s+)?(\d{1,3})\s*(m|min|minutes?|h|hr|hours?|d|days?)", re.I)
UNITS = {"m": 60, "h": 3600, "d": 86400}
NOT_SENT = ("failed", "needs_review", "couldnt_run")
OSASCRIPT = "/usr/bin/osascript"


class ScheduleError(Exception):
    """One sentence that says which schedule and what to change."""


@dataclass(frozen=True)
class Spec:
    root: Path
    name: str
    when: str                    # as written: "every 1h" or the cron
    every_s: int = 0             # > 0: an interval
    cron: str = ""               # else a five-field cron
    zone: str = "UTC"
    checks: tuple = ()           # of paths relative to the project; empty: every check
    tags: tuple = ()
    sim: str = None
    source: str = FILE

    def next_after(self, after_ms):
        if self.every_s:
            return after_ms + self.every_s * 1000
        return cron.next_occurrences(self.cron, self.zone, after_ms)[0]


def parse_when(text, where):
    """(every seconds, cron) for "every 1h", "30m" or a five-field cron. ScheduleError otherwise."""
    text = str(text).strip()
    found = EVERY.fullmatch(text)
    if found:
        seconds = int(found[1]) * UNITS[found[2][0].lower()]
        if not MIN_EVERY_S <= seconds <= MAX_EVERY_S:
            raise ScheduleError(f"{where}: every must be from 5 minutes to 7 days.")
        return seconds, ""
    try:
        return 0, cron.validate_cron(text, "UTC")
    except ValueError:
        raise ScheduleError(f"{where}: write `every 1h` (m, h or d), or a five-field cron such as `0 * * * *`. "
                            "Runs must be at least 5 minutes apart.") from None


def _zone(value, where):
    zone = value if value is not None else cron.local_timezone()
    try:
        cron.timezone(zone)
    except ValueError:
        raise ScheduleError(f"{where}: timezone must be an IANA name, such as America/Los_Angeles.") from None
    return zone


def load(root):
    """([Spec], [problems]): the project's schedules from schedule.yaml and its checks' ``schedule:``."""
    root = Path(root)
    specs, problems = [], []
    path = root / ".mobster" / FILE
    if path.is_file():
        try:
            _from_file(root, path, specs, problems)
        except ScheduleError as error:
            problems.append(str(error))
    from .discover import DiscoveryError, discover
    try:
        entries = discover([], root, cwd=root)
    except DiscoveryError:
        entries = []
    taken = {spec.name for spec in specs}
    for entry in entries:
        when = getattr(entry.check, "schedule", None)
        if not when:
            continue
        where = f"{entry.file}: schedule"
        try:
            every, expr = parse_when(when, where)
        except ScheduleError as error:
            problems.append(str(error))
            continue
        name = f"check {Path(entry.file).stem}"
        if name in taken:
            problems.append(f"{entry.file}: another schedule is named {name!r}.")
            continue
        taken.add(name)
        specs.append(Spec(root, name, str(when).strip(), every, expr, _zone(None, where), (entry.file,), (),
                          None, entry.file))
    return specs, problems


def _from_file(root, path, specs, problems):
    import yaml
    where = f".mobster/{FILE}"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        raise ScheduleError(f"{where} can't be read. Check that it is a text file.") from None
    except yaml.YAMLError:
        raise ScheduleError(f"{where} isn't valid YAML. Write `schedules:` with a list of schedules.") from None
    if data is None:
        return
    if not isinstance(data, dict) or set(data) - {"timezone", "schedules"}:
        raise ScheduleError(f"{where} holds `timezone:` and `schedules:` and nothing else.")
    zone = _zone(data.get("timezone"), where)
    items = data.get("schedules") or []
    if not isinstance(items, list) or len(items) > MAX_SCHEDULES:
        raise ScheduleError(f"{where}: `schedules:` is a list of at most {MAX_SCHEDULES}.")
    for index, item in enumerate(items, 1):
        label = f"{where}: schedule {index}"
        try:
            specs.append(_one(root, item, zone, label))
        except ScheduleError as error:
            problems.append(str(error))
    names = [spec.name for spec in specs]
    for name in sorted({n for n in names if names.count(n) > 1}):
        problems.append(f"{where}: two schedules are named {name!r}; only the first runs.")
    seen = set()
    specs[:] = [spec for spec in specs if not (spec.name in seen or seen.add(spec.name))]


def _one(root, item, zone, label):
    if not isinstance(item, dict) or set(item) - {"name", "every", "cron", "checks", "tags", "sim"}:
        raise ScheduleError(f"{label} takes name, every or cron, checks or tags, and sim. A real "
                            "iPhone is attached on this Mac, never in a repository file.")
    name = item.get("name")
    if not isinstance(name, str) or not NAME.fullmatch(name.strip()):
        raise ScheduleError(f"{label} needs a `name:` of letters, numbers, spaces, - and _.")
    name = name.strip()
    label = f"{label} ({name})"
    if ("every" in item) == ("cron" in item):
        raise ScheduleError(f"{label} needs `every:` or `cron:`, one of them.")
    when = item.get("every") if "every" in item else item.get("cron")
    every, expr = parse_when(when, label)
    if "cron" in item and every:
        raise ScheduleError(f"{label}: `cron:` takes a five-field cron; use `every:` for an interval.")
    if "every" in item and expr:
        raise ScheduleError(f"{label}: `every:` takes an interval such as 1h; use `cron:` for a cron.")
    checks = item.get("checks") or []
    tags = item.get("tags") or []
    for field_name, value in (("checks", checks), ("tags", tags)):
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value) or len(value) > 50:
            raise ScheduleError(f"{label}: `{field_name}:` is a list of text.")
    sim = item.get("sim")
    if sim is not None and (not isinstance(sim, str) or not sim.strip() or len(sim) > 100):
        raise ScheduleError(f"{label}: `sim:` is a simulator, such as iPhone 17 Pro. A real iPhone is attached on "
                            "this Mac, never in a repository file.")
    return Spec(root, name, str(when).strip(), every, expr, zone, tuple(c.strip() for c in checks),
                tuple(t.strip() for t in tags), sim.strip() if sim else None)


def state_path(root):
    return Path(root) / ".mobster" / "test-results" / STATE


def read_state(root):
    try:
        data = json.loads(state_path(root).read_text(encoding="utf-8"))
        schedules = data["schedules"]
        if data.get("schema") != SCHEMA or not isinstance(schedules, dict):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}
    out = {}
    for name, item in schedules.items():
        if isinstance(name, str) and isinstance(item, dict):
            runs = [r for r in item.get("runs") or () if isinstance(r, dict) and r.get("outcome") in ("ran", "skipped")]
            out[name] = {"nextAt": item["nextAt"] if isinstance(item.get("nextAt"), int) else None,
                         "runs": [{key: r.get(key) for key in ("at", "outcome", "reason", "suite", "verdict")}
                                  for r in runs][-KEEP_RUNS:]}
    return out


def _update(root, change):
    """Read the state under its lock, let ``change(state)`` edit it, write it back. Return whether the write succeeded;
    never raise."""
    try:
        with writer(state_path(root)):
            state = read_state(root)
            change(state)
            write_text_atomic(state_path(root), json.dumps({"schema": SCHEMA, "schedules": state}) + "\n")
        return True
    except Exception:  # noqa: BLE001 -- a failed claim never permits a run
        return False


def record(root, name, at, outcome, reason=None, suite=None, verdict=None):
    entry = {"at": at, "outcome": outcome, "reason": reason, "suite": suite, "verdict": verdict}

    def change(state):
        item = state.setdefault(name, {"nextAt": None, "runs": []})
        item["runs"] = (item["runs"] + [entry])[-KEEP_RUNS:]
    _update(root, change)


def listing(root, now_ms):
    """Each schedule of the project with its next time and last runs, and the problems with its files."""
    specs, problems = load(root)
    state = read_state(root)
    devices = read_devices()
    off = read_off()
    if off is None:
        problems = problems + [f"Mobster can't read {OFF} in its data folder, so no schedule runs. Turn one on or off "
                               "to write it again."]
    rows = []
    for spec in specs:
        item = state.get(spec.name) or {"nextAt": None, "runs": []}
        last = item["runs"][-1] if item["runs"] else None
        on = off is not None and device_key(root, spec.name) not in off
        rows.append({"name": spec.name, "when": spec.when, "source": spec.source, "sim": spec.sim,
                     "device": devices.get(device_key(root, spec.name)), "tags": list(spec.tags),
                     "checks": list(spec.checks), "on": on,
                     "nextAt": (item["nextAt"] or _first(spec, now_ms)) if on else None,
                     "last": last, "runs": item["runs"]})
    return {"project": Path(root).name, "root": str(root), "schedules": rows, "problems": problems}


def _first(spec, now_ms):
    try:
        return spec.next_after(now_ms)
    except ValueError:
        return None


# No repository file names a phone: one is attached on this Mac, and only a running Mobster uses it.
def device_key(root, name):
    return f"{Path(root).resolve()}|{name}"


def read_devices(data_dir=None):
    try:
        data = json.loads((Path(data_dir or dev_data_dir()) / DEVICES).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(data, dict) \
        else {}


def set_device(root, name, device, *, data_dir=None):
    """Attach a real iPhone (or a named simulator) to a schedule on this Mac; None takes it off."""
    path = Path(data_dir or dev_data_dir()) / DEVICES
    with writer(path):
        devices = read_devices(data_dir)
        if device:
            if not isinstance(device, str) or len(device) > 100:
                raise ScheduleError("Name the device as `mobster devices` lists it.")
            devices[device_key(root, name)] = device.strip()
        else:
            devices.pop(device_key(root, name), None)
        write_text_atomic(path, json.dumps(devices) + "\n")


def read_off(data_dir=None):
    """The schedules turned off on this Mac, by ``device_key``; None when the file can't be read, which runs none."""
    path = Path(data_dir or dev_data_dir()) / OFF
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    except (OSError, ValueError):
        return None
    return {key for key in data if isinstance(key, str)} if isinstance(data, list) else None


def set_on(root, name, on, *, data_dir=None):
    """Turn a schedule off on this Mac, or on again: the repository file stays as it is. An unreadable file is
    written again with this change alone."""
    path = Path(data_dir or dev_data_dir()) / OFF
    with writer(path):
        off = read_off(data_dir) or set()
        (off.discard if on else off.add)(device_key(root, name))
        write_text_atomic(path, json.dumps(sorted(off)) + "\n")


def macos_notification(title, body, *, run=subprocess.run, platform=sys.platform):
    """A notification on this Mac. The text goes in as arguments to a fixed script, never into the script."""
    if platform != "darwin":
        return False
    script = ["-e", "on run argv", "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
              "-e", "end run"]
    try:
        run([OSASCRIPT, *script, title[:120], body[:240]], check=False, timeout=10, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


class TestSchedules:
    """Runs the schedules of the projects `mobster test` has been used in."""
    __test__ = False

    def __init__(self, *, roots=None, guard=None, plan=None, execute=None, clock=time.time, notify=None,
                 macos=macos_notification, devices=None, off=None, spawn=None):
        self.roots = roots
        self.guard = guard or (lambda spec, device: None)
        self._plan, self._execute = plan, execute
        self.clock = clock
        self.notify = notify
        self.macos = macos
        self.devices = devices or read_devices
        self.off = off or read_off
        self.spawn = spawn or self._thread
        self._worker = None             # the background suite this service owns
        self._running = None            # (root, name) of the scheduled suite going now
        self._lock = threading.Lock()
        self._checked = 0.0
        self.stop = threading.Event()

    def _thread(self, work):
        with self._lock:
            if self.stop.is_set():
                self._running = None
                return False
            worker = self._worker = threading.Thread(target=work, name="mobster-test-schedule", daemon=True)
            try:
                worker.start()
            except RuntimeError:
                self._worker = self._running = None
                raise
        return True

    def _project_roots(self):
        if self.roots is not None:
            return list(self.roots())
        from .recent import known_roots
        return known_roots()

    def tick(self):
        """Called by the scheduler worker every second. Never raises."""
        if self.stop.is_set() or time.monotonic() - self._checked < CHECK_SECONDS:
            return
        self._checked = time.monotonic()
        try:
            for root in self._project_roots():
                self.tick_project(root)
        except Exception:  # noqa: BLE001
            pass

    def tick_project(self, root):
        specs, _ = load(root)
        now = round(self.clock() * 1000)
        due = []
        off = self.off()
        if off is None:
            return                                           # can't tell which are off: run none

        def claim(state):
            for spec in specs:
                item = state.setdefault(spec.name, {"nextAt": None, "runs": []})
                if device_key(spec.root, spec.name) in off:
                    item["nextAt"] = None                    # off: turned on again, it waits for its next slot
                elif item["nextAt"] is None:
                    item["nextAt"] = _first(spec, now)       # first seen: wait for the next slot, never run at once
                elif item["nextAt"] <= now:
                    slot = item["nextAt"]
                    item["nextAt"] = _first(spec, now)       # claimed before it runs: no second run, no pile-up
                    due.append((spec, slot))
            for name in set(state) - {spec.name for spec in specs}:
                del state[name]                              # a schedule taken out of the files
        if not _update(root, claim):
            return
        for spec, slot in due:
            if now - slot > LATE_MS:
                record(root, spec.name, now, "skipped", "Mobster wasn't running at the scheduled time (the Mac "
                       "slept or Mobster was closed), so this slot was not made up.")
                continue
            self.start(spec, trigger="schedule")

    def start(self, spec, *, trigger, device=None, sim=None, wait=False, progress=None):
        """Run ``spec`` now unless something else is going."""
        with self._lock:
            if self.stop.is_set():
                return self._skip(spec, "Mobster is stopping, so this scheduled run was skipped.")
            if self._running is not None:
                reason = ("Another scheduled run is going" if self._running != (str(spec.root), spec.name)
                          else "This schedule's last run is still going") + ", so this one was skipped, not queued."
                record(spec.root, spec.name, round(self.clock() * 1000), "skipped", reason)
                return {"outcome": "skipped", "reason": reason}
            self._running = (str(spec.root), spec.name)
        if wait:
            return self._run(spec, device, sim, progress)
        if self.spawn(lambda: self._run(spec, device, sim, progress)) is False:
            return self._skip(spec, "Mobster is stopping, so this scheduled run was skipped.")
        return {"outcome": "started", "reason": None}

    def _skip(self, spec, reason):
        record(spec.root, spec.name, round(self.clock() * 1000), "skipped", reason)
        return {"outcome": "skipped", "reason": reason}

    def _run(self, spec, device, sim, progress):
        try:
            if self.stop.is_set():
                return self._skip(spec, "Mobster is stopping, so this scheduled run was skipped.")
            device = device if device is not None else self.devices().get(device_key(spec.root, spec.name))
            sim = sim or spec.sim
            why = self.guard(spec, device) if device else None
            if why:
                return self._skip(spec, why)
            from .api import Options, UsageError, execute, plan
            options = Options(paths=tuple(spec.checks), tags=spec.tags, sims=(sim,) if sim and not device else (),
                              devices=(device,) if device else (), command=f"scheduled: {spec.name}")
            try:
                planned = (self._plan or plan)(options, cwd=spec.root)
            except UsageError as error:
                return self._skip(spec, f"The project can't plan this run: {error}")
            if not planned.entries or not planned.targets:
                return self._skip(spec, "No check matches this schedule.")
            doc, paths = (self._execute or execute)(planned, progress=progress, cwd=spec.root, stop=self.stop)
            return self._finish(spec, doc, paths)
        except Exception as error:  # noqa: BLE001 -- a scheduled run never takes the scheduler down
            return self._skip(spec, f"The run stopped before it finished ({type(error).__name__}).")
        finally:
            with self._lock:
                self._running = None

    def _finish(self, spec, doc, paths):
        cells = [r for check in doc.get("checks") or () for r in check.get("results") or ()
                 if not check.get("quarantined")]
        held = [r for r in cells if r.get("status") == "couldnt_run"
                and ((r.get("reason") or {}).get("class") in ("busy", "device_lost"))]
        if cells and len(held) == len(cells):
            return self._skip(spec, (held[0].get("reason") or {}).get("message") or "The device was busy.")
        verdict = {0: "passed", 1: "failed", 2: "needs_review", 3: "couldnt_run"}.get(doc.get("exitCode"), "failed")
        record(spec.root, spec.name, round(self.clock() * 1000), "ran", None, doc.get("suite"), verdict)
        if verdict != "passed" and not self.stop.is_set():
            self._tell(spec, doc, paths)
        return {"outcome": "ran", "reason": None, "suite": doc.get("suite"), "verdict": verdict,
                "exitCode": doc.get("exitCode")}

    def _tell(self, spec, doc, paths):
        """Slack by the project's rule when it has one; else a notification on this Mac. Never raises."""
        try:
            from . import notify as slack
            if slack.load_rules(spec.root) is not None:
                (self.notify or slack.notify)(spec.root, paths.get("folder"), doc)
                return
        except Exception:  # noqa: BLE001 -- a broken rules file falls back to the Mac notification
            pass
        try:
            counts = doc.get("summary") or {}
            bad = counts.get("failed", 0) + counts.get("needsReview", 0) + counts.get("couldntRun", 0)
            self.macos(f"{Path(spec.root).name}: {spec.name} didn't pass",
                       f"{bad} check{'' if bad == 1 else 's'} didn't pass. Open Mobster's test results.")
        except Exception:  # noqa: BLE001
            pass

    def close(self, timeout=5):
        """Cancel the suite and wait for its worker; false retains runtime ownership while it is still active."""
        with self._lock:
            self.stop.set()
            worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout)
        return worker is None or not worker.is_alive()
