"""Durable saved workflows and at-most-once dispatch, sharing the canonical run journal."""

import json
import re
import threading
import time
import uuid

from .. import engines, harness_api
from ..api_errors import APIError
from ..catalog import APPS, pack_apps
from ..graph import catalog as graph_catalog
from ..graph import chain as graph_chain
from ..graph import draft as graph_draft
from ..graph import edit as graph_edit
from ..graph import nodes as graph_nodes
from ..graph import schema as graph_schema
from ..journal import encode
from ..output_contract import validate_output
from ..workflow_tools import TOOL_WAIT_DEFAULT, TOOL_WAIT_MAX, _as_tool, tools
from . import brief, starters
from . import records as batch
from .cron import DAILY_LIMIT, local_timezone, next_occurrences, timezone, validate_cron
from .history import WorkflowHistory
from .runs import WorkflowRuns
from .triggers import WorkflowTriggers

brief.register()  # the brief step (brief.py): one result from a workflow's tasks
graph_nodes.register()  # the input a graph's phone step reads, as data (graph/nodes.py)


MAX_WORKFLOWS = 200
# The start app a Smart task names for the Home Screen (server.ANY_APP); a saved task stores null.
ANY_APP_ID = "any"
CREATE_FIELDS = {"name", "appId", "appName", "goal", "outputSchema", "outputFormat", "engine", "cron", "timezone",
                 "enabled",
                 "device", "sameConversation", "homeThreadId", "account", "sendTo", "steps", "repeat", "limits",
                 "produces", "graph", "publish", "asTool"}
UPDATE_FIELDS = {"name", "goal", "appId", "appName", "cron", "timezone", "enabled", "revision", "engine", "device",
                 "sameConversation", "homeThreadId", "account", "sendTo", "steps", "repeat", "limits", "produces",
                 "graph",
                 "publish", "asTool"}
# What a body can't send with a graph: the graph says it (its steps, its words, its app, its schedule trigger).
NOT_WITH_GRAPH = {"steps", "repeat", "limits", "goal", "appId", "cron", "timezone"}
# The record's version: 2 is a conversation starter (blanks, sameConversation, homeThreadId); 3 adds its step cards
# (steps, repeat, limits); 4 makes it a graph (graph). Older records are upgraded when Mobster opens them (_migrate):
# version 1 text keeps its meaning, braces and all, a starter becomes one task card with the same words and app, and
# cards become a linear graph.
VERSION = 4
THREAD_ID = re.compile(r"[a-f0-9]{12}")


def public_file(file):
    """A workflow's file as the API shows it"""
    if not isinstance(file, dict):
        return None
    return {key: file.get(key) for key in ("projectId", "projectName", "path", "state", "problem", "revision",
                                           "syncedAt")}


def validate_produces(value):
    """The result blocks a workflow says it hands back (result_blocks.TYPES, docs/results.mdx)"""
    from ..result_blocks import TYPES
    if value is None:
        return None
    if (not isinstance(value, list) or not 1 <= len(value) <= 3 or len(set(value)) != len(value)
            or any(kind not in TYPES for kind in value)):
        raise ValueError("produces is 1 to 3 result block kinds: " + ", ".join(TYPES))
    return list(value)


def validate_goal(value):
    """A saved task's instruction: 1–4000 characters, stored trimmed."""
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 4000:
        raise ValueError("Supply a task of 1–4000 characters")
    return starters.check_steps(value.strip())


def validate_engine(value):
    """A saved engine is "smart" or None (Mobster's agent either way). Quick mode's retired "fast" reads as "smart"."""
    value = engines.read_engine(value)
    if value is None or value == engines.SMART:
        return value
    raise ValueError('engine must be "smart"')


