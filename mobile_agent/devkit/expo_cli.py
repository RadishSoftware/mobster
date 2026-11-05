"""`mobster expo`: an Expo or React Native app on a simulator or your iPhone (expo.py does the work)."""

import base64
import json
import sys
import time
from pathlib import Path

from . import DevError, expo_dev, main, say, subcommands

EXPECT_WAIT = 45
CHECK_EVERY = 3


class ClientMissing(DevError):
    """The device couldn't open the dev client's link: it isn't installed there (or didn't answer)."""


def add_arguments(parser, helpers):
    add = subcommands(parser, "expo_command")

    doctor = add("doctor", help="say what Mobster found in this Expo or React Native project",
                 description="Read package.json and the Expo config, and say what Mobster found: the app's name, "
                             "bundle ID and schemes, whether it opens in your dev client or Expo Go, whether the "
                             "ios folder exists, and whether Metro is running. Reads only.")
    doctor.add_argument("--port", type=int, default=8081, metavar="N", help="Metro's port (default: 8081)")
    doctor.add_argument("--json", action="store_true", help="print it as JSON")

    preview = add("preview", help="open the app on a simulator or your iPhone from Metro, and check a screen",
                  description="Open your app on a simulator or your iPhone from the JavaScript Metro serves from "
                              "your working tree: in your dev client when the app has expo-dev-client, else in "
                              "Expo Go. Starts Metro in the background when nothing answers on the port. With "
                              "--expect, waits for the text and says whether it's on screen, with a screenshot.",
                  epilog="exit codes: 0 opened (and every expectation held), 1 an expectation didn't hold, "
                         "3 couldn't\n\nexamples:\n  mobster expo preview\n"
                         "  mobster expo preview --device \"Test iPhone\" --expect \"Welcome back\"\n"
                         "  mobster expo preview --path settings/profile --expect Profile")
    preview.add_argument("--device", metavar="NAME", help="a device from `mobster devices` (default: your iPhone, "
                                                          "else the only device)")
    preview.add_argument("--port", type=int, default=8081, metavar="N", help="Metro's port (default: 8081)")
    preview.add_argument("--path", metavar="PATH",
                         help="then open this screen by the app's own deep link, such as settings/profile")
    preview.add_argument("--expect", action="append", metavar="TEXT",
                         help="text that must be on screen once the app loads; repeatable")
    preview.add_argument("--wait", type=float, default=EXPECT_WAIT, metavar="S",
                         help=f"how long --expect waits for the bundle to load (default: {EXPECT_WAIT})")
    preview.add_argument("--no-start", action="store_true", help="don't start Metro; fail when it isn't running")
    preview.add_argument("--screenshot", metavar="FILE",
                         help="save the screen here (default with --expect: .mobster/expo/preview.png)")
    preview.add_argument("--json", action="store_true", help="print the result as JSON")

    dev = add("dev", help="run the dev loop: Metro, the dev client, the app open, a reload on every save",
              description="Start this project's Metro (or reuse it), install your dev client on the device when "
                          "it isn't there, open the app, and reload it each time you save a source file. When "
                          "Metro can't bundle the app, it says so with Metro's error and keeps watching: the next "
                          "save tries again. ctrl+c stops the Metro this command started. --check runs saved "
                          "checks on the device once the app is up.",
              epilog="exit codes: 0 stopped with ctrl+c (or --once: opened and every check held), 1 Metro can't "
                     "bundle the app or a check didn't hold, 3 couldn't\n\nexamples:\n"
                     "  mobster expo dev --device \"Test iPhone\"\n"
                     "  mobster expo dev --once --check .mobster/checks/home.yaml")
    dev.add_argument("--device", metavar="NAME", help="a device from `mobster devices` (default: your iPhone, "
                                                      "else the only device)")
    dev.add_argument("--port", type=int, metavar="N", help="Metro's port (default: 8081, else the next free one)")
    dev.add_argument("--check", action="append", metavar="PATH",
                     help="a check file or folder to run on the device once the app is up; repeatable")
    dev.add_argument("--no-install", action="store_true", help="don't build and install the dev "
                     "client when it is missing")
    dev.add_argument("--once", action="store_true", help="open the app, run the checks, and stop; no watching")
    dev.add_argument("--json", action="store_true", help="print one JSON line per event")

    reload = add("reload", help="reload the JavaScript in every app connected to Metro",
                 description="Send Metro's reload to every app connected to it, as pressing r in `npx expo start` "
                             "does.")
    reload.add_argument("--port", type=int, default=8081, metavar="N", help="Metro's port (default: 8081)")

    build = add("build", help="build the app for a simulator or your iPhone",
                description="Build the app. For a simulator: a Release build into .mobster/build with the "
                            "JavaScript inside, so it runs with no Metro (what checks and CI use), after `npx expo "
                            "prebuild` when there's no ios folder. For an iPhone: your dev client, signed with your "
                            "team and installed by `npx expo run:ios --device`. --eas builds an EAS profile on this "
                            "Mac with `eas build --local`.",
                epilog="examples:\n  mobster expo build\n  mobster expo build --device \"iPhone 17 Pro\" --install\n"
                       "  mobster expo build --device \"Test iPhone\"\n"
                       "  mobster expo build --eas development --device \"Test iPhone\" --install")
    build.add_argument("--device", metavar="NAME",
                       help="build for this device: an iPhone (installs your dev client) or a Mobster simulator")
    build.add_argument("--install", action="store_true", help="install the build on --device")
    build.add_argument("--debug", action="store_true",
                       help="a Debug build that loads its JavaScript from Metro (the dev client), not Release")
    build.add_argument("--eas", metavar="PROFILE", help="build this EAS profile on this Mac (eas build --local)")
    build.add_argument("--json", action="store_true", help="print the build as JSON")

    add("stop", help="stop the Metro that `mobster expo preview` started",
        description="Stop the Metro server `mobster expo preview` started in the background for this project.")


