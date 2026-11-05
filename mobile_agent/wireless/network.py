"""This Mac's network, read without changing it: changes, a VPN, Local Network permission, peers and routes."""

import errno
import hashlib
import ipaddress
import os
import re
import select
import socket
import subprocess
import threading
import time

SCUTIL = "/usr/sbin/scutil"
ROUTE = "/sbin/route"
IFCONFIG = "/sbin/ifconfig"
DNS_SD = "/usr/bin/dns-sd"
VPN_INTERFACE = re.compile(r"^(utun|ipsec|ppp|tun|tap|wg)\d*$")
# A network this Mac can share with an iPhone: Wi-Fi or Ethernet (en0, en7, ...), or a bridge.
LAN_INTERFACE = re.compile(r"^(en|bridge)\d+$")
# Apple's CoreDevice tunnel to a phone is a utun interface on this Mac that holds this Mac's end of the link: an
# address on the phone's own /64 (or a narrower network), or a point-to-point address whose peer is the phone. A VPN
# is a utun too, so the route must also be more than a default route, and the utun not one macOS marks as a VPN.
TUNNEL_INTERFACE = re.compile(r"^utun\d+$")
TUNNEL_PREFIX = 64
# `ifconfig -v`'s marks of a VPN: the IS_VPN interface flag, or a NetworkExtension VPN agent.
VPN_MARK = re.compile(r"\bIS_VPN\b|agent domain:NetworkExtension type:VPN\b")
INET6 = re.compile(r"^\s*inet6\s+([0-9a-fA-F:.]+)(?:%\S+)?(?:\s+-->\s+([0-9a-fA-F:.]+))?\s+prefixlen\s+(\d+)", re.M)
# What phones, TVs, speakers, printers and computers advertise. Info.plist's NSBonjourServices lists the same.
PEER_TYPES = ("_companion-link._tcp", "_airplay._tcp", "_raop._tcp", "_apple-mobdev2._tcp", "_remotepairing._tcp",
              "_googlecast._tcp", "_hap._tcp", "_ipp._tcp", "_sleep-proxy._udp", "_spotify-connect._tcp")
BROWSE_SECONDS = 2.5
# mDNSResponder's "policy denied": macOS's Local Network privacy refused the browse.
POLICY_DENIED = "-65570"
LOOPBACK_INDEX = 1


def read_output(argv, timeout=3):
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def nwi(reader=None):
    """`scutil --nwi`: the network state macOS routes by (interfaces, addresses, reachability), or None."""
    return (reader or read_output)([SCUTIL, "--nwi"])


def signature(text):
    if text is None:
        return None
    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16]


def ipv4_interfaces(text):
    """The interfaces `scutil --nwi` lists under IPv4, in its order (the primary first)."""
    interfaces, section = [], None
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("IPv4 network interface information"):
            section = "v4"
            continue
        if stripped.startswith("IPv6 network interface information") or stripped.startswith("Network interfaces:"):
            section = None
            continue
        if section == "v4":
            match = re.match(r"^([a-z]+\d+)\s*:\s*flags", stripped)
            if match:
                interfaces.append(match.group(1))
    return interfaces


def vpn_interface(text):
    """The IPv4 interface of a VPN when one carries this Mac's traffic (utun, ipsec, ppp, ...), else None."""
    return next((name for name in ipv4_interfaces(text) if VPN_INTERFACE.match(name)), None)


def default_gateway(reader=None):
    """The IPv4 default gateway (`route -n get default`), or None."""
    text = (reader or read_output)([ROUTE, "-n", "get", "default"])
    match = re.search(r"^\s*gateway:\s*(\S+)\s*$", text or "", re.M)
    if not match:
        return None
    try:
        return str(ipaddress.IPv4Address(match.group(1)))
    except ValueError:
        return None


# macOS's Local Network privacy refuses with EHOSTUNREACH to a gateway that is there; any other answer means allowed.
def local_network_allowed(gateway, timeout=0.6, connect=None):
    if not gateway:
        return None
    attempt = connect or socket.create_connection
    try:
        attempt((gateway, 9), timeout).close()
        return True
    except ConnectionRefusedError:
        return True
    except (socket.timeout, TimeoutError):
        return True
    except OSError as error:
        if error.errno in (errno.EHOSTUNREACH, errno.EPERM, errno.EACCES):
            return False
        return None


