"""A suite on a background thread, for callers that answer before it ends: `mobster mcp`'s run_tests and the
editor's routes on `mobster serve` (editor.py). Each server keeps its own `Suites`: one runs at a time."""

import threading
import time
from pathlib import Path

from .reports import counts_text, new_suite_id

PATTERN_SUITE = r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$"
KEEP_SUITES = 10
MAX_FAILED = 6  # failed expectations a result row carries; the report has every one


class Busy(Exception):
    """A suite is still running: ``job``."""

    def __init__(self, job):
        super().__init__(job.suite_id)
        self.job = job


class Suites:
    """The suites one server started: one runs at a time, and the last KEEP_SUITES finished stay readable."""

    def __init__(self):
        self.jobs = {}  # suite id -> SuiteJob, oldest first
        self._lock = threading.Lock()

    def get(self, suite_id):
        return self.jobs.get(suite_id)

    def start(self, make):
        """The job ``make()`` builds, started; Busy while another runs."""
        with self._lock:
            running = next((job for job in self.jobs.values() if not job.finished.is_set()), None)
            if running is not None:
                raise Busy(running)
            job = make()
            self.jobs[job.suite_id] = job
            job.start()
            for old in [key for key, item in self.jobs.items() if item.finished.is_set()][:-KEEP_SUITES]:
                self.jobs.pop(old, None)
        return job

    def latest(self):
        return next(reversed(self.jobs.values()), None)

    def close(self):
        for job in list(self.jobs.values()):
            job.stop.set()


