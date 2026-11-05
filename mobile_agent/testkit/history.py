"""Per-check test history; a file that can't be read is set aside as history.json.corrupt and never stops a suite."""

import json
import re
from pathlib import Path
from statistics import median

from ..env_file import writer
from ..paths import write_text_atomic
from ..verify import perf

SCHEMA = "mobster.test-history/1"
XCTEST_FILE = "xctest:"      # the file of a history entry that is an XCTest case: xctest:Target/Class/test
FILE = "history.json"
KEEP, MAX_CHECKS, MAX_TESTS = 20, 400, 2000
MIN_RUNS, FLAKY_AT = 5, 0.15
TREND_AT, TREND_MIN = 0.25, 4
KEPT = ("passed", "failed", "needs_review")
# A suite's folder in .mobster/test-results (reports.make_suite_dir): its name sorts by when it started.
SUITE_FOLDER = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")


def path_of(root):
    return Path(root) / ".mobster" / "test-results" / FILE


def key_of(check, deviceKey):
    return (f"{check.get('file') or check.get('name')}|{deviceKey}"
            + (f"|{check['locale']}" if check.get("locale") else ""))


def _run(item):
    """A stored run, checked at the boundary; None when it isn't one."""
    if not isinstance(item, dict) or item.get("status") not in KEPT:
        return None
    ms, attempts = item.get("ms"), item.get("attempts")
    return {"suite": str(item.get("suite") or "")[:40], "at": item["at"] if isinstance(item.get("at"), int) else None,
            "status": item["status"], "flaky": item.get("flaky") is True,
            # A budget the run couldn't measure (a missed reading) is neither a pass nor a failure of the app.
            "unmeasured": item.get("unmeasured") is True,
            "ms": ms if isinstance(ms, int) and ms >= 0 else None,
            "attempts": attempts if isinstance(attempts, int) and attempts > 0 else 1,
            "perf": perf.clean(item.get("perf"))}


def load(root):
    """(history, ok): {key: {"name", "file", "device", "runs": [...]}} (oldest run first) and whether the file was
    absent or readable."""
    try:
        text = path_of(root).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, True
    except (OSError, UnicodeError):
        return {}, False
    try:
        data = json.loads(text)
        checks = data["checks"]
        if data.get("schema") != SCHEMA or not isinstance(checks, dict):
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError):
        return {}, False
    out = {}
    for key, entry in checks.items():
        if not isinstance(key, str) or not isinstance(entry, dict) or not isinstance(entry.get("runs"), list):
            continue
        runs = [run for run in map(_run, entry["runs"]) if run is not None][-KEEP:]
        if runs:
            out[key] = {"name": str(entry.get("name") or "")[:200], "file": entry.get("file") if isinstance(
                entry.get("file"), str) else None, "device": str(entry.get("device") or "")[:200], "runs": runs}
    return out, True


def _is_test(entry):
    return str(entry.get("file") or "").startswith(XCTEST_FILE)


def _save(root, history):
    # A project's tests are many and its checks few: each kind keeps its newest, so tests never push checks out.
    for is_test, limit in ((False, MAX_CHECKS), (True, MAX_TESTS)):
        keys = [key for key, entry in history.items() if _is_test(entry) is is_test]
        for key in sorted(keys, key=lambda k: history[k]["runs"][-1].get("at") or 0)[:max(0, len(keys) - limit)]:
            del history[key]
    write_text_atomic(path_of(root), json.dumps({"schema": SCHEMA, "checks": history}, ensure_ascii=False) + "\n")


def _set_aside(root):
    source = path_of(root)
    try:
        source.replace(source.with_name(FILE + ".corrupt"))
    except OSError:
        pass


def stats(runs):
    """One check's numbers over its kept runs (oldest first)."""
    runs = list(runs)[-KEEP:]
    judged = [run for run in runs if not run.get("unmeasured")]
    n = len(judged)
    passed = [run["status"] == "passed" for run in judged]
    flaky_runs = sum(run["flaky"] for run in judged)
    changes = sum(a != b for a, b in zip(passed, passed[1:]))
    score = round(max(flaky_runs / n, changes / (n - 1) if n > 1 else 0.0), 3) if n else 0.0
    if n < MIN_RUNS:
        state = "new"
    elif not passed[-1]:
        state = "failing"
    elif score >= FLAKY_AT:
        state = "flaky"
    else:
        state = "stable"
    return {"runs": n, "passRate": round(sum(passed) / n, 4) if n else None, "flakyRuns": flaky_runs,
            "flakeScore": score, "state": state, "lastStatus": runs[-1]["status"] if runs else None,
            "lastAt": runs[-1]["at"] if runs else None,
            "durationMs": [run["ms"] for run in runs], "trend": _trend([run["ms"] for run in runs]),
            "outcomes": [run["status"] for run in runs], "perf": perf.trend(runs), "unmeasured": len(runs) - n,
            "perfLatest": (runs[-1].get("perf") or {}) if runs else {}}


def _trend(durations):
    """slower, faster or steady: the newer half's median time against the older half's; None with too few times."""
    known = [ms for ms in durations if ms is not None]
    if len(known) < TREND_MIN:
        return None
    half = len(known) // 2
    before, after = median(known[:half]), median(known[-half:])
    if before <= 0:
        return None
    change = (after - before) / before
    return "slower" if change > TREND_AT else "faster" if change < -TREND_AT else "steady"


