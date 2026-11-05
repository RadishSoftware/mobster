"""The node types a workflow graph is made of (docs/workflows.mdx › Nodes): each type's fields, its input and output
ports with their JSON schemas, whether it holds a phone and whether it acts outward, and the check each field gets.

``catalog()`` is what ``GET /api/workflows/node-types`` serves, so the canvas builds its palette and inspector from the
engine; a track adds a type with ``register_node_type``. graph/schema.py checks a whole graph against these.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Callable

from ..output_contract import validate_schema
from ..workflows import starters
from . import expr, values
from .infer import schema_kind

VERSION = 2
MAX_NODES = 100
MAX_EDGES = 300
MAX_FANOUT = 10
MAX_NESTING = 2
MAX_CALL_DEPTH = 3
MAX_ITERATIONS = 200
MAX_UNTIL = 50
MAX_TIMES = 50
MAX_CONCURRENCY = 10
MAX_GRAPH_BYTES = 512_000
MAX_PIN_BYTES = 64_000
MAX_PINS_BYTES = 1_000_000
MAX_INPUTS = 30
MAX_VARS = 50
MAX_VARS_BYTES = 16_000
MAX_CASES = 8
MAX_FIELDS = 50
MAX_ROWS = 200
TEXT_CHARS = 2000
CHECK_CHARS = 300
TITLE_CHARS = 80
NOTES_CHARS = 2000
POSITION = 100_000
WAIT_RANGE = (5, 86_400)
APPROVAL_RANGE = (1, 10_080)
SPEND_RANGE = (0.05, 100.0)
MINUTES_RANGE = (1, 1440)
DEFAULT_LIMITS = {"maxSpendUsd": 5.0, "maxMinutes": 240}
ID = re.compile(r"[A-Za-z0-9_-]{1,24}")
VAR = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,39}")
WORKFLOW_ID = re.compile(r"[0-9a-f]{12}")
INPUT_TYPES = ("string", "number", "boolean", "list", "object")
TRIGGERS = ("trigger.manual", "trigger.schedule", "trigger.webhook", "trigger.notification", "trigger.shortcut",
            "trigger.app")
MODES = ("auto", "replay", "agent")
TRUSTED_ROOTS = frozenset({"inputs", "vars", "run", "index"})
TRUSTED_ITEMS = ("times", "rows", "until", "phones")  # loops whose $item is the person's own (or a count)
RESERVED_PORTS = ("in", "out", "error", "else", "items", "item", "index")
# A node's retry (docs/workflows.mdx › Retries): allowed only on types that may not act outward by their nature; whether
# an attempt is retried is decided at run time from the permission gate and the effect ledger, never from the type.
RETRY_TYPES = frozenset({"phone.task", "phone.taught", "phone.check", "app.read", "connector.call", "ai.ask", "wait"})
RETRY_TRIES = (1, 5)
RETRY_BACKOFF = (1, 3600)
APP_EVERY = (5, 1440)
EXPECT_CHARS = 300


ANY = {}
STRING, NUMBER, INTEGER, BOOLEAN = {"type": "string"}, {"type": "number"}, {"type": "integer"}, {"type": "boolean"}


def obj(**properties):
    return {"type": "object", "properties": properties, "additionalProperties": False}


def nullable(schema):
    kind = schema.get("type")
    return {**schema, "type": [kind, "null"]} if isinstance(kind, str) else ANY


def array(items=ANY):
    return {"type": "array", "items": items}


VALUES = {"type": "object", "additionalProperties": STRING}
ROWS = nullable(array({"type": "object", "additionalProperties": STRING}))
ERROR_OUT = obj(node=STRING, reason=STRING, status=nullable(STRING), runId=nullable(STRING))
PHONE_OUT = obj(answer=nullable(STRING), data=ANY, status=STRING, runId=STRING, costUsd=nullable(NUMBER))
PHONE_DEVICE = obj(deviceId=STRING, name=STRING)


@dataclass(frozen=True)
class Field:
    """One config field; an ``instructs`` field tells a model what to do, so it reads only trusted roots."""
    kind: str
    label: str
    required: bool = False
    default: object = None
    low: float | None = None
    high: float | None = None
    choices: tuple = ()
    expr: str | None = None
    instructs: bool = False
    blanks: bool = False
    wants: str | None = None          # the kind a bare expression must give (boolean, number)
    help: str = ""

    def public(self):
        out = {"kind": self.kind, "label": self.label, "required": self.required}
        for key in ("default", "low", "high", "help", "wants"):
            value = getattr(self, key)
            if value not in (None, ""):
                out[key] = value
        if self.choices:
            out["choices"] = list(self.choices)
        if self.expr:
            out["expressions"] = self.expr
        if self.instructs:
            out["instructs"] = True
        if self.blanks:
            out["blanks"] = True
        return out


def text(label, high=TEXT_CHARS, *, required=True, default=None, instructs=False, blanks=False, help=""):
    return Field("text", label, required, default, 1, high, expr="text", instructs=instructs, blanks=blanks, help=help)


DEVICE = Field("device", "Phone", default=None,
               help='null: the workflow\'s phone. "any": any free phone. "loop": the phone of the loop it is in.')
ACCOUNT = Field("account", "Profile", default=None)


@dataclass(frozen=True)
class NodeType:
    type: str
    label: str
    group: str
    description: str
    fields: dict
    outputs: Callable
    inputs: tuple = ("in",)
    single: frozenset = frozenset()
    phone: bool = False
    outward: bool = False
    check: Callable | None = None
    static: dict = field(default_factory=dict)

    @property
    def trigger(self):
        return self.group == "trigger"

    def public(self):
        ports = self.static
        outs = [{"name": name, "schema": schema} for name, schema in ports.items()]
        if not self.trigger:
            outs.append({"name": "error", "schema": ERROR_OUT})
        return {"type": self.type, "label": self.label, "group": self.group, "description": self.description,
                "phone": self.phone, "outward": self.outward,
                "fields": {name: f.public() for name, f in self.fields.items()},
                "inputs": [{"name": name, "single": name in self.single} for name in self.inputs],
                "outputs": outs}


TYPES = {}


def register_node_type(node_type):
    """Add a node type (a track's own: a connector call, say). Its name is new: lowercase words joined by dots."""
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,23}(\.[a-z][a-z0-9_]{1,23})?", node_type.type):
        raise ValueError("A node type is a short lowercase name, like connector.call")
    TYPES[node_type.type] = node_type
    return node_type


