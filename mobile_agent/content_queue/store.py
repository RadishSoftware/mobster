"""queues.json and each queue's journal: the posts a person lines up, which profile posts each, how each ended."""

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path

STORE = "queues.json"
LOCK = "queues.lock"
JOURNALS = "queue-journal"
LOCK_TIMEOUT = 10.0
MAX_QUEUES = 50
MAX_ITEMS = 200
MAX_MEDIA = 10
CAPTION_CHARS = 2200          # Instagram's caption limit; TikTok's is larger
HASHTAGS = 30
SOUND_CHARS = 120
NAME_CHARS = 60
LATE_LIMIT_MS = 2 * 3600 * 1000
STAGGER_S = 90
STAGGER_MAX_S = 3600
WATCH_SEEN = 2000
ID = re.compile(r"[a-f0-9]{12}")
HASHTAG = re.compile(r"#?([\w.]{1,100})", re.UNICODE)
STATES = ("draft", "ready", "sending", "posting", "posted", "declined", "failed", "unknown", "missed")
ADD_PHOTOS = "Add photos"      # a draft's note: a caption and a profile, no media yet. It never goes out as it is.
APPROVALS = ("each", "batch")
FORMATS = ("auto", "post", "reel", "story", "video")
EDITABLE = frozenset({"ready", "declined", "failed", "missed"})


class QueueError(ValueError):
    def __init__(self, message, code="invalid", status=400):
        super().__init__(message)
        self.code, self.status = code, status


def now_ms():
    return round(time.time() * 1000)


def armed_for(queue, item):
    """Whether ``item`` may go out now: its queue runs, for every profile or for the item's."""
    scoped = queue.get("armedProfiles")
    return bool(queue.get("armed")) and (scoped is None or item.get("profile") in scoped)


def _text(value, name, limit):
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        raise QueueError(f"{name} is text of at most {limit:,} characters.")
    return value.strip()


def clean_hashtags(value):
    """["#spring", "#sale"] from a list or a line ("spring, #sale"): each starts with #, no duplicates, at most 30."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = re.split(r"[\s,]+", value)
    if not isinstance(value, list):
        raise QueueError("hashtags is a list, or one line of them.")
    tags = []
    for raw in value:
        if not isinstance(raw, str) or not raw.strip():
            continue
        found = HASHTAG.fullmatch(raw.strip())
        if not found:
            raise QueueError(f"{raw[:40]!r} isn't a hashtag: letters, digits, underscores and dots.")
        tag = "#" + found.group(1)
        if tag.casefold() not in {t.casefold() for t in tags}:
            tags.append(tag)
    if len(tags) > HASHTAGS:
        raise QueueError(f"A post takes at most {HASHTAGS} hashtags.")
    return tags


def clean_media(value):
    """[{path, name, bytes, sha256, kind}] of files on this Mac that Photos takes, read now."""
    from ..phone_io import PhoneIOError
    from ..phone_io.photos import digest, sniff
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_MEDIA:
        raise QueueError(f"A post has 1 to {MAX_MEDIA} photos or videos.")
    media = []
    for entry in value:
        path = entry.get("path") if isinstance(entry, dict) else entry
        if not isinstance(path, str) or not path.strip() or len(path) > 1024:
            raise QueueError("Each photo or video is a path on this Mac.")
        file = Path(path).expanduser()
        if not file.is_absolute() or not file.is_file():
            raise QueueError(f"There is no file at {path}.", "media_missing")
        try:
            kind, _ = sniff(file)
        except PhoneIOError as error:
            raise QueueError(str(error), "not_media") from None
        known = entry.get("sha256") if isinstance(entry, dict) else None
        size = file.stat().st_size
        same = isinstance(entry, dict) and entry.get("bytes") == size and isinstance(known, str)
        media.append({"path": str(file), "name": file.name, "bytes": size, "kind": kind,
                      "sha256": known if same else digest(file)})
    return media


def fingerprint(item):
    """What a batch approval covers: the media's checksums, the words, the sound, the format and the profile."""
    words = json.dumps([[m["sha256"] for m in item["media"]], item["caption"], item["hashtags"], item["sound"],
                        item["format"], item["profile"]], ensure_ascii=False)
    return hashlib.sha256(words.encode()).hexdigest()


