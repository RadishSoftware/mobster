"""Durable saved tasks and at-most-once dispatch, sharing the canonical run journal."""

from dataclasses import dataclass
import hashlib
import inspect
import json
import re
import threading
import time
import uuid

from .api_errors import APIError
from .catalog import APPS
from .journal import JournalError, encode
from .output_contract import validate_output
from .schedule import DAILY_LIMIT, MIN_INTERVAL_MS, local_timezone, next_occurrences, timezone, validate_cron
from .helper_models import resolve_helper_model


MAX_WORKFLOWS = 200
MAX_DISPATCHES = 10000
LATE_TOLERANCE_MS = 60_000
DAY_MS = 86_400_000
# Finished dispatch records are replay protection for a week: a slot never comes round again (its claim commits
# the next occurrence), a request key is retried within minutes, and a task that started keeps its request
# record in the run journal, which refuses a replay after that. Older records go, so schedules never fill up.
DISPATCH_RETENTION_MS = 7 * DAY_MS
# A scheduled dispatch's key (tick): workflow:<id>:scheduled:<slot>. Only these alert.
SCHEDULED = re.compile(r"workflow:([0-9a-f]{1,32}):scheduled:\d+")
# A scheduled run waiting on an approval this long gets an alert (alerts.APPROVAL_WAIT_S).
APPROVAL_ALERT_MS = 60_000
# The start app a Smart task names for the Home Screen (server.ANY_APP); a saved task stores null.
ANY_APP_ID = "any"
ENGINES = {"smart", "fast"}
CREATE_FIELDS = {"name", "appId", "appName", "goal", "outputSchema", "outputFormat", "helperModel", "engine", "cron", "timezone", "enabled",
                 "device"}
UPDATE_FIELDS = {"name", "goal", "appId", "appName", "cron", "timezone", "enabled", "revision", "helperModel", "engine", "device"}


def validate_goal(value):
    """A saved task's instruction: 1–4000 characters, stored trimmed."""
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 4000:
        raise ValueError("Supply a task of 1–4000 characters")
    return value.strip()


