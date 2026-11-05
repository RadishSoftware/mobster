"""Connectors' state in the run journal: schema ``connectors`` v1, in the same private SQLite file (0600).

``connectors``: one row per MCP server the person added.

| Column | What it holds |
|---|---|
| id | 12 hex characters |
| name | what the person and the model call it ("Notion"); unique, case ignored |
| transport | "stdio" or "http" |
| command, args | stdio: the command and its arguments (JSON list). Never an env value |
| url | http: the server's URL |
| auth | "none", "token" or "oauth": what the Keychain item holds (vault.py) |
| enabled | 0 or 1 |
| preset | the preset's key ("notion"), or NULL |
| plugin | the plugin that added it, or NULL (plugins track) |
| tools | the last ``tools/list``, trimmed (JSON list of {name, description, inputSchema, readOnly}) |
| tools_at | when ``tools`` was read |
| modes | {tool: "read" \\| "ask" \\| "off"} the person chose (JSON). A tool not in it is "ask" |
| last_test | {ok, at, message} of the last connect (JSON) |
| created_at, updated_at | seconds |

Writes go through ``Journal.transaction()``, reads hold ``Journal.lock``.
"""

import json
import sqlite3

from ..journal import JournalError
from . import presets

SCHEMA = "connectors"
VERSION = 1
MAX_CONNECTORS = 50
MODES = ("read", "ask", "off")
DEFAULT_MODE = "ask"          # unknown means ask: a tool nobody chose for never runs unasked
COLUMNS = ("id", "name", "transport", "command", "args", "url", "auth", "enabled", "preset", "plugin", "tools",
           "tools_at", "modes", "last_test", "created_at", "updated_at")
JSON_COLUMNS = ("args", "tools", "modes", "last_test")


def migrate(connection, from_version):
    if from_version < 1:
        connection.execute("""CREATE TABLE IF NOT EXISTS connectors (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            transport TEXT NOT NULL CHECK (transport IN ('stdio', 'http')),
            command TEXT,
            args TEXT,
            url TEXT,
            auth TEXT NOT NULL DEFAULT 'none',
            enabled INTEGER NOT NULL DEFAULT 1,
            preset TEXT,
            plugin TEXT,
            tools TEXT,
            tools_at REAL,
            modes TEXT,
            last_test TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL)""")


def mode_of(row, tool):
    """The mode of ``tool`` on a connector row: what the person chose, else "ask"."""
    mode = (row.get("modes") or {}).get(tool)
    mode = mode if mode in MODES else DEFAULT_MODE
    # A tool of its preset's always_ask (a send to other people) never runs unasked: "read" counts as "ask".
    if mode == "read" and tool in (presets.preset(row.get("preset")) or {}).get("always_ask", ()):
        return "ask"
    return mode


def _row(values):
    if values is None:
        return None
    row = dict(zip(COLUMNS, values))
    for column in JSON_COLUMNS:
        try:
            row[column] = json.loads(row[column]) if row[column] else None
        except ValueError:
            row[column] = None
    row["args"] = row["args"] or []
    row["tools"] = row["tools"] or []
    row["modes"] = row["modes"] if isinstance(row["modes"], dict) else {}
    row["enabled"] = bool(row["enabled"])
    return row


class ConnectorStore:
    def __init__(self, journal):
        self.journal = journal

    def _read(self, sql, args=()):
        with self.journal.lock:
            if self.journal.connection is None:
                raise JournalError("The run journal is closed")
            try:
                return self.journal.connection.execute(sql, args).fetchall()
            except sqlite3.Error:
                raise JournalError("Mobster could not read its connectors") from None

    def all(self):
        return [_row(r) for r in self._read(f"SELECT {', '.join(COLUMNS)} FROM connectors ORDER BY created_at, rowid")]

    def get(self, connector_id):
        rows = self._read(f"SELECT {', '.join(COLUMNS)} FROM connectors WHERE id=?", (connector_id,))
        return _row(rows[0]) if rows else None

    def count(self):
        return self._read("SELECT count(*) FROM connectors")[0][0]

    def add(self, row):
        values = []
        for column in COLUMNS:
            value = row.get(column)
            if column in JSON_COLUMNS:
                value = json.dumps(value) if value is not None else None
            elif column == "enabled":
                value = int(bool(value if value is not None else True))
            values.append(value)
        with self.journal.transaction() as connection:
            connection.execute(f"INSERT INTO connectors({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
                               values)
        return self.get(row["id"])

    def update(self, connector_id, **changes):
        allowed = set(COLUMNS) - {"id", "created_at"}
        if set(changes) - allowed:
            raise ValueError("Unknown connector field")
        values = []
        for key, value in changes.items():
            if key in JSON_COLUMNS:
                value = json.dumps(value) if value is not None else None
            elif key == "enabled":
                value = int(bool(value))
            values.append(value)
        if values:
            with self.journal.transaction() as connection:
                connection.execute(f"UPDATE connectors SET {', '.join(f'{k}=?' for k in changes)} WHERE id=?",
                                   [*values, connector_id])
        return self.get(connector_id)

    def delete(self, connector_id):
        with self.journal.transaction() as connection:
            connection.execute("DELETE FROM connectors WHERE id=?", (connector_id,))