def describe(entry):
    """An entry with its stats, and whether to suggest quarantine; what the CLI, the API and the report show."""
    found = stats(entry["runs"])
    return {"name": entry["name"], "file": entry["file"], "device": entry["device"], **found,
            "suggestQuarantine": found["state"] == "flaky"}


def _items(value):
    """The dicts in ``value`` when it is a list; results.json is external data, so anything else is skipped."""
    items = value if isinstance(value, list) else []
    return [item for item in items if isinstance(item, dict)]


def _cells(doc):
    """(check, result) for every check result in ``doc``, and for every XCTest case: a case is the result of a check
    named for its class and test, whose file is xctest:Target/Class/test."""
    for check in _items(doc.get("checks")):
        for result in _items(check.get("results")):
            yield check, result
    xctest = doc.get("xctest")
    for target in _items(xctest.get("targets") if isinstance(xctest, dict) else None):
        for case in _items(target.get("cases")):
            if not all(isinstance(case.get(key), str) for key in ("classname", "name")) or not isinstance(
                    target.get("name"), str):
                continue
            yield {"name": f"{case['classname']}.{case['name']}",
                   "file": f"{XCTEST_FILE}{target['name']}/{case['classname']}/{case['name']}"}, case


def _add(history, doc):
    """Fold the finished suite ``doc`` into ``history`` once (a finished suite is immutable); the keys it added to."""
    added = set()
    for check, result in _cells(doc):
        reason = result.get("reason")
        found = _run({"suite": doc.get("suite"), "at": doc.get("startedAt"), "status": result.get("status"),
                      "flaky": bool(result.get("flaky")), "ms": result.get("durationMs"),
                      "unmeasured": isinstance(reason, dict) and reason.get("class") == "budget_unmeasured",
                      "attempts": result.get("attempts"), "perf": perf.numbers(result.get("perf"))})
        if found is None:
            continue
        key = key_of(check, result.get("deviceKey"))
        entry = history.setdefault(key, {"runs": []})
        entry.update(name=check.get("name"), file=check.get("file"), device=result.get("device"))
        if found["suite"] and any(run["suite"] == found["suite"] for run in entry["runs"]):
            continue
        entry["runs"] = (entry["runs"] + [found])[-KEEP:]
        added.add(key)
    return added


def _unrecorded(root, history):
    """The project's last KEEP suites that ``history`` has no run of (from before the file existed), oldest first."""
    folder = path_of(root).parent
    try:
        names = sorted(item.name for item in folder.iterdir() if SUITE_FOLDER.match(item.name))[-KEEP:]
    except OSError:
        return []
    known = {run["suite"] for entry in history.values() for run in entry["runs"]}
    docs = []
    for name in names:
        if name in known:
            continue
        try:
            doc = json.loads((folder / name / "results.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            continue
        if isinstance(doc, dict) and str(doc.get("suite") or name)[:40] not in known:
            docs.append({**doc, "suite": doc.get("suite") or name})
    return docs


def record(root, doc):
    try:
        with writer(path_of(root)):
            history, ok = load(root)
            if not ok:
                _set_aside(root)
            _add(history, doc)
            _save(root, history)
        suggested = []
        for check, result in _cells(doc):
            entry = history.get(key_of(check, result.get("deviceKey")))
            if entry is None or result.get("status") not in KEPT:
                continue
            result["history"] = stats(entry["runs"])
            # A test isn't a check: the quarantine file can't name it, so its flake score is shown and not suggested.
            if (result["history"]["state"] == "flaky" and not check.get("quarantined")
                    and not _is_test(check)):
                suggested.append({"check": check.get("name"), "file": check.get("file"),
                                  "device": result.get("device"), "flakeScore": result["history"]["flakeScore"]})
        doc["history"] = {"suggestQuarantine": suggested}
    except Exception:
        pass
    return doc


def flaky_reasons(root):
    """{check file: reason} for checks whose history is flaky on any device, for --auto-quarantine."""
    history, _ = load(root)
    entries = [(entry, stats(entry["runs"])) for entry in history.values()]
    # Quarantine applies to the whole check, so a failure on any recorded device vetoes it.
    failing = {entry["file"] for entry, found in entries if found["lastStatus"] != "passed"}
    return {entry["file"]: (f"flaky: passed {round(found['passRate'] * 100)}% of its last "
                            f"{found['runs']} runs on {entry['device']}")
            for entry, found in entries if found["state"] == "flaky" and entry["file"]
            and entry["file"] not in failing}


def report(root, *, only=None):
    """Every check's history under ``root`` as ``describe`` shows it, flaky first, then by name; ``ok`` False when the
    file couldn't be read. Suites the file has none of are folded in, by when they started (never written back)."""
    history, ok = load(root)
    added = set()
    for doc in _unrecorded(root, history):
        added |= _add(history, doc)
    for key in added:
        runs = sorted(history[key]["runs"], key=lambda run: run["at"] or 0)
        history[key]["runs"] = runs[-KEEP:]
    rows = [describe(entry) for entry in history.values() if only is None or only(entry)]
    rows.sort(key=lambda row: (row["state"] != "flaky", -row["flakeScore"], str(row["name"]), row["device"]))
    return {"checks": rows, "ok": ok}


__all__ = ["record", "load", "stats", "describe", "report", "flaky_reasons", "KEEP", "FILE", "SCHEMA"]