def validate_engine(value):
    """A saved engine is "smart", "fast" or None (None follows the app's default engine at run time)."""
    if value is None or value in ENGINES:
        return value
    raise ValueError('engine must be "smart" or "fast"')


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
    def __init__(self, runtime, clock=time.time, daily_limit=DAILY_LIMIT, alerts=None):
        self.runtime = runtime
        self.journal = runtime.journal
        self.clock = clock
        self.daily_limit = daily_limit
        # Webhook alerts for scheduled runs (alerts.py): opt-in by address, never blocking, never failing a run.
        self.alerts = alerts if alerts is not None else _default_alerts()
        self._alerted_approvals = set()
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

    def app_ids(self, wanted):
        """The ids a saved task may name: the catalog's, and the apps found on the phone."""
        ids = {app["id"] for app in APPS}
        apps = getattr(self.runtime, "apps", None)
        if wanted not in ids and callable(apps):
            ids |= {app["id"] for app in apps()}
        return ids

    def _accepts_engine(self):
        """Whether this runtime runs engines (Runtime.create(engine=...)); a saved engine must never be dropped."""
        return self._accepts("engine")

    def _accepts(self, name):
        try:
            parameters = inspect.signature(self.runtime.create).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(p.name == name or p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters)

    def _devices_here(self):
        """Whether this runtime runs tasks on several devices (a fleet, and Runtime.create(default_device=...))."""
        return getattr(self.runtime, "fleet", None) is not None and self._accepts("default_device")

    def _device(self, value):
        """A saved task's device: None (the default device when it runs), or a device this Mobster knows, saved
        by its id however it was named. The device may be away now; a run then skips as offline."""
        if value is None:
            return None
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 128:
            raise ValueError("device must be a device's id, UDID or name")
        fleet = getattr(self.runtime, "fleet", None)
        if fleet is None or not self._accepts("device"):
            raise ValueError("This version of Mobster runs every task on one device. Save the task without a device.")
        try:
            return fleet.find(value.strip()).id
        except LookupError as error:
            raise ValueError(str(error)) from None

    def _app_name(self, app_id, given=None):
        """The name shown for a saved task's app, kept with it so a phone app still has a name while the phone is away.
        The caller's name wins; otherwise the catalog's, then the phone's. None when the task has no app."""
        if app_id is None:
            return None
        if given is not None:
            if not isinstance(given, str) or not 1 <= len(given.strip()) <= 120:
                raise ValueError("Name the app using 1–120 characters")
            return given.strip()
        names = {app["id"]: app["name"] for app in APPS}
        apps = getattr(self.runtime, "apps", None)
        if app_id not in names and callable(apps):
            try:
                names.update({app["id"]: app["name"] for app in apps()})
            except Exception:  # The phone's inventory is optional here; the list falls back to a plain label.
                pass
        return names.get(app_id)

    def _check_task(self, workflow):
        """The app and engine a saved task starts with. Only Smart may start without an app (on the Home screen)."""
        engine = validate_engine(workflow.get("engine"))
        if engine is not None and not self._accepts_engine():
            raise ValueError("This version of Mobster has one engine. Save the task without an engine.")
        app_id = workflow.get("appId")
        if app_id is None and engine == "smart":
            return
        if app_id is None:
            raise ValueError("Choose the app to run this task in")
        if not isinstance(app_id, str) or app_id not in self.app_ids(app_id):
            raise ValueError("Choose an app from your catalog")

    def _schedule(self, workflow):
        """Validates cron, timezone and enabled in place and returns the next occurrence (None while paused)."""
        if type(workflow["enabled"]) is not bool:
            raise ValueError("enabled must be true or false")
        timezone(workflow["timezone"])
        if workflow["cron"] is not None:
            workflow["cron"] = validate_cron(workflow["cron"], workflow["timezone"], self.daily_limit)
            # Also validate sparse expressions even while paused.
            next_at = next_occurrences(workflow["cron"], workflow["timezone"], self.now())[0]
        else:
            next_at = None
        if workflow["enabled"] and next_at is None:
            raise ValueError("Configure a cron schedule before enabling it")
        return next_at

    def create(self, body):
        if not isinstance(body, dict) or set(body) - CREATE_FIELDS:
            raise ValueError("Unsupported saved task field")
        name = body.get("name")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
            raise ValueError("Name the saved task using 1–120 characters")
        goal = validate_goal(body.get("goal"))
        engine = validate_engine(body.get("engine"))
        app_id = body.get("appId")
        if app_id == ANY_APP_ID and engine == "smart":
            app_id = None  # a Smart run started on the Home Screen, saved from the run view
        self._check_task({"appId": app_id, "engine": engine})
        app_name = self._app_name(app_id, body.get("appName"))
        output_format = body.get("outputFormat", "auto")
        schema = validate_output(output_format, body.get("outputSchema"))
        helper_model = resolve_helper_model(body.get("helperModel"))
        now = self.now()
        # A new schedule reads in the Mac's own time unless the caller names a zone (existing ones keep theirs).
        device = self._device(body.get("device"))
        workflow = {"id": uuid.uuid4().hex[:12], "name": name.strip(), "appId": app_id, "appName": app_name, "goal": goal,
                    "outputSchema": schema, "outputFormat": output_format, "helperModel": helper_model,
                    "engine": engine, "device": device, "cron": body.get("cron"), "timezone": body.get("timezone", local_timezone()),
                    "enabled": body.get("enabled", False), "revision": 1, "createdAt": now, "updatedAt": now,
                    "nextAt": None, "lastRunId": None, "lastOutcome": None}
        next_at = self._schedule(workflow)
        workflow["nextAt"] = next_at if workflow["enabled"] else None
        with self.lock, self.journal.lock:
            if self.journal.connection.execute("SELECT count(*) FROM workflows").fetchone()[0] >= MAX_WORKFLOWS:
                raise APIError("Saved task limit reached", 409, "workflow_limit")
            with self.journal.transaction() as connection:
                connection.execute("INSERT INTO workflows VALUES (?,?)", (workflow["id"], encode(workflow)))
        return workflow

    def update(self, identifier, body):
        if (not isinstance(body, dict) or set(body) - UPDATE_FIELDS
                or type(body.get("revision")) is not int):
            raise ValueError("Supply the current revision and supported schedule fields")
        with self.lock, self.journal.lock:
            workflow = self._get(identifier)
            if body["revision"] != workflow["revision"]:
                raise APIError("The saved task changed. Reload before editing it.", 409, "revision_conflict")
            updated = {**workflow, **body}
            if "helperModel" in body:
                updated["helperModel"] = resolve_helper_model(body["helperModel"])
            if "goal" in body:
                updated["goal"] = validate_goal(body["goal"])
            if "device" in body:
                updated["device"] = self._device(body["device"])
            if "engine" in body or "appId" in body:
                self._check_task(updated)
            if "appId" in body or "appName" in body:
                # A name sent with the change wins. Without one, the same app keeps its name and a new app is looked up.
                if "appName" in body:
                    given = body["appName"]
                elif updated.get("appId") == workflow.get("appId"):
                    given = workflow.get("appName")
                else:
                    given = None
                updated["appName"] = self._app_name(updated.get("appId"), given)
            if not isinstance(updated["name"], str) or not 1 <= len(updated["name"].strip()) <= 120:
                raise ValueError("Name the saved task using 1–120 characters")
            updated["name"] = updated["name"].strip()
            next_at = self._schedule(updated)
            updated.update(revision=workflow["revision"] + 1, updatedAt=self.now())
            schedule_changed = any(updated[key] != workflow[key] for key in ("cron", "timezone", "enabled"))
            if schedule_changed:
                updated["nextAt"] = next_at if updated["enabled"] else None
            with self.journal.transaction() as connection:
                self._save(connection, updated)
            return updated

    def delete(self, identifier):
        """Removes a saved task and its dispatch records. Its schedule stops; tasks it started stay in History.
        Serialized with dispatch (self.lock), so a delete never lands between a claim and its outcome."""
        with self.lock, self.journal.lock:
            self._get(identifier)
            with self.journal.transaction() as connection:
                connection.execute("DELETE FROM workflow_dispatches WHERE workflow_id=?", (identifier,))
                connection.execute("DELETE FROM workflows WHERE id=?", (identifier,))
        return identifier

    def _claim(self, workflow, key, scheduled_at, outcome="claimed"):
        with self.journal.lock:
            previous = self.journal.connection.execute("SELECT outcome,run_id FROM workflow_dispatches WHERE key=?", (key,)).fetchone()
            if previous:
                return previous
            now = self.now()
            with self.journal.transaction() as connection:
                self._prune(connection, now)
                if connection.execute("SELECT count(*) FROM workflow_dispatches").fetchone()[0] >= MAX_DISPATCHES:
                    raise JournalError("Workflow dispatch retention limit reached; no new dispatch authorized")
                connection.execute("INSERT INTO workflow_dispatches VALUES (?,?,?,?,?,NULL)",
                                   (key, workflow["id"], scheduled_at, now, outcome))
                workflow["lastOutcome"] = outcome
                if scheduled_at is not None:
                    workflow["nextAt"] = next_occurrences(workflow["cron"], workflow["timezone"], max(now, scheduled_at))[0]
                self._save(connection, workflow)
        return None

    @staticmethod
    def _prune(connection, now):
        """Drop finished dispatch records past DISPATCH_RETENTION_MS. A full table then gives up its oldest finished
        records: skipped ones of any age, and others from before the last day, which the daily limit and interval
        read. They never count a skipped record, so 200 workflows skipping 60 times a day each can't fill it."""
        finished = "outcome NOT IN ('claimed','started')"
        connection.execute(f"DELETE FROM workflow_dispatches WHERE claimed_at<? AND {finished}",
                           (now - DISPATCH_RETENTION_MS,))
        excess = connection.execute("SELECT count(*) FROM workflow_dispatches").fetchone()[0] - MAX_DISPATCHES + 1
        if excess > 0:
            connection.execute(f"""DELETE FROM workflow_dispatches WHERE rowid IN (SELECT rowid FROM workflow_dispatches
                WHERE {finished} AND (claimed_at<? OR outcome LIKE 'skipped_%') ORDER BY claimed_at LIMIT ?)""",
                               (now - DAY_MS, excess))

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
            # The engine and the device ride along only when one was saved: older runtimes and workflows have none.
            engine, device = workflow.get("engine"), workflow.get("device")
            # A saved task with no device runs on the primary phone, never on whichever phone was used last: a
            # schedule fires unattended, and "text my team" must not go out from another phone.
            run, replayed = self.runtime.create(workflow.get("appId"), workflow["goal"], "live", key,
                                               output_schema=workflow["outputSchema"], output_format=workflow["outputFormat"],
                                               helper_model=workflow.get("helperModel"),
                                               **({"engine": engine} if engine else {}),
                                               **({"device": device} if device else
                                                  {"default_device": "primary"} if self._devices_here() else {}),
                                               **({"origin": "schedule" if ":scheduled:" in key else "workflow"}
                                                  if "origin" in inspect.signature(self.runtime.create).parameters
                                                  else {}))
        except APIError as exc:
            request = self.journal.lookup(key)
            if request:
                self._record_started(key, request[0])
                raise
            outcome = ("skipped_busy" if exc.code in {"run_active", "device_busy"} else
                       "skipped_offline" if exc.code in {"device_unavailable", "shutting_down", "device_not_found"}
                       else "dispatch_failed")
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
            self._alert_finished(key, run_id, outcome)

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

    # -- alerts (alerts.py): scheduled runs only, never a manual one -----------------------------------------

    def _alert_finished(self, key, run_id, outcome):
        """A scheduled run ended without finishing: post its alert. Never raises."""
        if self.alerts is None or SCHEDULED.fullmatch(key or "") is None or run_id is None:
            return
        try:
            with self.journal.lock:
                workflow = self._get(SCHEDULED.fullmatch(key).group(1))
            with self.runtime.lock:
                run = self.runtime.runs.get(run_id)
            summary = getattr(run, "summary", None) or {}
            code = summary.get("code") if isinstance(summary, dict) else None
            self.alerts.run_finished(workflow.get("name"), outcome, run_id, code if isinstance(code, str) else None)
        except Exception:
            pass

    def _alert_waiting(self):
        """A scheduled run has waited on an approval for a minute or more: post one alert per approval."""
        if self.alerts is None:
            return
        try:
            with self.journal.lock:
                started = self.journal.connection.execute(
                    "SELECT key,run_id FROM workflow_dispatches WHERE outcome='started' AND scheduled_at IS NOT NULL"
                ).fetchall()
            if not started:
                self._alerted_approvals.clear()
                return
            now = self.now()
            for key, run_id in started:
                with self.runtime.lock:
                    run = self.runtime.runs.get(run_id)
                approval = getattr(run, "approval", None)
                if not isinstance(approval, dict) or approval.get("id") in self._alerted_approvals:
                    continue
                asked = approval.get("requestedAt")
                match = SCHEDULED.fullmatch(key)
                if match and isinstance(asked, (int, float)) and now - asked >= APPROVAL_ALERT_MS:
                    self._alerted_approvals.add(approval.get("id"))
                    with self.journal.lock:
                        name = self._get(match.group(1)).get("name")
                    self.alerts.approval_waiting(name, run_id)
        except Exception:
            pass

    def tick(self):
        with self.lock:
            self._alert_waiting()
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


def _default_alerts():
    """alerts.Alerts, or None in a build without it: scheduling never depends on alerts."""
    try:
        from .alerts import Alerts
    except ImportError:
        return None
    return Alerts()
