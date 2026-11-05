"""`mobster sim` (and `python -m mobile_agent.sim`): Mobster's own headless simulators."""

import argparse
import json
import sys
import time

from .models import SimError

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


def add_arguments(parser, helpers):
    from .biometrics import ACTIONS, BIOMETRICS
    formatter = getattr(parser, "formatter_class", argparse.HelpFormatter)
    subs = parser.add_subparsers(dest="sim_command", metavar="<command>", required=True,
                                 help="what to do with Mobster's simulators")
    prog = parser.prog

    def command(name, help, description):
        return subs.add_parser(name, prog=f"{prog} {name}", help=help, description=description,
                               formatter_class=formatter)

    listing = command("list", "list Mobster's simulators",
                      "List the simulators Mobster created, with their state and WebDriverAgent address.")
    listing.add_argument("--json", action="store_true", help="print JSON instead of a table")

    release = command("release", "ask the agents you started to let go of their simulators",
                      "Ask each `mobster mcp` server started by the same app as this shell, such as the agent that "
                      "runs this command, to release the simulator it holds. It lets go at its next sweep, within "
                      "15 seconds. A simulator another app's agent holds is left alone.")
    release.add_argument("--all-mine", action="store_true", required=True,
                         help="every simulator held by an agent started from the same app as this shell")

    prepare = command("prepare", "create, boot and start WebDriverAgent, timing each phase",
                      "Create the simulator if needed, boot it headless, build WebDriverAgent the first time, start "
                      "it on 127.0.0.1, and print how long each phase took. It stays up afterwards.")
    prepare.add_argument("--device", metavar="NAME", help="the simulator device type (default: iPhone 17 Pro, else "
                                                          "the newest iPhone the runtime supports)")
    prepare.add_argument("--runtime", metavar="NAME", help="the iOS runtime, such as \"iOS 26.4\" (default: the "
                                                           "newest installed)")
    prepare.add_argument("--json", action="store_true", help="print the simulator and the phase times as JSON")

    biometric = command("biometric", "test biometric enrollment and authentication on a simulator",
                        "Read or change biometric enrollment, or send a Face ID or Touch ID test event to an "
                        "idle, booted Mobster simulator. Real devices are never supported. A sent event does "
                        "not confirm that the app authenticated.")
    biometric.add_argument("udid", metavar="UDID", help="the booted Mobster simulator")
    biometric.add_argument("action", choices=ACTIONS,
                           help="read enrollment, enable or disable it, or send a match or nonmatch event")
    biometric.add_argument("--type", choices=tuple(BIOMETRICS), default="face-id", dest="biometric_type",
                           help="the biometric event type (default: face-id; enrollment is shared)")
    biometric.add_argument("--json", action="store_true", help="print enrollment and event delivery as JSON")

    shutdown = command("shutdown", "stop WebDriverAgent and shut down Mobster's simulators",
                       "Stop WebDriverAgent and shut down every Mobster simulator that isn't running a check, or "
                       "the one named.")
    shutdown.add_argument("udid", nargs="?", metavar="UDID", help="only this simulator")

    erase = command("erase", "erase one Mobster simulator",
                    "Shut down and erase one Mobster simulator: its apps and data go, and the next run boots it "
                    "fresh.")
    erase.add_argument("udid", metavar="UDID", help="the simulator to erase")

    delete = command("delete", "delete Mobster's simulators",
                     "Shut down and delete Mobster's simulators. Only UDIDs in Mobster's registry whose names start "
                     "with “Mobster · ” are deleted; any other UDID is refused.")
    which = delete.add_mutually_exclusive_group(required=True)
    which.add_argument("udid", nargs="?", metavar="UDID", help="the simulator to delete")
    which.add_argument("--all", action="store_true", help="delete every Mobster simulator")

    prune = command("prune", "remove unused simulators and stale WebDriverAgent builds",
                    "Delete Mobster simulators unused for 14 days, WebDriverAgent builds for Xcode versions no "
                    "longer installed, and registry entries for simulators that are gone.")
    prune.add_argument("--json", action="store_true", help="print what was removed as JSON")

    from .. import android
    if android.enabled():
        droid = command("android", "list, boot or stop Android emulators and devices",
                        "List Android devices (emulators and USB phones), boot an emulator and hold it for a run, or "
                        "shut down the emulators Mobster booted. Emulators you started yourself are never stopped.")
        droid.add_argument("action", choices=("list", "boot", "shutdown"), help="what to do")
        droid.add_argument("avd", nargs="?", metavar="AVD", help="the virtual device to boot or stop")
        droid.add_argument("--json", action="store_true", help="print JSON instead of a table")

    doctor = command("doctor", "check what `mobster verify` needs",
                     "Check Xcode, the iOS runtime, the device type, git, disk space, WebDriverAgent, Mobster's "
                     "simulators, ports, and your OpenAI or Anthropic key for Smart, and print the fix for each "
                     "problem. It never runs sudo and never downloads a runtime.")
    doctor.add_argument("--fix", action="store_true", help="prepare the simulator: create, boot and build "
                                                           "WebDriverAgent once")
    doctor.add_argument("--json", action="store_true",
                        help="print {\"ok\", \"checks\": [{\"key\", \"label\", \"state\", \"detail\", \"fix\"}]}")


