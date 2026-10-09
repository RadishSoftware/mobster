"""`mobster memory`: see and edit what Mobster remembers (devtools.py creates its parser with fixed help).

It reads and writes the same file as the Mac app (user_data_dir()/memory/memory.sqlite3), so a change here shows in
Settings › Memory the next time it's opened. ``run(args)`` returns 0 done, 1 refused or not found, 2 usage, 3 memory
can't be opened here (no home folder, or saved by a newer Mobster), 130 ctrl+c. It never raises.
"""

import argparse
import json
from pathlib import Path
import sys

from ..api_errors import APIError

CONFIRM = "delete everything"


def add_arguments(parser, helpers):
    subs = parser.add_subparsers(dest="memory_command", metavar="<subcommand>")

    def as_json(sub):
        sub.add_argument("--json", action="store_true", help="print JSON")

    listing = subs.add_parser("list", help="list what Mobster remembers (the default)",
                              description="List what Mobster remembers: pinned things first, then the newest.")
    # dest "bundle": `--app` before a command's name is the terminal UI's (__main__.check_placement reads args.app).
    listing.add_argument("--app", dest="bundle", metavar="BUNDLE", help="only what Mobster remembers for this app (its bundle ID)")
    as_json(listing)

    add = subs.add_parser("add", help="tell Mobster something to remember",
                          description="Tell Mobster something to remember, in your words (up to 300 characters). "
                                      "Pinned things go with every task; the rest go with tasks that mention them. "
                                      "Mobster never remembers passwords, codes or card numbers.",
                          epilog='examples:\n  mobster memory add "My gym is the one on 5th Street"\n'
                                 '  mobster memory add "Sign my texts with – Sam" --app com.apple.MobileSMS --pin')
    add.add_argument("text", metavar="TEXT", help="what to remember")
    add.add_argument("--app", dest="bundle", metavar="BUNDLE", help="only for tasks in this app (its bundle ID)")
    add.add_argument("--pin", action="store_true", help="send it with every task (up to 10 pinned)")
    as_json(add)

    remove = subs.add_parser("rm", help="forget one thing", description="Forget one thing, by the ID `list` shows.")
    remove.add_argument("id", metavar="ID", help="the 12-character ID from `mobster memory list`")

    pin = subs.add_parser("pin", help="send one thing with every task",
                          description="Pin one thing, so it goes with every task (up to 10 pinned).")
    pin.add_argument("id", metavar="ID", help="the 12-character ID from `mobster memory list`")
    unpin = subs.add_parser("unpin", help="send one thing only with tasks that mention it",
                            description="Unpin one thing: it goes only with tasks that mention it.")
    unpin.add_argument("id", metavar="ID", help="the 12-character ID from `mobster memory list`")

    clear = subs.add_parser("clear", help="delete everything Mobster remembers",
                            description="Delete everything Mobster remembers, and its suggestions. Asks you to "
                                        "type \"delete everything\" first, unless you add --yes.")
    clear.add_argument("--yes", action="store_true", help="don't ask")

    export = subs.add_parser("export", help="print everything Mobster remembers as Markdown",
                             description="Print everything Mobster remembers as Markdown, or save it with --output.")
    export.add_argument("--output", metavar="FILE", help="save it to this file instead")

    # Routines (v2) aren't in this build: the command answers so, and stays out of the help.
    routines = subs.add_parser("routines", help=argparse.SUPPRESS,
                               description="Routines: tasks Mobster has done before and replays. Not in this build "
                                           "yet.")
    routines.add_argument("rest", nargs="*", help="list, show ID, run ID or rm ID")


def _print(text=""):
    try:
        print(text, flush=True)
    except BrokenPipeError:
        pass


def _fail(message, code=1):
    print(f"mobster memory: {message}", file=sys.stderr, flush=True)
    return code


def _scope(bundle):
    return f"app:{bundle}" if bundle else "global"


def _line(fact):
    where = f"In {fact['app']['name']}: " if fact.get("app") else ""
    return f"{fact['id']}  {where}{fact['text']}{'  (pinned)' if fact['pinned'] else ''}"


def run(args):
    from .store import default_store
    command = getattr(args, "memory_command", None) or "list"
    if command == "routines":
        return _fail("Routines aren't in this build yet.", 3)
    store = default_store()
    if store is None:
        return _fail("Mobster can't keep memory here: there's no home folder (HOME isn't set).", 3)
    try:
        return COMMANDS[command](store, args)
    except APIError as error:
        if error.status == 503:
            return _fail(str(error), 3)
        return _fail(str(error), 1)
    except KeyboardInterrupt:
        return 130


def _list(store, args):
    bundle = getattr(args, "bundle", None)
    facts = store.list_facts(scope=_scope(bundle) if bundle else None)
    if getattr(args, "json", False):
        _print(json.dumps({"facts": facts}, ensure_ascii=False))
        return 0
    if not facts:
        _print("Mobster doesn't remember anything yet. Tell it something:\n"
               '  mobster memory add "My gym is the one on 5th Street"')
        return 0
    for fact in facts:
        _print(_line(fact))
    count = len(facts)
    _print(f"\n{count} {'thing' if count == 1 else 'things'}. Pinned ones go with every task; the rest go with "
           "tasks that mention them. Everything stays on this Mac.")
    return 0


def _add(store, args):
    fact = store.add_fact(args.text, _scope(args.bundle), pinned=args.pin)
    if args.json:
        _print(json.dumps({"fact": fact}, ensure_ascii=False))
    else:
        _print(f"Mobster will remember: {fact['text']}  ({fact['id']})")
    return 0


def _remove(store, args):
    fact = store.get_fact(args.id)
    store.delete_fact(args.id)
    _print(f"Forgot: {fact['text']}")
    return 0


def _pin(pinned):
    def change(store, args):
        fact = store.update_fact(args.id, pinned=pinned)
        _print(f"{'Pinned' if pinned else 'Unpinned'}: {fact['text']}")
        return 0
    return change


def _clear(store, args):
    count = store.count()
    if not args.yes:
        if not sys.stdin.isatty():
            return _fail("Add --yes to delete everything Mobster remembers without being asked.", 2)
        try:
            typed = input(f'Delete all {count} {"thing" if count == 1 else "things"} Mobster remembers? '
                          f'Type "{CONFIRM}" to confirm: ')
        except EOFError:
            typed = ""
        if typed.strip().lower() != CONFIRM:
            _print("Nothing was deleted.")
            return 1
    deleted = store.clear()
    _print(f"Deleted everything Mobster remembered ({deleted['facts']} "
           f"{'thing' if deleted['facts'] == 1 else 'things'}).")
    return 0


def _export(store, args):
    text = store.export_markdown()
    if args.output:
        try:
            Path(args.output).expanduser().write_text(text, encoding="utf-8")
        except OSError as error:
            return _fail(f"Couldn't write {args.output}: {error.strerror or error}.", 1)
        _print(f"Saved to {args.output}")
        return 0
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except BrokenPipeError:
        pass
    return 0


COMMANDS = {"list": _list, "add": _add, "rm": _remove, "pin": _pin(True), "unpin": _pin(False), "clear": _clear,
            "export": _export}
