"""`mobster mcp`'s testing tools: simulators, or with pr a phone only when `mobster mcp --allow-device` names it."""

import json
import time
from pathlib import Path

from ..android import enabled as android_enabled
from .jobs import PATTERN_SUITE, Busy, PrJob, SuiteJob, Suites

WAIT_DEFAULT, WAIT_MAX = 30.0, 38.0
LOCAL = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}
PR_ONLY = frozenset({"base", "device", "build", "scheme", "testflight", "drafts",
                     "github"})  # run_tests arguments for pr
SUITE_ONLY = frozenset({"parallel", "retries", "tags", "fresh", "no_heal", "locales",
                        "a11y"})  # and the ones pr can't use


class TestTools:
    name = "testkit"
    __test__ = False  # not a unittest class, whatever its name

    def __init__(self, *, execute=None, plan=None, clock=time.monotonic, pr_run=None):
        self._execute, self._plan = execute, plan
        self._pr_run = pr_run
        self.clock = clock
        self.suites = Suites()

    def definitions(self, toolset):
        from .mcp_comparison import definition as compare_definition
        return [
            {"name": "run_tests",
             "description": "Run the project's saved checks on simulators; returns the summary and JUnit/HTML reports, "
                            "or a suite_id to wait on. With pr, test what this branch changed and answer with the "
                            "verdict, frames and GIFs.",
             "inputSchema": {"type": "object", "properties": {
                 "path": {"type": "string", "minLength": 1, "maxLength": 4096,
                          "description": "absolute path of a check file or a folder of checks; default the "
                                         "project's .mobster/checks. With pr: the project (the folder holding "
                                         ".mobster or the git repository), default the server's"},
                 "sims": {"type": "array", "maxItems": 8, "items": {"type": "string", "minLength": 1,
                                                                    "maxLength": 128},
                          "description": "simulators to run on, each a device type with an optional runtime, such "
                                         "as \"iPhone 17 Pro@iOS 26.4\"; default each check's own"},
                 **({"android": {"type": "string", "maxLength": 128,
                                 "description": "run on an Android emulator: an AVD name, or an empty string for any "
                                                "idle emulator (experimental)"}} if android_enabled() else {}),
                 "parallel": {"type": "integer", "minimum": 1, "maximum": 4,
                              "description": "simulators running checks at once (default 1)"},
                 "retries": {"type": "integer", "minimum": 0, "maximum": 3,
                             "description": "attempts after a failure (default 1)"},
                 "tags": {"type": "array", "maxItems": 10, "items": {"type": "string", "minLength": 1,
                                                                     "maxLength": 40},
                          "description": "run only the checks with any of these tags"},
                 "locales": {"type": "array", "maxItems": 12, "items": {"type": "string", "minLength": 2,
                                                                         "maxLength": 12},
                             "description": "run every check once in each of these languages, such as [\"de\", "
                                            "\"ja\", \"ar\"] (`mobster test --locale`): the app launches in the "
                                            "language and the screens are read for truncated text, untranslated "
                                            "strings and a right-to-left layout that didn't mirror"},
                 "a11y": {"type": "string", "enum": ["warn", "strict"],
                          "description": "audit the screens each check reads for accessibility (`mobster test "
                                         "--a11y`): warn reports findings and passes, strict fails the check"},
                 "fresh": {"type": "boolean",
                           "description": "run every step with Smart and record the path again, instead of "
                                          "replaying the recorded one with no model (default false)"},
                 "no_heal": {"type": "boolean",
                             "description": "fail a check whose screen changed instead of healing the step with "
                                            "Smart (strict CI; default false)"},
                 "pr": {"type": "boolean",
                        "description": "test what this branch changed (default false); base, device, build, scheme, "
                        "testflight, drafts and github go with it, and parallel, retries, tags, fresh "
                        "and no_heal don't"},
                 "base": {"type": "string", "minLength": 1, "maxLength": 200,
                          "description": "with pr: the branch the PR merges into (default origin/main)"},
                 "device": {"type": "string", "minLength": 1, "maxLength": 128,
                            "description": "with pr: run on this iPhone instead of simulators: a name from "
                                           "list_devices that `mobster mcp --allow-device` names"},
                 "build": {"type": "boolean", "description": "with pr: build the app first (default true)"},
                 "scheme": {"type": "string", "minLength": 1, "maxLength": 200,
                            "description": "with pr: the Xcode scheme to build (default the only one)"},
                 "testflight": {"type": "string", "minLength": 1, "maxLength": 200,
                                "description": "with pr and device: the bundle ID of the user's TestFlight build to "
                                               "test; only one `mobster mcp --allow-testflight` names"},
                 "drafts": {"type": "string", "enum": ["run", "write", "off"],
                            "description": "with pr: draft checks for uncovered screens and run them (default), only "
                                           "write them, or neither"},
                 "github": {"type": "string", "enum": ["check", "comment", "off"],
                            "description": "with pr: put the verdict on the pull request with Mobster Checks, the "
                                           "GitHub App: a check run (default), the check run and one comment, or "
                                           "nothing"},
                 "suite_id": {"type": "string", "pattern": PATTERN_SUITE,
                              "description": "a suite run_tests started: wait for it instead of starting one"},
                 "timeout_s": {"type": "number", "minimum": 1, "maximum": WAIT_MAX,
                               "description": f"how long to wait for the suite (default {WAIT_DEFAULT:g})"}},
                 "required": [], "additionalProperties": False}},
            {"name": "test_history",
             "description": "Read the project's test history: each check's last 20 results per device, pass rate "
                            "and flake score, flaky first.",
             "inputSchema": {"type": "object", "properties": {
                 "path": {"type": "string", "minLength": 1, "maxLength": 4096,
                          "description": "absolute path of the project (the folder holding .mobster); default the "
                                         "server's"},
                 "check": {"type": "string", "minLength": 1, "maxLength": 200,
                           "description": "only checks whose name or file contains this text"},
                 "flaky_only": {"type": "boolean", "description": "only checks the history calls flaky"},
                 "limit": {"type": "integer", "minimum": 1, "maximum": 200,
                           "description": "most rows to return (default 50)"}},
                 "required": [], "additionalProperties": False}},
            {"name": "import_maestro",
             "description": "Turn Maestro flows into checks in the project's .mobster/checks. A check that exists is "
                            "kept unless force; dry_run writes nothing; a flow Mobster can't run is refused.",
             "inputSchema": {"type": "object", "properties": {
                 "flows": {"type": "array", "minItems": 1, "maxItems": 20,
                           "items": {"type": "string", "minLength": 1, "maxLength": 4096},
                           "description": "absolute paths of Maestro flow files or folders of them"},
                 "path": {"type": "string", "minLength": 1, "maxLength": 4096,
                          "description": "absolute path of the project (the folder holding .mobster); default the "
                                         "server's"},
                 "bundle": {"type": "string", "minLength": 3, "maxLength": 155,
                            "description": "the app's bundle ID, when the flows don't carry an appId"},
                 "app": {"type": "string", "minLength": 1, "maxLength": 4096,
                         "description": "absolute path of the simulator build (.app) the checks launch"},
                 "skip_unsupported": {"type": "boolean",
                                      "description": "drop a command Mobster can't run instead of refusing the flow"},
                 "dry_run": {"type": "boolean", "description": "write nothing; say what would be written"},
                 "force": {"type": "boolean", "description": "replace a check that exists"}},
                 "required": ["flows"], "additionalProperties": False}},
            {"name": "test_schedule",
             "description": "list: the project's nightly run, when it runs and last night's verdict. run: run it "
                            "now on simulators; a schedule that names an iPhone is refused.",
             "inputSchema": {"type": "object", "properties": {
                 "action": {"type": "string", "enum": ["list", "run"], "description": "list (default) or run"},
                 "path": {"type": "string", "minLength": 1, "maxLength": 4096,
                          "description": "absolute path of the project (the folder holding .mobster); default the "
                                         "server's"},
                 "suite_id": {"type": "string", "pattern": PATTERN_SUITE,
                              "description": "with run: a suite this tool started, to wait for it again"},
                 "timeout_s": {"type": "number", "minimum": 1, "maximum": WAIT_MAX,
                               "description": f"with run: how long to wait (default {WAIT_DEFAULT:g})"}},
                 "required": [], "additionalProperties": False}},
            {"name": "repro_crash",
             "description": "Turn a crash (.ips, Sentry event JSON, TestFlight or Xcode crash log, or a brief) into a "
                            "check that reproduces it on a simulator, recorded. Run it after your fix: it passes "
                            "once the app stops crashing.",
             "inputSchema": {"type": "object", "properties": {
                 "ips_path": {"type": "string", "minLength": 1, "maxLength": 4096,
                              "description": "absolute path of a .ips crash report"},
                 "sentry_json": {"type": "string", "minLength": 2, "maxLength": 1000000,
                                 "description": "a Sentry event as JSON text, as its API or MCP returns it"},
                 "crash_log": {"type": "string", "minLength": 1, "maxLength": 1000000,
                               "description": "a TestFlight or Xcode Organizer crash log in Apple's text format"},
                 "brief": {"type": "object", "additionalProperties": False, "properties": {
                     "bundle": {"type": "string", "maxLength": 255}, "type": {"type": "string", "maxLength": 80},
                     "signal": {"type": "string", "maxLength": 20}, "message": {"type": "string", "maxLength": 300},
                     "version": {"type": "string", "maxLength": 40}, "device": {"type": "string", "maxLength": 60},
                     "os": {"type": "string", "maxLength": 60}, "link": {"type": "string", "maxLength": 2000},
                     "steps": {"type": "array", "maxItems": 20, "items": {"type": "string", "maxLength": 200}},
                     "frames": {"type": "array", "maxItems": 12, "items": {
                         "type": "object", "additionalProperties": False, "properties": {
                             "symbol": {"type": "string", "maxLength": 200}, "own": {"type": "boolean"},
                             "file": {"type": "string", "maxLength": 120}, "line": {"type": "integer"}}}}},
                           "description": "a crash from Crashlytics, Datadog or anywhere else: the app's bundle, "
                                          "exception type, frames (symbol, own), steps in plain words, and a link"},
                 "path": {"type": "string", "minLength": 1, "maxLength": 4096,
                          "description": "absolute path of the project (the folder holding .mobster); default the "
                                         "server's"},
                 "bundle": {"type": "string", "minLength": 1, "maxLength": 255,
                            "description": "the app's bundle ID when the crash doesn't name it"},
                 "app": {"type": "string", "minLength": 1, "maxLength": 4096,
                         "description": "absolute path of a .app built for the simulator, to install first"},
                 "launch_args": {"type": "array", "maxItems": 20, "items": {"type": "string", "minLength": 1,
                                                                            "maxLength": 200},
                                 "description": "launch arguments the crash needs"},
                 "link": {"type": "string", "minLength": 1, "maxLength": 2000,
                          "description": "a link that opens the app on the crashing screen"},
                 "sims": {"type": "array", "maxItems": 1, "items": {"type": "string", "minLength": 1,
                                                                    "maxLength": 128},
                          "description": "the simulator to use, such as \"iPhone 17 Pro@iOS 26.4\""},
                 "suite_id": {"type": "string", "pattern": PATTERN_SUITE,
                              "description": "wait for the reproduction a first call started; pass it alone"},
                 "timeout_s": {"type": "number", "minimum": 1, "maximum": WAIT_MAX,
                               "description": "seconds to wait for the answer (default 30)"}},
                 "required": [], "additionalProperties": False}},
            {"name": "review_heals",
             "description": "List the healed steps waiting for review (each with the step before and after, and "
                            "frames), or settle them: accept keeps them; reject restores the recorded steps. A heal "
                            "never changes a check's expectations.",
             "inputSchema": {"type": "object", "properties": {
                 "path": {"type": "string", "minLength": 1, "maxLength": 4096,
                          "description": "absolute path of the project's .mobster/checks folder or a check file; "
                                         "default the project's"},
                 "action": {"type": "string", "enum": ["list", "accept", "reject"],
                            "description": "list (default), accept or reject"},
                 "checks": {"type": "array", "maxItems": 50, "items": {"type": "string", "minLength": 1,
                                                                       "maxLength": 4096},
                            "description": "absolute paths of the check files to accept or reject; default every "
                                           "check with a heal waiting"}},
                 "required": [], "additionalProperties": False}},
        ] + [compare_definition()]

    def annotations(self):
        # run_tests with pr can post the verdict on the pull request (github_checks.py): it reaches GitHub.
        return {"run_tests": {**LOCAL, "openWorldHint": True}, "review_heals": dict(LOCAL),
                "test_history": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
                                 "openWorldHint": False},
                "import_maestro": dict(LOCAL), "test_schedule": dict(LOCAL), "repro_crash": dict(LOCAL),
                "compare_test_suites": {"readOnlyHint": True, "destructiveHint": False,
                                        "idempotentHint": True, "openWorldHint": False}}

    def instructions(self):
        # One sentence, at most 300 characters: ToolSet.instructions leaves out a longer one.
        return ("run_tests runs the project's saved checks on simulators (JUnit, HTML), replaying recorded steps with "
                "no model once a check passed, or with pr tests what the branch changed; repro_crash turns a crash "
                "report into a check that reproduces it.")

    def call(self, name, arguments, call, toolset):
        if name == "compare_test_suites":
            from .mcp_comparison import call as compare
            return compare(arguments, toolset)
        if name == "run_tests":
            return self.run_tests(arguments, call, toolset)
        if name == "repro_crash":
            return self.repro_crash(arguments, call, toolset)
        if name == "review_heals":
            return self.review_heals(arguments, toolset)
        if name == "test_history":
            return self.test_history(arguments, toolset)
        if name == "import_maestro":
            return self.import_maestro(arguments, toolset)
        if name == "test_schedule":
            return self.test_schedule(arguments, call, toolset)
        from ..mcp_server.tools import ToolError
        raise ToolError(f"Mobster has no tool named {name}.")

    def run_tests(self, args, call, toolset):
        from ..mcp_server.tools import ToolError
        pr = bool(args.get("pr"))
        stray = (PR_ONLY if not pr else SUITE_ONLY) & args.keys()
        if stray and not args.get("suite_id"):
            raise ToolError(f"{', '.join(sorted(stray))} {'go with pr: true' if not pr else 'go without pr'}.")
        key = self._key(toolset)
        return self._start_or_wait(args, call, toolset, (lambda: PrJob(
            *self._pr_options(args, toolset), key=key, run=self._pr_run)) if pr else (lambda: SuiteJob(
                self._options(args, toolset), key=key, execute=self._execute, plan=self._plan)))

    @staticmethod
    def _key(toolset):
        """Smart's key for the suite's heals: the server's, never with --keyless."""
        return None if getattr(toolset, "keyless", False) else getattr(toolset, "_key", None)

    def repro_crash(self, args, call, toolset):
        from ..mcp_server.tools import ToolError
        from . import repro
        if args.get("suite_id"):
            return self._start_or_wait(args, call, toolset, None)
        sources = [(key, kind) for key, kind in (("ips_path", "ips"), ("sentry_json", "sentry"),
                                                  ("crash_log", "text"), ("brief", "brief")) if args.get(key)]
        if len(sources) != 1:
            raise ToolError("Pass the crash as exactly one of ips_path, sentry_json, crash_log or brief.")
        root = self._project(args, toolset)
        key, kind = sources[0]
        if key == "ips_path" and not Path(args[key]).is_absolute():
            raise ToolError("ips_path must be absolute, because MCP clients start servers in different folders.")
        if args.get("app") and not Path(args["app"]).is_absolute():
            raise ToolError("app must be an absolute path.")
        try:
            content = Path(args[key]) if key == "ips_path" else args[key]
            if kind == "sentry":
                content = json.loads(content)
            brief = repro.from_content(kind, content)
            check, _, _ = repro.write(brief, root, bundle=args.get("bundle"), app=args.get("app"),
                                      args=tuple(args.get("launch_args") or ()), link=args.get("link"))
        except ValueError:
            raise ToolError("sentry_json isn't JSON. Pass the event's text.") from None
        except repro.ReproError as error:
            raise ToolError(str(error)) from None
        from .api import Options
        options = Options(paths=(str(check),), sims=tuple(args.get("sims") or ()), retries=0, video=True,
                          keyless=bool(getattr(toolset, "keyless", False)), command="repro_crash (MCP)")
        return self._start_or_wait(args, call, toolset, lambda: ReproJob(
            options, brief, root, check, key=self._key(toolset), execute=self._execute, plan=self._plan))

    def _start_or_wait(self, args, call, toolset, make):
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
        try:
            job = self.suites.start(make)
        except Busy as busy:
            raise ToolError(f"Suite {busy.job.suite_id} is still running. Call run_tests "
                            f"with suite_id {busy.job.suite_id} to wait for it.") from None
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
        from .api import UsageError, parse_locales
        try:
            locales = parse_locales(args.get("locales"))
        except UsageError as error:
            raise ToolError(str(error)) from None
        android = args.get("android")
        if android is not None:
            self._check_android(android, toolset)
        return Options(paths=(path,), sims=tuple(args.get("sims") or ()), android=android,
                       parallel=int(args.get("parallel", 1)),
                       retries=args.get("retries"), tags=tuple(args.get("tags") or ()),
                       keyless=bool(getattr(toolset, "keyless", False)),
                       fresh=bool(args.get("fresh")), no_heal=bool(args.get("no_heal")),
                       a11y=args.get("a11y"), locales=locales, command="run_tests (MCP)")

    @staticmethod
    def _check_android(name, toolset):
        """An Android phone (not an emulator) is driven only when the user's server command names it."""
        from ..android.records import records
        from ..mcp_server.tools import ToolError
        if not android_enabled():
            raise ToolError("Android is off: the user sets MOBSTER_EXPERIMENTAL_ANDROID=1 to turn it on.")
        for record in records():
            if name in (record["serial"], record["id"]) and not record["emulator"]:
                allowed = getattr(toolset, "allow_devices", None) or frozenset()
                if not {record["id"].casefold(), record["serial"].casefold()} & set(allowed):
                    raise ToolError(f"“{record['name']}” is an Android phone: run_tests drives it only when the user "
                                    "starts the server with `mobster mcp --allow-device` naming it.")

    def _project(self, args, toolset):
        from ..mcp_server.tools import ToolError
        path = args.get("path")
        if path is not None and not Path(path).is_absolute():
            raise ToolError("path must be absolute, because MCP clients start servers in different folders.")
        if path is None:
            path = Path(toolset.runs_dir).resolve().parent.parent  # <project>/.mobster/runs
        if not Path(path).is_dir():
            raise ToolError("Pass path: the absolute path of the project, the folder holding .mobster.")
        return Path(path)

    def _pr_options(self, args, toolset):
        from ..mcp_server.tools import ToolError
        from .api import Options
        from .prrun import PrOptions
        root = self._project(args, toolset)
        device, testflight = args.get("device"), args.get("testflight")
        if testflight and not device:
            raise ToolError("testflight goes with device: a TestFlight build is on an iPhone, not a simulator.")
        if device:
            from ..device_targets import named_device
            from ..devices import AmbiguousDevice
            try:
                record = named_device(device)
            except AmbiguousDevice as error:
                raise ToolError(str(error)) from None
            if record is None:
                raise ToolError(f"No device is named {device!r}. Call list_devices.")
            if record.get("kind") != "simulator":
                names = {str(record.get(key) or "").strip().casefold() for key in ("id", "udid", "name")}
                allowed = getattr(toolset, "allow_devices", None)
                if not allowed or not names & allowed:
                    raise ToolError(f"pr runs on “{record.get('name') or device}” only when the user starts "
                                    "the server with `mobster mcp --allow-device` naming it. Ask the user, or run "
                                    "on a simulator.")
        if testflight and testflight not in getattr(toolset, "allow_testflight", frozenset()):
            raise ToolError(f"Mobster tests the TestFlight build of {testflight} only when the user starts the server "
                            "with `mobster mcp --allow-testflight` naming it. Ask the user.")
        options = Options(sims=tuple(args.get("sims") or ()), devices=(device,) if device else (),
                          keyless=bool(getattr(toolset, "keyless", False)), command="run_tests pr (MCP)",
                          testflight=(testflight,) if testflight else ())
        pr_options = PrOptions(base=args.get("base"), build=bool(args.get("build", True)), scheme=args.get("scheme"),
                               drafts=args.get("drafts") or "run", github=args.get("github") or "check")
        return pr_options, options, root

    def test_history(self, args, toolset):
        from ..mcp_server.protocol import ToolResult
        from . import history
        root = self._project(args, toolset)
        found = history.report(root)
        needle = (args.get("check") or "").casefold()
        rows = [row for row in found["checks"]
                if (not needle or needle in str(row.get("name")).casefold()
                    or needle in str(row.get("file")).casefold())
                and (not args.get("flaky_only") or row.get("state") == "flaky")]
        limit = int(args.get("limit", 50))
        shown = rows[:limit]
        lines = [f"{len(rows)} check/device histories" + (f", showing {limit}" if len(rows) > limit else "") + "."
                 + ("" if found["ok"] else " The history file couldn't be read, so it starts again with the next run.")]
        for row in shown:
            lines.append(f"- {row.get('name')} on {row.get('device')}: {row.get('state')}, "
                         f"{row.get('passed')}/{row.get('runs')} passed")
        return ToolResult("\n".join(lines[:60]), {"ok": found["ok"], "total": len(rows), "checks": shown})

    def import_maestro(self, args, toolset):
        from ..mcp_server.protocol import ToolResult
        from ..mcp_server.tools import ToolError
        from . import maestro
        from .cli import _relative, _write_imports, import_text
        root = self._project(args, toolset)
        sources = list(args["flows"])
        if any(not Path(item).is_absolute() for item in sources):
            raise ToolError("flows must be absolute paths, because MCP clients start servers in different folders.")
        app = args.get("app")
        if app is not None and (not Path(app).is_absolute() or not str(app).endswith(".app")):
            raise ToolError("app must be the absolute path of a simulator build (.app).")
        bundle = args.get("bundle")
        if bundle:
            from ..state import validate_bundle_id
            try:
                validate_bundle_id(bundle)
            except ValueError:
                raise ToolError(f"bundle {bundle!r} is not a bundle ID such as com.example.app.") from None
        folder = root / ".mobster" / "checks"
        try:
            results = maestro.convert_tree([Path(item) for item in sources], bundle=bundle, app_path=app,
                                           skip_unsupported=bool(args.get("skip_unsupported")))
        except maestro.MaestroError as error:
            raise ToolError(str(error)) from None
        dry, force = bool(args.get("dry_run")), bool(args.get("force"))
        written, refused = _write_imports(results, folder, root, dry=dry, force=force)
        return ToolResult(import_text(results, folder, root, dry=dry),
                          {"flows": [item.to_dict() for item in results], "folder": _relative(folder, root),
                           "written": written, "refused": refused, "dry_run": dry})

    SCHEDULE_FLAGS = {"--sim": "sims", "--tag": "tags"}

    def test_schedule(self, args, call, toolset):
        from ..devkit import nightly
        from ..mcp_server.protocol import ToolResult
        from ..mcp_server.tools import ToolError
        action = args.get("action") or "list"
        if args.get("suite_id") and action != "run":
            raise ToolError("suite_id goes with run.")
        root = self._project(args, toolset)
        state = nightly.status(root)
        if action == "list":
            text = (f"The nightly run is on at {state['at']}"
                    + (f" ({' '.join(state['flags'])})" if state["flags"] else "")
                    + "." if state["on"] else "No nightly run is set up for this project. Turn it on: "
                    "mobster test nightly on --at 02:00")
            if state["last"]:
                text += f" Last run {state['last'].get('date')}: {state['last'].get('headline')}."
            return ToolResult(text, state)
        if args.get("suite_id"):
            return self._start_or_wait({k: v for k, v in args.items() if k in ("suite_id", "timeout_s")}, call, toolset,
                                       None)
        if not state["on"]:
            raise ToolError("No nightly run is set up for this project, so there is no schedule to run. Turn it on: "
                            "mobster test nightly on --at 02:00")
        phone = next((flag for flag in state["flags"] if flag in ("--device", "--testflight")), None)
        if phone:
            raise ToolError(f"This schedule names an iPhone ({phone}), and an agent never reaches a phone from here. "
                            "Run it yourself: mobster test nightly run")
        options = self._schedule_options(state["flags"], root, toolset)
        return self._start_or_wait({k: v for k, v in args.items() if k == "timeout_s"}, call, toolset,
                                   lambda: SuiteJob(options, root=root, key=self._key(toolset), execute=self._execute,
                                                    plan=self._plan))

    def _schedule_options(self, flags, root, toolset):
        from ..mcp_server.tools import ToolError
        from .api import Options
        sims, tags, parallel, retries, keyless, spread = [], [], 1, None, False, False
        it = iter(flags)
        for flag in it:
            if flag == "--sim":
                sims.append(next(it, ""))
            elif flag == "--tag":
                tags.append(next(it, ""))
            elif flag == "--parallel":
                parallel = int(next(it, "1") or 1)
            elif flag == "--retries":
                retries = int(next(it, "0") or 0)
            elif flag == "--keyless":
                keyless = True
            elif flag == "--spread":
                spread = True
            else:
                raise ToolError(f"The schedule has a flag ({flag}) this tool doesn't run. Run it yourself: "
                                "mobster test nightly run")
        return Options(paths=(str(root / ".mobster" / "checks"),), sims=tuple(sims), tags=tuple(tags),
                       parallel=min(parallel, 4), retries=retries, spread=spread,
                       keyless=keyless or bool(getattr(toolset, "keyless", False)),
                       command="test_schedule run (MCP)")

    def review_heals(self, args, toolset):
        from ..mcp_server.protocol import ToolResult
        from ..mcp_server.tools import ToolError
        from ..replay_core.heal import HealError
        from ..verify.checks import project_root
        from . import heal_review
        from .discover import DiscoveryError
        action = args.get("action") or "list"
        path = args.get("path")
        if path is not None and not Path(path).is_absolute():
            raise ToolError("path must be absolute, because MCP clients start servers in different folders.")
        root = project_root(Path(path)) if path else Path(toolset.runs_dir).resolve().parent.parent
        given = list(args.get("checks") or ())
        if any(not Path(item).is_absolute() for item in given):
            raise ToolError("checks must be absolute paths of check files.")
        if given and action == "list":
            raise ToolError("checks go with accept or reject; list shows every check's heals.")
        try:
            found = heal_review.pending(root, given or ([path] if path else []))
        except (DiscoveryError, HealError) as error:
            raise ToolError(str(error)) from None
        if action == "list":
            items = [{key: row[key] for key in ("check", "diff", "heals")} for row in heal_review.rows(found)]
            lines = [f"{sum(len(item['heals']) for item in items)} healed step(s) wait for review."]
            for item in items:
                for heal in item["heals"]:
                    lines.append(f"- {Path(item['check']).name} step {heal['step']}: {heal['from']} → {heal['to']}"
                                 + (f" ({heal['why']})" if heal.get("why") else ""))
            if items:
                lines.append("Accept with action accept, or keep the check failing there with action reject.")
            return ToolResult("\n".join(lines), {"heals": items})
        settled = [{"check": str(entry.path), "steps": count}
                   for entry, count in heal_review.settle(root, found, action)]
        word = "Accepted" if action == "accept" else "Kept failing"
        text = (f"{word}: " + ", ".join(f"{Path(item['check']).name} ({item['steps']})" for item in settled)
                if settled else "No heals wait for review.")
        return ToolResult(text, {"action": action, "settled": settled})


class ReproJob(SuiteJob):
    """repro_crash's one run of the repro check, judged against the crash it came from."""

    kind = "repro_crash"

    def __init__(self, options, brief, root, check, *, key=None, execute=None, plan=None):
        super().__init__(options, root=root, key=key, execute=execute, plan=plan)
        self.brief, self.check = brief, check

    def result(self):
        from ..mcp_server.protocol import ToolResult
        from . import repro
        if self.error is not None or not self.finished.is_set():
            return super().result()
        found = repro.save(self.brief, self.root, repro.outcome(self.brief, self.doc, check=self.check,
                                                                folder=self.paths.get("folder")))
        return ToolResult("\n".join(repro.summary_lines(self.brief, found)),
                          {"status": "finished", "suite_id": self.suite_id, "reproduction": found["status"],
                           "brief": self.brief.public(), **{k: found[k] for k in ("check", "stepsTried", "crash",
                                                                                   "video", "report", "note")}})
