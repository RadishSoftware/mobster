"""What one firing does: send to a destination, run a workflow or start a task, once and never retried."""

import hashlib
import logging
import time
import uuid

from .. import harness_api, secret_filter
from ..api_errors import APIError
from ..notify_triggers.sources import iso
from . import model

log = logging.getLogger("mobster.triggers")

TOPIC = "triggers"
NOTIFICATION_MAX = 1200
TEXT_MAX = 3000
DESTINATION_GONE = ("The destination was removed, so nothing was sent. Choose another destination for this trigger "
                    "in Connectors.")
WORKFLOW_GONE = "The workflow was deleted, so it didn't run. Choose another workflow for this trigger."
TRIGGER_GONE = "The trigger was removed before it could run."
BUSY = "Another task was running, so this one didn't start. Mobster doesn't queue a notification's run."
DUPLICATE = "Mobster already started a run for this notification."
NO_OUTLETS = "Sending isn't available right now. Restart Mobster, then the next match sends."
NO_WORKFLOWS = "Workflows aren't available right now. Restart Mobster, then the next match runs."
WORKFLOW_SKIPS = {"skipped_busy": "The workflow was running and one run already waits, so this one didn't start.",
                  "skipped_rate_limit": "The workflow already started 6 times in the last hour, "
                  "so this one didn't start."}
SKIP_CODES = {"run_active", "device_busy"}
OFFLINE_CODES = {"device_unavailable", "shutting_down", "device_not_found"}
LABELS = {"destination": "Send to a destination", "workflow": "Run a workflow", "task": "Start a task"}


def request_key(trigger_id, notification):
    """The Idempotency-Key for this trigger and notification: [A-Za-z0-9_.:-], at most 128 characters."""
    digest = hashlib.sha256(f"{trigger_id}\n{notification.key}".encode()).hexdigest()[:32]
    return f"trigger:{trigger_id}:{digest}"


def event_body(trigger, notification, masked):
    """What a destination receives."""
    head = notification.title or notification.app
    said = " ".join((notification.body or notification.subtitle or "").split())
    text = f"{trigger['name']}: {head}" + (f": {said}" if said else "")
    when = iso(notification.date)
    fields = {"trigger": trigger["name"], "app": notification.app, "title": notification.title,
              "body": notification.body or notification.subtitle, "date": when}
    body = {"type": "notification.matched", **fields, "masked": bool(masked),
            "runId": None, "status": "completed", "answer": text[:TEXT_MAX], "data": dict(fields), "reason": None,
            "startedAt": when, "finishedAt": when,
            "workflow": {"id": trigger["id"], "name": trigger["name"], "blanks": {}}, "text": text[:TEXT_MAX]}
    from ..outlets import webhook
    return webhook.masked(body)


def fenced(notification):
    """The notification as data, for the end of a workflow's steps or a task's words. Never longer than a run takes."""
    lines = [f"App: {notification.app}", f"From: {'your iPhone' if notification.source == 'iphone' else 'this Mac'}"]
    for label, value in (("Title", notification.title), ("Subtitle", notification.subtitle),
                         ("Body", notification.body)):
        if value:
            lines.append(f"{label}: {value}")
    text = secret_filter.redact("\n".join(lines))[:NOTIFICATION_MAX]
    return harness_api.fence("The notification that started this", text)


def who(trigger):
    """The run field ``trigger``: who started it. The run view says "Started by trigger: <name>"."""
    return {"id": trigger["id"], "name": trigger["name"]}


