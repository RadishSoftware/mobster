"""MCP with several devices (ship 2 SPEC §3): list_devices, device on verify_start and verify, and the phone tools
driving a device directly. Fakes only (test_mcp_tools' harness): no USB iPhone, simulator or WebDriverAgent."""

import unittest
from unittest.mock import patch

from mobile_agent.devices import record
from mobile_agent.device_targets import PinnedSimulator, WdaDevice
from mobile_agent.tests.test_mcp_tools import FakeRun, ToolsHarness, W, general_tree, tap_general

PHONE = "00008020-000A1B2C3D4E5F60"
SIM = "6F1C0E52-0000-4000-8000-00000000000A"


def records():
    return [record(id=PHONE, kind="usb", name="Work iPhone", udid=PHONE, model="iPhone11,8", model_name="iPhone XR",
                   ios="18.6", state="ready", wda_url_value="http://127.0.0.1:8101",
                   mjpeg_url="http://127.0.0.1:9101"),
            record(id=SIM, kind="simulator", name="Mobster · iPhone 17 Pro · iOS 26.4", udid=SIM,
                   model="iPhone 17 Pro", ios="26.4", state="ready", wda_url_value="http://127.0.0.1:8310"),
            record(id="AAAA0000-0000-4000-8000-000000000001", kind="usb", name="Drawer iPhone", state="disconnected",
                   reason="It isn't connected. Plug it in with a cable and unlock it.", wda_url_value="u")]


class FakeDeviceRun(FakeRun):
    """direct.DeviceRun's interface over FakeRun's screen and driver."""

    opened = []

    def __init__(self, record, *, runs_dir, **options):
        super().__init__(None, mode="direct", runs_dir=runs_dir, on_action=tap_general, **options)
        self.record, self.device_id, self.device_name = record, record["id"], record["name"]
        self.manager, self.target = None, None
        self.closed = []
        self.launched, self.homes = [], 0
        FakeDeviceRun.opened.append(self)

    def launch_app(self, bundle_id):
        self.launched.append(bundle_id)

    def close(self, reason):
        self.closed.append(reason)
        self.lease.release()
        return {"schema": "mobster.device/1", "run_id": self.run_id, "device": self.device_id, "status": "closed",
                "summary": reason, "steps": list(self.steps), "run_dir": str(self.run_dir)}


