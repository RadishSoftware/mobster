"""netxray.json (``mobster.netxray/1``), report.html and report.md; names from the traffic are data, always escaped."""

import json
import time
from pathlib import Path

from .. import links
from ..verify.report import BASE_STYLE, CSP, _e, brand, icon
from .analyze import headline
from .trackers import CATEGORY_LABELS, TRACKING

CATEGORY_TONE = {"advertising": "fail", "analytics": "warn", "attribution": "review", "engagement": "warn",
                 "crash": "none", "social": "none", "platform": "none", "cdn": "none"}

STYLE = BASE_STYLE + """
.stats { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 1px; margin: 0 0 12px;
  border-radius: var(--radius-card); overflow: hidden; }
.stat { padding: 14px 16px; background: var(--card); min-width: 0; }
.stat dt { font-size: 12px; line-height: 18px; color: var(--ink-3); }
.stat dd { margin: 2px 0 0; font-size: 20px; line-height: 28px; font-weight: var(--weight-medium);
  font-variant-numeric: tabular-nums; }
.stat.alert dd { color: var(--coral); }
table.hosts { width: 100%; border-collapse: collapse; font-size: 13px; line-height: 20px; }
table.hosts th { text-align: left; font-weight: var(--weight-medium); color: var(--ink-3); font-size: 12px;
  padding: 10px 12px 6px; white-space: nowrap; }
table.hosts td { padding: 8px 12px; border-top: 1px solid var(--line); vertical-align: middle; }
table.hosts td.num, table.hosts th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
table.hosts .host { font-family: var(--font-mono); font-size: 12.5px; overflow-wrap: anywhere; }
table.hosts .company { color: var(--ink-2); white-space: nowrap; }
table.hosts tr.tracking .host { color: var(--ink); }
.bar { display: block; height: 3px; margin-top: 4px; border-radius: 2px; background: var(--tone, var(--ink-4));
  opacity: .55; min-width: 2px; }
.hosts-card { padding: 4px 4px 8px; overflow-x: auto; }
.tag.tone.none::before { background: var(--ink-4); }
.unnamed { margin: 8px 16px 4px; font-size: 13px; color: var(--ink-3); }
.privacy { margin: 16px 0 0; max-width: 720px; font-size: 13px; line-height: 20px; color: var(--ink-2); }
.synthetic { --tone: var(--amber); }
@media (max-width: 760px) {
  .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  table.hosts .hide-narrow { display: none; }
  table.hosts td:first-child { min-width: 150px; }
  table.hosts td, table.hosts th { padding-left: 8px; padding-right: 8px; }
}
"""


def size(count):
    """Bytes as people read them: 812 B, 14.2 KB, 3.1 MB."""
    if count < 1000:
        return f"{count} B"
    if count < 1_000_000:
        return f"{count / 1000:.1f} KB"
    return f"{count / 1_000_000:.1f} MB"


def category_tag(category):
    if not category:
        return '<span class="quiet">Unknown</span>'
    label = CATEGORY_LABELS.get(category, category)
    return f'<span class="tag tone {CATEGORY_TONE.get(category, "none")}">{_e(label)}</span>'


