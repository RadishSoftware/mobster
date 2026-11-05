"""The `mobster` command."""

import os
import sys

from . import __version__, links
from .cli_constants import DEFAULT_WDA_URL, EXIT_COULDNT, EXIT_FAILED, EXIT_OK, EXIT_USAGE
from .cli_run import (
    DeviceBusy,
    MissingKey,
    NoDevice,
    device_target,
    error_line,
    error_result,
    no_device_reason,
    output,
    wda_url_for,
)
from .paths import DATA_DIR_ENV, build_dir, source_checkout, user_data_dir

DOCS = links.DOCS


# Commands that read the keys Mobster for Mac saved (config.load_env_layers). Not `mcp`: an installed MCP entry
# must never switch on paid Smart tools the user didn't give it. Not `serve`: the Mac app passes its own file.
APP_KEY_COMMANDS = {"tui", "run", "chat", "doctor", "verify", "test", "setup", "login", "logout"}


def default_state_db(args):
    """Where `serve` keeps its journal when --state-db is not given: never site-packages or the signed app bundle."""
    from pathlib import Path
    data_dir = getattr(args, "data_dir", None) or os.environ.get(DATA_DIR_ENV)
    if data_dir or not source_checkout():
        return (Path(data_dir).expanduser().resolve() if data_dir else user_data_dir()) / "state" / "mobster.sqlite3"
    return Path(__file__).parent / ".state" / "mobster.sqlite3"

def check_placement(parser, args, on_error=None):
    """Options given before a command's name, as in `mobster --wda-url URL run …`."""
    from . import devtools
    from .cli_parser import wda_url_type

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
            and command not in devtools.ORDER:
        error("argument --device: not allowed with argument --wda-url")
    listing = command == "devices" and getattr(args, "action", None) is None  # the list reads no WDA address
    if "--wda-url" in accepted and command != "serve" and not listing and not getattr(args, "wda_url", None) \
            and os.environ.get("MOBSTER_WDA_URL"):
        import argparse
        try:
            wda_url_type(os.environ["MOBSTER_WDA_URL"])
        except argparse.ArgumentTypeError as problem:
            error(f"$MOBSTER_WDA_URL: {problem}")


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
    from .style import columns, palette
    found = devices.discover()
    if as_json:
        output({"devices": found})
        return EXIT_OK
    paint = palette(sys.stdout)
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
    width = columns()
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


def print_device_status(args):
    """`mobster devices status`: a device's battery and heat, and this Mac's power (device_status.py)."""
    from . import device_status, devices
    found = devices.discover(probe=False)
    if args.device:
        try:
            item = devices.resolve(found, args.device)
        except LookupError as error:
            raise NoDevice(str(error)) from None
    elif args.wda_url:
        url = wda_url_for(args)
        item = next((record for record in found if (record.get("wdaUrl") or "").rstrip("/") == url),
                    {"id": url, "kind": "wda", "name": url, "wdaUrl": url})
    else:
        item = (found[0] if len(found) == 1 else next((record for record in found if record.get("primary")), None))
        if item is None:
            raise NoDevice("No device to read. " + ("Name one with --device." if found else
                                                    "Plug in your iPhone with a cable and unlock it."))
    phone = device_status.read_phone(item["kind"], udid=item.get("udid"), wda_url=item.get("wdaUrl"))
    mac = device_status.read_mac()
    if args.json or not sys.stdout.isatty():
        output({"ok": True, "device": item["id"], "name": item.get("name"), "phone": phone, "mac": mac})
        return EXIT_OK
    from .style import palette
    paint = palette(sys.stdout)
    print(paint(item.get("name") or item["id"], "bold"))
    found_rows = device_status.rows(phone, mac)
    width = max(len(label) for label, _ in found_rows) + 2
    for label, value in found_rows:
        print("  " + paint(label.ljust(width), "tertiary") + value)
    return EXIT_OK


def lock_probe(args):
    """`mobster devices lock-probe`: reads the lock screen under the device lease, with no tap, key press or typing."""
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
        result = lockscreen.probe(driver, udid=getattr(args, "device_udid", None)
                                  if getattr(args, "device_kind", None) == "usb" else None)
    finally:
        close_all(driver)
        lease.close()
    if args.json or not sys.stdout.isatty():
        output({"ok": True, **result})
    else:
        print("\n".join(lockscreen.describe(result)))
    return EXIT_OK


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
    """The command ``argv`` names, read before parsing."""
    names = parser.commands if hasattr(parser, "commands") else parser
    index = _positional(list(argv), names)
    if index is None:
        return None
    if index and argv[index - 1] == "--":
        return None  # `mobster -- verify`: after --, the word is a task, not a command
    word = argv[index]
    return word if word in names else None


