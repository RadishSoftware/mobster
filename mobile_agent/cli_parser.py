"""The `mobster` command's argument parser and help."""

import argparse
import sys

from . import __version__, harness_api, help_topics, links, traces
from .cli_constants import DEFAULT_WDA_URL, EXIT_OK, EXIT_USAGE
from .style import columns, palette

# The commands in the order `mobster --help` lists them (the docs generator reads it).
EVERYDAY = tuple(n for _, names in help_topics.GROUPS for n in names) + help_topics.MORE


class Parser(argparse.ArgumentParser):
    """argparse with no abbreviated options."""

    def __init__(self, *args, allow_abbrev=False, **kwargs):
        super().__init__(*args, allow_abbrev=allow_abbrev, **kwargs)

    def error(self, message):
        paint = palette(sys.stderr)
        message, tip = explain(self, message, paint)
        self.exit(EXIT_USAGE, usage_lines(paint, message, tip, self.prog, short_usage(self)))

    def print_help(self, file=None):
        file = file or sys.stdout
        text = self.format_help()
        paint = palette(file)
        if paint.enabled:
            text = colour_help(text, paint)
        file.write(text)


def usage_lines(paint, message, tip="", prog="mobster", usage=""):
    """A usage error the way uv prints one: a coloured `error:`, one `tip:`, the command's usage, and `More:`."""
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
    """(message, tip) for argparse's message, painted."""
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
    return next((action for action in parser._actions if isinstance(action, argparse._SubParsersAction)), None)


class Formatter(argparse.RawDescriptionHelpFormatter):
    """The help formatter every command's parser shares (`python -m mobile_agent.sim` uses it too)."""

    def __init__(self, prog):
        super().__init__(prog, max_help_position=30, width=min(100, columns()))


TRACE_HELP = ("\n\n--trace saves the task's trace as a zip instead: its journal (decisions, actions, cost\n"
              "and time per model call), its outcome, and the tape that replays it offline when the\n"
              "task ran with MOBSTER_EXPERIMENTAL_TRACES=1. Keys, codes and passwords are masked in\n"
              "it. The task text and the text on its screens are not, so read it before you share it.\n"
              "--screens adds the screenshots, which can't be masked.")

# A command's own options default to SUPPRESS: argparse copies a subcommand's defaults over the values
# parsed before it, so `mobster --wda-url URL run …` would otherwise drive the default phone.
COMMAND_DEFAULT = argparse.SUPPRESS


def wda_url_type(value):
    """--wda-url and $MOBSTER_WDA_URL: an http(s) address without credentials, else a usage error (exit 2)."""
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


def _bounded(kind, low, high=None, above=False):
    """An argparse type for a number from ``low`` (exclusive with ``above``) up to ``high``."""
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


def _long_budget(value):
    """--budget: 10m, 90s, 1h, 30 or all (longform.budget.from_cli); a usage error otherwise."""
    from .longform.budget import from_cli
    try:
        from_cli(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error).replace("--budget ", "")) from None
    return value


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
                        help="load KEY=VALUE lines (ANTHROPIC_API_KEY, OPENAI_API_KEY, ...); "
                             "variables already set win"
                             + (" [env: MOBSTER_ENV_FILE]" if default_from_environment else ""))


def _tui_options(parser, top_level=False):
    """``top_level``: the bare `mobster` parser, which sets the real defaults; `mobster tui` suppresses its own."""
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


