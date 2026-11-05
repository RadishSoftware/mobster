import copy
import ipaddress
import json
import os
import plistlib
import random
import socket
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mobile_agent import device_manager, mac_tools, runner_hints, supervised
from mobile_agent.device_manager import DeviceManager
from mobile_agent.tests.test_fleet import PRO, XR
from mobile_agent.tests.wireless_fixtures import PLAN_V1, PLAN_V2, TUNNEL
from mobile_agent.wda import patches as wda_patches
from mobile_agent.wireless import relay as wifi_relay
from mobile_agent.wireless import (
    xctestrun,
)


def products(
    root, plan=PLAN_V1, name="WebDriverAgentRunner_iphoneos26.0-arm64.xctestrun"
):
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
    item = patch(
        "mobile_agent.wireless.network.through_tunnel",
        lambda address, reader=None: answer,
    )
    item.start()
    test.addCleanup(item.stop)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mobster-xctestrun-"))
        through_a_tunnel(self)

    def test_the_copy_listens_on_the_tunnel_address_and_spells_out_its_folder(self):
        folder = products(self.root)
        path = xctestrun.write(
            folder, self.root / "wda-build" / xctestrun.FOLDER, TUNNEL
        )
        self.assertEqual(path.parent, self.root / "wda-build" / "mobster-wifi")
        plan = read(path)
        self.assertEqual(xctestrun.use_ips(plan), [TUNNEL, TUNNEL])
        target = plan["WebDriverAgentRunner"]
        self.assertEqual(
            target["TestHostPath"],
            f"{folder.resolve()}/Debug-iphoneos/WebDriverAgentRunner-Runner.app",
        )
        self.assertNotIn("__TESTROOT__", json.dumps(plan))
        self.assertEqual(
            target["TestBundlePath"], "__TESTHOST__/PlugIns/WebDriverAgentRunner.xctest"
        )
        self.assertEqual(target["EnvironmentVariables"]["USE_PORT"], "")
        self.assertEqual(
            xctestrun.use_ips(read(xctestrun.source_plan(folder))),
            ["127.0.0.1", "127.0.0.1"],
        )
        self.assertEqual(len(list(folder.glob("*.xctestrun"))), 1)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(path.parent).st_mode), 0o700)

    def test_use_ip_is_never_anything_but_a_tunnel_address(self):
        folder = products(self.root)
        out = self.root / "wifi"
        rng = random.Random(11)
        fixed = [
            "",
            " ",
            "0.0.0.0",
            "::",
            "::0",
            "127.0.0.1",
            "::1",
            "192.168.1.20",
            "10.0.0.7",
            "172.20.1.4",
            "169.254.1.1",
            "fe80::1",
            "fe80::1%en0",
            "2001:db8::5",
            "fc00::1",
            "::ffff:192.168.1.20",
            None,
        ]
        generated = (
            [str(ipaddress.IPv4Address(rng.getrandbits(32))) for _ in range(150)]
            + [str(ipaddress.IPv6Address(rng.getrandbits(128))) for _ in range(150)]
            + [
                str(ipaddress.IPv6Address((0xFD << 120) | rng.getrandbits(120)))
                for _ in range(50)
            ]
        )
        for address in fixed + generated:
            with self.subTest(address=address):
                try:
                    path = xctestrun.write(folder, out, address)
                except ValueError:
                    self.assertFalse(
                        isinstance(address, str)
                        and address.strip()
                        and ipaddress.ip_address(address.split("%")[0].strip())
                        in ipaddress.IPv6Network("fd00::/8")
                        and "%" not in address
                    )
                    continue
                values = set(xctestrun.use_ips(read(path)))
                self.assertEqual(len(values), 1)
                value = values.pop()
                parsed = ipaddress.ip_address(value)
                self.assertIn(parsed, ipaddress.IPv6Network("fd00::/8"))
                self.assertNotIn(value, ("", "0.0.0.0", "::"))
                path.unlink()
        self.assertEqual(list(out.glob("*.xctestrun")), [])

    def test_usb_pins_both_environment_sections_in_both_plan_formats(self):
        for version, template in enumerate((PLAN_V1, PLAN_V2), 1):
            with self.subTest(version=version):
                drifted = copy.deepcopy(template)
                target = xctestrun.targets(drifted)[0]
                target["EnvironmentVariables"] = "invalid"
                target["TestingEnvironmentVariables"] = {
                    "USE_IP": "0.0.0.0",
                    "OTHER": "kept",
                }
                folder = products(self.root / str(version), drifted)
                wda_patches.pin_loopback(folder)
                saved = read(xctestrun.source_plan(folder))
                self.assertEqual(set(xctestrun.use_ips(saved)), {"127.0.0.1"})
                self.assertEqual(
                    xctestrun.targets(saved)[0]["TestingEnvironmentVariables"]["OTHER"],
                    "kept",
                )
                if version == 2:
                    self.assertEqual(saved["TestPlan"], PLAN_V2["TestPlan"])


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mobster-xctestrun-route-"))

    def test_the_writer_asks_the_route_and_refuses_one_through_wifi(self):
        folder = products(self.root)
        asked = []
        with self.assertRaises(ValueError):
            xctestrun.write(
                folder,
                self.root / "wifi",
                TUNNEL,
                through=lambda address: asked.append(address),
            )
        self.assertEqual(asked, [TUNNEL])
        self.assertFalse((self.root / "wifi").exists())
        path = xctestrun.write(
            folder, self.root / "wifi", TUNNEL, through=lambda address: True
        )
        self.assertEqual(set(xctestrun.use_ips(read(path))), {TUNNEL})


class ManagerWifiTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mobster-wifi-manager-"))
        through_a_tunnel(self)
        self.wda, self.video = free_port(), free_port()
        self.manager = DeviceManager(
            self.root, f"http://127.0.0.1:{self.wda}", video_port=self.video
        )
        products(self.root)
        self.manager.save_settings(
            udid=PRO,
            built_for=PRO,
            built_patches=wda_patches.patch_digest(),
            team="ABCDE12345",
            autostart=True,
            expires_at=4102444800,
        )
        self.started = []
        test = self

        class FakeSupervised:
            def __init__(self, name, command, log_path, env=None, gate=None, hold=None):
                self.name, self.command, self.running, self.gate = (
                    name,
                    command,
                    False,
                    gate,
                )
                test.started.append(self)

            def start(self):
                self.running = True

            def stop(self):
                self.running = False

        for item in (
            patch.object(supervised, "Supervised", FakeSupervised),
            patch.object(mac_tools, "tool", lambda name: f"/fake/{name}"),
            patch.object(mac_tools, "usb_attached", lambda udid, **_: False),
            patch.object(DeviceManager, "xcode_env", lambda manager: None),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(self.manager.stop_runner)

    def test_the_runner_starts_over_the_tunnel_behind_a_loopback_relay(self):
        self.manager.start_wifi_runner(TUNNEL, PRO)
        runner = self.started[-1]
        plan = (
            self.root
            / "wda-build"
            / "mobster-wifi"
            / "WebDriverAgentRunner_iphoneos26.0-arm64.xctestrun"
        )
        self.assertEqual(
            runner.command,
            [
                "/fake/xcodebuild",
                "test-without-building",
                "-xctestrun",
                str(plan),
                "-destination",
                f"id={PRO}",
            ],
        )
        self.assertTrue(runner.running)
        self.assertEqual(runner.gate, self.manager._relaunch_gate)
        self.assertIsInstance(self.manager.relay, wifi_relay.Relay)
        self.assertEqual(self.manager.relay.target, TUNNEL)
        self.assertEqual(
            self.manager.relay.pairs, [(self.wda, 8100), (self.video, 9100)]
        )
        self.assertEqual(
            [listener.getsockname() for listener, _ in self.manager.relay.listeners],
            [("127.0.0.1", self.wda), ("127.0.0.1", self.video)],
        )
        self.assertEqual(
            (self.manager.transport, self.manager.wifi_address), ("wifi", TUNNEL)
        )
        self.assertEqual(set(xctestrun.use_ips(read(plan))), {TUNNEL})
        self.assertNotIn("-n", runner.command)

    def test_it_refuses_what_it_cannot_do(self):
        for address in ("192.168.1.20", "0.0.0.0", "::", "", "fe80::1"):
            with (
                self.subTest(address=address),
                self.assertRaises(LookupError) as refused,
            ):
                self.manager.start_wifi_runner(address, PRO)
            self.assertEqual(
                str(refused.exception), "Wi-Fi isn't available for this iPhone yet."
            )
        self.assertEqual(self.started, [])
        self.manager.save_settings(team=None)
        with self.assertRaises(LookupError):
            self.manager.start_wifi_runner(TUNNEL, PRO)
        self.manager.save_settings(team="ABCDE12345")
        self.manager.watching.set()
        with self.assertRaisesRegex(LookupError, "quitting"):
            self.manager.start_wifi_runner(TUNNEL, PRO)
        self.assertIsNone(self.manager.transport)

    def test_another_phones_address_is_refused_and_nothing_starts(self):
        with self.assertRaisesRegex(LookupError, "different iPhone"):
            self.manager.start_wifi_runner(TUNNEL, XR)
        self.assertEqual(
            (self.started, self.manager.transport, self.manager.relay), ([], None, None)
        )
        self.assertFalse((self.root / "wda-build" / "mobster-wifi").exists())
        self.manager.start_wifi_runner(TUNNEL, PRO.lower())
        self.assertEqual(self.manager.transport, "wifi")
        self.assertEqual(self.manager.wifi_udid(), PRO)

    def test_a_runner_built_without_this_mobsters_patches_is_not_started(self):
        self.manager.pinned = None
        for recorded in (None, "0123456789abcdef"):
            self.manager.save_settings(built_patches=recorded)
            self.assertFalse(self.manager.built())
            self.assertTrue(self.manager.out_of_date())
            with self.assertRaisesRegex(LookupError, "Build the runner first"):
                self.manager.start_wifi_runner(TUNNEL, PRO)
            self.assertEqual((self.started, self.manager.transport), ([], None))
        self.manager.save_settings(built_patches=wda_patches.patch_digest())
        self.assertTrue(self.manager.built())
        self.assertFalse(self.manager.out_of_date())
        self.manager.start_wifi_runner(TUNNEL, PRO)
        self.assertEqual(self.manager.transport, "wifi")

    def test_the_patch_fingerprint_follows_the_patches(self):
        before = wda_patches.patch_digest()
        patches = wda_patches.WDA_PATCHES
        with patch.object(wda_patches, "WDA_PATCHES", patches[:-1]):
            self.assertNotEqual(wda_patches.patch_digest(), before)
        self.assertEqual(wda_patches.patch_digest(), before)

    def test_a_helper_found_answering_on_another_address_is_stopped_and_stays_stopped(
        self,
    ):
        self.manager.start_wifi_runner(TUNNEL, PRO)
        self.manager.wda_ready = lambda timeout=1.5: True
        with patch(
            "mobile_agent.wireless.exposure.exposed",
            lambda names: (["172.20.10.1"], ["172.20.10.1:9100"]),
        ):
            self.manager.check_exposure(hostnames=())
        self.assertEqual(
            (self.manager.runner, self.manager.transport, self.manager.exposure),
            (None, None, ["172.20.10.1:9100"]),
        )
        self.assertFalse(self.manager.settings()["autostart"])
        self.manager.wda_ready = lambda timeout=1.5: False
        self.assertEqual(
            self.manager.runner_state()["error"], runner_hints.EXPOSED_HINT
        )

    def test_a_helper_that_answers_only_where_it_should_keeps_running_and_is_checked_once(
        self,
    ):
        self.manager.start_wifi_runner(TUNNEL, PRO)
        self.manager.wda_ready = lambda timeout=1.5: True
        asked = []

        def clean(names):
            asked.append(names)
            return ["192.168.1.20"], []

        with patch("mobile_agent.wireless.exposure.exposed", clean):
            self.manager.check_exposure(hostnames=("Sams-iPhone.local",))
            self.manager.check_exposure(hostnames=("Sams-iPhone.local",))
        self.assertEqual(
            (self.manager.transport, self.manager.exposure, len(asked)),
            ("wifi", None, 1),
        )
        self.assertTrue(self.manager.settings()["autostart"])

    def test_a_lan_ula_this_mac_reaches_through_wifi_is_refused(self):
        through_a_tunnel(self, answer=False)
        with self.assertRaises(LookupError) as refused:
            self.manager.start_wifi_runner(TUNNEL, PRO)
        self.assertEqual(
            str(refused.exception), "Wi-Fi isn't available for this iPhone yet."
        )
        self.assertEqual(
            (self.started, self.manager.transport, self.manager.relay), ([], None, None)
        )
        self.assertFalse((self.root / "wda-build" / "mobster-wifi").exists())

    def test_the_command_takes_only_a_udid(self):
        with self.assertRaises(ValueError):
            device_manager.wifi_runner_command("/x", "/plan.xctestrun", "-n; rm -rf /")


class IproxyTests(unittest.TestCase):
    def test_no_iproxy_argument_list_has_dash_n(self):
        with (
            patch.object(mac_tools, "tool", lambda name: "/opt/homebrew/bin/iproxy"),
            patch.object(mac_tools, "iproxy_binds_loopback", lambda path: True),
        ):
            rng = random.Random(3)
            for _ in range(50):
                udid = "".join(rng.choice("0123456789ABCDEF") for _ in range(24))
                command = mac_tools.relay_command(
                    udid, 8100 + rng.randrange(100), 9100 + rng.randrange(100)
                )
                self.assertNotIn("-n", command)
                self.assertNotIn("--network", command)
                self.assertEqual(command[1:3], ["-s", "127.0.0.1"])
        root = Path(device_manager.__file__).resolve().parent
        for path in root.rglob("*.py"):
            if any(
                part == "tests" or part.startswith(".")
                for part in path.relative_to(root).parts
            ):
                continue
            text = path.read_text()
            if "iproxy" not in text:
                continue
            with self.subTest(path=path.name):
                self.assertNotRegex(text, r"""iproxy["'][^\n]*["']-n["']""")
                self.assertNotRegex(text, r"""["']-n["'][^\n]*iproxy""")