def catalog():
    return [TYPES[name].public() for name in TYPES]


class Shapes:

    def __init__(self, validator, node):
        self.validator, self.node = validator, node

    @property
    def input(self):
        return self.validator.input_schema(self.node["id"])

    @property
    def values(self):
        return self.validator.values_schema

    def out(self, node_id, port="out"):
        return self.validator.schema_of(node_id, port)

    def items(self):
        return self.validator.items_schema(self.node["id"])


def _static(schema):
    return lambda node, shapes: {"out": schema}


def _trigger(label, description, out, fields=None):
    return NodeType("trigger." + label.lower().split()[0], label, "trigger", description, fields or {},
                    lambda node, shapes: {"out": out(shapes) if callable(out) else out}, inputs=(),
                    static={"out": None if callable(out) else out})


def _phone_out(node, shapes):
    schema = node["config"].get("outputSchema")
    return {"out": {**PHONE_OUT, "properties": {**PHONE_OUT["properties"],
                                                "data": nullable(schema) if schema else ANY}}}


def _app_read_out(node, shapes):
    config = node["config"]
    columns = (shapes.validator.context.packs or {}).get(config.get("app"), {}).get("readers", {}).get(
        config.get("reader"), [])
    kinds = {"number": NUMBER, "boolean": BOOLEAN}
    row = {"type": "object", "properties": {c["key"]: nullable(kinds.get(c.get("type"), STRING)) for c in columns
                                            if isinstance(c, dict) and isinstance(c.get("key"), str)}}
    rows = array(row)
    return {"out": obj(rows=rows, columns=array(), title=STRING), "rows": rows}


def _loop_out(node, shapes):
    config = node["config"]
    over = config.get("over")
    item = {"times": INTEGER, "until": INTEGER, "phones": PHONE_DEVICE, "rows": {"type": "object",
            "additionalProperties": STRING}}.get(over)
    if over == "items":
        source = shapes.items()
        item = source.get("items", ANY) if schema_kind(source) == "list" else ANY
    collected = shapes.out(config["collect"]) if config.get("collect") else ANY
    return {"item": item or ANY, "index": INTEGER, "out": obj(results=array(nullable(collected) if collected else ANY),
                                                               count=INTEGER)}


def _branch_out(node, shapes):
    passed = shapes.input
    ports = {case["id"]: passed for case in node["config"].get("cases") or []}
    ports["else"] = passed
    return ports


