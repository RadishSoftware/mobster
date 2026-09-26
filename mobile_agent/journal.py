"""Private, crash-safe local run journal. Intent is durable before a device mutation."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import threading

from . import usage
from .errors import MobsterError


class JournalError(MobsterError):
    pass


EVENT_LIMIT = 1024
RUN_BYTE_LIMIT = 16_000_000
TERMINAL_BYTE_LIMIT = 1_000_000


class Lease:
    """Process lease; never unlink a locked inode, which would allow a second owner."""
    def __init__(self, path):
        self.fd = None
        try:
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode):
                os.close(fd)
                raise JournalError("Lock file has an unexpected owner or type")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                raise JournalError("Another Mobster process already owns this resource") from None
            self.fd = fd
        except OSError:
            raise JournalError("Cannot acquire the private Mobster lease") from None

    @classmethod
    def device(cls, identity):
        directory = Path(tempfile.gettempdir()) / f"mobster-device-leases-{os.getuid()}"
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise JournalError("Device lease directory must be private and owned by this user")
        identity = str(Path(identity).resolve()) if str(identity).startswith("/") else str(identity)
        return cls(directory / (hashlib.sha256(identity.encode()).hexdigest() + ".lock"))

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def encode(value):
    return json.dumps(value, allow_nan=False, separators=(",", ":"))


class Journal:
    def __init__(self, path=None):
        self.lock = threading.RLock()
        self.lease = None
        self.connection = None
        try:
            if path is not None:
                path = Path(path).absolute()
                path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                if path.is_symlink():
                    raise JournalError("The journal cannot be a symbolic link")
                self.lease = Lease(str(path) + ".lock")
                fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    os.close(fd)
                    raise JournalError("Journal has an unexpected owner or type")
                os.fchmod(fd, 0o600)
                os.close(fd)
            self.connection = sqlite3.connect(str(path) if path else ":memory:",
                timeout=3, check_same_thread=False, isolation_level=None)
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise JournalError("Unsupported journal version; no device action authorized")
            self.connection.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, data TEXT NOT NULL, bytes INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL, data TEXT NOT NULL, PRIMARY KEY(run_id, seq));
                CREATE TABLE IF NOT EXISTS requests (
                    key TEXT PRIMARY KEY, run_id TEXT NOT NULL, fingerprint TEXT NOT NULL);
                PRAGMA user_version=1;
            """)
            usage.initialize(self.connection)
        except Exception:
            self.close()
            raise JournalError("Cannot open the run journal; existing data was not reset") from None

    @contextmanager
    def transaction(self):
        with self.lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                yield self.connection
                self.connection.execute("COMMIT")
            except Exception as exc:
                if self.connection and self.connection.in_transaction:
                    self.connection.execute("ROLLBACK")
                if isinstance(exc, JournalError):
                    raise
                raise JournalError("Run journal could not be saved; no further device action authorized") from None

    def lookup(self, key):
        with self.lock:
            return self.connection.execute("SELECT run_id, fingerprint FROM requests WHERE key=?", (key,)).fetchone()

    def create(self, metadata, key=None, fingerprint=""):
        with self.transaction() as connection:
            if key and connection.execute("SELECT count(*) FROM requests").fetchone()[0] >= 10000:
                raise JournalError("Idempotency journal reached its retention limit; operator maintenance required")
            connection.execute("INSERT INTO runs(id,data) VALUES (?,?)", (metadata["id"], encode(metadata)))
            if key:
                connection.execute("INSERT INTO requests VALUES (?,?,?)", (key, metadata["id"], fingerprint))
            # Retention and insertion are one commit. A failed task must not erase history.
            expired = [row[0] for row in connection.execute(
                "SELECT id FROM runs ORDER BY rowid DESC LIMIT -1 OFFSET 100")]
            connection.executemany("DELETE FROM runs WHERE id=?", [(identifier,) for identifier in expired])
            # Request tombstones deliberately survive run retention, preventing replay.
        return expired

    def append(self, metadata, event):
        data = encode(event)
        size = len(data.encode())
        if size > 1_000_000 or event["seq"] >= EVENT_LIMIT:
            raise JournalError("Run event budget exceeded; no further device action authorized")
        with self.transaction() as connection:
            row = connection.execute("SELECT bytes,data FROM runs WHERE id=?", (metadata["id"],)).fetchone()
            if not row or row[0] + size > RUN_BYTE_LIMIT:
                raise JournalError("Run journal budget exceeded; no further device action authorized")
            if json.loads(row[1])["finishedAt"] is not None:
                raise JournalError("A finished run cannot accept further events")
            connection.execute("INSERT INTO events VALUES (?,?,?)", (metadata["id"], event["seq"], data))
            usage.record_call(connection, metadata["id"], event)
            connection.execute("UPDATE runs SET data=?,bytes=bytes+? WHERE id=?",
                               (encode(metadata), size, metadata["id"]))

    def finish(self, metadata, event):
        """One reserved terminal record; ordinary event exhaustion cannot strand recovery."""
        data, final = encode(event), encode(metadata)
        if (event["event"] != "run_finished" or metadata["finishedAt"] is None
                or metadata["status"] in {"queued", "running"}
                or event["status"] != metadata["status"]
                or len(data.encode()) > TERMINAL_BYTE_LIMIT or len(final.encode()) > TERMINAL_BYTE_LIMIT):
            raise JournalError("Invalid or oversized terminal run record")
        with self.transaction() as connection:
            row = connection.execute("SELECT data FROM runs WHERE id=?", (metadata["id"],)).fetchone()
            if not row or json.loads(row[0])["finishedAt"] is not None:
                raise JournalError("Run is missing or already finished")
            count = connection.execute("SELECT count(*) FROM events WHERE run_id=?", (metadata["id"],)).fetchone()[0]
            if event["seq"] != count or count > EVENT_LIMIT:
                raise JournalError("Terminal event does not follow the saved run history")
            connection.execute("INSERT INTO events VALUES (?,?,?)", (metadata["id"], count, data))
            connection.execute("UPDATE runs SET data=?,bytes=bytes+? WHERE id=?",
                               (final, len(data.encode()), metadata["id"]))
            usage.close_pending(connection, metadata["id"], event["timestamp"])

    def load(self):
        with self.lock:
            records = []
            try:
                for identifier, data in self.connection.execute("SELECT id,data FROM runs ORDER BY rowid"):
                    record = json.loads(data)
                    record["events"] = [json.loads(row[0]) for row in self.connection.execute(
                        "SELECT data FROM events WHERE run_id=? ORDER BY seq", (identifier,))]
                    if [e["seq"] for e in record["events"]] != list(range(len(record["events"]))):
                        raise ValueError("Non-contiguous journal")
                    records.append(record)
                return records
            except (ValueError, KeyError, TypeError, sqlite3.Error):
                raise JournalError("Run journal is unreadable; no automatic recovery or replay attempted") from None

    def usage(self, run_id=None):
        with self.lock:
            return usage.summary(self.connection, run_id)

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.lease:
            self.lease.close()
            self.lease = None
