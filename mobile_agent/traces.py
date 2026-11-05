"""Traces: a run's tape while it runs, and its bundle after.

``MOBSTER_EXPERIMENTAL_TRACES=1`` turns traces on. A run in the app or the terminal then records a tape
(bench/tape.py): the request, every screen the agent read as data, every model reply and every action, in a private
file beside the run's frames. The secret filter masks each entry as it is written. Without durable history there is
nowhere to keep it, so nothing is recorded.

``mobster export ID --trace`` packs one finished run into a bundle (a zip):

    manifest.json     the run's facts: task, app, status, outcome, cost, model, counts, and what the bundle holds
    events.jsonl      the run's journal, one event a line: decisions, actions, refusals, cost and latency per call
    tape.jsonl.gz     the tape, when the run recorded one: replay it with bench/tape_replay.py
    screens/*.jpg     the run's screenshots, only with --screens

Every string in the bundle goes through ``secret_filter.redact``; the pictures cannot, so they stay out unless asked
for. A bundle holds task text and screen text. Nothing here uploads it.
"""

import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from . import __version__, failure_line, secret_filter

ENV_FLAG = "MOBSTER_EXPERIMENTAL_TRACES"
FORMAT = "mobster-trace/1"
TAPE = "tape.jsonl.gz"          # bench/tape.py's NAME, gzipped when the run ends
BLOB = 4096              # a longer string with no space in it is a picture or a dump: never kept
MAX_DEPTH = 64
MAX_SCREENS_BYTES = 40_000_000
# What the bundle keeps of a run's summary: how it ended and what it cost, never its answer data or result cards.
SUMMARY_KEYS = ("status", "outcome", "reason", "answer", "code", "stop_code", "actions", "decisions", "elapsed_ms",
                "costUsd", "model", "engine", "timing", "deviceKind", "failure")
RUN_KEYS = ("id", "goal", "appName", "mode", "engine", "status", "createdAt", "finishedAt", "costUsd", "origin")


def enabled(env=None):
    return (os.environ if env is None else env).get(ENV_FLAG, "").strip().casefold() in ("1", "true", "yes", "on")


# -- masking --------------------------------------------------------------------------------------------------------

def mask(value, depth=0):
    """``value`` (JSON-shaped) with the secret filter applied to every string and every key. Unlike
    ``secret_filter.redact_deep``, nothing passes unfiltered: a long unbroken string is dropped, and so is
    everything nested deeper than MAX_DEPTH."""
    if isinstance(value, str):
        if len(value) > BLOB and " " not in value[:BLOB]:
            return f"[{len(value)} characters left out]"
        return secret_filter.redact(value)
    if isinstance(value, dict):
        return {secret_filter.redact(str(key)): mask(item, depth + 1) for key, item in value.items()} \
            if depth < MAX_DEPTH else {}
    if isinstance(value, (list, tuple)):
        return [mask(item, depth + 1) for item in value] if depth < MAX_DEPTH else []
    return value


# -- recording ------------------------------------------------------------------------------------------------------

class Recording:
    """One run's tape. ``attach`` is what ``engines.build_frontier`` calls with the pair the agent is built on: it
    returns the same pair recording to a tape in the run's folder when traces are on and the run has a place to keep
    it, and the pair unchanged otherwise. ``finish`` ends the tape with the run's outcome, once."""

    def __init__(self, frames, run_id, request):
        self.frames, self.run_id, self.request, self.tape = frames, run_id, request, None

    def attach(self, driver, client, *, apps, frontier, hooks=()):
        folder = self.frames.folder(self.run_id) if self.frames is not None and enabled() else None
        if folder is None:
            return driver, client
        from .bench import tape as tape_module
        try:
            folder.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(folder.parent, 0o700)
            folder.mkdir(exist_ok=True, mode=0o700)
        except OSError:
            return driver, client
        tape = tape_module.Tape(folder / tape_module.NAME, scrub=mask)
        tape.header(request=self.request, apps=apps, model=getattr(client, "model", None), frontier=frontier,
                    driver=driver, hooks=hooks)
        self.tape = tape
        return tape_module.TapeDriver(driver, tape), tape_module.TapeClient(client, tape)

    def finish(self, outcome=None):
        """End the tape; a run that ended without an outcome (an exception) ends it as an error. A tape that stopped
        early (past its size, a write that failed) can't replay anything: it is removed."""
        tape, self.tape = self.tape, None
        if tape is None:
            return
        tape.end(outcome or {"status": "error"})
        if tape.broken:
            tape.path.unlink(missing_ok=True)
            Path(str(tape.path) + ".gz").unlink(missing_ok=True)


