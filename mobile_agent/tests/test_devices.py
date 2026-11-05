import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from mobile_agent import devices
from mobile_agent.device_manager import DeviceManager
from mobile_agent.devices import REASONS, AmbiguousDevice, DeviceStore, allocate_slot, record, resolve, usb_state

PRO = "00008130-001A2B3C4D5E6F70"
XR = "00008020-000A1B2C3D4E5F60"
SE = "00008030-000B2C3D4E5F6071"


class FakeManager:
    def __init__(self, built=True, expired=False, built_for=None, running=False, stale=False):
        self._built, self._expired, self._stale = built, expired, stale
        self._settings = {"udid": built_for, "built_for": built_for}
        self.runner = type("Runner", (), {"running": running})() if running else None

    def settings(self):
        return dict(self._settings)

    def built(self):
        return self._built

    def expired(self):
        return self._expired

    def out_of_date(self):
        return self._stale


class SlotTests(unittest.TestCase):
    def test_the_lowest_free_slot_whose_ports_nothing_listens_on(self):
        self.assertEqual(allocate_slot([], busy=lambda port: False), 1)
        self.assertEqual(allocate_slot([{"slot": 1}, {"slot": 3}], busy=lambda port: False), 2)
        self.assertEqual(allocate_slot([{"slot": 1}], busy=lambda port: port in (8102, 9103)), 4)

    def test_a_full_range_says_what_to_do(self):
        with self.assertRaisesRegex(LookupError, "Forget a phone"):
            allocate_slot([{"slot": n} for n in range(1, 100)], busy=lambda port: False)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = DeviceStore(self.folder.name)

    def test_a_phone_keeps_its_slot_and_the_file_is_private(self):
        first = self.store.add(XR, "Work iPhone", busy=lambda port: False)
        second = self.store.add(SE, "Test iPhone", busy=lambda port: False)
        self.assertEqual((first["slot"], first["wdaPort"], first["mjpegPort"]), (1, 8101, 9101))
        self.assertEqual((second["slot"], second["wdaPort"]), (2, 8102))
        self.assertEqual(self.store.add(XR, busy=lambda port: True)["slot"], 1)
        self.assertEqual(Path(self.store.path).stat().st_mode & 0o777, 0o600)
        self.store.remove(XR)
        self.assertEqual([entry["udid"] for entry in self.store.phones()], [SE])
        self.assertEqual(self.store.add(PRO, busy=lambda port: False)["slot"], 1)

    def test_the_composers_phones_are_kept_in_order_and_cleared_with_their_phone(self):
        self.store.add(XR, busy=lambda port: False)
        self.store.set_chosen([XR, SE])
        self.assertEqual(DeviceStore(self.folder.name).read()["chosen"], [XR, SE])
        self.store.remove(XR)
        self.assertEqual(self.store.read()["chosen"], [SE])
        for bad in ([XR, XR], ["../x"], [7], XR):
            with self.assertRaises(ValueError):
                self.store.set_chosen(bad)
        Path(self.store.path).write_text(json.dumps({"chosen": [SE, "bad id", SE, 3]}))
        self.assertEqual(self.store.read()["chosen"], [SE])

    def test_a_damaged_file_reads_empty_and_is_rewritten(self):
        Path(self.store.path).write_text("{not json")
        self.assertEqual(self.store.phones(), [])
        Path(self.store.path).write_text(json.dumps({"phones": [{"udid": "bad", "slot": 1},
                                                                {"udid": XR, "slot": 400}]}))
        self.assertEqual(self.store.phones(), [])
        self.store.add(XR, busy=lambda port: False)
        self.assertEqual(json.loads(Path(self.store.path).read_text())["phones"][0]["udid"], XR)

    def test_concurrent_adds_never_share_a_slot(self):
        udids = [f"00008030-{index:016X}" for index in range(12)]
        threads = [threading.Thread(target=self.store.add, args=(udid,), kwargs={"busy": lambda port: False})
                   for udid in udids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        slots = [entry["slot"] for entry in self.store.phones()]
        self.assertEqual(sorted(slots), list(range(1, 13)))

    def test_malformed_phone_collections_and_port_assignments_are_ignored(self):
        for entries in (1, True, "phones", {"phone": XR},
                        [{"udid": XR, "slot": True, "wdaPort": 8101, "mjpegPort": 9101}],
                        [{"udid": XR, "slot": 1}],
                        [{"udid": XR, "slot": 1, "wdaPort": 8100, "mjpegPort": 9100}]):
            with self.subTest(entries=entries):
                Path(self.store.path).write_text(json.dumps({"phones": entries}))
                self.assertEqual(self.store.phones(), [])
        self.store.add(XR, busy=lambda port: False)
        self.assertEqual(self.store.phones()[0]["wdaPort"], 8101)


class ResolveTests(unittest.TestCase):
    RECORDS = [record(id=PRO, kind="usb", name="My iPhone", udid=PRO, state="ready", wda_url_value="u1"),
               record(id=XR, kind="usb", name="Work iPhone", udid=XR, state="busy", wda_url_value="u2"),
               record(id="SIM-1", kind="simulator", name="Mobster · iPhone 17 Pro · iOS 26.4", udid="SIM-1",
                      state="ready", wda_url_value="u3"),
               record(id="SIM-2", kind="simulator", name="Twin", state="ready", wda_url_value="u4"),
               record(id="SIM-3", kind="simulator", name="twin", state="ready", wda_url_value="u5")]

    def test_a_name_two_devices_share_must_be_named_by_id(self):
        with self.assertRaisesRegex(AmbiguousDevice, "SIM-2, SIM-3"):
            resolve(self.RECORDS, "TWIN")


class StateTests(unittest.TestCase):
    def state(self, device, manager, ready=True, free=True, locked=None):
        return usb_state(device, manager, lambda: ready, lambda: free, locked)

    def test_each_state_and_its_reason(self):
        trusted = {"udid": PRO, "trusted": True}
        self.assertEqual(self.state(None, FakeManager()), ("disconnected", REASONS["unplugged"]))
        self.assertEqual(self.state({"udid": PRO, "trusted": False}, FakeManager()),
                         ("needs_setup", REASONS["untrusted"]))
        self.assertEqual(self.state(trusted, None), ("needs_setup", REASONS["not_built"]))
        self.assertEqual(self.state(trusted, FakeManager(built=False)), ("needs_setup", REASONS["not_built"]))
        self.assertEqual(self.state(trusted, FakeManager(built=False, built_for=XR)),
                         ("needs_setup", REASONS["built_for_other"]))
        self.assertEqual(self.state(trusted, FakeManager(built=False, stale=True)),
                         ("needs_setup", REASONS["out_of_date"]))
        self.assertEqual(self.state(trusted, FakeManager(expired=True)), ("needs_setup", REASONS["expired"]))
        self.assertEqual(self.state(trusted, FakeManager(), ready=False), ("connected", REASONS["stopped"]))
        self.assertEqual(self.state(trusted, FakeManager(running=True), ready=False),
                         ("connected", REASONS["starting"]))
        waiting = FakeManager(running=True)
        waiting.runner_cause = "ui_automation_off"
        self.assertEqual(self.state(trusted, waiting, ready=False), ("connected", REASONS["ui_automation"]))
        first = FakeManager(running=True)
        first.prearm = lambda: "ui_automation"
        self.assertEqual(self.state(trusted, first, ready=False), ("connected", REASONS["prearm"]))
        self.assertEqual(self.state(trusted, FakeManager(), free=False), ("busy", REASONS["busy_elsewhere"]))
        self.assertEqual(self.state(trusted, FakeManager(), locked=True), ("connected", REASONS["locked"]))
        self.assertEqual(self.state(trusted, FakeManager()), ("ready", None))

    def test_a_simulator_row_becomes_a_device(self):
        row = {"udid": "SIM-1", "name": "Mobster · iPhone 17 Pro · iOS 26.4", "device_type": "iPhone 17 Pro",
               "runtime": "iOS 26.4", "state": "Booted", "wda_url": "http://127.0.0.1:8310",
               "mjpeg_url": "http://127.0.0.1:9310"}
        item = devices.simulator_record(row, ready=True, free=True)
        self.assertEqual((item["kind"], item["state"], item["ios"], item["model"]),
                         ("simulator", "ready", "26.4", "iPhone 17 Pro"))
        self.assertEqual(devices.simulator_record(row, ready=False, free=True)["state"], "connected")
        self.assertEqual(devices.simulator_record(row, ready=True, free=False)["state"], "busy")
        self.assertEqual(devices.simulator_record({**row, "state": "Shutdown"})["state"], "disconnected")
        self.assertEqual(devices.simulator_record(row, busy_here=True)["reason"], REASONS["busy"])


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)

    def test_the_primary_keeps_its_files_and_another_phone_gets_its_own(self):
        primary = DeviceManager(self.root)
        self.assertEqual((primary.settings_path, primary.derived, primary.logs, primary.wda_port, primary.video_port),
                         (self.root / "device.json", self.root / "wda-build", self.root / "logs", 8100, 9100))
        folder = devices.phone_folder(self.root, XR)
        folder.mkdir(parents=True)
        other = DeviceManager(self.root, "http://127.0.0.1:8101", udid=XR, root=folder, video_port=9101)
        self.assertEqual((other.settings_path, other.derived, other.wda_port, other.video_port),
                         (folder / "device.json", folder / "wda-build", 8101, 9101))
        self.assertEqual(other.project, primary.project)
        self.assertEqual(other.settings()["udid"], XR)

    def test_a_pinned_manager_drives_only_its_phone(self):
        folder = devices.phone_folder(self.root, XR)
        folder.mkdir(parents=True)
        other = DeviceManager(self.root, "http://127.0.0.1:8101", udid=XR, root=folder)
        listing = [{"udid": PRO, "trusted": True, "name": "A"}, {"udid": XR, "trusted": True, "name": "B"}]
        self.assertEqual(other.device(listing)["udid"], XR)
        self.assertIsNone(other.device(listing[:1]))
        with self.assertRaises(LookupError):
            other.choose(PRO)

    def test_the_primary_never_picks_a_phone_another_manager_drives(self):
        primary = DeviceManager(self.root)
        listing = [{"udid": PRO, "trusted": True}, {"udid": XR, "trusted": True}]
        self.assertIsNone(primary.device(listing))
        primary.claimed = lambda: {XR}
        self.assertEqual(primary.device(listing)["udid"], PRO)
        primary.claimed = lambda: {PRO, XR}
        self.assertIsNone(primary.device(listing))
        with patch.object(DeviceManager, "devices", return_value=listing), \
                self.assertRaisesRegex(LookupError, "already set up as another device"):
            primary.choose(XR)

    def test_reading_a_phone_never_writes_its_folder(self):
        DeviceManager(self.root, "http://127.0.0.1:8101", udid=XR, root=devices.phone_folder(self.root, XR))
        self.assertFalse(devices.phone_folder(self.root, XR).exists())

    def test_builds_wait_for_each_other(self):
        from mobile_agent import device_manager
        order = []
        manager = DeviceManager(self.root)
        manager._build_locked = lambda team, udid, log, tail: order.append(udid)
        device_manager.BUILD_LOCK.acquire()
        thread = threading.Thread(target=manager._build, args=("ABCDE12345", XR))
        thread.start()
        thread.join(.3)
        self.assertTrue(thread.is_alive())
        self.assertEqual(manager.build["log_tail"], ["Waiting for another iPhone's build to finish."])
        device_manager.BUILD_LOCK.release()
        thread.join(5)
        self.assertEqual(order, [XR])


