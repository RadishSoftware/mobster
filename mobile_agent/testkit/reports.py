"""A suite's reports, in its folder `.mobster/test-results/<suite-id>/`:

- ``results.json`` (``schema: "mobster.test/1"``): every check's result per device, with its attempts;
- ``junit.xml``: one ``<testsuite>`` per device, one ``<testcase>`` per check, for CI;
- ``index.html``: the verdict, what didn't pass and why, then every check with its verdict on each device; a check
  that didn't pass opens to its numbered expectations beside the verdict frame. Then the flake table. It uses
  verify/report.py's design system (mobster.dev's tokens) and filters by status with radio buttons, not scripts;
- ``runs/<run-id>.html``: a copy of each verify run's own report, which is self-contained, so the folder can be
  uploaded as a CI artifact on its own;
- ``videos/``: screen recordings, with ``--video``.

Paths in results.json and the HTML are relative to the suite folder. The HTML makes no requests (inline CSS, frames
as data: URIs) and escapes every string from an app or a model.
"""

import base64
import html
from io import BytesIO
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import time
from xml.etree import ElementTree as ET

SCHEMA = "mobster.test/1"
CSP = "default-src 'none'; img-src data:; style-src 'unsafe-inline'"
THUMB_WIDTH = 120
WORDS = {"passed": "Passed", "failed": "Failed", "needs_review": "Needs review", "couldnt_run": "Couldn't run"}
TONES = {"passed": "pass", "failed": "fail", "needs_review": "review", "couldnt_run": "none"}
BANNERS = {0: ("Passed", "pass"), 1: ("Failed", "fail"), 2: ("Needs review", "review"), 3: ("Couldn't run", "none"),
           130: ("Stopped", "none")}
# Characters XML 1.0 can't hold, even escaped.
NOT_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")


def new_suite_id():
    import secrets
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def make_suite_dir(results_dir, suite_id=None):
    """(suite_id, folder): a new private folder under ``results_dir`` (`.mobster/test-results`)."""
    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    while True:
        suite_id = suite_id or new_suite_id()
        folder = results_dir / suite_id
        try:
            folder.mkdir(mode=0o700)
        except FileExistsError:
            suite_id = None
            continue
        os.chmod(folder, 0o700)
        return suite_id, folder


# -- the result document -----------------------------------------------------------------------------------------

def document(results, *, suite_id, options, version, command=None):
    """results.json's content: Suite.results() with the suite's id, options and schema."""
    return {"schema": SCHEMA, "suite": suite_id, "startedAt": results.get("startedAt"),
            "finishedAt": results.get("finishedAt"), "durationMs": results.get("durationMs"),
            "exitCode": results.get("exitCode"), "options": dict(options or {}), "command": command,
            "devices": results.get("devices") or [], "checks": results.get("checks") or [],
            "summary": results.get("summary") or {}, "mobster": {"version": version}}


def collect(doc, folder):
    """Copy each run's report.html into ``folder``/runs and point the document's paths at the copies, relative to
    ``folder``; videos already in ``folder`` become relative too. Changes ``doc`` in place and returns it."""
    folder = Path(folder)
    copies = {}
    for check in doc.get("checks") or ():
        for result in check.get("results") or ():
            for run in result.get("runs") or ():
                run["report"] = _copy_report(run.get("report"), run.get("runId"), folder, copies)
                run["video"] = _inside(run.get("video"), folder)
            result["report"] = _copy_report(result.get("report"), (result.get("runs") or [{}])[-1].get("runId"),
                                            folder, copies)
            result["video"] = _inside(result.get("video"), folder)
    return doc


def _copy_report(path, run_id, folder, copies):
    if not path or not run_id:
        return None
    if run_id in copies:
        return copies[run_id]
    source = Path(path)
    if not source.is_file():
        copies[run_id] = None
        return None
    target = folder / "runs" / f"{run_id}.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    copies[run_id] = target.relative_to(folder).as_posix()
    return copies[run_id]


def _inside(path, folder):
    if not path:
        return None
    try:
        return Path(path).resolve().relative_to(Path(folder).resolve()).as_posix()
    except ValueError:
        return str(path)


def write_json(doc, path):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(doc, indent=1, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path


# -- JUnit ---------------------------------------------------------------------------------------------------------

def _xml(text):
    return NOT_XML.sub("", str(text if text is not None else ""))


def _seconds(ms):
    return f"{(ms or 0) / 1000:.3f}"


def _iso(ms):
    if not ms:
        return time.strftime("%Y-%m-%dT%H:%M:%S")
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ms / 1000))


