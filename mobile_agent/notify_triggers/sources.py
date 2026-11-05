"""Where macOS keeps delivered notifications, read-only, tailed by polling."""

import datetime
import os
import plistlib
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

MAC_EPOCH = 978307200.0   # 2001-01-01T00:00:00Z as a Unix time
HOME = Path(os.path.expanduser("~"))
MAC_DB = HOME / "Library/Group Containers/group.com.apple.usernoted/db2/db"
IPHONE_STORE = HOME / ("Library/Group Containers/group.com.apple.UserNotifications/Library/UserNotifications/"
                       "Remote/default")
DELIVERED = "DeliveredNotifications.plist"
LIBRARY = "Library.plist"
# A notification dated this long before the watcher started is old, whatever the store says.
SLACK_S = {"mac": 5.0, "iphone": 120.0}
FIELD_MAX = 2000
ACCESS = ("Mobster can't read {where}. Give the app that runs it Full Disk Access in System Settings › Privacy & "
          "Security, then start it again.")


class SourceError(Exception):
    """A store couldn't be read this time. ``message`` names the store, never a notification."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Notification:
    app: str
    title: str
    subtitle: str
    body: str
    date: float | None       # Unix time
    source: str              # "mac" or "iphone"
    id: str                  # the store's own id: usernoted's uuid, or the iPhone notification's identifier

    @property
    def key(self):
        return f"{self.source}:{self.app}:{self.id}"

    def text(self):
        """Title, subtitle and body, one per line, empty ones left out."""
        return "\n".join(part for part in (self.title, self.subtitle, self.body) if part)


def iso(seconds):
    if not isinstance(seconds, (int, float)):
        return None
    moment = datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _text(value):
    return " ".join(value.split())[:FIELD_MAX] if isinstance(value, str) else ""


def _signature(path):
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_ino, info.st_mtime_ns, info.st_size)


def mac_notification(uuid, app, data, request_date=None):
    """A Notification from one ``record`` row, or None when its plist can't be read."""
    try:
        value = plistlib.loads(data)
    except Exception:  # noqa: BLE001 -- a row Mobster can't read is skipped, never guessed at
        return None
    if not isinstance(value, dict):
        return None
    request = value.get("req") if isinstance(value.get("req"), dict) else {}
    when = value.get("date") if isinstance(value.get("date"), (int, float)) else request_date
    return Notification(
        app=str(app or value.get("app") or "").lower(), title=_text(request.get("titl")),
        subtitle=_text(request.get("subt")), body=_text(request.get("body")),
        date=when + MAC_EPOCH if isinstance(when, (int, float)) else None,
        source="mac", id=uuid.hex() if isinstance(uuid, bytes) else str(uuid))


