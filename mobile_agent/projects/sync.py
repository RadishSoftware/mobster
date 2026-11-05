"""Workflows as files: each project's `.mobster/workflows/*.yaml` and the workflows Mobster runs, in step both ways.

Every 1.5 seconds Mobster looks at each project's workflow files (a stat each; a file is read only when it changed)
and at the workflows that have a file (the record's ``file``: where it is, the hash and the published revision last
in step, and how it stands). Then, for each file:

- **A new file** becomes a workflow, published, its schedule off until you turn it on. A file that doesn't parse or
  check stays out and is listed with its problem.
- **The file changed** (a pull, an editor): its graph is checked as any save is and published as a new revision, so
  Versions keeps the one before. If the workflow has an unpublished draft, or was published since, nothing is
  overwritten: the file is ``conflict`` until you resolve it.
- **The workflow was published** (Publish, the card builder, Bring it back) or renamed: the file is written, unless
  it changed on disk since (``conflict``) or the graph names a real iPhone or holds a secret (``blocked``).
- **The file was deleted**: the workflow stays and says so (``missing``); publishing writes the file again.
- **The workflow was deleted in Mobster**: its file goes too, only when it is as Mobster last left it.

Saving a draft never writes the file. Nothing is retried: a write that failed says why on the next look. The rest
of a workflow (its phone, profile, destination, schedule switch, pins) stays on the Mac. Events: ``workflow_file``
on the ``workflows`` topic.
"""

import os
import threading
import time
from pathlib import Path

from ..api_errors import APIError
from ..paths import write_text_atomic
from . import folder
from .workflow_files import MAX_BYTES, SUFFIXES, FileError, blocked, digest, parse, render, slug

INTERVAL = 1.5
MAX_FILES = 200
FOLDER = Path(".mobster") / "workflows"
TOO_BIG = "is larger than 512 KB, so Mobster didn't read it."


PATH_PROBLEM = ("The workflow path contains a link or leaves this project. Keep .mobster/workflows and its files "
                "inside the project without links, then publish again.")


def _safe_path(root, path):
    """Workflow files stay under their registered project and never follow a link, including a parent folder."""
    root, path = Path(root), Path(path)
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return ".." not in relative.parts and not any(
        root.joinpath(*relative.parts[:index]).is_symlink() for index in range(len(relative.parts) + 1))


def _live(record):
    published = record.get("published")
    return published["graph"] if isinstance(published, dict) and isinstance(published.get("graph"), dict) \
        else record["graph"]


def _published_revision(record):
    published = record.get("published")
    return published.get("revision") if isinstance(published, dict) else None