def junit(doc, *, report_base=None, strict=False):
    """The JUnit XML for ``doc`` (bytes). ``report_base``: the folder report paths are written relative to (the
    suite folder's own paths are relative to it already); None keeps them as they are."""
    devices = [device["key"] for device in doc.get("devices") or ()]
    names = {device["key"]: device.get("name") or device["key"] for device in doc.get("devices") or ()}
    by_device = {key: [] for key in devices}
    for check in doc.get("checks") or ():
        for result in check.get("results") or ():
            by_device.setdefault(result.get("deviceKey"), []).append((check, result))
            names.setdefault(result.get("deviceKey"), result.get("device"))
    root = ET.Element("testsuites", name="mobster test")
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0, "time": 0}
    for key, cases in by_device.items():
        if not cases:
            continue
        suite = ET.SubElement(root, "testsuite", name=_xml(names.get(key) or key))
        counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0, "time": 0}
        properties = ET.SubElement(suite, "properties")
        ET.SubElement(properties, "property", name="mobster.suite", value=_xml(doc.get("suite")))
        ET.SubElement(properties, "property", name="mobster.version",
                      value=_xml((doc.get("mobster") or {}).get("version")))
        for check, result in cases:
            counts["tests"] += 1
            counts["time"] += result.get("durationMs") or 0
            case = ET.SubElement(suite, "testcase", classname=_xml(check.get("classname") or "checks"),
                                 name=_xml(check.get("name")), time=_seconds(result.get("durationMs")),
                                 file=_xml(check.get("file")))
            props = ET.SubElement(case, "properties")
            report = _rebase(result.get("report"), report_base)
            for name, value in (("status", result.get("status")), ("attempts", result.get("attempts")),
                                ("flaky", "true" if result.get("flaky") else "false"),
                                ("passRate", result.get("passRate")), ("report", report),
                                ("video", _rebase(result.get("video"), report_base))):
                if value is not None:
                    ET.SubElement(props, "property", name=name, value=_xml(value))
            status = result.get("status")
            reason = result.get("reason") or {}
            if check.get("quarantined"):
                counts["skipped"] += 1
                why = check.get("quarantineReason") or "listed in the quarantine file"
                ET.SubElement(case, "skipped", message=_xml(f"Quarantined ({WORDS.get(status, status)}): {why}"))
            elif status != "passed":
                counts["failures"] += 1
                kind = {"failed": "assertion", "couldnt_run": "couldnt_run"}.get(status, status)
                failure = ET.SubElement(case, "failure", message=_xml(reason.get("message") or WORDS.get(status)),
                                        type=_xml(kind))
                failure.text = _xml("\n".join(filter(None, [
                    f"{WORDS.get(status, status)} ({reason.get('class')})" if reason.get("class") else
                    WORDS.get(status, status), reason.get("message"), reason.get("fix")])))
            elif strict and result.get("flaky"):
                counts["failures"] += 1
                flaky = result.get("flakyReason") or {}
                failure = ET.SubElement(case, "failure", type="flaky",
                                        message=_xml(f"Flaky: passed after {result.get('attempts')} attempts"))
                failure.text = _xml(flaky.get("message") or "")
            repeated = ((doc.get("options") or {}).get("repeat") or 1) > 1
            out = ET.SubElement(case, "system-out")
            out.text = _xml("\n".join(filter(None, [f"Report: {report}" if report else None] + [
                (f"Run {run.get('repetition')}, attempt {run.get('attempt')}" if repeated
                 else f"Attempt {run.get('attempt')}") + f": {WORDS.get(run.get('verdict'), run.get('verdict'))}"
                + (f" ({run.get('message')})" if run.get("verdict") != "passed" and run.get("message") else "")
                for run in result.get("runs") or ()] + list(result.get("notes") or ()))))
        for name in ("tests", "failures", "errors", "skipped"):
            suite.set(name, str(counts[name]))
            totals[name] += counts[name]
        suite.set("time", _seconds(counts["time"]))
        suite.set("timestamp", _iso(doc.get("startedAt")))
        totals["time"] += counts["time"]
    for name in ("tests", "failures", "errors", "skipped"):
        root.set(name, str(totals[name]))
    root.set("time", _seconds(doc.get("durationMs") if doc.get("durationMs") is not None else totals["time"]))
    ET.indent(root)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8") + b"\n"


