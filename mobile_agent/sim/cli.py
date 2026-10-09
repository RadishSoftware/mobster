"""`mobster sim` (and `python -m mobile_agent.sim`): Mobster's own headless simulators.

Exactly two functions, as every developer command module has (SPEC §12.3): ``add_arguments(parser, helpers)``
adds the sub-commands under dest "sim_command", and ``run(args)`` runs one and returns its exit code:
0 ok, 1 a check or an action failed, 2 usage.
"""

import argparse
import json
import sys

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


def add_arguments(parser, helpers):
    """``helpers`` ({"env_option", "path_type", "bounded"} from mobster's main) goes unused: the sim commands
    take no --env-file or --wda-url of their own."""
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

    prepare = command("prepare", "create, boot and start WebDriverAgent, timing each phase",
                      "Create the simulator if needed, boot it headless, build WebDriverAgent the first time, start "
                      "it on 127.0.0.1, and print how long each phase took. It stays up afterwards.")
    prepare.add_argument("--device", metavar="NAME", help="the simulator device type (default: iPhone 17 Pro, else "
                                                          "the newest iPhone the runtime supports)")
    prepare.add_argument("--runtime", metavar="NAME", help="the iOS runtime, such as \"iOS 26.4\" (default: the "
                                                           "newest installed)")
    prepare.add_argument("--json", action="store_true", help="print the simulator and the phase times as JSON")

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
    from .api import SimError, SimulatorManager
    as_json = bool(getattr(args, "json", False))
    manager = SimulatorManager(progress=progress)
    end = getattr(progress, "end", lambda: None)  # a "Still …" line on a terminal
    name = getattr(args, "sim_command", None)
    try:
        if name == "list":
            return _list(manager, as_json)
        if name == "prepare":
            return _prepare(manager, args, as_json, end)
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
        from ..console import color_enabled
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
    table = [("NAME", "STATE", "WEBDRIVERAGENT", "IN USE", "UDID")]
    for row in rows:
        wda = row["wda_url"].split("//")[-1] if row["wda"] == "ready" else "stopped"
        table.append((row["name"], row["state"], wda, "yes" if row["in_use"] else "no", row["udid"]))
    widths = [max(len(line[column]) for line in table) for column in range(len(table[0]) - 1)]
    for line in table:
        print("  ".join(cell.ljust(width) for cell, width in zip(line, widths)) + "  " + line[-1])
    return EXIT_OK


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
