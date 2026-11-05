"""Journal schema ``longform`` v1 (DESIGN §5.4): a long run's notebook and report, kept with the run."""

import json
import logging
import threading
import time

log = logging.getLogger("mobster.longform")

SCHEMA, VERSION = "longform", 1


def migrate(connection, from_version):
    if from_version < 1:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS longform_notebooks(run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE "
            "CASCADE, version INTEGER NOT NULL, data TEXT NOT NULL, bytes INTEGER NOT NULL, updated_at REAL NOT NULL)")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS longform_reports(run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE "
            "CASCADE, report TEXT NOT NULL, markdown TEXT NOT NULL, created_at REAL NOT NULL)")


def _open(journal):
    return journal is not None and getattr(journal, "connection", None) is not None


def save_notebook(journal, run_id, data):
    """``data``: the notebook's redacted JSON text (Notebook.dumps)."""
    if not _open(journal):
        return False
    with journal.transaction() as connection:
        connection.execute(
            "INSERT INTO longform_notebooks(run_id, version, data, bytes, updated_at) VALUES (?, 1, ?, ?, ?) "
            "ON CONFLICT(run_id) DO UPDATE SET version=excluded.version, data=excluded.data, bytes=excluded.bytes, "
            "updated_at=excluded.updated_at", (run_id, data, len(data.encode()), time.time()))
    return True


def save_report(journal, run_id, report, markdown):
    if not _open(journal):
        return False
    from ..secret_filter import redact
    from .notebook import _redacted
    text = json.dumps(_redacted(report, redact), ensure_ascii=False, allow_nan=False)
    with journal.transaction() as connection:
        connection.execute(
            "INSERT INTO longform_reports(run_id, report, markdown, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(run_id) DO UPDATE SET report=excluded.report, markdown=excluded.markdown, "
            "created_at=excluded.created_at", (run_id, text, redact(markdown or ""), time.time()))
    return True


def load_notebook(journal, run_id):
    """The run's notebook as a dict, or None."""
    if not _open(journal):
        return None
    try:
        with journal.transaction() as connection:
            row = connection.execute("SELECT data FROM longform_notebooks WHERE run_id=?", (run_id,)).fetchone()
    except Exception:  # noqa: BLE001 -- the track is off (no table) or the journal is closing
        return None
    if not row:
        return None
    try:
        value = json.loads(row[0])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def load_report(journal, run_id):
    """(report dict, markdown) or (None, None)."""
    if not _open(journal):
        return None, None
    try:
        with journal.transaction() as connection:
            row = connection.execute("SELECT report, markdown FROM longform_reports WHERE run_id=?",
                                     (run_id,)).fetchone()
    except Exception:  # noqa: BLE001
        return None, None
    if not row:
        return None, None
    try:
        return json.loads(row[0]), row[1]
    except ValueError:
        return None, None


class Writer:
    """One daemon thread writing the newest notebook (and the report) per run."""

    def __init__(self):
        self._condition = threading.Condition()
        self._pending = {}
        self._busy = False
        self._thread = None

    def submit(self, journal, run_id, notebook, *, report=None, markdown=None):
        with self._condition:
            previous = self._pending.get(run_id)
            if report is None and previous is not None and previous[2] is not None:
                report, markdown = previous[2], previous[3]
            self._pending[run_id] = (journal, notebook, report, markdown)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="mobster-longform-store", daemon=True)
                self._thread.start()
            self._condition.notify_all()

    def _loop(self):
        while True:
            with self._condition:
                while not self._pending:
                    if not self._condition.wait(30):
                        self._thread = None
                        return
                run_id, (journal, notebook, report, markdown) = next(iter(self._pending.items()))
                del self._pending[run_id]
                self._busy = True
            try:
                if notebook is not None:
                    save_notebook(journal, run_id, notebook.dumps())
                if report is not None:
                    save_report(journal, run_id, report, markdown)
            except Exception:  # noqa: BLE001 -- never reaches a run
                log.info("longform store write failed", exc_info=True)
            finally:
                with self._condition:
                    self._busy = False
                    self._condition.notify_all()

    def flush(self, timeout=2.0):
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._pending or self._busy:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._condition.wait(min(left, .05))
        return True


writer = Writer()


def close(runtime=None):
    writer.flush(1.5)
