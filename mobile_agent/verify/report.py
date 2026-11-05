"""result.json (``mobster.verify/1``) and report.html, which makes no requests and escapes every app string."""

import base64
import html
import os
import re
import time
from io import BytesIO
from pathlib import Path

from .. import brand as tokens
from ..brand_fonts import FONT_FACES
from .overlay import pill_numbers

CSP = "default-src 'none'; img-src data:; font-src data:; style-src 'unsafe-inline'"


THUMB_WIDTH = 160
FRAME_WIDTH = 600       # px of the frames embedded at FRAME_CSS_WIDTH (2x)
VERDICTS = {"passed": ("Passed", "pass"), "failed": ("Failed", "fail"),
            "needs_review": ("Needs review", "review"), "couldnt_run": ("Couldn't run", "none")}
MODES = {"smart": "Smart", "keyless": "Key-less", "launch": "Launch-only"}


FINDING_WORDS = {"warn": "Warning. ", "known": "In the baseline. "}


def _short(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _e(value):
    return html.escape("" if value is None else str(value), quote=True)


def _png_url(path, limit=200_000):
    """A small PNG the run saved (the app's icon) as a data URL, or None when there is none."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    return "data:image/png;base64," + base64.b64encode(data).decode() if data.startswith(b"\x89PNG") and len(
        data) <= limit else None


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
MARK_SVG = ('<span class="mark" aria-hidden="true">'
            + "".join(tokens.MARK[theme].replace("<svg ", f'<svg class="mark-{theme}" ', 1)
                      for theme in ("light", "dark"))
            + "</span>")


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
    """Whether the overlay outlines an assertion's element: it matched one with an area (a suite's copy of the assertion
    says so in ``outlined``)."""
    if "outlined" in item:
        return bool(item["outlined"])
    return any(len(match.get("rect") or ()) == 4 and match["rect"][2] > 0 and match["rect"][3] > 0
               for match in item.get("matches") or () if isinstance(match, dict))


def expectations(items, *, frame=True, quiet_held=False):
    """The numbered list of assertions: ``items`` are result.json's assertions (index, text, ok, observed, matches)."""
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


def business_section(result):
    business = result.get("business")
    if not isinstance(business, dict):
        return ""
    expected = {item.get("field"): item.get("equals") for item in
                (result.get("check") or {}).get("business", {}).get("expect", ()) if isinstance(item, dict)}

    def shown(value):
        return str(value).lower() if value is None or type(value) is bool else f"[{type(value).__name__} redacted]"

    rows = []
    for item in business.get("assertions", ()):
        field = item.get("field")
        status = "held" if item.get("ok") else "failed"
        observed = shown(item.get("observed")) if item.get("present") else "missing"
        rows.append(f'<li class="{status}"><code class="assert">{_e(field)}</code>'
                    f'<span class="observed">expected {_e(shown(expected.get(field)))}; '
                    f'observed {_e(observed)}</span></li>')
    lifecycle = (f'Setup: {business.get("setup", "not_started")}; '
                 f'Observation: {business.get("status", "unavailable")}; '
                 f'Cleanup: {business.get("cleanup", "not_needed")}')
    detail = f'<p>{_e(lifecycle)}</p>'
    if business.get("error"):
        detail += f'<p>{_e(business["error"])}</p>'
    if business.get("primary"):
        detail += f'<p>Primary result: {_e(business["primary"].get("verdict"))} '
        detail += f'({_e(business["primary"].get("class"))}).</p>'
    return ('<h2>Persisted app state</h2><div class="card">' + detail
            + ('<ol class="expect">' + "".join(rows) + "</ol>" if rows else "") + "</div>")


EVIDENCE_TEXT = 300   # characters of a log line the page shows; logs/app.log has the whole line


def _seconds(ms):
    return f"{(ms or 0) / 1000:.1f} s"


def crash_title(crash):
    """'Crashed: EXC_BREAKPOINT (SIGTRAP)', or 'Crashed: SIGTRAP' before the report is in."""
    what = " ".join(filter(None, [crash.get("type"), f"({crash['signal']})" if crash.get("type") and crash.get("signal")
                                  else crash.get("signal")]))
    return "Crashed" + (f": {what}" if what else "")


