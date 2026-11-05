import errno
import os
import plistlib
import socket
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mobile_agent.tests.test_fleet import PRO
from mobile_agent.tests.wireless_fixtures import TUNNEL_IFCONFIG, VPN_IFCONFIG, tunnel_reader
from mobile_agent.wireless import (
    exposure,
    network,
    pair,
)
from mobile_agent.wireless.relay import NOT_AVAILABLE


def pkg_config_flags():
    import shutil

    tool = shutil.which("pkg-config") or next(
        (
            path
            for path in ("/opt/homebrew/bin/pkg-config", "/usr/local/bin/pkg-config")
            if os.access(path, os.X_OK)
        ),
        None,
    )
    if not tool or not pair.SOURCE.is_file():
        return None
    try:
        return subprocess.run(
            [tool, "--cflags", "--libs", *pair.LIBRARIES],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        ).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return None


class BuildTests(unittest.TestCase):
    def test_it_compiles_and_refuses_bad_arguments(self):
        flags = pkg_config_flags()
        if flags is None:
            self.skipTest(
                "libimobiledevice isn't installed here (brew install libimobiledevice pkg-config)"
            )
        folder = Path(tempfile.mkdtemp(prefix="mobster-wifi-pair-build-"))
        binary = folder / "mobster-wifi-pair"
        try:
            built = subprocess.run(
                [
                    "cc",
                    "-std=c11",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-O2",
                    str(pair.SOURCE),
                    "-o",
                    str(binary),
                    *flags,
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as error:
            self.skipTest(f"no C compiler here: {error}")
        self.assertEqual(built.returncode, 0, built.stderr)
        for argv in (
            [],
            ["--enable"],
            ["--udid", "not-a-udid", "--enable"],
            ["--udid", PRO],
            ["--udid", PRO, "--enable", "--disable"],
            ["--udid", PRO, "--unlock"],
        ):
            with self.subTest(argv=argv):
                ran = subprocess.run(
                    [str(binary), *argv], capture_output=True, text=True, timeout=10
                )
                self.assertEqual(ran.returncode, 2)
                self.assertIn("usage: mobster-wifi-pair", ran.stderr)
                self.assertEqual(ran.stdout, "")


HOTSPOT = "172.20.10.1"


class FakeSocket:
    def __init__(self, reply):
        self.reply, self.sent = reply, b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def settimeout(self, _seconds):
        pass

    def sendall(self, data):
        self.sent += data

    def recv(self, _size):
        return self.reply


def connecting(answers):
    def connect(address, timeout=None):
        if address not in answers:
            raise ConnectionRefusedError
        return FakeSocket(answers[address])

    return connect


class Speaks(unittest.TestCase):
    def test_webdriveragent_is_known_by_what_it_says(self):
        for reply in (
            b"HTTP/1.0 200 OK\r\nServer: WDA MJPEG Server\r\n",
            b'{"value":{"sessionId":"x"}}',
            b'{"value":{"error":"Local clients only"}}',
        ):
            with self.subTest(reply=reply):
                self.assertTrue(
                    exposure.speaks_wda(
                        HOTSPOT, 9100, connect=connecting({(HOTSPOT, 9100): reply})
                    )
                )

    def test_a_router_page_a_refusal_and_silence_are_not(self):
        for answers in (
            {(HOTSPOT, 9100): b"HTTP/1.1 200 OK\r\nServer: lighttpd\r\n"},
            {(HOTSPOT, 9100): b""},
            {},
        ):
            with self.subTest(answers=answers):
                self.assertFalse(
                    exposure.speaks_wda(HOTSPOT, 9100, connect=connecting(answers))
                )

    def test_it_asks_with_the_local_host_header_and_only_reads(self):
        sock = FakeSocket(b"WDA MJPEG Server")
        exposure.speaks_wda(HOTSPOT, 9100, connect=lambda *a, **k: sock)
        self.assertTrue(
            sock.sent.startswith(b"GET /status HTTP/1.0\r\nHost: 127.0.0.1:9100")
        )


class Exposed(unittest.TestCase):
    def check(
        self,
        *,
        names=("Sams-iPhone.local",),
        addresses=(),
        gateway=None,
        accepting=(),
        speaking=(),
    ):
        return exposure.exposed(
            names,
            resolve=lambda hosts: {"ipv4": list(addresses), "ipv6": []},
            gateway=lambda: gateway,
            accepts=lambda host, port: (host, port) in accepting,
            speaks=lambda host, port: (host, port) in speaking,
        )

    def test_a_port_open_on_the_phones_own_address_counts_whatever_it_says(self):
        addresses, hits = self.check(
            addresses=("192.168.1.20",), accepting={("192.168.1.20", 8100)}
        )
        self.assertEqual((addresses, hits), (["192.168.1.20"], ["192.168.1.20:8100"]))

    def test_the_hotspot_router_counts_when_it_is_the_helper(self):
        _addresses, hits = self.check(
            names=(), gateway=HOTSPOT, speaking={(HOTSPOT, 9100)}
        )
        self.assertEqual(hits, [f"{HOTSPOT}:9100"])

    def test_it_still_acts_when_nothing_answers(self):
        self.assertEqual(
            self.check(addresses=("192.168.1.20",), gateway="192.168.1.1")[1], []
        )
        self.assertEqual(self.check(names=(), gateway=HOTSPOT)[1], [])


class LocalNetworkTests(unittest.TestCase):
    def knock(self, outcome):
        calls = []

        def connect(address, timeout):
            calls.append((address, timeout))
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        return connect, calls

    def test_macos_refusing_the_route_means_not_allowed(self):
        for code in (errno.EHOSTUNREACH, errno.EPERM, errno.EACCES):
            connect, _ = self.knock(OSError(code, "No route to host"))
            with self.subTest(code=code):
                self.assertIs(
                    network.local_network_allowed("192.168.1.1", connect=connect), False
                )


def addrinfo(*addresses):
    rows = []
    for address in addresses:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        sockaddr = (address, 0, 0, 0) if family == socket.AF_INET6 else (address, 0)
        rows.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
    return rows


class ResolveTests(unittest.TestCase):
    def test_wifi_addresses_without_the_tunnels(self):
        answers = {
            "Sams-iPhone.local": addrinfo(
                "192.168.1.40",
                "fe80::18c2:aa1:9b2e:4d01%en0",
                "fd7a:1c2b::1",
                "2001:db8::40",
            ),
            "Sams-iPhone-2.local": addrinfo("192.168.1.40", "fdab::7"),
        }
        found = network.resolve(list(answers), lookup=lambda name, *a: answers[name])
        self.assertEqual(
            found,
            {
                "ipv4": ["192.168.1.40"],
                "ipv6": ["fe80::18c2:aa1:9b2e:4d01%en0", "2001:db8::40"],
            },
        )


def browse_output(*rows):
    lines = [
        "Browsing for _airplay._tcp.local.",
        "DATE: ---Wed 07 Oct 2026---",
        "21:52:48.769  ...STARTING...",
        "Timestamp     A/R    Flags  if Domain               Service Type         Instance Name",
    ]
    for index, name in rows:
        lines.append(
            f"21:52:48.771  Add        3  {index:>2} local.               _airplay._tcp.       {name}"
        )
    return "\n".join(lines) + "\n"


MINE = "Sam’s MacBook Pro"


EN0 = 19


class PeerTests(unittest.TestCase):
    def check(self, outputs, **kwargs):
        kwargs = {
            "interface": "en0",
            "gateway": "192.168.1.1",
            "allowed": True,
            "index_of": lambda name: EN0,
            **kwargs,
        }
        return network.peer_check(outputs=outputs, **kwargs)

    def test_only_this_mac_and_airdrop_means_blocked(self):
        guest = {
            "_airplay._tcp": browse_output(
                (EN0, MINE), (1, MINE), (36, "Someone’s iPhone")
            ),
            "_companion-link._tcp": browse_output((1, MINE), (EN0, MINE)),
            "_hap._tcp": browse_output(),
        }
        self.assertEqual(
            self.check(guest), {"state": "blocked", "interface": "en0", "peers": 0}
        )
        self.assertEqual(
            self.check({"_airplay._tcp": browse_output()})["state"], "blocked"
        )

    def test_macos_refusing_the_browse_or_the_gateway_is_permission_not_the_network(
        self,
    ):
        refused = {"_airplay._tcp": "DNSServiceBrowse failed -65570 (PolicyDenied)\n"}
        self.assertEqual(self.check(refused)["state"], "no_permission")
        self.assertEqual(
            self.check({"_airplay._tcp": browse_output()}, allowed=False)["state"],
            "no_permission",
        )

    def test_under_no_devices_it_reads_nothing_itself(self):
        reads = []
        with (
            patch.dict("os.environ", {"MOBSTER_NO_DEVICES": "1"}),
            patch.object(
                network, "read_output", lambda argv, timeout=3: reads.append(argv)
            ),
            patch.object(network, "browse", lambda *a, **k: reads.append("browse")),
        ):
            self.assertEqual(network.peer_check()["state"], "unknown")
        self.assertEqual(reads, [])


@unittest.skipUnless(Path("/bin/sh").exists(), "no shell")
class BrowseTests(unittest.TestCase):
    def fake(self, body):
        import os
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

    def test_nothing_heard_waits_the_whole_browse_then_stops_dns_sd(self):
        self.fake("printf 'Browsing for _x._tcp.local.\\n'\nexec sleep 30\n")
        began = time.monotonic()
        outputs = network.browse(19, types=("_airplay._tcp",), seconds=0.8, settle=0.2)
        self.assertLess(time.monotonic() - began, 4)
        self.assertEqual(network.peers_heard(outputs, 19), ([], False, True))


class InfoPlistTests(unittest.TestCase):
    PLIST = (
        Path(network.__file__).resolve().parents[2]
        / "desktop"
        / "src-tauri"
        / "Info.plist"
    )

    @unittest.skipUnless(
        PLIST.is_file(), "the Mac app's files are not part of this copy"
    )
    def test_the_mac_app_declares_every_type_it_browses(self):
        with open(self.PLIST, "rb") as stream:
            declared = plistlib.load(stream)["NSBonjourServices"]
        self.assertEqual(sorted(set(network.PEER_TYPES) - set(declared)), [])
        self.assertIn("_remotepairing._tcp", declared)


class TunnelRouteTests(unittest.TestCase):
    def test_the_tunnel_is_the_utun_holding_this_macs_end_of_the_link(self):
        seen = []
        self.assertEqual(
            network.tunnel_route("FD7A:1C2B:3D4E:0::1", tunnel_reader(seen=seen)),
            ("utun4", None),
        )
        self.assertEqual(
            seen,
            [
                [network.ROUTE, "-n", "get", "-inet6", "fd7a:1c2b:3d4e::1"],
                [network.IFCONFIG, "-v", "utun4"],
            ],
        )
        self.assertTrue(network.through_tunnel("fd7a:1c2b:3d4e::1", tunnel_reader()))
        point_to_point = TUNNEL_IFCONFIG.replace(
            "fd7a:1c2b:3d4e::2 prefixlen 64",
            "fd99:1::2 --> fd7a:1c2b:3d4e::1 prefixlen 128",
        )
        self.assertTrue(
            network.through_tunnel(
                "fd7a:1c2b:3d4e::1", tunnel_reader(ifconfig=point_to_point)
            )
        )

    def test_wifi_ethernet_no_route_or_not_an_address_is_not_the_tunnel(self):
        for interface in ("en0", "en7", "bridge100", "awdl0", "lo0"):
            with self.subTest(interface=interface):
                self.assertEqual(
                    network.tunnel_route(
                        "fd00::1",
                        tunnel_reader(interface=interface),
                    ),
                    (interface, f"this Mac reaches fd00::1 through {interface}, not a tunnel (utun)"),
                )
        self.assertFalse(network.through_tunnel("fd00::1", lambda argv: None))
        for address in ("not an address", "192.168.1.20", None):
            with self.subTest(address=address):
                self.assertFalse(
                    network.through_tunnel(address, lambda argv: self.fail("asked route"))
                )

    def test_a_vpn_is_never_the_tunnel_even_when_its_route_covers_the_address(self):
        refused = {
            "the VPN marks": tunnel_reader(interface="utun5", ifconfig=VPN_IFCONFIG),
            "a NetworkExtension VPN agent alone": tunnel_reader(
                interface="utun5",
                ifconfig=VPN_IFCONFIG.replace("IS_VPN,", ""),
            ),
            "only a default route": tunnel_reader(
                destination="default", mask="default"
            ),
            "a /48 that covers the address": tunnel_reader(
                ifconfig=TUNNEL_IFCONFIG.replace(
                    "fd7a:1c2b:3d4e::2 prefixlen 64", "fd7a:1c2b:3d4e::2 prefixlen 48"
                )
            ),
            "another network's address": tunnel_reader(
                ifconfig=TUNNEL_IFCONFIG.replace("fd7a:1c2b:3d4e::2", "fd7a:1c2b:9999::2")
            ),
            "a link-local address only": tunnel_reader(
                ifconfig=TUNNEL_IFCONFIG.replace(
                    "\tinet6 fd7a:1c2b:3d4e::2 prefixlen 64\n", ""
                )
            ),
            "the phone's own address": tunnel_reader(
                ifconfig=TUNNEL_IFCONFIG.replace("fd7a:1c2b:3d4e::2", "fd7a:1c2b:3d4e::1")
            ),
            "an interface it can't read": tunnel_reader(ifconfig=None),
        }
        for why, reader in refused.items():
            with self.subTest(why=why):
                interface, problem = network.tunnel_route("fd7a:1c2b:3d4e::1", reader)
                self.assertIsNotNone(problem)
                self.assertFalse(network.through_tunnel("fd7a:1c2b:3d4e::1", reader))
        self.assertEqual(
            network.tunnel_route(
                "fd7a:1c2b:3d4e::1", tunnel_reader(interface="utun5", ifconfig=VPN_IFCONFIG)
            ),
            ("utun5", "this Mac reaches fd7a:1c2b:3d4e::1 through utun5, which is a VPN, not the tunnel"),
        )
        self.assertEqual(
            network.tunnel_route("fd7a:1c2b:3d4e::1", refused["a /48 that covers the address"]),
            (
                "utun4",
                "utun4 holds no address on a link to fd7a:1c2b:3d4e::1 (fd7a:1c2b:3d4e::2/48), so it isn't "
                "the tunnel to the phone",
            ),
        )

    def test_require_tunnel_refuses_a_lan_ula(self):
        from mobile_agent.wireless.relay import require_tunnel

        self.assertEqual(
            require_tunnel("FD7A:1C2B:3D4E::1", lambda address: True),
            "fd7a:1c2b:3d4e::1",
        )
        with self.assertRaises(ValueError) as refused:
            require_tunnel("fd7a:1c2b:3d4e::1", lambda address: False)
        self.assertEqual(str(refused.exception), NOT_AVAILABLE)
        with self.assertRaises(ValueError):
            require_tunnel(
                "192.168.1.20",
                lambda address: self.fail("asked the route of a LAN address"),
            )


CHECK_MAIN = r"""
int main(int argc, const char *argv[])
{
  @autoreleasepool {
    for (int i = 1; i < argc; i++) {
      NSString *arg = [NSString stringWithUTF8String:argv[i]];
      BOOL local;
      if ([arg hasPrefix:@"host:"]) {
        NSString *host = [arg substringFromIndex:5];
        local = FBMobsterLocalHost([host isEqualToString:@"<none>"] ? nil : host);
      } else {
        local = FBMobsterLocalStreamRequest([arg dataUsingEncoding:NSISOLatin1StringEncoding]);
      }
      printf("%d\n", local ? 1 : 0);
    }
  }
  return 0;
}
"""


class LocalClientsOnlyTests(unittest.TestCase):
    """The helper's Host and Origin check (wda/patches.py), compiled and run on this Mac."""

    @classmethod
    def setUpClass(cls):
        import shutil

        from mobile_agent.wda import patches

        clang = shutil.which("clang")
        tools = subprocess.run(["xcode-select", "-p"], capture_output=True, timeout=10) if clang else None
        if tools is None or tools.returncode != 0:
            raise unittest.SkipTest("Xcode's command line tools aren't installed")
        folder = Path(tempfile.mkdtemp(prefix="mobster-local-host-"))
        cls.addClassCleanup(shutil.rmtree, folder, True)
        source, cls.binary = folder / "check.m", folder / "check"
        source.write_text(
            "#import <Foundation/Foundation.h>\n"
            + patches.WDA_LOCAL_HOST
            + patches.WDA_LOCAL_STREAM
            + CHECK_MAIN
        )
        built = subprocess.run(
            [clang, "-fobjc-arc", "-framework", "Foundation", str(source), "-o", str(cls.binary)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert built.returncode == 0, built.stderr[-800:]

    def local(self, *cases):
        done = subprocess.run(
            [str(self.binary), *cases], capture_output=True, text=True, timeout=30
        )
        return [line == "1" for line in done.stdout.splitlines()]

    def test_the_relays_requests_are_answered(self):
        self.assertEqual(
            self.local(
                "host:127.0.0.1:8100",
                "host:localhost",
                "host:LOCALHOST:9100",
                "host:[::1]:8100",
                "GET / HTTP/1.1\r\nHost: 127.0.0.1:9100\r\n\r\n",
            ),
            [True] * 5,
        )

    def test_no_host_another_host_or_a_web_page_is_refused(self):
        self.assertEqual(
            self.local(
                "host:<none>",
                "host:",
                "host:192.168.1.20:8100",
                "host:[fd7a:1c2b:3d4e::1]:8100",
                "host:127.0.0.1.example.com",
                "GET / HTTP/1.0\r\n\r\n",
                "GET / HTTP/1.1\r\nHost:\r\n\r\n",
                "GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nOrigin: http://example.com\r\n\r\n",
            ),
            [False] * 8,
        )
