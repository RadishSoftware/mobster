"""`mobster test --xctest`: the project's own XCTest and Swift Testing targets, run beside the checks and read from the
.xcresult into the suite's one results model (doc["xctest"]).

This is not a general XCTest runner: one simulator, no retries of a test, no device runs, and the project's schemes
and test plans are read, never changed."""

import json
import plistlib
import re
from pathlib import Path

from ..sim.models import SimError
from .prbuild import BuildError, find_project, pick_scheme, project_arg, run_command
from .prbuild import build as build_app

BUNDLE_DIR = "xctest"
BUNDLE_NAME = "tests.xcresult"
ATTACHMENTS = "attachments"
RUN_LOG = "xctest.log"
RUN_TIMEOUT = 30 * 60
READ_TIMEOUT = 120
FAILURE_EXIT = 65                 # xcodebuild's code for "the tests ran and some failed"
MAX_FAILURES = 5                  # failure messages kept per test
MAX_MESSAGE = 600
ONLY_TESTING = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ .+()-]*(/[A-Za-z0-9_ .+()-]+){0,2}$")
LOCATION = re.compile(r"^(?P<file>[^\n:]+\.(?:swift|m|mm|h|c|cpp)):(?P<line>\d+):?\s*(?P<text>.*)$", re.S)
EXPORT_SUFFIX = re.compile(r"_\d+_[0-9A-Fa-f-]{36}(?=\.[^.]*$|$)")    # xcresulttool appends _<n>_<UUID> to a name
RESULTS = {"passed": "passed", "failed": "failed", "skipped": "skipped", "expected failure": "passed"}


class XctestError(Exception):
    """The XCTest half of the run couldn't be done: what happened, and what to do (the checks still ran)."""

    def __init__(self, message, fix=""):
        super().__init__(message)
        self.message, self.fix = message, fix


def only_testing_ok(items):
    """``--only-testing`` values: Target, Target/Class or Target/Class/test. XCTestError otherwise."""
    for item in items:
        if not ONLY_TESTING.match(item):
            raise XctestError(f"--only-testing {item!r} isn't a test identifier.",
                              "Write Target, Target/Class or Target/Class/testName.")
    return tuple(items)


def find_xctestrun(derived):
    """The .xctestrun file build-for-testing wrote under ``derived``, or None."""
    found = sorted((Path(derived) / "Build" / "Products").glob("*.xctestrun"))
    return found[-1] if found else None


def targets_of(xctestrun):
    """The test targets an .xctestrun names, in the order it lists them."""
    try:
        with open(xctestrun, "rb") as handle:
            data = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return []
    blocks = []
    for config in data.get("TestConfigurations") or ():
        blocks += [block for block in config.get("TestTargets") or () if isinstance(block, dict)]
    blocks += [block for key, block in data.items() if key != "__xctestrun_metadata__" and isinstance(block, dict)
               and "TestBundlePath" in block]
    names = [str(block.get("BlueprintName") or block.get("ProductModuleName") or "") for block in blocks]
    return list(dict.fromkeys(name for name in names if name))


def select_targets(targets, changed, only=()):
    """(targets to run, {target: why}). ``only`` (--only-testing) wins; else the targets whose folder holds a changed
    file; else every target, because a change that touches no test file can still break one."""
    if only:
        named = {item.split("/", 1)[0] for item in only}
        return [name for name in targets if name in named], {name: "named with --only-testing" for name in named
                                                              if name in targets}
    if changed is None:
        return list(targets), {}
    hit = {}
    for target in targets:
        files = [path for path in changed if target in Path(path).parts]
        if files:
            hit[target] = f"{files[0]} changed" + (f" and {len(files) - 1} more" if len(files) > 1 else "")
    if hit:
        return [name for name in targets if name in hit], hit
    return list(targets), {name: "no test file changed, so every target runs" for name in targets}


def build(root, *, scheme, project, test_plan=None, runner=None, progress=None, bundle=None, configuration="Debug"):
    """build-for-testing into .mobster/build: the app the checks drive, the test bundles and the .xctestrun. The
    result is prbuild.build's, plus "xctestrun" and "targets". BuildError."""
    built = build_app(root, scheme=scheme, project=project, platform="sim", configuration=configuration,
                      runner=runner, progress=progress, bundle=bundle, for_testing=True, test_plan=test_plan)
    derived = Path(root) / ".mobster" / "build"
    run_file = find_xctestrun(derived)
    if run_file is None:
        raise BuildError(f"{scheme} built, but it has no test target to run.",
                         "Add a unit test target to the scheme's Test action, or leave --xctest out.")
    return {**built, "scheme": scheme, "xctestrun": str(run_file), "targets": targets_of(run_file)}


