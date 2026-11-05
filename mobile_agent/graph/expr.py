"""Mobster expressions: the small, pure language a workflow graph maps data with (docs/workflows.mdx › Expressions).

An expression is written ``{= ... }`` inside a node's text, as an edge's ``map`` or as a branch's condition::

    {= count($node.read.rows) > 0 and $inputs.who != "" }

It reads JSON values and nothing else. There are no imports, no attribute access on objects, no I/O, no clock and no
randomness, so no code runs on the Mac. Every expression is bounded: ``MAX_SOURCE`` characters, ``MAX_NODES`` syntax
nodes, ``MAX_STEPS`` evaluation steps, results of ``MAX_RESULT`` bytes as JSON, lists of ``MAX_ITEMS``.

- Roots: ``$input`` (the node's input, or the source port's value in an edge's map), ``$inputs`` (the run's input
  values), ``$vars`` (the graph's constants), ``$node.<id>`` (an ancestor's output), ``$item`` and ``$index`` (in a
  loop body) and ``$run``.
- A missing key or index reads as ``null``, and so does anything read from ``null``. An operation that can't apply is
  an error: a number operation on text or null, comparing text with a number, dividing by zero, ``and`` on text.
  Errors fail the node that evaluates them; nothing guesses past one (fail closed).

``{name}`` and ``{name: default}`` in the same text stay the workflow's blanks (workflows/starters.py), and ``{{`` and
``}}`` stay escaped braces, so every text saved before expressions means what it meant.

The values it works on and their rules are graph/values.py, its functions graph/functions.py, and the typing a graph
gets at save (an operation that can only fail is refused there) graph/infer.py.
"""

import json
import re
from dataclasses import dataclass

from ..workflows import starters
from .functions import FUNCTIONS
from .values import (
    COMPARE,
    MAX_DEPTH,
    MAX_ITEMS,
    MAX_NODES,
    MAX_RESULT,
    MAX_SOURCE,
    MAX_STEPS,
    ExprError,
    arith,
    compare,
    kind_of,
    lookup,
    need,
    text_of,
)

__all__ = ["ExprError", "Expr", "Template", "compile", "evaluate", "template", "render", "kind_of"]

ROOTS = ("input", "inputs", "vars", "node", "item", "index", "run")
KEYWORDS = {"and", "or", "not", "in", "true", "false", "null"}


TOKEN = re.compile(r"""
    (?P<space>\s+)
  | (?P<number>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
  | (?P<string>"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')
  | (?P<root>\$[A-Za-z_][A-Za-z0-9_]*)
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op>\?\?|==|!=|<=|>=|[<>+\-*/%()\[\]{},.:])
""", re.VERBOSE)
ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"', "/": "/"}


def _string(raw, at):
    out, i, body = [], 0, raw[1:-1]
    while i < len(body):
        char = body[i]
        if char != "\\":
            out.append(char)
            i += 1
            continue
        code = body[i + 1] if i + 1 < len(body) else ""
        if code in ESCAPES:
            out.append(ESCAPES[code])
            i += 2
        elif code == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", body[i + 2:i + 6]):
            out.append(chr(int(body[i + 2:i + 6], 16)))
            i += 6
        else:
            raise ExprError(f"Unknown escape \\{code} in text", at + i + 1)
    return "".join(out)


def _tokens(source):
    out, at = [], 0
    while at < len(source):
        match = TOKEN.match(source, at)
        if match is None:
            raise ExprError(f"Mobster expressions don't use {source[at]!r}", at)
        kind = match.lastgroup
        text = match.group(kind)
        if kind == "number":
            out.append(("lit", float(text) if any(c in text for c in ".eE") else int(text), at))
        elif kind == "string":
            out.append(("lit", _string(text, at), at))
        elif kind == "root":
            if text[1:] not in ROOTS:
                raise ExprError(f"{text} isn't a root. Use one of " + ", ".join("$" + r for r in ROOTS), at)
            out.append(("root", text[1:], at))
        elif kind == "name":
            if text in ("true", "false", "null"):
                out.append(("lit", {"true": True, "false": False, "null": None}[text], at))
            else:
                out.append(("kw" if text in KEYWORDS else "name", text, at))
        elif kind == "op":
            out.append(("op", text, at))
        at = match.end()
    out.append(("end", None, len(source)))
    return out


