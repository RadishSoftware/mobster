"""`mobster test`: run a project's checks on simulators and iPhones, with retries, flake detection and reports."""

import json
import os
import shlex
import sys
from pathlib import Path

from ..paths import write_text_atomic
from ..verify.checks import LIMITS
from ..verify.report import dots
from . import repro, snapshots_cli, xctest
from .cli_arguments import USAGE_EXIT, add_arguments

RECORD = "record"
IMPORT = "import"
IMPORT_SHOWN = 8
ADD = "add"
PR = "pr"
PR_FLAGS = (("base", "--base"), ("no_build", "--no-build"), ("scheme", "--scheme"), ("project", "--project"),
            ("drafts", "--drafts"), ("no_gifs", "--no-gifs"), ("tested_with", "--tested-with"),
            ("comment", "--comment"), ("github", "--github"))
HEAL = "heal"
NIGHTLY = "nightly"
SCHEDULE = "schedule"
CAPSULE = "capsule"
COMPARE = "compare"
SCENARIO = "scenario"
MINIMIZE = "minimize"
LOCAL_RESULTS = {
    "sim": None, "device": None, "testflight": None, "matrix": None, "locale": None,
    "spread": False, "parallel": 1, "retries": None, "repeat": 1, "strict": False,
    "quarantine": None, "auto_quarantine": False, "tag": None, "audit": None,
    "a11y": None, "update_a11y_baseline": False, "shard": None, "keyless": False,
    "replay": False, "fresh": False, "no_heal": False, "junit": None, "html": None,
    "video": False, "history": False, "share": False, "share_days": 30,
    "assert_timeout": 5, "record_run": None, "record_name": None,
    "bundle": None, "app": None, "skip_unsupported": False, "dry_run": False,
    "force": False, "add_list": False, "add_set": None, "base": None,
    "no_build": False, "scheme": None, "project": None, "drafts": "run",
    "no_gifs": False, "tested_with": False, "comment": None, "github": "check",
    "at": None, "accept": False, "reject": False,
}
REPRO = "repro"
TONES = {"passed": "green", "failed": "coral", "needs_review": "accent", "couldnt_run": "tertiary", "flaky": "amber",
         "draft": "accent"}


def _paint(color):
    from ..style import Palette
    return Palette(color)


def _pr_flags(args):
    """The first `mobster test pr` flag given without pr, or None."""
    for name, flag in PR_FLAGS:
        if name in ("scheme", "project") and getattr(args, "xctest", False):
            continue
        value = getattr(args, name, None)
        if value and not (name == "drafts" and value == "run") and not (name == "github" and value == "check"):
            return flag
    return None


def _isatty(stream):
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def _only_testing(args, options):
    """Set options.xctest_only from --only-testing; True (after saying why) when one isn't a test identifier."""
    try:
        options.xctest_only = xctest.only_testing_ok(getattr(args, "only_testing", None) or ())
    except xctest.XctestError as error:
        _usage(f"{error.message} {error.fix}")
        return True
    return False


def _usage(message):
    print(f"mobster test: {message}", file=sys.stderr, flush=True)
    return USAGE_EXIT


