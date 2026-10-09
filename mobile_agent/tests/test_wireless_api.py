"""Track wireless through the server (SPEC §3.7, API and data model): /api/wireless, turning Wi-Fi on and off for a
phone (needs the cable and trust: 409 otherwise, one sentence and one fix each), the hidden wifiTransport setting,
reconnect, and GET /api/devices, which is exactly the cable-only record while Wi-Fi for iPhones is off. Fakes only:
the Mac app's server (`serve --manage-device`) with a fake USB listing; no phone, no devicectl, no pair tool."""

import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from mobile_agent import tracks
from mobile_agent.device_manager import DeviceManager
from mobile_agent.devices import REASONS, DeviceStore
from mobile_agent.tests.test_fleet import Harness, PRO, XR
from mobile_agent.wireless import api as wireless_api
from mobile_agent.wireless import monitor as monitor_module
from mobile_agent.wireless import pair, store, transport

TUNNEL = "fd7a:1c2b:3d4e::1"
KEYS = {"id", "kind", "name", "udid", "model", "modelName", "ios", "state", "reason", "primary", "setUp", "default",
        "activeRunId", "approval", "lastUsedAt"}


class Runner:
    running = True

    def stop(self):
        self.running = False


class WirelessHarness(Harness):
    def setUp(self):
        super().setUp()
        transport.forget()
        self.addCleanup(transport.forget)
        self.sims = []
        self.listing = [{"udid": PRO, "name": "Sam's iPhone", "model": "iPhone16,1", "ios": "26.0", "trusted": True}]
        self.paired = []
        for item in (patch.object(DeviceManager, "watch", lambda manager, stop: None),
                     patch.object(DeviceManager, "devices", lambda manager: list(self.listing)),
                     patch.object(DeviceManager, "teams", lambda manager: []),
                     patch.object(DeviceManager, "built", lambda manager: True),
                     patch.object(DeviceManager, "expired", lambda manager, now=None: False),
                     patch.object(DeviceManager, "tools", lambda manager: {
                         "xcodebuild": "/x", "iproxy": "/i", "idevice_id": "/d", "ideviceinfo": "/v", "git": "/g",
                         "xcode": {"state": "ok", "version": "Xcode 26.4", "app": "/Applications/Xcode.app",
                                   "fixes": [], "selected": "/x", "developer_dir": "/x", "problems": []}}),
                     patch("mobile_agent.setup_service.SetupService.developer_mode", lambda service, device: True),
                     patch("mobile_agent.installed_apps.InstalledApps.snapshot", lambda apps: None),
                     patch.object(pair, "run", self.fake_pair),
                     patch.object(monitor_module.Monitor, "tick", lambda monitor: None)):
            item.start()
            self.addCleanup(item.stop)
        self.data = self.root / "data"
        self.data.mkdir()
        (self.data / "device.json").write_text(json.dumps({"udid": PRO, "device_name": "Sam's iPhone",
                                                          "autostart": True}))
        self.pair_result = True

    def fake_pair(self, udid, action, **kwargs):
        self.paired.append((udid, action))
        if isinstance(self.pair_result, Exception):
            raise self.pair_result
        return action == "enable"

    def managed(self):
        return self.runtime(wda_url="http://127.0.0.1:8100", manage_device=True, data_dir=str(self.data), socket=None)


class OverviewTests(WirelessHarness):
    def test_the_track_loads_and_lists_each_phone(self):
        runtime = self.managed()
        self.assertEqual(tracks.STATUS["wireless"], "ok")
        self.assertEqual(runtime.status()["extensions"]["wireless"], "ok")
        status, body = self.request(runtime, "GET", "/api/wireless")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"wifiTransport": False, "network": None, "devices": [{
            "id": PRO, "udid": PRO, "name": "Sam's iPhone", "primary": True, "enabled": False, "transport": None,
            "tunnel": "unknown", "reachable": False, "lastSeenAt": None, "problem": None}]})

    def test_a_server_without_usb_phones_lists_none(self):
        status, body = self.request(self.runtime(), "GET", "/api/wireless")
        self.assertEqual((status, body), (200, {"wifiTransport": False, "network": None, "devices": []}))
        status, body = self.request(self.runtime(), "POST", f"/api/devices/{PRO}/wifi", {"enabled": True})
        self.assertEqual((status, body["code"]), (409, "not_managed"))


