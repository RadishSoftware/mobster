import io
import json
import os
import shlex
import stat
import subprocess
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent import device_manager as dm
from mobile_agent import env_file, env_secrets, mac_tools, runner_hints, setup_errors, setup_service, supervised
from mobile_agent.setup_service import SetupService, ToolsInstall, install_words
from mobile_agent.wda import patches as wda_patches


class EnvFileTests(unittest.TestCase):
    def test_private_writers_refuse_symlinks(self):
        from mobile_agent.paths import write_private
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.write_text("keep")
            link = root / "linked"
            link.symlink_to(target)
            with self.assertRaises(OSError):
                env_file.open_private(link, "w")
            with self.assertRaises(OSError):
                write_private(link, "private")
            self.assertEqual(target.read_text(), "keep")

    def test_values_are_replaced_and_the_file_is_private(self):
        path = Path(tempfile.mkdtemp()) / "agent.env"
        path.write_text("OTHER=1\nTYPESAFE_API_KEY=old\n")
        env_secrets.update_env_file(path, {"TYPESAFE_API_KEY": "new"})
        self.assertEqual(path.read_text(), "OTHER=1\nTYPESAFE_API_KEY=new\n")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_writers_take_turns_so_every_key_lands(self):
        path = Path(tempfile.mkdtemp()) / "agent.env"
        keys = [f"KEY_{n}" for n in range(24)]
        start = threading.Barrier(len(keys))

        def write(key):
            start.wait()
            for _ in range(5):
                env_secrets.update_env_file(path, {key: "1"})
        threads = [threading.Thread(target=write, args=(key,)) for key in keys]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(line.split("=")[0] for line in path.read_text().splitlines()), sorted(keys))
        self.assertEqual(stat.S_IMODE(path.with_name("agent.env.lock").stat().st_mode), 0o600)

    def test_the_lock_file_refuses_a_symlink(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "elsewhere").write_text("keep")
        (folder / "agent.env.lock").symlink_to(folder / "elsewhere")
        with self.assertRaises(OSError):
            env_secrets.update_env_file(folder / "agent.env", {"KEY": "1"})
        self.assertEqual((folder / "elsewhere").read_text(), "keep")

    def test_newlines_cannot_inject_entries(self):
        with self.assertRaises(ValueError):
            env_secrets.update_env_file(Path(tempfile.mkdtemp()) / "e", {"KEY": "a\nEVIL=1"})

    def test_a_line_that_is_not_utf8_is_kept_and_the_key_still_saves(self):
        path = Path(tempfile.mkdtemp()) / "agent.env"
        path.write_bytes(b"TEXT_MODEL=caf\xe9\n")
        env_secrets.update_env_file(path, {"TYPESAFE_API_KEY": "x"})
        self.assertEqual(path.read_bytes(), b"TEXT_MODEL=caf\xe9\nTYPESAFE_API_KEY=x\n")
        env_secrets.update_env_file(path, {"TYPESAFE_API_KEY": None})
        self.assertEqual(path.read_bytes(), b"TEXT_MODEL=caf\xe9\n")
        with self.assertRaises(ValueError):
            env_secrets.update_env_file(path, {"KEY": "caf\udce9"})
        path.write_bytes(b"\xef\xbb\xbfTYPESAFE_API_KEY=old\n")
        env_secrets.update_env_file(path, {"TYPESAFE_API_KEY": "new"})
        self.assertEqual(path.read_bytes(), b"TYPESAFE_API_KEY=new\n")


class DeviceTests(unittest.TestCase):
    def manager(self):
        return dm.DeviceManager(tempfile.mkdtemp())

    def fake_run(self, trusted=True):
        def run(command, timeout=10):
            if command[-1] == "-l":
                return "0000FE01-AAAAAAAAAAAAAAAA\n00008130-001A2B3C4D5E6F70\n00008130-001A2B3C4D5E6F70\n"
            if not trusted:
                return None
            return {"DeviceName": "Alex's iPhone\n", "ProductType": "iPhone16,1\n", "ProductVersion": "26.0.1\n",
                    "DeviceEnclosureColor": "9\n"}[command[-1]]
        return run

    def test_physical_phones_are_listed_once_with_trust(self):
        with patch.object(mac_tools, "tool", lambda name: f"/bin/{name}"), patch.object(mac_tools, "run",
                                                                                        self.fake_run()):
            devices = self.manager().devices()
        self.assertEqual(devices, [{"udid": "00008130-001A2B3C4D5E6F70", "name": "Alex's iPhone",
                                    "model": "iPhone16,1", "modelName": "iPhone 15 Pro", "ios": "26.0.1",
                                    "enclosure": "9", "trusted": True}])

    def test_an_untrusted_phone_is_reported_as_such(self):
        with patch.object(mac_tools, "tool", lambda name: f"/bin/{name}"), patch.object(mac_tools, "run",
                                                                                        self.fake_run(False)):
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
        manager.save_settings(udid=self.PRO["udid"], built_for=self.PRO["udid"],
                              built_patches=wda_patches.patch_digest())
        self.assertTrue(manager.built())
        manager.save_settings(udid=self.XR["udid"])
        self.assertFalse(manager.built())


class ReconnectTests(unittest.TestCase):
    def test_the_runner_resumes_when_the_chosen_phone_returns_but_not_after_a_stop(self):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        manager.save_settings(autostart=True)
        phone = {"udid": "00008130-001A2B3C4D5E6F70", "name": "Test Pro"}
        present = []
        with patch.object(manager, "built", return_value=True), patch.object(manager, "wda_alive",
                                                                             return_value=False), \
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

    def test_a_finished_install_starts_the_helper_without_any_window_open(self):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        phone = {"udid": "00008130-001A2B3C4D5E6F70", "name": "Test Pro"}
        manager.save_settings(udid=phone["udid"])

        def fake_step(command, log, tail):
            if "build-for-testing" in command:
                products = manager.derived / "Build" / "Products"
                products.mkdir(parents=True, exist_ok=True)
                (products / "WebDriverAgentRunner.xctestrun").write_text("")
        manager.project.mkdir(parents=True)
        manager.logs.mkdir(parents=True)
        with patch.object(manager, "_step", fake_step), patch.object(dm.wda_source, "is_pinned", return_value=True), \
                patch.object(manager, "_rebrand"), patch.object(manager, "_drop_stale_profiles"), \
                patch.object(dm.wda_patches, "patch_wda"), patch.object(manager, "_remove_legacy_runner"), \
                patch.object(dm.signing, "profile_expiry", return_value=None):
            manager._build_locked("ABCDE12345", phone["udid"], manager.logs / "wda-build.log", [])
        self.assertEqual(manager.build["state"], "succeeded")
        self.assertTrue(manager.settings()["autostart"])
        with patch.object(manager, "wda_ready", return_value=False):
            self.assertEqual(manager.runner_state(), {"state": "starting", "error": None})
        with patch.object(manager, "wda_alive", return_value=False), \
                patch.object(manager, "device", return_value=phone), patch.object(manager, "_start_runner") as start:
            manager.autostart()
        start.assert_called_once()

    def test_a_helper_stopped_by_hand_reads_stopped_and_stays_stopped(self):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        manager.save_settings(autostart=False)
        with patch.object(manager, "built", return_value=True), patch.object(manager, "wda_ready", return_value=False):
            self.assertEqual(manager.runner_state()["state"], "stopped")
        manager.save_settings(autostart=True)
        with patch.object(manager, "built", return_value=False), patch.object(manager, "wda_ready", return_value=False):
            self.assertEqual(manager.runner_state()["state"], "stopped")   # nothing built: nothing to start

    def test_fast_failures_back_off(self):
        log = Path(tempfile.mkdtemp()) / "s.log"
        waits = []
        service = supervised.Supervised("t", ["/bin/sh", "-c", "exit 1"], log)
        original = service.stopping.wait

        def record(timeout=None):
            if timeout is not None and timeout >= supervised.RESTART_DELAY:
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

        def urlopen(path, timeout):
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return {"value": answer}
        return manager, urlopen

    def test_locked_and_wedged_phones_are_named_and_a_long_wedge_restarts_the_runner(self):
        manager, urlopen = self.manager([True, TimeoutError(), TimeoutError(), TimeoutError(), False])
        with patch.object(manager, "wda_ready", return_value=True), patch.object(manager, "_wda_get", urlopen), \
                patch.object(manager, "start_runner") as restart:
            manager.probe(now=100)
            self.assertIn("locked", manager.runner_state()["error"])
            manager.probe(now=100 + dm.PROBE_INTERVAL / 2)  # too soon: not probed again
            manager.probe(now=110)
            self.assertIn("isn't responding", manager.runner_state()["error"])
            manager.probe(now=150)
            restart.assert_not_called()                  # wedged 40 s: not yet
            manager.probe(now=200)
            restart.assert_called_once()                 # wedged 90 s: restarted
            manager.probe(now=210)
            self.assertIsNone(manager.runner_state()["error"])


