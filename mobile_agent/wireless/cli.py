"""`mobster wifi`: use an iPhone without the cable, over the encrypted Wi-Fi link."""

import json
import sys

from . import GATE, store, switch, transport_on, words

DONE, FAILED, USAGE, MISSING_EXIT = 0, 1, 2, 3


def add_arguments(parser, helpers):
    subs = parser.add_subparsers(dest="wifi_command", metavar="<subcommand>")
    subs.required = True

    def device(sub):
        sub.add_argument("--device", metavar="NAME",
                         help="the iPhone: an id, UDID or name from `mobster devices` (default: your iPhone)")

    def as_json(sub):
        sub.add_argument("--json", action="store_true", help="print one JSON object")

    status = subs.add_parser("status", help="show how each iPhone is connected, and whether Wi-Fi is on for it",
                             description="Show each iPhone set up here: on the cable, on Wi-Fi, or not reachable, "
                                         "and whether Wi-Fi is on for it. Reads only; changes nothing.")
    as_json(status)
    on = subs.add_parser("on", help="let an iPhone be used over Wi-Fi (plug it in first)",
                         description="Let Mobster reach an iPhone over Wi-Fi when it's unplugged, through the "
                                     "encrypted link between this Mac and the phone. The iPhone must be plugged in "
                                     "and trust this Mac; this is the same switch as Connect via network in Xcode.",
                         epilog="exit codes: 0 on, 1 it couldn't be turned on, 2 usage, 3 no such iPhone")
    device(on)
    as_json(on)
    off = subs.add_parser("off", help="use an iPhone only with the cable again",
                          description="Stop using an iPhone over Wi-Fi. When Mobster turned on the iPhone's own "
                                      "switch (Connect via network), it turns that off too, over the cable. When it "
                                      "can't, or Xcode turned the switch on, it says what to do.",
                          epilog="exit codes: 0 off, 1 it failed or the iPhone's own switch is still on (plug it in "
                                 "and run this again), 2 usage, 3 no such iPhone")
    device(off)
    as_json(off)
    probe = subs.add_parser("probe", help="check the encrypted Wi-Fi link to an unplugged iPhone, step by step",
                            description="Check an unplugged iPhone over Wi-Fi: what Xcode's device service reports, "
                                        "the encrypted tunnel, WebDriverAgent on the tunnel through this Mac's "
                                        "relay, and that the phone's Wi-Fi address does not answer on 8100 or 9100. "
                                        "It binds only 127.0.0.1 and changes nothing on the phone (with --start, it "
                                        "launches Mobster's helper on it for the probe, then stops it).",
                            epilog="exit codes: 0 every check passed, 1 a check failed, 2 usage, 3 couldn't run")
    device(probe)
    probe.add_argument("--start", action="store_true",
                       help="launch Mobster's helper over Wi-Fi for the probe when it isn't running (quit the Mac "
                            "app first)")
    as_json(probe)


def run(args):
    as_json = bool(getattr(args, "json", False))
    try:
        command = args.wifi_command
        if command == "status":
            return _status(as_json)
        if command == "probe":
            return _probe(args, as_json)
        return _switch(args, command == "on", as_json)
    except KeyboardInterrupt:
        return 130
    except _Stop as stop:
        return _fail(stop, as_json)


class _Stop(Exception):
    def __init__(self, code, message, fix="", exit_code=FAILED):
        super().__init__(message)
        self.code, self.message, self.fix, self.exit_code = code, message, fix, exit_code


def _problem(code, name, exit_code=FAILED):
    problem = words.problem(code, name)
    return _Stop(code, problem["message"], problem["fix"], exit_code)


def _fail(stop, as_json):
    if as_json:
        _print({"ok": False, "error": stop.message, "code": stop.code, **({"fix": stop.fix} if stop.fix else {})})
    else:
        print(stop.message, file=sys.stderr, flush=True)
        if stop.fix:
            print(stop.fix, file=sys.stderr, flush=True)
    return stop.exit_code


def _print(value):
    try:
        print(json.dumps(value, ensure_ascii=False), flush=True)
    except BrokenPipeError:
        pass


