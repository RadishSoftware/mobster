"""`mobster signing`: put Mobster's helper on an iPhone without Xcode, for a paid Apple Developer team.

    mobster signing status
    mobster signing card                      # reads your team and prints exactly what Mobster would send to Apple
    mobster signing apply --approve DIGEST    # sends it, signs the helper here and installs it
    mobster signing resolve DIGEST --made     # after a change whose answer Mobster never saw
    mobster signing export helper.tgz         # the signed helper, for Macs without a key or Xcode
    mobster signing import helper.tgz --team ABCDE12345

Off unless ``MOBSTER_EXPERIMENTAL_NO_XCODE=1``. Exit codes: 0 done, 3 couldn't (usage too).
"""

import json
import sys

from ..devkit import main, say, subcommands


def add_arguments(parser, helpers):
    add = subcommands(parser, "signing_command")

    def phone_options(sub):
        sub.add_argument(
            "--phone-udid",
            metavar="UDID",
            help="the iPhone to register and sign for (default: the iPhone chosen in Setup)",
        )
        sub.add_argument("--phone-name", metavar="NAME", help="with --phone-udid: its name on your team")
        sub.add_argument("--bundle-id", metavar="ID", help="sign for this bundle id instead of the helper's (a test)")
        sub.add_argument(
            "--team", metavar="ID", help="your team's 10-character id (default: read from App Store Connect)"
        )
        sub.add_argument("--json", action="store_true", help="print JSON")

    status = add("status", help="say whether signing without Xcode is on, what it has and what it waits for")
    status.add_argument("--json", action="store_true", help="print JSON")

    card = add(
        "card",
        help="read your team and print the requests Mobster would send to Apple, with a digest",
        description="Reads App Store Connect with your key and prints each request Mobster would send: register "
        "your iPhone, create a development certificate from a request made on this Mac, create a "
        "development profile. Sends nothing. Approve it with `mobster signing apply --approve DIGEST`.",
    )
    phone_options(card)

    apply = add(
        "apply",
        help="send the card you approved, sign the helper on this Mac and install it",
        description="Reads your team again and sends the card only if its digest is the one you approved. Each "
        "request is recorded before it is sent. A request whose answer never arrives is not sent "
        "again.",
    )
    apply.add_argument("--approve", required=True, metavar="DIGEST", help="the digest `mobster signing card` printed")
    apply.add_argument(
        "--writes-only",
        action="store_true",
        help="make the changes at Apple and stop: sign and install nothing (a test with no iPhone)",
    )
    phone_options(apply)

    resolve = add("resolve", help="say whether a change whose answer Mobster never saw was made")
    resolve.add_argument("digest", metavar="DIGEST", help="the change, from `mobster signing status`")
    which = resolve.add_mutually_exclusive_group(required=True)
    which.add_argument("--made", action="store_true", help="you found it at developer.apple.com")
    which.add_argument("--not-made", action="store_true", help="it isn't there; Mobster may send it once more")

    export = add("export", help="write the signed helper and its profile to a file other Macs can install")
    export.add_argument("file", metavar="FILE", help="where to write it (a .tgz)")
    export.add_argument("--phone-udid", metavar="UDID", help="the iPhone it was signed for (default: the one chosen)")

    imp = add(
        "import",
        help="install a helper that another Mac signed",
        description="Opens the file only if it is from the team you name, still has a day of profile left, lists "
        "this iPhone and is intact. The team is your check: any team's signature verifies.",
    )
    imp.add_argument("file", metavar="FILE", help="the .tgz `mobster signing export` wrote")
    imp.add_argument("--team", required=True, metavar="ID", help="the team id you expect the file to be from")
    imp.add_argument("--phone-udid", metavar="UDID", help="the iPhone to install on (default: the one chosen)")
    imp.add_argument("--phone-name", metavar="NAME", help="with --phone-udid: its name")