def evidence_pane(evidence, *, step=None):
    from .crashes import frame_text
    evidence = evidence if isinstance(evidence, dict) else {}
    crash, window, log = evidence.get("crash"), evidence.get("window"), evidence.get("log")
    if not crash and not window:
        return ""
    at = evidence.get("atMs")
    after = f", step {step}" if step else ""
    out = ['<div class="evidence">']
    if crash:
        link = (f'<a class="ev-file" href="{_e(crash["report"])}">Crash report</a>' if crash.get("report")
                else "")
        out.append(f'<div class="ev-part fail"><p class="ev-head"><strong>{_e(crash_title(crash))}</strong>'
                   f'<span>at {_e(_seconds(crash.get("atMs")))}{_e(after)}</span>{link}</p>')
        if crash.get("message"):
            out.append(f'<p class="ev-msg">{_e(crash["message"])}</p>')
        frames = crash.get("frames") or []
        if frames:
            out.append('<ol class="frames" aria-label="The crashed thread\'s top frames">')
            for frame in frames:
                where = f"{frame['file']}:{frame['line']}" if frame.get("file") and frame.get("line") else ""
                symbol = frame_text({**frame, "file": None}, image=False)
                out.append(f'<li class="{"own" if frame.get("own") else ""}"><span class="fn">{_e(frame.get("index"))}'
                           f'</span><span class="fimg">{_e(frame.get("image"))}</span><span><code>{_e(symbol)}</code>'
                           + (f'<span class="floc">{_e(where)}</span>' if where else "") + "</span></li>")
            out.append("</ol>")
            if crash.get("symbolicated"):
                out.append('<p class="ev-note">Your frames are named from the dSYM in .mobster/build.</p>')
        else:
            out.append('<p class="ev-note">The app exited on this signal. macOS hadn\'t written its crash report '
                       "when the run ended, so there is no stack here.</p>")
        out.append("</div>")
    if window is not None:
        lines = window.get("lines") or []
        link = f'<a class="ev-file" href="{_e(log["path"])}">Full log</a>' if log and log.get("path") else ""
        count = f"{len(lines)} line{'' if len(lines) == 1 else 's'}" + (
            f" of {log['lines']}" if log and log.get("lines") and log["lines"] > len(lines) else "")
        out.append(f'<div class="ev-part"><p class="ev-head"><strong>Log, 10 s either side</strong><span>{_e(count)}'
                   f"</span>{link}</p>")
        if lines:
            out.append('<ol class="ev-lines">')
            marked = False
            for line in lines:
                if not marked and at is not None and (line.get("atMs") or 0) > at:
                    out.append(_at_row(crash, at))
                    marked = True
                level = line.get("level") or ""
                text = str(line.get("text") or "")
                text = text if len(text) <= EVIDENCE_TEXT else text[:EVIDENCE_TEXT] + "…"
                out.append(f'<li class="{_e(level)}"><span class="t">{_e(f"{(line.get("atMs") or 0) / 1000:.3f}")}'
                           f'</span><span class="lv">{_e(level)}</span><span class="tx">{_e(text)}</span></li>')
            if not marked and at is not None:
                out.append(_at_row(crash, at))
            out.append("</ol>")
        else:
            out.append('<p class="ev-note">The app logged nothing in those 20 seconds.</p>')
        out.append("</div>")
    out.append("</div>")
    return "".join(out)


SIDES = {"app": "App problem", "check": "Check problem", "environment": "Environment problem"}


def cause_line(proof):
    """'App problem: app crashed', for a run that didn't pass."""
    cause = (proof or {}).get("cause")
    if not isinstance(cause, dict) or not cause.get("line"):
        return ""
    return f'<p class="cause"><strong>{_e(SIDES.get(cause.get("side"), "Cause"))}</strong> {_e(cause["line"])}</p>'


def _clock(seconds):
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


def proof_pane(entry, *, clip=None, video=None, title="", opened=False):
    """One step's proof: its clip (when the recording is known), the app's log lines in its span and the requests the
    log shows. ``clip`` is (from, to) seconds into ``video``."""
    if not isinstance(entry, dict):
        return ""
    log, network = entry.get("log") or {}, entry.get("network") or {}
    lines, rows = log.get("lines") or [], network.get("rows") or []
    if not lines and not rows and not (clip and video):
        return ""
    counts = [f"{log.get('count') or 0} log line{'' if log.get('count') == 1 else 's'}",
              f"{network.get('count') or 0} request{'' if network.get('count') == 1 else 's'}"]
    if clip and video:
        counts.insert(0, f"clip {_clock(clip[0])} to {_clock(clip[1])}")
    out = [f'<details class="proof"{" open" if opened else ""}><summary>{_e(title or "Proof")}'
           f"<span>{_e(', '.join(counts))}</span></summary>"]
    if clip and video:
        out.append(f'<p class="ev-note"><a href="{_e(video)}#t={clip[0]},{clip[1]}">Play this step</a> '
                   f"({clip[0]} to {clip[1]} s of the recording)</p>")
    if rows:
        out.append('<table class="net"><tbody>')
        for row in rows:
            size = f"{row['bytes']:,} B" if isinstance(row.get("bytes"), int) else ""
            took = f"{row['durationMs']} ms" if isinstance(row.get("durationMs"), int) else ""
            out.append(f'<tr><td>{_e(row.get("method") or "")}</td><td>{_e(row.get("host"))}</td>'
                       f'<td>{_e(row.get("status") or "")}</td><td>{_e(took)}</td><td>{_e(size)}</td></tr>')
        out.append("</tbody></table>")
    if lines:
        out.append('<ol class="ev-lines">')
        for line in lines:
            text = str(line.get("text") or "")
            text = text if len(text) <= EVIDENCE_TEXT else text[:EVIDENCE_TEXT] + "…"
            out.append(f'<li class="{_e(line.get("level") or "")}"><span class="t">'
                       f'{_e(f"{(line.get("atMs") or 0) / 1000:.3f}")}</span><span class="lv">'
                       f'{_e(line.get("level") or "")}</span><span class="tx">{_e(text)}</span></li>')
        out.append("</ol>")
    out.append("</details>")
    return "".join(out)


def _at_row(crash, at):
    words = ("Crash" if crash else "Failure") + f" at {at / 1000:.3f} s"
    return f'<li class="at" role="separator">{_e(words)}</li>'


OVERLAY_LEGEND = ('<p class="legend" aria-hidden="true"><span class="key read"></span>Read by Mobster '
                  '<span class="key held"></span>Held <span class="key failed"></span>Failed</p>')


def command(text):
    """A shell command, highlighted as on mobster.dev: flags violet, quoted strings green, arguments grey."""
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
    """A device's label for the page: 'iPhone 17 Pro · iOS 26.4' reads 'iPhone 17 Pro, iOS 26.4'."""
    # The suite keeps the dotted label (JUnit names it); the page has no middle dots.
    return None if text is None else str(text).replace(" · ", ", ")


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


