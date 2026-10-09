"""Track wireless: the loopback relay (SPEC §3.7 item 4, acceptance 1). It binds only 127.0.0.1, refuses any target
but a tunnel address (fd00::/8), forwards both ways, and no code in the package listens anywhere else. Offline: a
local echo server over ::1 stands in for WebDriverAgent on the phone."""

import ipaddress
from pathlib import Path
import random
import re
import socket
import threading
import unittest
from unittest.mock import patch

from mobile_agent.wireless import relay
from mobile_agent.wireless.relay import LOOPBACK, NOT_AVAILABLE, Relay, check_target

PACKAGE = Path(relay.__file__).resolve().parent


def echo_server():
    """An echo server on [::1]:<free port> (WebDriverAgent's stand-in); returns (port, stop)."""
    server = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    server.bind(("::1", 0))
    server.listen(8)
    stopped = threading.Event()

    def serve():
        while not stopped.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                return

            def echo(conn=conn):
                with conn:
                    while True:
                        data = conn.recv(65536)
                        if not data:
                            return
                        conn.sendall(b"echo:" + data)
            threading.Thread(target=echo, daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()

    def stop():
        stopped.set()
        server.close()
    return server.getsockname()[1], stop


def ipv6_available():
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(("::1", 0))
        return True
    except OSError:
        return False


class TargetTests(unittest.TestCase):
    def test_only_a_tunnel_address_is_a_target(self):
        for address in ("fd00::1", "fdab:cdef:1234::1", "FD12:3456:789A::2", "[fd00::5]"):
            with self.subTest(address=address):
                self.assertEqual(check_target(address), ipaddress.IPv6Address(address.strip("[]")).compressed)
        for address in ("", " ", None, 8100, "0.0.0.0", "::", "::1", "127.0.0.1", "192.168.1.20", "10.0.0.7",
                        "172.16.4.2", "169.254.3.1", "100.64.0.1", "8.8.8.8", "fe80::1", "fe80::1%en0", "fd00::1%utun3",
                        "2001:db8::1", "fc00::1", "fe00::1", "ff02::1", "::ffff:192.168.1.20", "localhost",
                        "Sams-iPhone.local", "[fd00::1]:8100", "not an address"):
            with self.subTest(address=address), self.assertRaises(ValueError) as refused:
                check_target(address)
            self.assertEqual(str(refused.exception), NOT_AVAILABLE)

    def test_random_addresses_outside_fd00_are_always_refused(self):
        rng = random.Random(7)
        for _ in range(500):
            if rng.random() < .5:
                address = str(ipaddress.IPv4Address(rng.getrandbits(32)))
            else:
                value = rng.getrandbits(128)
                if value >> 120 == 0xfd:
                    value ^= 1 << 127
                address = str(ipaddress.IPv6Address(value))
            with self.subTest(address=address), self.assertRaises(ValueError):
                check_target(address)

    def test_a_relay_never_takes_a_lan_target(self):
        for address in ("192.168.1.20", "0.0.0.0", "::", "fe80::1"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                Relay(address, [(0, 8100)])


@unittest.skipUnless(ipv6_available(), "no IPv6 loopback here")
class ForwardingTests(unittest.TestCase):
    def setUp(self):
        self.port, stop = echo_server()
        self.addCleanup(stop)
        # The echo server stands in for the phone's tunnel address: ::1 is let through for this test only.
        allow = patch.object(relay, "check_target", lambda address: "::1")
        allow.start()
        self.addCleanup(allow.stop)

    def relay(self, pairs=None):
        forwarder = Relay("::1", pairs or [(0, self.port)], name="test")
        forwarder.start()
        self.addCleanup(forwarder.stop)
        return forwarder

    def test_it_forwards_both_ways_and_binds_loopback_only(self):
        forwarder = self.relay()
        self.assertTrue(forwarder.running)
        for listener, _ in forwarder.listeners:
            self.assertEqual(listener.getsockname()[0], LOOPBACK)
            self.assertEqual(listener.family, socket.AF_INET)
        with socket.create_connection((LOOPBACK, forwarder.ports[0]), timeout=5) as client:
            client.sendall(b"GET /status")
            self.assertEqual(client.recv(100), b"echo:GET /status")
            client.sendall(b"again")
            self.assertEqual(client.recv(100), b"echo:again")

    def test_two_ports_and_many_connections(self):
        forwarder = self.relay([(0, self.port), (0, self.port)])
        api, video = forwarder.ports
        self.assertNotEqual(api, video)
        clients = [socket.create_connection((LOOPBACK, port), timeout=5) for port in (api, video) * 5]
        try:
            for index, client in enumerate(clients):
                client.sendall(f"n{index}".encode())
            for index, client in enumerate(clients):
                self.assertEqual(client.recv(100), f"echo:n{index}".encode())
        finally:
            for client in clients:
                client.close()

    def test_stop_closes_the_ports_and_live_connections(self):
        forwarder = self.relay()
        port = forwarder.ports[0]
        client = socket.create_connection((LOOPBACK, port), timeout=5)
        client.sendall(b"x")
        self.assertEqual(client.recv(10), b"echo:x")
        forwarder.stop()
        self.assertFalse(forwarder.running)
        client.settimeout(5)
        self.assertEqual(client.recv(10), b"")   # the relay closed it
        client.close()
        with self.assertRaises(OSError):
            socket.create_connection((LOOPBACK, port), timeout=1).close()

    def test_a_port_in_use_leaves_nothing_open(self):
        taken = socket.socket()
        taken.bind((LOOPBACK, 0))
        taken.listen(1)
        self.addCleanup(taken.close)
        forwarder = Relay("::1", [(0, self.port), (taken.getsockname()[1], self.port)])
        with self.assertRaises(OSError):
            forwarder.start()
        self.assertFalse(forwarder.running)
        self.assertEqual(forwarder.listeners, [])

    def test_a_target_that_is_not_there_closes_the_client(self):
        forwarder = Relay("::1", [(0, 1)], connect_timeout=1).start()   # nothing listens on port 1
        self.addCleanup(forwarder.stop)
        with socket.create_connection((LOOPBACK, forwarder.ports[0]), timeout=5) as client:
            client.settimeout(5)
            self.assertEqual(client.recv(10), b"")
        self.assertGreaterEqual(forwarder.failures, 1)


class NoOtherListenerTests(unittest.TestCase):
    def test_every_bind_in_the_package_is_loopback(self):
        """Acceptance 1: no code path in wireless/ opens a listener on anything but 127.0.0.1."""
        binds = []
        for path in sorted(PACKAGE.glob("*.py")):
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if re.search(r"\.bind\(", line):
                    binds.append((path.name, number, line.strip()))
        self.assertEqual([name for name, _, _ in binds], ["relay.py"])
        for name, number, line in binds:
            self.assertIn("LOOPBACK", line, f"{name}:{number} binds something else: {line}")
        self.assertEqual(LOOPBACK, "127.0.0.1")
        for path in sorted(PACKAGE.glob("*.py")):
            if path.name == "relay.py":
                continue
            with self.subTest(path=path.name):
                self.assertNotRegex(path.read_text(), r"\.listen\(|create_server|HTTPServer|socketserver")


if __name__ == "__main__":
    unittest.main()
