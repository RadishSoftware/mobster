"""This Mac's network, read without changing it: network changes, a VPN, Local Network permission, whether this
network lets devices see each other, the route to the phone's tunnel address, and the phone's Wi-Fi addresses for the
probe's LAN check (SPEC §3.7 items 6 and 8).

Everything here is a short read with a timeout; nothing listens, and nothing reaches the phone except the probe's
connection attempts to its Wi-Fi address, which must fail.

**Networks that keep devices apart.** Guest, hotel and campus Wi-Fi often stop devices from reaching each other
(client isolation) and drop Bonjour, so this Mac can't find the iPhone however it's set up. ``peer_check`` tells: it
browses a few Bonjour service types that phones, TVs, speakers, printers and other computers advertise (Apple's
``dns-sd``, which asks macOS's own mDNSResponder: Mobster opens no socket for it) for at most 2.5 s, and counts the
devices it hears on this Mac's own network interface, leaving out this Mac's own services (which macOS also lists on
the loopback interface) and peer-to-peer links such as AirDrop's. Hearing none, on a network that is up and that macOS
lets Mobster use, means the network keeps devices apart: "This network stops devices from seeing each other. Use the
cable or your phone's hotspot."
"""

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
DNS_SD = "/usr/bin/dns-sd"
VPN_INTERFACE = re.compile(r"^(utun|ipsec|ppp|tun|tap|wg)\d*$")
# A network this Mac can share with an iPhone: Wi-Fi or Ethernet (en0, en7, ...), or a bridge.
LAN_INTERFACE = re.compile(r"^(en|bridge)\d+$")
# Apple's CoreDevice tunnel to a phone is a utun interface on this Mac: the route to the phone's tunnel address
# must go through one, never through Wi-Fi or Ethernet.
TUNNEL_INTERFACE = re.compile(r"^utun\d+$")
# What phones, TVs, speakers, printers and computers advertise. Info.plist's NSBonjourServices lists the same.
PEER_TYPES = ("_companion-link._tcp", "_airplay._tcp", "_raop._tcp", "_apple-mobdev2._tcp", "_remotepairing._tcp",
              "_googlecast._tcp", "_hap._tcp", "_ipp._tcp", "_sleep-proxy._udp", "_spotify-connect._tcp")
BROWSE_SECONDS = 2.5
# mDNSResponder's "policy denied": macOS's Local Network privacy refused the browse.
POLICY_DENIED = "-65570"
LOOPBACK_INDEX = 1


def _output(argv, timeout=3):
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def nwi(reader=None):
    """`scutil --nwi`: the network state macOS routes by (interfaces, addresses, reachability), or None."""
    return (reader or _output)([SCUTIL, "--nwi"])


def signature(text):
    """A short hash of `scutil --nwi`: it changes when the primary interface or an address changes (another Wi-Fi, a
    new DHCP lease, a VPN coming up). None when the state couldn't be read."""
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
    text = (reader or _output)([ROUTE, "-n", "get", "default"])
    match = re.search(r"^\s*gateway:\s*(\S+)\s*$", text or "", re.M)
    if not match:
        return None
    try:
        return str(ipaddress.IPv4Address(match.group(1)))
    except ValueError:
        return None


def local_network_allowed(gateway, timeout=0.6, connect=None):
    """Whether this process may reach the local network (macOS Local Network privacy): True, False when macOS
    refuses (EHOSTUNREACH, "No route to host", to a gateway that is there) or None when it can't be told.
    It knocks once on the gateway's TCP port 9 (discard): any answer, a refusal or a timeout means allowed."""
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
    """The IPv4 and IPv6 addresses ``hostnames`` resolve to (Bonjour .local names included), skipping the tunnel's
    fd00::/8 addresses: {"ipv4": [...], "ipv6": [...]}. Each lookup runs at most ``timeout`` seconds."""
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
            if parsed.version == 6 and parsed in ipaddress.IPv6Network("fd00::/8"):
                continue
            key = "ipv4" if parsed.version == 4 else "ipv6"
            if sockaddr[0] not in found[key]:
                found[key].append(sockaddr[0])
    return found