class NetworkTests(WirelessHarness):
    """GET /api/wireless/network: whether this network lets devices see each other (guest, hotel, campus Wi-Fi with
    client isolation, or Bonjour blocked), checked at most every NETWORK_FRESH seconds; reads only."""

    def setUp(self):
        super().setUp()
        wireless_api._network.update(at=None, result=None)
        self.addCleanup(wireless_api._network.update, at=None, result=None)
        self.result = {"state": "blocked", "interface": "en0", "peers": 0}
        self.checks = 0

        def check():
            self.checks += 1
            return dict(self.result)
        item = patch("mobile_agent.wireless.network.peer_check", check)
        item.start()
        self.addCleanup(item.stop)

    def test_a_network_that_keeps_devices_apart(self):
        runtime = self.managed()
        status, body = self.request(runtime, "GET", "/api/wireless/network")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"network": {"state": "blocked", "problem": {
            "code": "network_blocked", "message": "This network stops devices from seeing each other.",
            "fix": "Use the cable or your phone's hotspot."}}})
        # The overview carries the last answer too.
        self.assertEqual(self.request(runtime, "GET", "/api/wireless")[1]["network"]["state"], "blocked")

    def test_each_state_and_the_cache(self):
        runtime = self.managed()
        self.result = {"state": "ok", "interface": "en0", "peers": 3}
        self.assertEqual(self.request(runtime, "GET", "/api/wireless/network")[1],
                         {"network": {"state": "ok", "problem": None}})
        self.result = {"state": "no_permission"}
        # Within NETWORK_FRESH seconds the same answer comes back without a second check.
        self.assertEqual(self.request(runtime, "GET", "/api/wireless/network")[1]["network"]["state"], "ok")
        self.assertEqual(self.checks, 1)
        wireless_api._network["at"] -= wireless_api.NETWORK_FRESH + 1
        body = self.request(runtime, "GET", "/api/wireless/network")[1]
        self.assertEqual(body["network"]["problem"]["code"], "no_local_network")
        self.assertIn("Local Network", body["network"]["problem"]["fix"])
        for state, code in (("vpn", "vpn"), ("offline", None), ("unknown", None)):
            with self.subTest(state=state):
                self.result = {"state": state}
                wireless_api._network["at"] = None
                answer = self.request(runtime, "GET", "/api/wireless/network")[1]["network"]
                self.assertEqual((answer["state"], (answer["problem"] or {}).get("code")), (state, code))

    def test_a_check_that_fails_says_unknown(self):
        def broken():
            raise OSError("no route")
        with patch("mobile_agent.wireless.network.peer_check", broken):
            body = self.request(self.managed(), "GET", "/api/wireless/network")[1]
        self.assertEqual(body, {"network": {"state": "unknown", "problem": None}})

    def test_it_needs_the_token_like_every_route(self):
        from email.message import Message
        import io
        from unittest.mock import Mock
        from mobile_agent.server import make_handler
        runtime = self.managed()
        for path in ("/api/wireless", "/api/wireless/network"):
            with self.subTest(path=path):
                handler = object.__new__(make_handler(runtime, token="a-token-for-tests"))
                handler.headers = Message()
                handler.headers["Host"] = "127.0.0.1:8765"
                handler.command, handler.path = "GET", path
                handler.rfile, handler.wfile, handler.connection = io.BytesIO(), io.BytesIO(), Mock()
                statuses = []
                handler.send_response = statuses.append
                handler.send_header = lambda name, value: None
                handler.end_headers = lambda: None
                handler.do_GET()
                self.assertEqual(statuses[-1], 401)
        self.assertEqual(self.checks, 0)