def _rebase(path, base):
    if not path or base is None:
        return path
    return str(Path(base) / path)


# -- HTML ----------------------------------------------------------------------------------------------------------

def _e(value):
    return html.escape(NOT_XML.sub("", str(value if value is not None else "")), quote=True)


def _thumb(path, width=THUMB_WIDTH, quality=70):
    if not path:
        return None
    try:
        from PIL import Image
        with Image.open(path) as image:
            image = image.convert("RGB")
            if image.width > width:
                image = image.resize((width, max(1, round(image.height * width / image.width))), Image.LANCZOS)
            out = BytesIO()
            image.save(out, "JPEG", quality=quality)
    except Exception:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()


SUITE_STYLE = """
.filters { display: flex; flex-wrap: wrap; gap: 2px; padding: 3px; border-radius: 999px; background: var(--sunken);
  box-shadow: inset 0 0 0 1px var(--line); margin-left: auto; }
.filters label { display: inline-flex; align-items: center; gap: 6px; padding: 4px 12px; border-radius: 999px;
  font-size: 12.5px; font-weight: 500; color: var(--ink-2); cursor: pointer; user-select: none;
  -webkit-user-select: none; }
.filters label span { color: var(--ink-3); font-variant-numeric: tabular-nums; }
ul.failures { list-style: none; margin: 14px 0 0; padding: 0; display: grid; gap: 6px; max-width: 860px; }
ul.failures li { display: grid; grid-template-columns: 16px minmax(0, 1fr); gap: 8px; align-items: baseline;
  font-size: 14px; }
ul.failures li .icon { color: var(--tone); stroke-width: 2; position: relative; top: 3px; }
ul.failures a { font-weight: 550; color: var(--ink); }
ul.failures .why { display: block; margin-top: 1px; font-size: 13px; color: var(--ink-2); }
ul.failures .why code.assert { font-size: 12.5px; }
ul.failures li.more { display: block; padding-left: 24px; font-size: 13px; color: var(--ink-3); }
.found { color: var(--fail); }
.checks-head { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 16px; margin-bottom: 12px; }
.checks-head h2 { margin: 0; }
FILTER_RULES
.list-head, .check > summary { display: grid; gap: 4px 20px; align-items: start;
  grid-template-columns: minmax(0, 1fr) repeat(var(--cols, 1), minmax(132px, 196px)) 16px; }
.list-head { padding: 10px 20px; font-size: 12px; color: var(--ink-3); border-bottom: 1px solid var(--line); }
.list-head span { overflow-wrap: anywhere; }
.check { border-top: 1px solid var(--line); }
.list-head + .check { border-top: 0; }
.check > summary { list-style: none; cursor: pointer; padding: 16px 20px; border-radius: 14px; }
.check > summary::-webkit-details-marker { display: none; }
.check > summary:hover { background: color-mix(in srgb, var(--ink) 2.5%, transparent); }
.check[open] > summary { border-radius: 0; }
.check:last-child > summary { border-radius: 0 0 14px 14px; }
.list-head + .check:last-child > summary { border-radius: 0 0 14px 14px; }
.check:last-child[open] > summary { border-radius: 0; }
.what { min-width: 0; display: grid; gap: 4px; }
.what .name { font-size: 14.5px; font-weight: 550; letter-spacing: -.006em; overflow-wrap: anywhere; }
.what .sub { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
code.file { font: 12px/1.4 var(--mono); color: var(--ink-3); overflow-wrap: anywhere; margin-right: 4px; }
.what .tag { height: 20px; font-size: 11.5px; padding: 0 7px; }
.res { display: grid; gap: 1px; min-width: 0; }
.res .v { display: inline-flex; align-items: center; gap: 6px; font-weight: 600; color: var(--tone); }
.res .v .icon { width: 15px; height: 15px; stroke-width: 2; }
.res .v .tag { height: 18px; font-size: 11px; padding: 0 6px; margin-left: 2px; }
.res .small { font-size: 12px; color: var(--ink-3); overflow-wrap: break-word; padding-left: 21px; }
.res.muted .v { color: var(--ink-2); }
.res.muted .v .icon { color: var(--tone); }
.res .dev { display: none; font-size: 12px; color: var(--ink-3); overflow-wrap: anywhere; }
.res.missing { color: var(--ink-3); font-size: 13px; }
.chev { width: 16px; height: 16px; margin-top: 2px; color: var(--ink-3);
  transition: transform .18s cubic-bezier(.23, 1, .32, 1); }
@media (prefers-reduced-motion: reduce) { .chev { transition: none; } }
.check[open] .chev { transform: rotate(180deg); }
.body { padding: 4px 20px 22px; display: grid; gap: 22px; }
.result { display: grid; grid-template-columns: 280px minmax(0, 1fr); gap: 14px 32px; align-items: start; }
.result.noframe { grid-template-columns: minmax(0, 1fr); }
.result .result-head { grid-column: 1 / -1; }
.result .shot { grid-column: 1; grid-row: 2; width: 280px; }
.result .result-text { grid-column: 2; grid-row: 2; }
.result.noframe .result-text { grid-column: 1; }
.result .phone { border-radius: 46px; }
.result .phone img { border-radius: 40px; }
.result-text { min-width: 0; display: grid; gap: 12px; align-content: start; }
.result-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 14px; font-size: 13px;
  color: var(--ink-2); }
.result-head strong { color: var(--ink); font-weight: 600; }
.links { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 13px; font-weight: 500; }
.links a { color: var(--ink); }
.result-text .card { box-shadow: 0 0 0 1px var(--line); }
.callout { padding: 12px 16px; border-radius: 12px; font-size: 13.5px;
  background: color-mix(in srgb, var(--tone) var(--wash), var(--surface));
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--tone) 22%, transparent); overflow-wrap: anywhere; }
.callout p { margin: 0; } .callout p + p { margin-top: 4px; color: var(--ink-2); }
.aside { margin: 0; font-size: 13px; color: var(--ink-2); overflow-wrap: anywhere; }
.result-text pre.cmd { padding: 10px 14px; font-size: 12.5px; }
.aside strong { color: var(--ink); font-weight: 600; }
.table-wrap { overflow-x: auto; }
table.flaky { border-collapse: collapse; width: 100%; min-width: 560px; font-size: 13.5px; }
table.flaky th, table.flaky td { text-align: left; padding: 11px 20px; border-top: 1px solid var(--line);
  vertical-align: top; overflow-wrap: anywhere; }
table.flaky thead th { border-top: 0; font-size: 12px; font-weight: 500; color: var(--ink-3); white-space: nowrap; }
table.flaky th[scope=row] { font-weight: 550; }
table.flaky td.num, table.flaky td.dev { white-space: nowrap; }
table.flaky td.num { font-variant-numeric: tabular-nums; }
.outcomes { display: inline-flex; gap: 3px; }
.outcomes .icon { width: 15px; height: 15px; stroke-width: 2.2; color: var(--tone); }
@media (max-width: 760px) {
  .result { grid-template-columns: minmax(0, 1fr); }
  .result .shot, .result .result-text { grid-column: 1; grid-row: auto; }
  .result .shot { margin: 8px auto 0; max-width: 100%; }
}
@media (max-width: 640px) {
  .checks-head .filters { margin-left: 0; }
  .list-head { display: none; }
  .check > summary { grid-template-columns: minmax(0, 1fr) 16px; padding: 14px 16px; gap: 10px 12px; }
  .check > summary .what { grid-column: 1; }
  .check > summary .chev { grid-column: 2; grid-row: 1; align-self: start; margin-top: 2px; }
  .check > summary .res { grid-column: 1; display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 10px; }
  .multi .check > summary .res .dev { flex-basis: 100%; }
  .multi .res .dev { display: block; }
  .res .small { padding-left: 0; }
  .body { padding: 2px 16px 20px; }
  .result-text .card { box-shadow: none; border-radius: 0; border-top: 1px solid var(--line);
    border-bottom: 1px solid var(--line); margin: 0 -16px; }
  .result-text .card ol.expect li { border-radius: 0; box-shadow: none; }
  .result-text .card ol.expect li.failed { box-shadow: inset 2px 0 0 var(--fail-solid); }
  table.flaky th, table.flaky td { padding: 10px 14px; }
}
@media print {
  .filters, .chev { display: none; }
  .check > summary { cursor: default; }
  details::details-content { content-visibility: visible; display: contents; }
  .check { break-inside: avoid; }
  .result .shot { width: 200px; }
  .result { grid-template-columns: 200px minmax(0, 1fr); }
}
"""
FILTERS = (("all", "All"), ("unpassed", "Didn't pass"), ("flaky", "Flaky"), ("passed", "Passed"))
OPEN_FRAME, CLOSED_FRAME = 560, 420


