"""Threads and their items in the run journal (schema ``threads`` v1, SPEC §3.2)."""

import json
import math
import re
import sqlite3
import time
import uuid

from ..api_errors import APIError
from ..journal import JournalError

SCHEMA = "threads"
VERSION = 1

MAX_ITEMS = 2000                  # per thread, every kind
RESERVE = 20                      # kept free for the items a running task still adds (questions, results)
MAX_THREAD_BYTES = 8_000_000      # per thread
MAX_SINK_ITEM_BYTES = 8_000       # an item another track posts (post_thread_item)
TITLE_CHARS = 80
MAX_RULES = 10
RULES_CHARS = 1_200               # "Rules you gave in this conversation", newest kept
LIST_LIMIT = 100
REQUEST_LIMIT = 10_000
KIND = re.compile(r"[a-z_]{1,40}")
ID = re.compile(r"[a-f0-9]{12}")
CORE_KINDS = frozenset({"user", "run", "clarify"})
STATUSES = ("idle", "running", "waiting")
FULL = "This conversation is full. Start a new one."


NOT_FOUND = "That conversation doesn't exist here. It may have been deleted, or it's in another Mobster."


class ThreadFull(APIError):
    """The thread has no room for what was asked (MAX_ITEMS or MAX_THREAD_BYTES): 409 thread_full."""

    def __init__(self, message=FULL):
        super().__init__(message, 409, "thread_full")


class ThreadMissing(APIError):
    """The thread doesn't exist (deleted meanwhile): 404 thread_not_found."""

    def __init__(self, message=NOT_FOUND):
        super().__init__(message, 404, "thread_not_found")


def new_id():
    return uuid.uuid4().hex[:12]


def encode(value):
    return json.dumps(value, allow_nan=False, separators=(",", ":"), ensure_ascii=False)


def now_ms():
    return time.time() * 1000


def request_unknown():
    return APIError("Mobster accepted this request but can't confirm its outcome. Check the conversation "
                    "and task history before starting another request.", 409, "request_outcome_unknown")


def request_result(response):
    """Replay a stored success or known refusal; unresolved intent never authorizes another dispatch."""
    if response is None:
        raise request_unknown()
    if response["status"] >= 400:
        error = response["response"]
        if "code" not in error:
            raise ValueError(error["error"])
        raise APIError(error["error"], response["status"], error["code"],
                       **{key: value for key, value in error.items() if key not in ("error", "code")})
    return response


def failed_request(error):
    body = {"error": str(error)}
    if isinstance(error, APIError):
        body.update(code=error.code, **error.details)
    return {"status": error.status if isinstance(error, APIError) else 400, "response": body}


def cursor_parts(value):
    """(stored timestamp, optional thread id) from a next cursor or a legacy millisecond timestamp."""
    timestamp, separator, thread_id = str(value).partition(":")
    try:
        timestamp = float(timestamp) / 1000
        if not math.isfinite(timestamp) or separator and not ID.fullmatch(thread_id):
            raise ValueError
    except ValueError:
        raise ValueError("before is a next cursor or an updatedAt from a thread (milliseconds)") from None
    return timestamp, thread_id if separator else None


def migrate(connection, from_version):
    """journal.register_schema's migration: v0 -> v1 creates the tables. Runs inside BEGIN IMMEDIATE."""
    if from_version < 1:
        connection.execute("""CREATE TABLE IF NOT EXISTS threads(
            id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at REAL NOT NULL)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS thread_items(
            thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
            seq INTEGER NOT NULL, data TEXT NOT NULL,
            id TEXT NOT NULL, kind TEXT NOT NULL, run_id TEXT, bytes INTEGER NOT NULL,
            PRIMARY KEY(thread_id, seq))""")
        connection.execute("CREATE INDEX IF NOT EXISTS threads_updated ON threads(updated_at DESC)")
        connection.execute("CREATE INDEX IF NOT EXISTS thread_items_run ON thread_items(thread_id, run_id)")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS thread_items_id ON thread_items(id)")
        # Idempotency-Key -> the answer a message got, so a retried post never starts a second task.
        connection.execute("""CREATE TABLE IF NOT EXISTS thread_requests(
            key TEXT PRIMARY KEY, thread_id TEXT, fingerprint TEXT NOT NULL, response TEXT NOT NULL,
            created_at REAL NOT NULL)""")


