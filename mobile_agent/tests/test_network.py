import argparse
import contextlib
import fcntl
import hashlib
import io
import json
import os
import plistlib
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from mobile_agent.phone_io import PhoneIOError, cli
from mobile_agent.phone_io import network_capture as capture
from mobile_agent.phone_io import network_profiles as profiles

PHONE = {"kind": "usb", "udid": "TEST-PHONE"}
HEADER = b"\xd4\xc3\xb2\xa1" + bytes(20)


class FakeTcpdump:
    def __init__(self, outcome=0, stop_timeout=False, signal_error=None):
        self.outcome = outcome
        self.stop_timeout = stop_timeout
        self.signal_error = signal_error
        self.returncode = None
        self.signals = []
        self.killed = False
        self.waits = []

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        self.waits.append(timeout)
        if self.killed:
            self.returncode = -signal.SIGKILL
        elif self.signals:
            if self.stop_timeout:
                raise subprocess.TimeoutExpired("tcpdump", timeout)
            self.returncode = -signal.SIGINT
        elif isinstance(self.outcome, BaseException):
            raise self.outcome
        elif callable(self.outcome):
            self.outcome()
        else:
            self.returncode = self.outcome
        return self.returncode

    def send_signal(self, sig):
        if self.signal_error:
            raise self.signal_error
        self.signals.append(sig)

    def kill(self):
        self.killed = True


class CaptureTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.path = self.folder / "trace.pcap"
        self.rvi_calls = []
        self.process = FakeTcpdump()
        self.existing = False
        self.detach_error = False
        self.data = HEADER
        self.stream = None
        self.start_error = None
        self.start_signal = None
        patches = [
            mock.patch("mobile_agent.native_helpers.require_devices"),
            mock.patch("mobile_agent.mac_tools.usb_attached", return_value=True),
            mock.patch("mobile_agent.paths.user_data_dir", return_value=self.folder),
            mock.patch.object(capture, "binary", side_effect=lambda name: "/fake/" + name),
            mock.patch.object(capture, "rvi_command", side_effect=self.rvi),
            mock.patch.object(capture.subprocess, "Popen", side_effect=self.popen),
        ]
        self.mocks = [self.enterContext(patch) for patch in patches]

    def rvi(self, tool, *args):
        self.rvi_calls.append(args)
        if args == ("-l",):
            return "TEST-PHONE rvi2" if self.existing else "\nCould not get list of devices\n"
        if args == ("-s", "TEST-PHONE"):
            return "Starting device TEST-PHONE [SUCCEEDED] with interface rvi2"
        if args == ("-x", "TEST-PHONE"):
            if self.detach_error:
                raise PhoneIOError("detach failed")
            return "Stopping device TEST-PHONE [SUCCEEDED]"
        self.fail(f"Unexpected rvictl command: {args}")

    def popen(self, args, **kwargs):
        self.argv = args
        self.stream = kwargs["stdout"]
        if self.start_error:
            raise self.start_error
        self.stream.write(self.data)
        if self.start_signal is not None:
            signal.raise_signal(self.start_signal)
        return self.process

    def run_capture(self, **kwargs):
        return capture.capture(PHONE, self.path, **kwargs)

    def test_bounded_capture_private_output_and_owned_interface_cleanup(self):
        result = self.run_capture(seconds=10, packets=5, snaplen=256)
        self.assertEqual(self.path.read_bytes(), HEADER)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(result["bytes"], 24)
        self.assertEqual(self.argv, ["/fake/tcpdump", "-i", "rvi2", "-n", "-U", "-s", "256", "-c", "5", "-w", "-"])
        self.assertEqual(self.rvi_calls, [("-l",), ("-s", "TEST-PHONE"), ("-x", "TEST-PHONE")])
        self.assertTrue(self.stream.closed)
        self.assertIsNone(result["warning"])
        self.assertEqual(self.process.waits, [10])

    def test_timeout_sends_sigint_and_existing_interface_is_never_detached(self):
        self.existing = True
        self.process = FakeTcpdump(subprocess.TimeoutExpired("tcpdump", 2))
        result = self.run_capture(seconds=2)
        self.assertTrue(result["ok"])
        self.assertEqual(self.process.signals, [signal.SIGINT])
        self.assertEqual(self.process.waits, [2, 5])
        self.assertFalse(self.process.killed)
        self.assertEqual(self.rvi_calls, [("-l",)])

    def test_termination_signals_stop_capture_detach_and_restore_handlers(self):
        previous = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
        for number in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=number):
                self.path.unlink(missing_ok=True)
                self.process = FakeTcpdump(lambda: signal.raise_signal(number))
                with self.assertRaises(KeyboardInterrupt):
                    self.run_capture()
                self.assertEqual(self.process.signals, [signal.SIGINT])
                self.assertIsNotNone(self.process.returncode)
                self.assertTrue(self.stream.closed)
                self.assertEqual(self.rvi_calls[-1], ("-x", "TEST-PHONE"))
                self.assertEqual(self.path.read_bytes(), HEADER)
                self.assertEqual({number: signal.getsignal(number) for number in previous}, previous)

    def test_detach_failure_is_visible_when_capture_fails_or_is_cancelled(self):
        self.detach_error = True
        for outcome, message in ((1, "tcpdump couldn't capture"), (KeyboardInterrupt(), "capture was interrupted")):
            with self.subTest(outcome=outcome):
                self.path.unlink(missing_ok=True)
                self.process = FakeTcpdump(outcome)
                with self.assertRaises(PhoneIOError) as caught:
                    self.run_capture()
                self.assertIn(message, str(caught.exception))
                self.assertIn("RVI could not be removed", str(caught.exception))
                self.assertIn("rvictl -l", str(caught.exception))
                self.assertTrue(self.stream.closed)

    def test_stop_failure_still_closes_stream_and_detaches_owned_interface(self):
        self.process = FakeTcpdump(KeyboardInterrupt(), signal_error=PermissionError("private diagnostic"))
        with self.assertRaises(PhoneIOError) as caught:
            self.run_capture()
        self.assertNotIn("private diagnostic", str(caught.exception))
        self.assertTrue(self.stream.closed)
        self.assertEqual(self.rvi_calls[-1], ("-x", "TEST-PHONE"))

    def test_unresponsive_tcpdump_is_killed_and_not_reported_successful(self):
        self.process = FakeTcpdump(subprocess.TimeoutExpired("tcpdump", 2), stop_timeout=True)
        with self.assertRaises(PhoneIOError):
            self.run_capture(seconds=2)
        self.assertTrue(self.process.killed)
        self.assertTrue(self.stream.closed)
        self.assertEqual(self.rvi_calls[-1], ("-x", "TEST-PHONE"))

    def test_start_failure_removes_empty_artifact_and_detaches(self):
        self.start_error = PermissionError("private diagnostic")
        with self.assertRaises(PhoneIOError) as caught:
            self.run_capture()
        self.assertEqual(caught.exception.code, "tools")
        self.assertNotIn("private diagnostic", str(caught.exception))
        self.assertFalse(self.path.exists())
        self.assertTrue(self.stream.closed)
        self.assertEqual(self.rvi_calls[-1], ("-x", "TEST-PHONE"))

    def test_signal_during_spawn_still_stops_and_reaps_the_started_child(self):
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            self.path.unlink(missing_ok=True)
            self.process = FakeTcpdump()
            self.start_signal = number
            with self.subTest(signal=number), self.assertRaises(KeyboardInterrupt):
                self.run_capture()
            self.assertIsNotNone(self.process.returncode)
            self.assertEqual(self.process.signals, [signal.SIGINT])
            self.assertTrue(self.stream.closed)
            self.assertEqual(self.rvi_calls[-1], ("-x", "TEST-PHONE"))

    def test_bad_capture_header_and_exit_status_are_errors(self):
        for data, code in ((b"", 0), (b"partial", 0), (HEADER, 1)):
            with self.subTest(data=data, code=code):
                self.path.unlink(missing_ok=True)
                self.data, self.process = data, FakeTcpdump(code)
                with self.assertRaises(PhoneIOError):
                    self.run_capture()
                self.assertEqual(self.path.exists(), bool(data))
                self.assertEqual(self.rvi_calls[-1], ("-x", "TEST-PHONE"))

    def test_detach_failure_preserves_successful_trace_and_warns(self):
        self.detach_error = True
        result = self.run_capture()
        self.assertEqual(self.path.read_bytes(), HEADER)
        self.assertIn("RVI could not be removed", result["warning"])

    def test_existing_output_and_concurrent_capture_refuse_before_rvi_changes(self):
        self.path.write_bytes(b"keep")
        with self.assertRaises(PhoneIOError):
            self.run_capture()
        self.assertEqual(self.path.read_bytes(), b"keep")
        self.assertEqual(self.rvi_calls, [])
        self.path.unlink()
        with mock.patch.object(capture.fcntl, "flock", side_effect=BlockingIOError), self.assertRaises(PhoneIOError):
            self.run_capture()
        self.assertFalse(self.path.exists())
        self.assertEqual(self.rvi_calls, [])

    def test_device_identifier_case_cannot_bypass_the_capture_lock(self):
        def duplicate():
            with mock.patch.object(capture, "_capture") as start:
                with self.assertRaises(PhoneIOError) as caught:
                    capture.capture({**PHONE, "udid": "test-phone"}, self.folder / "second.pcap")
                self.assertIn("already capturing", str(caught.exception))
                start.assert_not_called()
            self.process.returncode = 0

        self.process = FakeTcpdump(duplicate)
        self.run_capture()

    def test_invalid_limits_and_non_usb_devices_never_start_capture(self):
        for options in ({"seconds": 0}, {"seconds": 301}, {"seconds": True}, {"packets": 0},
                        {"packets": 100001}, {"snaplen": 63}, {"snaplen": 65536}):
            with self.subTest(options=options), self.assertRaises(PhoneIOError):
                self.run_capture(**options)
        for record in ({"kind": "simulator", "udid": "SIM"}, {"kind": "usb"}):
            with self.subTest(record=record), self.assertRaises(PhoneIOError):
                capture.capture(record, self.path)
        self.mocks[1].return_value = False
        with self.assertRaises(PhoneIOError):
            self.run_capture()
        self.assertEqual(self.rvi_calls, [])
        self.assertFalse(self.path.exists())

    def test_unknown_rvi_creation_outcome_is_not_retried_or_detached(self):
        self.mocks[4].side_effect = ["No devices", "Starting device TEST-PHONE [FAILED]"]
        with self.assertRaises(PhoneIOError):
            self.run_capture()
        self.assertEqual(self.mocks[4].call_count, 2)
        self.assertFalse(self.path.exists())
        self.mocks[5].assert_not_called()

    def state_file(self):
        key = hashlib.sha256(b"TEST-PHONE").hexdigest()[:24]
        return self.folder / "network-capture" / (key + ".json")

    def test_dead_capture_leftovers_are_cleaned_at_the_next_start(self):
        self.state_file().parent.mkdir()
        self.state_file().write_text('{"owned": true}')
        self.existing = True
        self.run_capture()
        self.assertEqual(self.rvi_calls[:2], [("-l",), ("-x", "TEST-PHONE")])
        self.assertFalse(self.state_file().exists())
        self.assertEqual(self.path.read_bytes(), HEADER)

    def test_stale_state_without_a_leftover_interface_is_only_cleared(self):
        self.state_file().parent.mkdir()
        self.state_file().write_text("not json")
        self.run_capture()
        self.assertNotIn(("-x", "TEST-PHONE"), self.rvi_calls[:2])
        self.assertEqual(self.rvi_calls[0], ("-l",))
        self.assertFalse(self.state_file().exists())

    def test_failed_stale_cleanup_refuses_and_keeps_the_record(self):
        self.state_file().parent.mkdir()
        self.state_file().write_text("{}")
        self.existing = True
        self.detach_error = True
        with self.assertRaises(PhoneIOError):
            self.run_capture()
        self.assertTrue(self.state_file().exists())
        self.assertFalse(self.path.exists())
        self.mocks[5].assert_not_called()

    def test_live_lock_is_refused_and_its_state_is_left_alone(self):
        self.state_file().parent.mkdir()
        self.state_file().write_text('{"owned": true}')
        self.existing = True
        with open(self.folder / "network-capture" / (self.state_file().stem + ".lock"), "a") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(PhoneIOError) as caught:
                self.run_capture()
        self.assertIn("already capturing", str(caught.exception))
        self.assertEqual(self.rvi_calls, [])
        self.assertTrue(self.state_file().exists())
        self.assertFalse(self.path.exists())

    def test_tcpdump_gets_a_kernel_deadline_past_the_normal_stop(self):
        self.run_capture(seconds=10)
        with mock.patch.object(capture.signal, "alarm") as alarm:
            self.mocks[5].call_args.kwargs["preexec_fn"]()
        alarm.assert_called_once_with(10 + capture.HARD_STOP_GRACE)