class AttachRearmTests(unittest.TestCase):
    CHOSEN, OTHER = "00008130-001A2B3C4D5E6F70", "00008020-000A1B2C3D4E5F60"

    def manager(self, plugged):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        manager.save_settings(udid=self.CHOSEN, team="ABCDE12345", autostart=True)
        manager.runner = SimpleNamespace(running=True)        # an xcodebuild still waiting for its destination
        self.plugged = plugged
        self.starts = []
        for item in (patch.object(mac_tools, "usb_attached", lambda udid: udid.upper() in self.plugged),
                     patch.object(manager, "built", return_value=True),
                     patch.object(manager, "expired", return_value=False),
                     patch.object(manager, "device", return_value={"udid": self.CHOSEN, "name": "alex"}),
                     patch.object(manager, "wda_ready", return_value=False),
                     patch.object(manager, "_start_runner", lambda: self.starts.append(1))):
            item.start()
            self.addCleanup(item.stop)
        return manager

    def test_plugging_the_chosen_phone_in_starts_the_helper_at_once_and_only_once(self):
        manager = self.manager(set())
        self.assertFalse(manager.rearm_on_attach())           # away: nothing to do, and the first look is only a look
        self.assertFalse(manager.rearm_on_attach())
        self.plugged.add(self.CHOSEN)
        self.assertTrue(manager.rearm_on_attach())            # the edge
        self.assertFalse(manager.rearm_on_attach())           # still plugged in: not again
        self.assertEqual(len(self.starts), 1)

    def test_another_phone_being_plugged_in_starts_nothing(self):
        manager = self.manager(set())
        manager.rearm_on_attach()
        self.plugged.add(self.OTHER)                          # a different iPhone: never this Mac's chosen one
        self.assertFalse(manager.rearm_on_attach())
        self.assertEqual(self.starts, [])

    def test_a_helper_that_already_answers_a_launch_with_the_phone_there_and_autostart_off_start_nothing(self):
        manager = self.manager({self.CHOSEN})
        # first look with it plugged in is autostart's, not an edge
        self.assertFalse(manager.rearm_on_attach())
        manager.save_settings(autostart=False)
        self.plugged.clear()
        manager.rearm_on_attach()
        self.plugged.add(self.CHOSEN)
        self.assertFalse(manager.rearm_on_attach())
        manager.save_settings(autostart=True)
        self.plugged.clear()
        manager.rearm_on_attach()
        self.plugged.add(self.CHOSEN)
        with patch.object(manager, "wda_ready", return_value=True):
            self.assertFalse(manager.rearm_on_attach())       # it answers: leave it alone
        self.assertEqual(self.starts, [])

    def test_it_runs_with_the_watch_round_and_a_wrong_phone_on_the_port_is_still_refused_before_a_task(self):
        manager = self.manager(set())
        manager.watch_round()
        self.plugged.add(self.CHOSEN)
        with patch.object(manager, "autostart"), patch.object(manager, "poll_presence"), \
                patch.object(manager, "check_exposure"), patch.object(manager, "retry_locked_build"):
            manager.watch_round()
        self.assertEqual(len(self.starts), 1)
        from mobile_agent.wda import identity as wda_identity
        with self.assertRaises(wda_identity.WrongDevice):
            wda_identity.verify("http://127.0.0.1:8100", name="alex", read=lambda url: {"name": "someone else"})


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
                stream.write('Error "Unlock Test Pro to Continue" Xcode cannot launch it because the '
                             'device is locked.\n')
            self.assertIn("Unlock your iPhone", manager.runner_hint())
            with open(log, "a") as stream:
                stream.write("ServerURLHere->http://192.0.2.2:8100<-ServerURLHere\n")
            self.assertIsNone(manager.runner_hint())

    def test_an_automation_mode_timeout_names_the_switch_to_turn_on(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = dm.DeviceManager(directory)
            manager.logs.mkdir(parents=True)
            log = manager.logs / "wda-runner.log"
            log.write_text("\tWebDriverAgentRunner-Runner (46090) encountered an error (The test runner failed to "
                           "initialize for UI testing. (Underlying Error: "
                           "Timed out while enabling automation mode.))\n")
            self.assertIn("Settings › Developer › Enable UI Automation", manager.runner_hint())
            self.assertEqual(manager.runner_problem()["kind"], "ui_automation")

    RUNNING = "2026-10-17 19:43:35.287224-0700 WebDriverAgentRunner-Runner[26205:2280688] [Default] Running tests...\n"
    SUITE = "Test Suite 'All tests' started at 2026-10-17 19:43:37.200.\n"

    def automation_log(self, directory, text):
        manager = dm.DeviceManager(directory)
        manager.logs.mkdir(parents=True)
        (manager.logs / "wda-runner.log").write_text(text)
        return manager, runner_hints.runner_stamp(self.RUNNING)

    def test_a_runner_still_waiting_for_automation_mode_is_named_before_ios_times_out(self):
        with tempfile.TemporaryDirectory() as directory:
            manager, said = self.automation_log(directory, self.RUNNING)
            self.assertIsNotNone(said)
            self.assertIsNone(manager.runner_hint(now=said + 3))          # a healthy start says more within ~2 s
            self.assertIn("Enable UI Automation", manager.runner_hint(now=said + runner_hints.AUTOMATION_WAIT_S))
            self.assertEqual(manager.runner_cause, "ui_automation_off")   # engine health names it now
            self.assertEqual(manager.runner_problem()["kind"], "ui_automation")

    def test_a_runner_that_got_past_automation_mode_or_failed_is_not_called_waiting(self):
        with tempfile.TemporaryDirectory() as directory:
            manager, said = self.automation_log(directory, self.RUNNING + self.SUITE)
            self.assertIsNone(manager.runner_hint(now=said + 120))       # it started: nothing to do
            self.assertIsNone(manager.runner_cause)
        with tempfile.TemporaryDirectory() as directory:
            manager, said = self.automation_log(directory, "Running tests... (no runner stamp)\n")
            self.assertIsNone(manager.runner_hint(now=time.time() + 120))   # an unreadable time names nothing
        with tempfile.TemporaryDirectory() as directory:
            manager, said = self.automation_log(directory, self.RUNNING + "ServerURLHere->http://127.0.0.1:8100<-"
                                                                          "ServerURLHere\n")
            self.assertIsNone(manager.runner_hint(now=said + 120))


class PrearmTests(unittest.TestCase):
    PHONE = "00008140-00000000ABCD000D"

    def manager(self, directory, ios="18.3.1"):
        manager = dm.DeviceManager(directory)
        manager.save_settings(udid=self.PHONE)
        if ios is not None:
            manager.cache[("info", self.PHONE)] = (time.monotonic(), {"name": "Test iPhone", "ios": ios})
        return manager

    def test_a_phone_that_never_ran_the_helper_is_asked_until_it_answers_then_never_again(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            self.assertEqual(manager.prearm(), "ui_automation")
            manager.runner = types.SimpleNamespace(running=True, restarts=0, last_exit=None, held=None, alive=True)
            state = manager.runner_state(ready=False)
            self.assertEqual(state["prearm"], {"kind": "ui_automation", "ask": runner_hints.PREARM_ASK})
            self.assertIn("Enable UI Automation", state["prearm"]["ask"])
            self.assertNotIn("prearm", manager.runner_state(ready=True))     # it answered: nothing to ask
            self.assertIsNone(manager.prearm())
            self.assertEqual(dm.DeviceManager(directory).settings()["automation_ok"], [self.PHONE])   # kept
            restarted = self.manager(directory)                               # a later launch of Mobster
            self.assertIsNone(restarted.prearm())

    def test_nothing_is_asked_on_an_ios_without_the_switch_or_when_the_phone_is_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(self.manager(directory, ios="16.7.10").prearm())
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(self.manager(directory, ios=None).prearm())    # iOS not read yet
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(dm.DeviceManager(directory).prearm())          # no phone chosen


class LockedStartTests(unittest.TestCase):
    def manager(self, directory):
        manager = dm.DeviceManager(directory)
        manager.save_settings(udid="00008130-000A1B2C3D4E5F60")
        return manager

    def test_the_runner_is_held_only_while_coredevice_says_locked(self):
        from mobile_agent.wireless import devicectl
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            for reading, held in ((True, "locked"), (False, None), (None, None)):
                with self.subTest(reading=reading), patch.object(devicectl, "read_lock_state", return_value=reading):
                    self.assertEqual(manager._runner_hold(), held)

    def test_a_held_runner_says_unlock_and_is_not_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            manager.runner = SimpleNamespace(running=True, alive=False, held="locked", restarts=5, last_exit=65)
            with patch.object(manager, "wda_ready", return_value=False):
                state = manager.runner_state()
                problem = manager.runner_problem()
            self.assertEqual((state["state"], state["waiting"]), ("starting", "unlock"))
            self.assertEqual(state["error"], runner_hints.LOCKED_START_HINT)
            self.assertEqual((problem["kind"], problem["action"]), ("locked", None))   # nothing to press
            from mobile_agent import devices
            with patch.object(manager, "built", return_value=True):
                reason = devices.usb_state({"udid": "u", "trusted": True}, manager, lambda: False, lambda: True)
            self.assertEqual(reason, ("connected", devices.REASONS["locked"]))
            manager.runner = SimpleNamespace(running=True, alive=False, held=None, restarts=5, last_exit=65)
            with patch.object(manager, "wda_ready", return_value=False):
                self.assertEqual(manager.runner_state()["state"], "failed")

    def failed_locked(self, manager, kind="locked", started_at=100.0):
        manager.save_settings(team="ABCDE12345")
        manager.build = {"state": "failed", "error": "x", "log_tail": [], "started_at": started_at,
                         "problem": {"kind": kind, "fix": "f", "action": "retry", "raw": "r"}}

    def test_a_build_that_failed_on_a_locked_phone_goes_again_once_it_reads_unlocked(self):
        from mobile_agent.wireless import devicectl
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            self.failed_locked(manager)
            with patch.object(manager, "device", return_value={"udid": "u", "name": "iPhone"}), \
                    patch.object(manager, "start_build") as build:
                with patch.object(devicectl, "read_lock_state", return_value=True):
                    self.assertFalse(manager.retry_locked_build())          # still locked: waits
                with patch.object(devicectl, "read_lock_state", return_value=None):
                    self.assertFalse(manager.retry_locked_build())          # can't tell: waits
                with patch.object(devicectl, "read_lock_state", return_value=False):
                    self.assertTrue(manager.retry_locked_build())           # unlocked: once
                    self.assertFalse(manager.retry_locked_build())
                build.assert_called_once_with("ABCDE12345")

    def test_no_other_failure_is_tried_again_by_itself(self):
        from mobile_agent.wireless import devicectl
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            for kind in ("no_team", "untrusted_developer", "free_app_limit", "unknown"):
                self.failed_locked(manager, kind=kind)
                with patch.object(manager, "device", return_value={"udid": "u", "name": "iPhone"}), \
                        patch.object(devicectl, "read_lock_state", return_value=False), \
                        patch.object(manager, "start_build") as build:
                    self.assertFalse(manager.retry_locked_build(), kind)
                    build.assert_not_called()
            manager.build = {"state": "succeeded", "error": None, "log_tail": [], "started_at": 1.0}
            self.assertFalse(manager.retry_locked_build())

    def test_the_start_up_grace_counts_from_the_launch_after_a_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            manager.runner = SimpleNamespace(running=True, alive=True, held=None, launched_at=1000.0)
            manager.runner_started_at = 100.0              # started, then held 900 s while the phone was locked
            with patch.object(manager, "wda_ready", return_value=False), \
                    patch.object(manager.watchdog, "probe_result") as probe_result, \
                    patch.object(dm.time, "monotonic", return_value=1010.0):
                manager.probe(now=1010.0)
            probe_result.assert_not_called()               # 10 s after its launch: starting, not wedged


class BuildTeamTests(unittest.TestCase):
    PHONE = {"udid": "u", "name": "iPhone"}
    PAID = {"id": "ABCDE12345", "name": "Radish Retail, LLC", "personal": False}
    PERSONAL = {"id": "QQQQQ11111", "name": "Alex", "personal": True}

    def manager(self, teams=(), saved=None):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        if saved:
            manager.save_settings(team=saved)
        manager.cache["teams"] = (time.monotonic(), list(teams))
        return manager

    def started(self, manager, *args):
        with patch.object(manager, "device", return_value=self.PHONE), \
                patch.object(manager, "tools", return_value={"xcode": {"state": "ok"}, "git": "/x/git"}), \
                patch.object(manager, "_build") as build:
            manager.start_build(*args)
        return build

    def test_the_build_signs_with_the_team_named_and_saves_it_only_after_it_worked(self):
        manager = self.manager([self.PAID, self.PERSONAL], saved=self.PERSONAL["id"])
        build = self.started(manager, self.PAID["id"])
        build.assert_called_once_with(self.PAID["id"], "u")
        self.assertEqual(manager.build["team"], self.PAID["id"])
        self.assertEqual(manager.settings()["team"], self.PERSONAL["id"])     # not yet the chosen one
        steps = []
        manager.project.mkdir(parents=True)
        manager.logs.mkdir(parents=True, exist_ok=True)
        with patch.object(dm.wda_source, "is_pinned", return_value=True), patch.object(manager, "_rebrand"), \
                patch.object(manager, "_drop_stale_profiles"), patch.object(dm.wda_patches, "patch_wda"), \
                patch.object(manager, "_remove_legacy_runner"), patch.object(dm.mac_tools, "tool",
                                                                             lambda n: f"/x/{n}"), \
                patch.object(manager, "_step", lambda command, log, tail: steps.append(command)):
            manager._build_locked(self.PAID["id"], "u", manager.logs / "b.log", [])
        self.assertEqual(manager.build["error"], None)
        self.assertIn(f"DEVELOPMENT_TEAM={self.PAID['id']}", steps[-1])
        self.assertEqual((manager.build["state"], manager.settings()["team"]), ("succeeded", self.PAID["id"]))

    def test_a_failed_build_leaves_the_saved_team_alone(self):
        manager = self.manager([self.PAID, self.PERSONAL], saved=self.PERSONAL["id"])
        self.started(manager, self.PAID["id"])
        manager.project.mkdir(parents=True)
        manager.logs.mkdir(parents=True, exist_ok=True)

        def fail(command, log, tail):
            tail.append("error: No profiles for 'x' were found")
            raise RuntimeError("xcodebuild exited with 65")
        with patch.object(dm.wda_source, "is_pinned", return_value=True), patch.object(manager, "_rebrand"), \
                patch.object(manager, "_drop_stale_profiles"), patch.object(dm.wda_patches, "patch_wda"), \
                patch.object(dm.mac_tools, "tool", lambda n: f"/x/{n}"), patch.object(manager, "_step", fail):
            manager._build_locked(self.PAID["id"], "u", manager.logs / "b.log", [])
        self.assertEqual((manager.build["state"], manager.settings()["team"]), ("failed", self.PERSONAL["id"]))

    def test_no_team_named_uses_the_saved_one_only_while_xcode_lists_it(self):
        manager = self.manager([self.PAID, self.PERSONAL], saved=self.PERSONAL["id"])
        self.started(manager).assert_called_once_with(self.PERSONAL["id"], "u")
        for teams, saved in (([self.PAID], self.PERSONAL["id"]), ([self.PAID, self.PERSONAL], None), ([], "bad")):
            with self.subTest(teams=teams, saved=saved):
                refused = self.manager(teams, saved=saved)
                with self.assertRaisesRegex(ValueError, "Choose your Apple Account"):
                    self.started(refused)
                self.assertNotEqual(refused.build.get("state"), "running")

    def test_a_team_xcode_has_no_account_for_is_a_team_error_naming_it(self):
        raw = 'error: No Account for Team "QQQQQ11111". Add a new account in Accounts settings.'
        signin = setup_errors.problem(raw)
        self.assertEqual(signin["kind"], "xcode_signin")
        other = self.manager([self.PAID])._team_problem(signin, self.PERSONAL["id"])
        self.assertEqual((other["kind"], other["action"]), ("team_account", "choose_team"))
        self.assertIn(self.PERSONAL["id"], other["fix"])
        for teams, team in (([], self.PERSONAL["id"]), ([self.PAID], self.PAID["id"])):
            self.assertEqual(self.manager(teams)._team_problem(signin, team)["kind"], "xcode_signin")
        rejected = setup_errors.problem("error: Unable to log in with account 'a@b.c'. "
                                        "The login details were rejected.")
        self.assertEqual(self.manager([self.PAID])._team_problem(rejected, self.PERSONAL["id"])["kind"], "xcode_signin")


class PortOwnedByAnotherMobsterTests(unittest.TestCase):
    def test_a_start_refuses_when_another_program_holds_the_ports(self):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        manager.save_settings(team="ABCDE12345")
        with patch.object(manager, "device", return_value={"udid": "u", "name": "iPhone"}), \
                patch.object(manager, "expired", return_value=False), patch.object(manager, "built",
                                                                                   return_value=True), \
                patch.object(mac_tools, "relay_command", return_value=["/bin/true"]), \
                patch.object(manager, "stop_runner"), \
                patch("mobile_agent.devices.listening", lambda port, **k: port == manager.wda_port):
            with self.assertRaisesRegex(LookupError, "Another Mobster is using port"):
                manager.start_runner()
        self.assertIsNone(manager.runner)


class BuildStepTests(unittest.TestCase):
    def step(self, manager, seconds):
        import threading
        errors = []

        def run():
            try:
                with open(os.devnull, "w") as log:
                    manager._step(["/bin/sh", "-c", f"echo $$; exec sleep {seconds}"], log, [])
            except RuntimeError as error:
                errors.append(str(error))
        thread = threading.Thread(target=run)
        thread.start()
        end = time.monotonic() + 5
        while manager.build_step is None and time.monotonic() < end:
            time.sleep(.02)
        return thread, manager.build_step, errors

    def test_closing_the_manager_stops_a_running_step(self):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        with patch.object(manager, "xcode_env", return_value=dict(os.environ)):
            thread, process, errors = self.step(manager, 60)
            self.assertIsNotNone(process)
            manager.close()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIsNotNone(process.poll())
        self.assertEqual(len(errors), 1)
        self.assertNotIn(process, dm.BUILD_STEPS)
        with patch.object(manager, "xcode_env", return_value=dict(os.environ)), \
                self.assertRaisesRegex(RuntimeError, "closing"):
            manager._step(["/usr/bin/true"], io.StringIO(), [])

    def test_a_step_past_its_limit_is_stopped(self):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        with patch.object(manager, "xcode_env", return_value=dict(os.environ)), patch.object(supervised,
                                                                                             "BUILD_STEP_LIMIT", .3):
            thread, process, errors = self.step(manager, 60)
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertIsNotNone(process.poll())
        self.assertEqual(len(errors), 1)
        self.assertIn("was stopped", errors[0])


def end(process):
    if process.poll() is None:
        os.killpg(process.pid, 9)
    process.wait(timeout=5)


GUARD = 120  # seconds: a hang guard for a wait on an event, never what decides a result


class SupervisedTests(unittest.TestCase):
    def test_restarts_and_stops_the_whole_process_group(self):
        log = Path(tempfile.mkdtemp()) / "s.log"
        with patch.object(supervised, "RESTART_DELAY", .01):
            service = supervised.Supervised("t", ["/bin/sh", "-c", "exit 3"], log)
            service.start()
            self.assertTrue(service.wait_for(lambda s: s.restarts >= 2, GUARD))
            service.stop()
        self.assertGreaterEqual(service.restarts, 2)
        self.assertEqual(service.last_exit, 3)
        self.assertFalse(service.running)
        long_running = supervised.Supervised("t", ["/bin/sh", "-c", "sleep 30 & wait"], log)
        long_running.start()
        self.assertTrue(long_running.wait_for(lambda s: s.launches >= 1, GUARD))
        pid = long_running.process.pid
        long_running.stop()
        with self.assertRaises((ProcessLookupError, PermissionError)):
            os.killpg(pid, 0)

    def test_a_child_an_earlier_run_left_running_is_stopped_before_a_new_one_starts(self):
        folder = Path(tempfile.mkdtemp())
        log = folder / "wda-runner.log"
        command = ["/bin/sh", "-c", "sleep 30 & wait", "leftover-runner"]
        leftover = subprocess.Popen(command, start_new_session=True)
        self.addCleanup(end, leftover)
        supervised.record(Path(str(log) + ".pid"), leftover.pid, command)
        service = supervised.Supervised("t", ["/bin/sh", "-c", "sleep 30 & wait"], log)
        with patch.object(supervised, "command_of",
                          side_effect=lambda pid: " ".join(command) if pid == leftover.pid else None):
            service.start()
            self.assertTrue(service.wait_for(lambda s: s.launches >= 1, GUARD))
            leftover.wait(timeout=GUARD)                          # stopped, with its group, before the new child began
            self.assertIsNotNone(leftover.poll())
        recorded = json.loads((folder / "wda-runner.log.pid").read_text())
        self.assertEqual(recorded["pid"], service.process.pid)    # the new child is the one recorded now
        service.stop()
        self.assertFalse((folder / "wda-runner.log.pid").exists())   # a stopped child is forgotten

    def test_a_recorded_pid_now_running_something_else_is_never_signalled(self):
        folder = Path(tempfile.mkdtemp())
        other = subprocess.Popen(["/bin/sh", "-c", "sleep 30 & wait", "someone-elses"], start_new_session=True)
        self.addCleanup(end, other)
        supervised.record(folder / "r.pid", other.pid, ["/usr/bin/xcodebuild", "test-without-building"])
        with patch.object(supervised, "command_of", return_value=" ".join(other.args)):
            self.assertFalse(supervised.reap_leftover(folder / "r.pid"))
        self.assertIsNone(other.poll())                       # a reused pid: left alone (reap_leftover has returned)
        self.assertFalse((folder / "r.pid").exists())
        self.assertFalse(supervised.reap_leftover(folder / "missing.pid"))

    def test_a_relaunch_waits_for_the_gate_and_the_first_launch_does_not(self):
        folder = Path(tempfile.mkdtemp())
        asked, released, open_gate = [], [], threading.Event()
        three_asks = threading.Event()

        def gate():
            asked.append(1)
            if len(asked) >= 3:
                three_asks.set()
            return (lambda: released.append(1)) if open_gate.is_set() else None

        with patch.object(supervised, "RESTART_DELAY", .01), patch.object(supervised, "MAX_RESTART_DELAY", .02):
            service = supervised.Supervised("t", ["/bin/sh", "-c", "exit 0"], folder / "s.log", gate=gate)
            service.start()
            self.assertTrue(three_asks.wait(GUARD))
            self.assertEqual((service.launches, released), (1, []))
            open_gate.set()
            self.assertTrue(service.wait_for(lambda s: s.launches >= 2, GUARD))
            service.stop()
        self.assertGreaterEqual(service.launches, 2)
        self.assertGreaterEqual(len(released), 1)            # released once the relaunch had started

    def test_a_hold_keeps_every_launch_back_and_counts_no_failure(self):
        folder = Path(tempfile.mkdtemp())
        locked, asked, three_asks = threading.Event(), [], threading.Event()
        locked.set()

        def hold():
            asked.append(1)
            if len(asked) >= 3:
                three_asks.set()
            return "locked" if locked.is_set() else None

        with patch.object(supervised, "HOLD_POLL", .01):
            service = supervised.Supervised("t", ["/bin/sh", "-c", "sleep 30"], folder / "s.log", hold=hold)
            service.start()
            self.assertTrue(three_asks.wait(GUARD))
            self.assertEqual((service.launches, service.held, service.restarts, service.failures),
                             (0, "locked", 0, 0))
            locked.clear()                                   # unlocked: it launches at its next look
            self.assertTrue(service.wait_for(lambda s: s.launches >= 1, GUARD))
            service.stop()
        self.assertEqual(service.launches, 1)
        self.assertIsNone(service.held)
        self.assertIsNotNone(service.launched_at)

    def test_a_hold_that_fails_launches_as_before(self):
        folder = Path(tempfile.mkdtemp())

        def broken():
            raise RuntimeError("devicectl hung")
        service = supervised.Supervised("t", ["/bin/sh", "-c", "sleep 30"], folder / "s.log", hold=broken)
        service.start()
        self.assertTrue(service.wait_for(lambda s: s.launches >= 1, GUARD))
        service.stop()
        self.assertEqual(service.launches, 1)

    def test_stopping_ends_the_wait_for_the_gate_without_a_relaunch(self):
        folder = Path(tempfile.mkdtemp())
        asked, three_asks = [], threading.Event()

        def gate():
            asked.append(1)
            if len(asked) >= 3:
                three_asks.set()
            return None

        with patch.object(supervised, "RESTART_DELAY", .01), patch.object(supervised, "MAX_RESTART_DELAY", .02):
            service = supervised.Supervised("t", ["/bin/sh", "-c", "exit 0"], folder / "s.log", gate=gate)
            service.start()
            self.assertTrue(three_asks.wait(GUARD))
            service.stop()
        self.assertEqual(service.launches, 1)
        self.assertFalse(service.running)


class QuitStopsChildrenTests(unittest.TestCase):
    def started(self, command):
        folder = Path(tempfile.mkdtemp())
        marker = folder / "ready"
        service = supervised.Supervised(
            "t", ["/bin/sh", "-c", command.replace("{ready}", f": > {shlex.quote(str(marker))}")], folder / "s.log")
        service.start()
        self.addCleanup(service.stop)
        self.assertTrue(service.wait_for(lambda s: s.launches >= 1, GUARD))
        guard = time.monotonic() + GUARD
        while not marker.exists() and time.monotonic() < guard:
            time.sleep(.01)
        self.assertTrue(marker.exists())
        return service

    def test_two_children_that_ignore_sigterm_are_killed_together(self):
        manager = object.__new__(dm.DeviceManager)
        manager.runner = self.started('trap "" TERM; {ready}; sleep 60 & wait; sleep 60')
        manager.relay = self.started('trap "" TERM; {ready}; sleep 60 & wait; sleep 60')
        groups = [manager.runner.process.pid, manager.relay.process.pid]
        runner, relay = manager.runner, manager.relay
        seen = {}
        for name, service, other in (("runner", runner, relay), ("relay", relay, runner)):
            stop = service.stop
            def watched(stop=stop, name=name, other=other):
                seen[name] = other.stopping.is_set()      # the other was already told to stop when this began to wait
                return stop()
            service.stop = watched
        manager._stop_runner(remember=False)
        self.assertEqual(seen, {"runner": True, "relay": True})
        for group in groups:
            with self.assertRaises((ProcessLookupError, PermissionError)):
                os.killpg(group, 0)

    def test_a_child_that_ignores_sigterm_is_killed_after_its_shell_exits(self):
        manager = object.__new__(dm.DeviceManager)
        manager.runner, manager.relay = self.started('(trap "" TERM; {ready}; sleep 60) & wait'), None
        group = manager.runner.process.pid
        manager._stop_runner(remember=False)
        with self.assertRaises((ProcessLookupError, PermissionError)):
            os.killpg(group, 0)

    def test_children_that_exit_on_sigterm_stop_without_waiting_out_the_grace(self):
        manager = object.__new__(dm.DeviceManager)
        manager.runner, manager.relay = (self.started("{ready}; sleep 60 & wait"),
                                         self.started("{ready}; sleep 60 & wait"))
        waited = []
        with patch.object(supervised.time, "sleep", side_effect=lambda seconds: waited.append(seconds)):
            manager._stop_runner(remember=False)
        self.assertIsNone(manager.runner)
        self.assertIsNone(manager.relay)

    def test_begin_close_signals_without_waiting(self):
        manager = object.__new__(dm.DeviceManager)
        manager.watching = __import__("threading").Event()
        manager.runner = self.started('trap "" TERM; {ready}; sleep 60 & wait; sleep 60')
        manager.relay = None
        manager.build_step = None
        waiting_threads = []
        with patch.object(supervised, "stop_group", side_effect=lambda *a, **k: waiting_threads.append(
                threading.get_ident())):
            manager.begin_close()
        self.assertNotIn(threading.get_ident(), waiting_threads)
        self.assertTrue(manager.watching.is_set())
        self.assertTrue(manager.runner.stopping.is_set())


class LoopbackTests(unittest.TestCase):
    UDID = "00008130-001A2B3C4D5E6F70"

    def iproxy(self, help_text):
        path = Path(tempfile.mkdtemp()) / "iproxy"
        path.write_text(f"#!/bin/sh\necho '{help_text}'\n")
        path.chmod(0o755)
        return str(path)

    def test_the_relay_listens_on_loopback_only_or_not_at_all(self):
        modern = self.iproxy("  -s, --source ADDR  source address for listening socket (default 127.0.0.1)")
        with patch.object(mac_tools, "tool", lambda name: modern):
            self.assertEqual(mac_tools.relay_command(self.UDID)[:5], [modern, "-s", "127.0.0.1", "-u", self.UDID])
        old = self.iproxy("usage: iproxy LOCAL_TCP_PORT DEVICE_TCP_PORT [UDID]")
        with patch.object(mac_tools, "tool", lambda name: old), self.assertRaisesRegex(LookupError, "Update"):
            mac_tools.relay_command(self.UDID)
        with patch.object(mac_tools, "tool", lambda name: None), self.assertRaises(LookupError):
            mac_tools.relay_command(self.UDID)

    def test_the_runner_and_relay_start_bound_to_loopback(self):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        manager.save_settings(team="ABCDE12345")
        started = []
        modern = self.iproxy("--source ADDR")
        with patch.object(manager, "device", return_value={"udid": self.UDID}), \
                patch.object(manager, "built", return_value=True), patch.object(mac_tools, "tool",
                                                                                lambda name: modern), \
                patch("mobile_agent.devices.listening", lambda port, **k: False), \
                patch.object(supervised.Supervised, "start", lambda service: started.append(service.command)):
            manager.start_runner()
        relay, runner = started
        self.assertIn("USE_IP=127.0.0.1", runner)
        self.assertEqual(relay[1:3], ["-s", "127.0.0.1"])

    def test_an_old_iproxy_leaves_a_running_runner_alone(self):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        manager.save_settings(team="ABCDE12345")
        old = self.iproxy("usage: iproxy LOCAL_TCP_PORT DEVICE_TCP_PORT [UDID]")
        with patch.object(manager, "device", return_value={"udid": self.UDID}), \
                patch.object(manager, "built", return_value=True), patch.object(mac_tools, "tool", lambda name: old), \
                patch.object(manager, "stop_runner") as stop, self.assertRaises(LookupError):
            manager.start_runner()
        stop.assert_not_called()

    def test_existing_builds_are_pinned_to_loopback(self):
        import plistlib
        products = Path(tempfile.mkdtemp())
        plan = {"__xctestrun_metadata__": {"FormatVersion": 1},
                "WebDriverAgentRunner": {"EnvironmentVariables": {"USE_IP": "", "MJPEG_SERVER_PORT": ""}}}
        (products / "Runner.xctestrun").write_bytes(plistlib.dumps(plan))
        wda_patches.pin_loopback(products)
        saved = plistlib.loads((products / "Runner.xctestrun").read_bytes())
        self.assertEqual(saved["WebDriverAgentRunner"]["EnvironmentVariables"],
                         {"USE_IP": "127.0.0.1", "MJPEG_SERVER_PORT": ""})
        self.assertEqual(saved["__xctestrun_metadata__"], {"FormatVersion": 1})

    def test_wda_patches_apply_once_and_refuse_an_unknown_source(self):
        project = Path(tempfile.mkdtemp())
        for index, (name, anchor, _) in enumerate(wda_patches.WDA_PATCHES):
            if any(anchor in patched for _, _, patched in wda_patches.WDA_PATCHES[:index]):
                continue  # anchored on lines an earlier patch adds (the transfer routes on the Photos patch)
            path = project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text((path.read_text() if path.exists() else "") + "// before\n" + anchor + "// after\n")
        wda_patches.patch_wda(project)
        once = {name: (project / name).read_text() for name, _, _ in wda_patches.WDA_PATCHES}
        wda_patches.patch_wda(project)
        self.assertEqual(once, {name: (project / name).read_text() for name, _, _ in wda_patches.WDA_PATCHES})
        self.assertIn("screenshotsBroadcaster.interface", once["WebDriverAgentLib/Routing/FBWebServer.m"])
        self.assertIn('requestHeaders[@"origin"]', once["WebDriverAgentLib/Routing/FBHTTPServer.m"])
        self.assertIn("FBMobsterLocalStreamRequest(data)", once["WebDriverAgentLib/Utilities/FBMjpegServer.m"])
        (project / wda_patches.WDA_PATCHES[0][0]).write_text("// another version\n")
        with self.assertRaisesRegex(RuntimeError, "can't be limited to USB"):
            wda_patches.patch_wda(project)

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
        env_file.private_dir(log.parent)
        with env_file.open_private(log, "ab") as stream:
            stream.write(b"new\n")
        mode = lambda path: stat.S_IMODE(path.stat().st_mode)
        self.assertEqual((mode(data), mode(manager.settings_path), mode(log.parent), mode(log)),
                         (0o700, 0o600, 0o700, 0o600))
        self.assertEqual(log.read_text(), "old\nnew\n")
        self.assertEqual(manager.settings()["team"], "ABCDE12345")

    @unittest.skipUnless(mac_tools.tool("iproxy") and mac_tools.iproxy_binds_loopback(mac_tools.tool("iproxy")),
                         "needs iproxy with -s")
    def test_the_real_iproxy_binds_only_loopback(self):
        import socket
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        command = mac_tools.relay_command("0000")[:-2] + [f"{port}:8100"]
        process = dm.subprocess.Popen(command, stdout=dm.subprocess.DEVNULL, stderr=dm.subprocess.DEVNULL)
        self.addCleanup(process.wait)
        self.addCleanup(process.terminate)
        listening, guard = "", time.monotonic() + GUARD
        while process.poll() is None and time.monotonic() < guard:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
            except OSError:
                time.sleep(.05)
                continue
            listening = mac_tools.run(["/usr/sbin/lsof", "-nP", "-a", "-p", str(process.pid), "-iTCP",
                                       "-sTCP:LISTEN"]) or ""
            if "LISTEN" in listening:
                break
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
        env = {"ANTHROPIC_API_KEY": "sk-ant-" + "k" * 30} if key else {}
        service = SetupService(runtime, manager, Path(tempfile.mkdtemp()) / "agent.env")
        service.developer_mode = lambda device: developer_mode
        return service, env

    def states(self, plan=False, agents=(), **kwargs):
        service, env = self.service(**kwargs)
        with patch.dict(os.environ, env, clear=True), \
                patch.object(mac_tools, "executable", return_value="/opt/homebrew/bin/brew"), \
                patch.object(setup_service, "plan_ready", return_value=plan), \
                patch.object(setup_service, "agents_ready", return_value=list(agents)):
            state = service.state()
        return state, {step["id"]: step["state"] for step in state["steps"]}

    def step(self, state, id_):
        return next(step for step in state["steps"] if step["id"] == id_)

    def test_a_fresh_mac_blocks_later_steps(self):
        state, steps = self.states(key=False, tools=False)
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
                                          "libimobiledevice": False, "homebrew": True, "bundled": False,
                                          "install": {"state": "idle", "line": None, "error": None}})

    def test_the_command_line_tools_selected_are_no_step_at_all(self):
        xcode = xcode_state("ok", selected=mac_tools.CLT_DIR)
        state, steps = self.states(xcode=xcode)
        self.assertEqual(steps["tools"], "done")
        self.assertEqual(self.step(state, "tools")["commands"], [])

    def test_each_xcode_problem_has_its_own_advice(self):
        for problem, words in (("license", "Accept the Xcode license"), ("first_launch", "Open it once"),
                               ("no_ios", "Settings › Components"), ("broken", "isn't responding")):
            state, steps = self.states(xcode=xcode_state(problem))
            self.assertEqual(steps["tools"], "todo")
            self.assertIn(words, self.step(state, "tools")["detail"])
        state, _ = self.states()
        self.assertEqual(self.step(state, "tools")["detail"],
                         "Xcode 26.4 and the iPhone connection tools are installed.")

    def test_homebrew_is_offered_first_when_it_is_missing(self):
        service, env = self.service(tools=False, xcode=xcode_state("ok"))
        with patch.dict(os.environ, env, clear=True), patch.object(mac_tools, "executable", return_value=None):
            state = service.state()
        tools = self.step(state, "tools")
        self.assertEqual(tools["commands"][1], "brew install libimobiledevice")
        self.assertIn("Homebrew/install", tools["commands"][0])
        self.assertIn("Install Homebrew, then the iPhone connection tools", tools["detail"])

    def test_the_apps_own_iphone_tools_come_first_and_read_as_included(self):
        bundled = Path(tempfile.mkdtemp())
        for name in mac_tools.IPHONE_TOOLS:
            (bundled / name).write_text("#!/bin/sh\n")
            (bundled / name).chmod(0o755)
        with patch.dict(os.environ, {"MOBSTER_IPHONE_TOOLS": str(bundled)}):
            self.assertEqual(mac_tools.tool("iproxy"), str(bundled / "iproxy"))
            self.assertEqual(mac_tools.tool("ideviceinstaller"), str(bundled / "ideviceinstaller"))
            self.assertNotEqual(mac_tools.tool("git"), str(bundled / "git"))
            service, _ = self.service()
            paths = {**service.manager.tools(), "iproxy": str(bundled / "iproxy"),
                     "idevice_id": str(bundled / "idevice_id")}
            service.manager.tools = lambda: paths
            state = service.state()
        self.assertTrue(state["tools"]["bundled"])
        self.assertEqual(self.step(state, "tools")["commands"], [])
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(mac_tools.bundled_tools_dir())

    def test_install_runs_homebrew_in_the_background_and_reports_progress_in_words(self):
        lines = ["==> Fetching dependencies for libimobiledevice: libplist", "==> Fetching libplist\n",
                 "==> Pouring libplist--2.7.0.arm64_tahoe.bottle.tar.gz", "==> Downloading https://ghcr.io/x"]
        seen, calls = [], []
        install = ToolsInstall(popen=None)

        def popen(argv, **kwargs):
            calls.append((argv, kwargs["env"].get("HOMEBREW_NO_AUTO_UPDATE")))

            class Process:
                stdout = iter(line + "\n" for line in lines)

                def wait(self):
                    seen.append(install.status()["line"])
                    return 0
            return Process()
        install.popen = popen
        install.update(state="running")
        install.run("/opt/homebrew/bin/brew", on_done=lambda: seen.append("refreshed"))
        self.assertEqual(calls, [(["/opt/homebrew/bin/brew", "install", "libimobiledevice", "ideviceinstaller"], "1")])
        self.assertEqual(seen, ["Installing libplist", "refreshed"])
        self.assertEqual(install.status(), {"state": "succeeded", "line": None, "error": None})
        self.assertIsNone(install_words("==> Downloading https://ghcr.io/v2/homebrew/core/libplist"))

    def test_a_failed_install_says_what_to_do_and_homebrew_is_needed_for_one(self):
        class Process:
            stdout = iter(["Error: No internet\n"])

            def wait(self):
                return 1
        install = ToolsInstall(popen=lambda *args, **kwargs: Process())
        install.run("/x/brew")
        self.assertEqual(install.status()["state"], "failed")
        self.assertIn("Check your internet connection", install.status()["error"])
        self.assertIn("Error: No internet", install.status()["error"])
        service, env = self.service(tools=False, xcode=xcode_state("ok"))
        with patch.dict(os.environ, env, clear=True), patch.object(mac_tools, "executable", return_value=None):
            with self.assertRaisesRegex(LookupError, "needs Homebrew"):
                service.install_tools()
        service, env = self.service(tools=True)
        service.tools_install.start = lambda *args, **kwargs: self.fail("started an install")
        service.install_tools()

    def test_open_xcode_opens_the_installed_app_and_checks_again(self):
        service, _ = self.service(xcode=xcode_state("first_launch"))
        opened = []
        service.manager.refresh = lambda: opened.append("refresh")
        service.open_xcode(opener=lambda argv, **kwargs: opened.append(argv))
        self.assertEqual(opened, [["/usr/bin/open", "/Applications/Xcode.app"], "refresh"])
        service, _ = self.service(xcode=xcode_state("missing"))
        with self.assertRaisesRegex(LookupError, "App Store"):
            service.open_xcode(opener=lambda argv, **kwargs: self.fail("opened"))

    def test_an_expired_runner_asks_for_a_rebuild(self):
        device = {"udid": "u", "name": "iPhone", "trusted": True}
        state, steps = self.states(device=device, built=False, expired=True, expires_at=1_000,
                                   settings={"udid": "u", "built_for": "u"})
        build = self.step(state, "wda_build")
        self.assertEqual(steps["wda_build"], "todo")
        self.assertIn("needs a refresh", build["detail"])
        self.assertIn("7 days", build["detail"])
        self.assertEqual((state["build"]["expired"], state["build"]["expires_at"]), (True, 1_000_000))
        self.assertEqual(steps["wda_running"], "blocked")

    def test_a_plan_with_no_key_does_the_ai_step(self):
        state, steps = self.states(key=False, plan=True)
        self.assertEqual(steps["api_key"], "done")
        self.assertEqual(self.step(state, "api_key")["detail"], setup_service.KEY_DETAIL["plan"])
        _, steps = self.states(key=False, plan=False)
        self.assertEqual(steps["api_key"], "todo")

    def test_a_coding_agent_signed_in_does_the_ai_step_in_its_own_words(self):
        ready = {"claude_code": {"id": "claude_code", "name": "Claude Code", "ready": True, "installed": True},
                 "codex": {"id": "codex", "name": "Codex", "ready": False, "installed": True}}
        with patch.dict(os.environ, {}, clear=True), patch("mobile_agent.ai_plans.agents.snapshot", return_value=ready):
            self.assertFalse(setup_service.plan_ready())
            self.assertEqual(setup_service.agents_ready(), ["Claude Code"])
        state, steps = self.states(key=False, agents=["Claude Code"])
        self.assertEqual(steps["api_key"], "done")
        self.assertEqual(self.step(state, "api_key")["detail"],
                         "Tasks you type run on Claude Code, signed in on this Mac.")
        self.assertEqual(setup_service.agent_detail(["Codex", "Claude Code"]),
                         "Tasks you type run on Codex or Claude Code, signed in on this Mac.")
        state, steps = self.states(key=False, agents=[])
        self.assertEqual(steps["api_key"], "todo")
        self.assertEqual(self.step(state, "api_key")["detail"], setup_service.KEY_DETAIL["none"])
        state, steps = self.states(key=True, agents=["Claude Code"])
        self.assertIn("Claude account", self.step(state, "api_key")["detail"])

    def test_a_running_build_gives_a_realistic_estimate_and_its_start(self):
        device = {"udid": "u", "name": "iPhone", "trusted": True}
        running = {"state": "running", "error": None, "log_tail": [], "started_at": 100.5, "first": True}
        state, steps = self.states(device=device, build=running)
        self.assertEqual(steps["wda_build"], "working")
        self.assertIn("about 2 minutes", self.step(state, "wda_build")["detail"])
        self.assertEqual((state["build"]["started_at"], state["build"]["first"]), (100_500, True))
        state, _ = self.states(device=device, build={**running, "first": False})
        self.assertIn("quicker than the first time", self.step(state, "wda_build")["detail"])

    def test_the_phone_reports_developer_mode_when_it_can(self):
        device = {"udid": "u", "name": "iPhone", "trusted": True}
        state, _ = self.states(device=device, developer_mode=False)
        self.assertIs(state["device"]["developerMode"], False)
        self.assertEqual(state["teams"], [{"id": "ABCDE12345", "name": "Test Person", "personal": True,
                                           "label": "Test Person (Personal Team)"}])

    def test_developer_mode_is_read_from_the_trusted_phone_and_an_off_answer_is_re_read(self):
        service, _ = self.service()
        del service.developer_mode  # the real method, not the fixture's
        answers = ["false\n", "true\n"]
        with patch.object(mac_tools, "tool", return_value="/bin/ideviceinfo"), \
                patch.object(mac_tools, "run", side_effect=lambda command, timeout=10: answers.pop(0)) as run:
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
        self.assertIn("installed on another iPhone", state["steps"][3]["detail"])
        state, _ = self.states(device=pro, settings={"udid": "p", "built_for": "x", "device_name": None})
        self.assertTrue(state["steps"][3]["detail"].endswith("Install it again on your iPhone."))

    def test_a_fully_set_up_mac_is_complete(self):
        device = {"udid": "u", "name": "iPhone", "trusted": True}
        state, steps = self.states(device=device, built=True, runner="running", live=True, frame=True)
        self.assertTrue(state["complete"])
        self.assertTrue(all(value == "done" for value in steps.values()))

    def test_the_key_step_asks_for_claude_or_openai_and_a_jev_key_alone_does_not_count(self):
        for env, done, words in (({}, "todo", "a plan you already pay for or on your own key"),
                                 ({"OPENAI_API_KEY": "sk-" + "x" * 30}, "done", "uses your OpenAI account"),
                                 ({"ANTHROPIC_API_KEY": "sk-ant-" + "x" * 30}, "done", "uses your Claude account"),
                                 ({"OPENAI_API_KEY": "sk-" + "x" * 30, "ANTHROPIC_API_KEY": "sk-ant-" + "x" * 30},
                                  "done", "uses your Claude account"),
                                 ({"OPENAI_API_KEY": "sk-" + "x" * 30, "ANTHROPIC_API_KEY": "sk-ant-" + "x" * 30,
                                   "MOBSTER_SMART_PROVIDER": "openai"}, "done", "uses your OpenAI account"),
                                 ({"TYPESAFE_API_KEY": "k"}, "todo", "a plan you already pay for or on your own key")):
            service, _ = self.service(key=False)
            with patch.dict(os.environ, env, clear=True), patch.object(mac_tools, "executable",
                                                                       return_value="/bin/brew"), \
                    patch.object(setup_service, "agents_ready", return_value=[]):
                state = service.state()
            step = self.step(state, "api_key")
            self.assertEqual((step["title"], step["state"]), ("Connect your AI account", done))
            self.assertIn(words, step["detail"])
            self.assertEqual(state["keys"],
                             {"openai": "OPENAI_API_KEY" in env, "anthropic": "ANTHROPIC_API_KEY" in env})

    def test_a_failed_build_and_runner_carry_their_fix(self):
        device = {"udid": "u", "name": "iPhone", "trusted": True}
        problem = {"kind": "no_team", "fix": "Choose your Apple Account", "action": "choose_team", "raw": "error: x"}
        state, _ = self.states(device=device, build={"state": "failed", "error": "Choose your team", "log_tail": [],
                                                     "problem": problem})
        self.assertEqual(self.step(state, "wda_build")["problem"], problem)
        self.assertEqual(state["build"]["problem"], problem)
        state, _ = self.states(device=device)
        self.assertIsNone(self.step(state, "wda_build")["problem"])

    def test_live_persists_privately(self):
        service, _ = self.service()
        service.set_live(True)
        self.assertTrue(service.runtime.config.enable_live)
        self.assertIn("MOBSTER_ENABLE_LIVE=1", service.env_file.read_text())