def prepare(planned, progress, *, runner=None):
    """The build for a run that didn't build for testing first (`mobster test --xctest`): the project's own scheme.
    BuildError."""
    options, root = planned.options, planned.root
    found = project_arg(Path(root) / options.xctest_project) if options.xctest_project else find_project(root)
    if found is None:
        raise BuildError(f"{root} has no Xcode project to build the tests from.",
                         "Pass --project YourApp.xcodeproj, or leave --xctest out.")
    scheme = options.xctest_scheme or pick_scheme(found, runner=runner, cwd=root)
    return build(root, scheme=scheme, project=found, test_plan=options.xctest_plan, runner=runner, progress=progress)


def run_argv(xctestrun, udid, bundle_path, only):
    argv = ["xcodebuild", "test-without-building", "-xctestrun", str(xctestrun), "-destination", f"id={udid}",
            "-resultBundlePath", str(bundle_path)]
    for item in only:
        argv.append(f"-only-testing:{item}")
    return argv


def run_tests(xctestrun, udid, folder, only, *, runner=None, progress=None):
    """Run the built tests on simulator ``udid``; the result bundle goes under ``folder``/xctest/. The bundle's path,
    or XctestError when xcodebuild stopped before running tests."""
    progress = progress or (lambda line: None)
    bundle = Path(folder) / BUNDLE_DIR / BUNDLE_NAME
    bundle.parent.mkdir(parents=True, exist_ok=True)
    progress("Running your XCTest targets")
    code, output = (runner or run_command)(run_argv(xctestrun, udid, bundle, only), Path(folder),
                                           bundle.parent / RUN_LOG, RUN_TIMEOUT)
    if code not in (0, FAILURE_EXIT) or not (bundle / "Info.plist").is_file():
        errors = [line.strip() for line in output.splitlines() if "error:" in line or "Testing failed" in line][:3]
        detail = "; ".join(errors)[:500] if errors else f"xcodebuild exited {code}"
        raise XctestError(f"The tests didn't run: {detail}.", f"Read {bundle.parent / RUN_LOG}, then run it again.")
    return bundle


def _json(argv, runner):
    code, output = (runner or run_command)(argv, Path.cwd(), None, READ_TIMEOUT)
    start = output.find("{")
    try:
        data = json.JSONDecoder().raw_decode(output[start:])[0] if code == 0 and start >= 0 else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise XctestError("Mobster couldn't read the test results.", "Run `xcrun xcresulttool get test-results "
                          "summary --path <the .xcresult>` to see why; it needs Xcode 16 or later.")
    return data


def read_bundle(bundle, *, runner=None):
    """(tests, summary): xcresulttool's JSON for ``bundle``. XctestError when it can't be read."""
    base = ["xcrun", "xcresulttool", "get", "test-results"]
    tail = ["--path", str(bundle), "--format", "json"]
    return _json(base + ["tests"] + tail, runner), _json(base + ["summary"] + tail, runner)


def _seconds(node):
    value = node.get("durationInSeconds")
    if isinstance(value, (int, float)):
        return value
    match = re.match(r"^\s*([0-9.]+)\s*(ms|s|m)\b", str(node.get("duration") or ""))
    if not match:
        return 0
    return float(match.group(1)) * {"ms": 0.001, "s": 1, "m": 60}[match.group(2)]


def _failures(node):
    """The case's failure messages, each as {"message", "file", "line"}."""
    out = []
    for child in node.get("children") or ():
        if child.get("nodeType") == "Failure Message":
            text = str(child.get("name") or "").strip()
            found = LOCATION.match(text)
            entry = {"message": (found.group("text") if found else text)[:MAX_MESSAGE], "file": None, "line": None}
            if found:
                entry["file"], entry["line"] = Path(found.group("file")).name, int(found.group("line"))
            out.append(entry)
        elif isinstance(child.get("children"), list):
            out += _failures(child)
    return out[:MAX_FAILURES]


def _cases(nodes, bundle=None, suites=()):
    """Every Test Case under ``nodes`` as (bundle, suite names, node)."""
    for node in nodes:
        kind = str(node.get("nodeType") or "")
        if kind == "Test Case":
            yield bundle, suites, node
        elif isinstance(node.get("children"), list):
            name = str(node.get("name") or "")
            yield from _cases(node["children"], name if "test bundle" in kind.lower() else bundle,
                              suites + (name,) if kind == "Test Suite" else suites)


