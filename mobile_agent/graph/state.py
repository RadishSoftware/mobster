"""The state of a graph run (graph/run.py): each node instance, the rounds of loops, which phone each running step
holds, what a test run or a re-run changes, and how outputs are shown and kept."""

import threading

from ..output_contract import bounded_json
from ..secret_filter import redact_deep
from .catalog import TYPES

PREVIEW_BYTES = 1000
IO_BYTES = 256_000


class Instance:
    """One run of one node: the node itself at the top, ``<node>@<i>`` in round i of its loop."""

    def __init__(self, ident, node, scope, seq):
        self.id, self.node, self.scope, self.seq = ident, node, scope, seq
        self.state = "pending"
        self.reason = self.status = self.port = self.run_id = self.device_id = self.device_name = None
        self.output = self.input = self.items = self.answer = None
        self.ports = {}
        self.cost = None
        self.model_calls = None
        self.replayed = False
        self.started_at = self.finished_at = self.until = None
        self.ready_at = None
        self.handled = self.pinned = self.reused = self.refused = False
        self.acted = None             # a failed step: whether it may have acted outward (None: can't tell)
        self.queued_reason = None
        self.key = self.label = self.line = None
        self.slot = None
        self.reserved = 0.0
        self.attempt = 0              # its attempts after the first: retries and waits for its profile's pace
        self.paces = 0
        self.approval = None
        self.child = None
        self.loop = None

    @property
    def kind(self):
        return self.node["type"]

    @property
    def config(self):
        return self.node.get("config") or {}

    @property
    def title(self):
        return self.node.get("title") or TYPES[self.kind].label

    def public(self):
        out = {"instance": self.id, "node": self.node["id"], "type": self.kind, "iteration": self.scope.path,
               "state": self.state, "seq": self.seq}
        for key, value in (("reason", self.reason), ("status", self.status), ("port", self.port),
                           ("runId", self.run_id), ("deviceId", self.device_id), ("deviceName", self.device_name),
                           ("costUsd", self.cost), ("modelCalls", self.model_calls), ("readyAt", self.ready_at),
                           ("startedAt", self.started_at),
                           ("finishedAt", self.finished_at),
                           ("until", self.until), ("queuedReason", self.queued_reason), ("key", self.key),
                           ("label", self.label), ("answer", (self.answer or "")[:500] or None), ("line", self.line)):
            if value is not None:
                out[key] = value
        for key, value in (("handled", self.handled), ("pinned", self.pinned), ("reused", self.reused),
                           ("refused", self.refused), ("replayed", self.replayed)):
            if value:
                out[key] = True
        if self.acted is not None:
            out["acted"] = self.acted
        if self.approval is not None and self.state == "waiting":
            out["approval"] = self.approval
        elif isinstance(self.approval, dict) and self.approval.get("answeredBy"):
            out["answeredBy"] = self.approval["answeredBy"]     # "Kate (by link)": who answered, when it wasn't you
        if self.child is not None:
            out["childBatchId"] = self.child
        if self.state == "done" and self.output is not None:
            out["output"] = preview(self.output)
        return out


class Scope:

    def __init__(self, level, parent=None, loop=None, index=None, item=None, row=None, phone=None):
        self.level, self.parent, self.loop, self.index, self.item = level, parent, loop, index, item
        self.row, self.phone = row or {}, phone
        self.members = {}
        self.path = (parent.path if parent else []) + ([index] if index is not None else [])
        self.suffix = "" if index is None else (parent.suffix + ("." if parent.suffix else "@") + str(index))

    def chain(self):
        scope = self
        while scope is not None:
            yield scope
            scope = scope.parent


class Slots:

    def __init__(self):
        self.lock = threading.Lock()
        self.held = {}

    def taken(self):
        with self.lock:
            return set(self.held)

    def take(self, key, owner):
        with self.lock:
            if key in self.held:
                return False
            self.held[key] = owner
            return True

    def free(self, key, owner):
        with self.lock:
            if self.held.get(key) == owner:
                del self.held[key]


class LoopState:
    def __init__(self, items, concurrency):
        self.items, self.concurrency = items, concurrency
        self.next = 0
        self.open = []
        self.results = []
        self.failed = None            # (reason, status) of the round that failed
        self.met = False
        self.offset = 0               # the first round's index (a round re-run on its own keeps its own)
        self.single = False           # one round re-run on its own: an until loop doesn't judge it


def preview(value):
    try:
        return redact_deep(bounded_json(value, max_bytes=PREVIEW_BYTES))
    except ValueError:
        text = str(value) if not isinstance(value, str) else value
        return {"cut": True, "text": redact_deep(text[:300])}


def kept(value):
    """``value`` as a node's history keeps it: masked, and cut (``{"cut": true, ...}``) past IO_BYTES."""
    try:
        return redact_deep(bounded_json(value, max_bytes=IO_BYTES, max_nodes=20_000, max_depth=24))
    except ValueError:
        return {"cut": True, "preview": preview(value)}


class Debug:

    def __init__(self, test=False, reuse=None, skip=None, inputs=None, items=None):
        self.test, self.reuse = test, reuse or {}
        self.skip, self.inputs, self.items = skip or {}, inputs or {}, items or {}


def ports_of(kind, output, port=None):
    """The ports a node's output fills: a branch's case, an app read's rows as well as out, or out."""
    if port is not None:
        return {port: output}
    if kind == "app.read" and isinstance(output, dict):
        return {"out": output, "rows": output.get("rows")}
    return {"out": output}
