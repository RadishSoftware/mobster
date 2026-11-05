"""The model's answer as result blocks: the Markdown tables, check lines and dated event lines in a DONE answer become
table, checklist and timeline blocks (``from_answer``), and the prompt line that asks for those shapes
(``ANSWER_SHAPES``, ``wants_shapes``). This changes with the prompt and the models' output habits; the block schema and
its one validation gate stay in ``result_blocks``.
"""

import re

from .result_blocks import (
    MAX_OPTIONS,
    MAX_SERIES,
    NUMERIC_COLUMNS,
    STATUSES,
    BlockError,
    checklist,
    status_word,
    table,
    timeline,
    validate_block,
)

ANSWER_SHAPES = ("\n\nIn your DONE answer, start with one sentence that answers the request. When you checked or "
                 "tested several screens or things, then write one line per check, - [pass] what you checked: what "
                 "you saw (or [fail], [unsure]), and no table. Otherwise write several things that share the same "
                 "details as a Markdown table (a header row, then one row each). For a chart or a graph, write its "
                 "numbers as that table, labels or dates first: Mobster draws it. For places, give the table a Name "
                 "and an Address column. Write events in time order as one line each, - <time or date>: what "
                 "happened.")
WANTS_SHAPES = re.compile(r"\b(list|lists|table|compare|comparison|each|every|all (?:my|the|of)|check|checks|test|go "
                          r"through|rank|ranking|top \d+|which (?:ones|of)|summari[sz]e|breakdown|report|chart|graph|"
                          r"plot|visuali[sz]e|trend|map|nearby|near me|timeline|itinerary|schedule|history|vs\.?|"
                          r"versus)\b", re.I)
CHART_WORDS = re.compile(r"\b(chart|graph|plot|visuali[sz]e|trend|trends|over time|histogram|pie|scatter)\b", re.I)
PIE_WORDS = re.compile(r"\b(pie|donut|share|split|breakdown|proportion|percent of|makeup)\b", re.I)
LINE_WORDS = re.compile(r"\b(line|trend|trends|over time|by (?:day|week|month|year)|daily|weekly|monthly|history)\b",
                        re.I)
SCATTER_WORDS = re.compile(r"\b(scatter|correlat\w*|relationship between)\b", re.I)
COMPARE_WORDS = re.compile(r"\b(compare|comparison|vs\.?|versus|which (?:one |is )?(?:is )?better|pros and cons|"
                           r"difference between|side by side)\b", re.I)
MAP_WORDS = re.compile(r"\b(map|maps|near|nearby|near me|where|directions|around|closest|places)\b", re.I)
ADDRESS_LABELS = re.compile(r"^(address|location|where|place|street|addr\.?)$", re.I)
LAT_LABELS = re.compile(r"^(lat|latitude)$", re.I)
LON_LABELS = re.compile(r"^(lon|lng|long|longitude)$", re.I)


def wants_shapes(request):
    """Whether the stable prompt asks this request's answer for tables and check lines."""
    return bool(WANTS_SHAPES.search(str(request or "")))


_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_CHECK = re.compile(r"^\s*[-*]\s*(?:\[(pass|passed|ok|fail|failed|unsure|uncertain|unknown)\]|(✅|❌|⚠️?))\s*(.+?)\s*$",
                    re.I)
_HEADING = re.compile(r"^\s*#{1,4}\s+(.+?)\s*#*\s*$")
_NUMBER = re.compile(r"^[-+]?[$€£¥]?\s?\d[\d,]*(\.\d+)?\s?(%|[kKmM])?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?$")


def _split_row(line):
    cells, current, escaped = [], "", False
    for char in _ROW.match(line).group(1):
        if escaped:
            current += char
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            cells.append(current.strip())
            current = ""
        else:
            current += char
    cells.append(current.strip())
    return [re.sub(r"^\*\*(.*)\*\*$", r"\1", cell) for cell in cells]


def _column_type(values):
    values = [value for value in values if value]
    if not values:
        return "text"
    if all(_NUMBER.match(value) for value in values):
        if all("%" in value for value in values):
            return "percent"
        if any(symbol in value for value in values for symbol in "$€£¥"):
            return "money"
        return "number" if not any(value[-1:] in "kKmM" for value in values) else "text"
    if all(_DATE.match(value) for value in values):
        return "date"
    if all(value.startswith("https://") and " " not in value for value in values):
        return "link"
    if all(status_word(value) in STATUSES for value in values):
        return "status"
    return "text"


def _title_before(lines, index):
    """A heading or a line ending in ":" right above ``index`` (blank lines between allowed): (title, its index)."""
    at = index - 1
    while at >= 0 and not lines[at].strip():
        at -= 1
    if at < 0:
        return None, None
    heading = _HEADING.match(lines[at])
    text = heading.group(1) if heading else lines[at].strip()[:-1] if lines[at].strip().endswith(":") and len(
        lines[at].strip()) <= 120 and not lines[at].lstrip().startswith(("-", "*", "|")) else None
    return (text.strip("* ") or None, at) if text else (None, None)


_WHEN = (r"(?:\d{1,2}(?::\d{2})?\s?(?:[AaPp]\.?[Mm]\.?)|\d{1,2}:\d{2}|\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2})?|"
         r"(?:Mon|Tue|Tues|Wed|Thu|Thur|Thurs|Fri|Sat|Sun)[a-z]*\.?(?:,? \d{1,2}(?::\d{2})?\s?(?:[AaPp][Mm])?)?|"
         r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.? \d{1,2}(?:, \d{4})?|"
         r"(?:Day|Week) \d{1,3}|(?:19|20)\d{2}|Today|Tonight|Tomorrow|Yesterday|Morning|Noon|Afternoon|Evening)")
