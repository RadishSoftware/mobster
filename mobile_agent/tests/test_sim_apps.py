import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from mobile_agent.sim import (
    SimError,
    SimulatorManager,
    apps,
    doctor,
)
from mobile_agent.sim.__main__ import build_parser, main
from mobile_agent.sim.biometrics import ENROLLMENT
from mobile_agent.sim.simctl import Result
from mobile_agent.tests.sim_fixtures import OTHER, UDID
from mobile_agent.tests.test_sim_fakes import (
    fake_manager,
    make_app,
)


class AppInfoTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.manager = SimulatorManager(data_dir=self.folder / "data")

    def test_a_simulator_build(self):
        app = make_app(self.folder, CFBundleDisplayName="Firefox Beta")
        info = self.manager.app_info(str(app))
        self.assertEqual(
            (info.bundle_id, info.name, info.version),
            ("org.mozilla.ios.Firefox", "Firefox Beta", "1.0 (1)"),
        )
        self.assertEqual(info.path, str(app.resolve()))

    def test_a_device_build_is_refused_with_the_simulator_build_line(self):
        app = make_app(self.folder, platforms=("iPhoneOS",))
        with self.assertRaises(SimError) as caught:
            self.manager.app_info(str(app))
        self.assertEqual(caught.exception.kind, "install")
        self.assertTrue(
            str(caught.exception).startswith(
                "This .app was built for a device. Build it for the iOS "
                "Simulator: xcodebuild"
            )
        )
        self.assertIn(
            "-destination 'generic/platform=iOS Simulator'", str(caught.exception)
        )
        self.assertTrue(caught.exception.fix)

    def test_not_an_app(self):
        (self.folder / "Empty.app").mkdir()
        (self.folder / "Broken.app").mkdir()
        (self.folder / "Broken.app" / "Info.plist").write_text("not a plist")
        for path in (
            self.folder / "missing.app",
            self.folder / "Empty.app",
            self.folder / "Broken.app",
        ):
            with self.subTest(path=path.name), self.assertRaises(SimError) as caught:
                self.manager.app_info(str(path))
            self.assertEqual(caught.exception.kind, "install")
        with self.assertRaises(SimError):
            self.manager.app_info(str(make_app(self.folder / "d", bundle="")))

    def test_without_supported_platforms_the_platform_name_decides(self):
        simulator = make_app(
            self.folder / "a", platforms=None, DTPlatformName="iphonesimulator"
        )
        self.assertEqual(
            self.manager.app_info(str(simulator)).bundle_id, "org.mozilla.ios.Firefox"
        )
        for folder, extra in (("b", {"DTPlatformName": "iphoneos"}), ("c", {})):
            with self.subTest(extra=extra), self.assertRaises(SimError):
                self.manager.app_info(
                    str(make_app(self.folder / folder, platforms=None, **extra))
                )

    ICONS = {
        "CFBundleIcons": {
            "CFBundlePrimaryIcon": {
                "CFBundleIconFiles": ["AppIcon60x60"],
                "CFBundleIconName": "AppIcon",
            }
        },
        "CFBundleIcons~ipad": {
            "CFBundlePrimaryIcon": {
                "CFBundleIconFiles": ["AppIcon60x60", "AppIcon76x76"]
            }
        },
    }

    def test_the_apps_own_icon_is_the_largest_its_bundle_names_scaled_to_120(self):
        app = make_app(self.folder, **self.ICONS)
        Image.new("RGB", (120, 120), (255, 0, 0)).save(app / "AppIcon60x60@2x.png")
        Image.new("RGB", (180, 180), (0, 0, 255)).save(app / "AppIcon60x60@3x.png")
        Image.new("RGB", (152, 152), (0, 255, 0)).save(app / "AppIcon76x76@2x~ipad.png")
        Image.new("RGB", (1024, 1024), (255, 255, 0)).save(app / "Unlisted.png")
        icon = self.manager.app_info(str(app)).icon
        with Image.open(io.BytesIO(icon)) as image:
            self.assertEqual((image.format, image.size), ("PNG", (120, 120)))
            self.assertEqual(image.convert("RGB").getpixel((60, 60)), (0, 0, 255))

    def test_an_icon_name_never_reaches_outside_the_bundle(self):
        Image.new("RGB", (180, 180), (0, 0, 255)).save(self.folder / "Outside.png")
        app = make_app(
            self.folder / "app", CFBundleIconFiles=["../Outside", "/tmp/Outside", "*"]
        )
        self.assertIsNone(apps.icon_png(app, apps.read_info(app)))


class ContainerGuardTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.devices = self.root / "Devices"
        patcher = mock.patch.object(apps, "devices_root", return_value=self.devices)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.container = (
            self.devices / UDID / "data" / "Containers" / "Data" / "Application" / "APP"
        )
        self.theirs = (
            self.devices
            / OTHER
            / "data"
            / "Containers"
            / "Data"
            / "Application"
            / "APP"
        )
        for container in (self.container, self.theirs):
            for name in apps.EMPTIED:
                (container / name).mkdir(parents=True)

    def test_a_missing_simulator_folder_is_refused(self):
        with self.assertRaises(SimError) as caught:
            apps.guard_container(
                str(self.container), "0A1B2C3D-0000-4000-8000-00000000FFFF"
            )
        self.assertIn("is missing", str(caught.exception))

    def test_another_simulators_container_is_refused(self):
        with self.assertRaises(SimError) as caught:
            apps.guard_container(str(self.theirs), UDID)
        self.assertEqual(caught.exception.kind, "simulator")
        self.assertIn("outside this simulator's folder", str(caught.exception))

    def test_a_symlink_or_dotdot_that_escapes_is_refused(self):
        link = (
            self.devices
            / UDID
            / "data"
            / "Containers"
            / "Data"
            / "Application"
            / "LINK"
        )
        link.symlink_to(self.theirs)
        escape = str(self.devices / UDID / "data" / ".." / ".." / OTHER / "data")
        for path in (str(link), escape, str(self.devices / UDID), "", "/tmp"):
            with self.subTest(path=path), self.assertRaises(SimError):
                apps.guard_container(path, UDID)

    def test_empty_keeps_the_folders_and_never_follows_links(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        (self.container / "Documents" / "note.txt").write_text("x")
        (self.container / "Library" / "Preferences").mkdir()
        (
            self.container / "Library" / "Preferences" / "org.mozilla.ios.Firefox.plist"
        ).write_text("x")
        (self.container / "tmp" / "outside-link").symlink_to(outside)
        (self.container / "SystemData").mkdir()
        (self.container / "SystemData" / "kept").write_text("x")
        removed = apps.empty_container(str(self.container), UDID)
        self.assertEqual(removed, 3)
        for name in apps.EMPTIED:
            self.assertTrue((self.container / name).is_dir())
            self.assertEqual(os.listdir(self.container / name), [])
        self.assertEqual((outside / "keep.txt").read_text(), "keep")
        self.assertTrue((self.container / "SystemData" / "kept").exists())

    def test_a_linked_documents_folder_is_refused(self):
        documents = self.container / "Documents"
        documents.rmdir()
        documents.symlink_to(self.root)
        with self.assertRaises(SimError):
            apps.empty_container(str(self.container), UDID)

    def test_every_reset_folder_is_checked_before_any_file_is_removed(self):
        note = self.container / "Documents" / "keep.txt"
        note.write_text("keep")
        library = self.container / "Library"
        library.rmdir()
        library.symlink_to(self.root)
        with self.assertRaises(SimError):
            apps.guard_container(str(self.container), UDID)
        with self.assertRaises(SimError):
            apps.empty_container(str(self.container), UDID)
        self.assertEqual(note.read_text(), "keep")


@contextlib.contextmanager
def biometric_simulator():
    with fake_manager() as (manager, simctl, wda, leases, root):
        lease = manager.acquire()
        target = lease.target
        lease.release()
        simctl.calls.clear()
        state = {"enrolled": False, "events": [], "read": None, "failure": None}

        def run(argv, **kwargs):
            if argv[2:3] == ["spawn"] and argv[4:5] == ["notifyutil"]:
                simctl.calls.append(argv)
                if target.wda_url not in leases.held:
                    raise AssertionError("biometric command ran without a device lease")
                if state["failure"] is not None:
                    return state["failure"]
                option, name = argv[5:7]
                if option == "-g":
                    output = state["read"]
                    if output is None:
                        output = f"{ENROLLMENT} {int(state['enrolled'])}\n"
                    return Result(0, output, "")
                if option == "-s":
                    state["enrolled"] = argv[7] == "1"
                if option == "-p":
                    state["events"].append(name)
                return Result(0, "", "")
            return simctl(argv, **kwargs)

        manager.run = run
        yield manager, target, simctl, leases, state


class BiometricTests(unittest.TestCase):
    def test_enroll_unenroll_and_status_confirm_result_and_release_lease(self):
        with biometric_simulator() as (manager, target, simctl, leases, state):
            for action, enabled in (
                ("status", False),
                ("enroll", True),
                ("enroll", True),
                ("status", True),
                ("unenroll", False),
            ):
                result = manager.biometric(target.udid.lower(), action)
                self.assertEqual(
                    result,
                    {
                        "udid": target.udid,
                        "action": action,
                        "type": "face-id",
                        "enrolled": enabled,
                        "event_sent": False,
                    },
                )
                self.assertFalse(leases.held)
            self.assertEqual(state["events"], [ENROLLMENT] * 3)

    def test_each_enrolled_match_type_sends_only_the_requested_event(self):
        with biometric_simulator() as (manager, target, simctl, leases, state):
            state["enrolled"] = True
            for kind, component in (("face-id", "pearl"), ("touch-id", "fingerTouch")):
                for action, outcome in (("match", "match"), ("nonmatch", "nomatch")):
                    with self.subTest(kind=kind, action=action):
                        result = manager.biometric(
                            target.udid, action, biometric_type=kind
                        )
                        self.assertTrue(result["event_sent"])
                        self.assertEqual(
                            state["events"].pop(),
                            f"com.apple.BiometricKit_Sim.{component}.{outcome}",
                        )
                        self.assertFalse(leases.held)

    def test_enrollment_must_be_verified(self):
        with biometric_simulator() as (manager, target, simctl, leases, state):
            state["read"] = f"{ENROLLMENT} 0"
            with self.assertRaisesRegex(SimError, "did not confirm"):
                manager.biometric(target.udid, "enroll")
            self.assertFalse(leases.held)

    def test_active_check_keeps_its_lease_and_prompt(self):
        with biometric_simulator() as (manager, target, simctl, leases, state):
            held = leases(target.wda_url)
            try:
                with self.assertRaises(SimError) as raised:
                    manager.biometric(target.udid, "match")
                self.assertEqual(raised.exception.kind, "busy")
                self.assertFalse(simctl.calls)
                self.assertEqual(leases.held, {target.wda_url})
            finally:
                held.close()

    def test_unknown_action_or_type_is_refused_before_device_access(self):
        with biometric_simulator() as (manager, target, simctl, leases, state):
            for action, kind in (("approve", "face-id"), ("match", "iris")):
                with self.assertRaises(SimError):
                    manager.biometric(target.udid, action, biometric_type=kind)
            self.assertFalse(simctl.calls)

    def test_cli_json_and_delivery_message(self):
        with biometric_simulator() as (manager, target, simctl, leases, state):
            code, out, err = run_cli(
                ["biometric", target.udid, "enroll", "--json"], manager
            )
            self.assertEqual(code, 0, err)
            self.assertTrue(json.loads(out)["enrolled"])
            code, out, err = run_cli(
                ["biometric", target.udid, "nonmatch", "--type", "touch-id"], manager
            )
            self.assertEqual(code, 0, err)
            self.assertIn(
                "Sent touch-id nonmatch event. Check the app for the result.", out
            )

    def test_match_requires_enrollment_and_known_status(self):
        with biometric_simulator() as (manager, target, simctl, leases, state):
            for output in (None, "", f"{ENROLLMENT} 2", f"{ENROLLMENT} 1 extra"):
                state["read"] = output
                with self.subTest(output=output), self.assertRaises(SimError):
                    manager.biometric(target.udid, "match")
                self.assertEqual(state["events"], [])
                self.assertFalse(leases.held)

    def test_real_unknown_foreign_missing_and_unbooted_devices_are_refused(self):
        for condition in ("real", "foreign", "missing", "shutdown"):
            with self.subTest(condition=condition), biometric_simulator() as setup:
                manager, target, simctl, leases, state = setup
                identifier = target.udid
                if condition == "real":
                    identifier = "00008110-000000000000001E"
                elif condition == "foreign":
                    simctl.devices[identifier]["name"] = "Someone else's simulator"
                elif condition == "missing":
                    del simctl.devices[identifier]
                else:
                    simctl.devices[identifier]["state"] = "Shutdown"
                with self.assertRaises(SimError):
                    manager.biometric(identifier, "enroll")
                self.assertFalse(simctl.simctl_calls("spawn"))
                self.assertFalse(leases.held)

    def test_failed_delivery_is_not_retried_and_releases_lease(self):
        with biometric_simulator() as (manager, target, simctl, leases, state):
            state["failure"] = Result(None, "", "notifyutil did not finish in 30 s")
            with self.assertRaises(SimError):
                manager.biometric(target.udid, "enroll")
            self.assertEqual(len(simctl.simctl_calls("spawn")), 1)
            self.assertFalse(leases.held)

    def test_registry_write_failure_releases_the_claimed_lease(self):
        with biometric_simulator() as (manager, target, simctl, leases, state):
            entries = manager.registry.read()

            @contextlib.contextmanager
            def failed_edit():
                yield entries
                raise OSError("registry write failed")

            with mock.patch.object(manager.registry, "edit", failed_edit):
                with self.assertRaisesRegex(OSError, "registry write failed"):
                    manager.biometric(target.udid, "enroll")
            self.assertFalse(leases.held)
            self.assertFalse(simctl.simctl_calls("spawn"))


def run_cli(argv, manager=None):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        if manager is None:
            code = main(argv)
        else:

            def build(progress=None, **kwargs):
                if not isinstance(manager, mock.Mock):
                    manager.progress = progress
                return manager

            before = manager.progress
            try:
                with mock.patch(
                    "mobile_agent.sim.api.SimulatorManager", side_effect=build
                ):
                    code = main(argv)
            finally:
                if not isinstance(manager, mock.Mock):
                    manager.progress = before
    return code, out.getvalue(), err.getvalue()


class RunTests(unittest.TestCase):
    def test_every_option_has_help_in_the_house_style(self):
        parser = build_parser()
        subs = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        actions = [("sim", action) for action in parser._actions]
        actions += [(f"sim {choice.dest}", choice) for choice in subs._choices_actions]
        actions += [
            (f"sim {name}", action)
            for name, sub in subs.choices.items()
            for action in sub._actions
        ]
        self.assertEqual(
            len([name for name, action in actions if action.dest == "sim_command"]), 1
        )
        for name, action in actions:
            if action.dest == "help":
                continue
            with self.subTest(command=name, option=action.dest):
                self.assertTrue(
                    action.help, f"mobster {name} {action.dest} has no help"
                )
                self.assertEqual(action.help[0], action.help[0].lower())
                self.assertFalse(action.help.endswith("."))

    def test_list_json_and_table(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            code, out, _ = run_cli(["list", "--json"], manager)
            self.assertEqual((code, json.loads(out)), (0, {"simulators": []}))
            code, out, _ = run_cli(["list"], manager)
            self.assertIn("No Mobster simulators yet", out)
            manager.acquire().release()
            code, out, _ = run_cli(["list"], manager)
            self.assertIn(
                "Mobster · iPhone 17 Pro · iOS 26.4  Booted  127.0.0.1:8310", out
            )

    def test_prepare_prints_each_phase_and_releases(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            code, out, err = run_cli(["prepare"], manager)
            self.assertEqual(code, 0)
            self.assertIn("Ready  Mobster · iPhone 17 Pro · iOS 26.4", out)
            for phase in (
                "create",
                "boot",
                "settings",
                "WebDriverAgent build",
                "WebDriverAgent start",
            ):
                self.assertIn(phase, out)
            self.assertIn("Creating simulator", err)
            self.assertEqual(leases.held, set())
            code, out, _ = run_cli(["prepare", "--json"], manager)
            data = json.loads(out)
            self.assertEqual(data["wda_url"], "http://127.0.0.1:8310")
            self.assertIn("total", data["timing"])

    def test_delete_all_and_prune(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            code, out, _ = run_cli(["delete", "--all"], manager)
            self.assertEqual(
                (code, out.strip()), (0, "No Mobster simulators to delete.")
            )
            manager.acquire().release()
            code, _, err = run_cli(["delete", "--all"], manager)
            self.assertEqual(code, 0)
            self.assertIn("Deleted Mobster · iPhone 17 Pro · iOS 26.4", err)
            with mock.patch(
                "mobile_agent.sim.xcode.installed_builds", return_value=set()
            ):
                code, out, _ = run_cli(["prune", "--json"], manager)
            self.assertEqual(
                json.loads(out), {"simulators": [], "entries": [], "wda_builds": []}
            )

    def test_a_sim_error_exits_1_with_the_fix(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            code, out, err = run_cli(
                ["delete", "0A1B2C3D-0000-4000-8000-00000000FFFF"], manager
            )
            self.assertEqual(code, 1)
            self.assertIn("isn't one of Mobster's simulators", err)
            self.assertIn("mobster sim list", err)
            code, out, err = run_cli(
                ["prepare", "--device", "iPhone 99", "--json"], manager
            )
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(out)["error"]["kind"], "environment")

    def test_doctor_json_exit_code(self):
        manager = mock.Mock()
        manager.doctor.return_value = [
            {
                "key": "xcode",
                "label": "Xcode",
                "state": "fail",
                "detail": "not installed",
                "fix": "Install Xcode",
            }
        ]
        code, out, _ = run_cli(["doctor", "--json"], manager)
        self.assertEqual(code, 1)
        self.assertEqual(
            json.loads(out), {"ok": False, "checks": manager.doctor.return_value}
        )
        manager.doctor.return_value[0]["state"] = "ok"
        code, out, _ = run_cli(["doctor"], manager)
        self.assertEqual(code, 0)
        self.assertIn("Ready for `mobster verify`.", out)
        manager.doctor.assert_called_with(fix=False)

    def test_interrupt_and_unexpected_errors_never_raise(self):
        manager = mock.Mock()
        manager.list.side_effect = KeyboardInterrupt
        self.assertEqual(run_cli(["list"], manager)[0], 130)
        manager.list.side_effect = RuntimeError("boom")
        code, _, err = run_cli(["list"], manager)
        self.assertEqual(code, 1)
        self.assertIn("boom", err)
        manager.list.side_effect = SimError("busy", "All are running checks.", "Wait.")
        code, _, err = run_cli(["list"], manager)
        self.assertEqual(
            (code, err), (1, "mobster sim: All are running checks.\n  Wait.\n")
        )


class ProgressLinesTests(unittest.TestCase):
    def test_on_a_terminal_still_lines_redraw_in_place(self):
        from mobile_agent.devtools import ProgressLines

        class Tty(io.StringIO):
            def isatty(self):
                return True

        stream = Tty()
        with mock.patch.dict("os.environ", {"TERM": "xterm-256color"}):
            lines = ProgressLines(stream)
        lines("Building WebDriverAgent")
        lines("Still building WebDriverAgent (30 s so far)")
        lines("Still building WebDriverAgent (1 min 00 s so far)")
        lines("Simulator: X")
        lines("Still booting X (30 s so far)")
        lines.end()
        self.assertEqual(
            stream.getvalue(),
            "Building WebDriverAgent\n\r\033[KStill building WebDriverAgent (30 s so far)"
            "\r\033[KStill building WebDriverAgent (1 min 00 s so far)\r\033[KSimulator: X\n"
            "\r\033[KStill booting X (30 s so far)\r\033[K",
        )

    def test_off_a_terminal_every_line_stays(self):
        from mobile_agent.devtools import ProgressLines

        stream = io.StringIO()
        lines = ProgressLines(stream)
        lines("Still building WebDriverAgent (30 s so far)")
        lines.end()
        self.assertEqual(
            stream.getvalue(), "Still building WebDriverAgent (30 s so far)\n"
        )


XCODE = "/Applications/Xcode.app/Contents/Developer"


VERSION = "Xcode 26.4\nBuild version 17E192\n"


SECRET = "sk-test-0123456789abcdefghijklmnopqrstuvwxyz"


@contextlib.contextmanager
def doctored(
    xcode=(XCODE, XCODE),
    version=Result(0, VERSION, ""),
    first_launch=Result(0, "", ""),
    machine="arm64",
    free=200 * 1024**3,
    git="/usr/bin/git",
    environ=None,
):
    with fake_manager(environ) as (manager, simctl, wda, leases, root):
        commands = []

        def run(argv, timeout=60, env=None):
            argv = [str(part) for part in argv]
            commands.append(argv)
            if argv[0].endswith("xcodebuild") and argv[1] == "-version":
                return version
            if argv[0].endswith("xcodebuild") and argv[1] == "-checkFirstLaunchStatus":
                return first_launch
            if argv[0] == "/usr/sbin/sysctl":
                return Result(0, "0\n", "")
            return simctl(argv, timeout, env)

        manager.run = run
        usage = mock.Mock(free=free)
        with (
            mock.patch("mobile_agent.mac_tools.developer_dir", return_value=xcode),
            mock.patch("mobile_agent.mac_tools.xcode_tool", return_value=git),
            mock.patch(
                "mobile_agent.sim.doctor.platform.machine", return_value=machine
            ),
            mock.patch("mobile_agent.sim.doctor.shutil.disk_usage", return_value=usage),
        ):
            yield manager, simctl, wda, commands


def by_key(checks):
    return {check["key"]: check for check in checks}


class DoctorTests(unittest.TestCase):
    def test_a_ready_mac(self):
        with doctored(environ={"OPENAI_API_KEY": ""}) as (
            manager,
            simctl,
            wda,
            commands,
        ):
            checks = manager.doctor()
        self.assertEqual(
            [check["key"] for check in checks],
            [
                "arch",
                "xcode",
                "first_launch",
                "runtime",
                "device_type",
                "git",
                "disk",
                "wda_build",
                "simulators",
                "ports",
                "key",
            ],
        )
        for check in checks:
            self.assertEqual(set(check), {"key", "label", "state", "detail", "fix"})
        states = {check["key"]: check["state"] for check in checks}
        self.assertEqual(
            states,
            {
                "arch": "ok",
                "xcode": "ok",
                "first_launch": "ok",
                "runtime": "ok",
                "device_type": "ok",
                "git": "ok",
                "disk": "ok",
                "wda_build": "warn",
                "simulators": "skip",
                "ports": "ok",
                "key": "skip",
            },
        )
        found = by_key(checks)
        self.assertEqual(
            found["xcode"]["detail"], "Xcode 26.4 (17E192) at /Applications/Xcode.app"
        )
        self.assertEqual(
            (found["runtime"]["detail"], found["device_type"]["detail"]),
            ("iOS 26.4", "iPhone 17 Pro"),
        )
        self.assertEqual(doctor.problems(checks), [])
        self.assertIn("Ready for `mobster verify`.", doctor.report(checks))

    def test_report_lists_fixes_under_problems(self):
        checks = [
            doctor.check("xcode", "Xcode", "fail", "not installed", "Install Xcode"),
            doctor.check("key", "Smart key", "skip", "not set", "Set it"),
        ]
        text = doctor.report(checks)
        self.assertIn("→ Install Xcode", text)
        self.assertNotIn("→ Set it", text)
        self.assertIn("1 problem to fix before `mobster verify` can run.", text)

    def test_no_xcode(self):
        with doctored(xcode=(None, "/Library/Developer/CommandLineTools")) as (
            manager,
            simctl,
            wda,
            commands,
        ):
            found = by_key(manager.doctor())
        self.assertEqual(found["xcode"]["state"], "fail")
        self.assertIn("App Store", found["xcode"]["fix"])
        self.assertEqual(
            (found["runtime"]["state"], found["device_type"]["state"]), ("skip", "skip")
        )
        self.assertNotIn("wda_build", found)

    def test_no_runtime_prints_the_download_and_never_runs_it(self):
        with doctored() as (manager, simctl, wda, commands):
            for runtime in simctl.runtimes:
                runtime["isAvailable"] = False
            found = by_key(manager.doctor(fix=True))
        # The table's words (states.ROWS): the sentence and command the Mac app and `mobster doctor` use.
        self.assertEqual(
            (found["runtime"]["state"], found["runtime"]["detail"], found["runtime"]["fix"]),
            ("fail", "No iOS Simulator runtime is installed.",
             "Download one with xcodebuild -downloadPlatform iOS."),
        )
        self.assertEqual(found["runtime"]["action"], {"kind": "run", "command": "xcodebuild -downloadPlatform iOS"})
        self.assertEqual(found["prepare"]["state"], "skip")
        self.assertEqual(simctl.simctl_calls("create"), [])
        self.assertFalse(
            any(
                "sudo" in command or "-downloadPlatform" in command
                for command in commands
            )
        )

    def test_fix_prepares_and_releases(self):
        with doctored() as (manager, simctl, wda, commands):
            checks = manager.doctor(fix=True)
            found = by_key(checks)
            self.assertEqual(found["prepare"]["state"], "ok")
            self.assertIn("127.0.0.1:8310", found["prepare"]["detail"])
            self.assertEqual(len(simctl.simctl_calls("create")), 1)
            self.assertEqual(manager.lease.held, set())
            self.assertEqual(found["simulators"]["state"], "ok")
            self.assertIn("booted, WebDriverAgent ready", found["simulators"]["detail"])
            self.assertLess(
                list(found).index("prepare"), list(found).index("simulators")
            )

    def test_the_key_is_reported_never_shown(self):
        with doctored(environ={"OPENAI_API_KEY": SECRET}) as (
            manager,
            simctl,
            wda,
            commands,
        ):
            checks = manager.doctor(fix=True)
        self.assertEqual(by_key(checks)["key"]["state"], "ok")
        self.assertNotIn(SECRET, json.dumps(checks))
        self.assertNotIn(SECRET, doctor.report(checks))
        self.assertNotIn(SECRET[:12], json.dumps(checks))

    KEY_VARIABLES = (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "MOBSTER_SMART_MODEL",
        "MOBSTER_HELPER_PROVIDER",
        "TEXT_MODEL",
        "TEXT_MODEL_API_KEY",
        "TEXT_MODEL_BASE_URL",
        "TEXT_MODEL_PROVIDER",
    )

    @contextlib.contextmanager
    def only(self, environ):
        with mock.patch.dict("os.environ"):
            for name in self.KEY_VARIABLES:
                os.environ.pop(name, None)
            os.environ.update(environ)
            yield

    def test_a_chosen_model_whose_key_is_missing_says_so_when_the_other_key_is_set(
        self,
    ):
        from mobile_agent import engines

        with self.only(
            {"OPENAI_API_KEY": SECRET, "MOBSTER_SMART_MODEL": "claude-sonnet-5-5"}
        ):
            self.assertIsNone(engines.smart_key())
            found = doctor.check_key()
        self.assertEqual(found["state"], "warn")
        self.assertEqual(
            found["detail"],
            "MOBSTER_SMART_MODEL is claude-sonnet-5-5, which needs ANTHROPIC_API_KEY; "
            "only your OpenAI key is set, so Smart can't run",
        )
        self.assertEqual(
            found["fix"],
            "Add ANTHROPIC_API_KEY=… to your env file, or remove MOBSTER_SMART_MODEL to "
            f"run Smart on {engines.OPENAI_SMART_MODEL}",
        )
        self.assertNotIn(SECRET, json.dumps(found))
        with self.only(
            {
                "ANTHROPIC_API_KEY": SECRET,
                "MOBSTER_SMART_MODEL": engines.OPENAI_SMART_MODEL,
            }
        ):
            found = doctor.check_key()
        self.assertEqual(found["state"], "warn")
        self.assertEqual(
            found["detail"],
            f"MOBSTER_SMART_MODEL is {engines.OPENAI_SMART_MODEL}, which needs "
            "OPENAI_API_KEY; only your Anthropic key is set, so Smart can't run",
        )
        self.assertTrue(
            found["fix"].endswith(f"run Smart on {engines.ANTHROPIC_SMART_MODEL}")
        )

    def test_a_crashing_check_is_reported_not_raised(self):
        with doctored() as (manager, simctl, wda, commands):
            with mock.patch.object(
                doctor, "check_disk", side_effect=PermissionError("denied")
            ):
                found = by_key(manager.doctor())
        self.assertEqual(found["disk"]["state"], "fail")
        self.assertIn("denied", found["disk"]["detail"])
