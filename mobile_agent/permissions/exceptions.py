"""What the person allowed, as allow rules, remembered on this Mac: an app Mobster may use ("Allow Mobster to use
Things?", policy.APP_OP), one kind of action in one app ("Don't ask again for sending messages in Messages",
rules.RULE_OP), and one app's links (deeplinks.decide: "Don't ask again for Things"). Never for paying, buying,
booking or deleting, a one-time code, or a link that acts as it opens (a call, FaceTime, a message, an email, running
a shortcut): the approval card doesn't offer it, and the gate and deeplinks.decide never look one up.

Kept in a small JSON file beside the run journal (``permission-exceptions.json``), at most ``LIMIT`` entries, each
the app's bundle id, the folded label (the kind of action), the operation, the app's name and when it was added. Settings › Safety lists
them and forgets one. The Mac app writes; ``mobster mcp`` (another process) reads, so a read notices a newer file.
"""

import json
import os
from pathlib import Path
import tempfile
import threading
import time

from .rules import norm

LIMIT = 500
SEP = "␟"


def key_of(app, label, op):
    return SEP.join((app or "", norm(label), (op or "").upper()))


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
