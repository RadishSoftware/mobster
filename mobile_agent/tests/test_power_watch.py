import os
import plistlib
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent import device_status, power_watch
from mobile_agent.frontier import BATTERY_PAUSED_FEEDBACK, BATTERY_TOO_LONG
from mobile_agent.harness import control
from mobile_agent.run import Run
from mobile_agent.server import make_handler
from mobile_agent.steering import SteeringQueue
from mobile_agent.tests.seam_support import isolate, request
from mobile_agent.tests.test_frontier import Script
from mobile_agent.tests.test_harness_steering import Watched, agent, recording, rows
from mobile_agent.tests.test_server_engine import Base
from mobile_agent.tests.test_server_hardening import APP
from mobile_agent.tests.timing import bound

PMSET_AC = (
    "Now drawing from 'AC Power'\n -InternalBattery-0 (id=21889123)\t100%; charged; 0:00 remaining "
    "present: true\n")
PMSET_BATTERY = (
    "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=21889123)\t18%; discharging; 0:41 remaining "
                 "present: true\n")
PMSET_CHARGING = (
    "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t64%; charging; 1:10 remaining present: true\n")
PMSET_HELD = "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t80%; AC attached; not charging present: true\n"
PMSET_DESKTOP = "Now drawing from 'AC Power'\n"


class MacTests(unittest.TestCase):
    def test_pmset_reads_as_its_power_source_and_battery(self):
        self.assertEqual(device_status.parse_pmset(PMSET_AC), {"power": "ac", "battery": 100, "charging": False})
        self.assertEqual(device_status.parse_pmset(PMSET_BATTERY),
                         {"power": "battery", "battery": 18, "charging": False})
        self.assertEqual(device_status.parse_pmset(PMSET_CHARGING), {"power": "ac", "battery": 64, "charging": True})
        self.assertEqual(device_status.parse_pmset(PMSET_HELD), {"power": "ac", "battery": 80, "charging": False})

    def test_a_mac_with_no_battery_or_no_answer_reports_only_what_it_can(self):
        self.assertEqual(device_status.parse_pmset(PMSET_DESKTOP), {"power": "ac"})
        self.assertEqual(device_status.parse_pmset(""), {"power": None})
        self.assertEqual(device_status.read_mac(run=lambda argv: None), {"power": None})
        self.assertEqual(device_status.read_mac(run=lambda argv: PMSET_BATTERY.encode())["battery"], 18)


