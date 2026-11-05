import hashlib
import json
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from ..verify.report import dots, has_node
from .targets import Refused, Target, TargetError, for_target

EXIT_CODES = {"passed": 0, "failed": 1, "needs_review": 2, "couldnt_run": 3}
# A cell's status when its repetitions disagree, and the suite's exit code: the first of these present wins.
WORST_FIRST = ("failed", "couldnt_run", "needs_review", "passed")
# couldnt_run classes a second attempt can fix.
RETRY_CLASSES = frozenset({"wda", "simulator", "launch", "busy", "internal", "replay"})
SAFETY_STOPS = ("unknown_outcome", "journal")
SAFETY_EXIT_STOPS = (*SAFETY_STOPS, "refused")
STOP_RANK = {"stopped": 0, "refused": 1, "journal": 2, "unknown_outcome": 3}
MAX_RETRIES, MAX_REPEAT, MAX_PARALLEL = 5, 20, 8
NOT_RUN = "The suite was stopped before this ran."
MARKS = {"passed": "✓", "failed": "✗", "needs_review": "!", "couldnt_run": "✗"}
# A real iPhone is asked this long whether it still answers after a check couldn't run on it.
ALIVE_TIMEOUT = 3.0
LOST = ("{name} stopped answering while this check was running. Mobster didn't run it again, because it can't tell "
        "how far it got.")
LOST_FIX = "Reconnect and unlock the iPhone, then run the check again."
LOST_BEFORE = "{name} stopped answering earlier in this run, so this check didn't run on it."
BUSY_BEFORE = "{name} is in use by another Mobster app or command, so this check didn't run on it."
NO_DEVICE_LEFT = "Every device in this run stopped answering or is in use elsewhere, so this check didn't run."
# Where a spread check that never started is reported: on no device.
NOT_RUN_TARGET = Target("none", "device", record={"id": "none", "name": "Not run", "kind": "none"})


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
            "videoOffsetMs": result.get("video_offset_ms"), "device": dict(result.get("device") or {}),
            "costUsd": result.get("cost_usd") or 0,
            "replay": dict(result.get("replay") or {}) or None,
            "linkFirst": dict((result.get("agent") or {}).get("linkFirst") or {}) or None,
            "conditions": result.get("conditions"),
            "business": _business_proof(result), "visual": dict(result.get("visual") or {}) or None,
            "findings": [dict(item) for item in result.get("findings") or () if isinstance(item, dict)],
            "screen": dict(result.get("screen") or {}) or None,
            "snapshot": snapshot_of(result),
            "evidence": dict(result.get("evidence") or {}) or None, "stepProof": result.get("step_proof"),
            "perf": result.get("perf") or None, "screenCoverage": dict(result.get("screenCoverage") or {}) or None,
            "healed": [dict(item) for item in result.get("healed") or () if isinstance(item, dict)],
            "assertions": [{"index": item.get("index"), "text": item.get("text"), "ok": bool(item.get("ok")),
                            "observed": item.get("observed"), "outlined": has_node(item)}
                           for item in result.get("assertions") or () if isinstance(item, dict)]}


def _business_proof(result):
    """Keep lifecycle proof and registered field IDs, without app-produced values."""
    business = result.get("business") or {}
    if not isinstance(business, dict) or not business:
        return None
    declared = ((result.get("check") or {}).get("business") or {}).get("expect")
    observed = business.get("assertions")
    fields = ([item.get("field") for item in declared] if isinstance(declared, list) and
              all(isinstance(item, dict) and isinstance(item.get("field"), str) for item in declared) else None)
    complete = (fields is not None and len(fields) == len(set(fields)) and isinstance(observed, list)
                and len(observed) == len(fields) and
                all(isinstance(item, dict) and type(item.get("ok")) is bool for item in observed) and
                [item.get("field") for item in observed] == fields)
    generation = business.get("generation")
    source_digest = business.get("sourceSha256")
    return {"bindingSha256": business.get("bindingSha256"), "setup": business.get("setup"),
            "status": business.get("status"), "cleanup": business.get("cleanup"),
            "generation": generation if type(generation) is int and generation >= 1 else None,
            "sourceSha256": source_digest if isinstance(source_digest, str) and
                            re.fullmatch(r"[0-9a-f]{64}", source_digest) else None,
            "assertionFields": fields if complete else None,
            "passedFields": [item["field"] for item in observed if item["ok"]] if complete else None,
            "failedFields": [item["field"] for item in observed if not item["ok"]] if complete else None}


