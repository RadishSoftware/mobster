import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mobile_agent import (
    doctor,
    native_helpers,
)
from mobile_agent.tests.test_fleet import PRO, XR
from mobile_agent.tests.wireless_fixtures import TUNNEL, VPN_IFCONFIG, run, tunnel_reader
from mobile_agent.wireless import (
    cli,
    devicectl,
    pair,
    probe,
    result,
    store,
)
from mobile_agent.wireless.devicectl import DevicectlError, Peer
from mobile_agent.wireless.network import tunnel_route as TUNNEL_ROUTE


def record(udid, name, primary=True, wda_port=8100):
    return {
        "id": udid,
        "kind": "usb",
        "name": name,
        "udid": udid,
        "model": "iPhone16,1",
        "modelName": "iPhone 15 Pro",
        "ios": "26.0",
        "state": "disconnected",
        "reason": None,
        "primary": primary,
        "setUp": True,
        "wdaUrl": f"http://127.0.0.1:{wda_port}",
        "mjpegUrl": f"http://127.0.0.1:{wda_port + 1000}",
    }


class CliHarness(unittest.TestCase):
    def setUp(self):
        self.data = Path(tempfile.mkdtemp(prefix="mobster-wifi-cli-"))
        (self.data / "device.json").write_text(
            json.dumps({"udid": PRO, "device_name": "Studio iPhone"})
        )
        self.records = [record(PRO, "Studio iPhone")]
        self.listing = [{"udid": PRO, "name": "Studio iPhone", "trusted": True}]
        self.attached = {PRO: True}
        self.answers = set()
        self.paired = []
        self.pair_result = True
        self.peers = {}
        for item in (
            mock.patch.object(cli, "_data_dir", lambda: self.data),
            mock.patch(
                "mobile_agent.devices.discover",
                lambda *a, **k: [dict(r) for r in self.records],
            ),
            mock.patch(
                "mobile_agent.device_manager.DeviceManager.devices",
                lambda manager: list(self.listing),
            ),
            mock.patch(
                "mobile_agent.mac_tools.usb_attached",
                lambda udid, **_: self.attached.get(udid, False),
            ),
            mock.patch(
                "mobile_agent.devices.wda_answers",
                lambda url, timeout=1.0: url in self.answers,
            ),
            mock.patch.object(pair, "run", self.fake_pair),
            mock.patch.object(devicectl, "list_devices", self.fake_list),
            mock.patch.dict(os.environ, {"MOBSTER_WIFI_TRANSPORT": "0"}),
        ):
            item.start()
            self.addCleanup(item.stop)

    def fake_pair(self, udid, action, **kwargs):
        self.paired.append((udid, action))
        if isinstance(self.pair_result, Exception):
            raise self.pair_result
        return action == "enable"

    def fake_list(self, **kwargs):
        if isinstance(self.peers, Exception):
            raise self.peers
        return dict(self.peers)


