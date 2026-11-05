"""Result blocks: a task's answer as cards from a fixed vocabulary (docs/results.mdx).

A finished task may carry ``blocks``, a versioned envelope ``{"v": 1, "blocks": [...]}``, beside its plain ``answer``.
Each block is one of ``TYPES``: data only, never code, HTML or a path. ``validate`` is the one gate: the Runtime runs
every envelope through it before a block is stored or shown. A block that doesn't fit becomes a ``markdown`` block with
its readable text, so an answer never disappears; every string goes through ``secret_filter.redact``.

- ``validate(value)``: (envelope or None, problems), the envelope normalized and bounded.
- Builders for code that already holds the data (long tasks, a read list, a workflow): ``review_queue``, ``table``,
  ``media_list``, ``evidence``, ``checklist``, ``markdown``, ``chart`` and ``envelope``.
- ``to_markdown(envelope)`` and ``table_csv(block)``: the blocks as Markdown and a table as CSV, for export and for
  clients that show text.
- A review queue's Approve (``approval_item``, ``follow_up_goal``, ``preapproval``, ``preapproval_matches``): the item
  is read from the stored block, never from the client; Approve starts a task that sends exactly that text to exactly
  that recipient, and its send still goes through the run's permission mode and the journal. An item with no draft
  can't be approved, except one that code marked ``yourWords`` (``YOUR_WORDS``): there the person's own typed words
  are the draft.
"""

import csv
import io
import json
import math
import re

from .secret_filter import is_secret, redact

VERSION = 1

MAX_BLOCKS = 8
MAX_ROWS = 200
MAX_ITEMS = 60
MAX_COLUMNS = 16
MAX_TEXT = 2000
MAX_MARKDOWN = 12000
MAX_TITLE = 160
MAX_BYTES = 256_000
MAX_SERIES = 5
MAX_POINTS = 60
MAX_SCATTER_SERIES = 3         # all three pairs of the first three slots stay apart for colour-blind readers
MAX_SCATTER_POINTS = 200
CHART_KINDS = ("bar", "line", "area", "pie", "scatter", "sparkline")
CHART_ALIASES = {"column": "bar", "columns": "bar", "bars": "bar", "donut": "pie", "doughnut": "pie", "lines": "line"}
VALUE_FORMATS = ("number", "money", "percent")
TABLE_VIEWS = ("table", "chart", "map", "compare")
MAX_PLACES = 30
MAX_OPTIONS = 4
TIMELINE_STATES = ("done", "now", "next", "pass", "fail", "uncertain")

COLUMN_TYPES = ("text", "number", "money", "percent", "date", "duration", "bool", "link", "status", "brand")
NUMERIC_COLUMNS = ("number", "money", "percent", "duration")
STATUSES = ("pass", "fail", "uncertain")
QUEUE_ACTIONS = ("send_message", "reply", "post_comment")
DEFAULT_BUCKETS = ({"id": "reply", "label": "Needs a reply"}, {"id": "wait", "label": "Can wait"},
                   {"id": "spam", "label": "Spam"})
BLANK = re.compile(r"\[[^\]\n]{1,80}\]")
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class BlockError(ValueError):
    """A block that doesn't fit the vocabulary; its message says what, in plain words."""


class _Marker:
    """A value only code in this process can hold: JSON can't say it, and a copy of it is itself."""
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return f"<{self.name}>"

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self


# Code that makes an item with no draft ON PURPOSE, for the person to answer in their own words (Who's waiting on me),
# sets its ``yourWords`` to this. ``validate`` stores ``yourWords: true`` for it and for nothing else, so a block that
# comes from a model or a request can't carry the mark; ``approval_item`` is then the one place that reads it.
YOUR_WORDS = _Marker("your words")


def _text(value, limit=MAX_TEXT, *, required=False, field="text", multiline=True):
    """A bounded, redacted string, or None. Numbers become their text; anything else is refused."""
    if value is None or value == "":
        if required:
            raise BlockError(f"{field} is required")
        return None
    if isinstance(value, bool):
        value = "yes" if value else "no"
    elif isinstance(value, (int, float)) and math.isfinite(value):
        value = format(value, "g") if isinstance(value, float) else str(value)
    if not isinstance(value, str):
        raise BlockError(f"{field} must be text")
    value = _CONTROL.sub("", value).replace("\r\n", "\n")
    if not multiline:
        value = " ".join(value.split())
    value = value.strip()
    if not value:
        if required:
            raise BlockError(f"{field} is required")
        return None
    return redact(value[:limit])


def _id(value, fallback):
    value = str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    return value if _ID.fullmatch(value) else fallback