def home(text):
    from ..verify.report import home
    return home(text)


def _style(filters):
    from ..verify.report import BASE_STYLE
    rules = []
    for key, _ in filters:
        label = f"#show-{key}:checked ~ .checks-head label[for=show-{key}]"
        rules.append(f"{label} {{ background: var(--seg-on); color: var(--ink); box-shadow: 0 0 0 1px var(--line), "
                     "0 1px 2px rgb(24 23 28 / .08); }")
        rules.append(f"#show-{key}:focus-visible ~ .checks-head label[for=show-{key}] {{ outline: 2px solid "
                     "var(--violet); outline-offset: 1px; }")
        if key != "all":
            rules.append(f"#show-{key}:checked ~ .list .check:not(.is-{key}) {{ display: none; }}")
    return BASE_STYLE + SUITE_STYLE.replace("FILTER_RULES", "\n".join(rules))


def _kinds(check):
    results = check.get("results") or []
    kinds = {"all"}
    if any(result.get("status") != "passed" for result in results) or not results:
        kinds.add("unpassed")
    if any(result.get("flaky") for result in results):
        kinds.add("flaky")
    if results and all(result.get("status") == "passed" for result in results):
        kinds.add("passed")
    return kinds


def render_html(doc, folder):
    """index.html for ``doc``; ``folder`` holds the copies its relative paths name."""
    from ..verify.report import brand, command, dots, icon, verdict_chip, when
    folder = Path(folder)
    summary = doc.get("summary") or {}
    devices = doc.get("devices") or []
    checks = doc.get("checks") or []
    word, tone = BANNERS.get(doc.get("exitCode"), BANNERS[3])
    counted = summary.get("total", 0) - summary.get("quarantined", 0)
    kinds = [_kinds(check) for check in checks]
    filters = [(key, name) for key, name in FILTERS if key == "all" or any(key in k for k in kinds)]
    filters = filters if len(filters) > 2 else filters[:1]
    parts = ["<!doctype html>", '<html lang="en"><head><meta charset="utf-8">',
             f'<meta http-equiv="Content-Security-Policy" content="{CSP}">',
             '<meta name="viewport" content="width=device-width, initial-scale=1">',
             '<meta name="color-scheme" content="light dark">',
             f"<title>Mobster test: {summary.get('passed', 0)} of {counted} passed</title>",
             f"<style>{_style(filters)}</style></head><body><main>",
             brand("test", doc.get("suite")),
             '<header class="head"><div class="chips">', verdict_chip(word, tone)]
    options = doc.get("options") or {}
    if (options.get("repeat") or 1) > 1:
        parts.append(f'<span class="tag">Each check {_e(options["repeat"])} times</span>')
    if options.get("strict"):
        parts.append('<span class="tag">Strict: flaky fails</span>')
    if counted:
        heading = f"{summary.get('passed', 0)} of {counted} passed"
    else:
        heading = "Every check is quarantined"
    parts.append(f'</div><h1>{_e(heading)}<span class="quiet"> · {_e(_duration(doc.get("durationMs")))}</span></h1>')
    aside = _aside(summary)
    if aside:
        parts.append(f'<p class="lede">{_e(aside)}</p>')
    parts.append(_failures(checks, devices))
    meta = [("sim" if device.get("kind") in (None, "simulator") else "phone", dots(device.get("name")))
            for device in devices]
    meta.append(("calendar", when(ms=doc.get("startedAt"))))
    if summary.get("costUsd"):
        meta.append(("coin", f"${summary['costUsd']:.2f} in model costs"))
    parts.append('<ul class="meta">' + "".join(f"<li>{icon(name)}{_e(value)}</li>" for name, value in meta if value)
                 + "</ul>")
    if doc.get("command"):
        parts.append(command(home(doc["command"])))
    parts.append("</header>")

    parts.append('<section aria-labelledby="checks">')
    if len(filters) > 1:
        for key, name in filters:
            count = sum(key in k for k in kinds)
            parts.append(f'<input class="sr" type="radio" name="show" id="show-{key}"'
                         + (" checked" if key == "all" else "") + f' aria-label="Show {_e(name.lower())}: {count}">')
    parts.append(f'<div class="checks-head"><h2 id="checks">Checks<span class="count">{len(checks)}</span></h2>')
    if len(filters) > 1:
        parts.append('<div class="filters" aria-hidden="true">' + "".join(
            f'<label for="show-{key}">{_e(name)} <span>{sum(key in k for k in kinds)}</span></label>'
            for key, name in filters) + "</div>")
    parts.append("</div>")
    multi = " multi" if len(devices) > 1 else ""
    parts.append(f'<div class="card list{multi}" style="--cols: {max(1, len(devices))}">')
    if devices:
        parts.append('<div class="list-head" aria-hidden="true"><span>Check</span>'
                     + "".join(f'<span class="col">{_e(device.get("name"))}</span>' for device in devices)
                     + "<span></span></div>")
    for number, (check, kind) in enumerate(zip(checks, kinds), 1):
        parts.append(_row(number, check, kind, devices, folder))
    parts.append("</div></section>")

    flaky = [(check, result) for check in checks for result in check.get("results") or () if result.get("flaky")]
    if flaky:
        parts.append(f'<section aria-labelledby="flaky"><h2 id="flaky">Flaky<span class="count">{len(flaky)}</span>'
                     '</h2><div class="card table-wrap"><table class="flaky"><thead><tr>'
                     '<th scope="col">Check</th><th scope="col">Device</th><th scope="col">Attempts</th>'
                     '<th scope="col">Pass rate</th><th scope="col">What went wrong</th></tr></thead><tbody>')
        for check, result in flaky:
            runs = result.get("runs") or []
            outcomes = "".join(f'<span class="{TONES.get(run.get("verdict"), "none")}">'
                               f'{icon(STATUS_MARKS.get(run.get("verdict"), "dash"))}</span>' for run in runs)
            rate = result.get("passRate")
            if rate is None and runs:
                rate = sum(run.get("verdict") == "passed" for run in runs) / len(runs)
            why = _why(result.get("flakyReason") or {})
            parts.append(f'<tr><th scope="row">{_e(check.get("name"))}</th>'
                         f'<td class="dev">{_e(result.get("device"))}</td>'
                         f'<td><span class="outcomes" role="img" aria-label="{_e(_outcome_words(runs))}">{outcomes}'
                         f'</span></td><td class="num">{_e(f"{round((rate or 0) * 100)}%")}</td><td>{why}</td>'
                         "</tr>")
        parts.append("</tbody></table></div></section>")
    parts.append(f'<footer class="foot"><span>Mobster {_e((doc.get("mobster") or {}).get("version"))}</span>'
                 f'<span>Suite <span class="mono">{_e(doc.get("suite"))}</span></span>'
                 f"<span>{_e(_counts(summary))}</span></footer>")
    parts.append("</main></body></html>")
    return "\n".join(parts)


