"""Test runs and re-runs from the editor (docs/workflows.mdx › Debug a workflow).

- **A test run** (``test``) runs the draft the canvas edits, and a node with a ``pin`` doesn't run: it is done at once
  with its pinned output. Any other run runs the published graph and never reads pins.
- **From a node** (``{"from": {"batchId", "node", "input"?}}``): the node and everything after it run again; every other
  node takes the result it had in that run. A node it needs that didn't finish there can't be reused, so the re-run is
  refused and says which.
- **One round** (``{"from": {"batchId", "node": <loop>, "iteration": i}}``): that loop runs round ``i`` alone, with
  the item it had then; nothing after the loop runs.
- **Only** (``{"only": {"node", "batchId"?}}``): that node runs on the input it had, and nothing after it.
- **Resume** (``{"resume": batchId}``): a run picks up where one stopped. Everything done is reused; what failed, was
  blocked or was cancelled runs again, unless it may have acted: a step whose outcome is unknown, or one that failed
  after acting outward (``acted``), is never run again and stays for you to review. ``preview`` says which is which.
- **Resume several** (``resume_all``): each of a list of runs, oldest first, one after another, on one thread.

A re-run is a new run with a new key: it never replays the earlier one's requests, and what it reuses it never acts on.
"""

import re
import threading
import time

from .. import harness_api
from ..api_errors import APIError
from .catalog import TRIGGERS
from .run import Debug

MAX_RESUMES = 20
RESUME_WAIT_S = 24 * 3600         # the longest a bulk resume waits for one of its runs before it starts the next
RUN_ID = re.compile(r"[a-f0-9]{12}")

# Kinds that never act outward: one that failed may run again on a resume.
SAFE_KINDS = frozenset({"transform", "branch", "merge", "output", "wait", "phone.check", "app.read", "loop", "brief",
                        "ai.ask"})


def refuse(reason):
    return APIError(reason, 409, "rerun_unavailable")


def _source(owner, workflow_id, batch_id):
    record = owner.batches.get(batch_id) if isinstance(batch_id, str) else None
    if record is None or record["workflowId"] != workflow_id:
        raise refuse("That run isn't kept any more. Mobster keeps the newest 20 runs of each workflow.")
    if record.get("graph") != 2:
        raise refuse("That run is from before workflows were graphs, so its steps can't be reused. Run it again.")
    if record.get("status") == "running":
        raise refuse("That run is still going. Wait for it to end.")
    return record, owner.batches.nodes_with_io(batch_id)


def _top(graph, ident):
    nodes = {node["id"]: node for node in graph["nodes"]}
    at = ident
    while at in nodes and nodes[at].get("parent") is not None:
        at = nodes[at]["parent"]
    return at if at in nodes else None


def _lifted(graph):
    """The top level's edges, an edge into a loop's body counted as one into the loop."""
    out = set()
    for edge in graph.get("edges") or []:
        a, b = _top(graph, edge["source"]), _top(graph, edge["target"])
        if a and b and a != b:
            out.add((a, b))
    return out


def _after(graph, ident):
    edges, out, pending = _lifted(graph), set(), [ident]
    while pending:
        at = pending.pop()
        for a, b in edges:
            if a == at and b not in out:
                out.add(b)
                pending.append(b)
    return out


def _before(graph, ident):
    edges, out, pending = _lifted(graph), set(), [ident]
    while pending:
        at = pending.pop()
        for a, b in edges:
            if b == at and a not in out:
                out.add(a)
                pending.append(a)
    return out


def _recorded(entry, io):
    io = io or {}
    return {"state": entry["state"], "output": io.get("output"), "input": io.get("input"), "error": io.get("error"),
            "handled": bool(entry.get("handled")),
            "port": entry.get("port"), "reason": entry.get("reason"), "status": entry.get("status"),
            "runId": entry.get("runId"), "answer": entry.get("answer"), "startedAt": entry.get("startedAt"),
            "items": io.get("items")}


def _cut(io):
    return any(isinstance(value, dict) and value.get("cut") for value in (io or {}).values())


def _title(graph, ident):
    node = next((n for n in graph["nodes"] if n["id"] == ident), None)
    return f"“{(node or {}).get('title') or ident}”"


def _entry_of(graph, rows):
    trigger = next(((entry, io) for entry, io in rows if entry.get("type") in TRIGGERS and entry["state"] == "done"),
                   None)
    if trigger is None:
        raise refuse("That run's trigger isn't kept, so it can't be picked up. Run it again.")
    entry, io = trigger
    if entry["type"] not in {node["type"] for node in graph["nodes"]}:
        raise refuse("The workflow no longer has the trigger that run started from. Run it again.")
    payload = (io or {}).get("output") or {}
    return entry["type"], payload, (payload.get("values") if isinstance(payload, dict) else None) or {}


