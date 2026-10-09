"""`mobster verify`: check an iOS app on a simulator Mobster manages and print the verdict.

    mobster verify --bundle com.apple.Preferences --text General
    mobster verify --app Daybreak.app --open-url daybreak://paywall --text "Choose your plan"
    mobster verify "Open General, then About" --bundle com.apple.Preferences --text "iOS Version"
    mobster verify --check .mobster/checks/paywall.yaml

Exit codes: 0 passed, 1 failed, 2 needs review, 3 couldn't run (including usage errors: 2 already means needs
review), 130 ctrl+c. stdout is one JSON object with --json or in a pipe, else a short summary; progress goes to
stderr.
"""

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import sys

from .checks import RESETS, CheckError, check_from_dict, load_check

USAGE_EXIT = 3
KINDS = {"--text": "text", "--no-text": "no_text", "--visible": "visible", "--absent": "absent", "--expect": "expect"}
SELECTOR_KEYS = ("id", "label", "value", "role", "enabled", "selected")
# Flags that describe a check, and so can't be combined with --check.
CHECK_FLAGS = ("steps", "expectations", "launch_arg", "launch_env", "open_url", "name")


class _Expectation(argparse.Action):
    """--text, --no-text, --visible, --absent and --expect, kept in the order they were given."""

    def __call__(self, parser, namespace, values, option_string=None):
        items = list(getattr(namespace, self.dest, None) or [])
        items.append((KINDS[option_string], values))
        setattr(namespace, self.dest, items)


def add_arguments(parser, helpers):
    """`mobster verify`'s flags on ``parser``. ``helpers``: env_option, path_type and bounded from __main__."""
    path_type, bounded = helpers["path_type"], helpers["bounded"]
    parser.add_argument("steps", nargs="*", metavar="STEP",
                        help="one step of the flow in plain English; none means a launch-only check")
    parser.add_argument("--check", type=path_type, metavar="FILE",
                        help="run a saved check (.yaml or .json); not combined with steps or expectations")
    parser.add_argument("--app", dest="app_path", type=path_type, metavar="PATH",
                        help="the .app built for the iOS Simulator; it is installed before the run")
    parser.add_argument("--bundle", metavar="ID", help="the app's bundle ID (default: read from --app)")
    parser.add_argument("--device", metavar="NAME",
                        help="a device from `mobster devices` (its id, UDID or name), or the simulator device type "
                             "(default: iPhone 17 Pro, else the newest iPhone)")
    parser.add_argument("--runtime", metavar="NAME",
                        help='the iOS runtime, such as "iOS 26.4" (default: the newest installed)')
    parser.add_argument("--reset", choices=RESETS, metavar="LEVEL",
                        help="none, data or reinstall (default: reinstall with --app, else data; com.apple.* apps "
                             "are never reset)")
    parser.add_argument("--launch-arg", action="append", metavar="ARG",
                        help="a launch argument; repeatable (write --launch-arg=-Flag for one that starts with -)")
    parser.add_argument("--launch-env", action="append", metavar="KEY=VALUE",
                        help="a launch environment variable; repeatable")
    parser.add_argument("--open-url", metavar="URL", help="a deep link opened after launch")
    parser.add_argument("--text", action=_Expectation, dest="expectations", metavar="TEXT",
                        help="expect TEXT on screen; repeatable")
    parser.add_argument("--no-text", action=_Expectation, dest="expectations", metavar="TEXT",
                        help="expect TEXT not on screen; repeatable")
    parser.add_argument("--visible", action=_Expectation, dest="expectations", metavar="SEL",
                        help="expect an element on screen: key=value[,key=value] with keys id, label, value, role, "
                             "enabled and selected; repeatable")
    parser.add_argument("--absent", action=_Expectation, dest="expectations", metavar="SEL",
                        help="expect no element to match SEL; repeatable")
    parser.add_argument("--expect", action=_Expectation, dest="expectations", metavar="JSON",
                        help='any expectation as JSON, such as \'{"count": {"id": "/^plan_/"}, "equals": 3}\'; '
                             "repeatable")
    parser.add_argument("--name", metavar="TEXT", help='the check\'s name in reports (default: the first step, '
                                                       'or "Launch check")')
    parser.add_argument("--keyless", action="store_true",
                        help="never call a model; a check with steps then can't run")
    parser.add_argument("--max-usd", type=bounded(float, 0, 1.0, above=True), metavar="USD",
                        help="Smart's spend cap for the run (default: 0.25, at most 1.00)")
    parser.add_argument("--max-seconds", type=bounded(float, 0, 600, above=True), metavar="S",
                        help="Smart's time limit for the flow (default: 180, at most 600)")
    parser.add_argument("--assert-timeout", type=bounded(float, 0, 60), default=5, metavar="S",
                        help="how long expectations may wait for the screen to settle (default: 5)")
    parser.add_argument("--out", type=path_type, metavar="DIR", help="where runs go (default: ./.mobster/runs)")
    parser.add_argument("--save", metavar="NAME",
                        help="also save the check as .mobster/checks/NAME.yaml (a-z, 0-9 and -)")
    parser.add_argument("--json", action="store_true",
                        help="print the result as JSON (the default when stdout isn't a terminal)")
    helpers["env_option"](parser)
    # The shared text names the Mac app's keys; the one key verify reads is Smart's (OpenAI's or Anthropic's).
    parser._option_string_actions["--env-file"].help = ("load KEY=VALUE lines, such as OPENAI_API_KEY or "
                                                        "ANTHROPIC_API_KEY for Smart; variables already set win; "
                                                        "default: $MOBSTER_ENV_FILE")
    _remap_usage_errors(parser)