def parse(tests, summary=None, device=None):
    """The targets of an xcresulttool ``tests`` document as the suite's results model:
    [{"name", "status", "tests", "passed", "failed", "skipped", "durationMs", "cases": [...]}]. A case has name,
    classname, status (passed, failed or skipped), durationMs, failures [{message, file, line}] and attachments []."""
    targets = {}
    where = {} if device is None else {"device": device["name"], "deviceKey": device["key"]}
    for bundle, suites, node in _cases(tests.get("testNodes") or ()):
        target = targets.setdefault(bundle or "Tests", {"name": bundle or "Tests", "cases": []})
        status = RESULTS.get(str(node.get("result") or "").lower(), "failed")
        name = re.sub(r"\(\)$", "", str(node.get("name") or ""))
        failures = _failures(node)
        if status == "failed" and not failures:
            failures = [{"message": "The test failed without a message.", "file": None, "line": None}]
        target["cases"].append({
            "id": str(node.get("nodeIdentifier") or "/".join(filter(None, [suites[-1] if suites else None, name]))),
            "name": name, "classname": suites[-1] if suites else target["name"], "status": status,
            "durationMs": round(_seconds(node) * 1000), "failures": failures, "attachments": [], **where})
    out = []
    for target in targets.values():
        cases = sorted(target["cases"], key=lambda case: (case["status"] != "failed", case["classname"], case["name"]))
        counts = {key: sum(case["status"] == key for case in cases) for key in ("passed", "failed", "skipped")}
        out.append({"name": target["name"], "status": "failed" if counts["failed"] else "passed", "tests": len(cases),
                    **counts, "durationMs": sum(case["durationMs"] for case in cases), "cases": cases})
    return out


def export_attachments(bundle, folder, targets, *, runner=None):
    """Export the screenshots and files the tests attached into ``folder``/xctest/attachments/ and name them on their
    cases (paths relative to ``folder``). Best effort: a test's verdict never depends on it."""
    folder = Path(folder)
    out_dir = folder / BUNDLE_DIR / ATTACHMENTS
    (runner or run_command)(["xcrun", "xcresulttool", "export", "attachments", "--path", str(bundle),
                                       "--output-path", str(out_dir)], folder, None, READ_TIMEOUT)
    try:
        manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    wanted = {case["id"]: case for target in targets for case in target["cases"]}
    count = 0
    for entry in manifest if isinstance(manifest, list) else ():
        case = wanted.get(str(entry.get("testIdentifier") or ""))
        for item in entry.get("attachments") or ():
            name = str(item.get("exportedFileName") or "")
            if case is None or not name or not (out_dir / name).is_file() or Path(name).name != name:
                continue
            label = EXPORT_SUFFIX.sub("", str(item.get("suggestedHumanReadableName") or name))[:120]
            case["attachments"].append({"name": label, "path": f"{BUNDLE_DIR}/{ATTACHMENTS}/{name}"})
            count += 1
    return count


def section(targets, *, scheme, test_plan, device, bundle=None, why=None):
    """doc["xctest"]: the targets plus their totals, the device they ran on and why each target ran."""
    totals = {key: sum(target[key] for target in targets) for key in ("tests", "passed", "failed", "skipped")}
    status = "failed" if totals["failed"] else "passed"
    return {"status": status, "scheme": scheme, "testPlan": test_plan, "device": device["name"],
            "deviceKey": device["key"], "resultBundle": bundle, "why": dict(why or {}), "summary": totals,
            "durationMs": sum(target["durationMs"] for target in targets), "targets": targets}


def milliseconds(ms):
    """"38 ms", "1.2 s": a test's time."""
    return f"{ms} ms" if ms < 1000 else f"{ms / 1000:.1f} s"


def couldnt_run(error, *, scheme=None, test_plan=None, why=None):
    return {"status": "couldnt_run", "scheme": scheme, "testPlan": test_plan, "reason": {
        "message": error.message, "fix": error.fix}, "why": dict(why or {}), "summary": {
            "tests": 0, "passed": 0, "failed": 0, "skipped": 0}, "durationMs": 0, "targets": []}


def words(found):
    """"XCTest 3 of 4 passed", "XCTest 1 failed of 4", "XCTest couldn't run"."""
    totals = found.get("summary") or {}
    if found.get("status") == "couldnt_run":
        return "XCTest couldn't run"
    if totals.get("failed"):
        return f"XCTest {totals['failed']} failed of {totals['tests']}"
    return f"XCTest {totals.get('passed', 0)} of {totals.get('tests', 0)} passed"


def failed_cases(doc):
    """(target name, case) for every failed XCTest case in a suite's results document."""
    return [(target["name"], case) for target in (doc.get("xctest") or {}).get("targets") or ()
            for case in target.get("cases") or () if case.get("status") == "failed"]


