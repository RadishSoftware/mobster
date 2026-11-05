"""`mobster xcode`: your checks after every simulator build in Xcode (xcode.py does the work)."""

import os
import sys
from pathlib import Path

from . import DevError, main, say, self_argv, subcommands


def add_arguments(parser, helpers):
    add = subcommands(parser, "xcode_command")

    for name, text in (("hook", "run your checks after every simulator build in Xcode"),
                       ("unhook", "stop running your checks after Xcode builds")):
        sub = add(name, help=text,
                  description="Add Mobster's post-action to a shared scheme's build: after each simulator build, "
                              "the checks of the app Xcode built run in the background on that build, and the "
                              "verdict comes as a notification." if name == "hook" else
                              "Take Mobster's post-action out of the scheme. Nothing else in it changes.")
        sub.add_argument("--scheme", metavar="NAME", help="the shared scheme (default: the only one, or the one "
                                                          "named like the project)")
        sub.add_argument("--project", metavar="PATH", help="the .xcodeproj or .xcworkspace (default: the one here)")
        if name == "hook":
            sub.add_argument("--sim", action="append", metavar="TYPE[@RUNTIME]",
                             help="run the checks on this simulator; repeatable (default: each check's own)")
            sub.add_argument("--tag", action="append", metavar="TAG",
                             help="only the checks with this tag, such as smoke; repeatable")
    after = add("after-build", help="run the checks of the app Xcode just built (the post-action runs it)",
                description="Run the checks whose app is the one Xcode just built, on that build. It reads "
                            "BUILT_PRODUCTS_DIR, FULL_PRODUCT_NAME, PLATFORM_NAME and SRCROOT from Xcode. A build "
                            "for a device is left alone.")
    after.add_argument("--background", action="store_true",
                       help="return at once and run in the background, stopping an older build's run")
    after.add_argument("--sim", action="append", metavar="TYPE[@RUNTIME]", help="a simulator to run on; repeatable")
    after.add_argument("--tag", action="append", metavar="TAG", help="only the checks with this tag; repeatable")


def run(args, *, cwd=None, environ=None):
    environ = os.environ if environ is None else environ
    handlers = {"hook": _hook, "unhook": _unhook, "after-build": _after}
    return main("xcode", handlers, args, cwd=Path(cwd or Path.cwd()), environ=environ)


def _project(args, cwd):
    from ..testkit import prbuild
    try:
        found = prbuild.project_arg(cwd / args.project) if args.project else prbuild.find_project(cwd)
        if found is None:
            raise DevError("There's no Xcode project here.", "Run it from your project's folder, or pass --project.")
        scheme = args.scheme or prbuild.pick_scheme(found, cwd=cwd)
    except prbuild.BuildError as error:
        raise DevError(error.message, error.fix) from None
    return found[1], scheme


def _hook(args, cwd, environ):
    from . import xcode
    project, scheme = _project(args, cwd)
    path = xcode.hook(project, scheme, sims=tuple(args.sim or ()), tags=tuple(args.tag or ()))
    say(f"Added the \"{xcode.TITLE}\" post-action to {path.relative_to(cwd) if path.is_relative_to(cwd) else path}.\n"
         f"After each simulator build of {scheme}, Mobster runs the checks of the app you built, in the background, "
         "and says the verdict in a notification. The log is .mobster/xcode/last.log.\n"
         f"Commit the scheme to share it with your team. Take it out with: mobster xcode unhook --scheme {scheme}")
    return 0


def _unhook(args, cwd, environ):
    from . import xcode
    project, scheme = _project(args, cwd)
    if xcode.unhook(project, scheme):
        say(f"Took the \"{xcode.TITLE}\" post-action out of {scheme}.")
    else:
        say(f"{scheme} has no Mobster post-action.")
    return 0


def _after(args, cwd, environ):
    from ..verify.checks import project_root
    from . import xcode
    app, platform = xcode.built_app(environ)
    root = project_root(Path(environ.get("SRCROOT") or cwd))
    if platform != "iphonesimulator":
        built = platform or "an unknown platform"
        say(f"Skipped: Xcode built for {built}, and the post-action runs checks on simulator builds.")
        return 0
    if args.background:
        argv, extra = self_argv()
        argv = argv + ["xcode", "after-build"] + [item for sim in args.sim or () for item in ("--sim", sim)] + \
            [item for tag in args.tag or () for item in ("--tag", tag)]
        pid = xcode.spawn_background(root, argv, {**environ, **extra})
        say(f"Running the checks of {app.name} in the background (pid {pid}).")
        return 0
    from ..devtools import ProgressLines
    progress = ProgressLines(sys.stderr)
    try:
        code, headline = xcode.run_checks(root, app, sims=tuple(args.sim or ()), tags=tuple(args.tag or ()),
                                          progress=progress)
    finally:
        progress.end()
    say(headline)
    return code