class _Parser:
    """Recursive descent over the grammar in docs/workflows.mdx › Expressions. A tree is nested tuples."""

    def __init__(self, source):
        self.source = source
        self.tokens = _tokens(source)
        self.at = 0
        self.count = 0
        self.depth = 0

    def peek(self, kind=None, value=None):
        token = self.tokens[self.at]
        if kind is not None and token[0] != kind:
            return None
        if value is not None and token[1] != value:
            return None
        return token

    def take(self, kind=None, value=None):
        token = self.peek(kind, value)
        if token is None:
            found = self.tokens[self.at]
            wanted = repr(value) if value is not None else kind
            seen = "the end" if found[0] == "end" else repr(found[1])
            raise ExprError(f"Expected {wanted} but found {seen}", found[2])
        self.at += 1
        return token

    def node(self, *parts):
        self.count += 1
        if self.count > MAX_NODES:
            raise ExprError(f"An expression has at most {MAX_NODES} parts")
        return parts

    def nested(self, method):
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise ExprError(f"An expression nests at most {MAX_DEPTH} deep")
        try:
            return method()
        finally:
            self.depth -= 1

    def parse(self):
        tree = self.expr()
        self.take("end")
        return tree

    def expr(self):
        left = self.nested(self.or_)
        while self.peek("op", "??"):
            self.take()
            left = self.node("??", left, self.nested(self.or_))
        return left

    def or_(self):
        left = self.and_()
        while self.peek("kw", "or"):
            self.take()
            left = self.node("or", left, self.and_())
        return left

    def and_(self):
        left = self.not_()
        while self.peek("kw", "and"):
            self.take()
            left = self.node("and", left, self.not_())
        return left

    def not_(self):
        if self.peek("kw", "not"):
            self.take()
            return self.node("not", self.nested(self.not_))
        return self.cmp()

    def cmp(self):
        left = self.add()
        token = self.peek()
        if (token[0] == "op" and token[1] in COMPARE) or (token[0] == "kw" and token[1] == "in"):
            self.take()
            left = self.node(token[1], left, self.add())
            after = self.peek()
            if (after[0] == "op" and after[1] in COMPARE) or (after[0] == "kw" and after[1] == "in"):
                raise ExprError("Chain comparisons with and: a < b and b < c", after[2])
        return left

    def add(self):
        left = self.mul()
        while self.peek("op", "+") or self.peek("op", "-"):
            op = self.take()[1]
            left = self.node(op, left, self.mul())
        return left

    def mul(self):
        left = self.unary()
        while self.peek("op", "*") or self.peek("op", "/") or self.peek("op", "%"):
            op = self.take()[1]
            left = self.node(op, left, self.unary())
        return left

    def unary(self):
        if self.peek("op", "-"):
            self.take()
            return self.node("neg", self.nested(self.unary))
        return self.postfix()

    def postfix(self):
        tree = self.primary()
        while True:
            if self.peek("op", "."):
                self.take()
                token = self.peek()
                if token[0] not in ("name", "kw"):
                    raise ExprError("A name goes after the dot", token[2])
                self.take()
                tree = self.node("get", tree, ("lit", token[1]))
            elif self.peek("op", "["):
                self.take()
                tree = self.node("get", tree, self.nested(self.expr))
                self.take("op", "]")
            else:
                return tree

    def primary(self):
        token = self.peek()
        kind, value, at = token
        if kind == "lit":
            self.take()
            return self.node("lit", value)
        if kind == "root":
            self.take()
            if value != "node":
                return self.node("root", value)
            # $node.<id> or $node["id"]: the node is named in the text, so the graph can check it is an ancestor.
            if self.peek("op", "."):
                self.take()
                name = self.peek()
                if name[0] not in ("name", "kw"):
                    raise ExprError("Name the node: $node.read or $node[\"read-dms\"]", name[2])
                self.take()
                return self.node("node", name[1])
            if self.peek("op", "["):
                self.take()
                name = self.take("lit")
                if not isinstance(name[1], str):
                    raise ExprError("Name the node in quotes: $node[\"read-dms\"]", name[2])
                self.take("op", "]")
                return self.node("node", name[1])
            raise ExprError("Name the node: $node.read", at)
        if kind == "name":
            self.take()
            if value not in FUNCTIONS:
                raise ExprError(f"{value} isn't a function or a root. Roots start with $, like $input", at)
            self.take("op", "(")
            args = []
            if not self.peek("op", ")"):
                args.append(self.nested(self.expr))
                while self.peek("op", ","):
                    self.take()
                    args.append(self.nested(self.expr))
            self.take("op", ")")
            low, high = FUNCTIONS[value][0], FUNCTIONS[value][1]
            if not low <= len(args) <= high:
                count = f"{low}" if low == high else f"{low} to {high}"
                raise ExprError(f"{value}() takes {count} values, not {len(args)}", at)
            return self.node("call", value, tuple(args))
        if kind == "op" and value == "(":
            self.take()
            tree = self.nested(self.expr)
            self.take("op", ")")
            return tree
        if kind == "op" and value == "[":
            self.take()
            items = []
            if not self.peek("op", "]"):
                items.append(self.nested(self.expr))
                while self.peek("op", ","):
                    self.take()
                    items.append(self.nested(self.expr))
            self.take("op", "]")
            return self.node("list", tuple(items))
        if kind == "op" and value == "{":
            self.take()
            pairs = []
            if not self.peek("op", "}"):
                while True:
                    key = self.take("lit")
                    if not isinstance(key[1], str):
                        raise ExprError("An object's keys are text in quotes", key[2])
                    self.take("op", ":")
                    pairs.append((key[1], self.nested(self.expr)))
                    if not self.peek("op", ","):
                        break
                    self.take()
            self.take("op", "}")
            return self.node("object", tuple(pairs))
        if kind == "end":
            raise ExprError("The expression ends too soon", at)
        raise ExprError(f"Didn't expect {value!r} here", at)