def plan(owner, workflow, graph, spec, *, test):
    """(Debug, entry, payload, values, source) for a re-run, or Debug(test) with no source for a plain test run."""
    if not spec:
        return Debug(test=test), None, None, None, None
    if not isinstance(spec, dict) or len(spec) != 1 or next(iter(spec)) not in ("from", "only", "resume"):
        raise ValueError("A re-run is from, only or resume")
    kind, value = next(iter(spec.items()))
    if kind == "resume":
        return resume(owner, workflow, graph, value, test=test)
    if not isinstance(value, dict) or set(value) - {"batchId", "node", "iteration", "input"}:
        raise ValueError(f"{kind} takes batchId, node" + (", iteration and input" if kind == "from" else " and input"))
    ident = value.get("node")
    nodes = {node["id"]: node for node in graph["nodes"]}
    if ident not in nodes:
        raise ValueError("Name a step of this workflow")
    batch_id = value.get("batchId")
    if batch_id is None:
        if kind == "from":
            raise ValueError("Name the run to re-run from (batchId)")
        recent = [r for r in owner.batches.recent(workflow["id"]) if r.get("graph") == 2 and r["status"] != "running"]
        if not recent:
            raise refuse("This workflow has no run to take the step's input from. Run it first.")
        batch_id = recent[0]["id"]
    record, rows = _source(owner, workflow["id"], batch_id)
    iteration = value.get("iteration")
    if nodes[ident].get("parent") is not None:
        raise refuse("That step is inside a loop. Re-run the loop, or one round of it (iteration).")
    debug = Debug(test=test)
    after = _after(graph, ident)
    if iteration is not None:
        if nodes[ident]["type"] != "loop" or kind != "from" or type(iteration) is not int or iteration < 0:
            raise ValueError("iteration is a round of a loop: a whole number from 0")
        loop = next(((entry, io) for entry, io in rows if entry["instance"] == ident), None)
        items = (loop[1] or {}).get("items") if loop else None
        rounds = 1 + max((e["iteration"][0] for e, _io in rows if (e.get("iteration") or []) and
                          e["instance"].split("@")[0] in nodes and _top(graph, e["instance"].split("@")[0]) == ident),
                         default=-1)
        if iteration >= max(rounds, len(items) if isinstance(items, list) else 0):
            raise refuse(f"That run has no round {iteration + 1} of {_title(graph, ident)}.")
        item = items[iteration] if isinstance(items, list) and iteration < len(items) else iteration
        debug.items[ident] = (iteration, item)
    if kind == "only" or iteration is not None:
        what = "Only one round of" if iteration is not None else "Only"
        for later in after:
            debug.skip[later] = f"{what} {_title(graph, ident)} ran in this run."
    rerun = {ident} | after                     # these run again, or are skipped: never reused
    needed = _before(graph, ident)
    by_node = {}
    for entry, io in rows:
        node_id = entry["instance"].split("@")[0]
        top = _top(graph, node_id)
        if top is None or nodes.get(node_id, {}).get("type") != entry.get("type"):
            continue
        if top in rerun:
            continue
        by_node.setdefault(top, []).append((entry, io))
        if entry["state"] in ("done", "skipped") or entry["state"] == "failed" and entry.get("handled"):
            if top in needed and _cut(io):
                raise refuse(f"{_title(graph, top)}'s result was too big to keep, so it can't be reused. "
                             "Run the workflow from the start.")
            debug.reuse[entry["instance"]] = _recorded(entry, io)
        elif top in needed:
            raise refuse(f"{_title(graph, top)} didn't finish in that run, so its result can't be reused. "
                         "Re-run from it instead.")
        else:
            debug.reuse[entry["instance"]] = _recorded(entry, io)
    missing = [top for top in needed if top not in by_node]
    if missing:
        raise refuse(f"{_title(graph, missing[0])} didn't run in that run. Run the workflow from the start.")
    if "input" in value:
        debug.inputs[ident] = value["input"]
    entry, payload, values = _entry_of(graph, rows)
    return debug, entry, payload, values, {"batchId": batch_id, kind: ident,
                                           **({"iteration": iteration} if iteration is not None else {})}


def resume(owner, workflow, graph, batch_id, *, test):
    record, rows = _source(owner, workflow["id"], batch_id)
    debug = Debug(test=test)
    for entry, io in rows:
        verdict, _reason = judge(graph, entry)
        if verdict in ("reuse", "keep"):          # kept as it ended: what depends on it stays stopped
            debug.reuse[entry["instance"]] = _recorded(entry, io)
    entry, payload, values = _entry_of(graph, rows)
    return debug, entry, payload, values, {"batchId": batch_id, "resume": True}


def judge(graph, entry):
    """("reuse" | "run" | "keep", why); keep is an instance that may have acted, so what follows it stays stopped."""
    node_id = entry["instance"].split("@")[0]
    node = next((n for n in graph["nodes"] if n["id"] == node_id), None)
    if node is None or node["type"] != entry.get("type"):
        return "run", "It changed since that run."
    state = entry["state"]
    if state in ("done", "skipped") or state == "failed" and entry.get("handled"):
        return "reuse", None
    if state == "unknown":
        return ("keep", "Its outcome is unknown, so Mobster won't run it again. Check it, then "
                "re-run from the step after it.")
    if state == "failed" and node["type"] not in SAFE_KINDS and entry.get("acted") is not False:
        return ("keep", "It may have acted before it failed, so Mobster won't run it again. "
                "Check it, then re-run from it.")
    return "run", None


