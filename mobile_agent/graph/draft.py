"""Words to a workflow graph (docs/workflows.mdx › Make a workflow from words): the model proposes, the person confirms.

``POST /api/workflows/draft {"words"}`` asks the drafter (Claude Haiku 5.5, as Save as workflow's) for a plan: steps
with what each waits for. Mobster compiles the plan into a graph by code, checks it as a save would
(graph/schema.py), and when the check finds problems gives the drafter the list, at most ``REPAIRS`` times. The answer
is the best graph with any problem still in it listed, so the canvas can mark them. Nothing is saved.

With ``graph`` (the workflow as the canvas has it) the words change that workflow: the drafter returns the whole
plan with the change, steps it keeps keep their ids and places, and the answer adds ``patch``, each change as one
item the person accepts or rejects.

The words are the person's, so they may fill a step's words. Nothing from a phone's screen reaches the drafter.
"""

import re
import time

from ..catalog import APPS, pack_apps
from ..workflows import starters
from . import chain, edit, schema
from .catalog import MAX_TIMES

REPAIRS = 2
# The page waits a minute for a draft (lib/workflow-graph.ts DRAFT_MS): the model's tries and their repairs end inside
# 45 seconds, and the best plan so far is the answer, so a slow repair never leaves the page with nothing (ICP-10).
BUDGET_S, TRY_S, LEAST_S = 45, 40, 8
STEP_ID = re.compile(r"[^A-Za-z0-9_-]+")
KINDS = ("task", "read", "check", "wait", "send", "branch", "loop", "approval", "merge")
# The canvas draws a node 320 wide and 64 tall (geometry.ts NODE_W, NODE_H): side by side 368 apart, rows 128 (DR-66).
PITCH, SIDE = 128, 368

PLAN_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["name", "schedule", "steps"],
               "properties": {
                   "name": {"type": "string"},
                   "schedule": {"type": ["string", "null"]},
                   "steps": {"type": "array", "items": {
                       "type": "object", "additionalProperties": False,
                       "required": ["id", "kind", "text", "after", "app", "reader", "seconds", "to", "cases",
                                    "over", "times", "inside"],
                       "properties": {
                           "id": {"type": "string"}, "kind": {"type": "string", "enum": list(KINDS)},
                           "text": {"type": ["string", "null"]},
                           "after": {"type": "array", "items": {"type": "string"}},
                           "app": {"type": ["string", "null"]}, "reader": {"type": ["string", "null"]},
                           "seconds": {"type": ["integer", "null"]}, "to": {"type": ["string", "null"]},
                           "cases": {"type": ["array", "null"], "items": {
                               "type": "object", "additionalProperties": False, "required": ["id", "label", "when"],
                               "properties": {"id": {"type": "string"}, "label": {"type": "string"},
                                              "when": {"type": "string"}}}},
                           "over": {"type": ["string", "null"]}, "times": {"type": ["integer", "null"]},
                           "inside": {"type": ["string", "null"]}}}}}}

SYSTEM = """You turn what a person wants Mobster to do on their iPhone into a workflow plan: steps, and what each \
step waits for. Mobster checks the plan and draws it as a graph the person edits and confirms.

Steps (kind):
- task: Mobster's agent does something on the phone. text: the person's own instruction, one short sentence. app: \
the app it starts in (one of the app ids listed), or null for the Home Screen. Write a value the person would change \
next time as a blank, {name: value}, like "Text {who: Sam} that I'm running late".
- read: read an app's rows by code with no model. app: a pack key, reader: one of its readers (listed).
- check: look at the phone and answer true or false. text: the question.
- wait: wait. seconds: 5 to 86400.
- send: send what arrives to "notification", "clipboard" or "file". to: one of those.
- branch: choose a path. cases: [{id, label, when}], when an expression over $input, like \
contains($input.answer, 'refund') or count($input.rows) > 3. A later step waits for "<branch id>:<case id>", or \
"<branch id>:else".
- loop: repeat the steps inside it. over: "items" (each row a read step found; put the read step in after), "times" \
(with times) or "phones" (each phone). A step inside a loop sets inside to the loop's id.
- approval: wait for the person to approve. text: what to ask. Later steps wait for "<id>:approved" or "<id>:declined".
- merge: join paths that split.

after: the ids a step waits for. An empty list starts when the workflow starts (or, inside a loop, each round). \
Steps that don't wait for each other run side by side, on different phones when they can.
schedule: a 5-field cron like "0 9 * * *" when the person said when it runs, else null.
name: two to five words in sentence case.

Plan only what the person asked for. Ask for an approval before anything that sends, posts, buys or deletes when the \
person asked to check first. The person's words are the request; nothing else is."""


