"""The `mobster` command.

`mobster` on its own opens Mobster in this terminal (the terminal UI); `mobster "TASK"` opens it and runs TASK.
`login`, `doctor`, `setup` and `devices` get a Mac and an iPhone ready; `chat`, `run`, `memory`, `history`,
`export` and `screen` are your tasks; `test`, `verify`, `mcp` and `sim` check the app you're building (devtools.py
registers them and the other track commands). `serve` is the local API Mobster for Mac runs as its sidecar.

Start-up is kept short: `--version` and `--help` return before the argument parser is built, and a command's own
module is imported only when that command runs or its help is asked for (P1-1 of the CLI polish brief).
"""

import argparse
import os
import sys

from . import __version__
from . import links
from .extensions import load as load_extensions
from .paths import build_dir, source_checkout, user_data_dir

DOCS = links.DOCS
DEFAULT_WDA_URL = "http://127.0.0.1:8100"
# Exit codes: 0 done, 1 the task or check did not succeed, 2 usage, 3 couldn't run (no phone, no key).
EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_NO_DEVICE = 0, 1, 2, 3
EXIT_COULDNT = EXIT_NO_DEVICE

NO_KEY = "no model key yet. Run `mobster login`, then run this again."


def __getattr__(name):
    """``EVERYDAY``: the commands in the order `mobster --help` lists them (the docs generator reads it).
    ``Formatter``: the help formatter every command's parser shares (`python -m mobile_agent.sim` uses it too)."""
    if name == "EVERYDAY":
        from . import help_topics
        return tuple(n for _, names in help_topics.GROUPS for n in names) + help_topics.MORE
    if name == "Formatter":
        return _formatter()
    raise AttributeError(name)



# Commands that read the keys Mobster for Mac saved (config.load_env_layers). Not `mcp`: an installed MCP entry
# must never switch on paid Smart tools the user didn't give it. Not `serve`: the Mac app passes its own file.
APP_KEY_COMMANDS = {"tui", "run", "chat", "doctor", "verify", "test", "setup"}


def output(value):
    import json
    try:
        print(json.dumps(value, allow_nan=False), flush=True)
    except BrokenPipeError:
        # The reader went away (`demo | head -1`): stop quietly, not with a traceback on exit.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)


def default_state_db(args):
    """Where `serve` keeps its journal when --state-db is not given.

    A source checkout keeps it beside the package. An installed copy must not
    (site-packages is shared, may be read-only, and an upgrade deletes it), nor
    may a frozen sidecar (its modules live inside the signed app bundle), so
    both use the data directory.
    """
    from pathlib import Path
    if not source_checkout():
        return Path(getattr(args, "data_dir", None) or user_data_dir()) / "state" / "mobster.sqlite3"
    return Path(__file__).parent / ".state" / "mobster.sqlite3"


def _paint(stream):
    from .style import palette
    return palette(stream)


# -- the parser --------------------------------------------------------------------------------------------------

def _parser_class():
    import argparse

    class Parser(argparse.ArgumentParser):
        """argparse with no abbreviated options (`--exec` is not `--execute`), the error first with one tip, and
        coloured help on a terminal. Every subparser is one too (add_subparsers uses this class)."""

        def __init__(self, *args, allow_abbrev=False, **kwargs):
            super().__init__(*args, allow_abbrev=allow_abbrev, **kwargs)

        def error(self, message):
            paint = _paint(sys.stderr)
            message, tip = explain(self, message, paint)
            self.exit(EXIT_USAGE, usage_lines(paint, message, tip, self.prog, short_usage(self)))

        def print_help(self, file=None):
            file = file or sys.stdout
            text = self.format_help()
            paint = _paint(file)
            if paint.enabled:
                text = colour_help(text, paint)
            file.write(text)

    return Parser


Parser = None  # set by build_parser (argparse is imported only then)


def usage_lines(paint, message, tip="", prog="mobster", usage=""):
    """A usage error the way uv prints one: a coloured `error:`, one `tip:`, the command's usage, and `More:`.
    ``message`` and ``tip`` come painted (the bad word amber, a command to type in accent)."""
    lines = [paint("error:", "coral", "bold") + " " + message]
    if tip:
        lines.append("  " + paint("tip:", "accent") + " " + tip)
    if usage:
        rest = usage.removeprefix(prog).strip()
        lines.append(paint("Usage:", "bold") + " " + paint(prog, "accent") + (" " + paint(rest, "tertiary")
                                                                             if rest else ""))
    lines.append(paint("More:", "bold") + " " + paint(f"{prog} --help", "accent"))
    return "\n".join(lines) + "\n"


def explain(parser, message, paint=None):
    """(message, tip) for argparse's message, painted: "did you mean …?" for a mistyped command or option, and
    the error first. An unknown command no longer lists every command (`mobster --help` does)."""
    import difflib
    import re
    if paint is None:
        from .style import Palette
        paint = Palette(False)
    choice = re.match(r"argument <(?:sub)?command>: invalid choice: '([^']*)'", message)
    if choice:
        word = choice.group(1)
        subs = _subparsers(parser)
        close = difflib.get_close_matches(word, list(subs.choices) if subs else [], n=1, cutoff=.6)
        if close:
            tip = "did you mean " + paint(f"{parser.prog} {close[0]}", "accent") + "?"
        elif parser.prog == "mobster":
            tip = "to run it as a task: " + paint(f"mobster -- {word}", "accent")
        else:
            tip = paint(f"{parser.prog} --help", "accent") + " lists them"
        return paint(word, "amber") + " isn't a command.", tip
    from . import devtools
    if message.startswith("unrecognized arguments:"):
        words = message.split(":", 1)[1].split()
        hint = devtools.option_hint(parser, message)
        unknown = [w for w in words if w.startswith("-")]
        if unknown:
            what = ("unknown option " if len(unknown) == 1 else "unknown options ") + ", ".join(
                paint(w, "amber") for w in unknown)
            extra = [w for w in words if not w.startswith("-")]
            if extra:
                what += "; and " + paint(" ".join(extra), "amber") + " is extra"
        else:
            what = "unexpected " + paint(" ".join(words), "amber")
        what += "."
        tip = ""
        if hint:
            options = hint.removeprefix(". Did you mean ").removesuffix("?").split(" and ")
            tip = "did you mean " + " and ".join(paint(option, "accent") for option in options) + "?"
        return what, tip
    required = re.match(r"the following arguments are required: (.*)", message)
    if required:
        return f"{required.group(1)} is missing.", ""
    return message, ""


def short_usage(parser):
    """One line: `mobster run TASK [options]`."""
    import argparse
    parts = [parser.prog]
    for action in parser._actions:
        if action.option_strings or action.help == argparse.SUPPRESS:
            continue
        if isinstance(action, argparse._SubParsersAction):
            parts.append("<command>" if parser.prog == "mobster" else "<subcommand>")
            continue
        name = action.metavar or action.dest.upper()
        if isinstance(name, tuple):
            name = " ".join(name)
        parts.append(f"[{name}]" if action.nargs in ("?", "*") else f"{name} ..." if action.nargs == "+" else name)
    if any(action.option_strings and action.help != argparse.SUPPRESS and action.dest != "help"
           for action in parser._actions):
        parts.append("[options]")
    return " ".join(parts)


def colour_help(text, paint):
    """argparse's help with bold headings and accent commands, as `mobster --help` has them."""
    import re
    out = []
    section = ""
    for line in text.split("\n"):
        heading = re.match(r"^([a-z][a-z ()/]*):(\s*)$", line) or re.match(r"^(usage|exit codes|examples?):(.*)$", line)
        if heading and not line.startswith(" "):
            section = heading.group(1)
            out.append(paint(heading.group(1)[0].upper() + heading.group(1)[1:] + ":", "bold") + heading.group(2))
            continue
        if section.startswith("example") and line.startswith("  mobster"):
            command, gap, rest = re.match(r"^  (\S.*?)(\s{2,}|$)(.*)$", line).groups()
            out.append("  " + paint("$ ", "tertiary") + paint(command, "accent") + gap + rest)
            continue
        option = re.match(r"^(  (?:-\w, )?--?[\w-]+(?: [A-Z_<>.\[\]| ]+?)?)(\s{2,}.*|)$", line)
        if option and section not in ("usage",):
            out.append(paint(option.group(1), "accent") + option.group(2))
            continue
        out.append(line)
    return "\n".join(out)


