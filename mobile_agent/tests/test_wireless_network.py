"""Track wireless: reading this Mac's network without changing it (SPEC §3.7 items 6 and 8). Network changes from
`scutil --nwi`, a VPN, the default gateway, the Local Network permission knock, and the phone's Wi-Fi addresses for
the probe's LAN check. Offline: every read is a fake; nothing listens and nothing reaches a phone."""

import errno
from pathlib import Path
import socket
import threading
import unittest

from mobile_agent.wireless import network

HOME_WIFI = """Network information

IPv4 network interface information
     en0 : flags      : 0x5 (IPv4,DNS)
           address    : 192.168.1.23
           reach      : 0x00000002 (Reachable)

   REACH : flags 0x00000002 (Reachable)

IPv6 network interface information
     en0 : flags      : 0x6 (IPv6,DNS)
           address    : fe80::1c2b:3d4e:5f60:7182
           reach      : 0x00000002 (Reachable)

   REACH : flags 0x00000002 (Reachable)

Network interfaces: en0
"""

WITH_VPN = HOME_WIFI.replace("""IPv4 network interface information
""", """IPv4 network interface information
   utun4 : flags      : 0x5 (IPv4,DNS)
           address    : 10.8.0.6
           VPN server : 203.0.113.9
           reach      : 0x00000003 (Reachable,Transient Connection)
""").replace("Network interfaces: en0", "Network interfaces: utun4 en0")

ROUTE = """   route to: default
destination: default
       mask: default
    gateway: 192.168.1.1
  interface: en0
      flags: <UP,GATEWAY,DONE,STATIC,PRCLONING,GLOBAL>
"""


class NetworkChangeTests(unittest.TestCase):
    def test_the_signature_changes_with_an_address_or_an_interface_and_only_then(self):
        same = network.signature(HOME_WIFI)
        self.assertEqual(len(same), 16)
        self.assertEqual(network.signature(HOME_WIFI.replace("     en0", "  en0").replace("\n\n", "\n")), same)
        self.assertNotEqual(network.signature(HOME_WIFI.replace("192.168.1.23", "192.168.1.24")), same)
        self.assertNotEqual(network.signature(WITH_VPN), same)
        self.assertIsNone(network.signature(None))

    def test_nwi_reads_scutil_once_and_returns_none_when_it_fails(self):
        seen = []
        self.assertEqual(network.nwi(lambda argv: seen.append(argv) or HOME_WIFI), HOME_WIFI)
        self.assertEqual(seen, [["/usr/sbin/scutil", "--nwi"]])
        self.assertIsNone(network.nwi(lambda argv: None))


class VpnTests(unittest.TestCase):
    def test_the_ipv4_interfaces_in_order(self):
        self.assertEqual(network.ipv4_interfaces(HOME_WIFI), ["en0"])
        self.assertEqual(network.ipv4_interfaces(WITH_VPN), ["utun4", "en0"])
        self.assertEqual(network.ipv4_interfaces(""), [])
        self.assertEqual(network.ipv4_interfaces(None), [])

    def test_a_vpn_carrying_traffic_is_named(self):
        self.assertIsNone(network.vpn_interface(HOME_WIFI))
        self.assertEqual(network.vpn_interface(WITH_VPN), "utun4")
        for name in ("ipsec0", "ppp0", "wg0", "tun1"):
            with self.subTest(name=name):
                self.assertEqual(network.vpn_interface(HOME_WIFI.replace("     en0 : flags      : 0x5",
                                                                         f"     {name} : flags      : 0x5", 1)),
                                 name)

    def test_ipv6_only_interfaces_are_not_a_vpn(self):
        text = HOME_WIFI.replace("IPv6 network interface information\n",
                                 "IPv6 network interface information\n   utun2 : flags      : 0x6 (IPv6,DNS)\n")
        self.assertIsNone(network.vpn_interface(text))


class GatewayTests(unittest.TestCase):
    def test_the_default_gateway(self):
        seen = []
        self.assertEqual(network.default_gateway(lambda argv: seen.append(argv) or ROUTE), "192.168.1.1")
        self.assertEqual(seen, [["/sbin/route", "-n", "get", "default"]])
        for text in (None, "", "gateway: fe80::1%en0\n", "gateway: link#14\n", ROUTE.replace("gateway", "nexthop")):
            with self.subTest(text=text):
                self.assertIsNone(network.default_gateway(lambda argv, text=text: text))


