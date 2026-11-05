"""Outlets' state in the run journal: schema ``outlets`` v2, in the same private SQLite file (0600)."""

import json
import sqlite3

from ..journal import JournalError

SCHEMA = "outlets"
VERSION = 3
MAX_DESTINATIONS = 50
# Deliveries kept: the newest, for the runs History still has.
MAX_DELIVERIES = 2000
STATUSES = ("sending", "sent", "failed")
EVENTS = ("run.finished", "run.started", "needs_you")
LOG_MAX = 50                     # deliveries one destination's log shows


def migrate(connection, from_version):
    """journal.register_schema's migration: v0 -> v1 creates the tables; v1 -> v2 adds the kinds."""
    if from_version < 1:
        connection.execute("""CREATE TABLE IF NOT EXISTS outlet_destinations (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            hint TEXT NOT NULL,
            has_secret INTEGER NOT NULL DEFAULT 0,
            screenshot INTEGER NOT NULL DEFAULT 0,
            last_test TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS outlet_deliveries (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            destination_id TEXT NOT NULL,
            destination_name TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('sending', 'sent', 'failed')),
            attempts INTEGER NOT NULL DEFAULT 0,
            http_status INTEGER,
            error TEXT,
            next_at REAL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL)""")
        connection.execute("CREATE INDEX IF NOT EXISTS outlet_deliveries_run ON outlet_deliveries(run_id)")
    if from_version < 2:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(outlet_destinations)")}
        if "kind" not in columns:
            connection.execute("ALTER TABLE outlet_destinations ADD COLUMN kind TEXT NOT NULL DEFAULT 'webhook'")
        if "config" not in columns:
            connection.execute("ALTER TABLE outlet_destinations ADD COLUMN config TEXT")
        if "inflight_at" not in {row[1] for row in connection.execute("PRAGMA table_info(outlet_deliveries)")}:
            connection.execute("ALTER TABLE outlet_deliveries ADD COLUMN inflight_at REAL")
    if from_version < 3:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(outlet_deliveries)")}
        if "event" not in columns:
            connection.execute("ALTER TABLE outlet_deliveries ADD COLUMN event TEXT NOT NULL DEFAULT 'run.finished'")
        if "payload" not in columns:
            connection.execute("ALTER TABLE outlet_deliveries ADD COLUMN payload TEXT")
        connection.execute("CREATE INDEX IF NOT EXISTS outlet_deliveries_destination "
                           "ON outlet_deliveries(destination_id, created_at)")


DESTINATION_COLUMNS = ("id", "name", "hint", "has_secret", "screenshot", "last_test", "created_at", "updated_at",
                       "kind", "config")
DELIVERY_COLUMNS = ("id", "run_id", "destination_id", "destination_name", "status", "attempts", "http_status", "error",
                    "next_at", "created_at", "updated_at", "inflight_at", "event", "payload")


def _destination(row):
    if row is None:
        return None
    value = dict(zip(DESTINATION_COLUMNS, row))
    value["has_secret"], value["screenshot"] = bool(value["has_secret"]), bool(value["screenshot"])
    for key in ("last_test", "config"):
        try:
            value[key] = json.loads(value[key]) if value[key] else None
        except ValueError:
            value[key] = None
    value["kind"] = value["kind"] or "webhook"
    value["config"] = value["config"] if isinstance(value["config"], dict) else {}
    return value


def _delivery(row):
    if row is None:
        return None
    value = dict(zip(DELIVERY_COLUMNS, row))
    value["event"] = value["event"] or "run.finished"
    try:
        value["payload"] = json.loads(value["payload"]) if value["payload"] else None
    except ValueError:
        value["payload"] = None
    return value