def resolve(hostnames, timeout=4.0, lookup=None):
    lookup = lookup or socket.getaddrinfo
    found = {"ipv4": [], "ipv6": []}
    for name in hostnames:
        result = []

        def ask(name=name, result=result):
            try:
                result.extend(lookup(name, None, 0, socket.SOCK_STREAM))
            except (OSError, UnicodeError):
                pass

        worker = threading.Thread(target=ask, name="mobster-wifi-resolve", daemon=True)
        worker.start()
        worker.join(timeout)
        for family, *_rest, sockaddr in list(result):
            address = sockaddr[0].split("%", 1)[0]
            try:
                parsed = ipaddress.ip_address(address)
            except ValueError:
                continue
            # the tunnel's fd00::/8 end is not an address on the Wi-Fi network
            if parsed.version == 6 and parsed in ipaddress.IPv6Network("fd00::/8"):
                continue
            key = "ipv4" if parsed.version == 4 else "ipv6"
            if sockaddr[0] not in found[key]:
                found[key].append(sockaddr[0])
    return found


def port_open(host, port, timeout=2.0, connect=None):
    attempt = connect or socket.create_connection
    try:
        attempt((host, port), timeout).close()
        return True
    except OSError:
        return False


def primary_interface(reader=None):
    text = (reader or read_output)([ROUTE, "-n", "get", "default"])
    match = re.search(r"^\s*interface:\s*(\S+)\s*$", text or "", re.M)
    return match.group(1) if match else None


def _field(text, key):
    """One ``key: value`` line of `route -n get`'s answer, or None."""
    match = re.search(rf"^\s*{key}:\s*(\S+)\s*$", text or "", re.M)
    return match.group(1) if match else None


def tunnel_route(address, reader=None):
    """(interface, None) when this Mac reaches ``address``, the phone's end of its CoreDevice tunnel, through that
    tunnel; else (the interface or None, why not). Whatever it can't tell apart from a VPN is refused: no route, a
    route through Wi-Fi or Ethernet, only a default route, a utun macOS marks as a VPN, or a utun that holds no
    address on a link to ``address``."""
    read = reader or read_output
    try:
        target = ipaddress.IPv6Address(str(address).strip("[]"))
    except ValueError:
        return None, f"{address} isn't an IPv6 address"
    shown = target.compressed
    route = read([ROUTE, "-n", "get", "-inet6", shown])
    name = _field(route, "interface")
    if not (name and TUNNEL_INTERFACE.match(name)):
        return name, f"this Mac reaches {shown} through {name or 'no interface'}, not a tunnel (utun)"
    if _field(route, "destination") == "default":
        return name, f"this Mac reaches {shown} only through {name}'s default route, which a VPN sets, not a tunnel"
    facts = read([IFCONFIG, "-v", name])
    if facts is None:
        return name, f"this Mac couldn't read {name}'s addresses"
    if VPN_MARK.search(facts):
        return name, f"this Mac reaches {shown} through {name}, which is a VPN, not the tunnel"
    if not link_end(facts, target):
        held = ", ".join(f"{own}/{prefix}" + (f" to {peer}" if peer else "")
                         for own, peer, prefix in INET6.findall(facts)
                         if not own.lower().startswith("fe80:")) or "only link-local addresses"
        return name, f"{name} holds no address on a link to {shown} ({held}), so it isn't the tunnel to the phone"
    return name, None


def link_end(facts, target):
    """Whether an address `ifconfig` lists in ``facts`` is this Mac's end of a link to ``target``: one whose
    point-to-point peer is ``target``, or whose network, /64 or narrower, holds it."""
    for own, peer, prefix in INET6.findall(facts or ""):
        try:
            own, peer = ipaddress.IPv6Address(own), ipaddress.IPv6Address(peer) if peer else None
        except ValueError:
            continue
        if own.is_link_local or own == target:
            continue
        if peer == target or (TUNNEL_PREFIX <= int(prefix) < 128
                              and target in ipaddress.IPv6Network(f"{own}/{prefix}", strict=False)):
            return True
    return False