def _subparsers(parser):
    import argparse
    return next((action for action in parser._actions if isinstance(action, argparse._SubParsersAction)), None)


def _formatter():
    import argparse

    class Formatter(argparse.RawDescriptionHelpFormatter):
        def __init__(self, prog):
            super().__init__(prog, max_help_position=30, width=min(100, _columns()))

    return Formatter


def _columns():
    try:
        return int(os.environ.get("COLUMNS") or os.get_terminal_size().columns)
    except (OSError, ValueError):
        return 100


def _suppress():
    import argparse
    return argparse.SUPPRESS


# A command's own options default to SUPPRESS: argparse copies a subcommand's defaults over the values
# parsed before it, so `mobster --wda-url URL run …` would otherwise drive the default phone.
COMMAND_DEFAULT = argparse.SUPPRESS


def wda_url_type(value):
    """--wda-url and $MOBSTER_WDA_URL: an http(s) address without credentials, else a usage error (exit 2)."""
    import argparse
    from urllib.parse import urlsplit
    parts = urlsplit(value)
    try:
        parts.port
    except ValueError:
        parts = None
    if (parts is None or parts.scheme not in {"http", "https"} or not parts.hostname or parts.username
            or parts.password or parts.query or parts.fragment):
        # The value is not echoed: it may hold a user name and password.
        raise argparse.ArgumentTypeError(f"expected an http:// address such as {DEFAULT_WDA_URL}")
    return value.rstrip("/")


def path_type(value):
    """A path from the command line, with ~ expanded: shells leave `--env-file=~/keys.env` as typed."""
    from pathlib import Path
    return Path(value).expanduser()


def validate_bundle_id(value):
    """--allow-app: a reverse-DNS bundle ID (state.validate_bundle_id, imported only when one is given)."""
    from .state import validate_bundle_id as validate
    return validate(value)


def _bounded(kind, low, high=None, above=False):
    """An argparse type for a number from ``low`` (exclusive with ``above``) up to ``high``."""
    import argparse
    import math
    word = "a whole number" if kind is int else "a number"
    bounds = (f"more than {low:g}" if above else f"at least {low:g}") + (
        f" and at most {high:g}" if high is not None else "")

    def parse(value):
        try:
            number = kind(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"expected {word}, got {value!r}") from None
        if not math.isfinite(number) or (number <= low if above else number < low) or (
                high is not None and number > high):
            raise argparse.ArgumentTypeError(f"must be {bounds}")
        return number
    return parse


def _wda_option(parser, target=None, default=COMMAND_DEFAULT, help=None):
    (target or parser).add_argument(
        "--wda-url", metavar="URL", type=wda_url_type, default=default,
        help=help or f"the phone's address, for a phone you set up by hand (default: {DEFAULT_WDA_URL}) "
                     "[env: MOBSTER_WDA_URL]")


def _device_option(parser, target=None, default=COMMAND_DEFAULT, help=None):
    (target or parser).add_argument(
        "--device", metavar="NAME", default=default,
        help=help or "the iPhone or simulator to use: an id, UDID or name from `mobster devices` (default: your "
                     "iPhone)")


def _env_option(parser, default_from_environment=True, default=COMMAND_DEFAULT):
    parser.add_argument("--env-file", type=path_type, metavar="PATH", default=default,
                        help="load KEY=VALUE lines (ANTHROPIC_API_KEY, OPENAI_API_KEY, TYPESAFE_API_KEY, ...); "
                             "variables already set win"
                             + (" [env: MOBSTER_ENV_FILE]" if default_from_environment else ""))


def _tui_options(parser, top_level=False):
    """``top_level``: the bare `mobster` parser, which sets the real defaults; `mobster tui` suppresses its own."""
    import argparse
    unset, off = (None, False) if top_level else (COMMAND_DEFAULT, COMMAND_DEFAULT)
    _wda_option(parser, default=unset)
    _device_option(parser, default=unset)
    _env_option(parser, default=unset)
    parser.add_argument("--demo", action="store_true", default=off,
                        help="use a scripted phone and policy: no iPhone, no keys, no model calls")
    parser.add_argument("--resume", nargs="?", const="latest", metavar="ID", default=unset,
                        help="open a past task and put it back in the prompt (the latest without an ID)")
    parser.add_argument("-c", "--continue", dest="continue_", action="store_true", default=off,
                        help="continue your last conversation")
    parser.add_argument("--app", metavar="NAME", default=unset,
                        help="start with this app chosen (a name or bundle ID)")
    parser.add_argument("--no-bell", action="store_true", default=off,
                        help="don't ring the terminal bell when Mobster needs you")
    del argparse