# -- the bundle -----------------------------------------------------------------------------------------------------

def _frame_ids(value, found):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "frameId" and isinstance(item, str):
                found.setdefault(item)
            else:
                _frame_ids(item, found)
    elif isinstance(value, list):
        for item in value:
            _frame_ids(item, found)
    return found


def _facts(summary):
    return {k: v for k, v in summary.items() if k in SUMMARY_KEYS}


def _lean(event):
    """A run's last two events carry its whole summary (answer data, result cards): the bundle keeps the facts."""
    if event.get("event") == "run_finished" and isinstance(event.get("summary"), dict):
        return {**event, "summary": _facts(event["summary"])}
    if event.get("event") == "result":
        return {k: v for k, v in event.items() if k in SUMMARY_KEYS or k in ("event", "seq", "timestamp")}
    return event


def masked_tape(path):
    """The tape at ``path`` with every entry masked again, as gzip bytes, and its entry count; None when it can't
    be read whole (a bundle never carries a tape it could not mask)."""
    import gzip  # export only: a run's start never loads gzip or zipfile (scripts/perf-budgets.json)
    try:
        text = gzip.decompress(Path(path).read_bytes()).decode("utf-8")
        entries = [mask(json.loads(line)) for line in text.splitlines() if line.strip()]
    except (OSError, ValueError, EOFError):
        return None
    body = "".join(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n" for entry in entries)
    return gzip.compress(body.encode("utf-8"), 6, mtime=0), len(entries)


def _jsonl(rows):
    return "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows).encode("utf-8")


def bundle(record_, folder, *, read_frame=None):
    """(zip bytes, manifest) for a finished run. ``record_``: the run's journal row with its ``events``; ``folder``:
    where the run's tape is (its frames folder), or None; ``read_frame(frame_id)``: bytes or None, given only when
    the screenshots are wanted."""
    import io
    import zipfile
    events = [mask(_lean(event)) for event in record_.get("events") or () if isinstance(event, dict)]
    summary = record_.get("summary") if isinstance(record_.get("summary"), dict) else {}
    facts = {k: record_.get(k) for k in RUN_KEYS}
    facts["app"] = (record_.get("app") or {}).get("bundleId") if isinstance(record_.get("app"), dict) else None
    facts["summary"] = _facts(summary)
    members = {"events.jsonl": _jsonl(events)}
    tape_file = Path(folder) / TAPE if folder is not None else None
    packed = masked_tape(tape_file) if tape_file is not None and tape_file.is_file() else None
    if packed is not None:
        members[TAPE], entries = packed
        tape_note = {"included": True, "entries": entries}
    elif tape_file is not None and tape_file.exists():
        tape_note = {"included": False, "why": "the tape could not be read whole, so it was left out"}
    else:
        tape_note = {"included": False, "why": f"this run recorded no tape (set {ENV_FLAG}=1 before it runs)"}
    screens = {"included": False}
    if read_frame is not None:
        taken, size = {}, 0
        for frame_id in _frame_ids(record_.get("events") or (), {}):
            data = read_frame(frame_id)
            if data and size + len(data) <= MAX_SCREENS_BYTES:
                taken[f"screens/{frame_id}.jpg"] = bytes(data)
                size += len(data)
        members.update(taken)
        screens = {"included": True, "count": len(taken)}
    manifest = {"format": FORMAT, "run": mask(facts), "events": len(events), "tape": tape_note, "screens": screens,
                "masked": "secret_filter"}
    members = {"manifest.json": json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8") + b"\n", **members}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            info = zipfile.ZipInfo(name)  # the 1980 date: nothing of this Mac's clock rides along
            info.compress_type, info.external_attr = zipfile.ZIP_DEFLATED, 0o600 << 16
            archive.writestr(info, data)
    return out.getvalue(), manifest


def save(data, folder, stem):
    """Write ``data`` as ``folder``/``stem``.zip, private, and never over a file that is there ("stem (1).zip"...).
    The saved path; OSError when the folder can't be written."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for index in range(1000):
        target = folder / (f"{stem}.zip" if index == 0 else f"{stem} ({index}).zip")
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        return target
    raise OSError("Too many files with that name")


# -- the local eval set ---------------------------------------------------------------------------------------------

class EvalError(Exception):
    """A run that can't become an eval. ``code``: running, no_tape or unreadable; the message says what happened and
    what to do."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _slug(text):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", str(text).lower())).strip("-")[:40].strip("-")