FAKE_XCODEBUILD = """#!/bin/sh
case "$1" in
  -version) if [ -n "$NO_LICENSE" ]; then echo "You have not agreed to the Xcode license agreements." >&2; exit 69; fi
            echo "Xcode 26.4"; echo "Build version 17E192";;
  -checkFirstLaunchStatus) [ -z "$FIRST_LAUNCH" ];;
  -showsdks) echo "macOS SDKs:"; [ -z "$NO_IOS" ] && echo "	iOS 26.4	-sdk iphoneos26.4"; exit 0;;
esac
"""


class XcodeDetectionTests(unittest.TestCase):
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
        with patch.object(mac_tools, "XCODE_APP_DIRS", (str(self.root),)), \
                patch.object(mac_tools, "run", return_value=f"{selected}\n" if selected else None), \
                patch.dict(os.environ, env):
            return mac_tools.xcode_status()

    def test_the_usr_bin_stubs_are_never_xcode_or_git(self):
        with patch.object(mac_tools, "developer_dir", return_value=(None, mac_tools.CLT_DIR)), \
                patch.object(mac_tools, "GIT_DIRS", ()):
            self.assertIsNone(mac_tools.tool("xcodebuild"))
            self.assertIsNone(mac_tools.tool("git"))
        self.assertFalse(mac_tools.is_xcode("/usr"))
        self.assertFalse(mac_tools.is_xcode(mac_tools.CLT_DIR))

    def test_no_xcode_app_is_missing_even_with_the_command_line_tools(self):
        status = self.status(mac_tools.CLT_DIR)
        self.assertEqual((status["state"], status["fixes"]), ("missing", []))

    def test_the_command_line_tools_selected_still_use_the_xcode_found_with_no_fix(self):
        self.install_xcode()
        status = self.status(mac_tools.CLT_DIR)
        self.assertEqual((status["state"], status["problems"], status["fixes"]), ("ok", [], []))
        self.assertEqual(status["selected"], mac_tools.CLT_DIR)
        self.assertEqual(status["version"], "Xcode 26.4")
        self.assertEqual(status["developer_dir"], str(self.developer))
        with patch.object(mac_tools, "XCODE_APP_DIRS", (str(self.root),)), patch.object(mac_tools, "run",
                                                                                        return_value=mac_tools.CLT_DIR):
            self.assertEqual(mac_tools.tool("xcodebuild"), str(self.developer / "usr" / "bin" / "xcodebuild"))
            self.assertEqual(mac_tools.tool("git"), str(self.developer / "usr" / "bin" / "git"))

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
        status = self.status(mac_tools.CLT_DIR, NO_LICENSE="1")
        self.assertEqual(status["problems"], ["license"])
        self.assertEqual(status["fixes"], ["sudo xcodebuild -license accept"])

    def test_mobster_adopts_the_xcode_it_found_for_its_own_children_only(self):
        self.install_xcode()
        with patch.object(mac_tools, "XCODE_APP_DIRS", (str(self.root),)):
            with patch.object(mac_tools, "run", return_value=mac_tools.CLT_DIR):
                environ = {}
                self.assertEqual(mac_tools.adopt_xcode(environ), str(self.developer))
                self.assertEqual(environ, {"DEVELOPER_DIR": str(self.developer)})
                mine = {"DEVELOPER_DIR": "/Applications/Xcode-beta.app/Contents/Developer"}
                self.assertEqual(mac_tools.adopt_xcode(mine), mine["DEVELOPER_DIR"])
                self.assertEqual(mine, {"DEVELOPER_DIR": "/Applications/Xcode-beta.app/Contents/Developer"})
            with patch.object(mac_tools, "run", return_value=str(self.developer)):
                environ = {}
                mac_tools.adopt_xcode(environ)
                self.assertEqual(environ, {})
        with patch.object(mac_tools, "XCODE_APP_DIRS", (str(Path(tempfile.mkdtemp())),)), \
                patch.object(mac_tools, "run", return_value=mac_tools.CLT_DIR):
            environ = {}
            self.assertIsNone(mac_tools.adopt_xcode(environ))
            self.assertEqual(environ, {})

    def test_xcode_tools_never_see_the_shells_settings_for_building_for_this_mac(self):
        shell = {"PATH": "/usr/bin", "HOME": "/Users/test", "CPATH": "/usr/local/include",
                 "LIBRARY_PATH": "/usr/local/lib",
                 "SDKROOT": "/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk", "CFLAGS": "-I/opt/x",
                 "C_INCLUDE_PATH": "/x", "OBJC_INCLUDE_PATH": "/x", "LDFLAGS": "-L/x"}
        env = mac_tools.xcode_env("/Applications/Xcode.app/Contents/Developer", base=shell)
        self.assertEqual(env, {"PATH": "/usr/bin", "HOME": "/Users/test",
                               "DEVELOPER_DIR": "/Applications/Xcode.app/Contents/Developer"})
        self.assertEqual(mac_tools.xcode_env(base={"CPATH": "/x", "LANG": "en"}), {"LANG": "en"})
        manager = dm.DeviceManager(tempfile.mkdtemp())
        with patch.dict(os.environ, shell, clear=True), \
                patch.object(manager, "tools", return_value={"xcode": {"developer_dir": "/X.app/Contents/Developer"}}):
            for built in (manager.xcode_env(), manager.runner_env()):
                self.assertFalse(set(built) & set(mac_tools.HOST_BUILD_ENV))
                self.assertEqual(built["DEVELOPER_DIR"], "/X.app/Contents/Developer")
                self.assertEqual(built["PATH"], "/usr/bin")
        from mobile_agent.sim import xcode as sim_xcode
        with patch.dict(os.environ, shell, clear=True), \
                patch.object(mac_tools, "developer_dir", return_value=("/X.app/Contents/Developer", None)), \
                patch.object(sim_xcode, "app_version", return_value=("26.4", "17E192")), \
                patch.object(mac_tools, "xcode_tool", return_value="/x/git"):
            tools = sim_xcode.find_tools(lambda *args, **kwargs: None)
        self.assertFalse(set(tools.env) & set(mac_tools.HOST_BUILD_ENV))
        self.assertEqual(tools.env["DEVELOPER_DIR"], "/X.app/Contents/Developer")

    def test_a_build_is_refused_until_xcode_works(self):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        with patch.object(manager, "device", return_value={"udid": "u", "name": "iPhone"}), \
                patch.object(manager, "tools", return_value={"xcode": xcode_state("license"), "git": "/x/git"}):
            with self.assertRaises(LookupError) as refused:
                manager.start_build("ABCDE12345")
        self.assertIn("Finish installing Xcode", str(refused.exception))

    def test_xcodebuild_failures_about_xcode_say_what_to_do_without_terminal(self):
        hint = runner_hints.build_hint(["xcode-select: error: tool 'xcodebuild' requires Xcode, but active "
                                        "developer directory '/Library/Developer/CommandLineTools' is a command "
                                        "line tools instance"])
        self.assertIn("Install it from the Mac App Store", hint)
        license_hint = runner_hints.build_hint(["You have not agreed to the Xcode license agreements."])
        self.assertIn("open Xcode and agree", license_hint)
        for needle, text in runner_hints.BUILD_HINTS:
            with self.subTest(needle=needle):
                self.assertNotRegex(text, r"sudo|Terminal")
        self.assertIn("internet connection",
                      runner_hints.build_hint(["fatal: unable to access: Could not resolve host: github.com"]))


