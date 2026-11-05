import ipaddress
import json
import os
import re
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mobile_agent import (
    lockscreen,
    wireless,
)
from mobile_agent.agent_hooks import READY
from mobile_agent.devices import DeviceStore
from mobile_agent.tests.test_fleet import PRO, XR
from mobile_agent.tests.wireless_fixtures import PACKAGE, TUNNEL, Runner
from mobile_agent.wireless import monitor as monitor_module
from mobile_agent.wireless import (
    relay,
    store,
    transport,
)
from mobile_agent.wireless.devicectl import DevicectlError, Peer
from mobile_agent.wireless.monitor import (
    LOST_AFTER,
    NETWORK_EVERY,
    Monitor,
)
from mobile_agent.wireless.relay import LOOPBACK, NOT_AVAILABLE, Relay, check_target


def echo_server():
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
        for address in (
            "fd00::1",
            "fdab:cdef:1234::1",
            "FD12:3456:789A::2",
            "[fd00::5]",
        ):
            with self.subTest(address=address):
                self.assertEqual(
                    check_target(address),
                    ipaddress.IPv6Address(address.strip("[]")).compressed,
                )
        for address in (
            "",
            " ",
            None,
            8100,
            "0.0.0.0",
            "::",
            "::1",
            "127.0.0.1",
            "192.168.1.20",
            "10.0.0.7",
            "172.16.4.2",
            "169.254.3.1",
            "100.64.0.1",
            "8.8.8.8",
            "fe80::1",
            "fe80::1%en0",
            "fd00::1%utun3",
            "2001:db8::1",
            "fc00::1",
            "fe00::1",
            "ff02::1",
            "::ffff:192.168.1.20",
            "localhost",
            "Sams-iPhone.local",
            "[fd00::1]:8100",
            "not an address",
        ):
            with (
                self.subTest(address=address),
                self.assertRaises(ValueError) as refused,
            ):
                check_target(address)
            self.assertEqual(str(refused.exception), NOT_AVAILABLE)

    def test_a_relay_never_takes_a_lan_target(self):
        for address in ("192.168.1.20", "0.0.0.0", "::", "fe80::1"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                Relay(address, [(0, 8100)])


@unittest.skipUnless(ipv6_available(), "no IPv6 loopback here")
class ForwardingTests(unittest.TestCase):
    def setUp(self):
        self.port, stop = echo_server()
        self.addCleanup(stop)
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
        with socket.create_connection(
            (LOOPBACK, forwarder.ports[0]), timeout=5
        ) as client:
            client.sendall(b"GET /status")
            self.assertEqual(client.recv(100), b"echo:GET /status")
            client.sendall(b"again")
            self.assertEqual(client.recv(100), b"echo:again")

    def test_stop_closes_the_ports_and_live_connections(self):
        forwarder = self.relay()
        port = forwarder.ports[0]
        client = socket.create_connection((LOOPBACK, port), timeout=5)
        client.sendall(b"x")
        self.assertEqual(client.recv(10), b"echo:x")
        forwarder.stop()
        self.assertFalse(forwarder.running)
        client.settimeout(5)
        self.assertEqual(client.recv(10), b"")
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


class NoOtherListenerTests(unittest.TestCase):
    def test_every_bind_in_the_package_is_loopback(self):
        binds = []
        for path in sorted(PACKAGE.glob("*.py")):
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if re.search(r"\.bind\(", line):
                    binds.append((path.name, number, line.strip()))
        self.assertEqual([name for name, _, _ in binds], ["relay.py"])
        for name, number, line in binds:
            self.assertIn(
                "LOOPBACK", line, f"{name}:{number} binds something else: {line}"
            )
        self.assertEqual(LOOPBACK, "127.0.0.1")
        for path in sorted(PACKAGE.glob("*.py")):
            if path.name == "relay.py":
                continue
            with self.subTest(path=path.name):
                self.assertNotRegex(
                    path.read_text(),
                    r"\.listen\(|create_server|HTTPServer|socketserver",
                )


class PendingConnectionTests(unittest.TestCase):
    def test_pending_connects_count_toward_the_limit_and_close_on_stop(self):
        first, extra, listener = Mock(), Mock(), Mock()
        listener.accept.side_effect = [(first, None), (extra, None), OSError("closed")]
        with patch.object(relay, "MAX_CONNECTIONS", 1):
            forwarder = Relay("fd00::1", [(0, 8100)])
        with patch.object(relay.threading, "Thread") as thread:
            forwarder._accept(listener, 8100)
        self.assertEqual(thread.call_count, 1)
        self.assertEqual(forwarder.connections, {first})
        extra.close.assert_called_once()
        forwarder.stop()
        first.close.assert_called_once()
        with patch.object(
            relay.socket, "create_connection", side_effect=OSError("no tunnel")
        ):
            forwarder._serve(first, 8100)
        self.assertEqual(forwarder.connections, set())
        self.assertTrue(forwarder.slots.acquire(blocking=False))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.data = Path(tempfile.mkdtemp(prefix="mobster-wifi-store-"))
        (self.data / "device.json").write_text(
            json.dumps({"udid": PRO, "device_name": "Studio iPhone", "team": "T"})
        )
        (self.data / "devices.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "lastUsed": None,
                    "phones": [
                        {
                            "udid": XR,
                            "slot": 1,
                            "wdaPort": 8101,
                            "mjpegPort": 9101,
                            "name": "Work iPhone",
                        }
                    ],
                }
            )
        )

    def settings(self):
        return json.loads((self.data / "device.json").read_text())

    def test_the_primary_phone_keeps_it_in_device_json_with_its_udid(self):
        before = round(time.time() * 1000)
        value = store.put(self.data, PRO.lower(), True, "mobster")
        self.assertEqual((value["enabled"], value["via"]), (True, "mobster"))
        self.assertGreaterEqual(value["enabledAt"], before)
        saved = self.settings()
        self.assertEqual((saved["team"], saved["device_name"]), ("T", "Studio iPhone"))
        self.assertEqual(saved["wifi"]["udid"], PRO.lower())
        self.assertTrue(store.enabled(self.data, PRO))
        self.assertEqual(store.enabled_udids(self.data), {PRO})
        store.put(self.data, PRO, False)
        self.assertIsNone(self.settings()["wifi"])
        self.assertEqual(store.get(self.data, PRO), store.OFF)

    def test_a_phone_added_besides_it_keeps_it_on_its_own_entry(self):
        store.put(self.data, XR, True, "xcode")
        entry = DeviceStore(self.data).phones()[0]
        self.assertEqual(
            (entry["wifi"]["enabled"], entry["wifi"]["via"], entry["slot"]),
            (True, "xcode", 1),
        )
        self.assertNotIn("wifi", self.settings())
        self.assertEqual(store.enabled_udids(self.data), {XR})
        store.put(self.data, PRO, True, "mobster")
        self.assertEqual(store.enabled_udids(self.data), {PRO, XR})
        store.put(self.data, XR, False)
        self.assertNotIn("wifi", DeviceStore(self.data).phones()[0])
        self.assertEqual(store.enabled_udids(self.data), {PRO})

    def test_off_keeps_who_turned_on_the_phones_switch_until_it_is_off_too(self):
        for udid in (PRO, XR):
            with self.subTest(udid=udid):
                store.put(self.data, udid, True, "mobster")
                store.put(self.data, udid, False, "mobster")
                self.assertEqual(
                    store.get(self.data, udid),
                    {"enabled": False, "enabledAt": None, "via": "mobster"},
                )
                self.assertFalse(store.enabled(self.data, udid))
                self.assertNotIn(udid, store.enabled_udids(self.data))
                store.put(self.data, udid, False)
                self.assertEqual(store.get(self.data, udid), store.OFF)
        self.assertIsNone(self.settings()["wifi"])
        self.assertNotIn("wifi", DeviceStore(self.data).phones()[0])

    def test_hand_edited_values_read_safely(self):
        for wifi in ("yes", {"enabled": "true"}, {"enabled": 1}, None, [True]):
            settings = self.settings()
            settings["wifi"] = wifi
            (self.data / "device.json").write_text(json.dumps(settings))
            with self.subTest(wifi=wifi):
                self.assertFalse(store.enabled(self.data, PRO))
        settings = self.settings()
        settings["wifi"] = {
            "enabled": True,
            "udid": PRO,
            "enabledAt": True,
            "via": "script",
        }
        (self.data / "device.json").write_text(json.dumps(settings))
        self.assertEqual(
            store.get(self.data, PRO), {"enabled": True, "enabledAt": None, "via": None}
        )
        settings["wifi"]["udid"] = XR
        (self.data / "device.json").write_text(json.dumps(settings))
        self.assertFalse(store.enabled(self.data, PRO))

    def test_no_data_folder_yet(self):
        empty = Path(tempfile.mkdtemp(prefix="mobster-wifi-empty-"))
        self.assertEqual(store.get(empty, PRO), store.OFF)
        self.assertEqual(store.enabled_udids(empty), set())
        with self.assertRaises(LookupError):
            store.put(empty, PRO, True, "mobster")
        self.assertFalse((empty / "device.json").exists())


