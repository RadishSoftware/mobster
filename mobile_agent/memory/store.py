"""What Mobster remembers, in one SQLite file on this Mac (SPEC §3.3).

``user_data_dir()/memory/memory.sqlite3``: the folder is 0700 and the file 0600. WAL and a short-lived connection
per call, so the Mac app's agent, the terminal UI, `mobster memory` and MCP share it safely. ``MOBSTER_MEMORY_DIR``
moves it (tests). With neither that nor a home folder (a test that cleared the environment), there is no store:
nothing is read or written, and every caller treats memory as empty and off.

Nothing becomes a fact without the person's action: ``add_fact`` is called only for what they wrote themselves (the
app's Settings › Memory, `mobster memory add`), and ``resolve_proposal(accept=True)`` only when they accepted a
suggestion. Suggestions (proposals) are the person's own words, held until they answer or for 14 days; once answered
or expired only a digest stays, so the same suggestion isn't offered again.

Never secrets: a fact or a suggestion that ``secret_filter.is_secret`` flags is refused. Errors are
``api_errors.APIError`` with a plain sentence, so the HTTP routes and the CLI say the same thing.
"""

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import uuid

from ..api_errors import APIError
from ..paths import user_data_dir
from ..secret_filter import is_secret

SCHEMA_VERSION = 1
FILE = "memory.sqlite3"

FACT_CHARS = 300          # one fact
MAX_FACTS = 500           # all facts
MAX_PINNED = 10           # pinned facts ship with every task, so they're few and short
PINNED_CHARS = 1000
MAX_PENDING = 50          # suggestions waiting for an answer; the oldest expire first
PROPOSAL_DAYS = 14        # a suggestion nobody answered expires
QUIET_DAYS = 90           # a suggestion answered "Not now" isn't offered again for this long
DAY = 86400.0

SECRET = "Mobster doesn't remember passwords, codes or card numbers."
NEWER = "This was saved by a newer Mobster. Update Mobster to use it."
UNAVAILABLE = "Mobster can't open what it remembers on this Mac right now. Try again."

SETTINGS = {"useInTasks": True, "suggest": True}
BUNDLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)+")
ID = re.compile(r"[a-f0-9]{12}")
PROPOSAL_STATES = ("pending", "accepted", "dismissed", "expired")

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts(id TEXT PRIMARY KEY, scope TEXT NOT NULL,
  text TEXT NOT NULL CHECK(length(text) <= 300),
  source TEXT NOT NULL CHECK(source IN ('user','proposal')), pinned INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL, updated_at REAL NOT NULL, last_used_at REAL, uses INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY, text TEXT NOT NULL, scope TEXT NOT NULL, thread_id TEXT,
  run_id TEXT, status TEXT NOT NULL CHECK(status IN ('pending','accepted','dismissed','expired')),
  created_at REAL, resolved_at REAL, digest TEXT NOT NULL, pin INTEGER NOT NULL DEFAULT 0, item_id TEXT,
  fact_id TEXT);
