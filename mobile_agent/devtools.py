"""The developer commands in ORDER: their fixed help, and each command's module loaded only when it runs."""

import argparse
import contextlib
import json
import os
import signal
import sys
import threading

from .help_topics import HELP

# The commands in the order `mobster --help` lists them.
ORDER = ("verify", "sim", "mcp", "phone", "alerts", "audit", "chat", "workflow", "memory", "test", "wifi", "api", "ci",
         "account", "expo", "asc", "testflight", "xcode", "fixtures", "links", "xray", "env", "signing")
# `mobster intents` is hidden unless the flag is on: the help snapshots and the docs reference never list it.
if os.environ.get("MOBSTER_EXPERIMENTAL_INTENTS", "").strip().casefold() in ("1", "true", "yes", "on"):
    ORDER += ("intents",)
    HELP["intents"] = "read a simulator's apps for the links they declare (experimental)"
MISSING_EXIT = 3
PACKAGES = {"verify": "mobile_agent.verify", "sim": "mobile_agent.sim", "mcp": "mobile_agent.mcp_server",
            "phone": "mobile_agent.phone_io", "alerts": "mobile_agent.alerts", "audit": "mobile_agent.audit_verify"}
PACKAGES.update({"chat": "mobile_agent.threads", "workflow": "mobile_agent.threads", "memory": "mobile_agent.memory",
                 "test": "mobile_agent.testkit", "wifi": "mobile_agent.wireless", "api": "mobile_agent.app_api"})
PACKAGES["xray"] = "mobile_agent.xray"
PACKAGES["intents"] = "mobile_agent.intents"
PACKAGES["env"] = "mobile_agent.project_env_cli"
PACKAGES["signing"] = "mobile_agent.team_signing"
PACKAGES.update({name: "mobile_agent.devkit" for name in ("ci", "account", "expo", "asc", "testflight", "xcode",
                                                          "fixtures", "links")})
# The module that holds a command's add_arguments and run, where it isn't the package's ``cli``.
CLI_MODULES = {"workflow": "workflow_cli", "ci": "ci_cli", "account": "account_cli", "expo": "expo_cli",
               "asc": "asc_cli", "testflight": "testflight_cli", "xcode": "xcode_cli", "fixtures": "fixtures_cli",
               "links": "links_cli"}