def _app_section(app, seconds, index, *, lede=True):
    domains = app["domains"]
    biggest = max([d["up"] + d["down"] for d in domains] or [1]) or 1
    parts = [f'<section aria-labelledby="app-{index}"><h2 id="app-{index}">{_e(app["name"])}'
             f'<span class="count">{len(domains)} domain{"s" * (len(domains) != 1)}</span></h2>']
    if lede:
        parts.append(f'<p class="lede" style="margin-bottom:16px">{_e(headline(app, seconds))}</p>')
    via = ", ".join(app["via"])
    stats = [("Domains", app["domain_count"], False), ("Companies", len(app["companies"]), False),
             ("Ads, analytics, attribution", len(app["tracking"]), bool(app["tracking"])),
             ("Sent", size(app["up"]), False), ("Received", size(app["down"]), False)]
    parts.append('<dl class="stats">' + "".join(
        f'<div class="stat{" alert" * alert}"><dt>{_e(label)}</dt><dd>{_e(value)}</dd></div>'
        for label, value, alert in stats) + "</dl>")
    if domains:
        rows = []
        for domain in domains:
            total = domain["up"] + domain["down"]
            tone = CATEGORY_TONE.get(domain["category"] or "", "none")
            tracking = " tracking" if domain["category"] in TRACKING else ""
            rows.append(
                f'<tr class="{tone}{tracking}"><td><span class="host">{_e(domain["host"])}</span>'
                f'<span class="bar" style="width:{max(1, round(100 * total / biggest))}%"></span></td>'
                f'<td class="company hide-narrow">{_e(domain["company"])}</td>'
                f'<td>{category_tag(domain["category"])}</td>'
                f'<td class="num">{_e(size(domain["up"]))}</td><td class="num">{_e(size(domain["down"]))}</td>'
                f'<td class="num hide-narrow">{domain["connections"]}</td>'
                f'<td class="num hide-narrow">{domain["first"]:.1f} s</td></tr>')
        parts.append('<div class="card hosts-card"><table class="hosts"><thead><tr><th>Host</th>'
                     '<th class="hide-narrow">Company</th>'
                     '<th>Category</th><th class="num">Sent</th><th class="num">Received</th>'
                     '<th class="num hide-narrow">Connections</th><th class="num hide-narrow">First seen</th>'
                     '</tr></thead><tbody>' + "".join(rows) + "</tbody></table>")
        unnamed = app["unnamed"]
        if unnamed["connections"]:
            parts.append(f'<p class="unnamed">{unnamed["connections"]} more connection'
                         f'{"s" * (unnamed["connections"] != 1)} with no hostname in the capture '
                         f'({_e(size(unnamed["up"] + unnamed["down"]))}). Their addresses aren’t shown.</p>')
        parts.append("</div>")
    if app["kind"] == "ios" and app["processes"]:
        parts.append(f'<p class="privacy">These are parts of iOS itself: {_e(", ".join(app["processes"]))}.</p>')
    if app.get("note"):
        parts.append(f'<p class="privacy">{_e(app["note"])}</p>')
    if app["kind"] == "process" and app["processes"]:
        parts.append(f'<p class="privacy">Process: {_e(", ".join(app["processes"]))}.</p>')
    if via:
        parts.append(f'<p class="privacy">Some of this traffic went through {_e(via)} on the app’s behalf.</p>')
    parts.append("</section>")
    return "".join(parts)