def build_parser(load="all"):
    """The whole command line. ``load``: the developer commands whose modules add their flags ("all", or a set of
    names: main() passes the one command being run, so the others cost nothing)."""
    import argparse
    global Parser
    Parser = _parser_class()
    Formatter = _formatter()
    from . import devtools
    from .help_topics import HELP
    parser = Parser(prog="mobster", formatter_class=Formatter, usage='mobster [options] ["TASK"]\n'
                    "       mobster <command> [options]", description=None, add_help=False)
    options = parser.add_argument_group("options (the terminal UI)")
    _tui_options(options, top_level=True)
    options.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}",
                         help="print the version and exit")
    options.add_argument("-h", "--help", action=TopHelp, help="show this help and exit")
    subs = parser.add_subparsers(dest="command", metavar="<command>", help=argparse.SUPPRESS)

    def command(name, help, description, epilog=None):
        return subs.add_parser(name, prog=f"mobster {name}", help=HELP.get(name, help), description=description,
                               epilog=epilog, formatter_class=Formatter)

    login = command("login", HELP["login"], (
        "Save your Claude or OpenAI key for Mobster's agent, on this Mac. Mobster tests the key\n"
        "with one tiny request first, then saves it in Mobster's settings file, which only you\n"
        "can read. Mobster for Mac and every `mobster` command use it. The key is never shown:\n"
        "only its first characters and last four.\n\n"
        "`mobster login status` shows which keys are saved."),
        epilog=("exit codes: 0 saved (or status shown), 1 the key didn't work, 2 usage\n\n"
                "examples:\n"
                "  mobster login\n"
                "  mobster login --provider openai\n"
                "  mobster login --provider anthropic --with-key < key.txt\n"
                "  mobster login status"))
    login.add_argument("action", nargs="?", choices=["status"], metavar="status",
                       help="show which keys are saved, masked")
    login.add_argument("--provider", choices=["anthropic", "openai", "jev"], default=None,
                       help="anthropic (Claude, the default), openai, or jev for Quick mode")
    login.add_argument("--with-key", action="store_true",
                       help="read the key from standard input (a pipe or a file, never a terminal)")
    login.add_argument("--skip-check", action="store_true", help="save it without testing it (no network)")
    login.add_argument("--json", action="store_true", help="print the result as JSON")

    logout = command("logout", HELP["logout"], (
        "Remove a saved key from this Mac: from Mobster's settings file, which Mobster for Mac\n"
        "uses too. A key set in your shell or an --env-file stays where it is."),
        epilog="examples:\n  mobster logout\n  mobster logout --provider openai")
    logout.add_argument("--provider", choices=["anthropic", "openai", "jev"], default=None,
                        help="which key to remove (default: the one Mobster's agent uses)")

    setup = command("setup", HELP["setup"], (
        "Get your iPhone ready for Mobster: plug it in, trust this Mac, turn on Developer Mode\n"
        "and put Mobster's helper on it. Each step ticks itself off as it happens. The same\n"
        "guided setup as Mobster for Mac's."),
        epilog=("exit codes: 0 your iPhone is ready, 1 a step failed, 3 couldn't run (no Xcode, no tools),\n"
                "            130 ctrl+c\n\n"
                "examples:\n  mobster setup\n  mobster setup --device \"Sam's iPhone\""))
    setup.add_argument("--device", metavar="NAME", help="the iPhone to set up, when several are plugged in")
    setup.add_argument("--team", metavar="ID", help="a paid Apple developer team ID to sign Mobster's helper with")
    setup.add_argument("--json", action="store_true", help="print each change as a JSON line")

    # verify, sim, mcp and the track commands: fixed help text, flags from each package's cli module (devtools.py).
    devtools.add_commands(command, {"env_option": _env_option, "path_type": path_type, "bounded": _bounded},
                          load_only=None if load == "all" else set(load or ()))

    run = command("run", HELP["run"], (
        "Run one task on your iPhone and print each step. On a terminal each step prints as\n"
        "a line; piped, or with --json, every event prints as one JSON object per line, and\n"
        "the last is the result.\n\n"
        "Which agent runs it (--engine auto): Mobster's agent (Smart) on your Claude or OpenAI\n"
        "key, or Quick mode (Fast) when TYPESAFE_API_KEY is set. Smart asks before it sends,\n"
        "buys, posts or deletes: on a terminal you answer y or n; with --json or a pipe the\n"
        "answer is no, and the run ends as declined. Fast never asks: with --execute it can\n"
        "send, buy, post or delete without a prompt, so preview without --execute first.\n\n"
        "ctrl+c stops at the next safe point (twice to force)."),
        epilog=("exit codes: 0 done (or previewed), 1 the task didn't finish, 2 usage error,\n"
                "            3 couldn't run (no phone answered, no key), 130 stopped with ctrl+c\n\n"
                "examples:\n"
                "  mobster run \"Turn on Dark Mode\" --execute\n"
                "  mobster run \"What's my battery level?\" --execute --json\n"
                "  mobster run \"Turn on Dark Mode\" --demo          a scripted phone: no iPhone, no keys\n"
                "  mobster run \"Open Search\" --engine fast          preview Quick mode's first decision"))
    run.add_argument("goal", metavar="TASK", help="what to do, in plain words")
    target = run.add_mutually_exclusive_group()
    _wda_option(run, target)
    _device_option(run, target)
    run.add_argument("--session", metavar="ID", help=argparse.SUPPRESS)
    _env_option(run)
    run.add_argument("--execute", action="store_true", help="act on the phone (Fast without it: preview one decision)")
    run.add_argument("--engine", choices=["auto", "smart", "fast"], default="auto",
                     help="smart (Mobster's agent), fast (Quick mode), or auto: fast when TYPESAFE_API_KEY is set, "
                          "else smart (default: auto) [env: MOBSTER_DEFAULT_ENGINE]")
    run.add_argument("--demo", action="store_true", default=COMMAND_DEFAULT,
                     help="a scripted phone and policy: no iPhone, no keys, no model calls")
    run.add_argument("--helper", action="store_true",
                     help="Fast: use the helper model for typed text, answers and recovery (TEXT_MODEL)")
    run.add_argument("--json", action="store_true", help="print raw JSON events, even on a terminal")
    # Checked here, as usage errors (exit 2), before anything reaches the phone (config.RunBudgets' bounds).
    run.add_argument("--max-steps", type=_bounded(int, 1, 10000), default=30, metavar="N",
                     help="stop after N steps (default: 30)")
    run.add_argument("--max-seconds", type=_bounded(float, 0, above=True), default=120, metavar="S",
                     help="stop after S seconds (default: 120; Smart: 300)")
    run.add_argument("--spend-cap-usd", type=_bounded(float, 0, above=True), default=None, metavar="USD",
                     help="stop before further model calls once this task has cost USD")
    run.add_argument("--expected-text", metavar="TEXT",
                     help="Fast: finish as soon as TEXT is visible on the screen")
    run.add_argument("--allow-app", action="append", metavar="BUNDLE_ID", default=None,
                     type=validate_bundle_id,
                     help="Fast: let the agent switch to this app; repeat for several. "
                          "Without any --allow-app, app switching is never offered")
    # Long tasks (the harness track's long-run mode, the "budget" run field): hidden until it lands.
    run.add_argument("--long", action="store_true", help=argparse.SUPPRESS)

    doctor = command("doctor", HELP["doctor"], (
        "Check everything a task needs and print how to fix what is missing. It changes\n"
        "nothing on the phone. Keys are shown masked: their first characters and last four."),
        epilog="exit codes: 0 ready for tasks (warnings allowed), 1 at least one problem")
    _wda_option(doctor)
    _device_option(doctor)
    _env_option(doctor)
    doctor.add_argument("--json", action="store_true", help="print the checks as JSON")
    doctor.add_argument("--simulator", action="store_true",
                        help="skip the USB iPhone and Xcode checks (a simulator's phone)")

    history = command("history", HELP["history"], (
        "List the tasks you ran in this terminal, newest first. Open one with\n"
        "`mobster --resume ID`, or save one as a GIF with `mobster export ID --gif`. The\n"
        "history holds task text and screen text: keep it private."))
    history.add_argument("--json", action="store_true", help="print one JSON object per task")
    history.add_argument("-n", "--limit", type=_bounded(int, 1, 10000), default=20, metavar="N",
                         help="show N tasks (default: 20)")

    export = command("export", HELP["export"], (
        "Save a finished task as a captioned GIF, one screen per step with what Mobster did,\n"
        "ready to post: under X's 15 MB limit, on the Paper or Night ground. --mp4 saves an MP4\n"
        "too (it needs ffmpeg). The file goes to ~/Downloads and never replaces one there.\n"
        "It shows every screen of the task, so check it before you post it: --redact blurs\n"
        "text fields and messages and leaves the answer out. Nothing is uploaded."),
        epilog=("exit codes: 0 saved, 1 couldn't (no such task, no screens, no ffmpeg for --mp4), 2 usage\n\n"
                "examples:\n"
                "  mobster export latest --gif\n"
                "  mobster export 3f9c2a1b7d10 --gif --mp4 --theme night\n"
                "  mobster export 3f9c --gif --redact -o ~/Desktop/clip.gif"))
    export.add_argument("run_id", metavar="ID", help="a task's ID from `mobster history` (its first characters do), "
                                                     "or latest")
    export.add_argument("--gif", action="store_true", help="save a GIF")
    export.add_argument("--mp4", action="store_true", help="save an MP4 (needs ffmpeg)")
    export.add_argument("--redact", action="store_true",
                        help="blur text fields and messages, and leave out the answer")
    export.add_argument("--theme", choices=["paper", "night"], default="paper",
                        help="the ground: paper (light, the default) or night (dark)")
    export.add_argument("-o", "--output", type=path_type, metavar="PATH",
                        help="a file or folder to save to (default: ~/Downloads)")
    export.add_argument("--json", action="store_true", help="print where it saved as JSON")

    screen = command("screen", HELP["screen"], (
        "Print your iPhone's current screen. kitty, Ghostty, iTerm2 and WezTerm show the\n"
        "real image; other terminals get a half-block drawing. Nothing is tapped."))
    _wda_option(screen)
    _device_option(screen)
    screen.add_argument("--out", type=path_type, metavar="FILE", help="save the PNG to FILE instead of printing it")
    screen.add_argument("--width", type=_bounded(int, 1), metavar="COLUMNS", help="draw it this many columns wide")

    serve = command("serve", HELP["serve"], (
        "Start the loopback HTTP API (127.0.0.1 only) that Mobster for Mac launches as its\n"
        "sidecar. Every request needs the launch's API token. Scripting with it:\n"
        f"{links.SCRIPTING}"))
    serve.add_argument("--port", type=_bounded(int, 0, 65535), default=8765,
                       help="port on 127.0.0.1 (default: 8765; 0 picks a free one)")
    # serve does not read $MOBSTER_WDA_URL: the Mac app decides which phone its sidecar drives.
    _wda_option(serve, help=f"the WebDriverAgent to drive (default: none, or {DEFAULT_WDA_URL} with "
                            "--manage-device); a simulator's runner listens on its own port")
    _device_option(serve, help="the device a task that names none runs on: an id, UDID or name from "
                               "`mobster devices` (default: the only device, else the one last used)")
    serve.add_argument("--manage-device", action="store_true",
                       help="own the USB iPhone setup: build, run and supervise Mobster's helper (the Mac app does "
                            "this)")
    serve.add_argument("--data-dir", type=path_type, metavar="PATH", help="where the managed device keeps its files")
    serve.add_argument("--exit-with-parent", action="store_true",
                       help="shut down when the launching process exits (the Mac app's safety net)")
    serve.add_argument("--keep-runner", action="store_true",
                       help="leave the iPhone runner running on exit, for development reloads; the next server adopts it")
    _env_option(serve, default_from_environment=False)
    serve.add_argument("--session", metavar="ID", help="preferred WDA session; the one WDA is serving always wins")
    serve.add_argument("--enable-live", action="store_true", help="allow tasks to act on the phone")
    serve.add_argument("--spend-cap-usd", type=_bounded(float, 0, above=True), default=None, metavar="USD",
                       help="stop a run before further model calls once its observed inference spend reaches USD")
    serve.add_argument("--state-db", type=path_type, default=None, metavar="PATH",
                       help="the private task journal (never automatically replays interrupted tasks); "
                            "default: .state/ beside the source in a checkout, else <data dir>/state/")

    devices = command("devices", HELP["devices"], (
        "List every device Mobster can drive: USB iPhones (set up or just plugged in),\n"
        "Mobster's simulators, and phone addresses from MOBSTER_WDA_DEVICES. Each shows\n"
        "its state: Ready, In use, Connected, Needs setup or Unplugged. Name one with\n"
        "--device on run, screen, doctor, serve, mcp and the terminal UI. It changes nothing.\n"
        "\n"
        "`mobster devices lock-probe` reads whether an iPhone is locked or showing a system\n"
        "sheet (Face ID, a locked app, the App Store's side-button confirmation) the way a\n"
        "task's check does, and lists what the lock screen shows. It taps and types nothing."),
        epilog=("examples:\n"
                "  mobster devices\n"
                "  mobster devices --json\n"
                "  mobster devices lock-probe --device \"Sam's iPhone\"\n"
                "  mobster run \"Turn on Dark Mode\" --execute --device \"Sam's iPhone\""))
    devices.add_argument("action", nargs="?", choices=["lock-probe"], metavar="lock-probe",
                         help="read the iPhone's lock state and system sheets; taps nothing")
    devices.add_argument("--json", action="store_true", help="print the devices (or the probe) as JSON")
    _wda_option(devices, help="lock-probe: the phone's address (default: $MOBSTER_WDA_URL, else "
                              f"{DEFAULT_WDA_URL})")
    _device_option(devices, help="lock-probe: the iPhone to read, by id, UDID or name")

    demo = command("demo", HELP["demo"], (
        "Run the agent loop once against a scripted screen and model, offline. It is a\n"
        "fixture for trying the output and for tests, not a device benchmark. For the\n"
        "terminal UI with a scripted phone, run `mobster --demo`."))
    demo.add_argument("--json", action="store_true", help="print raw JSON events, even on a terminal")

    completion = command("completion", HELP["completion"], (
        "Print a completion script for your shell, so tab completes commands, options, your\n"
        "devices and past task IDs. Homebrew installs the zsh and fish ones for you."),
        epilog=("examples:\n"
                "  mobster completion zsh > \"${fpath[1]}/_mobster\"\n"
                "  mobster completion bash > ~/.local/share/bash-completion/completions/mobster\n"
                "  mobster completion fish > ~/.config/fish/completions/mobster.fish"))
    completion.add_argument("shell", nargs="?", choices=["zsh", "bash", "fish"], help="zsh, bash or fish")
    completion.add_argument("--values", choices=["devices", "tasks", "threads"], help=argparse.SUPPRESS)

    update = command("update", HELP["update"], (
        "Update Mobster to its latest release, the way you installed it: Homebrew, the\n"
        "install script, uv or pip. Mobster checks for a new release once a day and says so\n"
        "after a command; MOBSTER_NO_UPDATE_CHECK=1 turns that off."),
        epilog="examples:\n  mobster update\n  mobster update --check")
    update.add_argument("--check", action="store_true", help="only say whether a new release is out")

    version = command("version", HELP["version"], "Print the version of Mobster.")
    version.add_argument("--json", action="store_true", help="print the version, Python and platform as JSON")

    tui = command("tui", HELP["tui"], "Open the terminal UI. The same as running `mobster` with no command.")
    _tui_options(tui)

    helping = command("help", HELP["help"],
                      "Show the help for a command, or a topic: environment, exit-codes, tui. `mobster help --all`\n"
                      "lists every command.")
    helping.add_argument("topic", nargs="*", metavar="COMMAND", help="a command (and a subcommand), or a topic")
    helping.add_argument("--all", action="store_true", help="list every command, the hidden ones too")

    command("build-ocr", HELP["build-ocr"], "Compile the standalone Apple Vision OCR helper into the build folder.")
    fixture = command("decide-fixture", HELP["decide-fixture"],
                      "One real Jev decision on a synthetic screen. Needs TYPESAFE_API_KEY.")
    fixture.add_argument("--goal", default="Open Search")
    _env_option(fixture)

    commands = {"run": run, "serve": serve, "run_target": target, "doctor": doctor, "screen": screen}
    for extension in load_extensions().cli:
        extension.add_arguments(subs, commands)
    parser.commands = subs.choices  # for check_placement
    _tidy(parser)
    return parser


