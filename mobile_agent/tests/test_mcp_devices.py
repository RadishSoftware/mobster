import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent.device_targets import PinnedSimulator, WdaDevice
from mobile_agent.devices import record
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
        self.assertEqual(len(FakeDeviceRun.opened), 1)
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
        restart = self.call("launch_app", device=PHONE, restart=True)
        self.assertTrue(restart.is_error)
        self.assertIn("bundle_id", restart.text)
        for arguments in ({}, {"bundle_id": "com.apple.Preferences", "restart": True}):
            self.assertTrue(self.call("launch_app", device=PHONE, **arguments).is_error, arguments)

    def test_action_intent_is_durable_before_dispatch_and_survives_a_failed_screen_read(self):
        self.make()
        self.call("screen", device=SIM)
        run = FakeDeviceRun.opened[0]
        path = run.run_dir / "actions.jsonl"
        execute = run.driver.execute

        def dispatch(*args, **kwargs):
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([(r["event"], r["operation"]) for r in rows], [("action_started", "TAP")])
            return execute(*args, **kwargs)

        with patch.object(run.driver, "execute", side_effect=dispatch), \
                patch.object(self.tools, "_outline", side_effect=RuntimeError("post-dispatch read failed")):
            result = self.call("tap", device=SIM, target={"label": "General"})
        self.assertTrue(result.is_error)
        self.assertEqual(len([c for c in run.driver.calls if c[0] == "execute"]), 1)
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(rows[-1]["outcome"], "unknown")
        self.assertEqual(rows[0]["id"], rows[-1]["id"])

    def test_journal_sync_failure_refuses_dispatch(self):
        self.make()
        self.call("screen", device=SIM)
        run = FakeDeviceRun.opened[0]
        with patch("mobile_agent.journal.os.fsync", side_effect=OSError("disk full")):
            result = self.call("tap", device=SIM, target={"label": "General"})
        self.assertTrue(result.is_error)
        self.assertIn("journal could not be saved", result.text)
        self.assertFalse(any(c[0] == "execute" for c in run.driver.calls))
        self.assertEqual(run.steps, [])

    def test_an_unknown_foreground_app_refuses_reads_and_actions(self):
        self.make()
        self.call("screen", device=PHONE)
        run = FakeDeviceRun.opened[0]
        unknown = SimpleNamespace(bundle_id="")
        for observation in (unknown, OSError("read failed")):
            with self.subTest(observation=type(observation).__name__):
                calls_before, reads_before = len(run.driver.calls), run.reads
                options = {"side_effect": observation} if isinstance(observation,
                                                                     Exception) else {"return_value": observation}
                with patch.object(run.driver, "observe", **options):
                    result = self.call("tap", device=PHONE, target={"label": "General"})
                    screen = self.call("screen", device=PHONE)
                self.assertTrue(result.is_error)
                self.assertTrue(screen.is_error)
                self.assertIn("couldn't identify the app", result.text)
                self.assertFalse(any(c[0] == "execute" for c in run.driver.calls[calls_before:]))
                self.assertEqual(run.reads, reads_before)

    def test_verify_finish_and_save_check_refuse_a_device_run_and_leave_it_open(self):
        self.make()
        run_id = self.call("screen", device=PHONE).structured["run_id"]
        for tool, args in (("verify_finish", {}), ("save_check", {"name": "x"})):
            result = self.call(tool, run_id=run_id, **args)
            self.assertTrue(result.is_error, tool)
            self.assertIn("drives Work iPhone directly", result.text)
            self.assertIn(f"Call stop with device {PHONE}", result.text)
        self.assertEqual(FakeDeviceRun.opened[0].closed, [])
        self.assertFalse(self.call("screen", run_id=run_id).is_error)

    def test_an_idle_device_is_released_after_90_seconds_and_opens_again(self):
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
                                ("unlock_status", {}), ("unlock", {}), ("device_status", {})):
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
            with patch.dict("os.environ", data):
                self.assertEqual(cli.device_runs_dir(None, env=data), Path(data["MOBSTER_DATA_DIR"]) / "runs")
            self.assertEqual(cli.device_runs_dir("/abs/out", env=data), Path("/abs/out"))
            self.assertEqual(cli.device_runs_dir(None, env={**data, "MOBSTER_RUNS_DIR": "/env/runs"}),
                             Path("/env/runs"))


class DirectRunFolderTests(unittest.TestCase):
    def test_a_step_write_failure_is_reported_and_does_not_claim_a_saved_step(self):
        import tempfile

        from mobile_agent.journal import JournalError
        from mobile_agent.mcp_server.direct import DeviceRun
        with tempfile.TemporaryDirectory() as folder:
            run = DeviceRun(records()[1], runs_dir=folder, manager=object())
            with patch("mobile_agent.journal.os.fsync", side_effect=OSError("disk full")):
                with self.assertRaises(JournalError):
                    run.record_step(op="TAP", text="Tapped General")
            self.assertEqual(run.steps, [])


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


