"""Run checks from a project's page: build the app from the folder as it is, then run its checks, into the project's
own `.mobster/test-results` (where its page reads them).

It is `mobster test` (testkit/api.py: plan, then execute) with the build `mobster test pr` makes first
(testkit/prrun.py: the Xcode project's scheme, or an Expo app's Release build, into `.mobster/build`), so a check of
the app runs the build Mobster just made. By default it runs every check on the project's `.mobster/matrix.yaml`,
else each check's own simulator, and no phone is touched. A ``Choice`` narrows it: some simulators or one iPhone the
person picked (the service checks the plan and that no task holds it), the checks with some tags, or the project's
deep links as `mobster links check` checks them.

A run says how far it got (``state``: building, running, then done, failed or stopped) and is never started again by
itself. Stop ends the build's whole process tree, or stops the suite between steps.
"""

import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

MAX_RUNS = 2                                          # project check runs at once, local and CI
REQUEST_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")   # a durable start's request key


@dataclass(frozen=True)
class Choice:
    """What a run covers: ``sims`` (device types) or ``device`` (an iPhone's name), and ``tags`` or ``links``."""
    sims: tuple = ()
    device: str = None
    tags: tuple = ()
    links: bool = False

    def public(self):
        return {"sims": list(self.sims), "device": self.device, "tags": list(self.tags), "links": self.links}


