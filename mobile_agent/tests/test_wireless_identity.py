import json
import os
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mobile_agent import (
    native_helpers,
)
from mobile_agent.tests.test_fleet import PRO, XR
from mobile_agent.tests.wireless_fixtures import device, recorded
from mobile_agent.wireless import (
    devicectl,
    pair,
)
from mobile_agent.wireless.devicectl import DevicectlError


def listing(*devices):
    return {
        "info": {
            "arguments": ["devicectl", "list", "devices"],
            "commandType": "devicectl.list.devices",
            "jsonVersion": 2,
            "outcome": "success",
            "version": "518.27",
        },
        "result": {"devices": list(devices)},
    }


class ParseTests(unittest.TestCase):
    def test_a_phone_over_the_network_with_its_tunnel_up(self):
        peers = devicectl.parse(listing(device(PRO)))
        peer = peers[PRO]
        self.assertEqual(
            (peer.udid, peer.name, peer.transport, peer.tunnel, peer.address),
            (PRO, "Studio iPhone", "localNetwork", "up", "fd7a:1c2b:3d4e::1"),
        )
        self.assertTrue(peer.network)
        self.assertFalse(peer.wired)
        self.assertEqual(peer.developer_mode, "enabled")
        self.assertEqual(
            peer.hostnames, ("Sams-iPhone.local", f"{PRO}.coredevice.local")
        )
        self.assertEqual(peer.fields["tunnelState"], "connected")

    def test_tunnel_states_and_transports(self):
        cases = {
            "connected": "up",
            "connecting": "connecting",
            "disconnected": "down",
            "unavailable": "down",
            "somethingNew": "unknown",
        }
        for raw, state in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(
                    devicectl.parse(listing(device(PRO, tunnel=raw)))[PRO].tunnel, state
                )
        wired = devicectl.parse(listing(device(PRO, transport="wired")))[PRO]
        self.assertTrue(wired.wired)
        self.assertFalse(wired.network)

    def test_malformed_documents_read_as_no_device(self):
        for document in (
            None,
            [],
            {},
            {"result": None},
            {"result": {"devices": "nope"}},
            {"result": []},
        ):
            with self.subTest(document=document):
                self.assertEqual(devicectl.parse(document), {})


class RunTests(unittest.TestCase):
    def setUp(self):
        allow = patch.object(native_helpers, "devices_allowed", lambda: True)
        allow.start()
        self.addCleanup(allow.stop)

    def fake(self, script):
        folder = Path(tempfile.mkdtemp(prefix="mobster-devicectl-test-"))
        path = folder / "devicectl"
        path.write_text("#!/bin/sh\n" + script)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return path

    def runner_for(self, script):
        fake = self.fake(script)

        def run(argv, env, timeout):
            return devicectl._run([str(fake), *argv[2:]], env, timeout)

        return run

    def test_a_hang_is_killed_at_its_timeout(self):
        marker = Path(tempfile.mkdtemp(prefix="mobster-hang-")) / "child.pid"
        script = f"""sleep 30 &
echo $! > {marker}
wait
"""
        started = time.monotonic()
        with self.assertRaises(DevicectlError) as hung:
            devicectl.list_devices(
                timeout=0.5, developer="/X", runner=self.runner_for(script)
            )
        self.assertEqual(hung.exception.code, "hang")
        self.assertLess(time.monotonic() - started, 5)
        child = int(marker.read_text())
        for _ in range(50):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("the hung devicectl's child is still running")

    def test_a_failure_without_an_answer(self):
        with self.assertRaises(DevicectlError) as failed:
            devicectl.list_devices(
                developer="/X",
                runner=self.runner_for("echo 'No provider' >&2\nexit 1\n"),
            )
        self.assertEqual(failed.exception.code, "failed")
        self.assertIn("No provider", str(failed.exception))


def answering(document, seen=None):
    def run(argv, env, timeout):
        if seen is not None:
            seen.append(argv)
        Path(argv[argv.index("--json-output") + 1]).write_text(json.dumps(document))
        return 0, ""

    return run


class ReaderTests(unittest.TestCase):
    def setUp(self):
        allow = patch.object(native_helpers, "devices_allowed", lambda: True)
        allow.start()
        self.addCleanup(allow.stop)

    def test_lock_state_is_a_clear_yes_or_no_or_nothing(self):
        self.assertIs(devicectl.lock_state(recorded("lock_state_locked.json")), True)
        self.assertIs(devicectl.lock_state(recorded("lock_state_unlocked.json")), False)
        for garbage in (
            None,
            [],
            {},
            {"result": None},
            {"result": {}},
            {"result": {"passcodeRequired": "no"}},
            {"result": {"passcodeRequired": 0}},
            {"result": {"unlockedSinceBoot": True}},
        ):
            with self.subTest(garbage=garbage):
                self.assertIsNone(devicectl.lock_state(garbage))

    def test_the_lock_state_is_read_for_one_phone(self):
        seen = []
        locked = devicectl.read_lock_state(
            PRO,
            developer="/X",
            runner=answering(recorded("lock_state_locked.json"), seen),
        )
        self.assertIs(locked, True)
        self.assertEqual(
            seen[0][1:7], ["devicectl", "device", "info", "lockState", "--device", PRO]
        )
        self.assertIsNone(
            devicectl.read_lock_state(
                PRO, developer="/X", runner=lambda *a: (1, "No provider")
            )
        )
        with patch.object(native_helpers, "devices_allowed", lambda: False):
            self.assertIsNone(
                devicectl.read_lock_state(
                    PRO, runner=lambda *a: self.fail("devicectl ran")
                )
            )
        with self.assertRaises(ValueError):
            devicectl.read_lock_state(
                "; rm -rf /",
                developer="/X",
                runner=lambda *a: self.fail("devicectl ran"),
            )