class DeviceToolTests(ToolsHarness):
    def make(self, **options):
        FakeDeviceRun.opened = []
        options.setdefault("devices", records)
        options.setdefault("device_run", FakeDeviceRun)
        return super().make(**options)

    def test_list_devices_names_each_device_its_state_and_open_run(self):
        self.make()
        result = self.call("list_devices")
        self.assertFalse(result.is_error)
        rows = result.structured["devices"]
        self.assertEqual([(row["id"], row["kind"], row["state"], row["run_id"]) for row in rows],
                         [(PHONE, "usb", "ready", None), (SIM, "simulator", "ready", None),
                          ("AAAA0000-0000-4000-8000-000000000001", "usb", "disconnected", None)])
        self.assertIn("Work iPhone  [iPhone (USB), iPhone XR, iOS 18.6]  ready", result.text)
        self.assertNotIn("wdaUrl", rows[0])
        screen = self.call("screen", device="Work iPhone")
        listed = {row["id"]: row["run_id"] for row in self.call("list_devices").structured["devices"]}
        self.assertEqual(listed[PHONE], screen.structured["run_id"])

    def test_phone_tools_drive_a_device_directly_with_device_alone(self):
        self.make()
        screen = self.call("screen", device="Work iPhone")
        self.assertFalse(screen.is_error, screen.text)
        run = FakeDeviceRun.opened[0]
        self.assertEqual(run.device_id, PHONE)
        self.assertIn("General", screen.text)
        tapped = self.call("tap", device=PHONE, target={"label": "General"})
        self.assertFalse(tapped.is_error, tapped.text)
        self.assertIn(("execute", "TAP", f"{W}/XCUIElementTypeButton[1]"), run.driver.calls)
        self.assertEqual(len(FakeDeviceRun.opened), 1)  # one session per device
        by_run = self.call("screen", run_id=screen.structured["run_id"])
        self.assertFalse(by_run.is_error)
        self.assertEqual(run.tree.fingerprint(), general_tree().fingerprint())
        self.assertEqual(self.call("status").structured["devices_open"],
                         [{"run_id": run.run_id, "device": PHONE, "name": "Work iPhone"}])

    def test_launch_app_and_home_on_a_device(self):
        self.make()
        self.call("screen", device=PHONE)
        run = FakeDeviceRun.opened[0]
        launched = self.call("launch_app", device=PHONE, bundle_id="com.apple.Preferences")
        self.assertFalse(launched.is_error, launched.text)
        self.assertEqual(run.launched, ["com.apple.Preferences"])
        home = self.call("home", device=PHONE)
        self.assertFalse(home.is_error, home.text)
        self.assertIn(("POST", "/wda/pressButton", {"name": "home"}), run.driver.calls)
        relaunch = self.call("relaunch", device=PHONE)
        self.assertTrue(relaunch.is_error)
        self.assertIn("launch_app", relaunch.text)

    def test_stop_releases_the_device_and_the_next_call_opens_it_again(self):
        self.make()
        first = self.call("screen", device=PHONE).structured["run_id"]
        stopped = self.call("stop", device="Work iPhone")
        self.assertFalse(stopped.is_error)
        self.assertEqual(FakeDeviceRun.opened[0].closed, ["Stopped by the agent."])
        self.assertEqual(FakeDeviceRun.opened[0].lease.released, 1)
        closed = self.call("screen", run_id=first)
        self.assertTrue(closed.is_error)
        self.assertIn("is closed", closed.text)
        again = self.call("screen", device=PHONE)
        self.assertNotEqual(again.structured["run_id"], first)
        self.assertEqual(len(FakeDeviceRun.opened), 2)

    def test_an_idle_device_is_released_after_90_seconds_and_opens_again(self):
        # Its lease keeps the Mac app, `mobster run` and scheduled tasks off the phone: held only while in use.
        self.make()
        self.call("screen", device=PHONE)
        self.clock.now += 60
        self.tools.reap()
        self.assertEqual(FakeDeviceRun.opened[0].closed, [])
        self.clock.now += 31
        self.tools.reap()
        self.assertEqual(FakeDeviceRun.opened[0].closed, [
            "The device was idle for 90 seconds, so Mobster released it for the Mobster app and other commands. "
            "The next call on it opens it again."])
        self.assertEqual(FakeDeviceRun.opened[0].lease.released, 1)
        again = self.call("screen", device="Work iPhone")
        self.assertFalse(again.is_error, again.text)
        self.assertEqual(len(FakeDeviceRun.opened), 2)

    def test_calls_on_an_open_device_never_read_the_device_list_again(self):
        # Each read lists USB phones and simulators: an open session is found by its id, UDID or name instead.
        reads = []

        def source():
            reads.append(1)
            return records()
        self.make(devices=source)
        self.call("screen", device=PHONE)
        before = len(reads)
        real = __import__("time").monotonic
        offset = [0.0]
        with patch("mobile_agent.mcp_server.tools.time.monotonic", lambda: real() + offset[0]):
            for name in (PHONE, PHONE.lower(), "work iphone"):
                offset[0] += 40.0  # a model turn longer than any cache
                tapped = self.call("tap", device=name, target={"label": "General"})
                self.assertFalse(tapped.is_error, tapped.text)
        self.assertEqual(len(reads) - before, 0)
        self.assertEqual(len(FakeDeviceRun.opened), 1)

    def test_naming_a_device_reads_the_list_without_probing_and_list_devices_probes(self):
        import tempfile
        from pathlib import Path
        from mobile_agent.mcp_server.tools import ToolSet
        calls = []
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        with patch("mobile_agent.devices.discover", lambda **options: calls.append(options) or records()):
            tools = ToolSet(runs_dir=Path(folder.name), keyless=True, device_run=FakeDeviceRun)
            self.assertEqual(tools._named_device("Work iPhone")["id"], PHONE)
            self.assertEqual(calls, [{"probe": False}])
            tools._device_records(fresh=True)
            self.assertEqual(calls[-1], {"probe": True})

    def test_a_device_missing_from_the_cached_list_is_looked_up_again(self):
        listed = [records()[1:]]
        self.make(devices=lambda: listed[0])
        self.tools._device_records()
        self.tools._devices_cache = (self.tools._devices_cache[0] - 5, self.tools._devices_cache[1])
        listed[0] = records()  # the Work iPhone plugged in since
        result = self.call("screen", device="Work iPhone")
        self.assertFalse(result.is_error, result.text)

    def test_a_device_that_is_not_ready_says_why(self):
        self.make()
        result = self.call("screen", device="Drawer iPhone")
        self.assertTrue(result.is_error)
        self.assertEqual(result.text, "“Drawer iPhone” isn't ready: It isn't connected. Plug it in with a cable and "
                                      "unlock it.")
        unknown = self.call("tap", device="Nope", ref="e1")
        self.assertTrue(unknown.is_error)
        self.assertIn("Call list_devices", unknown.text)
        self.assertEqual(FakeDeviceRun.opened, [])

    def test_the_mcp_device_flag_is_the_default_device(self):
        self.make(device="Work iPhone")
        result = self.call("screen")
        self.assertFalse(result.is_error, result.text)
        self.assertEqual(FakeDeviceRun.opened[0].device_id, PHONE)

    def test_a_run_and_a_device_that_disagree(self):
        self.make()
        run_id = self.call("verify_start", bundle_id="com.example.app", steps=[], expect=[]).structured["run_id"]
        result = self.call("tap", run_id=run_id, device=PHONE, ref="e1")
        self.assertTrue(result.is_error)
        self.assertIn("isn't on Work iPhone", result.text)


