"""Track wireless: the runner's test plan over Wi-Fi (SPEC §3.7 item 3) and DeviceManager's start over Wi-Fi.
USE_IP is only ever the tunnel address (never 0.0.0.0, ::, empty or a LAN address), the built plan stays pinned to
loopback for the cable, and a runner on Wi-Fi sits behind a loopback relay on the phone's own ports. Fakes only: no
xcodebuild, no phone."""

import copy
import ipaddress
import json
import os
from pathlib import Path
import plistlib
import random
import socket
import stat
import tempfile
import unittest
from unittest.mock import patch

from mobile_agent import device_manager
from mobile_agent.device_manager import DeviceManager, pin_loopback, wifi_runner_command
from mobile_agent.wireless import relay as wifi_relay
from mobile_agent.wireless import xctestrun

UDID = "00008130-001A2B3C4D5E6F70"
TUNNEL = "fd7a:1c2b:3d4e::1"
RUNNER_APP = "__TESTROOT__/Debug-iphoneos/WebDriverAgentRunner-Runner.app"
PLAN_V1 = {
    "WebDriverAgentRunner": {
        "BlueprintName": "WebDriverAgentRunner",
        "DependentProductPaths": [RUNNER_APP, RUNNER_APP + "/PlugIns/WebDriverAgentRunner.xctest"],
        "EnvironmentVariables": {"USE_IP": "127.0.0.1", "USE_PORT": "", "MJPEG_SERVER_PORT": ""},
        "IsUITestBundle": True,
        "TestBundlePath": "__TESTHOST__/PlugIns/WebDriverAgentRunner.xctest",
        "TestHostPath": RUNNER_APP,
        "TestingEnvironmentVariables": {"DYLD_FRAMEWORK_PATH": "__TESTROOT__/Debug-iphoneos", "USE_IP": "127.0.0.1"},
        "UITargetAppPath": RUNNER_APP,
    },
    "__xctestrun_metadata__": {"FormatVersion": 1},
}
PLAN_V2 = {
    "TestConfigurations": [{"Name": "Test Scheme Action", "TestTargets": [{
        "BlueprintName": "WebDriverAgentRunner", "EnvironmentVariables": {"USE_IP": "127.0.0.1"},
        "TestHostPath": RUNNER_APP, "DependentProductPaths": [RUNNER_APP]}]}],
    "TestPlan": {"IsDefault": True, "Name": "WebDriverAgentRunner"},
    "__xctestrun_metadata__": {"FormatVersion": 2},
}


def products(root, plan=PLAN_V1, name="WebDriverAgentRunner_iphoneos26.0-arm64.xctestrun"):
    folder = Path(root) / "wda-build" / "Build" / "Products"
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / name, "wb") as stream:
        plistlib.dump(copy.deepcopy(plan), stream)
    return folder