class SigningTests(unittest.TestCase):
    def test_teams_signed_in_to_xcode_are_found_without_a_certificate(self):
        from mobile_agent import signing
        defaults = {"IDEProvisioningTeamByIdentifier": {"account-1": [
            {"teamID": "ABCDE12345", "teamName": "Test Person", "teamType": "Personal Team",
             "isFreeProvisioningTeam": True},
            {"teamID": "not-a-team", "teamName": "x"}]},
            "IDEProvisioningTeams": {"old": [{"teamID": "ABCDE12345"},
                                             {"teamID": "ZYXWV98765", "teamName": "Example Co"}]}}
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
        self.assertEqual(signing.subject_team("subject=UID=X, CN=Apple Development: "
                                              "a (QQQQQQQQQQ), OU=ABCDE12345, O=B"),
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


class RunnerIdTests(unittest.TestCase):
    PBXPROJ = ("PRODUCT_BUNDLE_IDENTIFIER = com.facebook.WebDriverAgentRunner;\n"
               "PRODUCT_BUNDLE_IDENTIFIER = com.facebook.WebDriverAgentLib;\n"
               "PRODUCT_BUNDLE_IDENTIFIER = com.facebook.WebDriverAgentRunnerTests;\n")

    def test_the_runner_gets_an_app_id_of_its_teams_own_and_follows_a_team_change(self):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        project = manager.project / "WebDriverAgent.xcodeproj" / "project.pbxproj"
        project.parent.mkdir(parents=True)
        project.write_text(self.PBXPROJ)
        from mobile_agent import signing
        mine, theirs = signing.runner_bundle_id("ABCDE12345"), signing.runner_bundle_id("ZYXWV98765")
        self.assertRegex(mine, r"^app\.mobster\.wda\.runner\.[0-9a-f]{10}$")
        self.assertNotEqual(mine, theirs)
        self.assertNotIn("abcde12345", mine.lower())    # the id on the phone doesn't spell out the team
        self.assertEqual(signing.runner_bundle_id(None), "app.mobster.wda.runner")
        manager._rebrand("ABCDE12345")
        text = project.read_text()
        self.assertIn(f"= {mine};", text)
        self.assertIn("= app.mobster.wda.lib;", text)
        self.assertIn("= com.facebook.WebDriverAgentRunnerTests;", text)
        manager._rebrand("ZYXWV98765")                   # an existing clone, built for another team before
        self.assertEqual(project.read_text().count(f"{theirs};"), 1)
        self.assertNotIn(mine, project.read_text())
        project.write_text(self.PBXPROJ.replace("com.facebook.WebDriverAgentRunner;", "app.mobster.wda.runner;"))
        manager._rebrand("ABCDE12345")                   # a clone from before the suffix
        self.assertIn(f"= {mine};", project.read_text())

    def test_the_plain_id_runner_is_uninstalled_once_after_a_build_with_the_suffix(self):
        import subprocess
        manager = dm.DeviceManager(tempfile.mkdtemp())
        udid = "00008130-001A2B3C4D5E6F70"
        calls, lines = [], []
        log = SimpleNamespace(write=lines.append)

        def run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "App uninstalled.", "")
        with patch.object(mac_tools, "tool", lambda name: f"/usr/bin/{name}"), \
                patch.object(dm.subprocess, "run", run), \
                patch.object(manager, "xcode_env", return_value=None):
            manager._remove_legacy_runner(None, udid, log)            # no team: the plain id is the runner itself
            self.assertEqual(calls, [])
            manager._remove_legacy_runner("ABCDE12345", udid, log)
            manager._remove_legacy_runner("ABCDE12345", udid, log)    # once per phone
        self.assertEqual(calls, [["/usr/bin/xcrun", "devicectl", "device", "uninstall", "app", "--device", udid,
                                  "app.mobster.wda.runner.xctrunner"]])
        self.assertIn("Removed the old runner (app.mobster.wda.runner.xctrunner)", lines[0])

        def fails(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, "", "ERROR: The device is locked.")
        other = dm.DeviceManager(tempfile.mkdtemp())
        with patch.object(mac_tools, "tool", lambda name: f"/usr/bin/{name}"), \
                patch.object(dm.subprocess, "run", fails), \
                patch.object(other, "xcode_env", return_value=None):
            other._remove_legacy_runner("ABCDE12345", udid, log)
        self.assertIn("Could not remove the old runner", lines[-1])
        self.assertNotIn(udid, other.settings().get("legacy_removed", []))   # tried again after the next build

    def test_only_expiring_profiles_for_the_runner_are_dropped_before_a_renewal(self):
        import plistlib
        from datetime import datetime, timezone

        from mobile_agent import signing
        folder = Path(tempfile.mkdtemp())
        profiles = {"runner": ("ABCDE12345.app.mobster.wda.runner.abcde12345.xctrunner", 1),
                    "fresh": ("ABCDE12345.app.mobster.wda.runner.abcde12345.xctrunner", 200),
                    "wildcard": ("ABCDE12345.*", 1), "other_app": ("ABCDE12345.com.example.app", 1),
                    "other_team": ("ZYXWV98765.app.mobster.wda.runner.zyxwv98765.xctrunner", 1)}
        now = 1_800_000_000
        for name, (identifier, hours) in profiles.items():
            (folder / f"{name}.mobileprovision").write_bytes(b"signed")
        def read(command, timeout=10, text=True, input=None):
            identifier, hours = profiles[Path(command[-1]).stem]
            return plistlib.dumps({"Entitlements": {"application-identifier": identifier},
                                   "ExpirationDate": datetime.fromtimestamp(now + hours * 3600, timezone.utc)
                                                     .replace(tzinfo=None)}).decode()
        with patch.object(signing, "_read", side_effect=read):
            stale = signing.stale_runner_profiles("ABCDE12345", now + 48 * 3600, [folder])
        self.assertEqual([path.stem for path in stale], ["runner"])
        self.assertEqual(signing.stale_runner_profiles("bad", now, [folder]), [])

    def test_a_build_inside_the_renewal_window_drops_the_old_profile_first(self):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        stale = Path(tempfile.mkdtemp()) / "old.mobileprovision"
        stale.write_bytes(b"x")
        log = io.StringIO()
        with patch.object(manager, "signature_expiry", return_value=time.time() + 3600), \
                patch.object(dm.signing, "stale_runner_profiles", return_value=[stale]) as find:
            manager._drop_stale_profiles("ABCDE12345", log)
        self.assertFalse(stale.exists())
        self.assertEqual(find.call_args[0][0], "ABCDE12345")
        with patch.object(manager, "signature_expiry", return_value=time.time() + 5 * 86_400), \
                patch.object(dm.signing, "stale_runner_profiles") as find:
            manager._drop_stale_profiles("ABCDE12345", log)
        find.assert_not_called()


