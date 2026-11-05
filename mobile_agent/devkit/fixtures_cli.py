"""`mobster fixtures`: the app states a project's checks start from (verify/fixtures.py).

    mobster fixtures list [--json]                         each fixture in .mobster/fixtures/ and what it does
    mobster fixtures apply NAME... --bundle ID [--sim TYPE]   put a simulator's app in that state now

``apply`` is what a check does before it launches the app, without the check: the reset, the keychain, the seed files
and the pasteboard, on a simulator. It doesn't launch the app, and the fixture's launch args, env and steps wait for a
check. On an iPhone it refuses anything that changes the phone's state.

Exit codes: 0 done, 3 couldn't (usage too).
"""

import json
import sys
from pathlib import Path

from . import DevError, main, say, subcommands


def add_arguments(parser, helpers):
    add = subcommands(parser, "fixtures_command")
    listed = add("list", help="list the project's fixtures and what each one sets up",
                 description="List the fixtures in .mobster/fixtures/, flattened with what they extend: the reset, the "
                             "launch args and env, the seed files, the keychain, the pasteboard and the steps. Reads "
                             "files only.")
    listed.add_argument("--json", action="store_true", help="print them as JSON")
    apply = add("apply", help="put a simulator's app in a fixture's state now",
                description="Reset the app on a simulator and seed it as the fixtures say: the keychain, the files "
                            "in its data container and the pasteboard. Several names compose in order. The app is "
                            "not launched.",
                epilog="exit codes: 0 applied, 3 couldn't\n\nexamples:\n"
                       "  mobster fixtures apply logged-in-cart --bundle com.example.shop\n"
                       "  mobster fixtures apply clean empty-cart --bundle com.example.shop --sim \"iPhone 17 Pro\"")
    apply.add_argument("names", nargs="+", metavar="NAME", help="fixtures to apply, in order")
    apply.add_argument("--bundle", metavar="ID", required=True, help="the app's bundle ID")
    apply.add_argument("--app", metavar="PATH", help="a built .app to install first (needed for a reinstall)")
    apply.add_argument("--sim", metavar="TYPE", help="a simulator, such as \"iPhone 17 Pro\" (default: Mobster's own)")
    apply.add_argument("--device", metavar="NAME", help="an iPhone: refused when the fixtures change its state")
    for sub in (listed, apply):
        sub.add_argument("--project", metavar="DIR", help="the project folder (default: the one you are in)")


def run(args, *, cwd=None):
    return main("fixtures", {"list": _list, "apply": _apply}, args, cwd=Path(cwd or Path.cwd()))


def _root(args, cwd):
    from ..verify.checks import project_root
    return project_root(Path(args.project).expanduser() if args.project else cwd)


def listing(root):
    """[{name, ...Effective.public()}] for every fixture. FixtureError for a broken one, naming it."""
    from ..verify import fixtures
    out = []
    for name in fixtures.names(root):
        out.append({"name": name, **fixtures.resolve(root, [name]).public()})
    return out


def _list(args, cwd):
    from ..verify import fixtures
    try:
        found = listing(_root(args, cwd))
    except fixtures.FixtureError as error:
        raise DevError(str(error)) from None
    if args.json:
        print(json.dumps({"fixtures": found}, ensure_ascii=False), flush=True)
        return 0
    if not found:
        say("No fixtures. Add .mobster/fixtures/NAME.yaml, such as:\n\n  reset: data\n  launch:\n    args: [-UITests]\n"
            "  steps:\n    - type ${TEST_EMAIL} into Email")
        return 0
    for item in found:
        parts = [f"reset {item['reset']}"] if item["reset"] != "none" else []
        parts += [f"{len(item['files'])} file{'' if len(item['files']) == 1 else 's'}"] if item["files"] else []
        parts += ["keychain reset"] if item["keychain"] else []
        parts += ["pasteboard"] if item["pasteboard"] else []
        parts += [f"{len(item['args'])} arg{'' if len(item['args']) == 1 else 's'}"] if item["args"] else []
        parts += [f"{len(item['env'])} env"] if item["env"] else []
        parts += [f"{len(item['steps'])} step{'' if len(item['steps']) == 1 else 's'}"] if item["steps"] else []
        parts += ([f"{len(item['dialogs'])} dialog answer{'' if len(item['dialogs']) == 1 else 's'}"]
                  if item["dialogs"] else [])
        built = [n for n in item["names"] if n != item["name"]]
        say(f"{item['name']}: " + (", ".join(parts) or "nothing")
            + (f"  (builds on {', '.join(built)})" if built else "")
            + ("  [simulators only]" if item["destructive"] else ""))
    return 0


def _apply(args, cwd):
    from ..verify import fixtures
    root = _root(args, cwd)
    if args.device:
        try:
            effective = fixtures.resolve(root, args.names)
        except fixtures.FixtureError as error:
            raise DevError(str(error), "`mobster fixtures list` shows the project's fixtures.") from None
        if effective.changes_state:
            raise DevError(fixtures.PHONE_REFUSAL.format(what=effective.changes_state[0],
                                                         name=", ".join(effective.names)),
                           fixtures.PHONE_FIX)
        say(f"{', '.join(effective.names)} only sets launch args, env and steps, which a check applies when it runs. "
            "Nothing to put on the phone now.")
        return 0
    app_path = str(Path(args.app).expanduser().resolve()) if args.app else None
    say(apply(root, args.names, args.bundle, sim=args.sim, app_path=app_path,
              progress=lambda line: say(line, sys.stderr)))
    return 0


def apply(root, names, bundle, *, sim=None, app_path=None, progress=None, manager=None, wait=600):
    """Put ``bundle`` on a simulator in the fixtures' state now (never an iPhone), and say what changed. ``wait`` is
    how long to wait for a simulator no check holds. DevError."""
    from ..sim.api import SimError, SimulatorManager
    from ..verify import conditions, fixtures
    try:
        effective = fixtures.resolve(root, names)
    except fixtures.FixtureError as error:
        raise DevError(str(error), "`mobster fixtures list` shows the project's fixtures.") from None
    manager = manager or SimulatorManager(progress=progress)
    try:
        with manager.acquire(sim, timeout=wait) as target:
            if effective.reset != "none":
                manager.reset(target, bundle, effective.reset, app_path=app_path)
            did = [f"reset {effective.reset}"] if effective.reset != "none" else []
            did += fixtures.seed_simulator(fixtures.Seed(effective.files, effective.keychain, effective.pasteboard),
                                           udid=target.udid, bundle=bundle, run=conditions.simctl)
            device = target.name
    except (SimError, fixtures.FixtureError) as error:
        raise DevError(str(error), getattr(error, "fix", "") or "") from None
    return (f"Applied {', '.join(effective.names)} to {bundle} on {device}: " + (", ".join(did) or "nothing to change")
            + ".")
