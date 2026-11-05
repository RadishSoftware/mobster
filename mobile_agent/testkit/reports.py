"""A suite's reports: results.json, junit.xml and index.html, which makes no requests and escapes every string."""

import base64
import hashlib
import html
import json
import os
import re
import shlex
import shutil
import time
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET

from .. import links
from ..paths import write_text_atomic
from ..verify import perf
from . import junit_verdict
from .junit_verdict import snapshot_lines
from .proof_report import cause_html, cause_lines, proof_steps, step_lines
from .suite import SAFETY_EXIT_STOPS
from .xctest import failure_line, milliseconds
from .xctest import words as xctest_words

SCHEMA = "mobster.test/1"
CSP = "default-src 'none'; img-src data:; font-src data:; style-src 'unsafe-inline'"
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
    supplied = suite_id is not None
    while True:
        suite_id = suite_id or new_suite_id()
        folder = results_dir / suite_id
        try:
            folder.mkdir(mode=0o700)
        except FileExistsError:
            if supplied:
                raise
            suite_id = None
        else:
            os.chmod(folder, 0o700)
            return suite_id, folder


def document(results, *, suite_id, options, version, command=None, testflight=(), git=None):
    """results.json's content: Suite.results() with the suite's id, options and schema; ``git`` is the branch and commit
    it tested (gitref.checkout), left out outside a git checkout."""
    doc = {"schema": SCHEMA, "suite": suite_id, "startedAt": results.get("startedAt"),
            "finishedAt": results.get("finishedAt"), "durationMs": results.get("durationMs"),
            "exitCode": results.get("exitCode"), "options": dict(options or {}), "command": command,
            "devices": results.get("devices") or [], "checks": results.get("checks") or [],
            "summary": results.get("summary") or {}, "mobster": {"version": version}}
    if results.get("stopReason") is not None:
        doc.update(stopReason=results["stopReason"], stopped=results["stopReason"] == "stopped")
    if testflight:
        doc["testflight"] = [dict(build) for build in testflight]
    if git:
        doc["git"] = {key: git.get(key) for key in ("branch", "head")}
    return doc


def collect(doc, folder):
    folder = Path(folder)
    copies = {}
    for check in doc.get("checks") or ():
        for result in check.get("results") or ():
            for run in result.get("runs") or ():
                run["report"] = _copy_report(run.get("report"), run.get("runId"), folder, copies)
                run["video"] = _inside(run.get("video"), folder)
                if run.get("verdict") != "passed":
                    _copy_evidence(run.get("runDir"), run.get("runId"), folder)
            chosen = result.get("runId") or (result.get("runs") or [{}])[-1].get("runId")
            result["report"] = _copy_report(result.get("report"), chosen, folder, copies)
            result["video"] = _inside(result.get("video"), folder)
            _point_evidence(result.get("evidence"), folder)
    return doc


EVIDENCE_PARTS = ("logs", "crashes")


def _copy_evidence(run_dir, run_id, folder):
    """The run's logs/ and crashes/ into ``folder``/runs/<run-id>/, beside its report's copy, which links there."""
    if not run_dir or not run_id:
        return
    for part in EVIDENCE_PARTS:
        source = Path(run_dir) / part
        if source.is_dir():
            shutil.copytree(source, folder / "runs" / str(run_id) / part, dirs_exist_ok=True)


def _point_evidence(evidence, folder):
    """The cell's evidence paths, relative to the suite folder (its copies); the run folder stays out of it."""
    if not isinstance(evidence, dict):
        return
    run_id = evidence.pop("runId", None)
    evidence.pop("runDir", None)
    if not run_id:
        return
    base = f"runs/{run_id}/"
    log, crash = evidence.get("log"), evidence.get("crash")
    if isinstance(log, dict) and log.get("path"):
        log["path"] = base + log["path"] if (folder / base / log["path"]).is_file() else None
    if isinstance(crash, dict) and crash.get("report"):
        crash["report"] = base + crash["report"] if (folder / base / crash["report"]).is_file() else None


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
    page = source.read_text(encoding="utf-8")
    target.write_text(page.replace('class="ev-file" href="', f'class="ev-file" href="{run_id}/'), encoding="utf-8")
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
    return write_text_atomic(path, json.dumps(doc, indent=1, ensure_ascii=False, allow_nan=False) + "\n")


def _xml(text):
    return NOT_XML.sub("", str(text if text is not None else ""))


def _seconds(ms):
    return f"{(ms or 0) / 1000:.3f}"


def _iso(ms):
    if not ms:
        return time.strftime("%Y-%m-%dT%H:%M:%S")
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ms / 1000))


