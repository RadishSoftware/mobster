"""Projects, approved test revisions and their durable requests in the private run journal.

One row per project: its id, its folder (absolute and resolved, one row per folder), its name, how it came in
(``source``, JSON: {"kind": "folder"} or {"kind": "clone", "url"}) and when it was added and last opened. Nothing
read from the folder is kept here; folder.py reads it each time.

Each project keeps its newest RUN_KEEP check runs. An older finished run is pruned and leaves only its request key
(hashed) with a summary, so a retry with that key replays the summary, never a second run, until KEY_DAYS after the
run started. After that the key is new.

Writes go through ``Journal.transaction()``, reads hold ``Journal.lock``. Times are seconds here; the API says
milliseconds.
"""

import hashlib
import json
import math
import sqlite3
import time

from ..journal import JournalError

SCHEMA = "projects"
VERSION = 2
MAX_PROJECTS = 100
RUN_KEEP = 2_000        # check runs a project keeps; an older finished one leaves only its request key
KEY_DAYS = 90           # how long a pruned run's request key refuses a retry, from when the run started
PRUNE_BATCH = 200       # rows one prune removes, so admission never waits on a long one
MAX_REVISIONS = 100     # approved revisions a project keeps; the oldest older one no kept run names makes room
RESULT_KEEP = 100
RETIRED = "id, owner, request_id, mode, project_id, grant_id, revision, state, data"   # what _retire reads of a run
SUMMARY = ("id", "projectId", "mode", "state", "exitCode", "code", "startedAt", "endedAt", "done", "total", "summary",
           "stopRequested", "choice")   # what a pruned run's key replays
UNFINISHED = "('building', 'running', 'stopping')"   # a row in one of these states may still change
COLUMNS = ("id", "path", "name", "source", "added_at", "opened_at")


def migrate(connection, from_version):
    """Create projects, then add durable check runs and immutable approved revisions."""
    if from_version < 1:
        connection.execute("""CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            source TEXT NOT NULL,
            added_at REAL NOT NULL,
            opened_at REAL NOT NULL)""")
    if from_version < 2:
        connection.execute("""CREATE TABLE project_check_runs (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            owner TEXT NOT NULL,
            grant_id TEXT,
            request_id TEXT NOT NULL,
            mode TEXT NOT NULL,
            revision INTEGER,
            state TEXT NOT NULL,
            data TEXT NOT NULL,
            result_json BLOB,
            result_junit BLOB,
            result_error TEXT,
            UNIQUE(owner, request_id))""")
        connection.execute("""CREATE TABLE project_test_revisions (
            project_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            fingerprint TEXT NOT NULL,
            manifest TEXT NOT NULL,
            PRIMARY KEY(project_id, revision),
            UNIQUE(project_id, fingerprint))""")
        _v2_objects(connection)


def _v2_objects(connection):
    """The removed runs' request keys and the indexes every admission's queries use. Idempotent: it also repairs a
    journal that an earlier build of this unreleased v2 wrote before they existed (ProjectStore.repair)."""
    connection.execute("""CREATE TABLE IF NOT EXISTS project_request_keys (
        owner TEXT NOT NULL,
        key TEXT NOT NULL,
        project_id TEXT NOT NULL,
        grant_id TEXT,
        mode TEXT NOT NULL,
        revision INTEGER,
        run TEXT NOT NULL,
        at REAL NOT NULL,
        run_id TEXT,
        PRIMARY KEY(owner, key))""")
    if "run_id" not in {row[1] for row in connection.execute("PRAGMA table_info(project_request_keys)")}:
        connection.execute("ALTER TABLE project_request_keys ADD COLUMN run_id TEXT")
    connection.execute("CREATE INDEX IF NOT EXISTS project_check_runs_project ON project_check_runs(project_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS project_request_keys_run ON project_request_keys(owner, run_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS project_request_keys_at ON project_request_keys(at)")


