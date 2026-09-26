"""Content-addressed decision memo: an identical decision request is answered without a model call.

The key is the full identity of one Jev decision request: the exact screen
(structural fingerprint, revision included), every input the model sees (the
request text, milestone, action history, observation history, recovery hint,
output classification flag, host operations, allowed apps), the configured
model, and the request clock normalized as below. A hit returns the decision
the model gave for exactly that request in an earlier run; every guard after
the decision (action verification, pre-dispatch refresh, effect ledger,
ineffective-action and duplicate refusal, completion check, answer
verification) still runs on the live screen, exactly as for a live decision.

Invalidation is strict:

* Entries are written only by runs that passed every gate (like compiled
  replay); a run that used a memo entry and then failed forgets every entry it
  used.
* An entry whose action proved ineffective, or whose action the verifier
  refused, is forgotten at once.
* WAIT and BLOCKED are never memoized: re-asking an unchanged screen is how a
  waiting run notices progress, and a refusal must be re-derived.
* The request clock is part of the key at day granularity (with its timezone),
  and at full precision when the request mentions time or dates, so a
  time-relative request never reuses an answer computed at another time.
  Entries also expire after ``TTL_SECONDS``.

Decision replay (``replay.py``) reuses a whole completed run's actions under
structural preconditions; the memo is stricter (byte-identical inputs) and
covers what replay never does: the final DONE decision, text operations, and
requests that replay skips (milestones, output classification).
"""

from collections import OrderedDict
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time

from .models import Decision
from .task_policy import OutputIntent, StopGate

VERSION = 1
TTL_SECONDS = 24 * 3600
MEMORY_ENTRIES = 2048
DISK_ENTRIES = 4096
MEMO_OPERATIONS = frozenset({"TAP", "TYPE", "TYPE_SUBMIT", "SUBMIT", "SWIPE_UP", "SWIPE_DOWN", "SWIPE_LEFT",
                             "SWIPE_RIGHT", "BACK", "HOME", "VOLUME_UP", "VOLUME_DOWN", "INCREMENT",
                             "DECREMENT", "LAUNCH_APP", "DONE"})

# Any mention of time or dates keeps the full request clock in the key.
TIME_SENSITIVE = re.compile(
    r"\b(now|today|tonight|tomorrow|yesterday|morning|afternoon|evening|noon|midnight|ago|hours?|minutes?|"
    r"seconds?|time|times|clock|alarm|timer|date|dates|day|days|week|weeks|weekend|month|months|year|years|"
    r"schedule|calendar|remind(er)?s?|deadline|mon(day)?|tues?(day)?|wed(nesday)?|thu(rs)?(day)?|"
    r"fri(day)?|sat(urday)?|sun(day)?|january|february|march|april|june|july|august|september|october|"
    r"november|december|(jan|feb|mar|apr|may|jun|jul|aug|sept?|oct|nov|dec)\.?\s+\d{1,2}|"
    r"\d{1,2}\s*(am|pm|a\.m\.|p\.m\.)|\d{1,2}:\d{2}|\d{4}-\d{2}-\d{2})\b", re.I)


def enabled():
    return os.environ.get("MOBSTER_DECISION_MEMO", "1").strip().lower() not in {"0", "off", "false", "no"}


def normalized_clock(temporal_context, *texts):
    """The part of the request clock that is part of the key."""
    if not isinstance(temporal_context, dict):
        return temporal_context
    if any(TIME_SENSITIVE.search(text or "") for text in texts):
        return dict(temporal_context)
    try:
        stamp = datetime.fromisoformat(temporal_context.get("request_time") or "")
    except (TypeError, ValueError):
        return dict(temporal_context)
    offset = stamp.utcoffset()
    return {"source": temporal_context.get("source"), "day": stamp.date().isoformat(),
            "utc_offset_minutes": None if offset is None else int(offset.total_seconds() // 60),
            "timezone_name": temporal_context.get("timezone_name")}


def memo_key(snapshot, request, model_name):
    """sha256 identity of one decision request (see module docstring)."""
    def plain(value):
        if isinstance(value, (set, frozenset)):
            return sorted(value)
        raise TypeError(type(value).__name__)
    identity = {**request, "temporal_context": normalized_clock(request.get("temporal_context"),
                                                                request.get("goal"), request.get("original_goal")),
                "screen": snapshot.fingerprint, "model": model_name, "v": VERSION}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, default=plain).encode()).hexdigest()