class GateTests(unittest.TestCase):
    def test_unset_follows_the_default_and_an_explicit_value_always_wins(self):
        for default in (False, True):
            with (
                self.subTest(default=default),
                patch.object(wireless, "DEFAULT_ON", default),
            ):
                self.assertIs(wireless.transport_on({}), default)
                self.assertIs(wireless.transport_on({wireless.GATE: " "}), default)
                self.assertIs(wireless.transport_on({wireless.GATE: "maybe"}), default)
                for word in ("1", "on", "True", "yes"):
                    self.assertIs(wireless.transport_on({wireless.GATE: word}), True)
                for word in ("0", "off", "False", "no"):
                    self.assertIs(wireless.transport_on({wireless.GATE: word}), False)


class MatrixTests(unittest.TestCase):
    def test_the_cable_wins_then_wifi_then_nothing(self):
        for usb, enabled, tunnel_up, expected in (
            (True, True, True, "usb"),
            (True, False, False, "usb"),
            (True, True, False, "usb"),
            (False, True, True, "wifi"),
            (False, True, False, None),
            (False, False, True, None),
            (False, False, False, None),
            (None, True, True, "wifi"),
            (None, False, False, None),
        ):
            with self.subTest(usb=usb, enabled=enabled, tunnel_up=tunnel_up):
                self.assertEqual(
                    transport.choose(usb is True, enabled, tunnel_up), expected
                )

    def test_the_backoff_schedule(self):
        backoff = transport.Backoff()
        self.assertTrue(backoff.ready(0))
        waits = [backoff.fail(100) for _ in range(10)]
        self.assertEqual(waits, [1, 2, 4, 8, 16, 32, 60, 60, 60, 60])
        self.assertFalse(backoff.ready(159))
        self.assertTrue(backoff.ready(160))
        backoff.reset()
        self.assertEqual((backoff.failures, backoff.ready(0)), (0, True))


