"""A chain of step cards is a linear graph (docs/workflows.mdx › The graph).

``from_chain`` turns a workflow's cards (``steps``, ``repeat``, ``limits``, saved by version 3 records;
workflows/records.py's format) and its schedule into a graph of version 2. ``as_chain`` reads a graph back as cards, or
None when the graph says something cards can't. The two are exact inverses on every valid chain, so no saved workflow
loses anything:

- the triggers: a manual one always (``start``), a schedule one when the workflow has a cron;
- each card, in order, as a node joined to the one before it (the first to every trigger);
- a ``repeat`` as a loop holding every card: ``times``, ``rows`` (each), or ``until`` with a last ``phone.check``.

A chain-shaped graph is what the card builder can still open; ``steps``, ``repeat`` and ``limits`` in the API view
come from here. A plain graph (one task, no repeat) runs as one task, exactly as a one-task workflow always has.
"""

from . import schema

PITCH = 128
CHILD = {"x": 24, "y": 64}
# The triggers share the first row, side by side, so the manual start and the schedule read as two ways in, never as two
# steps in a row (hillclimb, 26 Oct). A trigger's node is 224 wide on the canvas (lib/workflow-graph.ts › TRIGGER_W), so
# two fit at 1024; the canvas's own builders put the schedule at this x too (TRIGGER_SIDE).
SIDE = 272
# The steps' column under two triggers: a step's 320 centred under the row (lib/workflow-graph.ts › STEPS_UNDER_TWO).
STEPS_X = (SIDE + 224 - 320) // 2


def _unique(wanted, taken):
    ident, n = wanted, 1
    while ident in taken:
        n += 1
        ident = f"{wanted}-{n}"
    taken.add(ident)
    return ident


def _node(card):
    kind = card["kind"]
    if kind == "task":
        return "phone.task", {"text": card["text"], "appId": card.get("appId"), "device": None, "account": None,
                              "mode": "auto", "outputSchema": None, "input": None}
    if kind == "taught":
        return "phone.taught", {"workflowId": card["workflowId"], "device": None, "account": None}
    if kind == "wait":
        return "wait", {"seconds": card["seconds"], "until": None}
    if kind == "send":
        return "send", {"to": card["to"], "destination": card.get("destination"), "text": None,
                        "what": card.get("what", "last")}
    if kind == "connector":
        return "connector.call", {"connector": card["connector"], "tool": card["tool"],
                                  "arguments": card.get("arguments") or {}}
    if kind == "brief":
        return "brief", {"title": card.get("title")}
    raise ValueError(f"A {kind} step has no node yet")


def from_chain(steps, repeat=None, limits=None, cron=None, timezone=None):
    """The graph a workflow's cards make. ``steps`` and ``repeat`` as batch.validate saves them."""
    taken = {card["id"] for card in steps}
    nodes, edges = [], []
    column = 1

    def place(node, parent=None, index=None):
        nonlocal column
        if node["type"].startswith("trigger."):
            node["position"] = {"x": SIDE * sum(1 for n in nodes if n["type"].startswith("trigger.")), "y": 0}
        elif parent is None:
            node["position"] = {"x": STEPS_X if cron else 0, "y": PITCH * column}
            column += 1
        else:
            node["position"] = {"x": CHILD["x"], "y": CHILD["y"] + PITCH * index}
            node["parent"] = parent
        nodes.append(node)
        return node["id"]

    def edge(source, target, source_port="out"):
        edges.append({"id": f"e{len(edges) + 1}", "source": source, "sourcePort": source_port, "target": target,
                      "targetPort": "in"})

    triggers = [place({"id": _unique("start", taken), "type": "trigger.manual", "config": {}})]
    if cron:
        triggers.append(place({"id": _unique("schedule", taken), "type": "trigger.schedule",
                               "config": {"cron": cron, "timezone": timezone}}))
    loop = None
    if repeat:
        kind = repeat["kind"]
        config = {"over": {"times": "times", "each": "rows", "until": "until"}[kind], "times": None, "rows": None,
                  "until": None, "path": None, "phones": None, "max": None, "concurrency": 1, "collect": None}
        if kind == "times":
            config["times"] = repeat["times"]
        elif kind == "each":
            config.update(rows=repeat.get("rows"), max=repeat["max"])
        else:
            config.update(path="/observed", max=repeat["max"])
        loop = place({"id": _unique("repeat", taken), "type": "loop", "config": config})
        for trigger in triggers:
            edge(trigger, loop)
    previous = None
    for index, card in enumerate(steps):
        kind, config = _node(card)
        ident = place({"id": card["id"], "type": kind, "config": config}, loop, index)
        if previous is not None:
            edge(previous, ident)
        elif loop is not None:
            edge(loop, ident, "item")
        else:
            for trigger in triggers:
                edge(trigger, ident)
        previous = ident
    if repeat and repeat["kind"] == "until":
        check = place({"id": _unique("until", taken), "type": "phone.check",
                       "config": {"question": repeat["check"], "device": None, "account": None}}, loop, len(steps))
        edge(previous, check)
        next(n for n in nodes if n["id"] == loop)["config"]["until"] = check
    return {"version": schema.VERSION, "inputs": [], "vars": {}, "nodes": nodes, "edges": edges,
            "limits": dict(limits or schema.DEFAULT_LIMITS)}


