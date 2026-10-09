"""Attachments on this Mac: the journal's ``attachments`` tables and the files beside the journal.

A file lives at ``<journal folder>/attachments/<id>/`` (folders 0700, files 0600): ``original.<ext>`` as it came,
and what extract.py derived (``thumb.jpg``, ``model.jpg``, ``text.txt``). Its row says what it is
(``data``: name, type, kind, size, pages or rows, sha256, where it came from) and where it belongs: a conversation
(``thread_id``) and the tasks that used it (``attachment_runs``, which the journal empties itself when a task leaves
history).

Rules (docs/files.md): a file is at most 25 MB; a conversation holds at most 100 MB of files and Mobster at most 1 GB
(both count the derived files); a file nothing used within 24 hours is deleted; deleting a conversation deletes its
files; a file used only by tasks goes when the last of those tasks leaves history.
"""

from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import threading
import time
import uuid

from . import extract, sniff

log = logging.getLogger("mobster.attachments")

SCHEMA, VERSION = "attachments", 1
FILE_MAX = 26_214_400                 # 25 MB, the upload route's cap
THREAD_MAX = 100 * 1024 * 1024
TOTAL_MAX = 1024 * 1024 * 1024
PER_RUN = 10
UNUSED_TTL = 24 * 3600
ORPHAN_TTL = 3600
ID = re.compile(r"[a-f0-9]{12}")
ORIGINS = ("upload", "phone", "screenshot")
DERIVED = {"thumb": "thumb.jpg", "model": "model.jpg", "text": "text.txt"}


def migrate(connection, from_version):
    """The attachments schema (journal.register_schema): v1 creates both tables."""
    if from_version < 1:
        connection.execute("CREATE TABLE IF NOT EXISTS attachments(id TEXT PRIMARY KEY, thread_id TEXT, run_id TEXT, "
                           "data TEXT NOT NULL, bytes INTEGER NOT NULL, created_at REAL NOT NULL)")
        connection.execute("CREATE INDEX IF NOT EXISTS attachments_thread ON attachments(thread_id)")
        connection.execute("CREATE TABLE IF NOT EXISTS attachment_runs("
                           "attachment_id TEXT NOT NULL REFERENCES attachments(id) ON DELETE CASCADE, "
                           "run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE, "
                           "PRIMARY KEY(attachment_id, run_id))")
        connection.execute("CREATE INDEX IF NOT EXISTS attachment_runs_run ON attachment_runs(run_id)")


class StoreError(ValueError):
    """A refusal with a sentence, an HTTP status and a code (too_large, thread_full, storage_full, ...)."""

    def __init__(self, message, status=400, code="invalid_attachment"):
        super().__init__(message)
        self.status, self.code = status, code


@dataclass
class Attachment:
    id: str
    thread_id: object
    run_id: object
    data: dict
    bytes: int
    created_at: float
    run_ids: tuple = ()

    @property
    def name(self):
        return self.data.get("name") or "file"

    @property
    def kind(self):
        return self.data.get("kind")

    def ref(self):
        """The AttachmentRef every surface shares: {id, name, mime, bytes, kind}."""
        return {"id": self.id, "name": self.name, "mime": self.data.get("mime"), "bytes": self.data.get("bytes"),
                "kind": self.kind}

    def public(self):
        """The API's attachment: the ref, plus pages or rows, where its thumbnail and content are, and where it
        belongs."""
        out = self.ref()
        for key in ("pages", "rows", "columns", "width", "height"):
            if self.data.get(key) is not None:
                out[key] = self.data[key]
        out.update(label=self.data.get("label"), origin=self.data.get("origin", "upload"),
                   textAvailable=bool(self.data.get("textAvailable")),
                   thumbUrl=f"/api/attachments/{self.id}/thumb" if self.data.get("thumb") else None,
                   contentUrl=f"/api/attachments/{self.id}/content", createdAt=round(self.created_at * 1000),
                   threadId=self.thread_id, runIds=list(self.run_ids))
        return out


def new_id():
    return uuid.uuid4().hex[:12]