# -- usage errors exit 3 -----------------------------------------------------------------------------------------

def _remap_usage_errors(parser):
    """argparse's usage errors exit 2, which `verify` uses for needs review: they exit 3 here, and with --json (or
    in a pipe) print a couldnt_run result of class usage. Steps written after options are kept as steps."""
    parse_known = parser.parse_known_args

    def parse_known_args(args=None, namespace=None):
        parser._mobster_argv = list(sys.argv[1:] if args is None else args)
        namespace, extras = parse_known(args, namespace)
        stray = [item for item in extras if item.startswith("-")]
        if stray:
            parser.error(f"unrecognized arguments: {' '.join(stray)}")
        if extras:
            namespace.steps = list(getattr(namespace, "steps", None) or []) + extras
        return namespace, []

    def error(message):
        parser.print_usage(sys.stderr)
        print(f"{parser.prog}: error: {message}{_option_hint(parser, message)}", file=sys.stderr, flush=True)
        if "--json" in getattr(parser, "_mobster_argv", ()) or not _isatty(sys.stdout):
            _print_json(usage_result(message))
        raise SystemExit(USAGE_EXIT)

    parser.parse_known_args = parse_known_args
    parser.error = error


def _option_hint(parser, message):
    """" Did you mean --text?" for "unrecognized arguments: --tex" (stderr only: the JSON keeps argparse's words)."""
    try:
        from ..devtools import option_hint
    except ImportError:
        return ""
    return option_hint(parser, message)


def _isatty(stream):
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def usage_result(message, fix=None):
    """The result JSON for a check that never started: couldnt_run, class usage, no run folder."""
    from .. import __version__
    from .runner import SCHEMA
    return {"schema": SCHEMA, "run_id": None, "verdict": "couldnt_run", "exit_code": USAGE_EXIT, "summary": message,
            "reason": {"class": "usage", "message": message, "fix": fix}, "mode": None, "check": None,
            "app": None, "device": None, "assertions": [], "baseline": {"taken": False, "held": []}, "stable": None,
            "steps": [], "agent": None, "frames": [], "proof": [], "alert": None, "report": None,
            "draft_check": None, "run_dir": None, "repro": None, "seconds": 0, "timing": {}, "cost_usd": 0,
            "mobster": {"version": __version__}}


