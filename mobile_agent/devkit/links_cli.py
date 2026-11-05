"""`mobster links`: the deep links a project has, and whether each one still lands where it should (links.py).

    mobster links list [--json]                   every link, where it came from, the title its screen should show
    mobster links check [NAME...] [--sim TYPE]    open each on a simulator and say what screen it landed on

``check`` is `mobster test` for links: each link becomes a check that launches the app with the address and expects
its title, so the verdict, the failure's screenshot and the reports (.mobster/test-results/) are the ones checks have.
A link with no title is opened and its landing screen recorded; it reports "needs review" until you add a title to
.mobster/links.yaml. A link that acts as it opens (a call, a shortcut) is never opened by `check`.

Exit codes: 0 every link landed where it should, 1 one didn't, 3 couldn't (usage too).
"""

import json
import sys
from pathlib import Path

from . import DevError, main, say, subcommands


def add_arguments(parser, helpers):
    add = subcommands(parser, "links_command")
    listed = add("list", help="list the project's deep links and where each came from",
                 description="List the project's deep links: the ones in .mobster/links.yaml, the Expo config's "
                             "scheme and associated domains, the Xcode project's URL types and associated domains, "
                             "and links a check opened successfully. Dynamic Expo config runs `npx expo config`.")
    listed.add_argument("--json", action="store_true", help="print them as JSON")
    check = add("check", help="open each link on a simulator and record the screen it lands on",
                description="Open each link on a simulator, in the app, and say what screen it landed on: passed "
                            "when the screen shows the link's title, failed with a screenshot when it doesn't. "
                            "Name links to check some; with none, every link is checked.",
                epilog="exit codes: 0 every link landed where it should, 1 one didn't, 3 couldn't\n\n"
                       "examples:\n  mobster links check\n  mobster links check cart profile --sim \"iPhone 17 Pro\"")
    check.add_argument("names", nargs="*", metavar="NAME", help="links to check, by name or address (default: all)")
    check.add_argument("--sim", action="append", metavar="TYPE", help="a simulator, such as "
                       "\"iPhone 17 Pro\"; repeatable")
    check.add_argument("--device", action="append", metavar="NAME", help="an iPhone from `mobster devices`; repeatable")
    check.add_argument("--bundle", metavar="ID", help="the app's bundle ID (default: read from the project)")
    check.add_argument("--app", metavar="PATH", help="a built .app to install (default: the app "
                       "already on the simulator)")
    check.add_argument("--json", action="store_true", help="print results.json's document")
    for sub in (listed, check):
        sub.add_argument("--project", metavar="DIR", help="the project folder (default: the one you are in)")


def run(args, *, cwd=None):
    return main("links", {"list": _list, "check": _check}, args, cwd=Path(cwd or Path.cwd()))


def _root(args, cwd):
    from ..verify.checks import project_root
    return project_root(Path(args.project).expanduser() if args.project else cwd)


def _links(root):
    from . import links
    try:
        return links.discover(root)
    except links.LinksError as error:
        raise DevError(str(error)) from None


def _list(args, cwd):
    found = _links(_root(args, cwd))
    if args.json:
        print(json.dumps({"links": [link.public() for link in found]}, ensure_ascii=False), flush=True)
        return 0
    if not found:
        say("No deep links found. Add some to .mobster/links.yaml:\n\n  links:\n    - name: "
            "cart\n      url: myapp://cart\n"
            "      title: Your cart")
        return 0
    width = max(len(link.name) for link in found)
    wide = max(len(link.url) for link in found)
    for link in found:
        say(f"{link.name:<{width}}  {link.url:<{wide}}  {link.source:<10}  "
            + (f"\"{link.title}\"" if link.title else ""))
    return 0


def _check(args, cwd):
    from ..style import color_enabled
    from ..testkit.api import Options, UsageError, execute
    from ..testkit.cli import summary_text
    from . import links
    root = _root(args, cwd)
    found = _links(root)
    try:
        chosen = [links.find(found, name) for name in args.names] if args.names else found
    except links.LinksError as error:
        raise DevError(str(error)) from None
    chosen, skipped = links.checkable(chosen)
    for link in skipped:
        say(f"Skipped {link.name}: it acts as it opens, and Mobster doesn't open that one to check it.", sys.stderr)
    if not chosen:
        raise DevError("There are no links to check.", "`mobster links list` shows what Mobster found.")
    options = Options(sims=tuple(args.sim or ()), devices=tuple(args.device or ()), retries=0,
                      command="mobster links check")
    try:
        planned = links.plan_check(root, chosen, options, bundle=args.bundle,
                                   app_path=str(Path(args.app).expanduser().resolve()) if args.app else None)
        doc, paths = execute(planned, progress=lambda line: say(line, sys.stderr))
    except (links.LinksError, UsageError) as error:
        raise DevError(str(error)) from None
    except Exception as error:   # TargetError and the engine's own errors say a sentence
        if type(error).__name__ in ("TargetError", "DiscoveryError"):
            raise DevError(str(error)) from None
        raise
    if args.json:
        print(json.dumps(doc, ensure_ascii=False, allow_nan=False), flush=True)
    else:
        say(landings(doc) + "\n" + summary_text(doc, paths, color=color_enabled(sys.stdout)))
    return int(doc.get("exitCode", 3))


def landings(doc):
    """One line per link and device: the screen it landed on."""
    lines = []
    for check in doc.get("checks") or ():
        for result in check.get("results") or ():
            title = (result.get("screen") or {}).get("title")
            lines.append(f"{check.get('name')}: landed on " + (f"\"{title}\"" if title else "a screen with no title"))
    return "\n".join(lines)