class AllowDeviceTests(ToolsHarness):
    """`mobster mcp --allow-device NAME`: a client approves a tool, not a device, so the server keeps to the real
    devices the user named. Simulators are always allowed; with no flag every device is, as before."""

    def make(self, **options):
        FakeDeviceRun.opened = []
        options.setdefault("devices", records)
        options.setdefault("device_run", FakeDeviceRun)
        return super().make(**options)

    def test_a_real_device_not_named_is_listed_not_allowed_and_refused(self):
        self.make(allow_devices=["Drawer iPhone"])
        rows = {row["id"]: row for row in self.call("list_devices").structured["devices"]}
        self.assertEqual(rows[PHONE]["state"], "not_allowed")
        self.assertEqual(rows[SIM]["state"], "ready")
        self.assertEqual(rows["AAAA0000-0000-4000-8000-000000000001"]["state"], "disconnected")
        for tool, arguments in (("screen", {}), ("tap", {"target": {"label": "General"}}),
                                ("use_code", {"target": {"label": "Code"}}), ("read_notifications", {}),
                                ("set_clipboard", {"text": "hello"}), ("install_app", {"path": "/tmp/App.app"}),
                                ("unlock_status", {}), ("unlock", {})):
            with self.subTest(tool=tool):
                refused = self.call(tool, device="Work iPhone", **arguments)
                self.assertTrue(refused.is_error)
                self.assertIn("--allow-device", refused.text)
        check = self.call("verify_start", bundle_id="com.example.app", steps=[], expect=[], device=PHONE)
        self.assertTrue(check.is_error)
        self.assertIn("--allow-device", check.text)
        self.assertEqual(FakeDeviceRun.opened, [])
        self.assertFalse(self.call("screen", device=SIM).is_error)

    def test_a_named_device_and_the_default_device_are_allowed(self):
        for options in ({"allow_devices": ["work iphone"]}, {"allow_devices": [PHONE]},
                        {"allow_devices": ["Drawer iPhone"], "device": "Work iPhone"}, {}):
            with self.subTest(**{key: str(value) for key, value in options.items()}):
                self.make(**options)
                screen = self.call("screen", device="Work iPhone")
                self.assertFalse(screen.is_error, screen.text)
                self.tools.shutdown("next case", 0.1)