class AttachmentStore:
    """One journal's attachments. ``journal`` is the runtime's (its transaction() only); ``root`` the folder of files;
    ``publish(event)`` posts on the bus topic ``attachments``."""

    def __init__(self, journal, root, publish=None, clock=time.time, referenced=None):
        self.journal, self.root = journal, Path(root)
        self.publish = publish or (lambda event: None)
        self.clock = clock
        # {attachment id: [run ids]} of the tasks in memory that name it (service.referenced); kept by clean-up.
        self.referenced = referenced or (lambda: {})
        self._lock = threading.Lock()
        self.on_first_write = None   # service.start: the clean-up thread starts with the first file kept

    def count(self):
        with self.journal.transaction() as connection:
            return connection.execute("SELECT count(*) FROM attachments").fetchone()[0]

    # -- reading ------------------------------------------------------------------------------------------------

    def _row(self, connection, row):
        if row is None:
            return None
        identifier, thread_id, run_id, data, size, created = row
        runs = tuple(r[0] for r in connection.execute(
            "SELECT run_id FROM attachment_runs WHERE attachment_id=? ORDER BY rowid", (identifier,)))
        try:
            parsed = json.loads(data)
        except ValueError:
            parsed = {}
        return Attachment(identifier, thread_id, run_id, parsed if isinstance(parsed, dict) else {}, size, created,
                          runs)

    def get(self, attachment_id, runs=True):
        """The attachment, with ``run_ids``: the tasks that read it, then (``runs``) any other task in memory that
        names it. Never call it with ``runs`` while holding the runtime's lock."""
        if not isinstance(attachment_id, str) or not ID.fullmatch(attachment_id):
            return None
        with self.journal.transaction() as connection:
            row = connection.execute("SELECT id, thread_id, run_id, data, bytes, created_at FROM attachments "
                                     "WHERE id=?", (attachment_id,)).fetchone()
            found = self._row(connection, row)
        if found is not None and runs:
            named = [r for r in self._referenced().get(found.id, ()) if r not in found.run_ids]
            found.run_ids = found.run_ids + tuple(named)
        return found

    def _referenced(self):
        try:
            return dict(self.referenced() or {})
        except Exception:  # noqa: BLE001
            log.exception("could not read which tasks name attachments")
            return {}

    def many(self, ids):
        """The attachments ``ids`` names that exist, in that order."""
        out = []
        for identifier in ids or ():
            found = self.get(identifier, runs=False)
            if found is not None:
                out.append(found)
        return out

    def for_thread(self, thread_id):
        with self.journal.transaction() as connection:
            rows = connection.execute("SELECT id, thread_id, run_id, data, bytes, created_at FROM attachments "
                                      "WHERE thread_id=? ORDER BY created_at", (thread_id,)).fetchall()
            return [self._row(connection, row) for row in rows]

    def folder(self, attachment_id):
        if not ID.fullmatch(str(attachment_id)):
            raise ValueError("Not an attachment id")
        return self.root / attachment_id

    def path(self, attachment, which):
        """The file ``which`` ("original", "thumb", "model", "text") of ``attachment``, or None when it has none."""
        folder = self.folder(attachment.id)
        if which == "original":
            path = folder / f"original.{attachment.data.get('ext') or 'bin'}"
        else:
            path = folder / DERIVED[which]
        if path.is_symlink() or not path.is_file():
            return None
        return path

    def text(self, attachment):
        """What Mobster read from the file (``text.txt``): for a CSV only its header and first 200 rows. The agent
        reads this; anything that hands the file on uses ``whole_text``."""
        path = self.path(attachment, "text")
        if path is None:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")

    def whole_text(self, attachment):
        """A text or CSV file's own text, all of it, for the clipboard: never the excerpt the agent reads, so what
        lands on the phone is the file the person approved. UTF-8 without its byte-order mark, line ends as "\\n".
        None when the file is gone or isn't text."""
        if attachment.kind not in ("text", "csv"):
            return None
        path = self.path(attachment, "original")
        if path is None:
            return None
        text = sniff.decode_text(path.read_bytes())
        if text is None:
            return None
        return text.replace("\r\n", "\n").replace("\r", "\n")

    def usage(self, thread_id=None):
        """(bytes kept in all, bytes kept for ``thread_id``)."""
        with self.journal.transaction() as connection:
            return self._usage(connection, thread_id)

    @staticmethod
    def _usage(connection, thread_id):
        total = connection.execute("SELECT COALESCE(SUM(bytes), 0) FROM attachments").fetchone()[0]
        thread = 0
        if thread_id:
            thread = connection.execute("SELECT COALESCE(SUM(bytes), 0) FROM attachments WHERE thread_id=?",
                                        (thread_id,)).fetchone()[0]
        return int(total), int(thread)

    # -- writing ------------------------------------------------------------------------------------------------

    def _make_root(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink():
            raise StoreError("Mobster's attachments folder is a link; it won't write there.", 500, "io_error")
        os.chmod(self.root, 0o700)

    def create(self, data, name, *, thread_id=None, origin="upload"):
        """Keep ``data`` (a whole file) as an attachment named ``name``. Returns the Attachment. StoreError (with
        its status) for an empty, oversized or unsupported file or a full conversation or Mac."""
        data = bytes(data or b"")
        if not data:
            raise StoreError("This file is empty.", 400, "empty_file")
        if len(data) > FILE_MAX:
            raise StoreError("Attach files of 25 MB or less.", 413, "too_large")
        if thread_id is not None and not ID.fullmatch(str(thread_id)):
            raise StoreError("Not a conversation id.", 400, "invalid_thread")
        if origin not in ORIGINS:
            raise ValueError("Unknown attachment origin")
        try:
            file_type = sniff.sniff(data, name)
        except sniff.Unsupported as error:
            raise StoreError(str(error), 415, error.code) from None
        display = sniff.with_extension(sniff.clean_name(name, fallback=f"file.{file_type.ext}"), file_type)
        total, thread = self.usage(thread_id)
        self._check_room(len(data), total, thread)
        self._make_root()
        identifier = new_id()
        folder = self.folder(identifier)
        folder.mkdir(mode=0o700)
        try:
            original = folder / f"original.{file_type.ext}"
            fd = os.open(original, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            try:
                derived = extract.derive(folder, original, file_type)
            except extract.TooLarge as error:
                raise StoreError(str(error), 413, error.code) from None
            except extract.Locked as error:
                raise StoreError(str(error), 415, error.code) from None
            except sniff.Unsupported as error:
                raise StoreError(str(error), 415, error.code) from None
            for path in folder.iterdir():  # the helper's files too: private to this user
                if path.is_file() and not path.is_symlink():
                    os.chmod(path, 0o600)
            size = sum(path.stat().st_size for path in folder.iterdir() if path.is_file())
            record = {"id": identifier, "name": display, "mime": file_type.mime, "kind": file_type.kind,
                      "ext": file_type.ext, "label": file_type.label, "bytes": len(data),
                      "sha256": hashlib.sha256(data).hexdigest(), "origin": origin, **derived}
            created = self.clock()
            # A refusal is raised after the transaction: journal.transaction turns anything raised inside it into
            # a JournalError.
            room = None
            with self._lock, self.journal.transaction() as connection:
                total, thread = self._usage(connection, thread_id)
                room = self._room(size, total, thread)
                if room is None:
                    connection.execute("INSERT INTO attachments(id, thread_id, run_id, data, bytes, created_at) "
                                       "VALUES (?, ?, NULL, ?, ?, ?)",
                                       (identifier, thread_id, json.dumps(record, separators=(",", ":")), size,
                                        created))
            if room is not None:
                raise room
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        self.publish({"event": "attachment_created", "id": identifier, "threadId": thread_id})
        if self.on_first_write is not None:
            hook, self.on_first_write = self.on_first_write, None
            try:
                hook()
            except Exception:  # noqa: BLE001
                log.exception("the clean-up couldn't start")
        return Attachment(identifier, thread_id, None, record, size, created, ())

    @classmethod
    def _check_room(cls, size, total, thread):
        refusal = cls._room(size, total, thread)
        if refusal is not None:
            raise refusal

    @staticmethod
    def _room(size, total, thread):
        """None when ``size`` more bytes fit, else the StoreError that says why not."""
        if total + size > TOTAL_MAX:
            return StoreError("Mobster keeps at most 1 GB of attached files. Delete conversations you're done with, "
                              "then attach this again.", 413, "storage_full")
        if thread + size > THREAD_MAX:
            return StoreError("This conversation already holds 100 MB of files. Start a new one to attach more.",
                              413, "thread_full")
        return None

    def attach(self, ids, run_id, thread_id=None):
        """Record that the task ``run_id`` used ``ids``; files not yet in a conversation join ``thread_id``. Returns
        the ids recorded."""
        recorded = []
        with self.journal.transaction() as connection:
            exists = connection.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone() is not None
            for identifier in ids or ():
                if not ID.fullmatch(str(identifier)):
                    continue
                row = connection.execute("SELECT thread_id, run_id FROM attachments WHERE id=?",
                                         (identifier,)).fetchone()
                if row is None:
                    continue
                if exists:
                    connection.execute("INSERT OR IGNORE INTO attachment_runs(attachment_id, run_id) VALUES (?, ?)",
                                       (identifier, run_id))
                connection.execute("UPDATE attachments SET run_id=COALESCE(run_id, ?), thread_id=COALESCE(thread_id, ?) "
                                   "WHERE id=?", (run_id if exists else None,
                                                  thread_id if thread_id and ID.fullmatch(str(thread_id)) else None,
                                                  identifier))
                recorded.append(identifier)
        if recorded:
            self.publish({"event": "attachments_used", "ids": recorded, "runId": run_id, "threadId": thread_id})
        return recorded

    def delete(self, attachment_id):
        """Delete one attachment and its files. True when it existed."""
        return bool(self._delete([attachment_id]))

    def _delete(self, ids):
        ids = [i for i in ids if isinstance(i, str) and ID.fullmatch(i)]
        if not ids:
            return []
        removed = []
        with self.journal.transaction() as connection:
            for identifier in ids:
                if connection.execute("DELETE FROM attachments WHERE id=?", (identifier,)).rowcount:
                    removed.append(identifier)
        for identifier in removed:
            shutil.rmtree(self.folder(identifier), ignore_errors=True)
            self.publish({"event": "attachment_deleted", "id": identifier})
        return removed

    def delete_thread(self, thread_id):
        """Delete every attachment of a conversation (it was deleted)."""
        if not isinstance(thread_id, str) or not ID.fullmatch(thread_id):
            return []
        with self.journal.transaction() as connection:
            ids = [r[0] for r in connection.execute("SELECT id FROM attachments WHERE thread_id=?", (thread_id,))]
        return self._delete(ids)

    # -- clean-up -----------------------------------------------------------------------------------------------

    def expired(self, run_ids):
        """Tasks left history: a file that no task uses any more and that belongs to no conversation goes with the
        last of them. service.py calls it from its clean-up thread (the journal's expiry listener runs under the
        runtime's lock, and this reads the runtime's tasks)."""
        if not run_ids:
            return []
        gone = set(run_ids)
        with self.journal.transaction() as connection:
            ids = [r[0] for r in connection.execute(
                "SELECT id FROM attachments WHERE thread_id IS NULL AND run_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM attachment_runs WHERE attachment_id=attachments.id)")]
        named = self._referenced()
        return self._delete([i for i in ids if not set(named.get(i, ())) - gone])

    def gc(self, referenced=None, now=None):
        """Delete files nothing used within a day (``referenced``: ids a task names, kept; default: the tasks in
        memory), files whose conversation is gone (when Mobster has conversations), and folders with no row (an
        upload cut short)."""
        now = self.clock() if now is None else now
        referenced = set(self._referenced() if referenced is None else referenced)
        with self.journal.transaction() as connection:
            stale = [r[0] for r in connection.execute(
                "SELECT id FROM attachments WHERE thread_id IS NULL AND run_id IS NULL AND created_at < ?",
                (now - UNUSED_TTL,))]
            stale += [r[0] for r in connection.execute(
                "SELECT id FROM attachments WHERE thread_id IS NULL AND run_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM attachment_runs WHERE attachment_id=attachments.id)")]
            gone = []
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='threads'").fetchone():
                gone = [r[0] for r in connection.execute(
                    "SELECT id FROM attachments WHERE thread_id IS NOT NULL "
                    "AND thread_id NOT IN (SELECT id FROM threads)")]
            known = {r[0] for r in connection.execute("SELECT id FROM attachments")}
        removed = self._delete([i for i in stale + gone if i not in referenced])
        if self.root.is_dir():
            for folder in self.root.iterdir():
                if folder.name in known or folder.is_symlink() or not folder.is_dir() or not ID.fullmatch(folder.name):
                    continue
                try:
                    if now - folder.stat().st_mtime > ORPHAN_TTL:
                        shutil.rmtree(folder, ignore_errors=True)
                except OSError:
                    pass
        return removed
