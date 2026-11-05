"""What Mobster remembers, in one SQLite file on this Mac (SPEC §3.3).

``user_data_dir()/memory/memory.sqlite3``: the folder is 0700 and the file 0600. WAL and a short-lived connection
per call, so the Mac app's agent, the terminal UI, `mobster memory` and MCP share it safely. ``MOBSTER_MEMORY_DIR``
moves it (tests). With neither that nor a home folder (a test that cleared the environment), there is no store:
nothing is read or written, and every caller treats memory as empty and off.

A fact has a kind, a source, a scope, a confidence and a fingerprint (model.py). Two ways in:

- ``add_fact`` and ``resolve_proposal``: what the person wrote, or accepted from a suggestion of their own words.
- ``learn`` (and ``stage`` + ``commit_run``): what Mobster learned from use. A run stages what it learned, and the
  facts are written only when the run finishes with a result the person didn't dispute (``commit_run``). They show
  under "New since you last looked" until the person keeps or forgets them.

Forgetting a fact removes it and keeps its fingerprint for 90 days, so nothing relearns it. Never secrets, whoever
writes: a fact or a suggestion that ``secret_filter.is_secret`` flags is refused. Errors are ``api_errors.APIError``
with a plain sentence, so the HTTP routes and the CLI say the same thing.
"""

import json
import os
import sqlite3
import stat
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from ..api_errors import APIError
from ..catalog import app_name
from ..paths import user_data_dir
from . import model
from .model import FACT_CHARS as FACT_CHARS
from .model import (
    clean_confidence,
    clean_key,
    clean_kind,
    clean_ref,
    clean_scope,
    clean_source,
    clean_text,
    digest,
    fingerprint,
    scope_parts,
)
from .model import normalize as normalize

SCHEMA_VERSION = 2
FILE = "memory.sqlite3"

MAX_FACTS = 1000          # all facts; at the limit the least used learned fact makes room, a person's never does
MAX_PINNED = 10           # pinned facts ship with every task, so they're few and short
PINNED_CHARS = 1000
MAX_PENDING = 50          # suggestions waiting for an answer; the oldest expire first
CONFIRM = "delete everything"
PROPOSAL_DAYS = 14        # a suggestion nobody answered expires
QUIET_DAYS = 90           # a suggestion answered "Not now", or a fact forgotten, isn't offered again for this long
STALE_DAYS = 90           # a fact nobody used for this long is offered for removal
STAGED_DAYS = 2           # what a run learned is dropped if the run never finishes
USE_DAYS = 30             # how long the facts a run used stay readable from the run
DAY = 86400.0

NEWER = "This was saved by a newer Mobster. Update Mobster to use it."
UNAVAILABLE = "Mobster can't open what it remembers on this Mac right now. Try again."
GONE = "Mobster doesn't remember that anymore."