_EVENT = re.compile(r"^\s*[-*]\s+(?:\*\*)?(" + _WHEN + r"(?:\s?[-–]\s?" + _WHEN + r")?)"
                    r"(?:\*\*)?\s*(?::|—|–| - )\s*(.+?)\s*$")


def _event(line):
    """(at, title, detail) of a "- 9:00 AM: Boarded the train. Seat 14" line, or None."""
    match = _EVENT.match(line)
    if not match:
        return None
    text = match.group(2).strip()
    title, _, detail = text.partition(". ")
    if len(title) > 120:
        title, detail = text[:117].rstrip() + "…", text
    return match.group(1), title.strip("* ").rstrip("."), detail.strip() or None


def _table_view(block, request):
    """The view a table opens in, from what the request asked: its chart, its map or its options side by side."""
    columns, rows = block["columns"], block["rows"]
    words = str(request or "")
    labels = [c["label"].strip() for c in columns]
    numeric = [c for c in columns if c.get("type") in NUMERIC_COLUMNS]
    if (any(ADDRESS_LABELS.match(l) for l in labels) or (any(LAT_LABELS.match(l) for l in labels)
                                                         and any(LON_LABELS.match(l) for l in labels))) \
            and MAP_WORDS.search(words):
        block["view"] = "map"
        return block
    if CHART_WORDS.search(words) and numeric and len(rows) >= 2:
        first = next((c for c in columns if c not in numeric), None)
        if SCATTER_WORDS.search(words) and len(numeric) >= 2:
            chart = {"kind": "scatter", "x": numeric[0]["key"], "y": [numeric[1]["key"]]}
        else:
            if PIE_WORDS.search(words) and len(rows) <= 8 and all(
                    isinstance(r["cells"].get(numeric[0]["key"]), (int, float)) and r["cells"][numeric[0]["key"]] >= 0
                    for r in rows):
                kind = "pie"
            elif (first is not None and first.get("type") == "date") or LINE_WORDS.search(words):
                kind = "area" if re.search(r"\barea\b", words, re.I) else "line"
            else:
                kind = "bar"
            chart = {"kind": kind, "y": [c["key"] for c in numeric][:1 if kind == "pie" else MAX_SERIES]}
            if first is not None:
                chart["x"] = first["key"]
        block["chart"], block["view"] = chart, "chart"
        return block
    if COMPARE_WORDS.search(words) and 2 <= len(rows) <= MAX_OPTIONS and len(columns) >= 3:
        block["view"] = "compare"
    return block


def from_answer(text, request=None):
    if not isinstance(text, str):
        return text, []
    lines, blocks, drop, index = text.split("\n"), [], set(), 0
    while index < len(lines):
        if _ROW.match(lines[index]) and index + 1 < len(lines) and _RULE.match(lines[index + 1]):
            end = index + 2
            while end < len(lines) and _ROW.match(lines[end]):
                end += 1
            header, body = _split_row(lines[index]), [_split_row(line) for line in lines[index + 2:end]]
            if body and len(header) >= 2:
                width = len(header)
                body = [(row + [""] * width)[:width] for row in body]
                kinds = [_column_type([row[i] for row in body]) for i in range(width)]
                title, at = _title_before(lines, index)
                columns = [{"label": label or f"Column {i + 1}", "type": kinds[i]} for i, label in enumerate(header)]
                try:
                    shaped = validate_block(table(columns, body, title=title, id=f"t{len(blocks) + 1}"))
                except BlockError:  # a table a block can't hold (17+ columns) stays in the answer as text
                    index = end
                    continue
                blocks.append((index, _table_view(shaped, request) if request else shaped))
                drop.update(range(index, end))
                if at is not None:
                    drop.add(at)
                index = end
                continue
        if _event(lines[index]):
            end = index
            while end < len(lines) and _event(lines[end]):
                end += 1
            if end - index >= 3:
                items = []
                for line in lines[index:end]:
                    at, title, detail = _event(line)
                    items.append({"at": at, "title": title, "detail": detail})
                title, at = _title_before(lines, index)
                blocks.append((index, timeline(items, title=title, id=f"e{len(blocks) + 1}")))
                drop.update(range(index, end))
                if at is not None:
                    drop.add(at)
                index = end
                continue
        if _CHECK.match(lines[index]):
            end = index
            while end < len(lines) and _CHECK.match(lines[end]):
                end += 1
            if end - index >= 2:
                items = []
                for line in lines[index:end]:
                    match = _CHECK.match(line)
                    word = (match.group(1) or {"✅": "pass", "❌": "fail"}.get(match.group(2), "uncertain")).lower()
                    label, _, observed = match.group(3).partition(": ")
                    items.append({"label": label.strip("* ") or match.group(3), "status": status_word(word),
                                  "observed": observed.strip() or None})
                title, at = _title_before(lines, index)
                blocks.append((index, checklist(items, title=title, id=f"c{len(blocks) + 1}")))
                drop.update(range(index, end))
                if at is not None:
                    drop.add(at)
                index = end
                continue
        index += 1
    if not blocks:
        return text, []
    rest = "\n".join(line for i, line in enumerate(lines) if i not in drop)
    rest = re.sub(r"\n{3,}", "\n\n", rest).strip()
    return rest, [block for _, block in sorted(blocks, key=lambda pair: pair[0])]
