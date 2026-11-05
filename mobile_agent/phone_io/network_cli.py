"""Network profile generation and diagnostics for `mobster phone network`."""

from . import PhoneIOError, choose_device


def add_arguments(subs, helpers):
    path_type = helpers.get("path_type")
    parser = subs.add_parser("network", help="create network profiles, diagnose this Mac, or capture USB traffic",
                             description="Create removable iPhone network profiles, inspect this Mac's connectivity, "
                                         "or capture a USB iPhone's packets. Profiles need installation in Settings.")
    verbs = parser.add_subparsers(dest="network_command", metavar="<subcommand>", required=True)

    proxy = verbs.add_parser("proxy-profile", help="write a Wi-Fi HTTP proxy or PAC profile",
                             description="Write a Wi-Fi profile for one SSID. Apps may ignore HTTP proxies; this "
                                         "does not route cellular traffic or decrypt TLS.")
    proxy.add_argument("--ssid", required=True, help="the Wi-Fi network name")
    mode = proxy.add_mutually_exclusive_group(required=True)
    mode.add_argument("--server", help="the HTTP proxy hostname or IP address")
    mode.add_argument("--pac", metavar="URL", help="the HTTP or HTTPS proxy auto-configuration URL")
    proxy.add_argument("--port", type=int, default=8080, help="the HTTP proxy port (default: 8080)")
    proxy.add_argument("--username", help="the HTTP proxy username")
    proxy.add_argument("--password-file", type=path_type, metavar="FILE",
                       help="read the proxy password from this UTF-8 file")
    proxy.add_argument("--wifi-password-file", type=path_type, metavar="FILE",
                       help="read the Wi-Fi password from this UTF-8 file")
    proxy.add_argument("--encryption", choices=("Any", "WPA2", "WPA3", "None"), default="Any",
                       help="the Wi-Fi encryption type (default: Any)")

    vpn = verbs.add_parser("vpn-profile", help="write an IKEv2 VPN profile with username authentication",
                           description="Write an IKEv2 EAP VPN profile for your provider. This does not create a VPN "
                                       "server. Install the profile and connect in Settings.")
    vpn.add_argument("--server", required=True, help="the VPN server hostname or IP address")
    vpn.add_argument("--remote-id", required=True, help="the server identifier from your VPN provider")
    vpn.add_argument("--local-id", required=True, help="the client identifier from your VPN provider")
    vpn.add_argument("--username", required=True, help="the VPN account username")
    vpn.add_argument("--password-file", type=path_type, metavar="FILE",
                     help="read the VPN password from this UTF-8 file")

    relay = verbs.add_parser("relay-profile", help="write an Apple HTTP/2 network relay profile",
                             description="Write an Apple network relay profile for an existing compatible HTTP/2 "
                                         "relay service. This is not an ordinary HTTP proxy or a new relay server.")
    relay.add_argument("--url", required=True, help="the HTTPS relay endpoint")
    relay.add_argument("--token-file", type=path_type, metavar="FILE",
                       help="read the bearer token from this UTF-8 file")

    for command, kind, name in ((proxy, "proxy", "Mobster proxy"), (vpn, "vpn", "Mobster VPN"),
                                 (relay, "relay", "Mobster relay")):
        command.add_argument("--output", required=True, type=path_type, metavar="FILE",
                             help="write a new private .mobileconfig file; existing files are kept")
        command.add_argument("--name", default=name, help=f"the profile's display name (default: {name})")
        command.add_argument("--identifier", default=f"app.mobster.network.{kind}",
                             help=f"the stable profile identifier (default: app.mobster.network.{kind})")
        command.add_argument("--json", action="store_true", help="print one JSON object")

    diagnose = verbs.add_parser("diagnose", help="inspect this Mac's route and optional TCP endpoint reachability",
                                description="Check this Mac's default route and optional TCP endpoint. With --device, "
                                            "also report USB attachment. This does not test the iPhone's internet "
                                            "egress, proxy authentication, or installed profiles.")
    diagnose.add_argument("--device", metavar="NAME", help="also inspect USB attachment for this device")
    diagnose.add_argument("--host", help="the hostname or IP address to test from this Mac")
    diagnose.add_argument("--port", type=int, default=8080, help="the TCP endpoint port (default: 8080)")
    diagnose.add_argument("--json", action="store_true", help="print one JSON object")

    capture = verbs.add_parser("capture", help="save a bounded USB iPhone packet trace",
                               description="Capture packets through Apple's Remote Virtual Interface. Requires a "
                                           "paired USB iPhone, Xcode tools and BPF permissions on this Mac. TLS "
                                           "remains encrypted; packet data may contain private information.")
    capture.add_argument("--device", required=True, metavar="NAME", help="the paired USB iPhone's id, UDID or name")
    capture.add_argument("--output", required=True, type=path_type, metavar="FILE",
                         help="write a new private pcap file")
    capture.add_argument("--seconds", type=int, default=30, help="stop after 1 to 300 seconds (default: 30)")
    capture.add_argument("--packets", type=int, default=10000, help="stop after 1 to 100000 packets (default: 10000)")
    capture.add_argument("--snaplen", type=int, default=128, help="capture 64 to 65535 bytes per packet (default: 128)")
    capture.add_argument("--json", action="store_true", help="print one JSON object")


