"""Track wireless: reading CoreDevice (SPEC §3.7 item 2) and mobster-wifi-pair (item 1). devicectl's JSON on
fixtures, a hang that is killed at its timeout, the pair tool's arguments and exit codes with a fake tool, and both
refusing to run under MOBSTER_NO_DEVICES=1. No devicectl, no libimobiledevice and no phone are used."""

import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from mobile_agent import native_helpers
from mobile_agent.wireless import devicectl, pair
from mobile_agent.wireless.devicectl import DevicectlError, Peer

PRO = "00008130-001A2B3C4D5E6F70"
XR = "00008020-000A1B2C3D4E5F60"


def device(udid, *, transport="localNetwork", tunnel="connected", address="fd7a:1c2b:3d4e::1", name="Sam's iPhone",
           devmode="enabled", **extra):
    connection = {"authenticationType": "manualPairing", "isMobileDeviceOnly": False, "pairingState": "paired",
                  "potentialHostnames": [f"{udid}.coredevice.local", "Sams-iPhone.local"],
                  "localHostnames": ["Sams-iPhone.local"], "transportType": transport, "tunnelState": tunnel,
                  "tunnelTransportProtocol": "tcp", **extra}
    if address is not None:
        connection["tunnelIPAddress"] = address
    return {"capabilities": [], "connectionProperties": connection,
            "deviceProperties": {"bootState": "booted", "developerModeStatus": devmode, "name": name,
                                 "osVersionNumber": "26.0"},
            "hardwareProperties": {"deviceType": "iPhone", "marketingName": "iPhone 15 Pro", "platform": "iOS",
                                   "productType": "iPhone16,1", "reality": "physical", "udid": udid},
            "identifier": "8E5B6D2A-0000-4000-8000-000000000001", "visibilityClass": "default"}


def listing(*devices):
    return {"info": {"arguments": ["devicectl", "list", "devices"], "commandType": "devicectl.list.devices",
                     "jsonVersion": 2, "outcome": "success", "version": "518.27"},
            "result": {"devices": list(devices)}}