class PhoneTests(unittest.TestCase):
    def test_lockdown_battery_states(self):
        read = device_status.lockdown_battery
        self.assertEqual(read({"BatteryCurrentCapacity": 81, "BatteryIsCharging": True, "ExternalConnected": True,
                               "FullyCharged": False}), {"level": 81, "state": "charging"})
        self.assertEqual(read({"BatteryCurrentCapacity": 100, "BatteryIsCharging": False, "ExternalConnected": True,
                               "FullyCharged": True}), {"level": 100, "state": "full"})
        self.assertEqual(read({"BatteryCurrentCapacity": 9, "BatteryIsCharging": False, "ExternalConnected": True,
                               "FullyCharged": False}), {"level": 9, "state": "not_charging"})
        self.assertEqual(read({"BatteryCurrentCapacity": 9, "BatteryIsCharging": False, "ExternalConnected": False}),
                         {"level": 9, "state": "unplugged"})
        self.assertEqual(read({"BatteryCurrentCapacity": 50}), {"level": 50})
        for broken in (None, {}, {"BatteryCurrentCapacity": "50"}, {"BatteryCurrentCapacity": 140},
                       {"BatteryCurrentCapacity": True}, {"BatteryCurrentCapacity": 50, "HasBattery": False}):
            self.assertIsNone(read(broken), broken)

    def test_wda_battery_and_thermal(self):
        self.assertEqual(device_status.wda_battery({"level": .85, "state": 2}), {"level": 85, "state": "charging"})
        self.assertEqual(device_status.wda_battery({"level": .1, "state": 1}), {"level": 10, "state": "unplugged"})
        self.assertEqual(device_status.wda_battery({"level": .5, "state": 0}), {"level": 50})
        self.assertIsNone(device_status.wda_battery({"level": -1, "state": 0}))
        self.assertEqual(device_status.wda_thermal({"thermalState": 0}), "nominal")
        self.assertEqual(device_status.wda_thermal({"thermalState": 2}), "serious")
        self.assertIsNone(device_status.wda_thermal({"thermalState": 7}))
        self.assertIsNone(device_status.wda_thermal({}))

    def test_read_lockdown_asks_only_the_battery_domain_of_that_phone(self):
        calls = []

        def run(argv):
            calls.append(argv)
            return plistlib.dumps({"BatteryCurrentCapacity": 40, "BatteryIsCharging": True}, fmt=plistlib.FMT_XML)
        self.assertEqual(device_status.read_lockdown("<UDID>", run=run, tool="/bin/ideviceinfo"),
                         {"level": 40, "state": "charging"})
        self.assertEqual(calls, [["/bin/ideviceinfo", "-u", "<UDID>", "-q", "com.apple.mobile.battery", "-x"]])
        self.assertIsNone(device_status.read_lockdown("<UDID>", run=lambda argv: b"not a plist", tool="x"))
        self.assertIsNone(device_status.read_lockdown("<UDID>", run=run, tool=""))

    def test_a_usb_phone_reads_lockdown_first_and_wda_only_for_what_is_missing(self):
        asked = []

        def wda(url, battery):
            asked.append(battery)
            return {"level": 85, "state": "charging"}, "fair"
        with patch.dict(os.environ, {"MOBSTER_NO_DEVICES": ""}):
            status = device_status.read_phone("usb", udid="<UDID>", wda_url="http://127.0.0.1:8100",
                                              lockdown=lambda udid: {"level": 83, "state": "charging"}, wda=wda)
            self.assertEqual(status["battery"], {"level": 83, "state": "charging", "source": "lockdown"})
            self.assertEqual((status["thermal"], asked), ("fair", [False]))
            self.assertEqual(set(status), {"battery", "thermal", "readAt"})
            status = device_status.read_phone("usb", udid="<UDID>", wda_url="http://127.0.0.1:8100",
                                              lockdown=lambda udid: None, wda=wda)
            self.assertEqual(status["battery"]["source"], "wda")
            status = device_status.read_phone("simulator", udid="<UDID>", wda_url="http://127.0.0.1:8310",
                                              lockdown=self.fail, wda=self.fail)
            self.assertEqual(set(status), {"readAt"})

    def test_nothing_is_read_with_devices_off(self):
        with patch.dict(os.environ, {"MOBSTER_NO_DEVICES": "1"}):
            status = device_status.read_phone("usb", udid="<UDID>", wda_url="http://127.0.0.1:8100",
                                              lockdown=self.fail, wda=self.fail)
        self.assertEqual(set(status), {"readAt"})

    def test_words(self):
        phone = {"battery": {"level": 9, "state": "unplugged"},
                 "storage": {"freeBytes": 41_234_000_000, "totalBytes": 128_000_000_000}, "thermal": "nominal"}
        self.assertEqual(device_status.rows(phone, {"power": "battery", "battery": 54}),
                         [("Battery", "9%, not charging"), ("Thermal", "nominal"),
                          ("This Mac", "on battery, battery 54%")])
        self.assertEqual(device_status.rows({}, {"power": None}), [("Battery", "not reported")])
        self.assertEqual(device_status.describe("Studio iPhone", phone, {"power": "ac"}),
                         ["Studio iPhone: Battery 9%, not charging; Thermal nominal.",
                          "This Mac is on power."])


class FakeKeepAwake:
    enabled = True

    def __init__(self):
        self.held, self.holds, self.releases = False, 0, 0

    def hold(self):
        if not self.held:
            self.holds += 1
        self.held = True

    def release(self):
        if self.held:
            self.releases += 1
        self.held = False


