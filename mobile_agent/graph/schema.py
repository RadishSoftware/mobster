"""A workflow graph's saved shape: its node types, their ports and validation (docs/workflows.mdx › The graph).

The node types and their fields are graph/catalog.py. A graph is ``{"version": 2, "inputs", "vars", "nodes", "edges",
"limits"}``. Each node has an ``id``, a ``type`` from
``TYPES``, its ``config``, a ``position`` the engine never reads, and may have a ``title``, ``notes``, a ``parent`` (the
loop it sits in) and a ``pin`` (test data, by output port). Each edge joins a source's output port to a target's input
port and may reshape the value on its way (``map``, an expression).

``validate(graph, context)`` returns the graph as saved (config defaults filled in) or raises ``GraphError`` listing
every problem with the node, edge, port or field it is about, so the canvas can mark each one. ``catalog()`` is what
``GET /api/workflows/node-types`` serves: every type with its fields and ports, so the canvas builds its palette and
inspector from the engine.

Screen text is data: a field that instructs a model (a phone step's words, a check's question, ``ai.ask``'s
instruction, a sub-workflow's values) may read only the person's own values (``$inputs``, ``$vars``, ``$run``, and a
loop's index, row or phone). Everything a step produced reaches a model only as fenced input.
"""

import math
from dataclasses import dataclass
from typing import Callable

from ..workflows import starters
from . import expr
from .catalog import (
    ANY,
    BOOLEAN,
    DEFAULT_LIMITS,
    ERROR_OUT,
    ID,
    INPUT_TYPES,
    INTEGER,
    MAX_EDGES,
    MAX_FANOUT,
    MAX_GRAPH_BYTES,
    MAX_INPUTS,
    MAX_ITERATIONS,
    MAX_NESTING,
    MAX_NODES,
    MAX_PIN_BYTES,
    MAX_PINS_BYTES,
    MAX_VARS,
    MAX_VARS_BYTES,
    MINUTES_RANGE,
    NOTES_CHARS,
    NUMBER,
    POSITION,
    RETRY_BACKOFF,
    RETRY_TRIES,
    RETRY_TYPES,
    SPEND_RANGE,
    STRING,
    TITLE_CHARS,
    TRIGGERS,
    TRUSTED_ITEMS,
    TRUSTED_ROOTS,
    TYPES,
    VAR,
    VERSION,
    Field,
    Shapes,
    _Fields,
    _json_size,
    _short,
    array,
    catalog,
    pointer,
    public_limits,
    register_node_type,
)
from .expr import ExprError
from .infer import RUN_SCHEMA, infer, schema_kind

__all__ = ["VERSION", "TYPES", "TRIGGERS", "DEFAULT_LIMITS", "ERROR_OUT", "MAX_ITERATIONS", "GraphError", "Context",
           "validate", "check_graph", "coerce", "catalog", "public_limits", "register_node_type", "pointer"]


class GraphError(ValueError):
    """A graph that can't be saved."""

    code = "invalid_graph"

    def __init__(self, errors):
        self.errors = list(errors)
        more = len(self.errors) - 1
        super().__init__(self.errors[0]["message"] + (f" (and {more} more)" if more else ""))


@dataclass
class Context:
    """What validation asks the rest of Mobster."""
    workflow_id: str | None = None
    workflows: dict | None = None
    app_ids: Callable | None = None
    device: Callable | None = None
    account: Callable | None = None
    destination: Callable | None = None
    connector: Callable | None = None
    packs: dict | None = None
    cron: Callable | None = None
    timezone: Callable | None = None
    deliveries: tuple = ("notification", "clipboard", "file", "destination")
    plain: Callable | None = None