def run(args) -> int:
    return _run(args, _progress_lines())


def _progress_lines():
    try:
        from ..devtools import ProgressLines
    except ImportError:
        return _progress
    return ProgressLines(sys.stderr)


def _run(args, progress) -> int:
    from .api import SimulatorManager
    as_json = bool(getattr(args, "json", False))
    manager = SimulatorManager(progress=progress)
    end = getattr(progress, "end", lambda: None)  # a "Still …" line on a terminal
    name = getattr(args, "sim_command", None)
    try:
        if name == "list":
            return _list(manager, as_json)
        if name == "release":
            asked = manager.release_mine()
            print(f"Asked {len(asked)} simulator{'s' if len(asked) != 1 else ''} to let go. They are free within "
                  "15 seconds." if asked else "None of your agents holds a simulator.")
            return EXIT_OK
        if name == "prepare":
            return _prepare(manager, args, as_json, end)
        if name == "biometric":
            result = manager.biometric(args.udid, args.action, biometric_type=args.biometric_type)
            if as_json:
                _print_json(result)
            elif result["event_sent"]:
                print(f"Sent {args.biometric_type} {args.action} event. Check the app for the result.")
            else:
                print("Biometric enrollment: " + ("enabled" if result["enrolled"] else "disabled"))
            return EXIT_OK
        if name == "android":
            return _android(manager, args, as_json)
        if name == "shutdown":
            manager.shutdown(args.udid)
            if not manager.registry.read():
                print("No Mobster simulators.")
            return EXIT_OK
        if name == "erase":
            manager.erase(args.udid)
            return EXIT_OK
        if name == "delete":
            deleted = manager.delete(None if args.all else args.udid)
            if not deleted:
                print("No Mobster simulators to delete.")
            return EXIT_OK
        if name == "prune":
            removed = manager.prune()
            if as_json:
                _print_json(removed)
            else:
                lines = [f"Deleted {udid}" for udid in removed["simulators"]]
                lines += [f"Forgot {udid}: it no longer exists" for udid in removed["entries"]]
                lines += [f"Removed {path}" for path in removed["wda_builds"]]
                print("\n".join(lines) or "Nothing to prune.")
            return EXIT_OK
        if name == "doctor":
            from .doctor import problems, report
            checks = manager.doctor(fix=bool(args.fix))
            end()
            if as_json:
                _print_json({"ok": not problems(checks), "checks": checks})
            else:
                print(report(checks, color=_color()))
            return EXIT_FAILED if problems(checks) else EXIT_OK
        print(f"mobster sim: unknown command {name!r}", file=sys.stderr)
        return EXIT_USAGE
    except SimError as exc:
        end()
        if as_json:
            _print_json({"ok": False, "error": {"kind": exc.kind, "message": str(exc), "fix": exc.fix}})
        print(f"mobster sim: {exc}" + (f"\n  {exc.fix}" if exc.fix else ""), file=sys.stderr)
        return EXIT_FAILED
    except KeyboardInterrupt:
        end()
        return 130
    except Exception as exc:  # never raise into mobster's main
        end()
        print(f"mobster sim: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED


def _progress(message):
    print(message, file=sys.stderr, flush=True)


def _print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _color():
    try:
        from ..style import color_enabled
        return color_enabled(sys.stdout)
    except Exception:
        return False


def _list(manager, as_json):
    rows = manager.list()
    if as_json:
        _print_json({"simulators": rows})
        return EXIT_OK
    if not rows:
        print("No Mobster simulators yet. `mobster sim prepare` creates one.")
        return EXIT_OK
    table = [("NAME", "STATE", "WEBDRIVERAGENT", "HELD BY", "UDID")]
    for row in rows:
        wda = row["wda_url"].split("//")[-1] if row["wda"] == "ready" else "stopped"
        holder = row["holder"]
        held = (f"{holder['client']} ({holder['session']}), {_since(holder['since'])}" if holder
                else "a Mobster process" if row["in_use"] else "-")
        table.append((row["name"], row["state"], wda, held, row["udid"]))
    widths = [max(len(line[column]) for line in table) for column in range(len(table[0]) - 1)]
    for line in table:
        print("  ".join(cell.ljust(width) for cell, width in zip(line, widths)) + "  " + line[-1])
    return EXIT_OK


def _android(manager, args, as_json):
    from ..android.emulator import EmulatorManager
    droid = EmulatorManager(manager.data_dir)
    if args.action == "boot":
        lease = droid.acquire(args.avd)
        target = lease.target
        lease.release()  # the emulator stays up for the next run
        print(f"Ready  {target.name} ({target.serial})")
    elif args.action == "shutdown":
        stopped = droid.shutdown(args.avd)
        print("\n".join(f"Stopped {name}" for name in stopped) or "No Mobster emulators to stop.")
    else:
        rows = droid.devices()
        if as_json:
            _print_json({"devices": rows})
        elif not rows:
            print("No Android devices. `mobster sim android boot` starts an emulator.")
        for row in rows if not as_json else ():
            print(f"{row['name']:<24} {row['kind']:<9} {row['state']:<12} {'in use' if row['in_use'] else 'idle':<7} "
                  f"{row['serial']}")
    return EXIT_OK


def _since(epoch):
    minutes = max(0, int((time.time() - float(epoch or time.time())) // 60))
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60} min"


PHASES = (("create", "create"), ("boot", "boot"), ("configure", "settings"),
          ("wda_build", "WebDriverAgent build"), ("wda_start", "WebDriverAgent start"))


def _prepare(manager, args, as_json, end=lambda: None):
    lease = manager.acquire(args.device, args.runtime)
    try:
        target, timing = lease.target, dict(lease.timing)
    finally:
        lease.release()
    end()
    if as_json:
        _print_json({"udid": target.udid, "name": target.name, "device_type": target.device_type,
                     "runtime": target.runtime, "wda_url": target.wda_url, "mjpeg_url": target.mjpeg_url,
                     "xctestrun": target.xctestrun, "timing": timing})
        return EXIT_OK
    phases = [f"{label} {timing[key]:.1f} s" for key, label in PHASES if key in timing]
    print(f"Ready  {target.name}")
    print(f"  UDID            {target.udid}")
    print(f"  WebDriverAgent  {target.wda_url} (MJPEG {target.mjpeg_url})")
    print(f"  Took            {timing.get('total', 0):.1f} s" + (f": {', '.join(phases)}" if phases else
                                                                     ", already up"))
    return EXIT_OK