class CheckRun:
    """One run of a project's checks on a background thread. ``on_change(run)`` is told as it goes (at most 4 times a
    second) and once when it ends. ``plan``, ``build``, ``execute`` and ``popen`` are testkit's and subprocess's own
    (tests pass fakes)."""

    def __init__(self, identifier, project_id, root, *, mode="auto", request_id=None, choice=Choice(), on_change=None,
                 prepared=None, before_attempt=None, owner=None, grant_id=None, revision=None,
                 clock=time.time, home=None, plan=None, build=None, execute=None, popen=subprocess.Popen):
        from ..testkit.reports import new_suite_id
        self.id, self.project_id, self.root, self.choice = identifier, project_id, Path(root), choice
        self.mode, self.request_id = mode, request_id
        self.owner, self.grant_id, self.revision = owner or "local:" + project_id, grant_id, revision
        self.state, self.line, self.done, self.total = "building", "Reading the checks", 0, None
        self.targets, self.summary, self.exit_code = [], None, None
        self.code = self.error = self.fix = self.log = None
        self.suite_id = new_suite_id()
        self.started_at, self.ended_at = clock(), None
        self._on_change, self._clock, self._home = on_change, clock, home
        self._plan, self._build, self._execute, self._popen = plan, build, execute, popen
        self._prepared, self._before_attempt = prepared, before_attempt
        self.doc = None
        self._stop = threading.Event()
        self._suite = None
        self._started = False
        self._told = 0.0
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._work, name=f"mobster-project-run-{identifier}", daemon=True)

    def public(self):
        suite = self._suite
        done = suite.done if suite is not None else self.done
        total = suite.total if suite is not None else self.total
        return {"id": self.id, "projectId": self.project_id, "requestId": self.request_id,
                "mode": self.mode, "stopRequested": self._stop.is_set(),
                "state": self.state, "line": self.line, "done": done,
                "total": total, "targets": list(self.targets), "suite": self.suite_id, "summary": self.summary,
                "exitCode": self.exit_code, "code": self.code, "error": self.error, "fix": self.fix, "log": self.log,
                "choice": self.choice.public(),
                "startedAt": round(self.started_at * 1000),
                "endedAt": round(self.ended_at * 1000) if self.ended_at else None}

    @property
    def going(self):
        return self.state in ("building", "running")

    def start(self):
        self._thread.start()
        self._started = True

    def join(self, timeout=None):
        if self._started:
            self._thread.join(timeout)

    def stop(self):
        """Ask the suite's own seal/cancel gate whether Stop still owns its final verdict."""
        with self._lock:
            if not self.going:
                return False
            suite = self._suite
            if suite is None:
                self._stop.set()
                return True
        return bool(suite.cancel())

    # -- the work --------------------------------------------------------------------------------------------------

    def _work(self):
        from ..testkit import api, prrun
        from ..testkit.prbuild import BuildError
        from ..verify.runs import ensure_gitignore
        choice = self.choice
        try:
            # No command: the report shows a shell command only for a run from one.
            options = api.Options(fresh=self.mode == "record", no_heal=self.mode == "strict",
                                  retries=0 if self.mode in ("record", "strict") else None,
                                  sims=choice.sims, devices=(choice.device,) if choice.device else (), tags=choice.tags)
            if self._prepared is not None:
                planned = self._prepared.planned
            elif choice.links:
                planned = self._links_plan(options)
            else:
                planned = (self._plan or api.plan)(options, cwd=self.root)
            if planned is None:
                return None
            if not planned.entries:
                return self._fail("no_checks", "This project has no checks to run.",
                                  "Set up checks on the project's page, or add one with mobster test add.")
            self.targets = [_where(target) for target in planned.targets]
            self.total = len(planned.entries) * len(planned.targets)
            if self._prepared is None:
                ensure_gitignore(self.root / ".mobster" / "runs")  # the build goes in .mobster/build, kept out of git
                self._say("Building the app")
                bundle = next((entry.check.bundle_id for entry in planned.entries
                               if entry.check is not None and entry.check.bundle_id), None)
                built = (self._build or prrun.build_app)(self.root, prrun.PrOptions(), options, runner=self._runner,
                                                         progress=self._say, bundle=bundle)
                if self._stop.is_set():
                    return self._end("stopped")
                if built is not None:
                    self.log = self._relative(built.get("log"))
                    planned.entries = prrun.with_build(planned.entries, built, set())
            with self._lock:
                self.state = "running"
            self._say(f"Running {len(planned.entries)} check{'' if len(planned.entries) == 1 else 's'}")
            kwargs = {"progress": self._say, "stop": self._stop, "suite_id": self.suite_id,
                      "on_suite": self._on_suite, "cwd": self.root}
            if self._before_attempt is not None:
                kwargs["before_attempt"] = self._before_attempt
            doc, _ = (self._execute or api.execute)(planned, **kwargs)
            self.doc = doc
            self.summary, self.exit_code = doc.get("summary") or {}, doc.get("exitCode")
            from ..testkit.suite import SAFETY_EXIT_STOPS
            if doc.get("stopReason") in SAFETY_EXIT_STOPS:
                self._fail(doc["stopReason"], "The check run stopped for safety; review its report.",
                           "Reconcile the device or journal before starting another request.")
            else:
                self._end("stopped" if doc.get("stopped") else "done")
        except api.UsageError as error:
            self._fail("usage", str(error), None)
        except BuildError as error:
            if self._stop.is_set():
                return self._end("stopped")
            self.log = self.log or self._relative(self.root / ".mobster" / "build" / "pr-build.log")
            # prbuild's fix is written for `mobster test pr` (pass --no-build); here the page's own words say it.
            fix = error.fix if error.fix and "--no-build" not in error.fix else \
                "Fix the build, then run the checks again."
            self._fail("build_failed", error.message, fix)
        except FileExistsError:
            self._fail("suite_collision", "This check run's report folder already exists.",
                       "Review the existing report; Mobster will not run this request again.")
        except Exception as error:  # noqa: BLE001 -- the run's own failure: said once, never retried
            from .. import harness_api
            harness_api.log.exception("projects: a check run broke")
            self._fail("failed", f"The run hit an unexpected error ({type(error).__name__}).",
                       "Run the checks again, or run mobster test in the project's folder to see more.")

    def _links_plan(self, options):
        """The project's deep links as checks (`mobster links check`), or None after failing the run."""
        from ..devkit import links
        from ..testkit.targets import TargetError
        try:
            chosen, _ = links.checkable(links.discover(self.root))
            if not chosen:
                self._fail("no_links", "This project has no deep links to check.",
                           "Add them to .mobster/links.yaml, with the title each screen shows.")
                return None
            return links.plan_check(self.root, chosen, options)
        except (links.LinksError, TargetError) as error:
            self._fail("usage", str(error), None)
            return None

    def _on_suite(self, suite):
        with self._lock:
            self._suite = suite
            stopped = self._stop.is_set()
        if stopped and hasattr(suite, "cancel"):
            suite.cancel()
        self._tell(force=True)

    def _say(self, line):
        text = str(line).strip().splitlines()[0][:300] if str(line).strip() else ""
        if text:
            self.line = text
            self._tell()

    def _relative(self, path):
        if not path:
            return None
        try:
            return Path(path).resolve().relative_to(self.root.resolve()).as_posix()
        except (ValueError, OSError):
            return None

    def _runner(self, argv, cwd, log=None, timeout=None):
        """testkit/prbuild's runner, which Stop can end: (exit code, output). xcodebuild and what it starts run in a
        process group of their own; Stop or the timeout ends the group."""
        from ..testkit.prbuild import BUILD_TIMEOUT
        timeout = timeout or BUILD_TIMEOUT
        if self._stop.is_set():
            return 130, "Stopped."
        if log is not None:
            Path(log).parent.mkdir(parents=True, exist_ok=True)
        try:
            # w+b: the log is read back for its last 200 KB, which prbuild searches for the build's errors.
            with (open(log, "w+b") if log is not None else tempfile.TemporaryFile()) as output:
                try:
                    process = self._popen(argv, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=output,
                                          stderr=subprocess.STDOUT, start_new_session=True)
                except FileNotFoundError:
                    return 127, f"{argv[0]} isn't installed."
                deadline = time.monotonic() + timeout
                while process.poll() is None:
                    if self._stop.wait(0.2) or time.monotonic() > deadline:
                        _end_group(process)
                        break
                code = process.wait()
                output.flush()
                output.seek(0, os.SEEK_END)
                output.seek(max(0, output.tell() - 200_000))
                text = output.read(200_000).decode("utf-8", errors="replace")
        except OSError as error:
            return 1, f"Mobster couldn't start {argv[0]} ({error.strerror or type(error).__name__})."
        if self._stop.is_set():
            return 130, text
        if time.monotonic() > deadline:
            return 124, f"{argv[0]} didn't finish in {timeout // 60} minutes."
        return code, text

    # -- telling ---------------------------------------------------------------------------------------------------

    def _tell(self, force=False):
        now = self._clock()
        if self._on_change is not None and (force or now - self._told >= 0.25):
            self._told = now
            try:
                self._on_change(self)
            except Exception:  # noqa: BLE001 -- telling never stops a run
                if not self.going:
                    # A terminal row that did not commit is an unknown outcome, not a green durable run.
                    self.state, self.exit_code, self.code = "interrupted", 3, "journal_unavailable"
                    self.error = "Mobster could not save the check run's final status. Reconcile its report."

    def _fail(self, code, sentence, fix):
        self.code, self.error, self.fix = code, sentence, fix
        self._end("failed")

    def _end(self, state):
        with self._lock:
            self.state, self.ended_at = state, self._clock()
        if state == "stopped":
            self.line = "Stopped"
        self._tell(force=True)


def _where(target):
    """Where a run goes, as a sentence reads it: "iPhone 17 Pro, iOS 26.4", or "each check's simulator"."""
    return "each check's simulator" if target.kind == "default" else target.label.replace(" · ", ", ")


def _end_group(process):
    """TERM to the owned group, wait up to 3 seconds for its leader, then KILL surviving children."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, AttributeError):
        try:
            process.terminate()
        except OSError:
            return
    try:
        process.wait(3)
    except subprocess.TimeoutExpired:
        pass
    # The leader can exit before children that ignore TERM; reap the whole owned group.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        pass
