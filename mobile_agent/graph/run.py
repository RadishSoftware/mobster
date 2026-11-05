"""A graph run: one run of a workflow's published graph (docs/workflows.mdx › How a graph runs).

One coordinator thread decides what runs; worker threads run it. Nothing else changes a run's state.

- **Entry.** The trigger that started the run is done at once with what it carries; the other triggers are absent, so a
  node only they lead to is skipped.
- **Readiness.** A node runs when every edge into it is live; it is skipped when one is dead (a path not taken), and
  blocked when one is bad (something it needs failed). A merge joins paths: ``all`` waits for every path that runs,
  ``first`` goes on with the first. A loop counts the edges into its body from outside as its own.
- **Order.** Nodes that are ready together start in one fixed order (their loop round, then their place at their
  level, then the order they were made), so the same graph with the same outcomes runs the same way every time.
- **Phones.** A step on a phone holds that phone's slot from admission to its task's end: never two of a run's steps on
  one phone at once, and steps on different phones side by side. Non-phone nodes run side by side too, ``MAX_PARALLEL``
  at most. Every phone step is an ordinary task (``Runtime.create``): its approvals, the phone's rules and the spending
  stops all apply.
- **Journaled before it acts.** A node that acts is written as running, with its idempotency key, before it acts;
  then with its task's id; then how it ended. A run that was going when Mobster quit ends interrupted at the next
  launch, and nothing in it is run again.
- **Failure.** A node that fails fires its error edges when it has some (handled), else blocks what depends on it.
  A node whose outcome is unknown is never retried and never fires an error edge. The run ends with the first unhandled
  failure's own status (``approval_denied``, ``spend_cap``...), as a chain of steps always has.
- **Spending.** Each phone step reserves its cap before it starts, its share of what is left; a run never spends past
  its limit.
"""

import logging
import threading
import time
from collections import defaultdict

from ..api_errors import APIError
from ..workflows import records as batch
from ..workflows import starters
from . import expr, nodes
from .catalog import TRIGGERS, TYPES
from .schema import _topological
from .state import Debug, Instance, LoopState, Scope, Slots, kept, ports_of, preview  # noqa: F401 -- re-exported

log = logging.getLogger("mobster.graph")

MAX_PARALLEL = 8
MAX_INSTANCES = 1000
MAX_PHONE_TASKS = 400
MAX_PACES = 20                    # times one step waits for its profile's pace before it fails
FIRST_WAIT_S = 10.0               # how long a start waits for the run's first task, to open it
TERMINAL = frozenset({"done", "skipped", "failed", "blocked", "cancelled", "unknown"})
GOING = frozenset({"running", "waiting"})
ITERATION = object()