@dataclass(frozen=True)
class Expr:
    """A parsed expression: its source, its tree, the roots it reads, the nodes it names and its functions."""
    source: str
    tree: tuple
    roots: frozenset
    nodes: frozenset
    functions: frozenset


def compile(source):  # noqa: A001 -- the module's verb, as re.compile
    """The parsed expression, or ExprError with where it went wrong."""
    if not isinstance(source, str) or not source.strip():
        raise ExprError("Write an expression")
    if len(source) > MAX_SOURCE:
        raise ExprError(f"An expression is at most {MAX_SOURCE:,} characters")
    tree = _Parser(source).parse()
    roots, nodes, functions = set(), set(), set()
    pending = [tree]
    while pending:
        part = pending.pop()
        if not isinstance(part, tuple) or not part:
            continue
        head = part[0]
        if head == "root":
            roots.add(part[1])
        elif head == "node":
            roots.add("node")
            nodes.add(part[1])
        elif head == "call":
            functions.add(part[1])
            pending.extend(part[2])
            continue
        elif head == "object":
            pending.extend(value for _key, value in part[1])
            continue
        elif head == "list":
            pending.extend(part[1])
            continue
        if head != "lit":
            pending.extend(p for p in part[1:] if isinstance(p, tuple))
    return Expr(source, tree, frozenset(roots), frozenset(nodes), frozenset(functions))