class AwakeTests(unittest.TestCase):
    def test_held_while_any_task_or_schedule_needs_it_released_after_the_last(self):
        keep = FakeKeepAwake()
        holds = power_watch.AwakeHolds()
        holds.hold("run-1")
        holds.attach(keep)
        self.assertTrue(keep.held)
        schedule = holds.handle("schedule")
        schedule.hold()
        holds.release("run-1")
        self.assertTrue(keep.held)
        self.assertEqual(holds.status(), {"keptAwake": True, "keepAwake": True, "for": ["schedule"]})
        schedule.release()
        self.assertFalse(keep.held)
        self.assertEqual((keep.holds, keep.releases), (1, 1))
        self.assertEqual(holds.status(), {"keptAwake": False, "keepAwake": True, "for": []})

    def test_without_a_keep_awake_it_only_counts(self):
        holds = power_watch.AwakeHolds()
        holds.hold("run-1")
        self.assertEqual(holds.status(), {"keptAwake": False, "keepAwake": False, "for": ["task"]})
        holds.release("run-1")


def fake_runtime(*runs):
    published = []
    runtime = SimpleNamespace(lock=threading.Lock(), runs={run.id: run for run in runs},
                              active_runs={run.id: None for run in runs}, published=published,
                              publish=lambda topic, event: published.append((topic, event)),
                              phone_for=lambda run: SimpleNamespace(key="@primary"))
    return runtime


def long_run(origin="app", engine="smart", longform=True):
    run = Run(dict(APP), "Reply to my unread messages", "live", engine=engine, origin=origin)
    run.longform = {"plan": {}} if longform else None
    run.status = "running"
    return run


class Battery:
    def __init__(self, level, state):
        self.value = {"battery": {"level": level, "state": state}, "readAt": 1}
        self.reads = 0

    def __call__(self, phone):
        self.reads += 1
        return dict(self.value)

    def set(self, level, state):
        self.value = {"battery": {"level": level, "state": state}, "readAt": 1}


def watch(runtime, battery=None, mac=None):
    return power_watch.PowerWatch(runtime, read_phone=battery or Battery(80, "charging"),
                                  read_mac=mac or (lambda: {"power": "ac", "battery": 100, "charging": False}))


class MacBatteryWarningTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"MOBSTER_MAC_BATTERY_WARN": "", "MOBSTER_PHONE_BATTERY_PAUSE": ""})
        env.start()
        self.addCleanup(env.stop)

    def test_each_running_task_hears_once_and_the_bus_once_and_plugging_in_rearms(self):
        first, second = long_run(), long_run()
        runtime = fake_runtime(first, second)
        mac = {"value": {"power": "battery", "battery": 18}}
        power = watch(runtime, mac=lambda: dict(mac["value"]))
        power.tick()
        power.tick()
        for run in (first, second):
            warned = [e for e in run.events if e["event"] == "mac_battery_low"]
            self.assertEqual([(e["percent"], e["threshold"]) for e in warned], [(18, 20)])
        self.assertEqual([topic for topic, _ in runtime.published], ["power"])
        self.assertEqual(power.warnings()[0]["code"], "mac_battery_low")
        self.assertIn("18%", power.warnings()[0]["message"])
        mac["value"] = {"power": "ac", "battery": 19, "charging": True}
        power.tick()
        self.assertEqual(power.warnings(), [])
        mac["value"] = {"power": "battery", "battery": 17}
        power.tick()
        self.assertEqual(len([e for e in first.events if e["event"] == "mac_battery_low"]), 2)

    def test_no_warning_above_the_threshold_on_power_with_no_battery_or_turned_off(self):
        run = long_run()
        for mac, env in (({"power": "battery", "battery": 21}, ""), ({"power": "ac", "battery": 5}, ""),
                         ({"power": "ac"}, ""), ({"power": None}, ""), ({"power": "battery", "battery": 5}, "off")):
            with self.subTest(mac=mac, env=env), patch.dict(os.environ, {"MOBSTER_MAC_BATTERY_WARN": env}):
                power = watch(fake_runtime(run), mac=lambda: dict(mac))
                power.tick()
                self.assertEqual(power.warnings(), [])
        self.assertNotIn("mac_battery_low", [e["event"] for e in run.events])

    def test_the_threshold_can_be_moved(self):
        run = long_run()
        with patch.dict(os.environ, {"MOBSTER_MAC_BATTERY_WARN": "40"}):
            watch(fake_runtime(run), mac=lambda: {"power": "battery", "battery": 35}).tick()
        self.assertEqual([e["threshold"] for e in run.events if e["event"] == "mac_battery_low"], [40])

    def test_nonfinite_thresholds_keep_the_default_battery_protection(self):
        for value in ("inf", "-inf", "1e999", "nan"):
            with self.subTest(value=value), patch.dict(os.environ, {"MOBSTER_MAC_BATTERY_WARN": value,
                                                                   "MOBSTER_PHONE_BATTERY_PAUSE": value}):
                run = long_run()
                power = watch(fake_runtime(run), Battery(9, "unplugged"),
                              mac=lambda: {"power": "battery", "battery": 18})
                power.tick()
                self.assertIn("mac_battery_low", [e["event"] for e in run.events])
                self.assertTrue(control.paused(run.steering))


class PhoneBatteryPauseTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"MOBSTER_MAC_BATTERY_WARN": "", "MOBSTER_PHONE_BATTERY_PAUSE": ""})
        env.start()
        self.addCleanup(env.stop)

    def test_a_long_task_pauses_on_a_low_draining_battery_and_goes_on_once_it_charges(self):
        run = long_run()
        battery = Battery(9, "unplugged")
        power = watch(fake_runtime(run), battery)
        power.tick()
        self.assertTrue(control.paused(run.steering))
        self.assertEqual(control.reason(run.steering), {"code": "phone_battery", "level": 9,
                                                        "limit": power_watch.BATTERY_PAUSE_LIMIT})
        battery.set(8, "unplugged")
        power.tick()
        self.assertTrue(control.paused(run.steering))
        battery.set(8, "charging")
        power.tick()
        self.assertFalse(control.paused(run.steering))
        self.assertEqual(power.paused, {})

    def test_a_scheduled_task_pauses_too_and_plugged_in_without_charging_counts_as_draining(self):
        run = long_run(origin="schedule", longform=False)
        watch(fake_runtime(run), Battery(10, "not_charging")).tick()
        self.assertTrue(control.paused(run.steering))

    def test_it_doesnt_pause_when_it_shouldnt(self):
        cases = {
            "charging": (long_run(), Battery(5, "charging"), ""),
            "full": (long_run(), Battery(5, "full"), ""),
            "above the threshold": (long_run(), Battery(11, "unplugged"), ""),
            "turned off": (long_run(), Battery(5, "unplugged"), "off"),
            "a short task": (long_run(longform=False), Battery(5, "unplugged"), ""),
            "Quick mode": (long_run(engine="fast"), Battery(5, "unplugged"), ""),
            "unknown state": (long_run(), Battery(5, None), ""),
        }
        for name, (run, battery, env) in cases.items():
            with self.subTest(name), patch.dict(os.environ, {"MOBSTER_PHONE_BATTERY_PAUSE": env}):
                watch(fake_runtime(run), battery).tick()
                self.assertFalse(control.paused(run.steering))
        run = long_run()
        unreadable = Battery(5, "unplugged")
        unreadable.value = {"readAt": 1}
        watch(fake_runtime(run), unreadable).tick()
        self.assertFalse(control.paused(run.steering))

    def test_the_threshold_moves_with_the_setting(self):
        run = long_run()
        with patch.dict(os.environ, {"MOBSTER_PHONE_BATTERY_PAUSE": "25"}):
            watch(fake_runtime(run), Battery(20, "unplugged")).tick()
        self.assertTrue(control.paused(run.steering))

    def test_a_person_wins_both_ways(self):
        run = long_run()
        control.set_paused(run.steering, True)
        battery = Battery(5, "charging")
        power = watch(fake_runtime(run), battery)
        power.tick()
        self.assertTrue(control.paused(run.steering))
        self.assertIsNone(control.reason(run.steering))
        run = long_run()
        battery = Battery(5, "unplugged")
        power = watch(fake_runtime(run), battery)
        power.tick()
        control.set_paused(run.steering, False)
        power.tick()
        power.tick()
        self.assertFalse(control.paused(run.steering))
        self.assertIn(run.id, power.kept_going)

    def test_turning_it_off_lets_a_paused_task_go_on(self):
        run = long_run()
        power = watch(fake_runtime(run), Battery(5, "unplugged"))
        power.tick()
        with patch.dict(os.environ, {"MOBSTER_PHONE_BATTERY_PAUSE": "off"}):
            power.tick()
        self.assertFalse(control.paused(run.steering))