def junit(doc, *, report_base=None, strict=False):
    """The JUnit XML for ``doc`` (bytes)."""
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
        for name in ("branch", "head"):
            if (doc.get("git") or {}).get(name):
                ET.SubElement(properties, "property", name=f"git.{name}", value=_xml(doc["git"][name]))
        for build in doc.get("testflight") or ():
            ET.SubElement(properties, "property", name=f"testflight.{_xml(build.get('bundleId'))}",
                          value=_xml(f"{build.get('version')} ({build.get('build')})"))
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
                                ("passRate", result.get("passRate")),
                                *((f"perf.{key}", value) for key, value in perf.numbers(result.get("perf")).items()),
                                ("report", report),
                                ("linkFirstSteps", (result.get("linkFirst") or {}).get("taken")),
                                ("modelCallsSaved", (result.get("linkFirst") or {}).get("modelCallsSaved")),
                                ("video", _rebase(result.get("video"), report_base))):
                if value is not None:
                    ET.SubElement(props, "property", name=name, value=_xml(value))
            status = result.get("status")
            reason = result.get("reason") or {}
            safety_stop = result.get("safetyStop")
            if safety_stop in SAFETY_EXIT_STOPS:
                counts["errors"] += 1
                message = f"Suite stopped: {safety_stop}."
                ET.SubElement(case, "error", type="safety_stop", message=message).text = _xml(
                    reason.get("message") or message)
            elif check.get("quarantined"):
                counts["skipped"] += 1
                why = check.get("quarantineReason") or "listed in the quarantine file"
                ET.SubElement(case, "skipped", message=_xml(f"Quarantined ({WORDS.get(status, status)}): {why}"))
            elif status != "passed":
                counts["failures"] += 1
                kind = {"failed": "assertion", "couldnt_run": "couldnt_run"}.get(status, status)
                if reason.get("class") == "app_crashed":
                    kind = "app_crashed"
                failure = ET.SubElement(case, "failure", message=_xml(reason.get("message") or WORDS.get(status)),
                                        type=_xml(kind))
                failure.text = _xml("\n".join(filter(None, [
                    f"{WORDS.get(status, status)} ({reason.get('class')})" if reason.get("class") else
                    WORDS.get(status, status), reason.get("message"), reason.get("fix")]
                    + cause_lines(result.get("stepProof")) + _crash_lines(result.get("evidence")))))
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
                for run in result.get("runs") or ()] + list(result.get("notes") or ())
                + snapshot_lines(result.get("snapshot"), report_base, _rebase)
                + _log_lines(result.get("evidence"), report_base)
                + (step_lines(result.get("stepProof"), result.get("evidence")) if status != "passed" else []))))
            for finding in result.get("findings") or ():
                if finding.get("audit") in ("a11y", "l10n") and finding.get("level") != "known":
                    _finding_case(suite, counts, check, result, finding, report)
        for name in ("tests", "failures", "errors", "skipped"):
            suite.set(name, str(counts[name]))
            totals[name] += counts[name]
        suite.set("time", _seconds(counts["time"]))
        suite.set("timestamp", _iso(doc.get("startedAt")))
        totals["time"] += counts["time"]
    _xctest_suites(root, doc, totals, report_base)
    junit_verdict.add_if_unrepresented(root, doc, totals, _iso(doc.get("startedAt")))
    for name in ("tests", "failures", "errors", "skipped"):
        root.set(name, str(totals[name]))
    root.set("time", _seconds(doc.get("durationMs") if doc.get("durationMs") is not None else totals["time"]))
    ET.indent(root)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8") + b"\n"


def _xctest_suites(root, doc, totals, report_base):
    """One testsuite per XCTest target, in the same file as the checks' devices."""
    found = doc.get("xctest") or {}
    for target in found.get("targets") or ():
        suite = ET.SubElement(root, "testsuite", name=_xml(target["name"]), tests=str(target["tests"]),
                              failures=str(target["failed"]), errors="0", skipped=str(target["skipped"]),
                              time=_seconds(target["durationMs"]), timestamp=_iso(doc.get("startedAt")))
        properties = ET.SubElement(suite, "properties")
        for name, value in (("mobster.suite", doc.get("suite")), ("mobster.kind", "xctest"),
                            ("xctest.scheme", found.get("scheme")), ("xctest.device", found.get("device"))):
            if value:
                ET.SubElement(properties, "property", name=name, value=_xml(value))
        for case in target["cases"]:
            element = ET.SubElement(suite, "testcase", classname=_xml(case["classname"]), name=_xml(case["name"]),
                                    time=_seconds(case["durationMs"]))
            first = (case.get("failures") or [{}])[0]
            if first.get("file"):
                element.set("file", _xml(first["file"]))
                element.set("line", str(first["line"]))
            if case["status"] == "skipped":
                ET.SubElement(element, "skipped")
            elif case["status"] == "failed":
                failure = ET.SubElement(element, "failure", message=_xml(failure_line(case)), type="xctest")
                failure.text = _xml("\n".join((f"{item['file']}:{item['line']}: " if item.get("file") else "")
                                              + item["message"] for item in case["failures"]))
            if case.get("attachments"):
                out = ET.SubElement(element, "system-out")
                out.text = _xml("\n".join(f"Attachment: {_rebase(item['path'], report_base)}"
                                          for item in case["attachments"]))
        for name, key in (("tests", "tests"), ("failures", "failed"), ("skipped", "skipped")):
            totals[name] += target[key]
        totals["time"] += target["durationMs"]
    if found.get("status") == "couldnt_run":
        suite = ET.SubElement(root, "testsuite", name="XCTest", tests="1", failures="0", errors="1", skipped="0",
                              time="0.000", timestamp=_iso(doc.get("startedAt")))
        case = ET.SubElement(suite, "testcase", classname="xctest", name="your XCTest targets", time="0.000")
        reason = found.get("reason") or {}
        error = ET.SubElement(case, "error", message=_xml(reason.get("message") or "XCTest couldn't run"),
                              type="couldnt_run")
        error.text = _xml(" ".join(filter(None, [reason.get("message"), reason.get("fix")])))
        totals["tests"] += 1
        totals["errors"] += 1