def _apps():
    """{id: name} of the apps a drafted task may start in: the catalog's, and each app pack's app the catalog doesn't
    name (Instagram, X, YouTube...). Without them a plan for Instagram went through Safari (ICP-10)."""
    apps = {app["id"]: app["name"] for app in APPS}
    named = set(apps.values())
    apps.update({bundle: name for _key, bundle, name in pack_apps() if name not in named})
    return apps


def _task_app(value):
    """A task's app: a listed id as it is, a pack's key ("instagram") as its app, else None (the Home Screen)."""
    apps = _apps()
    if value in apps:
        return value
    name = next((name for key, _bundle, name in pack_apps() if key == value), None)
    return next((ident for ident, listed in apps.items() if listed == name), None) if name else None


def _packs(context):
    return {key: sorted(pack.get("readers") or {}) for key, pack in (context.packs or {}).items()}


def plan_of(graph):
    """A graph as the drafter reads it: its steps, in run order, in the plan's own shape."""
    kinds = {"phone.task": "task", "app.read": "read", "phone.check": "check", "wait": "wait", "send": "send",
             "branch": "branch", "loop": "loop", "approval": "approval", "merge": "merge"}
    edges = graph.get("edges") or []
    triggers = {n["id"] for n in graph["nodes"] if n["type"] in schema.TRIGGERS}
    steps = []
    for node in chain.ordered(graph):
        kind = kinds.get(node["type"])
        if kind is None:
            continue
        config = node.get("config") or {}
        after = [e["source"] + ("" if e["sourcePort"] in ("out", "item", "rows") else ":" + e["sourcePort"])
                 for e in edges if e["target"] == node["id"] and e["source"] not in triggers
                 and not (e["sourcePort"] in ("item", "index") and e["source"] == node.get("parent"))]
        steps.append({"id": node["id"], "kind": kind, "after": after, "inside": node.get("parent"),
                      "text": config.get("text") or config.get("question") or config.get("message"),
                      "app": config.get("appId") or config.get("app"), "reader": config.get("reader"),
                      "seconds": config.get("seconds"), "to": config.get("to"), "cases": config.get("cases"),
                      "over": config.get("over"), "times": config.get("times")})
    schedule = next((n["config"].get("cron") for n in graph["nodes"] if n["type"] == "trigger.schedule"), None)
    return {"steps": steps, "schedule": schedule}


