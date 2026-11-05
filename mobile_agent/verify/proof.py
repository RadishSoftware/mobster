"""Proof per step: for each step of a check, the log lines and network requests of its time span, the frame it ended on,
and one line saying why a run that didn't pass went red. No model call; it reads what the run already recorded."""

import re

# A run's result.json carries at most this many lines and rows in all; the full log stays in logs/app.log.
STEP_LOG_LINES = 8
TOTAL_LOG_LINES = 160
STEP_NET_ROWS = 10
TOTAL_NET_ROWS = 100
LINE_TEXT = 300

# Why a run didn't pass, by the reason class that verify records. ``side`` names who to look at first: the app, the
# check, or the environment (the simulator, the build, the phone, Mobster).
CAUSES = {
    "app_crashed": ("app", "app crashed"),
    "app_not_running": ("app", "app was not running at the verdict"),
    "assertion": ("app", "assertion failed on the final screen"),
    "blocked": ("app", "assertion failed on the final screen"),
    "finding": ("app", "audit found a problem on the final screen"),
    "budget": ("app", "a budget was passed"),
    "drift": ("check", "step could not be done (the recorded path no longer matches the screen)"),
    "replay": ("check", "step could not be done (control not found)"),
    "no_assertions": ("check", "check has nothing to judge: add an expectation"),
    "held_before_flow": ("check", "expectations already held before the steps ran"),
    "usage": ("check", "check could not start (see the message)"),
    "unknown_outcome": ("check", "step outcome unknown, so it was not repeated"),
    "wda": ("environment", "simulator or build problem (WebDriverAgent stopped answering)"),
    "launch": ("environment", "simulator or build problem (the app did not launch)"),
    "install": ("environment", "simulator or build problem (the app did not install)"),
    "environment": ("environment", "simulator or build problem"),
    "device": ("environment", "simulator or build problem (the device was not available)"),
    "model": ("environment", "model unavailable (check your key and credit)"),
    "stopped": ("environment", "run stopped before it finished"),
    "internal": ("environment", "Mobster stopped with an unexpected error (run.log has the details)"),
}
SETTLE = "timed out waiting for the screen to settle"


def cause(verdict, klass, *, stable=None):
    """``{"side", "kind", "line"}`` for a run that didn't pass, else None. A read of a screen still changing is a
    timeout, not an app bug."""
    if verdict == "passed":
        return None
    if klass in ("assertion", "blocked") and stable is False:
        return {"side": "app", "kind": "settle", "line": SETTLE}
    side, line = CAUSES.get(klass, ("environment", "run ended without a clear cause (see the reason)"))
    return {"side": side, "kind": klass or "unknown", "line": line}


# CFNetwork's per-task lines, which the app's own log carries on a simulator and on an iPhone's syslog.
URL = re.compile(r"\b(?P<method>GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)?\s*(?P<scheme>https?)://(?P<host>[A-Za-z0-9.\-]+)")
TASK = re.compile(r"Task <(?P<task>[0-9A-Fa-f\-]+)>\.<\d+>")
STATUS = re.compile(r"(?:response_status=|received response, status )(?P<status>\d{3})")
DURATION = re.compile(r"transaction_duration_ms=(?P<ms>\d+)")
RESPONSE_BYTES = re.compile(r"response_bytes=(?P<n>\d+)")
CONTENT = re.compile(r"received response, status \d{3} content (?P<n>\d+)")


class _Rows:
    """Requests assembled from the lines of one CFNetwork task: host and method (never the path, query, headers or a
    body), status, duration, bytes."""

    def __init__(self):
        self.tasks = {}
        self.order = []

    def feed(self, line):
        text = line.get("text") or ""
        found = TASK.search(text)
        if not found:
            return
        task = found.group("task")
        row = self.tasks.get(task)
        if row is None:
            row = self.tasks[task] = {"atMs": line.get("atMs") or 0, "host": None, "method": None, "status": None,
                                      "durationMs": None, "bytes": None, "_seen": False}
            self.order.append(task)
        url = URL.search(text)
        if url:
            row["host"] = row["host"] or url.group("host")
            row["method"] = row["method"] or url.group("method")
        for key, pattern, group in (("status", STATUS, "status"), ("durationMs", DURATION, "ms")):
            match = pattern.search(text)
            if match and row[key] is None:
                row[key] = int(match.group(group))
        size = RESPONSE_BYTES.search(text) or CONTENT.search(text)
        if size:
            row["bytes"] = int(size.group("n"))
        row["_seen"] = row["_seen"] or row["status"] is not None or row["durationMs"] is not None

    def done(self):
        out = []
        for task in self.order:
            row = self.tasks[task]
            if row.pop("_seen") and row["host"]:
                out.append({key: value for key, value in row.items() if value is not None})
        return out