class FrontierBatteryPauseTests(unittest.TestCase):
    def test_the_pause_says_why_and_the_model_hears_mobster_paused_it(self):
        queue = SteeringQueue()
        control.set_paused(queue, True, reason={"code": "phone_battery", "level": 9, "limit": 60})
        script = Script([("TAP", "Row 0", None), ("DONE", None, None), ("DONE", None, None)])
        prompts = recording(script)
        events = []
        threading.Timer(.2, control.set_paused, args=(queue, False)).start()
        result = agent(Watched(rows()), script, queue, events).run("Tap the first row")
        paused = next(e for e in events if e["event"] == "paused")
        resumed = next(e for e in events if e["event"] == "continued")
        self.assertEqual((paused.get("reason"), paused.get("level"), resumed.get("reason")),
                         ("phone_battery", 9, "phone_battery"))
        self.assertIn(BATTERY_PAUSED_FEEDBACK, prompts[0])
        self.assertIn("This turn so far: Mobster paused you while the iPhone's battery was low, then let you go on",
                      prompts[0])
        self.assertEqual(result["status"], "completed")
        self.assertLess(result["elapsed"], bound(.2))

    def test_a_battery_pause_has_its_own_limit_and_reason(self):
        queue = SteeringQueue()
        control.set_paused(queue, True, reason={"code": "phone_battery", "level": 4, "limit": .2})
        driver = Watched(rows())
        started = time.monotonic()
        result = agent(driver, Script([("TAP", "Row 0", None)]), queue).run("Tap the first row")
        self.assertLess(time.monotonic() - started, bound(5))
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["reason"], BATTERY_TOO_LONG["reason"])
        self.assertEqual([kind for kind, _ in driver.log if kind == "act"], [])

    def test_a_persons_pause_reads_as_before(self):
        queue = SteeringQueue()
        control.set_paused(queue, True)
        events = []
        threading.Timer(.1, control.set_paused, args=(queue, False)).start()
        agent(Watched(rows()), Script([("DONE", None, None), ("DONE", None, None)]), queue, events).run("Look")
        self.assertEqual([{k: v for k, v in e.items() if k != "t"} for e in events
                          if e["event"] in ("paused", "continued")],
                         [{"event": "paused", "step": 0}, {"event": "continued", "step": 0}])