class LeaseProbeTests(unittest.TestCase):
    def setUp(self):
        import os
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        patcher = patch.dict(os.environ, {"HOME": home.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.url = "http://127.0.0.1:18411"

    def test_a_lease_held_for_an_instant_is_taken_once_it_is_free(self):
        import inspect

        from mobile_agent.journal import Lease
        self.assertGreater(inspect.signature(Lease.device).parameters["wait"].default, 0)
        probe = Lease.device(self.url, wait=0)
        threading.Timer(0.03, probe.close).start()
        Lease.device(self.url, wait=5.0).close()

    def test_a_lease_held_by_a_task_is_still_refused(self):
        import time

        from mobile_agent.journal import Lease, LeaseHeld
        held = Lease.device(self.url)
        self.addCleanup(held.close)
        started = time.monotonic()
        with self.assertRaises(LeaseHeld):
            Lease.device(self.url)
        self.assertLess(time.monotonic() - started, 1.0)
        started = time.monotonic()
        self.assertFalse(devices.lease_free(self.url))
        self.assertLess(time.monotonic() - started, 0.05)
        held.close()
        self.assertTrue(devices.lease_free(self.url))


class DiscoverTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        (self.root / "device.json").write_text(json.dumps({"udid": PRO, "device_name": "My iPhone"}))
        DeviceStore(self.root).add(XR, "Work iPhone", busy=lambda port: False)
        self.listing = [{"udid": PRO, "name": "My iPhone", "model": "iPhone16,1", "ios": "26.0", "trusted": True},
                        {"udid": XR, "name": "Work iPhone", "model": "iPhone11,8", "ios": "18.6", "trusted": True},
                        {"udid": SE, "name": "New iPhone", "model": None, "ios": None, "trusted": False}]
        self.answering = set()
        patches = [patch.object(DeviceManager, "devices", lambda manager: list(self.listing)),
                   patch.object(DeviceManager, "built", lambda manager: True),
                   patch.object(DeviceManager, "expired", lambda manager, now=None: False),
                   patch("mobile_agent.devices.wda_answers",
                         side_effect=lambda url, timeout=1.0: url in self.answering),
                   patch("mobile_agent.devices.lease_free", return_value=True),
                   patch("mobile_agent.devices.simulator_records", return_value=[
                       record(id="SIM-1", kind="simulator", name="Mobster · iPhone 17 Pro · iOS 26.4", udid="SIM-1",
                              state="ready", wda_url_value="http://127.0.0.1:8310")]),
                   patch("mobile_agent.devices.static_specs", return_value=[])]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_every_kind_with_its_ports_and_state(self):
        self.answering = {"http://127.0.0.1:8101"}
        found = {item["id"]: item for item in devices.discover(self.root)}
        self.assertEqual(list(found), [PRO, XR, SE, "SIM-1"])
        self.assertTrue(found[PRO]["primary"])
        self.assertEqual((found[PRO]["wdaUrl"], found[PRO]["state"]), ("http://127.0.0.1:8100", "connected"))
        self.assertEqual((found[XR]["wdaUrl"], found[XR]["mjpegUrl"], found[XR]["state"]),
                         ("http://127.0.0.1:8101", "http://127.0.0.1:9101", "ready"))
        self.assertEqual((found[SE]["state"], found[SE]["wdaUrl"], found[SE]["reason"]),
                         ("needs_setup", None, REASONS["untrusted"]))
        self.assertEqual(found[XR]["modelName"], "iPhone XR")

    def test_an_unplugged_phone_set_up_earlier_stays_listed(self):
        self.listing = self.listing[:1]
        found = {item["id"]: item for item in devices.discover(self.root, sims=False)}
        self.assertEqual(found[XR]["state"], "disconnected")
        self.assertEqual(found[XR]["name"], "Work iPhone")


class CliTests(unittest.TestCase):
    FOUND = [record(id=XR, kind="usb", name="Work iPhone", udid=XR, model="iPhone11,8", model_name="iPhone XR",
                    ios="18.6", state="ready", wda_url_value="http://127.0.0.1:8101"),
             record(id="SIM-1", kind="simulator", name="Mobster · iPhone 17 Pro · iOS 26.4", udid="SIM-1",
                    state="ready", wda_url_value="http://127.0.0.1:8310"),
             record(id=SE, kind="usb", name="New iPhone", udid=SE, state="needs_setup", reason=REASONS["not_built"],
                    wda_url_value=None, set_up=False)]

    def main(self, *argv):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        from mobile_agent import __main__ as cli
        from mobile_agent import harness_api
        out, err = io.StringIO(), io.StringIO()
        with patch("mobile_agent.devices.discover", return_value=[dict(item) for item in self.FOUND]), \
                patch.object(harness_api.hooks(), "cli", []), \
                patch("mobile_agent.tls.ensure_ca_bundle"), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_devices_lists_every_device_or_prints_json(self):
        code, out, _ = self.main("devices")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"Name +Kind +State +Details\n")
        self.assertRegex(out, r"Work iPhone +iPhone +Ready +iPhone XR, iOS 18\.6\n")
        self.assertRegex(out, r"New iPhone +iPhone +Needs setup *\n +" + re.escape(REASONS["not_built"]))
        self.assertIn('Give one a task: mobster --device "Work iPhone"', out)
        code, out, _ = self.main("devices", "--json")
        self.assertEqual([item["id"] for item in json.loads(out)["devices"]], [XR, "SIM-1", SE])

    def test_device_is_that_devices_address_for_run_screen_and_doctor(self):
        from mobile_agent import __main__ as cli
        seen = {}
        with patch("mobile_agent.smart_run.run_smart",
                   side_effect=lambda args, demo: seen.update(url=cli.wda_url_for(args), demo=demo) or 0):
            code, _, _ = self.main("run", "Open About", "--device", "work iphone", "--execute")
        self.assertEqual((code, seen["url"], seen["demo"]), (0, "http://127.0.0.1:8101", False))
        with patch("mobile_agent.terminal_image.show_screen", return_value=0) as show:
            self.main("screen", "--device", "SIM-1", "--out", "/dev/null")
        self.assertEqual(show.call_args.args[0], "http://127.0.0.1:8310")
        with patch("mobile_agent.doctor.run_checks", return_value=[]) as checks:
            self.main("doctor", "--device", "Mobster · iPhone 17 Pro · iOS 26.4", "--json")
        self.assertEqual((checks.call_args.kwargs["wda_url"], checks.call_args.kwargs["device"]),
                         ("http://127.0.0.1:8310", False))
        with patch("mobile_agent.doctor.run_checks", return_value=[]) as checks:
            self.main("doctor", "--device", XR, "--json")
        self.assertEqual((checks.call_args.kwargs["device"], checks.call_args.kwargs["udid"]), (True, XR))

    def test_a_device_that_is_not_there_or_not_set_up_exits_3(self):
        code, out, _ = self.main("run", "Open About", "--device", "nope", "--json")
        self.assertEqual(code, 3)
        self.assertIn("No device is named or has the id “nope”", json.loads(out)["error"])
        code, _, err = self.main("screen", "--device", "New iPhone")
        self.assertEqual(code, 3)
        self.assertIn("“New iPhone” isn't set up yet", err)

    def test_device_and_wda_url_together_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as caught:
            self.main("doctor", "--device", XR, "--wda-url", "http://127.0.0.1:8203")
        self.assertEqual(caught.exception.code, 2)

    def test_serve_device_names_the_default_device(self):
        with patch("mobile_agent.serve.serve") as serve:
            self.main("serve", "--device", "SIM-1", "--port", "0")
        config = serve.call_args.args[0]
        self.assertEqual((config.device, config.wda_url), ("SIM-1", "http://127.0.0.1:8310"))
        with patch("mobile_agent.serve.serve") as serve:
            self.main("serve", "--device", "SIM-1", "--manage-device", "--port", "0")
        config = serve.call_args.args[0]
        self.assertEqual((config.device, config.wda_url), ("SIM-1", "http://127.0.0.1:8100"))


if __name__ == "__main__":
    unittest.main()
