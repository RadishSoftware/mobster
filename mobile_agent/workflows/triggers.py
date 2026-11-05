"""The triggers that start a published, turned-on workflow from outside its schedule: a notification, a new row."""

import hashlib
import json
import logging

from .. import secret_filter
from ..api_errors import APIError
from ..journal import JournalError
from ..output_contract import bounded_json

log = logging.getLogger("mobster.workflows")

SEEN_KEPT = 5000                   # row keys kept per app trigger, newest first (pending rows always stay)
NOTIFY_PER_HOUR = 6                # runs one workflow's notification trigger starts in an hour
NOTIFY_CHARS = 2000                # a notification's title or body, as a run sees it
ROW_BYTES = 16_000                 # one row, as a run sees it
READ_ROWS = 20                     # rows one poll reads
BUSY_RETRY_MS = 60_000             # a poll whose phone was busy tries again this much later


def _hash(text):
    return hashlib.sha256(str(text).encode()).hexdigest()[:32]


def _trigger(graph, kind):
    return next((n for n in (graph or {}).get("nodes") or () if n.get("type") == kind), None)


def notification_of(value):
    """A notification as a run sees it: {app, title, body, receivedAt}, masked and cut. ValueError when it isn't one."""
    if not isinstance(value, dict) or not isinstance(value.get("app"), str) or not value["app"]:
        raise ValueError("A notification has its app's bundle id, and its title, body and receivedAt")
    received = value.get("receivedAt")
    if isinstance(received, bool) or not isinstance(received, int):
        raise ValueError("receivedAt is when it arrived, in milliseconds since 1970")
    text = {key: secret_filter.redact(str(value.get(key) or ""))[:NOTIFY_CHARS] for key in ("title", "body")}
    return {"app": value["app"][:255], **text, "receivedAt": received}


def matches(node, notification):
    """Whether a trigger.notification's ``app`` and ``contains`` take this notification (case and spacing ignored)."""
    config = node.get("config") or {}
    if config.get("app") and config["app"] != notification["app"]:
        return False
    wanted = " ".join(str(config.get("contains") or "").split()).casefold()
    said = " ".join(f"{notification['title']} {notification['body']}".split()).casefold()
    return not wanted or wanted in said