class SwitchTests(WirelessHarness):
    def test_turning_it_on_with_the_cable(self):
        runtime = self.managed()
        status, body = self.request(runtime, "POST", f"/api/devices/{PRO}/wifi", {"enabled": True})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.paired, [(PRO, "enable")])
        self.assertEqual((body["device"]["id"], body["device"]["enabled"]), (PRO, True))
        self.assertNotIn("next", body)
        saved = store.get(self.data, PRO)
        self.assertEqual((saved["enabled"], saved["via"]), (True, "mobster"))
        self.assertIsInstance(saved["enabledAt"], int)
        self.assertEqual(json.loads((self.data / "device.json").read_text())["wifi"]["udid"], PRO)

    def test_it_needs_the_cable_and_trust(self):
        runtime = self.managed()
        self.listing = []
        status, body = self.request(runtime, "POST", f"/api/devices/{PRO}/wifi", {"enabled": True})
        self.assertEqual((status, body["code"]), (409, "needs_cable"))
        self.assertEqual(body["error"], "Plug Sam's iPhone into this Mac with a cable to turn on Wi-Fi for it.")
        self.assertEqual(body["fix"], "Plug it in, unlock it and tap Trust if it asks, then try again.")
        self.listing = [{"udid": PRO, "name": "Sam's iPhone", "trusted": False}]
        status, body = self.request(runtime, "POST", f"/api/devices/{PRO}/wifi", {"enabled": True})
        self.assertEqual((status, body["code"]), (409, "not_trusted"))
        self.assertEqual(self.paired, [])
        self.assertFalse(store.enabled(self.data, PRO))

    def test_without_the_tool_the_answer_says_to_tick_it_in_xcode(self):
        runtime = self.managed()
        self.pair_result = LookupError("mobster-wifi-pair isn't available on this Mac")
        status, body = self.request(runtime, "POST", f"/api/devices/{PRO}/wifi", {"enabled": True})
        self.assertEqual(status, 200)
        self.assertEqual(body["next"]["code"], "needs_xcode")
        self.assertIn("tick Connect via network", body["next"]["fix"])
        self.assertEqual(store.get(self.data, PRO)["via"], "xcode")

    def test_the_phone_saying_no(self):
        runtime = self.managed()
        for error, code in ((pair.PairError("not_trusted"), "not_trusted"), (pair.PairError("refused"), "refused"),
                            (pair.PairError("failed", "lockdown handshake failed"), "refused")):
            self.pair_result = error
            with self.subTest(code=code):
                status, body = self.request(runtime, "POST", f"/api/devices/{PRO}/wifi", {"enabled": True})
                self.assertEqual((status, body["code"]), (409, code))
                self.assertTrue(body["error"] and body["fix"])
        self.pair_result = RuntimeError("Devices are off in this process (MOBSTER_NO_DEVICES=1)")
        status, body = self.request(runtime, "POST", f"/api/devices/{PRO}/wifi", {"enabled": True})
        self.assertEqual((status, body["code"]), (503, "devices_off"))
        self.assertFalse(store.enabled(self.data, PRO))

    def test_turning_it_off_clears_the_flag_mobster_set(self):
        runtime = self.managed()
        self.request(runtime, "POST", f"/api/devices/{PRO}/wifi", {"enabled": True})
        status, body = self.request(runtime, "POST", f"/api/devices/{PRO}/wifi", {"enabled": False})
        self.assertEqual((status, body["device"]["enabled"]), (200, False))
        self.assertEqual(self.paired, [(PRO, "enable"), (PRO, "disable")])
        self.assertFalse(store.enabled(self.data, PRO))
        # A phone the person turned on in Xcode: Mobster leaves the phone's own switch alone.
        store.put(self.data, PRO, True, "xcode")
        self.request(runtime, "POST", f"/api/devices/{PRO}/wifi", {"enabled": False})
        self.assertEqual(self.paired[-1], (PRO, "disable"))
        self.assertEqual(len(self.paired), 2)

    def test_not_while_a_task_runs_over_wifi(self):
        runtime = self.managed()
        store.put(self.data, PRO, True, "mobster")
        transport.update(PRO, transport="wifi", reachable=True)
        with patch.object(type(runtime), "device_busy", lambda runtime, device: device == PRO):
            status, body = self.request(runtime, "POST", f"/api/devices/{PRO}/wifi", {"enabled": False})
        self.assertEqual((status, body["code"]), (409, "busy"))
        self.assertTrue(store.enabled(self.data, PRO))

    def test_not_while_the_first_iphone_runs_a_task_as_the_server_records_it(self):
        """The server records a run on the first iPhone under the fleet's key "@primary", not its UDID: turning Wi-Fi
        off for it mid-task is refused all the same, and so is a lease another Mobster process holds."""
        runtime = self.managed()
        store.put(self.data, PRO, True, "mobster")
        transport.update(PRO, transport="wifi", reachable=True)
        with runtime.lock:
            runtime.active_runs["a1b2c3d4e5f6"] = "@primary"
        try:
            status, body = self.request(runtime, "POST", f"/api/devices/{PRO}/wifi", {"enabled": False})
        finally:
            with runtime.lock:
                runtime.active_runs.pop("a1b2c3d4e5f6", None)
        self.assertEqual((status, body["code"]), (409, "busy"))
        self.assertTrue(store.enabled(self.data, PRO))
        with patch.object(type(runtime.fleet), "in_use", lambda fleet, phone: True):
            status, body = self.request(runtime, "POST", f"/api/devices/{PRO}/wifi", {"enabled": False})
        self.assertEqual((status, body["code"]), (409, "busy"))
        status, _ = self.request(runtime, "POST", f"/api/devices/{PRO}/wifi", {"enabled": False})
        self.assertEqual(status, 200)
        self.assertFalse(store.enabled(self.data, PRO))

    def test_bad_requests_and_other_devices(self):
        runtime = self.managed()
        for body in ({}, {"enabled": "yes"}, {"enabled": True, "extra": 1}, [True]):
            with self.subTest(body=body):
                self.assertEqual(self.request(runtime, "POST", f"/api/devices/{PRO}/wifi", body)[0], 400)
        status, body = self.request(runtime, "POST", "/api/devices/nope/wifi", {"enabled": True})
        self.assertEqual((status, body["code"]), (404, "device_not_found"))
        self.listing.append({"udid": XR, "name": "Work iPhone", "model": "iPhone11,8", "trusted": True})
        runtime.fleet.cache.pop("usb", None)
        status, body = self.request(runtime, "POST", f"/api/devices/{XR}/wifi", {"enabled": True})
        self.assertEqual((status, body["code"]), (409, "not_set_up"))   # plugged in, not set up here yet
        self.assertEqual(body["error"], "Work iPhone isn't set up in Mobster yet.")
        status, body = self.request(runtime, "POST", "/api/devices/Sam's%20iPhone/wifi", {"enabled": True})
        self.assertEqual(status, 200)        # by name, URL-encoded

    def test_a_second_phone_keeps_it_on_its_own_entry(self):
        DeviceStore(self.data).add(XR, "Work iPhone", busy=lambda port: False)
        self.listing.append({"udid": XR, "name": "Work iPhone", "model": "iPhone11,8", "trusted": True})
        runtime = self.managed()
        status, _ = self.request(runtime, "POST", f"/api/devices/{XR}/wifi", {"enabled": True})
        self.assertEqual(status, 200)
        entry = DeviceStore(self.data).phones()[0]
        self.assertEqual((entry["udid"], entry["wifi"]["enabled"], entry["wifi"]["via"], entry["slot"]),
                         (XR, True, "mobster", 1))
        self.assertFalse(store.enabled(self.data, PRO))
        self.assertEqual(store.enabled_udids(self.data), {XR})
        _, body = self.request(runtime, "GET", "/api/wireless")
        self.assertEqual([(item["id"], item["enabled"]) for item in body["devices"]], [(PRO, False), (XR, True)])