# name: (description, epilog). The one-line help each command shows in `mobster --help` is help_topics.HELP.
TEXT = {
    "verify": (
        "Check an iOS app on a headless simulator that Mobster manages. Mobster installs and\n"
        "launches the app, runs the steps (Smart, on your OpenAI or Anthropic key) or none\n"
        "(a launch-only check), and checks your declared assertions. No model grades the\n"
        "outcome. Each run writes a report, with frames when available.\n\n"
        "A check is a saved YAML file. verify is the one-off form of `mobster test`: it runs one\n"
        "check, from flags or --check FILE, and --save NAME keeps it for `mobster test`.",
        "exit codes: 0 passed, 1 failed, 2 needs review, 3 couldn't run (usage errors too),\n"
        "            130 ctrl+c\n\n"
        "examples:\n"
        "  mobster verify --bundle com.apple.Preferences --text General\n"
        "  mobster verify --app build/YourApp.app --open-url yourapp://lesson --text \"Fill in the blank\"\n"
        "  mobster verify \"Open General, then About\" --bundle com.apple.Preferences --text \"iOS Version\"\n"
        "  mobster verify --check .mobster/checks/lesson-complete.yaml --json"),
    "sim": (
        "Manage the headless simulators that `mobster verify` and `mobster mcp` run on: list\n"
        "them, prepare one (create, boot, build and start WebDriverAgent), shut them down,\n"
        "erase or delete them. It deletes only simulators Mobster created.",
        None),
    "mcp": (
        "Serve Mobster as an MCP server on stdio, so your coding agent can launch and check\n"
        "an iOS app on a simulator: it starts a check with verify_start, drives the app with\n"
        "screen, tap, type_text and swipe, and gets the verdict from verify_finish.",
        "examples:\n"
        "  mobster mcp install --all      add Mobster to Claude Code, Codex, Cursor and the others found here\n"
        "  mobster mcp clients            which agent clients are here, and which run Mobster\n"
        "  mobster mcp doctor             start the server as a client would and call status"),
    "phone": (
        "Phone utilities that aren't tasks: put text on the phone's clipboard (or print what it\n"
        "holds), and install a signed .app or .ipa on a USB iPhone or a Mobster simulator.",
        "examples:\n"
        "  mobster phone clipboard set \"https://example.com/invite/42\"\n"
        "  mobster phone clipboard get\n"
        "  mobster phone file put menu.pdf --app com.apple.Pages\n"
        "  mobster phone install build/YourApp.ipa --device \"Work iPhone\""),
    "alerts": (
        "Mobster posts to MOBSTER_ALERT_WEBHOOK_URL (https: Slack, Discord, ntfy or any JSON\n"
        "webhook) when a scheduled task fails, stops or waits a minute for your approval.\n"
        "The post never holds the task's answer or anything it typed.",
        "example:\n"
        "  mobster alerts test"),
    "audit": (
        "Check a Team plan audit export offline: the hash chain, the signed head of the API's\n"
        "export, and with --keys every Mac's signature. Nothing leaves this Mac.",
        "examples:\n"
        "  mobster audit verify audit.jsonl\n"
        "  mobster audit verify audit.jsonl --keys keys.json\n"
        "  mobster audit verify export.json"),
}
TEXT.update({
    "chat": ("Start or continue a conversation with Mobster's agent on your iPhone. Follow-ups use\n"
             "what earlier tasks found; type while it works to steer it. Uses Mobster for Mac or\n"
             "`mobster serve` when one is open, so approvals show there too. With neither open, a\n"
             "message runs here in the terminal, and `mobster chat` alone opens the terminal UI.",
             "examples:\n  mobster chat\n  mobster chat \"What's on my calendar tomorrow?\"\n"
             "  mobster chat --thread 3f9c2a1b7d0e \"Now text Sam the first one\"\n"
             "  mobster chat --workflow \"Reels digest\" --set topic=basketball"),
    "workflow": ("Print a saved workflow's steps from Mobster for Mac or `mobster serve`, in the order\n"
                 "they run: each one's type, title and the steps it waits for. `mobster chat --workflow`\n"
                 "runs one.",
                 "examples:\n  mobster workflow show \"Answer DMs\"\n  mobster workflow show "
                 "3f9c2a1b7d0e --draft --json"),
    "memory": ("List, add and delete the things Mobster remembers, in your words. Mobster's agent\n"
               "reads the ones that fit each task. Everything stays on this Mac.",
               "examples:\n  mobster memory list\n  mobster memory add \"My gym is the one on 5th Street\"\n"
               "  mobster memory pin 3f9c2a1b7d0e"),
    "test": ("Run every check in .mobster/checks (or the paths given) on one or more devices, in\n"
             "parallel, with retries, flake detection, and JUnit and HTML reports with the frames.\n"
             "A check is a saved YAML file; `mobster verify` runs one without saving it.",
             "exit codes: 0 passed, 1 failed, 2 needs review, 3 couldn't run, 130 ctrl+c\n\n"
             "examples:\n  mobster test\n"
             "  mobster test --sim \"iPhone 17 Pro\" --sim \"iPhone SE (3rd generation)\" --parallel 2\n"
             "  mobster test --junit build/junit.xml --html build/mobster-report\n"
             "  mobster test record --run 3f9c2a1b7d0e --name paywall"),
    "wifi": ("Turn on Wi-Fi use for an iPhone you set up with a cable once, see how each phone is\n"
             "connected, and check the encrypted link between this Mac and the phone.",
             "examples:\n  mobster wifi status\n  mobster wifi on --device \"Sam's iPhone\"\n"
             "  mobster wifi probe --device \"Sam's iPhone\""),
    "api": ("Serve every app Mobster has an app pack for as an API on this Mac: GET /v1/<app>/<thing>\n"
            "reads that app on your iPhone into rows (JSON or CSV), and GET /v1/openapi.json\n"
            "describes every endpoint. Each one is also an MCP tool in `mobster mcp`. Every\n"
            "endpoint only reads, and each client calls it with its own token.",
            "examples:\n  mobster api tokens create n8n\n  mobster api serve --phone \"Test iPhone\"\n"
            "  mobster api tokens revoke n8n"),
    "ci": ("Run your app's checks on every pull request or merge request, on a hosted macOS runner\n"
           "(simulators, signed in with a CI token) or a Mac of yours (its simulators and the iPhones\n"
           "plugged into it), in GitHub Actions, GitLab CI, Xcode Cloud, Codemagic, Bitrise or CircleCI.\n"
           "One comment on the PR keeps the verdict, the repro steps, frames, GIFs and videos.",
           "examples:\n  mobster ci init\n  mobster ci doctor\n  mobster ci run --device \"Test iPhone\"\n"
           "  mobster ci comment --dry-run"),
    "account": ("Manage your Mobster account from the terminal. For now that is CI tokens: the secret a hosted CI\n"
                "runner signs in with to run simulator checks, with no Mac of yours.",
                "examples:\n  mobster account ci-token create \"app repo\"\n  mobster account ci-token list\n"
                "  mobster account ci-token revoke <ID>"),
    "expo": ("Open your Expo app on a simulator or your iPhone from the JavaScript Metro serves, in\n"
             "your dev client or Expo Go, and check a screen. Build it for checks and CI: Release\n"
             "for the simulator, with the JavaScript inside, or your dev client on your iPhone.",
             "examples:\n  mobster expo preview --device \"Test iPhone\" --expect \"Welcome back\"\n"
             "  mobster expo reload\n  mobster expo build\n  mobster expo build --device \"Test iPhone\""),
    "asc": ("Use your own App Store Connect API key, kept in your Keychain, to list builds and read\n"
            "your testers' TestFlight feedback and crashes, and make App Store screenshots from your\n"
            "checks in every size and language. Read-only: Mobster submits nothing to Apple.",
            "examples:\n  mobster asc key add --key-id 2X9R4HXF34 --issuer-id <issuer id> --p8 AuthKey_2X9R4HXF34.p8\n"
            "  mobster asc builds --app com.example.yourapp\n  mobster asc feedback --app "
            "com.example.yourapp --crashes\n"
            "  mobster asc screenshots --size 6.9 --size 13"),
    "testflight": ("List your app's TestFlight builds, pick one and install it on your iPhone through the\n"
                   "TestFlight app, with an App Store Connect API key kept per project in your Keychain. The\n"
                   "build and version go on the next `mobster test` report. Read-only: Mobster submits nothing\n"
                   "to Apple.",
                   "examples:\n  mobster testflight key add --key-id 2X9R4HXF34 --issuer-id <issuer id> "
                   "--p8 AuthKey_2X9R4HXF34.p8\n"
                   "  mobster testflight list --app com.example.yourapp\n"
                   "  mobster testflight install --app com.example.yourapp --build 87 --device \"Test iPhone\""),
    "signing": ("Put Mobster's helper on an iPhone without Xcode, for a paid Apple Developer team. With your own\n"
                "App Store Connect key, `card` reads your team and prints the three requests Mobster would send to\n"
                "Apple (register your iPhone, create a development certificate, create a development profile),\n"
                "and `apply` sends them after you approve their digest, signs the helper on this Mac and installs\n"
                "it with pymobiledevice3. Off unless MOBSTER_EXPERIMENTAL_NO_XCODE=1.",
                "examples:\n  mobster signing card\n  mobster signing apply --approve <digest>\n"
                "  mobster signing export helper.tgz\n  mobster signing import helper.tgz --team ABCDE12345"),
    "xray": ("X-ray an app on your iPhone. `app` maps an app with App X-ray: Mobster taps through every\n"
             "control that opens a screen and writes a map of the screens, how they connect and the paywalls\n"
             "it found, with no model and no tap on anything that could act. `network` records the phone's\n"
             "traffic with Network X-ray and says which domains and companies each app talked to, reading\n"
             "hostnames, sizes and times only. `diff` compares two maps to see what an update changed.\n"
             "Everything stays on this Mac.",
             "examples:\n  mobster xray app com.apple.Preferences\n  mobster xray app Weather "
             "--screens 60 --minutes 10\n"
             "  mobster xray network --device \"Test iPhone\" --seconds 30\n"
             "  mobster xray diff before/map.json after/map.json"),
    "intents": ("Experimental (MOBSTER_EXPERIMENTAL_INTENTS=1). `harvest` reads a simulator's apps for the URL\n"
                "schemes and associated domains they declare, and keeps them as discovered links the agent can\n"
                "open before it taps.",
                "examples:\n  mobster intents harvest\n  mobster intents harvest --sim <UDID> --force"),
    "env": ("Keep a project's test credentials and other secrets in your Keychain, named in\n"
            ".mobster/env.example (names only, committed). A check writes ${NAME} in its launch\n"
            "settings and steps; the run fills it in and masks it everywhere. A name with no value\n"
            "stops the run before it starts. Values are never printed or written to the project.",
            "examples:\n  mobster env set TEST_PASSWORD\n  printf '%s' \"$PW\" | mobster env "
            "set TEST_PASSWORD --stdin\n"
            "  mobster env list\n  mobster env unset TEST_PASSWORD"),
    "links": ("List the deep links your project has, from .mobster/links.yaml, the Expo config, the Xcode\n"
              "project and links a check opened, and open each on a simulator to see which screen it\n"
              "lands on. A step can then say \"open cart\" instead of the address.",
              "examples:\n  mobster links list\n  mobster links check\n  mobster links check "
              "cart --sim \"iPhone 17 Pro\""),
    "fixtures": ("List the app states your checks start from (.mobster/fixtures/: a reset, launch args, seed files,\n"
                 "the keychain, the pasteboard, sign-in steps) and put a simulator's app in one now.",
                 "examples:\n  mobster fixtures list\n  mobster fixtures apply logged-in-cart "
                 "--bundle com.example.shop"),
    "xcode": ("Add a post-action to your Xcode scheme: after each simulator build, Mobster runs the\n"
              "checks of the app you built, on that build, in the background, and the verdict comes\n"
              "as a notification.",
              "examples:\n  mobster xcode hook --scheme YourApp\n  mobster xcode hook --scheme YourApp --tag smoke\n"
              "  mobster xcode unhook --scheme YourApp"),
})


