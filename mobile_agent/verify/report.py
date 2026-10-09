"""A run's artifacts: result.json (``schema: "mobster.verify/1"``), the self-contained report.html, and the
accessibility overlay frame (NN-verdict-ax.jpg).

The report makes no requests: inline CSS and SVG, frames as data: URIs, and a Content-Security-Policy that allows
nothing else (no scripts: the frame toggle is two radio buttons). Every string from the app or the model is
HTML-escaped. It follows light and dark mode, and prints. Its look is mobster.dev's: Paper, Ink and greys, the
violet mark, the system font (SF Pro on a Mac) and a mono font for assertions.

The overlay is the verdict frame with every shown node's frame in a faint violet line (what Mobster read), and each
asserted node in a thin rounded outline: green when its assertion held, red when it failed. Each assertion with a
node on screen gets one numbered pill, placed outside its first node where there's room and never on another pill;
the report lists the assertions by the same numbers beside the frame. Assertions with no node on screen (a missing
text, an absent element) get no pill: the list says what was found instead. Lines and pills are sized for the width
the reports show a frame at (``FRAME_CSS_WIDTH``), so a pill's number is at least 11 px there.
"""

import base64
import html
from io import BytesIO
import json
import os
from pathlib import Path
import re
import time

CSP = "default-src 'none'; img-src data:; style-src 'unsafe-inline'"
# Pills: Mobster's green and coral, which hold white text at 4.5:1. Outlines a step brighter, to read on dark and
# light screens alike.
GREEN, RED = (19, 122, 76), (201, 59, 38)
GREEN_LINE, RED_LINE = (32, 168, 96), (234, 67, 53)
SHOWN = (110, 86, 246)  # violet: every element Mobster read
SHOWN_ALPHA = round(255 * .3)
TINT_ALPHA = round(255 * .10)
FONT_CANDIDATES = ("/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/Helvetica.ttc",
                   "/Library/Fonts/Arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
FRAME_CSS_WIDTH = 280   # CSS px: the verify report's frame; the suite report's is 264
MAX_PILLS = 6
THUMB_WIDTH = 160
FRAME_WIDTH = 600       # px of the frames embedded at FRAME_CSS_WIDTH (2x)
VERDICTS = {"passed": ("Passed", "pass"), "failed": ("Failed", "fail"),
            "needs_review": ("Needs review", "review"), "couldnt_run": ("Couldn't run", "none")}
TITLES = {"passed": "Passed", "failed": "Failed", "needs_review": "Needs review", "couldnt_run": "Couldn't run"}
MODES = {"smart": "Smart", "keyless": "Key-less", "launch": "Launch-only"}


def _font(size):
    from PIL import ImageFont
    for path in FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(path, size)
        except OSError:
            continue
        try:
            font.set_variation_by_name("Bold")  # SF is a variable font; others keep their one weight
        except Exception:
            pass
        return font
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _short(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


# -- the overlay frame ------------------------------------------------------------------------------------------

def pill_numbers(items):
    """Which assertions get a pill: ``items`` is [(ok, has_node)] in the check's order. Failures first, then the
    rest in order, at most MAX_PILLS. Returns the set of indexes (a pill shows index + 1)."""
    ranked = sorted((index for index, (ok, has_node) in enumerate(items) if has_node),
                    key=lambda index: (bool(items[index][0]), index))
    return set(ranked[:MAX_PILLS])


def draw_overlay(frame_path, tree, asserted, out_path):
    """The accessibility overlay: ``asserted`` is [(AssertionResult, [node paths])] in the check's order. Writes
    ``out_path``."""
    from PIL import Image, ImageDraw, ImageFilter
    with Image.open(frame_path) as source:
        base = source.convert("RGBA")
    width, height = base.size
    scale = width / (tree.size[0] or 1)
    k = width / FRAME_CSS_WIDTH  # image px per CSS px at the report's size
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    def box(rect, grow=0):
        x, y, w, h = rect
        left, top = max(0, x * scale - grow), max(0, y * scale - grow)
        right, bottom = min(width - 1, (x + w) * scale + grow), min(height - 1, (y + h) * scale + grow)
        return (left, top, right, bottom) if right > left and bottom > top else None

    def rounded(area, radius, **style):
        radius = max(0, min(radius, (area[2] - area[0]) / 2, (area[3] - area[1]) / 2))
        draw.rounded_rectangle(area, radius=radius, **style)

    shown = tree.shown()
    for node in shown:
        area = box(node.rect)
        # Not the containers that fill the screen: their edges would only rule lines across it.
        if area and _area(area) < .2 * width * height:
            rounded(area, 3 * k, outline=SHOWN + (SHOWN_ALPHA,), width=max(1, round(.75 * k)))
    grow, stroke = round(2.5 * k), max(2, round(1.5 * k))
    items = []
    for result, paths in asserted:
        nodes = [node for node in (tree.by_path(path) for path in paths) if node]
        areas = sorted({area for area in (box(node.rect, grow) for node in nodes) if area},
                       key=lambda area: (area[1], area[0]))
        # An element inside another the same assertion matched (a switch's own label) is outlined once, outside.
        areas = [area for area in areas if not any(other != area and _contains(other, area) for other in areas)]
        items.append((result, areas))
    # Held first, failed last, so a red outline is never under a green one.
    for result, areas in sorted(items, key=lambda item: item[0].ok, reverse=True):
        line = (GREEN_LINE if result.ok else RED_LINE) + (255,)
        tint = (GREEN_LINE if result.ok else RED_LINE) + (TINT_ALPHA,)
        for area in areas:
            rounded(area, 7 * k, fill=tint, outline=line, width=stroke)
    numbered = sorted(pill_numbers([(result.ok, bool(areas)) for result, areas in items]),
                      key=lambda index: (items[index][0].ok, index))
    asserted_areas = [area for _, areas in items for area in areas]
    # Where the screen is busy (text, icons, edges), from the screenshot itself: a pill goes where it's quiet.
    edges = base.convert("L").filter(ImageFilter.FIND_EDGES)
    font = _font(max(10, round(12 * k)))
    placed = []
    # Each chosen assertion's first element; then a failed one's second and third (the plans a count found too
    # many or too few of), while the frame has room for a few more.
    jobs = [(index, 0) for index in numbered] + [(index, n) for index in numbered if not items[index][0].ok
                                                   for n in range(1, min(3, len(items[index][1])))]
    for index, n in jobs:
        if n and len(placed) >= MAX_PILLS + 2:
            break
        result, areas = items[index]
        _pill(draw, font, index + 1, result.ok, areas[n], (width, height), k, placed,
              [area for area in asserted_areas if area != areas[n]], edges)
    Image.alpha_composite(base, layer).convert("RGB").save(out_path, "JPEG", quality=85)
    return Path(out_path)


def _overlap(a, b):
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


def _area(a):
    return max(0, a[2] - a[0]) * max(0, a[3] - a[1])


def _contains(outer, inner):
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def _pill(draw, font, number, ok, area, size, k, placed, others, edges):
    """One numbered pill for ``area``: outside it (above, beside, below) where the screen is quiet, else wherever
    it covers the least (``edges``, the screenshot's edge map, says where text and icons are): astride the
    element's top edge, or just inside a top corner. Never on a pill already ``placed``, an outline of another
    assertion, or off the frame."""
    from PIL import ImageStat
    text = str(number)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font, anchor="ls")
    pill_h = round(18 * k)
    pad, mark, gap, ring = round(5.5 * k), round(7.5 * k), round(3 * k), max(1, round(k))
    pill_w = pad + mark + gap + (right - left) + pad
    space = round(1.5 * k)  # outside, the pill touches the outline, so it reads as the outline's own label
    x0, y0, x1, y1 = area
    middle = (y0 + y1) / 2 - pill_h / 2 if y1 - y0 < 2 * pill_h else y0
    inset = round(8 * k)
    # (x, y, how much covering the element itself costs): outside first, then astride its edge, then inside.
    candidates = [(x0, y0 - space - pill_h, 0), (x0 - space - pill_w, middle, 0), (x1 + space, middle, 0),
                  (x0, y1 + space, 0), (x1 - pill_w, y0 - space - pill_h, 0), (x1 - pill_w, y1 + space, 0),
                  (x0 + inset, y0 - pill_h / 2, 2), (x1 - inset - pill_w, y0 - pill_h / 2, 2),
                  (x0 + space, y0 + space, 4), (x1 - space - pill_w, y0 + space, 4)]
    best = None
    for order, (x, y, own_cost) in enumerate(candidates):
        if x < ring or y < ring or x + pill_w > size[0] - ring or y + pill_h > size[1] - ring:
            continue
        rect = (x - ring, y - ring, x + pill_w + ring, y + pill_h + ring)
        if any(_overlap(rect, other) for other in placed):
            continue
        whole = _area(rect)
        busy = ImageStat.Stat(edges.crop(tuple(round(v) for v in rect))).mean[0]
        cost = (busy + 60 * sum(_overlap(rect, other) for other in others) / whole
                + own_cost * _overlap(rect, area) / whole + .3 * order)
        if best is None or cost < best[0]:
            best = (cost, x, y)
        if busy < 3 and not any(_overlap(rect, other) for other in others) and own_cost == 0:
            break
    if best is None:  # nowhere free: the top corner inside, clamped to the frame
        x = min(max(ring, x0 + space), size[0] - pill_w - ring)
        y = min(max(ring, y0 + space), size[1] - pill_h - ring)
        for _ in range(12):
            rect = (x - ring, y - ring, x + pill_w + ring, y + pill_h + ring)
            if not any(_overlap(rect, other) for other in placed):
                break
            y = min(y + pill_h + 2 * ring, size[1] - pill_h - ring)
    else:
        _, x, y = best
    placed.append((x - ring, y - ring, x + pill_w + ring, y + pill_h + ring))
    radius = pill_h / 2
    draw.rounded_rectangle((x - ring, y - ring, x + pill_w + ring, y + pill_h + ring), radius=radius + ring,
                           fill=(255, 255, 255, 235))
    draw.rounded_rectangle((x, y, x + pill_w, y + pill_h), radius=radius, fill=(GREEN if ok else RED) + (255,))
    white, line = (255, 255, 255, 255), max(2, round(1.5 * k))
    gx, gy = x + pad, y + (pill_h - mark) / 2
    if ok:
        draw.line([(gx + .1 * mark, gy + .55 * mark), (gx + .4 * mark, gy + .82 * mark),
                   (gx + .92 * mark, gy + .2 * mark)], fill=white, width=line, joint="curve")
    else:
        draw.line([(gx + .12 * mark, gy + .12 * mark), (gx + .88 * mark, gy + .88 * mark)], fill=white, width=line)
        draw.line([(gx + .88 * mark, gy + .12 * mark), (gx + .12 * mark, gy + .88 * mark)], fill=white, width=line)
    draw.text((gx + mark + gap - left, y + pill_h / 2 - (top + bottom) / 2), text, font=font, fill=white,
              anchor="ls")


# -- shared HTML -------------------------------------------------------------------------------------------------

def _e(value):
    return html.escape("" if value is None else str(value), quote=True)


def _data_url(path, max_width=None, quality=72):
    path = Path(path)
    try:
        if max_width is None:
            data = path.read_bytes()
        else:
            from PIL import Image
            with Image.open(path) as image:
                image = image.convert("RGB")
                if image.width > max_width:
                    image = image.resize((max_width, max(1, round(image.height * max_width / image.width))),
                                         Image.LANCZOS)
                out = BytesIO()
                image.save(out, "JPEG", quality=quality)
                data = out.getvalue()
    except (OSError, ValueError):
        return None
    return "data:image/jpeg;base64," + base64.b64encode(data).decode()


ICONS = {
    "check": '<path d="M3.5 8.5l3 3 6-7"/>',
    "cross": '<path d="M4.5 4.5l7 7M11.5 4.5l-7 7"/>',
    "review": '<path d="M8 4.5v4.25M8 11.4v.1"/>',
    "dash": '<path d="M4.5 8h7"/>',
    "phone": '<rect x="4.25" y="1.75" width="7.5" height="12.5" rx="2"/><path d="M7 4h2"/>',
    "app": '<rect x="2.25" y="2.25" width="4.75" height="4.75" rx="1.4"/><rect x="9" y="2.25" width="4.75" '
           'height="4.75" rx="1.4"/><rect x="2.25" y="9" width="4.75" height="4.75" rx="1.4"/><rect x="9" y="9" '
           'width="4.75" height="4.75" rx="1.4"/>',
    "sim": '<rect x="1.75" y="2.5" width="12.5" height="8.5" rx="1.5"/><path d="M5.5 13.5h5M8 11v2.5"/>',
    "clock": '<circle cx="8" cy="8" r="6"/><path d="M8 4.75V8l2 1.5"/>',
    "calendar": '<rect x="2.25" y="3" width="11.5" height="10.75" rx="2"/>'
                '<path d="M2.25 6.5h11.5M5.5 1.5v3M10.5 1.5v3"/>',
    "chevron": '<path d="M4.5 6.25l3.5 3.5 3.5-3.5"/>',
    "coin": '<circle cx="8" cy="8" r="6"/><path d="M9.75 5.9C9.4 5.35 8.75 5 8 5c-1 0-1.75.6-1.75 1.4 0 1.9 3.5.9 3.5 '
            '2.9 0 .85-.8 1.45-1.75 1.45-.8 0-1.5-.4-1.8-1M8 4v1M8 10.75v1"/>',
}
STATUS_ICON = {"pass": "check", "fail": "cross", "review": "review", "none": "dash"}
# The mark from mobster.dev (site/public/brand/mark-light.svg), its body in the scheme's violet.
MARK_SVG = ('<svg class="mark" viewBox="0 0 512 512" aria-hidden="true"><path class="mark-body" d="M265.6 38c35.8 0 '
            '53.8 0 67.5 7a64 64 0 0 1 28 28c7 13.7 7 31.6 7 67.4v231.2c0 35.8 0 53.8-7 67.5a64 64 0 0 1-28 28c-13.7 '
            '7-31.6 7-67.5 7h-19.2c-35.8 0-53.8 0-67.5-7a64 64 0 0 1-28-28c-7-13.7-7-31.6-7-67.5V140.4c0-35.8 0-53.8 '
            '7-67.5a64 64 0 0 1 28-28c13.7-7 31.6-7 67.5-7z"/><path fill="#fff" d="M248 127a25 25 0 0 1 25 25v28a25 '
            '25 0 0 1-50 0v-28a25 25 0 0 1 25-25zm80 0a25 25 0 0 1 25 25v28a25 25 0 0 1-50 0v-28a25 25 0 0 1 25-25z"/>'
            '</svg>')


def icon(name, cls="icon"):
    return (f'<svg class="{cls}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" '
            f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{ICONS[name]}</svg>')


def verdict_chip(word, tone):
    return f'<span class="verdict {tone}">{icon(STATUS_ICON.get(tone, "dash"))}{_e(word)}</span>'


def brand(tool, ident=None):
    return (f'<div class="brand">{MARK_SVG}<span class="brand-name">Mobster</span><span class="brand-tool">'
            f'{_e(tool)}</span>' + (f'<span class="brand-id">{_e(ident)}</span>' if ident else "") + "</div>")


def pill(number, ok, on_frame=True):
    tone = "pass" if ok else "fail"
    mark = icon("check" if ok else "cross")
    where = "" if on_frame else ", not outlined on the frame"
    words = f"{number}, {'held' if ok else 'failed'}{where}"
    return f'<span class="pill {tone}" role="img" aria-label="{_e(words)}">{mark}{_e(number)}</span>'


def has_node(item):
    """Whether the overlay outlines an assertion's element: it matched one with an area (a suite's copy of the
    assertion says so in ``outlined``)."""
    if "outlined" in item:
        return bool(item["outlined"])
    return any(len(match.get("rect") or ()) == 4 and match["rect"][2] > 0 and match["rect"][3] > 0
               for match in item.get("matches") or () if isinstance(match, dict))


def expectations(items, *, frame=True, quiet_held=False):
    """The numbered list of assertions: ``items`` are result.json's assertions (index, text, ok, observed,
    matches). Numbers match the overlay's pills; with ``quiet_held`` a held one shows its text only."""
    numbered = pill_numbers([(item.get("ok"), has_node(item)) for item in items]) if frame else set()
    rows = []
    for position, item in enumerate(items):
        ok = bool(item.get("ok"))
        number = (item.get("index") if isinstance(item.get("index"), int) else position) + 1
        observed = item.get("observed")
        rows.append(f'<li class="{"held" if ok else "failed"}">{pill(number, ok, position in numbered)}'
                    f'<code class="assert">{_e(item.get("text"))}</code>'
                    + (f'<span class="observed">{_e(observed)}</span>' if observed and not (ok and quiet_held)
                       else "") + "</li>")
    return '<ol class="expect">' + "".join(rows) + "</ol>"


OVERLAY_LEGEND = ('<p class="legend" aria-hidden="true"><span class="key read"></span>Read by Mobster '
                  '<span class="key held"></span>Held <span class="key failed"></span>Failed</p>')


def command(text):
    """A shell command, highlighted the way mobster.dev highlights code: flags violet, quoted strings green,
    arguments grey."""
    if not text:
        return ""
    tokens = re.findall(r"""'[^']*'|"(?:\\.|[^"\\])*"|\S+|\s+""", str(text))
    out, words = [], 0
    for token in tokens:
        if token.isspace():
            out.append(_e(token))
            continue
        words += 1
        if token.startswith("-"):
            kind = "flag"
        elif token[:1] in "'\"":
            kind = "string"
        elif words <= 2:
            kind = "cmd"
        else:
            kind = "arg"
        out.append(f'<span class="hl-{kind}">{_e(token)}</span>')
    return ('<pre class="cmd"><span class="hl-prompt" aria-hidden="true">$ </span><code>' + "".join(out)
            + "</code></pre>")


def dots(text):
    """'iPhone 17 Pro · iOS 26.4' with the dot kept on its line: a line never starts with '·'."""
    return None if text is None else str(text).replace(" · ", "\u00a0· ")


def home(text):
    """Paths under the home folder as ~/…, so a shared report doesn't spell out whose Mac ran it."""
    if not text:
        return text
    folder = str(Path.home())
    # Not inside quotes: a shell doesn't expand a quoted ~.
    return re.sub(r"(^|[\s=])" + re.escape(folder) + r"(?=/|$|\s)", lambda m: m.group(1) + "~", str(text))


def when(ms=None, run_id=None):
    """'8 Oct 2026, 12:59' from epoch ms, else from a run id's leading YYYYMMDD-HHMMSS."""
    stamp = None
    if ms:
        stamp = time.localtime(ms / 1000)
    elif run_id:
        match = re.match(r"(\d{8}-\d{6})", str(run_id))
        if match:
            try:
                stamp = time.strptime(match.group(1), "%Y%m%d-%H%M%S")
            except ValueError:
                stamp = None
    if stamp is None:
        return None
    return f"{stamp.tm_mday} {time.strftime('%b %Y, %H:%M', stamp)}"


def seconds_text(seconds):
    seconds = float(seconds or 0)
    if seconds < 60:
        return f"{seconds:.1f} s" if seconds < 10 and seconds % 1 else f"{round(seconds)} s"
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes} min {rest:02d} s"


# The design system: mobster.dev's tokens (site/app/globals.css), with the system font for Mobster Sans.
BASE_STYLE = """
:root { color-scheme: light dark;
  --paper: #faf8f5; --surface: #ffffff; --sunken: #f3f0eb; --ink: #18171c; --ink-2: #5c5a63; --ink-3: #6f6c76;
  --line: #e8e4de; --line-strong: #d9d4cc; --violet: #6e56f6; --violet-text: #5b41e8;
  --pass: #137a4c; --fail: #b53321; --review: #8a5a00; --none: #5c5a63;
  --pass-solid: #137a4c; --fail-solid: #c93b26; --read: #6e56f6;
  --bezel: #18171c; --wash: 6%; --seg-on: #ffffff;
  --card-shadow: 0 0 0 1px var(--line), 0 1px 2px rgb(24 23 28 / .04);
  --shot: 0 0 0 1px rgb(0 0 0 / .08), 0 1px 2px rgb(24 23 28 / .06), 0 28px 56px -32px rgb(24 23 28 / .34);
  --sans: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", system-ui, Roboto, "Helvetica Neue",
    Arial, sans-serif;
  --mono: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace; }
@media (prefers-color-scheme: dark) { :root {
  --paper: #111014; --surface: #1a191f; --sunken: #16151a; --ink: #f4f2ee; --ink-2: #b4b1ba; --ink-3: #8f8c96;
  --line: #2a2930; --line-strong: #3a3941; --violet: #7d68ff; --violet-text: #a493ff;
  --pass: #48d597; --fail: #ff806b; --review: #e8b04b; --none: #b4b1ba; --read: #7d68ff;
  --bezel: #2a2930; --wash: 7%; --seg-on: #34333b;
  --card-shadow: 0 0 0 1px var(--line);
  --shot: 0 0 0 1px rgb(255 255 255 / .08), 0 28px 56px -32px rgb(0 0 0 / .7); } }
* { box-sizing: border-box; }
html { background: var(--paper); -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--paper); color: var(--ink); font: 14px/1.55 var(--sans);
  font-synthesis: none; -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility; }
main { max-width: 1040px; margin: 0 auto; padding: 32px 24px 64px; }
:focus-visible { outline: 2px solid var(--violet); outline-offset: 2px; border-radius: 6px; }
::selection { background: color-mix(in srgb, var(--violet) 22%, transparent); }
.sr { position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0; overflow: hidden;
  clip: rect(0 0 0 0); white-space: nowrap; border: 0; }
.icon { width: 16px; height: 16px; flex: none; }
code, pre, .mono { font-family: var(--mono); }
a { color: inherit; text-decoration-line: underline; text-decoration-thickness: 1px; text-underline-offset: .2em;
  text-decoration-color: color-mix(in srgb, currentColor 35%, transparent); }
a:hover { text-decoration-color: currentColor; }
.pass { --tone: var(--pass); --solid: var(--pass-solid); } .fail { --tone: var(--fail); --solid: var(--fail-solid); }
.review { --tone: var(--review); --solid: var(--review); } .none { --tone: var(--none); --solid: var(--none); }

/* The page's head: the mark, the verdict, the one line that matters, then the facts. */
.brand { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--ink-2); min-width: 0; }
.mark { width: 20px; height: 20px; flex: none; }
.mark-body { fill: var(--violet); }
.brand-name { font-weight: 600; color: var(--ink); letter-spacing: -.01em; }
.brand-tool { color: var(--ink-3); }
.brand-tool::before { content: "/"; margin-right: 8px; color: var(--line-strong); }
.brand-id { margin-left: auto; font: 12px/1.4 var(--mono); color: var(--ink-3); overflow-wrap: anywhere;
  text-align: right; }
header.head { padding: 28px 0 28px; border-bottom: 1px solid var(--line); }
.chips { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.verdict { display: inline-flex; align-items: center; gap: 6px; height: 26px; padding: 0 11px 0 8px;
  border-radius: 999px; font-size: 13px; font-weight: 600; letter-spacing: -.005em; color: var(--tone);
  background: color-mix(in srgb, var(--tone) 11%, var(--surface));
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--tone) 24%, transparent); }
.verdict .icon { width: 14px; height: 14px; stroke-width: 2; }
.tag { display: inline-flex; align-items: center; height: 22px; padding: 0 8px; border-radius: 999px;
  font-size: 12px; font-weight: 500; color: var(--ink-2); background: var(--sunken);
  box-shadow: inset 0 0 0 1px var(--line); white-space: nowrap; }
.tag.tone { color: var(--tone); background: color-mix(in srgb, var(--tone) 9%, var(--surface));
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--tone) 20%, transparent); }
h1 { font-size: 28px; line-height: 1.2; letter-spacing: -.022em; font-weight: 650; margin: 14px 0 6px;
  overflow-wrap: anywhere; text-wrap: balance; }
h1 .quiet { color: var(--ink-3); font-weight: 500; }
.lede { margin: 0; font-size: 15px; color: var(--ink-2); overflow-wrap: anywhere; }
.lede + .lede { margin-top: 2px; }
ul.meta { display: flex; flex-wrap: wrap; gap: 8px 22px; list-style: none; margin: 16px 0 0; padding: 0;
  font-size: 13px; color: var(--ink-2); }
ul.meta li { display: inline-flex; align-items: center; gap: 7px; min-width: 0; overflow-wrap: anywhere; }
ul.meta .icon { color: var(--ink-3); width: 15px; height: 15px; }
header.head pre.cmd { margin-top: 18px; }

/* Sections and cards. */
section { margin-top: 40px; }
h2 { display: flex; align-items: baseline; gap: 10px; margin: 0 0 12px; font-size: 15px; line-height: 1.3;
  font-weight: 600; letter-spacing: -.01em; }
h2 .count { font-size: 13px; font-weight: 500; color: var(--ink-3); letter-spacing: 0; }
.card { background: var(--surface); border-radius: 14px; box-shadow: var(--card-shadow); }
.empty { color: var(--ink-3); margin: 0; }

/* A shell command, highlighted like mobster.dev's code. */
pre.cmd { margin: 0; width: fit-content; max-width: 100%; padding: 10px 16px 10px 14px; background: var(--surface);
  border-radius: 10px; box-shadow: 0 0 0 1px var(--line), 0 1px 2px rgb(24 23 28 / .04);
  font: 13px/1.6 var(--mono); color: var(--ink); white-space: pre-wrap; overflow-wrap: break-word; }
pre.cmd code { font: inherit; }
.hl-prompt { color: var(--ink-3); user-select: none; -webkit-user-select: none; }
.hl-flag { color: var(--violet-text); } .hl-string { color: var(--pass); } .hl-arg { color: var(--ink-2); }
.hl-cmd { color: var(--ink); }

/* The numbered assertions; the numbers are the overlay's pills. */
ol.expect { list-style: none; margin: 0; padding: 0; }
ol.expect li { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 3px 12px; align-items: baseline;
  padding: 12px 16px; border-top: 1px solid var(--line); }
ol.expect li:first-child { border-top: 0; }
ol.expect li.failed { background: color-mix(in srgb, var(--fail) var(--wash), var(--surface));
  box-shadow: inset 2px 0 0 var(--fail-solid); }
ol.expect li.failed + li.failed { border-top-color: color-mix(in srgb, var(--fail) 18%, var(--line)); }
ol.expect li:first-child { border-radius: 14px 14px 0 0; } ol.expect li:last-child { border-radius: 0 0 14px 14px; }
ol.expect li:only-child { border-radius: 14px; }
code.assert { font: 13px/1.5 var(--mono); color: var(--ink); overflow-wrap: break-word; min-width: 0; }
.observed { grid-column: 2; font-size: 13px; color: var(--ink-3); overflow-wrap: break-word; }
li.failed .observed { color: var(--fail); }
.pill { position: relative; top: -1px; display: inline-flex; align-items: center; gap: 3px; height: 20px;
  min-width: 34px; padding: 0 8px 0 6px; border-radius: 999px; background: var(--solid); color: #fff;
  font: 650 12px/1 var(--sans); font-variant-numeric: tabular-nums;
  print-color-adjust: exact; -webkit-print-color-adjust: exact; }
.pill .icon { width: 11px; height: 11px; stroke-width: 2.4; }

/* A frame, in a phone-shaped bezel. */
.phone { padding: 6px; border-radius: 46px; background: var(--bezel); box-shadow: var(--shot); }
.phone img { display: block; width: 100%; height: auto; border-radius: 40px; background: var(--sunken); }
.legend { display: flex; flex-wrap: wrap; align-items: center; justify-content: center; gap: 4px 6px;
  margin: 12px 0 0; font-size: 12px; color: var(--ink-3); }
.key { display: inline-block; width: 12px; height: 9px; border-radius: 3px; border: 1.5px solid; }
.legend .key:not(:first-child) { margin-left: 8px; }
.key.read { border-color: color-mix(in srgb, var(--read) 60%, transparent); }
.key.held { border-color: var(--pass-solid); background: color-mix(in srgb, var(--pass-solid) 14%, transparent); }
.key.failed { border-color: var(--fail-solid); background: color-mix(in srgb, var(--fail-solid) 14%, transparent); }

/* Facts, as a quiet grid. */
dl.facts { display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 18px 28px;
  margin: 0; padding: 18px 20px; }
dl.facts div { min-width: 0; }
dl.facts div.wide { grid-column: 1 / -1; }
dl.facts dt { font-size: 12px; color: var(--ink-3); margin-bottom: 2px; }
dl.facts dd { margin: 0; font-size: 14px; overflow-wrap: break-word; }
dl.facts dd.mono { font-size: 12.5px; }
footer.foot { margin-top: 48px; padding-top: 16px; border-top: 1px solid var(--line); font-size: 12px;
  color: var(--ink-3); display: flex; flex-wrap: wrap; gap: 4px 16px; }

@media (max-width: 640px) {
  main { padding: 20px 16px 48px; }
  header.head { padding: 22px 0 24px; }
  h1 { font-size: 23px; }
  .lede { font-size: 14px; }
  ul.meta { gap: 6px 16px; }
  ol.expect li { padding: 12px 14px; }
  dl.facts { padding: 16px; grid-template-columns: 1fr 1fr; gap: 14px 16px; }
  .brand-id { display: none; }
}
@media (max-width: 480px) { dl.facts { grid-template-columns: minmax(0, 1fr); } }
@media print {
  :root { color-scheme: light; --paper: #fff; --surface: #fff; --sunken: #f6f4f1; --ink: #18171c;
    --ink-2: #4a4850; --ink-3: #5c5a63; --line: #dcd8d2; --line-strong: #cbc6be; --violet: #6e56f6;
    --violet-text: #5b41e8; --pass: #137a4c; --fail: #b53321; --review: #8a5a00; --none: #5c5a63; --read: #6e56f6;
    --bezel: #18171c; --wash: 7%; --card-shadow: 0 0 0 1px #dcd8d2; --shot: none; }
  body { font-size: 12px; background: #fff; }
  main { max-width: none; padding: 0; }
  section, li, .result, .shot { break-inside: avoid; }
  a { text-decoration: none; }
  * { print-color-adjust: exact; -webkit-print-color-adjust: exact; }
}
"""

VERIFY_STYLE = """
.layout { display: grid; grid-template-columns: minmax(0, 1fr) 320px; gap: 40px 48px; align-items: start;
  margin-top: 36px; }
.layout > section { grid-column: 1; margin: 0; min-width: 0; }
.shot { grid-column: 2; position: sticky; top: 24px; width: 320px; }
.shot .phone { width: 320px; border-radius: 50px; }
.shot .phone img { border-radius: 44px; }
.notes { margin: 14px 2px 0; display: grid; gap: 4px; }
.notes p { margin: 0; font-size: 13px; color: var(--ink-2); overflow-wrap: anywhere; }
.seg { display: flex; width: max-content; margin: 0 auto 12px; padding: 3px; gap: 2px; border-radius: 999px;
  background: var(--sunken); box-shadow: inset 0 0 0 1px var(--line); }
.seg label { padding: 4px 12px; border-radius: 999px; font-size: 12.5px; font-weight: 500; color: var(--ink-2);
  cursor: pointer; user-select: none; -webkit-user-select: none; }
#frame-ax:checked ~ .seg label[for=frame-ax], #frame-plain:checked ~ .seg label[for=frame-plain] {
  background: var(--seg-on); color: var(--ink); box-shadow: 0 0 0 1px var(--line), 0 1px 2px rgb(24 23 28 / .1); }
#frame-ax:focus-visible ~ .seg label[for=frame-ax], #frame-plain:focus-visible ~ .seg label[for=frame-plain] {
  outline: 2px solid var(--violet); outline-offset: 1px; }
#frame-ax:checked ~ .phone .plain, #frame-plain:checked ~ .phone .ax { display: none; }
#frame-plain:checked ~ .legend { visibility: hidden; }
.callout { margin: 16px 0 0; max-width: 720px; padding: 12px 16px; border-radius: 12px; font-size: 14px;
  background: color-mix(in srgb, var(--tone) var(--wash), var(--surface));
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--tone) 22%, transparent); overflow-wrap: anywhere; }
.callout p { margin: 0; } .callout p + p { margin-top: 4px; color: var(--ink-2); }
ol.steps { list-style: none; margin: 0; padding: 0; }
ol.steps li { display: grid; grid-template-columns: 18px 40px minmax(0, 1fr) auto; gap: 14px; align-items: center;
  padding: 10px 16px; border-top: 1px solid var(--line); }
ol.steps li:first-child { border-top: 0; }
.step-n { font: 12px/1 var(--mono); color: var(--ink-3); text-align: right; }
.thumb { width: 40px; aspect-ratio: 3 / 4; border-radius: 7px; overflow: hidden; box-shadow: 0 0 0 1px var(--line);
  background: var(--sunken); }
.thumb img { display: block; width: 100%; height: 100%; object-fit: cover; object-position: top; }
.op { font-weight: 500; overflow-wrap: anywhere; }
.step-meta { font-size: 12.5px; color: var(--ink-3); overflow-wrap: anywhere; }
.at { font-size: 12.5px; color: var(--ink-3); font-variant-numeric: tabular-nums; white-space: nowrap; }
dl.facts dd .sub { display: block; margin-top: 2px; font-size: 12px; color: var(--ink-3); }
.note { margin: 0; padding: 16px 18px; white-space: pre-wrap; overflow-wrap: anywhere; color: var(--ink-2); }
.note-meta { display: block; margin-top: 10px; font-size: 12.5px; color: var(--ink-3); }
@media (max-width: 860px) {
  .layout { grid-template-columns: minmax(0, 1fr); margin-top: 28px; }
  .shot { grid-column: 1; grid-row: auto !important; position: static; justify-self: center; max-width: 100%; }
  .shot .phone { max-width: 100%; }
}
@media (max-width: 640px) {
  ol.steps li { grid-template-columns: 40px minmax(0, 1fr) auto; padding: 10px 14px; gap: 12px; }
  .step-n { display: none; }
}
@media print { .seg { display: none; } .layout { grid-template-columns: minmax(0, 1fr) 220px; gap: 28px 32px; }
  .shot, .shot .phone { width: 220px; position: static; } }
"""

STYLE = BASE_STYLE + VERIFY_STYLE


def render_html(result, run_dir):
    """The report for ``result`` (§5.5), as one HTML string."""
    run_dir = Path(run_dir)
    verdict = result.get("verdict")
    word, tone = VERDICTS.get(verdict, VERDICTS["couldnt_run"])
    reason = result.get("reason") or {}
    check = result.get("check") or {}
    device = result.get("device") or {}
    app = result.get("app") or {}
    assertions = result.get("assertions") or []
    held = sum(bool(item.get("ok")) for item in assertions)
    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        f'<meta http-equiv="Content-Security-Policy" content="{CSP}">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="color-scheme" content="light dark">',
        f"<title>{_e(TITLES.get(verdict, TITLES['couldnt_run']))}: {_e(check.get('name'))}</title>",
        f"<style>{STYLE}</style></head><body><main>",
        brand("verify", result.get("run_id")),
        '<header class="head"><div class="chips">', verdict_chip(word, tone),
    ]
    parts.append(f'</div><h1>{_e(check.get("name"))}</h1>')
    if assertions and reason.get("class") in (None, "assertion"):
        lede = (f"All {len(assertions)} expectations held" if held == len(assertions) else
                f"{len(assertions) - held} of {len(assertions)} expectations failed")
        parts.append(f'<p class="lede">{_e(lede)}</p>')
    # Why, when the assertions don't say (couldn't run, needs review, blocked), and the fix.
    failing = [item for item in assertions if not item.get("ok")]
    lines = []
    if reason.get("class") not in (None, "assertion") or not assertions:
        lines.append(reason.get("message") or result.get("summary"))
        if result.get("summary") and result.get("summary") != reason.get("message") and not failing:
            lines.append(result.get("summary"))
    if reason.get("fix"):
        lines.append(reason.get("fix"))
    lines = [line for line in lines if line]
    if lines:
        parts.append(f'<div class="callout {tone}">' + "".join(f"<p>{_e(line)}</p>" for line in lines) + "</div>")
    device_line = " · ".join(filter(None, [device.get("type"), device.get("runtime")])) or device.get("name")
    app_line = " ".join(filter(None, [app.get("name") or app.get("bundle_id"), app.get("version")]))
    meta = [("sim", device_line), ("app", app_line),
            ("clock", seconds_text(result.get("seconds")) if result.get("seconds") is not None else None),
            ("calendar", when(run_id=result.get("run_id"))), ("dash", MODES.get(result.get("mode")))]
    parts.append('<ul class="meta">' + "".join(f"<li>{icon(name)}{_e(dots(value))}</li>" for name, value in meta
                                               if value) + "</ul></header>")

    frames = result.get("frames") or []
    ax = next((f for f in reversed(frames) if f.endswith("-verdict-ax.jpg")), None)
    plain = next((f for f in reversed(frames) if f.endswith("-verdict.jpg")), None)
    ax_url = _data_url(run_dir / ax, FRAME_WIDTH, 80) if ax else None
    plain_url = _data_url(run_dir / plain, FRAME_WIDTH, 80) if plain else None

    has_steps = bool(result.get("steps"))
    sections = 2 + has_steps + bool(result.get("repro")) + bool(result.get("agent"))
    parts.append('<div class="layout"><section aria-labelledby="expectations"><h2 id="expectations">Expectations'
                 "</h2>")
    if assertions:
        parts.append('<div class="card">' + expectations(assertions, frame=bool(ax_url)) + "</div>")
    elif check.get("expect"):
        parts.append('<p class="empty">Not evaluated: the run ended before the verdict read.</p>')
    else:
        parts.append('<p class="empty">The check has no expectations.</p>')
    notes = []
    if result.get("stable") is False:
        notes.append("The screen was still changing when Mobster read it.")
    if result.get("alert"):
        notes.append(f"An alert was showing: {result.get('alert')}")
    if notes:
        parts.append('<div class="notes">' + "".join(f"<p>{_e(note)}</p>" for note in notes) + "</div>")
    parts.append("</section>")
    if ax_url or plain_url:
        parts.append(f'<aside class="shot" style="grid-row: 1 / span {sections}" aria-labelledby="frame">'
                     '<h2 class="sr" id="frame">The verdict frame</h2>')
        if ax_url and plain_url:
            parts.append('<input class="sr" type="radio" name="frame" id="frame-ax" checked '
                         'aria-label="The frame with the overlay">'
                         '<input class="sr" type="radio" name="frame" id="frame-plain" aria-label="The plain frame">'
                         '<div class="seg" aria-hidden="true"><label for="frame-ax">Outlined</label>'
                         '<label for="frame-plain">Plain</label></div>')
        parts.append('<div class="phone">')
        if ax_url:
            parts.append(f'<img class="ax" src="{ax_url}" alt="The screen at the verdict, each asserted element '
                         f'outlined and numbered as in the list: green held, red failed">')
        if plain_url:
            parts.append(f'<img class="plain" src="{plain_url}" alt="The screen at the verdict">')
        parts.append("</div>" + (OVERLAY_LEGEND if ax_url else "") + "</aside>")

    # A launch-only run took no steps: no Steps section.
    if has_steps:
        parts.append('<section aria-labelledby="steps"><h2 id="steps">Steps</h2>')
    launch = next((f for f in frames if f.endswith("-launch.jpg")), None)
    rows = []
    if launch:
        rows.append((launch, "Launched the app", None, None))
    for step in result.get("steps") or []:
        target = step.get("target") or {}
        who = ", ".join(f"{key} {_short(value, 60)}" for key, value in (
            ("id", target.get("id")), ("label", target.get("label")), ("role", target.get("role"))) if value)
        changed = step.get("changed")
        meta_line = ", ".join(filter(None, [who or None, None if changed is None else (
            "the screen changed" if changed else "no visible change")]))
        rows.append((step.get("frame"), step.get("text") or step.get("op"), meta_line,
                     f"{(step.get('at_ms') or 0) / 1000:.1f} s"))
    if rows and has_steps:
        parts.append('<div class="card"><ol class="steps">')
        for number, (frame, text, meta_line, at) in enumerate(rows, 1):
            url = _data_url(run_dir / frame, THUMB_WIDTH) if frame else None
            image = (f'<div class="thumb"><img src="{url}" alt=""></div>' if url
                     else '<div class="thumb noimg"></div>')
            parts.append(f'<li><span class="step-n">{number}</span>{image}<div><div class="op">{_e(text)}</div>'
                         + (f'<div class="step-meta">{_e(meta_line)}</div>' if meta_line else "") + "</div>"
                         + (f'<span class="at">{_e(at)}</span>' if at else "<span></span>") + "</li>")
        parts.append("</ol></div></section>")

    timing = result.get("timing") or {}
    facts = [
        ("App", " ".join(filter(None, [app.get("name"), app.get("version"), f"({app.get('bundle_id')})"
                                       if app.get("bundle_id") else None])) or None, ""),
        ("Simulator", dots(device.get("name") or device.get("type")), ""),
        ("Mode", {"smart": "Smart", "keyless": "Key-less (your coding agent drove)",
                  "launch": "Launch-only"}.get(result.get("mode"), result.get("mode")), ""),
        ("Duration", f"{result.get('seconds', 0)} s", ("sub", "\u00a0· ".join(
            f"{key}\u00a0{value}\u00a0s" for key, value in timing.items()))),
        ("Model cost", f"${result.get('cost_usd'):.4f}" if result.get("cost_usd") else "$0", ""),
        ("Stable read", {True: "Yes", False: "No, the screen was still changing", None: "Not read"}.get(
            result.get("stable")), ""),
        ("Run", result.get("run_id"), "mono"),
        ("Mobster", (result.get("mobster") or {}).get("version"), ""),
        ("App path", home(app.get("path")), "mono wide"),
    ]
    parts.append('<section aria-labelledby="run"><h2 id="run">Run</h2><div class="card"><dl class="facts">')
    for name, value, kind in facts:
        if value:
            sub = f'<span class="sub">{_e(kind[1])}</span>' if isinstance(kind, tuple) and kind[1] else ""
            kind = "" if isinstance(kind, tuple) else kind
            wide = ' class="wide"' if "wide" in kind else ""
            mono = ' class="mono"' if "mono" in kind else ""
            parts.append(f"<div{wide}><dt>{_e(name)}</dt><dd{mono}>{_e(value)}{sub}</dd></div>")
    parts.append("</dl></div></section>")

    if result.get("repro"):
        parts.append(f'<section aria-labelledby="again"><h2 id="again">Run it again</h2>'
                     f'{command(home(result["repro"]))}</section>')
    agent = result.get("agent")
    if agent:
        ended = "Smart ended " + str(agent.get("status")) + (f" after {agent.get('turns')} turns"
                                                              if agent.get("turns") else "")
        parts.append('<section aria-labelledby="note"><h2 id="note">The model\'s note (not the verdict)</h2>'
                     f'<div class="card"><p class="note">{_e(agent.get("note") or "No note.")}'
                     f'<span class="note-meta">{_e(ended)}</span></p></div></section>')
    parts.append(f'</div><footer class="foot"><span>Mobster {_e((result.get("mobster") or {}).get("version"))}</span>'
                 f'<span>Run <span class="mono">{_e(result.get("run_id"))}</span></span></footer>')
    parts.append("</main></body></html>")
    return "\n".join(parts)


def write_report(run_dir, result, *, scrub=lambda text: text):
    """report.html in ``run_dir`` for ``result``. result.json is written by the run itself."""
    run_dir = Path(run_dir)
    text = scrub(render_html(result, run_dir))
    temporary = run_dir / "report.html.tmp"
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, run_dir / "report.html")
    return run_dir / "report.html"


def write_result(run_dir, result):
    run_dir = Path(run_dir)
    temporary = run_dir / "result.json.tmp"
    temporary.write_text(json.dumps(result, indent=1, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, run_dir / "result.json")
    return run_dir / "result.json"
