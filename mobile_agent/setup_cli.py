"""`mobster setup`: get your iPhone ready for Mobster, in the terminal, one step at a time.

The same guided setup as Mobster for Mac's (``setup_service.SetupService`` over ``device_manager.DeviceManager``, used
here as they are): plug in and trust this Mac, Developer Mode, Mobster's helper on the iPhone, and letting tasks
tap and type. The checklist redraws itself every 2 seconds, each step ticking off as it happens on the phone, and
the command ends (exit 0) when your iPhone is ready. Mobster's helper keeps running afterwards, so `mobster` and
`mobster run` can use the phone straight away.

It asks before the two things that change something: installing Mobster's helper (Xcode signs it with your Apple
Account) and letting tasks tap and type.
"""

import json
import sys
import time

STEPS = ("tools", "api_key", "phone", "wda_build", "wda_running", "live")
POLL = 2.0


def render(state, paint, frame=0, width=100):
    """The checklist for ``state`` (SetupService.state()), as lines: a header with the count, then one row per step
    with its glyph, title and what to do now."""
    from .style import wrap
    spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    steps = [step for step in state.get("steps") or () if step.get("id") in STEPS]
    done = sum(1 for step in steps if step.get("state") == "done")
    lines = [paint("Set up your iPhone", "bold") + paint(f"{done} of {len(steps)} done".rjust(
        max(10, min(width, 72) - 18)), "tertiary"), ""]
    for number, step in enumerate(steps, 1):
        status = step.get("state")
        glyph = {"done": paint("✓", "green"), "working": paint(spinner[frame % len(spinner)], "accent"),
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
        runtime = _runtime()
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
            _choose(runtime, args.device)
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
            if _ready(state):
                if not as_json:
                    name = (state.get("device") or {}).get("name") or "Your iPhone"
                    print("\n" + paint("✓ ", "green", "bold") + paint(f"{name} is ready.", "bold")
                          + " Give it a first task: " + paint("mobster", "accent"))
                return 0
            failed = _failed(state)
            if failed and failed not in asked:
                asked.add(failed)
                if not as_json:
                    print("\n" + paint("✗ ", "coral", "bold") + (failed or "A step didn't finish.") +
                          "\n  Fix it, then run " + paint("mobster setup", "accent") + " again.")
                return 1
            action = _next_action(state, args, asked)
            if action is not None and not as_json:
                shown = 0
                if not _act(runtime, state, action, args, paint):
                    return 1
            elif action is not None and as_json:
                print(json.dumps({"waiting": action, "hint": "run mobster setup on a terminal to answer"}),
                      flush=True)
                return 1
            time.sleep(POLL / 8)
            for _ in range(7):
                time.sleep(POLL / 8)
                if not as_json and shown:
                    frame += 1
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


def _next_action(state, args, asked):
    steps = {step["id"]: step for step in state.get("steps") or ()}
    build, running, live = steps.get("wda_build"), steps.get("wda_running"), steps.get("live")
    if build and build["state"] == "todo" and "build" not in asked:
        return "build"
    if running and running["state"] == "todo" and build and build["state"] == "done" and "start" not in asked:
        return "start"
    if live and live["state"] == "todo" and "live" not in asked:
        return "live"
    return None


def _yes(question, paint):
    while True:
        try:
            answer = input(paint("? ", "accent", "bold") + question + " [Y/n] ").strip().lower()
        except EOFError:
            return False
        if answer in ("", "y", "yes"):
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
        took = "The first install can take 10 minutes or more." if first else "It's quicker than the first time."
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
    return True


def _choose(runtime, name):
    state = runtime.setup.state()
    wanted = name.casefold()
    for device in state.get("devices") or ():
        if wanted in {str(device.get("udid", "")).casefold(), str(device.get("name", "")).casefold()}:
            runtime.manager.choose(device["udid"])
            return
    raise LookupError(f"No iPhone called {name} is plugged in.")


def _runtime():
    """The runtime `mobster serve --manage-device` runs, with no task history and the helper kept running on exit."""
    from . import server
    from .config import app_env_file
    from .paths import user_data_dir
    from .tui.session import serve_config
    import os
    config = serve_config(manage_device=True, wda_url="http://127.0.0.1:8100", state_db=None,
                          enable_live=os.environ.get("MOBSTER_ENABLE_LIVE") == "1", data_dir=user_data_dir(),
                          env_file=app_env_file(), keep_runner=True)
    return server.Runtime(config)


def _width():
    from .style import columns
    return columns()
