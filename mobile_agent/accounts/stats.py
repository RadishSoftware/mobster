"""An account's numbers over time: one row a day per app and handle, in ``account-stats.json`` beside accounts.json."""

import json
import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

FILE = "account-stats.json"
KEEP_DAYS = 400
FIELDS = ("followers", "following", "posts", "likes", "period", "views", "reach", "profile_visits", "follower_change")
_lock = threading.Lock()


def _load(folder):
    try:
        data = json.loads((Path(folder) / FILE).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record(folder, app, handle, values, now=None):
    """Merge ``values`` (any of FIELDS) into today's row for ``app`` and ``handle``;
    a value of None keeps what is there."""
    now = now or datetime.now(timezone.utc)
    key, today = f"{app}/{handle.lstrip('@').casefold()}", now.date().isoformat()
    with _lock:
        data = _load(folder)
        rows = data.setdefault(key, {})
        row = rows.setdefault(today, {})
        row.update({k: v for k, v in values.items() if k in FIELDS and v is not None})
        row["at"] = now.isoformat(timespec="seconds")
        for old in sorted(rows)[:-KEEP_DAYS]:
            del rows[old]
        Path(folder).mkdir(parents=True, exist_ok=True)
        handle_, temporary = tempfile.mkstemp(prefix=".stats.", suffix=".json", dir=folder)
        with os.fdopen(handle_, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(json.dumps(data, indent=1) + "\n")
        os.replace(temporary, Path(folder) / FILE)


def series(folder, app, handle, days=30, now=None):
    """{app, handle, days, snapshots: [{date, at, ...FIELDS}] oldest first, latest: newest snapshot or None}."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).date().isoformat()
    handle = handle.lstrip("@").casefold()
    rows = _load(folder).get(f"{app}/{handle}", {})
    snapshots = [{"date": day, **{k: row.get(k) for k in ("at",) + FIELDS}} for day, row in sorted(rows.items())
                 if day >= since]
    return {"app": app, "handle": handle, "days": days, "snapshots": snapshots,
            "latest": snapshots[-1] if snapshots else None}