def run(args):
    """`mobster test` for parsed ``args``; returns the exit code. Never raises into __main__."""
    from ..devtools import signals_as_interrupt
    paths = list(getattr(args, "paths", None) or ())
    if (getattr(args, "minimize_bad_app", None) or getattr(args, "minimize_control_app", None) or
            getattr(args, "minimize_isolated", False) or getattr(args, "minimize_max_attempts", None) is not None) \
            and (not paths or paths[0] != MINIMIZE):
        return _usage("--bad-app, --control-app, --isolated and --max-attempts go with mobster test minimize")
    if getattr(args, "capsule_include", None) and (not paths or paths[0] != CAPSULE):
        return _usage("--include goes with capsule export: mobster test capsule export SUITE_ID --include KIND")
    if paths and paths[0] in (CAPSULE, COMPARE, SCENARIO, MINIMIZE):
        command = paths[0]
        for name, default in LOCAL_RESULTS.items():
            if getattr(args, name, default) != default:
                return _usage(f"--{name.replace('_', '-')} doesn't go with mobster test {command}")
        if command != CAPSULE and getattr(args, "record_output", None):
            return _usage(f"--output doesn't go with mobster test {command}")
        if command == SCENARIO:
            from . import scenario_cli
            with signals_as_interrupt():
                try:
                    return scenario_cli.run(args, paths[1:])
                except KeyboardInterrupt:
                    print("mobster test scenario: stopped", file=sys.stderr, flush=True)
                    return 130
                except Exception as error:
                    print(f"mobster test scenario: unexpected error ({type(error).__name__}: {error}). "
                          "Report the command you ran.", file=sys.stderr, flush=True)
                    return USAGE_EXIT
        if command == MINIMIZE:
            from . import minimizer_cli
            with signals_as_interrupt():
                try:
                    return minimizer_cli.run(args, paths[1:])
                except KeyboardInterrupt:
                    print("mobster test minimize: stopped; review the saved trial before another attempt",
                          file=sys.stderr, flush=True)
                    return 130
                except Exception as error:
                    print(f"mobster test minimize: unexpected error ({type(error).__name__}). "
                          "Review the saved trial before another attempt.", file=sys.stderr, flush=True)
                    return USAGE_EXIT
        from . import capsule_cli, comparison_cli
        return (capsule_cli if command == CAPSULE else comparison_cli).run(args, paths[1:])
    simple = {RECORD: record, IMPORT: import_flows, ADD: add, snapshots_cli.SNAPSHOTS: snapshots_cli.snapshots,
              REPRO: repro.main, NIGHTLY: nightly, SCHEDULE: schedule}   # each takes (args, the rest of the paths)
    if paths and paths[0] in simple:
        return simple[paths[0]](args, paths[1:])
    if paths and paths[0] == PR:
        with signals_as_interrupt():
            try:
                return pr(args, paths[1:])
            except KeyboardInterrupt:
                print("mobster test: stopped", file=sys.stderr, flush=True)
                return 130
            except Exception as error:  # a last resort, as for the suite
                print(f"mobster test pr: unexpected error ({type(error).__name__}: {error}). Report it at "
                      "github.com/RadishSoftware/mobster/issues with the command you ran.", file=sys.stderr, flush=True)
                return USAGE_EXIT
    if getattr(args, "history", False):
        return history(args, paths)
    if (getattr(args, "test_plan", None) or getattr(args, "only_testing", None)) and not getattr(args, "xctest", False):
        return _usage("--test-plan and --only-testing go with --xctest: mobster test --xctest --test-plan NAME")
    if getattr(args, "at", None):
        return _usage("--at goes with nightly: mobster test nightly on --at 02:00")
    stray = _pr_flags(args)
    if stray:
        return _usage(f"{stray} goes with pr: mobster test pr {stray} …")
    if paths and paths[0] == HEAL:
        return heal(args, paths[1:])
    if getattr(args, "accept", False) or getattr(args, "reject", False):
        return _usage("--accept and --reject go with heal: mobster test heal --accept CHECK")
    if getattr(args, "fresh", False) and (getattr(args, "no_heal", False) or getattr(args, "replay", False)):
        return _usage("--fresh records the path again, so it doesn't go with --replay or --no-heal")
    if getattr(args, "keyless", False) and (getattr(args, "fresh", False) or getattr(args, "no_heal", False)):
        return _usage("--keyless never runs Smart, so --fresh and --no-heal don't apply")
    if getattr(args, "record_run", None) or getattr(args, "record_name", None) or getattr(args, "record_output", None):
        return _usage("--run, --name and -o go with record: mobster test record --run RUN_ID")
    if getattr(args, "repro_arg", None) or getattr(args, "link", None):
        return _usage("--arg and --link go with repro: mobster test repro CRASH_FILE --link myapp://cart")
    stray = next((flag for flag, name in (("--bundle", "bundle"), ("--app", "app"),
                                          ("--skip-unsupported", "skip_unsupported"), ("--dry-run", "dry_run"),
                                          ("--force", "force"))
                  if getattr(args, name, None)), None)
    if stray:
        return _usage(f"{stray} goes with import: mobster test import maestro PATH {stray}")
    if getattr(args, "add_list", None) or getattr(args, "add_set", None):
        return _usage("--list and --set go with add: mobster test add RECIPE")
    with signals_as_interrupt():
        try:
            return suite(args, paths)
        except KeyboardInterrupt:
            print("mobster test: stopped", file=sys.stderr, flush=True)
            return 130
        except Exception as error:  # a last resort: never a traceback, never "failed" (exit 1)
            print(f"mobster test: unexpected error ({type(error).__name__}: {error}). Report it at "
                  "github.com/RadishSoftware/mobster/issues with the command you ran.", file=sys.stderr, flush=True)
            return USAGE_EXIT


def _command(args):
    return "mobster test " + " ".join(shlex.quote(item) for item in sys.argv[2:]) if len(sys.argv) > 2 and \
        sys.argv[1] == "test" else "mobster test"


def suite(args, paths, *, execute=None, plan=None, out=None, err=None):
    from .api import Options, UsageError, parse_locales
    out, err = out or sys.stdout, err or sys.stderr
    options = Options(paths=tuple(paths), sims=tuple(args.sim or ()), devices=tuple(args.device or ()),
                      matrix=args.matrix, parallel=args.parallel, retries=args.retries, repeat=args.repeat,
                      quarantine=args.quarantine, tags=tuple(args.tag or ()), shard=args.shard, junit=args.junit,
                      html=args.html, strict=args.strict, keyless=args.keyless, video=args.video,
                      assert_timeout=args.assert_timeout, command=_command(args),
                      testflight=tuple(getattr(args, "testflight", None) or ()),
                      audit=tuple(getattr(args, "audit", None) or ()), a11y=getattr(args, "a11y", None),
                      android=getattr(args, "android", None), **snapshots_cli.options(args),
                      **{flag: bool(getattr(args, flag, False)) for flag in (
                          "fresh", "no_heal", "update_a11y_baseline", "spread", "auto_quarantine", "xctest")},
                      xctest_scheme=getattr(args, "scheme", None), xctest_project=getattr(args, "project", None),
                      xctest_plan=getattr(args, "test_plan", None))
    if _only_testing(args, options):
        return USAGE_EXIT
    try:
        options.locales = parse_locales(getattr(args, "locale", None))
    except UsageError as error:
        return _usage(str(error))
    if options.testflight and not options.devices:
        return _usage("--testflight goes with --device: a TestFlight build is on an iPhone, not a simulator")
    if plan is None:
        from .api import plan
    if execute is None:
        from .api import execute
    try:
        planned = plan(options)
    except UsageError as error:
        return _usage(str(error))
    try:
        from ..devtools import ProgressLines
        progress = ProgressLines(err)
    except ImportError:
        def progress(line):
            print(line, file=err, flush=True)
    for note in planned.notes:
        progress(note)
    runs = len(planned.entries) * len(planned.targets) * options.repeat
    if not runs and not options.xctest:
        print("No checks to run.", file=out, flush=True)
        return 0
    progress(f"mobster test: {len(planned.entries)} check{'' if len(planned.entries) == 1 else 's'} on "
             f"{_targets(planned.targets)}" + (f", {options.repeat} times each" if options.repeat > 1 else "")
             + (", each on whichever is free" if options.spread else "")
             + (f", {options.parallel} at a time" if options.parallel > 1 else "")
             + (", then your XCTest targets" if options.xctest else ""))
    try:
        doc, report_paths = execute(planned, progress=progress)
    finally:
        getattr(progress, "end", lambda: None)()
    shared = None
    if getattr(args, "share", False) and report_paths.get("folder"):
        shared = _share(report_paths["folder"], args.share_days, err)
    _tell_slack(planned.root, report_paths, doc, shared, err)
    if args.json:
        try:
            print(json.dumps(doc, ensure_ascii=False, allow_nan=False), file=out, flush=True)
        except BrokenPipeError:
            os.dup2(os.open(os.devnull, os.O_WRONLY), out.fileno())
    else:
        from ..style import color_enabled
        print(summary_text(doc, report_paths, color=color_enabled(out)), file=out, flush=True)
    return int(doc.get("exitCode", USAGE_EXIT))