def preview(owner, workflow, graph, batch_id):
    """GET /api/workflows/{id}/runs/{batchId}/resume: what a resume would reuse, run again and leave."""
    _record, rows = _source(owner, workflow["id"], batch_id)
    out = {"reuse": [], "run": [], "skip": []}
    for entry, _io in rows:
        verdict, reason = judge(graph, entry)
        if verdict == "keep":
            out["skip"].append({"instance": entry["instance"], "reason": reason})
        else:
            out[verdict].append(entry["instance"])
    return out


def recorded_scope(owner, workflow, batch_id, node):
    """The values an expression at ``node`` read in a past run, for POST /api/workflows/evaluate's preview."""
    _record, rows = _source(owner, workflow["id"], batch_id)
    done = {}
    here = None
    for entry, io in rows:
        if entry["instance"] == node:
            here = io or {}
        if entry["state"] == "done" and "@" not in entry["instance"]:
            done[entry["instance"]] = (io or {}).get("output")
    trigger = next((io for entry, io in rows if entry.get("type") in TRIGGERS and entry["state"] == "done"), {}) or {}
    payload = trigger.get("output") if isinstance(trigger.get("output"), dict) else {}
    return {"input": (here or {}).get("input"), "inputs": payload.get("values") or {},
            "vars": workflow["graph"].get("vars") or {}, "node": done,
            "run": {"id": batch_id, "workflowId": workflow["id"], "trigger": None, "startedAt": None, "test": False}}


def resume_all(owner, identifier, batch_ids, request_key, origin=None):
    if not isinstance(batch_ids, list) or not 1 <= len(batch_ids) <= MAX_RESUMES or len(set(batch_ids)) != len(
            batch_ids) or not all(isinstance(b, str) and RUN_ID.fullmatch(b) for b in batch_ids):
        raise ValueError(f"batchIds is a list of 1 to {MAX_RESUMES} different run ids")
    if not isinstance(request_key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", request_key):
        raise ValueError("Supply one valid Idempotency-Key to resume runs")
    if origin in harness_api.UNATTENDED_ORIGINS:
        raise APIError("A test run or a re-run starts from Mobster's editor.", 403, "test_from_editor")
    with owner.journal.lock:
        owner._get(identifier)
    items = []
    for batch_id in batch_ids:
        record = owner.batches.get(batch_id)
        try:
            if record is None or record["workflowId"] != identifier:
                raise APIError("That run isn't kept any more.", 404, "not_found")
            if record.get("status") in ("running", "completed"):
                raise APIError(f"It {record['status'].replace('_', ' ')}.", 409, "rerun_unavailable")
            if not owner.resume_preview(identifier, batch_id)["run"]:
                raise APIError("Nothing in it would run again.", 409, "rerun_unavailable")
            items.append({"batchId": batch_id, "state": "queued", "createdAt": record.get("createdAt") or 0})
        except APIError as error:
            items.append({"batchId": batch_id, "state": "refused", "reason": str(error), "createdAt": None})
    # Oldest first, as they ran; what is refused is listed after.
    items.sort(key=lambda item: (item["createdAt"] is None, item["createdAt"] or 0))
    for item in items:
        item.pop("createdAt")
    with owner._batches_lock:
        if identifier in owner._resuming and owner._resuming[identifier].is_alive():
            raise APIError("This workflow is already resuming a list of runs.", 409, "resume_busy")
        thread = threading.Thread(target=_resume_each, args=(owner, identifier, items, request_key, origin),
                                  name=f"mobster-resume-{identifier}", daemon=True)
        owner._resuming[identifier] = thread
    thread.start()
    return {"resumes": [dict(item) for item in items]}

def _resume_each(owner, identifier, items, request_key, origin):
    """resume_all's thread: each queued run in turn, after the workflow's run before it has ended."""
    def said():
        _publish(owner, identifier, {"event": "resume_queue", "resumes": [dict(item) for item in items]})
    for item in items:
        if item["state"] != "queued":
            continue
        while identifier in owner.running() and not owner._closing.is_set():
            owner._closing.wait(0.5)
        if owner._closing.is_set():
            item.update(state="refused", reason="Mobster quit before it started.")
            break
        try:
            started = owner.start(identifier, f"{request_key}:{item['batchId']}"[:128],
                                 rerun={"resume": item["batchId"]}, origin=origin)
        except (APIError, ValueError) as error:
            item.update(state="refused", reason=str(error)[:300])
            said()
            continue
        item.update(state="running", resumedAs=(started.get("batch") or {}).get("id"))
        said()
        deadline = time.monotonic() + RESUME_WAIT_S
        ended = {"status": "running"}
        while ended["status"] in ("running", "queued") and time.monotonic() < deadline:
            if owner._closing.is_set():
                break
            ended = owner.follow(identifier, started, 1.0)
        item.update(state="ended", status=ended["status"])
    said()

def _publish(owner, identifier, event):
    publish = getattr(owner.runtime, "publish", None)
    if callable(publish):
        publish("workflows", {"workflowId": identifier, **event})

