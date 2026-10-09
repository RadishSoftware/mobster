"""The attachments service per Runtime: its store, the run listener that records which task used which file, and
the clean-up thread.

- ``store_for(runtime)``: the runtime's AttachmentStore (``<journal folder>/attachments``), made once, with the
  journal's expiry listener (a file only tasks used goes with the last of them). None without a data folder.
- ``referenced(runtime)``: the attachment ids the tasks in memory name (every task in history, as loaded): the
  clean-up keeps them, and an attachment's ``runIds`` include them (the composer drops a chip a task already took).
- The clean-up thread: once a minute after start, then every 30 minutes, deletes files nothing used within a day,
  files whose conversation is gone and folders an upload left behind; between runs it listens on the bus for
  ``thread_deleted`` and deletes that conversation's files at once.
"""

import logging
from pathlib import Path
import threading
import weakref

log = logging.getLogger("mobster.attachments")

GC_FIRST, GC_EVERY, BUS_WAIT = 60.0, 1800.0, 5.0
_stores = weakref.WeakKeyDictionary()
_lock = threading.Lock()


def store_for(runtime):
    """The runtime's AttachmentStore, or None when it keeps no journal on disk."""
    from .store import AttachmentStore
    with _lock:
        found = _stores.get(runtime)
        if found is not None:
            return found
        state_db = getattr(getattr(runtime, "config", None), "state_db", None)
        journal = getattr(runtime, "journal", None)
        if not state_db or journal is None:
            return None

        def publish(event):
            post = getattr(runtime, "publish", None)
            if callable(post):
                post("attachments", event)

        store = AttachmentStore(journal, Path(state_db).parent / "attachments", publish=publish,
                                referenced=lambda: referenced(runtime))
        try:
            # Called under the runtime's lock (delete_run, create's retention): only note the ids here; the
            # clean-up thread deletes what they leave unused.
            journal.add_expiry_listener(lambda run_ids: expire_later(runtime, run_ids))
        except Exception:  # noqa: BLE001
            log.exception("could not listen for expired tasks")
        _stores[runtime] = store
        return store


def device_record(runtime, device=None):
    """A devices.py-shaped record of the device ``device`` names (an id, UDID or name; None: the default device),
    for phone_io.files. APIError 404 when no device has that name."""
    phone = runtime.resolve_device(device)
    return {"id": phone.id, "kind": phone.kind, "udid": phone.udid, "name": phone.name or phone.id,
            "transport": getattr(phone, "transport", None), "wdaUrl": phone.wda_url}


def phone_files(record):
    """phone_io.files.PhoneFiles for ``record`` (tests replace this)."""
    from ..phone_io.files import PhoneFiles
    return PhoneFiles(record)


def referenced(runtime):
    """{attachment id: [run ids]} the tasks in memory name in their ``attachmentIds``."""
    out = {}
    lock = getattr(runtime, "lock", None)
    if lock is None:
        return out
    with lock:
        runs = list(getattr(runtime, "runs", {}).values())
    for run in runs:
        for identifier in (getattr(run, "extras", None) or {}).get("attachmentIds") or ():
            out.setdefault(identifier, []).append(run.id)
    return out


def expire_later(runtime, run_ids):
    """The journal's expiry listener: hand the removed tasks to the clean-up thread (there is one whenever this
    runtime keeps any file). Never blocks and never takes the runtime's lock."""
    cleaner = _cleaners.get(runtime)
    if cleaner is not None and run_ids:
        with cleaner.lock:
            cleaner.expired.update(run_ids)


class Cleaner:
    def __init__(self, runtime):
        self.runtime = runtime
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.expired = set()
        self.thread = threading.Thread(target=self._loop, name="mobster-attachments", daemon=True)

    def take_expired(self):
        with self.lock:
            ids, self.expired = self.expired, set()
        return ids

    def _loop(self):
        import time
        from ..eventbus import bus as default_bus
        bus = getattr(self.runtime, "bus", None) or default_bus
        cursor = bus.position()
        due = time.monotonic() + GC_FIRST
        while not self.stop.is_set():
            try:
                events = bus.wait({"threads"}, cursor, BUS_WAIT)
            except Exception:  # noqa: BLE001
                events = []
                self.stop.wait(BUS_WAIT)
            if events and isinstance(events[-1].get("id"), str):
                cursor = events[-1]["id"]
            store = store_for(self.runtime) if not self.stop.is_set() else None
            if store is None:
                continue
            expired = self.take_expired()
            if expired:
                try:
                    store.expired(sorted(expired))
                except Exception:  # noqa: BLE001
                    log.exception("could not clean up the files of tasks that left history")
            for event in events:
                thread = event.get("thread") if isinstance(event.get("thread"), dict) else {}
                if event.get("event") == "thread_deleted" and thread.get("id"):
                    try:
                        store.delete_thread(thread["id"])
                    except Exception:  # noqa: BLE001
                        log.exception("could not delete a conversation's files")
            if time.monotonic() >= due:
                due = time.monotonic() + GC_EVERY
                try:
                    store.gc()
                except Exception:  # noqa: BLE001
                    log.exception("attachment clean-up failed")


_cleaners = weakref.WeakKeyDictionary()


def start(runtime):
    """register_service start: the store, and its clean-up thread once there is anything to clean (now, or at the
    first upload). Never blocks: one small query."""
    store = store_for(runtime)
    if store is None:
        return
    store.on_first_write = lambda: ensure_cleaner(runtime)
    try:
        kept = store.count()
    except Exception:  # noqa: BLE001 -- a journal that can't answer starts no thread; the first upload will
        kept = 0
    if kept:
        ensure_cleaner(runtime)


def ensure_cleaner(runtime):
    with _lock:
        if runtime in _cleaners or getattr(runtime, "closing", False):
            return
        cleaner = Cleaner(runtime)
        _cleaners[runtime] = cleaner
    cleaner.thread.start()


def close(runtime):
    store = _stores.get(runtime)
    if store is not None:
        store.on_first_write = None
    with _lock:
        cleaner = _cleaners.pop(runtime, None)
    if cleaner is not None:
        cleaner.stop.set()