def public_item(item):
    """The item as the API and the bus show it: without its private ("_") keys."""
    return {key: value for key, value in item.items() if not key.startswith("_")}


def title_from(text):
    """A thread's title from its first message: one line, at most 80 characters, cut at a word."""
    text = " ".join(str(text or "").split())
    if len(text) <= TITLE_CHARS:
        return text
    cut = text[:TITLE_CHARS - 1]
    space = cut.rfind(" ")
    if space >= TITLE_CHARS // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:.") + "…"


class ThreadStore:
    def __init__(self, journal):
        self.journal = journal

    def _read(self, sql, args=()):
        with self.journal.lock:
            if self.journal.connection is None:
                raise JournalError("The run journal is closed")
            try:
                return self.journal.connection.execute(sql, args).fetchall()
            except sqlite3.Error:
                raise JournalError("Mobster could not read its conversations") from None

    def create_thread(self, *, title=None, device=None, device_name=None, thread_id=None):
        stamp = now_ms()
        thread = {"id": thread_id or new_id(), "title": title_from(title) or "New conversation",
                  "createdAt": stamp, "updatedAt": stamp, "device": device, "deviceName": device_name,
                  "archived": False, "pinned": False, "status": "idle", "lastRunId": None,
                  "rules": []}
        with self.journal.transaction() as connection:
            connection.execute("INSERT INTO threads(id, data, updated_at) VALUES (?,?,?)",
                               (thread["id"], encode(thread), stamp / 1000))
        return thread

    def get(self, thread_id):
        if not isinstance(thread_id, str) or not ID.fullmatch(thread_id):
            return None
        rows = self._read("SELECT data FROM threads WHERE id=?", (thread_id,))
        return json.loads(rows[0][0]) if rows else None

    def list(self, *, limit=50, before=None, device=None, query=None, archived=False):
        """(threads newest first, the ``before`` cursor for the next page or None)."""
        limit = max(1, min(LIST_LIMIT, int(limit)))
        where, args = [], []
        if before is not None:
            timestamp, thread_id = cursor_parts(before)
            if thread_id is None:
                where.append("updated_at < ?")
                args.append(timestamp)
            else:
                where.append("(updated_at, id) < (?, ?)")
                args.extend((timestamp, thread_id))
        sql = "SELECT data FROM threads" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY updated_at DESC, id DESC"
        matching = None
        if query:
            # Search the words themselves: JSON escaping changes quotes and backslashes, and a run's other fields
            # include private notes and metadata that aren't part of conversation search.
            wanted = query.casefold()
            matching = set()
            for thread_id, kind, data in self._read(
                    "SELECT thread_id, kind, data FROM thread_items WHERE kind IN ('user', 'run')"):
                item = json.loads(data)
                if wanted in str(item.get("text" if kind == "user" else "goal") or "").casefold():
                    matching.add(thread_id)
        out = []
        for (data,) in self._read(sql, args):
            thread = json.loads(data)
            if archived != "all" and bool(thread.get("archived")) != bool(archived):
                continue
            if device is not None and device not in (thread.get("device"), thread.get("deviceName")):
                continue
            if matching is not None and thread["id"] not in matching and \
                    query.casefold() not in str(thread.get("title") or "").casefold():
                continue
            out.append(thread)
            if len(out) > limit:
                break
        more = len(out) > limit
        out = out[:limit]
        cursor = f"{out[-1]['updatedAt']}:{out[-1]['id']}" if more and out else None
        return out, cursor

    def update_thread(self, thread_id, changes, *, touch=True):
        """Merge ``changes`` into the thread; returns it, or None when it doesn't exist."""
        with self.journal.transaction() as connection:
            row = connection.execute("SELECT data FROM threads WHERE id=?", (thread_id,)).fetchone()
            if row is None:
                return None
            thread = json.loads(row[0])
            thread.update(changes)
            if touch:
                thread["updatedAt"] = max(now_ms(), thread.get("updatedAt") or 0)
            connection.execute("UPDATE threads SET data=?, updated_at=? WHERE id=?",
                               (encode(thread), thread["updatedAt"] / 1000, thread_id))
        return thread

    def delete_thread(self, thread_id):
        """Remove the thread and its items. Returns True when it existed."""
        with self.journal.transaction() as connection:
            deleted = connection.execute("DELETE FROM threads WHERE id=?", (thread_id,)).rowcount == 1
            connection.execute("UPDATE thread_requests SET response=? WHERE thread_id=? AND response != 'null'", (
                encode({"status": 410, "response": {"error": "That conversation was deleted; it was not started again",
                                                    "code": "thread_expired"}}), thread_id))
        return deleted

    def threads_with_status(self, statuses):
        rows = self._read("SELECT data FROM threads")
        return [t for t in (json.loads(r[0]) for r in rows) if t.get("status") in statuses]

    def items(self, thread_id, after=None):
        """The thread's items in order (``after``: only those with a larger seq)."""
        rows = self._read("SELECT data FROM thread_items WHERE thread_id=? AND seq>? ORDER BY seq",
                          (thread_id, -1 if after is None else int(after)))
        return [json.loads(r[0]) for r in rows]

    def run_ids(self, thread_id):
        rows = self._read("SELECT DISTINCT run_id FROM thread_items WHERE thread_id=? AND kind='run' "
                          "AND run_id IS NOT NULL", (thread_id,))
        return [r[0] for r in rows]

    def find(self, thread_id, *, kind=None, run_id=None, match=None):
        """The items of ``kind`` (and ``run_id``) for which ``match(item)`` holds, in order."""
        sql, args = "SELECT data FROM thread_items WHERE thread_id=?", [thread_id]
        if kind is not None:
            sql += " AND kind=?"
            args.append(kind)
        if run_id is not None:
            sql += " AND run_id=?"
            args.append(run_id)
        items = [json.loads(r[0]) for r in self._read(sql + " ORDER BY seq", args)]
        return [item for item in items if match is None or match(item)]

    def usage(self, thread_id):
        """(item count, bytes) of the thread."""
        row = self._read("SELECT count(*), coalesce(sum(bytes), 0) FROM thread_items WHERE thread_id=?",
                         (thread_id,))[0]
        return int(row[0]), int(row[1])

    def append(self, thread_id, items, *, unless=None):
        """Add ``items`` (dicts with at least "kind") in one transaction, after checking there is room for them."""
        items = [dict(item) for item in items]
        encoded, problem = [], None
        # Journal.transaction turns any exception into a JournalError: decide inside, raise after.
        with self.journal.transaction() as connection:
            if connection.execute("SELECT 1 FROM threads WHERE id=?", (thread_id,)).fetchone() is None:
                problem = ThreadMissing()
            elif unless is not None and unless(self._existing(connection, thread_id, items)):
                return None
            else:
                count, size, last = connection.execute(
                    "SELECT count(*), coalesce(sum(bytes), 0), coalesce(max(seq), -1) FROM thread_items "
                    "WHERE thread_id=?", (thread_id,)).fetchone()
                stamp = now_ms()
                for offset, item in enumerate(items):
                    if not (isinstance(item.get("id"), str) and ID.fullmatch(item["id"])):
                        item["id"] = new_id()
                    item["seq"] = last + 1 + offset
                    item.setdefault("at", stamp)
                    data = encode(item)
                    encoded.append((item, data, len(data.encode())))
                if count + len(items) > MAX_ITEMS or \
                        size + sum(e[2] for e in encoded) > MAX_THREAD_BYTES:
                    problem = ThreadFull()
                else:
                    for item, data, size_bytes in encoded:
                        connection.execute(
                            "INSERT INTO thread_items(thread_id, seq, data, id, kind, run_id, bytes) "
                            "VALUES (?,?,?,?,?,?,?)",
                            (thread_id, item["seq"], data, item["id"], item["kind"], item.get("runId"), size_bytes))
                    row = connection.execute("SELECT data FROM threads WHERE id=?", (thread_id,)).fetchone()
                    thread = json.loads(row[0])
                    thread["updatedAt"] = max(stamp, thread.get("updatedAt") or 0)
                    connection.execute("UPDATE threads SET data=?, updated_at=? WHERE id=?",
                                       (encode(thread), thread["updatedAt"] / 1000, thread_id))
        if problem is not None:
            raise problem
        return [e[0] for e in encoded]

    @staticmethod
    def _existing(connection, thread_id, items):
        """The thread's stored items of the same kinds (and runs) as ``items``."""
        kinds = sorted({item["kind"] for item in items})
        runs = sorted({item.get("runId") for item in items if item.get("runId")})
        sql = f"SELECT data FROM thread_items WHERE thread_id=? AND kind IN ({','.join('?' * len(kinds))})"
        args = [thread_id, *kinds]
        if runs:
            sql += f" AND run_id IN ({','.join('?' * len(runs))})"
            args += runs
        return [json.loads(r[0]) for r in connection.execute(sql + " ORDER BY seq", args)]

    def update_item(self, thread_id, item_id, changes, *, kinds=None, max_bytes=None):
        """Merge ``changes`` into an item (never its id, seq, kind, at or runId)."""
        changes = {k: v for k, v in dict(changes).items() if k not in ("id", "seq", "kind", "at", "runId")}
        problem = None
        with self.journal.transaction() as connection:
            row = connection.execute("SELECT data, bytes FROM thread_items WHERE thread_id=? AND id=?",
                                     (thread_id, item_id)).fetchone()
            if row is None:
                return None
            item = json.loads(row[0])
            if kinds is not None and item.get("kind") not in kinds:
                return None
            item.update(changes)
            data = encode(item)
            size = len(data.encode())
            total = connection.execute("SELECT coalesce(sum(bytes), 0) FROM thread_items WHERE thread_id=?",
                                       (thread_id,)).fetchone()[0]
            if max_bytes is not None and size > max_bytes:
                problem = ValueError(f"A thread item is at most {max_bytes:,} bytes")
            elif total - row[1] + size > MAX_THREAD_BYTES:
                problem = ThreadFull()
            else:
                connection.execute("UPDATE thread_items SET data=?, bytes=? WHERE thread_id=? AND id=?",
                                   (data, size, thread_id, item_id))
        if problem is not None:
            raise problem
        return item

    def request(self, key):
        """(thread id, fingerprint, response) saved under a key, or None. A null response is unresolved intent."""
        rows = self._read("SELECT thread_id, fingerprint, response FROM thread_requests WHERE key=?", (key,))
        return (rows[0][0], rows[0][1], json.loads(rows[0][2])) if rows else None

    def remember(self, key, thread_id, fingerprint, response):
        with self.journal.transaction() as connection:
            if connection.execute("SELECT 1 FROM thread_requests WHERE key=?", (key,)).fetchone() is None:
                excess = connection.execute("SELECT count(*) FROM thread_requests").fetchone()[0] - REQUEST_LIMIT + 1
                if excess > 0:
                    # Never evict an unresolved intent: its effect may already have reached the phone.
                    connection.execute("DELETE FROM thread_requests WHERE rowid IN (SELECT rowid FROM thread_requests "
                                       "WHERE response != 'null' ORDER BY created_at, rowid LIMIT ?)", (excess,))
                    if connection.execute("SELECT count(*) FROM thread_requests").fetchone()[0] >= REQUEST_LIMIT:
                        raise JournalError(
                            "Conversation request journal is full; check unresolved requests before continuing")
            connection.execute("INSERT OR REPLACE INTO thread_requests(key, thread_id, fingerprint, response, "
                               "created_at) VALUES (?,?,?,?,?)",
                               (key, thread_id, fingerprint, encode(response), time.time()))
