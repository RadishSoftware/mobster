"""Sending a feedback report to the account service, and keeping it here when it can't go yet (docs/feedback.mdx)."""

import json
import os
import re
import threading
import time
from pathlib import Path

from ..api_errors import APIError

TIMEOUT = 30.0
MAX_ITEMS = 20
MAX_REPORT_BYTES = 4_400_000
DRAFT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
QUEUED_CODES = {"unreachable", "untrusted_server", "busy", "not_configured", "rate_limited", "error"}


class SendError(Exception):
    """The service refused or couldn't be reached."""

    def __init__(self, code, status, message):
        super().__init__(message)
        self.code, self.status = code, status


def _post(path, token, body, timeout=TIMEOUT):
    from ..share import ShareError, call
    try:
        status, value = call("POST", path, token, body, timeout, limit=64_000)
    except ShareError as error:
        if error.code == "untrusted_server":
            raise SendError("untrusted_server", 503, "Mobster couldn't verify its server, so it didn't send.") from None
        raise SendError("unreachable", 503, "Mobster couldn't reach its server.") from None
    if 200 <= status < 300:
        return value
    message = value.get("message") if isinstance(value.get("message"), str) else None
    code = value.get("error") if isinstance(value.get("error"), str) else "error"
    if status == 401:
        raise SendError("session", 401, "Sign in to Mobster again to send this.")
    if status == 429:
        raise SendError("rate_limited", 429, message or "Mobster's server is busy. It sends this later.")
    if status == 413:
        raise SendError("too_large", 413, message or "That report is too large. Leave out the screenshot.")
    if status in (400, 415):
        raise SendError("invalid", 400, message or "Mobster's server refused that report.")
    # A server without the route yet (404) or not set up for it (503 not_configured): it waits and goes later.
    if status == 404 or status == 503 and code == "not_configured":
        raise SendError("not_configured", 503, "Mobster's server isn't taking reports yet. It sends this later.")
    raise SendError("busy" if status in (502, 503, 504) else "error", 503,
                    message or "Mobster's server couldn't take the report. It sends this later.")


def submit(report, token):
    """Posts one report: {reference, kind, createdAt, forwarded}."""
    body = json.dumps(report, ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_REPORT_BYTES:
        raise SendError("too_large", 413, "That report is over 4.4 MB. Leave out the screenshot or crop it.")
    value = _post("/api/feedback", token, body)
    feedback = value.get("feedback")
    if not isinstance(feedback, dict) or not isinstance(feedback.get("reference"), str):
        raise SendError("error", 502, "Mobster's server answered without a reference. It sends this again later.")
    return feedback


class Outbox:
    """Reports waiting to be sent: a folder of JSON files, or memory."""

    def __init__(self, directory=None):
        self.directory = Path(directory) if directory else None
        self.memory = {}
        self.lock = threading.Lock()

    def _path(self, draft_id):
        return self.directory / f"{draft_id}.json"

    def _write(self, item):
        if self.directory is None:
            self.memory[item["draftId"]] = item
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        path = self._path(item["draftId"])
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(item, handle, ensure_ascii=False)
        os.replace(tmp, path)

    def items(self):
        """Every waiting report, oldest first."""
        if self.directory is None:
            found = list(self.memory.values())
        else:
            found = []
            for path in sorted(self.directory.glob("*.json")) if self.directory.is_dir() else []:
                try:
                    item = json.loads(path.read_text("utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(item, dict) and DRAFT_ID.fullmatch(str(item.get("draftId", ""))):
                    found.append(item)
        return sorted(found, key=lambda item: item.get("queuedAt", 0))

    def put(self, report, error):
        with self.lock:
            waiting = self.items()
            if len(waiting) >= MAX_ITEMS and not any(item["draftId"] == report["clientId"] for item in waiting):
                raise APIError(f"{MAX_ITEMS} reports are already waiting to send. Delete one, or send them first.",
                               409, "outbox_full")
            self._write({"draftId": report["clientId"], "queuedAt": time.time(), "attempts": 1, "lastError": error,
                         "report": report})

    def touch(self, item, error):
        with self.lock:
            self._write({**item, "attempts": int(item.get("attempts", 0)) + 1, "lastError": error})

    def remove(self, draft_id):
        with self.lock:
            if self.directory is None:
                return self.memory.pop(draft_id, None) is not None
            try:
                self._path(draft_id).unlink()
                return True
            except FileNotFoundError:
                return False

    def public(self):
        """What the app lists: no attachments, no description."""
        return [{"draftId": item["draftId"], "kind": item["report"].get("kind"), "title": item["report"].get("title"),
                 "queuedAt": int(item.get("queuedAt", 0) * 1000), "attempts": item.get("attempts", 0),
                 "lastError": item.get("lastError"), "screenshot": bool(item["report"].get("screenshot")),
                 "diagnostics": bool(item["report"].get("diagnostics"))} for item in self.items()]


_outboxes = {}
_outboxes_lock = threading.Lock()


def outbox_for(runtime):
    """The runtime's outbox: under its data folder when it has one (bundle.data_dir), else in memory."""
    from .bundle import data_dir
    with _outboxes_lock:
        box = _outboxes.get(id(runtime))
        if box is None or box[0] is not runtime:
            directory = data_dir(runtime)
            box = (runtime, Outbox(directory / "feedback" / "outbox" if directory else None))
            _outboxes[id(runtime)] = box
        return box[1]


def send(runtime, report, token):
    """Sends ``report`` now, or keeps it in the outbox when the service can't take it yet. The service answers a second
    send of the same draft (``clientId``) with the same reference, so sending again never makes a second report."""
    box = outbox_for(runtime)
    try:
        feedback = submit(report, token)
    except SendError as error:
        if error.code in QUEUED_CODES:
            box.put(report, error.code)
            return {"queued": True, "draftId": report["clientId"], "reason": str(error)}
        raise APIError(str(error), error.status, error.code) from None
    finally:
        token = None
    box.remove(report["clientId"])
    return {"feedback": feedback, "queued": False}


def flush(runtime, token):
    """Tries every waiting report, oldest first; stops at the first one the service can't take now."""
    box = outbox_for(runtime)
    sent, error = [], None
    try:
        for item in box.items():
            try:
                feedback = submit(item["report"], token)
            except SendError as problem:
                if problem.code in QUEUED_CODES:
                    box.touch(item, problem.code)
                    error = str(problem)
                    break
                if problem.code == "session":
                    error = str(problem)
                    break
                # The service refuses this report for good (a field it won't take): it would wait forever.
                box.remove(item["draftId"])
                error = f"Mobster couldn't send “{item['report'].get('title', '')}”: {problem}"
                continue
            box.remove(item["draftId"])
            sent.append({"draftId": item["draftId"], "reference": feedback["reference"]})
    finally:
        token = None
    out = {"sent": sent, "waiting": len(box.items())}
    if error:
        out["error"] = error
    return out