class ParseTests(unittest.TestCase):
    def test_a_phone_over_the_network_with_its_tunnel_up(self):
        peers = devicectl.parse(listing(device(PRO)))
        peer = peers[PRO]
        self.assertEqual((peer.udid, peer.name, peer.transport, peer.tunnel, peer.address),
                         (PRO, "Sam's iPhone", "localNetwork", "up", "fd7a:1c2b:3d4e::1"))
        self.assertTrue(peer.network)
        self.assertFalse(peer.wired)
        self.assertEqual((peer.pairing, peer.developer_mode, peer.boot), ("paired", "enabled", "booted"))
        self.assertEqual(peer.hostnames, ("Sams-iPhone.local", f"{PRO}.coredevice.local"))
        self.assertEqual(peer.fields["tunnelState"], "connected")   # what the probe prints

    def test_tunnel_states_and_transports(self):
        cases = {"connected": "up", "connecting": "connecting", "disconnected": "down", "unavailable": "down",
                 "somethingNew": "unknown"}
        for raw, state in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(devicectl.parse(listing(device(PRO, tunnel=raw)))[PRO].tunnel, state)
        wired = devicectl.parse(listing(device(PRO, transport="wired")))[PRO]
        self.assertTrue(wired.wired)
        self.assertFalse(wired.network)

    def test_several_phones_other_key_names_and_missing_fields(self):
        other = device(XR, tunnel="disconnected", address=None, tunnelIPAddressString="fd00::2")
        bare = {"hardwareProperties": {"udid": "00008030-000B2C3D4E5F6071"}}
        peers = devicectl.parse(listing(device(PRO.lower()), other, bare, {"no": "udid"}, "junk"))
        self.assertEqual(sorted(peers), sorted([PRO, XR, "00008030-000B2C3D4E5F6071"]))
        self.assertEqual(peers[XR].address, "fd00::2")
        self.assertEqual(peers[XR].tunnel, "down")
        self.assertEqual(peers["00008030-000B2C3D4E5F6071"], Peer(udid="00008030-000B2C3D4E5F6071"))

    def test_malformed_documents_read_as_no_device(self):
        for document in (None, [], {}, {"result": None}, {"result": {"devices": "nope"}}, {"result": []}):
            with self.subTest(document=document):
                self.assertEqual(devicectl.parse(document), {})

    def test_device_info_details_shape(self):
        details = {"info": {"outcome": "success"}, "result": {"device": device(PRO, tunnel="connected")}}
        self.assertEqual(devicectl.parse(details)[PRO].tunnel, "up")


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
        """A runner that runs a fake devicectl with the real argv's arguments (after `devicectl`)."""
        fake = self.fake(script)

        def run(argv, env, timeout):
            return devicectl._run([str(fake), *argv[2:]], env, timeout)
        return run

    def test_the_listing_is_read_from_the_json_output_file(self):
        document = json.dumps(listing(device(PRO)))
        script = f"""for a; do [ "$prev" = "--json-output" ] && out="$a"; prev="$a"; done
cat > "$out" <<'EOF'
{document}
EOF
exit 0
"""
        peers = devicectl.list_devices(developer="/Applications/Xcode.app/Contents/Developer",
                                       runner=self.runner_for(script))
        self.assertEqual(peers[PRO].tunnel, "up")

    def test_the_argv_uses_xcode_and_the_json_file(self):
        seen = []

        def runner(argv, env, timeout):
            seen.append((argv, env.get("DEVELOPER_DIR"), timeout))
            Path(argv[argv.index("--json-output") + 1]).write_text(json.dumps(listing()))
            return 0, ""
        devicectl.bring_up(PRO, developer="/X/Contents/Developer", runner=runner)
        argv, developer, timeout = seen[0]
        self.assertEqual(argv[1:6], ["devicectl", "device", "info", "details", "--device"])
        self.assertEqual(argv[6], PRO)
        self.assertEqual(argv[7], "--json-output")
        self.assertEqual((developer, timeout), ("/X/Contents/Developer", devicectl.BRING_UP_TIMEOUT))
        with self.assertRaises(ValueError):
            devicectl.bring_up("; rm -rf /", developer="/X", runner=runner)

    def test_a_hang_is_killed_at_its_timeout(self):
        marker = Path(tempfile.mkdtemp(prefix="mobster-hang-")) / "child.pid"
        script = f"""sleep 30 &
echo $! > {marker}
wait
"""
        started = time.monotonic()
        with self.assertRaises(DevicectlError) as hung:
            devicectl.list_devices(timeout=0.5, developer="/X", runner=self.runner_for(script))
        self.assertEqual(hung.exception.code, "hang")
        self.assertLess(time.monotonic() - started, 5)
        child = int(marker.read_text())
        for _ in range(50):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(.05)
        else:
            self.fail("the hung devicectl's child is still running")

    def test_a_failure_without_an_answer(self):
        with self.assertRaises(DevicectlError) as failed:
            devicectl.list_devices(developer="/X", runner=self.runner_for("echo 'No provider' >&2\nexit 1\n"))
        self.assertEqual(failed.exception.code, "failed")
        self.assertIn("No provider", str(failed.exception))

    def test_no_xcode(self):
        with patch.object(devicectl, "developer_dir", lambda: None), self.assertRaises(DevicectlError) as missing:
            devicectl.list_devices(runner=lambda *a: self.fail("ran without Xcode"))
        self.assertEqual(missing.exception.code, "no_xcode")


