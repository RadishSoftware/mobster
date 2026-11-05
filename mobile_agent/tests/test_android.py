"""Android: the adb wrapper, the tree mapping, the driver, logs and crashes, the emulator manager and its doctor rows.
Recorded uiautomator XML and a fake adb; no SDK, emulator or phone."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mobile_agent import android, drivers
from mobile_agent.android import adb as adb_module
from mobile_agent.android import doctor as android_doctor
from mobile_agent.android import logs
from mobile_agent.android.adb import Adb, AdbError, Result
from mobile_agent.android.driver import AndroidDriver
from mobile_agent.android.emulator import EmulatorManager
from mobile_agent.android.tree import to_wda
from mobile_agent.sim.models import SimError
from mobile_agent.state import validate_bundle_id
from mobile_agent.transport import TransportError
from mobile_agent.verify.tree import parse_tree, read_tree

FIXTURES = Path(__file__).parent / "android_fixtures"
SETTINGS = (FIXTURES / "settings_home.xml").read_text()
DEVICES = ("List of devices attached\n"
           "emulator-5554          device product:sdk_gphone64_arm64 model:sdk_gphone64_arm64 device:emu64a "
           "transport_id:1\n"
           "R5CT123ABC             device usb:1-1 product:tokay model:Pixel_9 device:tokay transport_id:2\n"
           "0123456789ABCDEF       unauthorized usb:1-2 transport_id:3\n")


class FakeAdb:
    """Records argv; answers by the first matching prefix of the adb arguments (after any -s SERIAL)."""

    def __init__(self, answers=None):
        self.calls, self.answers = [], answers or {}

    def __call__(self, argv, timeout, stdin):
        args = argv[1:]
        if args[:1] == ["-s"]:
            args = args[2:]
        self.calls.append(args)
        for prefix, answer in self.answers.items():
            if " ".join(map(str, args)).startswith(prefix):
                if isinstance(answer, Result):
                    return answer
                return Result(0, answer.encode() if isinstance(answer, str) else answer)
        return Result(0, b"")

    def shells(self):
        return [call[1:] for call in self.calls if call[:1] == ["shell"]]


def driver_for(fake=None, xml=SETTINGS):
    fake = fake or FakeAdb()
    fake.answers.setdefault("shell wm size", "Physical size: 1080x2400\n")
    fake.answers.setdefault("shell wm density", "Physical density: 420\n")
    fake.answers.setdefault("exec-out uiautomator dump", xml + "\nUI hierarchy dumped to: /dev/tty\n")
    return AndroidDriver("emulator-5554", Adb("emulator-5554", fake, "adb")), fake


class AdbTests(unittest.TestCase):
    def test_devices_parse_emulators_usb_and_unauthorized(self):
        rows = adb_module.parse_devices(DEVICES)
        self.assertEqual([(d.serial, d.kind, d.state) for d in rows],
                         [("emulator-5554", "emulator", "device"), ("R5CT123ABC", "usb", "device"),
                          ("0123456789ABCDEF", "usb", "unauthorized")])
        self.assertEqual(rows[1].model, "Pixel_9")
        self.assertFalse(rows[2].ready)

    def test_serial_and_package_are_validated(self):
        for bad in ("-s", "a b", "a;b"):
            with self.assertRaises(AdbError):
                Adb(bad)
        for bad in ("settings", "com.x; rm -rf /", "com..x"):
            with self.assertRaises(AdbError):
                adb_module.valid_package(bad)
        self.assertEqual(adb_module.valid_package("com.android.settings"), "com.android.settings")
        validate_bundle_id("com.android.settings")  # the iOS id check accepts an Android package too

    def test_dump_strips_the_trailer_and_refuses_garbage(self):
        fake = FakeAdb({"exec-out uiautomator dump": SETTINGS + "UI hierarchy dumped to: /dev/tty\n"})
        self.assertTrue(Adb("emulator-5554", fake, "adb").dump_ui().endswith("</hierarchy>"))
        with self.assertRaises(AdbError):
            Adb("emulator-5554", FakeAdb({"exec-out uiautomator dump": "ERROR: null root node"}), "adb").dump_ui()

    def test_text_is_quoted_for_the_device_shell_and_non_ascii_is_refused(self):
        fake = FakeAdb()
        adb = Adb("emulator-5554", fake, "adb")
        adb.text("it's $HOME; ok")
        self.assertEqual(fake.shells()[-1], ["input", "text", "'it'\"'\"'s%s$HOME;%sok'"])
        for refused in ("café", "50%", "a\nb"):
            with self.assertRaises(AdbError):
                adb.text(refused)
        self.assertEqual(len(fake.shells()), 1)

    def test_install_needs_an_apk_and_success(self):
        with tempfile.TemporaryDirectory() as folder:
            apk = Path(folder) / "app.apk"
            apk.write_bytes(b"PK")
            Adb("emulator-5554", FakeAdb({"install": "Performing Streamed Install\nSuccess\n"}), "adb").install(apk)
            with self.assertRaises(AdbError):
                Adb("emulator-5554", FakeAdb({"install": "Failure [INSTALL_FAILED_OLDER_SDK]"}), "adb").install(apk)
            with self.assertRaises(AdbError):
                Adb("emulator-5554", FakeAdb(), "adb").install(Path(folder) / "app.ipa")

    def test_a_failing_command_raises_with_its_last_line(self):
        fake = FakeAdb({"shell input": Result(1, b"", b"error: device offline")})
        with self.assertRaises(AdbError) as caught:
            Adb("emulator-5554", fake, "adb").tap(1, 2)
        self.assertIn("device offline", str(caught.exception))

    def test_front_package_is_the_focused_window(self):
        fake = FakeAdb({"shell dumpsys window":
                        "  mCurrentFocus=Window{928d73a u0 com.android.settings/com.android.settings.Settings}\n"})
        self.assertEqual(Adb("emulator-5554", fake, "adb").front_package(), "com.android.settings")
        self.assertEqual(Adb("emulator-5554", FakeAdb(), "adb").front_package(), "")

    def test_open_url_is_one_quoted_argument(self):
        fake = FakeAdb()
        Adb("emulator-5554", fake, "adb").open_url("https://example.com/a?b=1&c=2")
        self.assertEqual(fake.shells()[-1][-1], "'https://example.com/a?b=1&c=2'")
        with self.assertRaises(AdbError):
            Adb("emulator-5554", fake, "adb").open_url("https://x.test/' ; rm -rf /")

    def test_sdk_root_follows_the_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "platform-tools").mkdir()
            tool = Path(folder) / "platform-tools" / "adb"
            tool.write_text("#!/bin/sh\n")
            tool.chmod(0o755)
            self.assertEqual(adb_module.sdk_root({"ANDROID_HOME": folder}), Path(folder))
            self.assertEqual(adb_module.tool("adb", folder), str(tool))
            self.assertIsNone(adb_module.tool("emulator", folder))


class TreeTests(unittest.TestCase):
    def setUp(self):
        self.tree = parse_tree(to_wda(SETTINGS, 1080, 2400, 420))

    def test_screen_is_in_dp_and_the_package_is_the_app_in_front(self):
        self.assertEqual(self.tree.bundle_id, "com.android.settings")
        self.assertEqual(self.tree.size, (411.43, 914.29))

    def test_roles_labels_ids_and_values(self):
        by_label = {node.label: node for node in self.tree.nodes if node.label}
        search = next(n for n in self.tree.nodes if n.identifier == "search_action_bar_title")
        self.assertEqual((search.role, search.label, search.value), ("TextField", "", "Search settings"))
        wifi = next(n for n in self.tree.nodes if n.role == "Switch")
        self.assertEqual((wifi.value, wifi.selected, wifi.identifier), ("1", True, "switch_widget"))
        back = next(n for n in self.tree.nodes if n.label == "Navigate up")
        self.assertEqual((back.role, back.enabled), ("Button", False))
        self.assertEqual(by_label["Network & internet"].role, "StaticText")

    def test_an_unlabelled_icon_does_not_take_its_resource_id_as_a_label(self):
        xml = ('<hierarchy><node class="android.widget.FrameLayout" package="a.b" bounds="[0,0][1080,2400]">'
               '<node class="android.widget.ImageView" package="a.b" resource-id="a.b:id/icon" '
               'bounds="[0,0][100,100]" enabled="true" clickable="false"/></node></hierarchy>')
        from mobile_agent.state import _parse_wda, from_wda_root
        snapshot = from_wda_root(_parse_wda(to_wda(xml, 1080, 2400, 420)))
        self.assertNotIn("icon", snapshot.text)

    def test_selectors_find_android_nodes_like_ios_ones(self):
        from mobile_agent.verify.assertions import parse_selector
        self.assertEqual(len(self.tree.select(parse_selector({"label": "Wi-Fi"}))), 1)

    def test_not_a_hierarchy_is_refused(self):
        with self.assertRaises(ValueError):
            to_wda("<foo/>", 1080, 2400, 420)
        with self.assertRaises(ValueError):
            to_wda("<!DOCTYPE x [<!ENTITY a 'b'>]><hierarchy/>", 1080, 2400, 420)

    def test_password_fields_never_carry_their_text(self):
        xml = ('<hierarchy><node class="android.widget.EditText" package="a.b" password="true" text="hunter2" '
               'bounds="[0,0][100,100]" enabled="true" clickable="true"/></hierarchy>')
        self.assertNotIn("hunter2", to_wda(xml, 1080, 2400, 420))


class DriverTests(unittest.TestCase):
    def test_observe_gives_the_same_snapshot_shape(self):
        driver, _ = driver_for()
        snapshot = driver.observe()
        self.assertEqual(snapshot.source, "android")
        self.assertEqual(snapshot.bundle_id, "com.android.settings")
        labels = {e.label for e in snapshot.elements}
        self.assertIn("Network & internet", labels)
        self.assertIn("Wi-Fi", snapshot.text)

    def test_verify_reads_the_tree_through_evidence_source(self):
        driver, _ = driver_for()
        tree = read_tree(driver)
        self.assertEqual(tree.bundle_id, "com.android.settings")

    def test_importing_the_driver_registers_android(self):
        self.assertIs(drivers.build_driver("android", serial="emulator-5554").__class__, AndroidDriver)

    def test_tap_lands_on_the_element_in_pixels(self):
        driver, fake = driver_for()
        snapshot = driver.observe()
        target = next(e for e in snapshot.elements if e.label == "Network & internet")
        driver.execute("TAP", target, snapshot)
        x, y = target.tap_point
        self.assertEqual(fake.shells()[-1], ["input", "tap", str(round(x * 1080)), str(round(y * 2400))])

    def test_tap_on_an_element_not_in_the_snapshot_is_refused_and_nothing_is_sent(self):
        driver, fake = driver_for()
        snapshot = driver.observe()
        other = driver_for(xml=SETTINGS.replace("Wi-Fi", "Bluetooth"))[0].observe()
        stranger = next(e for e in other.elements if e.label == "Bluetooth")
        with self.assertRaises(ValueError):
            driver.execute("TAP", stranger, snapshot)
        self.assertFalse([c for c in fake.shells() if c[:1] == ["input"]])

    def test_an_old_snapshot_of_a_changed_screen_is_stale_and_nothing_is_sent(self):
        driver, fake = driver_for()
        snapshot = driver.observe()
        snapshot.captured_at -= 10
        fake.answers["exec-out uiautomator dump"] = SETTINGS.replace("Wi-Fi", "Bluetooth") + "\n"
        target = next(e for e in snapshot.elements if e.label == "Network & internet")
        from mobile_agent.wda.guards import DriverRejection
        with self.assertRaises(DriverRejection):
            driver.execute("TAP", target, snapshot)
        self.assertFalse([c for c in fake.shells() if c[:1] == ["input"]])

    def test_type_taps_the_field_then_types_and_submits(self):
        driver, fake = driver_for()
        snapshot = driver.observe()
        field = next(e for e in snapshot.elements if e.editable)
        driver.execute("TYPE_SUBMIT", field, snapshot, text="wifi calling")
        inputs = [c for c in fake.shells() if c[:1] == ["input"]]
        self.assertEqual([c[1] for c in inputs], ["tap", "text", "keyevent"])
        self.assertEqual(inputs[1][2], "wifi%scalling")
        self.assertEqual(inputs[2][2], "66")

    def test_home_back_launch_and_swipe(self):
        driver, fake = driver_for()
        snapshot = driver.observe()
        driver.execute("HOME", None, snapshot)
        driver.execute("BACK", None, snapshot)
        driver.execute("LAUNCH_APP", "com.android.settings", snapshot)
        driver.execute("SWIPE_UP", None, snapshot)
        shells = fake.shells()
        self.assertIn(["input", "keyevent", "3"], shells)
        self.assertIn(["input", "keyevent", "4"], shells)
        self.assertEqual(next(c for c in shells if c[:1] == ["monkey"])[2], "com.android.settings")
        swipe = next(c for c in shells if c[1:2] == ["swipe"])
        self.assertGreater(int(swipe[3]), int(swipe[5]))  # the finger moves up the screen

    def test_a_failed_input_is_a_transport_error_and_is_not_retried(self):
        driver, fake = driver_for()
        snapshot = driver.observe()
        fake.answers["shell input"] = Result(1, b"", b"error: closed")
        target = snapshot.elements[0]
        with self.assertRaises(TransportError):
            driver.execute("TAP", target, snapshot)
        self.assertEqual(len([c for c in fake.shells() if c[:1] == ["input"]]), 1)

    def test_unknown_operation_is_refused(self):
        driver, _ = driver_for()
        with self.assertRaises(ValueError):
            driver.execute("INCREMENT", None, driver.observe())


class LogTests(unittest.TestCase):
    def lines(self):
        return (FIXTURES / "crash.log").read_text().splitlines()

    def test_crash_and_anr_are_found_by_package(self):
        crashes = logs.parse_crashes(self.lines())
        self.assertEqual([(c.kind, c.package) for c in crashes],
                         [("crash", "org.mozilla.ios.Firefox"), ("anr", "com.example.other")])
        self.assertEqual(crashes[0].pid, 4242)
        self.assertTrue(crashes[0].exception.startswith("java.lang.IllegalStateException: cart is empty"))
        self.assertEqual([c.package for c in logs.parse_crashes(self.lines(), "org.mozilla.ios.Firefox")],
                         ["org.mozilla.ios.Firefox"])
        self.assertEqual(logs.parse_crashes(self.lines(), "com.example.none"), [])

    def test_logcat_lines_are_redacted_before_they_are_kept(self):
        fake = FakeAdb({"logcat": (FIXTURES / "crash.log").read_text()})
        text = "\n".join(logs.read_logcat(Adb("emulator-5554", fake, "adb")))
        self.assertNotIn("sk-ant-api03-AAAA", text)
        self.assertIn("FATAL EXCEPTION", text)

    def test_recording_pulls_the_file_and_cleans_up(self):
        fake = FakeAdb()
        process = mock.Mock()
        process.wait.return_value = 0
        started = []
        recording = logs.start_recording(Adb("emulator-5554", fake, "adb"),
                                         lambda argv: started.append(argv) or process)
        with mock.patch.object(logs.time, "sleep"):
            recording.stop("/tmp/x.mp4")
        self.assertIn("screenrecord", started[0])
        self.assertIn(["pull", logs.RECORD_PATH, "/tmp/x.mp4"], fake.calls)
        self.assertIn(["rm", "-f", logs.RECORD_PATH], fake.shells())


class FakeProcess:
    pid = 99999999

    def __init__(self):
        self.alive = True

    def poll(self):
        return None if self.alive else 1


class EmulatorTests(unittest.TestCase):
    def tearDown(self):
        self.folder.cleanup()

    def manager(self, devices=DEVICES, avds="Pixel_8\nMedium_Phone\n", leases=None, boot_ready=True):
        self.folder = tempfile.TemporaryDirectory()
        sdk = Path(self.folder.name) / "sdk"
        for relative in ("platform-tools/adb", "emulator/emulator"):
            (sdk / relative).parent.mkdir(parents=True, exist_ok=True)
            (sdk / relative).write_text("#!/bin/sh\n")
            (sdk / relative).chmod(0o755)
        self.fake = FakeAdb({"devices": devices, "emu avd name": "Pixel_8\nOK\n", "shell getprop sys.boot_completed":
                             "1" if boot_ready else "0"})
        self.fake.answers["/emulator -list-avds"] = avds
        self.held = set(leases or ())
        self.spawned = []

        def lease(identity):
            if identity in self.held:
                raise RuntimeError("held")
            self.held.add(identity)
            held = mock.Mock()
            held.close.side_effect = lambda: self.held.discard(identity)
            return held

        def spawn(argv):
            self.spawned.append(argv)
            return FakeProcess()

        class Runner:
            def __call__(inner, argv, timeout, stdin):
                if argv[0].endswith("/emulator"):
                    if "-list-avds" in argv:
                        return Result(0, avds.encode())
                    return self.fake(["emulator", *argv[1:]], timeout, stdin)
                return self.fake(argv, timeout, stdin)

        return EmulatorManager(self.folder.name + "/data", run=Runner(), spawn=spawn, lease=lease, root=sdk,
                               sleep=lambda s: None)

    def test_devices_have_a_platform_and_the_avd_name(self):
        rows = self.manager().devices()
        self.assertEqual([(r["platform"], r["serial"], r["name"], r["state"]) for r in rows],
                         [("android", "emulator-5554", "Pixel_8", "device"),
                          ("android", "R5CT123ABC", "Pixel_9", "device"),
                          ("android", "0123456789ABCDEF", "0123456789ABCDEF", "unauthorized")])
        self.assertFalse(rows[0]["owned"])

    def test_acquire_takes_an_idle_running_device_and_release_frees_it(self):
        manager = self.manager()
        lease = manager.acquire(avd="Pixel_8")
        self.assertEqual(lease.target.serial, "emulator-5554")
        self.assertTrue(manager.devices()[0]["in_use"])
        lease.release()
        self.assertFalse(manager.devices()[0]["in_use"])
        self.assertEqual(self.spawned, [])

    def test_a_busy_usb_phone_is_refused_not_shared(self):
        manager = self.manager(leases={"android:R5CT123ABC"})
        with self.assertRaises(SimError) as caught:
            manager.acquire(serial="R5CT123ABC")
        self.assertEqual(caught.exception.kind, "busy")

    def test_an_unauthorized_phone_is_never_leased(self):
        with self.assertRaises(SimError):
            self.manager().acquire(serial="0123456789ABCDEF")

    def test_boots_an_avd_when_none_is_running_and_records_ownership(self):
        manager = self.manager(devices="List of devices attached\n\n")
        self.fake.answers["devices"] = Result(0, b"List of devices attached\n")
        calls = {"n": 0}

        def devices_after_boot(argv, timeout, stdin):
            if argv[-2:] == ["devices", "-l"]:
                calls["n"] += 1
                if self.spawned:
                    return Result(0, b"List of devices attached\nemulator-5554 device model:sdk\n")
            return original(argv, timeout, stdin)

        original = manager._run
        manager.adb._run = devices_after_boot
        manager._run = devices_after_boot
        lease = manager.acquire(avd="Pixel_8")
        self.assertEqual(lease.target.serial, "emulator-5554")
        self.assertIn("-no-window", self.spawned[0])
        self.assertEqual(self.spawned[0][1:5], ["-avd", "Pixel_8", "-port", "5554"])
        info = json.loads((Path(self.folder.name) / "data/android/Pixel_8.json").read_text())
        self.assertEqual(info["serial"], "emulator-5554")
        lease.release()

    def test_the_cap_refuses_a_third_emulator(self):
        manager = self.manager(devices="List of devices attached\n")
        manager.data_dir.mkdir(parents=True)
        for name in ("A", "B"):
            (manager.data_dir / f"{name}.json").write_text("{}")
        rows = [{"owned": True}, {"owned": True}]
        with mock.patch.object(manager, "devices", side_effect=[[], rows]):
            with self.assertRaises(SimError) as caught:
                manager.acquire(avd="Pixel_8")
        self.assertEqual(caught.exception.kind, "busy")
        self.assertEqual(self.spawned, [])

    def test_an_unknown_avd_is_refused_with_the_known_ones(self):
        manager = self.manager(devices="List of devices attached\n")
        with self.assertRaises(SimError) as caught:
            manager.acquire(avd="Nope")
        self.assertIn("Pixel_8", str(caught.exception))
        self.assertEqual(self.spawned, [])

    def test_shutdown_never_touches_an_emulator_mobster_did_not_boot(self):
        manager = self.manager()
        self.assertEqual(manager.shutdown(), [])
        self.assertFalse([c for c in self.fake.calls if c[:2] == ["emu", "kill"]])

    def test_shutdown_stops_an_owned_idle_emulator_only(self):
        manager = self.manager()
        manager.data_dir.mkdir(parents=True)
        (manager.data_dir / "Pixel_8.json").write_text(json.dumps({"pid": 99999999, "serial": "emulator-5554"}))
        self.assertEqual(manager.shutdown(), ["Pixel_8"])
        self.assertIn(["emu", "kill"], self.fake.calls)
        self.assertFalse((manager.data_dir / "Pixel_8.json").exists())
        manager.data_dir.mkdir(exist_ok=True)
        (manager.data_dir / "Pixel_8.json").write_text(json.dumps({"pid": 99999999, "serial": "emulator-5554"}))
        manager.acquire(avd="Pixel_8")  # now running a check
        self.fake.calls.clear()
        self.assertEqual(manager.shutdown(), [])
        self.assertFalse([c for c in self.fake.calls if c[:2] == ["emu", "kill"]])


class DoctorTests(unittest.TestCase):
    def test_no_sdk_is_one_failing_row_with_the_cost(self):
        manager = EmulatorManager(tempfile.gettempdir(), run=FakeAdb(), root=None)
        manager.root = None
        rows = android_doctor.run_checks(manager)
        self.assertEqual([(r["key"], r["state"]) for r in rows], [("android_sdk", "fail")])
        self.assertIn("3 GB", rows[0]["fix"])

    def test_sdk_without_an_avd_warns_and_lists_devices(self):
        helper = EmulatorTests("test_devices_have_a_platform_and_the_avd_name")
        manager = helper.manager(avds="")
        self.addCleanup(helper.folder.cleanup)
        rows = {r["key"]: r for r in android_doctor.run_checks(manager)}
        self.assertEqual(rows["android_avd"]["state"], "warn")
        self.assertEqual(rows["android_devices"]["state"], "warn")  # the unauthorized phone
        self.assertIn("USB debugging", rows["android_devices"]["fix"])

    def test_the_flag_gates_everything(self):
        self.assertFalse(android.enabled({}))
        self.assertTrue(android.enabled({"MOBSTER_EXPERIMENTAL_ANDROID": "1"}))
        self.assertFalse(android.enabled({"MOBSTER_EXPERIMENTAL_ANDROID": "0"}))


class Recorded:
    """The adb answers of one Settings launch on a Pixel_8, for a whole VerifyRun."""

    @staticmethod
    def png():
        from io import BytesIO

        from PIL import Image
        buffer = BytesIO()
        Image.new("RGB", (1080, 2400), (240, 240, 240)).save(buffer, "PNG")
        return buffer.getvalue()

    @classmethod
    def fake(cls):
        return FakeAdb({
            "devices": DEVICES.split("R5CT")[0], "emu avd name": "Pixel_8\nOK\n",
            "shell getprop sys.boot_completed": "1", "shell getprop ro.build.version.release": "14\n",
            "shell wm size": "Physical size: 1080x2400\n", "shell wm density": "Physical density: 420\n",
            "exec-out uiautomator dump": SETTINGS, "exec-out screencap": cls.png(),
            "shell pm path": "package:/system/priv-app/Settings/Settings.apk\n",
            "shell dumpsys package": "    versionName=14\n",
            "shell dumpsys window": "  mCurrentFocus=Window{1 u0 com.android.settings/.Settings}\n",
        })


class RunTests(unittest.TestCase):
    """A saved launch-only check through VerifyRun, the manager and the driver, on a fake adb."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        sdk = Path(self.folder.name) / "sdk"
        for relative in ("platform-tools/adb", "emulator/emulator"):
            (sdk / relative).parent.mkdir(parents=True, exist_ok=True)
            (sdk / relative).write_text("#!/bin/sh\n")
            (sdk / relative).chmod(0o755)
        self.fake = Recorded.fake()
        self.fake.answers["-list-avds"] = "Pixel_8\n"
        fake = self.fake

        def run(argv, timeout, stdin):
            if argv[0].endswith("/emulator"):
                return fake(["emulator", *argv[1:]], timeout, stdin)
            return fake(argv, timeout, stdin)

        from mobile_agent.android.manager import AndroidManager
        emulators = EmulatorManager(self.folder.name + "/data", run=run, spawn=mock.Mock(), root=sdk,
                                    lease=lambda identity: mock.Mock(), sleep=lambda s: None)
        self.manager = AndroidManager(self.folder.name, emulators=emulators)
        self.manager._api_of = lambda avd: 34

    def check(self, **fields):
        from mobile_agent.verify.checks import check_from_dict
        return check_from_dict({"name": "settings launches", "app": {"bundle": "com.android.settings"},
                                "device": "Pixel 8", "runtime": "Android 14",
                                "expect": [{"text": "Network & internet"}], **fields}, base_dir=self.folder.name)

    def run_check(self, check):
        from mobile_agent.verify.runner import CouldntRun, VerifyRun
        run = VerifyRun(check, mode="launch", runs_dir=Path(self.folder.name) / "runs", manager=self.manager)
        try:
            run.prepare()
        except CouldntRun:
            pass  # the run's result carries the reason
        return run.finish()

    def test_a_launch_check_passes_on_a_recorded_settings_screen(self):
        result = self.run_check(self.check())
        self.assertEqual(result["verdict"], "passed", result.get("reason"))
        self.assertEqual(result["device"]["type"], "Pixel_8")
        self.assertEqual(result["app"]["bundle_id"], "com.android.settings")
        self.assertIn(["monkey", "-p", "com.android.settings", "-c", "android.intent.category.LAUNCHER", "1"],
                      self.fake.shells())
        self.assertTrue(result["frames"])

    def test_a_missing_label_fails_the_check_with_a_frame(self):
        result = self.run_check(self.check(expect=[{"text": "Bluetooth settings"}]))
        self.assertEqual(result["verdict"], "failed")

    def test_conditions_are_set_through_adb_and_restored(self):
        result = self.run_check(self.check(conditions={"appearance": "dark",
                                                       "permissions": {"location": "grant"}}))
        self.assertEqual(result["verdict"], "passed", result.get("reason"))
        shells = self.fake.shells()
        self.assertIn(["cmd", "uimode", "night", "yes"], shells)
        self.assertIn(["pm", "grant", "com.android.settings", "android.permission.ACCESS_FINE_LOCATION"], shells)
        self.assertEqual(result["conditions"]["applied"], ["appearance", "permissions"])

    def test_a_condition_with_no_android_equivalent_is_couldnt_run_and_launches_nothing(self):
        (Path(self.folder.name) / "push.json").write_text('{"aps": {"alert": "hi"}}')
        result = self.run_check(self.check(conditions={"push": "push.json"}))
        self.assertEqual(result["verdict"], "couldnt_run")
        self.assertIn("push", result["reason"]["message"])
        self.assertFalse([c for c in self.fake.shells() if c[:1] == ["monkey"]])

    def test_a_network_condition_is_refused_on_android(self):
        result = self.run_check(self.check(conditions={"network": "slow-3g"}))
        self.assertEqual(result["verdict"], "couldnt_run")
        self.assertIn("conditions.network", result["reason"]["message"])
        self.assertFalse([c for c in self.fake.shells() if c[:1] == ["monkey"]])

    def test_request_stubs_are_refused_on_android_and_nothing_launches(self):
        # The stubs' proxy and URLSession shim are an iOS simulator's: on Android they would be skipped silently.
        result = self.run_check(self.check(stubs=[{"match": {"host": "example.com", "path_prefix": "/"},
                                                   "respond": {"status": 200, "json": {"ok": True}}}]))
        self.assertEqual((result["verdict"], result["reason"]["class"]), ("couldnt_run", "environment"))
        self.assertIn("stubs", result["reason"]["message"])
        self.assertFalse([c for c in self.fake.shells() if c[:1] == ["monkey"]])

    def test_wire_assertions_are_refused_on_android_and_nothing_launches(self):
        with mock.patch.dict(os.environ, {"MOBSTER_EXPERIMENTAL_NETWORK": "1"}):
            check = self.check(expect=[{"text": "Network & internet"}, {"request": "example.com/"}])
        result = self.run_check(check)
        self.assertEqual((result["verdict"], result["reason"]["class"]), ("couldnt_run", "environment"))
        self.assertIn("wire assertions", result["reason"]["message"])
        self.assertFalse([c for c in self.fake.shells() if c[:1] == ["monkey"]])

    def test_fixtures_are_refused_on_android(self):
        from dataclasses import replace

        from mobile_agent.verify.fixtures import Seed
        result = self.run_check(replace(self.check(), seed=Seed(pasteboard="x")))
        self.assertEqual(result["verdict"], "couldnt_run")

    def test_launch_arguments_are_refused(self):
        result = self.run_check(self.check(launch={"args": ["-flag"]}))
        self.assertEqual(result["verdict"], "couldnt_run")
        self.assertFalse([c for c in self.fake.shells() if c[:1] == ["monkey"]])

    def on_the_phone(self, **options):
        from mobile_agent.android.manager import AndroidManager
        self.fake.answers["devices"] = DEVICES
        manager = AndroidManager(self.folder.name, emulators=self.manager.emulators, name="R5CT123ABC", **options)
        manager._api_of = lambda avd: 34
        return manager

    def test_a_check_on_a_usb_android_phone_is_refused_before_anything_runs(self):
        self.manager = self.on_the_phone()
        result = self.run_check(self.check(reset="data"))
        self.assertEqual(result["verdict"], "couldnt_run")
        self.assertIn("Android phone", result["reason"]["message"])
        self.assertFalse([c for c in self.fake.shells() if c[:1] in (["monkey"], ["pm"])])

    def test_a_phone_the_device_tools_reach_is_never_reset(self):
        manager = self.on_the_phone(phones=True)
        target = manager.acquire().target
        self.assertTrue(manager.real_device)
        for level in ("data", "reinstall"):
            with self.assertRaisesRegex(SimError, "never clears or reinstalls"):
                manager.reset(target, "com.example.app", level, app_path="/tmp/app.apk")
        manager.reset(target, "com.example.app", "none")
        self.assertFalse([c for c in self.fake.shells() if c[:1] == ["pm"]])
        self.assertFalse([c for c in self.fake.calls if c[:1] in (["install"], ["uninstall"])])

    def test_a_phones_permissions_are_skipped_and_an_emulators_are_set(self):
        from mobile_agent.android.conditions import AndroidConditions
        adb = adb_module.Adb("R5CT123ABC", self.fake, "adb")
        on_phone = AndroidConditions({"permissions": {"location": "grant"}}, adb, bundle="com.example.app",
                                     emulator=False)
        on_phone.before_launch()
        self.assertEqual(on_phone.report(), {"applied": [], "skipped": [
            {"key": "permissions", "why": "Mobster never changes an app's permissions on a phone."}]})
        self.assertFalse([c for c in self.fake.shells() if c[:1] == ["pm"]])
        from mobile_agent.verify.actions import ActionJournal
        journal = ActionJournal(Path(self.folder.name))
        on_emulator = AndroidConditions({"permissions": {"location": "grant"}}, adb, bundle="com.example.app",
                                        emulator=True, actions=journal)
        on_emulator.before_launch()
        self.assertEqual(on_emulator.report()["applied"], ["permissions"])
        journaled = [json.loads(line)["operation"] for line in (Path(self.folder.name) / "actions.jsonl").read_text()
                     .splitlines() if json.loads(line)["event"] == "action_started"]
        from mobile_agent.android.conditions import PERMISSIONS
        self.assertEqual(journaled, ["CONDITION_SET"] * len(PERMISSIONS["location"]))   # each grant journaled first
        from mobile_agent.verify.conditions import ConditionError
        without = AndroidConditions({"permissions": {"location": "grant"}}, adb, bundle="com.example.app",
                                    emulator=True)
        with self.assertRaisesRegex(ConditionError, "action journal"):
            without.before_launch()   # no journal, no change

    def test_a_package_that_is_not_installed_is_couldnt_run(self):
        self.fake.answers["shell pm path"] = ""
        result = self.run_check(self.check())
        self.assertEqual(result["verdict"], "couldnt_run")
        self.assertEqual(result["reason"]["class"], "install")