class RviTests(unittest.TestCase):
    def test_parser_matches_whole_device_identifier(self):
        self.assertEqual(capture.rvi_for("TEST-PHONE rvi4", "TEST-PHONE"), "rvi4")
        self.assertIsNone(capture.rvi_for("OTHER-TEST-PHONE rvi4", "TEST-PHONE"))
        self.assertIsNone(capture.rvi_for("TEST-PHONE-OTHER rvi4", "TEST-PHONE"))
        self.assertIsNone(capture.rvi_for("TEST-PHONE [FAILED]", "TEST-PHONE"))

    def test_rvi_timeout_and_failure_hide_diagnostics_and_never_retry(self):
        for outcome in (subprocess.TimeoutExpired("rvictl", 15), KeyboardInterrupt(), OSError("secret"),
                        subprocess.CompletedProcess([], 1, "secret", "secret")):
            with self.subTest(outcome=outcome), mock.patch.object(capture.subprocess, "run") as run:
                if isinstance(outcome, BaseException):
                    run.side_effect = outcome
                else:
                    run.return_value = outcome
                with self.assertRaises(PhoneIOError) as caught:
                    capture.rvi_command("/fake/rvictl", "-s", "TEST-PHONE")
                self.assertNotIn("secret", str(caught.exception))
                self.assertEqual(run.call_count, 1)