class SuiteJob:
    """One suite of saved checks on a background thread. ``root`` is the project, when the caller knows it; ``key`` is
    Smart's key for healing (None: the Mac's)."""

    kind = "checks"

    def __init__(self, options, *, root=None, key=None, execute=None, plan=None):
        self.options, self.root, self.key = options, root, key
        self._execute, self._plan = execute, plan
        self.suite = None
        self.suite_id = new_suite_id()
        self.doc = self.paths = None
        self.error = None
        self.lines = []
        self.finished = threading.Event()
        self.stop = threading.Event()

    def start(self):
        threading.Thread(target=self._work, daemon=True, name="mobster-suite").start()

    def _progress(self, line):
        self.lines.append(str(line).splitlines()[0][:300])
        del self.lines[:-20]

    def _work(self):
        from .api import UsageError, execute, plan
        try:
            planned = (self._plan or plan)(self.options)
            self.doc, self.paths = (self._execute or execute)(planned, progress=self._progress, stop=self.stop,
                                                               key=self.key, suite_id=self.suite_id,
                                                               on_suite=self._on_suite)
        except UsageError as error:
            self.error = str(error)
        except Exception as error:  # the job's own failure: said once, never a traceback to the agent
            self.error = f"The suite hit an unexpected error ({type(error).__name__})."
        finally:
            self.finished.set()

    def _on_suite(self, suite):
        self.suite = suite

    def wait(self, timeout, stop=None):
        end = time.monotonic() + max(0.0, timeout)
        while not self.finished.is_set():
            left = end - time.monotonic()
            if left <= 0 or (stop is not None and stop.is_set()):
                return False
            self.finished.wait(min(.5, left))
        return True

    def _counts(self):
        done = self.suite.done if self.suite is not None else 0
        total = self.suite.total if self.suite is not None else None
        return done, total, self.lines[-1] if self.lines else "Preparing the simulators."

    def public(self):
        """The suite as the editor's routes answer it: running with its progress, finished with each result, or the
        error that stopped it."""
        done, total, latest = self._counts()
        out = {"suite": self.suite_id, "kind": self.kind, "status": "running", "done": done, "total": total,
               "message": latest}
        if self.error is not None:
            return {**out, "status": "error", "message": self.error}
        if not self.finished.is_set():
            return out
        doc, paths = self.doc or {}, self.paths or {}
        folder = Path(paths["folder"]) if paths.get("folder") else None
        return {**out, "status": "finished", "message": counts_text(doc.get("summary") or {}),
                "exitCode": doc.get("exitCode"), "stopped": bool(doc.get("stopped")),
                "summary": doc.get("summary") or {}, "report": _text(paths.get("html")),
                "checks": [_check_row(check, folder) for check in doc.get("checks") or () if isinstance(check, dict)]}

    def result(self):
        from ..mcp_server.protocol import ToolResult
        from ..mcp_server.tools import ToolError
        if self.error is not None:
            raise ToolError(self.error)
        if not self.finished.is_set():
            done, total, latest = self._counts()
            count = f"{done} of {total} runs done" if total else "starting"
            return ToolResult(f"Suite {self.suite_id} is running ({count}): {latest} Call run_tests with suite_id "
                              f"{self.suite_id} to wait for it.",
                              {"status": "running", "suite_id": self.suite_id, "done": done, "total": total,
                               "message": latest})
        doc, paths = self.doc, self.paths
        summary = doc.get("summary") or {}
        lines = [f"Suite {self.suite_id}: {counts_text(summary)} (exit code {doc.get('exitCode')})."]
        for check in doc.get("checks") or ():
            for result in check.get("results") or ():
                if result.get("status") != "passed" or result.get("flaky"):
                    reason = (result.get("reason") or result.get("flakyReason") or {}).get("message") or ""
                    word = "flaky" if result.get("status") == "passed" else result.get("status")
                    lines.append(f"- {check.get('name')} on {result.get('device')}: {word}"
                                 + (f". {reason}" if reason else ""))
                    cause = (result.get("stepProof") or {}).get("cause")
                    if isinstance(cause, dict) and result.get("status") != "passed":
                        lines.append(f"  Cause ({cause.get('side')}): {cause.get('line')}.")
            for result in check.get("results") or ():
                for heal in result.get("healed") or ():
                    if isinstance(heal, dict):
                        lines.append(f"- {check.get('name')}: healed step {heal.get('step')}, {heal.get('from')} "
                                     f"→ {heal.get('to')} (review it with review_heals)")
        if summary.get("replayed"):
            lines.append(f"{summary['replayed']} check(s) replayed with no model.")
        lines.append(f"Report: {paths.get('html')} (each step's log lines, requests and clip)")
        lines.append(f"JUnit: {paths.get('junit')}")
        failures = [{"check": check.get("name"), "device": result.get("device"), "status": result.get("status"),
                     "cause": (result.get("stepProof") or {}).get("cause"),
                     "judged": (result.get("stepProof") or {}).get("judged"),
                     "step": (result.get("evidence") or {}).get("step")}
                    for check in doc.get("checks") or () for result in check.get("results") or ()
                    if result.get("status") != "passed"]
        structured = {"status": "finished", "suite_id": self.suite_id, "exit_code": doc.get("exitCode"),
                      "summary": summary, "failures": failures, "report": str(paths.get("html")),
                      "junit": str(paths.get("junit")), "results": str(paths.get("results"))}
        return ToolResult("\n".join(lines[:40]), structured)