def run(args, *, cwd=None, devices=None, wda=None):
    if devices is None or wda is None:
        from ..phone_io import choose_device
        from ..phone_io import wda as phone_wda
        devices, wda = devices or choose_device, wda or phone_wda
    handlers = {"doctor": _doctor, "preview": _preview, "dev": _dev, "reload": _reload, "build": _build, "stop": _stop}
    return main("expo", handlers, args, cwd=Path(cwd or Path.cwd()), devices=devices, wda=wda)


def _doctor(args, cwd, devices, wda):
    from . import expo
    app = expo.find_app(cwd)
    running = expo.metro_running(args.port, root=app.root)
    info = {**app.public(), "metro": {"port": args.port, "running": running}}
    if args.json:
        say(json.dumps(info, ensure_ascii=False))
        return 0
    kind = f"Expo app ({app.sdk})" if app.expo else "React Native app"
    lines = [f"{app.name}: {kind}",
             f"  Bundle ID   {app.bundle or 'not set (ios.bundleIdentifier)'}",
             f"  Schemes     {', '.join(app.schemes) or 'none (scheme)'}",
             f"  Opens in    {'your dev client (expo-dev-client)' if app.client == 'dev-client' else 'Expo Go'}",
             f"  ios folder  {'yes' if app.native else 'no: `mobster expo build` runs npx expo prebuild first'}",
             f"  Metro       {'running' if running else 'not running'} on port {args.port}",
             "", "Checks and CI build Release for the simulator, with the JavaScript inside: mobster expo build"]
    lines += [f"Note: {note}" for note in app.notes]
    say("\n".join(lines))
    return 0


def _preview(args, cwd, devices, wda):
    from . import expo
    app = expo.find_app(cwd)
    if not app.expo:
        raise DevError("Preview opens Expo apps. This React Native app has no Expo.",
                       "Run it from Xcode, or `mobster expo build --device NAME --install`.")
    record = devices(args.device)
    started = None
    if not expo.metro_running(args.port, root=app.root):
        if args.no_start:
            raise DevError(f"Metro isn't running on port {args.port}.", "Start it with `npx expo start`.")
        say(f"Starting Metro on port {args.port}: npx expo start (log: {expo.STATE_DIR}/metro.log)", sys.stderr)
        server, owned = expo_dev.ensure(app, args.port)
        started = server["pid"] if owned else None
    url = expo.preview_url(app, expo.host_for(record), args.port, args.path)
    link = expo.app_link(app, args.path) if args.path and app.client == "dev-client" else None
    if args.path and app.client == "dev-client" and link is None:
        raise DevError("--path needs the app's own URL scheme, and its Expo config has no scheme.",
                       "Add \"scheme\" to app.json, then rebuild the dev client.")
    result = preview(app, record, wda, url=url, link=link, expect=list(args.expect or ()), wait=args.wait,
                     screenshot=args.screenshot,
                     metro_error=lambda: expo_dev.bundle_error(app, args.port, wait=CHECK_EVERY))
    result["metroStarted"] = started
    if args.json:
        say(json.dumps(result, ensure_ascii=False))
    else:
        lines = [f"Opened {app.name} on {record.get('name')} from Metro on port {args.port}"
                 f" ({'your dev client' if app.client == 'dev-client' else 'Expo Go'})."]
        for item in result["expect"]:
            lines.append(f"{'✓' if item['ok'] else '✗'} {item['text']}: {item['observed']}")
        if result.get("error"):
            lines.append(f"✗ The app failed to load: {result['error']}")
        if result.get("screenshot"):
            lines.append(f"Screenshot  {result['screenshot']}")
        if started:
            lines.append("Metro keeps running in the background: `mobster expo reload` after a change, "
                         "`mobster expo stop` when you're done.")
        say("\n".join(lines))
    return 0 if all(item["ok"] for item in result["expect"]) and not result.get("error") else 1


