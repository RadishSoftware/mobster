"""`mobster workflow show NAME`: print a saved workflow's graph from the running Mobster (client.py)."""

import json
import sys

from ..graph import chain as graph_chain
from ..graph.catalog import TYPES
from .client import Client, NoService, ServiceError

WORDS = ("text", "question", "instruction", "message")   # the field that says what a step does, in the person's words
WORDS_SHOWN = 70
NOT_RUNNING = "Mobster isn't running here. Open Mobster or run `mobster serve`, then try again."


def add_arguments(parser, helpers):
    subs = parser.add_subparsers(dest="workflow_command", metavar="<subcommand>", required=True)
    show = subs.add_parser("show", help="print a workflow's steps in the order they run",
                           formatter_class=parser.formatter_class,
                           description="Print a saved workflow's steps in the order they run: each one's id, type, "
                                       "title and the steps it waits for, with a loop's steps under it. It shows "
                                       "the published version unless you add --draft, and changes nothing.",
                           epilog='examples:\n  mobster workflow show "Answer DMs"\n'
                                  "  mobster workflow show 3f9c2a1b7d0e --draft --json")
    show.add_argument("name", metavar="NAME", help="the workflow's name or id")
    show.add_argument("--draft", action="store_true", help="the draft you're editing, not the published version")
    show.add_argument("--json", action="store_true", help="print the steps as JSON")
    show.add_argument("--url", metavar="URL", help="Mobster's address (default: $MOBSTER_URL, else the running Mac "
                                                   "app's, else http://127.0.0.1:8765)")


def steps(graph):
    """[{id, type, title, after: [{node, port}], parent, depth, words}] in the order they run (graph/chain.ordered)."""
    nodes = {node["id"]: node for node in graph.get("nodes") or ()}
    into = {}
    for edge in graph.get("edges") or ():
        into.setdefault(edge["target"], []).append({"node": edge["source"], "port": edge.get("sourcePort") or "out"})

    def depth(node):
        level, parent = 0, node.get("parent")
        while parent in nodes and level < 10:
            level, parent = level + 1, nodes[parent].get("parent")
        return level
    out = []
    for node in graph_chain.ordered(graph):
        kind, config = node["type"], node.get("config") or {}
        words = next((config[field] for field in WORDS if isinstance(config.get(field), str)), None)
        out.append({"id": node["id"], "type": kind, "title": node.get("title")
                    or getattr(TYPES.get(kind), "label", kind),
                    "after": into.get(node["id"], []), "parent": node.get("parent"), "depth": depth(node),
                    "words": " ".join(words.split()) if words else None})
    return out


def lines(workflow, revision, shown):
    """The text `show` prints: a heading, then one line per step."""
    which = f"published revision {revision}" if revision else "the draft"
    head = f"“{workflow.get('name')}” ({workflow.get('id')}): {which}, {len(shown)} steps"
    width = max((len(step["id"]) + 2 * step["depth"] for step in shown), default=0)
    out = [head]
    for step in shown:
        waits = ", ".join(a["node"] + (f" ({a['port']})" if a["port"] != "out" else "") for a in step["after"])
        line = f"  {'  ' * step['depth']}{step['id']:<{width - 2 * step['depth']}}  {step['title']} ({step['type']})"
        if waits:
            line += f", after {waits}"
        if step["words"]:
            words = step["words"]
            line += f": “{words[:WORDS_SHOWN]}{'…' if len(words) > WORDS_SHOWN else ''}”"
        out.append(line)
    return out


def run(args):
    if args.workflow_command != "show":
        return 2
    client = Client(getattr(args, "url", None))
    try:
        try:
            workflow = client.find_workflow(args.name)
        except LookupError as error:
            return _fail(str(error), 1)
        revision = None if args.draft else (workflow.get("published") or {}).get("revision")
        graph = workflow.get("graph") or {}
        if revision:
            graph = (client.call("GET", f"/api/workflows/{workflow['id']}/versions/{revision}").get("workflow")
                     or {}).get("graph") or {}
    except NoService:
        return _fail(NOT_RUNNING, 3)
    except ServiceError as error:
        return _fail(str(error), 1)
    shown = steps(graph)
    try:
        if args.json:
            print(json.dumps({"workflowId": workflow.get("id"), "name": workflow.get("name"), "revision": revision,
                              "draft": revision is None, "steps": shown}, ensure_ascii=False), flush=True)
        else:
            print("\n".join(lines(workflow, revision, shown)), flush=True)
    except BrokenPipeError:
        pass
    return 0


def _fail(message, code):
    print(f"mobster workflow: {message}", file=sys.stderr, flush=True)
    return code