def _share(folder, days, err):
    """Upload the suite's report, sealed (teams/report_share.py), and say its link; or say why not."""
    try:
        from ..teams import report_share
    except ModuleNotFoundError as error:
        if error.name != "mobile_agent.teams":
            raise
        print(f"mobster test: sharing reports needs the Mobster app's team extension. Your report is in {folder}.",
              file=err, flush=True)
        return None
    try:
        shared = report_share.share_folder(folder, days=days)
    except report_share.ShareRefused as refused:
        print(f"mobster test: {refused}", file=err, flush=True)
        return None
    except (OSError, ValueError):
        # The error could name a file, and a file's path names a check or a task: say what happened, not the path.
        print(f"mobster test: the report wasn't shared. Your report is in {folder}.", file=err, flush=True)
        return None
    print(f"Shared with your team for {days} days: {shared.link}", file=err, flush=True)
    return shared


def _targets(targets):
    if len(targets) == 1:
        return targets[0].label if targets[0].kind != "default" else "each check's simulator"
    return f"{len(targets)} devices"


def _relative(path, root=None):
    if not path:
        return path
    try:
        return str(Path(path).resolve().relative_to(Path(root or Path.cwd()).resolve()))
    except ValueError:
        return str(path)


def schedule(args, rest, *, cwd=None, out=None, err=None, schedules=None):
    """`mobster test schedule [list | run-now NAME | device NAME DEVICE|none]`: the scheduled health checks."""
    import time

    from ..verify.checks import project_root
    from . import health
    out, err = out or sys.stdout, err or sys.stderr
    cwd = Path(cwd or Path.cwd())
    root = project_root(cwd)
    verb = rest[0] if rest else "list"
    if verb == "list":
        found = health.listing(root, round(time.time() * 1000))
        if args.json:
            print(json.dumps(found, ensure_ascii=False, allow_nan=False), file=out, flush=True)
            return 0
        for problem in found["problems"]:
            print(f"mobster test: {problem}", file=err, flush=True)
        if not found["schedules"]:
            print("No scheduled checks. Add `schedule: every 1h` to a check, or list schedules in "
                  ".mobster/schedule.yaml.", file=out, flush=True)
            return 0
        for row in found["schedules"]:
            last = row["last"]
            ended = "never ran" if last is None else f"last {last['outcome']}" + (
                f" {last['verdict']}" if last["verdict"] else "") + (f" ({last['reason']})" if last["reason"] else "")
            target = f"phone {row['device']}" if row["device"] else row["sim"] or "each check's simulator"
            nxt = time.strftime("%a %H:%M", time.localtime(row["nextAt"] / 1000)) if row["nextAt"] else "-"
            nxt = f"next {nxt}" if row["on"] else "off on this Mac"
            print(f"{row['name']:<24} {row['when']:<14} {nxt}  on {target}  {ended}", file=out)
        print(file=out, flush=True)
        return 0
    if verb in ("run-now", "device") and len(rest) < 2:
        return _usage(f"{verb} needs the schedule's name: mobster test schedule {verb} NAME")
    specs, problems = health.load(root)
    spec = next((item for item in specs if len(rest) > 1 and item.name == rest[1]), None)
    if verb not in ("run-now", "device"):
        return _usage(f"schedule takes list, run-now NAME or device NAME DEVICE, not {verb}")
    if spec is None:
        known = ", ".join(item.name for item in specs) or "none"
        return _usage(f"no schedule named {rest[1]!r} (known: {known})")
    if verb == "device":
        if len(rest) < 3:
            return _usage("device needs a device, or none: mobster test schedule device NAME \"Sam's iPhone\"")
        health.set_device(root, spec.name, None if rest[2] == "none" else rest[2])
        print(f"{spec.name} " + ("runs on its simulator again." if rest[2] == "none" else
                                 f"runs on {rest[2]} while Mobster is running."), file=out, flush=True)
        return 0
    # a phone only by --device: its plan gate runs first
    runner = schedules or health.TestSchedules(devices=lambda: {})
    device = (args.device or [None])[0]
    sim = (args.sim or [None])[0]
    answer = runner.start(spec, trigger="run-now", device=device, sim=sim, wait=True,
                          progress=lambda line: print(line, file=err, flush=True))
    if answer["outcome"] == "skipped":
        print(f"mobster test: {spec.name} was skipped. {answer['reason']}", file=err, flush=True)
        return USAGE_EXIT
    print(f"{spec.name}: {answer['verdict'].replace('_', ' ')} (suite {answer['suite']})", file=out, flush=True)
    return answer["exitCode"] or 0


