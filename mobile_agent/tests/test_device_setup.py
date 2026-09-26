"""Device manager and setup checklist. Offline: tools, devices and processes are faked."""

import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent import device_manager as dm
from mobile_agent.setup_service import SetupService


class EnvFileTests(unittest.TestCase):
    def test_values_are_replaced_and_the_file_is_private(self):
        path = Path(tempfile.mkdtemp()) / "agent.env"
        path.write_text("OTHER=1\nTYPESAFE_API_KEY=old\n")
        dm.save_env_value(path, "TYPESAFE_API_KEY", "new")
        self.assertEqual(path.read_text(), "OTHER=1\nTYPESAFE_API_KEY=new\n")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_newlines_cannot_inject_entries(self):
        with self.assertRaises(ValueError):
            dm.save_env_value(Path(tempfile.mkdtemp()) / "e", "KEY", "a\nEVIL=1")


class BuildHintTests(unittest.TestCase):
    def test_known_failures_become_instructions(self):
        hint = dm.build_hint(["error: Device \"iPhone\" isn't registered in your developer account."])
        self.assertIn("Xcode › Settings › Accounts", hint)
        self.assertIn("error: something else", dm.build_hint(["noise", "error: something else"]))


class DeviceTests(unittest.TestCase):
    def manager(self):
        return dm.DeviceManager(tempfile.mkdtemp())

    def fake_run(self, trusted=True):
        def run(command, timeout=10):
            if command[-1] == "-l":
                return "0000FE01-AAAAAAAAAAAAAAAA\n00008130-001A2B3C4D5E6F70\n00008130-001A2B3C4D5E6F70\n"
            if not trusted:
                return None
            return {"DeviceName": "Alex's iPhone\n", "ProductType": "iPhone16,1\n", "ProductVersion": "26.0.1\n"}[command[-1]]
        return run

    def test_physical_phones_are_listed_once_with_trust(self):
        with patch.object(dm, "tool", lambda name: f"/bin/{name}"), patch.object(dm, "run", self.fake_run()):
            devices = self.manager().devices()
        self.assertEqual(devices, [{"udid": "00008130-001A2B3C4D5E6F70", "name": "Alex's iPhone",
                                    "model": "iPhone16,1", "modelName": "iPhone 15 Pro", "ios": "26.0.1",
                                    "trusted": True}])

    def test_an_untrusted_phone_is_reported_as_such(self):
        with patch.object(dm, "tool", lambda name: f"/bin/{name}"), patch.object(dm, "run", self.fake_run(False)):
            self.assertFalse(self.manager().devices()[0]["trusted"])

    def test_build_needs_a_valid_team_and_a_phone(self):
        manager = self.manager()
        with self.assertRaises(ValueError):
            manager.start_build("nope")
        with patch.object(manager, "device", return_value=None), self.assertRaises(LookupError):
            manager.start_build("ABCDE12345")

    def test_runner_needs_a_build(self):
        manager = self.manager()
        with patch.object(manager, "device", return_value={"udid": "x"}), self.assertRaises(LookupError):
            manager.start_runner()


class ChosenPhoneTests(unittest.TestCase):
    PRO = {"udid": "00008130-001A2B3C4D5E6F70", "name": "Test Pro", "trusted": True}
    XR = {"udid": "00008020-000A1B2C3D4E5F60", "name": "Test XR", "trusted": True}

    def test_the_runner_never_moves_to_a_phone_that_was_not_chosen(self):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        manager.save_settings(udid=self.PRO["udid"], device_name="Test Pro")
        self.assertIsNone(manager.device([self.XR]))
        self.assertEqual(manager.device([self.XR, self.PRO]), self.PRO)

    def test_several_phones_need_a_choice_and_one_is_taken_as_is(self):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        self.assertIsNone(manager.device([self.PRO, self.XR]))
        self.assertEqual(manager.device([self.XR]), self.XR)
        with patch.object(manager, "devices", return_value=[self.PRO, self.XR]):
            manager.choose(self.XR["udid"])
            with self.assertRaises(LookupError):
                manager.choose("00008030-000000000000000E")
        self.assertEqual(manager.settings()["device_name"], "Test XR")

    def test_a_build_belongs_to_the_phone_it_was_made_for(self):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        products = manager.derived / "Build" / "Products"
        products.mkdir(parents=True)
        (products / "WebDriverAgentRunner.xctestrun").write_text("")
        manager.save_settings(udid=self.PRO["udid"], built_for=self.PRO["udid"])
        self.assertTrue(manager.built())
        manager.save_settings(udid=self.XR["udid"])
        self.assertFalse(manager.built())