def load(name):
    """The command's ``cli`` module, or None when this build doesn't have it. Static imports,
    so PyInstaller bundles them."""
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
        elif name == "audit":
            from . import audit_verify as cli
        elif name == "chat":
            from .threads import cli
        elif name == "workflow":
            from .threads import workflow_cli as cli
        elif name == "memory":
            from .memory import cli
        elif name == "test":
            from .testkit import cli
        elif name == "wifi":
            from .wireless import cli
        elif name == "api":
            from .app_api import cli
        elif name == "ci":
            from .devkit import ci_cli as cli
        elif name == "account":
            from .devkit import account_cli as cli
        elif name == "expo":
            from .devkit import expo_cli as cli
        elif name == "asc":
            from .devkit import asc_cli as cli
        elif name == "testflight":
            from .devkit import testflight_cli as cli
        elif name == "links":
            from .devkit import links_cli as cli
        elif name == "fixtures":
            from .devkit import fixtures_cli as cli
        elif name == "xcode":
            from .devkit import xcode_cli as cli
        elif name == "xray":
            from .xray import cli
        elif name == "env":
            from . import project_env_cli as cli
        elif name == "intents":
            from .intents import cli
        elif name == "signing":
            from .team_signing import cli
        else:
            return None
    except ImportError as error:
        package = PACKAGES[name]
        if getattr(error, "name", None) in (package, f"{package}.{CLI_MODULES.get(name, 'cli')}"):
            return None
        raise
    return cli