class AttachedTests(unittest.TestCase):
    def setUp(self):
        transport.forget()
        self.addCleanup(transport.forget)

    def guard(self, usb):
        return lockscreen.DetectGuard(
            device_id=PRO, usb=True, attached_reader=lambda udid: usb
        )

    def test_wifi(self):
        transport.update(PRO, transport="wifi", reachable=True)
        self.assertIs(self.guard(False).attached(), True)
        transport.update(PRO, switching=True)
        self.assertIsNone(self.guard(False).attached())
        transport.update(PRO, switching=False, reachable=False)
        self.assertIsNone(self.guard(False).attached())
        self.assertTrue(self.guard(False).wifi_lost())

    def test_a_lost_wifi_link_stops_the_run_in_plain_words(self):
        transport.update(PRO, transport="wifi", reachable=False)
        driver = SimpleNamespace(
            http=SimpleNamespace(
                request=lambda *a, **k: (_ for _ in ()).throw(OSError())
            )
        )
        verdict = self.guard(False).check(driver, cause="lock_suspected")
        self.assertEqual((verdict.state, verdict.code), ("stop", "unplugged"))
        self.assertIn("out of reach over Wi-Fi", verdict.message)
        transport.update(PRO, transport="wifi", reachable=True)
        unlocked = SimpleNamespace(
            http=SimpleNamespace(request=lambda *a, **k: {"value": False})
        )
        self.assertEqual(self.guard(False).check(unlocked, cause="preflight"), READY)

    def test_another_process_with_wifi_on_doesnt_call_it_unplugged(self):
        home = Path(tempfile.mkdtemp(prefix="mobster-attached-"))
        (home / "device.json").write_text(json.dumps({"udid": PRO}))
        store.put(home, PRO, True, "mobster")
        with patch("mobile_agent.paths.user_data_dir", lambda: home):
            with patch.dict(os.environ, {"MOBSTER_WIFI_TRANSPORT": "1"}):
                self.assertIsNone(self.guard(False).attached())
            with patch.dict(os.environ, {"MOBSTER_WIFI_TRANSPORT": "0"}):
                self.assertIs(self.guard(False).attached(), False)


