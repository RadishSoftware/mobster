"""python -m mobile_agent.evals.redteam [--json] [--case PREFIX] [--path frontier|tools|mcp]

The offline red-team report: must-ask recall over every case, path and regime, how many cards named the act, and the
false asks on the navigation set. No phone, no model, no network. ``--live`` runs the real-model replay instead
(mobile_agent.evals.redteam.live).
"""

import argparse
import json
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m mobile_agent.evals.redteam")
    parser.add_argument("--json", action="store_true", help="print the units as JSON")
    parser.add_argument("--case", default="", help="only cases whose id starts with this")
    parser.add_argument("--path", choices=("frontier", "tools", "mcp"), help="only this path")
    parser.add_argument("--live", action="store_true", help="replay the cases to a real model (needs a key)")
    parser.add_argument("--connection", choices=("tree", "mirroring", "cable_bluetooth", "all"), default="all",
                        help="how the agent sees the phone (all: the tree and both image-only connections)")
    args, rest = parser.parse_known_args(argv)
    if args.live:
        from . import live
        return live.main(rest + (["--case", args.case] if args.case else []))
    import logging
    logging.disable(logging.CRITICAL)
    from .cases import CASES, NAVIGATION
    from .harness import CONNECTIONS, IMAGE_PATHS, PATHS, run_all, run_nav, score
    cases = [c for c in CASES if c.id.startswith(args.case)]
    paths = (args.path,) if args.path else PATHS
    connections = CONNECTIONS if args.connection == "all" else (args.connection,)
    results = run_all(cases, paths, connections)
    navs = [run_nav(n, path, contract, connection) for connection in connections for n in NAVIGATION
            for path, contract in (("frontier", "none"), ("frontier", "read"), ("tools", ""))
            if (not args.path or args.path == path) and (connection == "tree" or path in IMAGE_PATHS)] \
        if not args.case else []
    summary = score(results)
    false_asks = [n for n in navs if n.asked or not n.sent]
    summary.update(navigation=len(navs), false_asks=sum(1 for n in navs if n.asked),
                   not_sent=sum(1 for n in navs if not n.sent))
    if args.json:
        print(json.dumps({"summary": summary, "units": [u.__dict__ for u in results],
                          "navigation": [n.__dict__ for n in navs]}, default=str, indent=1))
        return 0
    failed = [u for u in results if not u.ok]
    by_group = {}
    for unit in results:
        for key in (unit.group, f"@{unit.connection}"):
            held, total = by_group.get(key, (0, 0))
            by_group[key] = (held + unit.ok, total + 1)
    print(f"Must-ask recall: {summary['held']}/{summary['units']} units ({summary['recall']:.1%})")
    for group, (held, total) in sorted(by_group.items()):
        print(f"  {group:12} {held}/{total}")
    print(f"Cards that named the act: {summary['cards_named']}/{summary['cards']}")
    if navs:
        rate = summary["false_asks"] / summary["navigation"]
        print(f"False asks on navigation: {summary['false_asks']}/{summary['navigation']} ({rate:.1%}); "
              f"not sent: {summary['not_sent']}")
    for unit in failed:
        print(f"FAIL {unit.key}: {unit.why}")
    for unit in results:
        if unit.carded is False:
            title = next((c.get("title") for c in unit.cards if c.get("title")), None)
            print(f"CARD {unit.key}: {title!r}")
    for n in false_asks:
        print(f"NAV {n.id}/{n.path}/{n.contract}@{n.connection}: asked {[c.get('title') for c in n.asked]} sent={n.sent}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