def _data_dir():
    from ..paths import user_data_dir
    return user_data_dir()


def _phones(data_dir):
    """The USB iPhones set up here, primary first (devices.discover without probing)."""
    from .. import devices
    return [item for item in devices.discover(data_dir, sims=False, probe=False)
            if item["kind"] == "usb" and item.get("wdaUrl")]


def _choose(data_dir, name):
    from .. import devices
    records = devices.discover(data_dir, sims=False, probe=False)
    if name:
        try:
            item = devices.resolve(records, name)
        except LookupError as error:
            raise _Stop("no_device", str(error), "", MISSING_EXIT) from None
    else:
        primary = [item for item in records if item.get("primary")]
        usb = [item for item in records if item["kind"] == "usb"]
        item = primary[0] if len(primary) == 1 else usb[0] if len(usb) == 1 else None
        if item is None:
            if not usb:
                raise _Stop("no_device", "Mobster sees no iPhone set up here.",
                            "Plug one in and set it up in the Mobster app first.", MISSING_EXIT)
            raise _Stop("usage", "Several iPhones are here: " + ", ".join(f"{r['name']} ({r['id']})" for r in usb[:6])
                        + ".", "Name one with --device.", USAGE)
    if item["kind"] != "usb":
        raise _Stop("usage", f"{item['name']} isn't an iPhone on a cable. Wi-Fi is for iPhones set up with a cable.",
                    "", USAGE)
    return item


def _listing(data_dir, udid):
    """The phone's USB listing entry (trusted or not), or None when it isn't plugged in."""
    from ..device_manager import DeviceManager
    try:
        devices = DeviceManager(data_dir).devices()
    except Exception:
        devices = []
    return next((item for item in devices if item.get("udid", "").upper() == udid.upper()), None)


def _connection(item, enabled, peers):
    """The words for how a phone is connected now, read from this Mac (the app's relay answering on its port)."""
    from .. import devices
    from ..mac_tools import usb_attached
    if usb_attached(item["udid"]):
        return "usb", words.ON_CABLE
    if not enabled:
        return None, words.OFF
    if item.get("wdaUrl") and devices.wda_answers(item["wdaUrl"]):
        return "wifi", words.ON_WIFI
    peer = (peers or {}).get(item["udid"].upper())
    if peer is not None and peer.network:
        return None, f"{words.UNREACHABLE} yet (the encrypted link is {peer.tunnel})"
    return None, words.UNREACHABLE


def _status(as_json):
    from . import devicectl
    data_dir = _data_dir()
    phones = _phones(data_dir)
    settings = {item["udid"]: store.get(data_dir, item["udid"]) for item in phones}
    peers = None
    if any(value["enabled"] for value in settings.values()):
        try:
            peers = devicectl.list_devices()
        except (devicectl.DevicectlError, RuntimeError):
            peers = None
    rows = []
    for item in phones:
        value = settings[item["udid"]]
        now, text = _connection(item, value["enabled"], peers)
        peer = (peers or {}).get(item["udid"].upper())
        rows.append({"id": item["id"], "udid": item["udid"], "name": item["name"], "enabled": value["enabled"],
                     "via": value["via"], "transport": now, "connection": text,
                     "tunnel": peer.tunnel if peer is not None and peer.network else None})
    if as_json:
        _print({"wifiTransport": transport_on(), "devices": rows})
        return DONE
    if transport_on():
        print("Wi-Fi for iPhones: on")
    else:
        print(f"Wi-Fi for iPhones: off in this Mobster. To try it, set {GATE}=1 in your env file.")
    if not rows:
        print("No iPhone is set up here yet. Plug one in and set it up in the Mobster app.")
        return DONE
    width = max(len(row["name"] or "") for row in rows) + 2
    for row in rows:
        switch = "Wi-Fi on" if row["enabled"] else "Wi-Fi off"
        print(f"  {row['name'] or row['id']:<{width}} {row['connection']:<34} {switch}")
    return DONE