# Tokens only (test_brand: no colour literals); pills keep the light tones in both schemes, as the overlay does.
COMPONENTS = """
:root { --pass: var(--green); --fail: var(--coral); --review: var(--violet-text); --none: var(--ink-2);
  --read: var(--violet); --bezel: var(--ink);
  --fill-quiet: color-mix(in srgb, var(--ink) 7%, transparent);
  --fill-input: color-mix(in srgb, var(--ink) 5%, transparent);
  --hover: color-mix(in srgb, var(--ink) 6%, transparent);
  --pass-solid: PASS_SOLID; --fail-solid: FAIL_SOLID; --review-solid: var(--violet-solid); --none-solid: var(--ink-3); }
@media (prefers-color-scheme: dark) { :root { --bezel: var(--line-strong); } }
* { box-sizing: border-box; }
html { background: var(--paper); -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--paper); color: var(--ink); font: 400 14px/22px var(--font-sans);
  font-synthesis: none; -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility; }
main { max-width: 1040px; margin: 0 auto; padding: 32px 24px 64px; }
:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
a:focus-visible { border-radius: var(--radius-xs); }
::selection { background: var(--violet-wash); color: var(--ink); }
.sr { position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0; overflow: hidden;
  clip: rect(0 0 0 0); white-space: nowrap; border: 0; }
.icon { width: 16px; height: 16px; flex: none; }
code, pre, .mono { font-family: var(--font-mono); }
a { color: inherit; text-decoration-line: underline; text-decoration-thickness: 1px; text-underline-offset: .2em;
  text-decoration-color: color-mix(in srgb, currentColor 35%, transparent);
  transition: text-decoration-color var(--duration-medium) var(--ease-standard); }
a:hover { text-decoration-color: currentColor; }
.pass { --tone: var(--pass); --solid: var(--pass-solid); } .fail { --tone: var(--fail); --solid: var(--fail-solid); }
.review { --tone: var(--review); --solid: var(--review-solid); } .none { --tone: var(--none); --solid: var(--none-solid); }
.warn { --tone: var(--amber); }

/* The page's head: the mark and the tool, the verdict, the one line that matters, then the facts. */
.brand { display: flex; align-items: center; gap: 8px; font-size: 13px; line-height: 20px; color: var(--ink-3);
  min-width: 0; }
.mark { display: block; width: 22px; height: 22px; flex: none; }
.mark svg { display: block; width: 100%; height: 100%; }
.mark .mark-dark { display: none; }
@media (prefers-color-scheme: dark) { .mark .mark-light { display: none; } .mark .mark-dark { display: block; } }
.brand-name { margin-left: -2px; font-size: 15px; font-weight: var(--weight-medium); letter-spacing: -.01em;
  color: var(--ink); }
.brand-tool::before { content: "/"; margin-right: 8px; color: var(--ink-4); }
.brand-id { margin-left: auto; font: 12px/18px var(--font-mono); color: var(--ink-3); overflow-wrap: anywhere;
  text-align: right; }
header.head { padding-top: 32px; }
.chips { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; }
.verdict { display: inline-flex; align-items: center; gap: 6px; font-size: 14px; line-height: 20px;
  font-weight: var(--weight-medium); color: var(--tone); }
.verdict .icon { stroke-width: 2; }
.tag { display: inline-flex; align-items: center; gap: 6px; height: 20px; padding: 0 7px;
  border-radius: var(--radius-xs); font-size: 12px; line-height: 18px; font-weight: var(--weight-medium);
  color: var(--ink-2); background: var(--fill-quiet); white-space: nowrap; }
.tag.tone::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: var(--tone); }
.tag .icon { width: 12px; height: 12px; stroke-width: 2; color: var(--tone, var(--ink-3)); }
h1 { font-size: 28px; line-height: 36px; letter-spacing: -.015em; font-weight: var(--weight-medium);
  margin: 12px 0 4px; overflow-wrap: anywhere; text-wrap: balance; }
h1 .quiet { color: var(--ink-3); margin-left: 12px; }
.lede { margin: 0; font-size: 16px; line-height: 24px; color: var(--ink-2); overflow-wrap: anywhere; }
ul.meta { display: flex; flex-wrap: wrap; gap: 8px 20px; list-style: none; margin: 16px 0 0; padding: 0;
  font-size: 13px; line-height: 20px; color: var(--ink-2); }
ul.meta li { display: inline-flex; align-items: center; gap: 8px; min-width: 0; overflow-wrap: anywhere; }
ul.meta .icon { color: var(--ink-3); }
/* The app's own icon, read from its .app (sim/apps.py icon_png), in the squircle iOS draws it in, with the app's
   hairline ring (10% ink) so a dark icon keeps its edge on Night as in the Mac app. */
ul.meta .app-icon { width: 16px; height: 16px; border-radius: 23%; flex: none;
  box-shadow: 0 0 0 1px color-mix(in srgb, var(--ink) 10%, transparent); }
header.head pre.cmd { margin-top: 20px; }

/* Sections and cards: the app's flat card fill, no outline on Paper, a faint ring on Night. */
section { margin-top: 40px; }
h2 { display: flex; align-items: baseline; gap: 8px; margin: 0 0 12px; font-size: 16px; line-height: 24px;
  font-weight: var(--weight-title); letter-spacing: -.012em; }
h2 .count { font-size: 14px; font-weight: var(--weight-body); color: var(--ink-3);
  font-variant-numeric: tabular-nums; }
.card { background: var(--card); border-radius: var(--radius-card); box-shadow: var(--shadow-card); }
.empty { color: var(--ink-3); margin: 0; }

/* A shell command in a sunken well, highlighted like mobster.dev's code. */
pre.cmd { margin: 0; width: fit-content; max-width: 100%; padding: 10px 16px; background: var(--sunken);
  border-radius: var(--radius-input); font: 13px/20px var(--font-mono); color: var(--ink); white-space: pre-wrap;
  overflow-wrap: break-word; }
pre.cmd code { font: inherit; }
.hl-prompt { color: var(--ink-3); user-select: none; -webkit-user-select: none; }
.hl-flag { color: var(--violet-text); } .hl-string { color: var(--green); } .hl-arg { color: var(--ink-2); }
.hl-cmd { color: var(--ink); }

/* The numbered assertions, rows inside the card; the numbers are the overlay's pills. */
ol.expect { list-style: none; margin: 0; padding: 4px; }
ol.expect li { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 2px 12px; align-items: baseline;
  padding: 12px 16px; border-radius: var(--radius-container); }
ol.expect li.failed { background: color-mix(in srgb, var(--fail) 7%, transparent); }
code.assert { font: 13px/20px var(--font-mono); color: var(--ink); overflow-wrap: break-word; min-width: 0; }
.observed { grid-column: 2; font-size: 13px; line-height: 20px; color: var(--ink-3); overflow-wrap: break-word; }
li.failed .observed { color: var(--ink-2); }
.pill { position: relative; top: -1px; display: inline-flex; align-items: center; gap: 3px; height: 20px;
  min-width: 34px; padding: 0 8px 0 6px; border-radius: var(--radius-full); background: var(--solid); color: white;
  font: var(--weight-medium) 12px/1 var(--font-sans); font-variant-numeric: tabular-nums;
  print-color-adjust: exact; -webkit-print-color-adjust: exact; }
.pill .icon { width: 11px; height: 11px; stroke-width: 2.4; }

/* A frame, in a phone-shaped bezel (drawn hardware keeps its own radii). */
.phone { padding: 6px; border-radius: 46px; background: var(--bezel); box-shadow: var(--shadow-shot); }
.phone img { display: block; width: 100%; height: auto; border-radius: 40px; background: var(--sunken); }
.legend { display: flex; flex-wrap: wrap; align-items: center; justify-content: center; gap: 4px 6px;
  margin: 12px 0 0; font-size: 12px; line-height: 18px; color: var(--ink-3); }
.key { display: inline-block; width: 12px; height: 9px; border-radius: 3px; border: 1.5px solid; }
.legend .key:not(:first-child) { margin-left: 8px; }
.key.read { border-color: color-mix(in srgb, var(--read) 60%, transparent); }
.key.held { border-color: var(--pass-solid); background: color-mix(in srgb, var(--pass-solid) 14%, transparent); }
.key.failed { border-color: var(--fail-solid); background: color-mix(in srgb, var(--fail-solid) 14%, transparent); }

/* Facts, as a quiet grid. */
dl.facts { display: grid; grid-template-columns: repeat(auto-fill, minmax(176px, 1fr)); gap: 16px 24px; margin: 0;
  padding: 20px; }
dl.facts div { min-width: 0; }
dl.facts div.wide { grid-column: 1 / -1; }
dl.facts dt { font-size: 12px; line-height: 18px; color: var(--ink-3); margin-bottom: 2px; }
dl.facts dd { margin: 0; font-size: 14px; line-height: 20px; overflow-wrap: break-word; }
dl.facts dd.mono { font-size: 13px; }
footer.foot { margin-top: 56px; font-size: 12px; line-height: 18px; color: var(--ink-3); display: flex;
  flex-wrap: wrap; gap: 4px 16px; }

/* The replay core: the "Replayed in 4 s, $0" tag, and healed steps with their frames side by side. */
.heals { padding: 4px; }
.heal { padding: 16px; border-radius: var(--radius-container); }
.heal-title { margin: 0; font-weight: var(--weight-medium); overflow-wrap: anywhere; }
.heal-why { display: flex; flex-wrap: wrap; gap: 0 12px; margin: 2px 0 0; font-size: 13px; line-height: 20px;
  color: var(--ink-3); overflow-wrap: anywhere; }
.heal-frames { display: grid; grid-template-columns: repeat(2, minmax(0, 180px)); gap: 16px; margin: 16px 0 0; }
.heal-frames figure { margin: 0; }
.heal-frames figcaption { margin-bottom: 8px; font-size: 12px; line-height: 18px; font-weight: var(--weight-medium);
  color: var(--ink-3); }
.heal-frames .phone { padding: 4px; border-radius: 30px; }
.heal-frames .phone img { border-radius: 26px; }
.heal-review { display: grid; gap: 8px; margin: 16px 0 0; }
.heal-review div { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; min-width: 0; }
.heal-review .label { width: 96px; font-size: 13px; font-weight: var(--weight-medium); }
.heal-review pre.cmd { padding: 6px 12px; font-size: 12px; }

/* Failure evidence (evidence.py): the crash's stack and the app's log around the failure, beside the step. */
.evidence { display: grid; gap: 12px; min-width: 0; }
.ev-part { min-width: 0; padding: 12px 16px; border-radius: var(--radius-container); background: var(--sunken); }
.ev-part.fail { background: color-mix(in srgb, var(--fail) 7%, transparent); }
.ev-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 12px; margin: 0; font-size: 14px;
  line-height: 20px; }
.ev-head strong { font-weight: var(--weight-medium); color: var(--ink); overflow-wrap: anywhere; }
.ev-head span { font-size: 13px; color: var(--ink-3); font-variant-numeric: tabular-nums; }
.ev-head a { margin-left: auto; font-size: 13px; font-weight: var(--weight-medium); }
.ev-msg { margin: 4px 0 0; font: 13px/20px var(--font-mono); color: var(--ink); overflow-wrap: anywhere; }
.ev-note { margin: 8px 0 0; font-size: 13px; line-height: 20px; color: var(--ink-3); overflow-wrap: anywhere; }
ol.frames { list-style: none; margin: 8px 0 0; padding: 0; display: grid; gap: 2px; }
ol.frames li { display: grid; grid-template-columns: 20px minmax(96px, 168px) minmax(0, 1fr); gap: 12px;
  align-items: baseline; font: 12px/18px var(--font-mono); color: var(--ink-3); }
ol.frames li .fn { text-align: right; font-variant-numeric: tabular-nums; }
ol.frames li .fimg { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
ol.frames li code { color: var(--ink-2); overflow-wrap: anywhere; }
ol.frames li.own, ol.frames li.own code { color: var(--ink); }
ol.frames li .floc { display: block; color: var(--ink-3); }
.ev-lines { margin: 8px 0 0; padding: 0; list-style: none; display: grid; font: 12px/18px var(--font-mono); }
.ev-lines li { display: grid; grid-template-columns: 64px 44px minmax(0, 1fr); gap: 8px; color: var(--ink-2); }
.ev-lines li .t { text-align: right; color: var(--ink-3); font-variant-numeric: tabular-nums; }
.ev-lines li .lv { color: var(--ink-3); }
.ev-lines li.error .lv, .ev-lines li.fault .lv { color: var(--fail); }
.ev-lines li.error .tx, .ev-lines li.fault .tx { color: var(--ink); }
.ev-lines li .tx { overflow-wrap: anywhere; white-space: pre-wrap; }
.ev-lines li.at { display: flex; align-items: center; gap: 8px; margin: 4px 0; color: var(--fail);
  font-family: var(--font-sans); font-weight: var(--weight-medium); }
.ev-lines li.at::after { content: ""; flex: 1; height: 1px; background: color-mix(in srgb, var(--fail) 40%, transparent); }
ol.steps > li.failing { background: color-mix(in srgb, var(--fail) 7%, transparent); }
ol.steps > li.ev-row { display: block; padding: 4px 12px 12px 76px; }
.cause { margin: 12px 0 0; font-size: 14px; line-height: 22px; color: var(--ink-2); }
.cause strong { margin-right: 8px; font-weight: var(--weight-medium); color: var(--ink); }
details.proof { min-width: 0; }
details.proof > summary { cursor: pointer; font-size: 13px; line-height: 20px; color: var(--ink-2); }
details.proof > summary span { margin-left: 12px; color: var(--ink-3); font-variant-numeric: tabular-nums; }
table.net { margin: 8px 0 0; border-collapse: collapse; font: 12px/18px var(--font-mono); color: var(--ink-2); }
table.net td { padding: 0 16px 0 0; overflow-wrap: anywhere; }
.judged { margin: 8px 0 0; font-size: 13px; line-height: 20px; color: var(--ink-3); overflow-wrap: anywhere; }

@media (max-width: 640px) {
  main { padding: 20px 16px 48px; }
  .heal-review .label { width: auto; }
  header.head { padding-top: 24px; }
  h1 { font-size: 22px; line-height: 28px; }
  .lede { font-size: 14px; line-height: 22px; }
  ul.meta { gap: 6px 16px; }
  ol.expect li { padding: 12px; }
  dl.facts { padding: 16px; grid-template-columns: 1fr 1fr; gap: 16px; }
  ol.steps > li.ev-row { padding: 4px 4px 12px; }
  ol.frames li { grid-template-columns: 20px minmax(0, 1fr); }
  ol.frames li .fimg { display: none; }
  .ev-lines li { grid-template-columns: 56px minmax(0, 1fr); }
  .ev-lines li .lv { display: none; }
  .brand-id { display: none; }
}
@media (max-width: 480px) { dl.facts { grid-template-columns: minmax(0, 1fr); } }
@media print {
  :root { --bezel: var(--ink); }
  body { font-size: 12px; background: white; }
  main { max-width: none; padding: 0; }
  .mark .mark-light { display: block; } .mark .mark-dark { display: none; }
  section, li, .result, .shot { break-inside: avoid; }
  a { text-decoration: none; }
  * { print-color-adjust: exact; -webkit-print-color-adjust: exact; }
}
"""  # noqa: E501
BASE_STYLE = ("".join(FONT_FACES) + tokens.STYLESHEET + tokens.PRINT_STYLESHEET
              + COMPONENTS.replace("PASS_SOLID", tokens.LIGHT["green"]).replace("FAIL_SOLID", tokens.LIGHT["coral"]))

