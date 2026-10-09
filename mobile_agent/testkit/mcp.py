"""`mobster mcp`'s testing tools (seam S10 provider ``testkit``).

- ``run_tests {path?, sims?, parallel?, retries?, tags?}`` runs the project's saved checks on simulators in the
  background and answers within the call's budget: the summary and the reports when the suite ends in time, else
  ``{status: "running", suite_id}``. ``run_tests {suite_id}`` waits for that suite again. Core ``wait`` takes
  verify's runs only (mcp_server/tools.py), so a suite is followed through this tool. Simulators only: a real
  iPhone is reached by ``mobster test --device`` in Terminal, never from an agent.
- ``record_check {run_id, name?}`` turns a finished Mobster task into a check file's YAML (record.py). It writes
  nothing; the agent saves the text where the user wants it.
"""

import threading
import time
from pathlib import Path

PATTERN_SUITE = r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$"
WAIT_DEFAULT, WAIT_MAX = 30.0, 38.0
KEEP_SUITES = 10
LOCAL = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}
READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}


class TestTools:
    name = "testkit"
    __test__ = False  # not a unittest class, whatever its name

    def __init__(self, *, execute=None, plan=None, clock=time.monotonic):
        self._execute, self._plan = execute, plan
        self.clock = clock
        self.suites = {}          # suite id -> SuiteJob
        self._lock = threading.Lock()

    # -- the provider protocol ---------------------------------------------------------------------------------

    def definitions(self, toolset):
        return [
            {"name": "run_tests",
             "description": "Run the project's saved checks (.mobster/checks) on simulators Mobster manages, with "
                            "retries, and write JUnit, HTML and JSON reports. Returns the summary when the suite "
                            "ends within the call; otherwise its suite_id: call run_tests with suite_id to wait for "
                            "it. Checks with steps need Smart (the user's key).",
             "inputSchema": {"type": "object", "properties": {
                 "path": {"type": "string", "minLength": 1, "maxLength": 4096,
                          "description": "absolute path of a check file or a folder of checks; default the "
                                         "project's .mobster/checks"},
                 "sims": {"type": "array", "maxItems": 8, "items": {"type": "string", "minLength": 1,
                                                                    "maxLength": 128},
                          "description": "simulators to run on, each a device type with an optional runtime, such "
                                         "as \"iPhone 17 Pro@iOS 26.4\"; default each check's own"},
                 "parallel": {"type": "integer", "minimum": 1, "maximum": 4,
                              "description": "simulators running checks at once (default 1)"},
                 "retries": {"type": "integer", "minimum": 0, "maximum": 3,
                             "description": "attempts after a failure (default 1)"},
                 "tags": {"type": "array", "maxItems": 10, "items": {"type": "string", "minLength": 1,
                                                                     "maxLength": 40},
                          "description": "run only the checks with any of these tags"},
                 "suite_id": {"type": "string", "pattern": PATTERN_SUITE,
                              "description": "a suite run_tests started: wait for it instead of starting one"},
                 "timeout_s": {"type": "number", "minimum": 1, "maximum": WAIT_MAX,
                               "description": f"how long to wait for the suite (default {WAIT_DEFAULT:g})"}},
                 "additionalProperties": False}},
            {"name": "record_check",
             "description": "Turn a finished Mobster task (from the Mobster app or `mobster serve`) into a check "
                            "file: its steps, and what it proved as expectations. Returns the YAML; save it in the "
                            "project's .mobster/checks to run it with run_tests.",
             "inputSchema": {"type": "object", "properties": {
                 "run_id": {"type": "string", "pattern": "^[a-f0-9]{12}$",
                            "description": "the task's id, as the Mobster app and `mobster history` show it"},
                 "name": {"type": "string", "minLength": 1, "maxLength": 120,
                          "description": "the check's name (default the task's request)"}},
                 "required": ["run_id"], "additionalProperties": False}},
        ]

    def annotations(self):
        return {"run_tests": dict(LOCAL), "record_check": dict(READ_ONLY)}

    def instructions(self):
        return ("run_tests runs the project's saved checks on simulators and reports them (JUnit, HTML); "
                "record_check turns a finished Mobster task into a check.")

    def call(self, name, arguments, call, toolset):
        if name == "run_tests":
            return self.run_tests(arguments, call, toolset)
        if name == "record_check":
            return self.record_check(arguments)
        from ..mcp_server.tools import ToolError
        raise ToolError(f"Mobster has no tool named {name}.")

    # -- run_tests ---------------------------------------------------------------------------------------------

    def run_tests(self, args, call, toolset):
        from ..mcp_server.tools import ToolError
        # Every wait ends by WAIT_MAX from the request's arrival, inside the ToolSet's 44 s budget for the call.
        started = getattr(call, "started", None)
        started = started if isinstance(started, (int, float)) and not isinstance(started, bool) else time.monotonic()
        deadline = started + min(float(args.get("timeout_s", WAIT_DEFAULT)), WAIT_MAX)

        def left():
            return max(0.0, deadline - time.monotonic())
        if args.get("suite_id"):
            if set(args) - {"suite_id", "timeout_s"}:
                raise ToolError("Pass suite_id alone to wait for a suite, or leave it out to start one.")
            job = self.suites.get(args["suite_id"])
            if job is None:
                raise ToolError(f"This server has no suite {args['suite_id']}. Start one with run_tests.")
            job.wait(left(), getattr(call, "stop", None))
            return job.result()
        with self._lock:
            running = next((job for job in self.suites.values() if not job.finished.is_set()), None)
            if running is not None:
                raise ToolError(f"Suite {running.suite_id or 'starting'} is still running. Call run_tests with "
                                f"suite_id {running.suite_id} to wait for it.")
            job = SuiteJob(self._options(args, toolset), toolset, execute=self._execute, plan=self._plan)
            job.start()
            if not job.ready.wait(min(10.0, left())):
                raise ToolError("The suite didn't start within 10 seconds. Call status, then try again.")
            if job.error is not None and job.suite_id is None:
                raise ToolError(job.error)
            self.suites[job.suite_id] = job
            for old in [key for key, item in self.suites.items() if item.finished.is_set()][:-KEEP_SUITES]:
                self.suites.pop(old, None)
        job.wait(left(), getattr(call, "stop", None))
        return job.result()

    def _options(self, args, toolset):
        from ..mcp_server.tools import ToolError
        from .api import Options
        path = args.get("path")
        if path is not None and not Path(path).is_absolute():
            raise ToolError("path must be absolute, because MCP clients start servers in different folders.")
        if path is None:
            root = Path(toolset.runs_dir).resolve().parent.parent  # <project>/.mobster/runs
            path = str(root / ".mobster" / "checks")
            if not Path(path).is_dir():
                raise ToolError("Pass path: the absolute path of the project's .mobster/checks folder or a check "
                                "file.")
        return Options(paths=(path,), sims=tuple(args.get("sims") or ()), parallel=int(args.get("parallel", 1)),
                       retries=args.get("retries"), tags=tuple(args.get("tags") or ()),
                       keyless=bool(getattr(toolset, "keyless", False)),
                       command="run_tests (MCP)")

    # -- record_check ------------------------------------------------------------------------------------------

    def record_check(self, args):
        from ..mcp_server.protocol import ToolResult
        from ..mcp_server.tools import ToolError
        from .record import RecordError, check_data, check_yaml, find_run
        try:
            record = find_run(args["run_id"])
            data = check_data(record, name=args.get("name"))
            text = check_yaml(record, name=args.get("name"))
        except RecordError as error:
            raise ToolError(error.message) from None
        steps = len(data.get("steps") or ())
        return ToolResult(f"A check from task {args['run_id']}: {steps} step{'' if steps == 1 else 's'} and "
                          f"{len(data['expect'])} expectation{'' if len(data['expect']) == 1 else 's'}. Save it as "
                          f".mobster/checks/<name>.yaml in the project.\n\n{text}",
                          {"yaml": text, "name": data["name"], "app": data["app"]["bundle"], "steps": steps,
                           "expect": len(data["expect"])})