def history(args, paths, *, out=None):
    """`mobster test --history [PATH]`: the project's per-check history, flaky first. Runs nothing."""
    from ..verify import perf
    from ..verify.checks import project_root
    from .history import report
    out = out or sys.stdout
    start = Path(paths[0]) if paths else Path.cwd()
    root = project_root(start if start.exists() else Path.cwd())
    found = report(root)
    if args.json:
        print(json.dumps(found, ensure_ascii=False, allow_nan=False), file=out, flush=True)
        return 0
    if not found["ok"]:
        print("mobster test: the history file couldn't be read, so it starts again with the next run. Delete "
              ".mobster/test-results/history.json.corrupt when you no longer need it.", file=sys.stderr, flush=True)
    if not found["checks"]:
        print("No test history yet. Run `mobster test` and each check's results are kept here.", file=out, flush=True)
        return 0
    marks = {"passed": "✓", "failed": "✗", "needs_review": "!"}
    for row in found["checks"]:
        trend = f", {row['trend']}" if row["trend"] and row["trend"] != "steady" else ""
        line = (f"{row['state']:<7} {row['name']}  on {dots(row['device'])}  "
                f"{''.join(marks[o] for o in row['outcomes'])}  {round((row['passRate'] or 0) * 100)}% of "
                f"{row['runs']} runs, flake score {row['flakeScore']:.2f}{trend}{perf.last_run(row)}")
        print(line, file=out)
        if row["suggestQuarantine"]:
            print(f"    Flaky: add {row['file']} to .mobster/quarantine.yaml, or run with --auto-quarantine.", file=out)
    print(file=out, flush=True)
    return 0


def summary_text(doc, paths, color=False):
    """The human summary: one line per check and device that didn't pass cleanly, the counts, the reports."""
    from .reports import counts_text, duration_text
    words = {"passed": "✓ passed", "failed": "✗ failed", "needs_review": "! needs review",
             "couldnt_run": "✗ couldn't run", "flaky": "~ flaky"}

    def paint(text, tone):
        return _paint(color)(text, TONES[tone], "bold")

    lines = []
    for check in doc.get("checks") or ():
        for result in check.get("results") or ():
            status = result.get("status")
            tone = "flaky" if status == "passed" and result.get("flaky") else status
            line = f"{paint(words.get(tone, status), tone)}  {check.get('name')} on {dots(result.get('device') or '')}"
            if result.get("passRate") is not None:
                line += f"  ({round(result['passRate'] * 100)}% of {doc.get('options', {}).get('repeat')} runs)"
            elif (result.get("attempts") or 0) > 1:
                line += f"  ({result['attempts']} attempts)"
            if check.get("quarantined"):
                line += "  [quarantined]"
            lines.append(line)
            reason = result.get("reason") or (result.get("flakyReason") if result.get("flaky") else None) or {}
            if status != "passed" or result.get("flaky"):
                for text in (reason.get("message"), reason.get("fix")):
                    if text:
                        lines.append(f"    {text}")
            for item in result.get("healed") or ():
                if isinstance(item, dict):
                    lines.append(f"    healed step {item.get('step')}: {item.get('from')} → {item.get('to')}")
    lines += xctest.text_lines(doc, paint)
    for item in (doc.get("history") or {}).get("suggestQuarantine") or ():
        lines.append(f"~ {item['check']} on {dots(item['device'] or '')} is flaky over its last 20 runs. Quarantine "
                     f"it: add {item['file']} to .mobster/quarantine.yaml.")
    summary = doc.get("summary") or {}
    head = f"{counts_text(summary)} in {duration_text(doc.get('durationMs'))}"
    if summary.get("costUsd"):
        head += f", ${summary['costUsd']:.2f}"
    if summary.get("replayed"):
        head += f", {summary['replayed']} replayed with no model"
    if summary.get("linkFirst"):
        head += (f", {summary['linkFirst']['steps']} link-first, "
                 f"{summary['linkFirst']['modelCallsSaved']} model calls saved")
    lines += ["", head]
    if summary.get("healed"):
        lines.append(f"{summary['healed']} healed step{'' if summary['healed'] == 1 else 's'} wait for review: "
                     "mobster test heal")
    from .a11y_baseline import counts as a11y_counts
    found = a11y_counts(doc)
    if any(found.values()):
        lines.append(f"Accessibility: {found['new']} new, {found['warnings']} warning"
                     f"{'' if found['warnings'] == 1 else 's'}, {found['known']} in the baseline")
    if doc.get("a11yBaseline") is not None:
        lines.append(f"Wrote .mobster/a11y-baseline.json with {doc['a11yBaseline']} finding"
                     f"{'' if doc['a11yBaseline'] == 1 else 's'}.")
    if doc.get("stopped"):
        lines.append("Stopped before every check ran.")
    if paths.get("html"):
        lines.append(f"Report  {_relative(paths.get('htmlCopy') or paths['html'])}")
    if paths.get("junitCopy") or paths.get("junit"):
        lines.append(f"JUnit   {_relative(paths.get('junitCopy') or paths['junit'])}")
    return "\n".join(lines)


def _tell_slack(root, paths, doc, shared, err):
    """Post the suite to Slack when the project's rules ask (notify.py), and say what happened in one line."""
    if not paths.get("folder"):
        return
    from .notify import notify
    line = notify(root, paths["folder"], doc, link=getattr(shared, "link", None))
    if line:
        print(line, file=err, flush=True)


