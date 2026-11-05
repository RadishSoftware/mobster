"""`mobster setup`: get your iPhone ready for Mobster, in the terminal, one step at a time."""

import json
import sys
import time

STEPS = ("tools", "api_key", "phone", "wda_build", "wda_running", "live")
POLL = 2.0


def render(state, paint, frame=0, width=100):
    """The checklist for ``state`` (SetupService.state()), as lines."""
    from .style import SPINNER, wrap
    steps = [step for step in state.get("steps") or () if step.get("id") in STEPS]
    done = sum(1 for step in steps if step.get("state") == "done")
    lines = [paint("Set up your iPhone", "bold") + paint(f"{done} of {len(steps)} done".rjust(
        max(10, min(width, 72) - 18)), "tertiary"), ""]
    for number, step in enumerate(steps, 1):
        status = step.get("state")
        glyph = {"done": paint("✓", "green"), "working": paint(SPINNER[frame % len(SPINNER)], "accent"),
                 "blocked": paint("○", "tertiary")}.get(status, paint("○", "accent"))
        title = step.get("title") or step.get("id")
        lines.append(f"  {glyph} {number}. " + (paint(title, "tertiary") if status == "done" else
                                                  paint(title, "bold")))
        detail = step.get("detail") or ""
        if step.get("id") == "api_key" and status != "done":
            detail = "Run `mobster login` in another terminal to add your Claude or OpenAI key."
        if status == "blocked":
            detail = ""
        if detail and status != "done":
            lines.append(paint(wrap(detail, min(width, 100) - 2, indent=7), "secondary"))
        for command in step.get("commands") or () if status != "done" else ():
            text = command.get("command") if isinstance(command, dict) else str(command)
            if text:
                lines.append(" " * 7 + paint(text, "accent"))
    return lines


def run(args):
    """Returns 0 when the phone is ready, 1 when a step failed, 3 when setup can't run here, 130 on ctrl+c."""
    from .style import palette
    paint = palette(sys.stdout)
    as_json = bool(getattr(args, "json", False)) or not sys.stdout.isatty()
    try:
        runtime = open_runtime()
    except Exception as error:  # noqa: BLE001 -- the runtime's own sentence
        print(f"mobster setup: {error}", file=sys.stderr)
        return 3
    setup = runtime.setup
    asked = set()
    shown = 0
    frame = 0
    last = None
    try:
        if getattr(args, "device", None):
            choose(runtime, args.device)
        while True:
            state = setup.state()
            frame += 1
            if as_json:
                public = {"complete": _ready(state), "steps": [{k: s.get(k) for k in ("id", "title", "state",
                                                                                         "detail")}
                                                                for s in state["steps"] if s["id"] in STEPS]}
                if public != last:
                    print(json.dumps(public), flush=True)
                    last = public
            else:
                lines = render(state, paint, frame, _width())
                if shown:
                    sys.stdout.write(f"\033[{shown}F\033[J")
                sys.stdout.write("\n".join(lines) + "\n")
                sys.stdout.flush()
                shown = len(lines)
            if _ready(state) and (state.get("wifi") or {}).get("state") != "pairing":   # a Wi-Fi yes ends first
                if not as_json:
                    name = (state.get("device") or {}).get("name") or "Your iPhone"
                    print("\n" + paint("✓ ", "green", "bold") + paint(f"{name} is ready.", "bold")
                          + " Give it a first task: " + paint("mobster", "accent"))
                return 0
            failed = _failed(state)
            if failed:
                if not as_json:
                    print("\n" + paint("✗ ", "coral", "bold") + failed +
                          "\n  Fix it, then run " + paint("mobster setup", "accent") + " again.")
                return 1
            action = _next_action(state, asked)
            if action is not None and not as_json:
                shown = 0
                if not _act(runtime, state, action, args, paint):
                    return 1
            elif action is not None and as_json:
                print(json.dumps({"waiting": action, "hint": _hint(state, action)}), flush=True)
                return 1
            time.sleep(POLL)
    except KeyboardInterrupt:
        print()
        return 130
    finally:
        runtime.close(timeout=5)


def _ready(state):
    steps = {step["id"]: step["state"] for step in state.get("steps") or ()}
    return all(steps.get(name) == "done" for name in ("phone", "wda_build", "wda_running", "live"))


def _failed(state):
    build = state.get("build") or {}
    if build.get("state") == "failed" and build.get("error"):
        return build["error"]
    return None