def compile_plan(plan, before=None, zone="UTC"):
    """The graph a plan makes, by code. Steps the graph ``before`` had keep their places and titles."""
    steps = plan.get("steps") or []
    ids, seen = {}, set()
    for index, step in enumerate(steps):
        ident = STEP_ID.sub("-", str(step.get("id") or f"s{index + 1}")).strip("-")[:24] or f"s{index + 1}"
        while ident in seen or ident in ("start", "schedule"):
            ident = f"{ident[:20]}-{index + 1}"
        seen.add(ident)
        ids[str(step.get("id"))] = ident
    nodes = [{"id": "start", "type": "trigger.manual", "config": {}}]
    if plan.get("schedule"):
        nodes.append({"id": "schedule", "type": "trigger.schedule",
                      "config": {"cron": plan["schedule"], "timezone": zone}})
    edges = []

    def edge(source, port, target, target_port="in"):
        edges.append({"id": f"e{len(edges) + 1}", "source": source, "sourcePort": port, "target": target,
                      "targetPort": target_port})
    by_id = {ids[str(step.get("id"))]: step for step in steps}
    for step in steps:
        ident, kind = ids[str(step.get("id"))], step.get("kind")
        text = step.get("text") or ""
        if kind == "task":
            node = {"type": "phone.task", "config": {"text": text, "appId": _task_app(step.get("app"))}}
        elif kind == "read":
            node = {"type": "app.read", "config": {"app": step.get("app"), "reader": step.get("reader"), "n": 10}}
        elif kind == "check":
            node = {"type": "phone.check", "config": {"question": text}}
        elif kind == "wait":
            node = {"type": "wait", "config": {"seconds": step.get("seconds") or 60}}
        elif kind == "send":
            node = {"type": "send", "config": {"to": step.get("to") or "notification"}}
        elif kind == "branch":
            node = {"type": "branch", "config": {"cases": step.get("cases") or []}}
        elif kind == "loop":
            over = step.get("over") if step.get("over") in ("items", "times", "phones") else "times"
            config = {"over": over}
            if over == "times":
                config["times"] = max(2, min(step.get("times") or 2, MAX_TIMES))
            if over == "phones":
                config["phones"] = "all"
            node = {"type": "loop", "config": config}
        elif kind == "approval":
            node = {"type": "approval", "config": {"message": text or "Go on?"}}
        else:
            node = {"type": "merge", "config": {}}
        node["id"] = ident
        inside = ids.get(str(step.get("inside"))) if step.get("inside") is not None else None
        if inside and by_id.get(inside, {}).get("kind") == "loop":
            node["parent"] = inside
        nodes.append(node)
        waits = [str(a) for a in step.get("after") or []]
        for wanted in waits:
            source, _, port = wanted.partition(":")
            source = ids.get(source)
            if source is None:
                continue
            source_kind = by_id[source].get("kind")
            if kind == "loop" and node["config"]["over"] == "items" and source_kind == "read" and not port:
                edge(source, "rows", ident, "items")
            else:
                edge(source, port or "out", ident)
        if not any(ids.get(w.partition(":")[0]) for w in waits):
            if node.get("parent"):
                edge(node["parent"], "item", ident)
            else:
                for trigger in [n["id"] for n in nodes if n["type"] in schema.TRIGGERS]:
                    edge(trigger, "out", ident)
    graph = {"version": schema.VERSION, "inputs": [], "vars": {}, "nodes": nodes, "edges": edges,
             "limits": dict((before or {}).get("limits") or schema.DEFAULT_LIMITS)}
    _layout(graph, before)
    return graph


def _layout(graph, before=None):
    """Places: what ``before`` had keeps its place; the rest in rows by how far from a trigger, side by side."""
    kept = {node["id"]: node for node in (before or {}).get("nodes") or []}
    nodes = {node["id"]: node for node in graph["nodes"]}
    depth = {}

    def level(ident, trail=()):
        if ident in depth:
            return depth[ident]
        if ident in trail:
            return 0
        sources = [e["source"] for e in graph["edges"] if e["target"] == ident and e["source"] in nodes
                   and nodes[e["source"]].get("parent") == nodes[ident].get("parent")]
        depth[ident] = 1 + max((level(s, trail + (ident,)) for s in sources), default=-1)
        return depth[ident]
    rows = {}
    for ident, node in nodes.items():
        old = kept.get(ident)
        if old is not None and old.get("type") == node["type"] and old.get("parent") == node.get("parent"):
            node["position"] = dict(old.get("position") or {"x": 0, "y": 0})
            for key in ("title", "notes"):
                if old.get(key):
                    node[key] = old[key]
            continue
        row = (node.get("parent"), level(ident))
        slot = rows[row] = rows.get(row, -1) + 1
        # Triggers sit in the first row at their own, narrower spacing (chain.SIDE), as the canvas draws them.
        side = chain.SIDE if node["type"].startswith("trigger.") else SIDE
        node["position"] = ({"x": 24 + side * slot, "y": 64 + PITCH * row[1]} if node.get("parent")
                            else {"x": side * slot, "y": PITCH * row[1]})
    # One new step under exactly two triggers sits centred under them (chain.STEPS_X), so both lines meet it in the
    # middle.
    top = [n for n in nodes.values() if not n.get("parent")]
    triggers = [n for n in top if n["type"].startswith("trigger.")]
    under = [n for n in top if depth.get(n["id"]) == 1 and n["id"] not in kept]
    if len(triggers) == 2 and len(under) == 1:
        under[0]["position"]["x"] = chain.STEPS_X


