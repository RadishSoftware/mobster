"""`mobster testflight`: your app's TestFlight builds, listed and installed on an iPhone."""

import json
import sys
from pathlib import Path

from . import DevError, main, say, subcommands


def add_arguments(parser, helpers):
    add = subcommands(parser, "testflight_command")

    key = add("key", help="add, show or remove this project's App Store Connect API key (kept in the Keychain)",
              description="Keep an App Store Connect API key for this project in your login Keychain, readable only "
                          "by Mobster's own programs, and never printed. Make one in App Store Connect › Users and "
                          "Access › Integrations. The key is this project's: a folder with .mobster/ above the "
                          "current one, or the current folder. --shared keeps it for every project instead.")
    key.add_argument("action", choices=("add", "show", "remove"), help="add, show or remove")
    key.add_argument("--key-id", metavar="ID", help="with add: the key's Key ID")
    key.add_argument("--issuer-id", metavar="UUID", help="with add: the Issuer ID (a team key; none for an "
                                                        "individual key)")
    key.add_argument("--p8", metavar="FILE", help="with add: the AuthKey_<Key ID>.p8 file Apple gave you")
    key.add_argument("--shared", action="store_true", help="the key every project uses, not this project's")

    listing = add("list", help="list the app's builds and whether TestFlight can install each, newest first")
    listing.add_argument("--app", required=True, metavar="BUNDLE", help="the app's bundle ID")
    listing.add_argument("--limit", type=int, default=10, metavar="N", help="how many (default: 10)")
    listing.add_argument("--json", action="store_true", help="print JSON")

    install = add("install", help="install a TestFlight build on your iPhone through the TestFlight app",
                  description="Install the newest installable build, or the one --build names, on your iPhone: "
                              "Mobster opens the app's page in TestFlight and taps Install or Update, then waits "
                              "for Open. You must be a tester of the app. Mobster never accepts TestFlight's terms "
                              "for you, never picks from Previous Builds (it prints the taps instead), and after a "
                              "tap it can't confirm it never taps again.",
                  epilog="after it: mobster test --device NAME --testflight BUNDLE, whose report names the build")
    install.add_argument("--app", required=True, metavar="BUNDLE", help="the app's bundle ID")
    install.add_argument("--build", metavar="N", help="the build number (default: the newest installable)")
    install.add_argument("--version", metavar="V", help="the version the build belongs to, such as 1.4.2")
    install.add_argument("--device", metavar="NAME", help="the iPhone (default: your iPhone)")
    install.add_argument("--timeout", type=int, default=600, metavar="SECONDS",
                         help="how long to wait for TestFlight (default: 600)")
    install.add_argument("--json", action="store_true", help="print JSON")


def run(args, *, client=None, store=None, devices=None, wda=None, cwd=None):
    handlers = {"key": _key, "list": _list, "install": _install}
    return main("testflight", handlers, args, client=client, store=store, devices=devices, wda=wda,
                cwd=Path(cwd or Path.cwd()))


def project_of(cwd):
    from ..verify.checks import project_root
    return Path(project_root(Path(cwd))).resolve()


def _client(client, store, root):
    if client is not None:
        return client
    from . import asc
    return asc.Client(asc.load_key(store=store, project=root))


def _key(args, *, store, cwd, **_):
    from . import asc
    root = None if args.shared else project_of(cwd)
    if args.action == "add":
        if not args.key_id or not args.p8:
            raise DevError("key add needs --key-id and --p8 (and --issuer-id for a team key).")
        try:
            text = Path(args.p8).expanduser().read_text()
        except OSError:
            raise DevError(f"Mobster can't read {args.p8}.", "Pass the path of the .p8 file.") from None
        key = asc.save_key(args.key_id, args.issuer_id, text, store=store, project=root)
        say(f"Saved the {key.public()['kind']} key {key.key_id} in your Keychain for "
            f"{'every project' if root is None else root.name}. You can delete {Path(args.p8).name} now, or keep "
            "it somewhere safe.")
    elif args.action == "remove":
        asc.remove_key(store=store, project=root)
        say("Removed the key from your Keychain.")
    else:
        info = asc.load_key(store=store, project=root).public()
        say(f"Key ID     {info['keyId']}\nIssuer ID  {info['issuerId'] or 'none (an individual key)'}\n"
            f"From       {info['source']}")
    return 0


def _list(args, *, client, store, cwd, **_):
    from . import testflight
    app, rows = testflight.builds(_client(client, store, project_of(cwd)), args.app,
                                  limit=max(1, min(args.limit, 200)))
    if args.json:
        say(json.dumps({"app": app, "builds": rows}, ensure_ascii=False))
        return 0
    lines = [f"{app['name']} ({args.app}), newest first:"]
    for row in rows:
        when = str(row["uploaded"] or "")[:16].replace("T", " ")
        lines.append(f"  {row['version'] or '?'} ({row['build']})  {when}  "
                     + ("can install" if row["installable"] else f"can't install: {row['why']}"))
    if not rows:
        lines.append("  no builds yet")
    say("\n".join(lines))
    return 0


def _install(args, *, client, store, devices, wda, cwd, **_):
    from . import testflight
    if devices is None or wda is None:
        from ..phone_io import choose_device
        from ..phone_io import wda as phone_wda
        devices, wda = devices or choose_device, wda or phone_wda
    root = project_of(cwd)
    record = devices(args.device)
    done = testflight.install(_client(client, store, root), record, wda, args.app, root=root, build=args.build,
                              version=args.version, timeout=max(30, min(args.timeout, 3600)),
                              progress=lambda line: say(line, sys.stderr))
    row = done["row"]
    name = f"{row['version'] or '?'} ({row['build']})"
    if args.json:
        say(json.dumps({"device": record.get("name"), "app": args.app, "result": done["result"],
                        "build": {"version": row["version"], "build": row["build"]}}))
    elif done["result"] == "already":
        say(f"{args.app} {name} is already on {record.get('name')}: TestFlight says Open.")
    else:
        say(f"Installed {args.app} {name} from TestFlight on {record.get('name')}. Test it with: "
            f"mobster test --device \"{record.get('name')}\" --testflight {args.app}")
    return 0