class PrJob(SuiteJob):
    """A branch's suite: the diff, the build, the run and the verdict (prrun.test_pr), on a background thread."""

    kind = "changes"

    def __init__(self, pr_options, options, root, *, key=None, run=None):
        super().__init__(options, root=root, key=key)
        self.pr_options = pr_options
        self._run = run
        self.pr_result = None

    def _work(self):
        from .api import UsageError
        from .pr import PrError
        try:
            if self._run is None:
                from .prrun import test_pr as run
            else:
                run = self._run
            self.pr_result = run(self.pr_options, self.options, root=self.root, progress=self._progress,
                                 stop=self.stop, key=self.key, suite_id=self.suite_id, on_suite=self._on_suite)
            self.doc, self.paths = self.pr_result.doc, self.pr_result.paths
        except (UsageError, PrError) as error:
            self.error = str(error)
        except Exception as error:  # the job's own failure: said once, never a traceback to the agent
            self.error = f"run_tests pr hit an unexpected error ({type(error).__name__})."
        finally:
            self.finished.set()

    def public(self):
        out = super().public()
        if out["status"] != "finished":
            return out
        pr = self.pr_result.pr
        screens = [screen.get("name") for screen in (pr.get("plan") or {}).get("screens") or ()]
        return {**out, "message": pr.get("headline") or out["message"], "screens": screens[:10],
                "notes": list(pr.get("notes") or ())[:6]}

    def result(self):
        from ..mcp_server.protocol import ToolResult
        if self.error is not None or not self.finished.is_set():
            return super().result()
        pr, paths = self.pr_result.pr, self.pr_result.paths
        folder = Path(paths["folder"])
        lines = [f"{pr.get('headline')} (exit code {self.pr_result.exit_code})."]
        screens = [screen.get("name") for screen in (pr.get("plan") or {}).get("screens") or ()]
        if screens:
            lines.append("Changed screens: " + ", ".join(screens[:10]) + ".")
        for row in pr.get("rows") or ():
            word = "draft " if row.get("draft") else ""
            line = f"- {word}{row.get('check')} on {row.get('device')}: {row.get('status')}"
            if row.get("status") != "passed":
                line += f". {row.get('expected')}: {row.get('found')}"
            if row.get("frame"):
                line += f". Frame: {folder / row['frame']}"
            if row.get("gif"):
                line += f". GIF: {folder / row['gif']}"
            lines.append(line)
        drafts = (pr.get("plan") or {}).get("drafts") or []
        if drafts:
            lines.append("Drafts (move one into .mobster/checks/ to keep it): "
                         + ", ".join(item["file"] for item in drafts) + ".")
        lines += list(pr.get("notes") or ())
        lines.append(f"PR comment: {paths.get('comment')}")
        structured = {"status": "finished", "suite_id": self.suite_id, "exit_code": self.pr_result.exit_code,
                      "headline": pr.get("headline"), "counts": pr.get("counts"),
                      "rows": [{**row, "frame": str(folder / row["frame"]) if row.get("frame") else None,
                                "gif": str(folder / row["gif"]) if row.get("gif") else None}
                               for row in pr.get("rows") or ()],
                      "drafts": drafts, "comment": str(paths.get("comment")), "pr_json": str(paths.get("prJson")),
                      "report": str(folder / "index.html") if (folder / "index.html").is_file() else None}
        return ToolResult("\n".join(lines[:60]), structured, images=self._failed_frames(pr, folder))

    @staticmethod
    def _failed_frames(pr, folder, limit=2):
        """The verdict frames of the first failed checks, expectations outlined, as base64 JPEGs."""
        import base64
        out = []
        for row in pr.get("rows") or ():
            if len(out) >= limit:
                break
            if row.get("status") in ("failed", "needs_review") and not row.get("draft") and row.get("frame"):
                try:
                    out.append(base64.b64encode((folder / row["frame"]).read_bytes()).decode())
                except OSError:
                    continue
        return out


def _text(path):
    return str(path) if path else None


def _check_row(check, folder):
    """One check of a finished suite, for the editor: each device's verdict, why, the verdict frame and the heals."""
    def where(path):
        if not path:
            return None
        found = Path(path)
        return str(found if found.is_absolute() or folder is None else folder / found)
    results = []
    for result in check.get("results") or ():
        if not isinstance(result, dict):
            continue
        failed = [{"text": item.get("text"), "observed": item.get("observed")}
                  for item in result.get("assertions") or () if isinstance(item, dict) and not item.get("ok")]
        results.append({"device": result.get("device"), "status": result.get("status"),
                        "flaky": bool(result.get("flaky")), "replayed": bool(result.get("replayed")),
                        "reason": result.get("reason"), "frame": where(result.get("frame")),
                        "report": where(result.get("report")), "failed": failed[:MAX_FAILED],
                        "healed": [{key: heal.get(key) for key in ("step", "from", "to", "why")}
                                   for heal in result.get("healed") or () if isinstance(heal, dict)]})
    return {"name": check.get("name"), "file": check.get("file"), "quarantined": bool(check.get("quarantined")),
            "results": results}
