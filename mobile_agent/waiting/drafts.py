"""Drafted replies from Apple's on-device model, through the ``draft-reply`` helper (helper_draft_reply.swift)."""

import json
import re
import subprocess
import time

from .. import native_helpers

HELPER = "draft_reply"
PER_DRAFT_S = 15.0
TOTAL_S = 75.0
MAX_DRAFT = 280
REASONS = {
    "apple_intelligence_off": "Apple Intelligence is off on this Mac, so you'll write your own replies. Turn it on in "
    "System Settings to get drafts.",
    "device_not_eligible": "This Mac can't run Apple's on-device model, so you'll write your own replies.",
    "model_not_ready": "Apple's on-device model is still downloading, so you'll write your own replies this time.",
    "needs_macos_26": "Drafts need macOS 26 or later, so you'll write your own replies.",
}
UNAVAILABLE = "Drafts aren't available on this Mac right now, so you'll write your own replies."

# A draft that commits the person: a date, a time, money, a day or month, a yes or no to a request.
_COMMITS = re.compile(
    r"[$€£¥]|\b\d{1,2}[:.]\d{2}\b|\b\d{1,2}\s?(am|pm)\b|\b\d{1,2}/\d{1,2}\b|\b\d+\s?(dollars|bucks|usd|eur|euros)\b"
    r"|\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|tonight|today|january|february|march|april"
    r"|may \d|june|july|august|september|october|november|december)\b"
    r"|^(yes|yeah|yep|sure|no|nope|absolutely|definitely)\b|\bi('ll| will) (be there|come|pay|send|do it|take it)\b"
    r"|\bcount me in\b|\bi('m| am) in\b", re.I)


def reason_text(reason):
    return REASONS.get(reason, UNAVAILABLE)


def _run_helper():
    """The helper's path, or None when this Mac has none and can't build one."""
    path = native_helpers.find(HELPER)
    if path is not None:
        return str(path)
    try:
        return str(native_helpers.build(HELPER))
    except (LookupError, FileNotFoundError, OSError, subprocess.SubprocessError):
        return None


def status(runner=None):
    """{"available": bool, "reason": str}: whether the on-device model can draft. ``runner`` is for tests."""
    if runner is not None:
        return runner.status()
    helper = _run_helper()
    if helper is None:
        return {"available": False, "reason": "no_helper"}
    try:
        done = subprocess.run([helper, "status"], capture_output=True, text=True, timeout=20)
        data = json.loads((done.stdout or "").strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return {"available": False, "reason": "unavailable"}
    return {"available": bool(data.get("available")), "reason": str(data.get("reason") or "")}


def clean(text):
    """One tidy line of at most 280 characters, or '' when there isn't one."""
    text = " ".join(str(text or "").replace("“", '"').replace("”", '"').split()).strip(' "')
    if len(text) > MAX_DRAFT:
        text = text[:MAX_DRAFT].rsplit(" ", 1)[0].rstrip(" ,;:")
    return text


def acceptable(text):
    """A draft that commits the person to nothing (no date, time, money, yes or no on their behalf)."""
    return bool(text) and not _COMMITS.search(text)


def payload(items):
    """The helper's request for ``items`` (rank.Item): each item's id, situation and last 8 messages."""
    return {"items": [{"id": str(item.chat), "kind": item.bucket,
                       "messages": [{"from": "you" if m.mine else "them", "text": m.text} for m in item.context[-8:]]}
                      for item in items]}


def draft_all(items, runner=None, per_draft=PER_DRAFT_S, total=TOTAL_S, clock=time.monotonic):
    """{str(chat id): draft} for the items that got one. Never raises; whatever fails is simply missing."""
    if not items:
        return {}
    request = payload(items)
    if runner is not None:
        lines = runner.run(request, per_draft, total)
    else:
        lines = _stream(request, per_draft, total, clock)
    out = {}
    for line in lines:
        if isinstance(line, dict) and line.get("ok") and isinstance(line.get("id"), str):
            text = clean(line.get("draft"))
            if acceptable(text):
                out[line["id"]] = text
    return out


def _stream(request, per_draft, total, clock):
    helper = _run_helper()
    if helper is None:
        return
    deadline = clock() + total
    try:
        process = subprocess.Popen([helper, "run", "--timeout", str(per_draft)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except OSError:
        return
    try:
        process.stdin.write(json.dumps(request))  # message text goes on stdin, never on a command line or in a log
        process.stdin.close()
        import selectors
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        while clock() < deadline:
            if not selector.select(timeout=1.0):
                if process.poll() is not None:
                    break
                continue
            line = process.stdout.readline()
            if not line:
                break
            try:
                yield json.loads(line)
            except ValueError:
                continue
    except (OSError, ValueError):
        return
    finally:
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.SubprocessError:
            pass
