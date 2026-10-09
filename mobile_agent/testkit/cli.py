"""`mobster test`: run a project's checks on simulators and iPhones, with retries, flake detection and reports.

    mobster test                                   every check in .mobster/checks, each on its own simulator
    mobster test --sim "iPhone 17 Pro" --sim "iPhone SE (3rd generation)" --parallel 2
    mobster test --device "Sam's iPhone"           a real iPhone: only builds you installed there
    mobster test --junit build/junit.xml --html build/mobster-report
    mobster test record --run 3f9c2a1b7d0e --name paywall -o .mobster/checks/paywall.yaml

Exit codes: 0 passed, 1 failed (or flaky with --strict), 2 needs review, 3 couldn't run (usage errors too: 2 already
means needs review), 130 ctrl+c. Progress goes to stderr; stdout gets the summary, or results.json with --json.
"""

import json
import os
from pathlib import Path
import shlex
import sys

USAGE_EXIT = 3
RECORD = "record"


def add_arguments(parser, helpers):
    """`mobster test`'s flags. ``helpers``: env_option, path_type and bounded from __main__."""
    bounded = helpers["bounded"]
    parser.add_argument("paths", nargs="*", metavar="PATH",
                        help="check files or folders of them (default: .mobster/checks); `record` makes a check "
                             "from a task instead")
    where = parser.add_argument_group("where checks run")
    where.add_argument("--sim", action="append", metavar="TYPE[@RUNTIME]",
                       help="a simulator device type, with an optional runtime, such as \"iPhone 17 Pro@iOS 26.4\"; "
                            "repeatable (default: each check's own, or .mobster/matrix.yaml)")
    where.add_argument("--device", action="append", metavar="NAME",
                       help="a device from `mobster devices`; repeatable. On a real iPhone, checks drive only "
                            "builds you installed, never App Store apps")
    where.add_argument("--matrix", metavar="FILE",
                       help="a YAML file of simulators to run every check on (simulators only)")
    where.add_argument("--parallel", type=bounded(int, 1, 8), default=1, metavar="N",
                       help="simulators running checks at once (default: 1); each iPhone runs one check at a time")
    how = parser.add_argument_group("retries and flakes")
    how.add_argument("--retries", type=bounded(int, 0, 5), metavar="N",
                     help="attempts after a failure, from a fresh app (default: 1, or 0 with --repeat)")
    how.add_argument("--repeat", type=bounded(int, 1, 20), default=1, metavar="K",
                     help="run each check K times and report its pass rate (flake hunting)")
    how.add_argument("--strict", action="store_true", help="a flaky check fails the run")
    how.add_argument("--quarantine", metavar="FILE",
                     help="checks that run and are reported but never fail the run (default: "
                          ".mobster/quarantine.yaml)")
    which = parser.add_argument_group("which checks")
    which.add_argument("--tag", action="append", metavar="TAG", help="only checks with this tag; repeatable")
    which.add_argument("--shard", metavar="I/N", help="run the I-th of N even shards, such as 2/4")
    which.add_argument("--keyless", action="store_true",
                       help="never call a model: checks with steps couldn't run")
    out = parser.add_argument_group("reports")
    out.add_argument("--junit", metavar="FILE", help="also write the JUnit XML here")
    out.add_argument("--html", metavar="DIR", help="also write the HTML report (with each run's report) here")
    out.add_argument("--video", action="store_true", help="record each simulator run's screen into the report")
    out.add_argument("--json", action="store_true", help="print results.json on stdout instead of the summary")
    out.add_argument("--assert-timeout", type=bounded(float, 0, 60), default=5, metavar="S",
                     help="how long expectations may wait for the screen to settle (default: 5)")
    record = parser.add_argument_group("mobster test record")
    record.add_argument("--run", dest="record_run", metavar="RUN_ID", help="the finished task to make a check from")
    record.add_argument("--name", dest="record_name", metavar="TEXT", help="the check's name (default: the task)")
    record.add_argument("-o", "--output", dest="record_output", metavar="PATH",
                        help="write the check here (default: print it)")
    helpers["env_option"](parser)
    parser._option_string_actions["--env-file"].help = ("load KEY=VALUE lines, such as OPENAI_API_KEY or "
                                                        "ANTHROPIC_API_KEY for Smart steps; variables already set "
                                                        "win; default: $MOBSTER_ENV_FILE")
    _usage_exits_3(parser)


def _usage_exits_3(parser):
    """argparse's usage errors exit 2, which `test` uses for needs review: they exit 3 here. Paths written between
    options are kept as paths (`mobster test a.yaml --sim X b.yaml`)."""
    parse_known = parser.parse_known_args

    def parse_known_args(args=None, namespace=None):
        namespace, extras = parse_known(args, namespace)
        stray = [item for item in extras if item.startswith("-")]
        if stray:
            parser.error(f"unrecognized arguments: {' '.join(stray)}")
        if extras:
            namespace.paths = list(getattr(namespace, "paths", None) or []) + extras
        return namespace, []

    def error(message):
        parser.print_usage(sys.stderr)
        hint = ""
        try:
            from ..devtools import option_hint
            hint = option_hint(parser, message)
        except ImportError:
            pass
        print(f"{parser.prog}: error: {message}{hint}", file=sys.stderr, flush=True)
        raise SystemExit(USAGE_EXIT)
    parser.parse_known_args = parse_known_args
    parser.error = error