def _transform_out(node, shapes):
    types = shapes.validator.field_types.get(node["id"], {})
    return {"out": {"type": "object", "properties": {name: types.get(("fields", name), ANY)
                                                      for name in node["config"].get("fields") or {}},
                    "additionalProperties": False}}


def _call_out(node, shapes):
    target = (shapes.validator.context.workflows or {}).get(node["config"].get("workflowId")) or {}
    graph = target.get("graph") if isinstance(target.get("graph"), dict) else {}
    output = next((n for n in graph.get("nodes") or [] if n.get("type") == "output"), None)
    if output is None:
        return {"out": obj(status=STRING)}
    return {"out": (output.get("config") or {}).get("schema") or ANY}


def _register_core():
    trusted = "The person's own words: {blanks}, and {= } reading $inputs, $vars, $run or a loop's row."
    register_node_type(_trigger("Manual", "Runs when you run it: from Mobster, the CLI, MCP or Remote.",
                                lambda s: obj(values=s.values, rows=ROWS, input=ANY)))
    register_node_type(_trigger("Schedule", "Runs by itself at the times you pick.",
                                lambda s: obj(values=s.values, rows=ROWS, slot=INTEGER),
                                {"cron": Field("cron", "When", required=True),
                                 "timezone": Field("timezone", "Time zone", required=True),
                                 "missed": Field("choice", "When the Mac slept through it", default="skip",
                                                 choices=("skip", "run_once")),
                                 "overlap": Field("choice", "When the last run is still going", default="skip",
                                                  choices=("skip", "queue_one"))}))
    register_node_type(_trigger("Webhook", "Runs when another tool calls the workflow's webhook URL.",
                                lambda s: obj(values=s.values, body={"type": "object"})))
    register_node_type(_trigger("Notification", "Runs when a notification arrives on the phone.",
                                obj(app=STRING, title=STRING, body=STRING, receivedAt=INTEGER),
                                {"app": Field("app", "From app", default=None),
                                 "contains": Field("short", "Contains", default=None)}))
    register_node_type(_trigger("Shortcut", "Runs from a Shortcut on your Mac or iPhone.",
                                lambda s: obj(values=s.values, input=nullable(STRING))))
    register_node_type(NodeType(
        "trigger.app", "New in an app", "trigger", "Reads an app on the phone on a timer and "
        "runs once for each new row.",
        {"app": Field("pack", "App", required=True), "reader": Field("reader", "What to read", required=True),
         "arg": Field("short", "For", default=None), "every": Field("int", "Every (minutes)", default=15,
                                                                    low=APP_EVERY[0], high=APP_EVERY[1]),
         "key": Field("short", "The field that names a row", required=True), "device": DEVICE, "account": ACCOUNT},
        lambda node, shapes: {"out": obj(row=_app_read_out(node, shapes)["rows"]["items"],
                                         rows=_app_read_out(node, shapes)["rows"])},
        inputs=(), static={"out": None}, phone=True))
    register_node_type(NodeType(
        "phone.task", "Phone task", "phone", "Mobster's agent does a task on a phone, in your words.",
        {"text": text("Task", instructs=True, blanks=True, help=trusted),
         "appId": Field("app_id", "Starts in", default=None), "device": DEVICE, "account": ACCOUNT,
         "mode": Field("choice", "How it runs", default="auto", choices=MODES),
         "outputSchema": Field("schema", "Answer shape", default=None),
         "input": Field("input", "Input it reads (as data)", default="{= $input }", expr="text"),
         "expect": text("When it worked, the screen shows", EXPECT_CHARS, required=False, instructs=True, blanks=True,
                        help="Mobster looks at the screen after the step; a no fails the step with that screen.")},
        _phone_out, phone=True, outward=True, static={"out": PHONE_OUT}))
    register_node_type(NodeType(
        "phone.taught", "Taught task", "phone", "Repeats a task you taught, with no model where it can.",
        {"workflowId": Field("workflow", "Task", required=True), "device": DEVICE, "account": ACCOUNT},
        _static(PHONE_OUT), phone=True, outward=True, static={"out": PHONE_OUT}))
    open_out = obj(url=STRING, landedApp=STRING, screen=STRING)
    register_node_type(NodeType(
        "phone.open_url", "Open a link", "phone",
        "Opens a link on the phone: a web address or an app's deep link. No model. A link that "
        "writes to someone asks first.",
        {"url": text("Link", 2000, help="A web address or an app link, such as "
                     "https://example.com or maps://?q=coffee."),
         "device": DEVICE},
        _static(open_out), phone=True, outward=True, check=_open_url_check, static={"out": open_out}))
    notification_out = obj(title=STRING, body=STRING, app=STRING, at=nullable(STRING))
    waited_out = obj(waitedMinutes=INTEGER)

    def _notification_ports(node, shapes):
        return {"out": notification_out, "timeout": waited_out}
    register_node_type(NodeType(
        "phone.notification", "Wait for a notification", "phone",
        "Waits for a notification on the phone that holds your words, then opens it. If none comes in time, the "
        "timeout path runs. No model.",
        {"app": Field("app", "From app", default=None),
         "match": Field("short", "Contains", default=None, expr="text", blanks=True),
         "timeoutMinutes": Field("int", "Wait up to (minutes)", default=10, low=1, high=240),
         "tap": Field("bool", "Open it", default=True), "device": DEVICE},
        _notification_ports, phone=True, outward=True,
        static={"out": notification_out, "timeout": waited_out}))
    check_out = obj(observed=BOOLEAN, runId=STRING)
    register_node_type(NodeType(
        "phone.check", "Check the phone", "phone", "Looks at the phone and answers true or false. It only looks.",
        {"question": text("Question", CHECK_CHARS, instructs=True, blanks=True, help=trusted), "device": DEVICE,
         "account": ACCOUNT},
        _static(check_out), phone=True, static={"out": check_out}))
    register_node_type(NodeType(
        "app.read", "Read an app", "phone", "Reads an app's screen into rows with an app pack. No model, read only.",
        {"app": Field("pack", "App", required=True), "reader": Field("reader", "What to read", required=True),
         "n": Field("int", "How many", default=10, low=1, high=50), "arg": Field("short", "For", default=None,
                                                                                expr="text"),
         "device": DEVICE, "account": ACCOUNT},
        _app_read_out, phone=True, static={"out": None, "rows": None}))
    register_node_type(NodeType(
        "app.action", "App action", "phone", "Reserved: an app pack's outward action.",
        {"app": Field("pack", "App", required=True), "action": Field("short", "Action", required=True),
         "args": Field("object", "Arguments", default=None, expr="deep"),
         "expect": text("When it worked, the screen shows", EXPECT_CHARS, required=False, instructs=True, blanks=True)},
        _static(obj(done=BOOLEAN, runId=STRING)), phone=True, outward=True, check=_no_actions))
    connector_out = obj(text=STRING, structured=nullable({"type": "object"}), isError=BOOLEAN)
    register_node_type(NodeType(
        "connector.call", "Use a connector", "step", "Calls one tool of a connected service, with no model.",
        {"connector": Field("short", "Connector", required=True), "tool": Field("short", "Tool", required=True),
         "arguments": Field("object", "Arguments", default=None, expr="deep", blanks=True)},
        _static(connector_out), outward=True, check=_connector, static={"out": connector_out}))
    send_out = obj(line=STRING)
    register_node_type(NodeType(
        "send", "Send", "step", "Sends what arrives to a notification, the clipboard, a file or a destination.",
        {"to": Field("delivery", "To", required=True), "destination": Field("short", "Destination", default=None),
         "text": Field("text", "What it sends", default=None, low=1, high=TEXT_CHARS, expr="text", blanks=True),
         "what": Field("choice", "Results", default=None, choices=("last", "all"),
                       help="last or all: the run's newest result, or every result so far, instead of what arrives")},
        _static(send_out), outward=True, check=_send, static={"out": send_out}))
    brief_out = obj(answer=nullable(STRING), runId=STRING)
    register_node_type(NodeType(
        "brief", "Brief", "step", "Puts the results of the tasks wired into it together as one result.",
        {"title": Field("text", "Title", default=None, low=1, high=TITLE_CHARS)},
        _static(brief_out), static={"out": brief_out}))
    register_node_type(NodeType(
        "transform", "Set fields", "step", "Works out new fields from what arrived. Instant, no phone or model.",
        {"fields": Field("fields", "Fields", required=True, expr="deep")},
        _transform_out, static={"out": None}))
    ai_out = obj(answer=nullable(STRING), data=ANY)
    register_node_type(NodeType(
        "ai.ask", "Ask the model", "step", "Asks Claude about what arrived, in your words; it reads it as data.",
        {"instruction": text("Instruction", instructs=True, blanks=True, help=trusted),
         "input": Field("input", "Input it reads (as data)", default="{= $input }", expr="text"),
         "outputSchema": Field("schema", "Answer shape", default=None),
         "model": Field("choice", "Model", default="haiku", choices=("haiku", "sonnet"))},
        _phone_out_ai, static={"out": ai_out}))
    register_node_type(NodeType(
        "output", "Result", "step", "The workflow's result: what a run hands back, and what a parent sees.",
        {"value": Field("input", "Value", default="{= $input }", expr="text"),
         "schema": Field("schema", "Shape", default=None)},
        lambda node, shapes: {}, static={}))
    register_node_type(NodeType(
        "branch", "Branch", "flow", "Sends what arrives down the first way that holds, or down Otherwise.",
        {"cases": Field("cases", "Cases", required=True)}, _branch_out, single=frozenset({"in"})))
    register_node_type(NodeType(
        "loop", "Loop", "flow", "Runs the steps inside it for each item, row, phone or round.",
        {"over": Field("choice", "Over", required=True, choices=("times", "rows", "items", "until", "phones")),
         "times": Field("int", "Times", default=None, low=2, high=MAX_TIMES),
         "rows": Field("rows", "Rows", default=None),
         "until": Field("short", "Until", default=None), "path": Field("short", "Reads", default=None),
         "phones": Field("phones", "Phones", default=None),
         "max": Field("int", "At most", default=None, low=1, high=MAX_ITERATIONS),
         "concurrency": Field("int", "At once", default=None, low=1, high=MAX_CONCURRENCY),
         "collect": Field("short", "Collect", default=None)},
        _loop_out, inputs=("in", "items"), single=frozenset({"items"}), check=_loop))
    register_node_type(NodeType(
        "merge", "Merge", "flow", "Joins paths: waits for all of them, or goes on with the first.",
        {"mode": Field("choice", "Waits for", default="all", choices=("all", "first"))},
        _static(obj(items=array(), first=ANY))))
    register_node_type(NodeType(
        "wait", "Wait", "flow", "Waits a while, or until a time, then passes on what arrived.",
        {"seconds": Field("int", "Seconds", default=None, low=WAIT_RANGE[0], high=WAIT_RANGE[1]),
         "until": Field("bare", "Until", default=None, expr="bare", wants="number")},
        lambda node, shapes: {"out": shapes.input}, check=_wait))
    register_node_type(NodeType(
        "approval", "Ask me first", "flow", "Waits for you to approve before the workflow goes on.",
        {"message": text("Message", help="What you'll be asked. It may show values from earlier steps."),
         "timeoutMinutes": Field("int", "Wait at most (minutes)", default=1440, low=APPROVAL_RANGE[0],
                                 high=APPROVAL_RANGE[1]),
         "showScreen": Field("bool", "Show the phone's screen", default=True),
         "editable": Field("bool", "Let me edit before approving", default=False)},
        lambda node, shapes: {"approved": shapes.input, "declined": obj(reason=STRING, by=nullable(STRING))},
        static={"declined": obj(reason=STRING, by=nullable(STRING))}))
    register_node_type(NodeType(
        "workflow.call", "Run a workflow", "flow", "Runs another workflow and goes on with its result.",
        {"workflowId": Field("workflow", "Workflow", required=True),
         "revision": Field("int", "Version", default=None, low=1, high=10**9),
         "values": Field("values", "Its inputs", default=None, expr="deep", instructs=True)},
        _call_out, outward=True, check=_call))