def render_html(result, *, title="Network X-ray", synthetic=False):
    capture = result["capture"]
    apps = result["apps"]
    seconds = capture["seconds"]
    started = time.strftime("%d %b %Y %H:%M", time.localtime(capture["started"])) if capture["started"] else "-"
    focus = apps[0] if len(apps) == 1 else None
    heading = headline(focus, seconds) if focus else (
        f"{len(apps)} process{'es' * (len(apps) != 1)} talked to "
        f"{len({d['host'] for a in apps for d in a['domains']})} domains")
    chips = ['<span class="tag">Metadata only</span>']
    if synthetic:
        chips.append('<span class="tag tone synthetic">Synthetic capture</span>')
    if capture["truncated"]:
        chips.append('<span class="tag tone warn">The capture ends mid-packet</span>')
    if not capture["with_process"]:
        chips.append('<span class="tag tone warn">No process names in this capture</span>')
    meta = [("clock", f"{seconds:.0f} s captured"), ("calendar", started),
            ("phone", f"{capture['packets']:,} packets")]
    parts = [
        "<!doctype html>", '<html lang="en"><head><meta charset="utf-8">',
        f'<meta http-equiv="Content-Security-Policy" content="{CSP}">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="color-scheme" content="light dark">',
        f"<title>{_e(title)}</title><style>{STYLE}</style></head><body><main>",
        brand("network x-ray"),
        '<header class="head"><div class="chips">', "".join(chips), "</div>",
        f"<h1>{_e(heading)}</h1>",
        '<p class="lede">Hostnames, sizes and times, read from the phone’s traffic. Nothing was decrypted '
        "and no payload was kept.</p>",
        '<ul class="meta">' + "".join(f"<li>{icon(name)}{_e(text)}</li>" for name, text in meta) + "</ul></header>",
    ]
    for index, app in enumerate(apps):
        if app["domains"] or app["unnamed"]["connections"]:
            parts.append(_app_section(app, seconds, index, lede=focus is None))
    quiet = [app["name"] for app in apps if not app["domains"] and not app["unnamed"]["connections"]]
    local = result["local"]
    notes = []
    if local["packets"]:
        notes.append(f"{local['packets']:,} packets ({size(local['bytes'])}) stayed on the local network. "
                     "Local names and addresses aren’t shown.")
    if quiet:
        notes.append("Looked up names but opened no connection: " + ", ".join(quiet) + ".")
    if notes:
        parts.append('<section><h2>Also in the capture</h2>' + "".join(f'<p class="privacy">{_e(n)}</p>'
                                                                       for n in notes) + "</section>")
    parts.append(f'<footer class="foot"><span><a href="{links.MADE_WITH_XRAY}">Mobster</a> Network X-ray</span>'
                 '<span>Hostnames come from DNS '
                 'answers, TLS and QUIC server names and HTTP Host headers</span>'
                 '<span>Companies and categories come from Mobster’s own list</span></footer>')
    parts.append("</main></body></html>")
    return "".join(parts)


def _md(text):
    """Text from the capture, safe inside a Markdown table cell or line."""
    text = str(text)
    for char in "\\`*_[]<>|#":
        text = text.replace(char, "\\" + char)
    return text.replace("\n", " ")


def render_markdown(result, *, synthetic=False):
    capture = result["capture"]
    seconds = capture["seconds"]
    lines = ["# Network X-ray", ""]
    if synthetic:
        lines += ["Synthetic capture.", ""]
    lines += [f"{seconds:.0f} s captured, {capture['packets']:,} packets. Hostnames, sizes and times only: nothing was "
              "decrypted and no payload was kept.", ""]
    for app in result["apps"]:
        if not app["domains"] and not app["unnamed"]["connections"]:
            continue
        lines += [f"## {_md(app['name'])}", "", _md(headline(app, seconds)), ""]
        if app["domains"]:
            lines += ["| Host | Company | Category | Sent | Received | Connections |", "|---|---|---|---:|---:|---:|"]
        for domain in app["domains"]:
            category = CATEGORY_LABELS.get(domain["category"], "") if domain["category"] else "Unknown"
            lines.append(f"| `{domain['host']}` | {_md(domain['company'])} | {category} | {size(domain['up'])} | "
                         f"{size(domain['down'])} | {domain['connections']} |")
        if app["unnamed"]["connections"]:
            lines += ["", f"{app['unnamed']['connections']} {'more ' * bool(app['domains'])}connections with no "
                          f"hostname in the capture ({size(app['unnamed']['up'] + app['unnamed']['down'])})."]
        if app.get("note"):
            lines += ["", _md(app["note"])]
        if app["kind"] in ("ios", "process") and app["processes"]:
            lines += ["", _md(("Parts of iOS: " if app["kind"] == "ios" else "Process: ")
                              + ", ".join(app["processes"]) + ".")]
        lines.append("")
    return "\n".join(lines)


def write(result, out_dir, *, synthetic=False):
    """Write netxray.json, report.html and report.md into ``out_dir``; their paths."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    out.chmod(0o700)
    paths = {"json": out / "netxray.json", "html": out / "report.html", "markdown": out / "report.md"}
    paths["json"].write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    paths["html"].write_text(render_html(result, synthetic=synthetic), encoding="utf-8")
    paths["markdown"].write_text(render_markdown(result, synthetic=synthetic), encoding="utf-8")
    return paths
