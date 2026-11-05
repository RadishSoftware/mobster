"""`mobster phone`: the phone's clipboard, files in an app's folder, Photos both ways, deep links, buttons, and
installing a build, and typing a password privately (devtools.py registers it).

``add_arguments(parser, helpers)`` adds the subcommands; ``run(args)`` runs one and returns its exit code:
0 done, 1 it failed, 2 usage, 3 no device (or no Xcode tools), 130 ctrl+c. It never raises.
"""

import json
import sys

from . import PhoneIOError, choose_device, exit_code


def add_arguments(parser, helpers):
    path_type = helpers.get("path_type")
    subs = parser.add_subparsers(dest="phone_command", metavar="<subcommand>")
    subs.required = True

    def common(sub):
        sub.add_argument("--device", metavar="NAME",
                         help="the device: an id, UDID or name from `mobster devices` (default: your iPhone)")
        sub.add_argument("--json", action="store_true", help="print one JSON object")

    clipboard = subs.add_parser("clipboard", help="set or read the phone's clipboard (text, at most 64 KB)",
                                description="Set the phone's clipboard from the Mac, or print what it holds.")
    actions = clipboard.add_subparsers(dest="clipboard_command", metavar="<set|get>")
    actions.required = True
    set_ = actions.add_parser("set", help="put text on the phone's clipboard",
                              description="Put text on the phone's clipboard: TEXT, a file's text with --file, or "
                                          "standard input with -. At most 64 KB.")
    set_.add_argument("text", nargs="?", metavar="TEXT", help="the text, or - for standard input")
    set_.add_argument("--file", type=path_type, metavar="FILE", help="put this UTF-8 file's text on the clipboard")
    common(set_)
    get = actions.add_parser("get", help="print the phone's clipboard",
                             description="Print the phone's clipboard. WebDriverAgent's runner comes to the front for "
                                         "a moment to read it (iOS lets only the front app read the clipboard). "
                                         "Only the CLI reads it: no MCP tool does, because it may hold a password.")
    common(get)

    files = subs.add_parser("file", help="move files between this Mac and an app's folder on the iPhone (USB)",
                            description="Copy files between this Mac and the Documents folder of an iPhone app that "
                                        "shares files (the folder Files shows under On My iPhone). Over the cable "
                                        "only. Nothing else on the phone is reachable, the camera roll included.",
                            epilog="exit codes: 0 done, 1 it failed, 2 usage, 3 no iPhone on a cable or no iPhone "
                                   "tools")
    verbs = files.add_subparsers(dest="file_command", metavar="<put|get|ls>")
    verbs.required = True
    put = verbs.add_parser("put", help="copy a file from this Mac into an app's folder",
                           description="Copy a file (up to 100 MB) into an app's folder on the iPhone. A file with "
                                       "the same name is kept: the copy gets a new name (menu 2.pdf) unless you pass "
                                       "--replace.")
    put.add_argument("path", type=path_type, metavar="PATH", help="the file on this Mac")
    put.add_argument("--app", required=True, metavar="BUNDLE",
                     help="the app's bundle ID, such as com.apple.Pages (`mobster phone file ls` lists them)")
    put.add_argument("--name", metavar="NAME", help="the name it gets on the iPhone (default: its own)")
    put.add_argument("--replace", action="store_true", help="overwrite a file with the same name")
    put.add_argument("--container", action="store_true",
                     help="the app's own Documents folder, for a development build of yours that doesn't share files")
    common(put)
    get_file = verbs.add_parser("get", help="copy a file from an app's folder to this Mac",
                                description="Copy a file from an app's folder on the iPhone to this Mac.")
    get_file.add_argument("name", metavar="NAME", help="the file's name in the app's folder")
    get_file.add_argument("--app", required=True, metavar="BUNDLE", help="the app's bundle ID")
    get_file.add_argument("-o", "--output", metavar="PATH",
                          help="where to save it: a folder, or a file name (default: the current folder)")
    get_file.add_argument("--container", action="store_true",
                          help="the app's own Documents folder, for a development build of yours")
    common(get_file)
    ls = verbs.add_parser("ls", help="list the apps that share files, or the files in one",
                          description="Without --app, list the iPhone's apps whose folders show in Files. With "
                                      "--app, list the files in that app's folder.")
    ls.add_argument("--app", metavar="BUNDLE", help="the app's bundle ID")
    ls.add_argument("--container", action="store_true",
                    help="the app's own Documents folder, for a development build of yours")
    common(ls)

    media = subs.add_parser("photos",
                            help="add photos and videos to the iPhone's Photos, or copy the newest to this Mac",
                            description="Add a photo or video from this Mac to the iPhone's Photos library (Mobster's "
                            "runner saves it; the first time, iOS asks on the iPhone), or copy the newest "
                            "photos, screenshots and screen recordings from its camera roll to this Mac "
                            "(over the cable).",
                            epilog="exit codes: 0 done, 1 it failed, 2 usage, "
                            "3 no iPhone on a cable or no iPhone tools")
    media_verbs = media.add_subparsers(dest="photos_command", metavar="<add|pull>")
    media_verbs.required = True
    add = media_verbs.add_parser("add", help="add photos and videos to Photos",
                                 description="Add JPEGs, HEICs, PNGs, GIFs, WebPs, MOVs or MP4s (up to 4 GB each, 50 "
                                 "at a time) to the iPhone's Photos library, into the album Mobster unless you name "
                                 "another. Each one is checked by SHA-256 on the iPhone, and the library's count is "
                                 "read before and after.")
    add.add_argument("path", type=path_type, nargs="+", metavar="PATH", help="the photos or videos on this Mac")
    add.add_argument("--album", metavar="NAME", default="Mobster",
                     help="the album they go into, made if it isn't there (default Mobster); --album '' for none")
    add.add_argument("--via", choices=["auto", "cable", "wifi", "airdrop", "mirroring", "icloud"], default="auto",
                     metavar="ROUTE", help="auto (default), cable, wifi, airdrop, mirroring or icloud")
    add.add_argument("--profile", metavar="PROFILE", help="send to this profile's iPhone (Settings › Profiles)")
    common(add)
    pull = media_verbs.add_parser("pull", help="copy the newest photos and recordings to this Mac",
                                  description="Copy the newest photos, screenshots and screen recordings from the "
                                              "iPhone, newest first, each checked by SHA-256 or size. Over the cable "
                                              "by default; --via wifi reads them through Mobster's runner, --via "
                                              "tunnel through Apple's own tunnel (needs pymobiledevice3), --via "
                                              "icloud from this Mac's Photos library.")
    pull.add_argument("-n", "--count", type=int, default=1, metavar="N", help="how many, 1 to 20 (default 1)")
    pull.add_argument("--since", metavar="WHEN", help="only newer ones: 10m, 2h, 1d or an ISO time")
    pull.add_argument("--jpeg", action="store_true", help="convert HEIC photos to JPEG (over the cable)")
    pull.add_argument("-o", "--output", metavar="DIR", help="the folder to save into (default: the current folder)")
    pull.add_argument("--via", choices=["auto", "cable", "wifi", "tunnel", "icloud"], default="auto", metavar="ROUTE",
                      help="auto (default), cable, wifi, tunnel or icloud")
    pull.add_argument("--profile", metavar="PROFILE", help="copy from this profile's iPhone (Settings › Profiles)")
    common(pull)

    open_ = subs.add_parser("open", help="open a URL or deep link on the phone",
                            description="Open a URL or deep link on the phone, such as maps://?q=coffee or "
                                        "App-prefs:WIFI. Links that act as they open (tel:, facetime:, running a "
                                        "shortcut) ask for --yes.")
    open_.add_argument("url", metavar="URL", help="the link")
    open_.add_argument("--yes", action="store_true",
                       help="open a link that asks first: a call, a message, a shortcut run, or a new app's link")
    common(open_)

    button = subs.add_parser("button", help="press a hardware button, or lock the screen",
                             description="Press Home, Volume Up or Volume Down, or lock the screen. Mobster can't "
                                         "unlock an iPhone: after lock, unlock it yourself.")
    button.add_argument("name", choices=["home", "volume_up", "volume_down", "lock"], metavar="BUTTON",
                        help="home, volume_up, volume_down or lock")
    common(button)

    install = subs.add_parser("install", help="install a signed .app or .ipa on the device",
                              description="Install a build on a USB iPhone (xcrun devicectl) or a Mobster simulator "
                                          "(xcrun simctl). The build must be signed for that iPhone, and the iPhone "
                                          "needs Developer Mode on.",
                              epilog="exit codes: 0 installed, 1 the install failed, 2 usage, 3 no device or no Xcode")
    install.add_argument("path", metavar="PATH", help="the .app folder or .ipa file")
    common(install)

    record = subs.add_parser("record", help="record a USB iPhone's screen to a movie on this Mac",
                             description="Record a paired iPhone's screen, and the audio it offers, over USB into a "
                                         "new .mov file, without a WebDriverAgent session. It uses the capture "
                                         "device QuickTime Player records from, so macOS asks once whether Mobster "
                                         "may use the camera and microphone. iOS shows its demonstration status "
                                         "bar while it records, and protected content may not be recorded. It "
                                         "never overwrites a file.",
                             epilog="exit codes: 0 saved, 1 it failed, 2 usage, 3 no iPhone on a cable or no "
                                    "native helper")
    record.add_argument("--output", required=True, type=path_type, metavar="FILE", help="write a new .mov file")
    record.add_argument("--seconds", type=float, default=10, metavar="N",
                        help="how long to record, 1 to 300 (default 10)")
    common(record)

    from . import network_cli
    network_cli.add_arguments(subs, helpers)
    from . import secrets
    secrets.add_arguments(subs, helpers)