def _phone_out_ai(node, shapes):
    schema = node["config"].get("outputSchema")
    return {"out": obj(answer=nullable(STRING), data=nullable(schema) if schema else ANY)}


def _no_actions(config, ctx):
    raise ValueError("No app lists an action Mobster can take yet.")


def _connector(config, ctx):
    if ctx.connector is None:
        return config
    made = ctx.connector({"connector": config["connector"], "tool": config["tool"],
                          "arguments": config.get("arguments") or {}})
    return {**config, **{key: made[key] for key in ("connector", "tool", "arguments") if key in made}}


def _send(config, ctx):
    if config["to"] == "destination":
        if not isinstance(config.get("destination"), str) or not WORKFLOW_ID.fullmatch(config["destination"]):
            raise ValueError("Choose a saved destination")
        if ctx.destination is None:
            raise ValueError("Sending results isn't available in this Mobster. Choose another place.")
        if not ctx.destination(config["destination"]):
            raise ValueError("That destination was removed. Choose another one.")
    elif config.get("destination") is not None:
        raise ValueError("Only a send to a destination names one")
    return config


def _open_url_check(config, ctx):
    """A literal link is checked when the workflow is saved: a scheme Mobster never opens is refused there."""
    url = config.get("url")
    if isinstance(url, str) and "{" not in url:
        from .. import deeplinks
        try:
            deeplinks.risk_of(url)
        except deeplinks.LinkError as error:
            raise ValueError(str(error)) from None
    return config


