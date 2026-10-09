"""Track wireless: choosing cable or Wi-Fi (SPEC §3.7 items 5-7). The selection matrix, a switch only when no task runs,
the reconnect backoff, network changes, the phone guard's ``attached()`` per transport, and the monitor driving
fake DeviceManagers. Fakes only: no devicectl, no runner, no phone."""

import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent import lockscreen
from mobile_agent.agent_hooks import READY
from mobile_agent.wireless import monitor as monitor_module
from mobile_agent.wireless import store, transport
from mobile_agent.wireless.devicectl import DevicectlError, Peer
from mobile_agent.wireless.monitor import LOST_AFTER, NETWORK_EVERY, START_TIMEOUT, Monitor

PRO = "00008130-001A2B3C4D5E6F70"
XR = "00008020-000A1B2C3D4E5F60"
TUNNEL, OTHER = "fd7a:1c2b:3d4e::1", "fd7a:1c2b:3d4e::9"


class MatrixTests(unittest.TestCase):
    def test_the_cable_wins_then_wifi_then_nothing(self):
        for usb, enabled, tunnel_up, expected in (
                (True, True, True, "usb"), (True, False, False, "usb"), (True, True, False, "usb"),
                (False, True, True, "wifi"), (False, True, False, None), (False, False, True, None),
                (False, False, False, None), (None, True, True, "wifi"), (None, False, False, None)):
            with self.subTest(usb=usb, enabled=enabled, tunnel_up=tunnel_up):
                self.assertEqual(transport.choose(usb is True, enabled, tunnel_up), expected)

    def test_a_switch_waits_for_the_task(self):
        self.assertEqual(transport.next_step("wifi", "wifi", busy=True), "stay")
        self.assertEqual(transport.next_step("wifi", "usb", busy=True), "wait")
        self.assertEqual(transport.next_step("wifi", "usb", busy=False), "switch")
        self.assertEqual(transport.next_step(None, "wifi", busy=False), "switch")
        self.assertEqual(transport.next_step("usb", "wifi", busy=True), "wait")

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
    """lockscreen.DetectGuard.attached(): True on a live Wi-Fi link, None while switching or while the link is down,
    and the USB listing otherwise, exactly as before."""

    def setUp(self):
        transport.forget()
        self.addCleanup(transport.forget)

    def guard(self, usb):
        return lockscreen.DetectGuard(device_id=PRO, usb=True, attached_reader=lambda udid: usb)

    def test_the_cable_as_before(self):
        self.assertIs(self.guard(True).attached(), True)
        self.assertIs(self.guard(False).attached(), False)
        self.assertIsNone(self.guard(None).attached())
        transport.update(PRO, transport="usb", reachable=True)
        self.assertIs(self.guard(False).attached(), False)   # a task on the cable still stops when it's pulled
        self.assertIsNone(lockscreen.DetectGuard(device_id=None, usb=True).attached())

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
        driver = SimpleNamespace(http=SimpleNamespace(request=lambda *a, **k: (_ for _ in ()).throw(OSError())))
        verdict = self.guard(False).check(driver, cause="lock_suspected")
        self.assertEqual((verdict.state, verdict.code), ("stop", "unplugged"))
        self.assertIn("out of reach over Wi-Fi", verdict.message)
        transport.update(PRO, transport="wifi", reachable=True)
        unlocked = SimpleNamespace(http=SimpleNamespace(request=lambda *a, **k: {"value": False}))
        self.assertEqual(self.guard(False).check(unlocked, cause="preflight"), READY)

    def test_another_process_with_wifi_on_doesnt_call_it_unplugged(self):
        """`mobster run` driving a phone the Mac app runs over Wi-Fi: no link here, so unknown, not unplugged."""
        home = Path(tempfile.mkdtemp(prefix="mobster-attached-"))
        (home / "device.json").write_text(json.dumps({"udid": PRO}))
        store.put(home, PRO, True, "mobster")
        with patch("mobile_agent.paths.user_data_dir", lambda: home):
            with patch.dict(os.environ, {"MOBSTER_WIFI_TRANSPORT": "1"}):
                self.assertIsNone(self.guard(False).attached())
            with patch.dict(os.environ, {"MOBSTER_WIFI_TRANSPORT": ""}):
                self.assertIs(self.guard(False).attached(), False)