VERIFY_STYLE = """
.layout { display: grid; grid-template-columns: minmax(0, 1fr) 320px; gap: 40px 48px; align-items: start;
  margin-top: 40px; }
.layout > section { grid-column: 1; margin: 0; min-width: 0; }
.shot { grid-column: 2; position: sticky; top: 24px; width: 320px; }
.shot .phone { width: 320px; border-radius: 50px; }
.shot .phone img { border-radius: 44px; }
.snaps { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin: 0; }
.snaps figure { margin: 0; display: grid; gap: 6px; }
.snaps img { width: 100%; height: auto; border-radius: 8px; border: 1px solid var(--line); }
.snaps figcaption { font-size: 13px; line-height: 20px; color: var(--ink-2); }
.notes { margin: 12px 4px 0; display: grid; gap: 4px; }
.notes p { margin: 0; font-size: 13px; line-height: 20px; color: var(--ink-2); overflow-wrap: anywhere; }
.seg { display: flex; width: max-content; margin: 0 auto 12px; padding: 2px; gap: 2px;
  border-radius: var(--radius-full); background: var(--fill-input); }
.seg label { display: inline-flex; align-items: center; height: 28px; padding: 0 12px;
  border-radius: var(--radius-full); font-size: 13px; font-weight: var(--weight-medium); color: var(--ink-2);
  cursor: pointer; user-select: none; -webkit-user-select: none;
  transition: color var(--duration-short) var(--ease-standard), background-color var(--duration-short) var(--ease-standard); }
#frame-ax:checked ~ .seg label[for=frame-ax], #frame-plain:checked ~ .seg label[for=frame-plain] {
  background: var(--surface); color: var(--ink); box-shadow: var(--shadow-thumb); }
#frame-ax:focus-visible ~ .seg label[for=frame-ax], #frame-plain:focus-visible ~ .seg label[for=frame-plain] {
  outline: 2px solid var(--focus-ring); outline-offset: 1px; }
#frame-ax:checked ~ .phone .plain, #frame-plain:checked ~ .phone .ax { display: none; }
#frame-plain:checked ~ .legend { visibility: hidden; }
.callout { margin: 16px 0 0; max-width: 720px; padding: 12px 16px; border-radius: var(--radius-container);
  font-size: 14px; line-height: 20px; background: color-mix(in srgb, var(--tone) 10%, transparent);
  overflow-wrap: anywhere; }
.callout p { margin: 0; } .callout p + p { margin-top: 4px; color: var(--ink-2); }
ol.steps { list-style: none; margin: 0; padding: 4px; }
ol.steps > li { display: grid; grid-template-columns: 18px 40px minmax(0, 1fr) auto; gap: 14px; align-items: center;
  padding: 8px 12px; border-radius: var(--radius-container); }
.step-n { font: 12px/1 var(--font-mono); color: var(--ink-3); text-align: right; }
.thumb { width: 40px; aspect-ratio: 3 / 4; border-radius: var(--radius-inner); overflow: hidden;
  box-shadow: 0 0 0 1px color-mix(in srgb, var(--ink) 8%, transparent); background: var(--sunken); }
.thumb img { display: block; width: 100%; height: 100%; object-fit: cover; object-position: top; }
.op { font-weight: var(--weight-medium); overflow-wrap: anywhere; }
.step-meta { font-size: 13px; line-height: 20px; color: var(--ink-3); overflow-wrap: anywhere; }
.at { font-size: 13px; color: var(--ink-3); font-variant-numeric: tabular-nums; white-space: nowrap; }
dl.facts dd .sub { display: block; margin-top: 2px; font-size: 12px; line-height: 18px; color: var(--ink-3); }
.note { margin: 0; padding: 16px 20px; white-space: pre-wrap; overflow-wrap: anywhere; color: var(--ink-2); }
.note-meta { display: block; margin-top: 8px; font-size: 13px; color: var(--ink-3); }
@media (max-width: 860px) {
  .layout { grid-template-columns: minmax(0, 1fr); margin-top: 32px; }
  .shot { grid-column: 1; grid-row: auto !important; position: static; justify-self: center; max-width: 100%; }
  .shot .phone { max-width: 100%; }
}
@media (max-width: 640px) {
  ol.steps > li { grid-template-columns: 40px minmax(0, 1fr) auto; gap: 12px; }
  .step-n { display: none; }
}
@media print { .seg { display: none; } .layout { grid-template-columns: minmax(0, 1fr) 220px; gap: 28px 32px; }
  .shot, .shot .phone { width: 220px; position: static; } }
"""  # noqa: E501