def split_task(argv):
    """`mobster "TASK"`: (task, argv without it) when the first word is a task for the terminal UI,
    else (None, argv)."""
    from .help_topics import COMMANDS
    index = _positional(argv, COMMANDS)
    if index is None or argv[index] in COMMANDS:
        return None, argv
    word = argv[index]
    if index and argv[index - 1] == "--":
        # `mobster -- calendar`: after --, a word is always the task, even one word.
        return word.strip(), argv[:index - 1] + argv[index + 1:]
    from .harness_api import hooks
    if hooks().cli:
        # A private build may add commands: its parser knows them.
        from .cli_parser import build_parser
        if word in build_parser(load=()).commands:
            return None, argv
    later = [token for token in argv[index + 1:] if not token.startswith("-")]
    from .style import palette
    paint = palette(sys.stderr)
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
    close = difflib.get_close_matches(word, [name for name in COMMANDS if name != "build-ocr"], n=1, cutoff=.6)
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
        from .cli_parser import usage_lines
        from .style import palette
        sys.stderr.write(usage_lines(palette(sys.stderr), self.message, self.tip, prog))
        sys.stderr.flush()
        return EXIT_USAGE


def route_usage_errors(parser, argv):
    """A usage error the bare parser finds before `verify` exits 3,
    with verify's couldnt_run JSON when output is JSON."""
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
        from .style import columns, palette
        print(help_topics.top_level(palette(sys.stdout), columns()))
        return EXIT_OK
    if len(argv) == 2 and argv[0] == "help":
        from . import help_topics
        if argv[1] in help_topics.TOPICS:
            from .cli_parser import print_topic
            return print_topic(argv[1])
    return None


# Commands that run Xcode's tools (xcodebuild, xcrun simctl or devicectl, swiftc): they use the Xcode Mobster found,
# whatever xcode-select says (mac_tools.adopt_xcode), so nobody is asked to switch it in Terminal.
XCODE_COMMANDS = frozenset({"build-ocr", "ci", "devices", "doctor", "expo", "mcp", "phone", "serve", "setup", "sim",
                            "test", "testflight", "verify", "wifi", "xcode", "fixtures", "links", "intents"})


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["import"]:
        argv = ["test"] + argv   # `mobster import maestro .maestro` is `mobster test import maestro .maestro`
    answered = quick(argv)
    if answered is not None:
        return answered
    try:
        task, argv = split_task(argv)
    except UsageError as error:
        return error.report()
    from .cli_parser import build_parser, owner, print_help
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
    if getattr(args, "data_dir", None):
        from .paths import use_data_dir
        use_data_dir(args.data_dir)
    words = [word for word in extras if not word.startswith("-")]
    if extras and words == extras and args.command == "run" and getattr(args, "goal", None):
        # `mobster run Turn on Dark Mode`: the task is the words, unquoted.
        task = " ".join([args.goal] + words)
        from .style import palette
        paint = palette(sys.stderr)
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
        if exit_.code == 2 and args.command in devtools.ORDER:
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
        args.env_file_from_environment = True
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
    if command in XCODE_COMMANDS and sys.platform == "darwin":
        from . import mac_tools
        mac_tools.adopt_xcode()
    # Before any model or service connection: the frozen app's Python has no usable CA file.
    from . import tls
    tls.ensure_ca_bundle()
    from . import update_check
    update_check.start(command, args)
    code = dispatch(parser, args, command)
    update_check.after_command(command, args)
    return code


