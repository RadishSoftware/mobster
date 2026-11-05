import io
import json
import os
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mobile_agent import fleet as fleet_module
from mobile_agent.api_errors import APIError
from mobile_agent.device_manager import DeviceManager
from mobile_agent.devices import REASONS, DeviceStore
from mobile_agent.journal import Lease, LeaseHeld
from mobile_agent.server import Runtime, make_handler

A_URL, B_URL = "http://127.0.0.1:8310", "http://127.0.0.1:8311"
SIM_A, SIM_B = "6F1C0E52-0000-4000-8000-00000000000A", "6F1C0E52-0000-4000-8000-00000000000B"
PRO = "00008130-001A2B3C4D5E6F70"
XR = "00008020-000A1B2C3D4E5F60"
SE = "00008030-000B2C3D4E5F6071"
READY = {"device": True, "ready": True, "health": None, "can_act": True, "driver": "wda", "screen_reading": False}
DOWN = {**READY, "ready": False}
SETTINGS = {"id": "settings", "name": "Settings", "bundleId": "com.apple.Preferences", "installed": True}
CLEAN = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
         "ANTHROPIC_API_KEY": "sk-ant-test-000000000000",
         "MOBSTER_ASK_BEFORE_ACTING": "1"}


def row(udid, url, name):
    return {"udid": udid, "name": name, "device_type": "iPhone 17 Pro", "runtime": "iOS 26.4", "state": "Booted",
            "wda_url": url, "mjpeg_url": url.replace(":83", ":93")}


class Harness(unittest.TestCase):
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
                     patch("mobile_agent.journal.Lease.device", side_effect=lease),
                     patch("mobile_agent.devices.simulator_rows", side_effect=lambda *a, **k: list(self.sims)),
                     patch("mobile_agent.devices.lease_free", return_value=True),
                     patch("mobile_agent.devices.static_specs", return_value=[]),
                     patch.object(fleet_module.Phone, "target_status",
                                  lambda phone: self.targets.get(phone.id, READY)),
                     patch("mobile_agent.wda.video.WdaVideo"), patch("mobile_agent.manual_control.ManualControl")):
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

    def request(self, runtime, method, path, body=None, handler=None, headers=None):
        handler = object.__new__(handler or make_handler(runtime))
        raw = json.dumps(body).encode() if body is not None else b""
        handler.headers = Message()
        handler.headers["Host"] = "127.0.0.1:8765"
        for name, value in (headers or {}).items():
            handler.headers[name] = value
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
            self.assertNotIn("wdaUrl", item)
            self.assertEqual(set(item) - {"wdaUrl", "mjpegUrl"}, {
                "id", "kind", "name", "udid", "model", "modelName", "ios", "enclosure", "state", "reason", "primary",
                "setUp",
                "default", "activeRunId", "approval", "lastUsedAt", "nickname", "disconnected", "askBeforeActing",
                "apps", "defaultSet", "connection", "health", "holder", "queue", "queuePausedUntil"})
            self.assertEqual((item["nickname"], item["disconnected"], item["askBeforeActing"], item["apps"]),
                             (None, False, None, None))

    def test_the_fleet_health_report_lists_each_phone(self):
        runtime = self.runtime()
        status, body = self.request(runtime, "GET", "/api/fleet/health")
        self.assertEqual(status, 200)
        self.assertEqual([d["id"] for d in body["devices"]], [SIM_A, SIM_B])
        self.assertTrue(body["checkedAt"] > 0)
        self.assertEqual(body["devices"][0]["health"]["alerts"], [])

    def test_one_phone_stays_simple(self):
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


