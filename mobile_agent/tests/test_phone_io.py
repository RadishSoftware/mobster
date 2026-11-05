import base64
import contextlib
import io
import json
import os
import plistlib
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

from mobile_agent.phone_io import PhoneIOError, checked_request, choose_device, clipboard, install, wda

PHONE = {"id": "00008020-000A1B2C3D4E5F60", "kind": "usb", "name": "Work iPhone", "udid": "00008020-000A1B2C3D4E5F60",
         "state": "ready", "primary": True, "wdaUrl": "http://127.0.0.1:8101"}
SIM = {"id": "6F1C0E52-0000-4000-8000-00000000000A", "kind": "simulator", "name": "Mobster · iPhone 17 Pro",
       "udid": "6F1C0E52-0000-4000-8000-00000000000A", "state": "ready", "wdaUrl": "http://127.0.0.1:8310"}
ADDRESS = {"id": "wda-9100", "kind": "wda", "name": "Lab phone", "state": "ready", "wdaUrl": "http://10.0.0.5:8100"}


class FakeWDA:
    def __init__(self, clipboard_text=""):
        self.requests, self.pasteboard = [], base64.b64encode(clipboard_text.encode()).decode()

    def __call__(self, request, timeout=None):
        path = request.full_url.split("8101", 1)[1]
        body = json.loads(request.data) if request.data else None
        self.requests.append((request.get_method(), path, body))
        if path == "/status":
            value = {"sessionId": "S1", "value": {"ready": True}}
        elif path.endswith("/wda/setPasteboard"):
            self.pasteboard = body["content"]
            value = {"value": None, "sessionId": "S1"}
        elif path.endswith("/wda/getPasteboard"):
            value = {"value": self.pasteboard, "sessionId": "S1"}
        else:
            value = {"value": {"error": "unknown command"}}
        return contextlib.nullcontext(io.BytesIO(json.dumps(value).encode()))


class Lease:
    held = 0

    def __init__(self, url):
        Lease.held += 1

    def close(self):
        Lease.held -= 1


