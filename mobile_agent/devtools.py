"""The developer commands: `mobster verify`, `mobster sim`, `mobster mcp`, `mobster phone` and `mobster alerts`, and
the SOTA tracks' `mobster chat`, `mobster memory`, `mobster test` and `mobster wifi` (seam S9).

Each lives in its own package (mobile_agent.verify, mobile_agent.sim, mobile_agent.mcp_server, mobile_agent.phone_io,
mobile_agent.threads, mobile_agent.memory, mobile_agent.testkit, mobile_agent.wireless) whose ``cli`` module has two
functions: ``add_arguments(parser, helpers)`` and ``run(args) -> int`` (alerts: the module mobile_agent.alerts itself).
This module creates their subparsers with fixed help and descriptions, so `mobster --help` reads the same whichever of
them a build has, and imports each module with a static ``from .x import cli`` inside a function, which PyInstaller follows. A build
without one prints "`mobster sim` is not in this build" and exits 3.
"""

import argparse
import contextlib
import json
import os
import signal
import sys
import threading

COMMANDS = ("verify", "mcp", "sim", "phone", "alerts") + ("chat", "memory", "test", "wifi")
MISSING_EXIT = 3
PACKAGES = {"verify": "mobile_agent.verify", "sim": "mobile_agent.sim", "mcp": "mobile_agent.mcp_server",
            "phone": "mobile_agent.phone_io", "alerts": "mobile_agent.alerts"}
PACKAGES.update({"chat": "mobile_agent.threads", "memory": "mobile_agent.memory",
                 "test": "mobile_agent.testkit", "wifi": "mobile_agent.wireless"})

TEXT = {
    "verify": (
        "check an iOS app on a simulator and print the verdict",
        "Check an iOS app on a headless simulator that Mobster manages. Mobster installs and\n"
        "launches the app, runs the steps (Smart, on your OpenAI or Anthropic key) or none\n"
        "(a launch-only check), and decides the verdict from your expectations on the\n"
        "accessibility tree, never from a model. Each run writes a report with the frames\n"
        "that prove it.",
        "exit codes: 0 passed, 1 failed, 2 needs review, 3 couldn't run (usage errors too),\n"
        "            130 ctrl+c\n\n"
        "examples:\n"
        "  mobster verify --bundle com.apple.Preferences --text General\n"
        "  mobster verify --app build/Daybreak.app --open-url daybreak://paywall --text \"Choose your plan\"\n"
        "  mobster verify \"Open General, then About\" --bundle com.apple.Preferences --text \"iOS Version\"\n"
        "  mobster verify --check .mobster/checks/paywall.yaml --json"),
    "sim": (
        "create, prepare and clean up Mobster's simulators",
        "Manage the headless simulators that `mobster verify` and `mobster mcp` run on: list\n"
        "them, prepare one (create, boot, build and start WebDriverAgent), shut them down,\n"
        "erase or delete them. It deletes only simulators Mobster created.",
        None),
    "mcp": (
        "serve Mobster's checks to coding agents over MCP (stdio)",
        "Serve Mobster as an MCP server on stdio, so your coding agent can launch and check\n"
        "an iOS app on a simulator: it starts a check with verify_start, drives the app with\n"
        "screen, tap, type_text and swipe, and gets the verdict from verify_finish.",
        "examples:\n"
        "  mobster mcp install --all      add Mobster to Claude Code, Codex, Cursor and the others found here\n"
        "  mobster mcp clients            which agent clients are here, and which run Mobster\n"
        "  mobster mcp doctor             start the server as a client would and call status"),
    "phone": (
        "set the iPhone's clipboard, move files, or install a build on it",
        "Phone utilities that aren't tasks: put text on the phone's clipboard (or print what it\n"
        "holds), and install a signed .app or .ipa on a USB iPhone or a Mobster simulator.",
        "examples:\n"
        "  mobster phone clipboard set \"https://example.com/invite/42\"\n"
        "  mobster phone clipboard get\n"
        "  mobster phone file put menu.pdf --app com.apple.Pages\n"
        "  mobster phone install build/Daybreak.ipa --device \"Work iPhone\""),
    "alerts": (
        "test the webhook that hears about scheduled tasks",
        "Mobster posts to MOBSTER_ALERT_WEBHOOK_URL (https: Slack, Discord, ntfy or any JSON\n"
        "webhook) when a scheduled task fails, stops or waits a minute for your approval.\n"
        "The post never holds the task's answer or anything it typed.",
        "example:\n"
        "  mobster alerts test"),
}
TEXT.update({
    "chat": ("talk to Mobster's agent in a conversation that remembers",
             "Start or continue a conversation with Mobster's agent on your iPhone. Follow-ups use\n"
             "what earlier tasks found; type while it works to steer it. Uses Mobster for Mac or\n"
             "`mobster serve` when one is open, so approvals show there too. With neither open, a\n"
             "message runs here in the terminal, and `mobster chat` alone opens the terminal UI.",
             "examples:\n  mobster chat\n  mobster chat \"What's on my calendar tomorrow?\"\n"
             "  mobster chat --thread 3f9c2a1b7d0e \"Now text Sam the first one\""),
    "memory": ("see and edit what Mobster remembers",
               "List, add and delete the things Mobster remembers, in your words. Mobster's agent\n"
               "reads the ones that fit each task. Everything stays on this Mac.",
               "examples:\n  mobster memory list\n  mobster memory add \"My gym is the one on 5th Street\"\n"
               "  mobster memory pin 3f9c2a1b7d0e"),
    "test": ("run your app's checks on simulators and iPhones, with reports",
             "Run every check in .mobster/checks (or the paths given) on one or more devices, in\n"
             "parallel, with retries, flake detection, and JUnit and HTML reports with the frames.",
             "exit codes: 0 passed, 1 failed, 2 needs review, 3 couldn't run, 130 ctrl+c\n\n"
             "examples:\n  mobster test\n"
             "  mobster test --sim \"iPhone 17 Pro\" --sim \"iPhone SE (3rd generation)\" --parallel 2\n"
             "  mobster test --junit build/junit.xml --html build/mobster-report\n"
             "  mobster test record --run 3f9c2a1b7d0e --name paywall"),
    "wifi": ("use an iPhone without the cable, over the encrypted Wi-Fi link",
             "Turn on Wi-Fi use for an iPhone you set up with a cable once, see how each phone is\n"
             "connected, and check the encrypted link between this Mac and the phone.",
             "examples:\n  mobster wifi status\n  mobster wifi on --device \"Sam's iPhone\"\n"
             "  mobster wifi probe --device \"Sam's iPhone\""),
})