STATUS_MARKS = {"passed": "check", "failed": "cross", "needs_review": "review", "couldnt_run": "dash"}


def _row(number, check, kind, devices, folder):
    """One check: a summary row (its name, file, tags and each device's verdict) that opens to each device's
    frame and expectations. A check that didn't pass is open."""
    from ..verify.report import dots, icon
    results = {result.get("deviceKey"): result for result in check.get("results") or ()}
    classes = " ".join(f"is-{key}" for key in sorted(kind))
    is_open = "unpassed" in kind
    tags = [f'<span class="tag">{_e(tag)}</span>' for tag in check.get("tags") or ()]
    if check.get("quarantined"):
        tags.insert(0, '<span class="tag tone review">Quarantined</span>')
    head = [f'<details class="check {classes}" id="check-{number}"' + (" open" if is_open else "") + "><summary>",
            f'<div class="what"><span class="name">{_e(check.get("name"))}</span><span class="sub">'
            f'<code class="file">{_e(check.get("file"))}</code>{"".join(tags)}</span></div>']
    body = []
    for device in devices:
        result = results.get(device.get("key"))
        name = device.get("name")
        if result is None:
            head.append(f'<div class="res missing"><span class="dev">{_e(name)}</span>Not run here</div>')
            continue
        status = result.get("status")
        tone = TONES.get(status, "none") + (" muted" if check.get("quarantined") else "")
        small = []
        if result.get("passRate") is not None:
            small.append(f"{round(result['passRate'] * 100)}% passed")
        if (result.get("attempts") or 0) > 1:
            small.append(f"{result['attempts']} attempts")
        if result.get("durationMs"):
            small.append(_duration(result.get("durationMs")))
        if check.get("quarantined"):
            small.append("quarantined")
        head.append(f'<div class="res {tone}"><span class="sr">{_e(name)}: </span>'
                    f'<span class="dev" aria-hidden="true">{_e(dots(name))}</span>'
                    f'<span class="v">{icon(STATUS_MARKS.get(status, "dash"))}{_e(WORDS.get(status, status))}'
                    + ('<span class="tag tone review">Flaky</span>' if result.get("flaky") else "") + "</span>"
                    + (f'<span class="small">{_e(" · ".join(small))}</span>' if small else "") + "</div>")
        body.append(_result(check, result, name, is_open, device))
    head.append(icon("chevron", "chev") + "</summary>")
    if check.get("quarantined"):
        why = check.get("quarantineReason")
        body.insert(0, '<p class="aside"><strong>Quarantined</strong>' + (f": {_e(why)}." if why else ".")
                    + " Its results don't fail the suite.</p>")
    if check.get("error"):
        body.insert(0, f'<div class="callout none"><p>{_e(check["error"])}</p></div>')
    return "".join(head) + '<div class="body">' + "".join(body) + "</div></details>"