def port_open(host, port, timeout=2.0, connect=None):
    """Whether something accepts a TCP connection at ``host``:``port`` (the probe expects the phone's Wi-Fi address
    to refuse WebDriverAgent's ports)."""
    attempt = connect or socket.create_connection
    try:
        attempt((host, port), timeout).close()
        return True
    except OSError:
        return False


def primary_interface(reader=None):
    """The interface of this Mac's default route (`route -n get default`): "en0", "utun4" (a VPN), ... or None when
    there is no network."""
    text = (reader or _output)([ROUTE, "-n", "get", "default"])
    match = re.search(r"^\s*interface:\s*(\S+)\s*$", text or "", re.M)
    return match.group(1) if match else None


def route_interface(address, reader=None):
    """The interface this Mac routes ``address`` (an IPv6 address) through (`route -n get -inet6`), or None."""
    try:
        parsed = ipaddress.IPv6Address(str(address).strip("[]"))
    except ValueError:
        return None
    text = (reader or _output)([ROUTE, "-n", "get", "-inet6", parsed.compressed])
    match = re.search(r"^\s*interface:\s*(\S+)\s*$", text or "", re.M)
    return match.group(1) if match else None


def through_tunnel(address, reader=None):
    """Whether this Mac reaches ``address`` through a tunnel interface (utun), as it reaches a phone's end of the
    CoreDevice tunnel. An address on the Wi-Fi or Ethernet network (a home network's own fd00::/8 addresses
    included) is False, so WebDriverAgent is never told to listen on it."""
    name = route_interface(address, reader)
    return bool(name and TUNNEL_INTERFACE.match(name))


def parse_browse(text):
    """[(interface index, instance name)] for each service `dns-sd -B` added, and whether macOS refused the browse
    (Local Network privacy)."""
    found, denied = [], POLICY_DENIED in (text or "")
    for line in (text or "").splitlines():
        parts = line.split(None, 6)
        if len(parts) == 7 and parts[1] == "Add" and parts[3].isdigit():
            found.append((int(parts[3]), parts[6].strip()))
    return found, denied


def _browse_one(service, seconds, stop, sink):
    """`dns-sd -B service local.` for at most ``seconds`` (or until ``stop`` is set), its output appended to
    ``sink`` as it comes. dns-sd buffers its output unless it writes to a terminal, so it gets a pseudo-terminal; it
    is killed at the end. False when it couldn't run."""
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
    """{service type: dns-sd's output, or None when it couldn't run}, browsing every type at once for at most
    ``seconds``. It ends early, after ``settle`` seconds (so this Mac's own services have shown on the loopback
    interface too), once another device is heard on ``interface_index``."""
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
        if time.monotonic() - began >= settle and peers_heard(outputs(), interface_index)[0]:
            stop.set()
    stop.set()
    for worker in workers:
        worker.join(3)
    return outputs()


def peers_heard(outputs, interface_index):
    """(the names of other devices heard on ``interface_index``, whether macOS refused a browse, whether any browse
    ran). This Mac's own services are left out: macOS also lists them on the loopback interface."""
    own, others, denied, ran = set(), [], False, False
    for text in outputs.values():
        if text is None:
            continue
        ran = True
        found, refused = parse_browse(text)
        denied = denied or refused
        own.update(name for index, name in found if index == LOOPBACK_INDEX)
        others += [name for index, name in found if index == interface_index]
    return sorted({name for name in others if name not in own}), denied, ran


def peer_check(*, interface=None, gateway=None, allowed=None, outputs=None, index_of=None):
    """Whether this network lets devices see each other: {"state", "interface", "peers"}. ``state``:

    - "ok": another device answered on this Mac's network;
    - "blocked": the network is up and macOS lets Mobster use it, but no other device is heard: guest, hotel or
      campus Wi-Fi that keeps devices apart, or Bonjour blocked;
    - "no_permission": macOS's Local Network privacy refuses Mobster;
    - "vpn": a VPN carries this Mac's traffic, so the iPhone and this Mac may not meet;
    - "offline": no network;
    - "unknown": it couldn't be told (dns-sd missing, devices off in this process).

    The keyword arguments replace each read in tests. Under MOBSTER_NO_DEVICES=1 (tests, local CI) it reads nothing
    itself: "unknown" unless every read is given."""
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
