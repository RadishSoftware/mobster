"""An X-ray map as one self-contained page (report.html): it makes no requests, and every screen string is escaped."""

import base64
from pathlib import Path

from .. import links
from ..verify.report import BASE_STYLE, CSP, _e, brand, icon

XRAY_STYLE = """
.tag.tone::before { background: var(--violet); }
.facts-card { margin-top: 32px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(132px, 1fr)); gap: 20px 16px; }
.tile { min-width: 0; scroll-margin-top: 24px; }
.tile:target .thumb-x { box-shadow: 0 0 0 2px var(--violet); }
.thumb-x { display: block; aspect-ratio: 402 / 874; border-radius: 18px; overflow: hidden; background: var(--sunken);
  box-shadow: var(--shadow-thumb); }
.thumb-x img, .thumb-x svg { display: block; width: 100%; height: 100%; object-fit: cover; object-position: top; }
.tile h3 { margin: 10px 0 0; font-size: 14px; line-height: 20px; font-weight: var(--weight-medium);
  overflow-wrap: anywhere; }
.tile .via { margin: 2px 0 0; font-size: 12px; line-height: 18px; color: var(--ink-3); overflow-wrap: anywhere; }
.tile .opens { margin: 4px 0 0; font-size: 12px; line-height: 18px; color: var(--ink-2); }
.tile .pw { color: var(--amber); font-weight: var(--weight-medium); }
.depth + .depth { margin-top: 32px; }
.depth h3.level { margin: 0 0 12px; font-size: 13px; line-height: 20px; font-weight: var(--weight-medium);
  color: var(--ink-3); }
.wf-row { fill: color-mix(in srgb, var(--ink) 6%, transparent); }
.wf-bar { fill: color-mix(in srgb, var(--ink) 9%, transparent); }
.wf-text { fill: color-mix(in srgb, var(--ink) 22%, transparent); }
.wf-control { fill: color-mix(in srgb, var(--violet) 35%, transparent); }
.paywall { display: grid; grid-template-columns: 132px minmax(0, 1fr); gap: 20px; padding: 20px; }
.paywall + .paywall { border-top: 1px solid var(--line); }
.paywall h3 { margin: 0; font-size: 16px; line-height: 24px; font-weight: var(--weight-title); }
.paywall .headline { margin: 2px 0 0; color: var(--ink-2); overflow-wrap: anywhere; }
table.plans { width: 100%; margin-top: 12px; border-collapse: collapse; font-size: 13px; line-height: 20px;
  font-variant-numeric: tabular-nums; }
table.plans th { text-align: left; font-weight: var(--weight-medium); color: var(--ink-3); padding: 6px 12px 6px 0;
  border-bottom: 1px solid var(--line); }
table.plans td { padding: 8px 12px 8px 0; border-bottom: 1px solid var(--line); overflow-wrap: anywhere; }
table.plans tr:last-child td { border-bottom: 0; }
.signals { margin: 10px 0 0; font-size: 12px; line-height: 18px; color: var(--ink-3); }
ul.tree, ul.tree ul { list-style: none; margin: 0; padding: 0; }
ul.tree { padding: 16px 20px; font-size: 13px; line-height: 22px; }
ul.tree ul { padding-left: 18px; border-left: 1px solid var(--line); margin-left: 6px; }
ul.tree a { text-decoration: none; }
ul.tree a:hover { text-decoration: underline; }
ul.tree .edge { color: var(--ink-3); }
ul.tree .again { color: var(--ink-3); }
table.left { width: 100%; border-collapse: collapse; font-size: 13px; line-height: 20px; }
table.left td, table.left th { text-align: left; padding: 10px 16px; border-bottom: 1px solid var(--line);
  vertical-align: top; overflow-wrap: anywhere; }
table.left th { font-weight: var(--weight-medium); color: var(--ink-3); }
table.left tr:last-child td { border-bottom: 0; }
table.left td.n { font-variant-numeric: tabular-nums; color: var(--ink-2); white-space: nowrap; }
.why { margin: 0 0 12px; color: var(--ink-2); max-width: 720px; }
@media (max-width: 640px) {
  .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px 12px; }
  .paywall { grid-template-columns: 96px minmax(0, 1fr); padding: 16px; gap: 16px; }
}
"""
STYLE = BASE_STYLE + XRAY_STYLE
APP_NAMES = {"com.apple.Preferences": "Settings", "com.apple.weather": "Weather", "com.apple.mobilesafari": "Safari",
             "com.apple.Maps": "Maps", "com.apple.MobileSMS": "Messages", "com.apple.mobilenotes": "Notes"}