class TopHelp(argparse._HelpAction):
    """`mobster -h` with other options (`mobster --demo -h`): the same grouped help as `mobster --help`."""

    def __call__(self, parser, namespace, values, option_string=None):
        from . import help_topics
        print(help_topics.top_level(_paint(sys.stdout), _columns()))
        parser.exit()


def _tidy(parser):
    """Every parser's -h says "show this help and exit", and a subcommand hidden with SUPPRESS stays out of its
    parent's list (Python 3.12 prints "==SUPPRESS==" for it otherwise)."""
    import argparse
    seen = set()
    stack = [parser]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        for action in current._actions:
            if isinstance(action, argparse._HelpAction):
                action.help = "show this help and exit"
            if isinstance(action, argparse._SubParsersAction):
                action._choices_actions = [choice for choice in action._choices_actions
                                           if choice.help != argparse.SUPPRESS]
                stack.extend(action.choices.values())


def owner(parser, args):
    """The innermost parser ``args`` went through: `mobster sim prepare`'s for `mobster sim prepare --dev x`."""
    target = parser
    while True:
        subs = _subparsers(target)
        chosen = getattr(args, subs.dest, None) if subs is not None else None
        if chosen not in (subs.choices if subs is not None else ()):
            return target
        target = subs.choices[chosen]


def print_help(parser, topic, everything=False):
    """`mobster help [COMMAND [SUBCOMMAND] | TOPIC]`: that command's --help, or a topic, on stdout, exit 0."""
    from . import help_topics
    if everything:
        subs = _subparsers(parser)
        helps = {action.dest: action.help for action in subs._choices_actions}
        helps.update({name: help_topics.HELP.get(name, "") for name in subs.choices if name not in helps})
        print(help_topics.all_commands(_paint(sys.stdout), helps))
        return EXIT_OK
    if topic and topic[0] in help_topics.TOPICS:
        return print_topic(topic[0])
    target = parser
    for word in topic:
        subs = _subparsers(target)
        if subs is None or word not in subs.choices:
            target.error(f"argument <command>: invalid choice: {word!r}")
        target = subs.choices[word]
    if target is parser:
        print(help_topics.top_level(_paint(sys.stdout), _columns()))
        return EXIT_OK
    target.print_help()
    return EXIT_OK