class _Validator:
    def __init__(self, graph, ctx):
        self.graph, self.context = graph, ctx
        self.problems = []
        self.nodes = {}
        self.edges = []
        self.expressions = []
        self.field_types = {}
        self._schemas = {}
        self._input = {}
        self._busy = set()

    def problem(self, message, **where):
        self.problems.append({**{k: v for k, v in where.items() if v is not None}, "message": str(message)})

    def run(self):
        graph = self.graph
        if not isinstance(graph, dict):
            raise GraphError([{"message": "A graph is one JSON object"}])
        extra = set(graph) - {"version", "inputs", "vars", "nodes", "edges", "limits"}
        if extra:
            self.problem("A graph has only version, inputs, vars, nodes, edges and limits, not "
                         + ", ".join(sorted(extra)))
        if graph.get("version") != VERSION:
            self.problem(f"This is a graph of version {VERSION}")
        try:
            size = _json_size(graph)
        except (TypeError, ValueError):
            raise GraphError([{"message": "A graph is plain JSON"}]) from None
        if size > MAX_GRAPH_BYTES:
            raise GraphError([{"message": f"A graph is at most {MAX_GRAPH_BYTES // 1000} KB, pins included"}])
        inputs = self.inputs(graph.get("inputs"))
        variables = self.variables(graph.get("vars"))
        limits = self.limits(graph.get("limits"))
        nodes = self.read_nodes(graph.get("nodes"))
        self.read_parents()
        self.read_edges(graph.get("edges"))
        if not self.problems:
            self.structure()
        if not self.problems:
            self.values_schema = self.make_values_schema(inputs)
            self.vars_schema = {"type": "object", "properties": {k: _schema_of_value(x) for k, x in variables.items()},
                                "additionalProperties": False}
            self.check_expressions()
            self.check_pins()
        if self.problems:
            raise GraphError(self.problems)
        return {"version": VERSION, "inputs": inputs, "vars": variables, "nodes": nodes,
                "edges": [edge for edge in self.edges], "limits": limits}

    def inputs(self, value):
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > MAX_INPUTS:
            self.problem(f"A graph has at most {MAX_INPUTS} inputs", field="inputs")
            return []
        out, seen = [], set()
        for item in value:
            try:
                if not isinstance(item, dict) or set(item) - {"name", "type", "default", "description", "required"}:
                    raise ValueError("An input has a name, a type, and may have a default, a description and required")
                name = _short(item.get("name"), "An input's name", starters.NAME_CHARS)
                if not starters.NAMED.fullmatch(name):
                    raise ValueError(f"{name} can't be an input's name: start with a letter")
                key = starters._key(name)
                if key in seen:
                    raise ValueError(f"Two inputs are called {name}")
                seen.add(key)
                kind = item.get("type", "string")
                if kind not in INPUT_TYPES:
                    raise ValueError("An input's type is " + ", ".join(INPUT_TYPES))
                default = item.get("default")
                if default is not None:
                    default = coerce(default, kind, name)
                cleaned = {"name": name, "type": kind}
                if default is not None:
                    cleaned["default"] = default
                if item.get("description"):
                    cleaned["description"] = _short(item["description"], "A description", 200)
                if item.get("required") is not None:
                    if type(item["required"]) is not bool:
                        raise ValueError("required is true or false")
                    cleaned["required"] = item["required"]
                out.append(cleaned)
            except ValueError as error:
                self.problem(error, field="inputs")
        return out

    def variables(self, value):
        if value is None:
            return {}
        if not isinstance(value, dict) or len(value) > MAX_VARS:
            self.problem(f"A graph has at most {MAX_VARS} vars", field="vars")
            return {}
        for name in value:
            if not isinstance(name, str) or not VAR.fullmatch(name):
                self.problem(f"A var's name is letters, digits and _, starting with a letter: {str(name)[:40]!r}",
                             field="vars")
        if _json_size(value) > MAX_VARS_BYTES:
            self.problem(f"A graph's vars are at most {MAX_VARS_BYTES // 1000} KB in all", field="vars")
        return dict(value)

    def limits(self, value):
        value = value if value is not None else {}
        if not isinstance(value, dict) or set(value) - set(DEFAULT_LIMITS):
            self.problem("A workflow's limits are maxSpendUsd and maxMinutes", field="limits")
            return dict(DEFAULT_LIMITS)
        spend = value.get("maxSpendUsd", DEFAULT_LIMITS["maxSpendUsd"])
        if (isinstance(spend, bool) or not isinstance(spend, (int, float))
                or not SPEND_RANGE[0] <= spend <= SPEND_RANGE[1]):
            self.problem(f"maxSpendUsd is from ${SPEND_RANGE[0]:.2f} to ${SPEND_RANGE[1]:.0f}", field="limits")
            spend = DEFAULT_LIMITS["maxSpendUsd"]
        minutes = value.get("maxMinutes", DEFAULT_LIMITS["maxMinutes"])
        if type(minutes) is not int or not MINUTES_RANGE[0] <= minutes <= MINUTES_RANGE[1]:
            self.problem(f"maxMinutes is a whole number from {MINUTES_RANGE[0]} to {MINUTES_RANGE[1]:,}",
                         field="limits")
            minutes = DEFAULT_LIMITS["maxMinutes"]
        return {"maxSpendUsd": round(float(spend), 2), "maxMinutes": minutes}

    def read_nodes(self, value):
        if not isinstance(value, list) or not value:
            self.problem("A graph has at least one node", field="nodes")
            return []
        if len(value) > MAX_NODES:
            self.problem(f"A graph has at most {MAX_NODES} nodes", field="nodes")
            return []
        out = []
        for raw in value:
            node = self.read_node(raw)
            if node is not None:
                out.append(node)
        kinds = [node["type"] for node in out if node["type"] in TRIGGERS]
        if not kinds:
            self.problem("Add a trigger: when the workflow runs")
        for kind in set(kinds):
            if kinds.count(kind) > 1:
                self.problem(f"A graph has one {TYPES[kind].label} trigger at most")
        if sum(1 for node in out if node["type"] == "output") > 1:
            self.problem("A graph has one Result at most")
        return out

    def read_node(self, raw):
        if not isinstance(raw, dict):
            self.problem("Each node is an object")
            return None
        ident = raw.get("id")
        if not isinstance(ident, str) or not ID.fullmatch(ident):
            self.problem("Each node has an id: 1 to 24 letters, digits, - or _")
            return None
        if ident in self.nodes:
            self.problem(f"Two nodes have the id {ident}", node=ident)
            return None
        extra = set(raw) - {"id", "type", "config", "position", "title", "parent", "pin", "notes", "retry"}
        if extra:
            self.problem("A node has only id, type, config, position, title, parent, pin, notes and retry", node=ident)
        kind = raw.get("type")
        node_type = TYPES.get(kind)
        if node_type is None:
            self.problem(f"Mobster has no node of type {str(kind)[:40]!r}", node=ident)
            return None
        node = {"id": ident, "type": kind, "config": {}, "position": self.position(raw.get("position"), ident)}
        for key, high in (("title", TITLE_CHARS), ("notes", NOTES_CHARS)):
            if raw.get(key) is not None:
                try:
                    node[key] = _short(raw[key], "A node's " + key, high)
                except ValueError as error:
                    self.problem(error, node=ident, field=key)
        if raw.get("parent") is not None:
            node["parent"] = raw["parent"]
        if raw.get("pin") is not None:
            node["pin"] = raw["pin"]
        if raw.get("retry") is not None:
            retry = self.retry(raw["retry"], kind, ident)
            if retry is not None:
                node["retry"] = retry
        self.nodes[ident] = node
        node["config"] = self.config(node, node_type, raw.get("config"))
        return node

    def retry(self, value, kind, ident):
        if kind not in RETRY_TYPES:
            self.problem(f"A {TYPES[kind].label} step can act outward, so Mobster never retries it", node=ident,
                         field="retry")
            return None
        if (not isinstance(value, dict) or set(value) - {"tries", "backoffSeconds"}
                or type(value.get("tries")) is not int or not RETRY_TRIES[0] <= value["tries"] <= RETRY_TRIES[1]
                or type(value.get("backoffSeconds", 30)) is not int
                or not RETRY_BACKOFF[0] <= value.get("backoffSeconds", 30) <= RETRY_BACKOFF[1]):
            self.problem(f"retry is {{tries: {RETRY_TRIES[0]} to {RETRY_TRIES[1]}, backoffSeconds: {RETRY_BACKOFF[0]} "
                         f"to {RETRY_BACKOFF[1]:,}}}", node=ident, field="retry")
            return None
        return {"tries": value["tries"], "backoffSeconds": value.get("backoffSeconds", 30)}

    def position(self, value, ident):
        if value is None:
            return {"x": 0, "y": 0}
        if (not isinstance(value, dict) or set(value) != {"x", "y"} or
                any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n)
                    or abs(n) > POSITION for n in value.values())):
            self.problem(f"A node's position is {{x, y}}, each within ±{POSITION:,}", node=ident, field="position")
            return {"x": 0, "y": 0}
        return {"x": value["x"], "y": value["y"]}

    def config(self, node, node_type, value):
        ident = node["id"]
        value = value if value is not None else {}
        if not isinstance(value, dict):
            self.problem("A node's config is one object", node=ident, field="config")
            return {}
        for key in set(value) - set(node_type.fields):
            self.problem(f"A {node_type.label} node has no field {key}", node=ident, field=key)
        out = {}
        node["config"] = out
        failed = False
        for name, f in node_type.fields.items():
            given = value.get(name, f.default)
            if given is None:
                if f.required:
                    self.problem(f"{f.label} is needed", node=ident, field=name)
                    failed = True
                out[name] = None
                continue
            try:
                out[name] = getattr(_Fields, f.kind)(given, f, self.context, self, node)
            except (ValueError, ExprError) as error:
                self.problem(error, node=ident, field=name)
                failed = True
                out[name] = None
        if failed:
            return out
        if node_type.check is not None:
            try:
                out.update(node_type.check(dict(out), self.context))
            except (ValueError, ExprError) as error:
                self.problem(error, node=ident)
                return out
        self.collect_expressions(node, node_type)
        return out

    def collect_expressions(self, node, node_type):
        for name, f in node_type.fields.items():
            if not f.expr or node["config"].get(name) is None:
                continue
            value = node["config"][name]
            if f.expr == "bare":
                self.expressions.append((node["id"], (name,), expr.compile(value), f.instructs, f.wants))
                continue
            for path, raw in _strings(value, (name,)) if f.expr == "deep" else [((name,), value)]:
                try:
                    shape = expr.template(raw)
                except ExprError as error:
                    self.problem(f"{error}", node=node["id"], field=".".join(map(str, path)))
                    continue
                for parsed in shape.expressions:
                    self.expressions.append((node["id"], path, parsed, f.instructs, None))
                if shape.whole is not None:
                    self.expressions.append((node["id"], path + ("whole",), shape.whole, False, None))
        if node["type"] == "branch":
            for case in node["config"].get("cases") or []:
                self.expressions.append((node["id"], ("cases", case["id"]), expr.compile(case["when"]), False,
                                         "boolean"))

    def read_parents(self):
        for node in list(self.nodes.values()):
            parent = node.get("parent")
            if parent is None:
                continue
            owner = self.nodes.get(parent) if isinstance(parent, str) else None
            if owner is None or owner["type"] != "loop":
                self.problem("A node sits inside a loop: its parent is a loop's id", node=node["id"], field="parent")
                node.pop("parent", None)
                continue
            if node["type"] in TRIGGERS or node["type"] == "output":
                self.problem(f"A {TYPES[node['type']].label} can't sit inside a loop", node=node["id"],
                             field="parent")
        for node in self.nodes.values():
            seen, at = {node["id"]}, node.get("parent")
            depth = 0
            while at is not None:
                if at in seen:
                    self.problem("Loops can't sit inside each other in a circle", node=node["id"], field="parent")
                    node.pop("parent", None)
                    break
                seen.add(at)
                depth += 1
                at = self.nodes[at].get("parent")
            if node["type"] == "loop" and depth >= MAX_NESTING:
                self.problem(f"Loops sit inside each other at most {MAX_NESTING} deep", node=node["id"])

    def scopes(self, ident):
        """The loops around a node, innermost first, ending with None (the top)."""
        out, at = [], self.nodes[ident].get("parent")
        while at is not None:
            out.append(at)
            at = self.nodes[at].get("parent")
        return out + [None]

    def lift(self, ident, level):
        """The node at ``level`` (a loop's id, or None for the top) that holds ``ident``, or None."""
        at = ident
        while at is not None:
            if self.nodes[at].get("parent") == level:
                return at
            at = self.nodes[at].get("parent")
        return None

    def outputs(self, ident):
        node = self.nodes[ident]
        node_type = TYPES[node["type"]]
        ports = set(node_type.static)
        if node["type"] == "branch":
            ports |= {case["id"] for case in node["config"].get("cases") or []} | {"else"}
        elif node["type"] == "loop":
            ports |= {"item", "index", "out"}
        elif node["type"] == "approval":
            ports |= {"approved", "declined"}
        elif node["type"] in ("merge", "wait", "workflow.call") or not ports:
            ports |= {"out"} if node["type"] != "output" else set()
        if not node_type.trigger:
            ports.add("error")
        return ports

    def read_edges(self, value):
        value = value if value is not None else []
        if not isinstance(value, list) or len(value) > MAX_EDGES:
            self.problem(f"A graph has at most {MAX_EDGES} edges", field="edges")
            return
        seen, pairs, fanout = set(), set(), {}
        for raw in value:
            if not isinstance(raw, dict):
                self.problem("Each edge is an object")
                continue
            ident = raw.get("id")
            if not isinstance(ident, str) or not ID.fullmatch(ident) or ident in seen:
                self.problem("Each edge has its own id: 1 to 24 letters, digits, - or _")
                continue
            seen.add(ident)
            extra = set(raw) - {"id", "source", "sourcePort", "target", "targetPort", "map"}
            if extra:
                self.problem("An edge has only id, source, sourcePort, target, targetPort and map", edge=ident)
            source, target = raw.get("source"), raw.get("target")
            sport, tport = raw.get("sourcePort", "out"), raw.get("targetPort", "in")
            if source not in self.nodes or target not in self.nodes:
                self.problem("An edge joins two nodes of this graph", edge=ident)
                continue
            if source == target:
                self.problem("A node can't feed itself", edge=ident, node=source)
                continue
            if sport not in self.outputs(source):
                self.problem(f"{self.name(source)} has no output called {sport}", edge=ident, node=source,
                             port=sport)
                continue
            target_type = TYPES[self.nodes[target]["type"]]
            if target_type.trigger:
                self.problem("Nothing runs into a trigger", edge=ident, node=target)
                continue
            if tport not in target_type.inputs:
                self.problem(f"{self.name(target)} has no input called {tport}", edge=ident, node=target, port=tport)
                continue
            if (source, sport, target, tport) in pairs:
                self.problem("Those two ports are joined twice", edge=ident)
                continue
            pairs.add((source, sport, target, tport))
            fanout[(source, sport)] = fanout.get((source, sport), 0) + 1
            if fanout[(source, sport)] == MAX_FANOUT + 1:
                self.problem(f"One output feeds at most {MAX_FANOUT} steps", node=source, port=sport)
            edge = {"id": ident, "source": source, "sourcePort": sport, "target": target, "targetPort": tport}
            if raw.get("map") is not None:
                try:
                    edge["map"] = _Fields.bare(raw["map"], Field("bare", "The edge's map"), self.context, self, None)
                    self.expressions.append(((ident, "edge"), ("map",), expr.compile(edge["map"]), False, None))
                except (ValueError, ExprError) as error:
                    self.problem(error, edge=ident, field="map")
                    continue
            if not self.scope_ok(edge):
                continue
            self.edges.append(edge)
        for node in self.nodes.values():
            node_type = TYPES[node["type"]]
            for port in node_type.single:
                count = sum(1 for e in self.edges if e["target"] == node["id"] and e["targetPort"] == port)
                if count > 1:
                    self.problem(f"{self.name(node['id'])} takes one edge into {port}", node=node["id"], port=port)

    def scope_ok(self, edge):
        source, target, port = edge["source"], edge["target"], edge["sourcePort"]
        inside = source in self.scopes(target)[:-1]
        if self.nodes[source]["type"] == "loop" and port in ("item", "index"):
            if not inside:
                self.problem(f"A loop's {port} goes only to steps inside the loop", edge=edge["id"], node=source)
                return False
            return True
        if inside:
            self.problem("A loop's results leave through its out port, not into its own steps", edge=edge["id"],
                         node=source)
            return False
        if self.nodes[source].get("parent") not in self.scopes(target):
            self.problem("A step inside a loop sends its results out through the loop's collect and out port",
                         edge=edge["id"], node=source)
            return False
        return True

    def name(self, ident):
        node = self.nodes[ident]
        return f"“{node.get('title') or TYPES[node['type']].label}”"

    def level_edges(self, level):
        out = set()
        for edge in self.edges:
            if self.nodes[edge["source"]]["type"] == "loop" and edge["sourcePort"] in ("item", "index") \
                    and edge["source"] in self.scopes(edge["target"])[:-1]:
                continue
            a, b = self.lift(edge["source"], level), self.lift(edge["target"], level)
            if a is not None and b is not None and a != b:
                out.add((a, b))
        return out

    def levels(self):
        return [None] + [ident for ident, node in self.nodes.items() if node["type"] == "loop"]

    def structure(self):
        self.order = {}
        for level in self.levels():
            members = [ident for ident, node in self.nodes.items() if node.get("parent") == level]
            edges = self.level_edges(level)
            order = _topological(members, edges)
            if order is None:
                cycle = _cycle(members, edges)
                self.problem("These steps wait for each other in a circle: " + ", ".join(self.name(i) for i in cycle),
                             node=cycle[0])
                return
            self.order[level] = order
        top = [ident for ident, node in self.nodes.items() if node.get("parent") is None]
        reached = {ident for ident in top if self.nodes[ident]["type"] in TRIGGERS}
        edges = self.level_edges(None)
        changed = True
        while changed:
            changed = False
            for a, b in edges:
                if a in reached and b not in reached:
                    reached.add(b)
                    changed = True
        for ident in top:
            if ident not in reached:
                self.problem(f"{self.name(ident)} never runs: no trigger leads to it", node=ident)
        for node in self.nodes.values():
            self.node_rules(node)

    def incoming(self, ident, port=None):
        return [e for e in self.edges if e["target"] == ident and (port is None or e["targetPort"] == port)]

    def node_rules(self, node):
        ident, config, kind = node["id"], node["config"], node["type"]
        if kind == "merge" and len(self.incoming(ident)) < 2:
            self.problem("A merge joins two or more paths", node=ident)
        if kind == "branch" and len(self.incoming(ident, "in")) != 1:
            self.problem("A branch takes exactly one edge", node=ident)
        if kind == "loop":
            items = self.incoming(ident, "items")
            if config.get("over") == "items" and not items:
                self.problem("Join a list to the loop's items", node=ident, port="items")
            if config.get("over") != "items" and items:
                self.problem("Only a loop over items takes the items input", node=ident, port="items")
            for key in ("until", "collect"):
                inner = config.get(key)
                if inner is not None and (inner not in self.nodes or self.nodes[inner].get("parent") != ident):
                    self.problem(f"The loop's {key} is a step inside it", node=ident, field=key)
            if config.get("over") == "until" and config.get("until") in self.nodes:
                watched = self.nodes[config["until"]]
                if TYPES[watched["type"]].trigger:
                    self.problem("The loop's until is a step inside it", node=ident, field="until")
        if config.get("device") == "loop":
            loop = next((self.nodes[s] for s in self.scopes(ident)[:-1]), None)
            if loop is None or loop["config"].get("over") != "phones":
                self.problem('A step runs on "loop" only inside a loop over phones', node=ident, field="device")

    def make_values_schema(self, inputs):
        kinds = {"string": STRING, "number": NUMBER, "boolean": BOOLEAN, "list": array(), "object": {"type": "object"}}
        properties = {starters._key(item["name"]): kinds[item["type"]] for item in inputs}
        for node in self.nodes.values():
            for name, f in TYPES[node["type"]].fields.items():
                if not f.blanks or node["config"].get(name) is None:
                    continue
                for _path, raw in _strings(node["config"][name], ()):
                    for blank in expr.template(raw).blanks:
                        properties.setdefault(starters._key(blank[1]), STRING)
        return {"type": "object", "properties": properties, "additionalProperties": False}

    def schema_of(self, ident, port="out"):
        key = (ident, port)
        if key in self._schemas:
            return self._schemas[key]
        if port == "error":
            return ERROR_OUT
        if key in self._busy:
            return ANY
        self._busy.add(key)
        try:
            node = self.nodes[ident]
            ports = TYPES[node["type"]].outputs(node, Shapes(self, node))
        except (ValueError, ExprError, KeyError):
            ports = {}
        finally:
            self._busy.discard(key)
        for name, schema in ports.items():
            self._schemas[(ident, name)] = schema if isinstance(schema, dict) else ANY
        return self._schemas.get(key, ANY)

    def edge_schema(self, edge):
        schema = self.schema_of(edge["source"], edge["sourcePort"])
        if edge.get("map"):
            env = self.env_for_edge(edge, schema)
            try:
                schema = infer(edge["map"], env)
            except ExprError:
                return ANY
        return schema

    def input_schema(self, ident):
        if ident in self._input:
            return self._input[ident]
        self._input[ident] = ANY
        edges = self.incoming(ident, "in")
        schemas = [self.edge_schema(edge) for edge in edges]
        schema = array() if len(schemas) > 1 else schemas[0] if schemas else ANY
        self._input[ident] = schema
        return schema

    def items_schema(self, ident):
        edges = self.incoming(ident, "items")
        return self.edge_schema(edges[0]) if edges else ANY

    def ancestors(self, ident):
        """Every node that has finished before ``ident`` starts, in any run where both run."""
        out = set()
        for level in self.scopes(ident):
            here = self.lift(ident, level)
            edges = self.level_edges(level)
            before = {a: set() for a, _b in edges}
            for a, b in edges:
                before.setdefault(b, set()).add(a)
            pending = list(before.get(here, ()))
            while pending:
                at = pending.pop()
                if at not in out:
                    out.add(at)
                    pending.extend(before.get(at, ()))
        return out

    def env_for(self, ident):
        loops = self.scopes(ident)[:-1]
        env = {"input": self.input_schema(ident), "inputs": self.values_schema, "vars": self.vars_schema,
               "run": RUN_SCHEMA, "node": {a: self.schema_of(a) for a in self.ancestors(ident)}}
        if loops:
            ports = TYPES["loop"].outputs(self.nodes[loops[0]], Shapes(self, self.nodes[loops[0]]))
            env["item"], env["index"] = ports.get("item", ANY), INTEGER
        return env

    def env_for_edge(self, edge, schema):
        env = self.env_for(edge["target"]) if edge["target"] in self.nodes else {}
        return {**env, "input": schema}

    def trusted(self, ident, roots):
        loops = self.scopes(ident)[:-1]
        allowed = set(TRUSTED_ROOTS)
        if loops and self.nodes[loops[0]]["config"].get("over") in TRUSTED_ITEMS:
            allowed.add("item")
        return roots <= allowed

    def check_expressions(self):
        edges = {edge["id"]: edge for edge in self.edges}
        for owner, path, parsed, instructs, wants in self.expressions:
            if isinstance(owner, tuple):
                edge = edges.get(owner[0])
                if edge is None:
                    continue
                where = {"edge": owner[0], "field": "map"}
                env = self.env_for_edge(edge, self.schema_of(edge["source"], edge["sourcePort"]))
            else:
                where = {"node": owner, "field": ".".join(str(p) for p in path if p != "whole")}
                env = self.env_for(owner)
                if instructs and not self.trusted(owner, parsed.roots):
                    self.problem("Screen text can't become a step's instructions. Pass it as the step's input "
                                 "instead.", **where)
                    continue
            try:
                schema = infer(parsed, env)
            except ExprError as error:
                self.problem(error, **where)
                continue
            kind = schema_kind(schema)
            if wants and kind is not None and kind != wants:
                self.problem(f"This gives {_kind_name(kind)}, and it needs {_kind_name(wants)}", **where)
            if not isinstance(owner, tuple) and path and path[-1] == "whole":
                self.field_types.setdefault(owner, {})[path[:-1]] = schema
        for edge in self.edges:
            if edge["targetPort"] == "items":
                kind = schema_kind(self.edge_schema(edge))
                if kind is not None and kind != "list":
                    self.problem(f"A loop's items is a list, and this edge carries {_kind_name(kind)}. "
                                 "Reshape it with the edge's map.", edge=edge["id"])

    def check_pins(self):
        total = 0
        for node in self.nodes.values():
            pin = node.get("pin")
            if pin is None:
                continue
            if not isinstance(pin, dict) or not pin:
                self.problem("A pin is test data by output port, like {\"out\": ...}", node=node["id"], field="pin")
                continue
            try:
                size = _json_size(pin)
            except (TypeError, ValueError):
                self.problem("A pin is plain JSON", node=node["id"], field="pin")
                continue
            total += size
            if size > MAX_PIN_BYTES:
                self.problem(f"A node's pin is at most {MAX_PIN_BYTES // 1000} KB", node=node["id"], field="pin")
            for port, value in pin.items():
                if port not in self.outputs(node["id"]) or port == "error":
                    self.problem(f"{self.name(node['id'])} has no output called {port}", node=node["id"], field="pin")
                    continue
                kind = schema_kind(self.schema_of(node["id"], port))
                if kind is not None and value is not None and expr.kind_of(value) != kind:
                    self.problem(f"The pin for {port} should be {_kind_name(kind)}", node=node["id"], field="pin")
        if total > MAX_PINS_BYTES:
            self.problem(f"A graph's pins are at most {MAX_PINS_BYTES // 1_000_000} MB in all", field="pins")