def pr(args, rest, *, run=None, out=None, err=None, cwd=None):
    """`mobster test pr [--base REF] [options]`: the checks the branch's changes need, built, run and reported."""
    from .api import Options, UsageError
    from .pr import PrError
    from .prrun import PrOptions
    out, err = out or sys.stdout, err or sys.stderr
    if rest:
        return _usage(f"pr takes no paths ({' '.join(rest)}): it picks the checks from the diff")
    if getattr(args, "record_run", None) or getattr(args, "record_name", None) or \
            getattr(args, "record_output", None):
        return _usage("--run, --name and -o go with record: mobster test record --run RUN_ID")
    for flag, name in (("--matrix", "matrix"), ("--shard", "shard"), ("--tag", "tag")):
        if getattr(args, name, None):
            return _usage(f"{flag} doesn't go with pr: pr picks the checks from the diff")
    options = Options(sims=tuple(args.sim or ()), devices=tuple(args.device or ()), parallel=args.parallel,
                      retries=args.retries, repeat=args.repeat, quarantine=args.quarantine, junit=args.junit,
                      html=args.html, strict=args.strict, keyless=args.keyless, video=args.video,
                      assert_timeout=args.assert_timeout, command=_command(args),
                      testflight=tuple(getattr(args, "testflight", None) or ()),
                      fresh=bool(getattr(args, "fresh", False)), no_heal=bool(getattr(args, "no_heal", False)),
                      auto_quarantine=bool(getattr(args, "auto_quarantine", False)),
                      spread=bool(getattr(args, "spread", False)),
                      xctest=bool(getattr(args, "xctest", False)), xctest_scheme=args.scheme,
                      xctest_project=args.project, xctest_plan=getattr(args, "test_plan", None))
    if _only_testing(args, options):
        return USAGE_EXIT
    pr_options = PrOptions(base=args.base, build=not args.no_build, scheme=args.scheme, project=args.project,
                           drafts=args.drafts or "run", gifs=not args.no_gifs, tested_with=args.tested_with,
                           github=getattr(args, "github", None) or "check")
    if run is None:
        from .prrun import test_pr as run
    from ..verify.checks import project_root
    root = project_root(Path(cwd or Path.cwd()))
    try:
        from ..devtools import ProgressLines
        progress = ProgressLines(err)
    except ImportError:
        def progress(line):
            print(line, file=err, flush=True)
    try:
        result = run(pr_options, options, root=root, progress=progress)
    except (PrError, UsageError) as error:
        return _usage(str(error))
    finally:
        getattr(progress, "end", lambda: None)()
    shared = None
    if getattr(args, "share", False) and result.paths.get("folder"):
        shared = _share(result.paths["folder"], args.share_days, err)
        if shared is not None:
            from ..paths import write_text_atomic
            from .pr_report import comment, hosted_pages, write
            try:
                # A write that fails leaves the comment linking the local files, and the exit code as it was.
                write({**result.pr, "hostedReport": shared.link}, result.paths["folder"])
                write_text_atomic(Path(result.paths["comment"]), comment(result.pr, artifact_url=shared.link,
                                                                         file_url=hosted_pages(shared.link)))
                result.pr["hostedReport"] = shared.link
            except (OSError, ValueError):
                print("mobster test: the report is shared, but its link couldn't be written into the comment.",
                      file=err, flush=True)
    _tell_slack(root, result.paths, result.doc, shared, err)
    if args.comment:
        target = Path(args.comment).expanduser()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(Path(result.paths["comment"]).read_text(encoding="utf-8"), encoding="utf-8")
        except OSError as error:
            return _usage(f"--comment {args.comment}: can't write it ({error.strerror or type(error).__name__})")
    if args.json:
        print(json.dumps(result.pr, ensure_ascii=False), file=out, flush=True)
    else:
        from ..style import color_enabled
        print(pr_text(result, color=color_enabled(out)), file=out, flush=True)
    return result.exit_code


def pr_text(result, color=False):
    """The human summary of `mobster test pr`: the changed screens, a line per check, the drafts, the verdict and where
    the comment and GIFs are."""
    pr = result.pr
    tones = TONES
    words = {"passed": "✓ passed", "failed": "✗ failed", "needs_review": "! needs review",
             "couldnt_run": "✗ couldn't run", "draft": "~ draft"}

    def paint(text, tone):
        return _paint(color)(text, tones[tone], "bold")

    lines = []
    screens = [screen.get("name") for screen in (pr.get("plan") or {}).get("screens") or ()]
    if screens:
        lines.append("Changed screens: " + ", ".join(screens[:8]) + (" …" if len(screens) > 8 else ""))
    for row in pr.get("rows") or ():
        status = row.get("status")
        if row.get("draft"):
            lines.append(f"{paint(words['draft'], 'draft')}  {row.get('file')}  on {dots(row.get('device') or '')}, "
                         f"{(status or '').replace('_', ' ')}")
            continue
        lines.append(f"{paint(words.get(status, status), status if status in tones else 'couldnt_run')}  "
                     f"{row.get('check')}  on {dots(row.get('device') or '')}")
        if status != "passed":
            lines.append(f"    {row.get('expected')}: {row.get('found')}")
    lines += xctest.text_lines(result.doc, paint)
    lines += ["", pr.get("headline") or ""]
    lines += list(pr.get("notes") or ())
    folder = result.paths.get("folder")
    if result.paths.get("comment"):
        lines.append(f"Comment  {_relative(result.paths['comment'])}")
    gifs = [row.get("gif") for row in pr.get("rows") or () if row.get("gif")]
    if gifs and folder:
        lines.append(f"GIFs     {_relative(Path(folder) / 'pr' / 'gifs')} ({len(gifs)})")
    return "\n".join(lines).strip("\n")