def through_tunnel(address, reader=None):
    return tunnel_route(address, reader)[1] is None


def parse_browse(text):
    found, denied = [], POLICY_DENIED in (text or "")
    for line in (text or "").splitlines():
        parts = line.split(None, 6)
        if len(parts) == 7 and parts[1] == "Add" and parts[3].isdigit():
            found.append((int(parts[3]), parts[6].strip()))
    return found, denied


def _browse_one(service, seconds, stop, sink):
    # dns-sd buffers its output unless it writes to a terminal, so it gets a pseudo-terminal
    import pty
    try:
        master, slave = pty.openpty()
    except OSError:
        return False
    try:
        process = subprocess.Popen([DNS_SD, "-B", service, "local."], stdin=subprocess.DEVNULL, stdout=slave,
                                   stderr=slave, start_new_session=True)
    except OSError:
        os.close(master)
        os.close(slave)
        return False
    os.close(slave)
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline and not stop.is_set():
            ready, _, _ = select.select([master], [], [], 0.1)
            if not ready:
                continue
            try:
                data = os.read(master, 4096)
            except OSError:
                break
            if not data:
                break
            sink.append(data)
    finally:
        try:
            os.killpg(process.pid, 9)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        os.close(master)
    return True


def browse(interface_index, types=PEER_TYPES, seconds=BROWSE_SECONDS, settle=1.0):
    if not os.access(DNS_SD, os.X_OK):
        return {service: None for service in types}
    stop = threading.Event()
    sinks = {service: [] for service in types}
    ran = {}

    def run(service):
        ran[service] = _browse_one(service, seconds, stop, sinks[service])

    def outputs():
        return {service: b"".join(list(sinks[service])).decode("utf-8", "replace") if ran.get(service, True)
                else None for service in types}

    workers = [threading.Thread(target=run, args=(service,), name="mobster-wifi-bonjour", daemon=True)
               for service in types]
    began = time.monotonic()
    for worker in workers:
        worker.start()
    while any(worker.is_alive() for worker in workers) and time.monotonic() - began < seconds + 3:
        time.sleep(0.1)
        # after `settle`, this Mac's own services have shown on the loopback interface too
        if time.monotonic() - began >= settle and peers_heard(outputs(), interface_index)[0]:
            stop.set()
    stop.set()
    for worker in workers:
        worker.join(3)
    return outputs()


def peers_heard(outputs, interface_index):
    own, others, denied, ran = set(), [], False, False
    for text in outputs.values():
        if text is None:
            continue
        ran = True
        found, refused = parse_browse(text)
        denied = denied or refused
        # macOS also lists this Mac's own services on the loopback interface: they aren't other devices
        own.update(name for index, name in found if index == LOOPBACK_INDEX)
        others += [name for index, name in found if index == interface_index]
    return sorted({name for name in others if name not in own}), denied, ran


def peer_check(*, interface=None, gateway=None, allowed=None, outputs=None, index_of=None):
    from .. import native_helpers
    if outputs is None and not native_helpers.devices_allowed():
        return {"state": "unknown", "interface": interface, "peers": 0}
    interface = primary_interface() if interface is None else interface
    if not interface:
        return {"state": "offline", "interface": None, "peers": 0}
    if VPN_INTERFACE.match(interface):
        return {"state": "vpn", "interface": interface, "peers": 0}
    if not LAN_INTERFACE.match(interface):
        return {"state": "unknown", "interface": interface, "peers": 0}
    gateway = default_gateway() if gateway is None else gateway
    allowed = local_network_allowed(gateway) if allowed is None else allowed
    if allowed is False:
        return {"state": "no_permission", "interface": interface, "peers": 0}
    try:
        index = (index_of or socket.if_nametoindex)(interface)
    except OSError:
        return {"state": "unknown", "interface": interface, "peers": 0}
    if outputs is None:
        outputs = browse(index)
    names, denied, ran = peers_heard(outputs, index)
    if names:
        return {"state": "ok", "interface": interface, "peers": len(names)}
    if denied:
        return {"state": "no_permission", "interface": interface, "peers": 0}
    if not ran:
        return {"state": "unknown", "interface": interface, "peers": 0}
    return {"state": "blocked", "interface": interface, "peers": 0}