CREATE INDEX IF NOT EXISTS proposals_status ON proposals(status, created_at);
CREATE TABLE IF NOT EXISTS routines(id TEXT PRIMARY KEY, name TEXT NOT NULL, app_bundle TEXT, trigger TEXT NOT NULL,
  active_version INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, created_from_run TEXT,
  created_at REAL, updated_at REAL, last_run_at REAL, done INTEGER DEFAULT 0, handoffs INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS routine_versions(routine_id TEXT REFERENCES routines(id) ON DELETE CASCADE,
  version INTEGER, trajectory TEXT NOT NULL, status TEXT CHECK(status IN ('active','pending_review','rejected')),
  from_run TEXT, created_at REAL, PRIMARY KEY(routine_id, version));
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


def clean_text(text):
    """Whitespace collapsed; APIError 400 for an empty, long or secret text."""
    if not isinstance(text, str):
        raise APIError("Write what Mobster should remember.", 400, "invalid_text")
    text = " ".join(text.split())
    if not text:
        raise APIError("Write what Mobster should remember.", 400, "invalid_text")
    if len(text) > FACT_CHARS:
        raise APIError(f"Keep it to {FACT_CHARS} characters.", 400, "too_long", limit=FACT_CHARS)
    if is_secret(text):
        raise APIError(SECRET, 400, "secret")
    return text


def clean_scope(scope):
    """"global", or "app:<bundle id>". APIError 400 otherwise."""
    if scope in (None, "", "global"):
        return "global"
    if isinstance(scope, str) and scope.startswith("app:") and BUNDLE.fullmatch(scope[4:]) and len(scope) <= 160:
        return scope
    raise APIError("Choose all tasks or one app (its bundle ID, such as com.apple.MobileSMS).", 400,
                   "invalid_scope")


def normalize(text):
    """For spotting the same thing said twice: lowercase, single spaces, no closing punctuation."""
    return " ".join(str(text).lower().split()).rstrip(" .!;,")


def digest(text):
    return hashlib.sha256(normalize(text).encode()).hexdigest()[:32]


def app_name(bundle):
    """The app's display name (the catalog's, or one registered for apps outside it), else None."""
    if not bundle:
        return None
    from .. import catalog
    return catalog._EXTRA_NAMES.get(bundle) or next(
        (app["name"] for app in catalog.APPS if app["bundleId"] == bundle), None)


def millis(seconds):
    return None if seconds is None else round(seconds * 1000)


def _fact(row):
    scope = row["scope"]
    bundle = scope[4:] if scope.startswith("app:") else None
    return {"id": row["id"], "text": row["text"], "scope": scope,
            "app": {"bundleId": bundle, "name": app_name(bundle) or bundle} if bundle else None,
            "source": row["source"], "pinned": bool(row["pinned"]), "createdAt": millis(row["created_at"]),
            "updatedAt": millis(row["updated_at"]), "lastUsedAt": millis(row["last_used_at"]), "uses": row["uses"]}


def _proposal(row):
    scope = row["scope"]
    bundle = scope[4:] if scope.startswith("app:") else None
    return {"id": row["id"], "text": row["text"], "scope": scope,
            "app": {"bundleId": bundle, "name": app_name(bundle) or bundle} if bundle else None,
            "threadId": row["thread_id"], "runId": row["run_id"], "status": row["status"], "pin": bool(row["pin"]),
            "createdAt": millis(row["created_at"]), "resolvedAt": millis(row["resolved_at"]),
            "expiresAt": millis(row["created_at"] + PROPOSAL_DAYS * DAY) if row["status"] == "pending" else None,
            "factId": row["fact_id"]}


class MemoryStore:
    """Facts, suggestions and the memory settings. Every method opens and closes its own connection."""

    def __init__(self, folder, *, clock=time.time):
        self.folder = Path(folder)
        self.path = self.folder / FILE
        self.clock = clock

    # -- connections ----------------------------------------------------------------------------------------------

    def exists(self):
        return self.path.is_file()

    def _create(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.folder, 0o700)
        except OSError:
            pass
        if not self.path.exists():
            os.close(os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600))

    @contextmanager
    def _connect(self, write=False):
        """A connection, or None for a read when there is no file yet (reading never creates the store)."""
        if not self.exists():
            if not write:
                yield None
                return
            self._create()
        try:
            conn = sqlite3.connect(self.path, timeout=2.0, isolation_level=None, check_same_thread=False)
        except sqlite3.Error as error:
            raise APIError(UNAVAILABLE, 503, "memory_unavailable") from error
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout = 2000")
            conn.execute("PRAGMA foreign_keys = ON")
            # A deleted fact, or a suggestion's words once answered, is overwritten in the file, not only unlinked:
            # some SQLite builds (not every Python's) leave freed text readable until the page is reused.
            conn.execute("PRAGMA secure_delete = ON")
            self._migrate(conn)
            yield conn
        except sqlite3.Error as error:
            raise APIError(UNAVAILABLE, 503, "memory_unavailable") from error
        finally:
            conn.close()

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
            if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
                # Statement by statement: executescript would commit the open transaction first.
                for statement in (part.strip() for part in SCHEMA.split(";")):
                    if statement:
                        conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

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

    # -- facts ----------------------------------------------------------------------------------------------------

    def list_facts(self, scope=None, q=None):
        """Pinned first, then the newest. ``scope``: "global" or "app:<bundle>"; ``q``: words that must all appear."""
        with self._connect() as conn:
            if conn is None:
                return []
            rows = conn.execute("SELECT * FROM facts ORDER BY pinned DESC, updated_at DESC, rowid DESC").fetchall()
        facts = [_fact(row) for row in rows]
        if scope:
            scope = clean_scope(scope)
            facts = [fact for fact in facts if fact["scope"] == scope]
        words = [word for word in str(q or "").lower().split() if word]
        if words:
            facts = [fact for fact in facts
                     if all(word in (fact["text"] + " " + ((fact["app"] or {}).get("name") or "")).lower()
                            for word in words)]
        return facts

    def get_fact(self, fact_id):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone() if conn else None
        if row is None:
            raise APIError("Mobster doesn't remember that anymore.", 404, "not_found")
        return _fact(row)

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

    def _insert_fact(self, conn, text, scope, pinned, source):
        if conn.execute("SELECT count(*) FROM facts").fetchone()[0] >= MAX_FACTS:
            raise APIError(f"Mobster remembers up to {MAX_FACTS} things. Delete some first.", 409, "memory_full",
                           limit=MAX_FACTS)
        same = conn.execute("SELECT * FROM facts WHERE scope = ?", (scope,)).fetchall()
        wanted = normalize(text)
        for row in same:
            if normalize(row["text"]) == wanted:
                raise APIError("Mobster already remembers this.", 409, "duplicate", fact=_fact(row))
        if pinned:
            self._check_pins(conn, len(text))
        now = self.clock()
        fact_id = new_id()
        conn.execute("INSERT INTO facts(id, scope, text, source, pinned, created_at, updated_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (fact_id, scope, text, source, int(bool(pinned)), now, now))
        return _fact(conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone())

    def add_fact(self, text, scope="global", pinned=False):
        """A fact the person wrote themselves. APIError 400 (empty, long, secret, scope), 409 (duplicate, full,
        pins)."""
        text, scope = clean_text(text), clean_scope(scope)
        with self._write() as conn:
            return self._insert_fact(conn, text, scope, pinned, "user")

    def update_fact(self, fact_id, text=None, scope=None, pinned=None):
        clean = clean_text(text) if text is not None else None
        scope = clean_scope(scope) if scope is not None else None
        with self._write() as conn:
            row = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
            if row is None:
                raise APIError("Mobster doesn't remember that anymore.", 404, "not_found")
            new_text = clean if clean is not None else row["text"]
            new_scope = scope if scope is not None else row["scope"]
            new_pinned = bool(row["pinned"]) if pinned is None else bool(pinned)
            if (new_text, new_scope) != (row["text"], row["scope"]):
                wanted = normalize(new_text)
                for other in conn.execute("SELECT * FROM facts WHERE scope = ? AND id != ?", (new_scope, fact_id)):
                    if normalize(other["text"]) == wanted:
                        raise APIError("Mobster already remembers this.", 409, "duplicate", fact=_fact(other))
            if new_pinned:
                if not row["pinned"]:
                    self._check_pins(conn, len(new_text), excluding=fact_id)
                elif len(new_text) > len(row["text"]):
                    others = sum(len(r["text"]) for r in conn.execute(
                        "SELECT text FROM facts WHERE pinned = 1 AND id != ?", (fact_id,)))
                    if others + len(new_text) > PINNED_CHARS:
                        raise APIError(f"Pinned things can be {PINNED_CHARS:,} characters together. Unpin one "
                                       "first.", 409, "pinned_too_long", limit=PINNED_CHARS)
            conn.execute("UPDATE facts SET text = ?, scope = ?, pinned = ?, updated_at = ? WHERE id = ?",
                         (new_text, new_scope, int(new_pinned), self.clock(), fact_id))
            return _fact(conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone())

    def delete_fact(self, fact_id):
        with self._write() as conn:
            if conn.execute("DELETE FROM facts WHERE id = ?", (fact_id,)).rowcount == 0:
                raise APIError("Mobster doesn't remember that anymore.", 404, "not_found")
        return True

    def all_facts(self):
        """Every fact as a light row (retrieval). Never creates the store."""
        with self._connect() as conn:
            if conn is None:
                return []
            return [dict(row) for row in conn.execute(
                "SELECT id, scope, text, pinned, updated_at, last_used_at, uses FROM facts")]

    def mark_used(self, ids):
        """Record that a task used these facts (best effort: a busy file never holds up a task)."""
        if not ids:
            return
        try:
            with self._write() as conn:
                conn.executemany("UPDATE facts SET last_used_at = ?, uses = uses + 1 WHERE id = ?",
                                 [(self.clock(), fact_id) for fact_id in ids])
        except APIError:
            pass

    # -- suggestions ------------------------------------------------------------------------------------------------

    def _expire(self, conn):
        now = self.clock()
        conn.execute("UPDATE proposals SET status = 'expired', resolved_at = ?, text = '' "
                     "WHERE status = 'pending' AND created_at < ?", (now, now - PROPOSAL_DAYS * DAY))
        conn.execute("DELETE FROM proposals WHERE status IN ('dismissed', 'expired') AND resolved_at < ?",
                     (now - QUIET_DAYS * DAY,))
        conn.execute("DELETE FROM proposals WHERE status = 'accepted' AND fact_id NOT IN (SELECT id FROM facts)")

    def propose(self, text, scope="global", *, thread_id=None, run_id=None, pin=False):
        """A suggestion from the person's own words, or None when it isn't worth asking: a secret, too long, already
        remembered, already suggested, or answered "Not now" lately. Never stores a fact."""
        try:
            text, scope = clean_text(text), clean_scope(scope)
        except APIError:
            return None
        key = digest(text)
        with self._write() as conn:
            self._expire(conn)
            if conn.execute("SELECT 1 FROM proposals WHERE digest = ? AND status IN ('pending', 'dismissed', "
                            "'expired')", (key,)).fetchone():
                return None
            for row in conn.execute("SELECT text FROM facts"):
                if normalize(row["text"]) == normalize(text):
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
        """The person's answer: ``accept`` stores the fact (with their edit, ``text``); otherwise "Not now".
        Returns (proposal, fact or None). APIError 404, 409 when it was already answered or expired."""
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
                    fact = self._insert_fact(conn, fact_text, row["scope"], pin, "proposal")
                except APIError as error:
                    if error.code == "duplicate":
                        fact = error.details["fact"]
                    elif error.code in ("too_many_pinned", "pinned_too_long") and pinned is None:
                        fact = self._insert_fact(conn, fact_text, row["scope"], False, "proposal")
                    else:
                        raise
                conn.execute("UPDATE proposals SET status = 'accepted', resolved_at = ?, text = '', fact_id = ? "
                             "WHERE id = ?", (now, fact["id"], proposal_id))
            else:
                conn.execute("UPDATE proposals SET status = 'dismissed', resolved_at = ?, text = '' WHERE id = ?",
                             (now, proposal_id))
            return _proposal(conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()), fact

    # -- settings, clearing, export ---------------------------------------------------------------------------------

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
        """Delete every fact and suggestion (settings stay). Returns how many of each went."""
        if not self.exists():
            return {"facts": 0, "proposals": 0}
        with self._write() as conn:
            facts = conn.execute("DELETE FROM facts").rowcount
            proposals = conn.execute("DELETE FROM proposals").rowcount
            conn.execute("DELETE FROM routine_versions")
            conn.execute("DELETE FROM routines")
        with self._connect() as conn:
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("VACUUM")
            except sqlite3.Error:
                pass
        return {"facts": facts, "proposals": proposals}

    def export_markdown(self):
        """Everything Mobster remembers, as Markdown a person can read and keep."""
        facts = self.list_facts()
        lines = ["# What Mobster remembers", "",
                 "Saved on this Mac by Mobster. Pinned things go with every task; the rest go with tasks that "
                 "mention them.", ""]
        if not facts:
            lines.append("Nothing yet.")
        everywhere = [fact for fact in facts if fact["scope"] == "global"]
        if everywhere:
            lines += ["## In every app", ""]
            lines += [f"- {fact['text']}{' (pinned)' if fact['pinned'] else ''}" for fact in everywhere]
            lines.append("")
        apps = {}
        for fact in facts:
            if fact["app"]:
                apps.setdefault(fact["app"]["name"] or fact["app"]["bundleId"], []).append(fact)
        for name in sorted(apps, key=str.lower):
            lines += [f"## In {name}", ""]
            lines += [f"- {fact['text']}{' (pinned)' if fact['pinned'] else ''}" for fact in apps[name]]
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"