def _card(node):
    """The card a node is, or None when it says more than a card can."""
    kind, config, ident = node["type"], node.get("config") or {}, node["id"]
    if node.get("pin") is not None or node.get("retry") is not None:
        return None
    if kind == "phone.task":
        if (config.get("device"), config.get("account"), config.get("mode", "auto"), config.get("outputSchema"),
                config.get("input"), config.get("expect")) != (None, None, "auto", None, None, None):
            return None
        return {"id": ident, "kind": "task", "text": config["text"], "appId": config.get("appId")}
    if kind == "phone.taught":
        if config.get("device") is not None or config.get("account") is not None:
            return None
        return {"id": ident, "kind": "taught", "workflowId": config["workflowId"]}
    if kind == "wait":
        if config.get("until") is not None:
            return None
        return {"id": ident, "kind": "wait", "seconds": config["seconds"]}
    if kind == "send":
        if config.get("what") not in ("last", "all") or config.get("text") is not None:
            return None
        card = {"id": ident, "kind": "send", "to": config["to"], "what": config["what"]}
        if config.get("destination") is not None:
            card["destination"] = config["destination"]
        return card
    if kind == "connector.call":
        return {"id": ident, "kind": "connector", "connector": config["connector"], "tool": config["tool"],
                "arguments": config.get("arguments") or {}}
    if kind == "brief":
        return {"id": ident, "kind": "brief", **({"title": config["title"]} if config.get("title") else {})}
    return None


def _path(first, members, edges):
    """``members`` in order along single out→in edges from ``first``, or None when they aren't one path."""
    after = {}
    for edge in edges:
        if edge.get("map") or edge["sourcePort"] != "out" or edge["targetPort"] != "in":
            return None
        if edge["source"] in after:
            return None
        after[edge["source"]] = edge["target"]
    order, at = [], first
    while at is not None:
        if at in order or at not in members:
            return None
        order.append(at)
        at = after.get(at)
    return order if len(order) == len(members) else None