SETTINGS = {"useInTasks": True, "suggest": True}      # suggest also turns learning from use on and off
PROPOSAL_STATES = ("pending", "accepted", "dismissed", "expired")

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts(id TEXT PRIMARY KEY, scope TEXT NOT NULL,
  text TEXT NOT NULL CHECK(length(text) <= 300), kind TEXT NOT NULL, source TEXT NOT NULL,
  ref TEXT NOT NULL DEFAULT '{}', run_id TEXT, confidence REAL NOT NULL DEFAULT 1, key TEXT,
  fingerprint TEXT NOT NULL UNIQUE, pinned INTEGER NOT NULL DEFAULT 0, kept INTEGER NOT NULL DEFAULT 0,
  edited INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL, last_used_at REAL,
  uses INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS facts_run ON facts(run_id);
CREATE TABLE IF NOT EXISTS forgotten(fingerprint TEXT PRIMARY KEY, forgotten_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS staged(id TEXT PRIMARY KEY, run_id TEXT NOT NULL, payload TEXT NOT NULL,
  created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS staged_run ON staged(run_id);
CREATE TABLE IF NOT EXISTS run_use(run_id TEXT NOT NULL, fact_id TEXT NOT NULL, at REAL NOT NULL,
  PRIMARY KEY(run_id, fact_id));
CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, path TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY, text TEXT NOT NULL, scope TEXT NOT NULL, thread_id TEXT,
  run_id TEXT, status TEXT NOT NULL CHECK(status IN ('pending','accepted','dismissed','expired')),
  created_at REAL, resolved_at REAL, digest TEXT NOT NULL, pin INTEGER NOT NULL DEFAULT 0, item_id TEXT,
  fact_id TEXT);
CREATE INDEX IF NOT EXISTS proposals_status ON proposals(status, created_at);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def memory_dir():
    """The store's folder, or None when there is nowhere safe to keep it (no home folder in the environment)."""
    value = os.environ.get("MOBSTER_MEMORY_DIR", "").strip()
    if value:
        return Path(value).expanduser()
    if os.environ.get("HOME") or getattr(sys, "frozen", False):
        return user_data_dir() / "memory"
    return None


def default_store():
    """The store at ``memory_dir()``, or None. Creating it does no I/O."""
    folder = memory_dir()
    return MemoryStore(folder) if folder is not None else None


def new_id():
    return uuid.uuid4().hex[:12]


def millis(seconds):
    return None if seconds is None else round(seconds * 1000)


def _scope_view(scope):
    kind, ident = scope_parts(scope)
    return {"kind": kind, "id": ident, "app": ({"bundleId": ident, "name": app_name(ident) or ident}
                                                if kind == "app" else None)}


def _fact(row, looked=0.0, now=0.0):
    """A row as the API shows it. ``new``: learned since the person last looked and not kept. ``stale``: unpinned,
    not kept and unused for 90 days, so offered for removal."""
    where = _scope_view(row["scope"])
    learned = row["source"] != "user"
    return {"id": row["id"], "text": row["text"], "scope": row["scope"], "scopeKind": where["kind"],
            "scopeId": where["id"], "app": where["app"], "kind": row["kind"], "source": row["source"],
            "ref": json.loads(row["ref"] or "{}"), "confidence": row["confidence"], "pinned": bool(row["pinned"]),
            "kept": bool(row["kept"]), "edited": bool(row["edited"]), "key": row["key"],
            "new": learned and not row["kept"] and row["created_at"] > looked,
            "stale": not row["pinned"] and not row["kept"]
                     and (row["last_used_at"] or row["created_at"]) < now - STALE_DAYS * DAY,
            "createdAt": millis(row["created_at"]), "updatedAt": millis(row["updated_at"]),
            "lastUsedAt": millis(row["last_used_at"]), "uses": row["uses"]}


def _proposal(row):
    where = _scope_view(row["scope"])
    return {"id": row["id"], "text": row["text"], "scope": row["scope"], "app": where["app"],
            "threadId": row["thread_id"], "runId": row["run_id"], "status": row["status"], "pin": bool(row["pin"]),
            "createdAt": millis(row["created_at"]), "resolvedAt": millis(row["resolved_at"]),
            "expiresAt": millis(row["created_at"] + PROPOSAL_DAYS * DAY) if row["status"] == "pending" else None,
            "factId": row["fact_id"]}


def prepare(text, kind, scope, source, *, ref=None, confidence=None, key=None):
    """The checked parts of a fact, or APIError 400: its text (no secrets), kind, scope, source (only a person's
    words may be a rule or a preference), reference, confidence and key."""
    kind = clean_kind(kind)
    return {"text": clean_text(text), "kind": kind, "scope": clean_scope(scope), "source": clean_source(source, kind),
            "ref": clean_ref(ref), "confidence": clean_confidence(confidence), "key": clean_key(key)}


class MemoryStore:
    """Facts, suggestions and the memory settings. Every method opens and closes its own connection."""

    def __init__(self, folder, *, clock=time.time):
        self.folder = Path(folder)
        self.path = self.folder / FILE
        self.clock = clock

    def exists(self):
        return self.path.is_file()

    def _prepare(self, write):
        if write:
            self.folder.mkdir(parents=True, exist_ok=True)
        directory = os.open(self.folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if os.fstat(directory).st_uid != os.getuid():
                raise OSError("Memory folder has an unexpected owner")
            os.fchmod(directory, 0o700)
        finally:
            os.close(directory)
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_CREAT if write else 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise OSError("Memory store has an unexpected owner or type")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)

    @contextmanager
    def _connect(self, write=False):
        """A connection, or None for a read when there is no file yet (reading never creates the store). A file
        SQLite can't read is moved aside (``memory.sqlite3.corrupt-<time>``) and memory starts empty."""
        try:
            if not write and not os.path.lexists(self.path):
                yield None
                return
            self._prepare(write)
            conn = self._open()
        except (OSError, sqlite3.Error) as error:
            raise APIError(UNAVAILABLE, 503, "memory_unavailable") from error
        try:
            yield conn
        except sqlite3.Error as error:
            raise APIError(UNAVAILABLE, 503, "memory_unavailable") from error
        finally:
            conn.close()

    def _open(self, again=True):
        conn = sqlite3.connect(self.path, timeout=2.0, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout = 2000")
            conn.execute("PRAGMA foreign_keys = ON")
            # A deleted fact, or a suggestion's words once answered, is overwritten in the file, not only unlinked:
            # some SQLite builds (not every Python's) leave freed text readable until the page is reused.
            conn.execute("PRAGMA secure_delete = ON")
            self._migrate(conn)
            return conn
        except sqlite3.DatabaseError as error:
            conn.close()
            if not again or not any(word in str(error).lower() for word in ("not a database", "malformed", "corrupt")):
                raise
        except BaseException:
            conn.close()
            raise
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(self.clock()))
        for suffix in ("", "-wal", "-shm"):
            try:
                os.replace(f"{self.path}{suffix}", f"{self.path}.corrupt-{stamp}{suffix}")
            except FileNotFoundError:
                pass
        self._prepare(True)
        return self._open(again=False)

    def _migrate(self, conn):
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise APIError(NEWER, 503, "newer_data")
        if version == SCHEMA_VERSION:
            return
        # Switching to WAL takes the whole file, and SQLite answers "database is locked" at once (its busy timeout
        # doesn't apply) while another store, here or in another process, is creating it: try again until it's done.
        deadline = time.monotonic() + 2
        while conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
            try:
                conn.execute("PRAGMA journal_mode = WAL")
            except sqlite3.OperationalError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(.02)
        conn.execute("BEGIN IMMEDIATE")
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version < SCHEMA_VERSION:
                if version == 1:
                    conn.execute("ALTER TABLE facts RENAME TO facts_v1")
                # Statement by statement: executescript would commit the open transaction first.
                for statement in (part.strip() for part in SCHEMA.split(";")):
                    if statement:
                        conn.execute(statement)
                if version == 1:
                    self._upgrade_facts(conn)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    @staticmethod
    def _upgrade_facts(conn):
        """Version 1 held only what the person wrote: each fact becomes a "value" (a pinned one a "preference")
        from the user, with the fingerprint of its words."""
        for row in conn.execute("SELECT * FROM facts_v1").fetchall():
            conn.execute(
                "INSERT OR IGNORE INTO facts(id, scope, text, kind, source, confidence, fingerprint, pinned, "
                "created_at, updated_at, last_used_at, uses) VALUES (?, ?, ?, ?, 'user', 1, ?, ?, ?, ?, ?, ?)",
                (row["id"], row["scope"], row["text"], "preference" if row["pinned"] else "value",
                 fingerprint(row["scope"], row["text"]), row["pinned"], row["created_at"], row["updated_at"],
                 row["last_used_at"], row["uses"]))
        conn.execute("DROP TABLE facts_v1")

    @contextmanager
    def _write(self):
        with self._connect(write=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def _looked(self, conn):
        row = conn.execute("SELECT value FROM settings WHERE key = 'lookedAt'").fetchone()
        try:
            return float(row["value"]) if row else 0.0
        except ValueError:
            return 0.0

    def _view(self, conn, rows):
        looked, now = self._looked(conn), self.clock()
        return [_fact(row, looked, now) for row in rows]

    def _one(self, conn, fact_id):
        row = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
        if row is None:
            raise APIError(GONE, 404, "not_found")
        return self._view(conn, [row])[0]

    def list_facts(self, scope=None, q=None, kind=None, source=None, new=False):
        """Pinned first, then the newest. ``scope``: "global", "app:<bundle>", "project:<id>" and so on, or several
        separated by commas; ``kind`` and ``source`` narrow to one; ``new``: only what Mobster learned since the
        person last looked and they haven't kept; ``q``: words that must all appear."""
        scopes = [clean_scope(part.strip()) for part in str(scope).split(",") if part.strip()] if scope else []
        if kind is not None:
            kind = clean_kind(kind)
        if source is not None and source not in model.SOURCES:
            raise APIError(f"A fact's source is one of: {', '.join(model.SOURCES)}.", 400, "invalid_source")
        with self._connect() as conn:
            if conn is None:
                return []
            rows = conn.execute("SELECT * FROM facts ORDER BY pinned DESC, updated_at DESC, rowid DESC").fetchall()
            facts = self._view(conn, rows)
        if scopes:
            facts = [fact for fact in facts if fact["scope"] in scopes]
        if kind:
            facts = [fact for fact in facts if fact["kind"] == kind]
        if source:
            facts = [fact for fact in facts if fact["source"] == source]
        if new:
            facts = [fact for fact in facts if fact["new"]]
        words = [word for word in str(q or "").lower().split() if word]
        if words:
            facts = [fact for fact in facts
                     if all(word in (fact["text"] + " " + ((fact["app"] or {}).get("name") or "")).lower()
                            for word in words)]
        return facts

    def get_fact(self, fact_id):
        with self._connect() as conn:
            if conn is None:
                raise APIError(GONE, 404, "not_found")
            return self._one(conn, fact_id)

    def count(self):
        with self._connect() as conn:
            return conn.execute("SELECT count(*) FROM facts").fetchone()[0] if conn else 0

    def _check_pins(self, conn, adding_chars, excluding=None):
        rows = conn.execute("SELECT id, text FROM facts WHERE pinned = 1").fetchall()
        rows = [row for row in rows if row["id"] != excluding]
        if len(rows) >= MAX_PINNED:
            raise APIError(f"You can pin up to {MAX_PINNED} things. Unpin one first.", 409, "too_many_pinned",
                           limit=MAX_PINNED)
        if sum(len(row["text"]) for row in rows) + adding_chars > PINNED_CHARS:
            raise APIError(f"Pinned things can be {PINNED_CHARS:,} characters together. Unpin one first.", 409,
                           "pinned_too_long", limit=PINNED_CHARS)

    def _make_room(self, conn):
        """True when there is room for one more fact: at the limit the learned, unpinned, unkept fact used least
        long ago goes. What a person wrote or kept never does."""
        if conn.execute("SELECT count(*) FROM facts").fetchone()[0] < MAX_FACTS:
            return True
        oldest = conn.execute("SELECT id FROM facts WHERE source != 'user' AND pinned = 0 AND kept = 0 "
                              "ORDER BY COALESCE(last_used_at, created_at), rowid LIMIT 1").fetchone()
        if oldest is None:
            return False
        conn.execute("DELETE FROM facts WHERE id = ?", (oldest["id"],))
        return True

    def _insert(self, conn, parts, *, pinned=False, run_id=None):
        """Insert a prepared fact. APIError 409 ``duplicate`` (with the fact), ``memory_full`` or a pin limit."""
        print_ = fingerprint(parts["scope"], parts["text"], parts["key"])
        same = conn.execute("SELECT * FROM facts WHERE fingerprint = ?", (print_,)).fetchone()
        if same is not None:
            raise APIError("Mobster already remembers this.", 409, "duplicate", fact=self._view(conn, [same])[0])
        if not self._make_room(conn):
            raise APIError(f"Mobster remembers up to {MAX_FACTS} things. Delete some first.", 409, "memory_full",
                           limit=MAX_FACTS)
        if pinned:
            self._check_pins(conn, len(parts["text"]))
        now = self.clock()
        fact_id = new_id()
        conn.execute("INSERT INTO facts(id, scope, text, kind, source, ref, run_id, confidence, key, fingerprint, "
                     "pinned, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (fact_id, parts["scope"], parts["text"], parts["kind"], parts["source"], json.dumps(parts["ref"]),
                      run_id, parts["confidence"], parts["key"], print_, int(bool(pinned)), now, now))
        return self._one(conn, fact_id)

    def add_fact(self, text, scope="global", pinned=False, kind=None):
        """A fact the person wrote themselves; saying it again lifts a Forget."""
        parts = prepare(text, kind or ("preference" if pinned else "value"), scope, "user")
        with self._write() as conn:
            fact = self._insert(conn, parts, pinned=pinned)
            conn.execute("DELETE FROM forgotten WHERE fingerprint = ?",
                         (fingerprint(parts["scope"], parts["text"], parts["key"]),))
            return fact

    def update_fact(self, fact_id, text=None, scope=None, pinned=None, kind=None):
        """The person's edit: a fact they edit is theirs from then on (a run won't overwrite its words) and counts as
        reviewed."""
        clean = clean_text(text) if text is not None else None
        scope = clean_scope(scope) if scope is not None else None
        with self._write() as conn:
            row = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
            if row is None:
                raise APIError(GONE, 404, "not_found")
            new_kind = clean_kind(kind) if kind is not None else row["kind"]
            if new_kind != row["kind"] and new_kind in model.INSTRUCTIONAL and row["source"] not in model.INSTRUCTORS:
                clean_source(row["source"], new_kind)      # raises not_an_instruction
            new_text = clean if clean is not None else row["text"]
            new_scope = scope if scope is not None else row["scope"]
            new_pinned = bool(row["pinned"]) if pinned is None else bool(pinned)
            new_print = fingerprint(new_scope, new_text, row["key"])
            if new_print != row["fingerprint"]:
                other = conn.execute("SELECT * FROM facts WHERE fingerprint = ? AND id != ?",
                                     (new_print, fact_id)).fetchone()
                if other is not None:
                    raise APIError("Mobster already remembers this.", 409, "duplicate",
                                   fact=self._view(conn, [other])[0])
            if new_pinned and (not row["pinned"] or len(new_text) > len(row["text"])):
                self._check_pins(conn, len(new_text), excluding=fact_id)
            changed = (new_text, new_scope, new_kind) != (row["text"], row["scope"], row["kind"])
            conn.execute("UPDATE facts SET text = ?, scope = ?, kind = ?, pinned = ?, fingerprint = ?, "
                         "edited = ?, kept = ?, updated_at = ? WHERE id = ?",
                         (new_text, new_scope, new_kind, int(new_pinned), new_print, int(changed or row["edited"]),
                          int(changed or row["kept"]), self.clock(), fact_id))
            return self._one(conn, fact_id)

    def keep(self, fact_id):
        """Keep a learned fact: it leaves "New since you last looked" and is never offered for removal."""
        with self._write() as conn:
            if conn.execute("UPDATE facts SET kept = 1 WHERE id = ?", (fact_id,)).rowcount == 0:
                raise APIError(GONE, 404, "not_found")
            return self._one(conn, fact_id)

    def forget(self, fact_id):
        """Remove a fact and keep its fingerprint for 90 days, so no run or check teaches it again. Returns the fact."""
        with self._write() as conn:
            fact = self._one(conn, fact_id)
            row = conn.execute("SELECT fingerprint FROM facts WHERE id = ?", (fact_id,)).fetchone()
            conn.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
            conn.execute("INSERT OR REPLACE INTO forgotten(fingerprint, forgotten_at) VALUES (?, ?)",
                         (row["fingerprint"], self.clock()))
            conn.execute("DELETE FROM run_use WHERE fact_id = ?", (fact_id,))
        if fact["scopeKind"] == "project":
            from . import project
            project.export_file(self, fact["scopeId"])
        return fact

    def delete_fact(self, fact_id):
        self.forget(fact_id)
        return True

    def mark_looked(self):
        """The person has opened the page: what was new is no longer new. Returns the time (milliseconds)."""
        now = self.clock()
        with self._write() as conn:
            conn.execute("INSERT INTO settings(key, value) VALUES ('lookedAt', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (repr(now),))
        return millis(now)

    def all_facts(self):
        """Every fact as a light row (retrieval). Never creates the store."""
        with self._connect() as conn:
            if conn is None:
                return []
            return [dict(row) for row in conn.execute(
                "SELECT id, scope, text, kind, source, confidence, pinned, kept, updated_at, last_used_at, uses "
                "FROM facts")]

    def mark_used(self, ids, run_id=None):
        """Record that a task used these facts (best effort: a busy file never holds up a task)."""
        if not ids:
            return
        try:
            with self._write() as conn:
                now = self.clock()
                conn.executemany("UPDATE facts SET last_used_at = ?, uses = uses + 1 WHERE id = ?",
                                 [(now, fact_id) for fact_id in ids])
                if run_id:
                    conn.execute("DELETE FROM run_use WHERE at < ?", (now - USE_DAYS * DAY,))
                    conn.executemany("INSERT OR IGNORE INTO run_use(run_id, fact_id, at) VALUES (?, ?, ?)",
                                     [(run_id, fact_id, now) for fact_id in ids])
        except APIError:
            pass

    def learn(self, text, kind, scope, source, *, ref=None, run_id=None, key=None, confidence=0.7,
              authoritative=False):
        """A fact Mobster learned from use (a run, a lesson, a check, the repo, a profile). Returns
        ``{"status", "fact"}``, never raises for a refusal: ``added``, ``updated`` (a ``key``'s value changed),
        ``exists``, ``forgotten`` (the person forgot it lately), ``off`` (Suggest things to remember is off),
        ``full`` or ``refused`` (a secret, an unchecked kind, a rule from a screen, bad input). 503 still raises.
        ``authoritative``: the person just changed the thing this fact states (a profile's voice), so it overrides an
        edit and a Forget."""
        try:
            if source == "user":
                raise APIError("What you write yourself is added with add_fact.", 400, "invalid_source")
            parts = prepare(text, kind, scope, source, ref=ref, confidence=confidence, key=key)
        except APIError as error:
            if error.status != 400:
                raise
            return {"status": "refused", "code": error.code, "fact": None}
        with self._write() as conn:
            return self._learn(conn, parts, run_id, authoritative)

    def _learn(self, conn, parts, run_id, authoritative=False):
        row = conn.execute("SELECT value FROM settings WHERE key = 'suggest'").fetchone()
        if row is not None and row["value"] != "1":
            return {"status": "off", "fact": None}
        now = self.clock()
        print_ = fingerprint(parts["scope"], parts["text"], parts["key"])
        conn.execute("DELETE FROM forgotten WHERE forgotten_at < ?", (now - QUIET_DAYS * DAY,))
        if authoritative:
            conn.execute("DELETE FROM forgotten WHERE fingerprint = ?", (print_,))
        elif conn.execute("SELECT 1 FROM forgotten WHERE fingerprint = ?", (print_,)).fetchone():
            return {"status": "forgotten", "fact": None}
        same = conn.execute("SELECT * FROM facts WHERE fingerprint = ?", (print_,)).fetchone()
        if same is not None:
            status = "exists"
            if (parts["key"] and (authoritative or not (same["edited"] or same["kept"] or same["pinned"]))
                    and same["source"] != "user" and same["kind"] == parts["kind"]
                    and same["text"] != parts["text"]):
                conn.execute("UPDATE facts SET text = ?, confidence = ?, source = ?, ref = ?, run_id = ?, kept = 0, "
                             "updated_at = ? WHERE id = ?", (parts["text"], parts["confidence"], parts["source"],
                                                              json.dumps(parts["ref"]), run_id, now, same["id"]))
                status = "updated"
            return {"status": status, "fact": self._one(conn, same["id"])}
        try:
            fact = self._insert(conn, parts, run_id=run_id)
        except APIError as error:
            if error.code == "memory_full":
                return {"status": "full", "fact": None}
            raise
        return {"status": "added", "fact": fact}

    def drop(self, scope, key):
        """Remove the fact a ``key`` names in a scope, without keeping its fingerprint: the thing it stated is gone
        (a profile's voice cleared, a profile removed). Returns how many went."""
        scope, key = clean_scope(scope), clean_key(key)
        with self._write() as conn:
            return conn.execute("DELETE FROM facts WHERE fingerprint = ?", (fingerprint(scope, "", key),)).rowcount

    def drop_scope(self, scope):
        """Remove every fact in a scope that Mobster wrote for it (a removed profile's), not the person's own."""
        scope = clean_scope(scope)
        with self._write() as conn:
            return conn.execute("DELETE FROM facts WHERE scope = ? AND source != 'user'", (scope,)).rowcount

    def link_project(self, project_id, path):
        """A project's id and its folder, so its facts can travel in ``.mobster/memory.yaml``. Facts written under
        the id derived from the folder (before the project had one) move to ``project_id``. Returns how many moved."""
        scope = clean_scope(f"project:{project_id}")
        if not isinstance(path, str) or not path or len(path) > 1000:
            raise APIError("A project's folder is a path.", 400, "invalid_path")
        path = os.path.realpath(path)
        from . import project
        old = f"project:{project.derived_id(path)}"
        with self._write() as conn:
            conn.execute("INSERT INTO projects(id, path) VALUES (?, ?) ON CONFLICT(id) "
                         "DO UPDATE SET path = excluded.path",
                         (project_id, path))
            moved = 0
            if old != scope:
                for row in conn.execute("SELECT id, text, key FROM facts WHERE scope = ?", (old,)).fetchall():
                    new_print = fingerprint(scope, row["text"], row["key"])
                    if conn.execute("SELECT 1 FROM facts WHERE fingerprint = ?", (new_print,)).fetchone():
                        continue
                    conn.execute("UPDATE facts SET scope = ?, fingerprint = ? WHERE id = ?",
                                 (scope, new_print, row["id"]))
                    moved += 1
                conn.execute("DELETE FROM projects WHERE id = ?", (old.partition(":")[2],))
            return moved

    def project_path(self, project_id):
        with self._connect() as conn:
            row = conn.execute("SELECT path FROM projects WHERE id = ?", (project_id,)).fetchone() if conn else None
        return row["path"] if row else None

    def project_id_for(self, path):
        with self._connect() as conn:
            row = conn.execute("SELECT id FROM projects WHERE path = ?", (path,)).fetchone() if conn else None
        return row["id"] if row else None

    def stage(self, run_id, items):
        """What a run learned, held until it finishes. ``items``: dicts with ``learn``'s arguments (text, kind, scope,
        source, and optionally ref, key, confidence). Each is checked now, so a secret is never even held. Returns
        how many were held."""
        held = []
        for item in items:
            try:
                parts = prepare(item.get("text"), item.get("kind"), item.get("scope"), item.get("source"),
                                ref=item.get("ref"), confidence=item.get("confidence", 0.7), key=item.get("key"))
            except APIError as error:
                if error.status != 400:
                    raise
                continue
            held.append(parts)
        if not held:
            return 0
        with self._write() as conn:
            now = self.clock()
            conn.execute("DELETE FROM staged WHERE created_at < ?", (now - STAGED_DAYS * DAY,))
            room = 50 - conn.execute("SELECT count(*) FROM staged WHERE run_id = ?", (run_id,)).fetchone()[0]
            for parts in held[:max(0, room)]:
                conn.execute("INSERT INTO staged(id, run_id, payload, created_at) VALUES (?, ?, ?, ?)",
                             (new_id(), run_id, json.dumps(parts), now))
            return len(held[:max(0, room)])

    def commit_run(self, run_id, *, keep=True):
        """The run finished with a result the person didn't dispute: what it staged becomes facts (``keep=False``
        drops it instead). Returns each one's ``learn`` result."""
        with self._write() as conn:
            rows = conn.execute("SELECT * FROM staged WHERE run_id = ? ORDER BY created_at, rowid",
                                (run_id,)).fetchall()
            conn.execute("DELETE FROM staged WHERE run_id = ?", (run_id,))
            if not keep:
                return []
            return [self._learn(conn, json.loads(row["payload"]), run_id) for row in rows]

    def run_memory(self, run_id):
        """What a run used and what it learned, for its view: {"used": [...], "learned": [...], "pending": n}."""
        with self._connect() as conn:
            if conn is None:
                return {"used": [], "learned": [], "pending": 0}
            used = conn.execute("SELECT f.* FROM facts f JOIN run_use u ON u.fact_id = f.id WHERE u.run_id = ? "
                                "ORDER BY u.rowid", (run_id,)).fetchall()
            learned = conn.execute("SELECT * FROM facts WHERE run_id = ? ORDER BY created_at, rowid",
                                   (run_id,)).fetchall()
            pending = conn.execute("SELECT count(*) FROM staged WHERE run_id = ?", (run_id,)).fetchone()[0]
            return {"used": self._view(conn, used), "learned": self._view(conn, learned), "pending": pending}

    def _expire(self, conn):
        now = self.clock()
        conn.execute("UPDATE proposals SET status = 'expired', resolved_at = ?, text = '' "
                     "WHERE status = 'pending' AND created_at < ?", (now, now - PROPOSAL_DAYS * DAY))
        conn.execute("DELETE FROM proposals WHERE status IN ('dismissed', 'expired') AND resolved_at < ?",
                     (now - QUIET_DAYS * DAY,))
        conn.execute("DELETE FROM proposals WHERE status = 'accepted' AND fact_id NOT IN (SELECT id FROM facts)")

    def propose(self, text, scope="global", *, thread_id=None, run_id=None, pin=False):
        """A suggestion from the person's own words, or None when it isn't worth asking; never stores a fact."""
        try:
            text, scope = clean_text(text), clean_scope(scope)
        except APIError:
            return None
        key = digest(text)
        with self._write() as conn:
            self._expire(conn)
            if conn.execute("SELECT 1 FROM proposals WHERE digest = ? AND scope = ? "
                            "AND status IN ('pending', 'dismissed', 'expired')",
                            (key, scope)).fetchone():
                return None
            if conn.execute("SELECT 1 FROM facts WHERE fingerprint = ?", (fingerprint(scope, text),)).fetchone():
                return None
            pending = conn.execute("SELECT id FROM proposals WHERE status = 'pending' ORDER BY created_at, rowid"
                                   ).fetchall()
            for row in pending[:max(0, len(pending) - MAX_PENDING + 1)]:
                conn.execute("UPDATE proposals SET status = 'expired', resolved_at = ?, text = '' WHERE id = ?",
                             (self.clock(), row["id"]))
            proposal_id = new_id()
            conn.execute("INSERT INTO proposals(id, text, scope, thread_id, run_id, status, created_at, digest, pin) "
                         "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                         (proposal_id, text, scope, thread_id, run_id, self.clock(), key, int(bool(pin))))
            return _proposal(conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone())

    def set_proposal_item(self, proposal_id, item_id):
        """The thread item that shows this suggestion (conversations' sink), so answering it updates the card."""
        with self._write() as conn:
            conn.execute("UPDATE proposals SET item_id = ? WHERE id = ?", (str(item_id)[:64], proposal_id))

    def proposal_item(self, proposal_id):
        """(thread id, item id) of the card that shows the suggestion, or (None, None)."""
        with self._connect() as conn:
            row = conn.execute("SELECT thread_id, item_id FROM proposals WHERE id = ?",
                               (proposal_id,)).fetchone() if conn else None
        return (row["thread_id"], row["item_id"]) if row else (None, None)

    def list_proposals(self, thread_id=None, status="pending"):
        if status not in PROPOSAL_STATES + ("all",):
            raise APIError("Status is pending, accepted, dismissed, expired or all.", 400, "invalid_status")
        if not self.exists():
            return []
        with self._write() as conn:
            self._expire(conn)
            rows = conn.execute("SELECT * FROM proposals ORDER BY created_at DESC, rowid DESC").fetchall()
        out = [_proposal(row) for row in rows]
        if status != "all":
            out = [p for p in out if p["status"] == status]
        if thread_id:
            out = [p for p in out if p["threadId"] == thread_id]
        return out

    def resolve_proposal(self, proposal_id, accept, text=None, pinned=None):
        """The person's answer: ``accept`` stores the fact (with their edit, ``text``); otherwise "Not now"."""
        if not isinstance(accept, bool):
            raise APIError("Say whether to remember it (accept true or false).", 400, "invalid_answer")
        edited = clean_text(text) if (accept and text is not None) else None
        with self._write() as conn:
            self._expire(conn)
            row = conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
            if row is None:
                raise APIError("That suggestion is gone.", 404, "not_found")
            if row["status"] != "pending":
                raise APIError("That suggestion was already answered." if row["status"] != "expired"
                               else "That suggestion expired.", 409, "not_pending", proposal=_proposal(row))
            now = self.clock()
            fact = None
            if accept:
                pin = bool(row["pin"]) if pinned is None else bool(pinned)
                fact_text = edited or row["text"]
                try:
                    parts = prepare(fact_text, "preference" if pin else "value", row["scope"], "user")
                    fact = self._insert(conn, parts, pinned=pin)
                except APIError as error:
                    if error.code == "duplicate":
                        fact = error.details["fact"]
                    elif error.code in ("too_many_pinned", "pinned_too_long") and pinned is None:
                        parts = prepare(fact_text, "value", row["scope"], "user")
                        fact = self._insert(conn, parts, pinned=False)
                    else:
                        raise
                conn.execute("DELETE FROM forgotten WHERE fingerprint = ?",
                             (fingerprint(row["scope"], fact["text"]),))
                conn.execute("UPDATE proposals SET status = 'accepted', resolved_at = ?, text = '', fact_id = ? "
                             "WHERE id = ?", (now, fact["id"], proposal_id))
            else:
                conn.execute("UPDATE proposals SET status = 'dismissed', resolved_at = ?, text = '' WHERE id = ?",
                             (now, proposal_id))
            return _proposal(conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()), fact

    def settings(self):
        values = dict(SETTINGS)
        with self._connect() as conn:
            if conn is not None:
                for row in conn.execute("SELECT key, value FROM settings"):
                    if row["key"] in values:
                        values[row["key"]] = row["value"] == "1"
        return values

    def update_settings(self, changes):
        if not isinstance(changes, dict) or not changes:
            raise APIError("Change useInTasks or suggest.", 400, "invalid_settings")
        for key, value in changes.items():
            if key not in SETTINGS:
                raise APIError(f"Unknown memory setting {key}.", 400, "invalid_settings")
            if not isinstance(value, bool):
                raise APIError(f"{key} is true or false.", 400, "invalid_settings")
        with self._write() as conn:
            conn.executemany("INSERT INTO settings(key, value) VALUES (?, ?) "
                             "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                             [(key, "1" if value else "0") for key, value in changes.items()])
        return self.settings()

    def clear(self):
        """Delete every fact, suggestion, forgotten fingerprint and project link (settings stay). Returns how many
        facts and suggestions went."""
        for leftover in self.folder.glob(FILE + ".corrupt-*"):
            leftover.unlink(missing_ok=True)
        if not self.exists():
            return {"facts": 0, "proposals": 0}
        with self._write() as conn:
            facts = conn.execute("DELETE FROM facts").rowcount
            proposals = conn.execute("DELETE FROM proposals").rowcount
            for table in ("forgotten", "staged", "run_use", "projects"):
                conn.execute(f"DELETE FROM {table}")
            conn.execute("DELETE FROM settings WHERE key = 'lookedAt'")
            # Older builds created these unused tables. Clearing memory erases anything left in them too.
            conn.execute("DROP TABLE IF EXISTS routine_versions")
            conn.execute("DROP TABLE IF EXISTS routines")
        with self._connect() as conn:
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("VACUUM")
            except sqlite3.Error:
                pass
        return {"facts": facts, "proposals": proposals}

    def export_json(self):
        """Everything Mobster remembers, as data: every fact with its kind, source, scope and last use."""
        return {"version": SCHEMA_VERSION, "exportedAt": millis(self.clock()), "facts": self.list_facts()}

    def export_markdown(self):
        """Everything Mobster remembers, as Markdown a person can read and keep."""
        facts = self.list_facts()
        lines = ["# What Mobster remembers", "",
                 "Saved on this Mac by Mobster. Pinned things go with eligible tasks in their scope; the rest go "
                 "with eligible tasks that fit.", ""]
        if not facts:
            lines.append("Nothing yet.")
        groups = {}
        for fact in facts:
            kind = fact["scopeKind"]
            title = ("In every app" if kind == "global"
                     else f"In {fact['app']['name'] or fact['scopeId']}" if kind == "app"
                     else f"Saved profile {fact['scopeId'].split('/workflow:')[0]} and workflow "
                          f"{fact['scopeId'].split('/workflow:')[1]}" if kind == "profile_workflow"
                     else f"Saved profile {fact['scopeId']}" if kind == "profile"
                     else f"Workflow {fact['scopeId']}" if kind == "workflow"
                     else f"Unknown scope {fact['scope']}" if kind == "unknown"
                     else f"{kind.capitalize()} {fact['scopeId']}")
            groups.setdefault((0 if kind == "global" else 1, title.lower(), title), []).append(fact)
        for (_, _, title), members in sorted(groups.items(), key=lambda pair: pair[0]):
            lines += [f"## {title}", ""]
            for fact in members:
                marks = [mark for mark, on in (("pinned", fact["pinned"]),
                                               ("from " + fact["source"], fact["source"] != "user")) if on]
                lines.append(f"- {fact['text']}{' (' + ', '.join(marks) + ')' if marks else ''}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"