class CaptureCrashTests(unittest.TestCase):
    def test_child_keeps_phone_lock_after_parent_kill_until_capture_exits(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            lock_path, trace, ready = root / "capture.lock", root / "trace.pcap", root / "ready"
            helper = root / "fake-tcpdump"
            helper.write_text(f"#!{sys.executable}\nimport os, pathlib, time\n"
                              f"os.write(1, {HEADER!r})\n"
                              f"pathlib.Path({str(ready)!r}).write_text(str(os.getpid()))\n"
                              "time.sleep(60)\n")
            helper.chmod(0o700)
            parent_code = """
import fcntl, pathlib, sys
from mobile_agent.phone_io import network_capture as capture
capture.rvi_command = lambda *args: 'TEST-PHONE rvi2'
with open(sys.argv[1], 'a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    capture._capture('unused-rvictl', sys.argv[2], 'TEST-PHONE', sys.argv[3], 30, 10, 128, lock.fileno(), pathlib.Path(sys.argv[1] + '.json'))
"""  # noqa: E501
            parent = subprocess.Popen([sys.executable, "-c", parent_code, str(lock_path), str(helper), str(trace)],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            child_pid = None
            try:
                deadline = time.monotonic() + 10
                while not ready.exists() and parent.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists(), "fake capture did not start")
                child_pid = int(ready.read_text())
                parent.kill()
                parent.wait(timeout=5)
                with lock_path.open("a") as contender:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    os.kill(child_pid, signal.SIGTERM)
                    child_pid = None
                    deadline = time.monotonic() + 5
                    while True:
                        try:
                            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                self.fail("capture lock survived the fake child exit")
                            time.sleep(0.01)
                    with self.assertRaises(PhoneIOError):
                        capture._capture("unused", str(helper), "TEST-PHONE", trace, 1, 1, 128, contender.fileno(),
                                         root / "state.json")
                self.assertEqual(trace.read_bytes(), HEADER)
            finally:
                if parent.poll() is None:
                    parent.kill()
                parent.communicate(timeout=5)
                if child_pid is not None:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_capture_ends_by_itself_when_nothing_stops_it(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 preexec_fn=capture.die_after(1), stdin=subprocess.DEVNULL)
        try:
            self.assertEqual(child.wait(timeout=10), -signal.SIGALRM)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)