def _finding_case(suite, counts, check, result, finding, report):
    """One accessibility finding as its own test case beside the check's: named for the rule and the element, with where
    it is in the failure text."""
    element = finding.get("element") or {}
    x, y, w, h = (element.get("rect") or [0, 0, 0, 0])[:4]
    label = f"{element.get('role') or 'element'}" + (f" \"{element['label']}\"" if element.get("label") else
                                                        f" (id {element['identifier']})" if element.get("identifier")
                                                        else "")
    case = ET.SubElement(suite, "testcase",
                         classname=_xml(f"{check.get('classname') or 'checks'}.{finding.get('audit') or 'a11y'}"),
                         name=_xml(f"{check.get('name')}: {finding.get('id')}, {label}"), time="0.000",
                         file=_xml(check.get("file")))
    counts["tests"] += 1
    props = ET.SubElement(case, "properties")
    for name, value in (("audit", finding.get("audit") or "a11y"), ("rule", finding.get("id")),
                        ("guideline", finding.get("guideline")),
                        ("element", label), ("where", f"{x:g}, {y:g}, {w:g} x {h:g} pt"), ("report", report)):
        if value is not None:
            ET.SubElement(props, "property", name=name, value=_xml(value))
    if check.get("quarantined"):
        counts["skipped"] += 1
        ET.SubElement(case, "skipped", message=_xml(f"Quarantined: {check.get('quarantineReason') or 'quarantined'}"))
        return
    if finding.get("level") == "warn":
        # A warning is a case that passed, with the finding as its output; a CI summary that lists passed cases'
        # output shows it, and one that doesn't never fails on it.
        ET.SubElement(case, "system-out").text = _xml(f"Warning: {finding.get('message')}")
        return
    counts["failures"] += 1
    failure = ET.SubElement(case, "failure", message=_xml(finding.get("message")), type="accessibility")
    failure.text = _xml("\n".join(filter(None, [
        f"{finding.get('id')} ({finding.get('guideline')}) on {result.get('device')}",
        f"Element: {label}", f"Where: {x:g}, {y:g}, {w:g} x {h:g} pt", finding.get("message")])))


def _crash_lines(evidence):
    """The crash for JUnit's <failure>: its title, its message and the crashed thread's top frames."""
    from ..verify.crashes import frame_text
    from ..verify.report import crash_title
    crash = evidence.get("crash") if isinstance(evidence, dict) else None
    if not crash:
        return []
    lines = ["", crash_title(crash) + f" at {(crash.get('atMs') or 0) / 1000:.1f} s"]
    if crash.get("message"):
        lines.append(crash["message"])
    lines += [f"  {frame.get('index')}  {frame_text(frame)}" for frame in crash.get("frames") or ()]
    return lines


def _log_lines(evidence, base):
    """The log window for JUnit's <system-out>: the app's log within 10 s of the failure, and where the files are."""
    from ..verify.evidence import WINDOW_MS, log_text
    if not isinstance(evidence, dict) or not (evidence.get("window") or evidence.get("crash")):
        return []
    out = []
    log, crash = evidence.get("log") or {}, evidence.get("crash") or {}
    if log.get("path"):
        out.append(f"Log: {_rebase(log['path'], base)}")
    if crash.get("report"):
        out.append(f"Crash report: {_rebase(crash['report'], base)}")
    window = evidence.get("window")
    if window is not None:
        out.append(f"The app's log within {WINDOW_MS // 1000} s of the failure "
                   f"({len(window.get('lines') or ())} lines):")
        out.append(log_text(window.get("lines")))
    return out


def _rebase(path, base):
    if not path or base is None:
        return path
    return str(Path(base) / path)


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
.filters { display: flex; flex-wrap: wrap; gap: 2px; padding: 2px; border-radius: var(--radius-full);
  background: var(--fill-input); margin-left: auto; }
.filters label { display: inline-flex; align-items: center; gap: 6px; height: 28px; padding: 0 12px;
  border-radius: var(--radius-full); font-size: 13px; font-weight: var(--weight-medium); color: var(--ink-2);
  cursor: pointer; user-select: none; -webkit-user-select: none;
  transition: color var(--duration-short) var(--ease-standard), background-color var(--duration-short) var(--ease-standard); }