def app_missing(app_id):
    """Why a step can't start in ``app_id``: this Mac's list of apps (an iPhone's, or the catalog) doesn't have it."""
    return (f"{str(app_id)[:40]} isn't on this Mac's list of apps yet. Connect the iPhone that has it, or choose Any "
            "app.")


def _wait(config, ctx):
    if (config.get("seconds") is None) == (config.get("until") is None):
        raise ValueError("A wait has seconds or until, one of them")
    return config


def _loop(config, ctx):
    over = config["over"]
    allowed = {"times": {"times"}, "rows": {"rows", "max"}, "items": {"max"}, "until": {"until", "path", "max"},
               "phones": {"phones", "max"}}[over] | {"over", "concurrency", "collect"}
    extra = [key for key, value in config.items() if value is not None and key not in allowed]
    if extra:
        raise ValueError(f"A loop over {over} doesn't take {', '.join(sorted(extra))}")
    if over == "times" and config.get("times") is None:
        raise ValueError("Say how many times")
    if over == "until":
        if not config.get("until"):
            raise ValueError("Choose the step whose answer ends the loop")
        if config.get("concurrency") not in (None, 1):
            raise ValueError("A loop until something is seen runs one round at a time")
        config = {**config, "path": config.get("path") or "/observed",
                  "max": config.get("max") if config.get("max") is not None else 10}
        if config["max"] > MAX_UNTIL:
            raise ValueError(f"A loop until something is seen runs at most {MAX_UNTIL} rounds")
        _pointer(config["path"])
    if over == "phones" and config.get("phones") is None:
        raise ValueError('Choose the phones: "all", a list, or {"count": n}')
    if over in ("rows", "items") and config.get("max") is None:
        config = {**config, "max": len(config["rows"]) if over == "rows" and config.get("rows") else MAX_ITERATIONS}
    if config.get("concurrency") is None:
        config = {**config, "concurrency": 1}
    return config