class WorkflowTriggers:
    def __init__(self, owner):
        self.owner = owner
        self.polls = {}              # (workflow id, node id) -> the read task going now
        self._listeners = []         # on_change's callbacks: told whether a notification trigger listens
        # (workflow id, node id) -> when the next read is due (ms); due at once after a launch
        self.next_poll = {}
        with owner.journal.transaction() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS workflow_seen (
                workflow_id TEXT NOT NULL, node TEXT NOT NULL, key TEXT NOT NULL, seen_at INTEGER NOT NULL,
                last_seen INTEGER NOT NULL, read_at INTEGER, row TEXT, run_key TEXT,
                PRIMARY KEY (workflow_id, node, key))""")

    def forget(self, connection, identifier):
        connection.execute("DELETE FROM workflow_seen WHERE workflow_id=?", (identifier,))

    def notify(self, notification):
        """Start each workflow whose ``trigger.notification`` takes it, once each, NOTIFY_PER_HOUR an hour at most."""
        note = notification_of(notification)
        with self.owner.lock:
            return [(workflow["id"], outcome) for workflow in self.owner._records()
                    if self._subscribed(workflow, note) and (outcome := self._start(workflow, note)) is not None]

    def fire(self, workflow_id, notification, started_by=None):
        """Start one published workflow for a notification a rule outside the graph matched"""
        note = notification_of(notification)
        with self.owner.lock:
            with self.owner.journal.lock:
                workflow = self.owner._get(workflow_id)
            if not workflow.get("published"):
                raise APIError("This workflow is a draft. Publish it, then run it.", 409, "not_published")
            return self._start(workflow, note, started_by)

    def listening(self):
        """Whether a turned-on published workflow has a ``trigger.notification``: notifications need reading."""
        with self.owner.lock:
            return any(self._subscribed(workflow, None) for workflow in self.owner._records())

    def on_change(self, callback):
        """Call ``callback(listening)`` now, and again after every create, edit, publish, rollback or delete"""
        self._listeners.append(callback)
        callback(self.listening())
        return lambda: self._listeners.remove(callback) if callback in self._listeners else None

    def changed(self):
        """Tell on_change's callbacks whether anything listens now, under the owner's lock: they must not wait on it."""
        if not self._listeners:
            return
        listening = self.listening()
        for callback in list(self._listeners):
            try:
                callback(listening)
            except Exception:  # noqa: BLE001 -- a listener that fails never fails the save
                log.exception("workflows: a notification listener failed")

    def _subscribed(self, workflow, note):
        node = _trigger(self.owner.live(workflow), "trigger.notification") if workflow.get("published") else None
        return node is not None and bool(workflow.get("enabled")) and (note is None or matches(node, note))

    def _start(self, workflow, note, started_by=None):
        """One notification for one workflow: started, queued behind its run, or skipped. Under the owner's lock."""
        digest = _hash(json.dumps(note, sort_keys=True, ensure_ascii=False))
        key = f"workflow:{workflow['id']}:notification:{digest}"
        with self.owner.journal.lock:
            recent = self.owner.journal.connection.execute(
                "SELECT count(*) FROM workflow_dispatches WHERE key LIKE ? AND claimed_at>? "
                "AND outcome NOT LIKE 'skipped_%'",
                (f"workflow:{workflow['id']}:notification:%", self.owner.now() - 3_600_000)).fetchone()[0]
        busy = workflow["id"] in self.owner.running()
        outcome = ("skipped_rate_limit" if recent >= NOTIFY_PER_HOUR else
                   "claimed" if not busy else
                   "skipped_busy" if workflow.get("queuedDispatch") else "queued")
        if self.owner._claim(workflow, key, None, outcome):
            return None                      # this notification started it already
        if outcome == "claimed":
            return self._dispatch(workflow, key, "notification", note, started_by)
        if outcome == "queued":
            # Mid-run: it waits as the one run behind this one (the schedule's queue_one slot, shared), and
            # workflow_runs._start_queued starts it when the run ends. Another one while it waits is skipped.
            with self.owner.journal.lock, self.owner.journal.transaction() as connection:
                current = self.owner._get(workflow["id"])
                current.update(queuedDispatch=key, queuedEvent={"trigger": "notification", "event": note,
                                                                **({"startedBy": started_by} if started_by else {})})
                self.owner._save(connection, current)
        return outcome

    def tick(self, records):
        """Read each due ``trigger.app`` in ``records`` and start its oldest new row; under the owner's lock."""
        now = self.owner.now()
        listening = set()
        for workflow in records:
            node = _trigger(self.owner.live(workflow), "trigger.app") if workflow.get("published") else None
            if node is None or not workflow.get("enabled"):
                continue
            place = (workflow["id"], node["id"])
            listening.add(place)
            task = self.polls.get(place)
            if task is not None and getattr(task, "finished_at", None) is not None:
                del self.polls[place]
                self._take(workflow, node, task, now)
            elif task is None and now >= self.next_poll.get(place, 0):
                self._poll(workflow, node, now)
            self._start_next(workflow, node)
        for place in set(self.polls) - listening:  # turned off or unpublished: its read ends on its own
            del self.polls[place]
        for place in set(self.next_poll) - listening:
            del self.next_poll[place]

    def _poll(self, workflow, node, now):
        config, place = node.get("config") or {}, (workflow["id"], node["id"])
        spec = {"pack": config["app"], "reader": config["reader"], "n": READ_ROWS,
                "arg": str(config["arg"])[:200] if config.get("arg") else None}
        app_ids = self.owner.app_ids(config["app"]) if hasattr(self.owner, "app_ids") else set()
        extras = {"packRead": spec, **({"account": config["account"]} if config.get("account") else {})}
        device = config.get("device") if config.get("device") not in (None, "any", "loop") else workflow.get("device")
        try:
            task, _ = self.owner.runtime.create(
                config["app"] if config["app"] in app_ids else None,
                f"Read {config['reader']} in {config['app']} for new rows", "live",
                f"workflow:{workflow['id']}:poll:{node['id']}:{now}", extras=extras, origin="workflow",
                **({"device": device} if device else {"default_device": "primary"}))
        except (APIError, ValueError) as error:
            self.next_poll[place] = now + BUSY_RETRY_MS    # a busy or away phone: the next try, not a burst
            log.info("workflow %s: its app trigger couldn't read now: %s", workflow["id"], error)
            return
        self.polls[place] = task
        self.next_poll[place] = now + int(config.get("every") or 15) * 60_000

    def _take(self, workflow, node, task, now):
        """A read ended: its rows whose key isn't in the seen-set go in, waiting for their runs (none on the first)."""
        data = task.summary.get("data") if isinstance(task.summary, dict) else None
        rows = data.get("rows") if task.status == "completed" and isinstance(data, dict) else None
        if not isinstance(rows, list):
            log.info("workflow %s: its app trigger's read didn't finish (%s)", workflow["id"], task.status)
            return
        field = (node.get("config") or {})["key"]
        found = []
        for row in reversed(rows):           # apps list the newest first: the oldest new row runs first
            if (not isinstance(row, dict) or isinstance(row.get(field), bool)
                    or not isinstance(row.get(field), (str, int))):
                continue
            try:
                kept = bounded_json(secret_filter.redact_deep(row), max_bytes=ROW_BYTES)
            except ValueError:
                continue
            found.append((str(row[field])[:300], json.dumps(kept, ensure_ascii=False)))
        identifier = workflow["id"]
        try:
            with self.owner.journal.transaction() as connection:
                primed = connection.execute("SELECT 1 FROM workflow_seen WHERE workflow_id=? AND node=? LIMIT 1",
                                            (identifier, node["id"])).fetchone()
                if primed is None:
                    # The first read: what is there already is seen, never run. "" marks it read even when empty.
                    found.append(("", None))
                for key, row in found:
                    # A key seen before is only marked as still there, so the newest SEEN_KEPT are the ones the app
                    # still lists, and a row the app keeps at its top never runs again.
                    connection.execute("UPDATE workflow_seen SET last_seen=? WHERE workflow_id=? AND node=? AND key=?",
                                       (now, identifier, node["id"], key))
                    connection.execute("INSERT OR IGNORE INTO workflow_seen VALUES (?,?,?,?,?,?,?,?)",
                                       (identifier, node["id"], key, now, now, now if primed else None,
                                        row if primed else None, None if primed else ""))
                connection.execute("""DELETE FROM workflow_seen WHERE workflow_id=? AND node=? AND run_key IS NOT NULL
                    AND key<>'' AND rowid NOT IN (SELECT rowid FROM workflow_seen WHERE workflow_id=? AND node=?
                    ORDER BY last_seen DESC, rowid DESC LIMIT ?)""", (identifier, node["id"], identifier, node["id"],
                                                                       SEEN_KEPT))
        except JournalError:
            log.exception("workflow %s: keeping its app trigger's rows failed", identifier)

    def _start_next(self, workflow, node):
        if workflow["id"] in self.owner.running():
            return                           # one run at a time: the next row waits for this one to end
        with self.owner.journal.lock:
            waiting = self.owner.journal.connection.execute(
                """SELECT key, row, read_at FROM workflow_seen WHERE workflow_id=? AND node=? AND run_key IS NULL
                ORDER BY seen_at, rowid LIMIT 1""", (workflow["id"], node["id"])).fetchone()
        if waiting is None:
            return
        key, row, read_at = waiting
        dispatch_key = f"workflow:{workflow['id']}:app:{node['id'][:40]}:{_hash(key)}"
        with self.owner.journal.transaction() as connection:
            connection.execute("UPDATE workflow_seen SET run_key=? WHERE workflow_id=? AND node=? AND key=?",
                               (dispatch_key, workflow["id"], node["id"], key))
            rows = [json.loads(r[0]) for r in connection.execute(
                "SELECT row FROM workflow_seen WHERE workflow_id=? AND node=? AND read_at=? AND row IS NOT NULL "
                "ORDER BY seen_at, rowid", (workflow["id"], node["id"], read_at))]
        if self.owner._claim(workflow, dispatch_key, None):
            return                           # claimed before (a seen-set that came back): never twice
        self._dispatch(workflow, dispatch_key, "app", {"row": json.loads(row), "rows": rows})

    def _dispatch(self, workflow, key, trigger, event, started_by=None):
        from .runs import Dispatch
        try:
            self.owner._dispatch(Dispatch(key, workflow, trigger=trigger, event=event, started_by=started_by))
        except (APIError, ValueError) as error:
            return getattr(error, "code", None) or "dispatch_failed"   # its outcome is recorded with its claim
        return "started"