def snapshot_of(result):
    """The run's snapshot (verify/snapshot_run.py) with its image paths made absolute, or None."""
    snapshot = result.get("snapshot")
    run_dir = result.get("run_dir")
    if not isinstance(snapshot, dict):
        return None
    out = dict(snapshot)
    for key in ("baseline", "current", "diff"):
        if out.get(key) and run_dir:
            out[key] = f"{run_dir}/{out[key]}"
    return out


def retryable(record):
    if record["class"] in SAFETY_STOPS:
        return False
    if record["verdict"] == "failed":
        return True
    return record["verdict"] == "couldnt_run" and record["class"] in RETRY_CLASSES


def device_label(target, attempts, check=None, resolved=None):
    # Any idle emulator's record names no device, so a default-target Android run is named by what it reported.
    named = resolved if resolved is not None and resolved.get("kind") != "android" else target.record
    if named is not None:
        device = (attempts[-1].get("device") or {}) if attempts else {}
        runtime = device.get("runtime")
        label = (named.get("name") or named.get("id") or "device") + (
            f" · {runtime}" if runtime and named.get("kind") != "simulator" else "")
        return label, target.key if named is target.record else (
            f"device:{named.get('id') or named.get('udid') or named.get('name') or 'unknown'}")
    for record in reversed(attempts):
        device = record.get("device") or {}
        if device.get("type") and device.get("runtime") and device.get("udid"):
            return f"{device['type']} · {device['runtime']}", f"sim:{device['type']}@{device['runtime']}"
    if target.kind == "default":
        named = getattr(check, "device", None)
        label = " · ".join(filter(None, [named or "Default simulator", getattr(check, "runtime", None)]))
        # Until a run reports its simulator, the label says each check picks its own; the key stays stable.
        return (label if named else target.label), f"default:{label}"
    return target.label, target.key


GB_PER_SIMULATOR = 4   # the memory budget for a simulator, WebDriverAgent and a run