STYLE = BASE_STYLE + VERIFY_STYLE


def replay_chip(replay, *, healed=0):
    replay = replay if isinstance(replay, dict) else {}
    mode = replay.get("mode")
    if mode in ("replayed", "healed"):
        cost = float(replay.get("costUsd") or 0)
        words = f"Replayed in {seconds_text(replay.get('seconds'))}, " + (f"${cost:.2f}" if cost >= .005 else "$0")
        chip = f'<span class="tag pass">{icon("check")}{_e(words)}</span>'
        count = healed or replay.get("healed") or 0
        if count:
            chip += f'<span class="tag tone review">{_e(count)} step{"" if count == 1 else "s"} healed</span>'
        return chip
    if replay.get("saved"):
        return '<span class="tag">Recorded for replay</span>'
    return ""


def heal_title(item):
    """'"Continue" is now "Next"' when one labelled step became another, else 'Tap "Continue" → Tap "Next"'."""
    old, new = item.get("old") or [], item.get("new") or []
    if len(old) == 1 and len(new) == 1 and old[0].get("label") and new[0].get("label") \
            and old[0].get("op") == new[0].get("op"):
        return f'"{old[0]["label"]}" is now "{new[0]["label"]}"'
    if not old and new:
        return f"Added: {item.get('to')}"
    return f"{item.get('from')} → {item.get('to')}"