PERIODS = {"week": "Weekly", "month": "Monthly", "quarter": "Quarterly", "half-year": "Every 6 months",
           "year": "Yearly", "lifetime": "Lifetime", "": "Not stated"}


def duration(seconds):
    seconds = float(seconds or 0)
    if seconds < 60:
        return f"{round(seconds)} s"
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes} min {rest:02d} s"


def app_name(result):
    bundle = (result.get("app") or {}).get("bundle", "")
    if bundle in APP_NAMES:
        return APP_NAMES[bundle]
    screens = result.get("screens") or []
    return (screens[0].get("title") if screens else "") or bundle or "App"


def _thumb_src(base, screen):
    name = screen.get("thumb")
    if not name:
        return None
    try:
        data = (Path(base) / name).read_bytes()
    except OSError:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(data).decode()


def wireframe(screen):
    """The screen's layout as a drawn skeleton: rows, bars, controls and text as bars of their own width."""
    parts = ['<svg viewBox="0 0 100 217" preserveAspectRatio="none" aria-hidden="true">']
    for box in screen.get("boxes") or ():
        x, y, w, h, kind = box[0] * 100, box[1] * 217, box[2] * 100, box[3] * 217, box[4]
        if kind == "row":
            parts.append(f'<rect class="wf-row" x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{max(h - .6, .6):.1f}" '
                         'rx="1.5"/>')
        elif kind == "bar":
            parts.append(f'<rect class="wf-bar" x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}"/>')
        else:
            line = min(h, 3.2)
            cls = "wf-control" if kind == "control" else "wf-text"
            parts.append(f'<rect class="{cls}" x="{x:.1f}" y="{y + (h - line) / 2:.1f}" width="{max(w, 2):.1f}" '
                         f'height="{line:.1f}" rx="{line / 2:.1f}"/>')
    parts.append("</svg>")
    return "".join(parts)


def _thumb(base, screen, alt):
    src = _thumb_src(base, screen)
    inner = (f'<img src="{src}" alt="{_e(alt)}" loading="lazy">' if src else wireframe(screen))
    return f'<span class="thumb-x">{inner}</span>'


def _plans(paywall):
    rows = []
    for plan in paywall.get("plans") or ():
        per_month = (f"{plan.get('currency', '')} {plan['per_month']:.2f}" if plan.get("per_month")
                     and plan.get("period") not in ("month", "lifetime") else "")
        rows.append(f"<tr><td>{_e(plan.get('name') or PERIODS.get(plan.get('period', ''), ''))}</td>"
                    f"<td>{_e(plan.get('price'))}</td><td>{_e(PERIODS.get(plan.get('period', ''), plan.get('period')))}"
                    f"</td><td>{_e(per_month)}</td><td>{_e(plan.get('trial') or paywall.get('trial') or '')}</td></tr>")
    if not rows:
        return '<p class="empty">No prices read on this screen.</p>'
    return ('<table class="plans"><thead><tr><th>Plan</th><th>Price</th><th>Billed</th><th>A month</th>'
            '<th>Trial</th></tr></thead><tbody>' + "".join(rows) + "</tbody></table>")


def _crumbs(screen):
    return " › ".join(step.get("label", "") for step in screen.get("path") or ()) or "First screen"


