"""The functions Mobster expressions call (graph/expr.py): a fixed list, each pure, over JSON values. A function
that gets a kind of value it can't use raises ExprError with its name."""

import fnmatch
import json
import math
import re
from datetime import datetime
from datetime import timezone as dt_timezone
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .values import (
    COMPARE,
    MAX_GLOB,
    MAX_ITEMS,
    MAX_RESULT,
    ExprError,
    as_int,
    casefold,
    compare,
    contains,
    equal,
    kind_of,
    need,
    parse_path,
    text_of,
    walk,
)


def _replace(text, old, new):
    need(text, ("string",), "replace()")
    if not need(old, ("string",), "replace()"):
        raise ExprError("replace() needs something to replace")
    return text.replace(old, need(new, ("string",), "replace()"))


def _split(text, sep):
    if not need(sep, ("string",), "split()"):
        raise ExprError("split() needs a separator")
    return need(text, ("string",), "split()").split(sep)


def _join(items, sep):
    need(items, ("list",), "join()")
    for item in items:
        need(item, ("string", "number", "boolean", "null"), "join()'s items")
    return need(sep, ("string",), "join()").join(text_of(item) for item in items)


def _slice(value, start, end=None):
    need(value, ("string", "list"), "slice()")
    return value[as_int(start, "slice()"):(None if end is None else as_int(end, "slice()"))]


def _like(text, glob):
    if len(need(glob, ("string",), "like()")) > MAX_GLOB:
        raise ExprError(f"like() takes a pattern of at most {MAX_GLOB} characters")
    return fnmatch.fnmatchcase(casefold(text, "like()"), glob.casefold())


def _number(value):
    if kind_of(value) == "number":
        return value
    text = need(value, ("string",), "number()").strip()
    if re.fullmatch(r"-?\d{1,18}", text):
        return int(text)
    try:
        number = float(text)
    except ValueError:
        raise ExprError(f"number() can't read {text[:40]!r} as a number") from None
    if not math.isfinite(number):
        raise ExprError("number() can't read that as a number")
    return number


def _round(value, digits=0):
    places = as_int(digits, "round()")
    if not 0 <= places <= 10:
        raise ExprError("round() keeps 0 to 10 digits")
    rounded = Decimal(str(need(value, ("number",), "round()"))).quantize(Decimal(1).scaleb(-places), ROUND_HALF_UP)
    return int(rounded) if places == 0 else float(rounded)


def _numbers(args, what):
    values = args[0] if len(args) == 1 else list(args)
    need(values, ("list",), what)
    for item in values:
        need(item, ("number",), what + "'s items")
    return values


def _min(*args):
    values = args[0] if len(args) == 1 else list(args)
    need(values, ("list",), "min()")
    return _ordered(values, "min()")[0] if values else None


def _max(*args):
    values = args[0] if len(args) == 1 else list(args)
    need(values, ("list",), "max()")
    return _ordered(values, "max()")[-1] if values else None


def _ordered(values, what, key=lambda value: value):
    keys = [key(value) for value in values]
    kinds = {kind_of(k) for k in keys if k is not None}
    if len(kinds) > 1 or kinds - {"number", "string"}:
        raise ExprError(f"{what} orders numbers or text, not a mix")
    present = [v for v, k in zip(values, keys) if k is not None]
    missing = [v for v, k in zip(values, keys) if k is None]
    return sorted(present, key=key) + missing


def _avg(items):
    values = _numbers((items,), "avg()")
    return sum(values) / len(values) if values else None


def _where(items, path, op, value):
    need(items, ("list",), "where()")
    if op not in COMPARE + ("contains",):
        raise ExprError("where()'s test is one of " + ", ".join(COMPARE + ("contains",)))
    parts = parse_path(path)
    return [item for item in items if compare(op, walk(item, parts), value)]


def _sort(items, path=None):
    need(items, ("list",), "sort()")
    parts = parse_path(path) if path is not None else []
    return _ordered(items, "sort()", key=lambda item: walk(item, parts))


def _unique(items):
    out = []
    for item in need(items, ("list",), "unique()"):
        if not any(equal(item, seen) for seen in out):
            out.append(item)
    return out


def _flatten(items):
    out = []
    for item in need(items, ("list",), "flatten()"):
        out.extend(item if isinstance(item, list) else [item])
    return out


def _range(a, b=None):
    start, stop = (0, as_int(a, "range()")) if b is None else (as_int(a, "range()"), as_int(b, "range()"))
    if stop - start > MAX_ITEMS:
        raise ExprError(f"range() makes at most {MAX_ITEMS:,} numbers")
    return list(range(start, stop))


def _empty(value):
    return value is None or (kind_of(value) in ("string", "list", "object") and len(value) == 0)


def _bool(value):
    if isinstance(value, bool):
        return value
    text = need(value, ("string",), "bool()").strip().casefold()
    if text in ("true", "yes"):
        return True
    if text in ("false", "no"):
        return False
    raise ExprError(f"bool() can't read {text[:40]!r} as true or false")


DATE_TOKENS = re.compile(r"\[[^\]]*\]|YYYY|MMMM|MMM|MM|DD|dddd|ddd|HH|hh|mm|ss|A|D|M|H")


