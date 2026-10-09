"""The server with several devices (ship 2 SPEC §3): the device list, one task per device and tasks on two devices
at once, per-device leases, approvals, control, history, setup and workflows. Fakes only: no USB iPhone,
simulator, WebDriverAgent, model or network."""

import io
import json
import os
from email.message import Message
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from mobile_agent import fleet as fleet_module
from mobile_agent.device_manager import DeviceManager
from mobile_agent.devices import DeviceStore
from mobile_agent.journal import Lease, LeaseHeld
from mobile_agent.server import APIError, Runtime, make_handler
from mobile_agent.workflows import Workflows

A_URL, B_URL = "http://127.0.0.1:8310", "http://127.0.0.1:8311"
SIM_A, SIM_B = "6F1C0E52-0000-4000-8000-00000000000A", "6F1C0E52-0000-4000-8000-00000000000B"
PRO = "00008130-001A2B3C4D5E6F70"
XR = "00008020-000A1B2C3D4E5F60"
SE = "00008030-000B2C3D4E5F6071"
READY = {"device": True, "ready": True, "health": None, "can_act": True, "driver": "wda", "screen_reading": False,
         "notes": []}
DOWN = {**READY, "ready": False}
SETTINGS = {"id": "settings", "name": "Settings", "bundleId": "com.apple.Preferences", "installed": True}
CLEAN = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""), "TYPESAFE_API_KEY": "jev-key-000000000000",
         "MOBSTER_ASK_BEFORE_ACTING": "1"}


def row(udid, url, name):
    return {"udid": udid, "name": name, "device_type": "iPhone 17 Pro", "runtime": "iOS 26.4", "state": "Booted",
            "wda_url": url, "mjpeg_url": url.replace(":83", ":93")}