def run(args):
    as_json = bool(getattr(args, "json", False))
    try:
        command = args.phone_command
        if command == "password":
            from . import secrets
            return secrets.run(args)
        if command == "network":
            from . import network_cli
            return network_cli.run(args)
        if command == "clipboard" and args.clipboard_command == "set":
            return _clipboard_set(args, as_json)
        if command == "clipboard":
            return _clipboard_get(args, as_json)
        if command == "file":
            return _file(args, as_json)
        if command == "photos":
            return _photos(args, as_json)
        if command == "open":
            return _open(args, as_json)
        if command == "button":
            return _button(args, as_json)
        if command == "record":
            return _record(args, as_json)
        return _install(args, as_json)
    except PhoneIOError as error:
        return _fail(error, as_json)
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        return _fail(PhoneIOError(f"Mobster hit an unexpected error ({type(error).__name__}).", "failed"), as_json)


def _print(value, as_json, line):
    try:
        print(json.dumps(value, ensure_ascii=False) if as_json else line, flush=True)
    except BrokenPipeError:
        pass


def _fail(error, as_json):
    if as_json:
        _print({"ok": False, "error": error.public()}, True, "")
    else:
        print(f"mobster phone: {error}" + (f" {error.fix}" if error.fix else ""), file=sys.stderr, flush=True)
    return exit_code(error)


