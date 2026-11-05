"""Triggers' state in the run journal: schema ``triggers`` v1, in the same private SQLite file (0600)."""

import json
import sqlite3

from ..journal import JournalError

SCHEMA = "triggers"
VERSION = 1
MAX_FIRINGS = 500
OUTCOMES = ("sending", "done", "skipped", "failed", "unknown")
COLUMNS = ("id", "name", "enabled", "app", "app_name", "words", "source", "action", "regex", "created_at", "updated_at")
FIRING_COLUMNS = ("id", "trigger_id", "trigger_name", "app", "at", "kind", "outcome", "message", "run_id")


def migrate(connection, from_version):
    """journal.register_schema's migration: v0 -> v1 creates the tables. Runs inside BEGIN IMMEDIATE."""
    if from_version < 1:
        connection.execute("""CREATE TABLE IF NOT EXISTS triggers (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0,
            app TEXT NOT NULL,
            app_name TEXT,
            words TEXT NOT NULL DEFAULT '[]',
            source TEXT NOT NULL DEFAULT 'iphone',
            action TEXT NOT NULL,
            regex TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS trigger_firings (
            id TEXT PRIMARY KEY,
            trigger_id TEXT NOT NULL,
            trigger_name TEXT NOT NULL,
            app TEXT NOT NULL,
            at REAL NOT NULL,
            kind TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK (outcome IN ('sending', 'done', 'skipped', 'failed', 'unknown')),
            message TEXT,
            run_id TEXT)""")
        connection.execute("CREATE INDEX IF NOT EXISTS trigger_firings_at ON trigger_firings(at)")


def _trigger(row):
    if row is None:
        return None
    value = dict(zip(COLUMNS, row))
    value["enabled"] = bool(value["enabled"])
    for key, default in (("words", []), ("action", {})):
        try:
            value[key] = json.loads(value[key]) if value[key] else default
        except ValueError:
            value[key] = default
    return value


class TriggerStore:
    def __init__(self, journal):
        self.journal = journal

    def _read(self, sql, args=()):
        with self.journal.lock:
            if self.journal.connection is None:
                raise JournalError("The run journal is closed")
            try:
                return self.journal.connection.execute(sql, args).fetchall()
            except sqlite3.Error:
                raise JournalError("Mobster could not read its saved triggers") from None

    def triggers(self):
        return [_trigger(row) for row in self._read(
            f"SELECT {', '.join(COLUMNS)} FROM triggers ORDER BY created_at, rowid")]

    def trigger(self, trigger_id):
        rows = self._read(f"SELECT {', '.join(COLUMNS)} FROM triggers WHERE id=?", (trigger_id,))
        return _trigger(rows[0]) if rows else None

    def count(self):
        return self._read("SELECT count(*) FROM triggers")[0][0]

    def add(self, row):
        with self.journal.transaction() as connection:
            connection.execute(
                f"INSERT INTO triggers({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
                [row["id"], row["name"], int(bool(row["enabled"])), row["app"], row.get("app_name"),
                 json.dumps(row.get("words") or []), row["source"], json.dumps(row["action"]), row.get("regex"),
                 row["created_at"], row["updated_at"]])
        return self.trigger(row["id"])

    def update(self, trigger_id, **changes):
        allowed = {"name", "enabled", "app", "app_name", "words", "source", "action", "updated_at"}
        if set(changes) - allowed:
            raise ValueError("Unknown trigger field")
        values = []
        for key, value in changes.items():
            values.append(int(bool(value)) if key == "enabled" else json.dumps(value) if key in ("words", "action")
                          else value)
        with self.journal.transaction() as connection:
            connection.execute(f"UPDATE triggers SET {', '.join(f'{k}=?' for k in changes)} WHERE id=?",
                               [*values, trigger_id])
        return self.trigger(trigger_id)

    def delete(self, trigger_id):
        with self.journal.transaction() as connection:
            connection.execute("DELETE FROM triggers WHERE id=?", (trigger_id,))

    def add_firing(self, row):
        with self.journal.transaction() as connection:
            connection.execute(
                f"INSERT INTO trigger_firings({', '.join(FIRING_COLUMNS)}) VALUES "
                f"({', '.join('?' * len(FIRING_COLUMNS))})",
                [row.get(column) for column in FIRING_COLUMNS])
            excess = connection.execute("SELECT count(*) FROM trigger_firings").fetchone()[0] - MAX_FIRINGS
            if excess > 0:
                connection.execute("DELETE FROM trigger_firings WHERE rowid IN (SELECT rowid FROM trigger_firings "
                                   "WHERE outcome != 'sending' ORDER BY at LIMIT ?)", (excess,))

    def update_firing(self, firing_id, **changes):
        if set(changes) - {"outcome", "message", "run_id"}:
            raise ValueError("Unknown firing field")
        with self.journal.transaction() as connection:
            connection.execute(f"UPDATE trigger_firings SET {', '.join(f'{k}=?' for k in changes)} WHERE id=?",
                               [*changes.values(), firing_id])

    def recent(self, limit=20):
        rows = self._read(f"SELECT {', '.join(FIRING_COLUMNS)} FROM trigger_firings ORDER BY at DESC, rowid DESC "
                          "LIMIT ?", (limit,))
        return [dict(zip(FIRING_COLUMNS, row)) for row in rows]

    def last_firing(self, trigger_id):
        rows = self._read(f"SELECT {', '.join(FIRING_COLUMNS)} FROM trigger_firings WHERE trigger_id=? "
                          "ORDER BY at DESC, rowid DESC LIMIT 1", (trigger_id,))
        return dict(zip(FIRING_COLUMNS, rows[0])) if rows else None

    def close_unknown(self):
        """A firing left ``sending`` when Mobster quit: its outcome is unknown, and it is never sent again."""
        with self.journal.transaction() as connection:
            cursor = connection.execute(
                "UPDATE trigger_firings SET outcome='unknown', message=? WHERE outcome='sending'",
                ("Mobster quit while sending this, so it may have arrived. "
                 "Check where it went before you run it again.",))
        return cursor.rowcount