class OutletStore:
    def __init__(self, journal):
        self.journal = journal

    def _read(self, sql, args=()):
        with self.journal.lock:
            if self.journal.connection is None:
                raise JournalError("The run journal is closed")
            try:
                return self.journal.connection.execute(sql, args).fetchall()
            except sqlite3.Error:
                raise JournalError("Mobster could not read its saved destinations") from None

    def destinations(self):
        cols = ", ".join(DESTINATION_COLUMNS)
        return [_destination(row) for row in self._read(f"SELECT {cols} FROM outlet_destinations "
                                                        "ORDER BY created_at, rowid")]

    def destination(self, destination_id):
        cols = ", ".join(DESTINATION_COLUMNS)
        rows = self._read(f"SELECT {cols} FROM outlet_destinations WHERE id=?", (destination_id,))
        return _destination(rows[0]) if rows else None

    def count(self):
        return self._read("SELECT count(*) FROM outlet_destinations")[0][0]

    def add_destination(self, row):
        with self.journal.transaction() as connection:
            connection.execute(f"INSERT INTO outlet_destinations({', '.join(DESTINATION_COLUMNS)}) "
                               f"VALUES ({', '.join('?' * len(DESTINATION_COLUMNS))})",
                               [row["id"], row["name"], row["hint"], int(bool(row["has_secret"])),
                                int(bool(row["screenshot"])), None, row["created_at"], row["updated_at"],
                                row.get("kind") or "webhook", json.dumps(row.get("config") or {})])
        return self.destination(row["id"])

    def update_destination(self, destination_id, **changes):
        allowed = {"name", "hint", "has_secret", "screenshot", "last_test", "updated_at", "config"}
        if set(changes) - allowed:
            raise ValueError("Unknown destination field")
        values = []
        for key, value in changes.items():
            if key in ("has_secret", "screenshot"):
                value = int(bool(value))
            elif key in ("last_test", "config"):
                value = json.dumps(value) if value is not None else None
            values.append(value)
        if values:
            with self.journal.transaction() as connection:
                connection.execute(f"UPDATE outlet_destinations SET {', '.join(f'{k}=?' for k in changes)} WHERE id=?",
                                   [*values, destination_id])
        return self.destination(destination_id)

    def delete_destination(self, destination_id):
        with self.journal.transaction() as connection:
            connection.execute("DELETE FROM outlet_destinations WHERE id=?", (destination_id,))

    def add_delivery(self, row):
        with self.journal.transaction() as connection:
            connection.execute(f"INSERT INTO outlet_deliveries({', '.join(DELIVERY_COLUMNS)}) "
                               f"VALUES ({', '.join('?' * len(DELIVERY_COLUMNS))})",
                               [json.dumps(row["payload"], ensure_ascii=False) if column == "payload" and
                                row.get("payload") is not None else
                                (row.get(column) or "run.finished") if column == "event" else row.get(column)
                                for column in DELIVERY_COLUMNS])
            excess = connection.execute("SELECT count(*) FROM outlet_deliveries").fetchone()[0] - MAX_DELIVERIES
            if excess > 0:
                connection.execute("DELETE FROM outlet_deliveries WHERE rowid IN (SELECT rowid FROM outlet_deliveries "
                                   "WHERE status != 'sending' ORDER BY created_at LIMIT ?)", (excess,))
        return self.delivery(row["id"])

    def delivery(self, delivery_id):
        rows = self._read(f"SELECT {', '.join(DELIVERY_COLUMNS)} FROM outlet_deliveries WHERE id=?", (delivery_id,))
        return _delivery(rows[0]) if rows else None

    def deliveries_for(self, run_id):
        return [_delivery(row) for row in self._read(
            f"SELECT {', '.join(DELIVERY_COLUMNS)} FROM outlet_deliveries WHERE run_id=? ORDER BY created_at, rowid",
            (run_id,))]

    def deliveries_to(self, destination_id, limit=LOG_MAX):
        """A destination's newest deliveries first: its delivery log."""
        return [_delivery(row) for row in self._read(
            f"SELECT {', '.join(DELIVERY_COLUMNS)} FROM outlet_deliveries WHERE destination_id=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?", (destination_id, max(1, min(int(limit), LOG_MAX))))]

    def pending(self):
        return [_delivery(row) for row in self._read(
            f"SELECT {', '.join(DELIVERY_COLUMNS)} FROM outlet_deliveries WHERE status='sending' ORDER BY next_at")]

    def update_delivery(self, delivery_id, **changes):
        allowed = {"status", "attempts", "http_status", "error", "next_at", "updated_at", "inflight_at"}
        if set(changes) - allowed:
            raise ValueError("Unknown delivery field")
        with self.journal.transaction() as connection:
            connection.execute(f"UPDATE outlet_deliveries SET {', '.join(f'{k}=?' for k in changes)} WHERE id=?",
                               [*changes.values(), delivery_id])
        return self.delivery(delivery_id)

    def forget_run(self, run_id):
        with self.journal.transaction() as connection:
            connection.execute("DELETE FROM outlet_deliveries WHERE run_id=?", (run_id,))