def _retire(connection, rows, now):
    """Delete finished runs, each leaving its request key and summary while KEY_DAYS haven't passed since it started.
    An interrupted run's outcome is unknown, so its key never expires. ``rows``: (id, owner, request_id, mode,
    project_id, grant_id, revision, state, data)."""
    cutoff = now - KEY_DAYS * 86_400
    for run_id, owner, request_id, mode, project_id, grant_id, revision, state, raw in rows:
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):
            data = None
        data = data if isinstance(data, dict) else {"id": run_id, "projectId": project_id, "mode": mode, "state": state}
        started = data.get("startedAt")
        at = started / 1000 if type(started) in (int, float) else now   # unknown: kept the full KEY_DAYS
        if state == "interrupted":
            at = math.inf
        if at >= cutoff:
            connection.execute("INSERT OR REPLACE INTO project_request_keys VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (owner, _key(request_id), project_id, grant_id, mode, revision,
                                json.dumps({key: data.get(key) for key in SUMMARY}, separators=(",", ":")), at,
                                run_id))
        connection.execute("DELETE FROM project_check_runs WHERE id=?", (run_id,))


def _key(request_id):
    return hashlib.sha256(request_id.encode()).hexdigest()


def _kept(connection, owner, now, *, request_id=None, run_id=None):
    """A removed run's key younger than KEY_DAYS, by its request or its run id under ``owner``: {mode, project_id,
    grant_id, revision, run}, else None. ``owner`` None (a local run by id): any local owner."""
    where = "key=?" if request_id is not None else "run_id=?"
    owners = "owner=?" if owner is not None else "owner LIKE 'local:%'"
    found = _key(request_id) if request_id is not None else run_id
    row = connection.execute(f"SELECT mode, project_id, grant_id, revision, run FROM project_request_keys "
                             f"WHERE {owners} AND {where} AND at>=?",
                             (*(() if owner is None else (owner,)), found, now - KEY_DAYS * 86_400)).fetchone()
    if row is None:
        return None
    mode, project_id, grant_id, revision, run = row
    return {"mode": mode, "project_id": project_id, "grant_id": grant_id, "revision": revision,
            "run": {**json.loads(run), "requestId": request_id, "pruned": True}}


def _project(row):
    value = dict(zip(COLUMNS, row))
    try:
        source = json.loads(value["source"])
    except ValueError:
        source = None
    value["source"] = source if isinstance(source, dict) else {"kind": "folder"}
    return value