def _switch(args, on, as_json):
    data_dir = _data_dir()
    item = _choose(data_dir, getattr(args, "device", None))
    udid, name = item["udid"], item["name"]
    listing = _listing(data_dir, udid)
    following = None
    try:
        if on:
            if switch.turn_on(data_dir, udid, listing) == "xcode":
                following = words.problem("needs_xcode", name)
        elif left_on := switch.turn_off(data_dir, udid, listing):
            following = words.problem(left_on, name)
    except switch.SwitchRefused as error:
        raise _problem(error.code, name) from None
    except RuntimeError as error:
        raise _Stop("devices_off", f"{error}.", "", MISSING_EXIT) from None
    if not on:
        return _switched_off(item, following, as_json)
    if as_json:
        _print({"ok": True, "device": {"id": item["id"], "udid": udid, "name": name, "enabled": on},
                "wifiTransport": transport_on(), **({"next": following} if following else {})})
        return DONE
    if following:
        print(following["message"])
        print(following["fix"])
    else:
        print(f"Wi-Fi is on for {name}. Unplug it when you like: Mobster reaches it through the encrypted link "
              "between this Mac and the phone.")
    print(f"Check it, unplugged: mobster wifi probe --device {json.dumps(name, ensure_ascii=False)}")
    if not transport_on():
        print(f"Wi-Fi for iPhones is off in this Mobster: to use it for tasks, set {GATE}=1 in your env file.")
    return DONE


def _switched_off(item, left_on, as_json):
    """Wi-Fi is off in Mobster; ``left_on`` is the problem of the phone's own switch still on, or None. A switch
    Mobster turned on and couldn't turn off fails (exit 1); one Xcode turned on is the person's to turn off (exit 0,
    like `on`'s Xcode step)."""
    name = item["name"]
    failed = left_on is not None and left_on["code"] != "xcode_on"
    if as_json:
        device = {"id": item["id"], "udid": item["udid"], "name": name, "enabled": False}
        _print({"ok": False, "error": left_on["message"], "code": left_on["code"], "fix": left_on["fix"],
                "device": device} if failed else
               {"ok": True, "device": device, "wifiTransport": transport_on(),
                **({"next": left_on} if left_on else {})})
        return FAILED if failed else DONE
    print(f"Wi-Fi is off for {name}. Mobster uses it only with the cable.")
    if left_on:
        stream = sys.stderr if failed else sys.stdout
        print(left_on["message"], file=stream)
        print(left_on["fix"], file=stream)
    if failed:
        print(f"Plugged in and unlocked: mobster wifi off --device {json.dumps(name, ensure_ascii=False)}",
              file=sys.stderr)
    return FAILED if failed else DONE


def _probe(args, as_json):
    from .. import devices, native_helpers
    from ..mac_tools import usb_attached
    from .probe import Probe, describe
    if not native_helpers.devices_allowed():
        raise _Stop("devices_off", "Devices are off in this process (MOBSTER_NO_DEVICES=1).", "", MISSING_EXIT)
    data_dir = _data_dir()
    item = _choose(data_dir, getattr(args, "device", None))
    manager = None
    if item.get("primary"):
        from ..device_manager import DeviceManager
        manager = DeviceManager(data_dir)
    else:
        manager = devices.manager_for(item["udid"], data_dir)
    slot = devices.port_of(item.get("wdaUrl"))
    if not as_json:
        print(f"Probing {item['name']} over Wi-Fi "
              f"({'Wi-Fi on' if store.enabled(data_dir, item['udid']) else 'Wi-Fi off'} "
              f"for it; Wi-Fi for iPhones {'on' if transport_on() else 'off'} in this Mobster)")
    probe = Probe(udid=item["udid"], name=item["name"], slot_port=slot, data_dir=data_dir,
                  start=bool(getattr(args, "start", False)), manager=manager,
                  out=None if as_json else (lambda step: print(describe(step), flush=True)))
    try:
        probe.run(usb_attached)
    finally:
        probe.stop_launched()
    from . import result
    result.record(data_dir, item["udid"], probe.public())   # the app's Wi-Fi tile reads it (GET /api/wireless)
    if as_json:
        _print(probe.public())
    else:
        print("")
        print("Every check passed." if probe.ok else "Not every check passed: see the lines above.")
    return DONE if probe.ok else FAILED