class Runner:
    running = True


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

    def start_wifi_runner(self, address):
        self.calls.append(("wifi", address))
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
        (self.data / "device.json").write_text(json.dumps({"udid": PRO, "device_name": "Sam's iPhone"}))
        store.put(self.data, PRO, True, "mobster")
        self.manager = FakeManager(PRO)
        self.phone = SimpleNamespace(primary=True, name="Sam's iPhone", udid=PRO, id=PRO)
        self.busy = set()
        self.events = []
        self.usb = {PRO: False}
        self.peers = {PRO: Peer(udid=PRO, transport="localNetwork", tunnel="up", tunnel_state="connected",
                                address=TUNNEL)}
        self.listed = 0
        self.brought_up = []
        self.net = "net-1"
        self.now = 1000.0
        fleet = SimpleNamespace(data_dir=self.data,
                                usb_managers=lambda: [(self.phone, PRO, self.manager)] + self.extra)
        self.extra = []
        self.runtime = SimpleNamespace(fleet=fleet, closing=False, device_busy=lambda udid: udid in self.busy)
        self.tunnel_route = True
        self.network = {"state": "ok", "interface": "en0", "peers": 3}
        self.network_checks = 0
        self.monitor = Monitor(self.runtime, lister=self.lister, bring_up=self.bring_up,
                               attached=lambda udid: self.usb.get(udid), nwi=lambda: self.net,
                               clock=lambda: self.now, publish=lambda topic, event: self.events.append((topic, event)),
                               through=lambda address: self.tunnel_route, network_check=self.check_network)

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
        self.assertEqual((link.transport, link.reachable, link.tunnel, link.problem), ("wifi", True, "up", None))
        self.assertIsNotNone(link.last_seen_at)
        self.assertEqual(self.events, [("devices", {"event": "transport_changed", "device": PRO, "transport": "wifi",
                                                    "reachable": True})])
        self.tick()
        self.assertEqual(len(self.events), 1)        # nothing changed: nothing published
        self.assertEqual(self.manager.calls, [("wifi", TUNNEL)])

    def test_a_running_task_keeps_its_transport_until_it_ends(self):
        self.manager.start_usb()                     # the cable's runner, still "running" after the pull
        self.busy.add(PRO)
        link = self.tick()
        self.assertEqual(self.manager.calls, [])
        self.assertEqual((link.transport, link.switching), ("usb", True))
        self.assertIs(lockscreen.DetectGuard(device_id=PRO, usb=True, attached_reader=lambda u: False).attached(),
                      False)                         # that task still stops as unplugged
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
        self.assertIsNone(lockscreen.DetectGuard(device_id=PRO, usb=True).attached())   # switching: unknown
        self.busy.clear()
        link = self.tick()
        self.assertEqual(self.manager.calls, [("wifi", TUNNEL), ("stop",), ("autostart",)])
        self.assertEqual((link.transport, link.reachable), ("usb", True))
        self.assertEqual(self.events[-1][1]["transport"], "usb")

    def test_the_tunnel_going_down_stops_wifi_when_idle_and_backs_off(self):
        self.tick()
        self.manager.answers = False
        self.peers = {PRO: Peer(udid=PRO, transport="localNetwork", tunnel="down", tunnel_state="disconnected")}
        link = self.tick()
        self.assertEqual(self.manager.calls[-1], ("stop",))
        self.assertEqual((link.transport, link.reachable, link.problem), (None, False, "tunnel_down"))
        self.assertEqual(self.monitor.backoff(PRO).failures, 1)
        self.tick(0.5)                               # within the backoff: no bring-up yet
        self.assertEqual(self.brought_up, [])
        self.tick(1.0)
        self.assertEqual(self.brought_up, [PRO])

    def test_a_tunnel_address_this_mac_reaches_through_wifi_is_never_used(self):
        """A home network's own fd00::/8 address passes check_target; the route through Wi-Fi (not utun) refuses it,
        so WebDriverAgent is never told to listen there."""
        self.tunnel_route = False
        link = self.tick()
        self.assertEqual(self.manager.calls, [])
        self.assertEqual((link.transport, link.reachable, link.problem), (None, False, "address"))
        self.tunnel_route = True
        link = self.tick()
        self.assertEqual(self.manager.calls, [("wifi", TUNNEL)])
        self.assertEqual(link.transport, "wifi")

    def test_a_network_that_keeps_devices_apart_is_the_problem_named(self):
        """CoreDevice can't see the phone, and this network lets no device see another (guest, hotel or campus
        Wi-Fi): the phone's problem is the network's, with its fix, not "isn't reachable"."""
        self.peers = {}
        self.network = {"state": "blocked", "interface": "en0", "peers": 0}
        link = self.tick()
        self.assertEqual(link.problem, "network_blocked")
        self.assertEqual(self.monitor.network_problem(), "network_blocked")
        self.assertEqual(self.network_checks, 1)
        self.tick()                                   # at most once a minute
        self.assertEqual(self.network_checks, 1)
        self.tick(NETWORK_EVERY)
        self.assertEqual(self.network_checks, 2)
        self.net = "net-2"                            # another Wi-Fi: check again at once
        self.tick(1.0)
        self.assertEqual(self.network_checks, 3)
        self.network = {"state": "ok", "interface": "en0", "peers": 4}
        self.tick(NETWORK_EVERY)
        self.assertEqual(transport.link(PRO).problem, "not_seen")
        for state, code in (("no_permission", "no_local_network"), ("vpn", "vpn")):
            with self.subTest(state=state):
                self.network = {"state": state}
                self.assertEqual(self.tick(NETWORK_EVERY).problem, code)

    def test_the_network_is_checked_only_while_a_phone_is_out_of_sight(self):
        self.tick()                                   # seen over the network: no check
        self.usb[PRO] = True
        self.tick(NETWORK_EVERY)                      # on the cable: no check
        self.assertEqual(self.network_checks, 0)

    def test_devicectl_not_answering_never_ends_a_working_link(self):
        self.tick()
        self.peers = DevicectlError("devicectl didn't answer in 8 s and was stopped", "hang")
        link = self.tick()
        self.assertEqual(self.manager.calls, [("wifi", TUNNEL)])
        self.assertEqual((link.transport, link.reachable), ("wifi", True))
        self.assertEqual(self.monitor.hangs, 1)

    def test_a_failed_start_backs_off_1_2_4_seconds(self):
        self.manager.start_error = "Another program is using port 8100 or 9100 on this Mac"
        link = self.tick()
        self.assertEqual(link.problem, "port_busy")
        attempts = [len(self.manager.calls)]
        for step in (0.5, 0.6, 1.0, 1.0, 1.0, 1.0, 4.0):
            self.tick(step)
            attempts.append(len(self.manager.calls))
        # attempts at +0 (fail: wait 1), +1.1 (fail: wait 2), +3.1 (fail: wait 4), and +9.1
        self.assertEqual(attempts, [1, 1, 2, 2, 3, 3, 3, 4])
        self.assertEqual(self.monitor.backoff(PRO).failures, 4)

    def test_the_next_look_comes_when_a_retry_is_due(self):
        from mobile_agent.wireless.monitor import TICK
        self.assertEqual(self.monitor.pause(), TICK)
        self.manager.start_error = "Another program is using port 8100 or 9100 on this Mac"
        self.tick()
        self.assertEqual(self.monitor.pause(), 1)            # the first retry: 1 s, not the next 10 s tick
        self.tick(1)
        self.assertEqual(self.monitor.pause(), 2)
        self.now += 1.9
        self.assertEqual(self.monitor.pause(), 0.5)          # never a busy loop
        for _ in range(8):
            self.tick(60)
        self.assertEqual(self.monitor.pause(), TICK)         # 60 s apart now: the tick comes first

    def test_a_network_change_retries_at_once(self):
        self.manager.start_error = "Another program is using port 8100 or 9100 on this Mac"
        for _ in range(4):
            self.tick(0.1)
            self.now += 100
        failures = self.monitor.backoff(PRO).failures
        self.assertGreaterEqual(failures, 3)
        self.manager.start_error = None
        self.net = "net-2"                            # another Wi-Fi network
        link = self.tick(0.1)
        self.assertEqual(link.transport, "wifi")
        self.assertEqual(self.monitor.backoff(PRO).failures, 0)

    def test_a_tunnel_not_up_is_brought_up(self):
        self.peers = {PRO: Peer(udid=PRO, transport="localNetwork", tunnel="down", tunnel_state="disconnected")}
        link = self.tick()
        self.assertEqual(self.brought_up, [PRO])
        self.assertEqual((link.transport, link.problem, link.tunnel), (None, "tunnel_down", "down"))

    def test_a_phone_coredevice_doesnt_see_and_a_wrong_address(self):
        self.peers = {}
        self.assertEqual(self.tick().problem, "not_seen")
        self.peers = {PRO: Peer(udid=PRO, transport="localNetwork", tunnel="up", address="192.168.1.20")}
        self.assertEqual(self.tick().problem, "address")
        self.assertEqual((self.manager.calls, self.brought_up), ([], []))

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

    def test_a_runner_that_never_answers_is_stopped_after_the_start_timeout(self):
        self.manager.answers = False
        self.tick()
        link = self.tick(START_TIMEOUT - 20)
        self.assertEqual((link.transport, link.switching), ("wifi", True))
        link = self.tick(30)
        self.assertEqual(self.manager.calls[-1], ("stop",))
        self.assertEqual((link.transport, link.problem), (None, "runner"))

    def test_a_runner_that_stops_answering_is_restarted_unless_a_task_runs(self):
        self.tick()
        self.manager.answers = False
        self.busy.add(PRO)
        self.tick(LOST_AFTER + 5)
        self.assertEqual(self.manager.calls, [("wifi", TUNNEL)])   # a task: left alone
        self.busy.clear()
        self.tick(1)
        self.tick(LOST_AFTER + 1)
        self.assertIn(("stop",), self.manager.calls)

    def test_the_tunnel_address_changing_restarts_on_the_new_one(self):
        self.tick()
        self.peers = {PRO: Peer(udid=PRO, transport="localNetwork", tunnel="up", address=OTHER)}
        self.tick()
        self.assertEqual(self.manager.calls, [("wifi", TUNNEL), ("wifi", OTHER)])

    def test_devicectl_is_asked_rarely_while_every_phone_is_on_the_cable(self):
        self.usb[PRO] = True
        self.manager.start_usb()
        for _ in range(12):
            self.tick(10)
        self.assertLessEqual(self.listed, 3)
        self.usb[PRO] = False
        listed = self.listed
        self.tick(10)
        self.tick(10)
        self.assertEqual(self.listed, listed + 2)

    def test_two_phones_each_on_their_own(self):
        second = FakeManager(XR)
        self.extra = [(SimpleNamespace(primary=False, name="Work iPhone", udid=XR, id=XR), XR, second)]
        devices = {"version": 1, "phones": [{"udid": XR, "slot": 1, "wdaPort": 8101, "mjpegPort": 9101}],
                   "lastUsed": None}
        (self.data / "devices.json").write_text(json.dumps(devices))
        store.put(self.data, XR, True, "xcode")
        self.peers[XR] = Peer(udid=XR, transport="localNetwork", tunnel="up", address=OTHER)
        self.usb[XR] = False
        self.busy.add(PRO)
        self.tick()
        self.assertEqual(self.manager.calls, [])                  # a task on Sam's iPhone: it waits
        self.assertEqual(second.calls, [("wifi", OTHER)])         # the other phone doesn't
        self.assertEqual((transport.link(XR).transport, transport.link(PRO).switching), ("wifi", True))

    def test_a_task_in_another_mobster_process_also_holds_the_switch(self):
        """`mobster run` or an agent's MCP server holds the phone's device lease: this server's runs don't show it."""
        self.manager.start_usb()                       # the cable's runner, still "running" after the pull
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

    def test_the_service_thread_starts_kicks_and_stops(self):
        ticks = []
        with patch.object(Monitor, "tick", lambda monitor: ticks.append(time.monotonic())):
            self.monitor.start()
            self.monitor.kick(PRO)
            for _ in range(100):
                if len(ticks) >= 2:
                    break
                time.sleep(.02)
            self.monitor.stop()
        self.assertGreaterEqual(len(ticks), 2)
        self.assertFalse(self.monitor.running)


class ServiceTests(unittest.TestCase):
    def test_the_monitor_runs_only_with_wifi_for_iphones_on_in_a_runtime_that_manages_phones(self):
        from mobile_agent.wireless import api
        unmanaged = SimpleNamespace(manager=None, fleet=None)
        api.start_service(unmanaged)
        self.assertIsNone(api.monitor_for(unmanaged))
        fleet = SimpleNamespace(data_dir=Path(tempfile.mkdtemp()), usb_managers=lambda: [])
        managed = SimpleNamespace(manager=object(), fleet=fleet, closing=False, device_busy=lambda udid: False)
        with patch.dict(os.environ, {"MOBSTER_WIFI_TRANSPORT": ""}):
            api.start_service(managed)
        self.assertIsNone(api.monitor_for(managed))               # off: nothing made, no thread
        api.close_service(managed)
        with patch.dict(os.environ, {"MOBSTER_WIFI_TRANSPORT": "1"}), \
                patch.object(monitor_module.Monitor, "tick", lambda monitor: None):
            api.start_service(managed)
            self.assertTrue(api.monitor_for(managed).running)
            api.close_service(managed)
        self.assertIsNone(api.monitor_for(managed))


if __name__ == "__main__":
    unittest.main()
