"""A task's result as one self-contained HTML page (every string escaped), then a PDF or PNG made on this Mac."""

import base64
import html
import re
import subprocess
import tempfile
import time
from pathlib import Path

from . import brand, brand_fonts, chart_svg, links, result_blocks

RENDER_SECONDS = 45
STATUS = {"pass": ("Pass", "var(--green)", "✓"), "fail": ("Fail", "var(--coral)", "✕"),
          "uncertain": ("Needs review", "var(--amber)", "?")}


class RenderError(RuntimeError):
    pass


def _e(value):
    return html.escape("" if value is None else str(value), quote=True)


def _people(envelope):
    groups = []

    def add(*names):
        spellings = set()
        for name in names:
            if isinstance(name, str) and len(name.strip()) > 1:
                name = name.strip()
                spellings |= {name, name.lstrip("@")}
                first = name.lstrip("@").split()[0]
                if " " in name and len(first) >= 3 and first[:1].isupper():
                    spellings.add(first)
        if spellings:
            groups.append(spellings)

    for block in (envelope or {}).get("blocks") or ():
        for item in block.get("items") or ():
            add(item.get("to"), item.get("handle"))
            add(item.get("account"))
            add(item.get("creator"))
        for row in block.get("rows") or ():
            for value in (row.get("cells") or {}).values():
                if isinstance(value, str) and re.fullmatch(r"@[\w.]{2,40}", value.strip()):
                    add(value)
    return groups


def masker(envelope):
    labels, spellings = {}, []
    for group in _people(envelope):
        key = next((labels[s.casefold()] for s in group if s.casefold() in labels), None)
        label = key or f"Person {len(set(labels.values())) + 1}"
        for spelling in group:
            labels.setdefault(spelling.casefold(), label)
            spellings.append(spelling)
    spellings.sort(key=len, reverse=True)
    # Lookarounds, not \b: a phone number starts with "+", which has no word boundary before it
    # (a share link leaked it).
    pattern = (re.compile(r"(?<!\w)@?(?:" + "|".join(re.escape(s) for s in spellings) + r")(?!\w)", re.I) if spellings
               else None)

    def mask(text):
        if not isinstance(text, str) or not text:
            return text
        if pattern is not None:
            text = pattern.sub(lambda m: labels.get(m.group(0).lstrip("@").casefold(),
                                                    labels.get(m.group(0).casefold(), "Person")), text)
        return re.sub(r"(?<![\w@])@[A-Za-z0-9_.]{2,40}", "@someone", text)

    return mask


def _walk(value, change):
    if isinstance(value, str):
        return change(value)
    if isinstance(value, list):
        return [_walk(item, change) for item in value]
    if isinstance(value, dict):
        return {key: (item if key in ("type", "id", "frameId", "bundleId", "kind", "key", "action", "bucket", "status",
                                      "verdict") else _walk(item, change)) for key, item in value.items()}
    return value


def _inline(text):
    out = _e(text)
    out = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<em>\1</em>", out)
    out = re.sub(r"\[([^\]\n]+)\]\((https://[^\s)&\"<>]+)\)", r'<a href="\2">\1</a>', out)
    return out