def caption_text(item):
    """The caption as posted: the words, then the hashtags on their own line."""
    tags = " ".join(item.get("hashtags") or ())
    caption = (item.get("caption") or "").strip()
    return f"{caption}\n\n{tags}".strip() if tags else caption


class QueueStore:
    """queues.json and the journals in ``data_dir``."""

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / STORE
        self.lock_path = self.data_dir / LOCK
        self.journals = self.data_dir / JOURNALS
        self._thread_lock = threading.RLock()

    def read(self):
        try:
            return self._load()
        except QueueError:
            return {"version": 1, "queues": []}

    def _load(self):
        """The stored queues; a missing file is empty, and one it can't trust raises so no edit writes over it."""
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {"version": 1, "queues": []}
        except (OSError, ValueError) as error:
            raise QueueError(f"Mobster can't read {STORE}: {error}. Fix or move the file, then try again.",
                             "store_unreadable", 503) from None
        queues = data.get("queues") if isinstance(data, dict) else None
        if not isinstance(queues, list):
            raise QueueError(f"Mobster can't read {STORE}: it isn't a list of queues. Fix or move the file, then "
                             "try again.", "store_unreadable", 503)
        return {"version": 1, "queues": [q for q in queues if isinstance(q, dict) and ID.fullmatch(
            str(q.get("id") or ""))]}

    def all(self):
        return self.read()["queues"]

    def get(self, queue_id):
        found = next((q for q in self.all() if q["id"] == queue_id), None)
        if found is None:
            raise QueueError("That queue isn't here any more.", "queue_not_found", 404)
        return found

    @contextlib.contextmanager
    def edit(self):
        with self._thread_lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                end = time.monotonic() + LOCK_TIMEOUT
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError as exc:
                        if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN) or time.monotonic() >= end:
                            raise OSError("Another Mobster process is holding the queue") from None
                        time.sleep(.05)
                data = self._load()
                before = json.dumps(data, sort_keys=True)
                yield data
                if json.dumps(data, sort_keys=True) != before:
                    self._write(data)
            finally:
                os.close(fd)

    def _write(self, data):
        fd, temporary = tempfile.mkstemp(prefix=".queues.", suffix=".json", dir=self.data_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
            os.replace(temporary, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise

    def journal(self, queue_id, event, **fields):
        """Append one line to the queue's journal: what happened, when, to which item. Written before the step."""
        self.journals.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"at": now_ms(), "event": event, **fields}, ensure_ascii=False)
        path = self.journals / f"{queue_id}.jsonl"
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, (line + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)

    def history(self, queue_id, limit=500):
        path = self.journals / f"{queue_id}.jsonl"
        try:
            lines = path.read_text().splitlines()[-limit:]
        except OSError:
            return []
        out = []
        for line in lines:
            with contextlib.suppress(ValueError):
                out.append(json.loads(line))
        return out

    def create(self, name=None, approval="each"):
        if approval not in APPROVALS:
            raise QueueError("approval is each or batch.")
        with self.edit() as data:
            if len(data["queues"]) >= MAX_QUEUES:
                raise QueueError(f"Mobster keeps at most {MAX_QUEUES} queues. Delete one first.", "too_many")
            stamp = now_ms()
            queue = {"id": uuid.uuid4().hex[:12], "name": _text(name, "name", NAME_CHARS) or
                     f"Queue {len(data['queues']) + 1}", "approval": approval, "armed": False, "batch": None,
                     "createdAt": stamp, "updatedAt": stamp, "items": [], "watch": None, "staggerSeconds": STAGGER_S}
            data["queues"].append(queue)
        self.journal(queue["id"], "created", approval=approval)
        return queue

    def update_queue(self, queue_id, changes, profiles=None):
        allowed = {"name", "approval", "watch", "staggerSeconds"}
        if set(changes) - allowed:
            raise QueueError("A queue's name, approval, watched folder and stagger can change.")
        with self.edit() as data:
            queue = self._find(data, queue_id)
            if "name" in changes:
                queue["name"] = _text(changes["name"], "name", NAME_CHARS) or queue["name"]
            if "approval" in changes:
                if changes["approval"] not in APPROVALS:
                    raise QueueError("approval is each or batch.")
                if changes["approval"] != queue["approval"]:
                    queue["approval"], queue["batch"] = changes["approval"], None
            if "staggerSeconds" in changes:
                value = changes["staggerSeconds"]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= STAGGER_MAX_S:
                    raise QueueError(f"staggerSeconds is 0 to {STAGGER_MAX_S}.")
                queue["staggerSeconds"] = int(value)
            if "watch" in changes:
                queue["watch"] = self._clean_watch(changes["watch"], queue, profiles)
            queue["updatedAt"] = now_ms()
        return queue

    @staticmethod
    def _clean_watch(value, queue, profiles):
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) - {"folder", "profile"}:
            raise QueueError("watch is {folder, profile}, or null.")
        folder = value.get("folder")
        if not isinstance(folder, str) or not Path(folder).expanduser().is_dir():
            raise QueueError("Choose a folder that exists on this Mac.", "folder_missing")
        profile = value.get("profile")
        if profile is not None and (not isinstance(profile, str) or (profiles is not None and profile not in {
                p["id"] for p in profiles})):
            raise QueueError("No profile has that id. Add it in Settings › Profiles.", "profile_not_found")
        folder = str(Path(folder).expanduser().resolve())
        before = queue.get("watch") or {}
        # What the folder holds now is already there: only files that arrive from now on join the queue.
        seen = before.get("seen") if before.get("folder") == folder else None
        if seen is None:
            seen = sorted(str(p) for p in Path(folder).iterdir() if p.is_file())[-WATCH_SEEN:]
        return {"folder": folder, "profile": profile, "seen": seen}

    def note_seen(self, queue_id, paths):
        with self.edit() as data:
            queue = self._find(data, queue_id)
            watch = queue.get("watch")
            if watch:
                watch["seen"] = (list(watch.get("seen") or ()) + list(paths))[-WATCH_SEEN:]

    def delete_queue(self, queue_id):
        with self.edit() as data:
            queue = self._find(data, queue_id)
            if any(i["state"] in ("sending", "posting") for i in queue["items"]):
                raise QueueError("A post in this queue is going out now. Wait for it to end.", "busy", 409)
            data["queues"] = [q for q in data["queues"] if q["id"] != queue_id]
        self.journal(queue_id, "deleted")

    def arm(self, queue_id, armed, profile=None):
        """Start or stop the queue; with ``profile``, only that profile's posts (a profile's Queue tab). armedProfiles
        is None while the whole queue runs, else the profiles whose posts go out."""
        with self.edit() as data:
            queue = self._find(data, queue_id)
            scoped = queue.get("armedProfiles")
            if profile is None:
                running, scoped = bool(armed), None
            elif armed:
                running = True
                scoped = None if queue.get("armed") and scoped is None else sorted({*(scoped or ()), profile})
            else:
                if queue.get("armed") and scoped is None:
                    scoped = sorted({i["profile"] for i in queue["items"] if i.get("profile")} - {profile})
                else:
                    scoped = [p for p in scoped or () if p != profile]
                running = bool(queue.get("armed")) and bool(scoped)
                scoped = scoped if running else None
            queue["armed"], queue["armedProfiles"] = running, scoped
            queue["updatedAt"] = now_ms()
        self.journal(queue_id, "started" if armed else "stopped", **({"profile": profile} if profile else {}))
        return queue

    def approve_batch(self, queue_id, item_ids):
        """The person's one approval for these items' Posts, as they are now. Items edited later ask again."""
        with self.edit() as data:
            queue = self._find(data, queue_id)
            if queue["approval"] != "batch":
                raise QueueError("This queue asks before each post. Choose Approve the batch first.", "not_batch",
                                 409)
            items = {i["id"]: i for i in queue["items"]}
            if not item_ids or any(i not in items for i in item_ids):
                raise QueueError("Approve posts that are in this queue.")
            covered = {i: fingerprint(items[i]) for i in item_ids if items[i]["state"] in EDITABLE}
            if not covered:
                raise QueueError("None of those posts is waiting to go out.", "nothing_to_approve", 409)
            queue["batch"] = {"at": now_ms(), "items": covered}
        self.journal(queue_id, "batch_approved", items=sorted(covered))
        return queue

    def clean_item(self, body, current=None, profiles=None):
        """A new or edited item from ``body``. ``profiles``: the stored profiles, to check ``profile``."""
        known = {"media", "caption", "hashtags", "sound", "format", "profile", "scheduledAt", "draft"}
        if set(body) - known:
            raise QueueError("Unsupported field: " + ", ".join(sorted(set(body) - known)))
        item = dict(current) if current else {"id": uuid.uuid4().hex[:12], "media": [], "caption": "", "hashtags": [],
                                               "sound": "", "format": "auto", "profile": None, "scheduledAt": None,
                                               "state": "ready", "runId": None, "transfer": None, "result": None,
                                               "note": None}
        if "draft" in body and (type(body["draft"]) is not bool or current is not None):
            raise QueueError("draft is true on a new post that has no photos or videos yet.")
        draft = body.get("draft") is True or (current is not None and current["state"] == "draft")
        if draft and not body.get("media"):
            item["media"] = [] if current is None else item["media"]
        elif "media" in body or current is None:
            item["media"] = clean_media(body.get("media"))
        if draft:
            # Photos turn a draft into a normal ready post; the approvals for sending apply to it from then on.
            item.update(state="ready" if item["media"] else "draft", note=None if item["media"] else ADD_PHOTOS)
        if "caption" in body:
            item["caption"] = _text(body["caption"], "caption", CAPTION_CHARS)
        if "hashtags" in body:
            item["hashtags"] = clean_hashtags(body["hashtags"])
        if "sound" in body:
            item["sound"] = _text(body["sound"], "sound", SOUND_CHARS)
        if "format" in body:
            if body["format"] not in FORMATS:
                raise QueueError("format is auto, post, reel, story or video.")
            item["format"] = body["format"]
        if "profile" in body:
            profile = body["profile"]
            if profile is not None and (not isinstance(profile, str) or not ID.fullmatch(profile)):
                raise QueueError("profile is a profile's id from Settings › Profiles.")
            if profile is not None and profiles is not None and profile not in {p["id"] for p in profiles}:
                raise QueueError("No profile has that id. Add it in Settings › Profiles.", "profile_not_found")
            item["profile"] = profile
        if "scheduledAt" in body:
            when = body["scheduledAt"]
            if when is not None and (isinstance(when, bool) or not isinstance(when, (int, float)) or when <= 0):
                raise QueueError("scheduledAt is a time in milliseconds since 1970, or null for as soon as it starts.")
            item["scheduledAt"] = int(when) if when is not None else None
        if len(caption_text(item)) > CAPTION_CHARS:
            raise QueueError(f"The caption and its hashtags come to more than {CAPTION_CHARS:,} characters.")
        item["updatedAt"] = now_ms()
        return item

    def add_items(self, queue_id, bodies, profiles=None):
        items = [self.clean_item(body, profiles=profiles) for body in bodies]
        with self.edit() as data:
            queue = self._find(data, queue_id)
            if len(queue["items"]) + len(items) > MAX_ITEMS:
                raise QueueError(f"A queue holds at most {MAX_ITEMS} posts.", "too_many")
            queue["items"].extend(items)
            queue["updatedAt"] = now_ms()
        for item in items:
            self.journal(queue_id, "added", item=item["id"], media=[m["sha256"] for m in item["media"]])
        return items

    def add_draft(self, queue_id, caption, profile, profiles=None):
        """A draft in ``queue_id`` for ``profile``: the words, no media yet. Nothing sends it as it is."""
        return self.add_items(queue_id, [{"draft": True, "caption": caption, "profile": profile}], profiles)[0]

    def edit_item(self, queue_id, item_id, body, profiles=None):
        with self.edit() as data:
            queue = self._find(data, queue_id)
            index, current = self._item(queue, item_id)
            if current["state"] not in EDITABLE and current["state"] != "draft":
                raise QueueError("This post can't change now: it is going out, went out, or may have.", "locked",
                                 409)
            item = self.clean_item(body, current, profiles)
            if current["state"] not in ("ready", "draft"):
                item.update(state="ready", note=None)
            queue["items"][index] = item
        self.journal(queue_id, "edited", item=item_id)
        return item

    def remove_item(self, queue_id, item_id):
        with self.edit() as data:
            queue = self._find(data, queue_id)
            _, item = self._item(queue, item_id)
            if item["state"] in ("sending", "posting"):
                raise QueueError("This post is going out now.", "busy", 409)
            queue["items"] = [i for i in queue["items"] if i["id"] != item_id]
        self.journal(queue_id, "removed", item=item_id)

    def move_item(self, queue_id, item_id, index):
        with self.edit() as data:
            queue = self._find(data, queue_id)
            at, item = self._item(queue, item_id)
            queue["items"].pop(at)
            queue["items"].insert(max(0, min(int(index), len(queue["items"]))), item)

    def set_state(self, queue_id, item_id, state, **fields):
        """Journal, then store, an item's new state (and its runId, transfer, result or note)."""
        if state not in STATES:
            raise QueueError("Unknown state")
        self.journal(queue_id, state, item=item_id, **{k: v for k, v in fields.items() if k in ("runId", "note")})
        with self.edit() as data:
            queue = self._find(data, queue_id)
            _, item = self._item(queue, item_id)
            item["state"] = state
            for key in ("runId", "transfer", "result", "note"):
                if key in fields:
                    item[key] = fields[key]
            item["updatedAt"] = now_ms()
            return dict(item)

    def change_item(self, queue_id, item_id, change):
        """``change(item)`` edits the item under the lock and returns the event to journal, or None for no change."""
        with self.edit() as data:
            _, item = self._item(self._find(data, queue_id), item_id)
            event = change(item)
            if event is not None:
                self.journal(queue_id, event, item=item_id)
            return dict(item)

    def mark(self, queue_id, item_id, outcome):
        """The person looked: an ``unknown`` post went out (``posted``) or didn't (``ready``, to run again)."""
        if outcome not in ("posted", "ready"):
            raise QueueError("Mark it posted, or ready to run again.")
        with self.edit() as data:
            queue = self._find(data, queue_id)
            _, item = self._item(queue, item_id)
            if item["state"] not in ("unknown", "missed", "declined", "failed") and not (
                    outcome == "posted" and item["state"] == "posted"):
                raise QueueError("Only a post that ended can be marked.", "locked", 409)
        note = "You said it went out." if outcome == "posted" else None
        return self.set_state(queue_id, item_id, outcome, note=note, **(
            {"result": {**(item.get("result") or {}), "markedAt": now_ms()}} if outcome == "posted" else {}))

    def recover(self):
        """After a stop mid-post: ``posting`` becomes ``unknown`` (it may have posted), ``sending`` ``failed``."""
        changed = []
        for queue in self.all():
            for item in queue["items"]:
                if item["state"] == "posting":
                    changed.append((queue["id"], item["id"], "unknown", "Mobster stopped while this post was going "
                                    "out. Look at the profile, then mark it."))
                elif item["state"] == "sending":
                    changed.append((queue["id"], item["id"], "failed", "Mobster stopped while it sent the media. "
                                    "Nothing was posted."))
        for queue_id, item_id, state, note in changed:
            self.set_state(queue_id, item_id, state, note=note)
        return len(changed)

    @staticmethod
    def _find(data, queue_id):
        queue = next((q for q in data["queues"] if q["id"] == queue_id), None)
        if queue is None:
            raise QueueError("That queue isn't here any more.", "queue_not_found", 404)
        return queue

    @staticmethod
    def _item(queue, item_id):
        for index, item in enumerate(queue["items"]):
            if item["id"] == item_id:
                return index, item
        raise QueueError("That post isn't in this queue any more.", "item_not_found", 404)


_stores = {}
_stores_lock = threading.Lock()


def store_for(runtime):
    """The QueueStore beside ``runtime``'s journal, else in its data folder (one per folder)."""
    from ..paths import user_data_dir
    config = getattr(runtime, "config", None)
    state_db = getattr(config, "state_db", None)
    folder = Path(state_db).parent if state_db else Path(getattr(config, "data_dir", None) or user_data_dir())
    with _stores_lock:
        store = _stores.get(folder)
        if store is None:
            store = _stores[folder] = QueueStore(folder)
        return store
