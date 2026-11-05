"""The run listener: keeps a run's steps in memory and writes them only when the run finished done, unredirected.

A step is kept only while ``consent.active()``; with the corpus off a run costs this listener nothing (no state,
no file). The skeleton comes from the step itself (``StepRecord.corpus``, filled by frontier/hooks.py), so nothing
here reads the phone.
"""

import hashlib
import threading

from ..errors import MobsterError
from ..frontier.protocol import TYPED_OPS
from . import consent, store
from .skeleton import fingerprint

DENY = frozenset({"com.apple.Passwords", "com.apple.Passbook", "com.apple.Health"})
MAX_STEPS = 400


class CorpusListener:
    def __init__(self):
        self._runs = {}
        self._lock = threading.Lock()

    def on_created(self, run, runtime):
        if consent.active():
            with self._lock:
                self._runs[run.id] = {"steps": [], "redirected": False, "runtime": runtime}

    def on_step(self, run, record):
        captured = getattr(record, "corpus", None)
        with self._lock:
            held = self._runs.get(getattr(run, "id", None))
            if held is None:
                return
            if getattr(record, "approval", None) == "redirected":
                held["redirected"] = True
            if (captured is None or record.app in DENY or not record.app or len(held["steps"]) >= MAX_STEPS
                    or not consent.active()):
                return
            held["steps"].append((record, captured))

    def on_finished(self, run, summary, private):
        with self._lock:
            held = self._runs.pop(getattr(run, "id", None), None)
        done = (summary or {}).get("status") == "completed" and (summary or {}).get("outcome") == "done"
        if held is None or held["redirected"] or not done or not held["steps"] or not consent.active():
            return
        rid = hashlib.sha256(f"corpus:{run.id}".encode()).hexdigest()[:16]
        versions = {}
        records = []
        for number, (step, captured) in enumerate(held["steps"]):
            if step.app not in versions:
                versions[step.app] = _version(held["runtime"], step.app)
            target = step.target if isinstance(step.target, dict) and captured["target"] is not None else None
            nodes = captured["nodes"]
            index = captured["target"]
            records.append({
                "v": store.SCHEMA, "run": rid, "step": number, "app": step.app, "version": versions[step.app],
                "screen": {"fp": fingerprint(nodes), "nodes": nodes},
                "action": {"op": step.op, "typed": step.op in TYPED_OPS,
                           "target": {"i": index, "role": nodes[index][0], "label": nodes[index][1]}
                           if target is not None else None},
                "outcome": {"changed": bool(step.changed), "run": "done"}})
        try:
            store.append(records)
        except (OSError, store.CorpusError, MobsterError):
            pass  # the corpus is a courtesy: a full disk or a bad folder never touches a run


def _version(runtime, bundle):
    from ..packs.core import app_version
    try:
        return app_version(runtime, bundle)
    except Exception:  # noqa: BLE001 -- no version is a valid answer
        return None