def heal_command(source):
    """The name `mobster test heal` takes for a check: its file's stem in .mobster/checks, else its path."""
    if not source:
        return "CHECK"
    path = Path(source)
    return path.stem if path.parent.name == "checks" and path.parent.parent.name == ".mobster" else home(str(path))


def healed_section(healed, base_dir, source=None, *, heading="Healed steps", ident="healed"):
    """The heal diff review: each healed step, why, its frames before and after, and how to accept or reject it."""
    items = [item for item in healed or () if isinstance(item, dict)]
    if not items:
        return ""
    name = heal_command(source)
    out = [f'<section aria-labelledby="{_e(ident)}"><h2 id="{_e(ident)}">{_e(heading)}<span class="count">'
           f'{len(items)}</span></h2><div class="card heals">']
    for item in items:
        out.append('<div class="heal">')
        out.append(f'<p class="heal-title">Healed: {_e(heal_title(item))}</p>')
        why = [f"Step {item.get('step')}" if item.get("step") else None, item.get("why")]
        if any(why):
            out.append('<p class="heal-why">' + "".join(f"<span>{_e(part)}</span>" for part in why if part) + "</p>")
        frames = item.get("frames") or {}
        shots = []
        for label, key in (("Before", "before"), ("After", "after")):
            path = frames.get(key)
            url = _data_url(Path(base_dir) / path if path and not Path(path).is_absolute() else Path(path),
                            THUMB_WIDTH * 2) if path else None
            if url:
                shots.append(f'<figure><figcaption>{label}</figcaption><div class="phone"><img src="{url}" '
                             f'alt="The screen {label.lower()} the heal"></div></figure>')
        if shots:
            out.append('<div class="heal-frames">' + "".join(shots) + "</div>")
        out.append('<div class="heal-review">'
                   f'<div><span class="label">Accept heal</span>{command(f"mobster test heal --accept {name}")}'
                   '</div>'
                   f'<div><span class="label">Keep failing</span>{command(f"mobster test heal --reject {name}")}'
                   "</div></div>")
        out.append("</div>")
    out.append("</div></section>")
    return "".join(out)


