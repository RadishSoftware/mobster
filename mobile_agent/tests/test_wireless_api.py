import io
import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import Mock, patch

from mobile_agent import (
    tracks,
)
from mobile_agent.device_manager import DeviceManager
from mobile_agent.tests.test_fleet import PRO, XR, Harness
from mobile_agent.tests.wireless_fixtures import Runner
from mobile_agent.wireless import api as wireless_api
from mobile_agent.wireless import monitor as monitor_module
from mobile_agent.wireless import (
    pair,
    result,
    store,
    transport,
)

KEYS = {
    "id",
    "kind",
    "name",
    "udid",
    "model",
    "modelName",
    "ios",
    "enclosure",
    "state",
    "reason",
    "primary",
    "setUp",
    "default",
    "activeRunId",
    "approval",
    "lastUsedAt",
    "nickname",
    "disconnected",
    "askBeforeActing",
    "apps",
    "defaultSet",
    "connection",
    "health",
    "holder",
    "queue",
    "queuePausedUntil",
    "owner",
    "watchdog",
    "renewal",
}


class WirelessHarness(Harness):
    def setUp(self):
        super().setUp()
        transport.forget()
        self.addCleanup(transport.forget)
        self.sims = []
        self.listing = [
            {
                "udid": PRO,
                "name": "Studio iPhone",
                "model": "iPhone16,1",
                "ios": "26.0",
                "trusted": True,
            }
        ]
        self.paired = []
        for item in (
            patch.object(DeviceManager, "watch", lambda manager, stop: None),
            patch.object(DeviceManager, "devices", lambda manager: list(self.listing)),
            patch.object(DeviceManager, "teams", lambda manager: []),
            patch.object(DeviceManager, "built", lambda manager: True),
            patch.object(DeviceManager, "expired", lambda manager, now=None: False),
            patch.object(
                DeviceManager,
                "tools",
                lambda manager: {
                    "xcodebuild": "/x",
                    "iproxy": "/i",
                    "idevice_id": "/d",
                    "ideviceinfo": "/v",
                    "git": "/g",
                    "xcode": {
                        "state": "ok",
                        "version": "Xcode 26.4",
                        "app": "/Applications/Xcode.app",
                        "fixes": [],
                        "selected": "/x",
                        "developer_dir": "/x",
                        "problems": [],
                    },
                },
            ),
            patch(
                "mobile_agent.setup_service.SetupService.developer_mode",
                lambda service, device: True,
            ),
            patch(
                "mobile_agent.installed_apps.InstalledApps.snapshot", lambda apps: None
            ),
            patch.object(pair, "run", self.fake_pair),
            patch.object(monitor_module.Monitor, "tick", lambda monitor: None),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.data = self.root / "data"
        self.data.mkdir()
        (self.data / "device.json").write_text(
            json.dumps({"udid": PRO, "device_name": "Studio iPhone", "autostart": True})
        )
        self.pair_result = True

    def fake_pair(self, udid, action, **kwargs):
        self.paired.append((udid, action))
        if isinstance(self.pair_result, Exception):
            raise self.pair_result
        return action == "enable"

    def managed(self):
        return self.runtime(
            wda_url="http://127.0.0.1:8100",
            manage_device=True,
            data_dir=str(self.data),
            socket=None,
        )


class OverviewTests(WirelessHarness):
    def test_the_track_loads_and_lists_each_phone(self):
        runtime = self.managed()
        self.assertEqual(tracks.STATUS["wireless"], "ok")
        self.assertEqual(runtime.status()["extensions"]["wireless"], "ok")
        status, body = self.request(runtime, "GET", "/api/wireless")
        self.assertEqual(status, 200)
        self.assertEqual(
            body,
            {
                "wifiTransport": False,
                "probe": {"state": "not_run", "at": None, "reason": None},
                "network": None,
                "devices": [
                    {
                        "id": PRO,
                        "udid": PRO,
                        "name": "Studio iPhone",
                        "primary": True,
                        "enabled": False,
                        "transport": None,
                        "tunnel": "unknown",
                        "reachable": False,
                        "lastSeenAt": None,
                        "probe": {"state": "not_run", "at": None, "reason": None},
                        "lastCheck": None,
                        "problem": None,
                    }
                ],
            },
        )

    def test_a_server_without_usb_phones_lists_none(self):
        status, body = self.request(self.runtime(), "GET", "/api/wireless")
        self.assertEqual(
            (status, body),
            (
                200,
                {
                    "wifiTransport": False,
                    "probe": {"state": "not_run", "at": None, "reason": None},
                    "network": None,
                    "devices": [],
                },
            ),
        )
        status, body = self.request(
            self.runtime(), "POST", f"/api/devices/{PRO}/wifi", {"enabled": True}
        )
        self.assertEqual((status, body["code"]), (409, "not_managed"))

    def test_the_monitor_closes_with_the_runtime(self):
        with patch.dict(os.environ, {"MOBSTER_WIFI_TRANSPORT": "1"}):
            runtime = self.managed()
            monitor = wireless_api.monitor_for(runtime)
            self.assertTrue(monitor.running)
            self.runtimes.remove(runtime)
            runtime.close()
        self.assertFalse(monitor.running)
        self.assertIsNone(wireless_api.monitor_for(runtime))


class ProbeResultTests(WirelessHarness):
    def public(self, ok, steps):
        return {
            "device": {"udid": PRO, "name": "Studio iPhone"},
            "ok": ok,
            "steps": steps,
            "fields": {},
        }

    def steps(self, usb="not plugged in", **extra):
        out = [{"key": "usb", "state": "info", "title": "USB", "detail": usb}]
        out += [
            {"key": key, "state": "ok", "title": key, "detail": ""}
            for key in ("coredevice", "tunnel", "wda", "lan")
        ]
        return [out[0]] + [{**item, **extra.get(item["key"], {})} for item in out[1:]]

    def test_a_failed_probe_says_which_step_and_why(self):
        steps = self.steps(
            tunnel={
                "state": "fail",
                "title": "Encrypted link",
                "detail": "the tunnel is down",
            }
        )
        result.record(self.data, PRO, self.public(False, steps), now=1_700_000_100)
        status, body = self.request(self.managed(), "GET", "/api/wireless")
        self.assertEqual(
            (body["probe"]["state"], body["devices"][0]["probe"]["reason"]),
            ("failed", "Encrypted link: the tunnel is down"),
        )
        result.record(
            self.data, PRO, self.public(False, self.steps()[:2]), now=1_700_000_200
        )
        self.assertEqual(result.get(self.data, PRO)["reason"], result.UNFINISHED)

    def test_a_probe_with_the_cable_still_plugged_in_proves_nothing_and_is_not_kept(
        self,
    ):
        result.record(
            self.data, PRO, self.public(True, self.steps()), now=1_700_000_000
        )
        self.assertIsNone(
            result.record(
                self.data,
                PRO,
                self.public(False, self.steps(usb="plugged in: unplug it")),
                now=1_700_000_300,
            )
        )
        self.assertEqual(result.get(self.data, PRO)["state"], "passed")

    def test_the_latest_probe_of_any_phone_leads_and_a_damaged_file_reads_as_not_run(
        self,
    ):
        result.record(
            self.data, PRO, self.public(True, self.steps()), now=1_700_000_000
        )
        result.record(
            self.data,
            XR,
            self.public(
                False,
                self.steps(
                    wda={"state": "fail", "title": "Helper", "detail": "no answer"}
                ),
            ),
            now=1_700_000_500,
        )
        self.assertEqual(
            (result.latest(self.data)["udid"], result.latest(self.data)["state"]),
            (XR.upper(), "failed"),
        )
        (self.data / result.FILE).write_text("{not json")
        self.assertEqual(result.latest(self.data), result.NOT_RUN)
        (self.data / result.FILE).write_text(
            json.dumps({PRO: {"state": "passed", "at": "yesterday", "reason": 5}})
        )
        self.assertEqual(
            result.get(self.data, PRO), {"state": "passed", "at": None, "reason": None}
        )


class NetworkTests(WirelessHarness):
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
        self.assertEqual(
            body,
            {
                "network": {
                    "state": "blocked",
                    "problem": {
                        "code": "network_blocked",
                        "message": "This network stops devices from seeing each other.",
                        "fix": "Use the cable or your phone's hotspot.",
                    },
                }
            },
        )
        self.assertEqual(
            self.request(runtime, "GET", "/api/wireless")[1]["network"]["state"],
            "blocked",
        )

    def test_each_state_and_the_cache(self):
        runtime = self.managed()
        self.result = {"state": "ok", "interface": "en0", "peers": 3}
        self.assertEqual(
            self.request(runtime, "GET", "/api/wireless/network")[1],
            {"network": {"state": "ok", "problem": None}},
        )
        self.result = {"state": "no_permission"}
        self.assertEqual(
            self.request(runtime, "GET", "/api/wireless/network")[1]["network"][
                "state"
            ],
            "ok",
        )
        self.assertEqual(self.checks, 1)
        wireless_api._network["at"] -= wireless_api.NETWORK_FRESH + 1
        body = self.request(runtime, "GET", "/api/wireless/network")[1]
        self.assertEqual(body["network"]["problem"]["code"], "no_local_network")
        self.assertIn("Local Network", body["network"]["problem"]["fix"])
        for state, code in (("vpn", "vpn"), ("offline", None), ("unknown", None)):
            with self.subTest(state=state):
                self.result = {"state": state}
                wireless_api._network["at"] = None
                answer = self.request(runtime, "GET", "/api/wireless/network")[1][
                    "network"
                ]
                self.assertEqual(
                    (answer["state"], (answer["problem"] or {}).get("code")),
                    (state, code),
                )

    def test_it_needs_the_token_like_every_route(self):
        from email.message import Message

        from mobile_agent.server import make_handler

        runtime = self.managed()
        for path in ("/api/wireless", "/api/wireless/network"):
            with self.subTest(path=path):
                handler = object.__new__(
                    make_handler(runtime, token="a-token-for-tests")
                )
                handler.headers = Message()
                handler.headers["Host"] = "127.0.0.1:8765"
                handler.command, handler.path = "GET", path
                handler.rfile, handler.wfile, handler.connection = (
                    io.BytesIO(),
                    io.BytesIO(),
                    Mock(),
                )
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
        status, body = self.request(
            runtime, "POST", f"/api/devices/{PRO}/wifi", {"enabled": True}
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(self.paired, [(PRO, "enable")])
        self.assertEqual((body["device"]["id"], body["device"]["enabled"]), (PRO, True))
        self.assertNotIn("next", body)
        saved = store.get(self.data, PRO)
        self.assertEqual((saved["enabled"], saved["via"]), (True, "mobster"))
        self.assertIsInstance(saved["enabledAt"], int)
        self.assertEqual(
            json.loads((self.data / "device.json").read_text())["wifi"]["udid"], PRO
        )

    def test_it_needs_the_cable_and_trust(self):
        runtime = self.managed()
        self.listing = []
        status, body = self.request(
            runtime, "POST", f"/api/devices/{PRO}/wifi", {"enabled": True}
        )
        self.assertEqual((status, body["code"]), (409, "needs_cable"))
        self.assertEqual(
            body["error"],
            "Plug Studio iPhone into this Mac with a cable to turn on Wi-Fi for it.",
        )
        self.assertEqual(
            body["fix"],
            "Plug it in, unlock it and tap Trust if it asks, then try again.",
        )
        self.listing = [{"udid": PRO, "name": "Studio iPhone", "trusted": False}]
        status, body = self.request(
            runtime, "POST", f"/api/devices/{PRO}/wifi", {"enabled": True}
        )
        self.assertEqual((status, body["code"]), (409, "not_trusted"))
        self.assertEqual(self.paired, [])
        self.assertFalse(store.enabled(self.data, PRO))

    def test_without_the_tool_the_answer_says_to_tick_it_in_xcode(self):
        runtime = self.managed()
        self.pair_result = LookupError("mobster-wifi-pair isn't available on this Mac")
        status, body = self.request(
            runtime, "POST", f"/api/devices/{PRO}/wifi", {"enabled": True}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["next"]["code"], "needs_xcode")
        self.assertIn("tick Connect via network", body["next"]["fix"])
        self.assertEqual(store.get(self.data, PRO)["via"], "xcode")

    def test_the_phone_saying_no(self):
        runtime = self.managed()
        for error, code in (
            (pair.PairError("not_trusted"), "not_trusted"),
            (pair.PairError("refused"), "refused"),
            (pair.PairError("failed", "lockdown handshake failed"), "refused"),
        ):
            self.pair_result = error
            with self.subTest(code=code):
                status, body = self.request(
                    runtime, "POST", f"/api/devices/{PRO}/wifi", {"enabled": True}
                )
                self.assertEqual((status, body["code"]), (409, code))
                self.assertTrue(body["error"] and body["fix"])
        self.pair_result = RuntimeError(
            "Devices are off in this process (MOBSTER_NO_DEVICES=1)"
        )
        status, body = self.request(
            runtime, "POST", f"/api/devices/{PRO}/wifi", {"enabled": True}
        )
        self.assertEqual((status, body["code"]), (503, "devices_off"))
        self.assertFalse(store.enabled(self.data, PRO))

    def test_turning_it_off_clears_the_flag_mobster_set(self):
        runtime = self.managed()
        self.request(runtime, "POST", f"/api/devices/{PRO}/wifi", {"enabled": True})
        status, body = self.request(
            runtime, "POST", f"/api/devices/{PRO}/wifi", {"enabled": False}
        )
        self.assertEqual((status, body["device"]["enabled"]), (200, False))
        self.assertNotIn("next", body)
        self.assertEqual(self.paired, [(PRO, "enable"), (PRO, "disable")])
        self.assertFalse(store.enabled(self.data, PRO))
        store.put(self.data, PRO, True, "xcode")
        status, body = self.request(
            runtime, "POST", f"/api/devices/{PRO}/wifi", {"enabled": False}
        )
        self.assertEqual(self.paired[-1], (PRO, "disable"))
        self.assertEqual(len(self.paired), 2)
        self.assertEqual((status, body["next"]["code"]), (200, "xcode_on"))
        self.assertIn("untick Connect via network", body["next"]["fix"])

    def test_turning_it_off_unplugged_says_the_phones_switch_is_still_on(self):
        runtime = self.managed()
        self.request(runtime, "POST", f"/api/devices/{PRO}/wifi", {"enabled": True})
        self.listing = []
        status, body = self.request(
            runtime, "POST", f"/api/devices/{PRO}/wifi", {"enabled": False}
        )
        self.assertEqual(
            (status, body["device"]["enabled"], body["next"]["code"]),
            (200, False, "still_on"),
        )
        self.assertEqual(
            body["next"]["message"],
            "Studio iPhone still accepts Wi-Fi connections from Macs it trusts: Mobster turns its Connect via "
            "network switch off only over the cable.",
        )
        self.assertEqual(self.paired, [(PRO, "enable")])

    def test_not_while_the_first_iphone_runs_a_task_as_the_server_records_it(self):
        runtime = self.managed()
        store.put(self.data, PRO, True, "mobster")
        transport.update(PRO, transport="wifi", reachable=True)
        with runtime.lock:
            runtime.active_runs["a1b2c3d4e5f6"] = "@primary"
        try:
            status, body = self.request(
                runtime, "POST", f"/api/devices/{PRO}/wifi", {"enabled": False}
            )
        finally:
            with runtime.lock:
                runtime.active_runs.pop("a1b2c3d4e5f6", None)
        self.assertEqual((status, body["code"]), (409, "busy"))
        self.assertTrue(store.enabled(self.data, PRO))
        with patch.object(type(runtime.fleet), "in_use", lambda fleet, phone: True):
            status, body = self.request(
                runtime, "POST", f"/api/devices/{PRO}/wifi", {"enabled": False}
            )
        self.assertEqual((status, body["code"]), (409, "busy"))
        status, _ = self.request(
            runtime, "POST", f"/api/devices/{PRO}/wifi", {"enabled": False}
        )
        self.assertEqual(status, 200)
        self.assertFalse(store.enabled(self.data, PRO))

    def test_bad_requests_and_other_devices(self):
        runtime = self.managed()
        for body in ({}, {"enabled": "yes"}, {"enabled": True, "extra": 1}, [True]):
            with self.subTest(body=body):
                self.assertEqual(
                    self.request(runtime, "POST", f"/api/devices/{PRO}/wifi", body)[0],
                    400,
                )
        status, body = self.request(
            runtime, "POST", "/api/devices/nope/wifi", {"enabled": True}
        )
        self.assertEqual((status, body["code"]), (404, "device_not_found"))
        self.listing.append(
            {"udid": XR, "name": "Work iPhone", "model": "iPhone11,8", "trusted": True}
        )
        runtime.fleet.cache.pop("usb", None)
        status, body = self.request(
            runtime, "POST", f"/api/devices/{XR}/wifi", {"enabled": True}
        )
        self.assertEqual((status, body["code"]), (409, "not_set_up"))
        self.assertEqual(body["error"], "Work iPhone isn't set up in Mobster yet.")
        status, body = self.request(
            runtime, "POST", "/api/devices/Studio%20iPhone/wifi", {"enabled": True}
        )
        self.assertEqual(status, 200)


class SettingTests(WirelessHarness):
    def test_the_hidden_setting_is_saved_and_starts_the_monitor(self):
        runtime = self.managed()
        self.assertIsNone(wireless_api.monitor_for(runtime))
        with patch.dict(os.environ, {}):
            status, body = self.request(
                runtime, "POST", "/api/wireless", {"wifiTransport": True}
            )
            self.assertEqual((status, body["wifiTransport"]), (200, True))
            self.assertEqual(os.environ.get("MOBSTER_WIFI_TRANSPORT"), "1")
            env = Path(runtime.setup.env_file).read_text()
            self.assertIn("MOBSTER_WIFI_TRANSPORT=1", env)
            monitor = wireless_api.monitor_for(runtime)
            self.assertTrue(monitor.running)
            status, body = self.request(
                runtime, "POST", "/api/wireless/reconnect", {"device": PRO}
            )
            self.assertEqual((status, body), (202, {"device": PRO, "accepted": True}))
            status, body = self.request(
                runtime, "POST", "/api/wireless", {"wifiTransport": False}
            )
            self.assertEqual((status, body["wifiTransport"]), (200, False))
            self.assertEqual(os.environ.get("MOBSTER_WIFI_TRANSPORT"), "0")
            self.assertIn(
                "MOBSTER_WIFI_TRANSPORT=0", Path(runtime.setup.env_file).read_text()
            )
            self.assertFalse(monitor.running)
        for body in ({}, {"wifiTransport": 1}, {"wifiTransport": True, "x": 1}):
            with self.subTest(body=body):
                self.assertEqual(
                    self.request(runtime, "POST", "/api/wireless", body)[0], 400
                )

    def test_reconnect_says_when_wifi_for_iphones_is_off(self):
        runtime = self.managed()
        status, body = self.request(
            runtime, "POST", "/api/wireless/reconnect", {"device": PRO}
        )
        self.assertEqual((status, body["code"]), (409, "off"))
        self.assertEqual(
            self.request(runtime, "POST", "/api/wireless/reconnect", {})[0], 400
        )


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
            self.listing = []
            runtime.manager.runner, runtime.manager.transport = Runner(), "wifi"
            transport.update(PRO, transport="wifi", reachable=True, tunnel="up")
            _, body = self.request(runtime, "GET", "/api/devices")
            item = body["devices"][0]
            self.assertEqual(set(item), KEYS | {"transport", "wifi"})
            self.assertEqual(
                (item["id"], item["state"], item["transport"], item["wifi"]),
                (PRO, "ready", "wifi", {"enabled": True, "reachable": True}),
            )
            _, body = self.request(runtime, "GET", "/api/wireless")
            row = body["devices"][0]
            self.assertEqual(
                (row["transport"], row["tunnel"], row["reachable"]),
                ("wifi", "up", True),
            )

    def test_in_use_sees_this_servers_tasks_and_other_processes(self):
        runtime = self.managed()
        runtime.fleet.inventory()
        phone, udid, _manager = runtime.fleet.usb_managers()[0]
        self.assertEqual(udid, PRO)
        held = set()
        with patch(
            "mobile_agent.devices.lease_free",
            side_effect=lambda url, udid=None: url.rstrip("/") not in held,
        ):
            self.assertFalse(runtime.fleet.in_use(phone))
            held.add("http://127.0.0.1:8100")
            self.assertTrue(runtime.fleet.in_use(phone))
        runtime.active_runs["3f9c2a1b7d0e"] = phone.key
        self.assertTrue(runtime.fleet.in_use(phone))


class ProbeRouteTests(WirelessHarness):
    """POST /api/wireless/probe: the Wi-Fi check Add a device runs, in the background, one at a time."""

    def fake_probe(self, steps, gate=None):
        from mobile_agent.wireless import probe as probe_module

        class Fake(probe_module.Probe):
            def run(self, usb_attached):
                for key, state, title, detail in steps:
                    self.step(key, state, title, detail)
                    if gate is not None and key == "usb":
                        gate.wait(30)
                return self

        return patch.object(probe_module, "Probe", Fake)

    def settled(self, runtime, state):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            check = self.request(runtime, "GET", "/api/wireless")[1]["devices"][0]["lastCheck"]
            if check and check["state"] == state:
                return check
            time.sleep(.02)
        self.fail(f"the check never reached {state}")

    def test_a_reachable_phone_passes_and_is_kept(self):
        runtime = self.managed()
        steps = [("usb", "info", "USB", "not plugged in")] + [
            (key, "ok", key, "") for key in ("coredevice", "tunnel", "wda", "lan")]
        with self.fake_probe(steps):
            status, body = self.request(runtime, "POST", "/api/wireless/probe", {"device": PRO})
            self.assertEqual((status, body["accepted"]), (202, True))
            check = self.settled(runtime, "passed")
        self.assertIsNone(check["next"])
        self.assertEqual(result.get(runtime.fleet.data_dir, PRO)["state"], "passed")

    def test_an_unreachable_phone_fails_with_its_one_next_step(self):
        runtime = self.managed()
        steps = [("usb", "info", "USB", "not plugged in"),
                 ("coredevice", "fail", "CoreDevice", "doesn't see it over the network")]
        with self.fake_probe(steps):
            self.request(runtime, "POST", "/api/wireless/probe", {})
            check = self.settled(runtime, "failed")
        self.assertEqual(check["next"]["code"], "not_seen")
        self.assertIn("same Wi-Fi", check["next"]["fix"])
        self.assertEqual(result.get(runtime.fleet.data_dir, PRO)["state"], "failed")

    def test_a_plugged_in_phone_is_told_to_unplug_and_nothing_is_kept(self):
        runtime = self.managed()
        with self.fake_probe([("usb", "info", "USB", "plugged in: unplug it to probe Wi-Fi"),
                              ("coredevice", "warn", "CoreDevice", "sees it on USB")]):
            self.request(runtime, "POST", "/api/wireless/probe", {"device": PRO})
            check = self.settled(runtime, "plugged_in")
        self.assertEqual(check["next"]["code"], "plugged_in")
        self.assertEqual(result.get(runtime.fleet.data_dir, PRO)["state"], "not_run")

    def test_a_check_that_never_ends_times_out_and_a_late_end_changes_nothing(self):
        runtime = self.managed()
        gate = threading.Event()
        steps = [("usb", "info", "USB", "not plugged in")] + [
            (key, "ok", key, "") for key in ("coredevice", "tunnel", "wda", "lan")]
        with self.fake_probe(steps, gate), patch.object(wireless_api, "PROBE_SECONDS", .3):
            self.request(runtime, "POST", "/api/wireless/probe", {"device": PRO})
            body = self.request(runtime, "GET", "/api/wireless")[1]["devices"][0]
            self.assertEqual((body["probe"]["state"], body["lastCheck"]["state"]), ("running", "running"))
            self.assertEqual(body["lastCheck"]["step"], "USB")
            check = self.settled(runtime, "timeout")
            self.assertEqual(check["next"]["code"], "probe_timeout")
            gate.set()
            time.sleep(.3)
        self.assertEqual(self.request(runtime, "GET", "/api/wireless")[1]["devices"][0]["lastCheck"]["state"],
                         "timeout")
        self.assertEqual(result.get(runtime.fleet.data_dir, PRO)["state"], "not_run")

    def test_it_refuses_a_second_check_and_a_phone_a_task_holds(self):
        runtime = self.managed()
        gate = threading.Event()
        self.addCleanup(gate.set)
        with self.fake_probe([("usb", "info", "USB", "not plugged in")], gate):
            self.request(runtime, "POST", "/api/wireless/probe", {"device": PRO})
            status, body = self.request(runtime, "POST", "/api/wireless/probe", {"device": PRO})
            self.assertEqual((status, body["code"]), (409, "probe_running"))
            gate.set()
            self.settled(runtime, "failed")
            with patch.object(wireless_api, "task_on", lambda *args: True):
                status, body = self.request(runtime, "POST", "/api/wireless/probe", {"device": PRO})
                self.assertEqual((status, body["code"]), (409, "busy"))
        for bad in ({"start": 1}, {"x": 1}, {"device": 3}):
            self.assertEqual(self.request(runtime, "POST", "/api/wireless/probe", bad)[0], 400)
