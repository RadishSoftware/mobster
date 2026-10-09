"""A `mobster test` suite: every check on every target, ``repeat`` times each, with retries, run in parallel lanes,
then judged as one result.

Scheduling. Checks on simulators by device type share ``parallel`` slots; each simulator a run takes is held by its
lease for the whole run, so two slots never share one. A named device (a real iPhone, or one Mobster simulator by
name) has a lane of its own and runs one check at a time.

Retries. An attempt that failed, or couldn't run for a reason a second try can fix (WebDriverAgent, the simulator,
the launch, a busy device, an internal error), runs again with the check's own reset, which by default reinstalls
the app or clears its data. A check that passes on a retry passed, and is flaky.

Repeats (``--repeat K``) run each check K times and give its pass rate. As with retries, a check whose attempts
disagree is flaky; it passed only if every repetition passed.

Exit code, from the checks that aren't quarantined: 1 when any failed (or, with ``strict``, is flaky), else 3 when
any couldn't run, else 2 when any needs review, else 0.
"""

from collections import deque
from dataclasses import dataclass, field
import re
import threading
import time
from typing import Optional

from ..verify.report import has_node
from .targets import Refused, TargetError, for_target

EXIT_CODES = {"passed": 0, "failed": 1, "needs_review": 2, "couldnt_run": 3}
# A cell's status when its repetitions disagree, and the suite's exit code: the first of these present wins.
WORST_FIRST = ("failed", "couldnt_run", "needs_review", "passed")
# couldnt_run classes a second attempt can fix.
RETRY_CLASSES = frozenset({"wda", "simulator", "launch", "busy", "internal"})
MAX_RETRIES, MAX_REPEAT, MAX_PARALLEL = 5, 20, 8
NOT_RUN = "The suite was stopped before this ran."
MARKS = {"passed": "✓", "failed": "✗", "needs_review": "!", "couldnt_run": "✗"}
# After ctrl+c: how long the runs in flight get to stop and write their artifacts.
STOP_GRACE = 30.0


@dataclass
class Unit:
    """One repetition of one check on one target, and its attempts."""
    index: int
    entry: object
    target: object
    repetition: int
    check: object = None
    record: Optional[dict] = None
    refused: Optional[dict] = None
    notes: list = field(default_factory=list)
    attempts: list = field(default_factory=list)
    started: bool = False


def worst(verdicts):
    for verdict in WORST_FIRST:
        if verdict in verdicts:
            return verdict
    return "couldnt_run"


def attempt_record(result, attempt):
    """What a suite keeps of one verify result."""
    reason = result.get("reason") or {}
    frames = result.get("frames") or []
    run_dir = result.get("run_dir")
    frame = next((f for f in reversed(frames) if f.endswith("-verdict-ax.jpg")), None) or \
        next((f for f in reversed(frames) if f.endswith("-verdict.jpg")), None)
    return {"attempt": attempt, "runId": result.get("run_id"), "verdict": result.get("verdict") or "couldnt_run",
            "class": reason.get("class"), "message": reason.get("message") or result.get("summary"),
            "fix": reason.get("fix"), "summary": result.get("summary"), "seconds": result.get("seconds") or 0,
            "runDir": run_dir, "report": result.get("report"),
            "frame": f"{run_dir}/{frame}" if run_dir and frame else None, "video": result.get("video"),
            "device": dict(result.get("device") or {}), "costUsd": result.get("cost_usd") or 0,
            "assertions": [{"index": item.get("index"), "text": item.get("text"), "ok": bool(item.get("ok")),
                            "observed": item.get("observed"), "outlined": has_node(item)}
                           for item in result.get("assertions") or () if isinstance(item, dict)]}


def retryable(record):
    if record["verdict"] == "failed":
        return True
    return record["verdict"] == "couldnt_run" and record["class"] in RETRY_CLASSES


def device_label(target, attempts, check=None):
    """(label, key) of the device a cell ran on: the simulator a run reported (type · runtime), else the target's
    (for each check's own simulator: the one the check names, else the default)."""
    for record in reversed(attempts):
        device = record.get("device") or {}
        if target.record is not None:
            runtime = device.get("runtime")
            label = target.label + (f" · {runtime}" if runtime and target.record.get("kind") != "simulator" else "")
            return label, target.key
        if device.get("type") and device.get("runtime") and device.get("udid"):
            return f"{device['type']} · {device['runtime']}", f"sim:{device['type']}@{device['runtime']}"
    if target.kind == "default":
        label = " · ".join(filter(None, [getattr(check, "device", None) or "Default simulator",
                                         getattr(check, "runtime", None)]))
        return label, f"default:{label}"
    return target.label, target.key