def failure_line(case):
    """"PlanTests.swift:14: XCTAssertEqual failed ..." for a failed case."""
    first = (case.get("failures") or [{}])[0]
    where = f"{first['file']}:{first['line']}: " if first.get("file") else ""
    return (where + str(first.get("message") or "failed")).strip()


# The worst outcome decides the exit code: failed, couldn't run, review, passed.
EXIT_RANK = {1: 0, 3: 1, 2: 2, 0: 3}


def merge_summary(doc):
    """Fold the XCTest result into the suite's summary and exit code (130, a stopped run, stays)."""
    found = doc.get("xctest")
    if not found:
        return doc
    summary = doc.setdefault("summary", {})
    summary["xctest"] = dict(found["summary"], status=found["status"])
    own = {"failed": 1, "couldnt_run": 3}.get(found["status"], 0)
    current = doc.get("exitCode")
    if current in EXIT_RANK and EXIT_RANK[own] < EXIT_RANK[current]:
        doc["exitCode"] = own
    elif current is None:
        doc["exitCode"] = own
    return doc


def run_suite(planned, attempts, folder, progress, *, runner=None):
    """Build for testing (unless `test pr` did), run the targets on a simulator leased from ``attempts`` and return
    doc["xctest"].

    The simulator is the first simulator target of the run (the lease the checks just used is free again, so a booted
    one is reused); a run of iPhones only has none, and says so."""
    options = planned.options
    labels = {"scheme": options.xctest_scheme, "test_plan": options.xctest_plan, "why": {}}
    built = planned.xctest_build
    if built is None:
        try:
            built = prepare(planned, progress, runner=runner)
        except BuildError as error:
            return couldnt_run(XctestError(error.message, error.fix), **labels)
    chosen, why = select_targets(built.get("targets") or [], planned.changed, tuple(options.xctest_only))
    labels = {"scheme": built.get("scheme"), "test_plan": options.xctest_plan, "why": why}
    target = next((t for t in planned.targets if t.kind in ("default", "simulator") and t.record is None), None)
    if target is None:
        return couldnt_run(XctestError("--xctest runs on a simulator, and this run names only iPhones.",
                                       "Add --sim, or run the XCTest targets in another run."), **labels)
    if not chosen:
        return couldnt_run(XctestError("None of the scheme's test targets matches --only-testing.",
                                       f"The scheme's targets are {', '.join(built.get('targets') or []) or 'none'}."),
                           **labels)
    only = [item for item in options.xctest_only if item.split("/", 1)[0] in chosen]
    if not only and set(chosen) != set(built.get("targets") or []):
        only = list(chosen)   # the targets a changed file picked
    try:
        lease = attempts.simulators().acquire(target.device_type, target.runtime)
    except SimError as error:
        return couldnt_run(XctestError(f"No simulator was free for the tests: {error}",
                                       error.fix or "Run `mobster sim doctor`."), **labels)
    try:
        sim = lease.target
        device = {"name": sim.name, "key": f"sim:{sim.device_type}@{sim.runtime}"}
        try:
            bundle = run_tests(built["xctestrun"], sim.udid, folder, only, runner=runner, progress=progress)
            tests, summary = read_bundle(bundle, runner=runner)
        except XctestError as error:
            return couldnt_run(error, **labels)
        targets = parse(tests, summary, device)
        if not targets:
            return couldnt_run(XctestError("The scheme ran no tests.", "Check --only-testing and the scheme's Test "
                                                                       "action."), **labels)
        export_attachments(bundle, folder, targets, runner=runner)
        return section(targets, scheme=built.get("scheme"), test_plan=options.xctest_plan, device=device,
                       bundle=Path(bundle).relative_to(folder).as_posix(), why=why)
    finally:
        lease.release()


def text_lines(doc, paint):
    """One line per failed XCTest case, with its file and line, and the XCTest totals."""
    found = doc.get("xctest")
    if not found:
        return []
    lines = [f"{paint('✗ failed', 'failed')}  {target}: {case['classname']}.{case['name']}\n    "
             f"{failure_line(case)}" for target, case in failed_cases(doc)]
    totals = found["summary"]
    if found["status"] == "couldnt_run":
        reason = found.get("reason") or {}
        lines.append(f"{paint('✗ couldn\'t run', 'couldnt_run')}  your XCTest targets\n    "
                     f"{reason.get('message')} {reason.get('fix')}".rstrip())
    else:
        lines.append(f"XCTest: {totals['passed']} of {totals['tests']} passed"
                     + (f", {totals['failed']} failed" if totals["failed"] else "")
                     + (f", {totals['skipped']} skipped" if totals["skipped"] else "")
                     + f" in {len(found['targets'])} target{'' if len(found['targets']) == 1 else 's'}")
    return lines
