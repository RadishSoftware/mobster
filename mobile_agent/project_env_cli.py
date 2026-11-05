"""`mobster env set|list|unset`: a project's test credentials and other secrets (project_env.py).

A value is read from a hidden prompt or from standard input, never from an argument (a shell would keep it in its
history), and is never printed.
"""

import getpass
import json
import sys
from pathlib import Path

from . import project_env
from .cli_constants import EXIT_FAILED, EXIT_OK, EXIT_USAGE


def add_arguments(parser, helpers):
    subs = parser.add_subparsers(dest="env_command", metavar="<subcommand>")
    subs.required = True
    for name, text in (("set", "save a value for a name, in your Keychain"),
                       ("list", "show the names and which have a value"),
                       ("unset", "remove a name's saved value")):
        sub = subs.add_parser(name, help=text, description=text[0].upper() + text[1:] + ".")
        sub.add_argument("--project", metavar="DIR", default=None,
                         help="the project folder (default: the folder with .mobster/ above this one)")
        sub.add_argument("--json", action="store_true", help="print the result as JSON")
        if name != "list":
            sub.add_argument("name", metavar="NAME", help="the variable's name, such as TEST_PASSWORD")
        if name == "set":
            sub.add_argument("--stdin", action="store_true",
                             help="read the value from standard input instead of asking for it")


def _root(args):
    from .verify.checks import project_root
    return project_root(args.project)


def run(args):
    def out(value, text):
        print(json.dumps(value) if args.json else text, flush=True)

    try:
        root = _root(args)
        env = project_env.ProjectEnv(root)
        if args.env_command == "list":
            rows = env.status()
            if not rows:
                out({"project": str(root), "names": []},
                    f"{project_env.EXAMPLE} lists no names. Add one with `mobster env set NAME`.")
            else:
                width = max(len(row["name"]) for row in rows)
                out({"project": str(root), "names": rows},
                    "\n".join(f"{row['name']:<{width}}  " + (f"set ({row['source']})" if row["set"] else "not set")
                              for row in rows))
            return EXIT_OK
        if args.env_command == "unset":
            removed = env.unset(args.name)
            out({"name": args.name, "removed": removed},
                f"Removed {args.name}." if removed else f"{args.name} had no saved value.")
            return EXIT_OK
        if args.stdin:
            value = sys.stdin.read().rstrip("\r\n")
        elif sys.stdin.isatty():
            value = getpass.getpass(f"Value for {args.name} (hidden): ")
        else:
            print("Pipe the value in with --stdin; Mobster won't take it as an argument.", file=sys.stderr)
            return EXIT_USAGE
        env.set(args.name, value)
        out({"name": args.name, "saved": True},
            f"Saved {args.name} in your Keychain for {Path(root).name}. {project_env.EXAMPLE} lists the name only.")
        return EXIT_OK
    except project_env.EnvError as error:
        print(str(error), file=sys.stderr)
        return EXIT_FAILED