def machine_parallel_cap(cpus=None, memory=None, environ=None):
    """How many simulators this Mac runs at once: one per two cores and per 4 GB of memory, 1 to MAX_PARALLEL."""
    import os
    environ = os.environ if environ is None else environ
    try:
        fixed = int(environ.get("MOBSTER_TEST_MAX_PARALLEL", ""))
    except ValueError:
        fixed = 0
    if fixed > 0:
        return min(fixed, MAX_PARALLEL)
    cpus = cpus if cpus is not None else (os.cpu_count() or 2)
    if memory is None:
        try:
            memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        except (ValueError, OSError, AttributeError):
            memory = 16 * 1024 ** 3
    return max(1, min(MAX_PARALLEL, cpus // 2, int(memory // (GB_PER_SIMULATOR * 1024 ** 3))))


class Suite:
    """``attempt(check, record, *, label, video_name)`` runs one attempt and returns verify's result (execute.Attempts,
    or a fake)."""

    def __init__(self, entries, targets, *, attempt, retries=1, repeat=1, parallel=1, strict=False, gate=None,
                 named_device=None, progress=None, stop=None, clock=time.time, on_cancel=None, spread=False,
                 alive=None, cap=None, before_attempt=None):
        self.entries, self.targets = list(entries), list(targets)
        self.attempt = attempt
        self.retries = max(0, min(int(retries), MAX_RETRIES))
        self.repeat = max(1, min(int(repeat), MAX_REPEAT))
        self.parallel = max(1, min(int(parallel), MAX_PARALLEL))
        self.progress = progress or (lambda line: None)
        cap = machine_parallel_cap() if cap is None else cap
        if self.parallel > cap:
            self.progress(f"This Mac’s configured simulator limit is {cap}, so --parallel {self.parallel} runs {cap}. "
                          "MOBSTER_TEST_MAX_PARALLEL=N changes the limit.")
            self.parallel = cap
        self.strict = bool(strict)
        self.gate, self.named_device = gate, named_device
        self.stop = stop or threading.Event()
        self.clock = clock
        self.on_cancel = on_cancel
        self.before_attempt = before_attempt
        self.spread = bool(spread)
        self.alive = alive or phone_answers   # alive(target) -> whether a real iPhone still answers
        self.out = {}   # target key -> "lost" | "busy": devices that take no more checks in this run
        self._inflight = 0
        self.interrupted = None   # the KeyboardInterrupt (ctrl+c, or devtools.Terminated) that stopped the run
        self.units = []
        self.started_at = self.finished_at = None
        self._lock = threading.Lock()
        self._abort_called = False
        self._sealed = False
        self.stop_reason = None
        bind_gate = getattr(attempt, "bind_gate", None)
        self._leased_real = callable(bind_gate)
        if self._leased_real:
            bind_gate(gate)
        self._plan()

    def _plan(self):
        index = 0
        for entry in self.entries:
            if self.spread:
                for repetition in range(1, self.repeat + 1):
                    index += 1
                    refused = None if entry.check is not None else self._resolve(entry, None)[2]
                    self.units.append(Unit(index, entry, NOT_RUN_TARGET if refused else None, repetition,
                                           refused=refused))
                continue
            for target in self.targets:
                check, record, refused, notes = self._resolve(entry, target)
                if self.repeat > 1 and check is not None:
                    bind_repeat = getattr(self.attempt, "bind_repeat", None)
                    if callable(bind_repeat):
                        bind_repeat(check, record)
                for repetition in range(1, self.repeat + 1):
                    index += 1
                    self.units.append(Unit(index, entry, target, repetition, check=check, record=record,
                                           refused=refused, notes=list(notes)))

    def _resolve(self, entry, target):
        if entry.check is None:
            return None, None, {"class": "usage", "message": entry.error or "The check can't be read.", "fix": None}, []
        if target is not None and target.real and not self._leased_real:
            return None, None, {"class": "device", "message": "Real-device checks need a runner that holds the "
                                "device lease during app admission.", "fix": None}, []
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
                unit.attempts and (self.stop.is_set() or not self._needs_more(unit))))

    def cancel(self, reason="stopped"):
        """Seal dispatch and abort in-flight work once, including an already-set external stop."""
        with self._lock:
            if self._sealed:
                return False
            self.stop.set()
            if self.stop_reason is None or STOP_RANK[reason] > STOP_RANK[self.stop_reason]:
                self.stop_reason = reason
            abort = not self._abort_called
            self._abort_called = True
        if abort and self.on_cancel is not None:
            self.on_cancel()
        return True

    def seal(self):
        """Commit the terminal stop decision before reports are serialized."""
        with self._lock:
            if self.stop.is_set() and self.stop_reason is None:
                self.stop_reason = "stopped"
            self._sealed = True

    def run(self):
        """Run every unit; returns ``results()``. Stops early, between attempts, once ``stop`` is set."""
        self.started_at = self.clock()
        if self.stop.is_set():
            self.cancel()
        workers = []
        if self.spread:
            queue = deque(unit for unit in self.units if unit.refused is None)
            sims = [target for target in self.targets if target.record is None]
            lanes = [(queue, target) for target in self.targets if target.record is not None]
            lanes += [(queue,
                       sims[number % len(sims)]) for number in range(max(self.parallel, len(sims)))] if sims else []
        else:
            queues = {}
            for unit in self.units:
                if unit.refused is None:
                    record = unit.record
                    lane = (f"device:{record.get('udid') or record.get('id') or 'unknown'}"
                            if record is not None else "sims")
                    queues.setdefault(lane, deque()).append(unit)
            lanes = [(queue, None) for lane, queue in queues.items()
                     for _ in range(self.parallel if lane == "sims" else 1)]
        try:
            for queue, target in lanes:
                worker = threading.Thread(target=self._work, args=(queue, target), daemon=True,
                                          name=f"mobster-test-{len(workers) + 1}")
                workers.append(worker)
                worker.start()
            for worker in workers:
                while worker.is_alive():
                    if self.stop.is_set():
                        self.cancel()
                    worker.join(.2)
        except BaseException as error:
            if isinstance(error, KeyboardInterrupt):
                self.interrupted = error
            self.cancel()
            self.progress("Stopping: the runs in flight end and write their reports.")
            # A terminal report cannot outrun a worker that still owns a device: its final action may be uncertain.
            while any(worker.is_alive() for worker in workers):
                for worker in workers:
                    if worker.is_alive():
                        try:
                            worker.join(.2)
                        except KeyboardInterrupt as second:
                            self.interrupted = self.interrupted or second
            if not isinstance(error, KeyboardInterrupt):
                raise
        if self.stop.is_set():
            self.cancel()
        self.finished_at = self.clock()
        return self.results()

    def _take(self, queue, target):
        """The next unit for a worker. Spread: it goes to ``target``; a unit that target refuses is finished here."""
        while True:
            with self._lock:
                if self.stop.is_set():
                    return None
                unit = queue.popleft() if queue else None
                if unit is not None:
                    unit.started = True
                    self._inflight += 1
                elif not self._inflight:
                    return None
            if unit is None:
                time.sleep(.02)
                continue
            if self.spread:
                unit.target = target
                unit.check, unit.record, unit.refused, unit.notes = self._resolve(unit.entry, target)
                if self.repeat > 1 and unit.check is not None:
                    bind_repeat = getattr(self.attempt, "bind_repeat", None)
                    if callable(bind_repeat):
                        bind_repeat(unit.check, unit.record)
                if unit.refused is not None:
                    with self._lock:
                        self._inflight -= 1
                    continue
            return unit

    def _work(self, queue, target):
        while True:
            if target is not None and target.key in self.out:
                return
            unit = self._take(queue, target)
            if unit is None:
                return
            try:
                self._run_unit(unit, queue)
            finally:
                with self._lock:
                    self._inflight -= 1

    def _needs_more(self, unit):
        last = unit.attempts[-1] if unit.attempts else None
        if last is None:
            return True
        return last["verdict"] != "passed" and retryable(last) and len(unit.attempts) <= self.retries

    def _run_unit(self, unit, queue):
        if unit.target.key in self.out and not self.spread:
            return   # its device is gone for this run; the cell says so
        while not self.stop.is_set():
            number = len(unit.attempts) + 1
            label = self._label(unit, number)
            self.progress(f"▸ {label}")
            validation_refused = False
            try:
                if self.before_attempt is not None:
                    self.before_attempt(unit.entry, unit.check, unit.record)
            except Refused as error:
                validation_refused = True
                self.cancel("refused")
                result = {"verdict": "couldnt_run", "reason": {"class": error.klass, "message": error.message,
                          "fix": error.fix or None}}
            except Exception as error:
                validation_refused = True
                self.cancel("refused")
                result = {"verdict": "couldnt_run", "reason": {"class": "internal", "message":
                          f"Mobster couldn't check whether this run is still allowed ({type(error).__name__})."}}
            else:
                if self.stop.is_set():
                    self.cancel()
                    result = {"verdict": "couldnt_run", "reason": {"class": "stopped", "message": NOT_RUN}}
                else:
                    try:
                        result = self.attempt(unit.check, unit.record, label=label,
                                              video_name=f"{unit.index:03d}-{_slug(unit.entry.name)}-{number}")
                    except Exception as error:  # an attempt runner that raises: couldn't run, class internal
                        result = {"verdict": "couldnt_run", "reason": {"class": "internal", "message":
                                  f"Mobster hit an unexpected error ({type(error).__name__})."}}
            record = self._lost(unit.target, attempt_record(result, number))
            if validation_refused:
                record["_validation_refused"] = True
            if self.spread and not unit.attempts and record["class"] == "busy" and unit.target.real and \
                    result.get("not_dispatched"):
                # Held by another Mobster process before anything was tapped: another device takes the check.
                with self._lock:
                    self.out[unit.target.key] = "busy"
                    unit.started, unit.target = False, None
                    queue.appendleft(unit)
                self.progress(f"! {unit.entry.name}: {record['message']} Another device takes it.")
                return
            with self._lock:
                unit.notes.extend(result.get("notes") or ())
                unit.attempts.append(record)
                safety_stop = record["class"] in SAFETY_STOPS
                if safety_stop:
                    self.stop.set()
                more = self._needs_more(unit) and not self.stop.is_set()
            if safety_stop:
                self.cancel(record["class"])
            self.progress(self._ended(unit, record, more))
            if not more:
                return

    def _lost(self, target, record):
        """``record``, or device_lost when a real iPhone's check couldn't run because the phone stopped answering."""
        # Never run again: Mobster can't tell how far it got, and a step may have sent something.
        if record["verdict"] != "couldnt_run" or record["class"] not in ("wda", "device") or not target.real \
                or self.alive(target):
            return record
        with self._lock:
            self.out[target.key] = "lost"
        return {**record, "class": "device_lost", "message": LOST.format(name=f"“{dots(target.label)}”"),
                "fix": LOST_FIX}

    def _label(self, unit, number, attempts=()):
        """The line's name: the simulator a default-target run reported once it has, else the target's own label."""
        where = device_label(unit.target, attempts, unit.entry.check)[0] if unit.target.kind == "default" \
            else unit.target.label
        parts = [f"{unit.entry.name} on {dots(where)}"]
        if self.repeat > 1:
            parts.append(f"run {unit.repetition} of {self.repeat}")
        if number > 1:
            parts.append(f"attempt {number} of {self.retries + 1}")
        return parts[0] + (f" ({', '.join(parts[1:])})" if len(parts) > 1 else "")

    def _ended(self, unit, record, more):
        mark = MARKS.get(record["verdict"], "?")
        word = {"passed": "passed", "failed": "failed", "needs_review": "needs review",
                "couldnt_run": "couldn't run"}.get(record["verdict"], record["verdict"])
        line = f"{mark} {word}  {self._label(unit, record['attempt'], [record])}  ({record['seconds']} s)"
        if record["verdict"] != "passed" and record.get("message"):
            line += f"\n    {record['message']}"
        if more:
            line += "\n    Trying again."
        return line

    def _units(self, entry, target):
        return [unit for unit in self.units if unit.entry is entry and (unit.target or NOT_RUN_TARGET) is target]

    def _targets_of(self, entry):
        """The targets ``entry`` has a result on: every target, or, spread, the ones its units ran on (or none)."""
        if not self.spread:
            return self.targets
        found = {}
        for unit in self.units:
            if unit.entry is entry:
                target = unit.target or NOT_RUN_TARGET
                found[target.key] = target
        return list(found.values())

    def _unstarted(self, target):
        """Why a unit that never ran has no attempt: its device was lost or busy, none was left, or the suite ended."""
        state = self.out.get(target.key)
        if state == "lost":
            return {"class": "device_lost", "message": LOST_BEFORE.format(name=f"“{dots(target.label)}”"),
                    "fix": LOST_FIX}
        if state == "busy":
            return {"class": "busy", "message": BUSY_BEFORE.format(name=f"“{dots(target.label)}”"), "fix": None}
        if self.spread and self.out and not self.stop.is_set():
            return {"class": "device_lost", "message": NO_DEVICE_LEFT, "fix": LOST_FIX}
        return {"class": "stopped", "message": NOT_RUN, "fix": None}

    def cell(self, entry, target):
        """One check on one target, across its repetitions: results.json's per-device result."""
        units = self._units(entry, target)
        finals, attempts = [], []
        for unit in units:
            if unit.refused is not None:
                finals.append(("couldnt_run", {}, dict(unit.refused)))
                continue
            if not unit.attempts:
                finals.append(("couldnt_run", {}, self._unstarted(unit.target or target)))
                continue
            last = unit.attempts[-1]
            finals.append((last["verdict"], last, {"class": last["class"], "message": last["message"],
                                                   "fix": last["fix"]}))
            attempts.extend({**record, "repetition": unit.repetition} for record in unit.attempts)
        passed = sum(verdict == "passed" for verdict, _, _ in finals)
        status = worst({verdict for verdict, _, _ in finals})
        selected, reason = next(((record, why) for verdict, record, why in reversed(finals)
                                 if verdict == status), ({}, None))
        safety_record = next((record for klass in SAFETY_STOPS for record in reversed(attempts)
                              if record["class"] == klass), None)
        safety_record = safety_record or next((record for record in reversed(attempts)
                                               if record.get("_validation_refused")), None)
        safety_stop = None
        if safety_record is not None:
            status, selected = "couldnt_run", safety_record
            reason = {"class": safety_record["class"], "message": safety_record["message"],
                      "fix": safety_record["fix"]}
            safety_stop = safety_record["class"] if safety_record["class"] in SAFETY_STOPS else "refused"
        verdicts = {record["verdict"] for record in attempts}
        flaky = "passed" in verdicts and len(verdicts) > 1
        label, key = device_label(target, attempts, entry.check, units[0].record if units else None)
        notes = list(dict.fromkeys(note for unit in units for note in unit.notes))
        skipped = (selected.get("conditions") or {}).get("skipped") or ()
        if skipped:
            notes.append("Skipped on this device: " + " ".join(f"{item['key']}: {item['why']}" for item in skipped))
        out = {"device": label, "deviceKey": key, "status": status, "attempts": len(attempts), "flaky": flaky,
               "runId": selected.get("runId"), "runDir": selected.get("runDir"),
               "report": selected.get("report"), "visual": selected.get("visual"),
               "business": selected.get("business"),
               "screenCoverage": selected.get("screenCoverage"),
               "durationMs": round(sum(r["seconds"] for r in attempts) * 1000),
               "healed": list(selected.get("healed") or ()), "reason": None if status == "passed" else reason,
               "quarantined": entry.quarantined is not None,
               "frame": selected.get("frame"), "video": selected.get("video"),
               "videoOffsetMs": selected.get("videoOffsetMs"),
               "assertions": list(selected.get("assertions") or ()), "stepProof": selected.get("stepProof"),
               "costUsd": round(sum(r.get("costUsd") or 0 for r in attempts), 5),
               "replayed": (selected.get("replay") or {}).get("mode") in ("replayed", "healed"),
               "runs": [{key_: record.get(key_) for key_ in ("repetition", "attempt", "runId", "verdict", "class",
                                                             "message", "seconds", "report", "runDir", "frame",
                                                             "video", "business", "visual", "screenCoverage")}
                        for record in attempts]}
        device = selected.get("device") or {}
        out["environment"] = {"kind": "iphone" if target.real else "simulator",
                              "model": device.get("type"), "runtime": device.get("runtime")}
        if self.repeat > 1:
            out["passRate"] = round(passed / len(finals), 4) if finals else 0.0
        if flaky:
            out["flakyReason"] = next(({"class": r["class"], "message": r["message"],
                                        "assertions": [item for item in r.get("assertions") or () if not item["ok"]]}
                                       for r in attempts if r["verdict"] != "passed"), None)
        if selected.get("conditions"):
            out["conditions"] = selected["conditions"]
        if selected.get("findings"):
            out["findings"] = selected["findings"]
        if selected.get("screen"):
            out["screen"] = selected["screen"]
        if selected.get("snapshot"):
            out["snapshot"] = selected["snapshot"]
        if selected.get("perf"):
            out["perf"] = selected["perf"]
        if notes:
            out["notes"] = notes
        if safety_stop:
            out["safetyStop"] = safety_stop
        # A pass after retry still retains the earlier crash/log evidence for diagnosing flakiness.
        evidence = (_evidence([selected]) if selected else None) or _evidence(attempts)
        if evidence is not None:
            out["evidence"] = evidence
        if selected.get("linkFirst"):
            out["linkFirst"] = selected["linkFirst"]
        if selected.get("replay"):
            replay = selected["replay"]
            out["replay"] = {key_: replay.get(key_) for key_ in ("mode", "reason", "steps", "replayed", "healed",
                                                                  "live", "seconds", "costUsd", "pendingHeals",
                                                                  "status", "approvedRevision", "fixed", "saved")}
        return out

    def results(self):
        checks, devices = [], {}
        for entry in self.entries:
            results = []
            for target in self._targets_of(entry):
                result = self.cell(entry, target)
                results.append(result)
                known = devices.setdefault(result["deviceKey"], {**target.public(), "key": result["deviceKey"],
                                                                 "name": result["device"]})
                if len(result["device"]) > len(known["name"]):
                    known["name"] = result["device"]  # "Sam's iPhone · iOS 26.4" once a run there reported its iOS
            item = {"name": entry.name, "file": entry.file, "classname": entry.classname, "tags": list(entry.tags),
                    "quarantined": entry.quarantined is not None, "results": results}
            if entry.locale:
                item["locale"] = entry.locale
                identity = json.dumps((entry.file, entry.locale), ensure_ascii=False, separators=(",", ":"))
                item["caseId"] = "locale-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
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
        smart = [r for r in cells if r.get("replay")]
        if smart:
            summary["replayed"] = sum(bool(r.get("replayed")) for r in smart)
            summary["healed"] = sum(len(r.get("healed") or ()) for r in smart)
            summary["cacheHitRate"] = round(summary["replayed"] / len(smart), 4)
        linked = [r["linkFirst"] for r in cells if r.get("linkFirst")]
        if linked:
            summary["linkFirst"] = {"steps": sum(r["taken"] for r in linked),
                                    "modelCallsSaved": sum(r["modelCallsSaved"] for r in linked)}
        return {"checks": checks, "devices": list(devices.values()), "summary": summary,
                "stopReason": self.stop_reason,
                "exitCode": self.exit_code(counted),
                "startedAt": _ms(self.started_at), "finishedAt": _ms(self.finished_at),
                "durationMs": round(((self.finished_at or self.clock()) - (self.started_at or self.clock())) * 1000)}

    def exit_code(self, counted):
        if self.stop_reason in SAFETY_EXIT_STOPS:
            return 3
        if self.stop_reason == "stopped":
            signum = getattr(self.interrupted, "signum", None)
            return 130 if signum is None else 128 + int(signum)
        statuses = {result["status"] for result in counted}
        if "failed" in statuses or (self.strict and any(result["flaky"] for result in counted)):
            return 1
        if "couldnt_run" in statuses:
            return 3
        if "needs_review" in statuses:
            return 2
        return 0


def _evidence(attempts):
    for record in reversed(attempts):
        if record["verdict"] != "passed" and record.get("evidence"):
            return {**record["evidence"], "runId": record.get("runId"), "runDir": record.get("runDir")}
    return None


def phone_answers(target):
    """Whether a real iPhone's WebDriverAgent still answers. An iPhone with no address doesn't."""
    from ..devices import wda_answers
    url = (target.record or {}).get("wdaUrl") or ""
    return bool(url) and wda_answers(url, timeout=ALIVE_TIMEOUT)


def _ms(seconds):
    return round(seconds * 1000) if seconds is not None else None


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:40] or "check"
