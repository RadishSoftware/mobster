"""A Runtime's projects: the saved list (store.py), what each folder holds (folder.py) and clones in flight
(clone.py). One ProjectService per Runtime, made by register_service's start and closed with the Runtime.

It publishes on the event bus topic ``projects``: ``project_added``, ``project_opened``, ``project_removed`` and
``project_changed`` (each with ``projectId``), ``clone_changed`` (with ``clone``, at most 4 a second per clone),
``clone_ended``, ``run_changed`` (with ``run``, at most 4 a second) and ``run_ended``.
"""

import hashlib
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..api_errors import APIError
from . import clone as cloning
from . import drafts, folder, github, setup
from . import runs as checkruns
from .ci import ApprovedSuites
from .runs import MAX_RUNS, REQUEST_ID
from .store import ProjectStore
from .sync import WorkflowSync

MAX_CLONES = 3
KEEP_ENDED = 10
MAX_SUGGESTIONS = 5
GITHUB_KEEP = 60
SUITES = 20
FRAME_LIMIT = 8_000_000
MAX_NAME = 80
GONE = "Projects aren't available right now. Restart Mobster, then try again."

_services = {}
_services_lock = threading.Lock()


def service_for(runtime):
    with _services_lock:
        return _services.get(id(runtime))


def start(runtime):
    """register_service's start: a ProjectService for the runtime, and its workflow files' sync if it has any."""
    service = ProjectService(runtime)
    with _services_lock:
        _services[id(runtime)] = service
    service.start()


def close(runtime):
    with _services_lock:
        service = _services.pop(id(runtime), None)
    if service is not None:
        service.close()


def default_clone_folder(home=None):
    """Where a clone goes when the person names no folder: ~/Mobster/Projects."""
    return Path(home or Path.home()) / "Mobster" / "Projects"


def ms(seconds):
    return round(seconds * 1000) if isinstance(seconds, (int, float)) else None


def _name(value, fallback):
    if value is None:
        return fallback[:MAX_NAME]
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= MAX_NAME:
        raise ValueError(f"A project's name is 1 to {MAX_NAME} characters")
    return " ".join(value.split())