class ChosenPhoneTests(Harness):
    def test_the_phone_chosen_in_the_composer_survives_a_relaunch(self):
        state = self.root / "kept"
        runtime = self.runtime(state=state)
        status, body = self.request(runtime, "POST", "/api/devices/chosen", {"devices": [SIM_B]})
        self.assertEqual((status, body["chosenDevices"], body["chosenSaved"]), (200, [SIM_B], [SIM_B]))
        self.finish(runtime, runtime.create("settings", "Open About", "live", device=SIM_A))
        self.assertEqual(self.request(runtime, "GET", "/api/devices")[1]["defaultDevice"], SIM_A)
        runtime.close()
        self.runtimes.remove(runtime)
        again = self.runtime(state=state)
        _, body = self.request(again, "GET", "/api/devices")
        self.assertEqual((body["chosenDevices"], body["chosenSaved"], body["defaultDevice"]), ([SIM_B], [SIM_B], SIM_A))
        _, body = self.request(again, "POST", "/api/devices/chosen", {"devices": [SIM_B, SIM_A]})
        self.assertEqual(body["chosenDevices"], [SIM_B, SIM_A])
        _, body = self.request(again, "POST", "/api/devices/chosen", {"devices": []})
        self.assertEqual((body["chosenDevices"], body["chosenSaved"]), ([SIM_A], []))

    def test_a_chosen_phone_still_connected_stays_chosen_even_when_it_isnt_ready(self):
        runtime = self.runtime()
        self.request(runtime, "POST", "/api/devices/chosen", {"devices": [SIM_B]})
        records = runtime.fleet.public()
        for state in ("connected", "busy"):
            self.assertEqual(runtime.fleet.chosen([{**item, "state": state} if item["id"] == SIM_B
                                                   else item for item in records]), [SIM_B])
        # Needing setup, it gives way while another phone is set up (DR-78), and the choice is kept for when it is.
        unset = [{**item, "state": "needs_setup"} if item["id"] == SIM_B
                 else {**item, "state": "ready"} for item in records]
        self.assertEqual(runtime.fleet.chosen(unset), [SIM_A])
        self.assertEqual(runtime.fleet.stored()["chosen"], [SIM_B])
        self.assertEqual(runtime.fleet.chosen([{**item, "state": "needs_setup"} for item in records]), [SIM_B])

    def test_with_the_chosen_phone_away_a_ready_phone_is_picked_and_the_choice_is_kept(self):
        runtime = self.runtime()
        fleet = runtime.fleet
        fleet.choose(["alex-iphone"])
        phone = lambda id, state, **more: {"id": id, "state": state, "default": False, "primary": False, **more}
        self.assertEqual(fleet.chosen([
            phone("example", "needs_setup", default=True, primary=True), phone("sam", "ready")]), ["sam"])
        self.assertEqual(fleet.chosen([
            phone("example", "needs_setup", primary=True), phone("alex-iphone", "disconnected"),
            phone("sam", "connected")]), ["sam"])
        self.assertEqual(fleet.chosen([phone("a", "busy", primary=True), phone("b", "ready")]), ["b"])
        self.assertEqual(fleet.chosen([phone("a", "connected", primary=True), phone("b", "busy")]), ["b"])
        self.assertEqual(fleet.chosen([phone("a", "ready", primary=True), phone("b", "ready", default=True)]), ["b"])
        self.assertEqual(fleet.chosen([phone("a", "ready", primary=True), phone("b", "ready")]), ["a"])
        self.assertEqual(fleet.chosen([phone("a", "disconnected", default=True), phone("b", "disconnected")]), ["a"])
        self.assertEqual(fleet.chosen([]), [])
        self.assertEqual(fleet.stored()["chosen"], ["alex-iphone"])
        self.assertEqual(fleet.chosen([phone("sam", "ready"), phone("alex-iphone", "connected")]), ["alex-iphone"])

    def test_a_bad_choice_is_refused_and_changes_nothing(self):
        runtime = self.runtime()
        self.request(runtime, "POST", "/api/devices/chosen", {"devices": [SIM_B]})
        for body in ({"devices": SIM_A}, {"devices": [SIM_A, SIM_A]}, {"devices": ["../etc"]}, {"devices": [1]},
                     {"devices": [SIM_A], "extra": True}, {}):
            status, answer = self.request(runtime, "POST", "/api/devices/chosen", body)
            self.assertEqual(status, 400, body)
        self.assertEqual(runtime.fleet.stored()["chosen"], [SIM_B])