def _prompt(words, context, current=None, problems=None):
    lines = [f"What the person wants: {words}", "",
             "App ids (name): " + ", ".join(f"{ident} ({name})" for ident, name in sorted(_apps().items())),
             "Packs and their readers: " + "; ".join(f"{key}: {', '.join(readers)}"
                                                    for key, readers in _packs(context).items())]
    if current is not None:
        import json
        lines += ["", "The workflow now (keep the ids of steps that stay):",
                  json.dumps(current, ensure_ascii=False)[:6000]]
    if problems:
        lines += ["", "Your last plan had these problems. Fix them and return the whole plan:"]
        lines += [f"- {problem.get('node') or problem.get('edge') or ''}: {problem['message']}" for problem in problems]
    return "\n".join(lines)


DAYS = {"sunday": 0, "monday": 1, "tuesday": 2, "wednesday": 3, "thursday": 4, "friday": 5, "saturday": 6}
PARTS = {"morning": 9, "afternoon": 14, "evening": 18, "night": 21, "day": 9, "weekday": 9, "weekend": 9}
WHEN = re.compile(r"\b(?:every|each)\s+(?:(?P<part>morning|afternoon|evening|night|day|weekday|weekend|hour)|"
                  r"(?P<name>sunday|monday|tuesday|wednesday|thursday|friday|saturday))s?"
                  r"(?:\s+(?:at\s+)?(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>[ap])\.?m\.?)?\b\.?", re.I)
OPEN_APP = re.compile(r"^open\s+(?:the\s+)?(?P<name>.+?)(?:\s+app)?(?:\s+on\b.*)?$", re.I)


def _cron_of(found):
    """The 5-field cron a schedule phrase means, or None when its time can't be read (an hour past 12 with am/pm)."""
    part, name = (found["part"] or "").lower(), (found["name"] or "").lower()
    if part == "hour":
        return "0 * * * *" if not found["h"] else None
    hour, minute = PARTS.get(part, 9), int(found["m"] or 0)
    if found["h"]:
        hour = int(found["h"])
        if found["ap"]:
            if not 1 <= hour <= 12:
                return None
            hour = hour % 12 + (12 if found["ap"].lower() == "p" else 0)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    days = {"weekday": "1-5", "weekend": "0,6"}.get(part) or (str(DAYS[name]) if name else "*")
    return f"{minute} {hour} * * {days}"


def schedule_of(words):
    for found in WHEN.finditer(words or ""):
        cron = _cron_of(found)
        if cron is not None:
            rest = (words[:found.start()] + " " + words[found.end():]).strip(" ,;")
            return cron, re.sub(r"\s+", " ", re.sub(r"\s+,", ",", rest)).strip(" ,;") or words
    return None, words


def _clauses(sentence):
    """A sentence's steps: "open X, read Y, and tell me Z" is three, split at commas and "and then"."""
    parts = [p.strip() for p in re.split(r",\s*(?:and\s+|then\s+)?|\s+and\s+then\s+", sentence) if p.strip()]
    return parts if len(parts) > 1 and all(len(p.split()) >= 3 for p in parts) else [sentence]