class NoDevicesTests(unittest.TestCase):
    def test_devicectl_and_the_pair_tool_raise_instead_of_running(self):
        with patch.dict(os.environ, {"MOBSTER_NO_DEVICES": "1"}):
            with self.assertRaises(RuntimeError):
                devicectl.list_devices(runner=lambda *a: self.fail("devicectl ran"))
            with self.assertRaises(RuntimeError):
                devicectl.bring_up(PRO, runner=lambda *a: self.fail("devicectl ran"))
            with patch.object(subprocess, "run", side_effect=AssertionError("the pair tool ran")), \
                    self.assertRaises(RuntimeError):
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
        path.write_text(f"#!/bin/sh\necho \"$@\" > {log}\nprintf '%s' '{stdout}'\nprintf '%s' '{stderr}' >&2\n"
                        f"exit {exit_code}\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return str(path), log

    def test_the_arguments(self):
        self.assertEqual(pair.command("/t/mobster-wifi-pair", PRO, "enable"),
                         ["/t/mobster-wifi-pair", "--udid", PRO, "--enable"])
        self.assertEqual(pair.command("/t", XR, "status")[-1], "--status")
        for udid, action in ((PRO, "unlock"), (PRO, "--enable"), ("not-a-udid", "enable"), (None, "enable"),
                             (PRO + "; reboot", "enable")):
            with self.subTest(udid=udid, action=action), self.assertRaises(ValueError):
                pair.command("/t", udid, action)

    def test_turning_it_on_and_off(self):
        tool, log = self.tool(0, '{"enabled": true}\n')
        self.assertTrue(pair.run(PRO, "enable", tool=tool))
        self.assertEqual(log.read_text().split(), ["--udid", PRO, "--enable"])
        tool, _ = self.tool(0, '{"enabled": false}\n')
        self.assertFalse(pair.run(PRO, "disable", tool=tool))

    def test_exit_codes_map_to_problems(self):
        for code, problem in ((3, "needs_cable"), (4, "not_trusted"), (5, "refused"), (6, "failed"), (2, "usage"),
                              (9, "failed")):
            tool, _ = self.tool(code, "", "lockdown handshake failed (-17)")
            with self.subTest(code=code), self.assertRaises(pair.PairError) as said_no:
                pair.run(PRO, "enable", tool=tool)
            self.assertEqual(said_no.exception.code, problem)
        tool, _ = self.tool(0, "not json")
        with self.assertRaises(pair.PairError):
            pair.run(PRO, "enable", tool=tool)

    def test_no_tool_means_the_xcode_step(self):
        with patch.object(pair, "locate", lambda: None), self.assertRaises(LookupError):
            pair.run(PRO, "enable")

    def test_the_mac_apps_copy_is_found_first(self):
        folder = Path(tempfile.mkdtemp(prefix="mobster-tools-"))
        bundled = folder / "mobster-wifi-pair"
        bundled.write_text("#!/bin/sh\n")
        bundled.chmod(0o755)
        with patch.dict(os.environ, {"MOBSTER_IPHONE_TOOLS": str(folder)}):
            self.assertEqual(pair.find(), str(bundled))

    @unittest.skipUnless(pair.SOURCE.is_file(), "the Mac app's tools are not part of this copy")
    def test_the_c_source_is_the_one_the_app_bundles(self):
        source = pair.SOURCE.read_text()
        self.assertIn('"com.apple.mobile.wireless_lockdown"', source)
        self.assertIn('"EnableWifiConnections"', source)
        self.assertIn("IDEVICE_LOOKUP_USBMUX", source)      # USB only: never turned on over the network
        self.assertIn("lockdownd_client_new_with_handshake", source)
        for code, name in ((3, "NO_DEVICE"), (4, "NOT_TRUSTED"), (5, "REFUSED"), (6, "FAILED"), (2, "USAGE")):
            self.assertIn(f"{name} = {code}", source.replace(",", "").replace("  ", " "))


def pkg_config_flags():
    """The compiler flags for libimobiledevice and libplist on this Mac (Homebrew's), or None."""
    import shutil
    import subprocess
    tool = shutil.which("pkg-config") or next((path for path in ("/opt/homebrew/bin/pkg-config",
                                                                  "/usr/local/bin/pkg-config")
                                               if os.access(path, os.X_OK)), None)
    if not tool or not pair.SOURCE.is_file():
        return None
    try:
        return subprocess.run([tool, "--cflags", "--libs", *pair.LIBRARIES], capture_output=True, text=True,
                              timeout=20, check=True).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return None


class BuildTests(unittest.TestCase):
    """mobster-wifi-pair compiles cleanly (-Wall -Wextra -Werror) against the libraries the Mac app bundles, and
    refuses bad arguments before it looks for any phone. It never reaches usbmuxd here: only usage is run."""

    def test_it_compiles_and_refuses_bad_arguments(self):
        import subprocess
        flags = pkg_config_flags()
        if flags is None:
            self.skipTest("libimobiledevice isn't installed here (brew install libimobiledevice pkg-config)")
        folder = Path(tempfile.mkdtemp(prefix="mobster-wifi-pair-build-"))
        binary = folder / "mobster-wifi-pair"
        try:
            built = subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-O2", str(pair.SOURCE), "-o",
                                    str(binary), *flags], capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as error:
            self.skipTest(f"no C compiler here: {error}")
        self.assertEqual(built.returncode, 0, built.stderr)
        for argv in ([], ["--enable"], ["--udid", "not-a-udid", "--enable"], ["--udid", PRO],
                     ["--udid", PRO, "--enable", "--disable"], ["--udid", PRO, "--unlock"]):
            with self.subTest(argv=argv):
                ran = subprocess.run([str(binary), *argv], capture_output=True, text=True, timeout=10)
                self.assertEqual(ran.returncode, 2)
                self.assertIn("usage: mobster-wifi-pair", ran.stderr)
                self.assertEqual(ran.stdout, "")


if __name__ == "__main__":
    unittest.main()