.filters label span { color: var(--ink-3); font-weight: var(--weight-body); font-variant-numeric: tabular-nums; }
ul.failures { list-style: none; margin: 16px 0 0; padding: 0; display: grid; gap: 8px; max-width: 860px; }
ul.failures li { display: grid; grid-template-columns: 16px minmax(0, 1fr); gap: 8px; align-items: baseline;
  font-size: 14px; line-height: 20px; }
ul.failures li .icon { color: var(--tone); stroke-width: 2; position: relative; top: 3px; }
ul.failures a { font-weight: var(--weight-medium); color: var(--ink); }
ul.failures .why { display: block; margin-top: 2px; font-size: 13px; color: var(--ink-2); }
ul.failures .why code.assert { font-size: 12px; }
ul.failures li.more { display: block; padding-left: 24px; font-size: 13px; color: var(--ink-3); }
.found { color: var(--fail); }
.checks-head { display: flex; flex-wrap: wrap; align-items: center; gap: 12px 16px; margin-bottom: 12px; }
.checks-head h2 { margin: 0; }
h3.dev-group { display: flex; align-items: baseline; gap: 8px; margin: 32px 0 4px; font-size: 16px;
  line-height: 24px; font-weight: var(--weight-card-title); overflow-wrap: anywhere; }
h3.dev-group .count { font-size: 14px; font-weight: var(--weight-body); color: var(--ink-3);
  font-variant-numeric: tabular-nums; }
FILTER_RULES
/* The checks are a table on the page, as mobster.dev's tables: a header over a rule, a hairline between rows. */
.list-head, .check > summary { display: grid; gap: 4px 20px; align-items: start;
  grid-template-columns: minmax(0, 1fr) repeat(var(--cols, 1), minmax(132px, 196px)) 16px; }
.list-head { padding: 8px 12px; font-size: 13px; line-height: 20px; font-weight: var(--weight-card-title);
  color: var(--ink-2); border-bottom: 1px solid var(--line-strong); }
.list-head span { overflow-wrap: anywhere; }
.check { border-top: 1px solid var(--line); }
.list-head + .check { border-top: 0; }
.check > summary { list-style: none; cursor: pointer; margin: 4px 0; padding: 12px; border-radius: var(--radius-input);
  transition: background-color var(--duration-short) var(--ease-standard); }
.check > summary::-webkit-details-marker { display: none; }
@media (hover: hover) and (pointer: fine) { .check > summary:hover { background: var(--hover); } }
.what { min-width: 0; display: grid; gap: 4px; }
.what .name { font-size: 14px; line-height: 20px; font-weight: var(--weight-medium); overflow-wrap: anywhere; }
.what .sub { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
code.file { font: 12px/18px var(--font-mono); color: var(--ink-3); overflow-wrap: anywhere; margin-right: 4px; }
.res { display: grid; gap: 2px; min-width: 0; }
.res .v { display: inline-flex; align-items: center; gap: 6px; font-weight: var(--weight-medium); color: var(--tone); }
.res .v .icon { stroke-width: 2; }
.res .v .tag { margin-left: 2px; }
.res .small { font-size: 12px; line-height: 18px; color: var(--ink-3); overflow-wrap: break-word; padding-left: 22px; }
.res.muted .v { color: var(--ink-2); }
.res.muted .v .icon { color: var(--tone); }
.res .dev { display: none; font-size: 12px; color: var(--ink-3); overflow-wrap: anywhere; }
.res.missing { color: var(--ink-3); font-size: 13px; }
.chev { width: 16px; height: 16px; margin-top: 2px; color: var(--ink-3);
  transition: transform var(--duration-medium) var(--ease-out); }
@media (prefers-reduced-motion: reduce) { .chev { transition: none; } }
.check[open] .chev { transform: rotate(180deg); }
.body { padding: 4px 12px 24px; display: grid; gap: 24px; }
.result { display: grid; grid-template-columns: 280px minmax(0, 1fr); gap: 16px 32px; align-items: start; }
.result.noframe { grid-template-columns: minmax(0, 1fr); }
.result .result-head { grid-column: 1 / -1; }
.result .shot { grid-column: 1; grid-row: 2; width: 280px; }
.result .result-text { grid-column: 2; grid-row: 2; }
.result.noframe .result-text { grid-column: 1; }
.result .phone { border-radius: 46px; }
.result .phone img { border-radius: 40px; }
.result-text { min-width: 0; display: grid; gap: 12px; align-content: start; }
.result-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 16px; font-size: 13px;
  line-height: 20px; color: var(--ink-2); }
.result-head strong { color: var(--ink); font-weight: var(--weight-medium); }
.links { display: flex; flex-wrap: wrap; gap: 4px 16px; font-size: 13px; font-weight: var(--weight-medium); }
.links a { color: var(--ink); }
.callout { padding: 12px 16px; border-radius: var(--radius-container); font-size: 14px; line-height: 20px;
  background: color-mix(in srgb, var(--tone) 10%, transparent); overflow-wrap: anywhere; }
