"""Starting and running workflows: each dispatch is claimed before it acts, and an unknown outcome never runs again."""

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass

from .. import engines, harness_api, secret_filter
from ..api_errors import APIError, nothing_started
from ..graph import chain as graph_chain
from ..graph import dispatch as graph_dispatch
from ..graph import nodes as graph_nodes
from ..graph import rerun as graph_rerun
from ..graph import run as graph_run
from ..graph import schema as graph_schema
from ..graph.run import GraphRun
from ..journal import JournalError
from . import health, starters
from . import records as batch
from .cron import MIN_INTERVAL_MS, next_occurrences

THREAD_ID = re.compile(r"[a-f0-9]{12}")
MAX_DISPATCHES = 10000
LATE_TOLERANCE_MS = 60_000
DAY_MS = 86_400_000
# Finished dispatch records protect replays for a week. A slot's claim advances its schedule; a started task's
# run-journal request record keeps refusing a replay after that, while old dispatches can be pruned.
DISPATCH_RETENTION_MS = 7 * DAY_MS
# A scheduled dispatch's key (tick): workflow:<id>:scheduled:<slot>. Only these alert.
SCHEDULED = re.compile(r"workflow:([0-9a-f]{1,32}):scheduled:\d+")
# A scheduled run waiting on an approval this long gets an alert (alerts.APPROVAL_WAIT_S).
APPROVAL_ALERT_MS = 60_000


@dataclass(frozen=True)
class Dispatch:
    key: str
    workflow: dict
    text: str | None = None              # the filled steps (None: the defaults)
    thread: str | None = None            # run in this conversation ("Run in this conversation")
    new_conversation: bool = False       # a new conversation even when the workflow has a home one
    origin: str | None = None            # the request's origin (X-Mobster-Origin); None for a schedule
    values: dict | None = None           # the blanks' values a run gave (None: the defaults)
    rows: list | None = None             # an each repeat's rows a run gave (None: the workflow's own)
    remote_grant: str | None = None      # the Mobster Remote app that started it (origin "remote" only)
    test: bool = False                   # a test run: the draft, with its pins (graph/rerun.py)
    rerun: dict | None = None            # {"from" | "only" | "resume": ...}: a re-run of an earlier run
    body: dict | None = None             # a webhook's JSON body: the webhook trigger's body, data (masked, 16 KB)
    trigger: str | None = None           # shortcut, notification or app: what started it (its trigger's entry)
    input: str | None = None             # what the Shortcut sent: the trigger's input, data
    event: dict | None = None            # a notification, or an app trigger's {row, rows}: data (triggers.py)
    # the Settings trigger that started it, {id, name}: its tasks' run field trigger
    started_by: dict | None = None
    project_secrets: bool = True         # trusted local dispatch or private app session, never request JSON
    prove: bool = False                  # a test run that, when it completes, pins its steps' outputs (prove)


@dataclass(frozen=True)
class Admission:
    """What a dispatch resolves before it creates a task"""
    thread: dict | None
    made: bool
    home: bool
    extras: dict
    engine: str | None
    device: str | None
    origin: str

    @property
    def thread_id(self):
        return self.thread and self.thread["id"]


class WorkflowDispatchUncertain(JournalError):
    """Canonical run creation succeeded, but saving its workflow linkage failed."""
    def __init__(self, run_id):
        super().__init__("The task may already have started. Check its run status before making another request.")
        self.run_id = run_id