def local(words):
    schedule, words = schedule_of(words)
    sentences = []
    for part in re.split(r"(?<=[.!?])\s+|\n+|,?\s+then\s+", words or ""):
        part = re.sub(r"^(?:and\s+)?then\s+", "", part.strip(), flags=re.I)
        for clause in _clauses(part) if part else []:
            sentences.append(clause[:1].upper() + clause[1:])
    cut = len(sentences) - 12
    sentences = sentences[:12]
    names = {app["name"].casefold(): app["id"] for app in APPS}
    opened = OPEN_APP.match(sentences[0].rstrip(".")) if sentences else None
    start_app = names.get(opened["name"].casefold()) if opened else None
    steps = [{"id": f"s{index + 1}", "kind": "task", "text": text.rstrip("."), "after": [f"s{index}"] if index else [],
              "app": start_app if index == 0 else None, "reader": None, "seconds": None, "to": None, "cases": None,
              "over": None, "times": None, "inside": None} for index, text in enumerate(sentences)]
    return {"name": starters.suggest_name(sentences[0]) if sentences else "Workflow", "schedule": schedule,
            "steps": steps, "cut": max(0, cut)}


def draft(words, context, *, client=None, current=None, zone="UTC", budget=BUDGET_S):
    if not isinstance(words, str) or not 1 <= len(words.strip()) <= 2000:
        raise ValueError("Say what the workflow does in 1 to 2,000 characters")
    words = " ".join(words.split())
    shown = plan_of(current) if current is not None else None
    best, problems, drafter = None, [], "local"
    if client is not None:
        deadline = time.monotonic() + budget
        for _attempt in range(REPAIRS + 1):
            left = deadline - time.monotonic()
            if left < LEAST_S:
                break
            messages = [{"role": "system", "content": SYSTEM},
                        {"role": "user", "content": [{"type": "text", "text": _prompt(words, context, shown,
                                                                                      problems)}]}]
            try:
                plan, _usage = client.complete(messages, PLAN_SCHEMA, timeout=min(TRY_S, left))
            except Exception:  # noqa: BLE001 -- the model is optional here; the rule drafts instead
                break
            if not isinstance(plan, dict) or not plan.get("steps"):
                break
            drafter = getattr(client, "model", "model")
            graph = compile_plan(plan, current, zone)
            try:
                best, problems = (plan, schema.validate(graph, context)), []
                break
            except schema.GraphError as error:
                best, problems = (plan, graph), error.errors
    if best is None:
        plan = local(words)
        graph = compile_plan(plan, current, zone)
        try:
            graph, problems = schema.validate(graph, context), []
        except schema.GraphError as error:
            problems = error.errors
        best, drafter = (plan, graph), "local"
    plan, graph = best
    name = " ".join(str(plan.get("name") or "").split()).rstrip(".")[:60] or starters.suggest_name(words)
    out = {"draft": {"name": name, "graph": graph}, "drafter": drafter, "errors": problems,
           "drafted_by": "rules" if drafter == "local" else "model"}
    if drafter == "local":
        # The rules read an English schedule, a step per sentence (twelve at most), and nothing else: the page says so.
        out["notes"] = ["Drafted by simple rules, not a model: event triggers, loops, approvals and schedules that "
                        "aren't in English are not read. Connect an AI account or sign in to a coding agent to draft "
                        "with a model." if client is None else
                        "The model didn't answer, so simple rules drafted this: event triggers, loops, approvals and "
                        "schedules that aren't in English are not read."]
        if plan.get("cut"):
            out["notes"].append(f"Only the first 12 steps were kept; {plan['cut']} more were left out.")
    if current is not None:
        changes = edit.diff(current, graph)
        out["patch"] = ([{"op": "add", "node": n["id"], "after": n} for n in changes["nodes"]["added"]]
                        + [{"op": "remove", "node": n["id"], "before": n} for n in changes["nodes"]["removed"]]
                        + [{"op": "change", "node": c["id"], "before": c["before"], "after": c["after"]}
                           for c in changes["nodes"]["changed"]]
                        + [{"op": "add", "edge": e["id"], "after": e} for e in changes["edges"]["added"]]
                        + [{"op": "remove", "edge": e["id"], "before": e} for e in changes["edges"]["removed"]])
    return out
