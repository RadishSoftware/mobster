"""`mobster asc`: App Store Connect with your own API key, read-only, and App Store screenshots from your checks."""

import json
import sys
from pathlib import Path

from . import DevError, main, say, subcommands


def add_arguments(parser, helpers):
    add = subcommands(parser, "asc_command")

    key = add("key", help="add, show or remove your App Store Connect API key (kept in the Keychain)",
              description="Keep your App Store Connect API key in your login Keychain, readable only by Mobster's "
                          "own programs. Make one in App Store Connect › Users and Access › Integrations. A CI job "
                          "can pass MOBSTER_ASC_KEY_ID, MOBSTER_ASC_ISSUER_ID and MOBSTER_ASC_KEY_PATH instead.")
    key.add_argument("action", choices=("add", "show", "remove"), help="add, show or remove")
    key.add_argument("--key-id", metavar="ID", help="with add: the key's Key ID")
    key.add_argument("--issuer-id", metavar="UUID", help="with add: the Issuer ID (a team key; none for an "
                                                        "individual key)")
    key.add_argument("--p8", metavar="FILE", help="with add: the AuthKey_<Key ID>.p8 file Apple gave you")

    apps = add("apps", help="list the apps your key can see")
    apps.add_argument("--json", action="store_true", help="print JSON")

    builds = add("builds", help="list an app's builds and their TestFlight state, newest first")
    builds.add_argument("--app", required=True, metavar="BUNDLE", help="the app's bundle ID")
    builds.add_argument("--limit", type=int, default=10, metavar="N", help="how many (default: 10)")
    builds.add_argument("--json", action="store_true", help="print JSON")

    feedback = add("feedback", help="read your testers' TestFlight feedback and crashes",
                   description="List the screenshot feedback and crash reports your testers sent from TestFlight, "
                               "newest first. --save downloads each crash log and screenshot.")
    feedback.add_argument("--app", required=True, metavar="BUNDLE", help="the app's bundle ID")
    feedback.add_argument("--crashes", action="store_true", help="only crashes")
    feedback.add_argument("--screenshots", action="store_true", help="only screenshot feedback")
    feedback.add_argument("--limit", type=int, default=20, metavar="N", help="how many (default: 20)")
    feedback.add_argument("--save", metavar="DIR", help="download the crash logs and screenshots here")
    feedback.add_argument("--json", action="store_true", help="print JSON")

    shots = add("screenshots", help="make App Store screenshots from your checks, in every size and language",
                description="Run each check on a simulator of each App Store display size, in each language, with "
                            "the status bar at 9:41, and save its last screen as a full-resolution PNG in the "
                            "folders fastlane deliver reads. Uploads nothing.",
                epilog="examples:\n  mobster asc screenshots\n"
                       "  mobster asc screenshots .mobster/checks/store --size 6.9 --size 13 --locale en-US "
                       "--locale de-DE")
    shots.add_argument("paths", nargs="*", metavar="PATH",
                       help="check files or folders (default: the checks tagged screenshot, else every check)")
    shots.add_argument("--size", action="append", choices=("6.9", "6.5", "6.3", "13"),
                       help="a display size; repeatable (default: 6.9, and 13 for an iPad app)")
    shots.add_argument("--locale", action="append", metavar="CODE",
                       help="an App Store locale such as de-DE; repeatable (default: every language the app has)")
    shots.add_argument("-o", "--output", default="fastlane/screenshots", metavar="DIR",
                       help="where the PNGs go (default: fastlane/screenshots)")
    shots.add_argument("--json", action="store_true", help="print screenshots.json")
    helpers["env_option"](shots)


def run(args, *, client=None, store=None, devices=None, wda=None, cwd=None):
    handlers = {"key": _key, "apps": _apps, "builds": _builds, "feedback": _feedback,
                "screenshots": _screenshots}
    return main("asc", handlers, args, client=client, store=store, devices=devices, wda=wda,
                cwd=Path(cwd or Path.cwd()))


def _client(client, store):
    if client is not None:
        return client
    from . import asc
    return asc.Client(asc.load_key(store=store))


def _key(args, *, store, **_):
    from . import asc
    if args.action == "add":
        if not args.key_id or not args.p8:
            raise DevError("key add needs --key-id and --p8 (and --issuer-id for a team key).")
        try:
            text = Path(args.p8).expanduser().read_text()
        except OSError:
            raise DevError(f"Mobster can't read {args.p8}.", "Pass the path of the .p8 file.") from None
        key = asc.save_key(args.key_id, args.issuer_id, text, store=store)
        say(f"Saved the {key.public()['kind']} key {key.key_id} in your Keychain. Mobster only reads from App Store "
             f"Connect with it. You can delete {Path(args.p8).name} now, or keep it somewhere safe.")
        return 0
    if args.action == "remove":
        asc.remove_key(store=store)
        say("Removed the App Store Connect key from your Keychain.")
        return 0
    key = asc.load_key(store=store)
    info = key.public()
    say(f"Key ID     {info['keyId']}\nIssuer ID  {info['issuerId'] or 'none (an individual key)'}\n"
         f"From       {info['source']}")
    return 0


def _apps(args, *, client, store, **_):
    apps = _client(client, store).apps()
    if args.json:
        say(json.dumps(apps, ensure_ascii=False))
    else:
        say("\n".join(f"{item['bundleId']:<40} {item['name']}  (Apple ID {item['id']})" for item in apps)
             or "This key sees no apps.")
    return 0


