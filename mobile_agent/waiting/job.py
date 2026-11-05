"""The "Who's waiting on me" job: Messages read, ranked and drafted on this Mac, never by a cloud model."""

import os
import sqlite3
import threading
import time
import uuid

from .. import result_blocks
from ..api_errors import APIError
from . import contacts as contact_book
from . import drafts, messages, rank

BUNDLE = "com.apple.MobileSMS"
APP = {"id": "messages", "name": "Messages", "bundleId": BUNDLE}
GOAL = "Who's waiting on me?"
MAX_LAST = 200
MISSING = ("Mobster can't find your Messages history on this Mac. Turn on Messages in iCloud in Messages settings, "
           "then try again.")
DENIED = (
    "Mobster needs Full Disk Access to read your Messages on this Mac. Turn it on in System Settings, then try again.")
UNREADABLE = (
    "Mobster couldn't read the Messages database on this Mac, so nothing was changed. Update Mobster, then try again.")
BUSY = "Mobster is already looking through your Messages. Wait for it to finish."
_lock = threading.Lock()


def ago(seconds):
    """'3 hours ago', '1 day ago', '2 weeks ago'."""
    minutes = max(int(seconds // 60), 0)
    for unit, size in (("week", 7 * 24 * 60), ("day", 24 * 60), ("hour", 60)):
        if minutes >= size:
            count = minutes // size
            return f"{count} {unit}{'' if count == 1 else 's'} ago"
    return "just now"


def _short(text, limit=MAX_LAST):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"


def block_for(items, names, written, *, title=None):
    """The review_queue block for ranked ``items``: ``names`` is {chat id: name}, ``written`` {str(chat id): draft}."""
    out = []
    for item in items:
        name = names.get(item.chat)
        when = ago(item.age_s)
        quote = _short(item.last.text)
        if item.bucket == "promised":
            context = f"You said this {when}. They haven't written since."
            quote = f"You wrote: {quote}"
        elif item.bucket == "asked":
            context = f"Asked you {when}"
        else:
            context = f"Wrote {when}"
        row = {"id": f"c{item.chat}", "bucket": item.bucket, "to": name or item.handle,
               "app": APP["name"], "bundleId": BUNDLE, "context": context, "lastMessage": quote,
               "draft": written.get(str(item.chat)) or ""}
        if name:
            row["handle"] = item.handle
        if not row["draft"]:
            # No draft because the on-device model is off or its draft was dropped: the person writes the reply, and
            # Approve takes their typed words (result_blocks.approval_item).
            row["yourWords"] = result_blocks.YOUR_WORDS
        out.append(row)
    buckets = [{"id": bucket, "label": rank.LABELS[bucket]} for bucket in rank.BUCKETS
               if any(item.bucket == bucket for item in items)]
    return result_blocks.review_queue(out, title=title, buckets=buckets, action="send_message", id="waiting")


def _plural(count, one, many):
    return f"{count} {one if count == 1 else many}"


def answer_for(items, drafted, availability):
    """The one line the result says: counts only, no names and no message text."""
    if not items:
        return (
            "Nobody is waiting on you in your Messages. Group chats, short codes and conversations older than 30 days "
            "aren't counted.")
    counts = rank.counts(items)
    parts = []
    if counts["asked"]:
        parts.append(f"{_plural(counts['asked'], 'person asked', 'people asked')} you something")
    if counts["waiting"]:
        parts.append(f"{_plural(counts['waiting'], 'person is', 'people are')} waiting on a reply")
    if counts["promised"]:
        parts.append(f"{_plural(counts['promised'], 'promise', 'promises')} to keep")
    line = ", ".join(parts).capitalize() + "."
    if availability.get("available"):
        line += f" {_plural(drafted, 'draft', 'drafts')} to approve. Nothing was sent."
    else:
        line += " " + drafts.reason_text(availability.get("reason")) + " Nothing was sent."
    return line


def read(now, path=None):
    """Ranked items from chat.db with the errors the app shows in plain words."""
    target = path or messages.DEFAULT_PATH
    if not os.path.exists(target):
        raise APIError(MISSING, 404, "messages_missing")
    if not messages.columns_ok(target):
        # A file we can see but can't open is Full Disk Access; one we open but that lacks the columns is a newer macOS.
        try:
            messages.connect(target).close()
        except sqlite3.Error:
            raise APIError(DENIED, 403, "full_disk_access") from None
        raise APIError(UNREADABLE, 422, "messages_unreadable")
    try:
        chats = messages.read_chats(target, now)
    except sqlite3.Error:
        raise APIError(DENIED, 403, "full_disk_access") from None
    return rank.rank(chats, now)


def run(runtime, *, now=None, path=None, book=None, runner=None, key=None):
    """Make the finished task. Returns the Run. ``runner`` and ``book`` are for tests."""
    if key:
        # A retried start (a lost answer) returns the task the first one made, without reading or drafting again.
        prior = runtime.journal.lookup(key[:128])
        if prior and prior[0] in runtime.runs:
            return runtime.runs[prior[0]]
    if not _lock.acquire(blocking=False):
        raise APIError(BUSY, 409, "waiting_busy")
    try:
        now = time.time() if now is None else now
        items = read(now, path)
        availability = drafts.status(runner)
        written = drafts.draft_all(items, runner=runner) if availability.get("available") else {}
        book = book if book is not None else contact_book.ContactBook.load()
        names = {item.chat: book.name(item.handle) for item in items}
        total = len(items)
        title = f"Who's waiting on you: {_plural(total, 'person', 'people')}"
        blocks = [block_for(items, names, written, title=title)] if items else []
        answer = answer_for(items, len(written), availability)
        summary = {"event": "result", "status": "completed", "outcome": "done", "answer": answer, "reason": None,
                   "independently_verified": True, "actions": 0, "decisions": 0, "elapsed_ms": 0, "model": None,
                   "proof": [{"quote": "Read your Messages on this Mac. Nothing was sent.", "app": APP["name"]}],
                   **({"blocks": {"v": 1, "blocks": blocks}} if blocks else {})}
        app = next((dict(one) for one in runtime.apps_for(None) if one.get("bundleId") == BUNDLE), dict(APP))
        return runtime.compose_result(app=app, goal=GOAL, summary=summary,
                                      idempotency_key=(key or f"waiting-{uuid.uuid4().hex}")[:128], origin="app")
    finally:
        _lock.release()