def print_topic(name):
    from . import help_topics
    paint, width = _paint(sys.stdout), _columns()
    if name == "environment":
        print(help_topics.environment(paint, width))
    elif name == "exit-codes":
        print(help_topics.exit_codes(paint, width))
    else:
        from .tui_keys import KEYS
        print(help_topics.tui(paint, width, KEYS))
    return EXIT_OK


def check_placement(parser, args, on_error=None):
    """Options given before a command's name, as in `mobster --wda-url URL run …`.

    A command that takes --wda-url or --env-file receives it from either side of its name (its own options
    default to SUPPRESS, so they no longer overwrite the value). The terminal UI's --demo, --resume, --continue and
    --app change what would run, so another command refuses them rather than acting on the real phone without them;
    --no-bell and an --env-file or --wda-url a command does not use (an alias's) are harmless there.
    ``on_error(message)`` hears the message before parser.error exits.
    """
    from . import devtools

    def error(message):
        if on_error is not None:
            on_error(message)
        parser.error(message)
    command = args.command
    if command in (None, "tui"):
        return
    accepted = parser.commands[command]._option_string_actions
    for dest, flag in (("demo", "--demo"), ("resume", "--resume"), ("app", "--app"), ("continue_", "--continue")):
        if getattr(args, dest, None) and flag not in accepted:
            error(f"{flag} is an option of the terminal UI (`mobster {flag}`), not of `mobster {command}`")
    if getattr(args, "wda_url", None) and getattr(args, "socket", None):
        error("argument --socket: not allowed with argument --wda-url")
    if getattr(args, "device", None) and getattr(args, "socket", None):
        error("argument --socket: not allowed with argument --device")
    if command != "serve" and getattr(args, "device", None) and getattr(args, "wda_url", None) \
            and command not in devtools.COMMANDS:
        error("argument --device: not allowed with argument --wda-url")
    listing = command == "devices" and getattr(args, "action", None) is None  # the list reads no WDA address
    if "--wda-url" in accepted and command != "serve" and not listing and not getattr(args, "wda_url", None) \
            and os.environ.get("MOBSTER_WDA_URL"):
        import argparse
        try:
            wda_url_type(os.environ["MOBSTER_WDA_URL"])
        except argparse.ArgumentTypeError as problem:
            error(f"$MOBSTER_WDA_URL: {problem}")


# -- running a task ----------------------------------------------------------------------------------------------

def run_task(args, *, lease_key, target, visual_options=None):
    """The ``run`` command on Fast against one target: preview a decision, or execute a bounded task.

    ``target`` gives ``build_target_driver``'s keyword arguments, or is a callable
    that returns them once the device lease is held; ``lease_key`` names the device
    for the cross-process lease.
    """
    import threading
    from .agent import Agent
    from .compose import build_target_driver, build_visual, close_all
    from .costs import SpendLedger, usd_to_nanodollars
    from .journal import Lease, LeaseHeld

    usd_to_nanodollars(args.spend_cap_usd)  # Fail before touching the device.
    emit = event_printer(args, preview=not args.execute)
    stop = threading.Event()
    ledger = SpendLedger()

    def observe(event):
        ledger.add_event(event)
        if emit is not output:
            emit(event)
    # The models before the phone: a missing key fails here, with no lease taken and nothing sent to WDA.
    model, helper = run_models(args, observe)
    driver = None
    try:
        lease = Lease.device(lease_key) if args.execute else None
    except LeaseHeld:
        close_all(model, helper)
        raise DeviceBusy("Another Mobster process is using this phone: Mobster for Mac, `mobster serve`, the "
                         "terminal UI or another `mobster run`. Finish or stop its task, then run again.") from None
    except BaseException:
        close_all(model, helper)
        raise
    if emit is not output:
        model_name = os.environ.get("TYPESAFE_MODEL", "jev-latest")
        emit.start(args.goal, f"Fast · {model_name}" + ("" if args.execute else " · preview, nothing is sent"))
    try:
        if callable(target):
            target = target()
        driver = build_target_driver(**target)
        if target.get("wda_url") and args.execute:
            # Pixel settle (MOBSTER_FRAME_CLOCK) over its own MJPEG connection: the stream paired with
            # this WDA's port (8100 -> 9100, a simulator's 8203 -> 9203), never another device's.
            from .frame_clock import attach_frame_clock
            from .pool import default_mjpeg_url
            attach_frame_clock(driver, wda_url=target["wda_url"], session=target.get("session"),
                               mjpeg_url=default_mjpeg_url(target["wda_url"]))
        visual = build_visual(enabled=getattr(args, "read_screen", False), driver=driver,
                              **(visual_options or {}))
        if target.get("wda_url"):
            # Before the first model call: a locked or unplugged phone stops here in one read, at $0.
            check_phone(driver, args)
        agent = Agent(driver, model, helper, args.max_steps, args.max_seconds,
                      emit=emit, visual=visual, spend_cap_usd=args.spend_cap_usd,
                      spend_ledger=ledger, allowed_bundles=args.allow_app, cancelled=stop.is_set)
        with stop_at_safe_point(stop) as caught:
            try:
                result = agent.run(args.goal, execute=args.execute, expected_text=args.expected_text)
            except KeyboardInterrupt:
                # A second ctrl+c: no safe point waited for, but a script still reads how the run ended.
                if emit is output:
                    output({"event": "result", "status": "stopped", "reason": "Stopped with a second signal "
                            "before the next safe point; an action may have been sent", "exit_code": 130})
                raise
        if caught:
            return 128 + caught[0]
        return EXIT_OK if result["status"] in {"preview", "expected_text_visible", "completed_unverified"} else EXIT_FAILED
    finally:
        if emit is not output:
            emit.close()
        close_all(driver, model, helper)
        if lease:
            lease.close()


class stop_at_safe_point:
    """While a task runs: the first ctrl+c, SIGTERM or SIGHUP sets ``stop`` (the agent's ``cancelled``), so the run
    ends at its next operation boundary with its `result` event; a second raises KeyboardInterrupt. Yields the
    signal numbers caught (exit code 128 + the first). Off the main thread, nothing changes. ``on_stop()`` hears
    the first."""

    def __init__(self, stop, on_stop=None):
        self.stop, self.on_stop, self.caught, self.previous = stop, on_stop, [], {}

    def handler(self, signum, frame):
        self.caught.append(signum)
        if self.stop.is_set():
            raise KeyboardInterrupt
        self.stop.set()
        if self.on_stop is not None:
            self.on_stop()
        try:
            print("mobster: stopping at the next safe point (ctrl+c again to force)", file=sys.stderr, flush=True)
        except (OSError, ValueError):
            pass

    def __enter__(self):
        import signal
        import threading
        if threading.current_thread() is threading.main_thread():
            for name in ("SIGINT", "SIGTERM", "SIGHUP"):
                number = getattr(signal, name, None)
                if number is not None:
                    self.previous[number] = signal.signal(number, self.handler)
        return self.caught

    def __exit__(self, *exc):
        import signal
        for number, old in self.previous.items():
            signal.signal(number, old)
        return False


def check_phone(driver, args):
    """The phone guard's preflight for `run` (lockscreen): PhoneStopped with its sentence when the phone is
    locked, unplugged or not answering. Read-only; a driver that is not WebDriverAgent has nothing to read."""
    if getattr(driver, "http", None) is None:
        return
    from .agent_hooks import safe_check
    from .lockscreen import guard_for
    udid = getattr(args, "device_udid", None) if getattr(args, "device_kind", None) == "usb" else None
    guard = guard_for(device_id=udid, wda_url=getattr(args, "wda_url", None), mode="interactive", usb=bool(udid))
    verdict = safe_check(guard, driver, "preflight")
    if verdict.state == "stop":
        raise PhoneStopped(verdict.message, verdict.code)


