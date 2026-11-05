"""The values Mobster expressions work on (graph/expr.py): JSON only, with the rules every operation and function
shares. A missing key reads as null; an operation that can't apply raises ExprError, so nothing guesses."""

import json
import math
import re

MAX_SOURCE = 2000
MAX_NODES = 500
MAX_DEPTH = 32
MAX_STEPS = 20_000
MAX_ITEMS = 10_000
MAX_RESULT = 1_000_000
MAX_GLOB = 200
COMPARE = ("==", "!=", "<", "<=", ">", ">=", "in")


class ExprError(ValueError):
    """An expression that doesn't parse, can only fail its types, or failed while it ran."""

    def __init__(self, message, at=None):
        super().__init__(message)
        self.at = at



def kind_of(value):
    """The JSON kind of a value: null, boolean, number, string, list or object."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    raise ExprError(f"{type(value).__name__} isn't a JSON value")


def equal(a, b):
    """JSON equality: 1 equals 1.0, true never equals 1, lists and objects compare by content."""
    ka, kb = kind_of(a), kind_of(b)
    if ka != kb:
        return False
    if ka == "list":
        return len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b))
    if ka == "object":
        return a.keys() == b.keys() and all(equal(a[k], b[k]) for k in a)
    return a == b


def text_of(value):
    """How a value reads inside text: null is empty, 3.0 is "3", lists and objects are compact JSON."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ExprError("That number is too big to write")
        return str(int(value)) if value.is_integer() and abs(value) < 1e16 else repr(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def describe(value):
    kind = kind_of(value)
    return {"null": "null", "boolean": "a true or false", "number": "a number", "string": "text",
            "list": "a list", "object": "an object"}[kind]


def need(value, kinds, what):
    if kind_of(value) not in kinds:
        names = {"number": "a number", "string": "text", "list": "a list", "object": "an object",
                 "boolean": "true or false"}
        raise ExprError(f"{what} needs " + " or ".join(names[k] for k in kinds) + f", not {describe(value)}")
    return value


def as_int(value, what):
    if kind_of(value) != "number" or float(value) != int(value):
        raise ExprError(f"{what} needs a whole number, not {describe(value)}")
    return int(value)


PATH = re.compile(r"\s*(?:\.?\s*([^.\[\]\s]+)|\[\s*(-?\d+)\s*\]|\[\s*\"([^\"]*)\"\s*\])")


def parse_path(text):
    """A text path ("data.items[0].name", "[\"a b\"]") as its keys and indexes."""
    if not isinstance(text, str) or len(text) > 500:
        raise ExprError("A path is text like \"data.items[0].name\"")
    parts, at = [], 0
    while at < len(text):
        match = PATH.match(text, at)
        if match is None or match.end() == at:
            raise ExprError(f"Can't read the path {text!r}")
        name, index, quoted = match.groups()
        parts.append(int(index) if index is not None else quoted if quoted is not None else name)
        at = match.end()
        if not text[at:].strip():
            break
    return parts


def lookup(value, key):
    """value[key] as an expression reads it: null for a missing key or index, an error where it can't apply."""
    if value is None:
        return None
    kind = kind_of(value)
    if kind == "object":
        if not isinstance(key, str):
            raise ExprError(f"An object's fields are named with text, not {describe(key)}")
        return value.get(key)
    if kind in ("list", "string"):
        index = as_int(key, "A list's position") if not isinstance(key, str) else None
        if index is None:
            raise ExprError(f"{key!r} reads a field, but this is {describe(value)}. "
                            + ("Use pluck() to read a field of each item" if kind == "list" else ""))
        if -len(value) <= index < len(value):
            return value[index]
        return None
    raise ExprError(f"Can't read {key!r} from {describe(value)}")


def walk(value, path):
    for part in parse_path(path) if isinstance(path, str) else path:
        value = lookup(value, part)
    return value


def casefold(value, what):
    return need(value, ("string",), what).casefold()


def contains(haystack, needle):
    if kind_of(haystack) == "list":
        return any(equal(item, needle) for item in haystack)
    return casefold(needle, "contains()") in casefold(haystack, "contains()")



def compare(op, a, b):
    if op == "==":
        return equal(a, b)
    if op == "!=":
        return not equal(a, b)
    if op == "in":
        kind = kind_of(b)
        if kind == "list":
            return any(equal(a, item) for item in b)
        if kind == "object":
            return need(a, ("string",), "in") in b
        if kind == "string":
            return need(a, ("string",), "in") in b
        raise ExprError(f"in looks inside a list, an object or text, not {describe(b)}")
    if op == "contains":
        return contains(a, b)
    ka, kb = kind_of(a), kind_of(b)
    if ka != kb or ka not in ("number", "string"):
        raise ExprError(f"{op} compares two numbers or two texts, not {describe(a)} and {describe(b)}")
    return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]



def arith(op, a, b):
    if op == "+":
        ka, kb = kind_of(a), kind_of(b)
        if ka == kb == "string":
            if len(a) + len(b) > MAX_RESULT:
                raise ExprError("That text is too long")
            return a + b
        if ka == kb == "list":
            if len(a) + len(b) > MAX_ITEMS:
                raise ExprError(f"A list holds at most {MAX_ITEMS:,} items")
            return a + b
        if ka == kb == "number":
            return a + b
        raise ExprError(f"+ adds two numbers, or joins two texts or two lists, not {describe(a)} and {describe(b)}")
    need(a, ("number",), op)
    need(b, ("number",), op)
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if b == 0:
        raise ExprError("Can't divide by zero")
    if op == "/":
        result = a / b
        return int(result) if isinstance(a, int) and isinstance(b, int) and result.is_integer() else result
    return a % b