class ReconnectTests(unittest.TestCase):
    def test_the_runner_resumes_when_the_chosen_phone_returns_but_not_after_a_stop(self):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        manager.save_settings(autostart=True)
        phone = {"udid": "00008130-001A2B3C4D5E6F70", "name": "Test Pro"}
        present = []
        with patch.object(manager, "built", return_value=True), patch.object(manager, "wda_alive", return_value=False), \
                patch.object(manager, "device", side_effect=lambda devices=None: present[0] if present else None), \
                patch.object(manager, "_start_runner") as start:
            manager.autostart()
            start.assert_not_called()
            present.append(phone)
            manager.autostart()
            start.assert_called_once()
            manager.save_settings(autostart=False)
            manager.autostart()
            start.assert_called_once()

    def test_fast_failures_back_off(self):
        log = Path(tempfile.mkdtemp()) / "s.log"
        waits = []
        service = dm.Supervised("t", ["/bin/sh", "-c", "exit 1"], log)
        original = service.stopping.wait

        def record(timeout=None):
            if timeout is not None and timeout >= dm.RESTART_DELAY:
                waits.append(timeout)
                if len(waits) >= 5:
                    service.stopping.set()
                return original(0)
            return original(timeout)

        service.stopping.wait = record
        service._loop()
        self.assertEqual(waits, [2.0, 4.0, 8.0, 16.0, 30.0])


class AdoptionTests(unittest.TestCase):
    def test_a_busy_runner_is_adopted_not_replaced(self):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        answers = [False, False, True]
        with patch.object(manager, "wda_ready", side_effect=lambda timeout=1.5: answers.pop(0)), \
                patch.object(dm.time, "sleep"):
            self.assertTrue(manager.wda_alive())
        with patch.object(manager, "wda_ready", return_value=False), patch.object(dm.time, "sleep"):
            self.assertFalse(manager.wda_alive())


class HealthProbeTests(unittest.TestCase):
    def manager(self, answers):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        manager.runner = SimpleNamespace(running=True)

        def urlopen(url, timeout):
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            import io, json
            return io.BytesIO(json.dumps({"value": answer}).encode())
        return manager, urlopen

    def test_locked_and_wedged_phones_are_named_and_a_long_wedge_restarts_the_runner(self):
        manager, urlopen = self.manager([True, TimeoutError(), TimeoutError(), TimeoutError(), False])
        with patch.object(manager, "wda_ready", return_value=True), patch.object(dm.urllib.request, "urlopen", urlopen), \
                patch.object(manager, "start_runner") as restart:
            manager.probe(now=100)
            self.assertIn("locked", manager.runner_state()["error"])
            manager.probe(now=105)                       # too soon: not probed again
            manager.probe(now=110)
            self.assertIn("isn't responding", manager.runner_state()["error"])
            manager.probe(now=150)
            restart.assert_not_called()                  # wedged 40 s: not yet
            manager.probe(now=200)
            restart.assert_called_once()                 # wedged 90 s: restarted
            manager.probe(now=210)
            self.assertIsNone(manager.runner_state()["error"])


