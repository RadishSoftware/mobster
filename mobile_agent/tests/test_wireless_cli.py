"""Track wireless: `mobster wifi status|on|off|probe` (SPEC §3.7, CLI), the owner's probe step by step, and the Doctor
lines for a phone with Wi-Fi on. Fakes only: the USB listing, devicectl, the pair tool, the relay's target and the
network are stand-ins; nothing reaches a phone and nothing binds outside 127.0.0.1."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from mobile_agent import __main__ as main_module
from mobile_agent import doctor, native_helpers
from mobile_agent.extensions import Hooks
from mobile_agent.wireless import cli, devicectl, pair, probe, store
from mobile_agent.wireless.devicectl import DevicectlError, Peer

PRO = "00008130-001A2B3C4D5E6F70"
XR = "00008020-000A1B2C3D4E5F60"
TUNNEL = "fd7a:1c2b:3d4e::1"


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(main_module, "load_extensions", return_value=Hooks()), redirect_stdout(out), \
            redirect_stderr(err):
        try:
            code = main_module.main(argv)
        except SystemExit as done:      # --help
            code = done.code
    return code, out.getvalue(), err.getvalue()


def record(udid, name, primary=True, wda_port=8100):
    return {"id": udid, "kind": "usb", "name": name, "udid": udid, "model": "iPhone16,1", "modelName": "iPhone 15 Pro",
            "ios": "26.0", "state": "disconnected", "reason": None, "primary": primary, "setUp": True,
            "wdaUrl": f"http://127.0.0.1:{wda_port}", "mjpegUrl": f"http://127.0.0.1:{wda_port + 1000}"}


class CliHarness(unittest.TestCase):
    def setUp(self):
        self.data = Path(tempfile.mkdtemp(prefix="mobster-wifi-cli-"))
        (self.data / "device.json").write_text(json.dumps({"udid": PRO, "device_name": "Sam's iPhone"}))
        self.records = [record(PRO, "Sam's iPhone")]
        self.listing = [{"udid": PRO, "name": "Sam's iPhone", "trusted": True}]
        self.attached = {PRO: True}
        self.answers = set()
        self.paired = []
        self.pair_result = True
        self.peers = {}
        for item in (mock.patch.object(cli, "_data_dir", lambda: self.data),
                     mock.patch("mobile_agent.devices.discover", lambda *a, **k: [dict(r) for r in self.records]),
                     mock.patch("mobile_agent.device_manager.DeviceManager.devices",
                                lambda manager: list(self.listing)),
                     mock.patch("mobile_agent.device_manager.usb_attached",
                                lambda udid, **_: self.attached.get(udid, False)),
                     mock.patch("mobile_agent.devices.wda_answers", lambda url, timeout=1.0: url in self.answers),
                     mock.patch.object(pair, "run", self.fake_pair),
                     mock.patch.object(devicectl, "list_devices", self.fake_list),
                     mock.patch.dict(os.environ, {"MOBSTER_WIFI_TRANSPORT": ""})):
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


class SwitchTests(CliHarness):
    def test_on_with_the_cable(self):
        code, out, err = run(["wifi", "on", "--device", "Sam's iPhone"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.paired, [(PRO, "enable")])
        self.assertIn("Wi-Fi is on for Sam's iPhone.", out)
        self.assertIn("mobster wifi probe --device \"Sam's iPhone\"", out)
        self.assertIn("add MOBSTER_WIFI_TRANSPORT=1 to your env file", out)   # the hidden switch is still off
        self.assertTrue(store.enabled(self.data, PRO))
        code, out, _ = run(["wifi", "on", "--json"])                          # the only iPhone: no --device needed
        body = json.loads(out)
        self.assertEqual((code, body["ok"], body["device"]["udid"], body["wifiTransport"]), (0, True, PRO, False))

    def test_on_needs_the_cable_and_trust(self):
        self.listing = []
        code, out, err = run(["wifi", "on"])
        self.assertEqual(code, 1)
        self.assertEqual(err.splitlines(), ["Plug Sam's iPhone into this Mac with a cable to turn on Wi-Fi for it.",
                                            "Plug it in, unlock it and tap Trust if it asks, then try again."])
        self.listing = [{"udid": PRO, "trusted": False}]
        code, out, _ = run(["wifi", "on", "--json"])
        self.assertEqual((code, json.loads(out)["code"]), (1, "not_trusted"))
        self.assertEqual(self.paired, [])
        self.assertFalse(store.enabled(self.data, PRO))

    def test_on_without_the_tool_says_the_xcode_step(self):
        self.pair_result = LookupError("mobster-wifi-pair isn't available on this Mac")
        code, out, _ = run(["wifi", "on"])
        self.assertEqual(code, 0)
        self.assertIn("Mobster couldn't turn on Wi-Fi for Sam's iPhone by itself.", out)
        self.assertIn("Window › Devices and Simulators, select Sam's iPhone and tick Connect via network.", out)
        self.assertEqual(store.get(self.data, PRO)["via"], "xcode")

    def test_on_the_phone_saying_no_and_devices_off(self):
        self.pair_result = pair.PairError("not_trusted", "lockdown handshake failed (-17)")
        code, _, err = run(["wifi", "on"])
        self.assertEqual(code, 1)
        self.assertIn("doesn't trust this Mac", err)
        self.pair_result = RuntimeError("Devices are off in this process (MOBSTER_NO_DEVICES=1)")
        code, _, err = run(["wifi", "on"])
        self.assertEqual(code, 3)
        self.assertIn("MOBSTER_NO_DEVICES", err)

    def test_off_clears_what_mobster_set(self):
        run(["wifi", "on"])
        code, out, _ = run(["wifi", "off"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "Wi-Fi is off for Sam's iPhone. Mobster uses it only with the cable.")
        self.assertEqual(self.paired, [(PRO, "enable"), (PRO, "disable")])
        self.assertFalse(store.enabled(self.data, PRO))

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


class StatusTests(CliHarness):
    def test_each_phone_and_how_it_is_connected(self):
        self.records.append(record(XR, "Work iPhone", primary=False, wda_port=8101))
        devices = {"version": 1, "phones": [{"udid": XR, "slot": 1, "wdaPort": 8101, "mjpegPort": 9101}],
                   "lastUsed": None}
        (self.data / "devices.json").write_text(json.dumps(devices))
        store.put(self.data, XR, True, "mobster")
        self.attached = {PRO: True, XR: False}
        self.answers = {"http://127.0.0.1:8101"}                # the Mac app's relay answers on its port
        code, out, _ = run(["wifi", "status"])
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertIn("off in this Mobster", lines[0])
        self.assertRegex(lines[1], r"Sam's iPhone +On cable +Wi-Fi off")
        self.assertRegex(lines[2], r"Work iPhone +On Wi-Fi · encrypted +Wi-Fi on")
        self.answers = set()
        self.peers = {XR: Peer(udid=XR, transport="localNetwork", tunnel="down")}
        code, out, _ = run(["wifi", "status", "--json"])
        body = json.loads(out)
        self.assertEqual(body["wifiTransport"], False)
        work = body["devices"][1]
        self.assertEqual((work["enabled"], work["transport"], work["tunnel"]), (True, None, "down"))
        self.assertIn("Not reachable over Wi-Fi", work["connection"])

    def test_devicectl_failing_doesnt_stop_status(self):
        store.put(self.data, PRO, True, "mobster")
        self.attached = {PRO: False}
        self.peers = DevicectlError("hung", "hang")
        code, out, _ = run(["wifi", "status", "--json"])
        self.assertEqual((code, json.loads(out)["devices"][0]["connection"]), (0, "Not reachable over Wi-Fi"))

    def test_no_phone_yet(self):
        self.records = []
        code, out, _ = run(["wifi", "status"])
        self.assertEqual(code, 0)
        self.assertIn("No iPhone is set up here yet.", out)

    def test_help_lists_the_subcommands(self):
        code, out, _ = run(["wifi", "--help"])
        self.assertEqual(code, 0)
        for name in ("status", "on", "off", "probe"):
            self.assertRegex(out, rf"\n    {name} +")
        code, out, _ = run(["wifi", "probe", "--help"])
        self.assertIn("--start", out)
        self.assertIn("binds only 127.0.0.1", " ".join(out.split()))


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
        self.peers = {PRO: Peer(udid=PRO, transport="localNetwork", tunnel="up", tunnel_state="connected",
                                address=TUNNEL, hostnames=("Sams-iPhone.local",),
                                fields={"transportType": "localNetwork", "tunnelState": "connected",
                                        "tunnelIPAddress": TUNNEL})}
        self.status = {"ready": True}
        self.open_ports = set()
        for item in (mock.patch.object(native_helpers, "devices_allowed", lambda: True),
                     mock.patch.object(probe, "Relay", FakeRelay),
                     mock.patch.object(probe, "http_status", lambda port, timeout=3.0: self.status),
                     mock.patch.object(probe, "stream_answers", lambda port, timeout=3.0: True),
                     mock.patch.object(probe, "usbmux_network", lambda udid: True),
                     mock.patch.object(devicectl, "bring_up", lambda udid: {}),
                     mock.patch("mobile_agent.wireless.network.resolve",
                                lambda names, **k: {"ipv4": ["192.168.1.20"], "ipv6": ["fe80::1c2b%en0"]}),
                     mock.patch("mobile_agent.wireless.network.port_open",
                                lambda host, port, timeout=2.0: (host, port) in self.open_ports),
                     mock.patch("mobile_agent.wireless.network.route_interface", lambda address, reader=None: "utun4"),
                     mock.patch("mobile_agent.wireless.network.peer_check",
                                lambda **k: {"state": "ok", "interface": "en0", "peers": 3})):
            item.start()
            self.addCleanup(item.stop)

    def test_every_check_passes(self):
        code, out, _ = run(["wifi", "probe", "--json"])
        body = json.loads(out)
        self.assertEqual((code, body["ok"], body["tunnelAddress"]), (0, True, TUNNEL))
        states = {step["key"]: step["state"] for step in body["steps"]}
        self.assertEqual(states, {"usb": "info", "network": "ok", "fields": "info", "coredevice": "ok", "tunnel": "ok",
                                  "wda": "ok", "app_relay": "info", "lan": "ok", "usbmux": "info"})
        self.assertEqual(body["fields"]["tunnelState"], "connected")
        self.assertEqual([relay.target for relay in FakeRelay.started], [TUNNEL])   # only the tunnel address
        self.assertEqual(FakeRelay.started[0].pairs, [(0, 8100), (0, 9100)])

    def test_plain_lines(self):
        code, out, _ = run(["wifi", "probe"])
        self.assertEqual(code, 0)
        self.assertIn("✓ Encrypted tunnel", out)
        self.assertIn(f"up, the phone's end is {TUNNEL}", out)
        self.assertTrue(out.rstrip().endswith("Every check passed."))

    def test_wda_answering_on_the_wifi_address_fails_loudly(self):
        self.open_ports = {("192.168.1.20", 8100)}
        code, out, _ = run(["wifi", "probe", "--json"])
        body = json.loads(out)
        lan = next(step for step in body["steps"] if step["key"] == "lan")
        self.assertEqual((code, body["ok"], lan["state"]), (1, False, "fail"))
        self.assertIn("ANSWERS on 192.168.1.20 port 8100", lan["detail"])

    def test_on_the_cable_it_says_to_unplug(self):
        self.peers[PRO] = Peer(udid=PRO, transport="wired", tunnel="up", address=TUNNEL)
        self.attached = {PRO: True}
        code, out, _ = run(["wifi", "probe", "--json"])
        body = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(body["steps"][0]["detail"], "plugged in: unplug it to probe Wi-Fi")
        self.assertIn("Unplug it", body["steps"][-1]["detail"])

    def test_not_seen_tunnel_down_and_a_lan_address(self):
        self.peers = {}
        body = json.loads(run(["wifi", "probe", "--json"])[1])
        self.assertEqual(body["steps"][-1]["key"], "coredevice")
        self.peers = {PRO: Peer(udid=PRO, transport="localNetwork", tunnel="down", tunnel_state="disconnected")}
        body = json.loads(run(["wifi", "probe", "--json"])[1])
        self.assertEqual((body["steps"][-1]["key"], body["steps"][-1]["state"]), ("tunnel", "fail"))
        self.peers = {PRO: Peer(udid=PRO, transport="localNetwork", tunnel="up", address="192.168.1.20")}
        body = json.loads(run(["wifi", "probe", "--json"])[1])
        self.assertIn("isn't an fd00::/8 tunnel address", body["steps"][-1]["detail"])
        self.assertEqual(FakeRelay.started, [])              # nothing ever relayed to a LAN address

    def test_a_tunnel_address_reached_through_wifi_is_refused(self):
        """A home network's own fd00::/8 address: this Mac routes it through Wi-Fi, not the tunnel, so the probe
        stops there and nothing is relayed to it."""
        with mock.patch("mobile_agent.wireless.network.route_interface", lambda address, reader=None: "en0"):
            code, out, _ = run(["wifi", "probe", "--json"])
        body = json.loads(out)
        self.assertEqual((code, body["steps"][-1]["key"], body["steps"][-1]["state"]), (1, "tunnel", "fail"))
        self.assertIn("through en0, not a tunnel (utun)", body["steps"][-1]["detail"])
        self.assertIsNone(body["tunnelAddress"])
        self.assertEqual(FakeRelay.started, [])

    def test_a_network_that_keeps_devices_apart_is_said_first(self):
        self.peers = {}
        with mock.patch("mobile_agent.wireless.network.peer_check",
                        lambda **k: {"state": "blocked", "interface": "en0", "peers": 0}):
            code, out, _ = run(["wifi", "probe"])
        self.assertEqual(code, 1)
        self.assertIn("! This Mac's network", out)
        self.assertIn("This network stops devices from seeing each other. Use the cable or your phone's hotspot.", out)

    def test_wda_not_running_yet(self):
        self.status = None
        code, out, _ = run(["wifi", "probe", "--json"])
        wda = next(step for step in json.loads(out)["steps"] if step["key"] == "wda")
        self.assertEqual((code, wda["state"]), (1, "warn"))
        self.assertIn("--start", wda["detail"])

    def test_devicectl_hanging(self):
        self.peers = DevicectlError("devicectl didn't answer in 8 s and was stopped", "hang")
        code, out, _ = run(["wifi", "probe", "--json"])
        self.assertEqual((code, json.loads(out)["steps"][-1]["state"]), (1, "fail"))

    def test_devices_off(self):
        with mock.patch.object(native_helpers, "devices_allowed", lambda: False):
            code, _, err = run(["wifi", "probe"])
        self.assertEqual(code, 3)
        self.assertIn("MOBSTER_NO_DEVICES=1", err)


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.data = Path(tempfile.mkdtemp(prefix="mobster-wifi-doctor-"))
        (self.data / "device.json").write_text(json.dumps({"udid": PRO, "device_name": "Sam's iPhone"}))
        env = mock.patch.dict(os.environ, {"MOBSTER_WIFI_TRANSPORT": "1"})
        env.start()
        self.addCleanup(env.stop)

    def checks(self, peers, **kwargs):
        kwargs = {"nwi": "IPv4 network interface information\n     en0 : flags : 0x5 (IPv4,DNS)\n",
                  "gateway": "192.168.1.1", "local_network": True,
                  "network_result": {"state": "ok", "interface": "en0", "peers": 4}, **kwargs}
        return {check.key: check for check in doctor.check_wifi(PRO, self.data, "Sam's iPhone", peers=peers, **kwargs)}

    def test_nothing_for_a_phone_without_wifi(self):
        self.assertEqual(doctor.check_wifi(PRO, self.data, "Sam's iPhone", peers={}), [])
        self.assertEqual(doctor.check_wifi(None, self.data), [])

    def test_a_phone_reachable_over_wifi(self):
        store.put(self.data, PRO, True, "mobster")
        found = self.checks({PRO: Peer(udid=PRO, transport="localNetwork", tunnel="up", developer_mode="enabled")})
        self.assertEqual({key: check.state for key, check in found.items()},
                         {"wifi": "ok", "devmode": "ok", "localnet": "ok", "peers": "ok", "awake": "skip"})
        self.assertEqual(found["wifi"].detail, "Sam's iPhone is reachable over the encrypted Wi-Fi link")
        self.assertIn("on the charger", found["awake"].detail)

    def test_problems_each_with_one_fix(self):
        store.put(self.data, PRO, True, "mobster")
        vpn = "IPv4 network interface information\n     utun4 : flags : 0x5 (IPv4,DNS)\n     en0 : flags : 0x5\n"
        found = self.checks({PRO: Peer(udid=PRO, transport=None, developer_mode="disabled")}, nwi=vpn,
                            local_network=False)
        self.assertEqual((found["wifi"].state, found["devmode"].state, found["localnet"].state, found["vpn"].state),
                         ("warn", "fail", "fail", "warn"))
        for check in found.values():
            if check.state in ("warn", "fail"):
                with self.subTest(check=check.key):
                    self.assertTrue(check.fix)
        self.assertIn("utun4", found["vpn"].detail)
        found = self.checks({PRO: Peer(udid=PRO, transport="localNetwork", tunnel="down")})
        self.assertIn("encrypted link isn't up yet", found["wifi"].detail)
        found = self.checks({})
        self.assertIn("doesn't list Sam's iPhone", found["wifi"].detail)

    def test_a_network_that_keeps_devices_apart_is_named_with_its_fix(self):
        """Guest, hotel and campus Wi-Fi with client isolation, or Bonjour blocked: one plain sentence, one fix."""
        store.put(self.data, PRO, True, "mobster")
        blocked = {"state": "blocked", "interface": "en0", "peers": 0}
        found = self.checks({PRO: Peer(udid=PRO, transport=None)}, network_result=blocked)
        self.assertEqual(found["peers"].state, "fail")
        self.assertEqual(found["peers"].detail, "This network stops devices from seeing each other.")
        self.assertEqual(found["peers"].fix, "Use the cable or your phone's hotspot.")
        report = doctor.plain_report(list(found.values()))
        self.assertIn("This network stops devices from seeing each other.", report)
        self.assertIn("→ Use the cable or your phone's hotspot.", report)
        # On the cable it's only a warning: the cable works whatever the network.
        found = self.checks({PRO: Peer(udid=PRO, transport="wired")}, network_result=blocked)
        self.assertEqual(found["peers"].state, "warn")
        found = self.checks({PRO: Peer(udid=PRO, transport=None)}, network_result={"state": "offline"})
        self.assertEqual((found["peers"].state, bool(found["peers"].fix)), ("warn", True))
        found = self.checks({PRO: Peer(udid=PRO, transport=None)}, network_result={"state": "unknown"})
        self.assertEqual(found["peers"].state, "skip")
        # Local Network permission and a VPN have their own lines, so the network line doesn't repeat them.
        for state in ("no_permission", "vpn"):
            with self.subTest(state=state):
                self.assertNotIn("peers", self.checks({}, network_result={"state": state}))

    def test_the_network_check_is_asked_with_the_gateway_and_permission_already_read(self):
        store.put(self.data, PRO, True, "mobster")
        seen = []
        with mock.patch("mobile_agent.wireless.network.peer_check",
                        lambda **k: seen.append(k) or {"state": "blocked"}):
            found = self.checks({}, network_result=None)
        self.assertEqual(seen, [{"gateway": "192.168.1.1", "allowed": True}])
        self.assertEqual(found["peers"].state, "fail")

    def test_on_the_cable_local_network_is_only_a_warning(self):
        store.put(self.data, PRO, True, "mobster")
        found = self.checks({PRO: Peer(udid=PRO, transport="wired")}, local_network=False)
        self.assertEqual((found["wifi"].state, found["localnet"].state), ("ok", "warn"))

    def test_devicectl_hanging_or_off(self):
        store.put(self.data, PRO, True, "mobster")
        with mock.patch.object(devicectl, "list_devices", side_effect=DevicectlError("hung", "hang")):
            self.assertEqual(self.checks(None)["wifi"].state, "warn")
        with mock.patch.object(devicectl, "list_devices", side_effect=RuntimeError("off")):
            self.assertEqual(self.checks(None)["wifi"].state, "skip")

    def test_the_hidden_switch_off_is_said(self):
        store.put(self.data, PRO, True, "mobster")
        with mock.patch.dict(os.environ, {"MOBSTER_WIFI_TRANSPORT": ""}):
            found = self.checks({PRO: Peer(udid=PRO, transport="localNetwork", tunnel="up")})
        self.assertEqual(found["wifi_gate"].state, "skip")

    def test_run_checks_adds_them_only_for_a_phone_with_wifi_on(self):
        with mock.patch.object(doctor, "check_wda", lambda url: doctor.Check("wda", "WebDriverAgent", doctor.OK, "")), \
                mock.patch.object(doctor, "check_locked", lambda url: doctor.Check("locked", "Screen", doctor.OK, "")), \
                mock.patch.object(doctor, "check_lease", lambda url: doctor.Check("lease", "Lock", doctor.OK, "")), \
                mock.patch.object(doctor, "check_xcode", lambda: doctor.Check("xcode", "Xcode", doctor.OK, "")), \
                mock.patch.object(doctor, "check_tools", lambda: doctor.Check("tools", "t", doctor.OK, "")), \
                mock.patch.object(doctor, "check_usb", lambda m: doctor.Check("usb", "u", doctor.OK, "")), \
                mock.patch.object(doctor, "check_runner", lambda m: doctor.Check("runner", "r", doctor.OK, "")), \
                mock.patch.object(devicectl, "list_devices", lambda **k: {}), \
                mock.patch("mobile_agent.wireless.network.nwi", lambda: ""), \
                mock.patch("mobile_agent.wireless.network.default_gateway", lambda: None), \
                mock.patch("mobile_agent.wireless.network.peer_check", lambda **k: {"state": "unknown"}):
            keys = [check.key for check in doctor.run_checks(data_dir=self.data)]
            self.assertNotIn("wifi", keys)
            store.put(self.data, PRO, True, "mobster")
            keys = [check.key for check in doctor.run_checks(data_dir=self.data)]
            self.assertEqual(keys[keys.index("runner") + 1:keys.index("wda")], ["wifi", "peers", "awake"])


if __name__ == "__main__":
    unittest.main()