class SettingTests(WirelessHarness):
    def test_the_hidden_setting_is_saved_and_starts_the_monitor(self):
        runtime = self.managed()
        self.assertIsNone(wireless_api.monitor_for(runtime))     # off: no monitor, no thread
        with patch.dict(os.environ, {}):
            status, body = self.request(runtime, "POST", "/api/wireless", {"wifiTransport": True})
            self.assertEqual((status, body["wifiTransport"]), (200, True))
            self.assertEqual(os.environ.get("MOBSTER_WIFI_TRANSPORT"), "1")
            env = Path(runtime.setup.env_file).read_text()
            self.assertIn("MOBSTER_WIFI_TRANSPORT=1", env)
            monitor = wireless_api.monitor_for(runtime)
            self.assertTrue(monitor.running)
            status, body = self.request(runtime, "POST", "/api/wireless/reconnect", {"device": PRO})
            self.assertEqual((status, body), (202, {"device": PRO, "accepted": True}))
            status, body = self.request(runtime, "POST", "/api/wireless", {"wifiTransport": False})
            self.assertEqual((status, body["wifiTransport"]), (200, False))
            self.assertNotIn("MOBSTER_WIFI_TRANSPORT", os.environ)
            self.assertNotIn("MOBSTER_WIFI_TRANSPORT", Path(runtime.setup.env_file).read_text())
            self.assertFalse(monitor.running)
        for body in ({}, {"wifiTransport": 1}, {"wifiTransport": True, "x": 1}):
            with self.subTest(body=body):
                self.assertEqual(self.request(runtime, "POST", "/api/wireless", body)[0], 400)

    def test_reconnect_says_when_wifi_for_iphones_is_off(self):
        runtime = self.managed()
        status, body = self.request(runtime, "POST", "/api/wireless/reconnect", {"device": PRO})
        self.assertEqual((status, body["code"]), (409, "off"))
        self.assertEqual(self.request(runtime, "POST", "/api/wireless/reconnect", {})[0], 400)

    def test_the_monitor_closes_with_the_runtime(self):
        with patch.dict(os.environ, {"MOBSTER_WIFI_TRANSPORT": "1"}):
            runtime = self.managed()
            monitor = wireless_api.monitor_for(runtime)
            self.assertTrue(monitor.running)
            self.runtimes.remove(runtime)
            runtime.close()
        self.assertFalse(monitor.running)
        self.assertIsNone(wireless_api.monitor_for(runtime))