class WorkflowSync:
    """One project's workflow files and their workflows, for every project of a ProjectService."""

    def __init__(self, service, workflows, *, interval=INTERVAL, clock=time.time):
        self.service, self.workflows = service, workflows
        self.interval, self.clock = interval, clock
        self._files = {}          # absolute path -> (mtime_ns, size, text or None, hash or None)
        self._invalid = {}        # (project id, path) -> (hash, sentence): new files that don't parse or check
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        workflows.deleted.append(self._deleted)

    # -- the loop --------------------------------------------------------------------------------------------------

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="mobster-workflow-files", daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._deleted in self.workflows.deleted:
            self.workflows.deleted.remove(self._deleted)
        if self._thread is not None:
            self._thread.join(2)

    def _loop(self):
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 -- one bad look never stops the next
                from .. import harness_api
                harness_api.log.exception("workflow files: a look failed")

    def tick(self):
        """Look once at every project's workflow files and bring each pair in step."""
        with self._lock:
            records = self.workflows.file_records()
            for project in self.service.store.projects():
                if folder.state(project["path"], self.service.home)[0] != "ready":
                    continue
                mine = [record for record in records if record["file"].get("projectId") == project["id"]]
                self._project(project, mine)

    def _project(self, project, records):
        root = Path(project["path"])
        if not _safe_path(root, root / FOLDER):
            for record in records:
                self._mark(project, record, "blocked", PATH_PROBLEM)
            return
        files = {}
        try:
            entries = sorted(os.scandir(root / FOLDER), key=lambda entry: entry.name)
        except OSError:
            entries = []
        for entry in entries[:MAX_FILES]:
            if (not entry.name.startswith(".") and entry.name.lower().endswith(SUFFIXES)
                    and entry.is_file(follow_symlinks=False)):
                files[(FOLDER / entry.name).as_posix()] = Path(entry.path)
        links = {record["file"].get("path"): record for record in records}
        for path, absolute in files.items():
            text, hashed = self._read(absolute, project["path"])
            record = links.pop(path, None)
            if record is None:
                self._new(project, path, text, hashed)
            else:
                self._pair(project, record, path, absolute, text, hashed)
        for path, record in links.items():
            self._gone(project, record, root / path)
        known = set(files)
        for key in [key for key in self._invalid if key[0] == project["id"] and key[1] not in known]:
            self._invalid.pop(key, None)

    def _read(self, absolute, root):
        """(text, hash) of a file, read again only when its size or time changed; (None, None) when it can't be read,
        ("", TOO_BIG) when it is too large."""
        if not _safe_path(root, absolute):
            return None, None
        try:
            status = absolute.stat()
        except OSError:
            return None, None
        kept = self._files.get(str(absolute))
        if kept and kept[:2] == (status.st_mtime_ns, status.st_size):
            return kept[2], kept[3]
        if status.st_size > MAX_BYTES:
            text, hashed = "", TOO_BIG
        else:
            try:
                text = absolute.read_text(encoding="utf-8")
                hashed = digest(text)
            except (OSError, UnicodeDecodeError):
                text, hashed = None, None
        self._files[str(absolute)] = (status.st_mtime_ns, status.st_size, text, hashed)
        return text, hashed

    # -- each case -------------------------------------------------------------------------------------------------

    def _link(self, project, path, record, *, hashed, revision, state="synced", problem=None):
        file = {"projectId": project["id"], "projectName": project["name"], "path": path, "hash": hashed,
                "revision": revision, "name": record["name"], "state": state, "problem": problem,
                "syncedAt": round(self.clock() * 1000)}
        before = record.get("file") or {}
        if all(before.get(key) == file[key] for key in ("hash", "revision", "name", "state", "problem", "path")):
            return
        if self.workflows.set_file(record["id"], file) is not None:
            self._publish(record["id"], state, revision)

    def _mark(self, project, record, state, problem):
        before = record["file"]
        self._link(project, before["path"], record, hashed=before.get("hash"), revision=before.get("revision"),
                   state=state, problem=problem)

    def _new(self, project, path, text, hashed):
        key = (project["id"], path)
        if text is None or self._invalid.get(key, (None,))[0] == hashed:
            return
        if hashed == TOO_BIG:
            self._invalid[key] = (hashed, f"{path} {TOO_BIG}")
            return
        try:
            name, graph = parse(text, path)
            created = self.workflows.create({"name": name, "graph": graph, "publish": True})
        except (FileError, ValueError, APIError) as error:
            self._invalid[key] = (hashed, _sentence(path, error))
            self._publish(None, "invalid", None, path=path, projectId=project["id"])
            return
        self._invalid.pop(key, None)
        self._link(project, path, {**created, "file": None}, hashed=hashed, revision=created["revision"])

    def _pair(self, project, record, path, absolute, text, hashed):
        file = record["file"]
        if text is None:
            return                                      # unreadable for a moment: the next look tells
        file_changed = hashed != file.get("hash")
        app_changed = _published_revision(record) != file.get("revision") or record["name"] != file.get("name")
        if hashed == TOO_BIG:
            if file_changed:
                self._mark(project, record, "invalid", f"{path} {TOO_BIG}")
            return
        if file.get("state") == "conflict":
            return                                      # it waits for resolve()
        if file_changed and (app_changed or record["graph"] != _live(record)):
            self._mark(project, record, "conflict",
                       f"{path} changed while this workflow had changes of its own in Mobster. Keep the file's version "
                       "or Mobster's.")
        elif file_changed:
            self._take(project, record, path, text, hashed)
        elif app_changed:
            self._write(project, record, path, absolute)
        elif file.get("state") != "synced":
            self._mark(project, record, "synced", None)

    def _take(self, project, record, path, text, hashed):
        """The file's graph becomes the published one (a new revision; Versions keeps the one before)."""
        if file_problem := self._invalid.get((project["id"], path)):
            if file_problem[0] == hashed:
                return
        try:
            name, graph = parse(text, path, previous=record["graph"])
            saved = self.workflows.update(record["id"], {"revision": record["revision"], "graph": graph,
                                                         "publish": True, "name": name})
        except APIError as error:
            if error.code == "revision_conflict":
                return                                      # saved meanwhile: the next look sees both changes
            self._mark(project, record, "invalid", _sentence(path, error))
            return
        except (FileError, ValueError) as error:
            self._invalid[(project["id"], path)] = (hashed, _sentence(path, error))
            file = record["file"]
            self._link(project, path, record, hashed=file.get("hash"), revision=file.get("revision"), state="invalid",
                       problem=_sentence(path, error))
            return
        self._invalid.pop((project["id"], path), None)
        self._link(project, path, {**record, "name": saved["name"]}, hashed=hashed, revision=saved["revision"])

    def _write(self, project, record, path, absolute):
        """The workflow's published graph is written to its file."""
        if not _safe_path(project["path"], absolute):
            self._mark(project, record, "blocked", PATH_PROBLEM)
            return
        graph = _live(record)
        reason = blocked(graph)
        if reason:
            self._mark(project, record, "blocked", reason)
            return
        text = render(record["name"], graph)
        try:
            write_text_atomic(absolute, text)
        except OSError as error:
            self._mark(project, record, "blocked", f"Mobster couldn't write {path}: {error.strerror or error}. Check "
                                                   "the folder, then publish again.")
            return
        try:
            status = absolute.stat()
            self._files[str(absolute)] = (status.st_mtime_ns, status.st_size, text, digest(text))
        except OSError:
            pass
        self._link(project, path, record, hashed=digest(text), revision=_published_revision(record))

    def _gone(self, project, record, absolute):
        file = record["file"]
        if _published_revision(record) != file.get("revision") or record["name"] != file.get("name"):
            self._write(project, record, file["path"], absolute)
        elif file.get("state") != "missing":
            self._mark(project, record, "missing",
                       f"{file['path']} isn't in the project any more. Publish the workflow to write it again, or "
                       "delete the workflow.")

    def _deleted(self, record):
        """A workflow deleted in Mobster takes its file with it, only when the file is as Mobster last left it."""
        file = record.get("file")
        if not isinstance(file, dict):
            return
        project = self.service.store.project(file.get("projectId"))
        if project is None:
            return
        absolute = Path(project["path"]) / file["path"]
        with self._lock:
            text, hashed = self._read(absolute, project["path"])
            if text is not None and hashed == file.get("hash"):
                try:
                    absolute.unlink()
                except OSError:
                    return
                self._files.pop(str(absolute), None)

    # -- asked from the API ----------------------------------------------------------------------------------------

    def _record(self, workflow_id):
        record = next((item for item in self.workflows.file_records() if item["id"] == workflow_id), None)
        if record is None:
            raise APIError("That workflow has no file in a project.", 404, "not_found")
        project = self.service.store.project(record["file"].get("projectId"))
        if project is None:
            raise APIError("That workflow's project isn't in Mobster any more.", 404, "not_found")
        return record, project

    def resolve(self, workflow_id, keep):
        """POST /api/projects/workflows/{id}/resolve {keep}: "file" publishes the file over Mobster's changes (they
        stay in Versions); "mobster" publishes the workflow's draft and writes it over the file."""
        if keep not in ("file", "mobster"):
            raise ValueError('keep is "file" or "mobster"')
        with self._lock:
            record, project = self._record(workflow_id)
            path = record["file"]["path"]
            absolute = Path(project["path"]) / path
            if keep == "file":
                text, hashed = self._read(absolute, project["path"])
                if text is None:
                    raise APIError(f"{path} isn't there to keep. Keep Mobster's version instead.", 409, "missing")
                record = {**record, "file": {**record["file"], "state": "synced"}}
                self._take(project, record, path, text, hashed)
            else:
                if record["graph"] != _live(record):
                    self.workflows.update(record["id"], {"revision": record["revision"], "graph": record["graph"],
                                                         "publish": True})
                    record = next(item for item in self.workflows.file_records() if item["id"] == workflow_id)
                self._write(project, {**record, "file": {**record["file"], "revision": None}}, path, absolute)
            return self.workflows.get(workflow_id)

    def add(self, project, workflow_id):
        """POST /api/projects/{id}/workflows {workflowId}: the workflow gets a file in the project, written from its
        published graph on the next look."""
        if not isinstance(workflow_id, str):
            raise ValueError("Send the workflow's id")
        with self._lock:
            workflow = self.workflows.get(workflow_id)
            if workflow.get("file"):
                raise APIError(f"{workflow['name']} already has a file: {workflow['file']['path']}.", 409, "exists")
            directory = Path(project["path"]) / FOLDER
            taken = {entry.name for entry in _entries(directory)} if _safe_path(project["path"], directory) else set()
            taken.update(Path(record["file"]["path"]).name for record in self.workflows.file_records()
                         if record["file"].get("projectId") == project["id"])
            path = (FOLDER / slug(workflow["name"], taken)).as_posix()
            self.workflows.set_file(workflow_id, {"projectId": project["id"], "projectName": project["name"],
                                                  "path": path, "hash": None, "revision": None,
                                                  "name": workflow["name"], "state": "pending", "problem": None,
                                                  "syncedAt": None})
            self._publish(workflow_id, "pending", None)
            return self.workflows.get(workflow_id)

    def forget(self, project_id):
        """The project was removed from Mobster: its workflows stay, without a file."""
        with self._lock:
            for record in self.workflows.file_records():
                if record["file"].get("projectId") == project_id:
                    self.workflows.set_file(record["id"], None)
            for key in [key for key in self._invalid if key[0] == project_id]:
                self._invalid.pop(key, None)

    def files(self, project_id):
        """The project's workflow files: each workflow's and each file that isn't one, with its problem."""
        out = [{"path": record["file"]["path"], "workflowId": record["id"], "name": record["name"],
                "state": record["file"].get("state"), "problem": record["file"].get("problem")}
               for record in self.workflows.file_records() if record["file"].get("projectId") == project_id]
        out += [{"path": path, "workflowId": None, "name": None, "state": "invalid", "problem": problem}
                for (project, path), (_, problem) in self._invalid.items() if project == project_id]
        return sorted(out, key=lambda item: item["path"])

    def _publish(self, workflow_id, state, revision, **fields):
        publish = getattr(self.service.runtime, "publish", None)
        if callable(publish):
            publish("workflows", {"event": "workflow_file", "workflowId": workflow_id, "state": state,
                                  "revision": revision, **fields})


def _entries(path):
    try:
        return list(os.scandir(path))
    except OSError:
        return []


def _sentence(path, error):
    text = str(error)
    return text if text.startswith(path) else f"{path}: {text}"