def _frame(value):
    """A run frame's id (GET /api/runs/{id}/frames/{frameId}), never a path or a URL."""
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def _link(value):
    """An https link, else None: no other scheme reaches a block."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:2000] if re.fullmatch(r"https://[^\s<>\"'`]{1,1990}", value) and not is_secret(value) else None


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value


def _crop(value):
    if not isinstance(value, dict):
        return None
    try:
        box = {key: float(value[key]) for key in ("x", "y", "w", "h")}
    except (KeyError, TypeError, ValueError):
        return None
    if not all(0 <= box[k] <= 1 for k in box) or box["w"] <= 0 or box["h"] <= 0:
        return None
    return {key: round(min(1.0, box[key]), 4) for key in box}


def status_word(value):
    value = str(value or "").strip().lower()
    return {"passed": "pass", "ok": "pass", "yes": "pass", "failed": "fail", "no": "fail",
            "unsure": "uncertain", "unknown": "uncertain"}.get(value, value) if value else None


def _review_queue(block):
    action = block.get("action") or "send_message"
    if action not in QUEUE_ACTIONS:
        raise BlockError("A review queue's action is send_message, reply or post_comment")
    buckets = []
    for bucket in block.get("buckets") or DEFAULT_BUCKETS:
        if isinstance(bucket, dict):
            bucket_id = _id(bucket.get("id"), None)
            label = _text(bucket.get("label"), 40, multiline=False)
            if bucket_id and label and bucket_id not in {b["id"] for b in buckets}:
                buckets.append({"id": bucket_id, "label": label})
        if len(buckets) >= 6:
            break
    raw = block.get("items")
    if not isinstance(raw, list) or not raw:
        raise BlockError("A review queue needs items")
    items, seen = [], set()
    for index, item in enumerate(raw[:MAX_ITEMS]):
        if not isinstance(item, dict):
            raise BlockError("A review queue's item is an object")
        item_id = _id(item.get("id"), f"i{index + 1}")
        if item_id in seen:
            item_id = f"i{index + 1}"
        seen.add(item_id)
        bucket = item.get("bucket")
        bucket = bucket if buckets and bucket in {b["id"] for b in buckets} else (buckets[0]["id"] if buckets else None)
        out = {"id": item_id, "bucket": bucket,
               "to": _text(item.get("to"), 120, required=True, field="An item's recipient (to)", multiline=False),
               "handle": _text(item.get("handle"), 80, multiline=False),
               "app": _text(item.get("app"), 80, multiline=False),
               "bundleId": item.get("bundleId") if isinstance(item.get("bundleId"), str)
               and re.fullmatch(r"[A-Za-z0-9.-]{3,155}", item["bundleId"]) else None,
               "account": _text(item.get("account"), 120, multiline=False),
               "context": _text(item.get("context") or item.get("summary"), 400),
               "lastMessage": _text(item.get("lastMessage"), 600),
               "draft": _text(item.get("draft"), 4000),
               "decision": _text(item.get("decision"), 200),
               "frameId": _frame(item.get("frameId"))}
        if item.get("yourWords") is YOUR_WORDS and out["draft"] is None:
            out["yourWords"] = True
        items.append({key: value for key, value in out.items() if value is not None or key in ("to", "draft")})
    return {"action": action, "buckets": buckets, "items": items}


def _column(column, index):
    if isinstance(column, str):
        column = {"label": column}
    if not isinstance(column, dict):
        raise BlockError("A table's column is a name or an object")
    label = _text(column.get("label") or column.get("key"), 60, required=True, field="A column's label",
                  multiline=False)
    key = (_id(column.get("key"), None) or re.sub(r"[^a-z0-9]+", "_", label.casefold()).strip("_")[:40]
           or f"c{index + 1}")
    kind = column.get("type") or "text"
    if kind not in COLUMN_TYPES:
        kind = "text"
    out = {"key": key, "label": label, "type": kind}
    if kind == "money" and isinstance(column.get("currency"), str) and re.fullmatch(r"[A-Z]{3}", column["currency"]):
        out["currency"] = column["currency"]
    return out


def _cell(value, kind):
    if value is None or value == "":
        return None
    if kind in NUMERIC_COLUMNS:
        number = _number(value)
        if number is not None:
            return number
        if isinstance(value, str):
            cleaned = re.sub(r"[\s,$€£¥%]", "", value)
            try:
                number = float(cleaned)
                return int(number) if number.is_integer() and "." not in cleaned else number
            except ValueError:
                pass
        return _text(value, MAX_TEXT)
    if kind == "bool":
        if isinstance(value, bool):
            return value
        word = str(value).strip().casefold()
        return (True if word in ("yes", "true", "y", "1") else False
                if word in ("no", "false", "n", "0") else _text(value))
    if kind == "link":
        return _link(value) or _text(value, 300, multiline=False)
    if kind == "brand":
        return _text(value, 80, multiline=False)
    if kind == "status":
        status = status_word(value)
        return status if status in STATUSES else _text(value, 120, multiline=False)
    return _text(value, MAX_TEXT)


def _table(block):
    columns = block.get("columns")
    if not isinstance(columns, list) or not 1 <= len(columns) <= MAX_COLUMNS:
        raise BlockError(f"A table has 1 to {MAX_COLUMNS} columns")
    columns = [_column(column, index) for index, column in enumerate(columns)]
    keys = set()
    for index, column in enumerate(columns):
        if column["key"] in keys:
            column["key"] = f"c{index + 1}"
        keys.add(column["key"])
    raw = block.get("rows")
    if not isinstance(raw, list):
        raise BlockError("A table's rows are a list")
    rows = []
    for row in raw[:MAX_ROWS]:
        frame = None
        if isinstance(row, dict) and "cells" in row:
            frame, cells = _frame(row.get("frameId")), row.get("cells")
        else:
            cells = row
        if isinstance(cells, list):
            cells = {column["key"]: cells[i] for i, column in enumerate(columns) if i < len(cells)}
        if not isinstance(cells, dict):
            raise BlockError("A table's row is a list of cells or an object of them")
        if isinstance(row, dict) and "cells" not in row:
            frame = _frame(row.get("frameId"))
        out = {"cells": {column["key"]: _cell(cells.get(column["key"], cells.get(column["label"])), column["type"])
                         for column in columns}}
        if frame:
            out["frameId"] = frame
        rows.append(out)
    result = {"columns": columns, "rows": rows}
    if len(raw) > MAX_ROWS:
        result["truncated"] = len(raw)
    sort = block.get("sort")
    if isinstance(sort, dict) and sort.get("key") in keys:
        result["sort"] = {"key": sort["key"], "dir": "asc" if sort.get("dir") == "asc" else "desc"}
    # How the card opens (the others stay a click away): its rows as a chart, on a map or as cards side by side.
    chart = _table_chart(block.get("chart"), columns)
    if chart:
        result["chart"] = chart
    if block.get("view") in TABLE_VIEWS and block["view"] != "table":
        result["view"] = block["view"]
    return result


def _table_chart(value, columns):
    """A table's chart: {"kind", "x": a column's key, "y": [numeric columns' keys], "stacked"?}, or None."""
    if not isinstance(value, dict):
        return None
    kind = CHART_ALIASES.get(str(value.get("kind") or "bar").lower(), str(value.get("kind") or "bar").lower())
    by_key = {column["key"]: column for column in columns}
    x = value.get("x") if value.get("x") in by_key else None
    wanted = value.get("y") if isinstance(value.get("y"), list) else []
    y = [key for key in wanted if key in by_key and by_key[key]["type"] in NUMERIC_COLUMNS and key != x]
    if kind not in CHART_KINDS or not y:
        return None
    out = {"kind": kind, "y": y[:1 if kind == "pie" else MAX_SCATTER_SERIES if kind == "scatter" else MAX_SERIES]}
    if x:
        out["x"] = x
    if value.get("stacked") is True and kind in ("bar", "area") and len(out["y"]) > 1:
        out["stacked"] = True
    return out


def _media_list(block):
    raw = block.get("items")
    if not isinstance(raw, list) or not raw:
        raise BlockError("A media list needs items")
    items = []
    for index, item in enumerate(raw[:MAX_ITEMS]):
        if not isinstance(item, dict):
            raise BlockError("A media list's item is an object")
        sounds = [s for s in (_text(sound, 80, multiline=False) for sound in (item.get("sounds") or [])[:6]
                              if isinstance(item.get("sounds"), list)) if s]
        out = {"id": _id(item.get("id"), f"m{index + 1}"),
               "title": _text(item.get("title"), 200, multiline=False),
               "creator": _text(item.get("creator"), 80, multiline=False),
               "frameId": _frame(item.get("frameId")), "at": _number(item.get("at")),
               "transcript": _text(item.get("transcript"), 600), "sounds": sounds or None,
               "link": _link(item.get("link"))}
        if not (out["title"] or out["creator"] or out["frameId"] or out["transcript"]):
            raise BlockError("A media item needs a title, a creator, a screenshot or a transcript")
        items.append({key: value for key, value in out.items() if value is not None})
    return {"items": items}


def _evidence(block):
    out = {"claim": _text(block.get("claim"), 600, required=True, field="Evidence's claim"),
           "frameId": _frame(block.get("frameId")), "crop": _crop(block.get("crop")),
           "quote": _text(block.get("quote"), 300), "verdict": status_word(block.get("verdict"))}
    if out["verdict"] not in STATUSES:
        out["verdict"] = None
    if not out["frameId"] and not out["quote"]:
        raise BlockError("Evidence needs a screenshot or a quote")
    return {key: value for key, value in out.items() if value is not None}


def _checklist(block):
    raw = block.get("items")
    if not isinstance(raw, list) or not raw:
        raise BlockError("A checklist needs items")
    items = []
    for index, item in enumerate(raw[:MAX_ITEMS]):
        if not isinstance(item, dict):
            raise BlockError("A checklist's item is an object")
        status = status_word(item.get("status"))
        out = {"id": _id(item.get("id"), f"s{index + 1}"),
               "label": _text(item.get("label"), 200, required=True, field="A checklist item's label"),
               "status": status if status in STATUSES else "uncertain",
               "expected": _text(item.get("expected"), 400), "observed": _text(item.get("observed"), 400),
               "frameId": _frame(item.get("frameId"))}
        items.append({key: value for key, value in out.items() if value is not None})
    return {"items": items}


def _markdown(block):
    return {"text": _text(block.get("text"), MAX_MARKDOWN, required=True, field="Markdown's text")}


def _chart(block):
    """bar, line, area, pie, scatter or sparkline."""
    kind = str(block.get("kind") or "bar").strip().lower()
    kind = CHART_ALIASES.get(kind, kind)
    if kind not in CHART_KINDS:
        raise BlockError("A chart is bar, line, area, pie, scatter or sparkline")
    out = {"kind": kind}
    raw = block.get("series") if isinstance(block.get("series"), list) else []
    if kind == "scatter":
        series = []
        for one in raw[:MAX_SCATTER_SERIES]:
            if not isinstance(one, dict) or not isinstance(one.get("points"), list):
                raise BlockError("A scatter chart's series each hold points")
            points = []
            for point in one["points"][:MAX_SCATTER_POINTS]:
                if isinstance(point, (list, tuple)) and len(point) >= 2:
                    point = {"x": point[0], "y": point[1]}
                if not isinstance(point, dict):
                    continue
                x, y = _number(point.get("x")), _number(point.get("y"))
                if x is None or y is None:
                    continue
                label = _text(point.get("label"), 60, multiline=False)
                points.append({"x": x, "y": y, **({"label": label} if label else {})})
            if points:
                series.append({"label": _text(one.get("label"), 40, multiline=False) or "Value", "points": points})
        if not series:
            raise BlockError("A scatter chart needs points")
        out["series"] = series
    else:
        x = block.get("x")
        if not isinstance(x, list) or not 1 <= len(x) <= MAX_POINTS:
            raise BlockError(f"A chart has 1 to {MAX_POINTS} labels")
        labels = [_text(label, 40, multiline=False) or "" for label in x]
        series = []
        for one in raw[:1 if kind == "pie" else MAX_SERIES]:
            if (not isinstance(one, dict) or not isinstance(one.get("values"), list)
                    or len(one["values"]) != len(labels)):
                raise BlockError("Each series has one value per label")
            values = [_number(value) for value in one["values"]]
            series.append({"label": _text(one.get("label"), 40, multiline=False) or "Value", "values": values})
        if not series:
            raise BlockError("A chart needs a series")
        out.update(x=labels, series=series)
    unit = _text(block.get("unit"), 12, multiline=False)
    if unit:
        out["unit"] = unit
    if block.get("format") in VALUE_FORMATS:
        out["format"] = block["format"]
    if isinstance(block.get("currency"), str) and re.fullmatch(r"[A-Z]{3}", block["currency"]):
        out["currency"] = block["currency"]
    if block.get("stacked") is True and kind in ("bar", "area") and len(out["series"]) > 1:
        out["stacked"] = True
    for key in ("xLabel", "yLabel"):
        label = _text(block.get(key), 40, multiline=False)
        if label:
            out[key] = label
    return out


def _coordinate(value, limit):
    number = _number(value)
    if number is None and isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    return round(number, 6) if number is not None and -limit <= number <= limit else None


def _map(block):
    """Places on a map: each a name and an address or a latitude and longitude (``lat``, ``lon``)."""
    raw = block.get("places")
    if not isinstance(raw, list) or not raw:
        raise BlockError("A map needs places")
    places = []
    for index, place in enumerate(raw[:MAX_PLACES]):
        if not isinstance(place, dict):
            raise BlockError("A map's place is an object")
        lat = _coordinate(place.get("lat", place.get("latitude")), 90)
        lon = _coordinate(place.get("lon", place.get("lng", place.get("longitude"))), 180)
        out = {"id": _id(place.get("id"), f"p{index + 1}"),
               "name": _text(place.get("name") or place.get("title"), 120, required=True, field="A place's name",
                             multiline=False),
               "address": _text(place.get("address"), 200, multiline=False),
               "lat": lat if lon is not None else None, "lon": lon if lat is not None else None,
               "note": _text(place.get("note") or place.get("detail"), 300),
               "rating": _number(place.get("rating")), "link": _link(place.get("link")),
               "frameId": _frame(place.get("frameId"))}
        if out["lat"] is None and not out["address"]:
            raise BlockError("A place needs an address or a latitude and longitude")
        places.append({key: value for key, value in out.items() if value is not None})
    out = {"places": places}
    near = _text(block.get("near"), 120, multiline=False)
    if near:
        out["near"] = near
    return out


def _timeline(block):
    """Events in order: ``at`` (a time, a date or ISO 8601, as text), a title, a detail, a state."""
    raw = block.get("items")
    if not isinstance(raw, list) or not raw:
        raise BlockError("A timeline needs items")
    items = []
    for index, item in enumerate(raw[:MAX_ITEMS]):
        if not isinstance(item, dict):
            raise BlockError("A timeline's item is an object")
        state = str(item.get("state") or item.get("status") or "").strip().lower()
        state = status_word(state) if state in ("passed", "failed", "ok", "unsure", "unknown") else state
        out = {"id": _id(item.get("id"), f"e{index + 1}"),
               "at": _text(item.get("at") or item.get("when") or item.get("time"), 40, multiline=False),
               "title": _text(item.get("title") or item.get("label"), 200, required=True,
                              field="A timeline item's title", multiline=False),
               "detail": _text(item.get("detail") or item.get("description"), 600),
               "state": state if state in TIMELINE_STATES else None, "frameId": _frame(item.get("frameId"))}
        items.append({key: value for key, value in out.items() if value is not None})
    return {"items": items}


def _comparison(block):
    raw = block.get("options")
    if not isinstance(raw, list) or not 2 <= len(raw) <= MAX_OPTIONS:
        raise BlockError(f"A comparison has 2 to {MAX_OPTIONS} options")
    options, seen = [], set()
    for index, option in enumerate(raw):
        if not isinstance(option, dict):
            raise BlockError("A comparison's option is an object")
        option_id = _id(option.get("id"), f"o{index + 1}")
        if option_id in seen:
            option_id = f"o{index + 1}"
        seen.add(option_id)
        facts = option.get("facts")
        if isinstance(facts, dict):
            facts = [{"label": key, "value": value} for key, value in facts.items()]
        clean = []
        for fact in (facts if isinstance(facts, list) else [])[:12]:
            if isinstance(fact, dict):
                label = _text(fact.get("label"), 60, multiline=False)
                value = _text(fact.get("value"), 200, multiline=False)
                if label and value is not None:
                    clean.append({"label": label, "value": value})

        def words(key):
            values = option.get(key) if isinstance(option.get(key), list) else []
            return [w for w in (_text(v, 160, multiline=False) for v in values[:6]) if w] or None

        out = {"id": option_id,
               "name": _text(option.get("name") or option.get("title"), 80, required=True,
                             field="An option's name", multiline=False),
               "subtitle": _text(option.get("subtitle"), 120, multiline=False),
               "badge": _text(option.get("badge"), 24, multiline=False),
               "facts": clean or None, "pros": words("pros"), "cons": words("cons"),
               "link": _link(option.get("link")), "frameId": _frame(option.get("frameId"))}
        options.append({key: value for key, value in out.items() if value is not None})
    out = {"options": options}
    if block.get("recommended") in seen:
        out["recommended"] = block["recommended"]
    verdict = _text(block.get("verdict"), 400)
    if verdict:
        out["verdict"] = verdict
    return out


def _gallery(block):
    """Screens from the run, in a grid: each a frameId and a caption."""
    raw = block.get("items")
    if not isinstance(raw, list) or not raw:
        raise BlockError("A gallery needs items")
    items = []
    for index, item in enumerate(raw[:MAX_ITEMS]):
        if not isinstance(item, dict):
            raise BlockError("A gallery's item is an object")
        frame = _frame(item.get("frameId"))
        if not frame:
            continue
        out = {"id": _id(item.get("id"), f"g{index + 1}"), "frameId": frame,
               "caption": _text(item.get("caption") or item.get("title"), 160, multiline=False),
               "link": _link(item.get("link"))}
        items.append({key: value for key, value in out.items() if value is not None})
    if not items:
        raise BlockError("A gallery's items each name a screen (frameId)")
    return {"items": items}


SHAPES = {"review_queue": _review_queue, "table": _table, "media_list": _media_list, "evidence": _evidence,
          "checklist": _checklist, "markdown": _markdown, "chart": _chart, "map": _map, "timeline": _timeline,
          "comparison": _comparison, "gallery": _gallery}
TYPES = tuple(SHAPES)


def _readable(block):
    """What a block that didn't fit still says, as Markdown: its title and every string in it, or None."""
    lines = []

    def walk(node, depth=0):
        if depth > 6 or len(lines) > 80:
            return
        if isinstance(node, str):
            text = _text(node, 400)
            if text and not text.startswith("https://") and text not in lines:
                lines.append(text)
        elif isinstance(node, (int, float)) and not isinstance(node, bool):
            return
        elif isinstance(node, dict):
            for key, value in node.items():
                if key not in ("type", "id", "frameId", "bundleId", "kind", "action", "bucket", "key"):
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                walk(value, depth + 1)

    walk(block)
    text = "\n".join(f"- {line}" if index else line for index, line in enumerate(lines))
    return text[:MAX_MARKDOWN] or None


def validate_block(block, index=0):
    """One block, normalized: {type, id, title?, ...its fields}. Raises BlockError when it doesn't fit."""
    if not isinstance(block, dict):
        raise BlockError("A block is an object")
    kind = block.get("type")
    if kind not in SHAPES:
        raise BlockError(f"Unknown block type {str(kind)[:40]!r}")
    out = {"type": kind, "id": _id(block.get("id"), f"b{index + 1}")}
    title = _text(block.get("title"), MAX_TITLE, multiline=False)
    if title:
        out["title"] = title
    out.update(SHAPES[kind](block))
    return out


def validate(value):
    """(envelope, problems)."""
    problems = []
    if isinstance(value, dict):
        if value.get("v", VERSION) != VERSION:
            return None, [f"Unsupported result blocks version {str(value.get('v'))[:8]}"]
        value = value.get("blocks")
    if not isinstance(value, list):
        return None, ["Result blocks are a list"] if value is not None else []
    if len(value) > MAX_BLOCKS:
        problems.append(f"Only the first {MAX_BLOCKS} blocks are kept")
    blocks, ids = [], set()
    for index, block in enumerate(value[:MAX_BLOCKS]):
        try:
            shaped = validate_block(block, index)
        except BlockError as problem:
            problems.append(str(problem))
            text = _readable(block)
            if not text:
                continue
            shaped = {"type": "markdown", "id": f"b{index + 1}", "text": text, "fallback": True}
        except (TypeError, ValueError, AttributeError, KeyError):
            problems.append("A block couldn't be read")
            continue
        if shaped["id"] in ids:
            shaped["id"] = f"b{index + 1}"
            while shaped["id"] in ids:
                shaped["id"] += "_"
        ids.add(shaped["id"])
        blocks.append(shaped)
    if not blocks:
        return None, problems
    envelope = {"v": VERSION, "blocks": blocks}
    while len(json.dumps(envelope, ensure_ascii=False).encode()) > MAX_BYTES and envelope["blocks"]:
        envelope["blocks"].pop()
        problems.append("The blocks were too large; the last was left out")
    return (envelope if envelope["blocks"] else None), problems


def review_queue(items, *, title=None, buckets=None, action="send_message", id="queue"):
    return {"type": "review_queue", "id": id, **({"title": title} if title else {}), "action": action,
            **({"buckets": list(buckets)} if buckets is not None else {}), "items": list(items)}


def table(columns, rows, *, title=None, sort=None, id="table"):
    return {"type": "table", "id": id, **({"title": title} if title else {}), "columns": list(columns),
            "rows": list(rows), **({"sort": sort} if sort else {})}


def media_list(items, *, title=None, id="media"):
    return {"type": "media_list", "id": id, **({"title": title} if title else {}), "items": list(items)}


def evidence(claim, *, frame_id=None, crop=None, quote=None, verdict=None, title=None, id="evidence"):
    return {"type": "evidence", "id": id, **({"title": title} if title else {}), "claim": claim, "frameId": frame_id,
            "crop": crop, "quote": quote, "verdict": verdict}


def checklist(items, *, title=None, id="checks"):
    return {"type": "checklist", "id": id, **({"title": title} if title else {}), "items": list(items)}


def markdown(text, *, title=None, id="text"):
    return {"type": "markdown", "id": id, **({"title": title} if title else {}), "text": text}


def chart(x, series, *, kind="bar", title=None, unit=None, id="chart", **extra):
    """A chart block."""
    return {"type": "chart", "id": id, **({"title": title} if title else {}), "kind": kind,
            **({"x": list(x)} if x is not None else {}), "series": list(series), **({"unit": unit} if unit else {}),
            **extra}


def map_block(places, *, title=None, near=None, id="map"):
    return {"type": "map", "id": id, **({"title": title} if title else {}), "places": list(places),
            **({"near": near} if near else {})}


def timeline(items, *, title=None, id="timeline"):
    return {"type": "timeline", "id": id, **({"title": title} if title else {}), "items": list(items)}


def comparison(options, *, title=None, recommended=None, verdict=None, id="compare"):
    return {"type": "comparison", "id": id, **({"title": title} if title else {}), "options": list(options),
            **({"recommended": recommended} if recommended else {}), **({"verdict": verdict} if verdict else {})}


def gallery(items, *, title=None, id="gallery"):
    return {"type": "gallery", "id": id, **({"title": title} if title else {}), "items": list(items)}


def envelope(blocks):
    """The validated envelope of ``blocks`` (None when none is usable)."""
    return validate(list(blocks))[0]


def merge(*envelopes):
    """Several envelopes as one, in order, ids kept unique (None when all are empty)."""
    blocks = [block for value in envelopes if isinstance(value, dict) for block in value.get("blocks") or ()]
    return validate(blocks)[0] if blocks else None


def _cell_text(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def _guard(text):
    """A spreadsheet never runs a cell: one that starts like a formula gets an apostrophe (result-formats.md)."""
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def table_csv(block):
    """A table block as CSV text: a header of the column labels, then one line per row."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow([_guard(column["label"]) for column in block["columns"]])
    for row in block["rows"]:
        writer.writerow([_guard(_cell_text(row["cells"].get(column["key"]))) for column in block["columns"]])
    return out.getvalue()


def _md_cell(value):
    return _cell_text(value).replace("|", "\\|").replace("\n", " ")


def comparison_rows(options):
    """Fact labels in first-seen order, with each option's first value or an empty cell."""
    labels = dict.fromkeys(fact["label"] for option in options for fact in option.get("facts") or ())
    for label in labels:
        yield label, [next((fact["value"] for fact in option.get("facts") or () if fact["label"] == label), "")
                      for option in options]


def block_markdown(block):
    """One block as Markdown."""
    kind, lines = block["type"], []
    if block.get("title"):
        lines += [f"## {block['title']}", ""]
    if kind == "markdown":
        lines.append(block["text"])
    elif kind == "table":
        columns = block["columns"]
        lines.append("| " + " | ".join(_md_cell(c["label"]) for c in columns) + " |")
        lines.append("| " + " | ".join("---:" if c["type"] in NUMERIC_COLUMNS else "---"
                                       for c in columns) + " |")
        for row in block["rows"]:
            lines.append("| " + " | ".join(_md_cell(row["cells"].get(c["key"])) for c in columns) + " |")
    elif kind == "review_queue":
        labels = {bucket["id"]: bucket["label"] for bucket in block.get("buckets") or ()}
        for item in block["items"]:
            where = f" ({item['app']})" if item.get("app") else ""
            bucket = f" [{labels[item['bucket']]}]" if item.get("bucket") in labels else ""
            lines.append(f"**{item['to']}**{where}{bucket}")
            if item.get("context"):
                lines.append(item["context"])
            if item.get("lastMessage"):
                lines.append(f"> {item['lastMessage']}")
            if item.get("decision"):
                lines.append(f"Needs your decision: {item['decision']}")
            if item.get("draft"):
                lines.append(f"Draft: {item['draft']}")
            lines.append("")
        lines.append("Nothing here was sent.")
    elif kind == "media_list":
        for item in block["items"]:
            head = ", ".join(part for part in (item.get("title"), item.get("creator")) if part) or "Item"
            lines.append(f"- **{head}**" + (f" ({item['link']})" if item.get("link") else ""))
            if item.get("sounds"):
                lines.append(f"  Sounds: {', '.join(item['sounds'])}")
            if item.get("transcript"):
                lines.append(f"  > {item['transcript']}")
    elif kind == "evidence":
        verdict = f" ({block['verdict']})" if block.get("verdict") else ""
        lines.append(f"{block['claim']}{verdict}")
        if block.get("quote"):
            lines.append(f"> {block['quote']}")
    elif kind == "checklist":
        marks = {"pass": "Pass", "fail": "Fail", "uncertain": "Needs review"}
        for item in block["items"]:
            lines.append(f"- {marks[item['status']]}: {item['label']}")
            if item.get("expected"):
                lines.append(f"  Expected: {item['expected']}")
            if item.get("observed"):
                lines.append(f"  Observed: {item['observed']}")
    elif kind == "chart" and block.get("kind") == "scatter":
        lines += ["| Series | Point | x | y |", "| --- | --- | ---: | ---: |"]
        for one in block["series"]:
            for point in one["points"]:
                lines.append(f"| {_md_cell(one['label'])} | {_md_cell(point.get('label'))} | "
                             f"{_cell_text(point['x'])} | {_cell_text(point['y'])} |")
    elif kind == "chart":
        names = [one["label"] for one in block["series"]]
        lines.append("| | " + " | ".join(names) + " |")
        lines.append("| --- |" + " ---: |" * len(names))
        for index, label in enumerate(block["x"]):
            lines.append(f"| {_md_cell(label)} | "
                         + " | ".join(_cell_text(one["values"][index]) for one in block["series"]) + " |")
    elif kind == "map":
        for index, place in enumerate(block["places"], 1):
            where = place.get("address") or f"{place['lat']}, {place['lon']}"
            lines.append(f"{index}. **{place['name']}**: {where}"
                         + (f" ({place['note']})" if place.get("note") else ""))
    elif kind == "timeline":
        for item in block["items"]:
            lines.append(f"- {item['at'] + ': ' if item.get('at') else ''}**{item['title']}**"
                         + (f". {item['detail']}" if item.get("detail") else ""))
    elif kind == "comparison":
        options = block["options"]
        lines.append("| | " + " | ".join(_md_cell(o["name"]) for o in options) + " |")
        lines.append("| --- |" + " --- |" * len(options))
        for label, values in comparison_rows(options):
            lines.append(f"| {_md_cell(label)} | " + " | ".join(_md_cell(v) for v in values) + " |")
        for key, word in (("pros", "Pros"), ("cons", "Cons")):
            if any(o.get(key) for o in options):
                lines.append(f"| {word} | " + " | ".join(_md_cell("; ".join(o.get(key) or ())) for o in options) + " |")
        best = next((o["name"] for o in options if o["id"] == block.get("recommended")), None)
        if best or block.get("verdict"):
            lines += ["", (f"**Pick: {best}.** " if best else "") + (block.get("verdict") or "")]
    elif kind == "gallery":
        for index, item in enumerate(block["items"], 1):
            lines.append(f"{index}. {item.get('caption') or 'Screen'}")
    return "\n".join(lines).rstrip() + "\n"


def to_markdown(value):
    """Every block of an envelope as Markdown, one after the other ("" for none)."""
    blocks = value.get("blocks") if isinstance(value, dict) else None
    return "\n".join(block_markdown(block) for block in blocks or ())


def find(value, block_id, item_id=None):
    """(block, item) in a stored envelope, or (None, None)."""
    for block in (value or {}).get("blocks") or () if isinstance(value, dict) else ():
        if block.get("id") == block_id:
            if item_id is None:
                return block, None
            item = next((one for one in block.get("items") or () if one.get("id") == item_id), None)
            return (block, item) if item is not None else (None, None)
    return None, None


def approval_item(value, block_id, item_id, text):
    """The item an Approve names and its exact words, from the STORED envelope; BlockError when it can't go."""
    block, item = find(value, block_id, item_id)
    if block is None or block.get("type") != "review_queue":
        raise BlockError("That draft isn't in this task's results anymore.")
    if text is not None and not isinstance(text, str):
        raise BlockError("A draft is text.")
    if not item.get("draft") and item.get("yourWords") is not True:
        # An item with no draft has no Approve (an opener to someone who doesn't fit or asked for no DMs, a
        # conversation that can wait): typed words don't make one. Only an item code marked ``yourWords`` takes
        # them (validate stores that mark for nothing a client or a model sent), and then they go through every check
        # below as a draft would: an empty reply, a part in brackets, a secret, no recipient.
        raise BlockError("This one has no draft, so there's nothing to approve.")
    words = _CONTROL.sub("", text if text is not None else item.get("draft") or "").strip()
    if not words:
        raise BlockError("Write the message first. There's nothing to send.")
    if len(words) > 4000:
        raise BlockError("A message is at most 4,000 characters.")
    if BLANK.search(words):
        raise BlockError("Fill in the part in brackets first, then approve it.")
    if is_secret(words):
        raise BlockError("This draft looks like it holds a password or a code, so Mobster won't send it.")
    if not item.get("to"):
        raise BlockError("Mobster doesn't know who this goes to, so it can't send it.")
    return block, item, words


VERBS = {"send_message": "send this message to", "reply": "reply to", "post_comment": "post this comment for"}


def follow_up_goal(block, item, words):
    """The follow-up task's words: exactly this text, to exactly this recipient, in this app, and nothing else."""
    who = item["to"] + (f" ({item['handle']})" if item.get("handle") and item["handle"] != item["to"] else "")
    where = f"In {item['app']}, " if item.get("app") else ""
    verb = VERBS.get(block.get("action"), VERBS["send_message"])
    first = (where + verb) if where else verb[:1].upper() + verb[1:]
    account = f" Send it from {item['account']}." if item.get("account") else ""
    return (f"{first} {who}, word for word: \u201c{words}\u201d.{account} Use these exact words, open the "
            f"conversation with exactly {item['to']}, and send nothing else.")


def preapproval(block, item, words):
    """What the follow-up task's run keeps, so its one send of these words to this person needs no second OK."""
    return {"to": item["to"], "handle": item.get("handle"), "app": item.get("app"), "text": words,
            "source": {"block": block["id"], "item": item["id"]}, "used": False}


def _who(text):
    return " ".join(str(text or "").casefold().lstrip("@").split())


def _plain(text):
    return " ".join(str(text or "").casefold().replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"')
                    .replace("\u201d", '"').replace("\u00a0", " ").split())


def preapproval_matches(spec, request):
    """Whether ``request`` is exactly the send ``spec`` covers (words, shown recipient, app, unused); else it asks."""
    if not isinstance(spec, dict) or spec.get("used") or not isinstance(request, dict):
        return False
    if spec.get("kind") is not None:
        return False  # the content queue's: Run.request_approval matches it itself
    if (request.get("kind") or "action") != "commit" or request.get("act") not in (
            "send_message", "reply", "comment", "post", "send"):
        return False
    if request.get("operation") not in ("TAP", "SUBMIT", "TYPE_SUBMIT"):
        return False
    if request.get("recipient_seen") is not True or not request.get("recipient"):
        return False
    if _plain(request.get("text")) != _plain(spec.get("text")) or not _plain(spec.get("text")):
        return False
    shown = _who(request.get("recipient"))
    if shown not in {who for who in (_who(spec.get("to")), _who(spec.get("handle"))) if who}:
        return False
    if spec.get("app") and _who(request.get("app_name")) != _who(spec["app"]):
        return False
    return True


def from_report(report):
    if not isinstance(report, dict):
        return None
    blocks = []
    own = report.get("blocks")
    if isinstance(own, dict):
        blocks += list(own.get("blocks") or ())
    elif isinstance(own, list):
        blocks += own
    if not any(isinstance(block, dict) and block.get("type") == "review_queue" for block in blocks):
        queue = report.get("queue") if isinstance(report.get("queue"), dict) else None
        rows = queue.get("items") if queue else report.get("drafts")
        if isinstance(rows, list) and rows:
            items = [{"id": row.get("id"), "bucket": row.get("bucket") or "reply", "to": row.get("to"),
                      "app": row.get("app"), "bundleId": row.get("bundleId"),
                      "context": row.get("summary") or row.get("context"), "lastMessage": row.get("lastMessage"),
                      "draft": row.get("draft"), "decision": row.get("decision"), "frameId": row.get("frameId")}
                     for row in rows if isinstance(row, dict) and row.get("to")]
            if items:
                title = report.get("title") if queue and isinstance(report.get("title"), str) else "Drafts to review"
                blocks.insert(0, review_queue(items, title=title))
    return envelope(blocks) if blocks else None
