"""`mobster test snapshots`: the project's screenshot baselines (verify/snapshots.py) and whether a check still
makes each."""

import json
import sys
from pathlib import Path

SNAPSHOTS = "snapshots"   # the subcommand: mobster test snapshots
VIEWS = ("list", "stale", "orphaned")


def add_arguments(which):
    """The suite's snapshot flags, in ``mobster test``'s check-selection group."""
    which.add_argument("--check", action="append", metavar="NAME", dest="names",
                       help="only the check with this name or file name; repeatable")
    which.add_argument("--update-snapshots", action="store_true",
                       help="write each snapshot's baseline from this run, in .mobster/snapshots/ (with --check, "
                            "only those checks'); the run doesn't fail on a changed screen")


def options(args):
    """The suite's options (api.Options) from the flags add_arguments defines."""
    return {"update_snapshots": bool(getattr(args, "update_snapshots", False)),
            "names": tuple(getattr(args, "names", None) or ())}


def snapshots(args, rest, *, out=None, cwd=None):
    """`mobster test snapshots [list|stale|orphaned]`: the project's screenshot baselines and whether a check still
    makes each. Runs nothing."""
    from ..verify import snapshots as store
    from ..verify.checks import project_root
    from .cli import _usage
    from .discover import DiscoveryError, discover
    out = out or sys.stdout
    view = rest[0] if rest else "list"
    if view not in VIEWS or len(rest) > 1:
        return _usage(f"snapshots takes one of {', '.join(VIEWS)}: mobster test snapshots stale")
    cwd = Path(cwd or Path.cwd())
    root = project_root(cwd)
    try:
        entries = discover((), root, cwd=cwd)
    except DiscoveryError:
        entries = []   # no checks: every baseline is orphaned
    checks = [(store.check_key(entry.path, root, entry.name), entry.check) for entry in entries
              if entry.check is not None]
    found = store.audit(root, checks)
    if view != "list":
        found = [item for item in found if item["state"] == view]
    if args.json:
        print(json.dumps({"snapshots": found}, ensure_ascii=False, allow_nan=False), file=out, flush=True)
        return 0
    if not found:
        print("No screenshot baselines." if view == "list" and not store.baselines(root) else
              f"No {view} screenshot baselines.", file=out, flush=True)
        return 0
    for item in found:
        print(f"{item['state']:<8} {item['file']}", file=out)
    print(f"{len(found)} baseline{'' if len(found) == 1 else 's'}. Delete an orphaned or stale file with git rm, or "
          "run `mobster test --update-snapshots` to record the current screens.", file=out, flush=True)
    return 0