def _clipboard_set(args, as_json):
    from . import wda
    from .clipboard import read_text_file, set_text
    if args.file and args.text:
        raise PhoneIOError("Pass TEXT or --file, not both.", "usage")
    if args.file:
        text = read_text_file(args.file)
    elif args.text == "-":
        text = sys.stdin.read()
    elif args.text is not None:
        text = args.text
    else:
        raise PhoneIOError("Pass the text, --file FILE, or - to read standard input.", "usage")
    record = choose_device(args.device)
    with wda(record) as request:
        count = set_text(request, text)
    _print({"ok": True, "device": record["id"], "characters": count}, as_json,
           f"Put {count:,} character{'s' if count != 1 else ''} on {record['name']}'s clipboard.")
    return 0


def _clipboard_get(args, as_json):
    from . import wda
    from .clipboard import get_text
    record = choose_device(args.device)
    with wda(record) as request:
        text = get_text(request)
    if as_json:
        _print({"ok": True, "device": record["id"], "text": text}, True, "")
    else:
        try:
            sys.stdout.write(text + ("" if text.endswith("\n") or not text else "\n"))
            sys.stdout.flush()
        except BrokenPipeError:
            pass
    return 0


def _install(args, as_json):
    from ..journal import Lease, LeaseHeld
    from .install import install
    record = choose_device(args.device)
    # A run holds the phone's lease for as long as it drives the phone; an install in the middle of one would
    # replace the app under it. A phone with no WebDriverAgent address cannot be in a run.
    try:
        lease = Lease.device(record["wdaUrl"]) if record.get("wdaUrl") else None
    except LeaseHeld:
        raise PhoneIOError(f"Another Mobster process is using {record['name']}.", "device_busy",
                           "Stop its run, then install the app.") from None

    def progress(line):
        print(line, file=sys.stderr, flush=True)
    try:
        result = install(record, args.path, progress=progress)
    finally:
        if lease is not None:
            lease.close()
    _print(result, as_json, f"Installed {result.get('bundle_id') or args.path} on {record['name']} "
                            f"in {result['seconds']:g} s.")
    return 0