class ClipboardTests(unittest.TestCase):
    def test_set_sends_base64_text_through_set_pasteboard_holding_the_lease(self):
        fake = FakeWDA()
        with wda(PHONE, opener=fake, lease=Lease) as request:
            self.assertEqual(Lease.held, 1)
            self.assertEqual(clipboard.set_text(request, "héllo 👋"), 7)
        self.assertEqual(Lease.held, 0)
        method, path, body = fake.requests[-1]
        self.assertEqual((method, path), ("POST", "/session/S1/wda/setPasteboard"))
        self.assertEqual(body, {"content": base64.b64encode("héllo 👋".encode()).decode(), "contentType": "plaintext"})

    def test_the_64_kb_cap_and_text_only(self):
        self.assertTrue(clipboard.encode("x" * clipboard.LIMIT))
        for bad in ("x" * (clipboard.LIMIT + 1), "é" * (clipboard.LIMIT // 2 + 1), "", "a\x00b"):
            with self.subTest(length=len(bad)), self.assertRaises(PhoneIOError) as caught:
                clipboard.encode(bad)
            self.assertEqual(caught.exception.code, "usage")
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / "x.bin"
            binary.write_bytes(b"\xff\xfe\x00")
            with self.assertRaises(PhoneIOError):
                clipboard.read_text_file(binary)
            big = Path(folder) / "big.txt"
            big.write_text("y" * (clipboard.LIMIT + 1))
            with self.assertRaises(PhoneIOError):
                clipboard.read_text_file(big)

    def test_a_refusal_from_wda_is_an_error(self):
        fake = FakeWDA()
        with wda(PHONE, opener=fake, lease=Lease) as request, self.assertRaises(PhoneIOError):
            request("POST", "/wda/nope", {})

    def test_the_runners_own_answer_with_an_error_key_is_data_not_a_refusal(self):
        for value in ({"ok": False, "error": "offset", "bytes": 8}, {"saved": False, "status": 3, "error": "denied"}):
            with self.subTest(value=value):
                request = checked_request(lambda method, path, body, timeout, value=value: {"value": value})
                self.assertEqual(request("POST", "/mobster/photos/chunk", {}), value)

    def test_the_bare_error_envelope_and_a_nonzero_status_are_refusals(self):
        for answer in ({"value": {"error": "no such element", "message": "x", "traceback": ""}},
                       {"value": None, "status": 13}):
            with self.subTest(answer=answer):
                request = checked_request(lambda method, path, body, timeout, answer=answer: answer)
                with self.assertRaises(PhoneIOError) as caught:
                    request("GET", "/x")
                self.assertEqual(caught.exception.code, "failed")

    def test_errors_map_to_codes_whatever_the_sender_raises(self):
        def raising(error):
            def send(method, path, body, timeout):
                raise error
            return checked_request(send)
        http = lambda code: urllib.error.HTTPError("http://x", code, "m", {}, None)  # noqa: E731
        for error, code in ((http(404), "unknown_route"), (http(500), "failed"), (OSError("down"), "failed"),
                            (RuntimeError("boom"), "failed")):
            with self.subTest(error=error), self.assertRaises(PhoneIOError) as caught:
                raising(error)("GET", "/x")
            self.assertEqual(caught.exception.code, code)

    def test_choose_device(self):
        self.assertIs(choose_device(None, [PHONE, SIM]), PHONE)
        self.assertIs(choose_device("Mobster · iPhone 17 Pro", [PHONE, SIM]), SIM)
        with self.assertRaises(PhoneIOError) as caught:
            choose_device("Kitchen iPad", [PHONE])
        self.assertEqual(caught.exception.code, "no_device")
        with self.assertRaises(PhoneIOError) as caught:
            choose_device(None, [])
        self.assertEqual(caught.exception.code, "no_device")


def app_bundle(folder, name="Firefox.app", bundle="org.mozilla.ios.Firefox"):
    app = Path(folder) / name
    app.mkdir(parents=True)
    with open(app / "Info.plist", "wb") as handle:
        plistlib.dump({"CFBundleIdentifier": bundle}, handle)
    return app


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.app = app_bundle(self.folder.name)

    def runner(self, code=0, output="", bundle=None):
        calls = []

        def run(argv):
            calls.append(argv)
            if bundle and "--json-output" in argv:
                Path(argv[argv.index("--json-output") + 1]).write_text(json.dumps(
                    {"result": {"installedApplications": [{"bundleID": bundle}]}}))
            return code, output
        return run, calls

    def test_a_tool_that_hangs_silently_is_stopped_at_the_time_limit(self):
        import sys
        import time
        started = time.monotonic()
        with self.assertRaises(PhoneIOError) as caught:
            install.run_streaming([sys.executable, "-c", "import time; time.sleep(30)"], timeout=.5)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(str(caught.exception), "The install took over 0.5 seconds; Mobster stopped it.")
        lines = []
        code, output = install.run_streaming([sys.executable, "-c", "print('Installing'); print('done')"],
                                             progress=lines.append, timeout=20)
        self.assertEqual((code, output, lines), (0, "Installing\ndone\n", ["Installing", "done"]))

    def test_real_install_refuses_when_devices_are_disabled(self):
        with mock.patch.dict(os.environ, {"MOBSTER_NO_DEVICES": "1"}), \
                mock.patch.object(install.subprocess, "Popen") as start:
            with self.assertRaises(RuntimeError):
                install.install(PHONE, self.app)
        start.assert_not_called()

    def test_the_arguments_are_a_list_for_devicectl_or_simctl(self):
        run, calls = self.runner(bundle="org.mozilla.ios.Firefox")
        result = install.install(PHONE, str(self.app), runner=run)
        argv = calls[0]
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[:7], ["xcrun", "devicectl", "device", "install", "app", "--device", PHONE["udid"]])
        self.assertEqual(argv[7], str(self.app.resolve()))
        self.assertEqual(argv[8], "--json-output")
        self.assertEqual(result["bundle_id"], "org.mozilla.ios.Firefox")
        self.assertEqual(set(result), {"ok", "device", "name", "path", "bundle_id", "seconds"})
        run, calls = self.runner()
        result = install.install(SIM, str(self.app), runner=run)
        self.assertEqual(calls[0], ["xcrun", "simctl", "install", SIM["udid"], str(self.app.resolve())])
        self.assertEqual(result["bundle_id"], "org.mozilla.ios.Firefox")
        with self.assertRaises(PhoneIOError):
            install.install(ADDRESS, str(self.app), runner=run)

    def test_a_phone_over_wifi_installs_through_devicectl_once(self):
        wifi = {**PHONE, "transport": "wifi", "state": "ready"}
        run, calls = self.runner(bundle="org.mozilla.ios.Firefox")
        result = install.install(wifi, str(self.app), runner=run)
        self.assertEqual(calls[0][:7], ["xcrun", "devicectl", "device", "install", "app", "--device", PHONE["udid"]])
        self.assertEqual((len(calls), result["bundle_id"]), (1, "org.mozilla.ios.Firefox"))
        run, calls = self.runner(1, "ERROR: The device is not connected. (CoreDeviceError 4000)")
        with self.assertRaises(PhoneIOError) as lost:
            install.install(wifi, str(self.app), runner=run)
        self.assertEqual((lost.exception.code, len(calls)), ("no_device", 1))

    def test_the_path_is_checked(self):
        run, calls = self.runner()
        empty = Path(self.folder.name) / "Empty.app"
        empty.mkdir()
        (Path(self.folder.name) / "notes.txt").write_text("hi")
        for bad in ("https://example.com/Firefox.ipa", str(Path(self.folder.name) / "Missing.app"), str(empty),
                    str(Path(self.folder.name) / "notes.txt"), ""):
            with self.subTest(path=bad), self.assertRaises(PhoneIOError) as caught:
                install.install(PHONE, bad, runner=run)
            self.assertEqual(caught.exception.code, "usage")
        self.assertEqual(calls, [])

    def test_an_ipa_is_unpacked_and_its_app_installed(self):
        ipa = Path(self.folder.name) / "Firefox.ipa"
        with zipfile.ZipFile(ipa, "w") as archive:
            archive.writestr("Payload/Firefox.app/Info.plist", plistlib.dumps({"CFBundleIdentifier": "com.x.y"}))
        run, calls = self.runner()
        result = install.install(PHONE, str(ipa), runner=run)
        self.assertTrue(calls[0][7].endswith("Payload/Firefox.app"))
        self.assertEqual(result["bundle_id"], "com.x.y")
        evil = Path(self.folder.name) / "Evil.ipa"
        with zipfile.ZipFile(evil, "w") as archive:
            archive.writestr("../../escape.txt", "x")
            archive.writestr("Payload/Evil.app/Info.plist", b"")
        with self.assertRaises(PhoneIOError):
            install.install(PHONE, str(evil), runner=run)

    def test_failures_are_explained(self):
        for output, code in (("ERROR: Failed to install the app on the device. (0xe8008015) A valid provisioning "
                              "profile for this executable was not found.", "not_signed"),
                             ("ERROR: The operation couldn't be completed. Developer Mode is disabled.",
                              "developer_mode"),
                             ("ERROR: Unable to install while the device is locked.", "locked"),
                             ("ERROR: No devices matched the provided criteria.", "no_device"),
                             ("ERROR: something new", "failed")):
            with self.subTest(code=code):
                run, _calls = self.runner(1, output)
                with self.assertRaises(PhoneIOError) as caught:
                    install.install(PHONE, str(self.app), runner=run)
                self.assertEqual(caught.exception.code, code)
                if code != "failed":
                    self.assertTrue(caught.exception.fix)


class CliTests(unittest.TestCase):
    def run_cli(self, argv):
        from mobile_agent import __main__ as mobster
        from mobile_agent import harness_api
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(harness_api.hooks(), "cli", []), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.dict(os.environ, {"MOBSTER_ENV_FILE": "/nonexistent/mobster-test.env"}):
            code = mobster.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_install_json_shape_and_exit_codes(self):
        with tempfile.TemporaryDirectory() as folder:
            app = app_bundle(folder)
            done = {"ok": True, "device": PHONE["id"], "name": PHONE["name"], "path": str(app), "bundle_id": "b",
                    "seconds": 1.0}
            with mock.patch("mobile_agent.phone_io.cli.choose_device", return_value=PHONE), \
                    mock.patch("mobile_agent.phone_io.install.install", return_value=done):
                code, out, _err = self.run_cli(["phone", "install", str(app), "--json"])
            self.assertEqual((code, json.loads(out)), (0, done))
            with mock.patch("mobile_agent.phone_io.cli.choose_device", return_value=PHONE), \
                    mock.patch("mobile_agent.phone_io.install.install",
                               side_effect=PhoneIOError("The build isn't signed for this iPhone.", "not_signed",
                                                        "Sign it.")):
                code, out, _err = self.run_cli(["phone", "install", str(app), "--json"])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(out), {"ok": False, "error": {
                "code": "not_signed", "message": "The build isn't signed for this iPhone.", "fix": "Sign it."}})
            with mock.patch("mobile_agent.phone_io.cli.choose_device",
                            side_effect=PhoneIOError("No device is named “x”.", "no_device")):
                code, _out, err = self.run_cli(["phone", "install", str(app)])
            self.assertEqual(code, 3)
            self.assertIn("No device is named", err)

    def test_install_refuses_a_device_a_run_holds_before_anything_is_sent(self):
        from mobile_agent.journal import Lease
        held = Lease.device(PHONE["wdaUrl"], wait=0)
        try:
            with mock.patch("mobile_agent.phone_io.cli.choose_device", return_value=PHONE), \
                    mock.patch("mobile_agent.phone_io.install.install") as install_app:
                code, output, _ = self.run_cli(["phone", "install", "unused.app", "--json"])
            self.assertEqual(code, 1)
            self.assertFalse(json.loads(output)["ok"])
            self.assertIn("Another Mobster process is using", output)
            install_app.assert_not_called()
        finally:
            held.close()

    def test_install_holds_the_device_lease_and_releases_it_on_every_outcome(self):
        from mobile_agent.journal import Lease, LeaseHeld
        for fail in (False, True):
            def installer(*args, **kwargs):
                with self.assertRaises(LeaseHeld):
                    Lease.device(PHONE["wdaUrl"], wait=0)
                if fail:
                    raise PhoneIOError("Install failed.")
                return {"ok": True, "bundle_id": "com.example.app", "seconds": 1}

            with self.subTest(fail=fail), mock.patch("mobile_agent.phone_io.cli.choose_device", return_value=PHONE), \
                    mock.patch("mobile_agent.phone_io.install.install", side_effect=installer):
                code, _, _ = self.run_cli(["phone", "install", "unused.app", "--json"])
            self.assertEqual(code, 1 if fail else 0)
            Lease.device(PHONE["wdaUrl"], wait=0).close()

    def test_record_refuses_a_bad_length_and_a_phone_that_is_not_on_a_cable(self):
        usb = {**PHONE, "kind": "usb", "udid": "TEST-PHONE"}
        for argv, record, expected, code in (
                (["--seconds", "0"], usb, 2, "usage"), (["--seconds", "301"], usb, 2, "usage"),
                ([], {**PHONE, "kind": "wifi", "udid": None}, 3, "needs_cable"),
                ([], {**usb, "udid": None}, 3, "needs_cable")):
            with self.subTest(argv=argv, kind=record.get("kind")), \
                    mock.patch("mobile_agent.phone_io.cli.choose_device", return_value=record), \
                    mock.patch("mobile_agent.phone_io.recording.record_video") as record_video:
                status, output, _ = self.run_cli(["phone", "record", "--output", "x.mov", "--json", *argv])
                self.assertEqual(status, expected)
                self.assertEqual(json.loads(output)["error"]["code"], code)
                record_video.assert_not_called()

    def test_record_saves_through_the_usb_udid(self):
        usb = {**PHONE, "kind": "usb", "udid": "TEST-PHONE"}
        answer = {"ok": True, "path": "/tmp/x.mov", "durationSeconds": 2.5}
        with mock.patch("mobile_agent.phone_io.cli.choose_device", return_value=usb), \
                mock.patch("mobile_agent.phone_io.recording.record_video", return_value=answer) as record_video:
            status, output, _ = self.run_cli(["phone", "record", "--output", "x.mov", "--seconds", "2"])
        self.assertEqual(status, 0)
        self.assertIn("Saved 2.5 seconds of video to /tmp/x.mov.", output)
        self.assertEqual(record_video.call_args.args, ("TEST-PHONE",))
        self.assertEqual(record_video.call_args.kwargs["seconds"], 2)

    def test_clipboard_set_and_get(self):
        fake = FakeWDA("from the phone")
        opened = mock.patch("mobile_agent.phone_io.wda", lambda record: wda(record, opener=fake, lease=Lease))
        with opened, mock.patch("mobile_agent.phone_io.cli.choose_device", return_value=PHONE):
            code, out, _err = self.run_cli(["phone", "clipboard", "set", "hello", "--json"])
            self.assertEqual((code, json.loads(out)), (0, {"ok": True, "device": PHONE["id"], "characters": 5}))
            code, out, _err = self.run_cli(["phone", "clipboard", "get"])
            self.assertEqual((code, out), (0, "hello\n"))
            code, _out, err = self.run_cli(["phone", "clipboard", "set"])
            self.assertEqual(code, 2)

    def pull_icloud(self, cloud, *extra):
        from mobile_agent.phone_io.transfer import Phone
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch("mobile_agent.phone_io.transfer.target", return_value=Phone(PHONE, icloud=cloud)):
            return self.run_cli(["phone", "photos", "pull", "--via", "icloud", "-o", folder, *extra])

    def test_a_pull_that_ends_unknown_is_a_failure_not_an_empty_camera_roll(self):
        class Timeout:
            def export_recent(self, folder, count=1):
                raise PhoneIOError("Photos on this Mac didn't answer in time.", "unknown", "Open Photos, then retry.")
        code, out, err = self.pull_icloud(Timeout())
        self.assertEqual((code, out), (1, ""))
        self.assertIn("didn't answer in time", err)
        self.assertIn("Open Photos", err)
        code, out, _err = self.pull_icloud(Timeout(), "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"]["code"], "unknown")

    def test_an_empty_camera_roll_still_exits_zero(self):
        class Empty:
            def export_recent(self, folder, count=1):
                return {"paths": []}
        code, out, _err = self.pull_icloud(Empty())
        self.assertEqual(code, 0)
        self.assertIn("Nothing in", out)


if __name__ == "__main__":
    unittest.main()