def network_rows(lines):
    """The requests the lines show, in order. ``lines`` are dicts with ``atMs`` and ``text`` (already through the secret
    filter). A line that names no host gives no row: what the log doesn't expose is not guessed."""
    rows = _Rows()
    for line in lines:
        rows.feed(line)
    return rows.done()


def _pick(lines, limit):
    """At most ``limit`` lines: the app's own, errors and faults first, the latest of the rest, in time order."""
    if len(lines) <= limit:
        return list(lines)

    def rank(i):
        line = lines[i]
        loud = line.get("own") or line.get("level") in ("error", "fault") or line.get("process") == "SpringBoard"
        return (0 if loud else 1, -(line.get("atMs") or 0))
    keep = sorted(range(len(lines)), key=rank)[:limit]
    return [lines[i] for i in sorted(keep)]


def _short(line):
    text = str(line.get("text") or "")
    return {"atMs": line.get("atMs") or 0, "level": line.get("level") or "",
            "text": text if len(text) <= LINE_TEXT else text[:LINE_TEXT] + "…"}


def _slice(lines, rows, start, end, *, log_room, net_room):
    """The lines and rows after ``start`` up to ``end`` (the first span has no start: a log clock a little ahead of the
    run's still belongs to the first step)."""
    low = float("-inf") if start is None else start
    inside = [line for line in lines if low < (line.get("atMs") or 0) <= end and not line.get("driver")]
    net = [row for row in rows if low < row["atMs"] <= end]
    return {"fromMs": start or 0, "toMs": end,
            "log": {"count": len(inside), "lines": [_short(line) for line in _pick(inside, log_room)]},
            "network": {"count": len(net), "rows": net[:net_room]}}


def steps_proof(steps, lines, *, end_ms):
    """One entry per step for the span from the step before it to its own time, and ``after`` for what the app did
    between the last step and the verdict. ``lines`` are dicts (``atMs``, ``level``, ``text``, ``own``)."""
    steps = [step for step in steps or () if isinstance(step.get("at_ms"), (int, float))]
    rows = network_rows(lines)
    count = len(steps) + 1
    log_room = max(2, min(STEP_LOG_LINES, TOTAL_LOG_LINES // count))
    net_room = max(2, min(STEP_NET_ROWS, TOTAL_NET_ROWS // count))
    out, start = [], None
    for step in steps:
        end = max(step["at_ms"], start or 0)
        entry = _slice(lines, rows, start, end, log_room=log_room, net_room=net_room)
        entry.update(index=step.get("index"), text=step.get("text"), frame=step.get("frame"))
        out.append(entry)
        start = end
    after = _slice(lines, rows, start, max(end_ms, start or 0), log_room=log_room, net_room=net_room)
    return out, after


def judged(frame, ax_frame, tree, verdict_ms):
    """Which frame and which read of the tree the verdict used."""
    return {"frame": frame, "overlay": ax_frame, "verdictAtMs": verdict_ms,
            "treeHash": tree.fingerprint() if tree is not None and hasattr(tree, "fingerprint") else None}


def build(*, verdict, klass, stable, steps, lines, end_ms, frame, ax_frame, tree, verdict_ms):
    """result.json's ``proof``."""
    entries, after = steps_proof(steps, lines, end_ms=end_ms)
    return {"cause": cause(verdict, klass, stable=stable),
            "judged": judged(frame, ax_frame, tree, verdict_ms),
            "steps": entries, "after": after}


def clip(entry, offset_ms):
    """Seconds into the recording for a step's span, given where the recording began on the run's clock (the result's
    ``video_offset_ms``), or None when the offset is unknown."""
    if not isinstance(offset_ms, (int, float)) or entry.get("toMs") is None:
        return None
    start = max(0.0, (max(entry.get("fromMs") or 0, 0) - offset_ms) / 1000)
    end = max(start, (entry["toMs"] - offset_ms) / 1000)
    return round(start, 1), round(end, 1)


def failing_entry(proof, evidence_step=None):
    """The step entry a failure belongs to: the one named by the evidence, else the last."""
    entries = (proof or {}).get("steps") or []
    for entry in entries:
        if evidence_step is not None and entry.get("index") == evidence_step:
            return entry
    return entries[-1] if entries else None


__all__ = ["build", "cause", "clip", "failing_entry", "network_rows", "steps_proof"]