def _call(config, ctx):
    target = config["workflowId"]
    if target == ctx.workflow_id:
        raise ValueError("A workflow can't run itself")
    records = ctx.workflows
    if records is not None:
        if target not in records:
            raise ValueError("The workflow it runs was deleted. Choose another one.")
        depth = _call_depth(target, records, {ctx.workflow_id} if ctx.workflow_id else set())
        if depth > 3:
            raise ValueError("Workflows run inside each other at most 3 deep")
    return config


def _call_depth(workflow_id, records, seen):
    if workflow_id in seen:
        raise ValueError("These workflows would run each other in a circle")
    graph = (records.get(workflow_id) or {}).get("graph") or {}
    calls = [n.get("config", {}).get("workflowId") for n in graph.get("nodes") or []
             if n.get("type") == "workflow.call"]
    if any(call in seen for call in calls):
        raise ValueError("These workflows would run each other in a circle")
    return 1 + max((_call_depth(c, records, seen | {workflow_id}) for c in calls if c in records), default=0)


def _pointer(path):
    if not isinstance(path, str) or not (path == "" or path.startswith("/")) or len(path) > 200:
        raise ValueError('A path is a JSON Pointer, like "/observed"')
    return path


def pointer(value, path):
    """``value`` at the JSON Pointer ``path``, or None."""
    for raw in path.split("/")[1:] if path else []:
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            value = value[int(key)] if key.isdigit() and int(key) < len(value) else None
        elif isinstance(value, dict):
            value = value.get(key)
        else:
            return None
    return value


def _json_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False))


def _short(value, label, high=200):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= high:
        raise ValueError(f"{label} is 1 to {high} characters")
    return value.strip()