def add(args, rest, *, cwd=None, out=None, err=None):
    from . import recipes
    out, err = out or sys.stdout, err or sys.stderr
    if getattr(args, "add_list", False):
        if rest:
            return _usage("--list takes no recipe")
        width = max(len(name) for name in recipes.names())
        for recipe in recipes.BUILTIN:
            print(f"{recipe.name.ljust(width)}  {recipe.summary}", file=out)
        return 0
    if len(rest) != 1:
        return _usage("add takes one recipe: mobster test add RECIPE (mobster test add --list shows them)")
    given = {}
    for item in getattr(args, "add_set", None) or ():
        key, equals, value = item.partition("=")
        if not equals or not key.strip():
            return _usage(f"--set {item}: write it as KEY=VALUE")
        given[key.strip().replace("-", "_")] = value
    if getattr(args, "bundle", None):
        given["bundle"] = args.bundle
    try:
        recipe, data = recipes.build(rest[0], given)
    except recipes.RecipeError as error:
        return _usage(str(error))
    from ..verify.checks import project_root
    base = Path(cwd or os.getcwd())
    target = project_root(base) / ".mobster" / "checks" / f"{recipe.name}.yaml"
    if target.exists() and not getattr(args, "force", False):
        return _usage(f"{_relative(target)} exists, so Mobster left it as it is: pass --force to replace it")
    import yaml
    text = (f"# A Mobster check from the {recipe.name} recipe. Run it with: mobster test {_relative(target)}\n"
            + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False, width=100))
    try:
        write_text_atomic(target, text)
    except OSError as error:
        return _usage(f"{_relative(target)}: can't write it ({error.strerror or type(error).__name__})")
    print(f"Saved {_relative(target)} from the {recipe.name} recipe. Run it with: mobster test "
          f"{shlex.quote(_relative(target))}" + (f". {recipe.notes}" if recipe.notes else ""), file=err, flush=True)
    return 0


def record(args, rest, *, find=None, out=None, err=None):
    """`mobster test record --run RUN_ID [--name NAME] [-o PATH]`."""
    from .record import RecordError, check_data, check_yaml, find_run
    out, err = out or sys.stdout, err or sys.stderr
    if rest:
        return _usage(f"record takes no paths ({' '.join(rest)}); write the check where you like with -o PATH")
    run_id = getattr(args, "record_run", None)
    if not run_id:
        return _usage("record needs the task: mobster test record --run RUN_ID (the Mobster app's task id)")
    output = getattr(args, "record_output", None)
    name = getattr(args, "record_name", None)
    if name is not None and (not name.strip() or len(name) > LIMITS["name"]):
        return _usage(f"--name is 1 to {LIMITS['name']} characters")
    target = Path(output).expanduser() if output else None
    shown = _relative(target) if target else ".mobster/checks/<name>.yaml"
    try:
        found = (find or find_run)(run_id)
        data = check_data(found, name=name)
        text = check_yaml(found, name=name, path=shown)
    except RecordError as error:
        return _usage(error.message)
    if target is None:
        print(text, end="", file=out, flush=True)
        return 0
    if target.suffix.lower() not in (".yaml", ".yml"):
        return _usage(f"-o {output}: a check file ends in .yaml")
    existed = target.exists()
    try:
        write_text_atomic(target, text)
    except OSError as error:
        return _usage(f"-o {output}: can't write it ({error.strerror or type(error).__name__})")
    steps = len(data.get("steps") or ())
    print(f"{'Replaced' if existed else 'Saved'} {_relative(target)}: {steps} step{'' if steps == 1 else 's'}, "
          f"{len(data['expect'])} expectation{'' if len(data['expect']) == 1 else 's'}. Run it with: mobster test "
          f"{shlex.quote(_relative(target))}", file=err, flush=True)
    return 0


def import_flows(args, rest, *, out=None, err=None, cwd=None):
    """`mobster test import maestro PATH… [-o DIR]`: Maestro flows as checks."""
    from . import maestro
    out, err = out or sys.stdout, err or sys.stderr
    if not rest or rest[0] != "maestro":
        return _usage("import needs the format: mobster test import maestro .maestro")
    sources = rest[1:]
    if not sources:
        return _usage("import maestro needs the flows: mobster test import maestro .maestro")
    cwd = Path(cwd or os.getcwd())
    for flag, name in (("--run", "record_run"), ("--name", "record_name")):
        if getattr(args, name, None):
            return _usage(f"{flag} goes with record, not import")
    bundle = getattr(args, "bundle", None)
    if bundle:
        from ..state import validate_bundle_id
        try:
            validate_bundle_id(bundle)
        except ValueError:
            return _usage(f"--bundle {bundle!r} is not a bundle ID such as com.example.app")
    folder = Path(getattr(args, "record_output", None) or ".mobster/checks").expanduser()
    if not folder.is_absolute():
        folder = cwd / folder
    root = cwd
    app = getattr(args, "app", None)
    try:
        results = maestro.convert_tree([Path(item) if Path(item).is_absolute() else cwd / item for item in sources],
                                       bundle=bundle, app_path=app,
                                       skip_unsupported=getattr(args, "skip_unsupported", False))
    except maestro.MaestroError as error:
        return _usage(str(error))
    dry, force = getattr(args, "dry_run", False), getattr(args, "force", False)
    written, refused = _write_imports(results, folder, root, dry=dry, force=force)
    if getattr(args, "json", False):
        print(json.dumps({"flows": [item.to_dict() for item in results], "folder": _relative(folder, root)},
                         indent=2, ensure_ascii=False), file=out, flush=True)
    else:
        print(import_text(results, folder, root, dry=dry), file=out, flush=True)
    return 1 if refused else 0