class DeviceRunFolderTests(ToolsHarness):
    """A real iPhone's frames show its owner's screen: they go to Mobster's data folder, never the repository."""

    def make(self, **options):
        FakeDeviceRun.opened = []
        options.setdefault("devices", records)
        options.setdefault("device_run", FakeDeviceRun)
        return super().make(**options)

    def test_a_real_device_driven_directly_keeps_its_frames_in_the_device_folder(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as data:
            self.make(device_runs_dir=Path(data) / "runs")
            self.assertFalse(self.call("screen", device="Work iPhone").is_error)
            self.assertFalse(self.call("screen", device=SIM).is_error)
            phone, sim = FakeDeviceRun.opened
            self.assertTrue(phone.run_dir.is_relative_to(Path(data) / "runs"))
            self.assertTrue(sim.run_dir.is_relative_to(self.tools.runs_dir))
            self.tools.shutdown("the test ended", 0.1)

    def test_the_real_device_runs_folder(self):
        import tempfile
        from pathlib import Path
        from mobile_agent.mcp_server import cli
        with tempfile.TemporaryDirectory() as folder:
            data = {"MOBSTER_DATA_DIR": str(Path(folder) / "data")}
            self.assertEqual(cli.device_runs_dir(None, env=data), cli.dev_data_dir(data) / "runs")
            self.assertEqual(cli.device_runs_dir("/abs/out", env=data), Path("/abs/out"))
            self.assertEqual(cli.device_runs_dir(None, env={**data, "MOBSTER_RUNS_DIR": "/env/runs"}),
                             Path("/env/runs"))


class DirectRunFolderTests(unittest.TestCase):
    def test_a_direct_run_writes_the_gitignore_and_a_private_folder(self):
        import tempfile
        from pathlib import Path
        from mobile_agent.mcp_server.direct import DeviceRun
        with tempfile.TemporaryDirectory() as folder:
            runs = Path(folder) / ".mobster" / "runs"
            run = DeviceRun(records()[0], runs_dir=runs, manager=object())
            self.assertEqual((runs.parent / ".gitignore").read_text(), "runs/\nbuild/\ncache/\ntest-results/\n")
            self.assertEqual(run.run_dir.stat().st_mode & 0o777, 0o700)
            self.assertTrue((run.run_dir / "frames").is_dir())


class CheckOnDeviceTests(ToolsHarness):
    def make(self, **options):
        options.setdefault("devices", records)
        options.setdefault("device_run", FakeDeviceRun)
        return super().make(**options)

    def test_a_check_on_a_listed_simulator_runs_on_that_simulator(self):
        self.make()
        result = self.call("verify_start", bundle_id="com.example.app", steps=[], expect=[],
                           device="Mobster · iPhone 17 Pro · iOS 26.4")
        self.assertFalse(result.is_error, result.text)
        run = self.api.runs[-1]
        self.assertIsInstance(run.manager, PinnedSimulator)
        self.assertEqual((run.manager.udid, run.manager._manager), (SIM, self.manager))
        self.assertEqual(run.device_id, SIM)
        listed = {row["id"]: row["run_id"] for row in self.call("list_devices").structured["devices"]}
        self.assertEqual(listed[SIM], run.run_id)
        busy = self.call("screen", device=SIM)
        self.assertTrue(busy.is_error)
        self.assertIn(f"Run {run.run_id} is checking an app", busy.text)

    def test_a_check_on_a_usb_iphone_runs_an_app_already_on_it(self):
        self.make()
        refused = self.call("verify_start", app_path="/tmp/Example.app", steps=[], expect=[], device=PHONE)
        self.assertTrue(refused.is_error)
        self.assertEqual(refused.text, "“Work iPhone” runs apps already on it: pass bundle_id, not app_path.")
        cleared = self.call("verify_start", bundle_id="com.example.app", steps=[], expect=[], device=PHONE,
                            reset="data")
        self.assertTrue(cleared.is_error)
        self.assertIn("never clears an app's data", cleared.text)
        result = self.call("verify_start", bundle_id="com.example.app", steps=[], expect=[], device="work iphone")
        self.assertFalse(result.is_error, result.text)
        run = self.api.runs[-1]
        self.assertIsInstance(run.manager, WdaDevice)
        self.assertEqual((run.manager.url, run.check.reset), ("http://127.0.0.1:8101", "none"))

    def test_any_other_device_is_a_simulator_device_type_as_before(self):
        self.make()
        result = self.call("verify_start", bundle_id="com.example.app", steps=[], expect=[], device="iPhone 16 Pro")
        self.assertFalse(result.is_error, result.text)
        run = self.api.runs[-1]
        self.assertIs(run.manager, self.manager)
        self.assertEqual(run.check.device, "iPhone 16 Pro")


class WdaDeviceTests(unittest.TestCase):
    """The WDA-only manager a check on a USB iPhone uses."""

    def test_it_never_installs_or_clears_and_launches_through_wda(self):
        from mobile_agent.sim.api import SimError
        calls = []

        class Response:
            def __init__(self, value):
                self.value = value

            def __enter__(self):
                import io
                import json
                return io.BytesIO(json.dumps(self.value).encode())

            def __exit__(self, *exc):
                return False

        def opener(request, timeout=None):
            calls.append((request.get_method(), request.full_url))
            if request.full_url.endswith("/status"):
                return Response({"value": {"ready": True}, "sessionId": "s1"})
            return Response({"value": None})

        device = WdaDevice(records()[0], lease=lambda url: type("Lease", (), {"close": lambda self: None})(),
                           opener=opener)
        lease = device.acquire()
        self.assertEqual((lease.target.wda_url, lease.target.name), ("http://127.0.0.1:8101", "Work iPhone"))
        with self.assertRaises(SimError) as caught:
            device.app_info("/tmp/Example.app")
        self.assertEqual(caught.exception.kind, "install")
        with self.assertRaises(SimError):
            device.reset(lease.target, "com.example.app", "data")
        device.reset(lease.target, "com.example.app", "none")
        device.launch(lease.target, "com.example.app", ["-Flag"], {"KEY": "1"})
        self.assertIn(("POST", "http://127.0.0.1:8101/session/s1/wda/apps/launch"), calls)
        self.assertIsNone(device.restarter(lease.target))

    def test_a_device_with_no_runner_answering_is_refused_and_its_lease_freed(self):
        from mobile_agent.sim.api import SimError
        released = []

        def opener(request, timeout=None):
            raise OSError("connection refused")
        device = WdaDevice(records()[0], lease=lambda url: type("Lease", (), {
            "close": lambda self: released.append(url)})(), opener=opener)
        with self.assertRaises(SimError) as caught:
            device.acquire()
        self.assertEqual(caught.exception.kind, "wda")
        self.assertEqual(released, ["http://127.0.0.1:8101"])


if __name__ == "__main__":
    unittest.main()
