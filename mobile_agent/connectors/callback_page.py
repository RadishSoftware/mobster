"""The page your browser shows when a sign-in comes back to this Mac (oauth.SignIn's listener): Mobster's app icon and
the service's own mark, joined by a dotted line, then one line that says what happened."""

import base64
import hashlib
import html
import re

from .. import brand
from ..brand_fonts import FONT_FACES
from .marks import DARK, LIGHT, NAMES

APP_LINK = "mobster://connectors"
BACK = "Mobster is back in front. You can close this tab."
# The success page waits for its line to finish drawing (about 600 ms) before Mobster comes forward.
HAND_BACK = (f'setTimeout(function () {{ location.href = "{APP_LINK}" }}, '
             f'matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 900)')

# Mobster's app icon, drawn from the mark (brand.MARK): the Night tile, the felt slabs and the violet row between them.
_SLABS = re.findall(r"M[^M]*", re.search(r' d="([^"]+)"', brand.MARK["light"]).group(1))
_TILE = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" fill="{night}"/>'
         '<g transform="translate(256 256) scale(.7) translate(-256 -256)">{paths}</g></svg>').format(
    night=brand.DARK["paper"],
    paths="".join(f'<path d="{d}" fill="{brand.LIGHT["violet-solid"] if index == 1 else brand.LIGHT["sunken"]}"/>'
                for index, d in enumerate(_SLABS)))
_SANS = "".join(face for face in re.findall(r"@font-face \{.*?\}", FONT_FACES) if '"Mobster Sans"' in face)