class ProjectService(ApprovedSuites):
    def __init__(self, runtime, *, home=None, data_dir=None, clock=time.time, popen=None, last_runs=None,
                 opener=None, run_parts=None):
        self.runtime = runtime
        self.store = ProjectStore(runtime.journal)
        self.home = Path(home) if home else None
        self.data_dir = data_dir if data_dir is not None else _data_dir()
        self.clock = clock
        self._popen = popen
        self._last_runs = last_runs or _last_runs
        self._opener = opener      # urllib's opener for GitHub (tests pass a fake)
        self._github = None        # (the sign-in's hash, when, repos): the list, kept GITHUB_KEEP seconds
        self._pulls = {}           # (the sign-in's hash, repo) -> (when, {branch: pull}), kept GITHUB_KEEP seconds
        self._clones = {}          # id -> Clone, in flight and the last KEEP_ENDED that ended
        self._runs = {}            # id -> CheckRun, the newest of each project kept after it ends
        self._run_parts = run_parts or {}   # plan, build, execute, popen for CheckRun (tests pass fakes)
        self._lock = threading.RLock()
        self._closed = False
        self._unsettled = False    # runs from before a restart could not be marked interrupted: admit no new one
        self._memory_work = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mobster-project-memory")
        workflows = getattr(runtime, "workflows", None)
        self.sync = WorkflowSync(self, workflows, clock=clock) if workflows is not None else None

    def start(self):
        try:
            if getattr(self.store.journal, "schema_status", {}).get("projects", "ok") == "ok":
                self.store.repair()   # a journal an earlier build of this branch wrote: add what v2 now holds
            self.store.interrupt_unfinished(self.clock())
        except Exception:
            self._unsettled = True
            raise
        if self.sync is not None:
            self.sync.start()

    # -- reading ---------------------------------------------------------------------------------------------------

    def _public(self, record, read, last_runs):
        return {"id": record["id"], "name": record["name"], "path": record["path"],
                "shownPath": folder.shown(record["path"], self.home), "source": record["source"],
                "addedAt": ms(record["added_at"]), "openedAt": ms(record["opened_at"]), **read,
                "lastRun": last_runs.get(record["path"]), "run": self._run_of(record["id"])}

    def list(self):
        """{projects (the one opened last first), suggestions, clones, cloneFolder and cloneFolderShown (~/…)}."""
        records = self.store.projects()
        last_runs = self._last_runs()
        projects = [self._public(record, folder.summary(record["path"], self.home), last_runs) for record in records]
        clone_folder = default_clone_folder(self.home)
        return {"projects": projects, "suggestions": self._suggestions(records, last_runs), "clones": self.clones(),
                "cloneFolder": str(clone_folder), "cloneFolderShown": folder.shown(clone_folder, self.home)}

    def get(self, project_id):
        """A project with its check files, its branch's state and its workflow files (``workflowFiles``)."""
        record = self._record(project_id)
        detail = folder.detail(record["path"], self.home)
        detail["workflowFiles"] = self.sync.files(record["id"]) if self.sync is not None else []
        return self._public(record, detail, self._last_runs())

    def add_workflow(self, project_id, body):
        """POST /api/projects/{id}/workflows {workflowId}: the workflow gets a file in the project."""
        record = self._record(project_id)
        if not isinstance(body, dict) or set(body) != {"workflowId"}:
            raise ValueError("Send the workflow's workflowId")
        if self.sync is None:
            raise APIError(GONE, 503, "unavailable")
        return self.sync.add(record, body["workflowId"])

    def resolve(self, workflow_id, body):
        """POST /api/projects/workflows/{id}/resolve {keep}: settle a workflow file's conflict."""
        if not isinstance(body, dict) or set(body) != {"keep"}:
            raise ValueError('Send keep: "file" or "mobster"')
        if self.sync is None:
            raise APIError(GONE, 503, "unavailable")
        return self.sync.resolve(workflow_id, body["keep"])

    def tests(self, project_id):
        """GET /api/projects/{id}/tests: this project's test results and nothing from any other folder: each check's
        last 20 results with its pass rate and flake state (testkit/history.py, from the project's own
        .mobster/test-results), and its last suites with what didn't pass and why (testkit/recent.py)."""
        record = self._record(project_id)
        if folder.state(record["path"], self.home)[0] != "ready":
            return {"checks": [], "ok": True, "suites": []}
        from ..testkit import history, recent
        return {**history.report(record["path"]), "suites": recent.recent(SUITES, root=record["path"])}

    def icon(self, project_id):
        """GET /api/projects/{id}/icon: (the PNG's bytes, its version) of the app's own icon, or APIError 404."""
        record = self._record(project_id)
        path = folder.icon(record["path"]) if folder.state(record["path"], self.home)[0] == "ready" else None
        try:
            if path is None or path.stat().st_size > folder.ICON_LIMIT:
                raise OSError
            data = path.read_bytes()
        except OSError:
            raise APIError("This project's app has no icon Mobster can show.", 404, "no_icon") from None
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise APIError("This project's app has no icon Mobster can show.", 404, "no_icon")
        return data

    def frame(self, project_id, run, path):
        """GET /api/projects/{id}/frame?run=&path=: (bytes, type) of a frame a check's run took, from the project's own
        .mobster/runs/<run>/frames, so a failure shows the screen it failed on (ICP-3). Only a run id and a frame's file
        name are taken (testkit/recent's RUN_ID and FRAME), and only a JPEG or PNG inside that folder is read."""
        from ..testkit.recent import FRAME, RUN_ID
        record = self._ready(project_id)
        if not (isinstance(run, str) and RUN_ID.match(run) and isinstance(path, str) and FRAME.match(path)):
            raise APIError("Name a run and one of its frames.", 400, "bad_frame")
        runs = (Path(record["path"]) / ".mobster" / "runs").resolve()
        target = (runs / run / path).resolve()
        try:
            if runs not in target.parents or not target.is_file() or target.stat().st_size > FRAME_LIMIT:
                raise OSError
            data = target.read_bytes()
        except OSError:
            raise APIError("That frame isn't in this project's runs any more.", 404, "no_frame") from None
        kind = ("image/png" if data.startswith(b"\x89PNG\r\n\x1a\n")
                else "image/jpeg" if data[:3] == b"\xff\xd8\xff" else None)
        if kind is None:
            raise APIError("That frame isn't an image Mobster can show.", 404, "no_frame")
        return data, kind

    def _record(self, project_id):
        record = self.store.project(project_id) if isinstance(project_id, str) else None
        if record is None:
            raise APIError("That project isn't in Mobster. It may have been removed.", 404, "not_found")
        return record

    def _suggestions(self, records, last_runs):
        """Folders `mobster test` ran in that aren't projects yet and are still there, newest first."""
        known = {record["path"] for record in records}
        out = []
        for root, run in sorted(last_runs.items(), key=lambda item: -(item[1].get("startedAt") or 0)):
            if len(out) >= MAX_SUGGESTIONS or root in known:
                continue
            if folder.state(root, self.home)[0] == "ready":
                out.append({"path": root, "name": Path(root).name, "shownPath": folder.shown(root, self.home),
                            "lastRun": run})
        return out

    # -- changing --------------------------------------------------------------------------------------------------

    def add(self, body, source=None):
        """POST /api/projects {path, name?}: (the project, created). Adding a folder that is a project already opens
        it and answers it, created false."""
        if not isinstance(body, dict) or set(body) - {"path", "name"}:
            raise ValueError("Send the folder's path and, if you like, a name")
        path = folder.choose(body.get("path"), home=self.home, data_dir=self.data_dir)
        name = _name(body.get("name"), path.name)
        now = self.clock()
        record, created = self.store.add({"id": uuid.uuid4().hex[:12], "path": str(path), "name": name,
                                          "source": source or {"kind": "folder"}, "added_at": now, "opened_at": now})
        if not created:
            record = self.store.opened(record["id"], now)
        self._publish("project_added" if created else "project_opened", projectId=record["id"])
        self._tell_memory(record)
        return self._public(record, folder.summary(record["path"], self.home), self._last_runs()), created

    def open(self, project_id):
        """POST /api/projects/{id}/open: it becomes the project opened last, which the app shows first."""
        record = self._record(project_id)
        record = self.store.opened(record["id"], self.clock())
        self._publish("project_opened", projectId=record["id"])
        self._tell_memory(record)
        return self._public(record, folder.summary(record["path"], self.home), self._last_runs())

    def forget(self, project_id):
        """DELETE /api/projects/{id}: Mobster forgets the project. The folder and everything in it stay."""
        record = self._record(project_id)
        self.store.delete(record["id"], self.clock())
        if self.sync is not None:
            self.sync.forget(record["id"])
        self._publish("project_removed", projectId=record["id"])
        return record["id"]

    def run_field(self, value):
        """The run field ``project`` (POST /api/runs, and a project's workflows): a project's id, or {id} or
        {id, path}, as {id, path, name}, so a task's memory can be the project's (memory reads id and path).
        ValueError for a project Mobster doesn't have."""
        project_id = value.get("id") if isinstance(value, dict) else value
        if isinstance(value, dict) and set(value) - {"id", "path", "name"}:
            raise ValueError("project is a project's id, or {id}")
        record = self.store.project(project_id) if isinstance(project_id, str) else None
        if record is None:
            raise ValueError("That project isn't in Mobster. Add its folder in Projects, then start the task again.")
        return {"id": record["id"], "path": record["path"], "name": record["name"]}

    def _tell_memory(self, record):
        """Tell memory a project's folder (its POST /api/memory/projects {id, path}, when the memory track is here):
        what it learned before the project had an id moves to it, and .mobster/memory.yaml is read in. On one
        worker, so adding or opening a project never waits on it; a failure there changes nothing here."""
        from ..api_routes import Request, match, track_off
        found = match("POST", "/api/memory/projects")
        if found is None or track_off(found[0]):
            return
        route, params = found
        request = Request(runtime=self.runtime, method="POST", path="/api/memory/projects", params=params, query={},
                          headers={}, body={"id": record["id"], "path": record["path"]},
                          content_type="application/json", origin="app", app_session=True)

        def tell():
            try:
                route.handler(request)
            except Exception:  # noqa: BLE001 -- memory keeps its own errors; the project is added either way
                from .. import harness_api
                harness_api.log.exception("projects: memory couldn't link %s", record["id"])
        with self._lock:
            if not self._closed:
                self._memory_work.submit(tell)

    # -- checks ----------------------------------------------------------------------------------------------------

    def setup(self, project_id, body):
        """POST /api/projects/{id}/setup {app?}: `.mobster/` with its .gitignore and a starter check for the app
        (setup.py). Returns (the project, the files written). Refused when the project has checks already."""
        if body is None:
            body = {}
        if not isinstance(body, dict) or set(body) - {"app"} or not isinstance(body.get("app", ""), str):
            raise ValueError("Send nothing, or the app's path as its project lists it")
        record = self._ready(project_id)
        created = setup.create(record["path"], folder.apps(record["path"]), wanted=body.get("app"), home=self.home)
        self._publish("project_changed", projectId=record["id"])
        return self.get(record["id"]), created

    def author_check(self, project_id, body):
        record = self._ready(project_id)
        bundles = {app["bundle"] for app in folder.apps(record["path"]) if app.get("bundle")}
        saved = drafts.author(Path(record["path"]), body, bundles)
        if not saved["replayed"]:
            self._publish("project_changed", projectId=record["id"])
        return saved

    def save_finished_check(self, project_id, body):
        record = self._ready(project_id)
        bundles = {app["bundle"] for app in folder.apps(record["path"]) if app.get("bundle")}
        saved = drafts.save_finished(self.runtime, Path(record["path"]), body, bundles)
        if not saved["replayed"]:
            self._publish("project_changed", projectId=record["id"])
        return saved

    def run_checks(self, project_id, body):
        """Claim the request durably, then build and execute once. A replay returns its original run.

        The body is {requestId, mode} (or nothing: a new auto run) and, while the project page's testing sections are
        on, the run's choice beside it: {sims} or {device}, and {tags} or {links: true} (_choice). A replay must name
        the same mode and choice as its request did."""
        if body is not None and not isinstance(body, dict):
            raise ValueError("Send requestId and mode: auto, strict or record")
        body = body or {}
        asked = {key: body[key] for key in ("requestId", "mode") if key in body}
        if not asked:
            mode, request_id = "auto", uuid.uuid4().hex
        elif set(asked) == {"requestId", "mode"} and asked["mode"] in ("auto", "strict", "record") and \
                isinstance(asked["requestId"], str) and REQUEST_ID.fullmatch(asked["requestId"]):
            mode, request_id = asked["mode"], asked["requestId"]
        else:
            raise ValueError("Send requestId and mode: auto, strict or record")
        choice = self._choice({key: value for key, value in body.items() if key not in asked}, phone=False)
        record = self._record(project_id)
        with self._lock:
            self._admit()
            prior = self.store.requested_run(record["id"], request_id, now=self.clock())
            if prior is not None:
                if prior[0] != mode or (prior[1].get("choice") or checkruns.Choice().public()) != choice.public():
                    raise APIError("That request id already belongs to a different check run.", 409,
                                   "idempotency_conflict")
                return prior[1]
            record = self._ready(project_id)
            if not folder.checks(record["path"]):
                raise APIError("This project has no checks to run. Set up checks first.", 409, "no_checks")
            going = [run for run in self._runs.values() if run.going]
            mine = next((run for run in going if run.project_id == record["id"]), None)
            if mine is not None:
                raise APIError("This project's checks are running already.", 409, "running",
                               details={"run": mine.id})
            if len(going) >= MAX_RUNS:
                raise APIError(f"Mobster runs at most {MAX_RUNS} projects' checks at once. Wait for one to finish.",
                               409, "busy")
            if choice.device:
                self._phone_free(choice.device)
            run = checkruns.CheckRun(uuid.uuid4().hex[:12], record["id"], record["path"], mode=mode,
                                     request_id=request_id, choice=choice, on_change=self._run_changed,
                                     clock=self.clock, home=self.home, **self._run_parts)
            previous_mode, accepted, created = self.store.claim_run(run.public())
            if not created:
                if previous_mode != mode:
                    raise APIError("That request id already belongs to a different check-run mode.", 409,
                                   "idempotency_conflict")
                return accepted
            for old in [old for old in self._runs.values() if old.project_id == record["id"]]:
                self._runs.pop(old.id, None)    # a project keeps its newest run only
            self._runs[run.id] = run
            try:
                run.start()
            except (OSError, RuntimeError):
                run._fail("worker_unavailable", "Mobster could not start the check run.",
                          "Restart Mobster, then start a new check run.")
            self._prune(record["id"])
        return run.public()

    def _choice(self, body, *, phone=True):
        """The run's Choice from the request. An iPhone runs only with a plan and while no task holds it: checked here
        unless ``phone`` is False, when the caller checks it after a replay could still answer (run_checks)."""
        from ..testkit.register import page_on
        if body in (None, {}):
            return checkruns.Choice()
        if not page_on():
            raise ValueError("Run checks takes no options yet: send nothing")

        def texts(name, most, limit):
            value = body.get(name) or []
            if not isinstance(value, list) or len(value) > most or not all(
                    isinstance(item, str) and item.strip() and len(item) <= limit for item in value):
                raise ValueError(f"{name} is a list of at most {most} names.")
            return tuple(dict.fromkeys(item.strip() for item in value))
        if not isinstance(body, dict) or set(body) - {"sims", "device", "tags", "links"}:
            raise ValueError("Send sims or device, and tags or links: true, or nothing.")
        sims, tags, device = texts("sims", 8, 128), texts("tags", 10, 40), body.get("device")
        if device is not None and (not isinstance(device, str) or not device.strip() or len(device) > 128):
            raise ValueError("device is an iPhone's name, as Mobster lists it.")
        if not isinstance(body.get("links", False), bool):
            raise ValueError("links is true or false.")
        if sims and device:
            raise ValueError("Pick simulators or an iPhone, not both.")
        if tags and body.get("links"):
            raise ValueError("Checking links takes no tags.")
        if device and phone:
            self._phone_free(device.strip())
        return checkruns.Choice(sims=sims, device=device.strip() if device else None, tags=tags,
                                links=bool(body.get("links")))

    def _phone_free(self, name):
        """APIError unless ``name`` is a real iPhone this Mac has, the plan covers phones and no task holds it."""
        from ..device_targets import named_device
        from ..devices import AmbiguousDevice
        gate = getattr(self.runtime, "plan_gate", None)
        if gate is not None:
            gate.require()
        try:
            record = named_device(name)
        except AmbiguousDevice as error:
            raise APIError(str(error), 409, "ambiguous_device") from None
        if record is None or record.get("kind") == "simulator":
            raise APIError(f"No iPhone named {name} is on this Mac. Plug it in, or pick a simulator.", 404,
                           "device_not_found")
        busy = getattr(self.runtime, "device_busy", None)
        if busy is None or busy(record.get("id")):
            raise APIError(f"A task is running on {name}. Run the checks when it finishes.", 409, "device_busy")

    def run_status(self, run_id):
        run = self._check_run(run_id)
        return run.public() if isinstance(run, checkruns.CheckRun) else run

    def requested_run(self, project_id, request_id):
        self._record(project_id)
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise ValueError("requestId must be 1 to 128 letters, numbers, _, ., : or -")
        found = self.store.requested_run(project_id, request_id, now=self.clock())
        if found is None:
            raise APIError("Mobster has no check run for that request id.", 404, "not_found")
        return found[1]

    def stop_run(self, run_id):
        """DELETE /api/projects/runs/{id}: stop a run in flight. Its checks that ran keep their results."""
        run = self._check_run(run_id)
        if not isinstance(run, checkruns.CheckRun):
            return run
        if run.stop():
            self._save_going(run)
        run.join(5)
        return run.public()

    def _run_of(self, project_id):
        with self._lock:
            run = next((run for run in self._runs.values() if run.project_id == project_id and
                        run.owner == "local:" + project_id), None)
        return run.public() if run is not None else self.store.latest_run(project_id)

    def _check_run(self, run_id):
        with self._lock:
            run = self._runs.get(run_id)
            if run is not None and run.owner != "local:" + run.project_id:
                run = None
        if run is None:
            run = self.store.check_run(run_id, now=self.clock())
        if run is None:
            raise APIError("Mobster has no run with that id. It may have ended before Mobster last started.", 404,
                           "not_found")
        return run

    def _prune(self, project_id):
        """Keep the project's newest runs (store.prune). A failed prune changes nothing and never refuses a run."""
        try:
            self.store.prune(project_id, self.clock())
        except Exception:  # noqa: BLE001 -- its transaction rolled back; the next run prunes again
            from .. import harness_api
            harness_api.log.exception("projects: could not prune old check runs")

    def _save_going(self, run):
        """A stop's or a close's snapshot, written only while the run goes: its worker writes the ending."""
        with self._lock:
            if run.going:
                self.store.save_run(run.public())

    def _run_changed(self, run):
        with self._lock:
            public = run.public()
            self.store.save_run(public)
        self._publish("run_changed" if run.going else "run_ended", projectId=run.project_id, run=public)

    def _admit(self):
        """APIError unless the service may admit new work: it isn't closing, and its restart recovery held."""
        if self._closed:
            raise APIError(GONE, 503, "unavailable")
        if self._unsettled:
            raise APIError("Mobster couldn't settle the check runs it had before it restarted, so it starts no new "
                           "one. Restart Mobster.", 503, "unavailable")

    def _ready(self, project_id):
        """The project's record when its folder is there and readable; APIError with the folder's problem otherwise."""
        record = self._record(project_id)
        current, problem = folder.state(record["path"], self.home)
        if current != "ready":
            raise APIError(problem, 404 if current == "missing" else 403, current)
        return record

    # -- cloning ---------------------------------------------------------------------------------------------------

    def clone(self, body):
        """POST /api/projects/clones {url, folder?, name?}: start a clone; it becomes a project when git is done."""
        if not isinstance(body, dict) or set(body) - {"url", "folder", "name"}:
            raise ValueError("Send the repository's url and, if you like, the folder to clone into and a name")
        url, host, name = cloning.parse(body.get("url"))
        target = cloning.destination(body.get("folder"), body.get("name") or name,
                                     default_parent=default_clone_folder(self.home), home=self.home)
        with self._lock:
            if self._closed:
                raise APIError(GONE, 503, "unavailable")
            going = [c for c in self._clones.values() if c.state == "cloning"]
            if len(going) >= MAX_CLONES:
                raise APIError(f"Mobster clones at most {MAX_CLONES} repositories at once. Wait for one to finish.",
                               409, "busy")
            if any(c.target == target for c in going):
                raise APIError(f"{folder.shown(target, self.home)} is being cloned already.", 409, "exists",
                               details={"path": str(target)})
            options = {} if self._popen is None else {"popen": self._popen}
            # A github.com repository is cloned with the GitHub connector's sign-in when there is one, so a private
            # repository clones too (github.clone_env: the token goes to github.com only, through git's environment).
            token = github.token_of(self.runtime) if host == "github.com" and url.startswith("https://") else None
            if token:
                options.update(env=github.clone_env(token), signed_in="GitHub")
            job = cloning.Clone(uuid.uuid4().hex[:12], url, host, target, on_change=self._clone_changed,
                                on_done=self._cloned, clock=self.clock, home=self.home, **options)
            token = None  # noqa: F841 -- the sign-in's only other reference goes with the clone
            self._clones[job.id] = job
            self._trim()
            try:
                job.start()
            except (RuntimeError, OSError):
                self._clones.pop(job.id, None)
                raise
        return job.public()

    def clones(self):
        with self._lock:
            return [job.public() for job in sorted(self._clones.values(), key=lambda c: -c.started_at)]

    def clone_status(self, clone_id):
        return self._clone(clone_id).public()

    def cancel_clone(self, clone_id):
        """DELETE /api/projects/clones/{id}: stop a clone in flight; the folder it made is removed."""
        job = self._clone(clone_id)
        job.cancel()
        job.join(5)
        return job.public()

    # -- import from GitHub ----------------------------------------------------------------------------------------

    def github(self, refresh=False):
        """GET /api/projects/github: {signedIn, repos}: the repositories the GitHub connector's sign-in can open, the
        one pushed to last first, each with ``projectId`` when it is a project here already. Read again after
        GITHUB_KEEP seconds, or at once with ``refresh``."""
        token = github.token_of(self.runtime)
        if not token:
            return {"signedIn": False, "repos": []}
        key = hashlib.sha256(token.encode()).hexdigest()
        cached = self._github
        token_kept = cached if cached and cached[0] == key else None
        if token_kept is None or refresh or self.clock() - token_kept[1] > GITHUB_KEEP:
            repos = github.repositories(token, **({"opener": self._opener} if self._opener else {}))
            self._github = (key, self.clock(), repos)
        else:
            repos = token_kept[2]
        token = None  # noqa: F841
        known = {}
        for record in self.store.projects():
            found = folder.git(record["path"]) if folder.state(record["path"], self.home)[0] == "ready" else None
            if found and found.get("repo"):
                known.setdefault(found["repo"].lower(), record["id"])
        return {"signedIn": True, "repos": [{**repo, "projectId": known.get(repo["repo"].lower())}
                                            for repo in repos]}

    def pulls(self, project_id):
        """GET /api/projects/{id}/pulls: {signedIn, repo, pulls: {branch: {number, title, url, state}}}: the GitHub
        repository's pull requests by branch, for the project page's runs by branch. Empty for a folder that isn't a
        GitHub repository; read again after GITHUB_KEEP seconds."""
        record = self._record(project_id)
        found = folder.git(record["path"]) if folder.state(record["path"], self.home)[0] == "ready" else None
        repo = found.get("repo") if found else None
        token = github.token_of(self.runtime)
        if not repo or not token:
            return {"signedIn": bool(token), "repo": repo, "pulls": {}}
        key = (hashlib.sha256(token.encode()).hexdigest(), repo.lower())
        kept = self._pulls.get(key)
        if kept is None or self.clock() - kept[0] > GITHUB_KEEP:
            kept = (self.clock(), github.pulls(token, repo, **({"opener": self._opener} if self._opener else {})))
            self._pulls = {k: v for k, v in self._pulls.items() if self.clock() - v[0] <= GITHUB_KEEP}
            self._pulls[key] = kept
        token = None  # noqa: F841
        return {"signedIn": True, "repo": repo, "pulls": kept[1]}

    def _clone(self, clone_id):
        with self._lock:
            job = self._clones.get(clone_id)
        if job is None:
            raise APIError("Mobster has no clone with that id.", 404, "not_found")
        return job

    def _cloned(self, job):
        """git is done: the folder becomes a project. When that's refused the clone stays where it is, and the
        sentence says so."""
        try:
            project, _ = self.add({"path": str(job.target)}, source={"kind": "clone", "url": folder.clean_url(job.url)})
            job.project_id = project["id"]
        except Exception as error:  # noqa: BLE001 -- APIError, ValueError, a closed journal: the clone stays
            job.code = getattr(error, "code", None) or "refused"
            job.error = (f"The repository is cloned at {folder.shown(job.target, self.home)}, but Mobster couldn't add "
                         f"it: {error}")

    def _clone_changed(self, job):
        ended = job.state != "cloning"
        self._publish("clone_ended" if ended else "clone_changed", clone=job.public())

    def _trim(self):
        ended = sorted((c for c in self._clones.values() if c.state != "cloning"), key=lambda c: c.ended_at or 0)
        for job in ended[:max(0, len(ended) - KEEP_ENDED)]:
            self._clones.pop(job.id, None)

    def _publish(self, event, **fields):
        publish = getattr(self.runtime, "publish", None)
        if callable(publish):
            publish("projects", {"event": event, **fields})

    def begin_close(self):
        """Stop owned work before Runtime tears down its simulator managers."""
        with self._lock:
            already_closed = self._closed
            self._closed = True
            going = [c for c in self._clones.values() if c.state == "cloning"]
            running = [run for run in self._runs.values() if run.going]
        saved = True
        if not already_closed and self.sync is not None:
            try:
                self.sync.close()
            except Exception:  # noqa: BLE001 -- a sync failure must not leave check workers dispatching
                from .. import harness_api
                harness_api.log.exception("projects: could not close workflow sync")
                saved = False
        for job in going:
            try:
                job.cancel()
            except Exception:  # noqa: BLE001 -- stop every owned worker before reporting a failed close
                from .. import harness_api
                harness_api.log.exception("projects: could not stop a clone during close")
                saved = False
        # Signal every worker even when one journal write fails; a later close retries the saved stop intent.
        for run in running:
            try:
                run.stop()
            except Exception:  # noqa: BLE001 -- one abort failure must not leave another worker dispatching
                from .. import harness_api
                harness_api.log.exception("projects: could not stop a check run during close")
                saved = False
        for run in running:
            try:
                self._save_going(run)
            except Exception:  # noqa: BLE001 -- preserve journal/leases; the next close can retry
                from .. import harness_api
                harness_api.log.exception("projects: could not save a check run during close")
                saved = False
        return saved

    def join(self, timeout):
        deadline = time.monotonic() + max(0, timeout)
        with self._lock:
            jobs = list(self._clones.values()) + list(self._runs.values())
        for job in jobs:
            job.join(max(0, deadline - time.monotonic()))
        return not any(job._thread.is_alive() for job in jobs)

    def close(self):
        """Idempotent late service cleanup after the early Runtime project-worker barrier."""
        self.begin_close()
        self.join(2)
        self._memory_work.shutdown(wait=True, cancel_futures=True)


def _data_dir():
    from ..paths import user_data_dir
    try:
        return user_data_dir()
    except OSError:
        return None


def _last_runs():
    from ..testkit.recent import by_root
    try:
        return by_root()
    except Exception:  # noqa: BLE001 -- the list stands without its last runs
        return {}