# -- building the check ------------------------------------------------------------------------------------------

def parse_selector_flag(text, flag="--visible"):
    """``key=value[,key=value]`` as a selector object. enabled and selected take true or false."""
    selector = {}
    for part in str(text).split(","):
        key, sep, value = part.partition("=")
        key = key.strip()
        if not sep or not key:
            raise CheckError(f"{flag} {text!r}: write key=value pairs such as label=Continue,role=button")
        if key not in SELECTOR_KEYS:
            raise CheckError(f"{flag} {text!r}: {key!r} is not a selector key; use {', '.join(SELECTOR_KEYS)}")
        if key in selector:
            raise CheckError(f"{flag} {text!r}: {key} is given twice")
        if key in ("enabled", "selected"):
            word = value.strip().lower()
            if word not in ("true", "false"):
                raise CheckError(f"{flag} {text!r}: {key} must be true or false")
            selector[key] = word == "true"
        else:
            selector[key] = value
    return selector


def _expectation(kind, value):
    """One expectation flag as an assertion object, checked here so an error names the flag."""
    from .assertions import expand_assertion, parse_matcher, parse_selector
    flag = "--" + kind.replace("_", "-")
    if kind in ("text", "no_text"):
        parse_matcher(value, flag)
        return {kind: value}
    if kind in ("visible", "absent"):
        selector = parse_selector_flag(value, flag)
        parse_selector(selector, flag)
        return {kind: selector}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise CheckError(f"--expect {value!r} is not JSON ({error.msg})") from None
    if not isinstance(parsed, dict):
        raise CheckError(f"--expect {value!r} must be a JSON object such as {{\"text\": \"Continue\"}}")
    expand_assertion(parsed, "--expect")
    return parsed


def _launch_env(items):
    env = {}
    for item in items or ():
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise CheckError(f"--launch-env {item!r}: write KEY=VALUE")
        env[key] = value
    return env


def build_check(args):
    """(the Check, the run mode) from the flags. Raises CheckError."""
    mode = "launch" if args.keyless else "auto"
    if args.check is not None:
        given = []
        for name in CHECK_FLAGS:
            value = getattr(args, name, None)
            if not value:
                continue
            if name == "steps":
                given.append("STEP")
            elif name == "expectations":
                flags = {kind: flag for flag, kind in KINDS.items()}
                given.extend(dict.fromkeys(flags[kind] for kind, _ in value))
            else:
                given.append("--" + name.replace("_", "-"))
        if given:
            raise CheckError(f"--check can't be combined with {', '.join(given)}; put them in the check file")
        check = load_check(args.check)
        overrides = {}
        if args.app_path is not None:
            overrides["app_path"] = str(Path(args.app_path).expanduser().resolve())
        if args.bundle is not None:
            from ..state import validate_bundle_id
            try:
                overrides["bundle_id"] = validate_bundle_id(args.bundle)
            except ValueError:
                raise CheckError(f"--bundle {args.bundle!r} is not a bundle ID such as com.example.app") from None
        for flag in ("device", "runtime", "reset"):
            if getattr(args, flag) is not None:
                overrides[flag] = getattr(args, flag)
        if args.max_usd is not None:
            overrides["max_usd"] = float(args.max_usd)
        if args.max_seconds is not None:
            overrides["max_seconds"] = float(args.max_seconds)
        return (replace(check, **overrides) if overrides else check), mode
    data = {"steps": list(args.steps or ()),
            "expect": [_expectation(kind, value) for kind, value in (args.expectations or ())]}
    for key, value in (("name", args.name), ("app_path", str(args.app_path) if args.app_path else None),
                       ("bundle_id", args.bundle), ("device", args.device), ("runtime", args.runtime),
                       ("reset", args.reset), ("launch_args", args.launch_arg),
                       ("launch_env", _launch_env(args.launch_env) or None), ("open_url", args.open_url),
                       ("max_usd", args.max_usd), ("max_seconds", args.max_seconds)):
        if value is not None:
            data[key] = value
    if "app_path" not in data and "bundle_id" not in data:
        raise CheckError("name the app: --bundle ID for an installed app, or --app PATH for a .app to install")
    return check_from_dict(data, base_dir=Path.cwd()), mode