class LocalNetworkTests(unittest.TestCase):
    def knock(self, outcome):
        calls = []

        def connect(address, timeout):
            calls.append((address, timeout))
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return connect, calls

    def test_any_answer_from_the_gateway_means_allowed(self):
        class Sock:
            def close(self):
                pass
        for outcome in (Sock(), ConnectionRefusedError(), socket.timeout(), TimeoutError()):
            connect, calls = self.knock(outcome)
            with self.subTest(outcome=type(outcome).__name__):
                self.assertIs(network.local_network_allowed("192.168.1.1", connect=connect), True)
                self.assertEqual(calls, [(("192.168.1.1", 9), 0.6)])

    def test_macos_refusing_the_route_means_not_allowed(self):
        for code in (errno.EHOSTUNREACH, errno.EPERM, errno.EACCES):
            connect, _ = self.knock(OSError(code, "No route to host"))
            with self.subTest(code=code):
                self.assertIs(network.local_network_allowed("192.168.1.1", connect=connect), False)

    def test_anything_else_cant_be_told(self):
        connect, _ = self.knock(OSError(errno.ENETDOWN, "Network is down"))
        self.assertIsNone(network.local_network_allowed("192.168.1.1", connect=connect))
        self.assertIsNone(network.local_network_allowed(None, connect=lambda *a: self.fail("knocked")))


def addrinfo(*addresses):
    rows = []
    for address in addresses:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        sockaddr = (address, 0, 0, 0) if family == socket.AF_INET6 else (address, 0)
        rows.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
    return rows