def run_models(args, on_inference):
    """Jev and, with --helper, the helper: Fast decides with Jev."""
    from .compose import build_models
    try:
        return build_models(helper=args.helper, on_inference=on_inference)
    except ValueError:
        if os.environ.get("TYPESAFE_API_KEY"):
            raise  # the helper's settings, which name themselves
        raise MissingKey("Quick mode (--engine fast) needs a Jev key, and TYPESAFE_API_KEY isn't set. Run "
                         "`mobster login --provider jev`, or leave out --engine fast to run on your Claude or "
                         "OpenAI key.") from None


def pick_engine(args, env=None):
    """"demo", "smart" or "fast" for `run`: --demo; else --engine; auto is fast when TYPESAFE_API_KEY is set (as
    `run` always worked), else smart."""
    env = os.environ if env is None else env
    if getattr(args, "demo", False) is True:
        return "demo"
    engine = getattr(args, "engine", "auto") or "auto"
    if engine == "auto":
        saved = env.get("MOBSTER_DEFAULT_ENGINE")
        if saved in ("smart", "fast"):
            return saved
        return "fast" if env.get("TYPESAFE_API_KEY") else "smart"
    return engine


def event_printer(args, preview=False):
    """``output`` (JSON lines) for scripts and --json; the step renderer on a terminal."""
    if getattr(args, "json", False) or not sys.stdout.isatty():
        return output
    from .console import ConsoleRenderer
    return ConsoleRenderer(preview=preview)


def wda_url_for(args):
    return (getattr(args, "wda_url", None) or os.environ.get("MOBSTER_WDA_URL") or DEFAULT_WDA_URL).rstrip("/")


def device_target(name):
    """The device `--device NAME` names (devices.discover), with a WDA address to drive. NoDevice (exit 3) when
    no device is named so, or the one named has no runner to drive yet."""
    from . import devices
    try:
        item = devices.resolve(devices.discover(probe=False), name)
    except LookupError as error:
        raise NoDevice(str(error)) from None
    if not item.get("wdaUrl"):
        raise NoDevice(f"“{item['name']}” isn't set up yet. {item.get('reason') or ''} Set it up with "
                       "`mobster setup`, or in Mobster for Mac.".replace("  ", " "))
    return item


PHONE_WORDS = {"ready": "Ready", "busy": "In use", "connected": "Connected", "needs_setup": "Needs setup",
               "disconnected": "Unplugged"}


def device_state_words(item):
    """Mobster for Mac's phone words for a device's state (MESSAGING §11, the phone list)."""
    state = item.get("state") or ""
    if state == "disconnected":
        return {"simulator": "Shut down", "wda": "Not answering"}.get(item.get("kind"), "Unplugged")
    return PHONE_WORDS.get(state, state.replace("_", " ").capitalize())


def print_devices(as_json=False):
    from . import devices
    found = devices.discover()
    if as_json:
        output({"devices": found})
        return EXIT_OK
    paint = _paint(sys.stdout)
    if not found:
        print("No iPhones or simulators yet. Plug in your iPhone with a cable and unlock it, then run "
              + paint("mobster setup", "accent") + ".")
        print("For a simulator: " + paint("mobster sim prepare", "accent") + ".")
        return EXIT_OK
    kinds = {"usb": "iPhone", "simulator": "Simulator", "wda": "Address"}
    rows = []
    for item in found:
        kind = kinds.get(item["kind"], item["kind"])
        if item["kind"] == "usb" and item.get("transport") == "wifi":
            kind = "iPhone, Wi-Fi"
        details = ", ".join(part for part in (item.get("modelName") or item.get("model"),
                                              f"iOS {item['ios']}" if item.get("ios") else None) if part)
        rows.append((item, kind, details))
    name_width = min(28, max(4, *(len(item["name"]) for item, _, _ in rows))) + 2
    kind_width = max(4, *(len(kind) for _, kind, _ in rows)) + 2
    state_width = max(5, *(len(device_state_words(item)) for item, _, _ in rows)) + 2
    print("  " + paint("Name".ljust(name_width), "bold") + paint("Kind".ljust(kind_width), "bold")
          + paint("State".ljust(state_width), "bold") + paint("Details", "bold"))
    roles = {"Ready": "green", "In use": "accent", "Needs setup": "amber"}
    width = _columns()
    for item, kind, details in rows:
        state = device_state_words(item)
        name = item["name"] if len(item["name"]) < name_width - 1 else item["name"][:name_width - 3] + "…"
        line = ("  " + paint(name.ljust(name_width), "accent") + kind.ljust(kind_width)
                + paint(state.ljust(state_width), roles.get(state, "secondary")) + paint(details, "tertiary"))
        print(line)
        if item.get("reason"):
            from .style import wrap
            print(paint(wrap(item["reason"], width - 2, indent=4), "tertiary"))
    ready = [item for item, _, _ in rows if item.get("state") == "ready"]
    print()
    if ready:
        print("Give one a task: " + paint(f'mobster --device "{ready[0]["name"]}"', "accent"))
    elif any(item.get("state") == "needs_setup" for item, _, _ in rows):
        print("Set one up: " + paint("mobster setup", "accent"))
    else:
        print("Check everything: " + paint("mobster doctor", "accent"))
    return EXIT_OK


def lock_probe(args):
    """`mobster devices lock-probe`: what the phone guard reads (lockscreen.probe), under the device lease so
    no task is running on the phone meanwhile. No tap, no key press, no typing; the one session setting it
    changes (SpringBoard as the active application, for one tree read) is put back."""
    from . import lockscreen
    from .compose import close_all
    from .drivers import build_driver, resolve_wda_session
    from .journal import Lease, LeaseHeld
    from .transport import TransportError
    url = wda_url_for(args)
    try:
        lease = Lease.device(url)
    except LeaseHeld:
        raise DeviceBusy("Another Mobster process is using this phone. Finish or stop its task, then probe "
                         "again.") from None
    driver = None
    try:
        try:
            session = resolve_wda_session(url)
        except (TransportError, OSError) as exc:
            raise NoDevice(f"Nothing answers at {url} ({no_device_reason(exc)}).") from None
        driver = build_driver("wda", url=url, session=session)
        result = lockscreen.probe(driver)
    finally:
        close_all(driver)
        lease.close()
    if args.json or not sys.stdout.isatty():
        output({"ok": True, **result})
    else:
        print("\n".join(lockscreen.describe(result)))
    return EXIT_OK


class NoDevice(ConnectionError):
    """Nothing answered at the WDA address (exit code 3)."""


class DeviceBusy(RuntimeError):
    """Another Mobster process holds the phone's device lease."""


class MissingKey(ValueError):
    """The model key a command needs is not set (exit code 3)."""


class PhoneStopped(RuntimeError):
    """The phone guard stopped `run` before it acted; ``code`` is api_errors.PHONE_STOP_CODES' name for why."""

    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


def no_device_reason(exc):
    """A few words on how reaching WDA failed, from the transport's message (which never holds a body)."""
    text = f"{type(exc).__name__} {exc}".lower()
    for needle, words in (("refused", "connection refused"), ("reset", "connection reset"),
                          ("disconnected", "connection closed"), ("gaierror", "unknown host"),
                          ("timeout", "timed out"), ("deadline", "timed out"), ("http 4", "not WebDriverAgent"),
                          ("json", "not WebDriverAgent")):
        if needle in text:
            return words
    return "no usable answer"


def error_result(exc, exit_code):
    """`run`'s (and `demo`'s) JSON line for an error before or around the run: a `result` event, so
    `jq 'select(.event == "result")'` sees how every run ended. ``ok`` and ``error_type`` stay for older scripts."""
    return {"event": "result", "status": "error", "ok": False, "error_type": type(exc).__name__,
            "error": str(exc), "reason": str(exc), "exit_code": exit_code,
            **({"stop_code": exc.code} if isinstance(exc, PhoneStopped) else {})}


