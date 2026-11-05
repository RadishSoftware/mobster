"""A chart block (or a table's chart) as one SVG, for the PDF and the share page (artifacts.py). The app draws the same
charts itself (dashboard/src/features/blocks/chart.tsx); both follow the same rules:

- colours from docs/brand/tokens.json "data" in order, never cycled (brand.DATA); text in ink, never a series colour;
- thin marks: 2px lines, bars at most 24px with a 4px rounded end, a 2px paper gap between touching marks, dots with
  a paper ring; hairline grid, no axis line but the zero line; y ticks round and compact (0, 5K, 10K);
- a legend for two or more series, none for one; values at the ends, never on every point;
- one axis: never a second y scale.

Every string is escaped. ``render(spec)`` takes a chart block's fields (kind, x, series, unit, format, currency,
stacked); ``from_table(block)`` makes that spec from a table block (its ``chart`` hint, else its first text or date
column and its number columns).
"""

import html
import math

from . import brand, result_blocks

FONT = "var(--font-sans)"
INK, INK_2, INK_3, GRID, PAPER, OTHER = (brand.LIGHT[name]
                                         for name in ("ink", "ink-2", "ink-3", "line", "surface", "ink-4"))
NUMERIC = result_blocks.NUMERIC_COLUMNS


def _e(value):
    return html.escape("" if value is None else str(value), quote=True)


def colours(dark=False):
    return list(brand.DATA["dark" if dark else "light"])


def nice_ticks(low, high, count=5):
    """Round tick values covering low..high (always including 0 when the data crosses or touches it)."""
    if not (math.isfinite(low) and math.isfinite(high)):
        return [0, 1]
    if low == high:
        high = low + (abs(low) or 1)
    span = high - low
    step = 10 ** math.floor(math.log10(span / max(1, count)))
    for factor in (1, 2, 2.5, 5, 10):
        if span / (step * factor) <= count:
            step *= factor
            break
    start, end = math.floor(low / step) * step, math.ceil(high / step) * step
    ticks, value = [], start
    while value <= end + step / 2 and len(ticks) < 12:
        ticks.append(round(value, 10))
        value += step
    return ticks


def compact(value, fmt=None, unit=None, currency=None, full=False):
    """1,284 / 12.9K / $4.2M / 12%: a value as a tick or a label says it."""
    if value is None:
        return ""
    sign = "-" if value < 0 else ""
    number = abs(value)
    if full or number < 1000:
        text = f"{number:,.2f}".rstrip("0").rstrip(".") if not float(number).is_integer() else f"{int(number):,}"
    else:
        for size, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
            if number >= size:
                text = f"{number / size:.1f}".rstrip("0").rstrip(".") + suffix
                break
    if fmt == "money":
        symbol = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}.get(currency or "USD")
        text = f"{symbol}{text}" if symbol else f"{text} {currency}"
    elif fmt == "percent":
        text += "%"
    if unit:
        text += f" {unit}"
    return sign + text


def from_table(block):
    """A table block's chart spec, or None when it has no numbers to draw."""
    columns, rows = block.get("columns") or [], block.get("rows") or []
    hint = block.get("chart") if isinstance(block.get("chart"), dict) else {}
    by_key = {c["key"]: c for c in columns}
    numeric = [c for c in columns if c.get("type") in NUMERIC]
    y = [by_key[k] for k in hint.get("y") or () if k in by_key] or numeric[:result_blocks.MAX_SERIES]
    if not y or not rows:
        return None
    x = by_key.get(hint.get("x")) or next((c for c in columns if c not in numeric), None)
    kind = hint.get("kind") or ("line" if x is not None and x.get("type") == "date" else "bar")
    fmt = {"money": "money", "percent": "percent"}.get(y[0].get("type"), "number")
    if kind == "scatter" and x is not None:
        points = [{"x": r["cells"].get(x["key"]), "y": r["cells"].get(y[0]["key"])} for r in rows]
        points = [p for p in points if isinstance(p["x"], (int, float)) and isinstance(p["y"], (int, float))]
        return {"kind": "scatter", "series": [{"label": y[0]["label"], "points": points}], "format": fmt,
                "xLabel": x["label"], "yLabel": y[0]["label"]} if points else None
    labels = [str(r["cells"].get(x["key"]) or "") if x is not None else str(i + 1) for i, r in enumerate(rows)]
    if x is not None and x.get("type") == "date":
        labels = [_short_date(label) for label in labels]
    series = [{"label": c["label"], "values": [r["cells"].get(c["key"])
                                               if isinstance(r["cells"].get(c["key"]), (int, float))
                                               and not isinstance(r["cells"].get(c["key"]), bool) else None
                                               for r in rows]}
              for c in (y[:1] if kind == "pie" else y)]
    cap = result_blocks.MAX_POINTS
    return {"kind": kind, "x": labels[:cap], "series": [{**s, "values": s["values"][:cap]} for s in series],
            "format": fmt, "currency": y[0].get("currency"), "stacked": bool(hint.get("stacked"))}


MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _short_date(text):
    """2026-10-13 -> Oct 13; 2026-10 -> Oct 2026; anything else as it is."""
    parts = str(text)[:10].split("-")
    try:
        if len(parts) == 3:
            return f"{MONTHS[int(parts[1]) - 1]} {int(parts[2])}"
        if len(parts) == 2:
            return f"{MONTHS[int(parts[1]) - 1]} {parts[0]}"
    except (ValueError, IndexError):
        pass
    return str(text)


def _text(x, y, text, anchor="start", size=11, fill=INK_3, weight=400, extra=""):
    return (f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}" font-size="{size}" font-weight="{weight}" '
            f'fill="{fill}" font-family="{FONT}" style="font-variant-numeric:tabular-nums"{extra}>{_e(text)}</text>')


def _legend(series, palette, x, y, line=False):
    parts, at = [], x
    for index, one in enumerate(series):
        colour = palette[index] if index < len(palette) else OTHER
        mark = (f'<rect x="{at:.1f}" y="{y - 4.5:.1f}" width="10" height="2" rx="1" fill="{colour}"/>' if line else
                f'<rect x="{at:.1f}" y="{y - 7:.1f}" width="8" height="8" rx="2" fill="{colour}"/>')
        parts.append(mark + _text(at + 13, y, one["label"], size=11, fill=INK_2))
        at += 13 + len(one["label"]) * 6.3 + 18
    return "".join(parts)


def _bar_path(x, y, w, h, horizontal):
    """A bar with a 4px rounded data end and a square base."""
    r = min(4, (h if horizontal else w) / 2, (w if horizontal else h))
    if r <= 0.2:
        return f'M{x:.1f},{y:.1f}h{w:.1f}v{h:.1f}h{-w:.1f}z'
    if horizontal:
        return (f"M{x:.1f},{y:.1f}h{w - r:.1f}a{r},{r} 0 0 1 {r},{r}v{h - 2 * r:.1f}a{r},{r} 0 0 1 {-r},{r}"
                f"h{-(w - r):.1f}z")
    return (f"M{x:.1f},{y + h:.1f}v{-(h - r):.1f}a{r},{r} 0 0 1 {r},{-r}h{w - 2 * r:.1f}a{r},{r} 0 0 1 {r},{r}"
            f"v{h - r:.1f}z")


def render(spec, width=640, height=240, dark=False):
    """The chart as an SVG element (a string). Unknown kinds draw as bars."""
    kind = spec.get("kind") or "bar"
    palette = colours(dark)
    fmt, unit, currency = spec.get("format"), spec.get("unit"), spec.get("currency")
    if kind == "pie":
        body = _pie(spec, palette, width, height, fmt, unit, currency)
    elif kind == "scatter":
        body = _scatter(spec, palette, width, height, fmt, currency)
    elif kind == "sparkline":
        body, height = _sparklines(spec, palette, width, fmt, unit, currency)
    elif kind == "bar" and _horizontal(spec):
        body, height = _hbars(spec, palette, width, fmt, unit, currency)
    else:
        body = _xy(spec, palette, width, height, fmt, unit, currency, kind)
    label = _e(spec.get("title") or "Chart")
    return (f'<svg class="chart-svg" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="100%" '
            f'role="img" aria-label="{label}">{body}</svg>')


def _horizontal(spec):
    labels = spec.get("x") or []
    return len(labels) > 8 or any(len(str(label)) > 12 for label in labels)


def _values(spec):
    return [v for s in spec.get("series") or () for v in s.get("values") or () if isinstance(v, (int, float))]


