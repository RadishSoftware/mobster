"""`mobster intents harvest`: read a simulator's apps and their associated domains into the link atlas.

Hidden unless MOBSTER_EXPERIMENTAL_INTENTS=1. The AASA fetch is the only network use; `--no-network` skips it.
Exit codes: 0 harvested, 3 couldn't (no simulator, no Xcode, usage too).
"""

import json
import sys

from ..devkit import DevError, main, say, subcommands


def add_arguments(parser, helpers):
    add = subcommands(parser, "intents_command")
    harvest = add("harvest", help="read a simulator's apps and domains into the link atlas",
                  description="Read each app's URL schemes and associated domains on a simulator through simctl, "
                              "fetch each domain's apple-app-site-association (https), and keep the atlas under "
                              "Mobster's data folder. Discovered links are never marked verified.")
    harvest.add_argument("--sim", metavar="UDID", help="the simulator (default: the one booted simulator)")
    harvest.add_argument("--force", action="store_true", help="read again although the atlas is under a day old")
    harvest.add_argument("--no-network", action="store_true", help="skip the AASA fetch")
    harvest.add_argument("--json", action="store_true", help="print the atlas as JSON")


def run(args):
    return main("intents", {"harvest": _harvest}, args)


def booted(run):
    """The UDID of the one booted simulator; DevError otherwise."""
    result = run(["xcrun", "simctl", "list", "devices", "booted", "-j"], timeout=30)
    try:
        devices = [d["udid"] for group in json.loads(result.out)["devices"].values() for d in group
                   if d.get("state") == "Booted"] if result.ok else []
    except (ValueError, KeyError, TypeError):
        devices = []
    if len(devices) != 1:
        raise DevError("Mobster needs one booted simulator to read.", "Pass --sim UDID, or boot just one.")
    return devices[0]


def _harvest(args):
    from ..sim.simctl import run as simctl_run
    from . import harvest
    udid = args.sim or booted(simctl_run)
    atlas = harvest.harvest(udid, simctl_run, fetch=None if args.no_network else harvest.fetch_aasa,
                            log=lambda line: say(line, sys.stderr), force=args.force)
    if args.json:
        say(atlas.to_json())
    else:
        for entry in atlas.entries.values():
            say(f"{entry.name}: {', '.join(s + '://' for s in entry.schemes) or '-'}"
                f"  {', '.join(entry.domains) or '-'}  discovered")
        say(f"{len(atlas.entries)} apps declare links.")
    return 0