def encode(decision):
    data = asdict(decision)
    data["stop_gate"] = decision.stop_gate.value
    data["output_intent"] = decision.output_intent.value if decision.output_intent is not None else None
    data["usage"] = {}
    return data


def decode(data):
    fields = dict(data)
    fields["stop_gate"] = StopGate(fields["stop_gate"])
    fields["output_intent"] = OutputIntent(fields["output_intent"]) if fields.get("output_intent") else None
    fields["model"], fields["latency_ms"], fields["usage"] = "memo", 0.0, {}
    return Decision(**fields)


def memoizable(decision):
    return (decision is not None and decision.operation in MEMO_OPERATIONS and decision.demoted_from is None
            and decision.model not in {"memo", "replay"})


class DecisionMemo:
    """Process memory in front of an optional private directory (one JSON file per key)."""

    def __init__(self, directory=None, *, ttl=TTL_SECONDS, clock=time.time):
        self.directory = Path(directory) if directory else None
        self.ttl, self._clock = ttl, clock
        self._memory = OrderedDict()
        self._lock = threading.Lock()
        self.stats = {"hits": 0, "misses": 0, "saved": 0, "forgotten": 0}

    def _path(self, key):
        if not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise ValueError("Invalid memo key")
        return self.directory / f"{key}.json"

    def get(self, key):
        """The memoized decision for ``key``, or None."""
        if key is None:
            return None
        with self._lock:
            entry = self._memory.get(key)
            if entry is not None:
                self._memory.move_to_end(key)
        if entry is None and self.directory is not None:
            try:
                entry = json.loads(self._path(key).read_text())
            except (OSError, ValueError):
                entry = None
        now = self._clock()
        if not isinstance(entry, dict) or entry.get("v") != VERSION or not isinstance(entry.get("decision"), dict):
            with self._lock:
                self.stats["misses"] += 1
            return None
        if not 0 <= now - entry.get("at", 0) <= self.ttl:
            self.forget(key)
            with self._lock:
                self.stats["misses"] += 1
            return None
        try:
            decision = decode(entry["decision"])
        except (TypeError, ValueError, KeyError):
            self.forget(key)
            return None
        with self._lock:
            self._memory[key] = entry
            self._memory.move_to_end(key)
            self.stats["hits"] += 1
        return decision

    def save(self, key, decision):
        if key is None or not memoizable(decision):
            return
        entry = {"v": VERSION, "at": self._clock(), "decision": encode(decision)}
        with self._lock:
            self._memory[key] = entry
            self._memory.move_to_end(key)
            while len(self._memory) > MEMORY_ENTRIES:
                self._memory.popitem(last=False)
            self.stats["saved"] += 1
        if self.directory is None or decision.text is not None:
            # Text chosen from the request stays in memory only, like typed text in replay.
            return
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            handle, temporary = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
            try:
                with os.fdopen(handle, "w") as stream:
                    json.dump(entry, stream)
                os.chmod(temporary, 0o600)
                os.replace(temporary, self._path(key))
            except BaseException:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
            self._prune()
        except OSError:
            pass  # The memo is an optimization; a failed write only costs a future model call.

    def _prune(self):
        try:
            files = list(self.directory.glob("*.json"))
        except OSError:
            return
        if len(files) <= DISK_ENTRIES:
            return
        files.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0)
        for path in files[:len(files) - DISK_ENTRIES]:
            try:
                path.unlink()
            except OSError:
                pass

    def forget(self, key):
        if key is None:
            return
        with self._lock:
            self._memory.pop(key, None)
            self.stats["forgotten"] += 1
        if self.directory is not None:
            try:
                self._path(key).unlink()
            except (OSError, ValueError):
                pass