# -- output ------------------------------------------------------------------------------------------------------

def _print_json(value):
    try:
        print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())


def _relative(path):
    if not path:
        return path
    try:
        return str(Path(path).resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def summary_text(result, color=False):
    """The human summary (§5.2)."""
    marks = {"passed": ("✓ passed", "32"), "failed": ("✗ failed", "31"), "needs_review": ("! needs review", "33"),
             "couldnt_run": ("✗ couldn't run", "90")}
    word, tone = marks.get(result.get("verdict"), ("? unknown", "90"))
    if color:
        word = f"\033[1;{tone}m{word}\033[0m"
    name = (result.get("check") or {}).get("name")
    lines = ["  ".join(filter(None, [word, name, f"({result.get('seconds', 0)} s)"]))]
    for item in result.get("assertions") or ():
        mark = "✓" if item.get("ok") else "✗"
        lines.append(f"  {mark} {item.get('text')}" + ("" if item.get("ok") else f": {item.get('observed')}"))
    reason = result.get("reason") or {}
    if result.get("verdict") != "passed" and reason.get("message") and not (
            reason.get("class") == "assertion" and result.get("assertions")):
        lines.append(f"  {reason['message']}")
    if reason.get("fix"):
        lines.append(f"  {reason['fix']}")
    if result.get("report"):
        lines.append(f"  Report  {_relative(result['report'])}")
    if result.get("draft_check"):
        lines.append(f"  Rerun   mobster verify --check {_relative(result['draft_check'])}")
    return "\n".join(lines)


def _checks_dir(out):
    """Where --save writes: the parent of --out's .mobster, or the working directory's .mobster."""
    if out is not None:
        out = Path(out).expanduser().resolve()
        for folder in (out, *out.parents):
            if folder.name == ".mobster":
                return folder / "checks"
    return Path.cwd() / ".mobster" / "checks"


def run(args):
    """Run `mobster verify` for parsed ``args``; returns the exit code. Never raises into __main__."""
    as_json = bool(getattr(args, "json", False)) or not _isatty(sys.stdout)
    if getattr(args, "app", None):
        # The terminal UI's --app, given before the command's name.
        return _usage("--app goes after the command: mobster verify --app PATH", as_json)
    try:
        check, mode = build_check(args)
        if args.save is not None:
            from .runner import SAVE_NAME
            if not SAVE_NAME.fullmatch(args.save):
                raise CheckError(f"--save {args.save!r}: a check's name is 1 to 60 of a-z, 0-9 and -, starting "
                                 "with a letter or digit")
    except CheckError as error:
        return _usage(str(error), as_json)
    except RecursionError:
        return _usage("the check is nested too deeply", as_json)
    except Exception as error:  # a last resort: a bad input is a usage error (exit 3), never "failed" (exit 1)
        return _usage(f"the check can't be read ({type(error).__name__})", as_json)
    from .runner import save_check_file, verify
    try:
        from ..devtools import ProgressLines
        progress = ProgressLines(sys.stderr)
    except ImportError:
        def progress(line):
            print(line, file=sys.stderr, flush=True)
    return _run(args, check, mode, as_json, progress, verify, save_check_file)


def _run(args, check, mode, as_json, progress, verify, save_check_file):
    runs_dir = Path(args.out).expanduser() if args.out is not None else Path.cwd() / ".mobster" / "runs"
    problem = _unwritable(runs_dir)
    if problem:
        flag = "--out" if args.out is not None else "the runs folder (pass --out DIR)"
        return _usage(f"{flag}: can't write runs to {runs_dir} ({problem})", as_json)
    try:
        try:
            resolved = _device(args, check, progress, as_json)
            if isinstance(resolved, int):
                return resolved
            check, manager = resolved
            result = verify(check, mode=mode, runs_dir=runs_dir, progress=progress,
                            assert_timeout=args.assert_timeout, manager=manager)
        finally:
            getattr(progress, "end", lambda: None)()  # a "Still …" line on a terminal, before any other output
    except KeyboardInterrupt as stop:
        signum = getattr(stop, "signum", None)
        code = 130 if signum is None else 128 + int(signum)
        how = "with ctrl+c" if signum is None else f"by {stop}"
        print(f"mobster verify: stopped {how}", file=sys.stderr, flush=True)
        if as_json:
            result = getattr(stop, "result", None) or _couldnt_run("stopped", f"The run was stopped {how}.")
            _print_json({**result, "exit_code": code})
        return code
    except Exception as error:  # verify() handles its own; this is a last resort, never a traceback
        message = f"unexpected error ({type(error).__name__}: {error})"
        print(f"mobster verify: {message}", file=sys.stderr, flush=True)
        if as_json:
            _print_json(_couldnt_run("internal", f"Mobster hit an {message}.",
                                     "Please report it with the command you ran."))
        return USAGE_EXIT
    if args.save is not None and result.get("run_id"):
        target = _checks_dir(args.out) / f"{args.save}.yaml"
        existed = target.exists()
        try:
            path = save_check_file(check, args.save, target.parent)
            progress(f"{'Replaced' if existed else 'Saved'} {_relative(path)}")
        except (OSError, CheckError) as error:
            progress(f"mobster verify: the check was not saved ({error})")
    if as_json:
        _print_json(result)
    else:
        from ..console import color_enabled
        print(summary_text(result, color=color_enabled(sys.stdout)), flush=True)
    return int(result.get("exit_code", USAGE_EXIT))


def _device(args, check, progress, as_json):
    """(check, manager) for the check's device, or a usage error's exit code. A device from `mobster devices` (an
    id, UDID or name) runs the check on it; any other --device is a simulator device type, as before."""
    if not check.device:
        return check, None
    from ..device_targets import manager_for, named_device
    from ..devices import AmbiguousDevice
    try:
        record = named_device(check.device)
    except AmbiguousDevice as error:
        return _usage(str(error), as_json)
    if record is None:
        return check, None
    if record.get("kind") != "simulator" and args.check is not None and getattr(args, "device", None) is None:
        # A check file travels with a repository: it may pick a simulator, never someone's iPhone.
        return _usage(f"{args.check} names “{record['name']}”, a real device, and a check file may name only a "
                      f"simulator. To run it there, pass --device \"{record['name']}\".", as_json)
    if record.get("kind") != "simulator" and check.app_path:
        return _usage(f"“{record['name']}” runs apps already on it: pass --bundle ID, not --app", as_json)
    manager = manager_for(record, progress=progress)
    if record.get("kind") != "simulator" and check.reset == "auto":
        check = replace(check, reset="none")  # an app's data is never cleared on a real iPhone
    return check, manager


def _unwritable(folder):
    """Why runs can't be written under ``folder`` (a few words), or "" when they can. Creates it, as the run
    would."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        return error.strerror or type(error).__name__
    if not folder.is_dir():
        return "not a folder"
    if not os.access(folder, os.W_OK | os.X_OK):
        return "permission denied"
    return ""


def _couldnt_run(klass, message, fix=None):
    """The result JSON for a run that ended before it had a result of its own: couldnt_run with ``klass``."""
    result = usage_result(message, fix)
    result["reason"]["class"] = klass
    return result


def _usage(message, as_json):
    print(f"mobster verify: {message}", file=sys.stderr, flush=True)
    if as_json:
        _print_json(usage_result(message))
    return USAGE_EXIT
