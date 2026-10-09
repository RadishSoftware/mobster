"""Private, crash-safe local run journal. Intent is durable before a device mutation."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import threading
import time

from . import usage
from .errors import MobsterError


class JournalError(MobsterError):
    pass


class LeaseHeld(JournalError):
    """Another process holds this lease: the phone or the journal is in use, nothing is damaged."""


# A Smart turn journals at least four events (inference_started, inference_finished, frontier_decision, cost) plus
# about two per action: 1,024 ended a 200-step run near step 150 (SPEC S5).
EVENT_LIMIT = 4096
RUN_BYTE_LIMIT = 16_000_000
TERMINAL_BYTE_LIMIT = 1_000_000
# Request keys kept for replay protection (see ``create``).
REQUEST_LIMIT = 10000


# How long Lease.device tries again while a lease is held (a device listing's probe holds it for an instant).
DEVICE_LEASE_WAIT = 0.1
DEVICE_LEASE_RETRY = 0.02


class Lease:
    """Process lease; never unlink a locked inode, which would allow a second owner."""
    def __init__(self, path):
        self.fd = None
        self.legacy = []  # the same phone's locks as earlier builds name them (``device``)
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
                raise LeaseHeld("Another Mobster process already owns this resource") from None
            self.fd = fd
        except OSError:
            raise JournalError("Cannot acquire the private Mobster lease") from None

    @classmethod
    def device(cls, identity, wait=DEVICE_LEASE_WAIT):
        """The one lease per phone, whoever asks: the Mac app, `mobster serve`, `run` or the terminal UI.

        Its folder is fixed per user (the data folder), not the temporary folder: $TMPDIR differs between a
        login shell, ssh, cron and launchd, and two processes that disagree on it could drive one phone.

        A lease held is tried again for up to ``wait`` seconds: listing devices (devices.lease_free, wait=0)
        takes each phone's lease for an instant to see whether it is free, and a task starting at that instant
        must not be refused as if the phone were in use.
        """
        deadline = time.monotonic() + max(0.0, wait)
        while True:
            try:
                return cls._device(identity)
            except LeaseHeld:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(DEVICE_LEASE_RETRY)

    @classmethod
    def _device(cls, identity):
        from .paths import user_data_dir
        directory = user_data_dir() / "device-leases"
        directory.parent.mkdir(parents=True, exist_ok=True)
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise JournalError("Device lease directory must be private and owned by this user")
        lease = cls(directory / (hashlib.sha256(device_identity(identity).encode()).hexdigest() + ".lock"))
        # Builds up to 0.1.0 lock the address as spelled, in the temporary folder. The Mac app's bundled agent
        # and the CLI update apart, so an older one may be driving this phone under that lock alone: hold it
        # too, and treat it held as the phone in use.
        try:
            for spelling in legacy_device_spellings(identity):
                held = cls._legacy_device(spelling)
                if held is not None:
                    lease.legacy.append(held)
        except BaseException:
            lease.close()
            raise
        return lease

    @classmethod
    def _legacy_device(cls, spelling):
        """The earlier builds' lock for this spelling, or None where it cannot be taken for another reason
        than being held (its folder missing or not private): that lock then guards nothing."""
        directory = Path(tempfile.gettempdir()) / f"mobster-device-leases-{os.getuid()}"
        try:
            directory.mkdir(mode=0o700, exist_ok=True)
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                return None
            return cls(directory / (hashlib.sha256(spelling.encode()).hexdigest() + ".lock"))
        except LeaseHeld:
            raise
        except (OSError, JournalError):
            return None

    def close(self):
        for held in self.legacy:
            held.close()
        self.legacy = []
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def legacy_device_spellings(identity):
    """The names builds up to 0.1.0 gave this phone's lock: the address as each caller spelled it (the Mac app
    without a trailing slash, `mobster run` as typed), a socket path resolved."""
    text = str(identity)
    if text.startswith("/"):
        return [str(Path(text).resolve())]
    return list(dict.fromkeys((text, text.rstrip("/"))))


def device_identity(identity):
    """One name per phone however its address is spelled: a socket path resolved; a WDA URL as host and port,
    with the scheme's default port filled in, the path dropped, and every loopback spelling (localhost,
    127.0.0.1, ::1, ::ffff:127.0.0.1) one host."""
    import ipaddress
    from urllib.parse import urlsplit
    text = str(identity)
    if text.startswith("/"):
        return str(Path(text).resolve())
    parts = urlsplit(text.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return text
    host = parts.hostname.lower().rstrip(".")
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
        address = getattr(address, "ipv4_mapped", None) or address
        host = "loopback" if address.is_loopback else address.compressed
    except ValueError:
        if host == "localhost" or host.endswith(".localhost"):
            host = "loopback"
    try:
        port = parts.port
    except ValueError:
        return text
    port = port or (443 if parts.scheme.lower() == "https" else 80)
    return f"wda://{host}:{port}"


def encode(value):
    return json.dumps(value, allow_nan=False, separators=(",", ":"))


log = logging.getLogger("mobster.journal")

# Track tables in the run journal (seam S5): name -> (version, migrate). Registered by a track's register(api).
SCHEMAS: dict = {}
SCHEMA_OWNERS: dict = {}     # name -> the track that registered it (tracks.STATUS key)
SCHEMA_NAME = re.compile(r"[a-z_]{3,32}")


def register_schema(name, version, migrate):
    """A track's tables in the journal. ``migrate(conn, from_version)`` runs inside BEGIN IMMEDIATE and creates or
    alters only its own tables (``from_version`` 0: none yet). When a journal opens, for each schema in name order:
    a stored version newer than ``version`` turns the track off ("error: newer data": its routes answer 503); an
    older one migrates in its own transaction, and a migration that raises rolls back and turns the track off
    ("error: migration"). The run journal always opens; PRAGMA user_version stays 1, so older builds still open
    the file and ignore the new tables."""
    if not isinstance(name, str) or not SCHEMA_NAME.fullmatch(name):
        raise ValueError("A schema's name is 3 to 32 lowercase letters or underscores")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("A schema's version is a whole number from 1")
    if not callable(migrate):
        raise ValueError("A schema needs migrate(conn, from_version)")
    if name in SCHEMAS:
        raise ValueError(f"The schema {name} is already registered")
    from . import harness_api
    SCHEMAS[name] = (version, migrate)
    SCHEMA_OWNERS[name] = harness_api._owner[0] or name


class Journal:
    def __init__(self, path=None):
        self.lock = threading.RLock()
        self.lease = None
        self.connection = None
        self.expiry_listeners = []
        self.schema_status = {}   # schema name -> "ok" | "newer" | "migration_failed"
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
                CREATE TABLE IF NOT EXISTS schema_versions (
                    name TEXT PRIMARY KEY, version INTEGER NOT NULL);
                PRAGMA user_version=1;
            """)
            self._migrate_schemas()
            usage.initialize(self.connection)
        except LeaseHeld:
            # In use, not damaged: say so rather than suggest the data is at risk.
            self.close()
            raise LeaseHeld("Another Mobster process has this task history open") from None
        except Exception:
            self.close()
            raise JournalError("Cannot open the run journal; existing data was not reset") from None

    def _migrate_schemas(self):
        """Bring each registered track schema up to date, each in its own transaction (see register_schema).
        Never raises: a schema only ever turns its own track off."""
        from . import tracks
        for name in sorted(SCHEMAS):
            version, migrate = SCHEMAS[name]
            track = SCHEMA_OWNERS.get(name, name)
            try:
                row = self.connection.execute("SELECT version FROM schema_versions WHERE name=?", (name,)).fetchone()
            except sqlite3.Error:
                self.schema_status[name] = "migration_failed"
                tracks.disable(track, "error: migration")
                continue
            stored = row[0] if row else 0
            if stored > version:
                self.schema_status[name] = "newer"
                tracks.disable(track, "error: newer data")
                continue
            if stored == version:
                self.schema_status[name] = "ok"
                continue
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                migrate(self.connection, stored)
                self.connection.execute(
                    "INSERT INTO schema_versions(name, version) VALUES (?, ?) "
                    "ON CONFLICT(name) DO UPDATE SET version=excluded.version", (name, version))
                self.connection.execute("COMMIT")
                self.schema_status[name] = "ok"
            except Exception:  # noqa: BLE001 -- a track's migration never blocks the run journal
                log.exception("the %s schema could not migrate from version %s", name, stored)
                if self.connection.in_transaction:
                    self.connection.execute("ROLLBACK")
                self.schema_status[name] = "migration_failed"
                tracks.disable(track, "error: migration")

    def add_expiry_listener(self, fn):
        """``fn(run_ids)`` after create()'s retention and delete() commit, with the removed run ids. Exceptions
        are logged."""
        if not callable(fn):
            raise ValueError("An expiry listener is a function")
        self.expiry_listeners.append(fn)

    def _expired(self, run_ids):
        if not run_ids:
            return
        for fn in list(self.expiry_listeners):
            try:
                fn(list(run_ids))
            except Exception:  # noqa: BLE001
                log.exception("an expiry listener failed")

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
            if key:
                self._make_room(connection)
            connection.execute("INSERT INTO runs(id,data) VALUES (?,?)", (metadata["id"], encode(metadata)))
            if key:
                connection.execute("INSERT INTO requests VALUES (?,?,?)", (key, metadata["id"], fingerprint))
            # Retention and insertion are one commit. A failed task must not erase history.
            expired = [row[0] for row in connection.execute(
                "SELECT id FROM runs ORDER BY rowid DESC LIMIT -1 OFFSET 100")]
            connection.executemany("DELETE FROM runs WHERE id=?", [(identifier,) for identifier in expired])
            # Request tombstones deliberately survive run retention, preventing replay.
        self._expired(expired)
        return expired

    @staticmethod
    def _make_room(connection):
        """Keep at most REQUEST_LIMIT request keys: once full, forget the oldest keys whose run already left
        history (a retry that late comes thousands of tasks after it). A key whose run is kept is never
        forgotten, so a replay of any task still in history returns that task."""
        excess = connection.execute("SELECT count(*) FROM requests").fetchone()[0] - REQUEST_LIMIT + 1
        if excess > 0:
            connection.execute("""DELETE FROM requests WHERE rowid IN (SELECT rowid FROM requests
                WHERE run_id NOT IN (SELECT id FROM runs) ORDER BY rowid LIMIT ?)""", (excess,))
            if connection.execute("SELECT count(*) FROM requests").fetchone()[0] >= REQUEST_LIMIT:
                raise JournalError("Idempotency journal reached its retention limit; operator maintenance required")

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

    def month_usd(self, now=None):
        """This calendar month's published-rate spend across the workspace (see usage.month_usd)."""
        with self.lock:
            return usage.month_usd(self.connection, now)

    def delete(self, run_id):
        """Remove a run and its events. Usage rows and request tombstones stay: spend already happened,
        and a replayed request must not start the task again."""
        with self.transaction() as connection:
            deleted = connection.execute("DELETE FROM runs WHERE id=?", (run_id,)).rowcount == 1
        if deleted:
            self._expired([run_id])
        return deleted

    def annotate(self, run_id, changes):
        """Merge ``changes`` into a saved run's metadata (a finished run's review mark)."""
        with self.transaction() as connection:
            row = connection.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise JournalError("Run is missing")
            data = json.loads(row[0])
            data.update(changes)
            connection.execute("UPDATE runs SET data=? WHERE id=?", (encode(data), run_id))

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.lease:
            self.lease.close()
            self.lease = None