class _Evaluation:
    def __init__(self, scope, zone):
        self.scope = scope
        self.zone = zone
        self.steps = 0

    def spend(self, count=1):
        self.steps += count
        if self.steps > MAX_STEPS:
            raise ExprError(f"The expression took more than {MAX_STEPS:,} steps")

    def run(self, tree):
        self.spend()
        head = tree[0]
        if head == "lit":
            return tree[1]
        if head == "root":
            if tree[1] not in self.scope:
                raise ExprError(f"${tree[1]} isn't available here" + (" (only inside a loop)"
                                                                      if tree[1] in ("item", "index") else ""))
            return self.scope[tree[1]]
        if head == "node":
            nodes = self.scope.get("node") or {}
            if tree[1] not in nodes:
                raise ExprError(f"$node.{tree[1]} hasn't run before this step")
            return nodes[tree[1]]
        if head == "get":
            return lookup(self.run(tree[1]), self.run(tree[2]))
        if head == "list":
            if len(tree[1]) > MAX_ITEMS:
                raise ExprError(f"A list holds at most {MAX_ITEMS:,} items")
            return [self.run(item) for item in tree[1]]
        if head == "object":
            return {key: self.run(value) for key, value in tree[1]}
        if head == "??":
            left = self.run(tree[1])
            return self.run(tree[2]) if left is None else left
        if head in ("and", "or"):
            left = need(self.run(tree[1]), ("boolean",), head)
            if (head == "and" and not left) or (head == "or" and left):
                return left
            return need(self.run(tree[2]), ("boolean",), head)
        if head == "not":
            return not need(self.run(tree[1]), ("boolean",), "not")
        if head == "neg":
            return -need(self.run(tree[1]), ("number",), "-")
        if head in COMPARE:
            return compare(head, self.run(tree[1]), self.run(tree[2]))
        if head in ("+", "-", "*", "/", "%"):
            return arith(head, self.run(tree[1]), self.run(tree[2]))
        if head == "call":
            return self.call(tree[1], tree[2])
        raise ExprError(f"Unknown expression part {head}")  # a tree this module didn't make

    def call(self, name, args):
        if name == "if":
            condition = need(self.run(args[0]), ("boolean",), "if()")
            return self.run(args[1] if condition else args[2])
        values = [self.run(arg) for arg in args]
        for value in values:
            # Work over a list or object costs a step an item; text costs one per 256 characters.
            if isinstance(value, (list, dict)):
                self.spend(len(value))
            elif isinstance(value, str):
                self.spend(len(value) // 256)
        if name == "date":
            return FUNCTIONS[name][2](*values, default_zone=self.zone)
        return FUNCTIONS[name][2](*values)


def evaluate(expr, scope, *, timezone=None):
    if isinstance(expr, str):
        expr = compile(expr)
    try:
        value = _Evaluation(scope, timezone).run(expr.tree)
    except RecursionError:
        raise ExprError("The value nests too deep") from None
    try:
        size = len(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
    except (ValueError, TypeError):
        raise ExprError("The expression gave a value that isn't JSON") from None
    if size > MAX_RESULT:
        raise ExprError("The expression gave more than 1 MB")
    return value


@dataclass(frozen=True)
class Template:
    parts: tuple

    @property
    def expressions(self):
        return [part[1] for part in self.parts if part[0] == "expr"]

    @property
    def whole(self):
        exprs = self.expressions
        if len(exprs) == 1 and all(part[0] == "expr" or not part[1].strip() for part in self.parts):
            return exprs[0]
        return None

    @property
    def blanks(self):
        out = []
        for part in self.parts:
            if part[0] == "text":
                out.extend(segment for segment in starters.parse(part[1]) if segment[0] == "blank")
        return out


def _expression_end(text, start):
    depth, at, quote = 0, start, None
    while at < len(text):
        char = text[at]
        if quote:
            if char == "\\":
                at += 2
                continue
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            if depth == 0:
                return at
            depth -= 1
        at += 1
    raise ExprError("An expression opened with {= has no closing }", start - 2)


def template(text):
    """``text`` as a Template, or ExprError. ``{{`` and ``}}`` are escaped braces, as they are for blanks."""
    if not isinstance(text, str):
        raise ExprError("Expected text")
    parts, literal, at = [], [], 0
    while at < len(text):
        if text.startswith("{{", at) or text.startswith("}}", at):
            literal.append(text[at:at + 2])
            at += 2
        elif text.startswith("{=", at):
            end = _expression_end(text, at + 2)
            if literal:
                parts.append(("text", "".join(literal)))
                literal = []
            try:
                parts.append(("expr", compile(text[at + 2:end])))
            except ExprError as error:
                raise ExprError(str(error), None if error.at is None else at + 2 + error.at) from None
            at = end + 1
        else:
            literal.append(text[at])
            at += 1
    if literal:
        parts.append(("text", "".join(literal)))
    return Template(tuple(parts))


def render(value, scope, *, values=None, timezone=None):
    """A config value with its expressions worked out and its blanks filled, inside lists and objects too."""
    if isinstance(value, list):
        return [render(item, scope, values=values, timezone=timezone) for item in value]
    if isinstance(value, dict):
        return {key: render(item, scope, values=values, timezone=timezone) for key, item in value.items()}
    if not isinstance(value, str):
        return value
    shape = template(value)
    if shape.whole is not None:
        return evaluate(shape.whole, scope, timezone=timezone)
    defaults = {}
    for blank in shape.blanks:
        defaults.setdefault(starters._key(blank[1]), blank[2])
        defaults[starters._key(blank[1])] = defaults[starters._key(blank[1])] or blank[2]
    out = []
    for part in shape.parts:
        if part[0] == "expr":
            out.append(text_of(evaluate(part[1], scope, timezone=timezone)))
        else:
            out.append(fill_blanks(part[1], values, defaults))
    rendered = "".join(out)
    if len(rendered) > MAX_RESULT:
        raise ExprError("That text is too long")
    return rendered


def fill_blanks(raw, values, defaults=None):
    given = {starters._key(k): v for k, v in (values or {}).items()}
    defaults = defaults or {}
    out, missing = [], []
    for segment in starters.parse(raw):
        if segment[0] == "text":
            out.append(segment[1])
            continue
        key = starters._key(segment[1])
        filled = given.get(key)
        filled = text_of(filled) if filled is not None and not isinstance(filled, str) else filled
        filled = filled or segment[2] or defaults.get(key)
        if not filled:
            missing.append(segment[1])
            continue
        out.append(filled)
    if missing:
        raise starters.MissingBlanks(missing)
    return "".join(out)