SNAPSHOT_WORDS = {"matched": "matched the baseline", "changed": "changed", "size_changed": "has a different size",
                  "recorded": "baseline recorded", "updated": "baseline updated"}


def snapshot_section(snapshot, run_dir):
    """The screenshot comparison: one line, then the baseline, the current screen and the diff side by side."""
    if not isinstance(snapshot, dict) or not snapshot.get("status"):
        return ""
    line = f"{snapshot.get('name')} {SNAPSHOT_WORDS.get(snapshot['status'], snapshot['status'])}"
    if snapshot["status"] in ("matched", "changed"):
        line += (f": {snapshot.get('changedPercent', 0):.2f} % of the screen differs "
                 f"(limit {snapshot.get('thresholdPercent'):g} %)")
    figures = []
    for key, label in (("baseline", "Baseline"), ("current", "Current"), ("diff", "Diff")):
        url = _data_url(Path(run_dir) / snapshot[key], FRAME_WIDTH, 80) if snapshot.get(key) else None
        if url:
            figures.append(f'<figure><img src="{url}" alt="{label}: {_e(snapshot.get("name"))}">'
                           f"<figcaption>{label}</figcaption></figure>")
    return (f'<h2>Screenshot</h2><div class="card"><p class="lede">{_e(line)}</p>'
            + (f'<div class="snaps">{"".join(figures)}</div>' if figures else "") + "</div>")


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
        f"<title>{_e(VERDICTS.get(verdict, VERDICTS['couldnt_run'])[0])}: {_e(check.get('name'))}</title>",
        f"<style>{STYLE}</style></head><body><main>",
        brand("verify", result.get("run_id")),
        '<header class="head"><div class="chips">', verdict_chip(word, tone),
        replay_chip(result.get("replay"), healed=len(result.get("healed") or ())),
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
    parts.append(cause_line(result.get("step_proof")))
    device_line = ", ".join(filter(None, [device.get("type"), device.get("runtime")])) or dots(device.get("name"))
    app_line = " ".join(filter(None, [app.get("name") or app.get("bundle_id"), app.get("version")]))
    app_icon = _png_url(run_dir / "app-icon.png")
    meta = [(icon("sim"), device_line),
            (f'<img class="app-icon" src="{app_icon}" alt="">' if app_icon else icon("app"), app_line),
            (icon("clock"), seconds_text(result.get("seconds")) if result.get("seconds") is not None else None),
            (icon("calendar"), when(run_id=result.get("run_id"))), (icon("dash"), MODES.get(result.get("mode")))]
    parts.append('<ul class="meta">' + "".join(f"<li>{mark}{_e(dots(value))}</li>" for mark, value in meta
                                               if value) + "</ul></header>")

    frames = result.get("frames") or []
    ax = next((f for f in reversed(frames) if f.endswith("-verdict-ax.jpg")), None)
    plain = next((f for f in reversed(frames) if f.endswith("-verdict.jpg")), None)
    ax_url = _data_url(run_dir / ax, FRAME_WIDTH, 80) if ax else None
    plain_url = _data_url(run_dir / plain, FRAME_WIDTH, 80) if plain else None

    has_steps = bool(result.get("steps"))
    healed = healed_section(result.get("healed"), run_dir, check.get("source"))
    evidence = result.get("evidence") or {}
    failing = evidence.get("step") if has_steps else None
    pane = evidence_pane(evidence, step=failing)
    alone = bool(pane) and not any(step.get("index") == failing for step in result.get("steps") or ())
    sections = (2 + has_steps + bool(result.get("repro")) + bool(result.get("agent")) + bool(healed)
                + alone)
    parts.append('<div class="layout"><section aria-labelledby="expectations"><h2 id="expectations">Expectations'
                 "</h2>")
    if assertions:
        parts.append('<div class="card">' + expectations(assertions, frame=bool(ax_url)) + "</div>")
    elif check.get("expect") and not result.get("findings"):
        parts.append('<p class="empty">Not evaluated: the run ended before the verdict read.</p>')
    elif result.get("business") is not None:
        parts.append('<p class="empty">This check has no accessibility-tree expectations.</p>')
    else:
        parts.append('<p class="empty">The check has no expectations.</p>')
    if result.get("business") is not None:
        parts.append(business_section(result))
    if result.get("findings"):
        parts.append("<h2>Findings</h2>")
        parts.append('<div class="card"><ol class="expect">' + "".join(
            f'<li class="{"failed" if item.get("level") == "fail" else "review"}"><code class="assert">'
            f'{_e((item.get("guideline") + " " if item.get("guideline") else "") + str(item.get("id")))}'
            f'</code><span class="observed">{_e(FINDING_WORDS.get(item.get("level"), ""))}{_e(item.get("message"))}'
            f' Evidence: {_e(item.get("evidence"))}</span></li>'
            for item in result["findings"]) + "</ol></div>")
    parts.append(snapshot_section(result.get("snapshot"), run_dir))
    notes = []
    if result.get("stable") is False:
        notes.append("The screen was still changing when Mobster read it.")
    if result.get("alert"):
        notes.append(f"An alert was showing: {result.get('alert')}")
    if notes:
        parts.append('<div class="notes">' + "".join(f"<p>{_e(note)}</p>" for note in notes) + "</div>")
    parts.append("</section>")
    if alone:
        parts.append(f'<section aria-labelledby="logs"><h2 id="logs">Logs and crash</h2>{pane}</section>')
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
        judged = (result.get("step_proof") or {}).get("judged") or {}
        caption = (f'<p class="judged">Judged on {_e(Path(judged["frame"]).stem)}'
                   + (f', screen {_e(judged["treeHash"][:12])}' if judged.get("treeHash") else "")
                   + (f", at {_e(_seconds(judged['verdictAtMs']))}" if judged.get("verdictAtMs") is not None else "")
                   + "</p>") if judged.get("frame") else ""
        parts.append("</div>" + (OVERLAY_LEGEND if ax_url else "") + caption + "</aside>")

    if healed:
        parts.append(healed)
    if has_steps:
        parts.append('<section aria-labelledby="steps"><h2 id="steps">Steps</h2>')
    launch = next((f for f in frames if f.endswith("-launch.jpg")), None)
    rows = []
    by_step = {entry.get("index"): entry for entry in (result.get("step_proof") or {}).get("steps") or ()}
    if launch:
        rows.append((launch, "Launched the app", None, None, None))
    for step in result.get("steps") or []:
        target = step.get("target") or {}
        who = ", ".join(f"{key} {_short(value, 60)}" for key, value in (
            ("id", target.get("id")), ("label", target.get("label")), ("role", target.get("role"))) if value)
        changed = step.get("changed")
        meta_line = ", ".join(filter(None, [who or None, None if changed is None else (
            "the screen changed" if changed else "no visible change")]))
        rows.append((step.get("frame"), step.get("text") or step.get("op"), meta_line,
                     f"{(step.get('at_ms') or 0) / 1000:.1f} s", step.get("index")))
    if rows and has_steps:
        parts.append('<div class="card"><ol class="steps">')
        for number, (frame, text, meta_line, at, index) in enumerate(rows, 1):
            url = _data_url(run_dir / frame, THUMB_WIDTH) if frame else None
            image = (f'<div class="thumb"><img src="{url}" alt=""></div>' if url
                     else '<div class="thumb noimg"></div>')
            here = bool(pane) and not alone and index == failing
            mark = ' class="failing"' if here else ""
            parts.append(f'<li{mark}><span class="step-n">{number}</span>{image}<div><div class="op">{_e(text)}</div>'
                         + (f'<div class="step-meta">{_e(meta_line)}</div>' if meta_line else "") + "</div>"
                         + (f'<span class="at">{_e(at)}</span>' if at else "<span></span>") + "</li>")
            if here:
                parts.append(f'<li class="ev-row" aria-label="Logs and crash">{pane}</li>')
            detail = proof_pane(by_step.get(index), title="Proof for this step", opened=bool(here))
            if detail:
                parts.append(f'<li class="ev-row" aria-label="Proof for step {_e(index)}">{detail}</li>')
        tail = proof_pane((result.get("step_proof") or {}).get("after"), title="Until the verdict")
        if tail:
            parts.append(f'<li class="ev-row" aria-label="Proof until the verdict">{tail}</li>')
        parts.append("</ol></div></section>")

    timing = result.get("timing") or {}
    facts = [
        ("App", " ".join(filter(None, [app.get("name"), app.get("version"), f"({app.get('bundle_id')})"
                                       if app.get("bundle_id") else None])) or None, ""),
        ("Simulator", device_line, ""),
        ("Mode", {"smart": "Smart", "keyless": "Key-less (your coding agent drove)",
                  "launch": "Launch-only"}.get(result.get("mode"), result.get("mode")), ""),
        ("Duration", f"{result.get('seconds', 0)} s", ("sub", ", ".join(
            f"{key}\u00a0{value}\u00a0s" for key, value in timing.items()))),
        ("Model cost", f"${result.get('cost_usd'):.4f}" if result.get("cost_usd") else "$0", ""),
        ("Replay", _replay_fact(result.get("replay")), ""),
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


def _replay_fact(replay):
    if not isinstance(replay, dict) or not replay:
        return None
    if replay.get("status") == "proposal_unavailable":
        return replay.get("reason") or "The healed replay path could not be staged for review."
    mode = replay.get("mode")
    if mode in ("replayed", "healed"):
        parts = [f"{replay.get('replayed') or 0} of {replay.get('steps') or 0} steps with no model"]
        if replay.get("healed"):
            parts.append(f"{replay['healed']} healed")
        if replay.get("live"):
            parts.append(f"{replay['live']} by the model")
        return ", ".join(parts)
    if replay.get("saved"):
        return f"Recorded {replay.get('steps') or 0} steps; the next run replays them"
    return f"Smart ran every step ({replay['reason']})" if replay.get("reason") else None


def write_report(run_dir, result, *, scrub=lambda text: text):
    """report.html in ``run_dir`` for ``result``. result.json is written by the run itself."""
    run_dir = Path(run_dir)
    text = scrub(render_html(result, run_dir))
    temporary = run_dir / "report.html.tmp"
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, run_dir / "report.html")
    return run_dir / "report.html"
