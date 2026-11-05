"""What a run teaches memory (a run listener, registered by register.py).

- At the start: the phone's model and iOS version become a device fact, and a project the run names (the run field
  ``project``, an id or ``{id, path}``) is linked to its folder, so its facts can travel in ``.mobster/memory.yaml``.
- While it works: a step that did what a fact it was given says is reported as ``from_memory {step, index, ids}``,
  so the run view can say "from memory: …" on that step and offer to forget the fact behind it.
- At the end: a run that completed, whatever it staged becomes facts, and what the task cost becomes a cost fact for
  its app. A run that didn't complete (stopped, failed, unconfirmed) saves nothing. Then ``learned {runId, facts}``
  goes on the bus topic ``memory`` and a changed project's file is rewritten.

It runs on the listeners' own thread, so the store never holds up a task.
"""

import logging

from ..catalog import app_name
from . import project, retrieval, writer
from .listener import publish
from .store import default_store

log = logging.getLogger("mobster.memory")
MIN_SHARED = 2          # words a step must share with a fact to be said to come from it


def completed(summary):
    return isinstance(summary, dict) and summary.get("status") == "completed"


def step_words(record):
    target = record.target if isinstance(record.target, dict) else {}
    return retrieval.tokens(f"{target.get('label') or ''} {record.text or ''}")


def attribute(words, facts):
    """The ids of the facts a step with these words acts on: it shares at least two words with the fact, or the one
    word a one-word fact has."""
    found = []
    for fact in facts:
        shared = words & retrieval.fact_tokens(fact["text"])
        if len(shared) >= min(MIN_SHARED, len(retrieval.fact_tokens(fact["text"]))) and shared:
            found.append(fact["id"])
    return found


class RunMemory:
    def __init__(self, store_factory=default_store):
        self.store_factory = store_factory
        self.used = {}                  # run id -> [fact rows given to the run]

    def on_created(self, run, runtime):
        try:
            store = self.store_factory()
            if store is None:
                return
            self.link_project(store, run)
            self.device(run, runtime)
        except Exception:  # noqa: BLE001
            log.exception("memory: starting a run failed")

    def link_project(self, store, run):
        value = (getattr(run, "extras", None) or {}).get("project")
        if isinstance(value, dict) and value.get("id") and isinstance(value.get("path"), str):
            store.link_project(str(value["id"]), value["path"])
            project.import_file(store, str(value["id"]))

    def device(self, run, runtime):
        device_id = getattr(run, "device_id", None)
        if not device_id or runtime is None or not hasattr(runtime, "resolve_device"):
            return
        phone = runtime.resolve_device(device_id)
        writer.from_device(device_id, getattr(run, "device_name", None) or getattr(phone, "name", None),
                           phone.info() if hasattr(phone, "info") else None)

    def on_event(self, run, event):
        if event.get("event") != "memory_used":
            return
        store = self.store_factory()
        if store is None:
            return
        try:
            self.used[run.id] = [store.get_fact(fact_id) for fact_id in event.get("ids") or ()]
        except Exception:  # noqa: BLE001
            self.used[run.id] = []

    def on_step(self, run, record):
        facts = self.used.get(getattr(run, "id", None))
        if not facts:
            return
        ids = attribute(step_words(record), facts)
        if ids:
            run.emit({"event": "from_memory", "step": record.step, "index": record.index, "ids": ids})

    def on_finished(self, run, summary, private):
        self.used.pop(getattr(run, "id", None), None)
        try:
            store = self.store_factory()
            if store is None:
                return
            ok = completed(summary)
            if ok:
                self.cost(store, run, summary)
            results = store.commit_run(run.id, keep=ok)
            facts = [r["fact"] for r in results if r["status"] in ("added", "updated") and r["fact"]]
            if facts:
                publish({"event": "learned", "runId": run.id, "facts": facts})
                for fact in facts:
                    publish({"event": "fact_changed", "op": "added", "fact": fact})
            self.sync_projects(store, facts)
        except Exception:  # noqa: BLE001
            log.exception("memory: finishing a run failed")

    def cost(self, store, run, summary):
        bundle = (getattr(run, "app", None) or {}).get("bundleId")
        usd, ms = summary.get("costUsd"), summary.get("elapsed_ms")
        if not bundle or not isinstance(usd, (int, float)) or not isinstance(ms, (int, float)) or ms <= 0:
            return
        store.stage(run.id, [{"text": f"A task in {app_name(bundle) or bundle} cost about ${usd:.2f} and took "
                                      f"{round(ms / 1000)} s (the latest one).",
                              "kind": "cost", "scope": f"app:{bundle}", "source": "run", "ref": {"runId": run.id},
                              "key": f"cost:{bundle}", "confidence": 0.9}])

    def sync_projects(self, store, facts):
        for scope in {fact["scope"] for fact in facts if fact["scopeKind"] == "project"}:
            project.export_file(store, scope.partition(":")[2])