class UsbCountTests(unittest.TestCase):
    def test_iphones_on_the_bus_are_counted_but_virtual_ones_are_not(self):
        tree = [{"_name": "USB 3.1 Bus", "_items": [
                    {"_name": "iPhone", "USBDeviceKeyVendorID": "0x05ac", "USBKeyHardwareType": "Removable"},
                    {"_name": "Keyboard", "USBDeviceKeyVendorID": "0x05ac"}]},
                {"_name": "Simulated Bus", "USBKeyHardwareType": "Simulated", "_items": [
                    {"_name": "iPhone Research Environment Virtual Machine", "USBDeviceKeyVendorID": "0x05ac"},
                    {"_name": "iPhone", "USBDeviceKeyVendorID": "0x05ac"}]}]
        manager = dm.DeviceManager(tempfile.mkdtemp())
        with patch.object(mac_tools, "run", return_value=json.dumps({"SPUSBHostDataType": tree})):
            self.assertEqual(manager.usb_iphones(), 1)
        manager.refresh()
        with patch.object(mac_tools, "run", return_value=None):
            self.assertIsNone(manager.usb_iphones())


class ExpiryTests(unittest.TestCase):
    def manager(self, expires_at):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        products = manager.derived / "Build" / "Products"
        products.mkdir(parents=True)
        (products / "WebDriverAgentRunner.xctestrun").write_text("")
        manager.save_settings(udid="u", built_for="u", built_patches=wda_patches.patch_digest(), team="ABCDE12345",
                              expires_at=expires_at)
        return manager

    def test_an_expired_signature_is_not_built_and_the_runner_says_rebuild(self):
        manager = self.manager(time.time() - 60)
        self.assertTrue(manager.expired())
        self.assertFalse(manager.built())
        self.assertEqual(manager.runner_hint(), runner_hints.EXPIRED_HINT)
        with patch.object(manager, "device", return_value={"udid": "u"}), self.assertRaises(LookupError) as refused:
            manager.start_runner()
        self.assertEqual(str(refused.exception), runner_hints.EXPIRED_HINT)
        manager.save_settings(autostart=True)
        with patch.object(manager, "device", return_value={"udid": "u"}), patch.object(manager,
                                                                                       "_start_runner") as start:
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