def _again(check, device):
    """The command that runs one check again on the simulator it ran on; None for an iPhone, whose name the suite
    doesn't keep."""
    if not check.get("file") or device.get("kind") not in (None, "simulator"):
        return None
    words = ["mobster", "test", shlex.quote(check["file"])]
    if device.get("type"):
        words += ["--sim", shlex.quote(device["type"] + (f"@{device['runtime']}" if device.get("runtime") else ""))]
    return " ".join(words)


def _result(check, result, device_name, is_open, device=None):
    """One device's result, opened: the frame its verdict came from beside the numbered expectations."""
    from ..verify.report import OVERLAY_LEGEND, command, dots, expectations
    status = result.get("status")
    tone = TONES.get(status, "none")
    word = WORDS.get(status, status)
    frame = _thumb(result.get("frame"), OPEN_FRAME if is_open else CLOSED_FRAME, 78 if is_open else 68)
    attempts = result.get("attempts") or 0
    line = word + (f" after {attempts} attempts" if attempts > 1 else "")
    if result.get("durationMs"):
        line += f" · {_duration(result.get('durationMs'))}"
    links = []
    if result.get("report"):
        links.append(f'<a href="{_e(result["report"])}">Full report</a>')
    runs = [run for run in result.get("runs") or () if run.get("report") and run.get("report") != result.get("report")]
    for run in runs[-4:]:
        links.append(f'<a href="{_e(run["report"])}">Attempt {_e(run.get("attempt"))}</a>')
    if result.get("video"):
        links.append(f'<a href="{_e(result["video"])}">Video</a>')
    head = (f'<div class="result-head"><strong>{_e(dots(device_name))}</strong><span>{_e(line)}</span>'
            + (f'<span class="links">{"".join(links)}</span>' if links else "") + "</div>")
    text = []
    assertions = result.get("assertions") or []
    reason = result.get("reason") or {}
    failing = [item for item in assertions if not item.get("ok")]
    if status != "passed" and (reason.get("message") or reason.get("fix")) and (
            not failing or reason.get("class") not in (None, "assertion")):
        text.append(f'<div class="callout {tone}">' + "".join(
            f"<p>{_e(value)}</p>" for value in (reason.get("message"), reason.get("fix")) if value) + "</div>")
    if assertions:
        text.append('<div class="card">' + expectations(assertions, frame=bool(frame), quiet_held=status != "passed")
                    + "</div>")
    flaky = result.get("flakyReason") or {}
    if result.get("flaky") and (flaky.get("message") or flaky.get("assertions")):
        text.append('<p class="aside"><strong>Flaky.</strong> An attempt that didn\'t pass: ' + _why(flaky) + "</p>")
    for note in result.get("notes") or ():
        text.append(f'<p class="aside">{_e(note)}</p>')
    again = _again(check, device or {}) if status != "passed" else None
    if again:
        text.append(command(again))
    image = (f'<div class="shot"><div class="phone"><img src="{frame}" alt="The screen at the verdict on '
             f'{_e(device_name)}, each asserted element outlined and numbered as in the list"></div>{OVERLAY_LEGEND}'
             "</div>" if frame else "")
    return (f'<div class="result{"" if frame else " noframe"}">{head}<div class="result-text">{"".join(text)}</div>'
            f"{image}</div>")