from .help_topics import HELP as _HELP  # noqa: E402

TEXT = {name: (_HELP.get(name, value[0]),) + tuple(value[1:]) for name, value in TEXT.items()}


def load(name):
    """The command's ``cli`` module, or None when this build doesn't have it."""
    try:
        if name == "verify":
            from .verify import cli
        elif name == "sim":
            from .sim import cli
        elif name == "mcp":
            from .mcp_server import cli
        elif name == "phone":
            from .phone_io import cli
        elif name == "alerts":
            from . import alerts as cli
        elif name == "chat":
            from .threads import cli
        elif name == "memory":
            from .memory import cli
        elif name == "test":
            from .testkit import cli
        elif name == "wifi":
            from .wireless import cli
        else:
            return None
    except ImportError as error:
        package = PACKAGES[name]
        if getattr(error, "name", None) in (package, package + ".cli"):
            return None
        raise
    return cli


def load_integrations():
    """`mobster mcp install`, `clients` and `doctor` (mobile_agent.integrations.cli), or None in a build without
    them."""
    try:
        from .integrations import cli
    except ImportError as error:
        if getattr(error, "name", None) in ("mobile_agent.integrations", "mobile_agent.integrations.cli"):
            return None
        raise
    return cli


ORDER = ("verify", "sim", "mcp", "phone", "alerts", "chat", "memory", "test", "wifi")


def add_commands(command, helpers, load_only=None):
    """Create the subparsers through ``command(name, help, description, epilog)`` (``__main__``'s factory) and let
    each module add its flags. Returns {name: parser}.

    ``load_only``: the commands whose modules are imported (the one being run, or whose help is asked for); the
    others get their fixed help and no flags, so `mobster --version` and another command's run never pay for
    importing them. None imports every one (the docs generator, completion, tests)."""
    parsers = {}
    for name in ORDER:
        help_text, description, epilog = TEXT[name]
        parser = command(name, help_text, description, epilog)
        if load_only is not None and name not in load_only:
            parser.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
            parser.set_defaults(unloaded_command=True)
            parsers[name] = parser
            continue
        module = load(name)
        if module is None:
            parser.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
            parser.set_defaults(stub_command=True)
        else:
            module.add_arguments(parser, helpers)
            if name == "mcp" and load_integrations() is not None:
                load_integrations().add_subcommands(parser, helpers)  # install, clients, doctor
        parsers[name] = parser
    return parsers