def run(args):
    from . import network_capture
    from . import network_profiles as profiles
    from .cli import _print

    command = args.network_command
    if command == "diagnose":
        result, lines = diagnose(args)
    elif command == "capture":
        # Validate before device discovery so bad input cannot initiate device work.
        if not (1 <= args.seconds <= 300 and 1 <= args.packets <= 100000 and 64 <= args.snaplen <= 65535):
            raise PhoneIOError("Use 1–300 seconds, 1–100000 packets and a snap length of 64–65535 bytes.", "usage")
        record = choose_device(args.device)
        result = network_capture.capture(record, args.output, seconds=args.seconds,
                                         packets=args.packets, snaplen=args.snaplen)
        lines = [f"Saved {result['bytes']:,} bytes to {result['path']}."]
        if result.get("warning"):
            lines.append(result["warning"])
    else:
        if command == "proxy-profile":
            payload = profiles.proxy(ssid=args.ssid, server=args.server, port=args.port, pac=args.pac,
                                     username=args.username, password=profiles.secret(args.password_file),
                                     wifi_password=profiles.secret(args.wifi_password_file), encryption=args.encryption)
        elif command == "vpn-profile":
            payload = profiles.vpn(server=args.server, remote_id=args.remote_id, local_id=args.local_id,
                                   username=args.username, password=profiles.secret(args.password_file))
        else:
            payload = profiles.relay(endpoint=args.url, token=profiles.secret(args.token_file))
        result = profiles.write_profile(payload, args.output, name=args.name, identifier=args.identifier)
        lines = [f"Saved {result['path']}. The profile is not installed.", result["next"]]
    _print(result, args.json, "\n".join(lines))
    return 0 if result["ok"] else 1


def diagnose(args):
    from .. import mac_tools
    from ..wireless import network
    from .network_profiles import host

    if not 1 <= args.port <= 65535:
        raise PhoneIOError("The endpoint port must be between 1 and 65535.", "usage")
    endpoint_host = host(args.host) if args.host is not None else None
    record = choose_device(args.device) if args.device else None
    interface = network.primary_interface()
    state = network.nwi()
    result = {"ok": True, "scope": "mac", "mac": {"defaultInterface": interface,
              "gateway": network.default_gateway(), "vpnInterface": network.vpn_interface(state)},
              "phoneEgressTested": False}
    lines = [f"Mac default interface: {interface or 'unknown'}.",
             f"Mac VPN interface: {result['mac']['vpnInterface'] or 'none detected'}."]
    if endpoint_host:
        reachable = network.port_open(endpoint_host, args.port)
        result["endpoint"] = {"host": endpoint_host, "port": args.port, "reachableFromMac": reachable}
        result["ok"] = reachable
        lines.append(f"TCP endpoint from this Mac: {'reachable' if reachable else 'unreachable'}.")
    if record:
        attached = mac_tools.usb_attached(record.get("udid")) if record.get("kind") == "usb" else None
        result["device"] = {"id": record["id"], "kind": record.get("kind"), "usbAttached": attached}
        lines.append(
            f"Device USB attachment: { {True: 'connected', False: 'disconnected', None: 'unknown'}[attached]}.")
    lines.append("The iPhone's internet egress, proxy authentication and installed profiles were not tested.")
    return result, lines