def _outcome_words(runs):
    return ", ".join(WORDS.get(run.get("verdict"), "?").lower() for run in runs)


def _aside(summary):
    """What the heading doesn't say: '1 flaky · 2 quarantined'. What didn't pass is listed under it."""
    return " · ".join(f"{summary[key]} {word}" for key, word in (("flaky", "flaky"), ("quarantined", "quarantined"))
                      if summary.get(key))


def _why(reason):
    """Why a result didn't pass, as HTML: its first failed assertion and what was found, else the message."""
    failed = [item for item in reason.get("assertions") or () if not item.get("ok")]
    if failed:
        first = failed[0]
        more = f" (and {len(failed) - 1} more)" if len(failed) > 1 else ""
        return (f'<code class="assert">{_e(first.get("text"))}</code> <span class="found">{_e(first.get("observed"))}'
                f"</span>{_e(more)}")
    return _e(reason.get("message") or "")


def _failures(checks, devices, limit=3):
    """Under the heading: the results that didn't pass, each linked to its row, with why."""
    from ..verify.report import dots, icon
    rows = []
    for number, check in enumerate(checks, 1):
        if check.get("quarantined"):
            continue
        for result in check.get("results") or ():
            status = result.get("status")
            if status == "passed":
                continue
            reason = dict(result.get("reason") or {})
            if reason.get("class") in (None, "assertion"):
                reason["assertions"] = result.get("assertions")
            where = f" on {dots(result.get('device'))}" if len(devices) > 1 else ""
            rows.append(f'<li class="{TONES.get(status, "none")}">{icon(STATUS_MARKS.get(status, "dash"))}'
                        f'<span><a href="#check-{number}">{_e(check.get("name"))}</a>{_e(where)}'
                        f'<span class="why">{_why(reason)}</span></span></li>')
    if not rows:
        return ""
    more = len(rows) - limit
    return ('<ul class="failures">' + "".join(rows[:limit])
            + (f'<li class="more">and {more} more below</li>' if more > 0 else "") + "</ul>")