class SuiteJob:
    """One suite running on a background thread for the MCP tools."""

    def __init__(self, options, toolset, *, execute=None, plan=None):
        self.options, self.toolset = options, toolset
        self._execute, self._plan = execute, plan
        self.suite = None
        self.suite_id = None
        self.doc = self.paths = None
        self.error = None
        self.lines = []
        self.ready = threading.Event()      # the suite id exists (or the plan failed)
        self.finished = threading.Event()
        self.stop = threading.Event()

    def start(self):
        threading.Thread(target=self._work, daemon=True, name="mobster-mcp-tests").start()

    def _progress(self, line):
        self.lines.append(str(line).splitlines()[0][:300])
        del self.lines[:-20]

    def _work(self):
        from .api import UsageError, execute, plan
        try:
            planned = (self._plan or plan)(self.options)
            from .reports import new_suite_id
            self.suite_id = new_suite_id()
            self.ready.set()
            key = None if self.options.keyless else getattr(self.toolset, "_key", None)
            self.doc, self.paths = (self._execute or execute)(planned, progress=self._progress, stop=self.stop,
                                                               key=key, suite_id=self.suite_id,
                                                               on_suite=self._on_suite)
        except UsageError as error:
            self.error = str(error)
        except Exception as error:  # the job's own failure: said once, never a traceback to the agent
            self.error = f"The suite hit an unexpected error ({type(error).__name__})."
        finally:
            self.ready.set()
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

    def result(self):
        from ..mcp_server.protocol import ToolResult
        from ..mcp_server.tools import ToolError
        if self.error is not None:
            raise ToolError(self.error)
        if not self.finished.is_set():
            done = self.suite.done if self.suite is not None else 0
            total = self.suite.total if self.suite is not None else None
            latest = self.lines[-1] if self.lines else "Preparing the simulators."
            count = f"{done} of {total} runs done" if total else "starting"
            return ToolResult(f"Suite {self.suite_id} is running ({count}): {latest} Call run_tests with suite_id "
                              f"{self.suite_id} to wait for it.",
                              {"status": "running", "suite_id": self.suite_id, "done": done, "total": total,
                               "message": latest})
        doc, paths = self.doc, self.paths
        summary = doc.get("summary") or {}
        lines = [f"Suite {self.suite_id}: {_counts(summary)} (exit code {doc.get('exitCode')})."]
        for check in doc.get("checks") or ():
            for result in check.get("results") or ():
                if result.get("status") != "passed" or result.get("flaky"):
                    reason = (result.get("reason") or result.get("flakyReason") or {}).get("message") or ""
                    word = "flaky" if result.get("status") == "passed" else result.get("status")
                    lines.append(f"- {check.get('name')} on {result.get('device')}: {word}"
                                 + (f". {reason}" if reason else ""))
        lines.append(f"Report: {paths.get('html')}")
        lines.append(f"JUnit: {paths.get('junit')}")
        structured = {"status": "finished", "suite_id": self.suite_id, "exit_code": doc.get("exitCode"),
                      "summary": summary, "report": str(paths.get("html")), "junit": str(paths.get("junit")),
                      "results": str(paths.get("results"))}
        return ToolResult("\n".join(lines[:40]), structured)


def _counts(summary):
    from .reports import _counts as counts
    return counts(summary)