def _kind_name(kind):
    return {"null": "null", "boolean": "true or false", "number": "a number", "string": "text", "list": "a list",
            "object": "an object"}.get(kind, "something")


def _schema_of_value(value):
    kind = expr.kind_of(value)
    return {"type": {"list": "array"}.get(kind, kind)}


def _strings(value, path):
    """(path, text) for every text inside a config value."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, path + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, path + (index,))


def _topological(members, edges):
    """``members`` in an order where every edge's source comes first (ties keep their saved order), or None."""
    position = {m: i for i, m in enumerate(members)}
    needs = {m: 0 for m in members}
    after = {m: [] for m in members}
    for a, b in edges:
        if a in needs and b in needs:
            needs[b] += 1
            after[a].append(b)
    ready = sorted((m for m in members if needs[m] == 0), key=position.get)
    out = []
    while ready:
        at = ready.pop(0)
        out.append(at)
        for b in after[at]:
            needs[b] -= 1
            if needs[b] == 0:
                ready.append(b)
                ready.sort(key=position.get)
    return out if len(out) == len(members) else None


def _cycle(members, edges):
    after = {m: [b for a, b in edges if a == m] for m in members}
    state = {}

    def visit(at, trail):
        state[at] = 1
        for b in after.get(at, ()):
            if state.get(b) == 1:
                return trail[trail.index(b):] + [b] if b in trail else [at, b]
            if b not in state:
                found = visit(b, trail + [b])
                if found:
                    return found
        state[at] = 2
        return None

    for m in members:
        if m not in state:
            found = visit(m, [m])
            if found:
                return found[:-1]
    return members[:1]


