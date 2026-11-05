"""`mobster corpus` (only with MOBSTER_EXPERIMENTAL_CORPUS=1): the opt-in, what is stored, and an export to read.

Added to the engine's commands by corpus.install as an extension: ``add_arguments(subs, commands)`` and
``run(args, output)``, which returns an exit code (0 done, 1 refused, 2 usage) or None for another command.
"""

import json
import sys

from . import consent, store


def add_arguments(subs, commands):
    parser = subs.add_parser("corpus", prog="mobster corpus", help="keep and inspect Mobster's private run corpus",
                             description="Keep a private record of each finished task's screens and actions on this "
                                         "Mac, if you opt in, and read it. Nothing is uploaded.")
    sub = parser.add_subparsers(dest="corpus_command", metavar="<subcommand>")
    for name, help in (("status", "say whether the corpus is on"), ("off", "turn the corpus off"),
                       ("stats", "count what is stored")):
        sub.add_parser(name, help=help).add_argument("--json", action="store_true", help="print JSON")
    on = sub.add_parser("on", help="show what is kept and turn the corpus on",
                        description=consent.TEXT)
    on.add_argument("--yes", action="store_true", help="answer yes to the question after the text is shown")
    export = sub.add_parser("export", help="write the stored records to a file you can read")
    export.add_argument("--out", required=True, metavar="FILE", help="a new file to write")
    commands["corpus"] = parser


def run(args, output):
    if getattr(args, "command", None) != "corpus":
        return None
    word = getattr(args, "corpus_command", None) or "status"
    as_json = getattr(args, "json", False)
    try:
        if word == "on":
            print(consent.TEXT)
            if not args.yes:
                if not sys.stdin.isatty() or input("\nTurn it on? Type yes: ").strip().lower() != "yes":
                    print("Not turned on.", file=sys.stderr)
                    return 1
            consent.set_on(True)
            print("\nThe corpus is on. `mobster corpus off` turns it off.")
        elif word == "off":
            consent.set_on(False)
            print("The corpus is off. What is stored stays until you delete the corpus folder.")
        elif word == "status":
            state = {"on": consent.on(), "folder": str(store.directory())}
            print(json.dumps(state) if as_json else f"The corpus is {'on' if state['on'] else 'off'}.")
        elif word == "stats":
            data = store.stats()
            print(json.dumps(data) if as_json else
                  f"{data['records']} steps from {data['runs']} runs, {data['bytes']} of {data['capBytes']} bytes "
                  f"in {data['files']} files, in {store.directory()}")
        elif word == "export":
            count = store.export(args.out)
            print(f"Wrote {count} records to {args.out}. Open it to see exactly what is stored.")
        else:
            print("mobster corpus: use on, off, status, stats or export", file=sys.stderr)
            return 2
    except (store.CorpusError, OSError) as error:
        print(f"mobster corpus: {error}", file=sys.stderr)
        return 1
    return 0