class ProjectStore:
    def __init__(self, journal):
        self.journal = journal

    def _read(self, sql, args=()):
        with self.journal.lock:
            if self.journal.connection is None:
                raise JournalError("The run journal is closed")
            try:
                return self.journal.connection.execute(sql, args).fetchall()
            except sqlite3.Error:
                raise JournalError("Mobster could not read its projects") from None

    def projects(self):
        """Every project, the one opened last first."""
        return [_project(row) for row in self._read(
            f"SELECT {', '.join(COLUMNS)} FROM projects ORDER BY opened_at DESC, rowid DESC")]

    def project(self, project_id):
        rows = self._read(f"SELECT {', '.join(COLUMNS)} FROM projects WHERE id=?", (project_id,))
        return _project(rows[0]) if rows else None

    def by_path(self, path):
        rows = self._read(f"SELECT {', '.join(COLUMNS)} FROM projects WHERE path=?", (str(path),))
        return _project(rows[0]) if rows else None

    def add(self, row):
        """Insert ``row``; the project already at that path when there is one (nothing is written then), else the
        new row. ValueError when MAX_PROJECTS are saved. (A raise inside a transaction reads as a journal failure,
        so the count is decided inside and refused after.)"""
        full = False
        with self.journal.transaction() as connection:
            found = connection.execute("SELECT id FROM projects WHERE path=?", (row["path"],)).fetchone()
            if found is None:
                full = connection.execute("SELECT count(*) FROM projects").fetchone()[0] >= MAX_PROJECTS
                if not full:
                    connection.execute(
                        f"INSERT INTO projects({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
                        [row["id"], row["path"], row["name"], json.dumps(row["source"]), row["added_at"],
                         row["opened_at"]])
        if full:
            raise ValueError(f"Mobster keeps at most {MAX_PROJECTS} projects. Remove one you don't use, then add "
                             "this one.")
        return (self.project(found[0]), False) if found else (self.project(row["id"]), True)

    def opened(self, project_id, at):
        with self.journal.transaction() as connection:
            connection.execute("UPDATE projects SET opened_at=? WHERE id=?", (at, project_id))
        return self.project(project_id)

    def delete(self, project_id, now=None):
        """Forget a project. Its finished runs go now, each leaving its key (_retire); an unfinished one stays."""
        with self.journal.transaction() as connection:
            _retire(connection, connection.execute(
                f"SELECT {RETIRED} FROM project_check_runs WHERE project_id=? AND state NOT IN {UNFINISHED}",
                (project_id,)).fetchall(), time.time() if now is None else now)
            return connection.execute("DELETE FROM projects WHERE id=?", (project_id,)).rowcount > 0

    def repair(self):
        """Give a journal an earlier build of this unreleased v2 wrote the v2 objects it lacks (_v2_objects)."""
        with self.journal.transaction() as connection:
            _v2_objects(connection)

    def check_run(self, run_id, now=None):
        """A local run by its id, or a removed one's summary (marked pruned); None for an id Mobster doesn't hold."""
        rows = self._read("SELECT data FROM project_check_runs WHERE id=? AND owner='local:' || project_id",
                          (run_id,))
        if rows:
            return json.loads(rows[0][0])
        kept = self._kept(None, now, run_id=run_id)
        return kept["run"] if kept else None

    def latest_run(self, project_id):
        rows = self._read("SELECT data FROM project_check_runs WHERE project_id=? AND owner='local:' || project_id "
                          "ORDER BY rowid DESC LIMIT 1",
                          (project_id,))
        return json.loads(rows[0][0]) if rows else None

    def requested_run(self, project_id, request_id, now=None):
        """(mode, run) for a request, or for a pruned run's key its summary; None for a key Mobster doesn't hold."""
        rows = self._read("SELECT mode, data FROM project_check_runs WHERE owner='local:' || ? AND request_id=?",
                          (project_id, request_id))
        if rows:
            return rows[0][0], json.loads(rows[0][1])
        kept = self._kept("local:" + project_id, now, request_id=request_id)
        return (kept["mode"], kept["run"]) if kept else None

    def _kept(self, owner, now, **lookup):
        with self.journal.lock:
            if self.journal.connection is None:
                raise JournalError("The run journal is closed")
            try:
                return _kept(self.journal.connection, owner, time.time() if now is None else now, **lookup)
            except sqlite3.Error:
                raise JournalError("Mobster could not read its projects. If an earlier build of this branch wrote "
                                   "this journal, restart Mobster so it can repair it.") from None

    def claim_run(self, run, *, owner=None, grant_id=None, revision=None, authorize=None):
        """Persist admission before a worker starts. A request key is never reused for another mode."""
        owner = owner or "local:" + run["projectId"]
        denied = None
        with self.journal.transaction() as connection:
            found = connection.execute("SELECT mode, data FROM project_check_runs WHERE owner=? AND request_id=?",
                                       (owner, run["requestId"])).fetchone()
            if found is not None:
                return found[0], json.loads(found[1]), False
            kept = _kept(connection, owner, run["startedAt"] / 1000, request_id=run["requestId"])
            if kept is not None:   # its run was pruned: the key still refuses a second run
                return kept["mode"], kept["run"], False
            if authorize is not None:
                try:
                    authorize(run["projectId"], revision, grant_id, connection)
                except Exception as error:  # noqa: BLE001 -- preserve the original denial outside Journal.transaction
                    denied = error
            if denied is None:
                connection.execute("INSERT INTO project_check_runs(id, project_id, owner, grant_id, request_id, "
                                   "mode, revision, state, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                   (run["id"], run["projectId"], owner, grant_id, run["requestId"], run["mode"],
                                    revision, run["state"], json.dumps(run, separators=(",", ":"))))
        if denied is not None:
            raise denied
        return run["mode"], run, True

    def save_run(self, run):
        """Write a run's state while its row is unfinished: a late snapshot never overwrites a finished row. Only
        complete_ci ends a CI row, so its verdict and its results are written together."""
        with self.journal.transaction() as connection:
            connection.execute(f"UPDATE project_check_runs SET state=?, data=? WHERE id=? AND state IN {UNFINISHED} "
                               f"AND (owner NOT LIKE 'ci:%' OR ? IN {UNFINISHED})",
                               (run["state"], json.dumps(run, separators=(",", ":")), run["id"], run["state"]))

    def prune(self, project_id, now):
        """Keep the project's newest RUN_KEEP runs: up to PRUNE_BATCH older finished ones go (_retire), and up to
        PRUNE_BATCH expired keys. An unfinished run is never pruned. One transaction, so a failure leaves every row and
        key as it was. Returns how many runs went."""
        with self.journal.transaction() as connection:
            rows = connection.execute(
                f"SELECT {RETIRED} FROM project_check_runs WHERE project_id=? AND state NOT IN {UNFINISHED} "
                "AND rowid < (SELECT min(rowid) FROM (SELECT rowid FROM project_check_runs WHERE project_id=? "
                "ORDER BY rowid DESC LIMIT ?)) ORDER BY rowid LIMIT ?",
                (project_id, project_id, RUN_KEEP, PRUNE_BATCH)).fetchall()
            _retire(connection, rows, now)
            connection.execute("DELETE FROM project_request_keys WHERE rowid IN (SELECT rowid FROM "
                               "project_request_keys WHERE at<? LIMIT ?)", (now - KEY_DAYS * 86_400, PRUNE_BATCH))
        return len(rows)

    def approve_revision(self, project_id, manifest, fingerprint):
        """An exact repeated local approval gets its original immutable positive revision."""
        with self.journal.transaction() as connection:
            prior = connection.execute("SELECT revision FROM project_test_revisions WHERE project_id=? AND "
                                       "fingerprint=?", (project_id, fingerprint)).fetchone()
            if prior is not None:
                return prior[0], False
            count = connection.execute("SELECT count(*) FROM project_test_revisions WHERE project_id=?",
                                       (project_id,)).fetchone()[0]
            if count >= MAX_REVISIONS:
                # The oldest revision no kept run names makes room. The newest stays, so a number is never given twice.
                spare = connection.execute(
                    "SELECT revision FROM project_test_revisions AS r WHERE project_id=? AND revision < (SELECT "
                    "max(revision) FROM project_test_revisions WHERE project_id=r.project_id) AND NOT EXISTS (SELECT 1 "
                    "FROM project_check_runs WHERE project_id=r.project_id AND revision=r.revision) ORDER BY revision "
                    "LIMIT 1", (project_id,)).fetchone()
                if spare is None:
                    raise JournalError("Every older approved test revision of this project is named by a kept run")
                connection.execute("DELETE FROM project_test_revisions WHERE project_id=? AND revision=?",
                                   (project_id, spare[0]))
            revision = connection.execute("SELECT coalesce(max(revision), 0) + 1 FROM project_test_revisions "
                                          "WHERE project_id=?", (project_id,)).fetchone()[0]
            connection.execute("INSERT INTO project_test_revisions VALUES (?, ?, ?, ?)",
                               (project_id, revision, fingerprint,
                                json.dumps(manifest, sort_keys=True, separators=(",", ":"))))
        return revision, True

    def revision(self, project_id, revision):
        rows = self._read("SELECT manifest FROM project_test_revisions WHERE project_id=? AND revision=?",
                          (project_id, revision))
        return json.loads(rows[0][0]) if rows else None

    def latest_revisions(self):
        rows = self._read("SELECT project_id, max(revision) FROM project_test_revisions GROUP BY project_id")
        return rows

    def ci_run(self, run_id=None, *, owner, request_id=None, with_artifacts=False, now=None):
        """A CI row by its id or its request. A removed run's key answers either with its summary and no results."""
        blobs = "result_json, result_junit" if with_artifacts else "NULL, NULL"
        columns = ("project_id, grant_id, revision, data, " + blobs + ", result_error, "
                   "CASE WHEN result_json IS NOT NULL AND result_junit IS NOT NULL THEN 1 ELSE 0 END")
        if run_id is not None:
            rows = self._read(f"SELECT {columns} FROM project_check_runs WHERE id=? AND owner=?", (run_id, owner))
        else:
            rows = self._read(f"SELECT {columns} FROM project_check_runs WHERE request_id=? AND owner=?",
                              (request_id, owner))
        if not rows:
            kept = self._kept(owner, now, request_id=request_id) if run_id is None else \
                self._kept(owner, now, run_id=run_id)
            return kept and {"project_id": kept["project_id"], "grant_id": kept["grant_id"],
                             "revision": kept["revision"], "run": kept["run"], "artifacts": None, "available": False,
                             "result_error": "expired"}
        project_id, grant_id, revision, data, result_json, result_junit, result_error, available = rows[0]
        return {"project_id": project_id, "grant_id": grant_id, "revision": revision, "run": json.loads(data),
                "artifacts": {"json": result_json, "junit": result_junit} if result_json is not None and
                result_junit is not None else None, "available": bool(available), "result_error": result_error}

    def complete_ci(self, run, artifacts, error=None):
        with self.journal.transaction() as connection:
            connection.execute("UPDATE project_check_runs SET state=?, data=?, result_json=?, result_junit=?, "
                               f"result_error=? WHERE id=? AND owner LIKE 'ci:%' AND state IN {UNFINISHED}",
                               (run["state"], json.dumps(run, separators=(",", ":")),
                                (artifacts or {}).get("json"), (artifacts or {}).get("junit"), error, run["id"]))
            connection.execute("UPDATE project_check_runs SET result_json=NULL, result_junit=NULL, "
                               "result_error='expired' WHERE id IN (SELECT id FROM project_check_runs WHERE "
                               "owner LIKE 'ci:%' AND result_json IS NOT NULL AND result_junit IS NOT NULL "
                               "AND state NOT IN ('building','running') ORDER BY rowid DESC LIMIT -1 OFFSET ?)",
                               (RESULT_KEEP,))

    def interrupt_unfinished(self, at):
        """A restarted process cannot know an old build or device action's outcome, so it never replays it."""
        with self.journal.transaction() as connection:
            rows = connection.execute("SELECT id, project_id, request_id, mode, data FROM project_check_runs "
                                      f"WHERE state IN {UNFINISHED}").fetchall()
            for run_id, project_id, request_id, mode, raw in rows:
                try:
                    run = json.loads(raw)
                except (ValueError, RecursionError):
                    run = None
                if not isinstance(run, dict):   # a row that can't be read still ends, from its columns
                    run = {"id": run_id, "projectId": project_id, "requestId": request_id, "mode": mode,
                           "stopRequested": False, "done": 0, "total": None, "summary": None, "startedAt": None,
                           "choice": None, "error": None, "fix": None}
                run.update(state="interrupted", code="interrupted", exitCode=3, endedAt=round(at * 1000),
                           line="Interrupted when Mobster restarted")
                connection.execute("UPDATE project_check_runs SET state='interrupted', data=? WHERE id=?",
                                   (json.dumps(run, separators=(",", ":")), run_id))
