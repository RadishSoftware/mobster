"""A workflow as a file in its project: `.mobster/workflows/<name>.yaml`, written so a pull request's diff reads.

``render(name, graph)`` writes the published graph; ``parse(text, where)`` reads a file back into (name, graph) or
raises FileError naming the line. The graph a file gives still goes through every check a save does (graph/schema.py)
before Mobster takes it. The format, version 1::

    version: 1
    name: Answer DMs
    inputs:
      - {name: who, type: string, default: Sam}
    limits: {maxSpendUsd: 2, maxMinutes: 60}
    nodes:
      - id: start
        type: trigger.manual
        at: [0, 0]
      - id: read
        type: phone.task
        at: [0, 152]
        config:
          text: Open Instagram and read the newest DM from {who}
    edges:
      - {from: start, to: read}
      - {from: check.asked, to: tell}

Keys always come in the same order, a node's config leaves out what is at its default, an edge names its ends as
``node`` or ``node.port`` (``out`` and ``in`` are left out), and a moved node changes one line. What stays on the Mac
and never goes in the file: the workflow's phone, profile and destination, whether its schedule is on, and pins (test
data). ``blocked(graph)`` refuses a graph that names a real iPhone or holds a secret, so neither reaches a repository.
"""

import functools
import hashlib
import re

from ..graph import catalog
from ..secret_filter import redact

VERSION = 1
MAX_BYTES = 512_000
SUFFIXES = (".yaml", ".yml")
SHARED_DEVICES = (None, "any", "loop")
TOP = ("version", "name", "inputs", "vars", "limits", "nodes", "edges")
NODE = ("id", "type", "title", "notes", "parent", "at", "retry", "config")
EDGE = ("from", "to", "map", "id")
HEADER = "# A Mobster workflow. Mobster for Mac keeps this file and its canvas in step: edit either.\n"


class FileError(ValueError):
    """A file that isn't a workflow: one sentence, with the file and the line where there is one."""


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def slug(name, taken=()):
    """The file name for a workflow called ``name``: lowercase words joined by dashes, unique among ``taken``."""
    base = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-")[:60].strip("-") or "workflow"
    candidate, n = f"{base}.yaml", 2
    taken = {name.casefold() for name in taken}
    while candidate in taken:
        candidate, n = f"{base}-{n}.yaml", n + 1
    return candidate


def edge_id(source, source_port, target, target_port):
    """An edge's id when its file gives none: the same ends always give the same id."""
    key = f"{source}.{source_port}>{target}.{target_port}"
    return "e" + hashlib.sha256(key.encode()).hexdigest()[:10]


# -- writing -------------------------------------------------------------------------------------------------------

def _config(node):
    """The node's config without the fields at their default, in the catalog's order."""
    node_type = catalog.TYPES.get(node["type"])
    config = dict(node.get("config") or {})
    if node_type is None:
        return config
    out = {}
    for name, field in node_type.fields.items():
        if name in config:
            value = config.pop(name)
            if field.required or value != field.default:
                out[name] = value
    out.update(config)
    return out


def _end(node, port, default):
    return node if port == default else f"{node}.{port}"


def blocked(graph):
    """Why this graph can't go in a repository file, or None: a step names a real iPhone, or a value is a secret."""
    for node in graph.get("nodes") or ():
        config = node.get("config") or {}
        if config.get("device") not in SHARED_DEVICES:
            return (f"The step {node.get('title') or node['id']} runs on one named iPhone, and a project's files never "
                    "name a real phone. Set it to the workflow's phone, then publish.")
    for text in _strings(graph):
        if redact(text) != text:
            return ("The workflow holds what looks like a key, a password or a card number, and Mobster never writes "
                    "one into a project. Keep it in the project's secrets (.mobster/env.example), then publish.")
    return None


def _strings(value):
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            yield item
        elif isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def document(name, graph):
    """The file's content as data, in the format's order."""
    doc = {"version": VERSION, "name": name}
    if graph.get("inputs"):
        doc["inputs"] = [{key: item[key] for key in ("name", "type", "default", "description", "required")
                          if item.get(key) is not None} for item in graph["inputs"]]
    if graph.get("vars"):
        doc["vars"] = graph["vars"]
    doc["limits"] = {key: _number(value) for key, value in (graph.get("limits") or catalog.DEFAULT_LIMITS).items()}
    nodes = []
    for node in graph.get("nodes") or ():
        item = {"id": node["id"], "type": node["type"]}
        for key in ("title", "notes", "parent"):
            if node.get(key):
                item[key] = node[key]
        position = node.get("position") or {}
        item["at"] = _Flow([_number(position.get("x", 0)), _number(position.get("y", 0))])
        if node.get("retry"):
            item["retry"] = node["retry"]
        config = _config(node)
        if config:
            item["config"] = config
        nodes.append(item)
    doc["nodes"] = nodes
    edges = []
    for edge in graph.get("edges") or ():
        item = {"from": _end(edge["source"], edge["sourcePort"], "out"),
                "to": _end(edge["target"], edge["targetPort"], "in")}
        if edge.get("map") is not None:
            item["map"] = edge["map"]
        edges.append(item)
    doc["edges"] = edges
    return doc