def coerce(value, kind, name="That input"):
    """A run's value for an input of ``kind``: text read as a number or true or false where the input says so."""
    if kind == "string":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return format(value, "g")
        if not isinstance(value, str):
            raise ValueError(f"{name} is text")
        return value
    if kind == "number":
        if isinstance(value, bool):
            raise ValueError(f"{name} is a number")
        if isinstance(value, (int, float)):
            return value
        try:
            return expr.evaluate("number($input)", {"input": value})
        except ExprError:
            raise ValueError(f"{name} is a number, not {str(value)[:40]!r}") from None
    if kind == "boolean":
        try:
            return expr.evaluate("bool($input)", {"input": value})
        except ExprError:
            raise ValueError(f"{name} is true or false") from None
    if kind == "list":
        if not isinstance(value, list):
            raise ValueError(f"{name} is a list")
        return value
    if not isinstance(value, dict):
        raise ValueError(f"{name} is an object")
    return value


def validate(graph, context=None):
    """The graph as saved, or GraphError listing every problem."""
    return _Validator(graph, context or Context()).run()


def check_graph(graph):
    """Whether ``graph`` (as saved) is a graph of this version: the light check readers make before trusting it."""
    return isinstance(graph, dict) and graph.get("version") == VERSION and isinstance(graph.get("nodes"), list)