class DeviceRecordTests(WirelessHarness):
    def test_off_the_record_is_the_cable_only_one(self):
        store.put(self.data, PRO, True, "mobster")
        runtime = self.managed()
        _, body = self.request(runtime, "GET", "/api/devices")
        self.assertEqual(set(body["devices"][0]), KEYS)

    def test_on_a_phone_running_over_wifi_is_ready_and_says_so(self):
        store.put(self.data, PRO, True, "mobster")
        with patch.dict(os.environ, {"MOBSTER_WIFI_TRANSPORT": "1"}):
            runtime = self.managed()
            self.listing = []                                      # unplugged
            runtime.manager.runner, runtime.manager.transport = Runner(), "wifi"
            transport.update(PRO, transport="wifi", reachable=True, tunnel="up")
            _, body = self.request(runtime, "GET", "/api/devices")
            item = body["devices"][0]
            self.assertEqual(set(item), KEYS | {"transport", "wifi"})
            self.assertEqual((item["id"], item["state"], item["transport"], item["wifi"]),
                             (PRO, "ready", "wifi", {"enabled": True, "reachable": True}))
            _, body = self.request(runtime, "GET", "/api/wireless")
            row = body["devices"][0]
            self.assertEqual((row["transport"], row["tunnel"], row["reachable"]), ("wifi", "up", True))

    def test_on_an_unplugged_phone_not_reachable_says_how_to_fix_it(self):
        store.put(self.data, PRO, True, "mobster")
        with patch.dict(os.environ, {"MOBSTER_WIFI_TRANSPORT": "1"}):
            runtime = self.managed()
            self.listing = []
            runtime.fleet.cache.pop("usb", None)
            transport.update(PRO, transport=None, reachable=False, tunnel="down", problem="tunnel_down")
            _, body = self.request(runtime, "GET", "/api/devices")
            item = body["devices"][0]
            self.assertEqual((item["state"], item["reason"], item["transport"]),
                             ("disconnected", REASONS["wifi_unreachable"], None))
            _, body = self.request(runtime, "GET", "/api/wireless")
            self.assertEqual(body["devices"][0]["problem"], {
                "code": "tunnel_down", "message": "The encrypted link to Sam's iPhone isn't up yet.",
                "fix": "Unlock it and keep it on the same Wi-Fi as this Mac; Mobster tries again by itself."})

    def test_in_use_sees_this_servers_tasks_and_other_processes(self):
        """The monitor switches a phone's transport only when Fleet.in_use is False (wireless/monitor.py)."""
        runtime = self.managed()
        runtime.fleet.inventory()
        phone, udid, _manager = runtime.fleet.usb_managers()[0]
        self.assertEqual(udid, PRO)
        held = set()
        with patch("mobile_agent.devices.lease_free", side_effect=lambda url: url.rstrip("/") not in held):
            self.assertFalse(runtime.fleet.in_use(phone))
            held.add("http://127.0.0.1:8100")                         # `mobster run` in another process
            self.assertTrue(runtime.fleet.in_use(phone))
        runtime.active_runs["3f9c2a1b7d0e"] = phone.key            # a task here
        self.assertTrue(runtime.fleet.in_use(phone))

    def test_on_the_cable_it_says_usb(self):
        with patch.dict(os.environ, {"MOBSTER_WIFI_TRANSPORT": "1"}):
            runtime = self.managed()
            _, body = self.request(runtime, "GET", "/api/devices")
            item = body["devices"][0]
            self.assertEqual((item["transport"], item["wifi"]), ("usb", {"enabled": False, "reachable": False}))


class TokenTests(WirelessHarness):
    def test_every_route_needs_the_token(self):
        from mobile_agent.server import make_handler
        from mobile_agent.tests.seam_support import request
        runtime = self.managed()
        handler = make_handler(runtime, "t" * 40)
        for method, path, body in (("GET", "/api/wireless", None), ("POST", "/api/wireless", {"wifiTransport": True}),
                                   ("POST", f"/api/devices/{PRO}/wifi", {"enabled": True}),
                                   ("POST", "/api/wireless/reconnect", {"device": PRO})):
            with self.subTest(path=path):
                self.assertEqual(request(handler, method, path, body)[0], 401)
        self.assertEqual(self.paired, [])
        self.assertNotIn("MOBSTER_WIFI_TRANSPORT", os.environ)


if __name__ == "__main__":
    unittest.main()