def read(path):
    with open(path, "rb") as stream:
        return plistlib.load(stream)


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def through_a_tunnel(test, answer=True):
    """This Mac's route to the tunnel address goes through utun (``answer``), without asking `route`."""
    item = patch("mobile_agent.wireless.network.through_tunnel", lambda address, reader=None: answer)
    item.start()
    test.addCleanup(item.stop)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mobster-xctestrun-"))
        through_a_tunnel(self)

    def test_the_copy_listens_on_the_tunnel_address_and_spells_out_its_folder(self):
        folder = products(self.root)
        path = xctestrun.write(folder, self.root / "wda-build" / xctestrun.FOLDER, TUNNEL)
        self.assertEqual(path.parent, self.root / "wda-build" / "mobster-wifi")
        plan = read(path)
        self.assertEqual(xctestrun.use_ips(plan), [TUNNEL, TUNNEL])   # EnvironmentVariables and the testing ones
        target = plan["WebDriverAgentRunner"]
        self.assertEqual(target["TestHostPath"], f"{folder.resolve()}/Debug-iphoneos/WebDriverAgentRunner-Runner.app")
        self.assertNotIn("__TESTROOT__", json.dumps(plan))
        self.assertEqual(target["TestBundlePath"], "__TESTHOST__/PlugIns/WebDriverAgentRunner.xctest")
        self.assertEqual(target["EnvironmentVariables"]["USE_PORT"], "")   # everything else as built
        # The built plan, which the cable's launch finds by itself, stays on loopback, and is the only one there.
        self.assertEqual(xctestrun.use_ips(read(xctestrun.source_plan(folder))), ["127.0.0.1", "127.0.0.1"])
        self.assertEqual(len(list(folder.glob("*.xctestrun"))), 1)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(path.parent).st_mode), 0o700)

    def test_format_2_plans_set_every_test_target_and_nothing_else(self):
        folder = products(self.root, PLAN_V2)
        plan = read(xctestrun.write(folder, self.root / "wifi", TUNNEL))
        self.assertEqual(xctestrun.use_ips(plan), [TUNNEL])
        self.assertNotIn("EnvironmentVariables", plan["TestPlan"])

    def test_the_address_is_canonical(self):
        folder = products(self.root)
        plan = read(xctestrun.write(folder, self.root / "wifi", "FD7A:1C2B:3D4E:0:0:0:0:1"))
        self.assertEqual(set(xctestrun.use_ips(plan)), {TUNNEL})   # as WDA's interface lookup prints it

    def test_use_ip_is_never_anything_but_a_tunnel_address(self):
        """Property: over many addresses, the writer either writes the tunnel address or refuses and writes nothing."""
        folder = products(self.root)
        out = self.root / "wifi"
        rng = random.Random(11)
        fixed = ["", " ", "0.0.0.0", "::", "::0", "127.0.0.1", "::1", "192.168.1.20", "10.0.0.7", "172.20.1.4",
                 "169.254.1.1", "fe80::1", "fe80::1%en0", "2001:db8::5", "fc00::1", "::ffff:192.168.1.20", None]
        generated = [str(ipaddress.IPv4Address(rng.getrandbits(32))) for _ in range(150)] + \
            [str(ipaddress.IPv6Address(rng.getrandbits(128))) for _ in range(150)] + \
            [str(ipaddress.IPv6Address((0xfd << 120) | rng.getrandbits(120))) for _ in range(50)]
        for address in fixed + generated:
            with self.subTest(address=address):
                try:
                    path = xctestrun.write(folder, out, address)
                except ValueError:
                    self.assertFalse(isinstance(address, str) and address.strip() and
                                     ipaddress.ip_address(address.split("%")[0].strip()) in
                                     ipaddress.IPv6Network("fd00::/8") and "%" not in address)
                    continue
                values = set(xctestrun.use_ips(read(path)))
                self.assertEqual(len(values), 1)
                value = values.pop()
                parsed = ipaddress.ip_address(value)
                self.assertIn(parsed, ipaddress.IPv6Network("fd00::/8"))
                self.assertNotIn(value, ("", "0.0.0.0", "::"))
                path.unlink()
        self.assertEqual(list(out.glob("*.xctestrun")), [])

    def test_no_built_plan_or_no_target(self):
        with self.assertRaises(LookupError):
            xctestrun.write(self.root / "nothing", self.root / "wifi", TUNNEL)
        folder = products(self.root, {"__xctestrun_metadata__": {"FormatVersion": 1}})
        with self.assertRaises(LookupError):
            xctestrun.write(folder, self.root / "wifi", TUNNEL)

    def test_pin_loopback_leaves_the_wifi_copy_alone_and_still_pins_the_built_plan(self):
        drifted = copy.deepcopy(PLAN_V1)
        drifted["WebDriverAgentRunner"]["EnvironmentVariables"]["USE_IP"] = "0.0.0.0"
        folder = products(self.root, drifted)
        wifi = xctestrun.write(folder, self.root / "wda-build" / xctestrun.FOLDER, TUNNEL)
        pin_loopback(folder)
        self.assertEqual(read(xctestrun.source_plan(folder))["WebDriverAgentRunner"]["EnvironmentVariables"]["USE_IP"],
                         "127.0.0.1")
        self.assertEqual(set(xctestrun.use_ips(read(wifi))), {TUNNEL})


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mobster-xctestrun-route-"))

    def test_the_writer_asks_the_route_and_refuses_one_through_wifi(self):
        folder = products(self.root)
        asked = []
        with self.assertRaises(ValueError):
            xctestrun.write(folder, self.root / "wifi", TUNNEL, through=lambda address: asked.append(address))
        self.assertEqual(asked, [TUNNEL])
        self.assertFalse((self.root / "wifi").exists())
        path = xctestrun.write(folder, self.root / "wifi", TUNNEL, through=lambda address: True)
        self.assertEqual(set(xctestrun.use_ips(read(path))), {TUNNEL})