def load_integrations():
    """`mobster mcp install`, `clients` and `doctor`."""
    try:
        from .integrations import cli
    except ImportError as error:
        if getattr(error, "name", None) in ("mobile_agent.integrations", "mobile_agent.integrations.cli"):
            return None
        raise
    return cli


def add_commands(command, helpers, load_only=None):
    """Create the subparsers through ``command(name, help, description, epilog)``."""
    parsers = {}
    for name in ORDER:
        description, epilog = TEXT[name]
        parser = command(name, HELP[name], description, epilog)
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
    """SIGTERM or SIGHUP, raised where ctrl+c would be."""

    def __init__(self, signum):
        super().__init__(signal.Signals(signum).name)
        self.signum = signum


@contextlib.contextmanager
def signals_as_interrupt():
    """SIGTERM and SIGHUP raise ``Terminated`` (a KeyboardInterrupt) in the main thread for the duration."""
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
    """Run the developer command ``args.command``; returns its exit code."""
    name = args.command
    module = load(name)
    if module is None:
        print(f"`mobster {name}` is not in this build", file=sys.stderr, flush=True)
        return MISSING_EXIT
    if name in ("chat", "memory", "test", "wifi", "xray"):
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
    """The exit code for a usage error found outside a command's own parser (`mobster --demo verify`)."""
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
    """" Did you mean --execute?" for argparse's "unrecognized arguments: --exec", else ""."""
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
    """verify's and sim's progress on stderr, one line each."""

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