.callout p { margin: 0; } .callout p + p { margin-top: 4px; color: var(--ink-2); }
.aside { margin: 0; font-size: 13px; line-height: 20px; color: var(--ink-2); overflow-wrap: anywhere; }
.result-text pre.cmd { padding: 10px 16px; font-size: 12px; }
.aside strong { color: var(--ink); font-weight: var(--weight-medium); }
.table-wrap { overflow-x: auto; }
table.flaky { border-collapse: collapse; width: 100%; min-width: 560px; font-size: 14px; line-height: 20px; }
table.flaky th, table.flaky td { text-align: left; padding: 12px; border-top: 1px solid var(--line);
  vertical-align: top; overflow-wrap: anywhere; }
table.flaky thead th { border-top: 0; border-bottom: 1px solid var(--line-strong); padding-block: 8px;
  font-size: 13px; font-weight: var(--weight-card-title); color: var(--ink-2); white-space: nowrap; }
table.flaky tbody tr:first-child > * { border-top: 0; }
table.flaky th[scope=row] { font-weight: var(--weight-medium); }
table.flaky td.num, table.flaky td.dev { white-space: nowrap; }
table.flaky td.num { font-variant-numeric: tabular-nums; }
details.unit > summary { cursor: pointer; padding: 12px 0; font-weight: var(--weight-medium); }
table.unit th[scope=row] { width: 38%; overflow-wrap: break-word; }
details.unit > summary .quiet { margin-left: 8px; font-weight: var(--weight-body); color: var(--ink-3); }
.outcomes { display: inline-flex; align-items: center; gap: 4px; }
.outcomes .icon { stroke-width: 2.2; color: var(--tone); }
@media (max-width: 760px) {
  .result { grid-template-columns: minmax(0, 1fr); }
  .result .shot, .result .result-text { grid-column: 1; grid-row: auto; }
  .result .shot { margin: 8px auto 0; max-width: 100%; }
}
@media (max-width: 640px) {
  .checks-head .filters { margin-left: 0; }
  .list-head { display: none; }
  .check > summary { grid-template-columns: minmax(0, 1fr) 16px; padding: 12px 8px; gap: 8px 12px; }
  .check > summary .what { grid-column: 1; }
  .check > summary .chev { grid-column: 2; grid-row: 1; align-self: start; margin-top: 2px; }
  .check > summary .res { grid-column: 1; display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 12px; }
  .multi .check > summary .res .dev { flex-basis: 100%; }
  .multi .res .dev { display: block; }
  .res .small { padding-left: 0; }
  .body { padding: 2px 8px 20px; }
  table.flaky th, table.flaky td { padding: 10px 8px; }
}
@media print {
  .filters, .chev { display: none; }
  .check > summary { cursor: default; }
  details::details-content { content-visibility: visible; display: contents; }
  .check { break-inside: avoid; }
  .result .shot { width: 200px; }
  .result { grid-template-columns: 200px minmax(0, 1fr); }
}
"""  # noqa: E501
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
        rules.append(f"{label} {{ background: var(--surface); color: var(--ink); box-shadow: var(--shadow-thumb); }}")
        rules.append(f"#show-{key}:focus-visible ~ .checks-head label[for=show-{key}] {{ outline: 2px solid "
                     "var(--focus-ring); outline-offset: 1px; }")
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
    if summary.get("replayed"):
        parts.append(f'<span class="tag pass">{icon("check")}{_e(summary["replayed"])} replayed with no model'
                     "</span>")
    if summary.get("linkFirst"):
        parts.append(f'<span class="tag pass">{icon("check")}{_e(summary["linkFirst"]["steps"])} link-first '
                     f'step{"" if summary["linkFirst"]["steps"] == 1 else "s"}, '
                     f'{_e(summary["linkFirst"]["modelCallsSaved"])} model calls saved</span>')
    if summary.get("healed"):
        count = summary["healed"]
        parts.append(f'<span class="tag tone review">{_e(count)} healed step{"" if count == 1 else "s"} '
                     'to review</span>')
    from .a11y_baseline import counts as a11y_counts
    found = a11y_counts(doc)
    if any(found.values()):
        tone = "tone fail" if found["new"] else "tone review"
        parts.append(f'<span class="tag {tone}">{found["new"]} new accessibility finding'
                     f'{"" if found["new"] == 1 else "s"}, {found["warnings"]} warning'
                     f'{"" if found["warnings"] == 1 else "s"}, {found["known"]} in the baseline</span>')
    if doc.get("xctest"):
        found = doc["xctest"]
        words = xctest_words(found)
        parts.append(f'<span class="tag{" tone fail" if found["status"] != "passed" else " pass"}">{_e(words)}</span>')
    if options.get("replay") == "strict":
        parts.append('<span class="tag">No heals: a changed step fails</span>')
    if counted:
        heading = f"{summary.get('passed', 0)} of {counted} passed"
    else:
        heading = "Every check is quarantined"
    parts.append(f'</div><h1>{_e(heading)}<span class="quiet">{_e(duration_text(doc.get("durationMs")))}</span></h1>')
    aside = _aside(summary)
    if aside:
        parts.append(f'<p class="lede">{_e(aside)}</p>')
    parts.append(_failures(checks, devices, doc.get("xctest")))
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
    if options.get("spread") and len(devices) > 1:
        for device in devices:
            rows = [(number, check, kind) for number, (check, kind) in enumerate(zip(checks, kinds), 1)
                    if any(result.get("deviceKey") == device.get("key") for result in check.get("results") or ())]
            parts.append(f'<h3 class="dev-group">{_e(dots(device.get("name")))}<span class="count">{len(rows)}'
                         '</span></h3><div class="list" style="--cols: 1">')
            parts.append('<div class="list-head" aria-hidden="true"><span>Check</span><span class="col">Result'
                         "</span><span></span></div>")
            parts.extend(_row(number, check, kind, [device], folder) for number, check, kind in rows)
            parts.append("</div>")
        parts.append("</section>")
    else:
        multi = " multi" if len(devices) > 1 else ""
        parts.append(f'<div class="list{multi}" style="--cols: {max(1, len(devices))}">')
        if devices:
            parts.append('<div class="list-head" aria-hidden="true"><span>Check</span>'
                         + "".join(f'<span class="col">{_e(dots(device.get("name")))}</span>' for device in devices)
                         + "<span></span></div>")
        for number, (check, kind) in enumerate(zip(checks, kinds), 1):
            parts.append(_row(number, check, kind, devices, folder))
        parts.append("</div></section>")

    parts.extend(_xctest_html(doc.get("xctest")))
    flaky = [(check, result) for check in checks for result in check.get("results") or () if result.get("flaky")]
    if flaky:
        parts.append(f'<section aria-labelledby="flaky"><h2 id="flaky">Flaky<span class="count">{len(flaky)}</span>'
                     '</h2><div class="table-wrap"><table class="flaky"><thead><tr>'
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
                         f'<td class="dev">{_e(dots(result.get("device")))}</td>'
                         f'<td><span class="outcomes" role="img" aria-label="{_e(_outcome_words(runs))}">{outcomes}'
                         f'</span></td><td class="num">{_e(f"{round((rate or 0) * 100)}%")}</td><td>{why}</td>'
                         "</tr>")
        parts.append("</tbody></table></div></section>")
    unstable = [(check, result) for check in checks for result in check.get("results") or ()
                if (result.get("history") or {}).get("state") == "flaky"]
    if unstable:
        parts.append(f'<section aria-labelledby="history"><h2 id="history">Flaky over time<span class="count">'
                     f'{len(unstable)}</span></h2><div class="table-wrap"><table class="flaky"><thead><tr>'
                     '<th scope="col">Check</th><th scope="col">Device</th><th scope="col">Last results</th>'
                     '<th scope="col">Pass rate</th><th scope="col">Flake score</th><th scope="col">Next step</th>'
                     '</tr></thead><tbody>')
        for check, result in unstable:
            found = result["history"]
            outcomes = "".join(f'<span class="{TONES.get(o, "none")}">{icon(STATUS_MARKS.get(o, "dash"))}</span>'
                               for o in found["outcomes"])
            step = "Quarantined" if check.get("quarantined") else f'Add {_e(check.get("file"))} to the quarantine file'
            parts.append(f'<tr><th scope="row">{_e(check.get("name"))}</th>'
                         f'<td class="dev">{_e(dots(result.get("device")))}</td>'
                         f'<td><span class="outcomes" role="img" aria-label="{found["runs"]} results">{outcomes}'
                         f'</span></td><td class="num">{round((found["passRate"] or 0) * 100)}%</td>'
                         f'<td class="num">{found["flakeScore"]:.2f}</td><td>{step}</td></tr>')
        parts.append("</tbody></table></div></section>")
    builds = "".join(f'<span>TestFlight {_e(build.get("version"))} ({_e(build.get("build"))})</span>'
                     for build in doc.get("testflight") or ())
    parts.append(f'<footer class="foot"><span><a href="{links.MADE_WITH_REPORT}">Mobster</a> '
                 f'{_e((doc.get("mobster") or {}).get("version"))}</span>{builds}'
                 f'<span>Suite <span class="mono">{_e(doc.get("suite"))}</span></span>'
                 f"<span>{_e(counts_text(summary))}</span></footer>")
    parts.append("</main></body></html>")
    return "\n".join(parts)


def _xctest_html(found):
    """The XCTest section: a block per target, failures first; a target with failures opens."""
    from ..verify.report import icon
    if not found:
        return []
    totals = found.get("summary") or {}
    parts = ['<section aria-labelledby="xctest"><h2 id="xctest">XCTest'
             f'<span class="count">{_e(totals.get("tests", 0))}</span></h2>']
    if found.get("status") == "couldnt_run":
        reason = found.get("reason") or {}
        parts.append(f'<p class="lede">{_e(reason.get("message"))} {_e(reason.get("fix"))}</p></section>')
        return parts
    for target in found.get("targets") or ():
        why = (found.get("why") or {}).get(target["name"])
        parts.append(f'<details class="unit"{" open" if target["failed"] else ""}><summary>{_e(target["name"])}'
                     f'<span class="quiet">{_e(target["passed"])} of {_e(target["tests"])} passed'
                     + (f", {_e(target['skipped'])} skipped" if target["skipped"] else "")
                     + f", {_e(milliseconds(target['durationMs']))}" + (f". Ran because {_e(why)}" if why else "")
                     + '</span></summary><div class="table-wrap"><table class="flaky unit"><thead><tr>'
                     '<th scope="col">Test</th><th scope="col">Result</th><th scope="col">Time</th>'
                     '<th scope="col">Details</th></tr></thead><tbody>')
        for case in target["cases"]:
            tone = TONES.get(case["status"], "none")
            mark = icon(STATUS_MARKS.get(case["status"], "dash"))
            detail = _e(failure_line(case)) if case["status"] == "failed" else ""
            files = "".join(f' <a href="{_e(item["path"])}">{_e(item["name"])}</a>' for item in case["attachments"])
            parts.append(f'<tr><th scope="row">{_e(case["classname"])}.{_e(case["name"])}</th>'
                         f'<td class="dev"><span class="outcomes {tone}">'
                         f'{mark}{_e(case["status"].capitalize())}</span></td>'
                         f'<td class="num">{_e(milliseconds(case["durationMs"]))}</td><td>{detail}{files}</td></tr>')
        parts.append("</tbody></table></div></details>")
    parts.append("</section>")
    return parts


STATUS_MARKS = {"passed": "check", "failed": "cross", "needs_review": "review", "couldnt_run": "dash"}


def _row(number, check, kind, devices, folder):
    """One check: a summary row (its name, file, tags and each device's verdict) that opens to each device's frame and
    expectations."""
    from ..verify.report import dots, icon
    results = {result.get("deviceKey"): result for result in check.get("results") or ()}
    classes = " ".join(f"is-{key}" for key in sorted(kind))
    is_open = "unpassed" in kind or any(result.get("healed") for result in results.values())
    tags = [f'<span class="tag">{_e(tag)}</span>' for tag in check.get("tags") or ()]
    if check.get("quarantined"):
        tags.insert(0, '<span class="tag tone warn">Quarantined</span>')
    head = [f'<details class="check {classes}" id="check-{number}"' + (" open" if is_open else "") + "><summary>",
            f'<div class="what"><span class="name">{_e(check.get("name"))}</span><span class="sub">'
            f'<code class="file">{_e(check.get("file"))}</code>{"".join(tags)}</span></div>']
    body = []
    for device in devices:
        result = results.get(device.get("key"))
        name = device.get("name")
        if result is None:
            head.append(f'<div class="res missing"><span class="dev">{_e(dots(name))}</span>Not run here</div>')
            continue
        status = result.get("status")
        tone = TONES.get(status, "none") + (" muted" if check.get("quarantined") else "")
        small = []
        if result.get("passRate") is not None:
            small.append(f"{round(result['passRate'] * 100)}% passed")
        if (result.get("attempts") or 0) > 1:
            small.append(f"{result['attempts']} attempts")
        if result.get("durationMs"):
            small.append(duration_text(result.get("durationMs")))
        measured = perf.line(perf.numbers(result.get("perf")), (result.get("history") or {}).get("perf"))
        if measured:
            small.append(measured)
        if result.get("replayed"):
            small.append("replayed, $0" if not result.get("costUsd") else f"replayed, ${result['costUsd']:.2f}")
        if result.get("healed"):
            small.append(f"{len(result['healed'])} healed")
        if check.get("quarantined"):
            small.append("quarantined")
        head.append(f'<div class="res {tone}"><span class="sr">{_e(dots(name))}: </span>'
                    f'<span class="dev" aria-hidden="true">{_e(dots(name))}</span>'
                    f'<span class="v">{icon(STATUS_MARKS.get(status, "dash"))}{_e(WORDS.get(status, status))}'
                    + ('<span class="tag tone warn">Flaky</span>' if result.get("flaky") else "") + "</span>"
                    + (f'<span class="small">{_e(", ".join(small))}</span>' if small else "") + "</div>")
        body.append(_result(check, result, name, is_open, device, folder))
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


def _result(check, result, device_name, is_open, device=None, folder=None):
    """One device's result, opened: the frame its verdict came from beside the numbered expectations."""
    from ..verify.report import OVERLAY_LEGEND, command, dots, evidence_pane, expectations, healed_section, replay_chip
    status = result.get("status")
    tone = TONES.get(status, "none")
    word = WORDS.get(status, status)
    frame = _thumb(result.get("frame"), OPEN_FRAME if is_open else CLOSED_FRAME, 78 if is_open else 68)
    attempts = result.get("attempts") or 0
    line = word + (f" after {attempts} attempts" if attempts > 1 else "")
    if result.get("durationMs"):
        line += f", {duration_text(result.get('durationMs'))}"
    links = []
    if result.get("report"):
        links.append(f'<a href="{_e(result["report"])}">Full report</a>')
    runs = [run for run in result.get("runs") or () if run.get("report") and run.get("report") != result.get("report")]
    for run in runs[-4:]:
        links.append(f'<a href="{_e(run["report"])}">Attempt {_e(run.get("attempt"))}</a>')
    if result.get("video"):
        links.append(f'<a href="{_e(result["video"])}">Video</a>')
    evidence = result.get("evidence") or {}
    if (evidence.get("log") or {}).get("path"):
        links.append(f'<a href="{_e(evidence["log"]["path"])}">Log</a>')
    chip = replay_chip(result.get("replay"), healed=len(result.get("healed") or ()))
    head = (f'<div class="result-head"><strong>{_e(dots(device_name))}</strong><span>{_e(line)}</span>{chip}'
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
    if status != "passed" or result.get("flaky"):
        text.append(cause_html(result.get("stepProof")))
        text.append(evidence_pane(evidence, step=evidence.get("step")))
        text.append(proof_steps(result))
    flaky = result.get("flakyReason") or {}
    if result.get("flaky") and (flaky.get("message") or flaky.get("assertions")):
        text.append('<p class="aside"><strong>Flaky.</strong> An attempt that didn\'t pass: ' + _why(flaky) + "</p>")
    for note in result.get("notes") or ():
        text.append(f'<p class="aside">{_e(note)}</p>')
    if result.get("linkFirst"):
        saved = result["linkFirst"]
        text.append(f'<p class="aside">Opened {_e(saved["taken"])} link{"" if saved["taken"] == 1 else "s"} before '
                    f'tapping; {_e(saved["modelCallsSaved"])} model call{"" if saved["modelCallsSaved"] == 1 else "s"}'
                    " saved.</p>")
    if result.get("healed"):
        text.append(healed_section(result["healed"], folder or ".", check.get("file"),
                                   heading="Healed steps to review",
                                   ident="healed-"
                                   + hashlib.sha1(f"{check.get('file')}|{device_name}".encode()).hexdigest()[:10]))
    again = _again(check, device or {}) if status != "passed" else None
    if again:
        text.append(command(again))
    image = (f'<div class="shot"><div class="phone"><img src="{frame}" alt="The screen at the verdict on '
             f'{_e(dots(device_name))}, each asserted element outlined and numbered as in the list">'
             f'</div>{OVERLAY_LEGEND}'
             "</div>" if frame else "")
    return (f'<div class="result{"" if frame else " noframe"}">{head}<div class="result-text">{"".join(text)}</div>'
            f"{image}</div>")


def _outcome_words(runs):
    return ", ".join(WORDS.get(run.get("verdict"), "?").lower() for run in runs)


def _aside(summary):
    """What the heading doesn't say: '1 flaky, 2 quarantined'. What didn't pass is listed under it."""
    return ", ".join(f"{summary[key]} {word}" for key, word in (("flaky", "flaky"), ("quarantined", "quarantined"))
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


def _failures(checks, devices, found=None, limit=3):
    """Under the heading: the results that didn't pass, each linked to its row, with why, then the XCTest tests that
    failed, each linked to the XCTest section."""
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
    if found and found.get("status") == "couldnt_run":
        reason = found.get("reason") or {}
        rows.append(f'<li class="none">{icon("dash")}<span><a href="#xctest">Your XCTest targets</a><span class="why">'
                    f'{_e(reason.get("message"))}</span></span></li>')
    for target in (found or {}).get("targets") or ():
        for case in target["cases"]:
            if case["status"] == "failed":
                rows.append(f'<li class="fail">{icon("cross")}<span><a href="#xctest">{_e(target["name"])}: '
                            f'{_e(case["classname"])}.{_e(case["name"])}</a><span class="why">'
                            f'{_e(failure_line(case))}</span></span></li>')
    if not rows:
        return ""
    more = len(rows) - limit
    return ('<ul class="failures">' + "".join(rows[:limit])
            + (f'<li class="more">and {more} more below</li>' if more > 0 else "") + "</ul>")


def counts_text(summary):
    parts = [f"{summary.get('passed', 0)} passed"]
    if summary.get("flaky"):
        parts.append(f"{summary['flaky']} flaky")
    for key, word in (("failed", "failed"), ("needsReview", "need review"), ("couldntRun", "couldn't run"),
                      ("quarantined", "quarantined")):
        if summary.get(key):
            parts.append(f"{summary[key]} {'needs review' if key == 'needsReview' and summary[key] == 1 else word}")
    return ", ".join(parts)


def duration_text(ms):
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
    """results.json, junit.xml and index.html in ``folder`` (after ``collect``), plus --junit FILE and --html DIR."""
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
    from .capsule import mark_complete
    mark_complete(folder, doc)
    return paths


def _relative_dir(folder, cwd):
    try:
        return Path(folder).resolve().relative_to(Path(cwd).resolve()).as_posix()
    except ValueError:
        return str(folder)