class SuiteTests(unittest.TestCase):
    """`mobster test --android`, an .apk check and `runtime: Android` through Attempts and VerifyRun, on a fake adb."""

    setUp = RunTests.setUp

    def run_suite(self, fields, **options):
        from mobile_agent.testkit import api
        from mobile_agent.testkit.execute import Attempts
        root = Path(self.folder.name).resolve() / "project"
        checks = root / ".mobster" / "checks"
        checks.mkdir(parents=True)
        check = checks / "settings.json"
        check.write_text(json.dumps({"version": 1, "name": "settings launches",
                                     "app": {"bundle": "com.android.settings"},
                                     "expect": [{"text": "Network & internet"}], **fields}))
        self.names = []

        def manager(data_dir, *, name=None):
            self.names.append(name)
            return self.manager

        gate = mock.Mock()
        planned = api.plan(api.Options(paths=(str(check),), keyless=True, retries=0, **options), cwd=root)
        attempts = Attempts(runs_dir=root / ".mobster" / "runs", keyless=True, simulators=mock.Mock())
        with mock.patch("mobile_agent.android.manager.AndroidManager", manager):
            doc, _ = api.execute(planned, attempts=attempts, gate=gate, cwd=root)
        self.assertEqual(gate.mock_calls, [])  # the iOS app gate never reads an Android record
        return doc, doc["checks"][0]["results"][0]

    def launched(self):
        return ["monkey", "-p", "com.android.settings", "-c", "android.intent.category.LAUNCHER", "1"] in \
            self.fake.shells()

    def test_each_documented_route_runs_the_check(self):
        for fields, options, name, device, key in (
                ({}, {"android": ""}, None, "Android · Android 14", "android:any"),
                ({}, {"android": "Pixel_8"}, "Pixel_8", "Pixel_8 · Android 14", "android:Pixel_8"),
                ({"runtime": "Android 14"}, {}, None, "Pixel_8 · Android 14", "sim:Pixel_8@Android 14")):
            with self.subTest(fields=fields, options=options):
                self.setUp()
                doc, cell = self.run_suite(fields, **options)
                self.assertEqual((doc["exitCode"], cell["status"], cell["attempts"]), (0, "passed", 1), cell["reason"])
                self.assertEqual((self.names, cell["device"], cell["deviceKey"]), ([name], device, key))
                self.assertTrue(self.launched())

    def test_an_apk_check_installs_and_runs_with_its_digest_pinned(self):
        from mobile_agent.sim.models import AppInfo
        apk = Path(self.folder.name).resolve() / "settings.apk"
        apk.write_bytes(b"apk")
        info = AppInfo(bundle_id="com.android.settings", name="Settings", version="14", path=str(apk))
        self.manager.app_info = lambda path: info
        self.manager.install = mock.Mock(return_value=info)
        doc, cell = self.run_suite({"app": {"path": str(apk)}})
        self.assertEqual((doc["exitCode"], cell["status"]), (0, "passed"), cell["reason"])
        self.manager.install.assert_called_once()
        self.assertEqual(self.names, [None])
        self.assertIsNotNone(cell["appDigest"])

    def test_a_usb_android_phone_still_refuses_before_anything_launches(self):
        self.fake.answers["devices"] = DEVICES
        self.manager.name = "R5CT123ABC"
        doc, cell = self.run_suite({}, android="R5CT123ABC")
        self.assertEqual((doc["exitCode"], cell["status"]), (3, "couldnt_run"))
        self.assertFalse(self.launched())

    def test_an_app_state_fixture_still_refuses_on_android(self):
        from mobile_agent.testkit.execute import Attempts
        from mobile_agent.verify.checks import check_from_dict
        check = check_from_dict({"name": "state", "app": {"bundle": "com.android.settings"},
                                 "business": {"adapter": "settings", "fixture": "fresh",
                                              "expect": [{"field": "a.b", "equals": True}]},
                                 "expect": [{"text": "x"}]})
        attempts = Attempts(runs_dir=Path(self.folder.name) / "runs", keyless=True)
        with mock.patch("mobile_agent.android.manager.AndroidManager", lambda *a, **k: self.manager):
            result = attempts(check, {"kind": "android", "id": "android:any", "name": "Android"}, label="x")
        self.assertEqual((result["verdict"], result["reason"]["class"]), ("couldnt_run", "business"))
        self.assertFalse(self.launched())

    def test_a_kind_with_no_driver_is_refused_and_an_iphone_still_needs_the_gate(self):
        from mobile_agent.testkit import api
        from mobile_agent.testkit.execute import Attempts
        from mobile_agent.testkit.targets import Refused, Target
        from mobile_agent.verify.checks import check_from_dict
        check = check_from_dict({"name": "app", "app": {"bundle": "com.example.app"}, "expect": [{"text": "x"}]})
        devices = mock.Mock()
        attempts = Attempts(runs_dir=Path(self.folder.name) / "runs", keyless=True, simulators=mock.Mock(),
                            devices=devices)
        for record in ({"kind": "bluetooth", "id": "b"}, {"kind": "android-phone", "id": "b"}, {"id": "b"}):
            with self.subTest(record=record), self.assertRaisesRegex(Refused, "no driver"):
                attempts(check, record, label="x")
        devices.assert_not_called()
        for record in ({"kind": "usb", "id": "U-1", "udid": "U-1", "wdaUrl": "http://127.0.0.1:8100"},
                       {"kind": "wda", "id": "W-1", "wdaUrl": "http://127.0.0.1:8100"}):
            with self.subTest(record=record):
                result = attempts(check, record, label="x")
                self.assertEqual((result["verdict"], result["reason"]["class"]), ("couldnt_run", "device"))
                self.assertIn("device gate", result["reason"]["message"])

        root = Path(self.folder.name).resolve() / "unknown"
        checks = root / ".mobster" / "checks"
        checks.mkdir(parents=True)
        file = checks / "app.json"
        file.write_text(json.dumps({"version": 1, "name": "app", "app": {"bundle": "com.example.app"},
                                    "expect": [{"text": "x"}]}))
        planned = api.plan(api.Options(paths=(str(file),), keyless=True, retries=0), cwd=root)
        planned.targets = [Target("device:b", "device", record={"kind": "bluetooth", "id": "b", "name": "Band"})]
        attempt = mock.Mock()
        doc, _ = api.execute(planned, attempts=attempt, cwd=root)
        cell = doc["checks"][0]["results"][0]
        self.assertEqual((cell["status"], cell["reason"]["class"], cell["attempts"]), ("couldnt_run", "device", 0))
        self.assertIn("no driver", cell["reason"]["message"])
        attempt.assert_not_called()