class RunnerHintTests(unittest.TestCase):
    def test_a_locked_phone_is_named_and_a_later_success_clears_it(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = dm.DeviceManager(directory)
            manager.logs.mkdir(parents=True)
            log = manager.logs / "wda-runner.log"
            log.write_text("old run: Turn on Developer Mode\n")
            manager.runner_log_start = log.stat().st_size
            self.assertIsNone(manager.runner_hint())
            with open(log, "a") as stream:
                stream.write('Error "Unlock Test Pro to Continue" Xcode cannot launch it because the device is locked.\n')
            self.assertIn("Unlock your iPhone", manager.runner_hint())
            with open(log, "a") as stream:
                stream.write("ServerURLHere->http://192.0.2.2:8100<-ServerURLHere\n")
            self.assertIsNone(manager.runner_hint())


class SupervisedTests(unittest.TestCase):
    def test_restarts_and_stops_the_whole_process_group(self):
        log = Path(tempfile.mkdtemp()) / "s.log"
        with patch.object(dm, "RESTART_DELAY", .05):
            service = dm.Supervised("t", ["/bin/sh", "-c", "echo up; exit 3"], log)
            service.start()
            deadline = time.monotonic() + 5
            while service.restarts < 2 and time.monotonic() < deadline:
                time.sleep(.05)
            service.stop()
        self.assertGreaterEqual(service.restarts, 2)
        self.assertFalse(service.running)
        long_running = dm.Supervised("t", ["/bin/sh", "-c", "sleep 30 & wait"], log)
        long_running.start()
        time.sleep(.3)
        pid = long_running.process.pid
        long_running.stop()
        with self.assertRaises(ProcessLookupError):
            os.killpg(pid, 0)


class LoopbackTests(unittest.TestCase):
    UDID = "00008130-001A2B3C4D5E6F70"

    def iproxy(self, help_text):
        path = Path(tempfile.mkdtemp()) / "iproxy"
        path.write_text(f"#!/bin/sh\necho '{help_text}'\n")
        path.chmod(0o755)
        return str(path)

    def test_the_relay_listens_on_loopback_only_or_not_at_all(self):
        modern = self.iproxy("  -s, --source ADDR  source address for listening socket (default 127.0.0.1)")
        with patch.object(dm, "tool", lambda name: modern):
            self.assertEqual(dm.relay_command(self.UDID)[:5], [modern, "-s", "127.0.0.1", "-u", self.UDID])
        old = self.iproxy("usage: iproxy LOCAL_TCP_PORT DEVICE_TCP_PORT [UDID]")
        with patch.object(dm, "tool", lambda name: old), self.assertRaisesRegex(LookupError, "Update"):
            dm.relay_command(self.UDID)
        with patch.object(dm, "tool", lambda name: None), self.assertRaises(LookupError):
            dm.relay_command(self.UDID)

    def test_the_runner_and_relay_start_bound_to_loopback(self):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        manager.save_settings(team="ABCDE12345")
        started = []
        modern = self.iproxy("--source ADDR")
        with patch.object(manager, "device", return_value={"udid": self.UDID}), \
                patch.object(manager, "built", return_value=True), patch.object(dm, "tool", lambda name: modern), \
                patch.object(dm.Supervised, "start", lambda service: started.append(service.command)):
            manager.start_runner()
        relay, runner = started
        self.assertIn("USE_IP=127.0.0.1", runner)
        self.assertEqual(relay[1:3], ["-s", "127.0.0.1"])

    def test_an_old_iproxy_leaves_a_running_runner_alone(self):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        manager.save_settings(team="ABCDE12345")
        old = self.iproxy("usage: iproxy LOCAL_TCP_PORT DEVICE_TCP_PORT [UDID]")
        with patch.object(manager, "device", return_value={"udid": self.UDID}), \
                patch.object(manager, "built", return_value=True), patch.object(dm, "tool", lambda name: old), \
                patch.object(manager, "stop_runner") as stop, self.assertRaises(LookupError):
            manager.start_runner()
        stop.assert_not_called()

    def test_existing_builds_are_pinned_to_loopback(self):
        import plistlib
        products = Path(tempfile.mkdtemp())
        plan = {"__xctestrun_metadata__": {"FormatVersion": 1},
                "WebDriverAgentRunner": {"EnvironmentVariables": {"USE_IP": "", "MJPEG_SERVER_PORT": ""}}}
        (products / "Runner.xctestrun").write_bytes(plistlib.dumps(plan))
        dm.pin_loopback(products)
        saved = plistlib.loads((products / "Runner.xctestrun").read_bytes())
        self.assertEqual(saved["WebDriverAgentRunner"]["EnvironmentVariables"],
                         {"USE_IP": "127.0.0.1", "MJPEG_SERVER_PORT": ""})
        self.assertEqual(saved["__xctestrun_metadata__"], {"FormatVersion": 1})

    def test_wda_patches_apply_once_and_refuse_an_unknown_source(self):
        project = Path(tempfile.mkdtemp())
        for name, anchor, _ in dm.WDA_PATCHES:
            path = project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text((path.read_text() if path.exists() else "") + "// before\n" + anchor + "// after\n")
        dm.patch_wda(project)
        once = {name: (project / name).read_text() for name, _, _ in dm.WDA_PATCHES}
        dm.patch_wda(project)
        self.assertEqual(once, {name: (project / name).read_text() for name, _, _ in dm.WDA_PATCHES})
        self.assertIn("screenshotsBroadcaster.interface", once["WebDriverAgentLib/Routing/FBWebServer.m"])
        self.assertIn('requestHeaders[@"origin"]', once["WebDriverAgentLib/Routing/FBHTTPServer.m"])
        self.assertIn("FBMobsterLocalStreamRequest(data)", once["WebDriverAgentLib/Utilities/FBMjpegServer.m"])
        (project / dm.WDA_PATCHES[0][0]).write_text("// another version\n")
        with self.assertRaisesRegex(RuntimeError, "can't be limited to USB"):
            dm.patch_wda(project)

    def test_the_app_data_folder_settings_and_logs_are_user_only(self):
        root = Path(tempfile.mkdtemp())
        data = root / "app"
        data.mkdir(mode=0o755)
        manager = dm.DeviceManager(data)
        self.assertEqual(stat.S_IMODE(data.stat().st_mode), 0o700)
        manager.save_settings(team="ABCDE12345")
        log = data / "logs" / "usb-relay.log"
        log.parent.mkdir(mode=0o755)
        log.write_text("old\n")
        log.chmod(0o644)
        dm.private_dir(log.parent)
        with dm.open_private(log, "ab") as stream:
            stream.write(b"new\n")
        mode = lambda path: stat.S_IMODE(path.stat().st_mode)
        self.assertEqual((mode(data), mode(manager.settings_path), mode(log.parent), mode(log)),
                         (0o700, 0o600, 0o700, 0o600))
        self.assertEqual(log.read_text(), "old\nnew\n")
        self.assertEqual(manager.settings()["team"], "ABCDE12345")

    @unittest.skipUnless(dm.tool("iproxy") and dm.iproxy_binds_loopback(dm.tool("iproxy")), "needs iproxy with -s")
    def test_the_real_iproxy_binds_only_loopback(self):
        import socket
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        command = dm.relay_command("0000")[:-2] + [f"{port}:8100"]
        process = dm.subprocess.Popen(command, stdout=dm.subprocess.DEVNULL, stderr=dm.subprocess.DEVNULL)
        self.addCleanup(process.wait)
        self.addCleanup(process.terminate)
        deadline, listening = time.monotonic() + 5, ""
        while "LISTEN" not in listening and time.monotonic() < deadline:
            time.sleep(.1)
            listening = dm.run(["/usr/sbin/lsof", "-nP", "-a", "-p", str(process.pid), "-iTCP", "-sTCP:LISTEN"]) or ""
        self.assertIn(f"127.0.0.1:{port} (LISTEN)", listening)
        self.assertNotIn(f"*:{port}", listening)


def xcode_state(state="ok", selected="/Applications/Xcode.app/Contents/Developer", fixes=()):
    return {"state": state, "app": "/Applications/Xcode.app" if state != "missing" else None,
            "developer_dir": None if state == "missing" else "/Applications/Xcode.app/Contents/Developer",
            "selected": selected, "version": "Xcode 26.4" if state == "ok" else None,
            "problems": [] if state == "ok" else [state], "fixes": list(fixes)}


class ChecklistTests(unittest.TestCase):
    def service(self, *, key=True, tools=True, device=None, built=False, runner="stopped", live=False, frame=False,
                devices=None, settings=None, xcode=None, expired=False, expires_at=None, build=None,
                developer_mode=None):
        devices = devices if devices is not None else [device] if device else []
        xcode = xcode or xcode_state("ok" if tools else "missing")
        paths = {name: ("/bin/x" if tools else None) for name in ("iproxy", "idevice_id", "ideviceinfo")}
        paths.update(xcodebuild="/bin/x" if xcode["state"] == "ok" else None,
                     git="/bin/x" if xcode["state"] != "missing" else None, xcode=xcode)
        manager = SimpleNamespace(
            tools=lambda: paths,
            devices=lambda: devices, device=lambda listed=None: device, settings=lambda: settings or {},
            built=lambda: built, teams=lambda: [{"id": "ABCDE12345", "name": "Test Person", "personal": True}],
            build=build or {"state": "idle", "error": None, "log_tail": []},
            runner_state=lambda: {"state": runner, "error": None},
            expired=lambda: expired, signature_expiry=lambda: expires_at)
        video = SimpleNamespace(status=lambda: {"capturedAt": 1 if frame else None})
        runtime = SimpleNamespace(config=SimpleNamespace(enable_live=live), video=video)
        env = {"TYPESAFE_API_KEY": "k"} if key else {}
        service = SetupService(runtime, manager, Path(tempfile.mkdtemp()) / "agent.env")
        service.developer_mode = lambda device: developer_mode
        return service, env

    def states(self, **kwargs):
        service, env = self.service(**kwargs)
        with patch.dict(os.environ, env, clear=True), patch.object(dm, "executable", return_value="/opt/homebrew/bin/brew"):
            state = service.state()
        return state, {step["id"]: step["state"] for step in state["steps"]}

    def step(self, state, id_):
        return next(step for step in state["steps"] if step["id"] == id_)

    def test_a_fresh_mac_blocks_later_steps(self):
        state, steps = self.states(key=False, tools=False)
        # Tools come first: the Xcode download takes longest.
        self.assertEqual(list(steps), ["tools", "api_key", "phone", "wda_build", "wda_running", "live", "video"])
        self.assertEqual(steps, {"api_key": "todo", "tools": "todo", "phone": "blocked", "wda_build": "blocked",
                                 "wda_running": "blocked", "live": "todo", "video": "blocked"})
        waits = {step["id"]: step["blocked_by"] for step in state["steps"]}
        self.assertEqual(waits, {"api_key": None, "tools": None, "phone": "tools", "wda_build": "phone",
                                 "wda_running": "phone", "live": None, "video": "wda_running"})
        tools = self.step(state, "tools")
        self.assertEqual(tools["commands"], ["brew install libimobiledevice"])
        self.assertIn("Install Xcode from the Mac App Store", tools["detail"])
        self.assertEqual(state["tools"], {"xcode": {"state": "missing", "version": None, "app": None},
                                          "libimobiledevice": False, "homebrew": True})

    def test_the_command_line_tools_are_not_xcode_and_the_step_says_how_to_switch(self):
        xcode = xcode_state("not_selected", selected=dm.CLT_DIR, fixes=["sudo xcode-select -s /Applications/Xcode.app"])
        state, steps = self.states(xcode=xcode)
        tools = self.step(state, "tools")
        self.assertEqual((steps["tools"], steps["phone"]), ("todo", "blocked"))
        self.assertIn("set to use the Command Line Tools", tools["detail"])
        self.assertEqual(tools["commands"], ["sudo xcode-select -s /Applications/Xcode.app"])

    def test_each_xcode_problem_has_its_own_advice(self):
        for problem, words in (("license", "Accept the Xcode license"), ("first_launch", "Open it once"),
                               ("no_ios", "Settings › Components"), ("broken", "isn't responding")):
            state, steps = self.states(xcode=xcode_state(problem))
            self.assertEqual(steps["tools"], "todo")
            self.assertIn(words, self.step(state, "tools")["detail"])
        state, _ = self.states()
        self.assertEqual(self.step(state, "tools")["detail"], "Xcode 26.4 and libimobiledevice are installed.")

    def test_homebrew_is_offered_first_when_it_is_missing(self):
        service, env = self.service(tools=False, xcode=xcode_state("ok"))
        with patch.dict(os.environ, env, clear=True), patch.object(dm, "executable", return_value=None):
            state = service.state()
        tools = self.step(state, "tools")
        self.assertEqual(tools["commands"][1], "brew install libimobiledevice")
        self.assertIn("Homebrew/install", tools["commands"][0])
        self.assertIn("Install Homebrew, then libimobiledevice", tools["detail"])

    def test_an_expired_runner_asks_for_a_rebuild(self):
        device = {"udid": "u", "name": "iPhone", "trusted": True}
        state, steps = self.states(device=device, built=False, expired=True, expires_at=1_000,
                                   settings={"udid": "u", "built_for": "u"})
        build = self.step(state, "wda_build")
        self.assertEqual(steps["wda_build"], "todo")
        self.assertIn("signature expired", build["detail"])
        self.assertIn("7 days", build["detail"])
        self.assertEqual((state["build"]["expired"], state["build"]["expires_at"]), (True, 1_000_000))
        self.assertEqual(steps["wda_running"], "blocked")

    def test_a_running_build_gives_a_realistic_estimate_and_its_start(self):
        device = {"udid": "u", "name": "iPhone", "trusted": True}
        running = {"state": "running", "error": None, "log_tail": [], "started_at": 100.5, "first": True}
        state, steps = self.states(device=device, build=running)
        self.assertEqual(steps["wda_build"], "working")
        self.assertIn("10 minutes or more", self.step(state, "wda_build")["detail"])
        self.assertEqual((state["build"]["started_at"], state["build"]["first"]), (100_500, True))
        state, _ = self.states(device=device, build={**running, "first": False})
        self.assertIn("quicker than the first build", self.step(state, "wda_build")["detail"])

    def test_the_phone_reports_developer_mode_when_it_can(self):
        device = {"udid": "u", "name": "iPhone", "trusted": True}
        state, _ = self.states(device=device, developer_mode=False)
        self.assertIs(state["device"]["developerMode"], False)
        self.assertEqual(state["teams"], [{"id": "ABCDE12345", "name": "Test Person", "personal": True}])

    def test_developer_mode_is_read_from_the_trusted_phone_and_an_off_answer_is_re_read(self):
        service, _ = self.service()
        del service.developer_mode  # the real method, not the fixture's
        answers = ["false\n", "true\n"]
        with patch.object(dm, "tool", return_value="/bin/ideviceinfo"), \
                patch.object(dm, "run", side_effect=lambda command, timeout=10: answers.pop(0)) as run:
            phone = {"udid": "00008130-001A2B3C4D5E6F70", "trusted": True}
            self.assertIs(service.developer_mode(phone), False)
            self.assertIn("DeveloperModeStatus", run.call_args[0][0])
            self.assertIs(service.developer_mode(phone), False)      # cached for a few seconds
            service.refresh()
            self.assertIs(service.developer_mode(phone), True)
            self.assertIsNone(service.developer_mode({"udid": "x", "trusted": False}))

    def test_the_phone_step_asks_for_a_choice_or_names_the_missing_phone(self):
        pro, xr = {"udid": "p", "name": "Test Pro", "trusted": True}, {"udid": "x", "name": "Test XR", "trusted": True}
        state, steps = self.states(devices=[pro, xr])
        phone = state["steps"][2]
        self.assertEqual((steps["phone"], phone["action"]), ("todo", "choose_phone"))
        self.assertIn("Choose which iPhone", phone["detail"])
        self.assertEqual(len(state["devices"]), 2)
        state, _ = self.states(devices=[xr], settings={"udid": "p", "device_name": "Test Pro"})
        self.assertIn("“Test Pro” isn't connected", state["steps"][2]["detail"])
        state, _ = self.states(device=pro, settings={"udid": "p", "built_for": "x"})
        self.assertIn("built for another iPhone", state["steps"][3]["detail"])

    def test_a_fully_set_up_mac_is_complete(self):
        device = {"udid": "u", "name": "iPhone", "trusted": True}
        state, steps = self.states(device=device, built=True, runner="running", live=True, frame=True)
        self.assertTrue(state["complete"])
        self.assertTrue(all(value == "done" for value in steps.values()))

    def test_saving_a_key_and_live_persist_privately(self):
        service, _ = self.service()
        with patch.dict(os.environ, {}, clear=True):
            service.save_key("apikey_TESTONLY_0000000000000000000000")
            self.assertEqual(os.environ["TYPESAFE_API_KEY"], "apikey_TESTONLY_0000000000000000000000")
        service.set_live(True)
        self.assertTrue(service.runtime.config.enable_live)
        self.assertIn("MOBSTER_ENABLE_LIVE=1", service.env_file.read_text())
        with self.assertRaises(ValueError):
            service.save_key("short")


FAKE_XCODEBUILD = """#!/bin/sh
case "$1" in
  -version) if [ -n "$NO_LICENSE" ]; then echo "You have not agreed to the Xcode license agreements." >&2; exit 69; fi
            echo "Xcode 26.4"; echo "Build version 17E192";;
  -checkFirstLaunchStatus) [ -z "$FIRST_LAUNCH" ];;
  -showsdks) echo "macOS SDKs:"; [ -z "$NO_IOS" ] && echo "	iOS 26.4	-sdk iphoneos26.4"; exit 0;;
esac
"""


class XcodeDetectionTests(unittest.TestCase):
    """The /usr/bin stubs never count as Xcode or git; Xcode must be an app that answers."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.developer = self.root / "Xcode.app" / "Contents" / "Developer"

    def install_xcode(self):
        bin_dir = self.developer / "usr" / "bin"
        bin_dir.mkdir(parents=True)
        for name, text in (("xcodebuild", FAKE_XCODEBUILD), ("git", "#!/bin/sh\n")):
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)

    def status(self, selected, **env):
        with patch.object(dm, "XCODE_APP_DIRS", (str(self.root),)), \
                patch.object(dm, "run", return_value=f"{selected}\n" if selected else None), \
                patch.dict(os.environ, env):
            return dm.xcode_status()

    def test_the_usr_bin_stubs_are_never_xcode_or_git(self):
        with patch.object(dm, "developer_dir", return_value=(None, dm.CLT_DIR)), patch.object(dm, "GIT_DIRS", ()):
            self.assertIsNone(dm.tool("xcodebuild"))
            self.assertIsNone(dm.tool("git"))
        self.assertFalse(dm.is_xcode("/usr"))
        self.assertFalse(dm.is_xcode(dm.CLT_DIR))

    def test_no_xcode_app_is_missing_even_with_the_command_line_tools(self):
        status = self.status(dm.CLT_DIR)
        self.assertEqual((status["state"], status["fixes"]), ("missing", []))

    def test_the_command_line_tools_selected_gives_the_exact_switch_command(self):
        self.install_xcode()
        status = self.status(dm.CLT_DIR)
        self.assertEqual(status["state"], "not_selected")
        self.assertEqual(status["fixes"], [f"sudo xcode-select -s {self.root / 'Xcode.app'}"])
        self.assertEqual(status["version"], "Xcode 26.4")
        self.assertEqual(status["developer_dir"], str(self.developer))
        # Built with Xcode's own tools, never /usr/bin's.
        with patch.object(dm, "XCODE_APP_DIRS", (str(self.root),)), patch.object(dm, "run", return_value=dm.CLT_DIR):
            self.assertEqual(dm.tool("xcodebuild"), str(self.developer / "usr" / "bin" / "xcodebuild"))
            self.assertEqual(dm.tool("git"), str(self.developer / "usr" / "bin" / "git"))

    def test_a_selected_working_xcode_is_ok(self):
        self.install_xcode()
        status = self.status(str(self.developer))
        self.assertEqual((status["state"], status["problems"], status["app"]), ("ok", [], str(self.root / "Xcode.app")))

    def test_license_first_launch_and_ios_platform_are_detected(self):
        self.install_xcode()
        cases = ((dict(NO_LICENSE="1"), "license", "sudo xcodebuild -license accept"),
                 (dict(FIRST_LAUNCH="1"), "first_launch", "sudo xcodebuild -runFirstLaunch"),
                 (dict(NO_IOS="1"), "no_ios", "xcodebuild -downloadPlatform iOS"))
        for env, state, fix in cases:
            status = self.status(str(self.developer), **env)
            self.assertEqual((status["state"], status["fixes"]), (state, [fix]))
        # Several problems: every fix, in the order to run them.
        status = self.status(dm.CLT_DIR, NO_LICENSE="1")
        self.assertEqual(status["problems"], ["not_selected", "license"])
        self.assertEqual(status["fixes"][1], "sudo xcodebuild -license accept")

    def test_tools_are_cached_until_a_refresh(self):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        with patch.object(dm, "xcode_status", return_value=xcode_state("missing")) as status, \
                patch.object(dm, "tool", return_value=None):
            self.assertEqual(manager.tools()["xcode"]["state"], "missing")
            manager.tools()
            self.assertEqual(status.call_count, 1)
            manager.refresh()
            manager.tools()
            self.assertEqual(status.call_count, 2)
            self.assertIsNone(manager.tools()["xcodebuild"])

    def test_a_build_is_refused_until_xcode_works(self):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        with patch.object(manager, "device", return_value={"udid": "u", "name": "iPhone"}), \
                patch.object(manager, "tools", return_value={"xcode": xcode_state("not_selected"), "git": "/x/git"}):
            with self.assertRaises(LookupError) as refused:
                manager.start_build("ABCDE12345")
        self.assertIn("Finish installing Xcode", str(refused.exception))

    def test_xcodebuild_failures_about_the_developer_directory_say_what_to_run(self):
        hint = dm.build_hint(["xcode-select: error: tool 'xcodebuild' requires Xcode, but active developer directory "
                              "'/Library/Developer/CommandLineTools' is a command line tools instance"])
        self.assertIn("sudo xcode-select -s /Applications/Xcode.app", hint)
        self.assertIn("sudo xcodebuild -license accept",
                      dm.build_hint(["You have not agreed to the Xcode license agreements."]))
        self.assertIn("internet connection", dm.build_hint(["fatal: unable to access: Could not resolve host: github.com"]))


class SigningTests(unittest.TestCase):
    def test_teams_signed_in_to_xcode_are_found_without_a_certificate(self):
        from mobile_agent import signing
        defaults = {"IDEProvisioningTeamByIdentifier": {"account-1": [
            {"teamID": "ABCDE12345", "teamName": "Test Person", "teamType": "Personal Team", "isFreeProvisioningTeam": True},
            {"teamID": "not-a-team", "teamName": "x"}]},
            "IDEProvisioningTeams": {"old": [{"teamID": "ABCDE12345"}, {"teamID": "ZYXWV98765", "teamName": "Example Co"}]}}
        self.assertEqual(signing.account_teams(defaults), [
            {"id": "ABCDE12345", "name": "Test Person", "personal": True},
            {"id": "ZYXWV98765", "name": "Example Co", "personal": False}])
        self.assertEqual(signing.account_teams({}), [])

    def test_certificate_subjects_give_the_team_id_and_name(self):
        from mobile_agent import signing
        multiline = ("subject=\n    userId                    = XXXXXXXXXX\n"
                     "    commonName                = Apple Development: test@example.com (QQQQQQQQQQ)\n"
                     "    organizationalUnitName    = ABCDE12345\n    organizationName          = Example, LLC\n")
        self.assertEqual(signing.subject_team(multiline), ("ABCDE12345", "Example, LLC"))
        self.assertEqual(signing.subject_team("subject=UID=X, CN=Apple Development: a (QQQQQQQQQQ), OU=ABCDE12345, O=B"),
                         ("ABCDE12345", None))
        self.assertEqual(signing.subject_team(""), (None, None))
        merged = signing.merge_teams([{"id": "ABCDE12345", "name": None, "personal": True}],
                                     [{"id": "ABCDE12345", "name": "Test Person", "personal": False},
                                      {"id": "ZYXWV98765", "name": "Example", "personal": False}])
        self.assertEqual(merged, [{"id": "ABCDE12345", "name": "Test Person", "personal": True},
                                  {"id": "ZYXWV98765", "name": "Example", "personal": False}])

    def test_the_profile_expiry_is_read_from_the_built_runner(self):
        import plistlib
        from datetime import datetime
        from mobile_agent import signing
        derived = Path(tempfile.mkdtemp())
        app = derived / "Build" / "Products" / "Debug-iphoneos" / "WebDriverAgentRunner-Runner.app"
        app.mkdir(parents=True)
        (app / "embedded.mobileprovision").write_bytes(b"signed")
        profile = plistlib.dumps({"ExpirationDate": datetime(2026, 10, 2, 12, 0, 0)}).decode()
        with patch.object(signing, "_read", return_value=profile) as read:
            self.assertEqual(signing.profile_expiry(derived), 1790942400.0)
        self.assertEqual(read.call_args[0][0][:4], ["security", "cms", "-D", "-i"])
        with patch.object(signing, "_read", return_value=None):
            self.assertIsNone(signing.profile_expiry(derived))
        self.assertIsNone(signing.profile_expiry(Path(tempfile.mkdtemp())))


class ExpiryTests(unittest.TestCase):
    def manager(self, expires_at):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        products = manager.derived / "Build" / "Products"
        products.mkdir(parents=True)
        (products / "WebDriverAgentRunner.xctestrun").write_text("")
        manager.save_settings(udid="u", built_for="u", team="ABCDE12345", expires_at=expires_at)
        return manager

    def test_an_expired_signature_is_not_built_and_the_runner_says_rebuild(self):
        manager = self.manager(time.time() - 60)
        self.assertTrue(manager.expired())
        self.assertFalse(manager.built())
        self.assertEqual(manager.runner_hint(), dm.EXPIRED_HINT)
        with patch.object(manager, "device", return_value={"udid": "u"}), self.assertRaises(LookupError) as refused:
            manager.start_runner()
        self.assertEqual(str(refused.exception), dm.EXPIRED_HINT)
        # The watcher stops relaunching a runner iOS will refuse.
        manager.save_settings(autostart=True)
        with patch.object(manager, "device", return_value={"udid": "u"}), patch.object(manager, "_start_runner") as start:
            manager.autostart()
        start.assert_not_called()

    def test_a_signature_in_date_is_built(self):
        manager = self.manager(time.time() + 86_400)
        self.assertFalse(manager.expired())
        self.assertTrue(manager.built())
        self.assertTrue(manager.expired(now=time.time() + 2 * 86_400))

    def test_an_older_build_reads_its_profile_once(self):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        with patch.object(dm.signing, "profile_expiry", return_value=5.0) as read:
            self.assertIsNone(manager.signature_expiry())        # nothing built: nothing to read
            profile = manager.derived / "Build" / "Products" / "Debug-iphoneos" / "WebDriverAgentRunner-Runner.app"
            profile.mkdir(parents=True)
            (profile / "embedded.mobileprovision").write_bytes(b"x")
            self.assertEqual(manager.signature_expiry(), 5.0)
            self.assertEqual(manager.signature_expiry(), 5.0)
        read.assert_called_once()


if __name__ == "__main__":
    unittest.main()


class UsbIdentityTests(unittest.TestCase):
    def test_usb_phone_identity_has_a_marketing_name_and_no_identifiers(self):
        from mobile_agent.device_info import DeviceInfo, usb_info
        info = usb_info({"udid": "00008130-SECRET", "model": "iPhone16,1", "ios": "26.0.1", "trusted": True})
        self.assertEqual((info["modelName"], info["iosVersion"], info["virtual"]), ("iPhone 15 Pro", "26.0.1", False))
        self.assertNotIn("00008130-SECRET", str(info))
        manager = SimpleNamespace(device=lambda: {"model": "iPhone18,4", "ios": "26.1", "trusted": True})
        self.assertEqual(DeviceInfo(manager=manager).snapshot()["modelName"], "iPhone Air")

    def test_unknown_or_malformed_models_are_never_guessed(self):
        from mobile_agent.device_info import usb_info
        self.assertIsNone(usb_info({"model": "iPhone99,9", "ios": "26.0"})["modelName"])
        self.assertIsNone(usb_info({"model": "rm -rf", "ios": "x"})["modelIdentifier"])