class _Fields:

    @staticmethod
    def text(value, f, ctx, v, node):
        cleaned = _short(value, f.label, int(f.high or TEXT_CHARS))
        if f.blanks:
            starters.check_steps(cleaned)
        return cleaned

    @staticmethod
    def bool(value, f, ctx, v, node):
        if type(value) is not bool:
            raise ValueError(f"{f.label} is true or false")
        return value

    @staticmethod
    def short(value, f, ctx, v, node):
        return _short(value, f.label)

    @staticmethod
    def int(value, f, ctx, v, node):
        if type(value) is not int or not f.low <= value <= f.high:
            raise ValueError(f"{f.label} is a whole number from {int(f.low):,} to {int(f.high):,}")
        return value

    @staticmethod
    def choice(value, f, ctx, v, node):
        if value not in f.choices:
            raise ValueError(f"{f.label} is one of " + ", ".join(f.choices))
        return value

    @staticmethod
    def bare(value, f, ctx, v, node):
        source = _short(value, f.label, values.MAX_SOURCE + 4)
        shape = expr.template(source) if source.startswith("{=") else None
        if shape is not None:
            if shape.whole is None:
                raise ValueError(f"{f.label} is one expression")
            return shape.whole.source.strip()
        expr.compile(source)
        return source

    @staticmethod
    def input(value, f, ctx, v, node):
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > TEXT_CHARS:
            raise ValueError(f"{f.label} is an expression, like {{= $input }}")
        return value

    @staticmethod
    def schema(value, f, ctx, v, node):
        return validate_schema(value)

    @staticmethod
    def object(value, f, ctx, v, node):
        if not isinstance(value, dict):
            raise ValueError(f"{f.label} is one JSON object")
        if _json_size(value) > 16_000:
            raise ValueError(f"{f.label} is too long")
        return value

    @staticmethod
    def fields(value, f, ctx, v, node):
        if not isinstance(value, dict) or not 1 <= len(value) <= MAX_FIELDS:
            raise ValueError(f"Set 1 to {MAX_FIELDS} fields")
        for name, item in value.items():
            if not VAR.fullmatch(name):
                raise ValueError(f"A field's name is letters, digits and _, not {name[:40]!r}")
            if not isinstance(item, str) or not item.strip() or len(item) > TEXT_CHARS:
                raise ValueError(f"{name} is a value or an expression, like {{= $input.answer }}")
        return value

    @staticmethod
    def values(value, f, ctx, v, node):
        if not isinstance(value, dict) or len(value) > starters.MAX_BLANKS:
            raise ValueError(f"Give at most {starters.MAX_BLANKS} values, by name")
        for name, item in value.items():
            _short(name, "An input's name", starters.NAME_CHARS)
            if not isinstance(item, (str, int, float, bool)) or (isinstance(item, str) and len(item) > TEXT_CHARS):
                raise ValueError(f"The value for {name} is short text, a number or an expression")
        return value

    @staticmethod
    def delivery(value, f, ctx, v, node):
        if value not in ctx.deliveries:
            raise ValueError("Send to a notification, the clipboard, a file or a saved destination")
        return value

    @staticmethod
    def app_id(value, f, ctx, v, node):
        if value is None:
            return None
        if value == "any":
            return None
        if not isinstance(value, str) or not 1 <= len(value) <= 160:
            raise ValueError("A task's app is an app's id, or null for the Home Screen")
        if ctx.app_ids is not None and value not in ctx.app_ids(value):
            raise ValueError(app_missing(value))
        return value

    @staticmethod
    def device(value, f, ctx, v, node):
        if value is None or value in ("any", "loop"):
            return value
        if ctx.device is None:
            raise ValueError("Mobster can't run a step on a chosen phone here. Use the workflow's phone.")
        return ctx.device(value)

    @staticmethod
    def account(value, f, ctx, v, node):
        if value is None:
            return None
        if ctx.account is None:
            raise ValueError("Profiles aren't available right now. Use the workflow's profile.")
        return ctx.account(value)

    @staticmethod
    def workflow(value, f, ctx, v, node):
        if not isinstance(value, str) or not WORKFLOW_ID.fullmatch(value):
            raise ValueError("Choose a saved workflow")
        if node["type"] == "phone.taught":
            if value == ctx.workflow_id:
                raise ValueError("A workflow can't repeat itself")
            if ctx.workflows is not None:
                found = ctx.workflows.get(value)
                if found is None:
                    raise ValueError("The workflow a step runs was deleted. Choose another one.")
                if ctx.plain is not None and not ctx.plain(found):
                    raise ValueError(f"“{found.get('name')}” has several steps. A taught step repeats a one-task "
                                     "workflow.")
        return value

    @staticmethod
    def pack(value, f, ctx, v, node):
        value = _short(value, "The app", 60)
        if ctx.packs is None:
            raise ValueError("App packs aren't available in this Mobster")
        if value not in ctx.packs:
            raise ValueError(f"No app pack is called {value}")
        return value

    @staticmethod
    def reader(value, f, ctx, v, node):
        value = _short(value, "What to read", 60)
        pack = (ctx.packs or {}).get(node["config"].get("app")) or {}
        if pack and value not in (pack.get("readers") or {}):
            raise ValueError(f"{pack.get('name') or 'That app'} can't read {value}. It reads "
                             + ", ".join(sorted(pack.get("readers") or {})))
        return value

    @staticmethod
    def cases(value, f, ctx, v, node):
        if not isinstance(value, list) or not 1 <= len(value) <= MAX_CASES:
            raise ValueError(f"A branch has 1 to {MAX_CASES} cases")
        seen, out = set(), []
        for case in value:
            if not isinstance(case, dict) or set(case) - {"id", "label", "when"}:
                raise ValueError("A case has an id, a label and when")
            ident = case.get("id")
            if not isinstance(ident, str) or not ID.fullmatch(ident) or ident in seen or ident in RESERVED_PORTS:
                raise ValueError("Each case has its own id: 1 to 24 letters, digits, - or _ (not else or error)")
            seen.add(ident)
            label = _short(case.get("label") or ident, "A case's label", TITLE_CHARS)
            when = _Fields.bare(case.get("when"), Field("bare", f"The case {label}"), ctx, v, node)
            out.append({"id": ident, "label": label, "when": when})
        return out

    @staticmethod
    def rows(value, f, ctx, v, node):
        if value is None:
            return None
        if not isinstance(value, list) or not 1 <= len(value) <= MAX_ROWS:
            raise ValueError(f"Give 1 to {MAX_ROWS} rows")
        out = []
        for index, row in enumerate(value, 1):
            try:
                cleaned = starters.clean_values(row)
            except ValueError as error:
                raise ValueError(f"Row {index}: {error}") from None
            if not cleaned:
                raise ValueError(f"Row {index} is empty")
            out.append({name: item for name, item in row.items() if starters._key(name) in cleaned})
        return out

    @staticmethod
    def phones(value, f, ctx, v, node):
        if value == "all":
            return value
        if isinstance(value, dict) and set(value) == {"count"}:
            if type(value["count"]) is not int or not 1 <= value["count"] <= MAX_CONCURRENCY:
                raise ValueError(f"A loop takes 1 to {MAX_CONCURRENCY} phones")
            return value
        if isinstance(value, list) and 1 <= len(value) <= MAX_ITERATIONS:
            if ctx.device is None:
                raise ValueError("Mobster can't choose phones here")
            ids = [ctx.device(item) for item in value]
            if len(set(ids)) != len(ids):
                raise ValueError("Each phone once")
            return ids
        raise ValueError('Choose the phones: "all", a list of phones, or {"count": n}')

    @staticmethod
    def cron(value, f, ctx, v, node):
        value = _short(value, "The schedule", 120)
        return ctx.cron(value, node["config"].get("timezone")) if ctx.cron else value

    @staticmethod
    def timezone(value, f, ctx, v, node):
        value = _short(value, "The time zone", 64)
        return ctx.timezone(value) if ctx.timezone else value

    @staticmethod
    def app(value, f, ctx, v, node):
        return None if value is None else _short(value, "The app", 200)


