"""`mobster xray diff BEFORE AFTER`: what changed between two maps of one app."""

import json
import sys
from pathlib import Path

HELP = "compare two maps of one app"
DESCRIPTION = (
    "Compare two maps `mobster xray app` wrote: the screens added, removed and changed, and the paywall prices\n"
               "that moved. Each argument is a map.json file or the folder that holds it. It reads two files on this\n"
               "Mac and needs no phone and no plan.")
EPILOG = ("exit codes: 0 compared (changes or none), 1 a map couldn't be read, 2 usage\n\n"
          "examples:\n"
          "  mobster xray diff before/map.json after/map.json\n"
          "  mobster xray diff before after --json")


def add_arguments(parser):
    parser.add_argument("before", metavar="BEFORE", help="the older map: a map.json file or its folder")
    parser.add_argument("after", metavar="AFTER", help="the newer map: a map.json file or its folder")
    parser.add_argument("--json", action="store_true", help="print the comparison as JSON")


def load(path):
    path = Path(path).expanduser()
    if path.is_dir():
        path = path / "map.json"
    return json.loads(path.read_text())


def run(args):
    from .diff import compare, summary
    try:
        result = compare(load(args.before), load(args.after))
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"mobster xray: couldn't read a map ({type(error).__name__}). Pass map.json files or the folders that "
              "hold them.", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=1, ensure_ascii=False))
    else:
        print("\n".join(summary(result)))
    return 0
