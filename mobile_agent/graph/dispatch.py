"""How a dispatch of a workflow becomes a graph run (workflows/runs.py hands over; graph/run.py runs it): the trigger it
enters at, what that trigger carries, the run's typed values and their checks before anything is claimed, and how a run
Mobster quit under ends at the next launch."""

import re

from ..api_errors import APIError
from ..workflows import starters
from . import run, schema

UNATTENDED_KEY = re.compile(r"workflow:[0-9a-f]{1,32}:(?:scheduled|notification|app|poll):")


def unattended(key):
    return bool(UNATTENDED_KEY.match(key or ""))


def entry(graph, dispatch):
    """The trigger a run enters at: its own kind's when the graph has one, else the manual trigger."""
    kinds = {node["type"] for node in graph["nodes"]}
    wanted = ("trigger.schedule" if ":scheduled:" in dispatch.key else
              "trigger.webhook" if dispatch.origin == "hook" else
              f"trigger.{dispatch.trigger}" if dispatch.trigger in ("shortcut", "notification", "app") else
              "trigger.manual")
    if wanted in kinds:
        return wanted
    if "trigger.manual" in kinds:
        return "trigger.manual"
    raise APIError("This workflow has no trigger for runs started this way. Add a Manual trigger.", 409,
                   "no_trigger")


def payload(entry, dispatch, values):
    if entry == "trigger.schedule":
        return {"values": values, "rows": None, "slot": int(dispatch.key.rsplit(":", 1)[1])}
    if entry == "trigger.webhook":
        return {"values": values, "body": dispatch.body if isinstance(dispatch.body, dict) else {}}
    if entry == "trigger.shortcut":
        return {"values": values, "input": dispatch.input}
    if entry in ("trigger.notification", "trigger.app"):
        return dict(dispatch.event or {})
    # A notification or a row for a graph with no trigger of its kind: the manual trigger carries it, as data.
    return {"values": values, "rows": dispatch.rows,
            "input": dispatch.event if dispatch.event is not None else dispatch.input}


def run_values(graph, values):
    out = {}
    declared = {starters._key(item["name"]): item for item in graph.get("inputs") or []}
    for key, item in declared.items():
        if item.get("default") is not None:
            out[key] = item["default"]
    for name, value in (values or {}).items():
        key = starters._key(name)
        out[key] = schema.coerce(value, declared[key]["type"], name) if key in declared else value
    return out


def check_values(graph, given, rows):
    loops = [n for n in graph["nodes"] if n["type"] == "loop" and n["config"].get("over") == "rows"]
    if rows is not None and not loops:
        raise ValueError("Rows are for a workflow that repeats for each row")
    if rows is None and any(not n["config"].get("rows") for n in loops):
        raise ValueError("Give the rows to repeat over, as a list or a CSV")
    values = run_values(graph, given)
    missing = [item["name"] for item in graph.get("inputs") or []
               if item.get("required") and values.get(starters._key(item["name"])) in (None, "")]
    if missing:
        raise starters.MissingBlanks(missing)
    return values


def recover(owner, record):
    """A run Mobster quit under ends interrupted; a step that may have acted keeps its task's outcome or unknown."""
    quit_reason = "Mobster quit while this step ran. It may have acted, so it won't run again."
    with owner.runtime.lock:
        runs = dict(getattr(owner.runtime, "runs", {}) or {})
    for node in owner.batches.nodes(record["id"]):
        if node["state"] in run.TERMINAL:
            continue
        state, reason = "cancelled", "Mobster quit before this step finished."
        if node["state"] in run.GOING and node.get("key"):
            if not node.get("runId"):
                request = owner.journal.lookup(node["key"])
                if request:
                    node["runId"] = request[0]
            task = runs.get(node.get("runId"))
            if task is not None and getattr(task, "finished_at", None) is not None:
                state = "done" if task.status == "completed" else "failed"
                node["status"] = task.status
                reason = None if state == "done" else f"Its task ended {task.status.replace('_', ' ')}."
            else:
                state, reason, node["status"] = "unknown", quit_reason, "interrupted"
        node.update(state=state, finishedAt=owner.now(), **({"reason": reason} if reason else {}))
        owner.batches.save_node(record["id"], node)
    record.update(status="interrupted", finishedAt=owner.now(),
                  reason="Mobster quit while this workflow ran. It wasn't run again.")
    owner.batches.save(record)