def _isatty(stream):
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def _usage(message):
    print(f"mobster test: {message}", file=sys.stderr, flush=True)
    return USAGE_EXIT


def run(args):
    """`mobster test` for parsed ``args``; returns the exit code. Never raises into __main__."""
    from ..devtools import signals_as_interrupt
    paths = list(getattr(args, "paths", None) or ())
    if paths and paths[0] == RECORD:
        return record(args, paths[1:])
    if getattr(args, "record_run", None) or getattr(args, "record_name", None) or getattr(args, "record_output", None):
        return _usage("--run, --name and -o go with record: mobster test record --run RUN_ID")
    with signals_as_interrupt():
        try:
            return suite(args, paths)
        except KeyboardInterrupt:
            print("mobster test: stopped", file=sys.stderr, flush=True)
            return 130
        except Exception as error:  # a last resort: never a traceback, never "failed" (exit 1)
            print(f"mobster test: unexpected error ({type(error).__name__}: {error}). Please report it with the "
                  "command you ran.", file=sys.stderr, flush=True)
            return USAGE_EXIT


def _command(args):
    return "mobster test " + " ".join(shlex.quote(item) for item in sys.argv[2:]) if len(sys.argv) > 2 and \
        sys.argv[1] == "test" else "mobster test"


def suite(args, paths, *, execute=None, plan=None, out=None, err=None):
    from .api import Options, UsageError
    out, err = out or sys.stdout, err or sys.stderr
    options = Options(paths=tuple(paths), sims=tuple(args.sim or ()), devices=tuple(args.device or ()),
                      matrix=args.matrix, parallel=args.parallel, retries=args.retries, repeat=args.repeat,
                      quarantine=args.quarantine, tags=tuple(args.tag or ()), shard=args.shard, junit=args.junit,
                      html=args.html, strict=args.strict, keyless=args.keyless, video=args.video,
                      assert_timeout=args.assert_timeout, command=_command(args))
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
    if not runs:
        print("No checks to run.", file=out, flush=True)
        return 0
    progress(f"mobster test: {len(planned.entries)} check{'' if len(planned.entries) == 1 else 's'} on "
             f"{_targets(planned.targets)}" + (f", {options.repeat} times each" if options.repeat > 1 else "")
             + (f", {options.parallel} at a time" if options.parallel > 1 else ""))
    try:
        doc, report_paths = execute(planned, progress=progress)
    finally:
        getattr(progress, "end", lambda: None)()
    if args.json:
        try:
            print(json.dumps(doc, ensure_ascii=False, allow_nan=False), file=out, flush=True)
        except BrokenPipeError:
            os.dup2(os.open(os.devnull, os.O_WRONLY), out.fileno())
    else:
        from ..console import color_enabled
        print(summary_text(doc, report_paths, color=color_enabled(out)), file=out, flush=True)
    return int(doc.get("exitCode", USAGE_EXIT))


def _targets(targets):
    if len(targets) == 1:
        return targets[0].label if targets[0].kind != "default" else "each check's simulator"
    return f"{len(targets)} devices"


def _relative(path):
    if not path:
        return path
    try:
        return str(Path(path).resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def summary_text(doc, paths, color=False):
    """The human summary: one line per check and device that didn't pass cleanly, the counts, the reports."""
    from .reports import _counts, _duration
    tones = {"passed": "32", "failed": "31", "needs_review": "33", "couldnt_run": "90", "flaky": "33"}
    words = {"passed": "✓ passed", "failed": "✗ failed", "needs_review": "! needs review",
             "couldnt_run": "✗ couldn't run", "flaky": "~ flaky"}

    def paint(text, tone):
        return f"\033[1;{tones[tone]}m{text}\033[0m" if color else text

    lines = []
    for check in doc.get("checks") or ():
        for result in check.get("results") or ():
            status = result.get("status")
            tone = "flaky" if status == "passed" and result.get("flaky") else status
            line = f"{paint(words.get(tone, status), tone)}  {check.get('name')}  ·  {result.get('device')}"
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
    summary = doc.get("summary") or {}
    head = f"{_counts(summary)} · {_duration(doc.get('durationMs'))}"
    if summary.get("costUsd"):
        head += f" · ${summary['costUsd']:.2f}"
    lines += ["", head]
    if doc.get("stopped"):
        lines.append("Stopped before every check ran.")
    if paths.get("html"):
        lines.append(f"Report  {_relative(paths.get('htmlCopy') or paths['html'])}")
    if paths.get("junitCopy") or paths.get("junit"):
        lines.append(f"JUnit   {_relative(paths.get('junitCopy') or paths['junit'])}")
    return "\n".join(lines)


# -- record ------------------------------------------------------------------------------------------------------

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
    if name is not None and (not name.strip() or len(name) > 120):
        return _usage("--name is 1 to 120 characters")
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
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, target)
    except OSError as error:
        return _usage(f"-o {output}: can't write it ({error.strerror or type(error).__name__})")
    steps = len(data.get("steps") or ())
    print(f"{'Replaced' if existed else 'Saved'} {_relative(target)}: {steps} step{'' if steps == 1 else 's'}, "
          f"{len(data['expect'])} expectation{'' if len(data['expect']) == 1 else 's'}. Run it with: mobster test "
          f"{shlex.quote(_relative(target))}", file=err, flush=True)
    return 0


__all__ = ["add_arguments", "run", "summary_text", "record", "suite"]