def _md(text):
    lines, out, index = str(text or "").split("\n"), [], 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if line.strip().startswith("```"):
            body, index = [], index + 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                body.append(lines[index])
                index += 1
            out.append("<pre><code>" + _e("\n".join(body)) + "</code></pre>")
            index += 1
            continue
        heading = re.match(r"\s*(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if heading:
            out.append(f"<h4>{_inline(heading.group(2))}</h4>")
            index += 1
            continue
        if re.match(r"\s*([-*+]|\d{1,3}[.)])\s+", line):
            ordered = bool(re.match(r"\s*\d", line))
            items = []
            while index < len(lines) and re.match(r"\s*([-*+]|\d{1,3}[.)])\s+", lines[index]):
                items.append(re.sub(r"^\s*([-*+]|\d{1,3}[.)])\s+", "", lines[index]))
                index += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{_inline(item)}</li>" for item in items) + f"</{tag}>")
            continue
        if line.lstrip().startswith(">"):
            body = []
            while index < len(lines) and lines[index].lstrip().startswith(">"):
                body.append(lines[index].lstrip()[1:].strip())
                index += 1
            out.append(f"<blockquote>{_inline(' '.join(body))}</blockquote>")
            continue
        body = []
        while (index < len(lines) and lines[index].strip()
               and not re.match(r"\s*(#{1,6}\s|```|>|[-*+]\s|\d{1,3}[.)]\s)", lines[index])):
            body.append(lines[index].strip())
            index += 1
        out.append(f"<p>{_inline(' '.join(body))}</p>")
    return "".join(out)


def _frame(frames, frame_id):
    return (frames(frame_id) if frames and frame_id else None) or None


def _data_uri(data):
    return "data:image/jpeg;base64," + base64.b64encode(data).decode() if data else None


def _image(frames, frame_id):
    return _data_uri(_frame(frames, frame_id))


def _crop(src, crop, data):
    if not crop:
        return f'<img class="shot" src="{src}" alt="The screen it rests on">'
    try:
        import io

        from PIL import Image
        width, height = Image.open(io.BytesIO(data)).size
    except Exception:  # noqa: BLE001 -- an unreadable frame shows whole
        return f'<img class="shot" src="{src}" alt="The screen it rests on">'
    ratio = (crop["w"] * width) / max(1e-6, crop["h"] * height)
    style = (f'width:{100 / crop["w"]:.3f}%;height:{100 / crop["h"]:.3f}%;'
             f'left:{-crop["x"] / crop["w"] * 100:.3f}%;top:{-crop["y"] / crop["h"] * 100:.3f}%')
    return (f'<div class="crop" style="aspect-ratio:{ratio:.4f}"><img src="{src}" style="{style}" '
            'alt="The part of the screen it rests on"></div>')


def _date(text):
    parts = text[:10].split("-")
    try:
        month = chart_svg.MONTHS[int(parts[1]) - 1]
    except (ValueError, IndexError):
        return text
    if len(parts) == 2:
        return f"{month} {parts[0]}"
    day = f"{month} {int(parts[2])}, {parts[0]}"
    clock = re.match(r"[T ](\d{2}):(\d{2})", text[10:])
    if clock:
        hour, minute = int(clock[1]), clock[2]
        day += f", {hour % 12 or 12}:{minute} {'AM' if hour < 12 else 'PM'}"
    return day


def _cell(value, column):
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    kind = column.get("type")
    if isinstance(value, (int, float)):
        if kind == "money":
            return (_e(f"${value:,.2f}") if column.get("currency", "USD") == "USD"
                    else _e(f"{value:,.2f} {column['currency']}"))
        if kind == "percent":
            return _e(f"{value:g}%")
        if kind == "duration":
            whole = int(round(value))
            return _e(f"{whole // 60}:{whole % 60:02d}")
        return _e(f"{value:,}" if isinstance(value, int) else f"{value:,.6g}")
    if kind == "date" and re.fullmatch(r"\d{4}-\d{2}(-\d{2})?([T ].*)?", str(value)):
        return _e(_date(str(value)))
    if kind == "status" and value in STATUS:
        word, color, mark = STATUS[value]
        return f'<span class="status" style="color:{color}">{mark}</span> {word}'
    if kind == "link" and str(value).startswith("https://"):
        host = re.sub(r"^https://(www\.)?", "", str(value)).split("/")[0]
        return f'<a href="{_e(value)}">{_e(host)}</a>'
    return _e(value)


TIMELINE_MARKS = {"done": ("✓", "var(--green)"), "pass": ("✓", "var(--green)"), "fail": ("✕", "var(--coral)"),
                  "uncertain": ("?", "var(--amber)"), "now": ("●", "var(--ink)"), "next": ("○", "var(--ink-3)")}


def _map_figure(block, draw):
    from . import maps
    places = maps.places_of(block)
    if not places:
        return ""
    drawn = None
    if draw is not None:
        try:
            drawn = draw(block)
        except Exception:  # noqa: BLE001 -- the list still says where each place is
            drawn = None
    numbers = {place["id"]: index for index, place in enumerate(places, 1)}
    figure = ""
    if drawn and drawn.get("image"):
        width, height = drawn.get("width") or 720, drawn.get("height") or 360
        src = "data:image/png;base64," + base64.b64encode(drawn["image"]).decode()
        pins = "".join(f'<span class="pin" style="left:{p["x"] / width * 100:.2f}%;top:{p["y"] / height * 100:.2f}%">'
                       f'{numbers.get(p["id"], "")}</span>' for p in drawn.get("places") or ()
                       if p.get("id") in numbers)
        figure = (f'<div class="map" style="aspect-ratio:{width}/{height}"><img src="{src}" alt="A map of these '
                  f'places">{pins}</div>')
    rows = []
    for place in places:
        source = next((p for p in block.get("places") or () if p.get("id") == place["id"]), place)
        where = source.get("address") or (f'{source["lat"]:.5f}, {source["lon"]:.5f}' if "lat" in source else "")
        note = f'<span class="meta"> {_e(source["note"])}</span>' if source.get("note") else ""
        rows.append(f'<li><span class="num">{numbers[place["id"]]}</span><div><p><strong>{_e(place["name"])}</strong>'
                    f'{note}</p><p class="meta">{_e(where)}</p></div></li>')
    return figure + f'<ol class="places">{"".join(rows)}</ol>'


def _block(block, frames, screens, draw_map=None):
    kind, parts = block["type"], []
    title = block.get("title")
    if kind == "table" and block.get("view") == "map":
        parts.append(_map_figure(block, draw_map))
    elif kind == "table" and block.get("view") == "chart":
        spec = chart_svg.from_table(block)
        if spec:
            parts.append(
                f'<figure class="chart-figure">'
                f'{chart_svg.render({**spec, "title": title}, width=540, height=220)}</figure>')
    if kind == "table" and block.get("view") == "compare":
        columns, rows = block["columns"], block["rows"]
        options = [{"id": str(i), "name": _cell(r["cells"].get(columns[0]["key"]), columns[0]),
                    "facts": [{"label": c["label"], "value": _cell(r["cells"].get(c["key"]), c)} for c in columns[1:]]}
                   for i, r in enumerate(rows)]
        parts.append(_comparison_html({"options": options}, escaped=True))
    elif kind == "table":
        numeric = {c["key"] for c in block["columns"] if c["type"] in result_blocks.NUMERIC_COLUMNS}
        head = "".join(f'<th{" class=n" if c["key"] in numeric else ""}>{_e(c["label"])}</th>'
                       for c in block["columns"])
        rows = "".join("<tr>" + "".join(f'<td{" class=n" if c["key"] in numeric else ""}>'
                                        f'{_cell(row["cells"].get(c["key"]), c)}</td>' for c in block["columns"])
                       + "</tr>" for row in block["rows"])
        parts.append(f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>")
        if block.get("truncated"):
            parts.append(f'<p class="note">The first {len(block["rows"])} of {block["truncated"]} rows.</p>')
    elif kind == "review_queue":
        labels = {b["id"]: b["label"] for b in block.get("buckets") or ()}
        for item in block["items"]:
            meta = " ".join(_e(part) for part in (item.get("handle"), item.get("app"),
                                                  f"from {item['account']}" if item.get("account") else None,
                                                  labels.get(item.get("bucket"))) if part)
            body = [f'<p class="who"><strong>{_e(item["to"])}</strong> <span class="meta">{meta}</span></p>']
            if item.get("context"):
                body.append(f'<p class="sub">{_e(item["context"])}</p>')
            if item.get("lastMessage"):
                body.append(f"<blockquote>{_e(item['lastMessage'])}</blockquote>")
            if item.get("decision"):
                body.append(f'<p class="decision">Needs your decision: {_e(item["decision"])}</p>')
            if item.get("draft"):
                body.append(f'<p class="draft">{_e(item["draft"])}</p>')
            parts.append(f'<div class="item">{"".join(body)}</div>')
        parts.append('<p class="note">Nothing here was sent.</p>')
    elif kind == "checklist":
        for item in block["items"]:
            word, color, mark = STATUS.get(item["status"], STATUS["uncertain"])
            facts = "".join(f"<dt>{name}</dt><dd>{_e(item[key])}</dd>" for key,
                            name in (("expected", "Expected"), ("observed", "Saw")) if item.get(key))
            shot = _image(frames, item.get("frameId")) if screens else None
            image = f'<img class="thumb" src="{shot}" alt="">' if shot else ""
            facts_html = f"<dl>{facts}</dl>" if facts else ""
            parts.append(f'<div class="check"><span class="status" style="color:{color}">{mark} {word}</span>'
                         f'<div><p><strong>{_e(item["label"])}</strong></p>{facts_html}</div>{image}</div>')
    elif kind == "evidence":
        data = _frame(frames, block.get("frameId")) if screens else None
        shot = _data_uri(data)
        verdict = STATUS.get(block.get("verdict"))
        lead = f'<span class="status" style="color:{verdict[1]}">{verdict[2]} {verdict[0]}</span> ' if verdict else ""
        parts.append(f'<p class="claim">{lead}{_e(block["claim"])}</p>')
        if block.get("quote"):
            parts.append(f"<blockquote>{_e(block['quote'])}</blockquote>")
        if shot:
            parts.append(_crop(shot, block.get("crop"), data))
    elif kind == "media_list":
        for item in block["items"]:
            shot = _image(frames, item.get("frameId")) if screens else None
            lines = [f'<p><strong>{_e(item.get("title") or item.get("creator") or "Item")}</strong></p>']
            if item.get("title") and item.get("creator"):
                lines.append(f'<p class="meta">{_e(item["creator"])}</p>')
            if item.get("sounds"):
                lines.append(f'<p class="meta">Sounds: {_e(", ".join(item["sounds"]))}</p>')
            if item.get("transcript"):
                lines.append(f'<p class="sub">“{_e(item["transcript"])}”</p>')
            image = f'<img class="thumb" src="{shot}" alt="">' if shot else ""
            parts.append(f'<div class="media">{image}<div>{"".join(lines)}</div></div>')
    elif kind == "markdown":
        parts.append(f'<div class="prose">{_md(block["text"])}</div>')
    elif kind == "chart":
        parts.append(f'<figure class="chart-figure">{chart_svg.render(block, width=540, height=220)}</figure>')
    elif kind == "map":
        parts.append(_map_figure(block, draw_map))
    elif kind == "timeline":
        rows = []
        for item in block["items"]:
            mark, colour = TIMELINE_MARKS.get(item.get("state"), ("", ""))
            dot = f'<span class="dot" style="color:{colour}">{mark}</span>' if mark else '<span class="dot"></span>'
            detail = f'<p class="sub">{_e(item["detail"])}</p>' if item.get("detail") else ""
            rows.append(f'<li><span class="at">{_e(item.get("at") or "")}</span>{dot}<div><p><strong>'
                        f'{_e(item["title"])}</strong></p>{detail}</div></li>')
        parts.append(f'<ol class="timeline">{"".join(rows)}</ol>')
    elif kind == "comparison":
        parts.append(_comparison_html(block))
    elif kind == "gallery":
        shots = []
        for item in block["items"]:
            shot = _image(frames, item.get("frameId")) if screens else None
            if shot:
                caption = f'<figcaption>{_e(item["caption"])}</figcaption>' if item.get("caption") else ""
                shots.append(f'<figure><img src="{shot}" alt="">{caption}</figure>')
        parts.append(f'<div class="gallery">{"".join(shots)}</div>' if shots else
                     '<p class="note">' + _e(", ".join(i.get("caption") or "Screen" for i in block["items"])) + "</p>")
    head = f'<h3 id="{_e(block["id"])}">{_e(title)}</h3>' if title else ""
    # A small card stays on one page; a long table or list flows across pages, its header row repeated.
    size = len(block.get("rows") or block.get("items") or ())
    small = " small" if size <= 12 else ""
    return f'<section class="block{small}" data-block="{_e(kind)}">{head}{"".join(parts)}</section>'


def _comparison_html(block, escaped=False):
    options = block["options"]
    picked = block.get("recommended")
    head = "".join(f'<th class="{"pick" if o["id"] == picked else ""}">{o["name"] if escaped else _e(o["name"])}'
                   + ('<span class="badge">Pick</span>' if o["id"] == picked else "")
                   + (f'<span class="meta"> {_e(o["subtitle"])}</span>' if o.get("subtitle") else "") + "</th>"
                   for o in options)
    body = []
    for label, values in result_blocks.comparison_rows(options):
        cells = "".join(f'<td>{value if escaped else _e(value)}</td>' for value in values)
        body.append(f'<tr><th scope="row">{_e(label)}</th>{cells}</tr>')
    for key, word, mark in (("pros", "Pros", "+"), ("cons", "Cons", "−")):
        if any(o.get(key) for o in options):
            cells = "".join("<td>" + "".join(f'<p class="pc">{mark} {_e(x)}</p>' for x in o.get(key) or ()) + "</td>"
                            for o in options)
            body.append(f'<tr><th scope="row">{word}</th>{cells}</tr>')
    verdict = f'<p class="verdict">{_e(block["verdict"])}</p>' if block.get("verdict") else ""
    return (
        f'<table class="compare"><thead><tr><th></th>{head}</tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table>{verdict}')


CSS = """
html { background: #fff; }
body { margin: 0; padding: 4px 2px 8px; background: #fff; color: var(--ink); font: 400 10.5pt/1.55 var(--font-sans);
  -webkit-font-smoothing: antialiased; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body.card { padding: 20px; background: var(--paper); }
body.share { max-width: 760px; margin: 0 auto; padding: 40px 20px 48px; }
@media (max-width: 560px) { body.share { padding: 24px 16px 32px; } .check { grid-template-columns: 56px 1fr; } .check .thumb { display: none; } }
html:has(body.card) { background: var(--paper); }
header.doc { display: flex; gap: 14px; align-items: flex-start; margin-bottom: 20px; }
header.doc svg { width: 34px; height: 34px; flex: none; margin-top: 2px; }
h1 { margin: 0; font-size: 19pt; line-height: 1.25; font-weight: var(--weight-title); letter-spacing: -0.01em; }
.dateline { margin: 4px 0 0; color: var(--ink-3); font-size: 9pt; }
.answer { margin: 0 0 6px; font-size: 13pt; line-height: 1.45; font-weight: var(--weight-medium); }
.rest { margin: 0 0 4px; color: var(--ink-2); white-space: pre-wrap; }
section.block.small { break-inside: avoid; }
section.block { margin-top: 22px; padding: 16px 18px; border-radius: 14px; background: var(--card); break-inside: auto; }
body.card section.block { margin-top: 0; }
.credit { display: flex; align-items: center; justify-content: flex-end; gap: 5px; margin: 10px 4px 0; color: var(--ink-3);
  font-size: 8pt; line-height: 1; }
.credit svg { width: 11px; height: 11px; flex: none; }
section.block h3 { margin: 0 0 10px; font-size: 11pt; font-weight: var(--weight-medium); }
table { width: 100%; border-collapse: collapse; font-size: 9.5pt; font-variant-numeric: tabular-nums; }
thead { display: table-header-group; }
th { padding: 4px 6px; border-bottom: 0.75pt solid var(--line-strong); color: var(--ink-3); font-size: 8.5pt; font-weight: var(--weight-medium); text-align: left; }
td { padding: 5px 6px; vertical-align: top; }
tr { break-inside: avoid; }
.n { text-align: right; }
a { color: var(--ink); text-decoration: underline; text-decoration-color: var(--line-strong); }
.item, .check, .media { padding: 8px 0; break-inside: avoid; }
.item + .item, .check + .check, .media + .media { border-top: 0.5pt solid var(--line); }
.who { margin: 0; }
.meta { color: var(--ink-3); font-size: 8.5pt; }
.sub { margin: 2px 0 0; color: var(--ink-2); }
blockquote { margin: 6px 0; padding-left: 10px; border-left: 1.5pt solid var(--line-strong); color: var(--ink-2); }
.decision { margin: 4px 0 0; color: var(--amber); font-size: 9pt; }
.draft { margin: 6px 0 0; padding: 8px 10px; border-radius: 8px; background: var(--paper); }
.note { margin: 10px 0 0; color: var(--ink-3); font-size: 8.5pt; }
.check { display: grid; grid-template-columns: 64px 1fr auto; gap: 10px; align-items: start; }
.check p { margin: 0; }
.status { font-weight: var(--weight-medium); white-space: nowrap; }
dl { display: grid; grid-template-columns: auto 1fr; gap: 1px 10px; margin: 3px 0 0; font-size: 9pt; }
dt { color: var(--ink-3); } dd { margin: 0; color: var(--ink-2); }
.thumb { width: 44px; border-radius: 6px; box-shadow: 0 0 0 0.5pt var(--line-strong); }
.shot { display: block; max-width: 240px; max-height: 360px; margin-top: 10px; border-radius: 10px; box-shadow: 0 0 0 0.5pt var(--line-strong); break-inside: avoid; }
.crop { position: relative; width: 300px; max-width: 100%; margin-top: 10px; overflow: hidden; border-radius: 10px; box-shadow: 0 0 0 0.5pt var(--line-strong); break-inside: avoid; }
.crop img { position: absolute; max-width: none; }
.claim { margin: 0; font-size: 11pt; }
.media { display: grid; grid-template-columns: auto 1fr; gap: 12px; }
.media p { margin: 0; }
.prose h4 { margin: 12px 0 4px; font-size: 10.5pt; font-weight: var(--weight-medium); }
.prose p, .prose ul, .prose ol { margin: 0 0 8px; }
.prose ul, .prose ol { padding-left: 18px; }
.prose code { font-family: var(--font-mono); font-size: 0.9em; }
.prose pre { padding: 8px 10px; border-radius: 8px; background: var(--paper); font-size: 8.5pt; white-space: pre-wrap; }
.chart-figure { margin: 4px 0 0; break-inside: avoid; }
.chart-svg { display: block; width: 100%; height: auto; }
.map { position: relative; overflow: hidden; border-radius: 10px; box-shadow: 0 0 0 0.5pt var(--line-strong); break-inside: avoid; }
.map img { display: block; width: 100%; height: 100%; }
.pin { position: absolute; display: grid; place-items: center; width: 18px; height: 18px; margin: -9px 0 0 -9px; border-radius: 50%;
  background: var(--ink); color: #fff; box-shadow: 0 0 0 2px #fff; font-size: 8pt; font-weight: var(--weight-medium); font-variant-numeric: tabular-nums; }
ol.places { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 18px; margin: 12px 0 0; padding: 0; list-style: none; }
ol.places li { display: grid; grid-template-columns: 18px 1fr; gap: 8px; break-inside: avoid; }
ol.places p { margin: 0; }
.num { display: grid; place-items: center; width: 18px; height: 18px; border-radius: 50%; background: var(--ink); color: #fff; font-size: 8pt; font-weight: var(--weight-medium); }
ol.timeline { margin: 0; padding: 0; list-style: none; }
ol.timeline li { position: relative; display: grid; grid-template-columns: 76px 16px 1fr; gap: 8px; padding: 0 0 10px; break-inside: avoid; }
ol.timeline li::before { content: ""; position: absolute; left: 91.5px; top: 14px; bottom: 0; width: 1px; background: var(--line-strong); }
ol.timeline li:last-child::before { display: none; }
ol.timeline p { margin: 0; }
.at { color: var(--ink-3); font-size: 9pt; text-align: right; font-variant-numeric: tabular-nums; }
.dot { position: relative; z-index: 1; display: grid; place-items: center; width: 16px; height: 16px; border-radius: 50%; background: var(--card); font-size: 8pt; }
.dot:empty::after { content: ""; width: 7px; height: 7px; border-radius: 50%; background: var(--ink-3); }
table.compare { table-layout: fixed; }
table.compare th, table.compare td { padding: 6px 8px; border-bottom: 0.5pt solid var(--line); vertical-align: top; text-align: left; }
table.compare thead th { color: var(--ink); font-size: 10.5pt; font-weight: var(--weight-medium); border-bottom: 0.75pt solid var(--line-strong); }
table.compare tbody th { width: 22%; color: var(--ink-3); font-size: 8.5pt; font-weight: var(--weight-medium); }
table.compare .pick { background: var(--paper); }
.badge { display: inline-block; margin-left: 6px; padding: 1px 6px; border-radius: 999px; background: var(--ink); color: #fff; font-size: 7.5pt; vertical-align: 1px; }
.pc { margin: 0 0 2px; color: var(--ink-2); }
.verdict { margin: 10px 0 0; color: var(--ink); }
.gallery { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
.gallery figure { margin: 0; break-inside: avoid; }
.gallery img { display: block; width: 100%; border-radius: 10px; box-shadow: 0 0 0 0.5pt var(--line-strong); }
.gallery figcaption { margin-top: 4px; color: var(--ink-2); font-size: 8.5pt; }
section.cover { display: flex; flex-direction: column; height: 8.7in; break-after: page; break-inside: avoid; }
.cover .eyebrow { margin: 0.9in 0 10px; color: var(--ink-3); font-size: 9pt; letter-spacing: 0.06em; text-transform: uppercase; }
.cover h1 { max-width: 6in; font-size: 30pt; line-height: 1.12; letter-spacing: -0.02em; }
.cover .lead { max-width: 5.6in; margin: 18px 0 0; color: var(--ink-2); font-size: 14pt; line-height: 1.4; }
.cover .contents { margin-top: auto; padding-top: 14px; border-top: 0.75pt solid var(--line-strong); }
.cover .contents h2 { margin: 0 0 8px; color: var(--ink-3); font-size: 8.5pt; font-weight: var(--weight-medium); text-transform: uppercase; letter-spacing: 0.06em; }
.cover .contents ol { display: grid; grid-template-columns: 1fr 1fr; gap: 0 24px; margin: 0; padding: 0; list-style: none; }
.cover .contents li { display: flex; gap: 10px; padding: 4px 0; border-top: 0.5pt solid var(--line); }
.cover .contents li span { color: var(--ink-3); font-variant-numeric: tabular-nums; }
.cover .facts { display: flex; gap: 28px; margin: 22px 0 0; color: var(--ink-3); font-size: 9pt; }
.cover .facts strong { display: block; color: var(--ink); font-size: 11pt; font-weight: var(--weight-medium); }
.cover svg.mark { width: 40px; height: 40px; }
footer.sources { margin-top: 26px; padding-top: 12px; border-top: 0.5pt solid var(--line); color: var(--ink-2); font-size: 9pt; break-inside: avoid; }
footer.sources h2 { margin: 0 0 6px; color: var(--ink-3); font-size: 8.5pt; font-weight: var(--weight-medium); }
footer.sources ol { margin: 0; padding-left: 16px; }
.made { margin-top: 18px; color: var(--ink-4); font-size: 8pt; }
.made a { color: inherit; }
"""  # noqa: E501


def _when(ms):
    if not isinstance(ms, (int, float)):
        return None
    return time.strftime("%b %-d, %Y, %-I:%M %p", time.localtime(ms / 1000))


ROBOTS = '<meta name="robots" content="noindex, nofollow, noarchive, nosnippet, noimageindex">'


KIND_WORDS = {"table": "Table", "chart": "Chart", "map": "Map", "timeline": "Timeline", "comparison": "Comparison",
              "gallery": "Screens", "checklist": "Checks", "evidence": "Evidence", "media_list": "Media",
              "review_queue": "Review queue", "markdown": "Notes"}


def _cover(heading, answer, when, where, chosen):
    eyebrow = "   ".join(_e(part) for part in (when, f"In {where}" if where else None) if part) or "Mobster report"
    lead = f'<p class="lead">{_e(answer)}</p>' if answer else ""
    items = "".join(
        f'<li><span>{index:02d}</span>{_e(block.get("title") or KIND_WORDS.get(block["type"], "Card"))}</li>'
        for index, block in enumerate(chosen, 1))
    contents = f'<div class="contents"><h2>In this report</h2><ol>{items}</ol></div>' if chosen else ""
    mark = brand.MARK["light"].replace("<svg", '<svg class="mark"', 1)
    return (f'<section class="cover">{mark}<p class="eyebrow">{eyebrow}</p><h1>{_e(heading)}</h1>{lead}'
            f"{contents}</section>")


def document(run, *, blocks=None, frames=None, people="show", screens=True, card=False, share=False, title=None,
             report=False, draw_map=None):
    summary = run.get("summary") or {}
    envelope = summary.get("blocks") if isinstance(summary.get("blocks"), dict) else None
    chosen = [b for b in (envelope or {}).get("blocks") or () if blocks is None or b.get("id") in blocks]
    answer, rest = summary.get("answer"), summary.get("data") if isinstance(summary.get("data"), str) else None
    heading = run.get("goal") or "Mobster result"
    proof = [p for p in summary.get("proof") or () if isinstance(p, dict) and p.get("quote")]
    if people == "mask":
        mask = masker(envelope)
        chosen = _walk(chosen, mask)
        answer, rest, heading = mask(answer), mask(rest), mask(heading)
        proof = _walk(proof, mask)
    if title:
        heading = " ".join(str(title).split())[:120]
    if rest and answer and rest.strip().startswith(str(answer).strip()):
        rest = rest.strip()[len(str(answer).strip()):].strip() or None
    when = _when(run.get("finishedAt"))
    where = run.get("appName") if run.get("appName") and run.get("appName") != "Any app" else None
    cards = [_block(block, frames, screens, draw_map) for block in chosen]
    footer = []
    if proof:
        items = "".join("<li>“" + _e(p["quote"]) + "”" + (f" in {_e(p['app'])}" if p.get("app") else "")
                        + (f" › {_e(p['screen'])}" if p.get("screen") else "") + "</li>" for p in proof[:12])
        footer.append(f'<footer class="sources"><h2>Sources, as the screen showed them</h2><ol>{items}</ol></footer>')
    footer.append(f'<p class="made">Made with <a href="{links.MADE_WITH_SHARE}">Mobster</a> on an iPhone. '
                  "Mobster read these screens; check anything important before you act on it.</p>")
    if card:
        # An image of one card ends with where it came from, as a run's clip does (run_gif.py): the mark and
        # "mobster.dev", small, under the card.
        mark = brand.MARK["light"].replace("<svg", '<svg aria-hidden="true"', 1)
        body = cards + [f'<p class="credit">{mark}mobster.dev</p>']
    elif report and chosen:
        body = [_cover(heading, answer, when, where, chosen)]
        if rest:
            body.append(f'<p class="rest">{_e(rest)}</p>')
        body += cards + footer
    else:
        dateline = "   ".join(_e(part) for part in (when, f"In {where}" if where else None) if part)
        dateline_html = f'<p class="dateline">{dateline}</p>' if dateline else ""
        body = [f'<header class="doc">{brand.MARK["light"]}<div><h1>{_e(heading)}</h1>{dateline_html}</div></header>']
        if answer:
            body.append(f'<p class="answer">{_e(answer)}</p>')
        if rest:
            body.append(f'<p class="rest">{_e(rest)}</p>')
        body += cards + footer
    head = (ROBOTS + '<meta name="viewport" content="width=device-width, initial-scale=1">') if share else ""
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">" + head +
            "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; img-src data:; "
            "font-src data:; style-src 'unsafe-inline'\">"
            f"<title>{_e(heading)}</title><style>{brand_fonts.FONT_FACES}{brand.LIGHT_STYLESHEET}{CSS}</style></head>"
            f'<body class="{"card" if card else "doc share" if share else "doc"}">{"".join(body)}</body></html>')


def markdown(run, *, blocks=None, people="show"):
    summary = run.get("summary") or {}
    envelope = summary.get("blocks") if isinstance(summary.get("blocks"), dict) else None
    chosen = [b for b in (envelope or {}).get("blocks") or () if blocks is None or b.get("id") in blocks]
    text = [f"# {run.get('goal') or 'Mobster result'}", ""]
    if summary.get("answer"):
        text += [str(summary["answer"]), ""]
    rest = summary.get("data") if isinstance(summary.get("data"), str) else None
    if rest and rest.strip() != str(summary.get("answer") or "").strip():
        text += [rest.strip(), ""]
    text.append(result_blocks.to_markdown({"blocks": chosen}))
    out = "\n".join(text).rstrip() + "\n"
    return masker(envelope)(out) if people == "mask" else out


def render(document_html, kind, *, title="", width=720, helper=None, timeout=RENDER_SECONDS):
    if kind not in ("pdf", "png"):
        raise ValueError("kind is pdf or png")
    if helper is None:
        from . import native_helpers
        helper = native_helpers.find("pdf")
        if helper is None:
            try:
                helper = native_helpers.build("pdf")
            except (LookupError, OSError, subprocess.SubprocessError) as error:
                raise RenderError("Mobster can't make PDFs on this Mac: " + str(error)) from None
    with tempfile.TemporaryDirectory(prefix="mobster-artifact-") as folder:
        source, target = Path(folder) / "document.html", Path(folder) / f"document.{kind}"
        source.write_text(document_html, encoding="utf-8")
        command = [str(helper), str(source), str(target)] + (["--png", "--width", str(int(width))] if kind == "png"
                                                             else ["--title", title[:120]])
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RenderError("Mobster took too long to make the file. Try again.") from None
        except OSError as error:
            raise RenderError(f"Mobster couldn't make the file: {error}") from None
        if done.returncode != 0 or not target.is_file():
            line = (done.stderr or "").strip().splitlines()
            raise RenderError(next((l for l in reversed(line) if l.startswith("Mobster")),
                                   "Mobster couldn't make the file."))
        return target.read_bytes()