class FakeManager:
    def __init__(self, udid, autostart=True):
        self.udid = udid
        self.runner = None
        self.transport = None
        self.wifi_address = None
        self.autostart_on = autostart
        self.answers = True
        self.calls = []
        self.start_error = None
        self.usb = False

    def settings(self):
        return {"udid": self.udid, "autostart": self.autostart_on}

    def start_wifi_runner(self, address, peer_udid):
        self.calls.append(("wifi", address))
        self.peer_udid = peer_udid
        if self.start_error:
            raise LookupError(self.start_error)
        self.runner, self.transport, self.wifi_address = Runner(), "wifi", address

    def start_usb(self):
        self.runner, self.transport = Runner(), "usb"

    def stop_runner(self, remember=False):
        self.calls.append(("stop",))
        self.runner, self.transport = None, None

    def autostart(self):
        self.calls.append(("autostart",))
        if self.usb and self.autostart_on:
            self.start_usb()

    def wda_ready(self, timeout=1.5):
        return self.runner is not None and self.answers


class MonitorTests(unittest.TestCase):
    def setUp(self):
        transport.forget()
        self.addCleanup(transport.forget)
        self.data = Path(tempfile.mkdtemp(prefix="mobster-monitor-"))
        (self.data / "device.json").write_text(
            json.dumps({"udid": PRO, "device_name": "Studio iPhone"})
        )
        store.put(self.data, PRO, True, "mobster")
        self.manager = FakeManager(PRO)
        self.phone = SimpleNamespace(
            primary=True, name="Studio iPhone", udid=PRO, id=PRO
        )
        self.busy = set()
        self.events = []
        self.usb = {PRO: False}
        self.peers = {
            PRO: Peer(
                udid=PRO,
                transport="localNetwork",
                tunnel="up",
                tunnel_state="connected",
                address=TUNNEL,
            )
        }
        self.listed = 0
        self.brought_up = []
        self.net = "net-1"
        self.now = 1000.0
        fleet = SimpleNamespace(
            data_dir=self.data,
            usb_managers=lambda: [(self.phone, PRO, self.manager)] + self.extra,
        )
        self.extra = []
        self.runtime = SimpleNamespace(
            fleet=fleet, closing=False, device_busy=lambda udid: udid in self.busy
        )
        self.tunnel_route = True
        self.network = {"state": "ok", "interface": "en0", "peers": 3}
        self.network_checks = 0
        self.monitor = Monitor(
            self.runtime,
            lister=self.lister,
            bring_up=self.bring_up,
            attached=lambda udid: self.usb.get(udid),
            nwi=lambda: self.net,
            clock=lambda: self.now,
            publish=lambda topic, event: self.events.append((topic, event)),
            through=lambda address: self.tunnel_route,
            network_check=self.check_network,
        )

    def check_network(self):
        self.network_checks += 1
        return dict(self.network)

    def lister(self):
        self.listed += 1
        if isinstance(self.peers, Exception):
            raise self.peers
        return dict(self.peers)

    def bring_up(self, udid):
        self.brought_up.append(udid)
        return {}

    def tick(self, seconds=10.0):
        self.now += seconds
        self.monitor.tick()
        return transport.link(PRO)

    def test_unplugged_with_the_tunnel_up_moves_to_wifi_and_says_so(self):
        link = self.tick()
        self.assertEqual(self.manager.calls, [("wifi", TUNNEL)])
        self.assertEqual(
            (link.transport, link.reachable, link.tunnel, link.problem),
            ("wifi", True, "up", None),
        )
        self.assertIsNotNone(link.last_seen_at)
        self.assertEqual(
            self.events,
            [
                (
                    "devices",
                    {
                        "event": "transport_changed",
                        "device": PRO,
                        "transport": "wifi",
                        "reachable": True,
                    },
                )
            ],
        )
        self.tick()
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.manager.calls, [("wifi", TUNNEL)])

    def test_it_starts_wifi_only_with_the_udid_coredevice_reported(self):
        self.tick()
        self.assertEqual(self.manager.peer_udid, PRO)

    def test_a_running_task_keeps_its_transport_until_it_ends(self):
        self.manager.start_usb()
        self.busy.add(PRO)
        link = self.tick()
        self.assertEqual(self.manager.calls, [])
        self.assertEqual((link.transport, link.switching), ("usb", True))
        self.assertIs(
            lockscreen.DetectGuard(
                device_id=PRO, usb=True, attached_reader=lambda u: False
            ).attached(),
            False,
        )
        self.busy.clear()
        link = self.tick()
        self.assertEqual(self.manager.calls, [("wifi", TUNNEL)])
        self.assertEqual((link.transport, link.switching), ("wifi", False))

    def test_the_cable_plugged_in_mid_task_waits_then_switches(self):
        self.tick()
        self.usb[PRO] = True
        self.manager.usb = True
        self.busy.add(PRO)
        link = self.tick()
        self.assertEqual(self.manager.calls, [("wifi", TUNNEL)])
        self.assertEqual((link.transport, link.switching), ("wifi", True))
        self.assertIsNone(lockscreen.DetectGuard(device_id=PRO, usb=True).attached())
        self.busy.clear()
        link = self.tick()
        self.assertEqual(
            self.manager.calls, [("wifi", TUNNEL), ("stop",), ("autostart",)]
        )
        self.assertEqual((link.transport, link.reachable), ("usb", True))
        self.assertEqual(self.events[-1][1]["transport"], "usb")

    def test_the_tunnel_going_down_stops_wifi_when_idle_and_backs_off(self):
        self.tick()
        self.manager.answers = False
        self.peers = {
            PRO: Peer(
                udid=PRO,
                transport="localNetwork",
                tunnel="down",
                tunnel_state="disconnected",
            )
        }
        link = self.tick()
        self.assertEqual(self.manager.calls[-1], ("stop",))
        self.assertEqual(
            (link.transport, link.reachable, link.problem), (None, False, "tunnel_down")
        )
        self.assertEqual(self.monitor.backoff(PRO).failures, 1)
        self.tick(0.5)
        self.assertEqual(self.brought_up, [])
        self.tick(1.0)
        self.assertEqual(self.brought_up, [PRO])

    def test_a_tunnel_address_this_mac_reaches_through_wifi_is_never_used(self):
        self.tunnel_route = False
        link = self.tick()
        self.assertEqual(self.manager.calls, [])
        self.assertEqual(
            (link.transport, link.reachable, link.problem), (None, False, "address")
        )
        self.tunnel_route = True
        link = self.tick()
        self.assertEqual(self.manager.calls, [("wifi", TUNNEL)])
        self.assertEqual(link.transport, "wifi")

    def test_a_network_that_keeps_devices_apart_is_the_problem_named(self):
        self.peers = {}
        self.network = {"state": "blocked", "interface": "en0", "peers": 0}
        link = self.tick()
        self.assertEqual(link.problem, "network_blocked")
        self.assertEqual(self.monitor.network_problem(), "network_blocked")
        self.assertEqual(self.network_checks, 1)
        self.tick()
        self.assertEqual(self.network_checks, 1)
        self.tick(NETWORK_EVERY)
        self.assertEqual(self.network_checks, 2)
        self.net = "net-2"
        self.tick(1.0)
        self.assertEqual(self.network_checks, 3)
        self.network = {"state": "ok", "interface": "en0", "peers": 4}
        self.tick(NETWORK_EVERY)
        self.assertEqual(transport.link(PRO).problem, "not_seen")
        for state, code in (("no_permission", "no_local_network"), ("vpn", "vpn")):
            with self.subTest(state=state):
                self.network = {"state": state}
                self.assertEqual(self.tick(NETWORK_EVERY).problem, code)

    def test_devicectl_not_answering_never_ends_a_working_link(self):
        self.tick()
        self.peers = DevicectlError(
            "devicectl didn't answer in 8 s and was stopped", "hang"
        )
        link = self.tick()
        self.assertEqual(self.manager.calls, [("wifi", TUNNEL)])
        self.assertEqual((link.transport, link.reachable), ("wifi", True))

    def test_a_working_link_ends_once_its_route_stops_being_the_tunnel(self):
        self.tick()
        self.peers = DevicectlError(
            "devicectl didn't answer in 8 s and was stopped", "hang"
        )
        self.tunnel_route = False   # a VPN took the route to the phone's tunnel address
        link = self.tick()
        self.assertEqual(self.manager.calls, [("wifi", TUNNEL), ("stop",)])
        self.assertEqual((link.transport, link.reachable, link.problem), (None, False, "route"))

    def test_the_next_look_comes_when_a_retry_is_due(self):
        from mobile_agent.wireless.monitor import TICK

        self.assertEqual(self.monitor.pause(), TICK)
        self.manager.start_error = (
            "Another program is using port 8100 or 9100 on this Mac"
        )
        self.tick()
        self.assertEqual(self.monitor.pause(), 1)
        self.tick(1)
        self.assertEqual(self.monitor.pause(), 2)
        self.now += 1.9
        self.assertEqual(self.monitor.pause(), 0.5)
        for _ in range(8):
            self.tick(60)
        self.assertEqual(self.monitor.pause(), TICK)

    def test_a_runner_the_person_stopped_stays_stopped(self):
        self.manager.autostart_on = False
        link = self.tick()
        self.assertEqual(self.manager.calls, [])
        self.assertEqual((link.transport, link.switching), (None, False))

    def test_wifi_off_for_the_phone_never_uses_wifi_and_ends_it(self):
        self.tick()
        store.put(self.data, PRO, False)
        link = self.tick()
        self.assertEqual(self.manager.calls, [("wifi", TUNNEL), ("stop",)])
        self.assertEqual((link.transport, link.problem), (None, None))
        self.tick()
        self.assertEqual(len(self.manager.calls), 2)

    def test_a_kick_for_a_new_phone_during_a_tick_does_not_break_it(self):
        self.tick()
        reset = type(self.monitor.backoff(PRO)).reset

        def reset_and_kick(backoff):
            reset(backoff)
            self.monitor.backoffs.setdefault("NEW-UDID", transport.Backoff())

        self.net = "net-2"
        with patch.object(type(self.monitor.backoff(PRO)), "reset", reset_and_kick):
            self.tick(1.0)
        self.assertIn("NEW-UDID", self.monitor.backoffs)
        self.assertEqual(self.net, self.monitor.net)

    def test_a_runner_that_stops_answering_is_restarted_unless_a_task_runs(self):
        self.tick()
        self.manager.answers = False
        self.busy.add(PRO)
        self.tick(LOST_AFTER + 5)
        self.assertEqual(self.manager.calls, [("wifi", TUNNEL)])
        self.busy.clear()
        self.tick(1)
        self.tick(LOST_AFTER + 1)
        self.assertIn(("stop",), self.manager.calls)

    def test_a_task_in_another_mobster_process_also_holds_the_switch(self):
        self.manager.start_usb()
        held = {PRO}
        self.runtime.fleet.in_use = lambda phone: phone.udid in held
        link = self.tick()
        self.assertEqual(self.manager.calls, [])
        self.assertEqual((link.transport, link.switching), ("usb", True))
        held.clear()
        self.assertEqual(self.tick().transport, "wifi")

    def test_when_busy_cant_be_told_nothing_switches(self):
        self.manager.start_usb()

        def broken(phone):
            raise OSError("the lease folder is gone")

        self.runtime.fleet.in_use = broken
        self.assertEqual((self.tick().transport, self.manager.calls), ("usb", []))
        self.runtime.fleet.in_use = lambda phone: False
        self.runtime.device_busy = lambda udid: 1 / 0
        self.assertEqual((self.tick().transport, self.manager.calls), ("usb", []))

    def test_turning_wifi_for_iphones_off_retires_idle_wifi_runners(self):
        self.tick()
        self.monitor.retire()
        self.assertEqual(self.manager.calls[-2:], [("stop",), ("autostart",)])
        self.assertIsNone(transport.link(PRO))


class ServiceTests(unittest.TestCase):
    def test_the_monitor_runs_only_with_wifi_for_iphones_on_in_a_runtime_that_manages_phones(
        self,
    ):
        from mobile_agent.wireless import api

        unmanaged = SimpleNamespace(manager=None, fleet=None)
        api.start_service(unmanaged)
        self.assertIsNone(api.monitor_for(unmanaged))
        fleet = SimpleNamespace(
            data_dir=Path(tempfile.mkdtemp()), usb_managers=lambda: []
        )
        managed = SimpleNamespace(
            manager=object(), fleet=fleet, closing=False, device_busy=lambda udid: False
        )
        with patch.dict(os.environ, {"MOBSTER_WIFI_TRANSPORT": "0"}):
            api.start_service(managed)
        self.assertIsNone(api.monitor_for(managed))
        api.close_service(managed)
        with (
            patch.dict(os.environ, {"MOBSTER_WIFI_TRANSPORT": "1"}),
            patch.object(monitor_module.Monitor, "tick", lambda monitor: None),
        ):
            api.start_service(managed)
            self.assertTrue(api.monitor_for(managed).running)
            api.close_service(managed)
        self.assertIsNone(api.monitor_for(managed))
