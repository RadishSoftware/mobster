"""A finished result leaving Mobster by a person's click: "Send to" a saved destination, and "File as issue" in a
connected tracker. Both ride the code that already sends (outlets' deliveries, ConnectorService.call), and both are
the person's: the routes need the Mac app's session."""

import time
import uuid

from .api_errors import APIError
from .connectors import client as mcp
from .connectors import issue as tracker
from .connectors import service as connector_service
from .connectors import store as connector_store
from .connectors.skill import result_text
from .journal import JournalError
from .outlets import service as outlet_service
from .outlets import webhook
from .secret_filter import redact

ISSUE_KEEP = 50
TITLE_MAX = 120


def _refuse(code, message, status=409):
    return APIError(message, status, code)


def send(runtime, run, destination):
    """Queue ``run``'s result for the saved destination ``destination`` names:
    the delivery, as the run view shows it."""
    service = outlet_service.service_for(runtime)
    if service is None:
        raise _refuse("unavailable", "Sending results isn't available right now. Restart Mobster, then try again.",
                      503)
    if not service.store.destinations():
        raise _refuse("no_destination", "No destination is saved yet. Add Slack, email or a webhook in Settings › "
                                        "Connectors, then send the result.")
    try:
        row = service.find(destination)
    except ValueError as error:
        raise APIError(str(error), 404, "destination_not_found") from None
    return {"delivery": service.public_delivery(service.enqueue(run.id, row["id"]))}


def _text(run):
    """The issue's title and text from the run's masked result (the same body a webhook gets)."""
    body = webhook.payload(run)
    title = " ".join(f"{run.goal or 'Mobster task'}".split())[:TITLE_MAX]
    lines = [body["text"], "", f"Task: {redact(run.goal or '')}".strip(), f"Status: {body['status']}"]
    if body.get("reason"):
        lines.append(f"Why: {body['reason']}")
    if run.device_name:
        lines.append(f"Device: {run.device_name}")
    lines += ["", f"Mobster run {run.id}"]
    return title, "\n".join(lines)


def _note(runtime, run, change):
    with run.condition:
        updated = change([dict(item) for item in run.issues])
        if run.journal:
            run.journal.annotate(run.id, {"issues": updated})  # JournalError: nothing is sent without this note
        run.issues = updated
    runtime.publish("runs", {"event": "run_issue", "runId": run.id})


def file_issue(runtime, run, body):
    """File ``run``'s result as one issue: the entry the run keeps ({"id", "tracker", "status", "url", ...}).
    The attempt is noted on the run before the tool is called, and an answer that never came is never asked again."""
    if (not isinstance(body, dict) or set(body) - {"tracker", "target", "again"}
            or type(body.get("again", False)) is not bool):
        raise ValueError('Send {"tracker": "GitHub", "target": {"owner": "...", "repo": "..."}} '
                         '(tracker, target optional)')
    target = tracker.clean_target(body.get("target"))
    service = connector_service.service_for(runtime)
    if service is None:
        raise _refuse("unavailable", "Connectors aren't available right now. Restart Mobster, then try again.", 503)
    row, refusal = tracker.pick(service.store.all(), body.get("tracker"))
    if refusal:
        raise _refuse(*refusal)
    name = row["name"]
    mine = [item for item in run.issues if item.get("connector") == row["id"]]
    for item in mine:
        if item["status"] in ("filing", "unknown"):
            raise _refuse("outcome_unknown", f"Mobster doesn't know whether {name} got this issue. Check {name} "
                                             "before filing it again; Mobster won't send it twice on its own.")
    if mine and not body.get("again"):
        raise _refuse("already_filed", f"This result is already filed in {name}. Send again with \"again\": true "
                                       "to file another.")
    title, text = _text(run)
    chosen = tracker.tool_for(row, target, title, text)
    if chosen is None:
        raise _refuse("tool_missing", f"{name} has no tool Mobster can file an issue with. Test it in Settings › "
                                      "Connectors, then try again.")
    tool, arguments = chosen
    if connector_store.mode_of(row, tool) == "off":
        raise _refuse("tool_blocked", f"{name}: {tool} is blocked in Settings › Connectors, so nothing was filed.")
    entry = {"id": uuid.uuid4().hex[:12], "connector": row["id"], "tracker": name, "tool": tool, "status": "filing",
             "url": None, "error": None, "at": int(time.time() * 1000)}
    try:
        _note(runtime, run, lambda items: [entry, *items][:ISSUE_KEEP])
    except JournalError:
        raise _refuse("journal_unavailable", "Mobster couldn't save that. Try again.", 503) from None
    status, url, error = "filed", None, None
    try:
        result = service.call(row, tool, arguments)
        text_back = result_text(result)
        if result.get("isError"):
            status, error = "failed", redact(text_back)[:200]
        else:
            url = tracker.link_of(text_back)
    except mcp.OutcomeUnknown:
        status, error = "unknown", f"{name} didn't answer. The issue may exist, so Mobster won't send it again."
    except mcp.ConnectorError as problem:
        status, error = "failed", redact(problem.message)[:200]

    def settle(items):
        return [{**item, "status": status, "url": url, "error": error} if item["id"] == entry["id"] else item
                for item in items]
    try:
        _note(runtime, run, settle)
    except JournalError:
        run.issues = settle(run.issues)  # the answer stays in memory; the note on disk keeps "filing"
    return {"issue": next(item for item in run.issues if item["id"] == entry["id"])}