def add_to_evals(record_, folder, evals_dir):
    """Keep a finished run as a regression task in the eval set at ``evals_dir`` (bench/trace_evals.py says what a
    task is). ``record_``: the run's row with its ``events``; ``folder``: where its tape is. A run that failed expects
    a replay that changes; one that didn't, a replay that holds. Adding the same run again changes nothing.
    Returns {"id", "path", "added", "expected", "recorded"}; EvalError when the run is still going or has no tape."""
    from .bench import trace_evals
    if record_.get("status") in ("queued", "running") or record_.get("finishedAt") is None:
        raise EvalError("running", "This task is still running. Add it to your evals once it ends.")
    tape_file = Path(folder) / TAPE if folder is not None else None
    if tape_file is None or not tape_file.is_file():
        raise EvalError("no_tape", f"This task recorded no tape, so it can't be replayed. Set {ENV_FLAG}=1 and run "
                                   "it again.")
    packed = masked_tape(tape_file)
    if packed is None:
        raise EvalError("unreadable", "This task's tape could not be read whole. Run it again to record a new one.")
    events = [event for event in record_.get("events") or () if isinstance(event, dict)]
    summary = record_.get("summary") if isinstance(record_.get("summary"), dict) else {}
    failure = summary.get("failure") or failure_line.failure_line(summary, events)
    recorded = mask({"status": record_.get("status"), "outcome": summary.get("outcome"),
                     "steps": summary.get("decisions"), "actions": summary.get("actions"),
                     "costUsd": summary.get("costUsd"), "failure": failure})
    expected = {"status": "completed" if failure else record_.get("status"),
                "replay": "changes" if failure else "holds"}
    goal = mask(record_.get("goal") or "")
    task_id = f"{_slug(goal) or 'task'}-{record_['id']}"
    task = {"format": trace_evals.FORMAT, "id": task_id, "added": time.strftime("%Y-%m-%d"), "goal": goal,
            "app": (record_.get("app") or {}).get("bundleId") if isinstance(record_.get("app"), dict) else None,
            "source": {"run": record_["id"], "version": __version__}, "recorded": recorded, "expected": expected}
    target = Path(evals_dir) / task_id
    result = {"id": task_id, "path": str(target), "expected": expected, "recorded": recorded}
    if target.exists():
        return {**result, "added": False}
    Path(evals_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(evals_dir, 0o700)
    building = Path(tempfile.mkdtemp(prefix=f".{task_id}.", dir=evals_dir))  # a task appears whole or not at all
    try:
        for name, data in ((trace_evals.TASK, (json.dumps(task, ensure_ascii=False, indent=1) + "\n").encode()),
                           (trace_evals.TAPE, packed[0])):
            descriptor = os.open(building / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
        try:
            os.rename(building, target)
        except OSError:
            if not target.exists():
                raise
            return {**result, "added": False}  # the same run, added at the same moment
    finally:
        shutil.rmtree(building, ignore_errors=True)
    return {**result, "added": True}