class IdentityTests(unittest.TestCase):
    def setUp(self):
        allow = patch.object(native_helpers, "devices_allowed", lambda: True)
        allow.start()
        self.addCleanup(allow.stop)

    def test_details_for_another_phone_are_refused(self):
        with self.assertRaises(DevicectlError) as refused:
            devicectl.bring_up(
                PRO, developer="/X", runner=answering(listing(device(XR)))
            )
        self.assertEqual(refused.exception.code, "identity")

    def test_details_for_the_phone_asked_about_are_taken_in_any_case(self):
        peers = devicectl.bring_up(
            PRO.lower(), developer="/X", runner=answering(listing(device(PRO)))
        )
        self.assertEqual(list(peers), [PRO])


class NoDevicesTests(unittest.TestCase):
    def test_devicectl_and_the_pair_tool_raise_instead_of_running(self):
        with patch.dict(os.environ, {"MOBSTER_NO_DEVICES": "1"}):
            with self.assertRaises(RuntimeError):
                devicectl.list_devices(runner=lambda *a: self.fail("devicectl ran"))
            with self.assertRaises(RuntimeError):
                devicectl.bring_up(PRO, runner=lambda *a: self.fail("devicectl ran"))
            with (
                patch.object(
                    subprocess, "run", side_effect=AssertionError("the pair tool ran")
                ),
                self.assertRaises(RuntimeError),
            ):
                pair.run(PRO, "enable", tool="/fake/mobster-wifi-pair")


class PairTests(unittest.TestCase):
    def setUp(self):
        allow = patch.object(native_helpers, "devices_allowed", lambda: True)
        allow.start()
        self.addCleanup(allow.stop)

    def tool(self, exit_code, stdout="", stderr=""):
        folder = Path(tempfile.mkdtemp(prefix="mobster-pair-test-"))
        log = folder / "argv"
        path = folder / "mobster-wifi-pair"
        path.write_text(
            f"#!/bin/sh\necho \"$@\" > {log}\nprintf '%s' '{stdout}'\nprintf '%s' '{stderr}' >&2\n"
            f"exit {exit_code}\n"
        )
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return str(path), log

    def test_the_arguments(self):
        self.assertEqual(
            pair.command("/t/mobster-wifi-pair", PRO, "enable"),
            ["/t/mobster-wifi-pair", "--udid", PRO, "--enable"],
        )
        self.assertEqual(pair.command("/t", XR, "status")[-1], "--status")
        for udid, action in (
            (PRO, "unlock"),
            (PRO, "--enable"),
            ("not-a-udid", "enable"),
            (None, "enable"),
            (PRO + "; reboot", "enable"),
        ):
            with self.subTest(udid=udid, action=action), self.assertRaises(ValueError):
                pair.command("/t", udid, action)

    def test_turning_it_on_and_off(self):
        tool, log = self.tool(0, '{"enabled": true}\n')
        self.assertTrue(pair.run(PRO, "enable", tool=tool))
        self.assertEqual(log.read_text().split(), ["--udid", PRO, "--enable"])
        tool, _ = self.tool(0, '{"enabled": false}\n')
        self.assertFalse(pair.run(PRO, "disable", tool=tool))

    def test_exit_codes_map_to_problems(self):
        for code, problem in (
            (3, "needs_cable"),
            (4, "not_trusted"),
            (5, "refused"),
            (6, "failed"),
            (2, "usage"),
            (9, "failed"),
        ):
            tool, _ = self.tool(code, "", "lockdown handshake failed (-17)")
            with self.subTest(code=code), self.assertRaises(pair.PairError) as said_no:
                pair.run(PRO, "enable", tool=tool)
            self.assertEqual(said_no.exception.code, problem)
        tool, _ = self.tool(0, "not json")
        with self.assertRaises(pair.PairError):
            pair.run(PRO, "enable", tool=tool)

    @unittest.skipUnless(
        pair.SOURCE.is_file(), "the Mac app's tools are not part of this copy"
    )
    def test_the_c_source_is_the_one_the_app_bundles(self):
        source = pair.SOURCE.read_text()
        self.assertIn('"com.apple.mobile.wireless_lockdown"', source)
        self.assertIn('"EnableWifiConnections"', source)
        self.assertIn("IDEVICE_LOOKUP_USBMUX", source)
        self.assertIn("lockdownd_client_new_with_handshake", source)
        for code, name in (
            (3, "NO_DEVICE"),
            (4, "NOT_TRUSTED"),
            (5, "REFUSED"),
            (6, "FAILED"),
            (2, "USAGE"),
        ):
            self.assertIn(
                f"{name} = {code}", source.replace(",", "").replace("  ", " ")
            )