class ResolveTests(unittest.TestCase):
    def test_wifi_addresses_without_the_tunnels(self):
        answers = {"Sams-iPhone.local": addrinfo("192.168.1.40", "fe80::18c2:aa1:9b2e:4d01%en0", "fd7a:1c2b::1",
                                                 "2001:db8::40"),
                   "Sams-iPhone-2.local": addrinfo("192.168.1.40", "fdab::7")}
        found = network.resolve(list(answers), lookup=lambda name, *a: answers[name])
        self.assertEqual(found, {"ipv4": ["192.168.1.40"], "ipv6": ["fe80::18c2:aa1:9b2e:4d01%en0", "2001:db8::40"]})

    def test_a_name_that_doesnt_resolve_or_hangs_is_skipped_in_time(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def lookup(name, *a):
            if name == "gone.local":
                raise socket.gaierror("nodename nor servname provided")
            if name == "slow.local":
                release.wait(5)
                return addrinfo("192.168.1.99")
            return addrinfo("192.168.1.40")
        found = network.resolve(["gone.local", "slow.local", "Sams-iPhone.local"], timeout=0.2, lookup=lookup)
        self.assertEqual(found, {"ipv4": ["192.168.1.40"], "ipv6": []})


class PortTests(unittest.TestCase):
    def test_port_open(self):
        class Sock:
            def close(self):
                pass
        self.assertTrue(network.port_open("192.168.1.40", 8100, connect=lambda address, timeout: Sock()))

        def refuse(address, timeout):
            raise ConnectionRefusedError()
        self.assertFalse(network.port_open("192.168.1.40", 8100, connect=refuse))

    def test_this_module_never_listens(self):
        source = Path(network.__file__).read_text()
        self.assertNotRegex(source, r"\.bind\(|\.listen\(|create_server")



# `dns-sd -B` as macOS prints it: this Mac's own services show on every interface, the loopback (1) included; other
# devices show only on the interface that heard them (19 here, en0), and AirDrop's peer-to-peer link has its own (36).
def browse_output(*rows):
    lines = ["Browsing for _airplay._tcp.local.", "DATE: ---Wed 07 Oct 2026---", "21:52:48.769  ...STARTING...",
             "Timestamp     A/R    Flags  if Domain               Service Type         Instance Name"]
    for index, name in rows:
        lines.append(f"21:52:48.771  Add        3  {index:>2} local.               _airplay._tcp.       {name}")
    return "\n".join(lines) + "\n"


MINE = "Sam’s MacBook Pro"
EN0 = 19


class PeerTests(unittest.TestCase):
    """Guest, hotel and campus Wi-Fi often keep devices apart (client isolation) and drop Bonjour: peer_check says
    "blocked" when the network is up and allowed but no other device is heard on this Mac's interface."""

    def check(self, outputs, **kwargs):
        kwargs = {"interface": "en0", "gateway": "192.168.1.1", "allowed": True, "index_of": lambda name: EN0,
                  **kwargs}
        return network.peer_check(outputs=outputs, **kwargs)

    def test_the_browse_lines(self):
        found, denied = network.parse_browse(browse_output((EN0, MINE), (1, MINE), (EN0, "Living Room TV")))
        self.assertEqual(found, [(EN0, MINE), (1, MINE), (EN0, "Living Room TV")])
        self.assertFalse(denied)
        self.assertEqual(network.parse_browse(""), ([], False))
        self.assertEqual(network.parse_browse(None), ([], False))
        self.assertTrue(network.parse_browse("DNSServiceBrowse failed -65570\n")[1])

    def test_another_device_heard_means_ok(self):
        home = {"_airplay._tcp": browse_output((EN0, MINE), (1, MINE), (EN0, "Living Room TV")),
                "_raop._tcp": browse_output((1, MINE), (EN0, MINE)), "_hap._tcp": None}
        self.assertEqual(self.check(home), {"state": "ok", "interface": "en0", "peers": 1})

    def test_only_this_mac_and_airdrop_means_blocked(self):
        """This Mac's own services (also on the loopback) and AirDrop's neighbours (another interface) don't count:
        on a guest network that keeps devices apart, that's all there is."""
        guest = {"_airplay._tcp": browse_output((EN0, MINE), (1, MINE), (36, "Someone’s iPhone")),
                 "_companion-link._tcp": browse_output((1, MINE), (EN0, MINE)), "_hap._tcp": browse_output()}
        self.assertEqual(self.check(guest), {"state": "blocked", "interface": "en0", "peers": 0})
        self.assertEqual(self.check({"_airplay._tcp": browse_output()})["state"], "blocked")

    def test_macos_refusing_the_browse_or_the_gateway_is_permission_not_the_network(self):
        refused = {"_airplay._tcp": "DNSServiceBrowse failed -65570 (PolicyDenied)\n"}
        self.assertEqual(self.check(refused)["state"], "no_permission")
        self.assertEqual(self.check({"_airplay._tcp": browse_output()}, allowed=False)["state"], "no_permission")

    def test_what_cant_be_told(self):
        self.assertEqual(self.check({"_airplay._tcp": None, "_raop._tcp": None})["state"], "unknown")
        self.assertEqual(self.check({}, interface="")["state"], "offline")
        self.assertEqual(self.check({}, interface="utun4")["state"], "vpn")
        self.assertEqual(self.check({}, interface="ipsec0")["state"], "vpn")
        self.assertEqual(self.check({}, interface="awdl0")["state"], "unknown")

        def gone(name):
            raise OSError("no such interface")
        self.assertEqual(self.check({"_airplay._tcp": browse_output()}, index_of=gone)["state"], "unknown")

    def test_under_no_devices_it_reads_nothing_itself(self):
        reads = []
        from unittest.mock import patch
        with patch.dict("os.environ", {"MOBSTER_NO_DEVICES": "1"}), \
                patch.object(network, "_output", lambda argv, timeout=3: reads.append(argv)), \
                patch.object(network, "browse", lambda *a, **k: reads.append("browse")):
            self.assertEqual(network.peer_check()["state"], "unknown")
        self.assertEqual(reads, [])


@unittest.skipUnless(Path("/bin/sh").exists(), "no shell")
class BrowseTests(unittest.TestCase):
    """browse() runs dns-sd under a pseudo-terminal (it buffers its output otherwise) and stops once a device is
    heard. A stand-in dns-sd prints what macOS would and then waits, as the real one does."""

    def fake(self, body):
        import os
        import stat
        import tempfile
        folder = Path(tempfile.mkdtemp(prefix="mobster-dns-sd-"))
        path = folder / "dns-sd"
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        from unittest.mock import patch
        item = patch.object(network, "DNS_SD", str(path))
        item.start()
        self.addCleanup(item.stop)
        self.assertTrue(os.access(path, os.X_OK))

    def test_a_device_heard_ends_the_browse_early(self):
        import time
        self.fake("printf 'Timestamp A/R Flags if Domain Service Type Instance Name\\n'\n"
                  "printf '21:52:48.771  Add        3   1 local.  _airplay._tcp.  Sam\\n'\n"
                  "printf '21:52:48.771  Add        2  19 local.  _airplay._tcp.  Living Room TV\\n'\n"
                  "exec sleep 30\n")
        began = time.monotonic()
        outputs = network.browse(19, types=("_airplay._tcp", "_raop._tcp"), seconds=8, settle=0.3)
        self.assertLess(time.monotonic() - began, 5)
        self.assertIn("Living Room TV", outputs["_airplay._tcp"])
        self.assertEqual(network.peers_heard(outputs, 19)[0], ["Living Room TV"])

    def test_nothing_heard_waits_the_whole_browse_then_stops_dns_sd(self):
        import time
        self.fake("printf 'Browsing for _x._tcp.local.\\n'\nexec sleep 30\n")
        began = time.monotonic()
        outputs = network.browse(19, types=("_airplay._tcp",), seconds=0.8, settle=0.2)
        self.assertLess(time.monotonic() - began, 4)
        self.assertEqual(network.peers_heard(outputs, 19), ([], False, True))

    def test_no_dns_sd_reads_as_nothing_ran(self):
        from unittest.mock import patch
        with patch.object(network, "DNS_SD", "/nonexistent/dns-sd"):
            outputs = network.browse(19, types=("_airplay._tcp",), seconds=0.5)
        self.assertEqual(outputs, {"_airplay._tcp": None})


class InfoPlistTests(unittest.TestCase):
    PLIST = Path(network.__file__).resolve().parents[2] / "desktop" / "src-tauri" / "Info.plist"

    @unittest.skipUnless(PLIST.is_file(), "the Mac app's files are not part of this copy")
    def test_the_mac_app_declares_every_type_it_browses(self):
        import plistlib
        with open(self.PLIST, "rb") as stream:
            declared = plistlib.load(stream)["NSBonjourServices"]
        self.assertEqual(sorted(set(network.PEER_TYPES) - set(declared)), [])
        self.assertIn("_remotepairing._tcp", declared)


class TunnelRouteTests(unittest.TestCase):
    """A phone's end of the CoreDevice tunnel is reached through a utun interface. A home network's own fd00::/8
    address is reached through Wi-Fi or Ethernet, and must never be where WebDriverAgent listens."""

    ROUTE = """   route to: fd7a:1c2b:3d4e::1
destination: fd7a:1c2b:3d4e::
       mask: ffff:ffff:ffff:ffff::
  interface: {interface}
      flags: <UP,DONE,STATIC,PRCLONING>
"""

    def test_the_interface_of_the_route(self):
        seen = []

        def reader(argv):
            seen.append(argv)
            return self.ROUTE.format(interface="utun4")
        self.assertEqual(network.route_interface("FD7A:1C2B:3D4E:0::1", reader), "utun4")
        self.assertEqual(seen, [[network.ROUTE, "-n", "get", "-inet6", "fd7a:1c2b:3d4e::1"]])
        self.assertTrue(network.through_tunnel("fd7a:1c2b:3d4e::1", lambda argv: self.ROUTE.format(interface="utun4")))

    def test_wifi_ethernet_no_route_or_not_an_address_is_not_the_tunnel(self):
        for interface in ("en0", "en7", "bridge100", "awdl0", "lo0"):
            with self.subTest(interface=interface):
                self.assertFalse(network.through_tunnel("fd00::1", lambda argv: self.ROUTE.format(interface=interface)))
        self.assertFalse(network.through_tunnel("fd00::1", lambda argv: None))
        self.assertIsNone(network.route_interface("not an address", lambda argv: self.fail("asked route")))
        self.assertIsNone(network.route_interface("192.168.1.20", lambda argv: self.fail("asked route")))

    def test_require_tunnel_refuses_a_lan_ula(self):
        from mobile_agent.wireless.relay import NOT_AVAILABLE, require_tunnel
        self.assertEqual(require_tunnel("FD7A:1C2B:3D4E::1", lambda address: True), "fd7a:1c2b:3d4e::1")
        with self.assertRaises(ValueError) as refused:
            require_tunnel("fd7a:1c2b:3d4e::1", lambda address: False)
        self.assertEqual(str(refused.exception), NOT_AVAILABLE)
        with self.assertRaises(ValueError):
            require_tunnel("192.168.1.20", lambda address: self.fail("asked the route of a LAN address"))

if __name__ == "__main__":
    unittest.main()