class UsbIdentityTests(unittest.TestCase):
    def test_usb_phone_identity_has_a_marketing_name_and_no_identifiers(self):
        from mobile_agent.device_info import DeviceInfo, usb_info
        info = usb_info({"udid": "00008130-SECRET", "model": "iPhone16,1", "ios": "26.0.1", "trusted": True})
        self.assertEqual((info["modelName"], info["iosVersion"], info["virtual"]), ("iPhone 15 Pro", "26.0.1", False))
        self.assertNotIn("00008130-SECRET", str(info))
        manager = SimpleNamespace(device=lambda: {"model": "iPhone18,4", "ios": "26.1", "trusted": True})
        self.assertEqual(DeviceInfo(manager=manager).snapshot()["modelName"], "iPhone Air")

    def test_an_ipad_is_named_by_its_model_with_wifi_and_cellular_as_one(self):
        from mobile_agent.device_info import usb_info
        self.assertEqual([usb_info({"model": model, "ios": "26.1"})["modelName"] for model in ("iPad17,3", "iPad17,4",
                                                                                               "iPad15,7", "iPad16,1")],
                         ["iPad Pro 13-inch (M5)", "iPad Pro 13-inch (M5)", "iPad (A16)", "iPad mini (A17 Pro)"])
        self.assertEqual(usb_info({"model": "iPad16,8", "ios": "26.1"})["modelIdentifier"], "iPad16,8")

    def test_unknown_or_malformed_models_are_never_guessed(self):
        from mobile_agent.device_info import usb_info
        self.assertIsNone(usb_info({"model": "iPhone99,9", "ios": "26.0"})["modelName"])
        self.assertIsNone(usb_info({"model": "rm -rf", "ios": "x"})["modelIdentifier"])
        self.assertIsNone(usb_info({"model": "iPad99,9", "ios": "26.0"})["modelName"])
        self.assertIsNone(usb_info({"model": "iPadOS", "ios": "26.0"})["modelIdentifier"])


if __name__ == "__main__":
    unittest.main()