# The signals `verify` and `sim` stop on the way ctrl+c stops them; `mcp` handles its own (mcp_server/cli.py).
STOP_SIGNALS = ("SIGTERM", "SIGHUP")


class Terminated(KeyboardInterrupt):
    """SIGTERM or SIGHUP, raised where ctrl+c would be: the run ends as stopped, writes its artifacts, and the
    simulator build it started is killed. ``signum`` gives the exit code, 128 + signum."""

    def __init__(self, signum):
        super().__init__(signal.Signals(signum).name)
        self.signum = signum


@contextlib.contextmanager
def signals_as_interrupt():
    """SIGTERM and SIGHUP raise ``Terminated`` (a KeyboardInterrupt) in the main thread for the duration; yields a
    list that holds the signal number once one arrived. Off the main thread, nothing changes."""
    caught = []
    if threading.current_thread() is not threading.main_thread():
        yield caught
        return

    def handler(signum, frame):
        first = not caught
        caught.append(signum)
        if first:
            raise Terminated(signum)

    previous = {}
    for name in STOP_SIGNALS:
        number = getattr(signal, name, None)
        if number is not None:
            previous[number] = signal.signal(number, handler)
    try:
        yield caught
    finally:
        for number, old in previous.items():
            signal.signal(number, old)


def run(args):
    """Run the developer command ``args.command``; returns its exit code. For verify and sim, SIGTERM and SIGHUP
    stop the command as ctrl+c does, and the exit code is 128 + the signal's number."""
    name = args.command
    module = load(name)
    if module is None:
        print(f"`mobster {name}` is not in this build", file=sys.stderr, flush=True)
        return MISSING_EXIT
    if name in ("chat", "memory", "test", "wifi"):
        # The SOTA tracks' commands register their pieces first (seam S1).
        from . import tracks
        tracks.load()
    if name == "mcp" and getattr(args, "mcp_command", None):
        return load_integrations().run(args)
    if name not in ("verify", "sim"):
        return module.run(args)
    with signals_as_interrupt() as caught:
        try:
            code = module.run(args)
        except KeyboardInterrupt:
            code = 130
    return 128 + caught[0] if caught else code


def usage_exit(command, message=None, *, as_json=False):
    """The exit code for a usage error found outside a command's own parser (`mobster --demo verify`). verify
    also prints its couldnt_run result (class usage) on stdout when its output is JSON: with --json, or when
    stdout isn't a terminal, as its own usage errors do."""
    if command != "verify":
        return 2
    module = load("verify")
    if module is not None and message and (as_json or not _isatty(sys.stdout)):
        try:
            print(json.dumps(module.usage_result(message), ensure_ascii=False), flush=True)
        except BrokenPipeError:
            pass
    return 3


def option_hint(parser, message):
    """" Did you mean --execute?" for argparse's "unrecognized arguments: --exec", else "": an option the word
    begins, or failing that the closest one."""
    import difflib
    if not message.startswith("unrecognized arguments:"):
        return ""
    flags = [option for option in parser._option_string_actions if option.startswith("--")]
    hints = []
    for word in message.split(":", 1)[1].split():
        if word.startswith("--") and word not in flags:
            stem = word.split("=", 1)[0]
            hints += [flag for flag in flags if flag.startswith(stem)][:1] or \
                difflib.get_close_matches(stem, flags, n=1, cutoff=.6)
    return f". Did you mean {' and '.join(dict.fromkeys(hints))}?" if hints else ""


class ProgressLines:
    """verify's and sim's progress on stderr, one line each. On a terminal the "Still …" heartbeat of a long phase
    (sim/wda.py's heartbeat: the first build, a boot) redraws one line in place instead of adding a line every 30
    seconds; the next line replaces it, and ``end()`` clears one left over."""

    def __init__(self, stream=None):
        self.stream = stream or sys.stderr
        self.live = _isatty(self.stream) and os.environ.get("TERM") != "dumb"
        self.pending = False
        self.lock = threading.Lock()

    def __call__(self, line):
        with self.lock:
            try:
                if self.live and str(line).startswith("Still "):
                    self.stream.write("\r\033[K" + str(line))
                    self.pending = True
                else:
                    self.stream.write(("\r\033[K" if self.pending else "") + str(line) + "\n")
                    self.pending = False
                self.stream.flush()
            except (OSError, ValueError):
                pass

    def end(self):
        with self.lock:
            if self.pending:
                try:
                    self.stream.write("\r\033[K")
                    self.stream.flush()
                except (OSError, ValueError):
                    pass
                self.pending = False


def _isatty(stream):
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False