def preview(app, record, wda, *, url, link=None, expect=(), wait=EXPECT_WAIT, screenshot=None,
            clock=time.monotonic, sleep=time.sleep, metro_error=None):
    from ..deeplinks import LinkError, open_link
    from ..phone_io import Calls, PhoneIOError
    from ..verify.assertions import evaluate_on, expand_assertion
    from ..verify.tree import read_tree
    assertions = [item for text in expect for item in expand_assertion({"text": text}, "--expect")]
    results = []
    shot = None
    failure = None
    with wda(record) as request:
        driver = Calls(request)
        try:
            open_link(driver, url)
            if link:
                sleep(3)
                open_link(driver, link)
        except (LinkError, PhoneIOError) as error:
            client = "your dev client" if app.client == "dev-client" else "Expo Go"
            fix = (f"Install it first: mobster expo build --device \"{record.get('name')}\" --install"
                   if app.client == "dev-client" else "Install Expo Go from the App Store on the iPhone.")
            raise ClientMissing(f"{record.get('name')} couldn't open {url}: {client} isn't installed there, or "
                                f"didn't answer ({error}).", fix) from None
        if assertions:
            deadline = clock() + max(0.0, float(wait))
            polls = 0
            while True:
                try:
                    tree = read_tree(driver)
                    results = evaluate_on(assertions, tree)
                except Exception:
                    tree, results = None, []
                if results and all(item.ok for item in results):
                    break
                if tree is not None:
                    failure = expo_dev.screen_error([text for node in tree.shown()
                                                     for text in (node.label, node.value)])
                if failure is None and metro_error is not None and polls % CHECK_EVERY == 0:
                    found = metro_error()
                    failure = found["message"] if found else None
                polls += 1
                if failure or clock() >= deadline:
                    break
                sleep(1)
            if not results:
                results = [_Unread(text) for text in expect]
        if not assertions:
            try:
                tree = read_tree(driver)
                failure = expo_dev.screen_error([text for node in tree.shown() for text in (node.label, node.value)])
            except Exception:
                pass
            if failure is None and metro_error is not None:
                found = metro_error()
                failure = found["message"] if found else None
        if screenshot or assertions:
            target = Path(screenshot or Path(app.root) / ".mobster" / "expo" / "preview.png")
            try:
                data = request("GET", "/screenshot", None, 20)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(base64.b64decode(data))
                shot = str(target)
            except Exception:
                shot = None
    return {"url": url, "link": link, "device": record.get("name"),
            "expect": [{"text": item.text, "ok": bool(item.ok), "observed": item.observed} for item in results],
            "screenshot": shot, "error": failure}


class _Unread:
    """An expectation whose screen couldn't be read before the wait ran out."""

    ok, observed = False, "the screen couldn't be read"

    def __init__(self, text):
        self.text = f'text "{text}"'


def _event(args, kind, **fields):
    if args.json:
        say(json.dumps({"event": kind, **fields}, ensure_ascii=False))


def _dev(args, cwd, devices, wda, *, sleep=time.sleep, watcher=None, rounds=None, run_checks=None):
    from ..devtools import signals_as_interrupt
    from . import expo
    app = expo.find_app(cwd)
    if not app.expo:
        raise DevError("The dev loop opens Expo apps. This React Native app has no Expo.",
                       "Run it from Xcode, or `mobster expo build --device NAME --install`.")
    record = devices(args.device)
    with signals_as_interrupt():
        server, started = expo_dev.ensure(app, args.port)
        port = server["port"]
        try:
            _event(args, "metro", port=port, started=started)
            if not args.json:
                say(f"Metro on port {port}" + (f" (started; log: {expo.STATE_DIR}/metro.log)." if started else
                                               " (already running for this project)."), sys.stderr)
            failed = expo_dev.bundle_error(app, port)
            if failed:
                return _failed(args, failed)
            result = _open(args, app, record, wda, port)
            if result.get("error"):
                return _failed(args, {"message": result["error"]})
            _event(args, "opened", device=record.get("name"), screenshot=result.get("screenshot"))
            if not args.json:
                say(f"Opened {app.name} on {record.get('name')}.")
            if args.check:
                code = (run_checks or _run_checks)(record.get("name"), args.check)
                _event(args, "checks", exit=code)
                if code or args.once:
                    return code
            if args.once:
                return 0
            if not args.json:
                say("Watching for saves: ctrl+c stops.", sys.stderr)
            watching = watcher or expo_dev.Watcher(app.root)
            count = 0
            while rounds is None or count < rounds:
                count += 1
                sleep(1)
                if not watching.changed():
                    continue
                expo.reload(port)
                failed = expo_dev.bundle_error(app, port)
                if failed:
                    _failed(args, failed, final=False)
                else:
                    _event(args, "reloaded", port=port)
                    if not args.json:
                        say("Reloaded.")
                    if args.check:
                        code = (run_checks or _run_checks)(record.get("name"), args.check)
                        _event(args, "checks", exit=code)
            return 0
        except KeyboardInterrupt:
            return 0
        finally:
            if started:
                expo_dev.stop(app.root, expected_pid=server["pid"])