def dispatch(parser, args, command):
    from . import devtools
    refused = plan_refusal(args, command)
    if refused is not None:
        return refused
    if command == "devices" and getattr(args, "action", None) is None and (
            getattr(args, "device", None) or getattr(args, "wda_url", None)):
        parser.commands["devices"].error("--device and --wda-url go with `mobster devices lock-probe` or `status`")
    probing = command == "devices" and getattr(args, "action", None) == "lock-probe"
    if (command in ("run", "screen", "doctor", "tui") or probing) and getattr(args, "device", None) \
            and not getattr(args, "demo", False) is True:
        # --device NAME is that device's WDA address from here on (exclusive with --wda-url).
        try:
            item = device_target(args.device)
        except NoDevice as error:
            if command == "run" and (getattr(args, "json", False) or not sys.stdout.isatty()):
                output(error_result(error, EXIT_COULDNT))
            else:
                print(error_line(error), file=sys.stderr)
            return EXIT_COULDNT
        args.wda_url = item["wdaUrl"]
        args.device_kind, args.device_udid = item["kind"], item.get("udid")
        args.device_name = item.get("name")
    if command == "chat" and not getattr(args, "unloaded_command", False):
        from .chat_here import maybe_here
        code = maybe_here(args)
        if code is not None:
            return code
    if command in devtools.ORDER:
        try:
            return devtools.run(args)
        except KeyboardInterrupt:
            return 130
    from .harness_api import hooks
    try:
        for extension in hooks().cli:
            code = extension.run(args, output)
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
            if args.action == "status":
                return print_device_status(args)
            return print_devices(as_json=args.json)
        if command == "doctor":
            return doctor_command(args)
        if command == "history":
            if getattr(args, "to_eval", None):
                from .run_export import to_eval
                return to_eval(args)
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
            subprocess.run(["xcrun", "swiftc", "-O", str(root / "swift" / "ocr.swift"),
                            "-o", str(build / "ocr")], check=True)
            output({"ok": True, "binary": str(build / "ocr")})
        elif command == "demo":
            from types import SimpleNamespace

            from .smart_run import run_smart
            return run_smart(SimpleNamespace(goal="Search for coffee", json=args.json, execute=True, spend_cap_usd=None,
                                             long=False, budget=None, yes=True), demo=True)
        elif command == "serve":
            from .serve import serve
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
        code = EXIT_COULDNT if isinstance(exc, (NoDevice, MissingKey)) else EXIT_FAILED
        if sys.stdout.isatty() and not getattr(args, "json", False):
            print(error_line(exc), file=sys.stderr)
        elif command in ("run", "demo"):
            output(error_result(exc, code))
        else:
            output({"ok": False, "error_type": type(exc).__name__, "error": str(exc)})
        return code


def doctor_command(args):
    """`mobster doctor`: the checks and the fix for each. With --fix, it does the fix each finding's row carries
    (doctor_fix), then checks again."""
    from . import doctor, doctor_fix
    if getattr(args, "apps", False):
        from .attach import cli as attach_cli
        return attach_cli.run(args, wda_url_for(args))
    usb = not args.simulator and getattr(args, "device_kind", "usb") == "usb"

    def check():
        return doctor.run_checks(wda_url=wda_url_for(args), device=usb, env_file=args.env_file,
                                 udid=getattr(args, "device_udid", None))
    checks = check()
    steps = None
    if args.fix:
        terminal = sys.stdin.isatty() and not args.json
        ctx = doctor_fix.Context(args, ask=doctor_fix.interactive_ask if terminal else None,
                                 say=(lambda line: print(line, flush=True)) if terminal else None)
        try:
            steps = doctor_fix.run(checks, ctx, terminal=terminal)
        finally:
            ctx.close()
        checks = check()
    # Someone who never pointed Mobster at an iPhone may be here for `verify`: say where its checks are.
    phone_chosen = getattr(args, "wda_url", None) or getattr(args, "device", None) \
        or os.environ.get("MOBSTER_WDA_URL")
    hint = doctor.VERIFY_HINT if doctor.problems(checks) and not phone_chosen else None
    if args.json:
        output({"ok": not doctor.problems(checks), "checks": [check.public() for check in checks],
                **({"fixes": [step.public() for step in steps]} if steps is not None else {}),
                **({"hint": hint} if hint else {})})
    else:
        from .style import color_enabled, columns
        print(doctor.plain_report(checks, color=color_enabled(sys.stdout), verify_hint=hint, width=columns(),
                                  steps=steps))
    return EXIT_FAILED if doctor.problems(checks) else EXIT_OK


def plan_refusal(args, command):
    """EXIT_COULDNT after saying so, when the agent inside Mobster.app has no active plan, fails its seal check or
    runs under a CI token that refuses; None otherwise."""
    from . import plan_gate
    gate = plan_gate.bundled_gate()
    if gate is not None:
        plan_gate.grant_ci_token(command, args)  # a CI token licenses simulator runs on a machine with no account
    message = plan_gate.stopped(command, args)  # a failed seal or a CI token that refuses: whatever it needs
    if message is None:
        message = gate.refusal() if gate is not None else None
        if message is None or not plan_gate.cli_needs_plan(command, args):  # the plan first: it is the cheaper read
            return None
    if getattr(args, "json", False):
        output({"ok": False, "error_type": "plan_required", "error": message})
    else:
        print(f"mobster: {message}", file=sys.stderr, flush=True)
    return EXIT_COULDNT


def run_command(args):
    """`mobster run`: Mobster's agent through the same runtime as the terminal UI, or the scripted demo."""
    from .smart_run import run_smart
    return run_smart(args, demo=getattr(args, "demo", False) is True)


if __name__ == "__main__":
    sys.exit(main())