def _data(svg):
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def mark_of(name):
    """The service's mark, by any name it goes by ("Google Drive", "monday.com"), as the tile's picture: the mark on the
    tile's square (docs/brand/marks, scripts/gen_agent_marks.py), with its version for a dark page where it has one."""
    key = NAMES.get(re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-"))
    if key is None:
        return None
    img = f'<img src="{_data(LIGHT[key])}" alt="">'
    if key not in DARK:
        return img
    return f'<picture><source srcset="{_data(DARK[key])}" media="(prefers-color-scheme: dark)">{img}</picture>'


STYLE = """
/* A desktop page at desktop widths, as Remote's: Mobster at the gutter (32, 16 on a phone), then one centred 640 px
   column with a 32 px title; a phone gets the same column at its own width. */
body { margin: 0; min-height: 100vh; display: grid; grid-template-rows: auto 1fr; background: var(--paper);
  color: var(--ink); font: 400 16px/24px var(--font-sans); -webkit-font-smoothing: antialiased; }
.top { display: flex; align-items: center; min-height: 72px; padding: 16px 32px; }
.brand { display: inline-flex; align-items: center; gap: 8px; font-size: 17px; font-weight: 500; letter-spacing: -.01em; }
.brand img { display: block; width: 28px; height: 28px; border-radius: 23%; }
main { place-self: center; display: grid; justify-items: center; width: min(640px, calc(100vw - 32px)); padding: 48px 0;
  text-align: center; }
.link { display: flex; align-items: center; gap: 12px; margin-bottom: 32px; }
.tile { position: relative; flex: none; display: grid; place-items: center; width: 80px; height: 80px;
  border-radius: 23%; overflow: hidden; background: var(--surface); color: var(--ink-2); font-size: 32px; font-weight: 500; }
.tile img, .tile picture { display: block; width: 100%; height: 100%; }
.tile::after { content: ""; position: absolute; inset: 0; border-radius: inherit; box-shadow: inset 0 0 0 1px rgb(24 24 24 / .08); }
.run { display: flex; gap: 8px; }
.run i { width: 4px; height: 4px; border-radius: 50%; background: var(--ink-4); }
.knot { display: grid; place-items: center; width: 24px; height: 24px; border-radius: 50%; }
.knot svg { display: block; }
[data-state="ok"] .run i { background: var(--green); }
[data-state="ok"] .knot { background: var(--green); color: var(--paper); }
[data-state="error"] .knot { background: var(--coral); color: var(--paper); }
/* Not allowed: the line breaks where the check would be, and its last dots fade into the gap. */
[data-state="denied"] .knot, [data-state="pick"] .knot { visibility: hidden; }
[data-state="denied"] .run:first-of-type i:last-child, [data-state="denied"] .run:last-of-type i:first-child { opacity: .35; }
h1 { margin: 0; font-size: 32px; line-height: 40px; font-weight: 500; letter-spacing: -.025em; text-wrap: balance; }
p { margin: 8px auto 0; max-width: 60ch; color: var(--ink-2); text-wrap: pretty; }
/* The title and the line take the column's width, centred inside it, so a short one doesn't shrink the page. */
main > h1, main > p { justify-self: stretch; }
a { display: inline-flex; align-items: center; height: 40px; margin-top: 32px; padding: 0 16px; border-radius: 999px;
  background: var(--surface); box-shadow: var(--shadow-button-secondary); color: var(--ink); font-weight: 500;
  letter-spacing: -.011em; text-decoration: none; transition: background-color 150ms var(--ease-standard); }
a:hover { background: var(--card-hover); }
a:focus-visible { outline: 2px solid var(--violet-solid); outline-offset: 2px; }
@media (prefers-color-scheme: dark) { .tile::after { box-shadow: inset 0 0 0 1px rgb(255 255 255 / .1); } }
@media (max-width: 639px) { .top { padding: 16px; } h1 { font-size: 24px; line-height: 32px; letter-spacing: -.022em; }
  .tile { width: 64px; height: 64px; font-size: 28px; } .link { margin-bottom: 24px; } }
/* Connected: the dots arrive from each icon toward the middle, 50 ms apart, then the check grows in and draws. */
@media (prefers-reduced-motion: no-preference) {
  [data-state="ok"] .run i { animation: dot 160ms var(--ease-out) both; }
  [data-state="ok"] .run:first-of-type i:nth-child(2), [data-state="ok"] .run:last-of-type i:nth-child(3) { animation-delay: 50ms; }
  [data-state="ok"] .run:first-of-type i:nth-child(3), [data-state="ok"] .run:last-of-type i:nth-child(2) { animation-delay: 100ms; }
  [data-state="ok"] .run:first-of-type i:nth-child(4), [data-state="ok"] .run:last-of-type i:nth-child(1) { animation-delay: 150ms; }
  [data-state="ok"] .knot { animation: knot 240ms var(--ease-out) 260ms both; }
  [data-state="ok"] .knot path { stroke-dasharray: 16; animation: tick 200ms var(--ease-out) 400ms both; }
}
@keyframes dot { from { opacity: 0; transform: scale(.4); background: var(--ink-4); } }
@keyframes knot { from { opacity: 0; transform: scale(.6); } }
@keyframes tick { from { stroke-dashoffset: 16; } }
"""  # noqa: E501

GLYPHS = {
    "ok": '<path d="m7.5 12.5 3 3 6-6.5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
          'stroke-linejoin="round"/>',
    "error": '<path d="M12 7.5v5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>'
             '<circle cx="12" cy="16.25" r="1.25" fill="currentColor"/>',
    "denied": "",
    "pick": "",
}


def render(state, name, message):
    """The page for ``state`` ("ok", "denied" or "error") about the connector called ``name``, with its mark when the
    name is a service's (a preset keeps its service's name). ``name`` and ``message`` are never markup."""
    if state == "ok":
        message = BACK
    title = {"ok": f"Connected to {name}", "denied": f"{name} isn’t connected"}.get(state,
                                                                                    f"Mobster couldn’t connect {name}")
    return document(state, name, title, message, script=HAND_BACK if state == "ok" else None)


def document(state, name, title, message, *, script=None, nonce=None, actions="", style="", referrer="no-referrer"):
    """The page: Mobster's icon and ``name``'s mark joined in ``state``, then ``title`` and ``message`` (text, never
    markup), ``actions`` (markup) beside Open Mobster, and ``script`` inline, with ``nonce`` when the page's CSP names
    one."""
    # The app's typography: a curly apostrophe, also in a message the service or the listener wrote.
    title, message = (text.replace("'", "’") for text in (title, message))
    theirs = mark_of(name) or html.escape((name or "?").strip()[:1].upper())
    dots = '<span class="run">' + "<i></i>" * 4 + "</span>"
    link = (f'<div class="link" aria-hidden="true"><span class="tile"><img src="{_data(_TILE)}" alt=""></span>{dots}'
            '<span class="knot"><svg width="24" height="24" viewBox="0 0 24 '
            f'24">{GLYPHS.get(state, GLYPHS["error"])}</svg></span>{dots}<span class="tile">{theirs}</span></div>')
    attribute = f' nonce="{nonce}"' if nonce else ""
    script = f"<script{attribute}>{script}</script>" if script else ""
    back = f'<a href="{APP_LINK}">Open Mobster</a>'
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,'
            'initial-scale=1"><meta name="color-scheme" content="light dark"><meta '
            f'name="referrer" content="{referrer}">'
            f"<title>{html.escape(title)}</title><style>{_SANS}{brand.STYLESHEET}{STYLE}{style}</style>"
            f'<header class="top"><span class="brand"><img src="{_data(_TILE)}" alt="">Mobster</span></header>'
            f'<main data-state="{state if state in GLYPHS else "error"}" '
            f'aria-live="polite">{link}<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>'
            f'{f"<div class=actions>{actions}{back}</div>" if actions else back}</main>'
            f"{script}</html>")


def csp():
    """The page's Content-Security-Policy: nothing from outside, inline styles and data: pictures and fonts, and the
    hand-back script by its hash."""
    digest = base64.b64encode(hashlib.sha256(HAND_BACK.encode()).digest()).decode()
    return ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; script-src "
            f"'sha256-{digest}'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