def _date(ms, pattern="YYYY-MM-DD HH:mm", zone=None, *, default_zone=None):
    when = need(ms, ("number",), "date()")
    pattern = need(pattern, ("string",), "date()")
    name = zone if zone is not None else default_zone
    try:
        tz = ZoneInfo(need(name, ("string",), "date()")) if name else dt_timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        raise ExprError(f"date() doesn't know the time zone {name!r}") from None
    try:
        moment = datetime.fromtimestamp(when / 1000, tz)
    except (OverflowError, OSError, ValueError):
        raise ExprError("date() needs a time in milliseconds since 1970") from None
    table = {"YYYY": "%Y", "MMMM": "%B", "MMM": "%b", "MM": "%m", "DD": "%d", "dddd": "%A", "ddd": "%a", "HH": "%H",
             "hh": "%I", "mm": "%M", "ss": "%S", "A": "%p"}

    def part(match):
        token = match.group(0)
        if token.startswith("["):
            return token[1:-1]
        if token == "D":
            return str(moment.day)
        if token == "M":
            return str(moment.month)
        if token == "H":
            return str(moment.hour)
        return moment.strftime(table[token])
    return DATE_TOKENS.sub(part, pattern)


def _parse_json(text):
    if len(need(text, ("string",), "parse_json()")) > MAX_RESULT:
        raise ExprError("parse_json() reads at most 1 MB")
    try:
        return json.loads(text, parse_constant=_no_constant)
    except (ValueError, RecursionError):
        raise ExprError("parse_json() needs JSON text") from None


def _no_constant(name):
    raise ValueError(f"{name} isn't JSON")


def _ms(unit):
    return lambda n: need(n, ("number",), "time") * unit


FUNCTIONS = {
    "lower": (1, 1, lambda s: need(s, ("string",), "lower()").lower(), "string"),
    "upper": (1, 1, lambda s: need(s, ("string",), "upper()").upper(), "string"),
    "trim": (1, 1, lambda s: need(s, ("string",), "trim()").strip(), "string"),
    "len": (1, 1, lambda x: len(need(x, ("string", "list", "object"), "len()")), "number"),
    "contains": (2, 2, contains, "boolean"),
    "starts_with": (2, 2, lambda s, p: casefold(s, "starts_with()").startswith(casefold(p, "starts_with()")),
                    "boolean"),
    "ends_with": (2, 2, lambda s, p: casefold(s, "ends_with()").endswith(casefold(p, "ends_with()")), "boolean"),
    "replace": (3, 3, _replace, "string"),
    "split": (2, 2, _split, "list"),
    "join": (2, 2, _join, "string"),
    "slice": (2, 3, _slice, None),
    "like": (2, 2, _like, "boolean"),
    "string": (1, 1, text_of, "string"),
    "json": (1, 1, lambda x: json.dumps(x, ensure_ascii=False, separators=(",", ":")), "string"),
    "parse_json": (1, 1, _parse_json, None),
    "number": (1, 1, _number, "number"),
    "round": (1, 2, _round, "number"),
    "floor": (1, 1, lambda n: math.floor(need(n, ("number",), "floor()")), "number"),
    "ceil": (1, 1, lambda n: math.ceil(need(n, ("number",), "ceil()")), "number"),
    "abs": (1, 1, lambda n: abs(need(n, ("number",), "abs()")), "number"),
    "min": (1, 20, _min, None),
    "max": (1, 20, _max, None),
    "sum": (1, 1, lambda items: sum(_numbers((items,), "sum()")), "number"),
    "avg": (1, 1, _avg, "number"),
    "count": (1, 1, lambda x: len(need(x, ("list", "object"), "count()")), "number"),
    "first": (1, 1, lambda items: (need(items, ("list",), "first()") or [None])[0], None),
    "last": (1, 1, lambda items: (need(items, ("list",), "last()") or [None])[-1], None),
    "pluck": (2, 2, lambda items, path: [walk(item, parse_path(path)) for item in need(items, ("list",), "pluck()")],
              "list"),
    "where": (4, 4, _where, "list"),
    "sort": (1, 2, _sort, "list"),
    "reverse": (1, 1, lambda x: need(x, ("list", "string"), "reverse()")[::-1], None),
    "unique": (1, 1, _unique, "list"),
    "flatten": (1, 1, _flatten, "list"),
    "range": (1, 2, _range, "list"),
    "keys": (1, 1, lambda o: list(need(o, ("object",), "keys()")), "list"),
    "values": (1, 1, lambda o: list(need(o, ("object",), "values()").values()), "list"),
    "get": (2, 3, lambda x, path, default=None: (lambda found: default if found is None else found)(walk(x, path)),
            None),
    "if": (3, 3, None, None),          # lazy: evaluates only the side it picks
    "default": (2, 2, lambda x, fallback: fallback if _empty(x) else x, None),
    "empty": (1, 1, _empty, "boolean"),
    "exists": (1, 1, lambda x: x is not None, "boolean"),
    "bool": (1, 1, _bool, "boolean"),
    "date": (1, 3, _date, "string"),
    "days": (1, 1, _ms(86_400_000), "number"),
    "hours": (1, 1, _ms(3_600_000), "number"),
    "minutes": (1, 1, _ms(60_000), "number"),
}
