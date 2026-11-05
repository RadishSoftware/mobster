"""`mobster api`: serve every app on your iPhone as a local, read-only API, and make the tokens clients call it with
(devtools.py creates the parser with fixed help; this module adds the subcommands)."""

import json
import sys
import threading

from . import DEFAULT_PORT

DONE, FAILED, USAGE, MISSING_EXIT = 0, 1, 2, 3


def add_arguments(parser, helpers):
    bounded = helpers.get("bounded")
    subs = parser.add_subparsers(dest="api_command", metavar="<subcommand>")
    subs.required = True
    serve = subs.add_parser(
        "serve", help="serve the App API on 127.0.0.1 until ctrl+c",
        description="Serve every app Mobster has an app pack for as GET /v1/<app>/<thing> on 127.0.0.1. Each "
                    "request needs a token from `mobster api tokens create`. Every endpoint only reads.",
        epilog="examples:\n  mobster api serve --phone \"Test iPhone\" --allow-device \"Test iPhone\"\n"
               "  curl -H \"Authorization: Bearer $TOKEN\" http://127.0.0.1:8780/v1/apps")
    serve.add_argument("--port", type=bounded(int, 0, 65535) if bounded else int, default=DEFAULT_PORT,
                       help=f"port on 127.0.0.1 (default: {DEFAULT_PORT}; 0 picks a free one)")
    serve.add_argument("--phone", metavar="NAME",
                       help="the iPhone a read uses when it names none: an id, UDID or name from `mobster devices`")
    serve.add_argument("--allow-device", action="append", metavar="NAME", dest="allow_devices",
                       help="read only this real iPhone, never another; repeatable")
    tokens = subs.add_parser("tokens", help="make, list and revoke the tokens clients call the App API with",
                             description="Make one token for each client you let call the App API, list them, and "
                                         "revoke one. Mobster keeps each token's hash, never the token.")
    actions = tokens.add_subparsers(dest="tokens_command", metavar="<action>")
    actions.required = True
    create = actions.add_parser("create", help="make a token for one client and print it once",
                                description="Make a token for one client, such as a script or n8n, and print it "
                                            "once. Mobster can't show it again.")
    create.add_argument("name", metavar="NAME", help="the client's name, 1 to 40 characters")
    listing = actions.add_parser("list", help="list the tokens: name, id, first characters, last use",
                                 description="List the tokens, never the tokens themselves.")
    listing.add_argument("--json", action="store_true", help="print JSON")
    revoke = actions.add_parser("revoke", help="revoke a token, by its id or name",
                                description="Revoke a token: the next request with it is refused.")
    revoke.add_argument("which", metavar="ID|NAME", help="the token's id or name from `mobster api tokens list`")


def _store():
    from ..paths import dev_data_dir
    from .tokens import TokenStore
    return TokenStore(dev_data_dir())


def run(args):
    from .tokens import TokenError
    try:
        if args.api_command == "tokens":
            return _tokens(args)
        return serve(args)
    except TokenError as error:
        print(str(error), file=sys.stderr)
        return MISSING_EXIT if "can't trust" in str(error) else FAILED
    except KeyboardInterrupt:
        return 130


def _when(seconds):
    import time
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(seconds)) if seconds else "never"


def _tokens(args):
    store = _store()
    if args.tokens_command == "create":
        record, token = store.create(args.name)
        print(token)
        print(f"Made the token “{record['name']}” ({record['id']}). It shows only now: put it in your client. "
              "Revoke it with mobster api tokens revoke " + record["id"] + ".", file=sys.stderr)
        return DONE
    if args.tokens_command == "revoke":
        record = store.revoke(args.which)
        print(f"Revoked “{record['name']}” ({record['id']}). Requests with it are refused now.")
        return DONE
    tokens = store.all()
    if args.json:
        print(json.dumps({"tokens": tokens}, ensure_ascii=False))
        return DONE
    if not tokens:
        print("No tokens yet. Make one with mobster api tokens create NAME.")
        return DONE
    for t in tokens:
        print(f"{t['id']}  {t['start']}…  {t['name']}  made {_when(t['createdAt'])}, last "
              f"used {_when(t['lastUsedAt'])}")
    return DONE


def installed_versions(toolset):
    """``installed(phone)`` for GET /v1/apps: {bundle id: version} of the user apps on that iPhone (ideviceinstaller,
    cached a minute), or None for a simulator or a phone it can't list."""
    from ..installed_apps import InstalledApps
    cache, lock = {}, threading.Lock()

    def installed(phone):
        record = toolset._named_device(phone or toolset.device)
        if record is None or record.get("kind") == "simulator" or not record.get("udid"):
            return None
        udid = record["udid"]
        with lock:
            apps = cache.setdefault(udid, InstalledApps(lambda: {"udid": udid, "trusted": True}))
        inventory = apps.snapshot()
        return {app["bundleId"]: app.get("version") for app in inventory.user} if inventory else None
    return installed


def serve(args):
    from .. import __version__
    from ..mcp_server.tools import ToolSet
    from ..packs.register import all_packs
    from ..paths import dev_data_dir, user_data_dir
    from .reading import service_profiles
    from .service import AppAPI, server
    from .sessions import Sessions
    from .tokens import TokenStore
    data_dir = dev_data_dir()
    store = TokenStore(data_dir)
    store.all()  # a token file Mobster can't trust stops it here, with the fix
    packs = all_packs()
    toolset = ToolSet(runs_dir=data_dir / "runs", keyless=True, device=args.phone, version=__version__,
                      allow_devices=args.allow_devices, device_runs_dir=data_dir / "runs",
                      log=lambda line: print(f"mobster api {line}", file=sys.stderr))
    toolset.client = {"name": "the App API"}   # how the Mac app's permission card names the caller
    sessions = Sessions(toolset, data_dir / "sessions")
    api = AppAPI(packs, toolset, store, port=args.port, version=__version__, health_dir=user_data_dir() / "state",
                 profiles=service_profiles("cli"), installed=installed_versions(toolset), sessions=sessions)
    try:
        httpd = server(api, args.port)
    except OSError as error:
        print(f"Port {args.port} on 127.0.0.1 is taken ({error.strerror}). Pass another with --port.", file=sys.stderr)
        return MISSING_EXIT
    toolset.start()
    names = ", ".join(pack.name for pack in packs) or "none yet (this build has no app packs)"
    print(f"Mobster App API on http://127.0.0.1:{api.port}/v1. It only reads. Apps: {names}.", file=sys.stderr)
    if not store.all():
        print("No tokens yet: make one with mobster api tokens create NAME.", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        sessions.close()
        toolset.shutdown("the App API stopped")
    return DONE