class ResolveTests(unittest.TestCase):
    def test_device_and_runtime_pick_an_avd(self):
        from mobile_agent.android.manager import avd_for
        avds = ["Pixel_8", "Pixel_9_API_36", "Medium_Phone"]
        api = {"Pixel_8": 34, "Pixel_9_API_36": 36, "Medium_Phone": 34}.get
        self.assertEqual(avd_for(avds, "Pixel 9", None, api), "Pixel_9_API_36")
        self.assertEqual(avd_for(avds, None, "Android 14", api), "Pixel_8")
        self.assertEqual(avd_for(avds, "Pixel 8", "Android 14", api), "Pixel_8")
        self.assertIsNone(avd_for(avds, None, None, api))
        for device, runtime in (("Pixel 9", "Android 14"), ("Galaxy", None), (None, "Android 99")):
            with self.assertRaises(SimError):
                avd_for(avds, device, runtime, api)

    def test_targets_for_android(self):
        from mobile_agent.testkit import targets
        from mobile_agent.verify.checks import check_from_dict
        apk = check_from_dict({"name": "a", "app": {"path": "/tmp/app.apk"}, "expect": [{"text": "x"}]})
        ios = check_from_dict({"name": "b", "app": {"bundle": "com.example.app"}, "expect": [{"text": "x"}]})
        self.assertTrue(targets.is_android(apk))
        self.assertFalse(targets.is_android(ios))
        record = targets.for_target(apk, targets.DEFAULT)[2]
        self.assertEqual(record["kind"], "android")
        self.assertIsNone(targets.for_target(ios, targets.DEFAULT)[2])
        explicit = targets.resolve(android="Pixel_8")[0]
        self.assertEqual(explicit.public()["kind"], "android")
        self.assertFalse(explicit.real)
        with self.assertRaises(targets.Refused):
            targets.for_target(check_from_dict({"name": "c", "app": {"path": "/tmp/App.app"},
                                                "expect": [{"text": "x"}]}), explicit)

    def test_an_unnamed_run_never_picks_a_usb_phone(self):
        usb_first = ("List of devices attached\nR5CT123ABC device usb:1-1 model:Pixel_9 transport_id:2\n"
                     "emulator-5554 device model:sdk transport_id:1\n")
        helper = EmulatorTests("test_devices_have_a_platform_and_the_avd_name")
        manager = helper.manager(devices=usb_first)
        self.addCleanup(helper.folder.cleanup)
        self.assertEqual(manager.acquire().target.serial, "emulator-5554")
        self.assertEqual(manager.acquire(serial="R5CT123ABC").target.serial, "R5CT123ABC")