class AdmissionTests(Harness):
    def test_one_task_per_device(self):
        runtime = self.runtime()
        first = runtime.create("settings", "Open About", "live", device=SIM_B)
        with self.assertRaises(APIError) as busy:
            runtime.create("settings", "Open General", "live", device=SIM_B)
        self.assertEqual((busy.exception.code, busy.exception.details["activeRunId"]), ("run_active", first.id))
        self.assertIn("choose another device", str(busy.exception))
        runtime.create("settings", "Open General", "live", device=SIM_A)
        self.finish(runtime, first)
        runtime.create("settings", "Open Wallpaper", "live", device=SIM_B)

    def test_a_device_not_ready_is_refused_plainly(self):
        runtime = self.runtime()
        self.targets[SIM_B] = DOWN
        status, body = self.request(runtime, "POST", "/api/runs",
                                    {"appId": "settings", "goal": "Open About", "device": SIM_B})
        self.assertEqual((status, body["code"], body["device"]), (503, "device_unavailable", SIM_B))
        self.assertTrue(body["error"].startswith("Mobster · iPhone 17 Pro · iOS 26.4 · 2 is not ready"))

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
        on_a.device_id = None
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
    def test_manual_control_blocks_admission_only_on_its_own_device(self):
        runtime = self.runtime()

        def gesture(_body):
            with self.assertRaises(APIError) as busy:
                runtime.create("settings", "Open About", "live", device=SIM_A)
            self.assertEqual(busy.exception.code, "device_busy")
            other = runtime.create("settings", "Open About", "live", device=SIM_B)
            self.finish(runtime, other)
            return "tap"

        runtime.control = Mock()
        runtime.control.perform.side_effect = gesture
        self.assertEqual(runtime.control_device({"action": "tap", "x": .5, "y": .5}), "tap")
        admitted = runtime.create("settings", "Open About", "live", device=SIM_A)
        self.finish(runtime, admitted)

    def test_control_is_refused_only_on_the_device_with_a_task(self):
        runtime = self.runtime()
        runtime.control = Mock()
        runtime.control.perform.return_value = "tap"
        runtime.create("settings", "Open About", "live", device=SIM_B)
        status, body = self.request(runtime, "POST", f"/api/devices/{SIM_B}/control", {"action": "tap"})
        self.assertEqual((status, body["code"]), (409, "device_busy"))
        status, body = self.request(runtime, "POST", f"/api/devices/{SIM_A}/control", {"action": "tap"})
        self.assertEqual((status, body), (200, {"ok": True, "action": "tap"}))
        status, body = self.request(runtime, "POST", "/api/device/control", {"action": "tap"})
        self.assertEqual(status, 200)

    def test_control_and_teach_need_the_mac_apps_session_not_only_the_token(self):
        runtime = self.runtime()
        runtime.control = Mock()
        runtime.control.perform.return_value = "tap"
        handler = make_handler(runtime, "t" * 43, app_session="s" * 43)
        token = {"Authorization": "Bearer " + "t" * 43}
        for path, body in ((f"/api/devices/{SIM_A}/control", {"action": "tap"}),
                           ("/api/device/control", {"action": "tap"}),
                           (f"/api/devices/{SIM_A}/teach", {"action": "start"}),
                           ("/api/device/teach", {"action": "start"})):
            with self.subTest(path=path):
                status, data = self.request(runtime, "POST", path, body, handler=handler, headers=token)
                self.assertEqual((status, data["code"]), (403, "app_only"))
        runtime.control.perform.assert_not_called()
        app = {**token, "X-Mobster-App-Session": "s" * 43}
        status, data = self.request(runtime, "POST", f"/api/devices/{SIM_A}/control", {"action": "tap"},
                                    handler=handler, headers=app)
        self.assertEqual((status, data), (200, {"ok": True, "action": "tap"}))
        status, data = self.request(runtime, "POST", "/api/device/control", {"action": "tap"}, handler=handler,
                                    headers=app)
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
        with patch("mobile_agent.runtime_driver.build_target_driver", side_effect=driver), \
                patch("mobile_agent.runtime_driver.prepare_wda_phone"):
            runtime.open_driver(run, trace, setup)
        self.assertEqual((built["wda_url"], built["session"]), (B_URL, "session-b"))

class SavedTaskDeviceTests(Harness):
    def dispatch(self, runtime, workflow, key):
        try:
            run, _, _ = runtime.workflows._run(workflow["id"], key)
        except APIError as error:
            run = error
        return run, [runtime.workflows.get(workflow["id"])["lastOutcome"]]

    def test_saved_tasks_name_their_device(self):
        runtime = self.runtime()
        saved = runtime.workflows.create({"name": "Check B", "appId": "settings", "goal": "Open About",
                                          "device": "Mobster · iPhone 17 Pro · iOS 26.4 · 2"})
        self.assertEqual(saved["device"], SIM_B)
        with self.assertRaisesRegex(ValueError, "No device"):
            runtime.workflows.create({"name": "Nowhere", "appId": "settings", "goal": "x", "device": "nope"})
        run, _ = self.dispatch(runtime, saved, "wk2")
        self.assertEqual(run.device_id, SIM_B)
        updated = runtime.workflows.update(saved["id"], {"revision": 1, "device": None})
        self.assertIsNone(updated["device"])

    def test_a_saved_task_without_a_device_runs_on_the_primary_after_a_task_on_another_phone(self):
        runtime = self.runtime()
        manual = runtime.create("settings", "Open About", "live", device=SIM_B)
        self.finish(runtime, manual)
        plain = runtime.workflows.create({"name": "Daily", "appId": "settings", "goal": "Open General"})
        run, _ = self.dispatch(runtime, plain, "wk1")
        self.assertEqual(run.device_id, SIM_A)

    def test_a_saved_task_whose_device_is_gone_is_skipped_as_offline(self):
        runtime = self.runtime()
        saved = runtime.workflows.create({"name": "B", "appId": "settings", "goal": "Open About", "device": SIM_B})
        self.sims = self.sims[:1]
        runtime.fleet.cache.clear()
        error, outcomes = self.dispatch(runtime, saved, "wk3")
        self.assertEqual((error.code, outcomes), ("device_not_found", ["skipped_offline"]))