def render(result, base="."):
    screens = result.get("screens") or []
    edges = result.get("edges") or []
    stats = result.get("stats") or {}
    by_id = {s["id"]: s for s in screens}
    name = app_name(result)
    bundle = (result.get("app") or {}).get("bundle", "")
    out_edges = {}
    for edge in edges:
        out_edges.setdefault(edge["from"], []).append(edge)
    connection = "Pixels (iPhone Mirroring)" if result.get("connection") == "pixels" else "Accessibility tree"
    lede = (f"{stats.get('screens', 0)} screens and {stats.get('edges', 0)} links, {stats.get('max_depth', 0)} taps "
            f"deep, mapped in {duration(stats.get('wall_seconds'))} with no model.")
    parts = [
        "<!doctype html>", '<html lang="en"><head><meta charset="utf-8">',
        f'<meta http-equiv="Content-Security-Policy" content="{CSP}">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="color-scheme" content="light dark">',
        f"<title>{_e(name)} X-ray</title>", f"<style>{STYLE}</style></head><body><main>",
        brand("App X-ray", bundle),
        '<header class="head"><div class="chips">',
        f'<span class="tag tone">{_e(connection)}</span><span class="tag">{icon("coin")}$0 in model calls</span>',
        f"</div><h1>{_e(name)}</h1>", f'<p class="lede">{_e(lede)}</p>',
        '<ul class="meta">',
        f'<li>{icon("app")}<span class="mono">{_e(bundle)}</span></li>',
        f'<li>{icon("calendar")}{_e((result.get("created_at") or "")[:16].replace("T", " "))}</li>',
        f'<li>{icon("clock")}Stopped: {_e(result.get("stopped"))}</li></ul></header>',
    ]
    facts = [("Screens", stats.get("screens")), ("Links", stats.get("edges")), ("Deepest", stats.get("max_depth")),
             ("Time", duration(stats.get("wall_seconds"))), ("Screens a minute", stats.get("screens_per_minute")),
             ("Taps", stats.get("taps")), ("Left alone", stats.get("refused")), ("Paywalls", stats.get("paywalls")),
             ("Ends with nothing to open", stats.get("dead_ends")), ("Model cost", "$0")]
    parts.append('<section class="facts-card"><dl class="facts card">' + "".join(
        f"<div><dt>{_e(k)}</dt><dd>{_e(v)}</dd></div>" for k, v in facts) + "</dl></section>")

    paywalled = [s for s in screens if s.get("paywall")]
    if paywalled:
        parts.append(f'<section><h2>Paywalls<span class="count">{len(paywalled)}</span></h2><div class="card">')
        for screen in paywalled:
            pw = screen["paywall"]
            parts.append('<div class="paywall"><a '
                         f'href="#{_e(screen["id"])}">{_thumb(base, screen, screen.get("name") or screen["title"])}</a>'
                         f'<div><h3>{_e(screen.get("name") or screen.get("title") or "Untitled")}</h3>'
                         f'<p class="headline">{_e(pw.get("headline") or _crumbs(screen))}</p>{_plans(pw)}'
                         f'<p class="signals">Read as a paywall from: {_e(", ".join(pw.get("signals") or ()))}. '
                         f'{"Shows Restore Purchases. " if pw.get("restore") else ""}Nothing on it was tapped.</p>'
                         "</div></div>")
        parts.append("</div></section>")

    parts.append(f'<section><h2>Screens<span class="count">{len(screens)}</span></h2>')
    for heading, group in _branches(screens, by_id):
        parts.append(f'<div class="depth"><h3 class="level">{_e(heading)} ({len(group)})</h3><div class="grid">')
        for screen in group:
            parts.append(_tile(base, screen, by_id, out_edges))
        parts.append("</div></div>")
    parts.append("</section>")

    parts.append('<section><h2>Paths</h2><div class="card"><ul class="tree">'
                 + _outline(screens, out_edges, by_id) + "</ul></div></section>")

    refused = {}
    for screen in screens:
        for item in screen.get("refused") or ():
            refused.setdefault(item["reason"], []).append(item["label"])
    if refused:
        parts.append(f'<section><h2>Left alone<span class="count">{stats.get("refused", 0)}</span></h2>'
                     '<p class="why">The crawler taps only controls shaped like navigation whose words name no act. '
                     'These it saw and didn’t tap.</p><div class="card"><table class="left"><thead><tr><th>Why</th>'
                     '<th>Count</th><th>Examples</th></tr></thead><tbody>')
        for reason, labels in sorted(refused.items(), key=lambda kv: -len(kv[1])):
            examples = ", ".join(f"“{label}”" for label in list(dict.fromkeys(labels))[:6])
            parts.append(f'<tr><td>{_e(reason)}</td><td class="n">{len(labels)}</td><td>{_e(examples)}</td></tr>')
        parts.append("</tbody></table></div></section>")

    parts.append(f'<footer class="foot"><span><a href="{links.MADE_WITH_XRAY}">Mobster</a> App X-ray, '
                 'experimental.</span>'
                 "<span>Screen text is shown as read from the app.</span></footer></main></body></html>")
    return "".join(parts)


