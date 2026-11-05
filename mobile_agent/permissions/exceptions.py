"""Allow rules kept on this Mac; never for paying, buying, booking, deleting, a one-time code or an acting link."""

import json
import os
import tempfile
import threading
import time
from pathlib import Path

from .rules import norm

LIMIT = 500
SEP = "␟"


def key_of(app, label, op):
    return SEP.join((app or "", norm(label), (op or "").upper()))


def remember_in(store, spec):
    """Keep in ``store`` what a card's "Don't ask again" or "Always allow" offered (``spec``)."""
    apps = spec.get("apps")
    entries = [one for one in apps if isinstance(one, dict)] if isinstance(apps, list) else [spec]
    for entry in entries:
        store.add(entry.get("app"), spec.get("label"), spec.get("op"), entry.get("app_name") or "")


class Exceptions:
    def __init__(self, path):
        self.path = Path(path).expanduser()
        self._lock = threading.Lock()
        self._items = None
        self._stamp = None

    def _mtime(self):
        try:
            stat = self.path.stat()
            return stat.st_mtime_ns, stat.st_size
        except OSError:
            return None

    def _load(self):
        stamp = self._mtime()
        if self._items is None or stamp != self._stamp:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._items = {item["key"]: item for item in data if isinstance(item, dict) and item.get("key")}
            except (OSError, ValueError, TypeError):
                self._items = {}
            self._stamp = stamp
        return self._items

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".exceptions-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(list(self._items.values()), stream, ensure_ascii=False)
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
        self._stamp = self._mtime()

    def contains(self, app, label, op):
        with self._lock:
            return key_of(app, label, op) in self._load()

    def add(self, app, label, op, app_name=""):
        if not app or not norm(label):
            raise ValueError("An exception names an app and a control")
        with self._lock:
            items = self._load()
            key = key_of(app, label, op)
            items[key] = {"key": key, "app": app, "appName": " ".join(str(app_name or "").split())[:80],
                          "label": " ".join(str(label).split())[:120], "op": (op or "").upper(),
                          "addedAt": time.time() * 1000}
            while len(items) > LIMIT:
                items.pop(min(items, key=lambda k: items[k]["addedAt"]))
            self._save()
            return items[key]

    def remove(self, key):
        with self._lock:
            items = self._load()
            if items.pop(key, None) is None:
                return False
            self._save()
            return True

    def public(self):
        with self._lock:
            return sorted(self._load().values(), key=lambda item: -item["addedAt"])


def default_path():
    from ..paths import user_data_dir
    return user_data_dir() / "state" / "permission-exceptions.json"