def run(args, *, service=None):
    handlers = {
        "status": _status,
        "card": _card,
        "apply": _apply,
        "resolve": _resolve,
        "export": _export,
        "import": _import,
    }
    return main("signing", handlers, args, service=service)


def _service(service):
    if service is not None:
        return service
    from .. import device_manager, paths
    from .service import TeamSigning

    return TeamSigning(device_manager.DeviceManager(paths.user_data_dir()))


def _phones(args):
    udid = getattr(args, "phone_udid", None)
    if not udid:
        return None
    return [{"udid": udid, "name": getattr(args, "phone_name", None) or "iPhone"}]


def _status(args, *, service, **_):
    from . import enabled

    if not enabled():
        say("Without Xcode is off. Turn it on with MOBSTER_EXPERIMENTAL_NO_XCODE=1.")
        return 0
    info = _service(service).status()
    if getattr(args, "json", False):
        say(json.dumps(info, ensure_ascii=False, default=str))
        return 0
    key = info["key"]
    lines = [
        "Key       "
        + (
            f"{key['keyId']} ({key['kind']})"
            if key
            else "none: mobster asc key add --key-id KEY_ID --issuer-id ISSUER_ID --p8 FILE"
        ),
        f"Driver    {info['driver'] or 'none: pipx install pymobiledevice3'}",
        f"Stage     {info['stage']}",
    ]
    for open_write in info["unresolved"]:
        lines.append(
            f"Waiting   {open_write['write'].replace('_', ' ')} was sent and never answered "
            f"({open_write['digest'][:12]}): mobster signing resolve {open_write['digest']} --made | --not-made"
        )
    say("\n".join(lines))
    return 0


def render(card):
    lines = [
        f"App ID    {card['bundle']['identifier']}"
        + ("  (your team's wildcard)" if card["bundle"]["identifier"] == "*" else "")
    ]
    if not card["steps"]:
        lines.append("Mobster sends nothing to Apple: your team already has everything the helper needs.")
    for step in card["steps"]:
        lines += ["", f"{step['number']}. {step['title']}", f"   {step['method']} {step['url']}"]
        lines += [f"   {item['label']:<10}{item['value']}" for item in step["lines"]]
    lines += ["", f"Digest    {card['digest']}"]
    return "\n".join(lines)


def _card(args, *, service, **_):
    card = _service(service).card(_phones(args), args.bundle_id, args.team)
    say(
        json.dumps(card, ensure_ascii=False)
        if args.json
        else render(card) + f"\n\nApprove: mobster signing apply --approve {card['digest']}"
    )
    return 0


def _apply(args, *, service, **_):
    info = _service(service).apply(args.approve, _phones(args), args.bundle_id, args.team, args.writes_only)
    if info["stage"] == "failed":
        say(f"mobster signing: {info['error']} {info['fix'] or ''}".rstrip(), sys.stderr)
        return 3
    say(
        json.dumps(info["result"], ensure_ascii=False, default=str)
        if args.json
        else "Done: "
        + (
            ", ".join(f"{name} {ident}" for name, ident in (info["result"].get("writes") or {}).items())
            if args.writes_only
            else f"the helper {info['result']['runner']} is on the iPhone."
        )
    )
    return 0


def _resolve(args, *, service, **_):
    _service(service).resolve(args.digest, bool(args.made))
    say("Recorded." + ("" if args.made else " Mobster may send it once more."))
    return 0


def _export(args, *, service, **_):
    manifest = _service(service).export_bundle(args.file, args.phone_udid)
    say(
        f"Wrote {args.file}: team {manifest['team']}, {len(manifest['udids'])} iPhone(s), profile ends "
        f"{_date(manifest['expires'])}."
    )
    return 0


def _import(args, *, service, **_):
    manifest = _service(service).import_bundle(args.file, args.team, _phones(args))
    say(f"Installed {manifest['bundle_id']} (team {manifest['team']}, profile ends {_date(manifest['expires'])}).")
    return 0


def _date(epoch):
    import datetime

    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%-d %B %Y")