def _next_action(state, asked):
    steps = {step["id"]: step for step in state.get("steps") or ()}
    build, running, live = steps.get("wda_build"), steps.get("wda_running"), steps.get("live")
    if build and build["state"] == "todo" and "build" not in asked:
        return "build"
    if running and running["state"] == "todo" and build and build["state"] == "done" and "start" not in asked:
        return "start"
    if live and live["state"] == "todo" and "live" not in asked:
        return "live"
    if (state.get("wifi") or {}).get("state") == "asking" and "wifi" not in asked:
        return "wifi"
    return None


def _hint(state, action):
    """What a run with no terminal to ask on says instead of the question: it never takes a yes for granted."""
    if action != "wifi":
        return "run mobster setup on a terminal to answer"
    name = json.dumps((state.get("device") or {}).get("name") or "your iPhone", ensure_ascii=False)
    return (f"Wi-Fi stays off for this iPhone until you say yes: run mobster setup on a terminal to answer, or run "
            f"mobster wifi on --device {name} with it plugged in")


def _yes(question, paint, default=True):
    while True:
        try:
            answer = input(paint("? ", "accent", "bold") + question + (" [Y/n] " if default else " [y/N] ")
                           ).strip().lower()
        except EOFError:
            return False
        if answer == "":
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def _act(runtime, state, action, args, paint):
    setup = runtime.setup
    name = (state.get("device") or {}).get("name") or "your iPhone"
    if action == "build":
        teams = state.get("teams") or []
        team = getattr(args, "team", None) or (teams[0].get("id") if len(teams) == 1 else None)
        if team is None and len(teams) > 1:
            print("Which Apple Account signs Mobster's helper?")
            for index, item in enumerate(teams, 1):
                print(f"  {paint(str(index), 'accent')}  {item.get('label') or item.get('id')}")
            choice = input("Choose a number: ").strip()
            if not choice.isdigit() or not 1 <= int(choice) <= len(teams):
                return False
            team = teams[int(choice) - 1].get("id")
        if team is None:
            print("Sign in to Xcode first: open Xcode, then Xcode › Settings › Accounts, choose +, and sign in "
                  "with your Apple Account. Then run " + paint("mobster setup", "accent") + " again.")
            return False
        first = not (state.get("build") or {}).get("expires_at")
        took = "The first install takes about 2 minutes." if first else "It's quicker than the first time."
        if not _yes(f"Install Mobster's helper on {name}? Xcode signs it with your Apple Account. {took}", paint):
            return False
        setup.manager.start_build(team)
        return True
    if action == "start":
        setup.manager.start_runner()
        return True
    if action == "live":
        if not _yes("Let Mobster tap and type on your iPhone while a task runs? It asks before it sends, buys, "
                    "posts or deletes.", paint):
            return False
        setup.set_live(True)
        return True
    if action == "wifi":
        yes = _yes(f"Use {name} over Wi-Fi when it's unplugged? Mobster turns on its Connect via network switch "
                   "over this cable, the same switch as in Xcode.", paint, default=False)
        try:
            setup.answer_wifi(state.get("device"), yes)
        except LookupError:
            pass   # the question ended meanwhile: unplugged, or answered in the Mac app
        if not yes:
            print(f"Wi-Fi stays off for {name}. To turn it on later, plug it in and run "
                  + paint("mobster wifi on", "accent") + ".")
        return True
    return True


def choose(runtime, name):
    state = runtime.setup.state()
    wanted = name.casefold()
    for device in state.get("devices") or ():
        if wanted in {str(device.get("udid", "")).casefold(), str(device.get("name", "")).casefold()}:
            runtime.manager.choose(device["udid"])
            return
    raise LookupError(f"No iPhone called {name} is plugged in.")


def open_runtime():
    """The runtime `mobster serve --manage-device` runs, with no task history and the helper kept running on exit."""
    import os

    from . import server
    from .cli_constants import DEFAULT_WDA_URL
    from .config import app_env_file
    from .paths import user_data_dir
    from .tui.session import serve_config
    config = serve_config(manage_device=True, wda_url=DEFAULT_WDA_URL, state_db=None,
                          enable_live=os.environ.get("MOBSTER_ENABLE_LIVE") == "1", data_dir=user_data_dir(),
                          env_file=app_env_file(), keep_runner=True)
    return server.Runtime(config)


def _width():
    from .style import columns
    return columns()