def _open(args, app, record, wda, port):
    """Open the app on the device. A dev client that isn't there is built and installed once, unless --no-install."""
    from . import expo
    url = expo.preview_url(app, expo.host_for(record), port)
    show = lambda: expo_dev.bundle_error(app, port, wait=CHECK_EVERY)  # noqa: E731
    try:
        return preview(app, record, wda, url=url, expect=[], metro_error=show)
    except ClientMissing:
        if args.no_install or app.client != "dev-client":
            raise
    from ..devtools import ProgressLines
    progress = ProgressLines(sys.stderr)
    try:
        if record.get("kind") == "simulator":
            built = expo.build_sim(app, configuration="Debug", progress=progress)
            from ..phone_io.install import install
            install(record, built["path"], progress=progress)
        else:
            if not record.get("udid"):
                raise DevError(f"Mobster doesn't know {record.get('name')}'s UDID.", "Plug the iPhone in.")
            expo.build_device(app, record["udid"], configuration="Debug", progress=progress)
    finally:
        progress.end()
    return preview(app, record, wda, url=url, expect=[], metro_error=show)


def _failed(args, error, final=True):
    """Say Metro can't bundle the app: its words, the file and the line when it names them."""
    where = f" ({error['file']}:{error['line']})" if error.get("file") and error.get("line") else ""
    _event(args, "failed", error=error)
    say(f"mobster expo dev: Metro can't bundle the app: {error['message']}{where}" +
        ("" if final else " Fix it and save: the next save tries again."), sys.stderr)
    return 1


def _run_checks(device_name, paths):
    from ..cli_parser import build_parser
    from ..testkit import cli as testkit_cli
    return testkit_cli.run(build_parser().parse_args(["test", "--device", device_name, *paths]))


def _reload(args, cwd, devices, wda):
    from . import expo
    expo.reload(args.port)
    say(f"Reloaded the apps connected to Metro on port {args.port}.")
    return 0


def _build(args, cwd, devices, wda):
    from ..devtools import ProgressLines
    from . import expo
    app = expo.find_app(cwd)
    record = devices(args.device) if args.device else None
    if args.install and record is None:
        raise DevError("--install needs --device: the simulator or iPhone to install on.")
    phone = record is not None and record.get("kind") != "simulator"
    progress = ProgressLines(sys.stderr)
    try:
        if args.eas:
            built = expo.build_eas(app, args.eas, simulator=not phone, progress=progress)
        elif phone:
            if not record.get("udid"):
                raise DevError(f"Mobster doesn't know {record.get('name')}'s UDID.", "Plug the iPhone in.")
            built = expo.build_device(app, record["udid"], configuration="Debug", progress=progress)
        else:
            built = expo.build_sim(app, configuration="Debug" if args.debug else "Release", progress=progress)
        installed = False
        if args.install and built.get("path"):
            from ..phone_io.install import install
            install(record, built["path"], progress=progress)
            installed = True
        elif phone and not args.eas:
            installed = True  # npx expo run:ios --device installs it
    finally:
        progress.end()
    built = {**built, "installed": installed, "device": record.get("name") if record else None}
    if args.json:
        say(json.dumps(built, ensure_ascii=False))
        return 0
    where = built.get("path") or "the iPhone"
    lines = [f"Built {app.name}: {where}"]
    if installed:
        lines.append(f"Installed on {record.get('name')}.")
    if built["platform"] == "sim" and not args.debug and not args.eas:
        lines.append("Release, with the JavaScript inside: it runs with no Metro. Point a check's app.path at it.")
    say("\n".join(lines))
    return 0


def _stop(args, cwd, devices, wda):
    from . import expo
    pid = expo_dev.stop(expo.project_root(cwd))
    say(f"Stopped Metro (pid {pid})." if pid else "No Metro that Mobster started is running here.")
    return 0