class Suite:
    """``attempt(check, record, *, label, video_name)`` runs one attempt and returns verify's result (execute.Attempts,
    or a fake). ``progress(line)`` gets one line per start and end of an attempt."""

    def __init__(self, entries, targets, *, attempt, retries=1, repeat=1, parallel=1, strict=False, gate=None,
                 named_device=None, progress=None, stop=None, clock=time.time, on_interrupt=None):
        self.entries, self.targets = list(entries), list(targets)
        self.attempt = attempt
        self.retries = max(0, min(int(retries), MAX_RETRIES))
        self.repeat = max(1, min(int(repeat), MAX_REPEAT))
        self.parallel = max(1, min(int(parallel), MAX_PARALLEL))
        self.strict = bool(strict)
        self.gate, self.named_device = gate, named_device
        self.progress = progress or (lambda line: None)
        self.stop = stop or threading.Event()
        self.clock = clock
        self.on_interrupt = on_interrupt
        self.interrupted = None   # the KeyboardInterrupt (ctrl+c, or devtools.Terminated) that stopped the run
        self.units = []
        self.started_at = self.finished_at = None
        self._lock = threading.Lock()
        self._resolved = {}
        self._plan()

    # -- planning ----------------------------------------------------------------------------------------------

    def _plan(self):
        index = 0
        for entry in self.entries:
            for target in self.targets:
                check, record, refused, notes = self._resolve(entry, target)
                for repetition in range(1, self.repeat + 1):
                    index += 1
                    self.units.append(Unit(index, entry, target, repetition, check=check, record=record,
                                           refused=refused, notes=list(notes)))

    def _resolve(self, entry, target):
        if entry.check is None:
            return None, None, {"class": "usage", "message": entry.error or "The check can't be read.", "fix": None}, []
        try:
            check, notes, record = for_target(entry.check, target, named_device=self.named_device, gate=self.gate)
        except Refused as error:
            return None, None, {"class": error.klass, "message": error.message, "fix": error.fix or None}, []
        except TargetError as error:
            return None, None, {"class": "usage", "message": str(error), "fix": None}, []
        return check, record, None, notes

    @property
    def total(self):
        return len(self.units)

    @property
    def done(self):
        with self._lock:
            return sum(1 for unit in self.units if unit.refused is not None or (
                unit.attempts and not self._needs_more(unit)))

    # -- running -----------------------------------------------------------------------------------------------

    def run(self):
        """Run every unit; returns ``results()``. Stops early, between attempts, once ``stop`` is set."""
        self.started_at = self.clock()
        lanes = {}
        for unit in self.units:
            if unit.refused is None:
                lanes.setdefault(unit.target.lane, deque()).append(unit)
        workers = []
        for lane, queue in lanes.items():
            for number in range(self.parallel if lane == "sims" else 1):
                worker = threading.Thread(target=self._work, args=(queue,), daemon=True,
                                          name=f"mobster-test-{len(workers) + 1}")
                workers.append(worker)
                worker.start()
        try:
            for worker in workers:
                while worker.is_alive():
                    worker.join(.2)
        except KeyboardInterrupt as error:
            self.interrupted = error
            self.stop.set()
            self.progress("Stopping: the runs in flight end and write their reports.")
            if self.on_interrupt is not None:
                self.on_interrupt()
            end = time.monotonic() + STOP_GRACE
            for worker in workers:
                worker.join(max(0.0, end - time.monotonic()))
        self.finished_at = self.clock()
        return self.results()

    def _take(self, queue):
        with self._lock:
            if self.stop.is_set() or not queue:
                return None
            unit = queue.popleft()
            unit.started = True
            return unit

    def _work(self, queue):
        while True:
            unit = self._take(queue)
            if unit is None:
                return
            self._run_unit(unit)

    def _needs_more(self, unit):
        last = unit.attempts[-1] if unit.attempts else None
        if last is None:
            return True
        return last["verdict"] != "passed" and retryable(last) and len(unit.attempts) <= self.retries

    def _run_unit(self, unit):
        while not self.stop.is_set():
            number = len(unit.attempts) + 1
            label = self._label(unit, number)
            self.progress(f"▸ {label}")
            try:
                result = self.attempt(unit.check, unit.record, label=label,
                                      video_name=f"{unit.index:03d}-{_slug(unit.entry.name)}-{number}")
            except Exception as error:  # an attempt runner that raises: couldn't run, class internal
                result = {"verdict": "couldnt_run", "reason": {"class": "internal", "message":
                          f"Mobster hit an unexpected error ({type(error).__name__})."}}
            record = attempt_record(result, number)
            with self._lock:
                unit.attempts.append(record)
                more = self._needs_more(unit) and not self.stop.is_set()
            self.progress(self._ended(unit, record, more))
            if not more:
                return

    def _label(self, unit, number):
        parts = [f"{unit.entry.name} · {unit.target.label}"]
        if self.repeat > 1:
            parts.append(f"run {unit.repetition} of {self.repeat}")
        if number > 1:
            parts.append(f"attempt {number} of {self.retries + 1}")
        return parts[0] + (f" ({', '.join(parts[1:])})" if len(parts) > 1 else "")

    def _ended(self, unit, record, more):
        mark = MARKS.get(record["verdict"], "?")
        word = {"passed": "passed", "failed": "failed", "needs_review": "needs review",
                "couldnt_run": "couldn't run"}.get(record["verdict"], record["verdict"])
        line = f"{mark} {word}  {self._label(unit, record['attempt'])}  ({record['seconds']} s)"
        if record["verdict"] != "passed" and record.get("message"):
            line += f"\n    {record['message']}"
        if more:
            line += "\n    Trying again."
        return line

    # -- the result --------------------------------------------------------------------------------------------

    def _units(self, entry, target):
        return [unit for unit in self.units if unit.entry is entry and unit.target is target]

    def cell(self, entry, target):
        """One check on one target, across its repetitions: results.json's per-device result."""
        units = self._units(entry, target)
        finals, attempts, reason = [], [], None
        for unit in units:
            if unit.refused is not None:
                finals.append("couldnt_run")
                reason = reason or dict(unit.refused)
                continue
            if not unit.attempts:
                finals.append("couldnt_run")
                reason = reason or {"class": "stopped", "message": NOT_RUN, "fix": None}
                continue
            last = unit.attempts[-1]
            finals.append(last["verdict"])
            attempts.extend({**record, "repetition": unit.repetition} for record in unit.attempts)
            if last["verdict"] != "passed" and reason is None:
                reason = {"class": last["class"], "message": last["message"], "fix": last["fix"]}
        passed = finals.count("passed")
        status = "passed" if finals and passed == len(finals) else worst(set(finals) - {"passed"})
        verdicts = {record["verdict"] for record in attempts}
        flaky = "passed" in verdicts and len(verdicts) > 1
        label, key = device_label(target, attempts, entry.check)
        last = attempts[-1] if attempts else {}
        notes = list(dict.fromkeys(note for unit in units for note in unit.notes))
        out = {"device": label, "deviceKey": key, "status": status, "attempts": len(attempts), "flaky": flaky,
               "report": last.get("report"), "durationMs": round(sum(r["seconds"] for r in attempts) * 1000),
               "healed": [], "reason": None if status == "passed" else reason,
               "quarantined": entry.quarantined is not None,
               "frame": last.get("frame"), "video": last.get("video"),
               "assertions": list(last.get("assertions") or ()),
               "costUsd": round(sum(r.get("costUsd") or 0 for r in attempts), 5),
               "runs": [{key_: record.get(key_) for key_ in ("repetition", "attempt", "runId", "verdict", "class",
                                                             "message", "seconds", "report", "runDir", "frame",
                                                             "video")} for record in attempts]}
        if self.repeat > 1:
            out["passRate"] = round(passed / len(finals), 4) if finals else 0.0
        if flaky:
            out["flakyReason"] = next(({"class": r["class"], "message": r["message"],
                                        "assertions": [item for item in r.get("assertions") or () if not item["ok"]]}
                                       for r in attempts if r["verdict"] != "passed"), None)
        if notes:
            out["notes"] = notes
        return out

    def results(self):
        checks, devices = [], {}
        for entry in self.entries:
            results = []
            for target in self.targets:
                result = self.cell(entry, target)
                results.append(result)
                known = devices.setdefault(result["deviceKey"], {**target.public(), "key": result["deviceKey"],
                                                                 "name": result["device"]})
                if len(result["device"]) > len(known["name"]):
                    known["name"] = result["device"]  # "Sam's iPhone · iOS 26.4" once a run there reported its iOS
            item = {"name": entry.name, "file": entry.file, "classname": entry.classname, "tags": list(entry.tags),
                    "quarantined": entry.quarantined is not None, "results": results}
            if entry.quarantined:
                item["quarantineReason"] = entry.quarantined
            if entry.error:
                item["error"] = entry.error
            checks.append(item)
        cells = [result for check in checks for result in check["results"]]
        counted = [result for result in cells if not result["quarantined"]]
        summary = {"passed": sum(r["status"] == "passed" for r in counted),
                   "failed": sum(r["status"] == "failed" for r in counted),
                   "needsReview": sum(r["status"] == "needs_review" for r in counted),
                   "couldntRun": sum(r["status"] == "couldnt_run" for r in counted),
                   "flaky": sum(bool(r["flaky"]) for r in counted),
                   "quarantined": len(cells) - len(counted), "total": len(cells),
                   "costUsd": round(sum(r["costUsd"] for r in cells), 5)}
        return {"checks": checks, "devices": list(devices.values()), "summary": summary,
                "exitCode": self.exit_code(counted),
                "startedAt": _ms(self.started_at), "finishedAt": _ms(self.finished_at),
                "durationMs": round(((self.finished_at or self.clock()) - (self.started_at or self.clock())) * 1000)}

    def exit_code(self, counted):
        statuses = {result["status"] for result in counted}
        if "failed" in statuses or (self.strict and any(result["flaky"] for result in counted)):
            return 1
        if "couldnt_run" in statuses:
            return 3
        if "needs_review" in statuses:
            return 2
        return 0


def _ms(seconds):
    return round(seconds * 1000) if seconds is not None else None


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:40] or "check"