def _branches(screens, by_id):
    def top(screen):
        seen = set()
        while screen.get("depth", 0) > 1 and screen.get("parent") in by_id and screen["id"] not in seen:
            seen.add(screen["id"])
            screen = by_id[screen["parent"]]
        return screen
    opening = [s for s in screens if s.get("depth", 0) == 0]
    groups = [("Where it opens", opening)] if opening else []
    sections = {}
    for screen in screens:
        if screen.get("depth", 0) == 0:
            continue
        sections.setdefault(top(screen)["id"], []).append(screen)
    for head_id, group in sections.items():
        head = by_id.get(head_id, group[0])
        groups.append((head.get("name") or head.get("title") or "Untitled", group))
    return groups


def _tile(base, screen, by_id, out_edges):
    leads = out_edges.get(screen["id"], [])
    opens = f"Opens {len(leads)}" if leads else "Nothing further"
    parent = by_id.get(screen.get("parent"))
    via = (f'From <a href="#{_e(parent["id"])}">{_e(parent.get("name") or parent.get("title") or parent["id"])}</a>'
           f' by “{_e(screen.get("via"))}”' if parent else "Where the app opens")
    flag = ' <span class="pw">Paywall</span>' if screen.get("paywall") else ""
    return (f'<article class="tile" id="{_e(screen["id"])}">'
            f'{_thumb(base, screen, screen.get("name") or screen.get("title"))}'
            f'<h3>{_e(screen.get("name") or screen.get("title") or "Untitled")}{flag}</h3><p class="via">{via}</p>'
            f'<p class="opens">{_e(opens)}</p></article>')


def _outline(screens, out_edges, by_id):
    """The crawl as nested lists: each screen under the one it was found from, each link with the control tapped."""
    children = {}
    for screen in screens:
        children.setdefault(screen.get("parent") or "", []).append(screen)
    lines = []

    def walk(screen, depth):
        lines.append('<li><a '
                     f'href="#{_e(screen["id"])}">{_e(screen.get("name") or screen.get("title") or "Untitled")}</a>')
        kids = children.get(screen["id"], [])
        again = [e for e in out_edges.get(screen["id"], [])
                 if e.get("to") and by_id.get(e["to"], {}).get("parent") != screen["id"]]
        if kids or again:
            lines.append("<ul>")
            for kid in kids:
                walk(kid, depth + 1)
            for edge in again[:8]:
                target = by_id.get(edge["to"], {})
                lines.append(f'<li class="again">“{_e(edge["label"])}” goes back to <a href="#{_e(edge["to"])}">'
                             f'{_e(target.get("name") or target.get("title") or edge["to"])}</a></li>')
            lines.append("</ul>")
        lines.append("</li>")

    for root in children.get("", []):
        walk(root, 0)
    return "".join(lines)


def write(result, folder, out=None):
    folder = Path(folder)
    path = Path(out) if out else folder / "report.html"
    path.write_text(render(result, folder), encoding="utf-8")
    return path
