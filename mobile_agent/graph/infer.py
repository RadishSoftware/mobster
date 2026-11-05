"""Typing Mobster expressions for the checks a graph gets at save (graph/schema.py): what an expression gives, from
the JSON schemas of the ports and values it reads, and ExprError when an operation in it can only fail."""

from .expr import compile
from .functions import FUNCTIONS
from .values import ExprError, kind_of

ANY = {}


def schema_kind(schema):
    """The one JSON kind a schema allows (list for array), or None when it allows several or says nothing."""
    if not isinstance(schema, dict):
        return None
    kind = schema.get("type")
    if isinstance(kind, list):
        kinds = {"list" if k == "array" else "number" if k == "integer" else k for k in kind}
        return kinds.pop() if len(kinds) == 1 else None
    return {"array": "list", "integer": "number"}.get(kind, kind) if isinstance(kind, str) else None


def _of(kind):
    return {"type": {"list": "array"}.get(kind, kind)} if kind else ANY


def _field(schema, key):
    """What a schema says sits at ``key`` (ANY when it doesn't say). A key the schema shuts out reads null."""
    kind = schema_kind(schema)
    if kind == "object" and isinstance(key, str):
        properties = schema.get("properties") or {}
        if key in properties:
            return properties[key]
        if schema.get("additionalProperties") is False:
            return {"type": "null"}
        extra = schema.get("additionalProperties")
        return extra if isinstance(extra, dict) else ANY
    if kind == "list" and not isinstance(key, str):
        return schema.get("items") or ANY
    if kind == "null":
        return {"type": "null"}
    if kind in ("number", "boolean"):
        raise ExprError(f"Can't read {key!r} from {'a number' if kind == 'number' else 'a true or false'}")
    return ANY


def _sure(kind_a, kind_b, kinds):
    """Whether two known kinds can't both be among ``kinds`` (so an operation can only fail)."""
    return kind_a is not None and kind_a not in kinds or kind_b is not None and kind_b not in kinds


class _Typing:
    def __init__(self, env):
        self.env = env

    def type(self, tree):
        head = tree[0]
        if head == "lit":
            return _of(kind_of(tree[1]))
        if head == "root":
            if tree[1] not in self.env:
                raise ExprError(f"${tree[1]} isn't available here" + (" (only inside a loop)"
                                                                      if tree[1] in ("item", "index") else ""))
            return self.env[tree[1]] if isinstance(self.env[tree[1]], dict) else ANY
        if head == "node":
            nodes = self.env.get("node") or {}
            if tree[1] not in nodes:
                raise ExprError(f"$node.{tree[1]} isn't a step that runs before this one")
            return nodes[tree[1]] if isinstance(nodes[tree[1]], dict) else ANY
        if head == "get":
            base = self.type(tree[1])
            self.type(tree[2])
            if tree[2][0] == "lit":
                return _field(base, tree[2][1])
            return ANY
        if head == "list":
            for item in tree[1]:
                self.type(item)
            return _of("list")
        if head == "object":
            return {"type": "object", "properties": {key: self.type(value) for key, value in tree[1]}}
        if head == "??":
            left, right = self.type(tree[1]), self.type(tree[2])
            return right if schema_kind(left) == "null" else left if schema_kind(left) == schema_kind(right) else ANY
        if head in ("and", "or", "not"):
            for part in tree[1:]:
                kind = schema_kind(self.type(part))
                if kind is not None and kind != "boolean":
                    raise ExprError(f"{head} takes true or false, and this side is {_kind_name(kind)}")
            return _of("boolean")
        if head == "neg":
            kind = schema_kind(self.type(tree[1]))
            if kind is not None and kind != "number":
                raise ExprError(f"- needs a number, and this is {_kind_name(kind)}")
            return _of("number")
        if head in ("==", "!="):
            self.type(tree[1])
            self.type(tree[2])
            return _of("boolean")
        if head in ("<", "<=", ">", ">="):
            a, b = schema_kind(self.type(tree[1])), schema_kind(self.type(tree[2]))
            if _sure(a, b, ("number", "string")) or (a and b and a != b):
                raise ExprError(f"{head} compares two numbers or two texts, not {_kind_name(a)} and {_kind_name(b)}")
            return _of("boolean")
        if head in ("in", "contains"):
            self.type(tree[1])
            container = schema_kind(self.type(tree[2]))
            if container is not None and container not in ("list", "object", "string"):
                raise ExprError(f"in looks inside a list, an object or text, not {_kind_name(container)}")
            return _of("boolean")
        if head == "+":
            a, b = schema_kind(self.type(tree[1])), schema_kind(self.type(tree[2]))
            if _sure(a, b, ("number", "string", "list")) or (a and b and a != b):
                raise ExprError(f"+ adds two numbers, or joins two texts or two lists, not {_kind_name(a)} and "
                                f"{_kind_name(b)}")
            return _of(a) if a is not None and a == b else ANY
        if head in ("-", "*", "/", "%"):
            a, b = schema_kind(self.type(tree[1])), schema_kind(self.type(tree[2]))
            if _sure(a, b, ("number",)):
                raise ExprError(f"{head} needs two numbers, not {_kind_name(a)} and {_kind_name(b)}")
            return _of("number")
        if head == "call":
            for arg in tree[2]:
                self.type(arg)
            if tree[1] == "if":
                return ANY
            kind = FUNCTIONS[tree[1]][3]
            return _of(kind)
        return ANY


def _kind_name(kind):
    return {"null": "null", "boolean": "true or false", "number": "a number", "string": "text", "list": "a list",
            "object": "an object", None: "something"}[kind]


def infer(expr, env):
    if isinstance(expr, str):
        expr = compile(expr)
    return _Typing(env).type(expr.tree)


RUN_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "id": {"type": "string"}, "workflowId": {"type": "string"}, "trigger": {"type": "string"},
    "startedAt": {"type": "integer"}, "test": {"type": "boolean"}}}