class Harness(unittest.TestCase):
    """Admission only: the task worker does nothing (``Runtime.work`` is replaced)."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mobster-fleet-"))
        env = patch.dict(os.environ, dict(CLEAN), clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.sims = [row(SIM_A, A_URL, "Mobster · iPhone 17 Pro · iOS 26.4"),
                     row(SIM_B, B_URL, "Mobster · iPhone 17 Pro · iOS 26.4 · 2")]
        self.held_elsewhere = set()
        self.leases = []
        self.targets = {}

        def lease(identity):
            if identity.rstrip("/") in self.held_elsewhere:
                raise LeaseHeld("held by another process")
            self.leases.append(identity.rstrip("/"))
            return Lease(self.root / ("lease-" + identity.rstrip("/").rsplit(":", 1)[-1]))

        for item in (patch.object(Runtime, "work", lambda runtime, run: None),
                     patch("mobile_agent.server.Lease.device", side_effect=lease),
                     patch("mobile_agent.devices.simulator_rows", side_effect=lambda *a, **k: list(self.sims)),
                     patch("mobile_agent.devices.lease_free", return_value=True),
                     patch("mobile_agent.devices.static_specs", return_value=[]),
                     patch.object(fleet_module.Phone, "target_status",
                                  lambda phone: self.targets.get(phone.id, READY)),
                     patch("mobile_agent.wda_video.WdaVideo"), patch("mobile_agent.manual_control.ManualControl")):
            item.start()
            self.addCleanup(item.stop)
        self.runtimes = []
        self.addCleanup(self.close)

    def close(self):
        for runtime in self.runtimes:
            for run in runtime.runs.values():
                if run.lease:
                    run.lease.close()
                    run.lease = None
            runtime.close()

    def runtime(self, wda_url=A_URL, device=None, state=None, **config):
        state = state or self.root / f"r{len(self.runtimes)}"
        config = SimpleNamespace(wda_url=wda_url, session=None, enable_live=True, port=8765, device=device,
                                 state_db=str(state / "runs.sqlite3"), env_file=str(state / "agent.env"), **config)
        with patch("mobile_agent.server.WdaVideo"), patch("mobile_agent.server.ManualControl"):
            runtime = Runtime(config)
        runtime.target_status = Mock(return_value=dict(READY))
        runtime.apps = Mock(return_value=[dict(SETTINGS)])
        self.runtimes.append(runtime)
        return runtime

    def finish(self, runtime, run):
        run.finish({"event": "result", "status": "completed"})
        if run.lease:
            run.lease.close()
            run.lease = None
        with runtime.lock:
            runtime.active_runs.pop(run.id, None)

    def request(self, runtime, method, path, body=None):
        handler = object.__new__(make_handler(runtime))
        raw = json.dumps(body).encode() if body is not None else b""
        handler.headers = Message()
        handler.headers["Host"] = "127.0.0.1:8765"
        if body is not None:
            handler.headers["Content-Type"] = "application/json"
            handler.headers["Content-Length"] = str(len(raw))
        handler.command, handler.path = method, path
        handler.rfile, handler.wfile = io.BytesIO(raw), io.BytesIO()
        handler.connection = Mock()
        statuses, headers = [], {}
        handler.send_response = statuses.append
        handler.send_header = lambda name, value: headers.__setitem__(name, value)
        handler.end_headers = lambda: None
        getattr(handler, f"do_{method}")()
        data = handler.wfile.getvalue()
        return statuses[-1], (json.loads(data) if headers.get("Content-Type") == "application/json" else data)


class DeviceListTests(Harness):
    def test_every_device_with_its_state_primary_first(self):
        status, body = self.request(self.runtime(), "GET", "/api/devices")
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in body["devices"]], [SIM_A, SIM_B])
        first, second = body["devices"]
        self.assertEqual((first["kind"], first["state"], first["primary"], first["default"]),
                         ("simulator", "ready", True, True))
        self.assertEqual((second["primary"], second["default"], second["activeRunId"], second["approval"]),
                         (False, False, None, None))
        self.assertEqual(body["defaultDevice"], SIM_A)
        for item in body["devices"]:
            self.assertNotIn("wdaUrl", item)  # no address or port goes to the dashboard
            self.assertEqual(set(item) - {"wdaUrl", "mjpegUrl"}, {
                "id", "kind", "name", "udid", "model", "modelName", "ios", "state", "reason", "primary", "setUp",
                "default", "activeRunId", "approval", "lastUsedAt"})

    def test_one_device_by_id_or_name(self):
        runtime = self.runtime()
        status, body = self.request(runtime, "GET", f"/api/devices/{SIM_B}")
        self.assertEqual((status, body["device"]["id"]), (200, SIM_B))
        status, body = self.request(runtime, "GET", "/api/devices/Mobster%20%C2%B7%20iPhone%2017%20Pro%20%C2%B7%20iOS%2026.4%20%C2%B7%202")
        self.assertEqual((status, body["device"]["id"]), (200, SIM_B))
        status, body = self.request(runtime, "GET", "/api/devices/nope")
        self.assertEqual((status, body["code"]), (404, "device_not_found"))

    def test_one_phone_stays_simple(self):
        """A server on one WDA address: one device, and every request without a device runs there, as before."""
        self.sims = []
        runtime = self.runtime(wda_url="http://127.0.0.1:8203")
        _, body = self.request(runtime, "GET", "/api/devices")
        self.assertEqual([(item["id"], item["kind"], item["default"]) for item in body["devices"]],
                         [("wda-8203", "wda", True)])
        run = runtime.create("settings", "Open About", "live")
        self.assertEqual((run.device_id, run.phone), ("wda-8203", None))
        self.assertEqual(self.leases, ["http://127.0.0.1:8203"])
        self.assertEqual(self.request(runtime, "GET", "/api/devices")[1]["defaultDevice"], "wda-8203")
        self.assertEqual(runtime.status()["concurrency"], {"capacity": 1, "running": 1, "available": 0})
        with self.assertRaises(APIError) as busy:
            runtime.create("settings", "Open General", "live")
        self.assertEqual((busy.exception.code, str(busy.exception)),
                         ("run_active", "Every assigned phone is busy; stop a run or wait for one to finish"))


class AdmissionTests(Harness):
    def test_two_tasks_run_at_once_on_two_devices(self):
        runtime = self.runtime()
        on_b = runtime.create("settings", "Open About", "live", device=SIM_B)
        on_a = runtime.create("settings", "Open General", "live", device=SIM_A)
        self.assertEqual(set(runtime.active_runs.items()), {(on_b.id, SIM_B), (on_a.id, "@primary")})
        self.assertEqual((on_b.device_id, on_a.device_id), (SIM_B, SIM_A))
        self.assertEqual(self.leases, [B_URL, A_URL])  # each device's own lease
        _, body = self.request(runtime, "GET", "/api/devices")
        self.assertEqual({item["id"]: (item["state"], item["activeRunId"]) for item in body["devices"]},
                         {SIM_A: ("busy", on_a.id), SIM_B: ("busy", on_b.id)})
        self.assertEqual(on_b.public()["device"], SIM_B)
        self.assertEqual(on_b.public()["deviceName"], "Mobster · iPhone 17 Pro · iOS 26.4 · 2")

    def test_one_task_per_device(self):
        runtime = self.runtime()
        first = runtime.create("settings", "Open About", "live", device=SIM_B)
        with self.assertRaises(APIError) as busy:
            runtime.create("settings", "Open General", "live", device=SIM_B)
        self.assertEqual((busy.exception.code, busy.exception.details["activeRunId"]), ("run_active", first.id))
        self.assertIn("choose another device", str(busy.exception))
        runtime.create("settings", "Open General", "live", device=SIM_A)  # the other device is free
        self.finish(runtime, first)
        runtime.create("settings", "Open Wallpaper", "live", device=SIM_B)  # and B again once its task ended

    def test_a_device_another_process_holds_is_busy_and_the_rest_are_not(self):
        runtime = self.runtime()
        self.held_elsewhere = {B_URL}
        with self.assertRaises(APIError) as busy:
            runtime.create("settings", "Open About", "live", device=SIM_B)
        self.assertEqual(busy.exception.code, "device_busy")
        self.assertEqual(runtime.runs, {})
        self.assertEqual(runtime.create("settings", "Open About", "live", device=SIM_A).device_id, SIM_A)

    def test_a_device_not_ready_is_refused_plainly(self):
        runtime = self.runtime()
        self.targets[SIM_B] = DOWN
        status, body = self.request(runtime, "POST", "/api/runs",
                                    {"appId": "settings", "goal": "Open About", "device": SIM_B})
        self.assertEqual((status, body["code"], body["device"]), (503, "device_unavailable", SIM_B))
        self.assertTrue(body["error"].startswith("Mobster · iPhone 17 Pro · iOS 26.4 · 2 is not ready"))

    def test_naming_a_device_that_is_not_there(self):
        runtime = self.runtime()
        status, body = self.request(runtime, "POST", "/api/runs", {"appId": "settings", "goal": "x", "device": "nope"})
        self.assertEqual((status, body["code"]), (404, "device_not_found"))
        status, body = self.request(runtime, "POST", "/api/runs", {"appId": "settings", "goal": "x", "device": 7})
        self.assertEqual(status, 400)
        self.assertEqual(runtime.runs, {})

    def test_the_default_device_is_the_one_last_used_unless_serve_names_one(self):
        runtime = self.runtime()
        run = runtime.create("settings", "Open About", "live", device=SIM_B)
        self.finish(runtime, run)
        self.assertEqual(runtime.create("settings", "Open General", "live").device_id, SIM_B)
        named = self.runtime(device="Mobster · iPhone 17 Pro · iOS 26.4", state=self.root / "named")
        named.fleet.last_used = SIM_B
        self.assertEqual(named.create("settings", "Open General", "live").device_id, SIM_A)

    def test_the_same_request_key_naming_a_device_two_ways_is_one_task(self):
        runtime = self.runtime()
        run, replayed = runtime.create("settings", "Open About", "live", "key-1", device=SIM_B)
        again, replayed = runtime.create("settings", "Open About", "live", "key-1",
                                         device="Mobster · iPhone 17 Pro · iOS 26.4 · 2")
        self.assertTrue(replayed)
        self.assertIs(again, run)

    def test_history_per_device_and_old_runs_belong_to_the_primary(self):
        runtime = self.runtime()
        on_b = runtime.create("settings", "Open About", "live", device=SIM_B)
        self.finish(runtime, on_b)
        on_a = runtime.create("settings", "Open General", "live", device=SIM_A)
        self.finish(runtime, on_a)
        on_a.device_id = None  # a run saved before devices had one
        _, body = self.request(runtime, "GET", f"/api/runs?device={SIM_B}")
        self.assertEqual([item["id"] for item in body["runs"]], [on_b.id])
        _, body = self.request(runtime, "GET", f"/api/runs?device={SIM_A}")
        self.assertEqual([item["id"] for item in body["runs"]], [on_a.id])
        _, body = self.request(runtime, "GET", "/api/runs")
        self.assertEqual(len(body["runs"]), 2)

    def test_a_run_keeps_its_device_across_a_restart(self):
        state = self.root / "kept"
        runtime = self.runtime(state=state)
        run = runtime.create("settings", "Open About", "live", device=SIM_B)
        self.finish(runtime, run)
        runtime.close()
        self.runtimes.remove(runtime)
        again = self.runtime(state=state)
        self.assertEqual((again.runs[run.id].device_id, again.runs[run.id].device_name),
                         (SIM_B, "Mobster · iPhone 17 Pro · iOS 26.4 · 2"))

    def test_approvals_show_on_their_device(self):
        runtime = self.runtime()
        run = runtime.create("settings", "Open About", "live", device=SIM_B)
        run.approval = {"id": "a1", "operation": "TAP", "label": "Delete"}
        _, body = self.request(runtime, "GET", f"/api/devices/{SIM_B}")
        self.assertEqual((body["device"]["activeRunId"], body["device"]["approval"]["id"]), (run.id, "a1"))
        _, body = self.request(runtime, "GET", f"/api/devices/{SIM_A}")
        self.assertIsNone(body["device"]["approval"])


class PerDeviceTests(Harness):
    def test_control_is_refused_only_on_the_device_with_a_task(self):
        runtime = self.runtime()
        runtime.control = Mock()
        runtime.control.perform.return_value = "tap"
        runtime.create("settings", "Open About", "live", device=SIM_B)
        status, body = self.request(runtime, "POST", f"/api/devices/{SIM_B}/control", {"action": "tap"})
        self.assertEqual((status, body["code"]), (409, "device_busy"))
        status, body = self.request(runtime, "POST", f"/api/devices/{SIM_A}/control", {"action": "tap"})
        self.assertEqual((status, body), (200, {"ok": True, "action": "tap"}))
        status, body = self.request(runtime, "POST", "/api/device/control", {"action": "tap"})  # the primary
        self.assertEqual(status, 200)

    def test_each_device_has_its_own_live_view(self):
        runtime = self.runtime()
        phone = runtime.fleet.find(SIM_B)
        phone.video.status.return_value = {"status": "streaming", "viewers": 1}
        _, body = self.request(runtime, "GET", f"/api/devices/{SIM_B}/video/status")
        self.assertEqual(body["status"], "streaming")
        self.assertIsNot(phone.video, runtime.video)
        status, body = self.request(runtime, "GET", f"/api/devices/{SIM_B}/info")
        self.assertEqual((status, body["modelName"], body["iosVersion"], body["virtual"]),
                         (200, "iPhone 17 Pro", "26.4", True))

    def test_a_task_on_another_device_drives_that_device(self):
        runtime = self.runtime()
        run = runtime.create("settings", "Open About", "live", device=SIM_B)
        phone = runtime.fleet.find(SIM_B)
        phone.wda_session = Mock(return_value="session-b")
        built = {}

        def driver(**options):
            built.update(options)
            return Mock()
        trace = SimpleNamespace(span=lambda name: open(os.devnull))
        setup = SimpleNamespace(hold=lambda value: value, phase=lambda name: None)
        with patch("mobile_agent.server.build_target_driver", side_effect=driver), \
                patch("mobile_agent.server.prepare_wda_phone"):
            runtime.open_driver(run, trace, setup)
        self.assertEqual((built["wda_url"], built["session"]), (B_URL, "session-b"))

    def test_saved_tasks_name_their_device(self):
        runtime = self.runtime()
        saved = runtime.workflows.create({"name": "Check B", "appId": "settings", "goal": "Open About",
                                          "device": "Mobster · iPhone 17 Pro · iOS 26.4 · 2"})
        self.assertEqual(saved["device"], SIM_B)  # by its id, however it was named
        with self.assertRaisesRegex(ValueError, "No device"):
            runtime.workflows.create({"name": "Nowhere", "appId": "settings", "goal": "x", "device": "nope"})
        updated = runtime.workflows.update(saved["id"], {"revision": 1, "device": None})
        self.assertIsNone(updated["device"])
        plain = runtime.workflows.create({"name": "Default", "appId": "settings", "goal": "Open General"})
        self.assertIsNone(plain["device"])
        create = Mock(return_value=(SimpleNamespace(id="abc123abc123"), False))
        with patch.object(runtime, "create", create), patch.object(Workflows, "_record_started", lambda *args: None):
            Workflows._dispatch(runtime.workflows, SimpleNamespace(key="k1", workflow={**saved, "device": SIM_B}))
            Workflows._dispatch(runtime.workflows, SimpleNamespace(key="k2", workflow=plain))
        self.assertEqual(create.call_args_list[0].kwargs["device"], SIM_B)
        self.assertNotIn("device", create.call_args_list[1].kwargs)
        self.assertEqual(create.call_args_list[1].kwargs["default_device"], "primary")


class SavedTaskDeviceTests(Harness):
    """A saved task runs on its own phone, or the primary: never on whichever phone was used last."""

    def dispatch(self, runtime, workflow, key):
        outcomes = []
        with patch.object(Workflows, "_record_started", lambda *args: None), \
                patch.object(Workflows, "_outcome", lambda workflows, key, outcome, run_id=None: outcomes.append(outcome)):
            try:
                run, _ = Workflows._dispatch(runtime.workflows, SimpleNamespace(key=key, workflow=workflow))
            except APIError as error:
                return error, outcomes
        return run, outcomes

    def test_a_saved_task_without_a_device_runs_on_the_primary_after_a_task_on_another_phone(self):
        runtime = self.runtime()
        manual = runtime.create("settings", "Open About", "live", device=SIM_B)
        self.finish(runtime, manual)
        plain = runtime.workflows.create({"name": "Daily", "appId": "settings", "goal": "Open General"})
        run, _ = self.dispatch(runtime, plain, "wk1")
        self.assertEqual(run.device_id, SIM_A)

    def test_a_saved_task_without_a_device_follows_serve_device(self):
        runtime = self.runtime(device="Mobster · iPhone 17 Pro · iOS 26.4 · 2")
        plain = runtime.workflows.create({"name": "Daily", "appId": "settings", "goal": "Open General"})
        run, _ = self.dispatch(runtime, plain, "wk2")
        self.assertEqual(run.device_id, SIM_B)

    def test_a_saved_task_whose_device_is_gone_is_skipped_as_offline(self):
        runtime = self.runtime()
        saved = runtime.workflows.create({"name": "B", "appId": "settings", "goal": "Open About", "device": SIM_B})
        self.sims = self.sims[:1]  # simulator B deleted
        runtime.fleet.cache.clear()
        error, outcomes = self.dispatch(runtime, saved, "wk3")
        self.assertEqual((error.code, outcomes), ("device_not_found", ["skipped_offline"]))

    def test_status_counts_every_device_as_capacity(self):
        runtime = self.runtime()
        runtime.fleet.public()
        runtime.create("settings", "Open About", "live", device=SIM_B)
        self.assertEqual(runtime.status()["concurrency"], {"capacity": 2, "running": 1, "available": 1})

    def test_a_runtime_with_one_target_never_lists_devices_for_a_task(self):
        # `mobster run` and the terminal UI: --wda-url / --device already named the phone.
        runtime = self.runtime()
        runtime.one_target = True  # as TerminalRuntime
        with patch.object(fleet_module.Fleet, "inventory", side_effect=AssertionError("listed the devices")):
            run = runtime.create("settings", "Open About", "live")
        self.assertIsNone(run.phone)
        self.assertEqual(self.leases, [A_URL])


class ManagedTests(Harness):
    """The Mac app's server (`serve --manage-device`): a second USB iPhone becomes its own device."""

    def setUp(self):
        super().setUp()
        self.sims = []
        self.listing = [{"udid": PRO, "name": "My iPhone", "model": "iPhone16,1", "ios": "26.0", "trusted": True},
                        {"udid": XR, "name": "Work iPhone", "model": "iPhone11,8", "ios": "18.6", "trusted": True}]
        self.builds = []
        for item in (patch.object(DeviceManager, "watch", lambda manager, stop: None),
                     patch.object(DeviceManager, "devices", lambda manager: list(self.listing)),
                     patch.object(DeviceManager, "start_build",
                                  lambda manager, team, renewal=False: self.builds.append((manager.wda_url, team))),
                     patch.object(DeviceManager, "teams", lambda manager: []),
                     patch.object(DeviceManager, "tools", lambda manager: {
                         "xcodebuild": "/x", "iproxy": "/i", "idevice_id": "/d", "ideviceinfo": "/v", "git": "/g",
                         "xcode": {"state": "ok", "version": "Xcode 26.4", "app": "/Applications/Xcode.app",
                                   "fixes": [], "selected": "/x", "developer_dir": "/x", "problems": []}}),
                     patch("mobile_agent.setup_service.SetupService.developer_mode", lambda service, device: True),
                     patch("mobile_agent.installed_apps.InstalledApps.snapshot", lambda apps: None)):
            item.start()
            self.addCleanup(item.stop)
        self.data = self.root / "data"
        self.data.mkdir()

    def managed(self, chosen=PRO):
        if chosen:
            (self.data / "device.json").write_text(json.dumps({"udid": chosen}))
        runtime = self.runtime(wda_url="http://127.0.0.1:8100", manage_device=True, data_dir=str(self.data),
                               socket=None)
        return runtime

    def test_setting_up_a_second_iphone_gives_it_its_own_ports_and_folder(self):
        runtime = self.managed()
        _, body = self.request(runtime, "GET", "/api/devices")
        self.assertEqual([(item["id"], item["primary"], item["state"]) for item in body["devices"]],
                         [(PRO, True, "needs_setup"), (XR, False, "needs_setup")])
        status, body = self.request(runtime, "POST", f"/api/devices/{XR}/setup/build", {"team": "ABCDE12345"})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.builds, [("http://127.0.0.1:8101", "ABCDE12345")])
        self.assertEqual([(entry["udid"], entry["wdaPort"], entry["mjpegPort"])
                          for entry in DeviceStore(self.data).phones()], [(XR, 8101, 9101)])
        phone = runtime.fleet.find(XR)
        self.assertEqual((phone.manager.pinned, phone.manager.settings_path),
                         (XR, self.data / "devices" / XR / "device.json"))
        self.assertEqual(body["device"]["udid"], XR)  # its own Setup checklist
        self.assertEqual(runtime.manager.settings()["udid"], PRO)  # the primary is untouched
        status, _ = self.request(runtime, "POST", "/api/setup/phone", {"udid": XR})
        self.assertEqual(status, 409)  # never two managers for one phone

    def test_the_first_iphone_set_up_is_the_primary(self):
        runtime = self.managed(chosen=None)
        status, _ = self.request(runtime, "POST", f"/api/devices/{XR}/setup/build", {"team": "ABCDE12345"})
        self.assertEqual(status, 200)
        self.assertEqual(self.builds, [("http://127.0.0.1:8100", "ABCDE12345")])
        self.assertEqual(runtime.manager.settings()["udid"], XR)
        self.assertEqual(DeviceStore(self.data).phones(), [])

    def test_a_phone_set_up_earlier_comes_back_after_a_restart_and_can_be_forgotten(self):
        DeviceStore(self.data).add(XR, "Work iPhone", busy=lambda port: False)
        runtime = self.managed()
        phone = runtime.fleet.find(XR)
        self.assertEqual(phone.wda_url, "http://127.0.0.1:8101")
        status, body = self.request(runtime, "DELETE", f"/api/devices/{PRO}")
        self.assertEqual(status, 409)  # the primary is changed in Setup, never forgotten
        status, body = self.request(runtime, "DELETE", f"/api/devices/{XR}")
        self.assertEqual((status, body), (200, {"forgotten": XR}))
        self.assertEqual(DeviceStore(self.data).phones(), [])
        self.assertTrue(phone.manager.watching.is_set())