class NetworkCLITests(unittest.TestCase):
    def setUp(self):
        self.parser = argparse.ArgumentParser()
        cli.add_arguments(self.parser, {})
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.output = Path(self.folder.name) / "network.mobileconfig"

    def invoke(self, *arguments):
        return self.invoke_phone("network", *arguments)

    def invoke_phone(self, *arguments):
        args = self.parser.parse_args([*arguments, "--json"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.run(args)
        return code, json.loads(output.getvalue())

    def test_proxy_profile_writes_credentials_without_printing_them_or_selecting_a_device(self):
        secret = Path(self.folder.name) / "password"
        secret.write_text("a-secret-password\n")
        with mock.patch("mobile_agent.phone_io.network_cli.choose_device") as choose:
            code, result = self.invoke("proxy-profile", "--ssid", "Test Wi-Fi", "--server", "proxy.example.com",
                                       "--username", "tester", "--password-file", str(secret),
                                       "--output", str(self.output))
        self.assertEqual(code, 0)
        self.assertFalse(result["installed"])
        self.assertNotIn("a-secret-password", json.dumps(result))
        self.assertIn("Settings", result["next"])
        choose.assert_not_called()
        profile = plistlib.loads(self.output.read_bytes())
        self.assertEqual(profile["PayloadIdentifier"], "app.mobster.network.proxy")
        self.assertEqual(profile["PayloadContent"][0]["ProxyPassword"], "a-secret-password")
        self.assertEqual(profile["PayloadContent"][0]["ProxyServerPort"], 8080)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)

    def test_existing_profile_is_kept(self):
        self.output.write_text("keep this")
        code, result = self.invoke("proxy-profile", "--ssid", "Test", "--pac", "https://proxy.example/pac",
                                   "--output", str(self.output))
        self.assertEqual(code, 2)
        self.assertEqual(result["error"]["code"], "usage")
        self.assertEqual(self.output.read_text(), "keep this")

    def test_proxy_mode_is_required_and_exclusive(self):
        for mode in ([], ["--server", "proxy.example", "--pac", "https://proxy.example/pac"]):
            with (self.subTest(mode=mode), contextlib.redirect_stderr(io.StringIO()),
                  self.assertRaises(SystemExit) as caught):
                self.parser.parse_args(["network", "proxy-profile", "--ssid", "Test", "--output", "test", *mode])
            self.assertEqual(caught.exception.code, 2)

    def test_pac_credentials_are_refused_without_creating_a_file(self):
        code, result = self.invoke("proxy-profile", "--ssid", "Test", "--pac", "https://proxy.example/pac",
                                   "--username", "tester", "--output", str(self.output))
        self.assertEqual(code, 2)
        self.assertFalse(result["ok"])
        self.assertFalse(self.output.exists())

    def test_vpn_and_relay_profiles_are_available_without_hardware(self):
        for kind, flags, payload_type in (
            ("vpn", ["--server", "vpn.example", "--remote-id", "vpn.example", "--local-id", "tester",
                     "--username", "tester"], "com.apple.vpn.managed"),
            ("relay", ["--url", "https://relay.example"], "com.apple.relay.managed"),
        ):
            with self.subTest(kind=kind):
                target = self.output.with_name(kind + ".mobileconfig")
                code, result = self.invoke(kind + "-profile", *flags, "--output", str(target))
                self.assertEqual(code, 0)
                self.assertEqual(result["identifier"], "app.mobster.network." + kind)
                self.assertEqual(plistlib.loads(target.read_bytes())["PayloadContent"][0]["PayloadType"], payload_type)

    def test_capture_validates_limits_before_selecting_a_device(self):
        for flag, value in (("--seconds", "0"), ("--seconds", "301"), ("--packets", "100001"),
                            ("--snaplen", "63"), ("--snaplen", "65536")):
            with self.subTest(flag=flag, value=value), \
                    mock.patch("mobile_agent.phone_io.network_cli.choose_device") as choose, \
                    mock.patch("mobile_agent.phone_io.network_capture.capture") as capture:
                code, result = self.invoke("capture", "--device", "Test", "--output", "test.pcap", flag, value)
            self.assertEqual(code, 2)
            self.assertEqual(result["error"]["code"], "usage")
            choose.assert_not_called()
            capture.assert_not_called()

    def test_capture_reports_saved_artifact(self):
        record = {"id": "test", "kind": "usb"}
        result = {"ok": True, "path": "/tmp/test.pcap", "bytes": 24, "warning": None}
        with mock.patch("mobile_agent.phone_io.network_cli.choose_device", return_value=record), \
                mock.patch("mobile_agent.phone_io.network_capture.capture", return_value=result) as capture:
            code, response = self.invoke("capture", "--device", "Test", "--output", "test.pcap")
        self.assertEqual((code, response), (0, result))
        capture.assert_called_once_with(record, "test.pcap", seconds=30, packets=10000, snaplen=128)

    def test_diagnose_only_claims_mac_reachability_and_reports_usb_unknown(self):
        for reachable in (True, False):
            with self.subTest(reachable=reachable), \
                    mock.patch("mobile_agent.phone_io.network_cli.choose_device", return_value={
                        "id": "test", "kind": "usb", "udid": "test"}), \
                    mock.patch("mobile_agent.mac_tools.usb_attached", return_value=None), \
                    mock.patch("mobile_agent.wireless.network.primary_interface", return_value="en0"), \
                    mock.patch("mobile_agent.wireless.network.default_gateway", return_value="192.0.2.1"), \
                    mock.patch("mobile_agent.wireless.network.nwi", return_value=None), \
                    mock.patch("mobile_agent.wireless.network.port_open", return_value=reachable) as connect:
                code, result = self.invoke("diagnose", "--device", "Test", "--host", "proxy.example", "--port", "8081")
            self.assertEqual(code, 0 if reachable else 1)
            self.assertEqual(result["scope"], "mac")
            self.assertFalse(result["phoneEgressTested"])
            self.assertEqual(result["endpoint"]["reachableFromMac"], reachable)
            self.assertIsNone(result["device"]["usbAttached"])
            connect.assert_called_once_with("proxy.example", 8081)

    def test_diagnose_rejects_bad_endpoint_before_reading_network(self):
        with mock.patch("mobile_agent.wireless.network.primary_interface") as interface:
            code, result = self.invoke("diagnose", "--host", "https://secret@example.com")
        self.assertEqual(code, 2)
        self.assertNotIn("secret", json.dumps(result))
        interface.assert_not_called()


class NetworkProfilesTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def write(self, payload, filename="network.mobileconfig"):
        path = self.folder / filename
        result = profiles.write_profile(payload, path)
        return path, result, plistlib.loads(path.read_bytes())

    def test_manual_proxy_is_removable_private_and_does_not_echo_credentials(self):
        payload = profiles.proxy(ssid="Lab Wi-Fi", server="proxy.example.com", port=3128,
                                 username="alice", password="proxy-secret", wifi_password="wifi-secret")
        path, result, profile = self.write(payload)
        content = profile["PayloadContent"][0]
        self.assertEqual(profile["PayloadType"], "Configuration")
        self.assertEqual(profile["PayloadVersion"], 1)
        self.assertFalse(profile["PayloadRemovalDisallowed"])
        self.assertEqual(content["PayloadType"], "com.apple.wifi.managed")
        self.assertEqual(content["SSID_STR"], "Lab Wi-Fi")
        self.assertEqual(content["ProxyType"], "Manual")
        self.assertEqual((content["ProxyServer"], content["ProxyServerPort"]), ("proxy.example.com", 3128))
        self.assertEqual((content["ProxyUsername"], content["ProxyPassword"]), ("alice", "proxy-secret"))
        self.assertEqual(content["Password"], "wifi-secret")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertFalse(result["installed"])
        for value in ("alice", "proxy-secret", "wifi-secret", "Lab Wi-Fi", "proxy.example.com"):
            self.assertNotIn(value, json.dumps(result))
        _, _, second = self.write(payload, "second.mobileconfig")
        self.assertEqual(profile["PayloadUUID"], second["PayloadUUID"])
        self.assertEqual(content["PayloadUUID"], second["PayloadContent"][0]["PayloadUUID"])
        self.assertNotEqual(profile["PayloadUUID"], content["PayloadUUID"])

    def test_pac_has_no_fallback_and_existing_output_is_untouched(self):
        payload = profiles.proxy(ssid="Lab", pac="https://config.example/proxy.pac", encryption="None")
        path, _, profile = self.write(payload)
        content = profile["PayloadContent"][0]
        self.assertEqual(content["ProxyType"], "Auto")
        self.assertEqual(content["ProxyPACURL"], "https://config.example/proxy.pac")
        self.assertFalse(content["ProxyPACFallbackAllowed"])
        self.assertNotIn("ProxyServer", content)
        original = path.read_bytes()
        with self.assertRaises(PhoneIOError):
            profiles.write_profile(payload, path)
        self.assertEqual(path.read_bytes(), original)
        link = self.folder / "link.mobileconfig"
        link.symlink_to(path)
        with self.assertRaises(PhoneIOError):
            profiles.write_profile(payload, link)
        self.assertEqual(path.read_bytes(), original)

    def test_vpn_and_relay_use_apple_payload_keys(self):
        payload = profiles.vpn(server="vpn.example.com", remote_id="vpn.example.com", local_id="alice",
                               username="alice", password="vpn-secret")
        _, result, profile = self.write(payload)
        content = profile["PayloadContent"][0]
        self.assertEqual((content["PayloadType"], content["VPNType"]), ("com.apple.vpn.managed", "IKEv2"))
        self.assertEqual(content["IKEv2"], {
            "RemoteAddress": "vpn.example.com", "RemoteIdentifier": "vpn.example.com", "LocalIdentifier": "alice",
            "AuthenticationMethod": "None", "ExtendedAuthEnabled": 1, "AuthName": "alice",
            "AuthPassword": "vpn-secret", "IncludeAllNetworks": 1, "OnDemandEnabled": 0,
        })
        self.assertNotIn("vpn-secret", json.dumps(result))
        payload = profiles.relay(endpoint="https://relay.example.com/relay", token="relay-secret")
        _, result, profile = self.write(payload, "relay.mobileconfig")
        content = profile["PayloadContent"][0]
        self.assertEqual(content["PayloadType"], "com.apple.relay.managed")
        self.assertEqual(content["Relays"], [{"HTTP2RelayURL": "https://relay.example.com/relay",
                                             "AdditionalHTTPHeaderFields": {"Authorization": "Bearer relay-secret"}}])
        self.assertNotIn("relay-secret", json.dumps(result))
        self.assertNotIn("AdditionalHTTPHeaderFields", profiles.relay(endpoint="https://relay.example")["Relays"][0])

    def test_bad_proxy_combinations_and_ports_refuse(self):
        for options in ({}, {"server": "proxy.example", "pac": "https://pac.example"},
                        {"pac": "https://pac.example", "username": "alice"},
                        {"server": "proxy.example", "password": "secret"},
                        {"server": "proxy.example", "encryption": "None", "wifi_password": "secret"}):
            with self.subTest(options=options), self.assertRaises(PhoneIOError):
                profiles.proxy(ssid="Lab", **options)
        for port in (0, -1, 65536, True, "8080", 3.5):
            with self.subTest(port=port), self.assertRaises(PhoneIOError):
                profiles.proxy(ssid="Lab", server="proxy.example", port=port)
        for ssid in ("", "é" * 17, "Lab\n", None):
            with self.subTest(ssid=ssid), self.assertRaises(PhoneIOError):
                profiles.proxy(ssid=ssid, server="proxy.example")
        self.assertEqual(profiles.proxy(ssid="é" * 16, server="::1", port=65535)["ProxyServer"], "::1")

    def test_invalid_urls_and_hosts_do_not_leak_inputs_in_errors(self):
        for value in ("ftp://host.example", "https://user:secret@host.example", "https://@host.example",
                      "https://host.example:65536", "https://host.example:bad", "https://host.example:0",
                      "https://host.example/path#fragment", "https://host.example/\nsecret", "http://[bad", ""):
            with self.subTest(value=value), self.assertRaises(PhoneIOError) as caught:
                profiles.url(value)
            self.assertNotIn("secret", str(caught.exception))
        for value in ("http://host.example", "host.example:8080", "user@host.example", "bad name", "-host", ""):
            with self.subTest(value=value), self.assertRaises(PhoneIOError):
                profiles.host(value)
        with self.assertRaises(PhoneIOError):
            profiles.relay(endpoint="http://relay.example")
        with self.assertRaises(PhoneIOError):
            profiles.relay(endpoint="https://relay.example", token="secret\r\nInjected: value")
        with self.assertRaises(PhoneIOError):
            profiles.vpn(server="vpn.example", remote_id="", local_id="alice", username="alice")

    def test_credentials_are_bounded_one_line_utf8_files(self):
        path = self.folder / "credential"
        for content, expected in ((b" secret \r\n", " secret "), (b"x" * 4096, "x" * 4096),
                                  (b"x" * 4096 + b"\r\n", "x" * 4096)):
            path.write_bytes(content)
            self.assertEqual(profiles.secret(path), expected)
        for content in (b"", b"\xff", b"x" * 4097, b"x" * 4096 + b"\nextra", b"x" * 4096 + b"\r\nextra",
                        b"line\nother", b"secret\x00", b"\n"):
            path.write_bytes(content)
            with self.subTest(content=content[:20]), self.assertRaises(PhoneIOError) as caught:
                profiles.secret(path)
            self.assertNotIn("secret", str(caught.exception))
        with self.assertRaises(PhoneIOError):
            profiles.secret(self.folder / "missing")
        self.assertIsNone(profiles.secret(None))

    def test_unencodable_profile_is_rejected_before_creating_output(self):
        path = self.folder / "network.mobileconfig"
        with self.assertRaises(PhoneIOError):
            profiles.write_profile(profiles.relay(endpoint="https://relay.example"), path, name="bad\x00name")
        self.assertFalse(path.exists())

    def test_partial_write_or_interruption_never_publishes_and_retry_succeeds(self):
        path = self.folder / "network.mobileconfig"
        payload = profiles.relay(endpoint="https://relay.example")
        temporary = tempfile.NamedTemporaryFile
        for failure in (OSError("disk full"), KeyboardInterrupt()):
            @contextmanager
            def failing_file(**kwargs):
                with temporary(**kwargs) as stream:
                    def write(data):
                        stream.write(data[:20])
                        stream.flush()
                        raise failure
                    wrapped = mock.Mock(wraps=stream)
                    wrapped.write.side_effect = write
                    yield wrapped

            with self.subTest(failure=failure), mock.patch.object(profiles.tempfile, "NamedTemporaryFile",
                                                                  failing_file):
                with self.assertRaises(PhoneIOError if isinstance(failure, OSError) else KeyboardInterrupt):
                    profiles.write_profile(payload, path)
            self.assertFalse(path.exists())
            self.assertEqual(list(self.folder.iterdir()), [])
        profiles.write_profile(payload, path)
        self.assertEqual(plistlib.loads(path.read_bytes())["PayloadType"], "Configuration")

    def test_concurrent_output_wins_without_clobbering_or_leaving_staging_file(self):
        path = self.folder / "network.mobileconfig"
        link = profiles.os.link

        def competing_writer(source, target):
            path.write_bytes(b"other writer")
            link(source, target)

        with mock.patch.object(profiles.os, "link", side_effect=competing_writer), self.assertRaises(PhoneIOError):
            profiles.write_profile(profiles.relay(endpoint="https://relay.example"), path)
        self.assertEqual(path.read_bytes(), b"other writer")
        self.assertEqual(list(self.folder.iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