class Firer:
    def __init__(self, runtime, store, lookup, *, clock=time.time, publish=None):
        self.runtime = runtime
        self.store = store
        self.lookup = lookup          # trigger id -> the saved trigger (dict) or None
        self.clock = clock
        self.publish = publish or getattr(runtime, "publish", None)

    def act(self, rule, notification, masked=False):
        trigger = self.lookup(rule.name)
        if trigger is None:
            return {"ok": False, "outcome": "skipped", "message": TRIGGER_GONE}
        kind = trigger["action"]["kind"]
        firing = {"id": uuid.uuid4().hex[:12], "trigger_id": trigger["id"], "trigger_name": trigger["name"],
                  "app": trigger["app"], "at": self.clock(), "kind": kind, "outcome": "sending", "message": None,
                  "run_id": None}
        self.store.add_firing(firing)       # before anything is sent
        try:
            outcome, message, run_id = self._do(trigger, notification, masked)
        except Exception as error:  # noqa: BLE001 -- one firing's failure never stops the watcher
            log.exception("triggers: a firing failed unexpectedly")
            outcome, message, run_id = "failed", f"The action failed unexpectedly ({type(error).__name__}).", None
        self.store.update_firing(firing["id"], outcome=outcome, message=message, run_id=run_id)
        self.announce(firing["id"], trigger, outcome, message, run_id)
        return {"ok": outcome == "done", "outcome": outcome, "message": message, "runId": run_id}

    def announce(self, firing_id, trigger, outcome, message, run_id=None, reason=None):
        event = {"event": "trigger_fired" if outcome in ("done", "failed") else "trigger_skipped",
                 "firingId": firing_id, "triggerId": trigger["id"], "trigger": trigger["name"], "app": trigger["app"],
                 "action": trigger["action"]["kind"], "outcome": outcome, "message": message, "runId": run_id}
        if reason:
            event["reason"] = reason
        if callable(self.publish):
            self.publish(TOPIC, event)

    def skip(self, rule_name, name, app, reason, message):
        """A refusal before any action (rate limit, loop guard, duplicate, access lost): journaled and announced."""
        trigger = self.lookup(rule_name) or {"id": rule_name, "name": name or "A removed trigger", "app": app or "",
                                             "action": {"kind": "destination"}}
        firing = {"id": uuid.uuid4().hex[:12], "trigger_id": trigger["id"], "trigger_name": trigger["name"],
                  "app": trigger["app"], "at": self.clock(), "kind": trigger["action"]["kind"], "outcome": "skipped",
                  "message": message, "run_id": None}
        try:
            self.store.add_firing(firing)
        except Exception:  # noqa: BLE001 -- the event still goes out
            log.warning("triggers: couldn't journal a skipped firing")
        self.announce(firing["id"], trigger, "skipped", message, reason=reason)

    def _do(self, trigger, notification, masked):
        action = trigger["action"]
        if action["kind"] == "destination":
            return self._destination(trigger, notification, masked)
        if action["kind"] == "workflow":
            return self._workflow(trigger, notification)
        return self._task(trigger, notification)

    def _destination(self, trigger, notification, masked):
        from ..outlets import service as outlet_service
        service = outlet_service.service_for(self.runtime)
        if service is None:
            return "skipped", NO_OUTLETS, None
        try:
            row = service.store.destination(trigger["action"]["outletId"])
            if row is None:
                return "skipped", DESTINATION_GONE, None
            outcome = service.send_event(row["id"], event_body(trigger, notification, masked))
        except APIError as error:
            if error.code == "not_found":
                return "skipped", DESTINATION_GONE, None
            return "failed", str(error), None
        if outcome.ok:
            return "done", f"Sent to {row['name']}.", None
        return "failed", (outcome.message or "It didn't arrive.") + " Mobster doesn't send it again.", None

    def _workflow(self, trigger, notification):
        workflows = getattr(self.runtime, "workflows", None)
        if workflows is None:
            return "skipped", NO_WORKFLOWS, None
        workflow_id = trigger["action"]["workflowId"]
        try:
            workflows.get(workflow_id)
        except APIError as error:
            if error.status == 404:
                return "skipped", WORKFLOW_GONE, None
            return "failed", str(error), None
        data = {"app": notification.app, "title": notification.title,
                "body": notification.body or notification.subtitle,
                "receivedAt": round((notification.date or self.clock()) * 1000)}
        try:
            # The engine masks, cuts and fences the fields; each task it starts says "Started by trigger: <name>".
            outcome = workflows.triggers.fire(workflow_id, data, started_by=who(trigger))
        except APIError as error:
            if error.code == "not_published":
                return "skipped", str(error), None
            return self._refused(error)
        except ValueError as error:
            return "skipped", str(error), None
        if outcome is None:
            return "skipped", DUPLICATE, None
        if outcome == "started":
            return "done", "Started the workflow.", None
        if outcome == "queued":
            return "done", "The workflow was running, so it starts when that run ends.", None
        if outcome in WORKFLOW_SKIPS:
            return "skipped", WORKFLOW_SKIPS[outcome], None
        return self._refused(APIError("The workflow didn't start.", 409, outcome))

    def _task(self, trigger, notification):
        action = trigger["action"]
        if not action.get("allowed"):
            return "skipped", model.TASK_OFF, None
        goal = action["text"] + "\n\n" + fenced(notification)
        kwargs = {"extras": {"trigger": who(trigger)}, "origin": "trigger"}
        if getattr(self.runtime, "fleet", None) is not None:
            kwargs["default_device"] = "primary"
        try:
            created = self.runtime.create(None, goal, "live", request_key(trigger["id"], notification), **kwargs)
        except APIError as error:
            return self._refused(error)
        except ValueError as error:
            return "skipped", str(error), None
        run, replayed = created
        if replayed:
            return "skipped", DUPLICATE, getattr(run, "id", None)
        return "done", "Started a task. It asks before it acts.", getattr(run, "id", None)

    @staticmethod
    def _refused(error):
        if error.code in SKIP_CODES:
            return "skipped", BUSY, None
        if error.code in OFFLINE_CODES:
            return "skipped", f"{error} Nothing was started.", None
        return "failed", str(error), None
