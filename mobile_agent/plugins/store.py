"""Plugins' state in the run journal: schema ``plugins`` v1, in the same private SQLite file (0600)."""

import json
import sqlite3

from ..journal import JournalError

SCHEMA = "plugins"
VERSION = 2
MAX_PLUGINS = 50
COLUMNS = ("name", "version", "description", "source", "enabled", "path", "skills", "added", "created_at", "updated_at",
           "author")


def migrate(connection, from_version):
    if from_version < 1:
        connection.execute("""CREATE TABLE IF NOT EXISTS plugins (
            name TEXT PRIMARY KEY,
            version TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            path TEXT NOT NULL,
            skills TEXT,
            added TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL)""")
    if from_version < 2:
        # Who makes it (plugin.json's author), shown as "by <author>" in Settings › Plugins.
        if "author" not in {row[1] for row in connection.execute("PRAGMA table_info(plugins)")}:
            connection.execute("ALTER TABLE plugins ADD COLUMN author TEXT NOT NULL DEFAULT ''")


def _row(values):
    if values is None:
        return None
    row = dict(zip(COLUMNS, values))
    for column in ("skills", "added"):
        try:
            row[column] = json.loads(row[column]) if row[column] else None
        except ValueError:
            row[column] = None
    row["skills"] = row["skills"] or []
    row["added"] = {"connectors": [], "workflows": [], "destinations": [], **(row["added"] or {})}
    row["enabled"] = bool(row["enabled"])
    return row


class PluginStore:
    def __init__(self, journal):
        self.journal = journal

    def _read(self, sql, args=()):
        with self.journal.lock:
            if self.journal.connection is None:
                raise JournalError("The run journal is closed")
            try:
                return self.journal.connection.execute(sql, args).fetchall()
            except sqlite3.Error:
                raise JournalError("Mobster could not read its plugins") from None

    def all(self):
        return [_row(r) for r in self._read(f"SELECT {', '.join(COLUMNS)} FROM plugins ORDER BY created_at, rowid")]

    def get(self, name):
        rows = self._read(f"SELECT {', '.join(COLUMNS)} FROM plugins WHERE name=?", (name,))
        return _row(rows[0]) if rows else None

    def put(self, row):
        values = [json.dumps(row[c]) if c in ("skills", "added") else int(bool(row[c])) if c == "enabled" else row[c]
                  for c in COLUMNS]
        with self.journal.transaction() as connection:
            connection.execute(f"INSERT OR REPLACE INTO plugins({', '.join(COLUMNS)}) "
                               f"VALUES ({', '.join('?' * len(COLUMNS))})", values)
        return self.get(row["name"])

    def delete(self, name):
        with self.journal.transaction() as connection:
            connection.execute("DELETE FROM plugins WHERE name=?", (name,))