def _builds(args, *, client, store, **_):
    asc = _client(client, store)
    app = asc.app(args.app)
    rows = asc.builds(app["id"], limit=max(1, min(args.limit, 200)))
    if args.json:
        say(json.dumps({"app": app, "builds": rows}, ensure_ascii=False))
        return 0
    lines = [f"{app['name']} ({args.app}), newest first:"]
    for row in rows:
        state = "expired" if row["expired"] else (row["external"] or row["internal"] or row["processing"] or "")
        lines.append(f"  {row['version'] or '?'} ({row['build']})  {str(row['uploaded'] or '')[:16].replace('T', ' ')}"
                     f"  {state.replace('_', ' ').lower()}")
    if not rows:
        lines.append("  no builds yet")
    say("\n".join(lines))
    return 0


def _feedback(args, *, client, store, cwd, **_):
    asc = _client(client, store)
    app = asc.app(args.app)
    kinds = ("crashes",) if args.crashes and not args.screenshots else ("screenshots",) if args.screenshots and \
        not args.crashes else ("screenshots", "crashes")
    rows = asc.feedback(app["id"], kinds=kinds, limit=max(1, min(args.limit, 200)))
    saved = 0
    if args.save:
        folder = (cwd / args.save).resolve() if not Path(args.save).is_absolute() else Path(args.save)
        folder.mkdir(parents=True, exist_ok=True)
        for row in rows:
            if row["kind"] == "crash":
                log = asc.crash_log(row["id"])
                if log:
                    (folder / f"{row['id']}.crash").write_text(log, encoding="utf-8")
                    row["saved"] = [f"{row['id']}.crash"]
                    saved += 1
            for number, url in enumerate(row["screenshots"], 1):
                try:
                    data = asc.download(url)
                except DevError:
                    continue
                name = f"{row['id']}-{number}.jpg"
                (folder / name).write_bytes(data)
                row.setdefault("saved", []).append(name)
                saved += 1
    if args.json:
        say(json.dumps({"app": app, "feedback": rows}, ensure_ascii=False))
        return 0
    lines = [f"{app['name']}: {len(rows)} TestFlight report{'' if len(rows) == 1 else 's'}, newest first"]
    for row in rows:
        what = "Crash" if row["kind"] == "crash" else "Feedback"
        lines.append(f"  {what:<8} {str(row['created'] or '')[:16].replace('T', ' ')}  build {row['build'] or '?'}"
                     f"  {row['device'] or ''} iOS {row['os'] or '?'}")
        if row.get("comment"):
            lines.append(f"           “{' '.join(str(row['comment']).split())[:200]}”")
    if args.save:
        lines.append(f"Saved {saved} file{'' if saved == 1 else 's'} in {args.save}")
    say("\n".join(lines))
    return 0


def _screenshots(args, *, cwd, **_):
    from ..devtools import ProgressLines
    from ..testkit.discover import DiscoveryError, discover
    from ..verify.checks import project_root
    from . import shots
    root = project_root(cwd)
    try:
        entries = discover(list(args.paths or ()), root, cwd=cwd)
    except DiscoveryError as error:
        raise DevError(str(error)) from None
    if not args.paths:
        tagged = [entry for entry in entries if "screenshot" in {tag.casefold() for tag in entry.tags}]
        entries = tagged or entries
    checks = [entry.check for entry in entries if entry.check is not None]
    if not checks:
        raise DevError("No checks to take screenshots from.",
                       "Save one per screen in .mobster/checks (tag them screenshot), or pass check files.")
    app_path = next((check.app_path for check in checks if check.app_path), None)
    sizes = tuple(args.size or (("6.9", "13") if app_path and shots.is_ipad_app(app_path) else ("6.9",)))
    locales = tuple(shots.asc_locale(item) for item in (args.locale or shots.app_locales(app_path)))
    planned, notes = shots.plan_shots(checks, sizes=sizes, locales=locales,
                                      available=shots.available_device_types())
    progress = ProgressLines(sys.stderr)
    for note in notes:
        progress(note)
    if not planned:
        raise DevError("This Mac has no simulator for those sizes.", notes[0] if notes else "")
    progress(f"{len(planned)} screenshot{'' if len(planned) == 1 else 's'}: {len(checks)} screen"
             f"{'' if len(checks) == 1 else 's'} × {', '.join(s + chr(34) for s in sizes)} × {', '.join(locales)}")
    out = (cwd / args.output) if not Path(args.output).is_absolute() else Path(args.output)
    try:
        doc = shots.take(planned, out=out, runs_dir=root / ".mobster" / "runs", progress=progress)
    finally:
        progress.end()
    if args.json:
        say(json.dumps(doc, ensure_ascii=False))
    else:
        lines = []
        for row in doc["shots"]:
            mark = "✓" if row["file"] and row["fits"] else "✗"
            where = row["file"] or "no shot"
            pixels = "×".join(map(str, row["pixels"])) if row["pixels"] else ""
            lines.append(f"{mark} {where}  {pixels}  ({row['verdict'].replace('_', ' ')})")
        lines += ["", f"Screenshots in {args.output}, listed in screenshots.json. Review them, then upload with "
                      "`fastlane deliver` or in App Store Connect. Mobster uploads nothing."]
        say("\n".join(lines))
    return 0 if doc["ok"] else 1