def error_line(exc):
    """A human line for a terminal: what happened, then what to do."""
    return f"mobster: {exc}"


# -- the first word ----------------------------------------------------------------------------------------------

# The bare `mobster` parser's options that take a value: their value is never the command's name.
VALUE_OPTIONS = ("--wda-url", "--device", "--env-file", "--app")


def _positional(argv, names):
    """The index of the first word in ``argv`` that is neither an option nor an option's value, or None."""
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return index + 1 if index + 1 < len(argv) else None
        if token in VALUE_OPTIONS:
            index += 2
            continue
        if token == "--resume":
            # --resume takes an optional ID: a command's name after it is the command.
            following = argv[index + 1] if index + 1 < len(argv) else None
            index += 1 if following is None or following in names or following.startswith("-") else 2
            continue
        if token.startswith("-") and token != "-":
            index += 1
            continue
        return index
    return None


def command_word(parser, argv):
    """The command ``argv`` names, read before parsing (so a usage error in an option before it can still be
    reported the command's way), or None for the terminal UI or a word that is no command."""
    names = parser.commands if hasattr(parser, "commands") else parser
    index = _positional(list(argv), names)
    if index is None:
        return None
    if index and argv[index - 1] == "--":
        return None  # `mobster -- verify`: after --, the word is a task, not a command
    word = argv[index]
    return word if word in names else None


def split_task(argv):
    """`mobster "TASK"`: (task, argv without it) when the first word is a task for the terminal UI, else (None, argv).

    One word with a space in it that isn't a command is a task. A single word that isn't a command, or several
    unquoted words, is a mistake: UsageError says how to run it as a task (or which command was meant)."""
    from .help_topics import COMMANDS
    index = _positional(argv, COMMANDS)
    if index is None or argv[index] in COMMANDS:
        return None, argv
    word = argv[index]
    if index and argv[index - 1] == "--":
        # `mobster -- calendar`: after --, a word is always the task, even one word.
        return word.strip(), argv[:index - 1] + argv[index + 1:]
    if load_extensions().cli:
        # A private build may add commands: its parser knows them.
        if word in build_parser(load=()).commands:
            return None, argv
    later = [token for token in argv[index + 1:] if not token.startswith("-")]
    paint = _paint(sys.stderr)
    if " " in word.strip() or "\n" in word.strip():
        if later:
            raise UsageError(paint(later[0], "amber") + " follows the task.",
                             "put the whole task in one pair of quotes: "
                             + paint(f'mobster "{word} {" ".join(later)}"', "accent"))
        return word.strip(), argv[:index] + argv[index + 1:]
    if later:
        words = " ".join([word] + later)
        raise UsageError(paint(word, "amber") + " isn't a command.",
                         "to run it as a task, put it in quotes: " + paint(f'mobster "{words}"', "accent"))
    import difflib
    close = difflib.get_close_matches(word, [name for name in COMMANDS if name not in ("build-ocr",
                                                                                         "decide-fixture")], n=1,
                                      cutoff=.6)
    if close:
        raise UsageError(paint(word, "amber") + " isn't a command.",
                         "did you mean " + paint(f"mobster {close[0]}", "accent") + "?")
    raise UsageError(paint(word, "amber") + " isn't a command.",
                     "to run it as a task: " + paint(f"mobster -- {word}", "accent"))


class UsageError(Exception):
    """A usage error found before the parser runs: (message, tip). Exit code 2."""

    def __init__(self, message, tip=""):
        super().__init__(message)
        self.message, self.tip = message, tip

    def report(self, prog="mobster"):
        sys.stderr.write(usage_lines(_paint(sys.stderr), self.message, self.tip, prog))
        sys.stderr.flush()
        return EXIT_USAGE


def route_usage_errors(parser, argv):
    """`mobster --bogus verify …`: a usage error the bare parser finds before `verify` exits 3 (2 is verify's
    needs review), with verify's couldnt_run result when its output is JSON, as verify's own usage errors do."""
    from . import devtools
    if command_word(parser, argv) != "verify":
        return
    as_json = "--json" in argv

    def error(message):
        parser.print_usage(sys.stderr)
        print(f"{parser.prog}: error: {message}", file=sys.stderr, flush=True)
        sys.exit(devtools.usage_exit("verify", message, as_json=as_json))
    parser.error = error


def quick(argv):
    """`--version` and `--help`, answered before the parser is built. None for anything else."""
    if argv in (["-V"], ["--version"], ["version"]):
        print(f"mobster {__version__}")
        return EXIT_OK
    if argv in (["-h"], ["--help"], ["help"]):
        from . import help_topics
        print(help_topics.top_level(_paint(sys.stdout), _columns()))
        return EXIT_OK
    if len(argv) == 2 and argv[0] == "help":
        from . import help_topics
        if argv[1] in help_topics.TOPICS:
            return print_topic(argv[1])
    return None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    answered = quick(argv)
    if answered is not None:
        return answered
    try:
        task, argv = split_task(argv)
    except UsageError as error:
        return error.report()
    from .help_topics import COMMANDS
    index = _positional(argv, COMMANDS)
    word = argv[index] if index is not None else None
    wanted = {word}
    if word == "help":
        wanted |= set(argv[index + 1:index + 2])
    parser = build_parser(load=wanted)
    route_usage_errors(parser, argv)
    args, extras = parser.parse_known_args(argv)
    args.task = task
    words = [word for word in extras if not word.startswith("-")]
    if extras and words == extras and args.command == "run" and getattr(args, "goal", None):
        # `mobster run Turn on Dark Mode`: the task is the words, unquoted.
        task = " ".join([args.goal] + words)
        paint = _paint(sys.stderr)
        return UsageError(paint("mobster run", "accent") + " needs the task in quotes.",
                          paint(f'mobster run "{task}"', "accent")).report("mobster run")
    if extras and not getattr(args, "stub_command", False):  # a track's stub takes anything (it exits 3)
        # `mobster run x --bogus`: the error and usage are run's (`sim prepare`'s), not the bare `mobster`'s.
        owner(parser, args).error(f"unrecognized arguments: {' '.join(extras)}")
    if args.command == "help":
        return print_help(parser, args.topic, everything=args.all)
    messages = []
    from . import devtools
    try:
        check_placement(parser, args, on_error=messages.append)
    except SystemExit as exit_:
        if exit_.code == 2 and args.command in devtools.COMMANDS:
            # verify's usage errors exit 3 (2 means needs review), with its JSON result when output is JSON.
            return devtools.usage_exit(args.command, messages[-1] if messages else None,
                                       as_json=bool(getattr(args, "json", False)))
        raise
    command = args.command or "tui"
    if hasattr(args, "env_file") and args.env_file is None and args.command != "serve" \
            and os.environ.get("MOBSTER_ENV_FILE"):
        # An explicit default for people who always use the same file; `serve` keeps its own rules.
        from pathlib import Path
        args.env_file = Path(os.environ["MOBSTER_ENV_FILE"]).expanduser()
    if command in APP_KEY_COMMANDS and not getattr(args, "demo", False) is True:
        # The keys Mobster for Mac saved come last: the shell and an --env-file win (config.load_env_layers).
        from .config import load_env_layers
        for warning in load_env_layers(getattr(args, "env_file", None)):
            print(f"mobster: {warning}", file=sys.stderr, flush=True)
    elif getattr(args, "env_file", None) and args.command != "mcp":  # mcp loads it itself, into its log
        from .config import load_env_file
        # A bad line is skipped, never fatal: the Mac app's agent logs it (agent.log) and still serves. A missing
        # file is said too, except to `serve`, whose file the Mac app's setup creates when it saves a value.
        for warning in load_env_file(args.env_file, missing_ok=args.command == "serve").warnings:
            print(f"mobster: {warning}", file=sys.stderr, flush=True)
    # Before any model or service connection: the frozen app's Python has no usable CA file.
    from . import tls
    tls.ensure_ca_bundle()
    from . import update_check
    update_check.start(command, args)
    code = dispatch(parser, args, command)
    update_check.after_command(command, args, code)
    return code