def _xy(spec, palette, width, height, fmt, unit, currency, kind):
    labels, series = spec.get("x") or [], spec.get("series") or []
    stacked = spec.get("stacked") and kind in ("bar", "area") and len(series) > 1
    if stacked:
        totals = [sum(max(0, s["values"][i] or 0) for s in series) for i in range(len(labels))]
        low, high = 0, max(totals or [0])
    else:
        values = _values(spec) or [0]
        low, high = min(0, min(values)), max(0, max(values))
    ticks = nice_ticks(low, high)
    legend_h = 26 if len(series) > 1 else 0
    left = max(28, 8 + 6.2 * max(len(compact(t, fmt, None, currency)) for t in ticks))
    top, bottom, right = 12 + legend_h, 26, 12
    plot_w, plot_h = width - left - right, height - top - bottom
    lo, hi = ticks[0], ticks[-1]
    y_of = lambda v: top + plot_h - (v - lo) / (hi - lo or 1) * plot_h  # noqa: E731
    parts = [_legend(series, palette, left, 14, line=kind in ("line", "area"))] if legend_h else []
    for tick in ticks:
        y = y_of(tick)
        parts.append(f'<line x1="{left:.1f}" x2="{width - right:.1f}" y1="{y:.1f}" y2="{y:.1f}" '
                     f'stroke="{INK if tick == 0 else GRID}" stroke-opacity="{0.35 if tick == 0 else 1}" '
                     f'stroke-width="{0.75 if tick == 0 else 0.5}"/>')
        parts.append(_text(left - 6, y + 3, compact(tick, fmt, None, currency), anchor="end", size=10))
    count = max(1, len(labels))
    band = plot_w / count
    every = max(1, math.ceil(count / max(1, plot_w // 64)))
    for index, label in enumerate(labels):
        if index % every == 0 or index == count - 1 and kind == "bar":
            cx = left + band * (index + 0.5) if kind == "bar" else left + (plot_w * index / max(1, count - 1))
            anchor = "middle" if kind == "bar" or 0 < index < count - 1 else "start" if index == 0 else "end"
            parts.append(_text(cx, height - 8, label if len(str(label)) <= 14 else str(label)[:13] + "…", anchor=anchor,
                               size=10))
    if kind == "bar":
        groups = 1 if stacked else len(series)
        bar = min(24, (band * 0.72 - 2 * (groups - 1)) / groups)
        for index in range(count):
            base = 0
            start = left + band * (index + 0.5) - (bar * groups + 2 * (groups - 1)) / 2
            for k, one in enumerate(series):
                value = one["values"][index]
                if value is None:
                    continue
                colour = palette[k] if k < len(palette) else OTHER
                if stacked:
                    y0, y1 = y_of(base), y_of(base + max(0, value))
                    base += max(0, value)
                    top_most = k == len(series) - 1 or all((s["values"][index] or 0) <= 0 for s in series[k + 1:])
                    h = max(0.0, y0 - y1 - (0 if k == 0 else 2))
                    path = (_bar_path(start, y1, bar, h, False) if top_most
                            else f'M{start:.1f},{y1:.1f}h{bar:.1f}v{h:.1f}h{-bar:.1f}z')
                    parts.append(f'<path d="{path}" fill="{colour}"/>')
                else:
                    x = start + k * (bar + 2)
                    y0, y1 = y_of(0), y_of(value)
                    if value >= 0:
                        parts.append(f'<path d="{_bar_path(x, y1, bar, y0 - y1, False)}" fill="{colour}"/>')
                    else:
                        parts.append(f'<rect x="{x:.1f}" y="{y0:.1f}" width="{bar:.1f}" '
                                     f'height="{y1 - y0:.1f}" fill="{colour}"/>')
        if len(series) == 1 and count <= 12:
            for index, value in enumerate(series[0]["values"]):
                if value is not None:
                    parts.append(_text(left + band * (index + 0.5), y_of(max(0, value)) - 4,
                                       compact(value, fmt, unit, currency), anchor="middle", size=10, fill=INK_2))
    else:
        x_of = lambda i: left + (plot_w * i / max(1, count - 1) if count > 1 else plot_w / 2)  # noqa: E731
        base = [0.0] * count
        for k, one in enumerate(series):
            colour = palette[k] if k < len(palette) else OTHER
            points = []
            for index, value in enumerate(one["values"]):
                if value is None:
                    points.append(None)
                    continue
                level = base[index] + value if stacked else value
                points.append((index, x_of(index), y_of(level)))
            runs, current = [], []
            for point in points:
                if point is None:
                    if current:
                        runs.append(current)
                    current = []
                else:
                    current.append(point)
            if current:
                runs.append(current)
            for run in runs:
                line = " ".join(f"{x:.1f},{y:.1f}" for _, x, y in run)
                if kind == "area":
                    floor = " ".join(f"{x:.1f},{y_of(base[i] if stacked else max(lo, 0)):.1f}" for i, x,
                                     _ in reversed(run))
                    parts.append(f'<polygon points="{line} {floor}" fill="{colour}" fill-opacity="0.12"/>')
                parts.append(f'<polyline points="{line}" fill="none" stroke="{colour}" stroke-width="2" '
                             'stroke-linejoin="round" stroke-linecap="round"/>')
            last = next((p for p in reversed(points) if p is not None), None)
            if last is not None:
                parts.append(f'<circle cx="{last[1]:.1f}" cy="{last[2]:.1f}" r="3.5" fill="{colour}" '
                             f'stroke="{PAPER}" stroke-width="2"/>')
            if stacked:
                base = [base[i] + (v or 0) for i, v in enumerate(one["values"])]
        if len(series) == 1:
            values = series[0]["values"]
            index = max((i for i, v in enumerate(values) if v is not None), default=None)
            if index is not None:
                parts.append(_text(min(x_of(index), width - right - 2), y_of(values[index]) - 8,
                                   compact(values[index], fmt, unit, currency), anchor="end", size=10.5, fill=INK,
                                   weight=500))
    return "".join(parts)


def _hbars(spec, palette, width, fmt, unit, currency):
    labels, series = spec.get("x") or [], spec.get("series") or []
    groups = len(series)
    bar = 14 if groups == 1 else 9
    row = bar * groups + 2 * (groups - 1) + 14
    legend_h = 26 if groups > 1 else 0
    label_w = min(200, max(60, 6.4 * max(len(str(l)) for l in labels or [""])))
    height = int(legend_h + row * len(labels) + 6)
    values = _values(spec) or [0]
    high = max(values) or 1
    value_w = 8 + 6.4 * max(len(compact(v, fmt, unit, currency)) for v in values)
    plot_w = width - label_w - 12 - value_w
    parts = [_legend(series, palette, 0, 12)] if legend_h else []
    for index, label in enumerate(labels):
        y = legend_h + index * row + 6
        shown = str(label) if len(str(label)) * 6.4 <= label_w else str(label)[:int(label_w / 6.4) - 1] + "…"
        parts.append(_text(0, y + (row - 12) / 2 + 3, shown, size=10.5, fill=INK_2))
        for k, one in enumerate(series):
            value = one["values"][index]
            if value is None:
                continue
            colour = palette[k] if k < len(palette) else OTHER
            w = max(0.0, value) / high * plot_w
            by = y + k * (bar + 2)
            parts.append(f'<path d="{_bar_path(label_w + 12, by, max(w, 1), bar, True)}" fill="{colour}"/>')
            parts.append(_text(label_w + 12 + w + 5, by + bar / 2 + 3, compact(value, fmt, unit, currency), size=10,
                               fill=INK_2))
    return "".join(parts), height


def _pie(spec, palette, width, height, fmt, unit, currency):
    labels, series = spec.get("x") or [], spec.get("series") or [{"values": []}]
    pairs = [(label, value) for label, value in zip(labels, series[0]["values"]) if isinstance(value, (int, float))
             and value > 0]
    pairs.sort(key=lambda pair: -pair[1])
    if len(pairs) > 5:
        pairs = pairs[:4] + [("Other", sum(v for _, v in pairs[4:]))]
    total = sum(v for _, v in pairs) or 1
    radius = min(height / 2 - 8, width * 0.22)
    cx, cy, inner = radius + 8, height / 2, radius * 0.62
    parts, angle = [], -math.pi / 2
    for index, (label, value) in enumerate(pairs):
        colour = OTHER if label == "Other" and index == 4 and len(pairs) == 5 and len(labels) > 5 else palette[index]
        sweep = value / total * math.tau
        end = angle + sweep
        large = 1 if sweep > math.pi else 0
        if len(pairs) == 1:
            parts.append(f'<circle cx="{cx}" cy="{cy}" r="{(radius + inner) / 2:.1f}" fill="none" stroke="{colour}" '
                         f'stroke-width="{radius - inner:.1f}"/>')
        else:
            p = lambda a, r: (cx + r * math.cos(a), cy + r * math.sin(a))  # noqa: E731
            (x1, y1), (x2, y2), (x3, y3), (x4, y4) = p(angle, radius), p(end, radius), p(end, inner), p(angle, inner)
            parts.append(f'<path d="M{x1:.2f},{y1:.2f}A{radius:.1f},{radius:.1f} 0 {large} 1 {x2:.2f},{y2:.2f}'
                         f'L{x3:.2f},{y3:.2f}A{inner:.1f},{inner:.1f} 0 {large} 0 {x4:.2f},{y4:.2f}z" fill="{colour}" '
                         f'stroke="{PAPER}" stroke-width="2" stroke-linejoin="round"/>')
        angle = end
    parts.append(_text(cx, cy + 1, compact(total, fmt, unit, currency), anchor="middle", size=16, fill=INK, weight=500))
    parts.append(_text(cx, cy + 14, "Total", anchor="middle", size=10))
    x0 = cx + radius + 28
    row = min(22, (height - 16) / max(1, len(pairs)))
    y0 = cy - row * len(pairs) / 2 + row / 2
    for index, (label, value) in enumerate(pairs):
        colour = OTHER if label == "Other" and len(labels) > 5 and index == 4 else palette[index]
        y = y0 + index * row
        parts.append(f'<rect x="{x0:.1f}" y="{y - 7:.1f}" width="8" height="8" rx="2" fill="{colour}"/>')
        parts.append(_text(x0 + 14, y, label if len(str(label)) <= 28 else str(label)[:27] + "…", size=11, fill=INK))
        parts.append(_text(width - 54, y, compact(value, fmt, unit, currency), anchor="end", size=11, fill=INK_2))
        parts.append(_text(width - 4, y, f"{value / total * 100:.0f}%", anchor="end", size=11, fill=INK_3))
    return "".join(parts)


def _scatter(spec, palette, width, height, fmt, currency):
    series = spec.get("series") or []
    xs = [p["x"] for s in series for p in s.get("points") or ()]
    ys = [p["y"] for s in series for p in s.get("points") or ()]
    if not xs:
        return ""
    xt, yt = nice_ticks(min(xs), max(xs)), nice_ticks(min(ys), max(ys))
    legend_h = 26 if len(series) > 1 else 0
    left = max(28, 8 + 6.2 * max(len(compact(t, fmt, None, currency)) for t in yt))
    top, bottom, right = 10 + legend_h, 30 if spec.get("xLabel") else 22, 12
    plot_w, plot_h = width - left - right, height - top - bottom
    x_of = lambda v: left + (v - xt[0]) / (xt[-1] - xt[0] or 1) * plot_w  # noqa: E731
    y_of = lambda v: top + plot_h - (v - yt[0]) / (yt[-1] - yt[0] or 1) * plot_h  # noqa: E731
    parts = [_legend(series, palette, left, 12)] if legend_h else []
    for tick in yt:
        parts.append(f'<line x1="{left}" x2="{width - right}" y1="{y_of(tick):.1f}" y2="{y_of(tick):.1f}" '
                     f'stroke="{GRID}" stroke-width="0.5"/>')
        parts.append(_text(left - 6, y_of(tick) + 3, compact(tick, fmt, None, currency), anchor="end", size=10))
    for tick in xt:
        parts.append(_text(x_of(tick), top + plot_h + 13, compact(tick), anchor="middle", size=10))
    if spec.get("xLabel"):
        parts.append(_text(left + plot_w / 2, height - 3, spec["xLabel"], anchor="middle", size=10, fill=INK_2))
    for k, one in enumerate(series):
        for point in one.get("points") or ():
            parts.append(f'<circle cx="{x_of(point["x"]):.1f}" cy="{y_of(point["y"]):.1f}" r="4" fill="{palette[k]}" '
                         f'fill-opacity="0.85" stroke="{PAPER}" stroke-width="1.5"/>')
    return "".join(parts)


def _sparklines(spec, palette, width, fmt, unit, currency):
    series = spec.get("series") or []
    tile = (width - 12 * (len(series) - 1)) / max(1, len(series))
    parts = []
    for k, one in enumerate(series):
        x0 = k * (tile + 12)
        values = one.get("values") or []
        real = [v for v in values if isinstance(v, (int, float))]
        if not real:
            continue
        last, first = real[-1], real[0]
        parts.append(f'<rect x="{x0:.1f}" y="0" width="{tile:.1f}" height="84" rx="10" fill="{PAPER}"/>')
        parts.append(_text(x0 + 12, 18, one["label"], size=10.5, fill=INK_2))
        parts.append(_text(x0 + 12, 42, compact(last, fmt, unit, currency), size=20, fill=INK, weight=500,
                           extra=' style="font-variant-numeric:normal"'))
        if first:
            change = (last - first) / abs(first) * 100
            parts.append(_text(x0 + tile - 12, 42, f"{'+' if change >= 0 else ''}{change:.0f}%", anchor="end", size=11,
                               fill=INK_2))
        low, high = min(real), max(real)
        points = [(x0 + 12 + i * (tile - 24) / max(1, len(values) - 1), 74 - ((v - low) / (high - low or 1)) * 20)
                  for i, v in enumerate(values) if isinstance(v, (int, float))]
        parts.append(f'<polyline points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in points)}" fill="none" '
                     f'stroke="{palette[k] if k < len(palette) else OTHER}" stroke-width="1.75" '
                     'stroke-linejoin="round" stroke-linecap="round"/>')
    return "".join(parts), 84