class Workflows(WorkflowRuns):
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
        # Batches going now, by workflow id (one at a time per workflow), under their own lock: a batch ends on its
        # own thread and must never wait for a dispatch that holds ``lock``.
        self._batches = {}
        self._batches_lock = threading.Lock()
        self._ending = set()  # batches past their last step that are still recording how they ended
        self._resuming = {}   # workflow id -> the thread resuming a list of its runs (resume_all)
        self._closing = threading.Event()
        self.deleted = []     # callables told each deleted record (projects/sync.py)
        with self.journal.transaction() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS workflows (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            connection.execute("""CREATE TABLE IF NOT EXISTS workflow_dispatches (
                key TEXT PRIMARY KEY, workflow_id TEXT NOT NULL REFERENCES workflows(id),
                scheduled_at INTEGER, claimed_at INTEGER NOT NULL,
                outcome TEXT NOT NULL, run_id TEXT)
            """)
            connection.execute("CREATE INDEX IF NOT EXISTS workflow_dispatch_times ON workflow_dispatches(claimed_at)")
            self._migrate(connection)
        self.batches = batch.BatchStore(self.journal)
        self.history = WorkflowHistory(self)
        self.triggers = WorkflowTriggers(self)
        self._approval_links = None
        self._links_lock = threading.Lock()

    @property
    def approval_links(self):
        """The approval links of this Mac (teams/approval_link.py), made the first time an approval step asks."""
        with self._links_lock:
            if self._approval_links is None:
                from .. import plan_gate
                try:
                    from ..teams import approval_link
                except ModuleNotFoundError as error:
                    if error.name != "mobile_agent.teams":
                        raise
                    raise APIError("Approval links need the Mobster app's team extension. Approve this step locally.",
                                   503, "extension_unavailable") from None

                def person():
                    stored = plan_gate.stored_account(plan_gate.app_account_file()) or {}
                    user = stored.get("user") if isinstance(stored.get("user"), dict) else {}
                    return str(user.get("name") or user.get("email") or "Mobster")[:60]
                self._approval_links = approval_link.ApprovalLinks(self, approval_link.Service(approval_link.mac_token),
                                                                   person=person)
            return self._approval_links

    @staticmethod
    def upgrade(workflow):
        """A saved task as today's record, or None when it is one already."""
        upgraded = dict(workflow)
        if workflow.get("version", 1) < 2:
            if starters.has_braces(upgraded.get("goal")):
                upgraded["goal"] = starters.escape(upgraded["goal"])
            upgraded.setdefault("sameConversation", False)
            upgraded.setdefault("homeThreadId", None)
            upgraded.setdefault("lastThreadId", None)
        if workflow.get("version", 1) < VERSION:
            upgraded["version"] = VERSION
        if not isinstance(upgraded.get("steps"), list) and isinstance(upgraded.get("goal"), str):
            # A starter is a one-task workflow: its words are its one card, starting where it started.
            upgraded["steps"] = batch.one_task(upgraded["goal"], upgraded.get("appId"))
            upgraded.setdefault("repeat", None)
            upgraded.setdefault("limits", dict(batch.DEFAULT_LIMITS))
        if upgraded.get("engine") is not None:
            upgraded["engine"] = engines.read_engine(upgraded["engine"])
        upgraded.pop("helperModel", None)
        if not graph_schema.check_graph(upgraded.get("graph")) and isinstance(upgraded.get("steps"), list):
            upgraded["graph"] = graph_chain.from_chain(upgraded["steps"], upgraded.get("repeat"),
                                                       upgraded.get("limits"),
                                                       upgraded.get("cron"), upgraded.get("timezone"))
            upgraded["published"] = {"revision": upgraded.get("revision", 1), "graph": upgraded["graph"]}
            Workflows.derive(upgraded)
        return upgraded if upgraded != workflow else None

    @staticmethod
    def derive(workflow):
        """``workflow`` with what its published graph says written on the record"""
        graph = Workflows.live(workflow)
        schedule = next((node for node in graph["nodes"] if node["type"] == "trigger.schedule"), None)
        workflow["cron"] = schedule["config"]["cron"] if schedule else None
        if schedule:
            workflow["timezone"] = schedule["config"]["timezone"]
        cards = graph_chain.as_chain(graph)
        workflow["steps"], workflow["repeat"] = (cards[0], cards[1]) if cards else (None, None)
        workflow["limits"] = dict(graph.get("limits") or graph_schema.DEFAULT_LIMITS)
        workflow["goal"] = graph_chain.words(graph) or workflow.get("goal") or workflow["name"]
        if graph_chain.is_plain(graph):
            workflow["appId"] = cards[0][0].get("appId")
        return workflow

    def _migrate(self, connection):
        for identifier, data in connection.execute("SELECT id, data FROM workflows").fetchall():
            try:
                upgraded = self.upgrade(json.loads(data))
            except ValueError:
                continue
            if upgraded is not None:
                connection.execute("UPDATE workflows SET data=? WHERE id=?", (encode(upgraded), identifier))

    @staticmethod
    def public(workflow):
        """A workflow as the API shows it"""
        graph = workflow.get("graph")
        if not graph_schema.check_graph(graph):
            steps = workflow.get("steps") if isinstance(workflow.get("steps"), list) else batch.one_task(
                workflow.get("goal"), workflow.get("appId"))
            graph = graph_chain.from_chain(steps, workflow.get("repeat"), workflow.get("limits"), workflow.get("cron"),
                                           workflow.get("timezone"))
            workflow = {**workflow, "graph": graph}
        live = Workflows.live(workflow)
        published = workflow.get("published") if isinstance(workflow.get("published"), dict) else None
        cards = graph_chain.as_chain(live)
        blanks = [{"name": item["name"], "default": None if item.get("default") is None else str(item["default"]),
                   "type": item["type"]} for item in live.get("inputs") or []]
        declared = {starters._key(item["name"]) for item in blanks}
        blanks += [blank for blank in starters.blanks(workflow.get("goal"))
                   if starters._key(blank["name"]) not in declared]
        # A notification waiting for its run stays on this Mac's record only: no client reads what it said.
        workflow = {key: value for key, value in workflow.items() if key != "queuedEvent"}
        return {**workflow, "file": public_file(workflow.get("file")),
                "sameConversation": bool(workflow.get("sameConversation")),
                "homeThreadId": workflow.get("homeThreadId"), "lastThreadId": workflow.get("lastThreadId"),
                "blanks": blanks, "graph": graph, "steps": cards[0] if cards else None,
                "repeat": cards[1] if cards else None,
                "published": {"revision": published["revision"]} if published else None,
                "draft": published is None or published.get("graph") != graph,
                "startsItself": _starts_itself(workflow), "listensFor": _listens_for(workflow),
                "limits": dict(live.get("limits") or graph_schema.DEFAULT_LIMITS)}

    def now(self):
        return round(self.clock() * 1000)

    def _get(self, identifier):
        row = self.journal.connection.execute("SELECT data FROM workflows WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise APIError("Saved task not found", 404, "not_found")
        return json.loads(row[0])

    def _save(self, connection, workflow):
        connection.execute("UPDATE workflows SET data=? WHERE id=?", (encode(workflow), workflow["id"]))

    def _records(self):
        """Every stored record, newest first (not the public view: these are saved back)."""
        with self.journal.lock:
            return [json.loads(row[0])
                    for row in self.journal.connection.execute("SELECT data FROM workflows ORDER BY rowid DESC")]

    def list(self):
        with self.lock:
            self._reconcile()
            with self.journal.lock:
                return [self.public(json.loads(row[0]))
                        for row in self.journal.connection.execute("SELECT data FROM workflows ORDER BY rowid DESC")]

    def tools(self):
        """GET /api/workflows/tools: each workflow made a tool, as MCP, REST and Shortcuts see it (``tools``)."""
        with self.lock:
            return tools([(self.public(record), self.live(record)) for record in reversed(self._records())])

    def tool(self, slug):
        """The tool ``slug`` names (GET /v1/workflows/<slug>); 404 when no workflow is that tool any more."""
        found = next((tool for tool in self.tools() if tool["slug"] == slug), None)
        if found is None:
            raise APIError(f"No workflow is the tool {slug}. GET /api/workflows/tools lists them.", 404, "not_found")
        return found

    def run_tool(self, slug, body, request_key, origin=None, project_secrets=False):
        """POST /v1/workflows/<slug>: run the published workflow the tool names with the values its schema takes"""
        tool = self.tool(slug)
        schema = tool["inputSchema"]
        if not isinstance(body, dict) or set(body) - set(schema["properties"]):
            raise ValueError(f"{tool['workflowName']} takes " + ", ".join(schema["properties"]))
        missing = [name for name in schema["required"] if name not in body]
        if missing:
            raise ValueError(f"Give {', '.join(missing)}: {tool['workflowName']} has no default for it")
        wait = body.get("wait_seconds", TOOL_WAIT_DEFAULT)
        if type(wait) is not int or not 0 <= wait <= TOOL_WAIT_MAX:
            raise ValueError(f"wait_seconds is a whole number from 0 to {TOOL_WAIT_MAX}")
        values = {name: value for name, value in body.items() if name != "wait_seconds"}
        started = self.start(tool["workflowId"], request_key, values=values or None, origin=origin,
                             project_secrets=project_secrets)
        answer = {"workflowId": tool["workflowId"], "threadId": started.get("threadId"),
                  **self.follow(tool["workflowId"], started, wait)}
        return answer, answer["status"] not in ("running", "queued")

    def prove(self, identifier, body, request_key, origin=None):
        """POST /api/workflows/{id}/prove {revision, graph?}"""
        if not isinstance(body, dict) or set(body) - {"revision", "graph"} or type(body.get("revision")) is not int:
            raise ValueError("A prove takes the draft's revision, and its graph when you save it now")
        if "graph" in body:
            saved = self.update(identifier, {"revision": body["revision"], "graph": body["graph"]})
        else:
            saved = self.get(identifier)
            if saved["revision"] != body["revision"]:
                raise APIError("The saved task changed. Reload before editing it.", 409, "revision_conflict")
        return {"workflow": saved, **self.start(identifier, request_key, origin=origin, test=True, prove=True)}

    def _proved(self, going):
        """A prove's test run completed: pin each step's real output on the draft it ran"""
        identifier, ran = going.workflow["id"], going.record.get("revision")
        with self.lock, self.journal.lock:
            workflow = self._get(identifier)
        if workflow.get("revision") != ran:
            return
        graph = json.loads(json.dumps(workflow["graph"]))
        nodes = {node["id"]: node for node in graph["nodes"]}
        pinned, total = [], 0
        for inst in sorted(going.instances.values(), key=lambda i: i.seq):
            if "@" in inst.id or inst.state != "done" or inst.reused or inst.kind not in PROVE_PINS:
                continue
            pin = {port: value for port, value in inst.ports.items() if port != "error"}
            size = len(json.dumps(pin, ensure_ascii=False, default=str).encode())
            if not pin or size > graph_schema.MAX_PIN_BYTES or total + size > graph_schema.MAX_PINS_BYTES:
                continue
            nodes[inst.node["id"]]["pin"], total = pin, total + size
            pinned.append(inst.node["id"])
        if pinned:
            try:
                ran = self.update(identifier, {"revision": ran, "graph": graph})["revision"]
            except (APIError, ValueError):
                pinned = []                  # the outputs don't fit the pins (or it changed): proven, unpinned
        with self.lock, self.journal.transaction() as connection:
            workflow = self._get(identifier)
            if workflow.get("revision") != ran:
                return
            workflow["proven"] = {"revision": ran, "batchId": going.record["id"], "at": self.now(), "pinned": pinned}
            self._save(connection, workflow)

    def get(self, identifier):
        with self.lock, self.journal.lock:
            return self.public(self._get(identifier))

    def find(self, wanted):
        """A workflow by its id, else by its name (case and spacing ignored)."""
        if not isinstance(wanted, str) or not wanted.strip():
            raise ValueError("Name the workflow, or give its id")
        wanted = wanted.strip()
        listed = self.list()
        found = [w for w in listed if w["id"] == wanted.lower()] or [
            w for w in listed if " ".join(w["name"].split()).casefold() == " ".join(wanted.split()).casefold()]
        if not found:
            raise APIError(f"No workflow is called {wanted[:120]}", 404, "not_found")
        if len(found) > 1:
            raise APIError(f"Several workflows are called {wanted[:120]}. Use its id: "
                           + ", ".join(w["id"] for w in found), 409, "ambiguous")
        return found[0]

    def app_ids(self, wanted):
        """The ids a saved task may name: the catalog's, the apps found on the phone, and, until the phone lists its
        apps, the app packs' apps (ICP-10). A phone that lists its apps without a pack's app refuses it."""
        ids = {app["id"] for app in APPS}
        if wanted in ids:
            return ids
        apps = getattr(self.runtime, "apps", None)
        listed = apps() if callable(apps) else []
        ids |= {app["id"] for app in listed}
        if all(app.get("installed") is None for app in listed):  # no inventory: the catalog claims nothing
            ids |= {bundle for _key, bundle, _name in pack_apps()}
        return ids

    def _devices_here(self):
        """Whether this runtime runs tasks on several devices (a fleet, and Runtime.create(default_device=...))."""
        return getattr(self.runtime, "fleet", None) is not None

    def _device(self, value):
        """A saved task's device: None (the default device when it runs)"""
        if value is None:
            return None
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 128:
            raise ValueError("device must be a device's id, UDID or name")
        fleet = getattr(self.runtime, "fleet", None)
        if fleet is None:
            raise ValueError("Mobster can't run a task on a chosen device here. Save the task without a device.")
        try:
            return fleet.find(value.strip()).id
        except LookupError as error:
            raise ValueError(str(error)) from None

    def _account(self, value):
        """A saved task's account (track accounts): None, or an account this Mobster knows, saved by its id."""
        if value is None:
            return None
        from .. import harness_api
        field = harness_api.run_fields().get("account")
        if field is None:
            raise ValueError("Profiles aren't available right now. Save the task without a profile.")
        return field[0](value, self.runtime)["id"]

    def _send_to(self, value):
        """A saved task's destination (track outlets): None, or a destination this Mobster has, saved by its id."""
        if value is None:
            return None
        field = harness_api.run_fields().get("sendTo")
        if field is None:
            raise ValueError("Sending results isn't available in this Mobster. Save the workflow without sendTo.")
        return field[0](value, self.runtime)["destination"]

    def _send_extras(self, workflow, values=None):
        """The run field that sends the result to the workflow's destination; None never stops the run."""
        destination = workflow.get("sendTo")
        field = harness_api.run_fields().get("sendTo")
        if not destination or field is None:
            return None
        try:
            given = starters.clean_values(self._account_values(workflow, values))
        except ValueError:
            given = {}
        blanks = {}
        for blank in starters.blanks(workflow.get("goal")):
            value = given.get(starters._key(blank["name"])) or blank["default"]
            if value:
                blanks[blank["name"]] = value
        value = {"destination": destination, "workflow": {"id": workflow["id"], "name": workflow["name"],
                                                          "blanks": blanks}}
        try:
            return field[0](value, self.runtime)
        except ValueError:
            return None

    def _app_name(self, app_id, given=None):
        """The name shown for a saved task's app, kept so it still has one while the phone is away."""
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
        """The app and engine a saved task starts with. Without an app it starts on the Home Screen."""
        validate_engine(workflow.get("engine"))
        app_id = workflow.get("appId")
        if app_id is None:
            return
        if not isinstance(app_id, str) or app_id not in self.app_ids(app_id):
            raise ValueError(graph_catalog.app_missing(app_id))

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
        if workflow["enabled"] and next_at is None and not _listens(workflow):
            raise ValueError(
                "Configure a cron schedule, or a trigger for notifications or an app, before turning it on")
        repeat = workflow.get("repeat") or {}
        if workflow["enabled"] and repeat.get("kind") == "each" and not repeat.get("rows"):
            raise ValueError("A schedule repeats over the rows saved with the workflow. Save its rows first.")
        filled = (self._account_values(workflow) or {}) if workflow["enabled"] else {}
        if workflow["enabled"] and repeat.get("rows"):
            # A blank every saved row fills has a value at every scheduled run.
            keys = [set(starters.clean_values(row)) for row in repeat["rows"]]
            filled = {**{key: "row" for key in set.intersection(*keys)}, **filled}
        missing = [name for name in starters.unfilled(workflow.get("goal")) if starters._key(name) not in filled] \
            if workflow["enabled"] else []
        if missing:
            raise ValueError("A schedule fills every blank with its default. Give "
                             + ", ".join(missing) + " a value, like {" + missing[0] + ": ...}")
        return next_at

    def _account_values(self, workflow, values=None, *, typed=False):
        """A run's values, with the workflow's account (track accounts) filling the blanks it has a value for"""
        account = workflow.get("account")
        if not account:
            return values
        try:
            from ..accounts.context import values as account_values
            from ..accounts.store import store_for
            known = account_values(store_for(self.runtime).get(account))
        except Exception:  # noqa: BLE001 -- no accounts track, or the account is gone: the blanks stay unfilled
            return values
        if typed and values is not None and not isinstance(values, dict):
            raise ValueError("Graph values are an object of named inputs")
        out = {}
        for blank in starters.blanks(workflow.get("goal")):
            key = starters._key(blank["name"])
            value = known.get(key, known.get(key.replace(" ", "_")))
            if value:
                out[key] = value
        # Graph inputs keep their JSON types until graph.dispatch checks and coerces them.
        # Plain starter blanks still use their text-only normalization.
        out.update((values or {}) if typed else starters.clean_values(values))
        return out

    def owns_run_request(self, workflow_id, key):
        """Only a saved workflow dispatch or a journaled graph node may attach its identity to a task."""
        if not isinstance(key, str) or not key.startswith(f"workflow:{workflow_id}:"):
            return False
        with self.journal.lock:
            row = self.journal.connection.execute(
                "SELECT workflow_id FROM workflow_dispatches WHERE key=?", (key,)).fetchone()
        if row is not None:
            return row[0] == workflow_id
        prefix, marker, suffix = key.rpartition(":g")
        batch_id, colon, _ = suffix.partition(":")
        if not marker or not colon or not re.fullmatch(r"[0-9a-f]{12}", batch_id):
            return False
        record = self.batches.get(batch_id)
        return bool(record and record["workflowId"] == workflow_id and record["key"] == prefix
                    and any(node.get("key") == key for node in self.batches.nodes(batch_id)))

    def run_hook_event(self, identifier, request_key, values, body, occurred_at):
        """A claimed webhook event skips an overlapping workflow under the same lock as dispatch."""
        with self.lock:
            self._reconcile()
            with self.journal.lock:
                busy = self.journal.connection.execute(
                    "SELECT 1 FROM workflow_dispatches WHERE workflow_id=? AND outcome IN ('claimed','started') "
                    "UNION ALL SELECT 1 FROM workflow_batches WHERE workflow_id=? AND status='running' LIMIT 1",
                    (identifier, identifier)).fetchone()
            if busy:
                raise APIError("This workflow is already running. This event was skipped.", 409,
                               "workflow_overlap")
            if abs(self.now() - occurred_at) > 300_000:
                raise APIError("This event expired before it could start.", 410, "event_expired")
            return self.run(identifier, request_key, values, origin="hook", body=body)

    def _conversation_fields(self, workflow):
        """Validates sameConversation and homeThreadId in place: a home conversation is one this Mobster has."""
        if type(workflow.get("sameConversation", False)) is not bool:
            raise ValueError("sameConversation must be true or false")
        home = workflow.get("homeThreadId")
        if home is None:
            return
        if not isinstance(home, str) or not THREAD_ID.fullmatch(home):
            raise ValueError("homeThreadId is a conversation's 12-character id")
        service = self._threads()
        if service is not None and service.store.get(home) is None:
            raise ValueError("That conversation was deleted. Save the workflow without it.")

    def _threads(self):
        """The conversations service (threads.service), or None in a Mobster without conversations."""
        try:
            from ..threads.service import service_for
        except ImportError:
            return None
        try:
            return service_for(self.runtime)
        except Exception:  # noqa: BLE001 -- a runtime that isn't a Runtime (a test double)
            return None

    def _destination_exists(self):
        """callable(id) -> whether a saved destination has that id, or None in a Mobster without outlets."""
        try:
            from ..outlets.service import service_for
        except ImportError:
            return None
        service = service_for(self.runtime)
        if service is None:
            return None
        return lambda identifier: service.store.destination(identifier) is not None

    def _check_cards(self, steps, repeat, limits, workflow_id=None):
        """(steps, repeat, limits) checked (batch.validate), each task's app one this Mobster knows."""
        records = {record["id"]: record for record in self._records()}
        steps = [{**step, "appId": None} if isinstance(step, dict) and step.get("appId") == ANY_APP_ID else step
                 for step in steps] if isinstance(steps, list) else steps
        steps, repeat, limits = batch.validate(steps, repeat, limits, workflow_id=workflow_id, workflows=records,
                                               destinations=self._destination_exists(), runtime=self.runtime)
        for step in steps:
            app = step.get("appId") if step["kind"] == "task" else None
            if app is not None and app not in self.app_ids(app):
                raise ValueError(graph_catalog.app_missing(app))
        return steps, repeat, limits

    def _graph_context(self, workflow_id=None):
        """What graph validation asks this Mobster (graph/schema.Context)."""
        def connector(config):
            if "connector" not in batch.step_kinds():
                raise ValueError("Connectors aren't available in this Mobster")
            made = batch._kinds["connector"][0]({"id": "check", "kind": "connector", **config}, self.runtime)
            return made

        def cron(value, zone):
            return validate_cron(value, zone or local_timezone(), self.daily_limit)

        def zone(value):
            timezone(value)
            return value

        return graph_schema.Context(
            workflow_id=workflow_id, workflows={record["id"]: record for record in self._records()},
            app_ids=self.app_ids, device=self._device, account=self._account, destination=self._destination_exists(),
            connector=connector, packs=_packs(), cron=cron, timezone=zone, deliveries=tuple(batch.delivery_kinds()),
            plain=graph_chain.record_is_plain)

    def check_graph(self, graph, workflow_id=None):
        """``graph`` as it would be saved, or graph_schema.GraphError listing every
        problem (POST /api/workflows/validate)."""
        return graph_schema.validate(graph, self._graph_context(workflow_id))

    def _chain_graph(self, steps, repeat, limits, cron, zone, workflow_id=None, before=None):
        """The graph a body's cards make, keeping where the canvas put each node and what it called it."""
        graph = graph_chain.from_chain(steps, repeat, limits, cron, zone)
        if before is not None:
            kept = {node["id"]: node for node in before.get("nodes") or []}
            for node in graph["nodes"]:
                old = kept.get(node["id"])
                if old is not None and old["type"] == node["type"]:
                    node["position"] = old.get("position", node["position"])
                    for key in ("title", "notes"):
                        if old.get(key):
                            node[key] = old[key]
        return self.check_graph(graph, workflow_id)

    def create(self, body):
        if not isinstance(body, dict) or set(body) - CREATE_FIELDS:
            raise ValueError("Unsupported saved task field")
        name = body.get("name")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
            raise ValueError("Name the saved task using 1–120 characters")
        app_id = body.get("appId")
        if app_id == ANY_APP_ID:
            app_id = None  # a run started on the Home Screen, saved from the run view
        zone = body.get("timezone", local_timezone())
        if "graph" in body:
            if set(body) & NOT_WITH_GRAPH:
                raise ValueError("Send the graph or the steps, not both: the graph holds the steps, their words, "
                                 "the app and the schedule")
            graph = self.check_graph(body["graph"])
        else:
            if "steps" in body:
                steps, repeat, limits = self._check_cards(body["steps"], body.get("repeat"), body.get("limits"))
            else:
                goal = validate_goal(body.get("goal"))
                steps, repeat, limits = self._check_cards(batch.one_task(goal, app_id), body.get("repeat"),
                                                          body.get("limits"))
            graph = self._chain_graph(steps, repeat, limits, body.get("cron"), zone)
        engine = validate_engine(body.get("engine"))
        output_format = body.get("outputFormat", "auto")
        schema = validate_output(output_format, body.get("outputSchema"))
        now = self.now()
        # A new schedule reads in the Mac's own time unless the caller names a zone (existing ones keep theirs).
        device = self._device(body.get("device"))
        account = self._account(body.get("account"))
        send_to = self._send_to(body.get("sendTo"))
        workflow = {"id": uuid.uuid4().hex[:12], "name": name.strip(), "appId": app_id, "appName": None, "goal": None,
                    "outputSchema": schema, "outputFormat": output_format,
                    "engine": engine, "device": device, **({"account": account} if account else {}),
                    **({"sendTo": send_to} if send_to else {}), "cron": None, "timezone": zone,
                    "enabled": body.get("enabled", False), "revision": 1, "createdAt": now, "updatedAt": now,
                    "nextAt": None, "lastRunId": None, "lastOutcome": None, "version": VERSION,
                    "sameConversation": body.get("sameConversation", False), "homeThreadId": body.get("homeThreadId"),
                    "lastThreadId": None, "graph": graph,
                    **({"produces": validate_produces(body["produces"])} if body.get("produces") is not None else {})}
        if body.get("publish", True) is not False:
            workflow["published"] = {"revision": 1, "graph": graph}
        if body.get("asTool") is not None:
            workflow["asTool"] = _as_tool(body["asTool"])
        self.derive(workflow)
        self._check_task({"appId": workflow["appId"], "engine": engine})
        workflow["appName"] = self._app_name(workflow["appId"], body.get("appName"))
        self._conversation_fields(workflow)
        next_at = self._schedule(workflow)
        workflow["nextAt"] = next_at if workflow["enabled"] else None
        with self.lock, self.journal.lock:
            if self.journal.connection.execute("SELECT count(*) FROM workflows").fetchone()[0] >= MAX_WORKFLOWS:
                raise APIError("Saved task limit reached", 409, "workflow_limit")
            with self.journal.transaction() as connection:
                connection.execute("INSERT INTO workflows VALUES (?,?)", (workflow["id"], encode(workflow)))
                self.history.keep(connection, workflow)
            self.triggers.changed()
        return self.public(workflow)

    def _graph_change(self, workflow, body, identifier):
        """(draft, published): the graphs an update leaves, ``published`` None when it doesn't publish."""
        publish = body.get("publish")
        if publish is not None and type(publish) is not bool:
            raise ValueError("publish is true or false")
        if "graph" in body:
            if set(body) & NOT_WITH_GRAPH:
                raise ValueError("Send the graph or the steps, not both: the graph holds the steps, their words, "
                                 "the app and the schedule")
            graph = self.check_graph(body["graph"], identifier)
            return graph, graph if publish else None
        if not set(body) & NOT_WITH_GRAPH:
            return workflow["graph"], workflow["graph"] if publish else None
        live = self.live(workflow)
        cron = body.get("cron", workflow.get("cron"))
        zone = body.get("timezone", workflow.get("timezone"))
        cards = graph_chain.as_chain(live)
        if cards is None:
            if set(body) & {"steps", "repeat", "goal", "appId"}:
                raise ValueError("This workflow is a graph with more than steps in a row. Change it in the canvas, "
                                 "or send its graph.")

            def changed(graph):
                graph = graph_edit.with_schedule(graph, cron, zone)
                return self.check_graph({**graph, "limits": body["limits"]} if "limits" in body else graph, identifier)
            graph = changed(live)
            draft = graph if workflow["graph"] == live else changed(workflow["graph"])
            return draft, None if publish is False else graph
        steps, repeat, limits = cards[0], cards[1], cards[2]
        if "steps" in body:
            steps, repeat, limits = self._check_cards(body["steps"], body.get("repeat", repeat),
                                                      body.get("limits", limits), identifier)
        elif "goal" in body or "appId" in body or "repeat" in body or "limits" in body:
            if "goal" in body or "appId" in body:
                if not batch.is_plain(steps):
                    raise ValueError("This workflow has several steps. Change them in its steps.")
                text = validate_goal(body["goal"]) if "goal" in body else steps[0]["text"]
                app = body["appId"] if "appId" in body else steps[0].get("appId")
                steps = [{**steps[0], "text": text, "appId": None if app == ANY_APP_ID else app}]
            steps, repeat, limits = self._check_cards(steps, body.get("repeat", repeat), body.get("limits", limits),
                                                      identifier)
        graph = self._chain_graph(steps, repeat, limits, cron, zone, identifier, before=workflow["graph"])
        return graph, None if publish is False else graph

    def update(self, identifier, body):
        if (not isinstance(body, dict) or set(body) - UPDATE_FIELDS
                or type(body.get("revision")) is not int):
            raise ValueError("Supply the current revision and supported schedule fields")
        with self.lock, self.journal.lock:
            workflow = self._get(identifier)
            if body["revision"] != workflow["revision"]:
                raise APIError("The saved task changed. Reload before editing it.", 409, "revision_conflict")
            if not graph_schema.check_graph(workflow.get("graph")):
                workflow = {**workflow, **(self.upgrade(workflow) or {})}
            updated = {**workflow, **{key: value for key, value in body.items()
                                      if key not in NOT_WITH_GRAPH | {"graph", "publish"}}}
            if "produces" in body:
                updated["produces"] = validate_produces(body["produces"])
                if updated["produces"] is None:
                    updated.pop("produces")
            if "asTool" in body:
                updated["asTool"] = _as_tool(body["asTool"])
            updated["graph"], published = self._graph_change(workflow, body, identifier)
            if published is not None:
                updated["published"] = {"revision": workflow["revision"] + 1, "graph": published}
            if "appId" in body and not graph_chain.is_plain(updated["graph"]):
                updated["appId"] = None if body["appId"] == ANY_APP_ID else body["appId"]
            if "timezone" in body:
                updated["timezone"] = body["timezone"]
            self.derive(updated)
            if "device" in body:
                updated["device"] = self._device(body["device"])
            if "account" in body:
                updated["account"] = self._account(body["account"])
                if updated["account"] is None:
                    updated.pop("account")
            if "sendTo" in body:
                updated["sendTo"] = self._send_to(body["sendTo"])
                if updated["sendTo"] is None:
                    updated.pop("sendTo")
            if "engine" in body:
                updated["engine"] = validate_engine(body["engine"])
            app_changed = updated.get("appId") != workflow.get("appId")
            if "engine" in body or "appId" in body or app_changed:
                self._check_task(updated)
            if "appId" in body or "appName" in body or app_changed:
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
            if "sameConversation" in body or "homeThreadId" in body:
                self._conversation_fields(updated)
            return self._commit(workflow, updated)

    def _commit(self, workflow, updated):
        """Save an edit (or a rollback) as the next revision, and keep it as a version. Under self.lock."""
        next_at = self._schedule(updated)
        updated.update(revision=workflow["revision"] + 1, updatedAt=self.now())
        schedule_changed = any(updated.get(key) != workflow.get(key) for key in ("cron", "timezone", "enabled"))
        if schedule_changed:
            updated["nextAt"] = next_at if updated["enabled"] else None
        with self.journal.transaction() as connection:
            self._save(connection, updated)
            self.history.keep(connection, updated)
        self.triggers.changed()
        return self.public(updated)

    def file_records(self):
        """Every stored record with a ``file``; only rows that mention one are decoded, as the sync reads it often."""
        with self.lock, self.journal.lock:
            rows = self.journal.connection.execute(
                """SELECT data FROM workflows WHERE instr(data, '"file":{') > 0""").fetchall()
        return [record for record in (json.loads(row[0]) for row in rows) if isinstance(record.get("file"), dict)]

    def set_file(self, identifier, file):
        """Keep where the workflow's file is and how its sync stands, or forget it (None)."""
        with self.lock, self.journal.lock:
            try:
                workflow = self._get(identifier)
            except APIError:
                return None
            if file is None:
                workflow.pop("file", None)
            else:
                workflow["file"] = file
            with self.journal.transaction() as connection:
                self._save(connection, workflow)
        return self.public(workflow)

    def delete(self, identifier):
        """Removes a saved task and its dispatch records, under self.lock so never between a claim and its outcome."""
        with self.lock, self.journal.lock:
            gone = self._get(identifier)
            with self._batches_lock:
                going = self._batches.get(identifier)
            if going is not None:
                going.stop.set()  # its task stops at the next safe point; nothing new starts
            with self.journal.transaction() as connection:
                connection.execute("DELETE FROM workflow_dispatches WHERE workflow_id=?", (identifier,))
                self.history.forget(connection, identifier)
                self.triggers.forget(connection, identifier)
                connection.execute("DELETE FROM workflows WHERE id=?", (identifier,))
            self.batches.forget(identifier)
            self.triggers.changed()
        for listener in list(self.deleted):
            try:
                listener(gone)
            except Exception:  # noqa: BLE001 -- the workflow is deleted whatever a listener does
                harness_api.log.exception("a workflow delete listener failed")
        return identifier

    def _answer(self, run_id):
        """A kept run's answer, or None."""
        with self.runtime.lock:
            run = self.runtime.runs.get(run_id) if run_id else None
        summary = getattr(run, "summary", None)
        answer = summary.get("answer") if isinstance(summary, dict) else None
        return answer if isinstance(answer, str) and answer.strip() else None

    def draft(self, body, client_factory=None):
        """{"draft": {...}, "drafter"} for POST /api/workflows/draft {threadId?, runId?}"""
        if isinstance(body, dict) and "words" in body:
            # Words to a graph (graph/draft.py): the drafter proposes, Mobster checks, nothing is saved.
            if set(body) - {"words", "graph"}:
                raise ValueError("A draft from words takes words, and the graph it changes")
            current = body.get("graph")
            if current is not None and not graph_schema.check_graph(current):
                raise ValueError("graph is a workflow graph of version 2")
            client = (client_factory or starters.draft_client)()
            return graph_draft.draft(body["words"], self._graph_context(), client=client, current=current,
                                     zone=local_timezone())
        if (not isinstance(body, dict) or set(body) - {"threadId", "runId"}
                or not (body.get("threadId") or body.get("runId"))):
            raise ValueError("Give the words of a workflow, the threadId of a conversation, or the runId of a task")
        thread_id, run_id = body.get("threadId"), body.get("runId")
        for name, value in (("threadId", thread_id), ("runId", run_id)):
            if value is not None and (not isinstance(value, str) or not THREAD_ID.fullmatch(value)):
                raise ValueError(f"{name} is a 12-character id")
        turns, run_ids = [], []
        service = self._threads()
        if thread_id and service is not None:
            thread = service.store.get(thread_id)
            if thread is None:
                raise APIError("That conversation was deleted.", 404, "thread_not_found")
            for item in service.store.items(thread_id):
                if item.get("kind") == "user" and item.get("routed") in ("new_run", "steer", "answer", "unread"):
                    turns.append(("You", item.get("text")))
                elif item.get("kind") == "run":
                    run_ids.append(item.get("runId"))
                    answer = (item.get("result") or {}).get("answer") or self._answer(item.get("runId"))
                    if answer:
                        turns.append(("Mobster", answer))
        else:
            thread_id = None
            with self.runtime.lock:
                run = self.runtime.runs.get(run_id) if run_id else None
            if run is None:
                raise APIError("That task is no longer kept.", 404, "not_found")
            run_ids.append(run.id)
            turns.append(("You", run.goal))
            answer = self._answer(run.id)
            if answer:
                turns.append(("Mobster", answer))
        made, drafter = starters.draft(turns, client_factory or starters.draft_client)
        if made is None:
            raise APIError("There's nothing in that conversation to save yet.", 409, "nothing_to_save")
        with self.runtime.lock:
            runs = [self.runtime.runs[i] for i in run_ids if i in self.runtime.runs]
        newest = runs[-1] if runs else None
        apps = {(getattr(r, "app", None) or {}).get("id") for r in runs}
        engine = engines.read_engine(getattr(newest, "engine", None)) if newest is not None else None
        app_id = apps.pop() if len(apps) == 1 else None
        if app_id == ANY_APP_ID or app_id is None:
            app_id, engine = None, "smart"
        out = {"name": made["name"], "goal": made["steps"], "appId": app_id, "engine": engine,
               "homeThreadId": thread_id, "blanks": starters.blanks(made["steps"]),
               "graph": graph_chain.from_chain(batch.one_task(made["steps"], app_id))}
        if app_id:
            out["appName"] = self._app_name(app_id)
        kinds = []
        for done in runs:
            for block in ((getattr(done, "summary", None) or {}).get("blocks") or {}).get("blocks") or ():
                if isinstance(block, dict) and block.get("type") and block["type"] not in kinds:
                    kinds.append(block["type"])
        if kinds:
            out["produces"] = kinds[:3]  # what the tasks it came from handed back, so the Apps page can show it
        if newest is not None and len(runs) == 1 and getattr(newest, "output_schema", None):
            out["outputSchema"] = newest.output_schema
            out["outputFormat"] = getattr(newest, "output_format", None) or "json"
        return {"draft": out, "drafter": drafter}


# The steps a prove pins: what reads, costs or acts. Approvals (a person's answer) and flow nodes always run again.
PROVE_PINS = frozenset({"phone.task", "phone.taught", "phone.check", "app.read", "connector.call", "ai.ask", "brief",
                        "send", "workflow.call"})
# The triggers in a graph that start it from outside, while it is turned on (workflows/triggers.py).
LISTENING = frozenset({"trigger.notification", "trigger.app"})


def _listens(workflow):
    """Whether the workflow's draft or published graph has a trigger that starts it from outside"""
    graph = workflow.get("graph") if graph_schema.check_graph(workflow.get("graph")) else {}
    published = (workflow.get("published") or {}).get("graph") if isinstance(workflow.get("published"), dict) else None
    return any(n.get("type") in LISTENING for g in (graph, published or {}) for n in g.get("nodes") or ())


def _published_graph(workflow):
    """The published graph, or None while nothing is published (nothing fires from a draft)."""
    published = workflow.get("published")
    graph = published.get("graph") if isinstance(published, dict) else None
    return graph if graph_schema.check_graph(graph) else None


def _listens_for(workflow):
    """The trigger in the published graph that starts it from outside, "notification" or "app" (a notification first, as
    the Mac app names one), or None: what the workflow's row says, read from the graph that fires, not the draft."""
    types = {n.get("type") for n in (_published_graph(workflow) or {}).get("nodes") or ()}
    return "notification" if "trigger.notification" in types else "app" if "trigger.app" in types else None


def _starts_itself(workflow):
    """Whether turning the workflow on makes it start by itself: published, with a schedule (``cron``, which ``derive``
    reads from the published graph and the tick fires at ``nextAt``) or a notification or app trigger in the published
    graph (``WorkflowTriggers`` reads only that one). ``enabled`` doesn't change it, so a paused workflow says True and
    can be resumed. The draft never counts: until it is published, nothing fires from it."""
    if _published_graph(workflow) is None:
        return False
    return workflow.get("cron") is not None or _listens_for(workflow) is not None


def _packs():
    """{key: {"name", "readers": {what: [columns]}}} of the app packs here, or None without them."""
    try:
        from ..packs.register import all_packs
        packs = all_packs()
    except Exception:  # noqa: BLE001 -- no packs track: app.read can't be saved, said by the graph's check
        return None
    return {pack.key: {"name": pack.name, "readers": {
        what: [{"key": key, "label": label, "type": kind}
               for key, label, kind in (getattr(pack, "read_columns", None) or {}).get(what, ())]
        for what in pack.readers}} for pack in packs}


def _default_alerts():
    """alerts.Alerts, or None in a build without it: scheduling never depends on alerts."""
    try:
        from ..alerts import Alerts
    except ImportError:
        return None
    return Alerts()
