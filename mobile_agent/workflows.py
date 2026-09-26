"""Durable saved tasks and at-most-once dispatch, sharing the canonical run journal."""

from dataclasses import dataclass
import hashlib
import json
import re
import threading
import time
import uuid

from .api_errors import APIError
from .catalog import APPS
from .journal import JournalError, encode
from .output_contract import validate_output
from .schedule import DAILY_LIMIT, MIN_INTERVAL_MS, next_occurrences, timezone, validate_cron
from .helper_models import resolve_helper_model


MAX_WORKFLOWS = 200
MAX_DISPATCHES = 10000
LATE_TOLERANCE_MS = 60_000
DAY_MS = 86_400_000


@dataclass(frozen=True)
class Dispatch:
    key: str
    workflow: dict


class WorkflowDispatchUncertain(JournalError):
    """Canonical run creation succeeded, but saving its workflow linkage failed."""
    def __init__(self, run_id):
        super().__init__("The task may already have started. Check its run status before making another request.")
        self.run_id = run_id


class Workflows:
    def __init__(self, runtime, clock=time.time, daily_limit=DAILY_LIMIT):
        self.runtime = runtime
        self.journal = runtime.journal
        self.clock = clock
        self.daily_limit = daily_limit
        # Updates and dispatch initiation serialize, so a completed pause cannot
        # race a not-yet-started dispatch. No journal transaction spans device I/O.
        self.lock = threading.RLock()
        self.scheduler_error = None
        with self.journal.transaction() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS workflows (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            connection.execute("""CREATE TABLE IF NOT EXISTS workflow_dispatches (
                key TEXT PRIMARY KEY, workflow_id TEXT NOT NULL REFERENCES workflows(id),
                scheduled_at INTEGER, claimed_at INTEGER NOT NULL,
                outcome TEXT NOT NULL, run_id TEXT)
            """)
            connection.execute("CREATE INDEX IF NOT EXISTS workflow_dispatch_times ON workflow_dispatches(claimed_at)")

    def now(self):
        return round(self.clock() * 1000)

    def _get(self, identifier):
        row = self.journal.connection.execute("SELECT data FROM workflows WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise APIError("Saved task not found", 404, "not_found")
        return json.loads(row[0])

    def _save(self, connection, workflow):
        connection.execute("UPDATE workflows SET data=? WHERE id=?", (encode(workflow), workflow["id"]))

    def list(self):
        with self.lock:
            self._reconcile()
            with self.journal.lock:
                return [json.loads(row[0]) for row in self.journal.connection.execute("SELECT data FROM workflows ORDER BY rowid DESC")]

    def get(self, identifier):
        with self.lock, self.journal.lock:
            return self._get(identifier)

    def create(self, body):
        if not isinstance(body, dict) or set(body) - {"name", "appId", "goal", "outputSchema", "outputFormat", "helperModel"}:
            raise ValueError("Unsupported saved task field")
        name, goal, app_id = body.get("name"), body.get("goal"), body.get("appId")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
            raise ValueError("Name the saved task using 1–120 characters")
        if not isinstance(goal, str) or not 1 <= len(goal.strip()) <= 4000:
            raise ValueError("Supply a task of 1–4000 characters")
        if not isinstance(app_id, str) or app_id not in {app["id"] for app in APPS}:
            raise ValueError("Choose an app from your catalog")
        output_format = body.get("outputFormat", "auto")
        schema = validate_output(output_format, body.get("outputSchema"))
        helper_model = resolve_helper_model(body.get("helperModel"))
        now = self.now()
        workflow = {"id": uuid.uuid4().hex[:12], "name": name.strip(), "appId": app_id, "goal": goal.strip(),
                    "outputSchema": schema, "outputFormat": output_format, "helperModel": helper_model,
                    "cron": None, "timezone": "UTC",
                    "enabled": False, "revision": 1, "createdAt": now, "updatedAt": now,
                    "nextAt": None, "lastRunId": None, "lastOutcome": None}
        with self.lock, self.journal.lock:
            if self.journal.connection.execute("SELECT count(*) FROM workflows").fetchone()[0] >= MAX_WORKFLOWS:
                raise APIError("Saved task limit reached", 409, "workflow_limit")
            with self.journal.transaction() as connection:
                connection.execute("INSERT INTO workflows VALUES (?,?)", (workflow["id"], encode(workflow)))
        return workflow

    def update(self, identifier, body):
        if (not isinstance(body, dict) or set(body) - {"name", "cron", "timezone", "enabled", "revision", "helperModel"}
                or type(body.get("revision")) is not int):
            raise ValueError("Supply the current revision and supported schedule fields")
        with self.lock, self.journal.lock:
            workflow = self._get(identifier)
            if body["revision"] != workflow["revision"]:
                raise APIError("The saved task changed. Reload before editing it.", 409, "revision_conflict")
            updated = {**workflow, **body}
            if "helperModel" in body:
                updated["helperModel"] = resolve_helper_model(body["helperModel"])
            if not isinstance(updated["name"], str) or not 1 <= len(updated["name"].strip()) <= 120:
                raise ValueError("Name the saved task using 1–120 characters")
            updated["name"] = updated["name"].strip()
            if type(updated["enabled"]) is not bool:
                raise ValueError("enabled must be true or false")
            timezone(updated["timezone"])
            if updated["cron"] is not None:
                updated["cron"] = validate_cron(updated["cron"], updated["timezone"], self.daily_limit)
                # Also validate sparse expressions even while paused.
                next_at = next_occurrences(updated["cron"], updated["timezone"], self.now())[0]
            else:
                next_at = None
            if updated["enabled"] and next_at is None:
                raise ValueError("Configure a cron schedule before enabling it")
            updated.update(revision=workflow["revision"] + 1, updatedAt=self.now())
            schedule_changed = any(updated[key] != workflow[key] for key in ("cron", "timezone", "enabled"))
            if schedule_changed:
                updated["nextAt"] = next_at if updated["enabled"] else None
            with self.journal.transaction() as connection:
                self._save(connection, updated)
            return updated

    def _claim(self, workflow, key, scheduled_at, outcome="claimed"):
        with self.journal.lock:
            previous = self.journal.connection.execute("SELECT outcome,run_id FROM workflow_dispatches WHERE key=?", (key,)).fetchone()
            if previous:
                return previous
            if self.journal.connection.execute("SELECT count(*) FROM workflow_dispatches").fetchone()[0] >= MAX_DISPATCHES:
                raise JournalError("Workflow dispatch retention limit reached; no new dispatch authorized")
            now = self.now()
            with self.journal.transaction() as connection:
                connection.execute("INSERT INTO workflow_dispatches VALUES (?,?,?,?,?,NULL)",
                                   (key, workflow["id"], scheduled_at, now, outcome))
                workflow["lastOutcome"] = outcome
                if scheduled_at is not None:
                    workflow["nextAt"] = next_occurrences(workflow["cron"], workflow["timezone"], max(now, scheduled_at))[0]
                self._save(connection, workflow)
        return None

    def _outcome(self, key, outcome, run_id=None):
        with self.journal.lock:
            row = self.journal.connection.execute("SELECT workflow_id FROM workflow_dispatches WHERE key=?", (key,)).fetchone()
            if row is None:
                raise JournalError("Workflow dispatch record is missing")
            workflow = self._get(row[0])
            latest = self.journal.connection.execute(
                "SELECT key FROM workflow_dispatches WHERE workflow_id=? ORDER BY rowid DESC LIMIT 1", (row[0],)).fetchone()[0]
            with self.journal.transaction() as connection:
                connection.execute("UPDATE workflow_dispatches SET outcome=?,run_id=? WHERE key=?", (outcome, run_id, key))
                if latest == key:
                    workflow.update(lastOutcome=outcome)
                    if run_id is not None:
                        workflow["lastRunId"] = run_id
                    self._save(connection, workflow)

    def _record_started(self, key, run_id):
        try:
            self._outcome(key, "started", run_id)
        except JournalError:
            raise WorkflowDispatchUncertain(run_id) from None

    def _dispatch(self, dispatch):
        workflow, key = dispatch.workflow, dispatch.key
        try:
            run, replayed = self.runtime.create(workflow["appId"], workflow["goal"], "live", key,
                                               output_schema=workflow["outputSchema"], output_format=workflow["outputFormat"],
                                               helper_model=workflow.get("helperModel"))
        except APIError as exc:
            request = self.journal.lookup(key)
            if request:
                self._record_started(key, request[0])
                raise
            outcome = ("skipped_busy" if exc.code in {"run_active", "device_busy"} else
                       "skipped_offline" if exc.code in {"device_unavailable", "shutting_down"} else "dispatch_failed")
            self._outcome(key, outcome)
            raise
        except (ValueError, JournalError):
            self._outcome(key, "dispatch_failed")
            raise
        # If this commit fails, the durable claim and canonical run idempotency
        # record remain. Recovery links them without ever invoking create again.
        self._record_started(key, run.id)
        return run, replayed

    def run(self, identifier, request_key):
        if not isinstance(request_key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_key):
            raise ValueError("Supply one valid Idempotency-Key to run a saved task")
        key = f"workflow:{identifier}:manual:{hashlib.sha256(request_key.encode()).hexdigest()}"
        with self.lock:
            workflow = self.get(identifier)
            prior = self._claim(workflow, key, None)
            if prior:
                with self.runtime.lock:
                    run = self.runtime.runs.get(prior[1])
                if run is not None:
                    return run, True
                if prior[1] is not None:
                    raise APIError("This task expired from history; it was not restarted", 410, "run_expired")
                raise APIError("The previous dispatch was not repeated. Review saved task status before using a new request.",
                               409, "workflow_dispatch_not_repeated")
            return self._dispatch(Dispatch(key, workflow))

    def _reconcile(self):
        with self.journal.lock:
            pending = self.journal.connection.execute("SELECT key,run_id FROM workflow_dispatches WHERE outcome='started'").fetchall()
        with self.runtime.lock:
            completed = [(key, run_id, self.runtime.runs[run_id].status if run_id in self.runtime.runs else "run_expired")
                         for key, run_id in pending if run_id not in self.runtime.runs or self.runtime.runs[run_id].finished_at is not None]
        for key, run_id, outcome in completed:
            self._outcome(key, outcome, run_id)

    def recover(self):
        """Run once before the scheduler starts; no missed/uncertain dispatch is replayed."""
        with self.lock:
            with self.journal.lock:
                claims = self.journal.connection.execute("SELECT key FROM workflow_dispatches WHERE outcome='claimed'").fetchall()
            for (key,) in claims:
                request = self.journal.lookup(key)
                self._outcome(key, "started" if request else "interrupted", request[0] if request else None)
            self._reconcile()
            for workflow in self.list():
                if workflow["enabled"] and workflow["nextAt"] <= self.now():
                    key = f"workflow:{workflow['id']}:scheduled:{workflow['nextAt']}"
                    self._claim(workflow, key, workflow["nextAt"], "skipped_missed")

    def tick(self):
        with self.lock:
            for workflow in self.list():
                now = self.now()  # Earlier readiness checks may have blocked or the wall clock may have changed.
                slot = workflow["nextAt"]
                if not workflow["enabled"] or slot is None or slot > now:
                    continue
                key = f"workflow:{workflow['id']}:scheduled:{slot}"
                outcome = "claimed" if now - slot < LATE_TOLERANCE_MS else "skipped_missed"
                with self.journal.lock:
                    attempts = self.journal.connection.execute("""SELECT workflow_id,claimed_at FROM workflow_dispatches
                        WHERE scheduled_at IS NOT NULL AND claimed_at>? AND outcome NOT LIKE 'skipped_%'""", (now - DAY_MS,)).fetchall()
                if len(attempts) >= self.daily_limit:
                    outcome = "skipped_daily_limit"
                elif any(identifier == workflow["id"] and now - claimed < MIN_INTERVAL_MS for identifier, claimed in attempts):
                    outcome = "skipped_interval_limit"
                if self._claim(workflow, key, slot, outcome) or outcome != "claimed":
                    continue
                try:
                    self._dispatch(Dispatch(key, workflow))
                except (APIError, ValueError):
                    # A skipped/failed occurrence is durable and never retried in a burst.
                    continue