def _counts(summary):
    parts = [f"{summary.get('passed', 0)} passed"]
    if summary.get("flaky"):
        parts.append(f"{summary['flaky']} flaky")
    for key, word in (("failed", "failed"), ("needsReview", "need review"), ("couldntRun", "couldn't run"),
                      ("quarantined", "quarantined")):
        if summary.get(key):
            parts.append(f"{summary[key]} {'needs review' if key == 'needsReview' and summary[key] == 1 else word}")
    return ", ".join(parts)


def _duration(ms):
    seconds = round((ms or 0) / 1000)
    if seconds < 60:
        return f"{seconds} s"
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes} min {seconds:02d} s"


def write_html(doc, folder, target=None):
    """index.html in ``folder``; with ``target`` (--html DIR), the page, runs/ and videos/ copied there too."""
    folder = Path(folder)
    page = render_html(doc, folder)
    (folder / "index.html").write_text(page, encoding="utf-8")
    if target is None:
        return folder / "index.html"
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    for sub in ("runs", "videos"):
        if (folder / sub).is_dir():
            shutil.copytree(folder / sub, target / sub, dirs_exist_ok=True)
    (target / "index.html").write_text(page, encoding="utf-8")
    return target / "index.html"


def write_all(doc, folder, *, junit_path=None, html_dir=None, strict=False, cwd=None):
    """results.json, junit.xml and index.html in ``folder`` (after ``collect``), plus --junit FILE and --html DIR.
    Returns {"results", "junit", "html", "junitCopy"?, "htmlCopy"?}."""
    folder = Path(folder)
    collect(doc, folder)
    paths = {"results": write_json(doc, folder / "results.json")}
    cwd = Path(cwd or Path.cwd())
    base = _relative_dir(folder, cwd)
    data = junit(doc, report_base=base, strict=strict)
    (folder / "junit.xml").write_bytes(data)
    paths["junit"] = folder / "junit.xml"
    if junit_path is not None:
        junit_path = Path(junit_path)
        junit_path.parent.mkdir(parents=True, exist_ok=True)
        junit_path.write_bytes(data)
        paths["junitCopy"] = junit_path
    paths["html"] = write_html(doc, folder)
    if html_dir is not None:
        paths["htmlCopy"] = write_html(doc, folder, html_dir)
    return paths


def _relative_dir(folder, cwd):
    try:
        return Path(folder).resolve().relative_to(Path(cwd).resolve()).as_posix()
    except ValueError:
        return str(folder)