class CliTests(unittest.TestCase):
    PHONES = [{"id": "<UDID>", "kind": "usb", "name": "Studio iPhone", "udid": "<UDID>", "primary": True,
               "wdaUrl": "http://127.0.0.1:8100"},
              {"id": "<UDID2>", "kind": "usb", "name": "Work iPhone", "udid": "<UDID2>", "primary": False,
               "wdaUrl": "http://127.0.0.1:8101"}]

    def status(self, *argv, found=None, tty=False):
        import contextlib
        import io

        from mobile_agent import __main__ as cli
        out, asked = io.StringIO(), []

        def read_phone(kind, udid=None, wda_url=None):
            asked.append(udid)
            return {"battery": {"level": 77, "state": "charging", "source": "lockdown"}, "thermal": "nominal",
                    "readAt": 1}
        out.isatty = lambda: tty
        with patch("mobile_agent.devices.discover", return_value=self.PHONES if found is None else found), \
                patch.object(device_status, "read_phone", read_phone), \
                patch.object(device_status, "read_mac", return_value={"power": "ac"}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["devices", "status", *argv])
        return code, out.getvalue(), asked

    def test_it_reads_the_named_device_or_the_first_set_up(self):
        import json
        code, text, asked = self.status("--json", "--device", "Work iPhone")
        self.assertEqual((code, asked), (0, ["<UDID2>"]))
        result = json.loads(text)
        self.assertEqual((result["device"], result["phone"]["battery"]["level"], result["mac"]), ("<UDID2>", 77,
                                                                                                   {"power": "ac"}))
        code, _, asked = self.status("--json")
        self.assertEqual((code, asked), (0, ["<UDID>"]))

    def test_it_prints_rows_in_a_terminal(self):
        import re
        code, text, _ = self.status(tty=True)
        text = re.sub(r"\x1b\[[0-9;]*m", "", text)
        self.assertEqual(code, 0)
        self.assertIn("Studio iPhone", text)
        self.assertRegex(text, r"Battery\s+77%, charging")
        self.assertRegex(text, r"This Mac\s+on power")

    def test_no_device_is_exit_3(self):
        code, _, asked = self.status("--json", found=[])
        self.assertEqual((code, asked), (3, []))
        code, _, _ = self.status("--json", "--device", "Nobody's iPhone")
        self.assertEqual(code, 3)


class RuntimeTests(Base):
    def setUp(self):
        super().setUp()
        isolate(self)
        self.app = self.runtime()
        self.handler = make_handler(self.app)

    def test_status_routes_answer_with_the_phone_the_mac_and_the_warnings(self):
        self.app.power.read_phone = lambda phone: {"battery": {"level": 42, "state": "unplugged"}, "readAt": 5}
        self.app.power.read_mac = lambda: {"power": "battery", "battery": 64, "charging": False}
        status, data, _ = request(self.handler, "GET", "/api/device/status")
        self.assertEqual(status, 200)
        self.assertEqual(data["phone"]["battery"], {"level": 42, "state": "unplugged"})
        self.assertEqual(data["mac"], {"power": "battery", "battery": 64, "charging": False, "keptAwake": False,
                                       "keepAwake": False, "for": []})
        self.assertEqual((data["warnings"], data["phoneBatteryPause"]), ([], 10))

    def test_the_pause_setting_saves_and_turns_off(self):
        self.assertEqual(self.app.settings()["phoneBatteryPause"], 10)
        self.assertEqual(self.app.update_settings({"phoneBatteryPause": 15})["phoneBatteryPause"], 15)
        self.assertEqual(os.environ["MOBSTER_PHONE_BATTERY_PAUSE"], "15")
        self.assertIsNone(self.app.update_settings({"phoneBatteryPause": None})["phoneBatteryPause"])
        self.assertEqual(os.environ["MOBSTER_PHONE_BATTERY_PAUSE"], "off")
        for bad in (0, 51, 10.5, "10", True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.app.update_settings({"phoneBatteryPause": bad})

    def test_a_running_task_keeps_the_mac_awake_until_it_ends(self):
        keep = FakeKeepAwake()
        self.app.power.awake.attach(keep)
        held = []
        run = Run(dict(APP), "Open Settings", "live")
        self.app.runs[run.id] = run
        self.app.active_runs[run.id] = None

        def smart(run, trace, setup):
            held.append(keep.held)
            return {"event": "result", "status": "completed"}
        self.worker.stop()
        try:
            with patch.object(self.app, "run_smart", side_effect=smart):
                self.app.work(run)
        finally:
            self.worker.start()
        self.assertEqual(held, [True])
        self.assertFalse(keep.held)
        self.assertEqual((keep.holds, keep.releases), (1, 1))


if __name__ == "__main__":
    unittest.main()
