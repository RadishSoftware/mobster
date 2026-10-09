"""`mobster phone`: the phone's clipboard, files in an app's folder, and installing a build (devtools.py registers it).

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
    common(put)
    get_file = verbs.add_parser("get", help="copy a file from an app's folder to this Mac",
                                description="Copy a file from an app's folder on the iPhone to this Mac.")
    get_file.add_argument("name", metavar="NAME", help="the file's name in the app's folder")
    get_file.add_argument("--app", required=True, metavar="BUNDLE", help="the app's bundle ID")
    get_file.add_argument("-o", "--output", metavar="PATH",
                          help="where to save it: a folder, or a file name (default: the current folder)")
    common(get_file)
    ls = verbs.add_parser("ls", help="list the apps that share files, or the files in one",
                          description="Without --app, list the iPhone's apps whose folders show in Files. With "
                                      "--app, list the files in that app's folder.")
    ls.add_argument("--app", metavar="BUNDLE", help="the app's bundle ID")
    common(ls)

    install = subs.add_parser("install", help="install a signed .app or .ipa on the device",
                              description="Install a build on a USB iPhone (xcrun devicectl) or a Mobster simulator "
                                          "(xcrun simctl). The build must be signed for that iPhone, and the iPhone "
                                          "needs Developer Mode on.",
                              epilog="exit codes: 0 installed, 1 the install failed, 2 usage, 3 no device or no Xcode")
    install.add_argument("path", metavar="PATH", help="the .app folder or .ipa file")
    common(install)


def run(args):
    as_json = bool(getattr(args, "json", False))
    try:
        command = args.phone_command
        if command == "clipboard" and args.clipboard_command == "set":
            return _clipboard_set(args, as_json)
        if command == "clipboard":
            return _clipboard_get(args, as_json)
        if command == "file":
            return _file(args, as_json)
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
    from .clipboard import read_text_file, set_text
    from . import wda
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
    from .clipboard import get_text
    from . import wda
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
    from .install import install
    record = choose_device(args.device)

    def progress(line):
        print(line, file=sys.stderr, flush=True)
    result = install(record, args.path, progress=progress)
    _print(result, as_json, f"Installed {result.get('bundle_id') or args.path} on {record['name']} "
                            f"in {result['seconds']:g} s.")
    return 0



def _file(args, as_json, runner=None):
    """`mobster phone file put|get|ls`: the user's own command is the go-ahead for a copy it names."""
    from pathlib import Path
    from .files import PhoneFiles
    from .install import describe_path
    record = choose_device(args.device)
    phone = PhoneFiles(record, runner=runner)
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
        if target.exists():
            saved.unlink(missing_ok=True)
            raise PhoneIOError(f"{describe_path(target)} already exists.", "usage", "Choose another name with -o.")
        saved = saved.rename(target)
    _print({"ok": True, "device": record["id"], "path": str(saved), "bytes": saved.stat().st_size}, as_json,
           f"Saved {args.name} to {describe_path(saved)}.")
    return 0