class WdaDeviceTests(unittest.TestCase):
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
            if request.full_url.endswith("/wda/apps/terminate"):
                return Response({"value": False})  # An app that is not running is still a successful response.
            return Response({"value": None})

        device = WdaDevice(records()[0], lease=lambda url: type("Lease", (), {"close": lambda self: None})(),
                           opener=opener)
        lease = device.acquire(cancelled=lambda: False)
        self.assertEqual((lease.target.wda_url, lease.target.name), ("http://127.0.0.1:8101", "Work iPhone"))
        with self.assertRaises(SimError) as caught:
            device.app_info("/tmp/Example.app")
        self.assertEqual(caught.exception.kind, "install")
        with self.assertRaises(SimError):
            device.reset(lease.target, "com.example.app", "data")
        device.reset(lease.target, "com.example.app", "none")
        calls.clear()
        device.launch(lease.target, "com.example.app", ["-Flag"], {"KEY": "1"})
        posts = [url for method, url in calls if method == "POST"]
        self.assertEqual(posts, ["http://127.0.0.1:8101/session/s1/wda/apps/terminate",
                                 "http://127.0.0.1:8101/session/s1/wda/apps/launch"])
        self.assertIsNone(device.restarter(lease.target))

    def test_unknown_termination_stops_launch_and_latches_the_action_journal(self):
        from mobile_agent.verify.actions import ActionError, ActionJournal

        calls, closed = [], []

        class Response:
            def __enter__(self):
                import io
                return io.BytesIO(b'{"value":{"ready":true},"sessionId":"s1"}')

            def __exit__(self, *exc):
                return False

        def opener(request, timeout=None):
            calls.append(request.full_url)
            if request.full_url.endswith("/wda/apps/terminate"):
                raise TimeoutError("the termination answer was lost")
            if request.full_url.endswith("/wda/apps/launch"):
                raise AssertionError("launch must not follow an uncertain termination")
            return Response()

        device = WdaDevice(records()[0], lease=lambda url: type("Lease", (), {
            "close": lambda self: closed.append(url)})(), opener=opener)
        held = device.acquire()
        try:
            with tempfile.TemporaryDirectory() as folder:
                journal = ActionJournal(folder)
                with self.assertRaises(TimeoutError):
                    with journal.action("LAUNCH"):
                        device.launch(held.target, "com.example.app")
                self.assertEqual(journal.klass, "unknown_outcome")
                with self.assertRaises(ActionError):
                    with journal.action("LAUNCH"):
                        pass
        finally:
            held.release()
        self.assertFalse(any(path.endswith("/wda/apps/launch") for path in calls))
        self.assertEqual(closed, ["http://127.0.0.1:8101"])

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

    def test_cancellation_before_or_just_after_the_lease_sends_no_probe(self):
        from mobile_agent.verify.actions import ActionNotDispatched

        taken, closed, requests = [], [], []

        def reserve(url):
            taken.append(url)
            return type("Lease", (), {"close": lambda self: closed.append(url)})()

        def opener(request, timeout=None):
            requests.append(request.full_url)
            raise AssertionError("cancelled acquisition must not probe")

        device = WdaDevice(records()[0], lease=reserve, opener=opener)
        with self.assertRaises(ActionNotDispatched) as before:
            device.acquire(cancelled=lambda: True)
        self.assertEqual(before.exception.kind, "stopped")
        self.assertEqual((taken, requests), ([], []))

        with self.assertRaises(ActionNotDispatched) as after:
            device.acquire(cancelled=lambda: bool(taken))
        self.assertEqual(after.exception.kind, "stopped")
        self.assertEqual((taken, closed), (["http://127.0.0.1:8101"], ["http://127.0.0.1:8101"]))
        self.assertEqual(requests, [])

    def test_cancellation_during_readiness_releases_lease_and_rejects_acquire_intent(self):
        import io
        import tempfile

        from mobile_agent.verify.actions import ActionJournal, ActionNotDispatched

        stopped, closed, requests = [False], [], []

        def opener(request, timeout=None):
            requests.append(request.full_url)
            stopped[0] = True
            return io.BytesIO(b'{"value":{"ready":true}}')

        device = WdaDevice(records()[0], lease=lambda url: type("Lease", (), {
            "close": lambda self: closed.append(url)})(), opener=opener)
        with tempfile.TemporaryDirectory() as run_dir:
            journal = ActionJournal(run_dir)
            with self.assertRaises(ActionNotDispatched) as refused:
                with journal.action("ACQUIRE"):
                    device.acquire(cancelled=lambda: stopped[0])
            self.assertEqual(refused.exception.kind, "stopped")
            self.assertIsNone(journal.klass)
            rows = [json.loads(line) for line in journal.path.read_text().splitlines()]
            self.assertEqual(rows[-1]["outcome"], "rejected")
        self.assertEqual(requests, ["http://127.0.0.1:8101/status"])
        self.assertEqual(closed, ["http://127.0.0.1:8101"])


if __name__ == "__main__":
    unittest.main()