class ManagedFixesTests(ManagedTests):
    """A phone plugged in but not set up, per-device requests, and forgetting a phone."""

    def test_a_new_phone_never_shows_the_first_iphones_screen(self):
        runtime = self.managed()  # PRO set up, XR plugged in only
        runtime.video = Mock(latest=Mock(return_value=(7, "image/jpeg", b"FIRST-IPHONE")),
                             subscribe=Mock(side_effect=AssertionError("streamed the first iPhone")))
        for part in ("frame", "stream"):
            status, body = self.request(runtime, "GET", f"/api/devices/{XR}/{part}")
            self.assertEqual((status, body["code"]), (503, "video_unavailable"), part)
            self.assertEqual(body["error"], "This device has no live view yet")
        runtime.video.latest.assert_not_called()

    def test_check_again_works_on_a_new_phones_first_steps(self):
        runtime = self.managed()
        status, _ = self.request(runtime, "GET", f"/api/devices/{XR}/setup")
        self.assertEqual(status, 200)
        refreshed = []
        with patch.object(DeviceManager, "refresh", lambda manager: refreshed.append(manager.pinned), create=True):
            status, body = self.request(runtime, "POST", f"/api/devices/{XR}/setup/refresh", {})
        self.assertEqual(status, 200, body)
        self.assertEqual(refreshed, [XR])
        self.assertEqual(body["device"]["udid"], XR)
        status, body = self.request(runtime, "POST", f"/api/devices/{XR}/setup/start", {})
        self.assertEqual((status, body["code"]), (409, "setup_refused"))  # no runner to start before the build

    def test_control_on_a_new_phone_says_to_set_it_up(self):
        runtime = self.managed()
        status, body = self.request(runtime, "POST", f"/api/devices/{XR}/control", {"action": "home"})
        self.assertEqual((status, body["code"]), (409, "control_unavailable"))
        self.assertIn("Set this iPhone up", body["error"])

    def test_requests_for_a_known_phone_never_list_usb_devices_again(self):
        DeviceStore(self.data).add(XR, "Work iPhone", busy=lambda port: False)
        runtime = self.managed()
        runtime.fleet.public()
        for ident in (PRO, XR):  # each phone's model and iOS, read once a minute
            self.assertEqual(self.request(runtime, "GET", f"/api/devices/{ident}/info")[0], 200)
        listings = []
        real = fleet_module.time.monotonic
        ticks = iter(range(3, 10_000, 3))  # every request after the USB listing's 2 seconds
        with patch.object(DeviceManager, "devices", lambda manager: listings.append(1) or list(self.listing)), \
                patch.object(fleet_module.time, "monotonic", side_effect=lambda: real() + next(ticks)):
            for ident, part in ((PRO, "info"), (XR, "info"), (XR.lower(), "frame"), (PRO, "frame")):
                status, _ = self.request(runtime, "GET", f"/api/devices/{ident}/{part}")
                self.assertIn(status, (200, 503))
        self.assertEqual(listings, [])
        self.assertIs(runtime.fleet.lookup(XR.lower()), runtime.fleet.phones[XR])
        # A name, and a task's device, still go through the whole list.
        with patch.object(DeviceManager, "devices", lambda manager: listings.append(1) or list(self.listing)):
            runtime.fleet.cache.pop("usb", None)
            self.assertIs(runtime.fleet.lookup("Work iPhone"), runtime.fleet.phones[XR])
            runtime.fleet.cache.pop("usb", None)
            self.assertIs(runtime.fleet.for_run(XR), runtime.fleet.phones[XR])
        self.assertEqual(len(listings), 2)

    def test_forgetting_a_phone_keeps_the_others_last_used_times(self):
        DeviceStore(self.data).add(XR, "Work iPhone", busy=lambda port: False)
        runtime = self.managed()
        runtime.fleet.public()
        runtime.fleet.mark_used(runtime.fleet.primary)
        used = runtime.fleet.last_used_at(runtime.fleet.primary)
        self.assertIsNotNone(used)
        status, _ = self.request(runtime, "DELETE", f"/api/devices/{XR}")
        self.assertEqual(status, 200)
        self.assertEqual(runtime.fleet.last_used_at(runtime.fleet.primary), used)


if __name__ == "__main__":
    unittest.main()