def _record(args, as_json):
    """`mobster phone record`: the person's own command, naming the file, is the go-ahead."""
    from .recording import record_video
    if not 1 <= args.seconds <= 300:
        raise PhoneIOError("--seconds is 1 to 300.", "usage")
    record = choose_device(args.device)
    if record.get("kind") != "usb" or not record.get("udid"):
        raise PhoneIOError("Screen recording needs a paired iPhone connected by USB.", "needs_cable")
    result = record_video(record["udid"], seconds=args.seconds, output=args.output)
    _print(result, as_json, f"Saved {result['durationSeconds']:g} seconds of video to {result['path']}.")
    return 0


def _file(args, as_json):
    """`mobster phone file put|get|ls`: the user's own command is the go-ahead for a copy it names."""
    from pathlib import Path

    from .files import PhoneFiles, land
    from .install import describe_path
    record = choose_device(args.device)
    phone = PhoneFiles(record, container=bool(getattr(args, "container", False)))
    verb = args.file_command
    if verb == "ls":
        if not args.app:
            apps = phone.apps()
            lines = [f"{app['name']}  {app['bundleId']}" for app in apps] or [
                f"No app on {phone.name} keeps files you can see in Files. Pages, Numbers and Keynote do."]
            _print({"ok": True, "device": record["id"], "apps": apps}, as_json, "\n".join(lines))
            return 0
        entries = phone.ls(args.app)
        from ..attachments.sniff import size_words
        lines = [f"{e['name']}{'/' if e['folder'] else ''}  {'' if e['folder'] else size_words(e['bytes'])}".rstrip()
                 for e in entries] or [f"{args.app}'s folder is empty."]
        _print({"ok": True, "device": record["id"], "app": args.app, "files": entries}, as_json, "\n".join(lines))
        return 0
    if verb == "put":
        result = phone.put(args.app, args.path, name=args.name, replace=args.replace)
        _print({"ok": True, "device": record["id"], **result}, as_json,
               f"Put {result['name']} in {args.app}'s folder on {phone.name}.")
        return 0
    output = Path(args.output).expanduser() if args.output else Path.cwd()
    if args.output and not output.is_dir() and (output.suffix or not str(args.output).endswith("/")):
        folder, rename = output.parent, output.name
    else:
        folder, rename = output, None
    saved = phone.get(args.app, args.name, folder)
    if rename and saved.name != rename:
        target = folder / rename
        try:
            saved = land(saved, folder, rename, exact=True)
        except FileExistsError:
            saved.unlink(missing_ok=True)
            raise PhoneIOError(f"{describe_path(target)} already exists.", "usage",
                               "Choose another name with -o.") from None
    _print({"ok": True, "device": record["id"], "path": str(saved), "bytes": saved.stat().st_size}, as_json,
           f"Saved {args.name} to {describe_path(saved)}.")
    return 0