class ManagedTests(Harness):
    def setUp(self):
        super().setUp()
        self.sims = []
        self.listing = [{"udid": PRO, "name": "My iPhone", "model": "iPhone16,1", "ios": "26.0", "trusted": True},
                        {"udid": XR, "name": "Work iPhone", "model": "iPhone11,8", "ios": "18.6", "trusted": True}]
        self.builds = []
        for item in (patch("mobile_agent.devices.listening", lambda port, *args, **kwargs: False),
                     patch.object(DeviceManager, "watch", lambda manager, stop: None),
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
        self.assertEqual(body["device"]["udid"], XR)
        self.assertEqual(runtime.manager.settings()["udid"], PRO)
        status, _ = self.request(runtime, "POST", "/api/setup/phone", {"udid": XR})
        self.assertEqual(status, 409)

    def test_a_new_phone_never_shows_the_first_iphones_screen(self):
        runtime = self.managed()
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
        self.assertEqual((status, body["code"]), (409, "setup_refused"))

    def test_control_on_a_new_phone_says_to_set_it_up(self):
        runtime = self.managed()
        status, body = self.request(runtime, "POST", f"/api/devices/{XR}/control", {"action": "home"})
        self.assertEqual((status, body["code"]), (409, "control_unavailable"))
        self.assertIn("Set this iPhone up", body["error"])

    def test_a_locked_iphone_raises_the_locked_alert_and_an_unresponsive_one_does_not(self):
        runtime = self.managed()
        phone = runtime.fleet.find(PRO)
        for name, value in (("built", True), ("expired", False), ("settings", {"udid": PRO}), ("out_of_date", False)):
            patch.object(phone.manager, name, return_value=value).start()
        self.addCleanup(patch.stopall)
        for health, alerts, reason in (
                ({"responsive": True, "locked": True}, ["locked"], REASONS["locked"]),
                ({"responsive": False, "locked": None}, [], phone.manager.health_error)):
            phone.manager.health.update(health)
            runtime.target_status.return_value = {**READY, "ready": False, "health": phone.manager.health_error()}
            item = next(item for item in runtime.fleet.public() if item["id"] == PRO)
            self.assertEqual((item["state"], [a["code"] for a in item["health"]["alerts"]]), ("connected", alerts))
            self.assertEqual(item["reason"], reason if isinstance(reason, str) else reason())

    def test_a_task_naming_no_phone_never_defaults_to_one_that_needs_setup(self):
        # DR-78: the first iPhone was never set up and a second one is ready; nothing was used yet this launch.
        DeviceStore(self.data).add(XR, "Work iPhone", busy=lambda port: False)
        runtime = self.managed()
        ready = runtime.fleet.find(XR)
        patch.object(ready.manager, "built", return_value=True).start()
        self.addCleanup(patch.stopall)
        self.assertIsNone(runtime.fleet.last_used)
        self.assertEqual(runtime.fleet.default().id, XR)
        _, body = self.request(runtime, "GET", "/api/devices")
        self.assertEqual(body["defaultDevice"], XR)
        # Used last, a phone that needs setup still gives way; with none set up, the order stays as it was.
        runtime.fleet.last_used = PRO
        self.assertEqual(runtime.fleet.default().id, XR)
        patch.object(ready.manager, "built", return_value=False).start()
        self.assertEqual(runtime.fleet.default().id, PRO)

    def test_forgetting_a_phone_moves_its_profiles_to_the_default_phone(self):
        from mobile_agent.accounts.store import store_for
        DeviceStore(self.data).add(XR, "Work iPhone", busy=lambda port: False)
        runtime = self.managed()
        store = store_for(runtime)
        app = {"bundleId": "com.apple.Preferences", "name": "Settings"}
        on_work = store.add({"handle": "work", "app": app})
        on_pro = store.add({"handle": "main", "app": app})
        store.update(on_work["id"], {"device": XR}, device=(XR, "Work iPhone"))
        store.update(on_pro["id"], {"device": PRO}, device=(PRO, "My iPhone"))
        runtime.fleet.public()
        self.assertEqual(self.request(runtime, "DELETE", f"/api/devices/{XR}")[0], 200)
        moved = {a["handle"]: (a["device"], a["deviceName"]) for a in store.all()}
        self.assertEqual(moved, {"work": (None, None), "main": (PRO, "My iPhone")})
        run = runtime.create("settings", "Open About", "live", engine="smart", extras={"account": "@work"})
        self.assertEqual(run.extras["account"]["handle"], "work")
        self.finish(runtime, run)
        with self.assertRaisesRegex(ValueError, "@main on Settings is on My iPhone"):
            runtime.create("settings", "Open About", "live", engine="smart", device=SIM_A,
                           extras={"account": "@main"})


if __name__ == "__main__":
    unittest.main()