def build_parser(load="all"):
    """The whole command line."""
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
        "`mobster login <plan or provider>` saves a plan's key or sign-in (Kimi Code, Grok ...)\n"
        "or another provider's key. A ChatGPT, Claude or Google plan runs through your own\n"
        "Codex, Claude Code or Gemini CLI, which signs in itself: `mobster login codex` says how.\n\n"
        "`mobster login status` shows which keys, plans and providers are saved."),
        epilog=("exit codes: 0 saved (or status shown), 1 the key or the sign-in didn't work, 2 usage,\n"
                "            130 ctrl+c\n\n"
                "examples:\n"
                "  mobster login\n"
                "  mobster login --provider openai\n"
                "  mobster login --provider anthropic --with-key < key.txt\n"
                "  mobster login kimi\n"
                "  mobster login status"))
    login.add_argument("action", nargs="?", metavar="status|PLAN|PROVIDER",
                       help="status: show what is saved, masked; a plan or provider: kimi, zai, grok, openrouter, "
                            "xai, custom ...; codex, claude or gemini: how that CLI signs in")
    login.add_argument("--provider", choices=["anthropic", "openai"], default=None,
                       help="anthropic (Claude, the default) or openai")
    login.add_argument("--with-key", action="store_true",
                       help="read the key from standard input (a pipe or a file, never a terminal)")
    login.add_argument("--skip-check", action="store_true", help="save it without testing it (no network)")
    login.add_argument("--json", action="store_true", help="print the result as JSON")
    login.add_argument("--model", metavar="ID", help="a plan or provider: the model Mobster's agent uses there")
    login.add_argument("--base-url", metavar="URL", dest="base_url",
                       help="custom, ollama or lmstudio: the endpoint's base URL, such as http://127.0.0.1:11434/v1")

    logout = command("logout", HELP["logout"], (
        "Remove a saved key from this Mac: from Mobster's settings file, which Mobster for Mac\n"
        "uses too. A key set in your shell or an --env-file stays where it is.\n\n"
        "`mobster logout <plan or provider>` forgets that plan's or provider's key or sign-in."),
        epilog="examples:\n  mobster logout\n  mobster logout --provider openai\n  mobster logout kimi")
    logout.add_argument("account", nargs="?", metavar="PLAN|PROVIDER",
                        help="forget a plan's or provider's key or sign-in, instead of removing a key")
    logout.add_argument("--provider", choices=["anthropic", "openai"], default=None,
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
        "Mobster's agent runs it on your Claude or OpenAI key. By default, it asks before it\n"
        "sends, buys, posts or deletes: on a terminal you answer y or n; with --json or a pipe\n"
        "the answer is no, and the run ends as declined.\n\n"
        "Long tasks (\"scroll basketball Reels for 10 minutes and tell me what you learned\",\n"
        "\"go through my unread emails\"): Mobster prints a plan with its estimate and asks\n"
        "Start? [Y/n] on a terminal (--yes starts at once). It covers the budget in as little\n"
        "phone time as it can, hands the phone back, then prints the report as Markdown.\n\n"
        "ctrl+c stops at the next safe point (twice to force)."),
        epilog=("exit codes: 0 done, 1 the task didn't finish, 2 usage error,\n"
                "            3 couldn't run (no phone answered, no key), 130 stopped with ctrl+c\n\n"
                "examples:\n"
                "  mobster run \"Turn on Dark Mode\" --execute\n"
                "  mobster run \"What's my battery level?\" --execute --json\n"
                "  mobster run \"Turn on Dark Mode\" --demo          a scripted phone: no iPhone, no keys\n"
                "  mobster run \"Scroll basketball Reels for 10 minutes and tell me what you learned\" --execute\n"
                "  mobster run \"Go through my unread emails\" --execute --long --budget all"))
    run.add_argument("goal", metavar="TASK", help="what to do, in plain words")
    target = run.add_mutually_exclusive_group()
    _wda_option(run, target)
    _device_option(run, target)
    _env_option(run)
    run.add_argument("--execute", action="store_true", help="act on the phone (a task needs it)")
    run.add_argument("--demo", action="store_true", default=COMMAND_DEFAULT,
                     help="a scripted phone and policy: no iPhone, no keys, no model calls")
    run.add_argument("--dry-run", action="store_true",
                     help="print the apps it would open, the acts it would ask about and the cost, and send nothing "
                          "to the phone")
    run.add_argument("--json", action="store_true", help="print raw JSON events, even on a terminal")
    # Checked here, as a usage error (exit 2), before anything reaches the phone.
    run.add_argument("--spend-cap-usd", type=_bounded(float, 0, above=True), default=None, metavar="USD",
                     help="stop before further model calls once this task has cost USD")
    run.add_argument("--state-db", type=path_type, default=None, metavar="PATH",
                     help="the private task journal this task is saved in (default: the terminal UI's, which "
                          "`mobster history` lists)")
    # Long tasks (mobile_agent/longform).
    run.add_argument("--long", action="store_true",
                     help="run it as a long task, with a plan first and a report at the end (most long tasks are "
                          "found on their own)")
    run.add_argument("--budget", type=_long_budget, default=None, metavar="VALUE",
                     help="a long task's budget: a time (10m, 90s, 1h), a number of items (30) or all")
    run.add_argument("--yes", action="store_true", help="start a long task without asking (its plan still prints)")

    doctor = command("doctor", HELP["doctor"], (
        "Check everything a task needs and print how to fix what is missing. Without --fix it\n"
        "changes nothing. Keys are shown masked: their first characters and last four.\n\n"
        "--fix does the fix each finding carries, then checks again and prints what it did and what\n"
        "it could not do. It starts Mobster's helper, or refreshes one that expired, on its own, and\n"
        "leaves it to Mobster for Mac when that is running. It names a program that holds a port, or a\n"
        "service that doesn't answer, and asks before it stops it. It hands over to `mobster login`\n"
        "and `mobster setup` for what needs your answers. It never enters a passcode, runs sudo or\n"
        "downloads a runtime. Without a terminal, or with --json, it skips every question and says\n"
        "which."),
        epilog=("exit codes: 0 ready for tasks (warnings allowed), 1 at least one problem\n\n"
                "examples:\n  mobster doctor\n  mobster doctor --fix"))
    _wda_option(doctor)
    _device_option(doctor)
    _env_option(doctor)
    doctor.add_argument("--fix", action="store_true",
                        help="do the fix each finding carries, then check again")
    doctor.add_argument("--json", action="store_true", help="print the checks as JSON")
    doctor.add_argument("--simulator", action="store_true",
                        help="skip the USB iPhone and Xcode checks (a simulator's phone)")
    doctor.add_argument("--apps", action="store_true", help=argparse.SUPPRESS)

    history = command("history", HELP["history"], (
        "List the tasks you ran in this terminal, newest first. Open one with\n"
        "`mobster --resume ID`, or save one as a GIF with `mobster export ID --gif`. The\n"
        "history holds task text and screen text: keep it private."))
    history.add_argument("--json", action="store_true", help="print one JSON object per task")
    history.add_argument("-n", "--limit", type=_bounded(int, 1, 10000), default=20, metavar="N",
                         help="show N tasks (default: 20)")
    history.add_argument("--to-eval", metavar="ID", help=(
        "keep a finished task as a regression task in your evals, to replay offline" if traces.enabled()
        else argparse.SUPPRESS))

    traces_on = traces.enabled()  # the trace options stay out of the help until MOBSTER_EXPERIMENTAL_TRACES is set
    export = command("export", HELP["export"], (
        "Save a finished task as a captioned GIF, one screen per step with what Mobster did,\n"
        "ready to post: under X's 15 MB limit, on the Paper or Night ground. --mp4 saves an MP4\n"
        "too (it needs ffmpeg). The file goes to ~/Downloads and never replaces one there.\n"
        "It shows every screen of the task, so check it before you post it: --redact blurs\n"
        "text fields and messages and leaves the answer out. Nothing is uploaded."
        + (TRACE_HELP if traces_on else "")),
        epilog=("exit codes: 0 saved, 1 couldn't (no such task, no screens, no ffmpeg for --mp4), 2 usage\n\n"
                "examples:\n"
                "  mobster export latest --gif\n"
                "  mobster export 3f9c2a1b7d10 --gif --mp4 --theme night\n"
                "  mobster export 3f9c --gif --redact -o ~/Desktop/clip.gif"
                + ("\n  mobster export 3f9c --trace -o ~/Desktop/" if traces_on else "")))
    export.add_argument("run_id", metavar="ID", help="a task's ID from `mobster history` (its first characters do), "
                                                     "or latest")
    export.add_argument("--gif", action="store_true", help="save a GIF")
    export.add_argument("--mp4", action="store_true", help="save an MP4 (needs ffmpeg)")
    export.add_argument("--trace", action="store_true", help=(
        "save the task's trace bundle (a zip) for a bug report or an offline replay"
        if traces_on else argparse.SUPPRESS))
    export.add_argument("--screens", action="store_true", help=(
        "with --trace, add the screenshots (they can't be masked: check them first)" if traces_on
        else argparse.SUPPRESS))
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
                       help="leave the iPhone runner running on exit, for development reloads; "
                       "the next server adopts it")
    _env_option(serve, default_from_environment=False)
    serve.add_argument("--session", metavar="ID", help="preferred WDA session; the one WDA is serving always wins")
    serve.add_argument("--enable-live", action="store_true", help="allow tasks to act on the phone")
    serve.add_argument("--spend-cap-usd", type=_bounded(float, 0, above=True), default=None, metavar="USD",
                       help="stop a run before further model calls once its observed inference spend reaches USD")
    serve.add_argument("--state-db", type=path_type, default=None, metavar="PATH",
                       help="the private task journal (never automatically replays interrupted tasks); "
                            "default: .state/ beside the source in a checkout, else <data dir>/state/")

    devices = command("devices", HELP["devices"], (
        "List every device Mobster can drive: USB iPhones (set up or newly plugged in),\n"
        "Mobster's simulators, and phone addresses from MOBSTER_WDA_DEVICES. Each shows\n"
        "its state: Ready, In use, Connected, Needs setup or Unplugged. Name one with\n"
        "--device on run, screen, doctor, serve, mcp and the terminal UI. It changes nothing.\n"
        "\n"
        "`mobster devices lock-probe` reads whether an iPhone is locked or showing a system\n"
        "sheet (Face ID, a locked app, the App Store's side-button confirmation) the way a\n"
        "task's check does, and lists what the lock screen shows. It taps and types nothing.\n"
        "\n"
        "`mobster devices status` reads a device's battery and thermal state, and whether\n"
        "this Mac is on battery. A value the device doesn't report is left out.\n"
        "Without --device it reads the only device, else the first iPhone set up."),
        epilog=("examples:\n"
                "  mobster devices\n"
                "  mobster devices --json\n"
                "  mobster devices lock-probe --device \"Sam's iPhone\"\n"
                "  mobster devices status --device \"Sam's iPhone\"\n"
                "  mobster run \"Turn on Dark Mode\" --execute --device \"Sam's iPhone\""))
    devices.add_argument("action", nargs="?", choices=["lock-probe", "status"], metavar="lock-probe|status",
                         help="lock-probe: the lock state and system sheets; status: battery and heat. "
                              "Neither taps anything")
    devices.add_argument("--json", action="store_true", help="print the devices (or the probe, or the status) as JSON")
    _wda_option(devices, help="lock-probe, status: the phone's address (default: $MOBSTER_WDA_URL, else "
                              f"{DEFAULT_WDA_URL})")
    _device_option(devices, help="lock-probe, status: the device to read, by id, UDID or name")

    demo = command("demo", HELP["demo"], (
        "Run one task offline, \"Search for coffee\", on a scripted phone and model: the real\n"
        "runtime and agent loop, no iPhone, no keys and no model calls. The same as `mobster\n"
        "run \"Search for coffee\" --demo`. For the terminal UI on it, run `mobster --demo`."))
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
        "Say whether a newer Mobster is out. New versions come in Mobster for Mac, which\n"
        "updates itself: https://mobster.dev/download. It installs nothing, and the mobster\n"
        "you have keeps working. Mobster checks once a day and says so after a command;\n"
        "MOBSTER_NO_UPDATE_CHECK=1 turns that off."),
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

    commands = {"run": run, "serve": serve, "run_target": target, "doctor": doctor, "screen": screen}
    for extension in harness_api.hooks().cli:
        extension.add_arguments(subs, commands)
    parser.commands = subs.choices  # for check_placement
    _tidy(parser)
    return parser


class TopHelp(argparse._HelpAction):
    """`mobster -h` with other options (`mobster --demo -h`): the same grouped help as `mobster --help`."""

    def __call__(self, parser, namespace, values, option_string=None):
        from . import help_topics
        print(help_topics.top_level(palette(sys.stdout), columns()))
        parser.exit()


def _tidy(parser):
    """Every parser's -h says "show this help and exit"; a SUPPRESSed subcommand stays out of its parent's list."""
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
                # Python 3.12 lists a SUPPRESSed subcommand as "==SUPPRESS==".
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
        print(help_topics.all_commands(palette(sys.stdout), helps))
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
        print(help_topics.top_level(palette(sys.stdout), columns()))
        return EXIT_OK
    target.print_help()
    return EXIT_OK


def print_topic(name):
    from . import help_topics
    paint, width = palette(sys.stdout), columns()
    if name == "environment":
        print(help_topics.environment(paint, width))
    elif name == "exit-codes":
        print(help_topics.exit_codes(paint, width))
    else:
        from .tui.keys import KEYS
        print(help_topics.tui(paint, width, KEYS))
    return EXIT_OK
