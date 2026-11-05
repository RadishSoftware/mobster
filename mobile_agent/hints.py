"""Hints: what Mobster knows about an app before it looks, used only while it keeps proving true."""

import hashlib
import json
import os
import threading
import time
from pathlib import Path

FILE = "hints.jsonl"
HEALS = "hints-heals.jsonl"
MAX_BYTES = 1_000_000
SURE = .6                    # the confidence a source needs to act on a hint (packs' recognizers set it, 12 Oct)
MISSES_TO_STEP_ASIDE = 2     # misses in a row, in one run, that turn a source off for that app for the run
STALE_AFTER = 2              # a key's last uses at one app version that all missed: stale at that version
RECENT = 400
SOURCES = ("pack", "replay", "ios")
# Heals are navigation: a step that commits is never learned from a fallback.
HEAL_OPS = frozenset({"TAP", "LONG_PRESS", "SWIPE_LEFT", "SWIPE_RIGHT", "SCROLL_TO"})
# A saved program's handovers that are drift, not by design.
PROGRAM_DRIFT = ("didn't fit this screen", "found no control it can act on")
_lock = threading.Lock()
_cache = {}                  # path -> (mtime, size, entries)


def _append(path, entry):
    line = json.dumps({"at": round(time.time(), 3), **entry}, separators=(",", ":"), sort_keys=True) + "\n"
    with _lock:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > MAX_BYTES:
                kept = path.read_bytes()[-MAX_BYTES // 2:]
                path.write_bytes(kept[kept.find(b"\n") + 1:])
            with open(path, "a", encoding="utf-8") as out:
                out.write(line)
            os.chmod(path, 0o600)
        except OSError:
            pass  # the ledger is a log: a full disk never stops a step


def _read(path):
    try:
        stat = path.stat()
    except OSError:
        return []
    with _lock:
        cached = _cache.get(path)
        if cached and cached[:2] == (stat.st_mtime_ns, stat.st_size):
            return cached[2]
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        return []
    with _lock:
        _cache[path] = (stat.st_mtime_ns, stat.st_size, out)
    return out


def record(data_dir, entry):
    """One use or drift (``entry``: source, app, key, ok, and what's known of version, variant, confidence, why)."""
    if data_dir:
        _append(Path(data_dir) / FILE, entry)


def recent(data_dir, *, source=None, app=None, limit=RECENT):
    """The newest ``limit`` ledger lines (of one source, one app), oldest first."""
    if not data_dir:
        return []
    lines = [e for e in _read(Path(data_dir) / FILE)
             if (source is None or e.get("source") == source) and (app is None or e.get("app") == app)]
    return lines[-limit:]


def stale(entries, source, app, key, version):
    """Whether ``key``'s last STALE_AFTER uses at ``version`` (among ``entries``) all missed."""
    uses = [e for e in entries if e.get("kind", "use") == "use" and e.get("source") == source
            and e.get("app") == app and e.get("key") == key and e.get("version") == version]
    last = uses[-STALE_AFTER:]
    return len(last) == STALE_AFTER and not any(e.get("ok") for e in last)


class Gate:
    """One run's gate for every source. ``trust`` before acting on a hint; ``note`` after each use."""

    def __init__(self, data_dir=None):
        self.data_dir = data_dir
        self.misses = {}     # (source, app) -> misses in a row this run

    def trust(self, source, app, key, confidence=None, version=None):
        """(True, None), or (False, why) when the general agent should do this step."""
        if confidence is not None and confidence < SURE:
            return False, f"not sure enough where it is ({confidence:.2f} < {SURE})"
        if self.misses.get((source, app), 0) >= MISSES_TO_STEP_ASIDE:
            return False, f"missed {MISSES_TO_STEP_ASIDE} times in a row in this task"
        if version and stale(recent(self.data_dir, source=source, app=app), source, app, key, version):
            return False, f"missed its last {STALE_AFTER} uses on this version of the app"
        return True, None

    def note(self, ok, source="pack", app=None):
        """A use landed or not; returns whether ``source`` is now off for ``app`` in this run."""
        misses = 0 if ok else self.misses.get((source, app), 0) + 1
        self.misses[(source, app)] = misses
        return misses >= MISSES_TO_STEP_ASIDE


def status(entries):
    """One app's state for one source, from its ledger lines."""
    uses = [e for e in entries if e.get("kind", "use") == "use"]
    drifts = [e for e in entries if e.get("kind") == "drift"] or [e for e in uses if not e.get("ok")]
    landed = sum(1 for e in uses if e.get("ok"))
    state = "unknown"
    if uses:
        last = uses[-STALE_AFTER:]
        state = ("stale" if len(last) == STALE_AFTER and not any(e.get("ok") for e in last)
                 else "healthy" if landed / len(uses) >= .8 else "degraded")
    last = drifts[-1] if drifts else None
    return {"uses": len(uses), "landed": landed, "missed": len(uses) - landed, "state": state,
            "versions": sorted({str(e["version"]) for e in uses if e.get("version")}),
            "lastDrift": ({k: last.get(k) for k in ("key", "version", "variant", "why", "at")} if last else None)}


def health(data_dir):
    """Every app's hints by source: [{app, sources: {source: status}, heals: {checked, rejected}}], by app."""
    by_app = {}
    for entry in recent(data_dir, limit=10 ** 6):
        by_app.setdefault(entry.get("app") or "", {}).setdefault(entry.get("source") or "", []).append(entry)
    heals = _read(Path(data_dir) / HEALS) if data_dir else []
    out = []
    for app in sorted(by_app):
        mine = [h for h in heals if h.get("app") == app]
        out.append({"app": app, "sources": {source: status(lines) for source, lines in sorted(by_app[app].items())},
                    "heals": {"checked": sum(1 for h in mine if h.get("checked")),
                              "rejected": sum(1 for h in mine if not h.get("checked"))}})
    return out


def pair_hash(role, label):
    """The screen-shape hash of one (role, label) pair: state.screen_shape's rule."""
    return hashlib.sha1(json.dumps([role, label], ensure_ascii=False).encode()).hexdigest()[:8]


def check(proposal, shape):
    """The replay check before a heal is kept."""
    action = proposal.get("action") or {}
    return (action.get("op") in HEAL_OPS and bool(action.get("label")) and action.get("commit") is None
            and pair_hash(action.get("role"), action.get("label")) in set(shape or ()))


def use_of(event, app=None, version=None):
    """A source's event as a ledger line, or None. ``app_pack`` lines are written by packs themselves (``own``)."""
    kind = event.get("event")
    if kind == "app_pack":
        from .packs.health import bundle_of
        return {"kind": "use", "source": "pack", "app": bundle_of(event.get("pack")), "key": event.get("step"),
                "ok": bool(event.get("ok")), "version": event.get("version"), "confidence": event.get("confidence"),
                "why": event.get("why"), "own": True}
    if kind == "frontier_program" and isinstance(event.get("at"), int):
        return {"kind": "use", "source": "replay", "app": app, "key": f"step {event['at'] + 1}", "ok": True,
                "version": version}
    if kind == "frontier_program_handover" and isinstance(event.get("at"), int) and any(
            phrase in str(event.get("why") or "") for phrase in PROGRAM_DRIFT):
        return {"kind": "use", "source": "replay", "app": app, "key": f"step {event['at'] + 1}", "ok": False,
                "version": version, "why": str(event.get("why") or "")[:160]}
    if kind == "replay_drift":
        return {"kind": "use", "source": "replay", "app": app, "key": f"step {event.get('step')}",
                "ok": False, "version": version, "why": str(event.get("why") or "")[:160]}
    if kind == "hint" and event.get("source") in SOURCES:
        return {"kind": "use", "source": event["source"], "app": event.get("app") or app, "key": event.get("key"),
                "ok": bool(event.get("ok")), "version": event.get("version") or version,
                "confidence": event.get("confidence"), "why": str(event.get("why") or "")[:160] or None}
    return None


class HintListener:
    """harness_api.RunListener: writes each hint use to the ledger."""

    def __init__(self):
        self.runs = {}
        self._lock = threading.Lock()

    def on_created(self, run, runtime):
        state_db = getattr(getattr(runtime, "config", None), "state_db", None)
        data_dir = getattr(run, "data_dir", None) or (Path(state_db).parent if state_db else None)
        if data_dir is None:
            return
        app = (getattr(run, "app", None) or {}).get("bundleId")
        version = None
        if app:
            try:
                from .packs.core import app_version
                version = app_version(runtime, app)
            except Exception:  # noqa: BLE001
                version = None
        with self._lock:
            self.runs[run.id] = {"data_dir": Path(data_dir), "app": app, "version": version, "pending": [],
                                 "proposals": []}

    def _state(self, run):
        with self._lock:
            return self.runs.get(getattr(run, "id", None))

    def on_event(self, run, event):
        state = self._state(run)
        if state is None or not isinstance(event, dict):
            return
        use = use_of(event, state["app"], state["version"])
        if use is None:
            return
        own = use.pop("own", False)
        if not own:
            record(state["data_dir"], use)
        if not use["ok"]:
            state["pending"].append(use)

    def on_step(self, run, record_):
        state = self._state(run)
        if state is None or not state["pending"]:
            return
        variant = getattr(record_, "screen", None)
        target = getattr(record_, "target", None) or {}
        for use in state["pending"]:
            record(state["data_dir"], {**{k: use.get(k) for k in ("source", "app", "key", "version", "why")},
                                       "kind": "drift", "variant": variant})
            proposal = {"source": use["source"], "app": use["app"], "key": use["key"], "version": use.get("version"),
                        "variant": variant, "run": getattr(run, "id", None),
                        "action": {"op": getattr(record_, "op", None), "role": target.get("role"),
                                   "label": target.get("label"), "commit": getattr(record_, "commit", None)}}
            proposal["checked"] = check(proposal, getattr(record_, "shape", ()))
            state["proposals"].append(proposal)
        state["pending"] = []

    def on_finished(self, run, summary, private):
        with self._lock:
            state = self.runs.pop(getattr(run, "id", None), None)
        if state is None:
            return
        for use in state["pending"]:  # a miss with no step after it: the drift, with no variant
            record(state["data_dir"], {**{k: use.get(k) for k in ("source", "app", "key", "version", "why")},
                                       "kind": "drift", "variant": None})
        done = (summary or {}).get("status") == "completed" and (summary or {}).get("outcome") == "done"
        if done:
            for proposal in state["proposals"]:
                _append(state["data_dir"] / HEALS, proposal)