class MacSource:
    name = "mac"
    where = "this Mac's notifications"

    def __init__(self, path=MAC_DB, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.started = clock()
        self.connection = None
        self.signature = None
        self.inode = None
        self.seen = None   # the record uuids there at the last read

    def _connect(self):
        if not self.path.exists():
            raise SourceError(f"{self.where} aren't where macOS keeps them.")
        try:
            # mode=ro: never writes the database (SQLite still maps the -shm to read the WAL).
            connection = sqlite3.connect(f"file:{quote(str(self.path))}?mode=ro", uri=True, timeout=0.25,
                                         check_same_thread=False)
            connection.execute("PRAGMA query_only = 1")
        except sqlite3.Error:
            raise SourceError(ACCESS.format(where=self.where)) from None
        self.connection = connection
        signature = _signature(self.path)
        self.inode = signature[0] if signature else None

    def close(self):
        connection, self.connection = self.connection, None
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass

    def _files(self):
        return (_signature(self.path), _signature(f"{self.path}-wal"))

    def prime(self):
        """Take what is there now as seen. SourceError when the database can't be read."""
        self.poll()

    def poll(self):
        """New notifications since the last poll. SourceError when the database can't be read."""
        files = self._files()
        if self.connection is not None and files == self.signature:
            return []
        if files[0] is None:
            self.close()
            self.signature = None
            raise SourceError(f"{self.where} aren't where macOS keeps them.")
        if self.connection is not None and files[0][0] != self.inode:
            self.close()   # usernoted made a new database (after a corrupt one): read it from the start
        if self.connection is None:
            self._connect()
        try:
            rows = self.connection.execute("SELECT uuid FROM record").fetchall()
            current = {bytes(row[0]) if isinstance(row[0], (bytes, memoryview)) else row[0] for row in rows}
            first = self.seen is None
            fresh = [] if first else [uuid for uuid in current if uuid not in self.seen]
            found = []
            if fresh:
                marks = ",".join("?" * len(fresh))
                found = self.connection.execute(
                    "SELECT r.rec_id, r.uuid, a.identifier, r.data, r.request_date FROM record r "
                    f"LEFT JOIN app a ON a.app_id = r.app_id WHERE r.uuid IN ({marks}) ORDER BY r.rec_id",
                    fresh).fetchall()
        except sqlite3.Error:
            self.close()
            self.signature = None
            raise SourceError(f"Mobster couldn't read {self.where}; it tries again.") from None
        self.signature, self.seen = files, current
        out = []
        oldest = self.started - SLACK_S[self.name]
        for _rec, uuid, app, data, request_date in found:
            notification = mac_notification(uuid, app, data, request_date)
            if notification is None or (notification.date is not None and notification.date < oldest):
                continue
            out.append(notification)
        return out


def unarchive(data, limit=20_000):
    """An NSKeyedArchiver plist as plain values: dicts, lists, strings, numbers, bytes and datetimes."""
    top = plistlib.loads(data)
    if not isinstance(top, dict) or not isinstance(top.get("$objects"), list):
        raise ValueError("not a keyed archive")
    objects = top["$objects"]
    done = {}
    budget = [limit]

    def resolve(value, depth=0):
        budget[0] -= 1
        if depth > 40 or budget[0] < 0:
            raise ValueError("archive too deep")
        if not isinstance(value, plistlib.UID):
            return value
        index = value.data
        if index in done:
            return done[index]
        item = objects[index]
        if item == "$null":
            return None
        if not isinstance(item, dict) or "$class" not in item:
            return item
        if "NS.keys" in item and "NS.objects" in item:
            out = done[index] = {}
            for key, val in zip(item["NS.keys"], item["NS.objects"]):
                out[resolve(key, depth + 1)] = resolve(val, depth + 1)
            return out
        if "NS.objects" in item:
            out = done[index] = []
            out.extend(resolve(val, depth + 1) for val in item["NS.objects"])
            return out
        if "NS.time" in item:
            return datetime.datetime.fromtimestamp(MAC_EPOCH + float(item["NS.time"]), datetime.timezone.utc)
        for name in ("NS.string", "NS.bytes", "NS.data"):
            if name in item:
                return resolve(item[name], depth + 1)
        out = done[index] = {}
        for key, val in item.items():
            if key != "$class":
                out[key] = resolve(val, depth + 1)
        return out

    root = top.get("$top", {}).get("root")
    return resolve(root)


def iphone_notification(entry, app=None):
    """A Notification from one entry of DeliveredNotifications.plist, or None."""
    if not isinstance(entry, dict):
        return None
    behavior = entry.get("EventBehavior") if isinstance(entry.get("EventBehavior"), dict) else {}
    details = behavior.get("eventDetails") if isinstance(behavior.get("eventDetails"), dict) else {}
    identifier = entry.get("AppNotificationIdentifier") or details.get("identifier")
    if not isinstance(identifier, str) or not identifier:
        return None
    created = entry.get("AppNotificationCreationDate")
    return Notification(
        app=str(app or details.get("bundleIdentifier") or "").lower(),
        title=_text(entry.get("AppNotificationTitle") or details.get("title")),
        subtitle=_text(entry.get("AppNotificationSubtitle") or details.get("subtitle")),
        body=_text(entry.get("AppNotificationMessage") or details.get("body")),
        date=created.timestamp() if isinstance(created, datetime.datetime) else None, source="iphone",
        id=identifier[:200])


class IPhoneSource:
    name = "iphone"
    where = "the notifications your iPhone forwards to this Mac"

    def __init__(self, root=IPHONE_STORE, clock=time.time):
        self.root = Path(root)
        self.clock = clock
        self.started = clock()
        self.apps = {}            # folder name (upper case) -> bundle id
        self.library = None       # Library.plist's signature when read
        self.files = {}           # folder -> DeliveredNotifications.plist's signature
        self.seen = {}            # folder -> the notification identifiers there at the last read
        self.primed = False

    def _read_library(self):
        signature = _signature(self.root / LIBRARY)
        if signature == self.library:
            return
        self.library = signature
        try:
            mapping = unarchive((self.root / LIBRARY).read_bytes()) if signature else {}
        except Exception:  # noqa: BLE001 -- unknown folders still read, with the app from the notification
            mapping = {}
        self.apps = {str(folder).upper(): str(bundle) for bundle, folder in (mapping or {}).items()
                     if isinstance(bundle, str) and isinstance(folder, str)}

    def prime(self):
        self.poll()
        self.primed = True

    def poll(self):
        if not self.root.is_dir():
            raise SourceError(f"{self.where} aren't on this Mac. Turn on iPhone Mirroring's notifications, or "
                              "watch this Mac's only.")
        try:
            folders = [entry.name for entry in os.scandir(self.root) if entry.is_dir(follow_symlinks=False)]
        except OSError:
            raise SourceError(ACCESS.format(where=self.where)) from None
        self._read_library()
        out = []
        oldest = self.started - SLACK_S[self.name]
        first = not self.primed and not self.files
        for folder in folders:
            path = self.root / folder / DELIVERED
            signature = _signature(path)
            if signature == self.files.get(folder):
                continue
            self.files[folder] = signature
            if signature is None:
                self.seen[folder] = set()
                continue
            try:
                entries = unarchive(path.read_bytes())
            except PermissionError:
                raise SourceError(ACCESS.format(where=self.where)) from None
            except Exception:  # noqa: BLE001 -- a file caught mid-write reads again at its next change
                self.files[folder] = None
                continue
            app = self.apps.get(folder.upper())
            found = [n for n in (iphone_notification(entry, app) for entry in (entries or [])
                                 if isinstance(entries, list)) if n is not None]
            before = self.seen.get(folder, set())
            self.seen[folder] = {n.id for n in found}
            if first:
                continue
            for notification in found:
                if notification.id in before:
                    continue
                if notification.date is not None and notification.date < oldest:
                    continue
                out.append(notification)
        for gone in set(self.files) - set(folders):
            self.files.pop(gone, None)
            self.seen.pop(gone, None)
        out.sort(key=lambda n: n.date or 0)
        return out

    def close(self):
        pass