class CliSwitchTests(CliHarness):
    def test_on_with_the_cable(self):
        code, out, err = run(["wifi", "on", "--device", "Studio iPhone"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.paired, [(PRO, "enable")])
        self.assertIn("Wi-Fi is on for Studio iPhone.", out)
        self.assertIn('mobster wifi probe --device "Studio iPhone"', out)
        self.assertIn("set MOBSTER_WIFI_TRANSPORT=1 in your env file", out)
        self.assertTrue(store.enabled(self.data, PRO))
        code, out, _ = run(["wifi", "on", "--json"])
        body = json.loads(out)
        self.assertEqual(
            (code, body["ok"], body["device"]["udid"], body["wifiTransport"]),
            (0, True, PRO, False),
        )

    def test_on_needs_the_cable_and_trust(self):
        self.listing = []
        code, out, err = run(["wifi", "on"])
        self.assertEqual(code, 1)
        self.assertEqual(
            err.splitlines(),
            [
                "Plug Studio iPhone into this Mac with a cable to turn on Wi-Fi for it.",
                "Plug it in, unlock it and tap Trust if it asks, then try again.",
            ],
        )
        self.listing = [{"udid": PRO, "trusted": False}]
        code, out, _ = run(["wifi", "on", "--json"])
        self.assertEqual((code, json.loads(out)["code"]), (1, "not_trusted"))
        self.assertEqual(self.paired, [])
        self.assertFalse(store.enabled(self.data, PRO))

    def test_off_clears_what_mobster_set(self):
        run(["wifi", "on"])
        code, out, _ = run(["wifi", "off"])
        self.assertEqual(code, 0)
        self.assertEqual(
            out.strip(),
            "Wi-Fi is off for Studio iPhone. Mobster uses it only with the cable.",
        )
        self.assertEqual(self.paired, [(PRO, "enable"), (PRO, "disable")])
        self.assertFalse(store.enabled(self.data, PRO))

    def test_on_notes_the_switch_before_it_is_sent_so_an_interrupted_on_still_shows_in_off(self):
        sent = []

        def interrupted(udid, action, **kwargs):
            sent.append((action, store.get(self.data, udid)))
            raise KeyboardInterrupt   # ctrl+c, or the process ending, after the phone took the switch

        with mock.patch.object(pair, "run", interrupted):
            self.assertEqual(run(["wifi", "on"])[0], 130)
        self.assertEqual(
            sent, [("enable", {"enabled": False, "enabledAt": None, "via": "mobster"})]
        )
        self.listing = []
        code, _, err = run(["wifi", "off"])
        self.assertEqual(code, 1)
        self.assertIn("still accepts Wi-Fi connections", err)

    def test_a_no_from_the_phone_puts_the_record_back_and_a_lost_answer_keeps_the_note(self):
        self.pair_result = pair.PairError("refused", "the iPhone refused the change (-1)")
        self.assertEqual(run(["wifi", "on"])[0], 1)
        self.assertEqual(store.get(self.data, PRO), store.OFF)
        self.pair_result = pair.PairError("failed", "mobster-wifi-pair didn't answer in 20 s")
        self.assertEqual(run(["wifi", "on"])[0], 1)
        self.assertEqual(
            store.get(self.data, PRO), {"enabled": False, "enabledAt": None, "via": "mobster"}
        )
        self.pair_result = True
        self.assertEqual(run(["wifi", "off"])[0], 0)
        self.assertEqual(self.paired[-1], (PRO, "disable"))
        self.assertEqual(store.get(self.data, PRO), store.OFF)

    def test_off_unplugged_says_the_phones_switch_is_still_on_until_an_off_on_the_cable(self):
        run(["wifi", "on"])
        self.listing = []
        code, out, err = run(["wifi", "off"])
        self.assertEqual(code, 1)
        self.assertEqual(
            out.strip(),
            "Wi-Fi is off for Studio iPhone. Mobster uses it only with the cable.",
        )
        self.assertEqual(
            err.splitlines(),
            [
                "Studio iPhone still accepts Wi-Fi connections from Macs it trusts: Mobster turns its Connect "
                "via network switch off only over the cable.",
                "Plug Studio iPhone in and unlock it, then turn off Wi-Fi for it again.",
                'Plugged in and unlocked: mobster wifi off --device "Studio iPhone"',
            ],
        )
        self.assertEqual(self.paired, [(PRO, "enable")])
        self.assertEqual(
            (store.enabled(self.data, PRO), store.get(self.data, PRO)["via"]),
            (False, "mobster"),
        )
        self.listing = [{"udid": PRO, "trusted": False}]
        code, out, _ = run(["wifi", "off", "--json"])
        body = json.loads(out)
        self.assertEqual(
            (code, body["ok"], body["code"], body["device"]["enabled"]),
            (1, False, "still_on", False),
        )
        self.listing = [{"udid": PRO, "name": "Studio iPhone", "trusted": True}]
        code, out, err = run(["wifi", "off", "--json"])
        body = json.loads(out)
        self.assertEqual((code, body["ok"], "next" in body, err), (0, True, False, ""))
        self.assertEqual(self.paired, [(PRO, "enable"), (PRO, "disable")])
        self.assertIsNone(store.get(self.data, PRO)["via"])

    def test_off_says_when_the_switch_wont_turn_off_or_xcode_turned_it_on(self):
        run(["wifi", "on"])
        self.pair_result = pair.PairError("refused", "the iPhone refused the change (-1)")
        code, out, _ = run(["wifi", "off", "--json"])
        self.assertEqual(
            (code, json.loads(out)["code"], store.get(self.data, PRO)["via"]),
            (1, "off_failed", "mobster"),
        )
        self.pair_result = True
        store.put(self.data, PRO, True, "xcode")
        self.paired.clear()
        code, out, err = run(["wifi", "off"])
        self.assertEqual((code, err, self.paired), (0, "", []))
        self.assertEqual(
            out.splitlines(),
            [
                "Wi-Fi is off for Studio iPhone. Mobster uses it only with the cable.",
                "Studio iPhone may still accept Wi-Fi connections from Macs it trusts: Connect via network was "
                "turned on for it in Xcode, so Mobster left it on.",
                "In Xcode, open Window › Devices and Simulators, select Studio iPhone and untick Connect via network.",
            ],
        )
        code, out, _ = run(["wifi", "off", "--json"])
        self.assertEqual((code, json.loads(out)["ok"], "next" in json.loads(out)), (0, True, False))

    def test_naming_and_choosing(self):
        code, _, err = run(["wifi", "on", "--device", "nope"])
        self.assertEqual(code, 3)
        self.assertIn("No device is named", err)
        self.records.append(record(XR, "Work iPhone", primary=False, wda_port=8101))
        self.records[0]["primary"] = False
        code, _, err = run(["wifi", "on"])
        self.assertEqual(code, 2)
        self.assertIn("Several iPhones are here", err)
        self.records = [{**record(XR, "Sim", primary=False), "kind": "simulator"}]
        code, _, err = run(["wifi", "on", "--device", "Sim"])
        self.assertEqual(code, 2)
        self.assertIn("Wi-Fi is for iPhones set up with a cable", err)

    def test_a_phone_not_set_up_here(self):
        (self.data / "device.json").write_text(json.dumps({"udid": XR}))
        code, _, err = run(["wifi", "on"])
        self.assertEqual(code, 1)
        self.assertIn("isn't set up in Mobster yet", err)
        self.assertEqual(self.paired, [])


class StatusTests(CliHarness):
    def test_each_phone_and_how_it_is_connected(self):
        self.records.append(record(XR, "Work iPhone", primary=False, wda_port=8101))
        devices = {
            "version": 1,
            "phones": [{"udid": XR, "slot": 1, "wdaPort": 8101, "mjpegPort": 9101}],
            "lastUsed": None,
        }
        (self.data / "devices.json").write_text(json.dumps(devices))
        store.put(self.data, XR, True, "mobster")
        self.attached = {PRO: True, XR: False}
        self.answers = {"http://127.0.0.1:8101"}
        code, out, _ = run(["wifi", "status"])
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertIn("off in this Mobster", lines[0])
        self.assertRegex(lines[1], r"Studio iPhone +On cable +Wi-Fi off")
        self.assertRegex(lines[2], r"Work iPhone +On Wi-Fi, encrypted +Wi-Fi on")
        self.answers = set()
        self.peers = {XR: Peer(udid=XR, transport="localNetwork", tunnel="down")}
        code, out, _ = run(["wifi", "status", "--json"])
        body = json.loads(out)
        self.assertEqual(body["wifiTransport"], False)
        work = body["devices"][1]
        self.assertEqual(
            (work["enabled"], work["transport"], work["tunnel"]), (True, None, "down")
        )
        self.assertIn("Not reachable over Wi-Fi", work["connection"])

    def test_devicectl_failing_doesnt_stop_status(self):
        store.put(self.data, PRO, True, "mobster")
        self.attached = {PRO: False}
        self.peers = DevicectlError("hung", "hang")
        code, out, _ = run(["wifi", "status", "--json"])
        self.assertEqual(
            (code, json.loads(out)["devices"][0]["connection"]),
            (0, "Not reachable over Wi-Fi"),
        )


class FakeRelay:
    started = []

    def __init__(self, target, pairs, name="relay", **kwargs):
        self.target, self.pairs = target, pairs
        FakeRelay.started.append(self)

    def start(self):
        return self

    def stop(self):
        pass

    @property
    def ports(self):
        return [50000 + index for index, _ in enumerate(self.pairs)]


class ProbeTests(CliHarness):
    def setUp(self):
        super().setUp()
        FakeRelay.started = []
        self.attached = {PRO: False}
        self.peers = {
            PRO: Peer(
                udid=PRO,
                transport="localNetwork",
                tunnel="up",
                tunnel_state="connected",
                address=TUNNEL,
                hostnames=("Sams-iPhone.local",),
                fields={
                    "transportType": "localNetwork",
                    "tunnelState": "connected",
                    "tunnelIPAddress": TUNNEL,
                },
            )
        }
        self.status = {"ready": True}
        self.open_ports = set()
        self.gateway, self.wda_answers = None, set()
        self.read_route = tunnel_reader()
        for item in (
            mock.patch.object(native_helpers, "devices_allowed", lambda: True),
            mock.patch.object(probe, "Relay", FakeRelay),
            mock.patch.object(
                probe, "http_status", lambda port, timeout=3.0: self.status
            ),
            mock.patch.object(probe, "stream_answers", lambda port, timeout=3.0: True),
            mock.patch.object(probe, "usbmux_network", lambda udid: True),
            mock.patch(
                "mobile_agent.wireless.network.default_gateway",
                lambda reader=None: self.gateway,
            ),
            mock.patch(
                "mobile_agent.wireless.exposure.speaks_wda",
                lambda host, port, timeout=1.5, connect=None: (
                    (host, port) in self.wda_answers
                ),
            ),
            mock.patch.object(devicectl, "bring_up", lambda udid: {}),
            mock.patch.object(devicectl, "run_json", self.devicectl_answer),
            mock.patch(
                "mobile_agent.wireless.network.resolve",
                lambda names, **k: {
                    "ipv4": ["192.168.1.20"],
                    "ipv6": ["fe80::1c2b%en0"],
                },
            ),
            mock.patch(
                "mobile_agent.wireless.network.port_open",
                lambda host, port, timeout=2.0: (host, port) in self.open_ports,
            ),
            mock.patch(
                "mobile_agent.wireless.network.tunnel_route",
                lambda address, reader=None: TUNNEL_ROUTE(address, self.read_route),
            ),
            mock.patch(
                "mobile_agent.wireless.network.peer_check",
                lambda **k: {"state": "ok", "interface": "en0", "peers": 3},
            ),
        ):
            item.start()
            self.addCleanup(item.stop)

    def devicectl_answer(self, args, timeout, **kwargs):
        name = {"lockState": "lock_state_unlocked.json", "apps": "apps.json"}[args[2]]
        if self.unreadable:
            return {"result": {}}
        return json.loads(
            (Path(__file__).parent / "fixtures" / "devicectl" / name).read_text()
        )

    unreadable = False

    def test_every_check_passes(self):
        code, out, _ = run(["wifi", "probe", "--json"])
        body = json.loads(out)
        self.assertEqual((code, body["ok"], body["tunnelAddress"]), (0, True, TUNNEL))
        states = {step["key"]: step["state"] for step in body["steps"]}
        self.assertEqual(
            states,
            {
                "usb": "info",
                "network": "ok",
                "fields": "info",
                "coredevice": "ok",
                "tunnel": "ok",
                "readers": "info",
                "wda": "ok",
                "app_relay": "info",
                "lan": "ok",
                "usbmux": "info",
            },
        )
        self.assertEqual(body["fields"]["tunnelState"], "connected")
        self.assertEqual([relay.target for relay in FakeRelay.started], [TUNNEL])
        self.assertEqual(FakeRelay.started[0].pairs, [(0, 8100), (0, 9100)])

    def test_the_result_is_kept_for_the_app_and_a_probe_on_the_cable_is_not(self):
        self.assertEqual(result.get(self.data, PRO)["state"], "not_run")
        self.attached[PRO] = True
        run(["wifi", "probe", "--json"])
        self.assertEqual(result.get(self.data, PRO)["state"], "not_run")
        self.attached[PRO] = False
        run(["wifi", "probe", "--json"])
        self.assertEqual(result.get(self.data, PRO)["state"], "passed")
        self.status = None
        code, _, _ = run(["wifi", "probe", "--json"])
        kept = result.get(self.data, PRO)
        self.assertEqual((code, kept["state"]), (1, "failed"))
        self.assertTrue(kept["reason"])

    def test_plain_lines(self):
        code, out, _ = run(["wifi", "probe"])
        self.assertEqual(code, 0)
        self.assertIn("✓ Encrypted tunnel", out)
        self.assertIn(f"up, the phone's end is {TUNNEL}", out)
        self.assertTrue(out.rstrip().endswith("Every check passed."))

    def test_a_helper_answering_on_the_hotspot_router_fails(self):
        self.peers[PRO] = Peer(
            udid=PRO,
            transport="localNetwork",
            tunnel="up",
            tunnel_state="connected",
            address=TUNNEL,
            fields={"tunnelState": "connected"},
        )
        self.gateway, self.wda_answers = "172.20.10.1", {("172.20.10.1", 9100)}
        code, out, _ = run(["wifi", "probe", "--json"])
        lan = next(step for step in json.loads(out)["steps"] if step["key"] == "lan")
        self.assertEqual((code, lan["state"]), (1, "fail"))
        self.assertIn("172.20.10.1:9100", lan["detail"])

    def test_wda_answering_on_the_wifi_address_fails_loudly(self):
        self.open_ports = {("192.168.1.20", 8100)}
        code, out, _ = run(["wifi", "probe", "--json"])
        body = json.loads(out)
        lan = next(step for step in body["steps"] if step["key"] == "lan")
        self.assertEqual((code, body["ok"], lan["state"]), (1, False, "fail"))
        self.assertIn("ANSWERS on 192.168.1.20:8100", lan["detail"])

    def test_not_seen_tunnel_down_and_a_lan_address(self):
        self.peers = {}
        body = json.loads(run(["wifi", "probe", "--json"])[1])
        self.assertEqual(body["steps"][-1]["key"], "coredevice")
        self.peers = {
            PRO: Peer(
                udid=PRO,
                transport="localNetwork",
                tunnel="down",
                tunnel_state="disconnected",
            )
        }
        body = json.loads(run(["wifi", "probe", "--json"])[1])
        self.assertEqual(
            (body["steps"][-1]["key"], body["steps"][-1]["state"]), ("tunnel", "fail")
        )
        self.peers = {
            PRO: Peer(
                udid=PRO, transport="localNetwork", tunnel="up", address="192.168.1.20"
            )
        }
        body = json.loads(run(["wifi", "probe", "--json"])[1])
        self.assertIn("isn't an fd00::/8 tunnel address", body["steps"][-1]["detail"])
        self.assertEqual(FakeRelay.started, [])

    def test_a_tunnel_address_reached_through_wifi_or_a_vpn_is_refused(self):
        for reader, said in (
            (tunnel_reader(interface="en0"), "through en0, not a tunnel (utun)"),
            (
                tunnel_reader(interface="utun5", ifconfig=VPN_IFCONFIG),
                "through utun5, which is a VPN, not the tunnel",
            ),
        ):
            with self.subTest(said=said):
                self.read_route = reader
                code, out, _ = run(["wifi", "probe", "--json"])
                body = json.loads(out)
                self.assertEqual(
                    (code, body["steps"][-1]["key"], body["steps"][-1]["state"]),
                    (1, "tunnel", "fail"),
                )
                self.assertIn(said, body["steps"][-1]["detail"])
                self.assertTrue(body["steps"][-1]["detail"].endswith("so Mobster won't use it"))
                self.assertIsNone(body["tunnelAddress"])
                self.assertEqual(FakeRelay.started, [])

    def test_devices_off(self):
        with mock.patch.object(native_helpers, "devices_allowed", lambda: False):
            code, _, err = run(["wifi", "probe"])
        self.assertEqual(code, 3)
        self.assertIn("MOBSTER_NO_DEVICES=1", err)


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.data = Path(tempfile.mkdtemp(prefix="mobster-wifi-doctor-"))
        (self.data / "device.json").write_text(
            json.dumps({"udid": PRO, "device_name": "Studio iPhone"})
        )
        env = mock.patch.dict(os.environ, {"MOBSTER_WIFI_TRANSPORT": "1"})
        env.start()
        self.addCleanup(env.stop)

    def checks(self, peers, **kwargs):
        kwargs = {
            "nwi": "IPv4 network interface information\n     en0 : flags : 0x5 (IPv4,DNS)\n",
            "gateway": "192.168.1.1",
            "local_network": True,
            "network_result": {"state": "ok", "interface": "en0", "peers": 4},
            **kwargs,
        }
        return {
            check.key: check
            for check in doctor.check_wifi(
                PRO, self.data, "Studio iPhone", peers=peers, **kwargs
            )
        }

    def test_problems_each_with_one_fix(self):
        store.put(self.data, PRO, True, "mobster")
        vpn = "IPv4 network interface information\n     utun4 : flags : 0x5 (IPv4,DNS)\n     en0 : flags : 0x5\n"
        found = self.checks(
            {PRO: Peer(udid=PRO, transport=None, developer_mode="disabled")},
            nwi=vpn,
            local_network=False,
        )
        self.assertEqual(
            (
                found["wifi"].state,
                found["devmode"].state,
                found["localnet"].state,
                found["vpn"].state,
            ),
            ("warn", "fail", "fail", "warn"),
        )
        for check in found.values():
            if check.state in ("warn", "fail"):
                with self.subTest(check=check.key):
                    self.assertTrue(check.fix)
        self.assertIn("utun4", found["vpn"].detail)
        found = self.checks(
            {PRO: Peer(udid=PRO, transport="localNetwork", tunnel="down")}
        )
        self.assertIn("encrypted link isn't up yet", found["wifi"].detail)
        found = self.checks({})
        self.assertIn("doesn't list Studio iPhone", found["wifi"].detail)

    def test_a_network_that_keeps_devices_apart_is_named_with_its_fix(self):
        store.put(self.data, PRO, True, "mobster")
        blocked = {"state": "blocked", "interface": "en0", "peers": 0}
        found = self.checks(
            {PRO: Peer(udid=PRO, transport=None)}, network_result=blocked
        )
        self.assertEqual(found["peers"].state, "fail")
        self.assertEqual(
            found["peers"].detail, "This network stops devices from seeing each other."
        )
        self.assertEqual(found["peers"].fix, "Use the cable or your phone's hotspot.")
        report = doctor.plain_report(list(found.values()))
        self.assertIn("This network stops devices from seeing each other.", report)
        self.assertIn("→ Use the cable or your phone's hotspot.", report)
        found = self.checks(
            {PRO: Peer(udid=PRO, transport="wired")}, network_result=blocked
        )
        self.assertEqual(found["peers"].state, "warn")
        found = self.checks(
            {PRO: Peer(udid=PRO, transport=None)}, network_result={"state": "offline"}
        )
        self.assertEqual(
            (found["peers"].state, bool(found["peers"].fix)), ("warn", True)
        )
        found = self.checks(
            {PRO: Peer(udid=PRO, transport=None)}, network_result={"state": "unknown"}
        )
        self.assertEqual(found["peers"].state, "skip")
        for state in ("no_permission", "vpn"):
            with self.subTest(state=state):
                self.assertNotIn(
                    "peers", self.checks({}, network_result={"state": state})
                )