def dispatch(parser, args, command):
    from . import devtools
    if command == "run" and getattr(args, "long", False):
        # The "budget" run field and harness's long-run mode (v2) aren't in this build.
        print("Long tasks aren't in this build yet.", file=sys.stderr, flush=True)
        return 3
    if command == "devices" and getattr(args, "action", None) is None and (
            getattr(args, "device", None) or getattr(args, "wda_url", None)):
        parser.commands["devices"].error("--device and --wda-url go with `mobster devices lock-probe`")
    probing = command == "devices" and getattr(args, "action", None) == "lock-probe"
    if (command in ("run", "screen", "doctor", "tui") or probing) and getattr(args, "device", None) \
            and not getattr(args, "demo", False) is True:
        # --device NAME is that device's WDA address from here on (exclusive with --wda-url).
        try:
            item = device_target(args.device)
        except NoDevice as error:
            if command == "run" and (getattr(args, "json", False) or not sys.stdout.isatty()):
                output(error_result(error, EXIT_NO_DEVICE))
            else:
                print(error_line(error), file=sys.stderr)
            return EXIT_NO_DEVICE
        args.wda_url = item["wdaUrl"]
        args.device_kind, args.device_udid = item["kind"], item.get("udid")
        args.device_name = item.get("name")
    if command == "chat" and not getattr(args, "unloaded_command", False):
        from .chat_here import maybe_here
        code = maybe_here(args)
        if code is not None:
            return code
    if command in devtools.COMMANDS:
        try:
            return devtools.run(args)
        except KeyboardInterrupt:
            return 130
    try:
        for extension in load_extensions().cli:
            code = extension.run(args, output, run_task)
            if code is not None:
                return code
        if command == "tui":
            from .tui import main as tui_main
            return tui_main(args)
        if command == "version":
            if args.json:
                import platform
                from . import tls
                output({"version": __version__, "python": platform.python_version(),
                        "platform": platform.platform(terse=True), "tls": tls.report()})
            else:
                print(f"mobster {__version__}")
            return EXIT_OK
        if command in ("login", "logout"):
            from . import login
            return login.run(args)
        if command == "setup":
            from . import setup_cli
            return setup_cli.run(args)
        if command == "completion":
            from . import completion
            return completion.run(args)
        if command == "update":
            from . import update_check
            return update_check.run_update(args)
        if command == "devices":
            if args.action == "lock-probe":
                return lock_probe(args)
            return print_devices(as_json=args.json)
        if command == "doctor":
            from . import doctor
            usb = not args.simulator and getattr(args, "device_kind", "usb") == "usb"
            checks = doctor.run_checks(wda_url=wda_url_for(args), device=usb, env_file=args.env_file,
                                       udid=getattr(args, "device_udid", None))
            # Someone who never pointed Mobster at an iPhone may be here for `verify`: say where its checks are.
            phone_chosen = getattr(args, "wda_url", None) or getattr(args, "device", None) \
                or os.environ.get("MOBSTER_WDA_URL")
            hint = doctor.VERIFY_HINT if doctor.problems(checks) and not phone_chosen else None
            if args.json:
                output({"ok": not doctor.problems(checks), "checks": [check.public() for check in checks],
                        **({"hint": hint} if hint else {})})
            else:
                from .style import color_enabled
                print(doctor.plain_report(checks, color=color_enabled(sys.stdout), verify_hint=hint,
                                          width=_columns()))
            return EXIT_FAILED if doctor.problems(checks) else EXIT_OK
        if command == "history":
            from .history import print_history
            return print_history(limit=args.limit, as_json=args.json)
        if command == "export":
            from .run_export import export as export_run
            return export_run(args)
        if command == "screen":
            from .terminal_image import show_screen
            return show_screen(wda_url_for(args), out=args.out, width=args.width)
        if command == "build-ocr":
            import subprocess
            from pathlib import Path
            root = Path(__file__).parent
            build = build_dir()
            build.mkdir(parents=True, exist_ok=True)
            subprocess.run(["xcrun", "swiftc", "-O", str(root / "ocr.swift"),
                            "-o", str(build / "ocr")], check=True)
            output({"ok": True, "binary": str(build / "ocr")})
        elif command == "decide-fixture":
            from dataclasses import asdict
            from .demo import screen
            from .models import Jev
            model = Jev()
            try:
                output({"input": "synthetic_fixture", **asdict(model.decide(screen(), args.goal, []))})
            finally:
                model.http.close()
        elif command == "demo":
            from .agent import Agent
            from .demo import DemoDriver, DemoHelper, DemoModel
            emit = event_printer(args)
            if emit is output:
                output({"event": "start", "mode": "synthetic replay; no phone or model APIs"})
            else:
                emit.start("Search for coffee", "scripted replay · no phone, no model calls")
            try:
                Agent(DemoDriver(), DemoModel(), DemoHelper(), emit=emit).run(
                    "Search for coffee", execute=True, expected_text="Coffee brewing guide")
            finally:
                if emit is not output:
                    emit.close()
        elif command == "serve":
            from .server import serve
            if args.state_db is None:
                args.state_db = default_state_db(args)
            if getattr(args, "device", None) and not args.wda_url and not getattr(args, "manage_device", False):
                args.wda_url = device_target(args.device)["wdaUrl"]  # serve that device
            if getattr(args, "manage_device", False) and not args.wda_url:
                args.wda_url = DEFAULT_WDA_URL  # The relay the device manager runs.
            saved = os.environ.get("MOBSTER_ENABLE_LIVE")
            if saved in {"0", "1"}:
                args.enable_live = saved == "1"  # The setup flow's saved choice wins.
            serve(args)
        elif command == "run":
            return run_command(args)
        return EXIT_OK
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        # Exception messages here are locally authored; transport never includes provider bodies/keys.
        code = EXIT_NO_DEVICE if isinstance(exc, (NoDevice, MissingKey)) else EXIT_FAILED
        if command in {"run", "demo", "doctor", "history", "screen", "version", "login", "logout", "setup",
                       "update", "completion"} and sys.stdout.isatty() and not getattr(args, "json", False):
            print(error_line(exc), file=sys.stderr)
        elif command in ("run", "demo"):
            output(error_result(exc, code))
        else:
            output({"ok": False, "error_type": type(exc).__name__, "error": str(exc),
                    **({"stop_code": exc.code} if isinstance(exc, PhoneStopped) else {})})
        return code


def run_command(args):
    """`mobster run`: Smart (Mobster's agent) through the same runtime as the terminal UI, or Fast (Jev) through
    the agent loop directly, or the scripted demo."""
    engine = pick_engine(args)
    if engine in ("smart", "demo"):
        from .smart_run import run_smart
        return run_smart(args, demo=engine == "demo")
    from .drivers import resolve_wda_session
    args.wda_url = wda_url_for(args)
    if getattr(args, "demo", False) is not True:
        args.demo = False

    def target():
        from .transport import TransportError
        try:
            session = resolve_wda_session(args.wda_url, preferred=args.session)
        except (TransportError, OSError) as exc:
            # Refused, reset (iproxy with no phone behind it), timed out, an unknown host, or a
            # server that is not WDA: no phone answered, whichever way the connection failed.
            from .doctor import start_wda_hint
            raise NoDevice(
                f"Nothing answers at {args.wda_url} ({no_device_reason(exc)}). "
                f"{start_wda_hint(one_line=True)}; then run again (`mobster doctor` checks the setup).") from None
        return {"wda_url": args.wda_url, "session": session}
    return run_task(args, lease_key=args.wda_url, target=target)


if __name__ == "__main__":
    sys.exit(main())
