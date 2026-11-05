"""The suites `mobster test` ran on this Mac, newest first, for the Mac app's Test results page."""

import json
import re
import threading
from pathlib import Path

from ..paths import dev_data_dir, write_text_atomic

INDEX = "test-results.json"
KEEP = 20
STEPS = 60
EXPECTATIONS = 20
FRAME = re.compile(r"^frames/[0-9A-Za-z._-]{1,80}\.(?:jpg|jpeg|png)$")
SUITE_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")
RUN_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")
_lock = threading.Lock()


def _index(folder=None):
    return Path(folder or dev_data_dir()) / INDEX


def _read(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value


def remember(doc, folder, root, *, data_dir=None):
    # Never raises: the page is a convenience, the reports are the record.
    try:
        folder = Path(folder).resolve()
        entry = {"suite": doc.get("suite"), "folder": str(folder), "root": str(Path(root).resolve()),
                 "startedAt": doc.get("startedAt"), "exitCode": doc.get("exitCode")}
        with _lock:
            path = _index(data_dir)
            known = _read(path)
            known = [item for item in known if isinstance(item, dict) and item.get("folder") != entry["folder"]] \
                if isinstance(known, list) else []
            write_text_atomic(path, json.dumps([entry] + known[:KEEP - 1], indent=1) + "\n")
    except Exception:
        pass


def _run(run_dir):
    """What the run that didn't pass left in its result.json: its steps (index, text, at_ms), each expectation with
    whether it held and what the screen showed instead (ICP-3: "1 of 5 expectations failed" named none), and the frame
    the verdict was read from, as {run, path} under .mobster/runs (GET /api/projects/{id}/frame serves it). Empty when
    the file can't be read."""
    empty = {"steps": [], "expectations": [], "frame": None}
    if not run_dir or not RUN_ID.match(Path(run_dir).name):
        return empty
    result = _read(Path(run_dir) / "result.json")
    if not isinstance(result, dict):
        return empty
    steps = []
    for step in result.get("steps") if isinstance(result.get("steps"), list) else ():
        if isinstance(step, dict) and isinstance(step.get("index"), int):
            steps.append({"index": step["index"], "text": str(step.get("text") or step.get("op") or "")[:300],
                          "atMs": step.get("at_ms") if isinstance(step.get("at_ms"), (int, float)) else None})
    expectations, frame = [], None
    for item in result.get("assertions") if isinstance(result.get("assertions"), list) else ():
        if not isinstance(item, dict) or not isinstance(item.get("ok"), bool):
            continue
        name = (item.get("assertion") or {}).get("name") if isinstance(item.get("assertion"), dict) else None
        observed = None if item["ok"] else str(item.get("observed") or "")[:300]
        expectations.append({"text": str(item.get("text") or "")[:200], "name": str(name)[:120] if name else None,
                             "ok": item["ok"], "observed": observed})
        shot = item.get("frame")
        if isinstance(shot, str) and FRAME.match(shot) and (frame is None or not item["ok"]):
            frame = {"run": Path(run_dir).name, "path": shot}
    return {"steps": steps[:STEPS], "expectations": expectations[:EXPECTATIONS], "frame": frame}


def _suite(entry):
    folder = Path(str(entry.get("folder") or ""))
    if (not SUITE_ID.match(folder.name) or folder.parent.name != "test-results"
            or folder.parent.parent.name != ".mobster"):
        return None
    doc = _read(folder / "results.json")
    if not isinstance(doc, dict) or doc.get("schema") != "mobster.test/1":
        return None
    unpassed = []
    checks = [check for check in doc.get("checks") or () if isinstance(check, dict)]
    for check in checks:
        for result in check.get("results") or ():
            if not isinstance(result, dict) or (result.get("status") == "passed" and not result.get("flaky")):
                continue
            runs = [run for run in result.get("runs") or () if isinstance(run, dict)]
            failing = next((run for run in reversed(runs) if run.get("verdict") != "passed"), runs[-1] if runs else {})
            unpassed.append({"check": check.get("name"), "file": check.get("file"), "device": result.get("device"),
                             "status": result.get("status"), "flaky": bool(result.get("flaky")),
                             "quarantined": bool(check.get("quarantined")),
                             "reason": result["reason"] if isinstance(result.get("reason"), dict) else None,
                             "evidence": result.get("evidence"), **_run(failing.get("runDir")),
                             "video": bool(result.get("video"))})
    # The devices the suite ran on, so the page's row says what ran where (DR-74), not only the suite's id.
    listed = [str(d.get("name") or d.get("key")) for d in doc.get("devices") or () if isinstance(d, dict)
              and (d.get("name") or d.get("key"))]
    seen = [r.get("device") for c in checks for r in c.get("results") or ()
            if isinstance(r, dict) and isinstance(r.get("device"), str)]
    devices = list(dict.fromkeys(listed or seen))[:8]
    return {"suite": doc.get("suite"), "devices": devices, "checks": len(checks),
            "project": Path(str(entry.get("root") or folder.parent.parent.parent)).name,
            "folder": str(folder), "startedAt": doc.get("startedAt"), "durationMs": doc.get("durationMs"),
            "exitCode": doc.get("exitCode"), "summary": doc.get("summary") or {}, "unpassed": unpassed,
            "git": _git(doc.get("git"))}


def _git(value):
    """results.json's git, as it was written; None for a suite from before it was, or one outside git."""
    if not isinstance(value, dict):
        return None
    out = {key: value[key] for key in ("branch", "head") if isinstance(value.get(key), str)}
    return out or None


def known_roots(*, data_dir=None):
    """The projects `mobster test` ran in on this Mac that still have a `.mobster/`, newest first."""
    known = _read(_index(data_dir))
    roots = []
    for entry in known if isinstance(known, list) else ():
        root = Path(str(entry.get("root") or "")) if isinstance(entry, dict) else None
        if root and root.is_absolute() and (root / ".mobster").is_dir() and root not in roots:
            roots.append(root)
    return roots


def recent(limit=5, *, data_dir=None, root=None):
    """The last ``limit`` suites that can still be read, newest first; with ``root``, only the suites run in that
    project folder."""
    known = _read(_index(data_dir))
    wanted = str(Path(root).resolve()) if root is not None else None
    out = []
    for entry in known if isinstance(known, list) else ():
        if len(out) >= limit:
            break
        if wanted is not None and (not isinstance(entry, dict) or entry.get("root") != wanted):
            continue
        suite = _suite(entry) if isinstance(entry, dict) else None
        if suite is not None:
            out.append(suite)
    return out


def by_root(*, data_dir=None):
    known = _read(_index(data_dir))
    out = {}
    for entry in known if isinstance(known, list) else ():
        root = entry.get("root") if isinstance(entry, dict) else None
        if not isinstance(root, str) or root in out:
            continue
        folder = Path(str(entry.get("folder") or ""))
        if not SUITE_ID.match(folder.name) or folder.parent.name != "test-results":
            continue
        doc = _read(folder / "results.json")
        if isinstance(doc, dict) and doc.get("schema") == "mobster.test/1":
            out[root] = {"suite": doc.get("suite"), "startedAt": doc.get("startedAt"), "exitCode": doc.get("exitCode"),
                         "summary": doc.get("summary") if isinstance(doc.get("summary"), dict) else {}}
    return out


__all__ = ["remember", "recent", "known_roots", "by_root", "INDEX"]