def _write_imports(results, folder, root, *, dry, force):
    """Write every imported check; a check that exists is kept unless ``force``. (checks written, flows refused)."""
    from . import maestro
    refused = sum(1 for item in results if item.outcome == "refused")
    written = 0
    for item in results:
        if item.outcome != "imported":
            continue
        for stem, data in item.checks:
            target = folder / f"{stem}.yaml"
            text = maestro.render(data, item.source)
            if target.exists() and not force:
                if target.read_text(encoding="utf-8", errors="replace") == text:
                    item.same.append(stem)   # importing twice gives the same file: nothing to replace or report
                    continue
                item.notes.append(f"{_relative(target, root)} exists, so it was not replaced; use --force")
                item.outcome = "kept"
                continue
            if dry:
                continue
            try:
                write_text_atomic(target, text)
                written += 1
            except OSError as error:
                item.outcome = "refused"
                item.reason = f"can't write {_relative(target, root)} ({error.strerror or type(error).__name__})"
                refused += 1
                break
    return written, refused


def _import_table(results):
    rows = [(item.source, item.converted, len(item.placeholders),
             len(item.problems) if item.outcome == "refused" else 0) for item in results if item.outcome != "skipped"]
    if not rows:
        return []
    rows.append(("Total", *(sum(row[n] for row in rows) for n in (1, 2, 3))))
    width = max(len(row[0]) for row in rows + [("Flow",)])
    head = ("Flow", "Converted", "Needs a first run", "Unsupported")
    out = [f"{head[0]:<{width}}  {head[1]:>9}  {head[2]:>17}  {head[3]:>11}"]
    out += [f"{name:<{width}}  {a:>9}  {b:>17}  {c:>11}" for name, a, b, c in rows]
    return out


def import_text(results, folder, root, *, dry=False):
    """What the import did, flow by flow: the checks written, what was refused and why, and what to run."""
    lines = []
    imported = [item for item in results if item.outcome in ("imported", "kept")]
    refused = [item for item in results if item.outcome == "refused"]
    checks = sum(len(item.checks) for item in imported)
    where = _relative(folder, root)
    verb = "Would import" if dry else "Imported"
    lines.append(f"{verb} {len(imported)} of {len(results)} Maestro flow{'' if len(results) == 1 else 's'} "
                 f"({checks} check{'' if checks == 1 else 's'}) into {where}")
    for item in results:
        lines.append("")
        lines.append(item.source)
        if item.outcome == "skipped":
            lines.append(f"  skipped: {item.reason}")
        elif item.outcome == "refused":
            lines.append(f"  refused: {item.reason}" if item.reason
                         else "  refused: it has commands Mobster can't run, so no check was written")
            for problem in item.problems[:IMPORT_SHOWN]:
                lines.append(f"    {problem.where}: {problem.why}")
                if problem.instead:
                    lines.append(f"      instead: {problem.instead}")
            if len(item.problems) > IMPORT_SHOWN:
                lines.append(f"    and {len(item.problems) - IMPORT_SHOWN} more; fix these, then import again")
        else:
            for stem, data in item.checks:
                steps, expects = len(data.get("steps") or ()), len(data["expect"])
                verb = "unchanged" if stem in item.same else "would write" if dry else "wrote"
                lines.append(f"  {verb} {_relative(folder / (stem + '.yaml'), root)}: "
                             f"{steps} step{'' if steps == 1 else 's'}, "
                             f"{expects} expectation{'' if expects == 1 else 's'}")
        for problem in item.placeholders:
            lines.append(f"  needs a first run, {problem.where}: {problem.why}")
        for problem in item.dropped:
            lines.append(f"  dropped {problem.where}: {problem.why}")
            if problem.instead:
                lines.append(f"      instead: {problem.instead}")
        for note in item.notes:
            lines.append(f"  note: {note}")
    lines.append("")
    lines.extend(_import_table(results))
    lines.append("")
    if refused:
        lines.append(f"{len(refused)} flow{'' if len(refused) == 1 else 's'} refused: Mobster won't write a check "
                     "that covers less than the flow does. Fix the commands named above, or add --skip-unsupported "
                     "to drop the ones that aren't assertions and list each.")
    if checks and not dry:
        lines.append(f"Run them with: mobster test {shlex.quote(where)}")
    return "\n".join(lines)