class GraphRun:
    """One run of ``graph`` for ``owner`` (workflows.Workflows)."""

    monotonic = staticmethod(time.monotonic)

    def __init__(self, owner, record, workflow, graph, *, entry, payload, values, create, origin, clock=time.time,
                 debug=None, slots=None, parent=None):
        self.owner, self.runtime = owner, owner.runtime
        # A test run or a re-run from the editor (graph/rerun.py): pins, outputs reused from an earlier run, nodes
        # that don't run, an edited input, one round of a loop.
        self.debug = debug if debug is not None else Debug()
        self.record, self.workflow, self.graph = record, workflow, graph
        self.entry, self.payload, self.values = entry, payload, values or {}
        self.create, self.origin, self.clock = create, origin, clock
        self.limits = graph.get("limits") or {"maxSpendUsd": 5.0, "maxMinutes": 240}
        self.cond = threading.Condition(threading.RLock())
        self.stop = threading.Event()
        self.stop_status = None
        self.first = threading.Event()
        self.first_run = None
        self.last = None
        self.current = {}
        self.task_runs = {}
        self.instances = {}
        self.workers = 0
        self.slots = slots if slots is not None else Slots()
        self.parent = parent
        self.children = {}
        self.call_stack = (parent.call_stack if parent is not None else []) + [workflow["id"]]
        self.approvals = {}
        self.approve_all = set()
        self.spent = self.reserved = 0.0
        self.model_calls = 0
        self.runs = 0
        self.seq = 0
        self.result = None
        self.finished_order = []
        self.started = clock()
        self.thread = threading.Thread(target=self._main, name=f"mobster-graph-{record['id']}", daemon=True)
        self.nodes = {node["id"]: node for node in graph["nodes"]}
        self.incoming = defaultdict(list)
        for edge in graph.get("edges") or []:
            self.incoming[edge["target"]].append(edge)
        self.error_edges = {edge["source"] for edge in graph.get("edges") or [] if edge["sourcePort"] == "error"}
        self.rank = {}
        for level in [None] + [n["id"] for n in graph["nodes"] if n["type"] == "loop"]:
            members = [n["id"] for n in graph["nodes"] if n.get("parent") == level]
            for index, ident in enumerate(_topological(members, self._level_edges(level)) or members):
                self.rank[ident] = index
        self.top = Scope(None)
        # A blank's default holds in every step that names it, wherever the default was written.
        self.blank_defaults = {}
        for node in graph["nodes"]:
            for name, field in TYPES[node["type"]].fields.items():
                value = (node.get("config") or {}).get(name)
                if not field.blanks or value is None:
                    continue
                for text in _texts(value):
                    try:
                        found = expr.template(text).blanks
                    except expr.ExprError:
                        continue
                    for _kind, blank, default in found:
                        key = starters._key(blank)
                        if default and not self.blank_defaults.get(key):
                            self.blank_defaults[key] = default

    def _parent(self, ident):
        return self.nodes[ident].get("parent")

    def _within(self, ident, loop):
        at = self._parent(ident)
        while at is not None:
            if at == loop:
                return True
            at = self._parent(at)
        return False

    def _lift(self, ident, level):
        at = ident
        while at is not None:
            if self._parent(at) == level:
                return at
            at = self._parent(at)
        return None

    def _level_edges(self, level):
        out = set()
        for edge in self.graph.get("edges") or []:
            if self.nodes[edge["source"]]["type"] == "loop" and edge["sourcePort"] in ("item", "index") \
                    and self._within(edge["target"], edge["source"]):
                continue
            a, b = self._lift(edge["source"], level), self._lift(edge["target"], level)
            if a is not None and b is not None and a != b:
                out.add((a, b))
        return out

    def _now_ms(self):
        return round(self.clock() * 1000)

    def _publish(self, event):
        publish = getattr(self.runtime, "publish", None)
        if callable(publish):
            publish("workflows", {"workflowId": self.workflow["id"], "batchId": self.record["id"],
                                  "parentBatchId": self.parent.record["id"] if self.parent is not None else None,
                                  **event})

    def _save(self, inst, io=None):
        """Journal ``inst`` as it is now (and its input and output when it ended), then say so. Under the lock."""
        entry = inst.public()
        self.owner.batches.save_node(self.record["id"], entry, io)
        self._publish({"event": "batch_node", **entry})
        if inst.kind in batch.LEGACY_KINDS:
            self._publish({"event": "batch_step", "entry": batch.legacy_entry(entry), "index": inst.seq})

    def start(self):
        self.thread.start()

    def _main(self):
        try:
            with self.cond:
                self._open(self.top)
                self._publish({"event": "batch_started", "graph": 2, "trigger": self.entry,
                               "revision": self.record.get("revision"),
                               "nodes": {i.id: i.state for i in self.instances.values()}})
            self._run()
            status, reason = self._outcome()
        except Exception as error:  # noqa: BLE001 -- a bug ends the run, said plainly; nothing is retried
            log.exception("graph run %s failed", self.record["id"])
            status, reason = "error", f"Mobster hit a problem running the workflow: {type(error).__name__}."
            self.stop.set()
            self._stop_tasks()
            self._drain()
            with self.cond:
                for inst in self.instances.values():
                    if inst.state not in TERMINAL:
                        self._end(inst, "cancelled", "The run stopped on a problem.")
        finally:
            self.first.set()
        if status == "completed":
            self._send_to()
        with self.cond:
            self.record.update(status=status, reason=reason, finishedAt=self._now_ms(),
                               spentUsd=round(self.spent, 6), runs=self.runs,
                               modelCalls=self.model_calls,
                               result=preview(self.result) if self.result is not None else None)
            if self.parent is not None:      # a sub-workflow's run answers to its parent, not to a dispatch
                try:
                    self.owner.batches.save(self.record)
                except Exception:  # noqa: BLE001 -- the parent's own end still says how it ended
                    log.exception("graph run %s: saving its end failed", self.record["id"])
        if self.parent is None:
            # The owner commits this record with the dispatch's outcome in one transaction, then publishes.
            self.owner.batch_finished(self, status, reason)
        else:
            self.publish_end(status, reason)

    def publish_end(self, status, reason):
        self._publish({"event": "batch_finished", "status": status, "reason": reason,
                       "result": self.record.get("result")})

    def _run(self):
        with self.cond:
            while True:
                self._check_limits()
                if self.stop.is_set():
                    self._cancel_waiting()
                moved = self._settle()
                moved = self._start_ready() or moved
                if self._finished():
                    return
                if not moved:
                    self.cond.wait(0.25)

    def _drain(self, seconds=5.0):
        deadline = self.monotonic() + seconds
        with self.cond:
            while self.workers and self.monotonic() < deadline:
                self.cond.wait(0.1)

    def _finished(self):
        return self.workers == 0 and all(inst.state in TERMINAL for inst in self.instances.values())

    def _check_limits(self):
        if self.stop.is_set():
            if self.stop_status is None:
                self.stop_status = "interrupted" if getattr(self.runtime, "closing", False) else "stopped"
            return
        if getattr(self.runtime, "closing", False):
            self.stop_status = "interrupted"
        elif self.clock() - self.started >= self.limits["maxMinutes"] * 60:
            self.stop_status = "time_limit"
        else:
            return
        self.stop.set()
        self._stop_tasks()

    def stop_all(self):
        self.stop.set()
        self._stop_tasks()
        for child in list(self.children.values()):
            child.stop_all()
        with self.cond:
            self.cond.notify_all()

    def _stop_tasks(self):
        for run in list(self.current.values()):
            try:
                self.runtime.stop_run(run.id)
            except Exception:  # noqa: BLE001 -- it may have just finished
                pass

    def stop_reason(self):
        return {"stopped": "You stopped the workflow.", "interrupted": "Mobster quit while this workflow ran.",
                "time_limit": f"The workflow reached its time limit of {self.limits['maxMinutes']} minutes."
                }.get(self.stop_status, "The workflow stopped.")

    def _cancel_waiting(self):
        for inst in list(self.instances.values()):
            if inst.state in ("pending", "queued"):
                self._end(inst, "cancelled", self.stop_reason())

    def _outcome(self):
        if self.stop_status:
            return self.stop_status, self.stop_reason()
        for inst in sorted(self.instances.values(), key=lambda i: (i.finished_at or 0, i.seq)):
            if inst.reused:
                continue                     # how an earlier run ended isn't how this one did
            if inst.state == "failed" and not inst.handled or inst.state == "unknown":
                return inst.status or ("error" if inst.state == "unknown" else "failed"), inst.reason
        if any(inst.state == "blocked" and not inst.reused for inst in self.instances.values()):
            # Stopped behind a step kept as it ended in the run this one picks up from (a resume).
            kept = next((inst for inst in self.instances.values() if inst.reused and
                         (inst.state == "unknown" or inst.state == "failed" and not inst.handled)), None)
            return (kept.status or "failed", kept.reason) if kept else ("failed", None)
        output = next((node for node in self.graph["nodes"] if node["type"] == "output"), None)
        advertised = (output or {}).get("config", {}).get("schema")
        if advertised is not None:
            result = next((inst for inst in self.instances.values() if inst.kind == "output"), None)
            if result is None or result.state != "done":
                if result is None or result.node["id"] not in self.debug.skip:
                    return "output_schema", "This workflow did not produce a result matching its saved shape."
            else:
                try:
                    nodes.result_value(advertised, self.result)
                except nodes.NodeFailed as failed:
                    return failed.status, failed.reason
        return "completed", None

    def _open(self, scope):
        """Make an instance of every node at the scope's level. At the top, the entry trigger is done at once."""
        members = sorted((n for n in self.graph["nodes"] if n.get("parent") == scope.level),
                         key=lambda n: self.rank.get(n["id"], 0))
        if len(self.instances) + len(members) > MAX_INSTANCES:
            raise _Limit(f"The workflow reached its limit of {MAX_INSTANCES:,} steps in one run.")
        for node in members:
            self.seq += 1
            inst = Instance(node["id"] + scope.suffix, node, scope, self.seq)
            scope.members[node["id"]] = inst
            self.instances[inst.id] = inst
            recorded = self.debug.reuse.get(inst.id)
            if recorded is not None:
                self._reuse(inst, recorded)
                continue
            if node["type"] in TRIGGERS:
                if node["type"] == self.entry:
                    inst.started_at = self._now_ms()
                    pin = node.get("pin") if self.debug.test else None
                    payload = pin["out"] if isinstance(pin, dict) and "out" in pin else self.payload
                    inst.pinned = payload is not self.payload
                    self._end(inst, "done", output=payload)
                else:
                    self._end(inst, "skipped", "This trigger didn't start this run.")
                continue
            self._save(inst)

    def _reuse(self, inst, recorded):
        """An instance whose result comes from an earlier run: it ends as it did then, and nothing in it runs again."""
        inst.reused, inst.started_at = True, recorded.get("startedAt")
        inst.input, inst.run_id = recorded.get("input"), recorded.get("runId")
        inst.answer, inst.cost = recorded.get("answer"), None
        state, output = recorded["state"], recorded.get("output")
        if state == "done":
            inst.port = recorded.get("port")
            self._end(inst, "done", output=output, ports=ports_of(inst.kind, output, inst.port))
        elif state == "failed":
            inst.handled = bool(recorded.get("handled"))
            inst.ports = {"error": recorded.get("error")} if inst.handled else {}
            self._end(inst, "failed", recorded.get("reason"), status=recorded.get("status"))
        else:
            self._end(inst, state, recorded.get("reason") or "It ended this way in the run this one picks up from.",
                      status=recorded.get("status"))

    def _source(self, edge, scope):
        """The instance an edge comes from, seen from ``scope``: ITERATION for a loop's item and index."""
        source = edge["source"]
        level = self._parent(source)
        for each in scope.chain():
            if each.loop is not None and each.loop.node["id"] == source and edge["sourcePort"] in ("item", "index"):
                return ITERATION, each
            if each.level == level:
                return each.members.get(source), each
        return None, None

    def _edge_state(self, edge, scope):
        source, where = self._source(edge, scope)
        if source is ITERATION:
            return "live"
        if source is None:
            return "pending"
        if source.kind in TRIGGERS and source.state == "skipped":
            return "absent"
        state = source.state
        if state == "done":
            return "live" if edge["sourcePort"] in source.ports else "dead"
        if state == "failed":
            if source.handled:
                return "live" if edge["sourcePort"] == "error" else "dead"
            return "bad"
        if state == "skipped":
            return "dead"
        if state in ("blocked", "cancelled", "unknown"):
            return "bad"
        return "pending"

    def _edges(self, inst):
        """(edge, state) of every edge inst waits for: its own, and for a loop those into its body from outside."""
        out = [(edge, self._edge_state(edge, inst.scope)) for edge in self.incoming[inst.node["id"]]]
        if inst.kind == "loop":
            loop = inst.node["id"]
            for target, edges in self.incoming.items():
                if not self._within(target, loop):
                    continue
                for edge in edges:
                    if edge["source"] == loop or self._within(edge["source"], loop):
                        continue
                    out.append((edge, self._edge_state(edge, inst.scope)))
        return out

    def _value(self, edge, scope):
        """What ``edge`` carries into its target in ``scope``, after its map."""
        source, where = self._source(edge, scope)
        if source is ITERATION:
            value = where.item if edge["sourcePort"] == "item" else where.index
        else:
            value = source.ports.get(edge["sourcePort"])
        if edge.get("map"):
            value = expr.evaluate(edge["map"], {**self.scope_values(scope), "input": value},
                                  timezone=self.workflow.get("timezone"))
        return value

    def scope_values(self, scope):
        """The roots an expression in ``scope`` reads (all but $input)."""
        done = {}
        for each in reversed(list(scope.chain())):
            for ident, member in each.members.items():
                if member.state == "done":
                    done[ident] = member.ports.get("out", member.output)
        values = {"inputs": self.values, "vars": self.graph.get("vars") or {}, "node": done,
                  "run": {"id": self.record["id"], "workflowId": self.workflow["id"], "trigger": self.entry,
                          "startedAt": round(self.started * 1000), "test": bool(self.record.get("test"))}}
        loop = next((each for each in scope.chain() if each.loop is not None), None)
        if loop is not None:
            values["item"], values["index"] = loop.item, loop.index
        return values

    def blank_values(self, scope):
        """The values a node's {blanks} fill from: the run's, then each enclosing row's over them."""
        out = {**self.blank_defaults, **self.values}
        for each in reversed(list(scope.chain())):
            out.update(each.row)
        return out

    def _settle(self):
        moved = False
        for inst in list(self.instances.values()):
            if inst.state == "pending":
                moved = self._resolve(inst) or moved
            elif inst.kind == "loop" and inst.state == "running":
                moved = self._drive_loop(inst) or moved
        return moved

    def _resolve(self, inst):
        edges = self._edges(inst)
        present = [(edge, state) for edge, state in edges if state != "absent"]
        if edges and not present:
            self._end(inst, "skipped", "It starts from a trigger that didn't start this run.")
            return True
        states = [state for _edge, state in present]
        bad = next((edge for edge, state in present if state == "bad"), None)
        if inst.kind == "merge":
            live = [edge for edge, state in present if state == "live"]
            if inst.config.get("mode") == "first" and live:
                first = min(live, key=lambda e: (self._finished_at(e, inst.scope), e["id"]))
                return self._ready(inst, [first])
            if "pending" in states:
                return False
            if bad is not None:
                return self._block(inst, bad)
            if live and inst.config.get("mode", "all") == "all":
                return self._ready(inst, live)
            self._end(inst, "skipped", "No path into it ran.")
            return True
        if bad is not None:
            return self._block(inst, bad)
        if "pending" in states:
            return False
        if "dead" in states:
            self._end(inst, "skipped", "It is on a path this run didn't take.")
            return True
        return self._ready(inst, [edge for edge, _state in present if edge["target"] == inst.node["id"]])

    def _finished_at(self, edge, scope):
        source, _where = self._source(edge, scope)
        return getattr(source, "finished_at", None) or 0

    def _block(self, inst, edge):
        source, _where = self._source(edge, inst.scope)
        title = source.title if isinstance(source, Instance) else edge["source"]
        why = source.reason if isinstance(source, Instance) and source.reason else "it didn't finish"
        self._end(inst, "blocked", f"Stopped because “{title}” didn't finish: {why}"[:300])
        return True

    def _ready(self, inst, edges):
        try:
            values = [self._value(edge, inst.scope) for edge in edges if edge["targetPort"] == "in"]
            inst.input = values[0] if len(values) == 1 else values if values else None
            items = [edge for edge in edges if edge["targetPort"] == "items"]
            inst.items = self._value(items[0], inst.scope) if items else None
        except expr.ExprError as error:
            self._fail(inst, f"An edge's map couldn't work out its value: {error}", "failed")
            return True
        if self.stop.is_set():
            self._end(inst, "cancelled", self.stop_reason())
            return True
        ident = inst.node["id"]
        if ident in self.debug.inputs and not inst.scope.path:
            inst.input = self.debug.inputs[ident]
        if ident in self.debug.skip:
            self._end(inst, "skipped", self.debug.skip[ident])
            return True
        pin = inst.node.get("pin")
        if self.debug.test and isinstance(pin, dict) and pin:
            inst.pinned, inst.started_at = True, self._now_ms()
            self._end(inst, "done", output=pin.get("out"), ports=dict(pin))
            return True
        inst.state, inst.ready_at = "queued", self._now_ms()
        self._save(inst)
        return True

    def _start_ready(self):
        if self.stop.is_set():
            return False
        moved = False
        queued = sorted((i for i in self.instances.values() if i.state == "queued"),
                        key=lambda i: (i.scope.path, self.rank.get(i.node["id"], 0), i.seq))
        now = self._now_ms()
        for inst in queued:
            if inst.queued_reason == "pace" and inst.until is not None and now < inst.until:
                continue
            if inst.kind in nodes.INLINE:
                self._inline(inst)
                moved = True
            elif inst.kind == "loop":
                moved = self._start_loop(inst) or moved
            elif TYPES[inst.kind].phone:
                moved = self._start_phone(inst) or moved
            elif self.workers < MAX_PARALLEL:
                if inst.kind in nodes.SPENDS and not self._reserve(inst):
                    moved = True
                    continue
                self._spawn(inst)
                moved = True
            elif inst.queued_reason != "slot":
                inst.queued_reason = "slot"
                self._save(inst)
        return moved

    def _inline(self, inst):
        inst.state, inst.started_at = "running", self._now_ms()
        try:
            output, ports = nodes.INLINE[inst.kind](self, inst)
        except nodes.NodeFailed as failed:
            self._fail(inst, failed.reason, failed.status)
            return
        except expr.ExprError as error:
            self._fail(inst, f"{error}", "failed")
            return
        self._end(inst, "done", output=output, ports=ports)

    def _spawn(self, inst):
        self.workers += 1
        inst.state, inst.started_at, inst.queued_reason, inst.until = "running", self._now_ms(), None, None
        self._save(inst)
        threading.Thread(target=self._work, args=(inst,), name=f"mobster-graph-{inst.id}", daemon=True).start()

    def _start_phone(self, inst):
        slot, device = self._slot(inst)
        if slot is None:
            if inst.queued_reason != "phone":
                inst.queued_reason = "phone"
                self._save(inst)
            return False
        if self.runs >= MAX_PHONE_TASKS:
            self._fail(inst, f"The workflow reached its limit of {MAX_PHONE_TASKS} phone tasks in one run.",
                       "loop_limit")
            return True
        if inst.kind in nodes.SPENDS and not self._reserve(inst):
            return True
        if not self.slots.take(slot, (self.record["id"], inst.id)):
            self.reserved = max(0.0, self.reserved - inst.reserved)
            inst.reserved = 0.0
            return False
        inst.slot, inst.device_id = slot, device
        self._spawn(inst)
        return True

    def _reserve(self, inst):
        """Set aside ``inst``'s share of what is left to spend; False (and the step failed) when nothing is."""
        unreserved = self.limits["maxSpendUsd"] - self.spent - self.reserved
        if unreserved <= 0.0001:
            self._fail(inst, f"The workflow spent its limit of ${self.limits['maxSpendUsd']:.2f}.", "spend_cap")
            return False
        ready = sum(1 for i in self.instances.values()
                    if i.state == "queued" and i.kind in nodes.SPENDS and i is not inst) + 1
        inst.reserved = round(unreserved / ready, 4)
        self.reserved += inst.reserved
        return True

    def _slot(self, inst):
        """(slot key, device id to run on) for a phone step, or (None, None) while its phone is held."""
        device = inst.config.get("device")
        fleet = getattr(self.runtime, "fleet", None)
        if device == "loop":
            phone = next((s.phone for s in inst.scope.chain() if s.phone is not None), None) or {}
            key, device = phone.get("deviceId") or "primary", phone.get("deviceId")
        elif device == "any":
            if fleet is None:
                key, device = "primary", None
            else:
                free = nodes.free_phones(self.runtime, self.slots.taken())
                if not free:
                    return None, None
                key = device = free[0]["id"]
        elif device:
            key = device
        elif inst.config.get("account") or self.workflow.get("account"):
            key, device = f"account:{inst.config.get('account') or self.workflow.get('account')}", None
        else:
            key, device = self.default_slot(), None
        return (None, None) if key in self.slots.taken() else (key, device)

    def default_slot(self):
        return self.workflow.get("device") or getattr(self, "admitted_device", None) or "primary"

    @staticmethod
    def ai_client(model):
        """The client an Ask the model step calls (graph/nodes.ai_client); a test gives its own."""
        return nodes.ai_client(model)

    def default_device(self):
        """The phone the workflow's steps use when they name none, or None for the primary."""
        return self.workflow.get("device") or getattr(self, "admitted_device", None)

    def answer(self, instance, decision, *, value=None, has_value=False, note=None, by=None, request_id=None):
        """The first answer wins; one naming ``request_id`` counts only while the step still waits for that request."""
        with self.cond:
            waiting = self.approvals.get(instance)
            if waiting is None:
                for child in self.children.values():
                    if instance in child.approvals:
                        return child.answer(instance, decision, value=value, has_value=has_value, note=note, by=by,
                                            request_id=request_id)
                raise APIError("That step isn't waiting for you.", 409, "not_waiting")
            request, answered, box = waiting
            if request_id is not None and request["id"] != request_id:
                raise APIError("That step isn't waiting for you.", 409, "not_waiting")
            if answered.is_set():
                raise APIError("That step has been answered.", 409, "answered")
            if has_value:
                if not request["editable"]:
                    raise APIError("This step can't be edited before you approve it.", 409, "not_editable")
                current = self.instances[instance].input
                if current is not None and value is not None and expr.kind_of(value) != expr.kind_of(current):
                    raise ValueError(f"The edited value is {expr.kind_of(value)}, and this step's is "
                                     f"{expr.kind_of(current)}")
                box["value"] = value
            box.update(decision=decision, note=note, by=by)
            answered.set()
            return request

    def _work(self, inst):
        tries = (inst.node.get("retry") or {}).get("tries", 0)
        backoff = (inst.node.get("retry") or {}).get("backoffSeconds", 30)
        try:
            for attempt in range(tries + 1):
                try:
                    output, ports = nodes.WORKERS[inst.kind](self, inst)
                except nodes.NodeFailed as failed:
                    if failed.retryable and attempt < tries and not self.stop.is_set():
                        with self.cond:
                            inst.reason = f"Try {attempt + 1} didn't finish: {failed.reason}"[:300]
                            inst.attempt += 1
                            self._save(inst)
                        self.stop.wait(backoff)
                        continue
                    with self.cond:
                        inst.acted = False if failed.refused else failed.acted
                        self._fail(inst, failed.reason, failed.status, failed.run_id, refused=failed.refused)
                    return
                except nodes.Paced as paced:
                    with self.cond:
                        self._paced(inst, paced)
                    return
                except nodes.OutcomeUnknown as unknown:
                    with self.cond:
                        self._end(inst, "unknown", unknown.reason, status=unknown.status)
                    return
                except nodes.Cancelled as cancelled:
                    with self.cond:
                        self._end(inst, "cancelled", cancelled.reason, status=cancelled.status)
                    return
                except Exception as error:  # noqa: BLE001 -- it may have acted: unknown, never retried
                    log.exception("graph run %s: %s failed", self.record["id"], inst.id)
                    with self.cond:
                        self._end(inst, "unknown", f"Mobster hit a problem running this step: {type(error).__name__}. "
                                                   "It may have acted, so it won't run again.", status="error")
                    return
                with self.cond:
                    self._end(inst, "done", output=output, ports=ports)
                return
        finally:
            with self.cond:
                self.workers -= 1
                self.current.pop(inst.id, None)
                if inst.slot is not None:
                    self.slots.free(inst.slot, (self.record["id"], inst.id))
                self.reserved = max(0.0, self.reserved - inst.reserved)
                inst.reserved = 0.0
                self.cond.notify_all()

    def _paced(self, inst, paced):
        """A step its pace held before it acted waits in ``queued``, then runs again as a new attempt with a new key."""
        inst.paces += 1
        left_ms = (self.started + self.limits["maxMinutes"] * 60 - self.clock()) * 1000
        if paced.until - self._now_ms() > left_ms:
            why, status = f"Waiting for it would pass the workflow's time limit of {self.limits['maxMinutes']} " \
                          "minutes.", "time_limit"
        elif inst.paces > MAX_PACES:
            why, status = f"The profile's pace held this step {MAX_PACES} times in this run.", "blocked"
        else:
            why = status = None
        if status is not None:
            inst.acted = False                  # nodes.phone raises Paced only for a task that didn't act outward
            self._fail(inst, f"{paced.reason} {why}", status, paced.run_id)
            return
        inst.state, inst.queued_reason, inst.until = "queued", "pace", paced.until
        inst.reason, inst.attempt = paced.reason, inst.attempt + 1
        self._save(inst)

    def _fail(self, inst, reason, status=None, run_id=None, refused=False):
        inst.handled = inst.node["id"] in self.error_edges
        if inst.handled:
            inst.ports = {"error": {"node": inst.node["id"], "reason": reason, "status": status, "runId": run_id
                                    or inst.run_id}}
        inst.refused = refused
        self._end(inst, "failed", reason, status=status)

    def _end(self, inst, state, reason=None, *, output=None, ports=None, status=None):
        inst.state, inst.finished_at = state, self._now_ms()
        if reason is not None:
            inst.reason = str(reason)[:300]
        if status is not None:
            inst.status = status
        if state == "done":
            inst.output = output
            inst.ports = ports if ports is not None else {"out": output}
            inst.reason = None
            if inst.answer is None and isinstance(output, dict):
                found = output.get("answer") if inst.kind != "connector.call" else output.get("text")
                inst.answer = found if isinstance(found, str) else None
            if inst.kind == "output":
                self.result = output
        inst.queued_reason = None
        self.finished_order.append(inst.id)
        io = None
        if state in ("done", "failed", "unknown", "cancelled"):
            io = {"input": kept(inst.input), "output": kept(inst.output) if state == "done" else None,
                  **({"items": kept(inst.items)} if inst.kind == "loop" and inst.items is not None else {}),
                  "error": inst.ports.get("error") if state == "failed" else
                  ({"reason": inst.reason, "status": inst.status} if state != "done" else None)}
        self._save(inst, io)
        self.cond.notify_all()

    def _start_loop(self, inst):
        config = inst.config
        over = config["over"]
        try:
            if inst.node["id"] in self.debug.items and not inst.scope.path:
                index, item = self.debug.items[inst.node["id"]]
                items = [item]
            elif over == "times":
                items = list(range(config["times"]))
            elif over == "rows":
                rows = self.payload.get("rows") if isinstance(self.payload, dict) else None
                items = (rows if rows else config.get("rows")) or []
                if not items:
                    self._fail(inst, "Give the rows to repeat over, as a list or a CSV.", "missing_values")
                    return True
                items = items[:config["max"]]
            elif over == "items":
                if not isinstance(inst.items, list):
                    self._fail(inst, "The loop's items aren't a list.", "failed")
                    return True
                items = inst.items[:config["max"]]
            elif over == "until":
                items = None
            else:
                items = nodes.loop_phones(self.runtime, config["phones"])[:config.get("max") or MAX_INSTANCES]
                if not items:
                    self._fail(inst, "No phone is ready to run the loop on.", "offline")
                    return True
        except nodes.NodeFailed as failed:
            self._fail(inst, failed.reason, failed.status)
            return True
        inst.items = items
        concurrency = config.get("concurrency") or (len(items) if over == "phones" and items else 1)
        inst.loop = LoopState(items, 1 if over == "until" else concurrency)
        if inst.node["id"] in self.debug.items and not inst.scope.path:
            inst.loop.offset, inst.loop.single = self.debug.items[inst.node["id"]][0], True
        if over == "rows" and not inst.scope.path:
            self.record["roundValues"] = [dict(row) for row in items]
        inst.state, inst.started_at = "running", self._now_ms()
        self._save(inst)
        self._drive_loop(inst)
        return True

    def _round(self, inst, index):
        config, state = inst.config, inst.loop
        item = index if state.items is None else state.items[index]
        over = config["over"]
        row = item if over == "rows" else None
        phone = item if over == "phones" else None
        scope = Scope(inst.node["id"], inst.scope, inst, state.offset + index, item, row, phone)
        self._open(scope)
        return scope

    def _drive_loop(self, inst):
        state, config = inst.loop, inst.config
        moved = False
        for scope in list(state.open):
            members = list(scope.members.values())
            if any(member.state not in TERMINAL for member in members):
                continue
            state.open.remove(scope)
            moved = True
            failed = next((m for m in sorted(members, key=lambda m: m.finished_at or 0)
                           if m.state in ("blocked", "unknown", "cancelled") or m.state == "failed" and not m.handled),
                          None)
            if failed is not None:
                if state.failed is None:
                    state.failed = (failed.reason or "A step didn't finish.", failed.status or (
                        "error" if failed.state == "unknown" else "stopped" if failed.state == "cancelled" else None))
                continue
            collected = scope.members.get(config.get("collect")) if config.get("collect") else None
            state.results.append(collected.ports.get("out") if collected is not None and collected.state == "done"
                                 else None)
            if config["over"] == "until" and not state.single:
                watched = scope.members.get(config["until"])
                seen = nodes.pointer(watched.output, config.get("path") or "/observed") \
                    if watched is not None and watched.state == "done" else None
                if seen is True:
                    state.met = True
                elif seen is not False:
                    question = (watched.config.get("question") if watched is not None else None) or "the check"
                    state.failed = (f"Mobster couldn't tell whether this is true: {question}", "condition_unknown")
        stop_new = state.failed is not None or state.met or self.stop.is_set()
        total = config["max"] if state.items is None else len(state.items)
        while not stop_new and len(state.open) < state.concurrency and state.next < total:
            try:
                state.open.append(self._round(inst, state.next))
            except _Limit as limit:
                state.failed = (str(limit), "loop_limit")
                break
            state.next += 1
            moved = True
        if state.open:
            return moved
        if self.stop.is_set() and state.failed is None and not state.met and state.next < total:
            self._end(inst, "cancelled", self.stop_reason())
        elif state.failed is not None:
            self._fail(inst, state.failed[0], state.failed[1])
        elif config["over"] == "until" and not state.met and not state.single:
            question = self.nodes.get(config["until"], {}).get("config", {}).get("question") or "the check"
            self._fail(inst, f"The workflow ran {config['max']} times without seeing: {question}", "loop_limit")
        else:
            output = {"results": state.results, "count": len(state.results)}
            self._end(inst, "done", output=output, ports={"out": output})
        return True

    @staticmethod
    def _fill(text, values):
        return text            # a graph node's text was worked out before it ran

    def public(self):
        with self.cond:
            return self.owner.batches.public(self.record)

    def _send_to(self):
        """The workflow's own destination (sendTo) gets the newest task's result when the run completes."""
        destination = self.workflow.get("sendTo")
        if not destination or self.last is None:
            return
        try:
            from ..outlets.service import service_for
            service = service_for(self.runtime)
            if service is not None:
                service.enqueue(self.last.id, destination)
        except Exception:  # noqa: BLE001 -- a result that can't be sent never changes how the run ended
            log.exception("graph run %s: sendTo failed", self.record["id"])


class _Limit(Exception):
    pass


def _texts(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _texts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _texts(item)


def unsupported(graph):
    """Why this Mobster can't run ``graph`` yet, or None: a node type it has no runner for."""
    for node in graph.get("nodes") or []:
        kind = node["type"]
        if kind in TRIGGERS:
            continue
        if kind not in nodes.INLINE and kind not in nodes.WORKERS and kind != "loop":
            return f"This Mobster can't run a {TYPES[kind].label} step yet. Update Mobster, then run it again."
    return None


__all__ = ["GraphRun", "Instance", "Scope", "unsupported", "APIError"]
