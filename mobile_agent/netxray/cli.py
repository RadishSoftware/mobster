import argparse
import json
import sys
import time

from . import capture, store, summary, target

DONE, FAILED, USAGE, MISSING = 0, 1, 2, 3


def seconds_type(text):
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a whole number of seconds") from None
    if not capture.MIN_SECONDS <= value <= capture.MAX_SECONDS:
        raise argparse.ArgumentTypeError(f"must be {capture.MIN_SECONDS} to {capture.MAX_SECONDS}")
    return value


HELP = "record your iPhone's network traffic and say who each app talked to"
DESCRIPTION = ("Record an iPhone's network traffic for a while and say who each app talked to: the domains, the\n"
               "companies behind them, and which do ads, analytics or attribution. Mobster reads hostnames, sizes\n"
               "and times. It decrypts nothing and keeps no page, no URL path and no address. The capture file is\n"
               "deleted after the analysis unless you pass --keep-capture.")
EPILOG = ("exit codes: 0 done, 1 the capture failed, 2 usage, 3 couldn't run (no such iPhone, a simulator, no\n"
          "            capture tool)\n\n"
          "examples:\n  mobster xray network --device \"Test iPhone\" --seconds 30\n"
          "  mobster xray network --device \"Test iPhone\" --app Weather\n"
          "  mobster xray network --from capture.pcapng")


def add_arguments(network):
    """The `network` subcommand's flags (mobile_agent/xray/cli.py makes the parser)."""
    network.add_argument("--device", metavar="NAME",
                         help="the iPhone: an id, UDID or name from `mobster devices` (default: the one connected)")
    network.add_argument("--seconds", type=seconds_type, metavar="N",
                         help="how long to record, 5 to 3600 (default 60)")
    network.add_argument("--wifi", action="store_true",
                         help="record over Wi-Fi even with the cable in (refused unless the link is encrypted)")
    # dest isn't "app": `mobster --app` is the terminal UI's, and main's check_placement refuses it on other commands.
    network.add_argument("--app", dest="only_app", metavar="NAME",
                         help="report only this app, by its name on the iPhone")
    network.add_argument("--keep-capture", action="store_true",
                         help="keep the .pcapng beside the report (readable only by you)")
    network.add_argument("--json", action="store_true", help="print one JSON object")
    network.add_argument("--from", dest="from_file", metavar="FILE.pcapng",
                         help="read a capture you already have, with no iPhone")


def run(args):
    return network(args)


def _say(text):
    print(text, file=sys.stderr, flush=True)


def network(args):
    as_json = bool(getattr(args, "json", False))
    if args.from_file:
        if args.device or args.seconds or args.wifi or args.keep_capture:
            _say("--from reads a capture you already have: leave out --device, --seconds, --wifi and --keep-capture.")
            return USAGE
        return _report(args, None, None, as_json)
    try:
        phone = target.pick(args.device, wifi=args.wifi)
    except target.TargetError as error:
        _say(str(error))
        return MISSING
    seconds = args.seconds or 60
    folder = store.new_folder()
    try:
        recording = capture.Recording(phone.udid, seconds, store.capture_path(folder), network=phone.network)
    except capture.CaptureError as error:
        _say(str(error))
        folder.rmdir()
        return MISSING
    _say(f"Recording {phone.name} over {'Wi-Fi' if phone.network else 'the cable'} for {seconds} s. Use the apps you "
         "want to look at. ctrl+c stops early.")
    try:
        try:
            deadline = time.monotonic() + seconds
            while recording.running() and time.monotonic() < deadline:
                time.sleep(0.25)
        except KeyboardInterrupt:
            _say("Stopping early.")
        recording.stop()
        recording.finish()
    except capture.CaptureError as error:
        _say(str(error))
        store.discard_capture(folder)
        _drop_empty(folder)
        return FAILED
    finally:
        recording.stop()
    names = target.phone_names(phone)
    return _report(args, phone, (folder, names), as_json)


def _drop_empty(folder):
    try:
        folder.rmdir()
    except OSError:
        pass


def _report(args, phone, recorded, as_json):
    from .pcapng import PcapngError
    if recorded is None:
        folder, names, source = store.new_folder(), None, args.from_file
    else:
        (folder, names), source = recorded, None
    try:
        result, paths = store.conclude(folder, names, keep=args.keep_capture, only=args.only_app, source=source)
    except FileNotFoundError:
        _say(f"There's no capture at {source}. Check the path.")
    except PcapngError as error:
        _say(str(error))
    except OSError:
        _say("Mobster couldn't read the capture or write the report in its data folder.")
    else:
        return _print(args, phone, folder, result, paths, as_json)
    _drop_empty(folder)
    return FAILED


def _print(args, phone, folder, result, paths, as_json):
    said = summary.lines(result)
    if as_json:
        kept = store.capture_path(folder) if args.keep_capture and not args.from_file else None
        print(json.dumps({"device": {"id": phone.id, "name": phone.name} if phone else None, "lines": said,
                          "report": {kind: str(path) for kind, path in paths.items()},
                          "capture": str(kept) if kept else None, "result": result}, ensure_ascii=False))
        return DONE
    for line in said:
        print(line)
    if not said:
        print(f"No traffic from {args.only_app} in this capture." if args.only_app
              else "No traffic to the internet in this capture.")
    print(f"Report: {paths['html']}")
    return DONE