class ManagerWifiTests(unittest.TestCase):
    """DeviceManager.start_wifi_runner with a fake build: no xcodebuild runs (Supervised is replaced)."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mobster-wifi-manager-"))
        through_a_tunnel(self)
        self.wda, self.video = free_port(), free_port()
        self.manager = DeviceManager(self.root, f"http://127.0.0.1:{self.wda}", video_port=self.video)
        products(self.root)
        self.manager.save_settings(udid=UDID, built_for=UDID, team="ABCDE12345", autostart=True,
                                   expires_at=4102444800)
        self.started = []
        test = self

        class FakeSupervised:
            def __init__(self, name, command, log_path, env=None):
                self.name, self.command, self.running = name, command, False
                test.started.append(self)

            def start(self):
                self.running = True

            def stop(self):
                self.running = False

        for item in (patch.object(device_manager, "Supervised", FakeSupervised),
                     patch.object(device_manager, "tool", lambda name: f"/fake/{name}"),
                     patch.object(device_manager, "usb_attached", lambda udid, **_: False),
                     patch.object(DeviceManager, "xcode_env", lambda manager: None)):
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(self.manager.stop_runner)

    def test_the_runner_starts_over_the_tunnel_behind_a_loopback_relay(self):
        self.manager.start_wifi_runner(TUNNEL)
        runner = self.started[-1]
        plan = self.root / "wda-build" / "mobster-wifi" / "WebDriverAgentRunner_iphoneos26.0-arm64.xctestrun"
        self.assertEqual(runner.command, ["/fake/xcodebuild", "test-without-building", "-xctestrun", str(plan),
                                          "-destination", f"id={UDID}"])
        self.assertTrue(runner.running)
        self.assertIsInstance(self.manager.relay, wifi_relay.Relay)
        self.assertEqual(self.manager.relay.target, TUNNEL)
        self.assertEqual(self.manager.relay.pairs, [(self.wda, 8100), (self.video, 9100)])
        self.assertEqual([listener.getsockname() for listener, _ in self.manager.relay.listeners],
                         [("127.0.0.1", self.wda), ("127.0.0.1", self.video)])
        self.assertEqual((self.manager.transport, self.manager.wifi_address), ("wifi", TUNNEL))
        self.assertEqual(set(xctestrun.use_ips(read(plan))), {TUNNEL})
        self.assertNotIn("-n", runner.command)

    def test_a_restart_while_unplugged_stays_on_wifi_and_a_stop_frees_the_ports(self):
        self.manager.start_wifi_runner(TUNNEL)
        first = self.manager.relay
        self.manager.start_runner()          # Setup's Start, or a wedged runner's restart
        self.assertEqual(self.manager.transport, "wifi")
        self.assertFalse(first.running)
        self.assertIsNot(self.manager.relay, first)
        self.manager.stop_runner()
        self.assertEqual((self.manager.transport, self.manager.runner, self.manager.relay), (None, None, None))
        with socket.socket() as again:       # the phone's ports are free for the cable's iproxy
            again.bind(("127.0.0.1", self.wda))

    def test_it_refuses_what_it_cannot_do(self):
        for address in ("192.168.1.20", "0.0.0.0", "::", "", "fe80::1"):
            with self.subTest(address=address), self.assertRaises(LookupError) as refused:
                self.manager.start_wifi_runner(address)
            self.assertEqual(str(refused.exception), "Wi-Fi isn't available for this iPhone yet.")
        self.assertEqual(self.started, [])
        self.manager.save_settings(team=None)
        with self.assertRaises(LookupError):
            self.manager.start_wifi_runner(TUNNEL)
        self.manager.save_settings(team="ABCDE12345")
        self.manager.watching.set()
        with self.assertRaisesRegex(LookupError, "quitting"):
            self.manager.start_wifi_runner(TUNNEL)
        self.assertIsNone(self.manager.transport)

    def test_a_lan_ula_this_mac_reaches_through_wifi_is_refused(self):
        """A home network may hand out fd00::/8 addresses too: one this Mac routes through Wi-Fi (not the utun of the
        CoreDevice tunnel) is never written as USE_IP, and no runner or relay starts."""
        through_a_tunnel(self, answer=False)
        with self.assertRaises(LookupError) as refused:
            self.manager.start_wifi_runner(TUNNEL)
        self.assertEqual(str(refused.exception), "Wi-Fi isn't available for this iPhone yet.")
        self.assertEqual((self.started, self.manager.transport, self.manager.relay), ([], None, None))
        self.assertFalse((self.root / "wda-build" / "mobster-wifi").exists())

    def test_a_port_in_use_is_said_plainly_and_starts_nothing(self):
        taken = socket.socket()
        taken.bind(("127.0.0.1", self.wda))
        taken.listen(1)
        self.addCleanup(taken.close)
        with self.assertRaisesRegex(LookupError, f"port {self.wda}"):
            self.manager.start_wifi_runner(TUNNEL)
        self.assertEqual((self.started, self.manager.transport), ([], None))

    def test_the_command_takes_only_a_udid(self):
        with self.assertRaises(ValueError):
            wifi_runner_command("/x", "/plan.xctestrun", "-n; rm -rf /")


class IproxyTests(unittest.TestCase):
    def test_no_iproxy_argument_list_has_dash_n(self):
        """usbmux over the network reaches WDA only if it listens on the LAN: Mobster never asks for it."""
        with patch.object(device_manager, "tool", lambda name: "/opt/homebrew/bin/iproxy"), \
                patch.object(device_manager, "iproxy_binds_loopback", lambda path: True):
            rng = random.Random(3)
            for _ in range(50):
                udid = "".join(rng.choice("0123456789ABCDEF") for _ in range(24))
                command = device_manager.relay_command(udid, 8100 + rng.randrange(100), 9100 + rng.randrange(100))
                self.assertNotIn("-n", command)
                self.assertNotIn("--network", command)
                self.assertEqual(command[1:3], ["-s", "127.0.0.1"])
        root = Path(device_manager.__file__).resolve().parent
        for path in root.rglob("*.py"):
            if "tests" in path.parts:
                continue
            text = path.read_text()
            if "iproxy" not in text:
                continue
            with self.subTest(path=path.name):
                self.assertNotRegex(text, r"""iproxy["'][^\n]*["']-n["']""")
                self.assertNotRegex(text, r"""["']-n["'][^\n]*iproxy""")


if __name__ == "__main__":
    unittest.main()