def heal(args, rest, *, cwd=None, out=None, err=None):
    """`mobster test heal [CHECK...] [--accept | --reject] [--json]`: list or settle the heals waiting for review."""
    from ..replay_core import heal as heals
    from ..replay_core.checks import cache_dir_for
    from ..verify.checks import project_root
    from .discover import DiscoveryError, discover
    out, err = out or sys.stdout, err or sys.stderr
    accept, reject = bool(getattr(args, "accept", False)), bool(getattr(args, "reject", False))
    if accept and reject:
        return _usage("heal takes --accept or --reject, not both")
    cwd = Path(cwd or Path.cwd())
    root = project_root(cwd / rest[0] if rest and (cwd / rest[0]).exists() else cwd)
    try:
        entries = discover(_heal_paths(rest, root, cwd), root, cwd=cwd)
    except DiscoveryError as error:
        return _usage(str(error))
    cache = cache_dir_for(root / ".mobster" / "runs")
    found = []
    for entry in entries:
        check_file = Path(entry.path)
        try:
            document = heals.read(check_file)
        except heals.HealError as error:
            print(f"mobster test heal: {error}", file=err, flush=True)
            continue
        if document is not None:
            found.append((check_file, document))
    if getattr(args, "json", False) and not (accept or reject):
        print(json.dumps({"heals": [{"check": _relative(str(file)), "diff": _relative(str(heals.heal_file(file))),
                                     **document} for file, document in found]}, ensure_ascii=False, default=str),
              file=out, flush=True)
        return 0
    if not found:
        print("No heals wait for review.", file=out, flush=True)
        return 0
    if accept or reject:
        total = 0
        for check_file, _ in found:
            try:
                count = (heals.accept if accept else heals.reject)(check_file, cache)
            except heals.HealError as error:
                print(f"mobster test heal: {_relative(str(check_file))}: {error}", file=err, flush=True)
                continue
            total += count
            word = "Accepted" if accept else "Kept failing"
            print(f"{word}: {_relative(str(check_file))} ({count} step{'' if count == 1 else 's'})", file=out,
                  flush=True)
        if reject and total:
            print("Those steps now fail the check when their screen changes again.", file=out, flush=True)
        return 0
    lines = []
    for check_file, document in found:
        app = document.get("app") or {}
        where = ", ".join(filter(None, [f"run {document.get('run')}" if document.get("run") else None,
                                        f"app {app.get('version')}" if isinstance(app, dict) and app.get("version")
                                        else None, f"iOS {document.get('ios')}" if document.get("ios") else None]))
        lines.append(f"{_relative(str(check_file))}" + (f"  ({where})" if where else ""))
        for item in document["heals"]:
            if not isinstance(item, dict):
                continue
            lines.append(f"  step {item.get('step')}  {item.get('from')}  →  {item.get('to')}")
            if item.get("why"):
                lines.append(f"          {item['why']}")
            for name in ("before", "after"):
                frame = (item.get("frames") or {}).get(name)
                if frame:
                    lines.append(f"          {name:<6} {frame}")
    count = sum(len(document["heals"]) for _, document in found)
    lines += ["", f"{count} heal{'' if count == 1 else 's'} wait{'s' if count == 1 else ''} for review. Accept: "
                  "mobster test heal --accept   Keep failing: mobster test heal --reject"]
    print("\n".join(lines), file=out, flush=True)
    return 0


def _heal_paths(rest, root, cwd):
    """Check paths for `heal`: as given (a name like "paywall" is .mobster/checks/paywall.yaml), else none (all)."""
    out = []
    for item in rest:
        path = Path(item)
        if not path.suffix and not (cwd / path).exists():
            for suffix in (".yaml", ".yml", ".json"):
                candidate = root / ".mobster" / "checks" / (item + suffix)
                if candidate.exists():
                    path = candidate
                    break
        out.append(str(path))
    return out


NIGHTLY_FLAGS = (("sim", "--sim"), ("device", "--device"), ("tag", "--tag"), ("testflight", "--testflight"))


def _night_flags(args):
    """The `mobster test` flags `nightly on` keeps for each night's run."""
    flags = []
    for name, flag in NIGHTLY_FLAGS:
        for value in getattr(args, name, None) or ():
            flags += [flag, value]
    if getattr(args, "parallel", 1) and args.parallel > 1:
        flags += ["--parallel", str(args.parallel)]
    if getattr(args, "retries", None) is not None:
        flags += ["--retries", str(args.retries)]
    for name, flag in (("keyless", "--keyless"), ("spread", "--spread")):
        if getattr(args, name, False):
            flags.append(flag)
    for value in getattr(args, "audit", None) or ():
        flags += ["--audit", value]
    if getattr(args, "a11y", None):
        flags.append(f"--a11y={args.a11y}")
    return flags


def nightly(args, rest, *, cwd=None, out=None, err=None):
    """`mobster test nightly on --at HH:MM [flags] | off | status | run [flags]`."""
    from ..devkit import DevError
    from ..devkit import nightly as night
    from ..verify.checks import project_root
    out, err = out or sys.stdout, err or sys.stderr
    verb = rest[0] if rest else "status"
    root = project_root(Path(cwd or Path.cwd()))
    try:
        if verb == "on":
            if not getattr(args, "at", None):
                return _usage("nightly on needs --at HH:MM, such as --at 02:00")
            path = night.turn_on(root, args.at, _night_flags(args))
            print(f"Every night at {args.at}, Mobster runs the checks of {root.name}"
                  + (f" ({' '.join(_night_flags(args))})" if _night_flags(args) else "")
                  + f". The morning report goes to {night.FOLDER}/<date>/morning.md, with a notification.\n"
                  f"Keep this Mac on power and signed in. Stop it with: mobster test nightly off\n({path})",
                  file=out, flush=True)
            return 0
        if verb == "off":
            print("Turned the nightly run off." if night.turn_off(root) else "No nightly run is set up here.",
                  file=out, flush=True)
            return 0
        if verb == "run":
            code, text = night.run_night(root, _night_flags(args))
            print(text, file=out, flush=True)
            return code
        if verb == "status":
            state = night.status(root)
            if getattr(args, "json", False):
                print(json.dumps(state), file=out, flush=True)
                return 0
            lines = [f"Every night at {state['at']}" + (f" ({' '.join(state['flags'])})" if state["flags"] else "")
                     if state["on"]
                     else "No nightly run is set up here. Turn it on: mobster test nightly on --at 02:00"]
            if state["last"]:
                lines.append(f"Last run {state['last'].get('date')}: {state['last'].get('headline')}. "
                             f"{state['last'].get('morning')}")
            print("\n".join(lines), file=out, flush=True)
            return 0
    except DevError as error:
        return _usage(error.line())
    return _usage(f"nightly takes on, off, status or run, not {verb}")


__all__ = ["add_arguments", "run", "summary_text", "suite", "pr", "pr_text", "add", "record", "import_flows", "heal",
           "nightly"]