def render(name, graph):
    """The file for a workflow called ``name`` whose published graph is ``graph``."""
    import yaml
    return HEADER + yaml.dump(document(name, graph), Dumper=_dumper(), sort_keys=False, allow_unicode=True, width=120,
                              default_flow_style=False)


def _number(value):
    return int(value) if isinstance(value, float) and value.is_integer() else value


class _Flow(list):
    """A list written on one line: a node's ``at``."""


@functools.cache
def _dumper():
    """yaml's safe dumper, with a node's ``at`` on one line. Built on first use, so yaml loads only when a file is
    written."""
    import yaml

    class Dumper(yaml.SafeDumper):
        pass

    Dumper.add_representer(_Flow, lambda dumper, value: dumper.represent_sequence(
        "tag:yaml.org,2002:seq", value, flow_style=True))
    return Dumper


# -- reading -------------------------------------------------------------------------------------------------------

def parse(text, where, *, previous=None):
    """(name, graph) from a file's text. ``where`` names the file in sentences. ``previous`` is the workflow's graph
    as Mobster has it: its edges keep their ids and its nodes their pins when the file leaves them as they were.
    FileError with what is wrong; the graph is then checked as any save is."""
    import yaml
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise FileError(f"{where} is larger than {MAX_BYTES // 1000} KB.")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        line = f" line {mark.line + 1}" if mark is not None else ""
        raise FileError(f"{where}{line}: {getattr(error, 'problem', None) or 'not valid YAML'}") from None
    except (RecursionError, ValueError):
        raise FileError(f"{where}: nested too deeply") from None
    if not isinstance(data, dict):
        raise FileError(f"{where} is empty or isn't a workflow: it needs version, name, nodes and edges.")
    unknown = [key for key in data if key not in TOP]
    if unknown:
        raise FileError(f"{where}: {unknown[0]!r} isn't a workflow field. A workflow has " + ", ".join(TOP) + ".")
    if data.get("version") != VERSION:
        raise FileError(f"{where}: this is a version {data.get('version')!r} file, and Mobster reads version "
                        f"{VERSION}. Update Mobster, or set version: 1.")
    name = data.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
        raise FileError(f"{where}: name is the workflow's name, 1 to 120 characters.")
    previous_nodes = {node["id"]: node for node in (previous or {}).get("nodes") or ()}
    nodes = []
    for index, item in enumerate(_list(data, "nodes", where)):
        if not isinstance(item, dict):
            raise FileError(f"{where}: node {index + 1} is a mapping with id, type and at.")
        unknown = [key for key in item if key not in NODE]
        if unknown:
            raise FileError(f"{where}: node {item.get('id', index + 1)} has {unknown[0]!r}; a node has "
                            + ", ".join(NODE) + ".")
        node = {key: item[key] for key in ("id", "type", "title", "notes", "parent", "retry") if key in item}
        at = item.get("at", [0, 0])
        if not (isinstance(at, list) and len(at) == 2
                and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in at)):
            raise FileError(f"{where}: node {item.get('id', index + 1)}'s at is [x, y], two numbers.")
        node["position"] = {"x": at[0], "y": at[1]}
        node["config"] = item.get("config") if item.get("config") is not None else {}
        if not isinstance(node.get("id"), str):
            raise FileError(f"{where}: node {index + 1}'s id is text.")
        before = previous_nodes.get(node["id"])
        if before is not None and before.get("type") == node.get("type") and before.get("pin") is not None:
            node["pin"] = before["pin"]
        nodes.append(node)
    ids = {node.get("id") for node in nodes}
    previous_edges = {(e["source"], e["sourcePort"], e["target"], e["targetPort"]): e["id"]
                      for e in (previous or {}).get("edges") or ()}
    edges = []
    for index, item in enumerate(_list(data, "edges", where)):
        if not isinstance(item, dict) or not isinstance(item.get("from"), str) or not isinstance(item.get("to"), str):
            raise FileError(f"{where}: edge {index + 1} is {{from: node, to: node}}, with .port after a node when it "
                            "isn't out or in.")
        unknown = [key for key in item if key not in EDGE]
        if unknown:
            raise FileError(f"{where}: edge {index + 1} has {unknown[0]!r}; an edge has from, to and map.")
        source, source_port = _split(item["from"], "out")
        target, target_port = _split(item["to"], "in")
        for end in (source, target):
            if end not in ids:
                raise FileError(f"{where}: edge {index + 1} joins {end!r}, and no node has that id.")
        key = (source, source_port, target, target_port)
        edge = {"id": item.get("id") or previous_edges.get(key) or edge_id(*key), "source": source,
                "sourcePort": source_port, "target": target, "targetPort": target_port}
        if item.get("map") is not None:
            edge["map"] = item["map"]
        edges.append(edge)
    graph = {"version": catalog.VERSION, "inputs": data.get("inputs") or [], "vars": data.get("vars") or {},
             "nodes": nodes, "edges": edges, "limits": data.get("limits") or dict(catalog.DEFAULT_LIMITS)}
    return " ".join(name.split()), graph


def _list(data, key, where):
    value = data.get(key) if data.get(key) is not None else []
    if not isinstance(value, list):
        raise FileError(f"{where}: {key} is a list.")
    return value


def _split(end, default):
    node, dot, port = end.strip().partition(".")
    return node, (port if dot else default)