class WorkflowRuns:
    @staticmethod
    def live(workflow):
        """The graph that runs: the published one (``graph`` is the draft the canvas edits)."""
        published = workflow.get("published")
        return published["graph"] if isinstance(published, dict) and graph_schema.check_graph(
            published.get("graph")) else workflow["graph"]

    def _claim(self, workflow, key, scheduled_at, outcome="claimed"):
        with self.journal.lock:
            previous = self.journal.connection.execute("SELECT outcome,run_id FROM workflow_dispatches WHERE key=?",
                                                       (key,)).fetchone()
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
                    workflow["nextAt"] = next_occurrences(workflow["cron"], workflow["timezone"],
                                                          max(now, scheduled_at))[0]
                self._save(connection, workflow)
        return None

    @staticmethod
    def _prune(connection, now):
        """Drop finished dispatch records past DISPATCH_RETENTION_MS."""
        finished = "outcome NOT IN ('claimed','started')"
        connection.execute(f"DELETE FROM workflow_dispatches WHERE claimed_at<? AND {finished}",
                           (now - DISPATCH_RETENTION_MS,))
        excess = connection.execute("SELECT count(*) FROM workflow_dispatches").fetchone()[0] - MAX_DISPATCHES + 1
        if excess > 0:
            connection.execute(f"""DELETE FROM workflow_dispatches WHERE rowid IN (SELECT rowid FROM workflow_dispatches
                WHERE {finished} AND (claimed_at<? OR outcome LIKE 'skipped_%') ORDER BY claimed_at LIMIT ?)""",
                               (now - DAY_MS, excess))

    def _outcome(self, key, outcome, run_id=None, thread_id=None, home=False):
        with self.journal.transaction() as connection:
            self._save_outcome(connection, key, outcome, run_id, thread_id, home)

    def _save_outcome(self, connection, key, outcome, run_id=None, thread_id=None, home=False):
        row = connection.execute("SELECT workflow_id FROM workflow_dispatches WHERE key=?", (key,)).fetchone()
        if row is None:
            raise JournalError("Workflow dispatch record is missing")
        workflow = self._get(row[0])
        success = self._success(workflow, outcome, run_id)
        latest = connection.execute(
            "SELECT key FROM workflow_dispatches WHERE workflow_id=? ORDER BY rowid DESC LIMIT 1",
            (row[0],)).fetchone()[0]
        connection.execute("UPDATE workflow_dispatches SET outcome=?,run_id=? WHERE key=?", (outcome, run_id, key))
        changed = False
        if home and thread_id and workflow.get("homeThreadId") != thread_id:
            workflow["homeThreadId"], changed = thread_id, True
        if success is not None and (success["at"] or 0) > ((workflow.get("lastSuccess") or {}).get("at") or 0):
            # Its newest verified success, kept with the workflow: the dispatch records go after a week.
            workflow["lastSuccess"], changed = success, True
        if latest == key:
            workflow.update(lastOutcome=outcome)
            if run_id is not None and not str(run_id).startswith("batch:"):
                workflow["lastRunId"] = run_id
            if thread_id is not None:
                workflow["lastThreadId"] = thread_id
            changed = True
        if changed:
            self._save(connection, workflow)

    def _success(self, workflow, outcome, run_id):
        """The success an ended attempt records ({at, runId, line}), or None"""
        if outcome in health.GOING or not isinstance(run_id, str) or run_id.startswith("batch:"):
            return None
        run = (getattr(self.runtime, "runs", None) or {}).get(run_id)
        if run is None or getattr(run, "finished_at", None) is None:
            return None
        done = health.verified(run) if health.is_plain(workflow) else outcome == "completed"
        return health.success_of(run) if done else None

    def attempts(self, identifier, limit=20):
        """The workflow's newest dispatches, newest first: (outcome, run id, claimed at in ms)."""
        with self.journal.lock:
            return self.journal.connection.execute(
                "SELECT outcome,run_id,claimed_at FROM workflow_dispatches WHERE workflow_id=? ORDER BY rowid DESC "
                "LIMIT ?",
                (identifier, limit)).fetchall()

    def health(self, workflow):
        """What the workflow's row says about it (health.py): its last verified success and what needs you."""
        with self.runtime.lock:
            runs = dict(self.runtime.runs)
        filled = (self._account_values(workflow) or {}) if workflow.get("enabled") else {}
        return health.workflow_health(workflow, self.attempts(workflow["id"]), runs, filled)

    def list_with_health(self):
        """GET /api/workflows: every workflow with its ``health``."""
        return [{**workflow, "health": self.health(workflow)} for workflow in self.list()]

    def _record_started(self, key, run_id, thread_id=None, home=False):
        try:
            self._outcome(key, "started", run_id, thread_id, home)
        except JournalError:
            raise WorkflowDispatchUncertain(run_id) from None

    def _target(self, service, thread_id):
        """The conversation a request names, checked before anything is claimed"""
        if not isinstance(thread_id, str) or not THREAD_ID.fullmatch(thread_id):
            raise ValueError("threadId is a conversation's 12-character id")
        if service is None:
            raise APIError("This Mobster can't run a workflow in a conversation. Update Mobster, then try again.",
                           409, "conversations_unavailable")
        thread = service.store.get(thread_id)
        if thread is None:
            raise APIError("That conversation was deleted.", 404, "thread_not_found")
        if thread.get("archived"):
            raise APIError("That conversation is archived. Restore it to continue.", 409, "thread_archived")
        return thread

    def _conversation(self, service, workflow, dispatch):
        """(thread, made, home): the run's conversation, whether it was made for it, whether it is now the home."""
        if dispatch.thread:
            thread = service.store.get(dispatch.thread)
            if thread is None or thread.get("archived"):
                raise APIError("That conversation is no longer open.", 409, "thread_unavailable")
            return thread, False, False
        same = bool(workflow.get("sameConversation")) and not dispatch.new_conversation
        home = workflow.get("homeThreadId") if same else None
        thread = service.store.get(home) if home else None
        if thread is not None and not thread.get("archived"):
            return thread, False, False
        # A new conversation, named after the workflow (a title is at most 80 characters, cut at a word).
        title = workflow["name"] if len(workflow["name"]) <= 80 else workflow["name"][:80].rsplit(" ", 1)[0]
        return service.create_thread(title), True, same

    def _drop(self, service, thread_id):
        """A conversation made for a run that never started goes again."""
        try:
            service.store.delete_thread(thread_id)
            service.published_thread({"id": thread_id}, "thread_deleted")
        except Exception:  # noqa: BLE001 -- an empty conversation left behind is harmless
            pass

    def _admit(self, service, dispatch):
        """The conversation, extras, engine, device and origin a run starts with (plain or batch)."""
        workflow = dispatch.workflow
        # The engine and the device ride along only when one was saved.
        engine, device = engines.read_engine(workflow.get("engine")), workflow.get("device")
        account = workflow.get("account")
        extras = {"account": account} if account else {}
        if dispatch.remote_grant:
            extras["remoteGrant"] = dispatch.remote_grant
        if dispatch.started_by and "trigger" in harness_api.run_fields():
            extras["trigger"] = dispatch.started_by     # "Started by trigger: <name>" (the triggers track's run field)
        file = workflow.get("file")
        if isinstance(file, dict) and file.get("projectId") and "project" in harness_api.run_fields():
            project = harness_api.clean_extras({"project": {"id": file["projectId"]}}, self.runtime)["project"]
            extras["project"] = project
            if "projectEnv" in harness_api.run_fields():
                extras["projectEnv"] = project["path"]
        thread, made, home = None, False, False
        if service is not None:
            thread, made, home = self._conversation(service, workflow, dispatch)
            extras["threadId"] = thread["id"]
            from ..threads.service import ORIGIN_VIA  # threads is optional: imported where it is present
            if ORIGIN_VIA.get(dispatch.origin):
                extras["via"] = ORIGIN_VIA[dispatch.origin]
            if dispatch.origin in harness_api.INTERACTIVE and "askUser" in harness_api.run_fields():
                extras["askUser"] = True  # someone is watching this conversation and can answer
            # Run here: the conversation's own phone, when the workflow names none. A workflow for an account
            # runs on the account's phone (accounts/routing.py), never on the conversation's.
            if not device and not account and thread.get("device") and self._devices_here():
                device = thread["device"]
        origin = (dispatch.origin if dispatch.origin in harness_api.UNATTENDED_ORIGINS
                  else "api" if not dispatch.project_secrets and extras.get("projectEnv")
                  else "schedule" if graph_dispatch.unattended(dispatch.key) else "workflow")
        return Admission(thread, made, home, extras, engine, device, origin)

    def _dispatch(self, dispatch):
        workflow, key = dispatch.workflow, dispatch.key
        refused = _unsupported(workflow, draft=dispatch.test)
        if refused:
            self._outcome(key, "dispatch_failed")
            raise refused
        if dispatch.test or dispatch.rerun or not graph_chain.is_plain(self.live(workflow)):
            return self._dispatch_batch(dispatch)
        service, admission = self._threads(), None
        try:
            text = dispatch.text if dispatch.text is not None else starters.fill(workflow["goal"],
                                                                                 self._account_values(workflow))
            admission = self._admit(service, dispatch)
            extras, engine, device = admission.extras, admission.engine, admission.device
            send_to = self._send_extras(workflow, dispatch.values)
            if send_to is not None:
                extras["sendTo"] = send_to
            if dispatch.input is not None or dispatch.event is not None:
                # What a Shortcut sent, or the notification: the task reads it as data, fenced, never as its words.
                extras["workflowInput"] = graph_nodes.as_text(dispatch.event if dispatch.event is not None
                                                              else dispatch.input)
            # With no key for Mobster's agent, the person's ready coding agent does the task, as the composer sends a
            # typed one (DR-56). A replay or a no-model task keeps its engine.
            if not engine and extras.get("replay") != "only" and not harness_api.calls_no_model(extras):
                engine = getattr(self.runtime, "plan_agent", lambda: None)()
            # A saved task with no device runs on the primary phone, never on whichever phone was used last: a
            # schedule fires unattended, and "text my team" must not go out from another phone.
            run, replayed = self.runtime.create(workflow.get("appId"), text, "live", key,
                                               output_schema=workflow["outputSchema"],
                                               output_format=workflow["outputFormat"],
                                               **({"engine": engine} if engine else {}),
                                               **({"device": device} if device else
                                                  {"default_device": "primary"} if self._devices_here() else {}),
                                               **({"extras": extras} if extras else {}),
                                               origin=admission.origin, _workflow_id=workflow["id"])
        except APIError as exc:
            request = self.journal.lookup(key)
            if request:
                self._record_started(key, request[0], admission and admission.thread_id, admission and admission.home)
                raise
            if admission and admission.made:
                self._drop(service, admission.thread_id)
            outcome = ("skipped_busy" if exc.code in {"run_active", "device_busy"} else
                       "skipped_offline" if exc.code in {"device_unavailable", "shutting_down", "device_not_found"}
                       else "skipped_monthly_limit" if exc.code == "monthly_limit_reached"
                       else "dispatch_failed")
            self._outcome(key, outcome)
            raise nothing_started(exc) from None   # no task under this request's key: none was recorded or sent
        except (ValueError, JournalError):
            request = self.journal.lookup(key)
            if request:  # the run was created: keep it, and never create it again
                self._record_started(key, request[0], admission and admission.thread_id, admission and admission.home)
                raise
            if admission and admission.made:
                self._drop(service, admission.thread_id)
            self._outcome(key, "dispatch_failed")
            raise
        # If this commit fails, the durable claim and canonical run idempotency
        # record remain. Recovery links them without ever invoking create again.
        self._record_started(key, run.id, admission.thread_id, admission.home)
        return run, replayed

    @staticmethod
    def _request_key(identifier, request_key, values=None, thread_id=None, new_conversation=False, rows=None,
                     debug=None):
        asked = values is not None or thread_id is not None or new_conversation
        try:
            fingerprint = starters.values_fingerprint(values, thread_id, new_conversation) if asked else None
        except ValueError:     # typed values (a graph's list or object inputs) aren't a starter's short text
            fingerprint = hashlib.sha256(json.dumps({"values": values, "thread": thread_id, "new": new_conversation},
                                                    sort_keys=True, ensure_ascii=False,
                                                    default=str).encode()).hexdigest()
        digest = request_key if not asked else request_key + "\n" + fingerprint
        if rows is not None:
            digest += "\n" + hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if debug:
            digest += "\n" + hashlib.sha256(json.dumps(debug, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return f"workflow:{identifier}:manual:{hashlib.sha256(digest.encode()).hexdigest()}"

    def _check_values(self, workflow, values, rows, draft=False):
        """The words a run sends (a one-task workflow's) after checking every blank has a value"""
        graph = workflow["graph"] if draft else self.live(workflow)   # a test run runs the draft
        given = self._account_values(workflow, values, typed=not graph_chain.is_plain(graph))
        if graph_chain.is_plain(graph):
            return starters.fill(graph_chain.words(graph) if draft else workflow["goal"], given)
        if draft or graph_chain.as_chain(graph) is None:
            graph_dispatch.check_values(graph, given, rows)
            return None
        repeat = workflow.get("repeat") or {}
        if repeat.get("kind") == "each":
            rows = rows if rows is not None else repeat.get("rows")
            if not rows:
                raise ValueError("Give the rows to repeat over, as a list or a CSV")
        elif rows is not None:
            raise ValueError("Rows are for a workflow that repeats for each row")
        names = {starters._key(blank["name"]) for blank in starters.blanks(workflow["goal"])}
        base = starters.clean_values(given)
        for row in rows or [{}]:
            merged = {**base, **starters.clean_values(row)}
            starters.fill(workflow["goal"], {name: value for name, value in merged.items() if name in names})
        return None

    def _outside(self, identifier, origin, remote_grant, values, thread_id, rows):
        """The values a run from Mobster Remote may send, after the rules that fail closed"""
        if origin != "remote":
            return values
        if not remote_grant:
            raise ValueError("A run through Mobster Remote names its app")
        if thread_id is not None or rows is not None:
            raise APIError("A run through Mobster Remote takes values only.", 403, "remote_unsupported")
        if not self.has_webhook(identifier):
            raise APIError("This workflow has no webhook URL, so apps connected through Mobster Remote can't start it. "
                           "To allow it, open the workflow in Mobster, choose Edit, More, "
                           "then Start from another tool.",
                           403, "remote_unsupported")
        if not isinstance(values, dict):
            return values
        return {name: secret_filter.redact(" ".join(str(value).split())[:300])
                if isinstance(value, (str, int, float)) and not isinstance(value, bool) else value
                for name, value in values.items()}

    def has_webhook(self, identifier):
        from ..outlets import hooks
        service = hooks.service_for(self.runtime)
        try:
            return service is not None and bool(service.store.all(identifier))
        except JournalError:
            return False

    def run(self, identifier, request_key, values=None, thread_id=None, new_conversation=False, origin=None, rows=None,
            remote_grant=None, test=False, rerun=None, body=None, trigger=None, input=None, prove=False,
            project_secrets=True):
        """Start a workflow now."""
        return self._run(identifier, request_key, values, thread_id, new_conversation, origin, rows, remote_grant,
                         test, rerun, body, trigger, input, prove, project_secrets)[:2]

    def _run(self, identifier, request_key, values=None, thread_id=None, new_conversation=False, origin=None, rows=None,
             remote_grant=None, test=False, rerun=None, body=None, trigger=None, input=None, prove=False,
             project_secrets=True):
        """run(), also returning the dispatch key: (run, replayed, key)."""
        if not isinstance(request_key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_key):
            raise ValueError("Supply one valid Idempotency-Key to run a saved task")
        if type(new_conversation) is not bool:
            raise ValueError("newConversation must be true or false")
        if thread_id is not None and new_conversation:
            raise ValueError("Give threadId or newConversation, not both")
        if rows is not None:
            rows = batch.validate_rows(rows)
        if type(test) is not bool or rerun is not None and not isinstance(rerun, dict):
            raise ValueError("test is true or false, and a re-run is from, only or resume")
        if (test or rerun) and origin in harness_api.UNATTENDED_ORIGINS:
            raise APIError("A test run or a re-run starts from Mobster's editor.", 403, "test_from_editor")
        if not test and rerun is None and origin in DRAFT_TESTERS and self._never_published(identifier):
            # Run on a workflow that was never published tests the draft (docs/workflows.mdx › Test it): marked as a
            # test, with its pins, and never a run of the published version. Only a person at the app, the terminal
            # or the CLI: a schedule, a webhook, MCP or Remote still meet "Publish it, then run it".
            test = True
        if trigger not in (None, "shortcut") or input is not None and (
                trigger is None or not isinstance(input, str) or len(input) > graph_nodes.INPUT_CHARS):
            raise ValueError(f"trigger is shortcut, and its input is text of at most {graph_nodes.INPUT_CHARS:,} "
                             "characters")
        if trigger and origin in harness_api.UNATTENDED_ORIGINS:
            # A Shortcut runs on this Mac (Run Mobster Workflow in the Mac app, or a script with its token); MCP,
            # Remote and webhooks start a workflow at its manual or webhook trigger.
            raise APIError("A Shortcut starts this run on your Mac.", 403, "shortcut_only")
        if prove and (not test or rerun):
            raise ValueError("A prove is a test run of the draft")
        debug = {key: value for key, value in (("test", test), ("rerun", rerun), ("trigger", trigger),
                                               ("input", input), ("prove", prove)) if value} or None
        if body is not None:
            from ..output_contract import bounded_json
            try:
                body = bounded_json(secret_filter.redact_deep(body), max_bytes=16_000)
            except ValueError:
                body = {"cut": True}
        with self.lock:
            values = self._outside(identifier, origin, remote_grant, values, thread_id, rows)
            key = self._request_key(identifier, request_key, values, thread_id, new_conversation, rows, debug)
            if getattr(self.runtime, "closing", False):
                # "Nothing started" only for a key never claimed or run here: a retry of a start must not hear it.
                with self.journal.lock:
                    seen = self.journal.connection.execute("SELECT 1 FROM workflow_dispatches WHERE key=?",
                                                           (key,)).fetchone() or self.journal.lookup(key)
                refusal = APIError("The service is shutting down", 503, "shutting_down")
                raise refusal if seen else nothing_started(refusal)
            with self.journal.lock:
                workflow = self._get(identifier)
            refused = _unsupported(workflow, draft=test)
            if refused:
                raise refused
            text = None if rerun else self._check_values(workflow, values, rows, draft=test)
            if thread_id is not None:
                self._target(self._threads(), thread_id)
            prior = self._claim(workflow, key, None)
            if prior:
                record = self.batches.public(self.batches.by_key(key))
                if record is not None or isinstance(prior[1], str) and prior[1].startswith("batch:"):
                    record = record or self.batches.public(self.batches.get(prior[1][6:]))
                    first = next((entry.get("runId") for entry in (record or {}).get("log", [])
                                  if entry.get("runId")), None)
                    with self.runtime.lock:
                        return (self.runtime.runs.get(first) if first else None), True, key
                with self.runtime.lock:
                    run = self.runtime.runs.get(prior[1])
                if run is not None:
                    return run, True, key
                if prior[1] is not None:
                    raise APIError("This task expired from history; it was not restarted", 410, "run_expired")
                raise APIError(
                    "The previous dispatch was not repeated. Review saved task status before using a new request.",
                    409, "workflow_dispatch_not_repeated")
            run, replayed = self._dispatch(Dispatch(key, workflow, text=text, thread=thread_id,
                                                    new_conversation=new_conversation, origin=origin, values=values,
                                                    rows=rows, remote_grant=remote_grant, test=test, rerun=rerun,
                                                    body=body, trigger=trigger, input=input, prove=prove,
                                                    project_secrets=project_secrets))
            return run, replayed, key

    def _never_published(self, identifier):
        """Whether the workflow is a graph that has never been published (so only its draft can run)."""
        try:
            with self.journal.lock:
                workflow = self._get(identifier)
        except APIError:
            return False
        return bool(graph_schema.check_graph(workflow.get("graph")) and not isinstance(workflow.get("published"), dict))

    def start(self, identifier, request_key, values=None, thread_id=None, new_conversation=False, origin=None,
              rows=None, remote_grant=None, test=False, rerun=None, body=None, trigger=None, input=None, prove=False,
              project_secrets=True):
        """POST /api/workflows/{id}/run's answer"""
        run, replayed, key = self._run(identifier, request_key, values, thread_id, new_conversation, origin, rows,
                                       remote_grant, test, rerun, body, trigger=trigger, input=input, prove=prove,
                                       project_secrets=project_secrets)
        record = self.batches.public(self.batches.by_key(key))
        extras = getattr(run, "extras", None)
        kept = record is not None and (record["status"] == "running"
                                       or any(r.get("runIds") for r in record.get("rounds", [])))
        thread = extras.get("threadId") if isinstance(extras, dict) else record["threadId"] if kept else None
        return {"run": run.public() if run is not None else None, "replayed": replayed, "threadId": thread,
                "batch": self._batch_view(record)}

    def _reconcile(self):
        with self.journal.lock:
            pending = self.journal.connection.execute(
                "SELECT key,run_id FROM workflow_dispatches WHERE outcome='started'").fetchall()
        batches = [(key, ref) for key, ref in pending if isinstance(ref, str) and ref.startswith("batch:")]
        pending = [(key, ref) for key, ref in pending if (key, ref) not in batches]
        live = set(self.running().values())
        for key, ref in batches:
            identifier = ref[6:]
            if identifier in live:
                continue
            record = self.batches.get(identifier)
            if record is not None and record["status"] == "running":
                continue  # recover() ends a batch that outlived its process
            runs = [entry["runId"] for entry in (self.batches.public(record) or {}).get("log", [])
                    if entry.get("runId")]
            self._outcome(key, record["status"] if record else "interrupted", runs[-1] if runs else None)
        with self.runtime.lock:
            completed = [(key, run_id, self.runtime.runs[run_id].status if run_id in self.runtime.runs
                          else "run_expired")
                         for key, run_id in pending if run_id not in self.runtime.runs
                         or self.runtime.runs[run_id].finished_at is not None]
        for key, run_id, outcome in completed:
            self._outcome(key, outcome, run_id)
            self._alert_finished(key, run_id, outcome)

    def recover(self):
        """Run once before the scheduler starts; no missed/uncertain dispatch is replayed."""
        with self.lock:
            live = set(self.running().values())
            for record in self.batches.running():
                if record["id"] in live:
                    continue
                if record.get("graph") == 2:
                    graph_dispatch.recover(self, record)
                    continue
                # Mobster quit while it ran: it ends here. A step claimed before the quit gets its task's id when the
                # task started; nothing is run again.
                for entry in record.get("log", []):
                    if entry.get("state") == "claimed" and entry.get("key"):
                        request = self.journal.lookup(entry["key"])
                        entry.update(state="interrupted", **({"runId": request[0]} if request else {}))
                for item in record.get("rounds", []):
                    if item.get("status") == "running":
                        item["status"] = "interrupted"
                record.update(status="interrupted", finishedAt=self.now(),
                              reason="Mobster quit while this workflow ran. It wasn't run again.")
                self.batches.save(record)
            with self.journal.lock:
                claims = self.journal.connection.execute(
                    "SELECT key FROM workflow_dispatches WHERE outcome='claimed'").fetchall()
            for (key,) in claims:
                request = self.journal.lookup(key)
                self._outcome(key, "started" if request else "interrupted", request[0] if request else None)
            self._reconcile()
            for workflow in self._records():
                if workflow["enabled"] and workflow["nextAt"] <= self.now():
                    if schedule_rule(workflow, "missed") == "run_once":
                        continue        # the first tick runs it once (tick)
                    key = f"workflow:{workflow['id']}:scheduled:{workflow['nextAt']}"
                    self._claim(workflow, key, workflow["nextAt"], "skipped_missed")

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

    def _start_queued(self):
        """The one run that waited for the workflow's last run starts once that run ended"""
        running = set(self.running())
        for workflow in self._records():
            queued = workflow.get("queuedDispatch")
            if not queued or workflow["id"] in running:
                continue
            event = workflow.get("queuedEvent") if isinstance(workflow.get("queuedEvent"), dict) else {}
            with self.journal.lock, self.journal.transaction() as connection:
                workflow.pop("queuedDispatch", None)
                workflow.pop("queuedEvent", None)
                self._save(connection, workflow)
            if not workflow.get("enabled"):
                self._outcome(queued, "skipped_busy")
                continue
            try:
                self._dispatch(Dispatch(queued, workflow, trigger=event.get("trigger"), event=event.get("event"),
                                        started_by=event.get("startedBy")))
            except (APIError, ValueError):
                continue

    def tick(self):
        with self.lock:
            if getattr(self.runtime, "closing", False):
                return
            self._alert_waiting()
            self._reconcile()
            self._start_queued()
            records = self._records()
            self.triggers.tick(records)
            for workflow in records:
                now = self.now()  # Earlier readiness checks may have blocked or the wall clock may have changed.
                slot = workflow["nextAt"]
                if not workflow["enabled"] or slot is None or slot > now:
                    continue
                key = f"workflow:{workflow['id']}:scheduled:{slot}"
                # A slot the Mac slept through is skipped, or run once when the schedule says so (missed: run_once):
                # the claim moves nextAt past now, so many missed slots still make one run.
                late = now - slot >= LATE_TOLERANCE_MS
                outcome = "skipped_missed" if late and schedule_rule(workflow, "missed") != "run_once" else "claimed"
                with self.journal.lock:
                    attempts = self.journal.connection.execute("""SELECT workflow_id,claimed_at FROM workflow_dispatches
                        WHERE scheduled_at IS NOT NULL AND claimed_at>? AND outcome NOT LIKE 'skipped_%'""",
                                                               (now - DAY_MS,)).fetchall()
                if len(attempts) >= self.daily_limit:
                    outcome = "skipped_daily_limit"
                elif any(identifier == workflow["id"] and now - claimed < MIN_INTERVAL_MS
                         for identifier, claimed in attempts):
                    outcome = "skipped_interval_limit"
                if self._claim(workflow, key, slot, outcome) or outcome != "claimed":
                    continue
                try:
                    self._dispatch(Dispatch(key, workflow))
                except (APIError, ValueError):
                    # A skipped/failed occurrence is durable and never retried in a burst.
                    continue

    def _batch_view(self, record):
        if record is None:
            return None
        with self._batches_lock:
            going = next((item for item in self._batches.values() if item.record["id"] == record["id"]), None)
        return going.public() if going is not None else self.batches.public(record)

    def _dispatch_batch(self, dispatch):
        workflow, key = dispatch.workflow, dispatch.key
        with self._batches_lock:
            busy = workflow["id"] in self._batches
        if busy:
            if ":scheduled:" in key and schedule_rule(workflow, "overlap") == "queue_one":
                with self.journal.lock:
                    current = self._get(workflow["id"])
                if not current.get("queuedDispatch"):
                    # One scheduled run waits for the one going (overlap: queue_one); tick starts it after.
                    with self.journal.lock, self.journal.transaction() as connection:
                        current["queuedDispatch"] = key
                        self._save(connection, current)
                    self._outcome(key, "queued")
                    raise APIError("This workflow is still running; this run starts when it ends.", 409, "queued")
            self._outcome(key, "skipped_busy")
            raise APIError("This workflow is already running. Stop it, or wait for it to finish.", 409, "run_active")
        service, admission = self._threads(), None
        graph = workflow["graph"] if dispatch.test else self.live(workflow)
        try:
            debug, entry, payload, values, source = graph_rerun.plan(self, workflow, graph, dispatch.rerun,
                                                                     test=dispatch.test)
            if entry is None:
                entry = graph_dispatch.entry(graph, dispatch)
            if values is None:
                values = graph_dispatch.check_values(
                    graph, self._account_values(workflow, dispatch.values, typed=True), dispatch.rows)
            else:
                graph_dispatch.check_values(graph, values, payload.get("rows") if isinstance(payload, dict) else None)
            admission = self._admit(service, dispatch)
            if payload is None:
                payload = graph_dispatch.payload(entry, dispatch, values)
            record = batch.new_record(workflow, key, admission.thread_id, self.now())
            record.update(graph=2, trigger=entry, test=dispatch.test, **({"from": source} if source else {}),
                          **({"prove": True} if dispatch.prove else {}),
                          revision=workflow.get("revision") if dispatch.test else
                          (workflow.get("published") or {}).get("revision", workflow.get("revision")))
            self.batches.add(record)
        except (APIError, ValueError, JournalError):
            if admission and admission.made:
                self._drop(service, admission.thread_id)
            self._outcome(key, "dispatch_failed")
            raise
        base = {**({"engine": admission.engine} if admission.engine else {}), "origin": admission.origin}
        default = ({"device": admission.device} if admission.device
                   else {"default_device": "primary"} if self._devices_here() else {})

        def create(app_id, goal, run_key, *, output_schema=None, output_format="auto", cap_usd=None, device=None,
                   extras=None, engine=None, workflow_id=workflow["id"]):
            merged = {**admission.extras, **(extras or {})}
            # A step's own fallback engine (graph/nodes.phone: the ready coding agent when Mobster's agent has no key)
            # never overrides the engine the workflow was started with.
            return self.runtime.create(app_id, goal, "live", run_key, output_schema=output_schema,
                                       output_format=output_format,
                                       **{**({"engine": engine} if engine else {}), **base},
                                       **({"device": device} if device else default),
                                       **({"extras": merged} if merged else {}),
                                       **({"cap_usd": cap_usd} if cap_usd is not None else {}),
                                       _workflow_id=workflow_id)
        going = GraphRun(self, record, workflow, graph, entry=entry, payload=payload, values=values, create=create,
                         origin=admission.origin, clock=self.clock, debug=debug)
        going.admitted_device = admission.device
        going.made_thread = admission.thread_id if admission.made else None
        try:
            # The admission lock owns these writes through cleanup; the worker starts only after durable linkage.
            self._record_started(key, f"batch:{record['id']}", admission.thread_id, admission.home)
            with self._batches_lock:
                self._batches[workflow["id"]] = going
            going.start()
        except Exception:
            with self._batches_lock:
                if self._batches.get(workflow["id"]) is going:
                    del self._batches[workflow["id"]]
            record.update(status="interrupted", finishedAt=self.now(), reason="This run never started.")
            try:
                self.batches.save(record)
            except JournalError:
                pass  # recover() ends a record left running at the next launch
            if admission.made:
                self._drop(service, admission.thread_id)
            raise
        going.first.wait(graph_run.FIRST_WAIT_S)
        return going.first_run, False

    def batch_finished(self, going, status, reason):
        """Commit the run's end and the workflow's result together, then publish while close() still owns the worker."""
        key = going.record["key"]
        last = going.last or going.first_run
        with self._batches_lock:
            if self._batches.get(going.workflow["id"]) is going:
                del self._batches[going.workflow["id"]]
            self._ending.add(going)  # still writing to the journal: close() waits for it
        try:
            if going.first_run is None and getattr(going, "made_thread", None):
                self._drop(self._threads(), going.made_thread)
            try:
                with going.cond, self.journal.transaction() as connection:
                    self.batches.write(connection, going.record)
                    # Deleting a workflow removes its dispatch; the run still records how its worker ended.
                    if connection.execute("SELECT 1 FROM workflow_dispatches WHERE key=?", (key,)).fetchone():
                        self._save_outcome(connection, key, status, last.id if last is not None else None)
            except JournalError:
                batch.log.exception("batch %s: saving its end failed", going.record["id"])
                return
            self._alert_finished(key, last.id if last is not None else None, status)
            if going.record.get("prove") and status == "completed":
                try:
                    self._proved(going)
                except Exception:  # noqa: BLE001 -- the run's outcome stands; the draft just isn't marked proven
                    graph_run.log.exception("pinning a prove's outputs failed")
            going.publish_end(status, reason)
        finally:
            with self._batches_lock:
                self._ending.discard(going)

    def stop_batch(self, identifier):
        """Stop a workflow that is running its steps"""
        with self._batches_lock:
            going = self._batches.get(identifier)
        if going is None:
            raise APIError("This workflow isn't running.", 409, "not_running")
        going.stop_all()
        return going.public()

    def batch_runs(self, identifier):
        """The workflow's recent batches, newest first, each with its rounds and its log."""
        with self.journal.lock:
            self._get(identifier)
        return [self._batch_view(record) for record in self.batches.recent(identifier)]

    def follow(self, identifier, started, seconds):
        """A run ``start`` began, once it ends or as it goes when ``seconds`` run out"""
        from ..threads.mcp import graph_state
        batch_id = (started.get("batch") or {}).get("id")
        if batch_id:
            going = self._going(identifier, batch_id)
            if going is not None:
                going.thread.join(seconds)
            record = next((r for r in self.batch_runs(identifier) if r["id"] == batch_id), None) or started["batch"]
            state = graph_state(record)
            return {"status": state["status"], "reason": state["reason"], "result": state["result"], "answer": None,
                    "runId": (started.get("run") or {}).get("id"), "batchId": batch_id, "steps": state["steps"]}
        run_id = (started.get("run") or {}).get("id")
        with self.runtime.lock:
            run = self.runtime.runs.get(run_id) if run_id else None
        if run is None:
            return {"status": "running", "reason": None, "result": None, "answer": None, "runId": run_id,
                    "batchId": None, "steps": []}
        deadline = time.monotonic() + seconds
        with run.condition:
            while getattr(run, "finished_at", None) is None and time.monotonic() < deadline:
                run.condition.wait(min(0.5, max(0.0, deadline - time.monotonic())))
        summary = run.summary if isinstance(run.summary, dict) else {}
        return {"status": run.status, "reason": summary.get("reason"), "result": summary.get("data"),
                "answer": batch.answer_text(summary) or None, "runId": run_id, "batchId": None, "steps": []}

    def estimate(self, identifier, draft=False, _depth=0):
        """GET /api/workflows/{id}/estimate[?draft=1]"""
        from ..graph import estimate as graph_estimate
        with self.journal.lock:
            workflow = self._get(identifier)
        return graph_estimate.of_workflow(self, workflow, workflow["graph"] if draft else self.live(workflow), _depth)

    def node_history(self, identifier, batch_id, instance):
        """GET /api/workflows/{id}/runs/{batchId}/nodes/{instance}"""
        with self.journal.lock:
            self._get(identifier)
        record = self.batches.get(batch_id)
        found = self.batches.node(batch_id, instance) if record is not None and record["workflowId"] == identifier \
            else None
        if found is None:
            raise APIError("That step isn't kept any more.", 404, "not_found")
        return found

    def node_frames(self, identifier, batch_id, instance):
        """GET /api/workflows/{id}/runs/{batchId}/nodes/{instance}/frames"""
        run_id = self.node_history(identifier, batch_id, instance).get("runId")
        return {"runId": run_id, "frames": self._frames(run_id)}

    def _frames(self, run_id):
        """[{at, frameId, event}] of the screens task ``run_id`` kept, in order; none when it isn't kept."""
        with self.runtime.lock:
            run = self.runtime.runs.get(run_id) if run_id else None
        if run is None:
            return []
        with run.condition:
            events = list(getattr(run, "events", None) or ())
        return [{"at": e.get("timestamp"), "frameId": e["frameId"], "event": e.get("event")}
                for e in events if isinstance(e, dict) and isinstance(e.get("frameId"), str)]

    def timeline(self, identifier, batch_id):
        """GET /api/workflows/{id}/runs/{batchId}/timeline: a kept run across its steps in time, for scrubbing it."""
        with self.journal.lock:
            self._get(identifier)
        record = self.batches.get(batch_id)
        if record is None or record["workflowId"] != identifier:
            raise APIError("That run isn't kept any more.", 404, "not_found")
        rows = [row for row in self.batches.nodes(batch_id) if not str(row.get("type")).startswith("trigger.")]
        keys = ("instance", "node", "type", "state", "status", "deviceId", "deviceName", "runId", "readyAt",
                "startedAt", "finishedAt", "queuedReason", "reused", "pinned")
        steps = sorted(({key: row[key] for key in keys if key in row} for row in rows),
                       key=lambda step: (step.get("startedAt") or step.get("readyAt") or 0))
        frames = sorted(({**frame, "instance": row["instance"], "runId": row["runId"]}
                         for row in rows if row.get("runId") and not row.get("reused")
                         for frame in self._frames(row["runId"])), key=lambda frame: frame.get("at") or 0)
        return {"batchId": batch_id, "status": record.get("status"), "startedAt": record.get("createdAt"),
                "finishedAt": record.get("finishedAt"), "steps": steps, "frames": frames}

    def _going(self, identifier, batch_id):
        """The run ``batch_id`` of workflow ``identifier`` while it goes"""
        with self._batches_lock:
            going = self._batches.get(identifier)
        pending = [going] if going is not None else []
        while pending:
            run = pending.pop()
            if run.record["id"] == batch_id:
                return run
            pending.extend(run.children.values())
        return None

    def answer_approval(self, identifier, batch_id, instance, body):
        """POST /api/workflows/{id}/runs/{batchId}/approvals/{instance} {decision, note?, value?}"""
        if not isinstance(body, dict) or set(body) - {"decision", "note", "value"}:
            raise ValueError("An answer has a decision, and may have a note and an edited value")
        if body.get("decision") not in ("approve", "approve_all", "decline"):
            raise ValueError("decision is approve, approve_all or decline")
        note = body.get("note")
        if note is not None and (not isinstance(note, str) or len(note) > 300):
            raise ValueError("A note is at most 300 characters")
        going = self._going(identifier, batch_id)
        if going is None:
            raise APIError("That run isn't going any more.", 409, "not_waiting")
        return going.answer(instance, body["decision"], value=body.get("value"), has_value="value" in body,
                            note=" ".join(note.split()) if note else None, by="you")

    def pending_approvals(self):
        """GET /api/workflows/approvals: every approval step waiting for you, oldest first."""
        with self._batches_lock:
            pending = list(self._batches.values())
        out = []
        while pending:
            run = pending.pop()
            pending.extend(run.children.values())
            with run.cond:
                for instance, (request, _answered, _box) in run.approvals.items():
                    out.append({"workflowId": run.workflow["id"], "workflowName": run.workflow["name"],
                                "batchId": run.record["id"], "instance": instance, **request})
        return sorted(out, key=lambda item: item["expiresAt"])

    def resume_all(self, identifier, batch_ids, request_key, origin=None):
        """POST /api/workflows/{id}/resume {batchIds}: resume each of those runs in order (graph/rerun.resume_all)."""
        return graph_rerun.resume_all(self, identifier, batch_ids, request_key, origin)

    def resume_preview(self, identifier, batch_id):
        """GET /api/workflows/{id}/runs/{batchId}/resume: what resuming that run would reuse, run again and leave."""
        with self.journal.lock:
            workflow = self._get(identifier)
        return graph_rerun.preview(self, workflow, self.live(workflow), batch_id)

    def recorded_scope(self, identifier, batch_id, node):
        """The values an expression at ``node`` read in a past run (POST /api/workflows/evaluate)."""
        with self.journal.lock:
            workflow = self._get(identifier)
        return graph_rerun.recorded_scope(self, workflow, batch_id, node)

    def running(self):
        """{workflow id: batch id} of the batches going now."""
        with self._batches_lock:
            return {identifier: going.record["id"] for identifier, going in self._batches.items()}

    def close(self, timeout=5.0):
        """Mobster is quitting: every batch stops before its next step; False while one still owns journal writes."""
        self._closing.set()                  # a bulk resume starts nothing more
        links = getattr(self, "_approval_links", None)
        if links is not None:
            links.close()                    # asking by link ends with the run that asked
        deadline = time.monotonic() + timeout
        with self._batches_lock:
            resuming = list(self._resuming.values())
        for thread in resuming:              # each sees the flag within a second; one starting a run registers it first
            if thread is not threading.current_thread():
                thread.join(max(0.0, min(2.0, deadline - time.monotonic())))
        if not self.lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            return False  # Admission still owns journal writes, even before a run exists.
        try:
            with self._batches_lock:
                going = list(self._batches.values())
                ending = list(self._ending)
            for item in going:
                item.stop.set()
        finally:
            self.lock.release()
        going += ending
        # A batch ends on its own thread and records its outcome in the journal. Wait for those threads (bounded) so the
        # journal isn't closed under a write: a write to a closed SQLite connection from another thread can
        # crash the process.
        ended = not any(thread.is_alive() for thread in resuming)
        for item in going:
            thread = getattr(item, "thread", None)
            if thread is None or thread is threading.current_thread():
                continue
            if thread.ident is None:
                ended = False  # registered but not started yet: it will write after this returns
                continue
            thread.join(max(0.0, deadline - time.monotonic()))
            ended = ended and not thread.is_alive()
        return ended  # False: a batch is still running, so the journal must stay open


def schedule_rule(workflow, name):
    """The published schedule trigger's ``missed`` or ``overlap`` rule ("skip" when it says none)."""
    graph = WorkflowRuns.live(workflow) if graph_schema.check_graph(workflow.get("graph")) else {}
    trigger = next((n for n in graph.get("nodes") or [] if n.get("type") == "trigger.schedule"), None)
    return ((trigger or {}).get("config") or {}).get(name) or "skip"


# The origins of a person starting a run themselves: Run on a draft is a test run for them.
DRAFT_TESTERS = frozenset({"app", "tui", "cli"})


def _unsupported(workflow, draft=False):
    """The refusal for a workflow that can't run"""
    if (not draft and graph_schema.check_graph(workflow.get("graph"))
            and not isinstance(workflow.get("published"), dict)):
        return APIError("This workflow is a draft. Publish it, then run it.", 409, "not_published")
    graph = workflow["graph"] if draft else WorkflowRuns.live(workflow)
    why = graph_run.unsupported(graph) if graph_schema.check_graph(graph) else None
    return APIError(why, 409, "graph_unsupported") if why else None