class McpTests(unittest.TestCase):
    EMULATOR = {"id": "android:emulator-5554", "kind": "android", "platform": "android", "name": "Pixel_8",
                "serial": "emulator-5554", "avd": "Pixel_8", "state": "ready", "emulator": True}
    PHONE = {**EMULATOR, "id": "android:R5CT123ABC", "name": "Pixel_9", "serial": "R5CT123ABC", "avd": None,
             "emulator": False}

    def test_list_devices_has_a_platform_and_android_rows(self):
        from mobile_agent.tests.test_mcp_devices import ToolsHarness, records
        harness = type("H", (ToolsHarness,), {"runTest": lambda self: None})("runTest")
        harness.setUp()
        try:
            harness.make(devices=lambda: records() + [self.EMULATOR, self.PHONE])
            result = harness.call("list_devices")
        finally:
            harness.tearDown()
        rows = {row["id"]: row for row in result.structured["devices"]}
        self.assertEqual(rows["android:emulator-5554"]["platform"], "android")
        self.assertEqual({row["platform"] for row in rows.values() if row["kind"] != "android"}, {"ios"})
        self.assertIn("Pixel_8  [Android emulator", result.text)
        self.assertIn("Pixel_9  [Android phone", result.text)

    def test_an_emulator_is_not_a_phone_and_a_usb_android_phone_is(self):
        from mobile_agent.android.records import is_phone
        self.assertFalse(is_phone(self.EMULATOR))
        self.assertTrue(is_phone(self.PHONE))
        self.assertFalse(is_phone({"kind": "simulator"}))
        self.assertTrue(is_phone({"kind": "usb"}))

    def test_records_are_empty_without_the_flag_and_follow_adb_with_it(self):
        from mobile_agent.android import records as module
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MOBSTER_EXPERIMENTAL_ANDROID", None)
            self.assertEqual(module.records(), [])
        rows = [{"serial": "emulator-5554", "name": "Pixel_8", "avd": "Pixel_8", "kind": "emulator", "state": "device"},
                {"serial": "X1", "name": "X1", "avd": "", "kind": "usb", "state": "unauthorized"}]
        with mock.patch.dict(os.environ, {"MOBSTER_EXPERIMENTAL_ANDROID": "1"}), \
                mock.patch("mobile_agent.android.emulator.EmulatorManager.devices", return_value=rows):
            found = module.records("/tmp/unused")
        self.assertEqual([(r["id"], r["state"], r["emulator"]) for r in found],
                         [("android:emulator-5554", "ready", True), ("android:X1", "needs_setup", False)])

    def test_manager_for_an_android_record_is_the_android_manager(self):
        from mobile_agent.android.manager import AndroidManager
        from mobile_agent.device_targets import manager_for
        manager = manager_for(self.EMULATOR)
        self.assertIsInstance(manager, AndroidManager)
        self.assertEqual(manager.name, "emulator-5554")


if __name__ == "__main__":
    unittest.main()