def _photos(args, as_json):
    """`mobster phone photos add|pull`: the user's own command is the go-ahead. Both go through transfer.py."""
    from pathlib import Path

    from .install import describe_path
    from .photos import parse_since
    from .transfer import Transfer, summary, target
    from .transfer import run as run_transfer
    if getattr(args, "profile", None):
        phone = target(profile=args.profile, device=args.device)
    else:
        phone = target(device=args.device)
    if args.photos_command == "add":
        transfer = Transfer("to_phone", "photos", phone, via=args.via, paths=[str(p) for p in args.path],
                            album=args.album or None)
        progress = _Progress(as_json)
        transfer.listeners.append(progress)
        run_transfer(transfer)
        _print({"ok": transfer.state == "done", "device": phone.id, **transfer.public()}, as_json, summary(transfer))
        return 0 if transfer.state == "done" else 1
    if not 1 <= int(args.count) <= 20:
        raise PhoneIOError("--count is 1 to 20.", "usage")
    try:
        since = parse_since(args.since)
    except ValueError as error:
        raise PhoneIOError(str(error), "usage") from None
    folder = Path(args.output).expanduser() if args.output else Path.cwd()
    transfer = Transfer("to_mac", "photos", phone, via=args.via, folder=str(folder), count=int(args.count),
                        since=since, convert_heic=bool(args.jpeg))
    run_transfer(transfer)
    if transfer.state != "done" and not transfer.items:
        error = transfer.error or {}
        raise PhoneIOError(error.get("message") or "Nothing moved.", error.get("code") or "failed",
                           error.get("fix") or "")
    lines = [f"Saved {describe_path(Path(item.path))}" for item in transfer.items if item.path] or [
        f"Nothing in {phone.name}'s camera roll" + (" since then." if since else ".")]
    _print({"ok": transfer.state == "done", "device": phone.id, **transfer.public(),
            "files": [i.public() for i in transfer.items if i.path]}, as_json, "\n".join(lines))
    return 0 if transfer.state == "done" else 1


class _Progress:
    """Each item's progress on standard error, one line per quarter, so a long video shows it's moving."""

    def __init__(self, quiet):
        self.quiet, self.shown = quiet, {}

    def __call__(self, transfer):
        if self.quiet:
            return
        for item in transfer.items:
            if not item.bytes:
                continue
            quarter = int(4 * item.sent / item.bytes) if item.state == "sending" else None
            if quarter is not None and quarter > self.shown.get(item.name, 0) and quarter < 4:
                self.shown[item.name] = quarter
                print(f"{item.name}: {quarter * 25}%", file=sys.stderr, flush=True)


def _open(args, as_json):
    from ..deeplinks import LinkError, app_for, decide, open_link
    from ..permissions.exceptions import Exceptions, default_path
    from . import Calls, wda
    try:
        decision = decide(args.url, Exceptions(default_path()).contains)
    except LinkError as error:
        raise PhoneIOError(str(error), "usage") from None
    # deeplinks.decide's rule: --yes is your OK for a link that asks.
    if decision.action == "ask" and not args.yes:
        if decision.always:
            raise PhoneIOError(f"{args.url} acts as soon as it opens (a call, FaceTime, a message, an email or a "
                               "shortcut).", "usage", "Pass --yes to open it.")
        raise PhoneIOError(f"{args.url} opens {decision.app_name}, an app Mobster asks about before its first link.",
                           "usage", "Pass --yes to open it.")
    record = choose_device(args.device)
    with wda(record) as request:
        driver = Calls(request)
        opened = open_link(driver, args.url, bundle=app_for(args.url) or None)
    said = f"Opened {args.url} on {record['name']}."
    if opened.get("requested"):   # deeplinks.APP_FORMS: the web address opens the home feed, so its app link opened
        said = (f"Opened {opened['url']} on {record['name']}: the app link for {opened['requested']}, which opens "
                "the home feed.")
    _print({"ok": True, "device": record["id"], **opened}, as_json, said)
    return 0


def _button(args, as_json):
    from . import wda
    from .controls import LOCKED_AFTER, lock, press
    record = choose_device(args.device)
    with wda(record) as request:
        if args.name == "lock":
            lock(request)
        else:
            press(request, args.name, simulator=record.get("kind") == "simulator")
    words = {"lock": "Locked " + record["name"] + "."
             + ("" if record.get("kind") == "simulator" else " " + LOCKED_AFTER),
             "home": "Pressed Home.", "volume_up": "Pressed Volume Up.", "volume_down": "Pressed Volume Down."}
    _print({"ok": True, "device": record["id"], "button": args.name}, as_json, words[args.name])
    return 0

