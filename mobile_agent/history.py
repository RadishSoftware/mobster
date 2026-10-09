"""`mobster history`: the terminal UI's past tasks, read without taking the journal's lock.

The journal is SQLite in WAL mode, so a read-only connection can list tasks
while the terminal UI has the journal open and is writing to it.
"""

import json
import sqlite3
import sys
import time


def read_history(path, limit=20):
    """Task records (the journal's run metadata, no events), newest first. [] when there is no journal."""
    from pathlib import Path
    path = Path(path)
    if not path.is_file():
        return []
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    try:
        rows = connection.execute("SELECT data FROM runs ORDER BY rowid DESC LIMIT ?", (max(0, limit),)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()
    records = []
    for (data,) in rows:
        try:
            record = json.loads(data)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def print_history(limit=20, as_json=False, path=None, stream=None):
    from .narrate import status_label, status_tone
    from .tui.session import default_history_db
    stream = stream or sys.stdout
    records = read_history(path or default_history_db(), limit)
    if as_json:
        for record in records:
            summary = record.get("summary") or {}
            stream.write(json.dumps({"id": record.get("id"), "app": record.get("appName"), "goal": record.get("goal"),
                                     "status": record.get("status"), "createdAt": record.get("createdAt"),
                                     "finishedAt": record.get("finishedAt"), "reason": summary.get("reason")},
                                    allow_nan=False) + "\n")
        return 0
    if not records:
        stream.write("No tasks yet. Run `mobster` (or `mobster --demo`) and give it one.\n")
        return 0
    from .console import Palette, color_enabled
    paint = Palette(color_enabled(stream))
    roles = {"success": "green", "error": "red", "warning": "amber", "review": "accent", "neutral": "muted"}
    for record in records:
        status = record.get("status") or ""
        created = (record.get("createdAt") or 0) / 1000
        when = time.strftime("%b %d %H:%M", time.localtime(created)) if created else ""
        goal = (record.get("goal") or "")[:56]
        stream.write(f"{paint(record.get('id', ''), 'faint')}  {when:<12}  "
                     f"{paint(f'{status_label(status):<22}', roles.get(status_tone(status), 'muted'))}"
                     f"{(record.get('appName') or '')[:12]:<13} {goal}\n")
    stream.write(paint("\nOpen one with: mobster --resume ID\nSave one as a GIF: mobster export ID --gif\n", "faint"))
    return 0