def public_limits():
    """The limits a graph is checked against, for the canvas (GET /api/workflows/node-types)."""
    return {"nodes": MAX_NODES, "edges": MAX_EDGES, "fanout": MAX_FANOUT, "nesting": MAX_NESTING,
            "iterations": MAX_ITERATIONS, "untilRounds": MAX_UNTIL, "times": MAX_TIMES, "concurrency": MAX_CONCURRENCY,
            "graphBytes": MAX_GRAPH_BYTES, "pinBytes": MAX_PIN_BYTES, "pinsBytes": MAX_PINS_BYTES, "inputs": MAX_INPUTS,
            "vars": MAX_VARS, "cases": MAX_CASES, "fields": MAX_FIELDS, "rows": MAX_ROWS, "textChars": TEXT_CHARS,
            "waitSeconds": list(WAIT_RANGE), "approvalMinutes": list(APPROVAL_RANGE), "spendUsd": list(SPEND_RANGE),
            "minutes": list(MINUTES_RANGE), "retryTries": list(RETRY_TRIES), "retryBackoffSeconds": list(RETRY_BACKOFF),
            "expression": {"chars": values.MAX_SOURCE, "nodes": values.MAX_NODES, "steps": values.MAX_STEPS,
                           "resultBytes": values.MAX_RESULT}}


_register_core()