def as_chain(graph):
    """(steps, repeat, limits, cron, timezone) the graph's cards are, or None when it isn't chain-shaped."""
    if not schema.check_graph(graph) or graph.get("inputs") or graph.get("vars"):
        return None
    nodes = {node["id"]: node for node in graph["nodes"]}
    triggers = [n for n in graph["nodes"] if n["type"] in schema.TRIGGERS]
    kinds = sorted(n["type"] for n in triggers)
    if kinds not in (["trigger.manual"], ["trigger.manual", "trigger.schedule"]):
        return None
    if any(n.get("pin") is not None for n in triggers):
        return None
    if any(n["type"] == "trigger.schedule" and (n["config"].get("missed", "skip"), n["config"].get("overlap", "skip"))
           != ("skip", "skip") for n in triggers):
        return None
    top = [n for n in graph["nodes"] if n.get("parent") is None and n["type"] not in schema.TRIGGERS]
    edges = graph.get("edges") or []
    trigger_ids = {n["id"] for n in triggers}
    from_triggers = [e for e in edges if e["source"] in trigger_ids]
    targets = {e["target"] for e in from_triggers}
    if len(targets) != 1 or len(from_triggers) != len(triggers) or any(
            e.get("map") or e["sourcePort"] != "out" or e["targetPort"] != "in" for e in from_triggers):
        return None
    first = targets.pop()
    schedule = next((n for n in triggers if n["type"] == "trigger.schedule"), None)
    cron = schedule["config"].get("cron") if schedule else None
    zone = schedule["config"].get("timezone") if schedule else None
    rest = [e for e in edges if e["source"] not in trigger_ids]
    repeat = None
    if nodes[first]["type"] == "loop":
        loop = nodes[first]
        if len(top) != 1:
            return None
        config = loop["config"]
        body = [n for n in graph["nodes"] if n.get("parent") == loop["id"]]
        if (any(n["type"] == "loop" for n in body) or not body or config.get("collect")
                or config.get("concurrency", 1) != 1):
            return None
        starts = [e for e in rest if e["source"] == loop["id"]]
        if len(starts) != 1 or starts[0]["sourcePort"] != "item" or starts[0]["targetPort"] != "in" or starts[0].get(
                "map"):
            return None
        inner = [e for e in rest if e["source"] != loop["id"]]
        order = _path(starts[0]["target"], {n["id"] for n in body}, inner)
        if order is None:
            return None
        over = config.get("over")
        if over == "times":
            repeat = {"kind": "times", "times": config["times"]}
        elif over == "rows":
            repeat = {"kind": "each", "rows": config.get("rows"), "max": config["max"]}
        elif over == "until":
            check = nodes.get(config.get("until"))
            if (check is None or check["type"] != "phone.check" or order[-1] != check["id"]
                    or config.get("path", "/observed") != "/observed" or check.get("pin") is not None
                    or check["config"].get("device") is not None or check["config"].get("account") is not None):
                return None
            repeat = {"kind": "until", "check": check["config"]["question"], "max": config["max"]}
            order = order[:-1]
        else:
            return None
    else:
        order = _path(first, {n["id"] for n in top}, rest)
        if order is None:
            return None
    steps = [_card(nodes[ident]) for ident in order]
    if not steps or any(card is None for card in steps):
        return None
    return steps, repeat, dict(graph.get("limits") or schema.DEFAULT_LIMITS), cron, zone


def is_plain(graph):
    """One task with the defaults and no repeat: it runs as a single task, as a one-task workflow always has."""
    chain = as_chain(graph)
    return chain is not None and chain[1] is None and len(chain[0]) == 1 and chain[0][0]["kind"] == "task"


def words(graph):
    chain = as_chain(graph)
    if chain is not None:
        from ..workflows import records as batch
        return batch.summary_of(chain[0], chain[1])
    lines = []
    for field, kind in (("text", "phone.task"), ("question", "phone.check"), ("instruction", "ai.ask")):
        for node in ordered(graph):
            if node["type"] == kind and isinstance(node["config"].get(field), str):
                lines.append(node["config"][field])
    return "\n".join(line for line in lines if line.strip())


def ordered(graph):
    """The graph's nodes, each level in run order with a loop's body right after the loop (saved order on ties)."""
    nodes = {node["id"]: node for node in graph.get("nodes") or []}
    out = []

    def level(parent):
        members = [n["id"] for n in graph["nodes"] if n.get("parent") == parent]
        inside = {m for m in nodes if _within(nodes, m, parent)}
        edges = set()
        for edge in graph.get("edges") or []:
            a, b = _lift(nodes, edge["source"], parent), _lift(nodes, edge["target"], parent)
            if a and b and a != b and edge["source"] in inside and edge["target"] in inside:
                edges.add((a, b))
        for ident in schema._topological(members, edges) or members:
            out.append(nodes[ident])
            if nodes[ident]["type"] == "loop":
                level(ident)

    level(None)
    return out


def _within(nodes, ident, parent):
    at = ident
    while at is not None:
        if nodes[at].get("parent") == parent:
            return True
        at = nodes[at].get("parent")
    return False


def _lift(nodes, ident, parent):
    at = ident
    while at is not None:
        if nodes[at].get("parent") == parent:
            return at
        at = nodes[at].get("parent")
    return None


def record_is_plain(record):
    published = record.get("published") if isinstance(record, dict) else None
    graph = published.get("graph") if isinstance(published, dict) else (
        record.get("graph") if isinstance(record, dict) else None)
    if schema.check_graph(graph):
        return is_plain(graph)
    from ..workflows import records as batch
    return batch.is_plain(record.get("steps"), record.get("repeat")) if isinstance(record, dict) else False
