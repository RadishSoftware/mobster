import base64
import contextlib
import datetime
import io
import itertools
import json
import plistlib
import shutil
import socket
import tarfile
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mobile_agent import plan_gate
from mobile_agent.devkit import (
    DevError,
    asc,
    expo,
    expo_cli,
    nightly,
    testflight,
    testflight_cli,
    testflight_routes,
    xcode,
)
from mobile_agent.devkit import asc as asc_module
from mobile_agent.testkit import targets
from mobile_agent.testkit.reports import document
from mobile_agent.tests.test_devkit_asc import APP, Keychain, Phone, client, p8_for
from mobile_agent.tests.test_verify_fixtures import app as tree_xml
from mobile_agent.tests.test_verify_fixtures import node


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) if not isinstance(data, str) else data)


class Project(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        owned = mock.patch.object(expo, "owns_metro", return_value=True)
        owned.start()
        self.addCleanup(owned.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def expo_app(self, dev_client=True, config=None):
        deps = {"expo": "~54.0.0", "react-native": "0.81.4"}
        if dev_client:
            deps["expo-dev-client"] = "~6.0.0"
        write(self.root / "package.json", {"name": "habit-app", "dependencies": deps})
        write(self.root / "app.json", {"expo": config or {"name": "Habits", "slug": "habits", "scheme": "habits",
                                                          "ios": {"bundleIdentifier": "com.example.habits"}}})
        return expo.detect(self.root)


class DetectTests(Project):
    def test_an_expo_app_with_a_dev_client(self):
        app = self.expo_app()
        self.assertEqual((app.name, app.slug, app.bundle, app.schemes), ("Habits", "habits", "com.example.habits",
                                                                          ("habits",)))
        self.assertEqual((app.expo, app.client, app.native, app.sdk), (True, "dev-client", False, "~54.0.0"))

    def test_expo_go_without_the_dev_client_and_an_ios_folder_counts_as_native(self):
        (self.root / "ios" / "Habits.xcworkspace").mkdir(parents=True)
        app = self.expo_app(dev_client=False)
        self.assertEqual((app.client, app.native), ("expo-go", True))

    def test_an_app_config_is_read_through_expo_config(self):
        write(self.root / "package.json", {"dependencies": {"expo": "54"}})
        write(self.root / "app.config.ts", "export default {}")
        calls = []

        def runner(argv, cwd, log=None, timeout=None):
            calls.append(argv)
            return 0, 'warning\n{"name": "Dyn", "slug": "dyn", "scheme": ["dyn", "dyn2"]}'
        app = expo.detect(self.root, runner=runner)
        self.assertEqual(calls, [["npx", "--no-install", "expo", "config", "--json", "--type", "public"]])
        self.assertEqual((app.name, app.schemes), ("Dyn", ("dyn", "dyn2")))

    def test_react_native_without_expo_and_anything_else(self):
        write(self.root / "package.json", {"dependencies": {"react-native": "0.81"}})
        app = expo.detect(self.root)
        self.assertEqual((app.expo, app.client), (False, "dev-client"))
        self.assertIn("no ios folder", app.notes[0])
        write(self.root / "package.json", {"dependencies": {"react": "19"}})
        self.assertIsNone(expo.detect(self.root))
        self.assertIsNone(expo.detect(self.root / "nowhere"))


class LinkTests(Project):
    def test_the_dev_client_and_expo_go_links(self):
        app = self.expo_app()
        self.assertEqual(expo.preview_url(app, "192.168.1.20", 8081),
                         "exp+habits://expo-development-client/?url=http%3A%2F%2F192.168.1.20%3A8081")
        self.assertEqual(expo.app_link(app, "/settings/profile"), "habits://settings/profile")
        go = self.expo_app(dev_client=False)
        self.assertEqual(expo.preview_url(go, "127.0.0.1", 8082, "settings"), "exp://127.0.0.1:8082/--/settings")

    def test_metro_status(self):
        answer = lambda body: lambda url, timeout=None: contextlib.nullcontext(io.BytesIO(body))
        self.assertTrue(expo.metro_running(8081, opener=answer(b"packager-status:running")))
        self.assertFalse(expo.metro_running(8081, opener=answer(b"<html>")))

        def refused(url, timeout=None):
            raise OSError("refused")
        self.assertFalse(expo.metro_running(8081, opener=refused))


class ReloadTests(unittest.TestCase):
    def test_reload_upgrades_to_a_websocket_and_broadcasts_reload(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        got = {}

        def serve():
            conn, _ = server.accept()
            data = b""
            while b"\r\n\r\n" not in data:
                data += conn.recv(1024)
            got["request"] = data.decode()
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
            frame = b""
            while len(frame) < 2 or len(frame) < 6 + (frame[1] & 0x7F):
                chunk = conn.recv(1024)
                if not chunk:
                    break
                frame += chunk
            length, mask = frame[1] & 0x7F, frame[2:6]
            got["opcode"], got["masked"] = frame[0], bool(frame[1] & 0x80)
            got["payload"] = bytes(b ^ mask[i % 4] for i, b in enumerate(frame[6:6 + length])).decode()
            conn.close()
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.assertTrue(expo.reload(port))
        thread.join(5)
        server.close()
        self.assertTrue(got["request"].startswith("GET /message HTTP/1.1\r\n"))
        self.assertIn("Upgrade: websocket", got["request"])
        self.assertEqual((got["opcode"], got["masked"]), (0x81, True))
        self.assertEqual(json.loads(got["payload"]), {"version": 2, "method": "reload"})

    def test_no_metro_says_how_to_start_it(self):
        def refused(address, timeout):
            raise ConnectionRefusedError()
        with self.assertRaises(DevError) as caught:
            expo.reload(8081, connect=refused)
        self.assertIn("mobster expo preview", caught.exception.fix)


class MetroTests(Project):
    def test_start_runs_expo_start_in_the_background_and_stop_kills_its_group(self):
        app = self.expo_app()
        launched = []

        class Process:
            pid = 4242

            def poll(self):
                return None

        def popen(argv, **kwargs):
            launched.append((argv, kwargs))
            return Process()
        answers = iter([False, False, True])
        def opener(url, timeout=None):
            response = io.BytesIO(b"packager-status:running" if next(answers) else b"")
            response.headers = {"X-React-Native-Project-Root": str(app.root)}
            return contextlib.nullcontext(response)
        pid = expo.start_metro(app, 8081, popen=popen, opener=opener, sleep=lambda s: None)
        self.assertEqual(pid, 4242)
        argv, kwargs = launched[0]
        self.assertEqual(argv, ["npx", "expo", "start", "--port", "8081", "--lan", "--dev-client"])
        self.assertTrue(kwargs["start_new_session"])
        killed = []
        self.assertEqual(expo.stop_metro(self.root, kill=lambda pid, sig: killed.append(pid)), 4242)
        self.assertEqual(killed, [-4242])
        self.assertIsNone(expo.stop_metro(self.root))

    def test_a_metro_that_never_answers_points_at_its_log(self):
        app = self.expo_app()

        class Dead:
            pid = 1

            def poll(self):
                return 1
        with self.assertRaises(DevError) as caught:
            expo.start_metro(app, popen=lambda argv, **k: Dead(), opener=lambda u, timeout=None: (_ for _ in ()).throw(
                OSError()), sleep=lambda s: None)
        self.assertIn("metro.log", caught.exception.message)


def fake_xcode(calls, bundle="com.example.habits"):
    def runner(argv, cwd, log=None, timeout=None):
        calls.append(argv)
        if argv[:3] == ["npx", "expo", "prebuild"]:
            (Path(cwd) / "ios" / "Habits.xcworkspace").mkdir(parents=True)
            return 0, ""
        if "-list" in argv:
            return 0, json.dumps({"workspace": {"schemes": ["Habits", "Pods-Habits"]}}) + \
                "\n2026-10-14 xcodebuild[1:2] DVTDeviceOperation: Encountered a build number \"\"\n"
        configuration = argv[argv.index("-configuration") + 1]
        app = (Path(cwd) / ".mobster" / "build" / "Build" / "Products" / f"{configuration}-iphonesimulator"
               / "Habits.app")
        app.mkdir(parents=True, exist_ok=True)
        (app / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": bundle}))
        return 0, "** BUILD SUCCEEDED **"
    return runner


class BuildTests(Project):
    def test_a_simulator_build_prebuilds_then_builds_release_with_the_js_inside(self):
        app = self.expo_app()
        calls = []
        built = expo.build_sim(app, runner=fake_xcode(calls))
        self.assertEqual(calls[0], ["npx", "expo", "prebuild", "--platform", "ios"])
        build = calls[-1]
        self.assertEqual(build[build.index("-configuration") + 1], "Release")
        self.assertEqual(build[build.index("-scheme") + 1], "Habits")
        self.assertIn("CODE_SIGNING_ALLOWED=NO", build)
        self.assertEqual((built["bundle"], Path(built["path"]).name), ("com.example.habits", "Habits.app"))
        calls.clear()
        expo.build_sim(app, configuration="Debug", runner=fake_xcode(calls))
        self.assertNotIn(["npx", "expo", "prebuild", "--platform", "ios"], calls)  # the ios folder is there now

    def test_mobster_test_pr_builds_an_expo_app_release_and_any_other_project_as_before(self):
        from mobile_agent.testkit import prrun
        from mobile_agent.testkit.api import Options
        self.expo_app()
        calls, lines = [], []
        built = prrun.build_app(self.root, prrun.PrOptions(), Options(), runner=fake_xcode(calls),
                                progress=lines.append)
        self.assertIn("Release-iphonesimulator", built["path"])
        self.assertEqual(calls[0][:3], ["npx", "expo", "prebuild"])
        self.assertIn("Habits is an Expo app: building Release, with the JavaScript inside", lines)
        (self.root / "package.json").unlink()
        self.assertIsNone(prrun._build_react_native(self.root, prrun.PrOptions(), "sim"))

    def test_the_iphone_build_is_expo_run_ios_and_a_signing_failure_names_the_fix(self):
        app = self.expo_app()
        calls = []
        expo.build_device(app, "UDID-1", runner=lambda argv, cwd, log=None, timeout=None: calls.append(argv) or (0, ""))
        self.assertEqual(calls[0], ["npx", "expo", "run:ios", "--device", "UDID-1", "--configuration", "Debug",
                                    "--no-bundler"])
        with self.assertRaises(DevError) as caught:
            expo.build_device(app, "UDID-1", runner=lambda *a, **k: (1, "No signing certificate found"))
        self.assertIn("Signing & Capabilities", caught.exception.fix)

    def test_an_eas_simulator_build_is_unpacked(self):
        app = self.expo_app()

        def runner(argv, cwd, log=None, timeout=None):
            output = Path(argv[argv.index("--output") + 1])
            staging = self.root / "staging" / "Habits.app"
            staging.mkdir(parents=True)
            (staging / "Info.plist").write_text("x")
            with tarfile.open(output, "w:gz") as archive:
                archive.add(staging, arcname="Habits.app")
            return 0, ""
        built = expo.build_eas(app, "development", simulator=True, runner=runner)
        self.assertTrue(built["path"].endswith(".mobster/expo/eas/Habits.app"))
        self.assertEqual(built["platform"], "sim")


class Wda:
    def __init__(self, xml, fail_open=False):
        self.xml, self.fail_open, self.opened = xml, fail_open, []

    @contextlib.contextmanager
    def __call__(self, record):
        def request(method, path, body=None, timeout=15):
            if path == "/url":
                if self.fail_open:
                    from mobile_agent.phone_io import PhoneIOError
                    raise PhoneIOError("WebDriverAgent refused the request.")
                self.opened.append(body["url"])
                return None
            if path.startswith("/source"):
                return self.xml
            if path == "/screenshot":
                return base64.b64encode(b"\x89PNG").decode()
            return None
        yield request


class PreviewTests(Project):
    def test_preview_opens_the_dev_client_then_the_screen_and_checks_it(self):
        app = self.expo_app()
        wda = Wda(tree_xml(node("StaticText", "Welcome back")))
        record = {"name": "Test iPhone", "kind": "usb"}
        with mock.patch("mobile_agent.wda.system_dialogs.read", return_value=None):
            result = expo_cli.preview(app, record, wda, url="exp+habits://expo-development-client/?url=x",
                                      link="habits://home", expect=["Welcome back", "Streak"], wait=0,
                                      sleep=lambda s: None)
        self.assertEqual(wda.opened, ["exp+habits://expo-development-client/?url=x", "habits://home"])
        self.assertEqual([(item["ok"]) for item in result["expect"]], [True, False])
        self.assertEqual(Path(result["screenshot"]).read_bytes(), b"\x89PNG")

    def test_a_client_that_isnt_installed_says_how_to_install_it(self):
        app = self.expo_app()
        with self.assertRaises(DevError) as caught:
            expo_cli.preview(app, {"name": "iPhone 17 Pro", "kind": "simulator"}, Wda("", fail_open=True), url="u")
        self.assertIn('mobster expo build --device "iPhone 17 Pro" --install', caught.exception.fix)

    def test_the_command_starts_metro_and_uses_the_macs_lan_address_for_a_phone(self):
        self.expo_app()
        from mobile_agent.cli_parser import build_parser
        args = build_parser().parse_args(["expo", "preview", "--expect", "Welcome back", "--wait", "0"])
        wda = Wda(tree_xml(node("StaticText", "Welcome back")))
        out = io.StringIO()
        with mock.patch.object(expo, "metro_running", return_value=False), \
                mock.patch.object(expo, "start_metro", return_value=77) as start, \
                mock.patch.object(expo, "lan_ip", return_value="10.0.0.5"), \
                mock.patch("mobile_agent.wda.system_dialogs.read", return_value=None), mock.patch("sys.stdout", out), \
                mock.patch("sys.stderr", io.StringIO()):
            code = expo_cli.run(args, cwd=self.root, devices=lambda name: {"name": "Test iPhone", "kind": "usb"},
                                wda=wda)
        self.assertEqual(code, 0)
        start.assert_called_once()
        self.assertEqual(wda.opened, ["exp+habits://expo-development-client/?url=http%3A%2F%2F10.0.0.5%3A8081"])
        self.assertIn('✓ text "Welcome back": found', out.getvalue())
        self.assertIn("mobster expo stop", out.getvalue())

    def test_not_an_expo_folder(self):
        from mobile_agent.cli_parser import build_parser
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            code = expo_cli.run(build_parser().parse_args(["expo", "doctor"]), cwd=self.root,
                                devices=lambda n: None, wda=lambda r: None)
        self.assertEqual(code, 3)
        self.assertIn("isn't an Expo or React Native app", err.getvalue())


class PlanTests(unittest.TestCase):
    def test_what_acts_needs_the_plan(self):
        from types import SimpleNamespace
        for verb, needs in (("preview", True), ("build", True), ("reload", True), ("doctor", False),
                            ("stop", False)):
            self.assertEqual(plan_gate.cli_needs_plan("expo", SimpleNamespace(expo_command=verb)), needs, verb)


class NightlyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "Habits"
        self.root.mkdir()
        self.home = Path(self._tmp.name) / "home"

    def tearDown(self):
        self._tmp.cleanup()

    def test_on_writes_a_per_user_agent_that_runs_every_night_and_off_removes_it(self):
        calls = []
        path = nightly.turn_on(self.root, "02:30", ["--device", "all"], home=self.home, uid=501,
                               run=lambda argv: calls.append(argv) or 0)
        data = plistlib.loads(path.read_bytes())
        self.assertTrue(path.name.startswith("dev.mobster.nightly.habits-"))
        self.assertEqual(data["StartCalendarInterval"], {"Hour": 2, "Minute": 30})
        self.assertEqual(data["ProgramArguments"][-5:], ["test", "nightly", "run", "--device", "all"])
        self.assertEqual(data["WorkingDirectory"], str(self.root.resolve()))
        self.assertEqual(calls[-1], ["launchctl", "bootstrap", "gui/501", str(path)])
        state = nightly.status(self.root, home=self.home)
        self.assertEqual((state["on"], state["at"], state["flags"]), (True, "02:30", ["--device", "all"]))
        self.assertTrue(nightly.turn_off(self.root, home=self.home, uid=501, run=lambda argv: 0))
        self.assertFalse(path.exists())
        with self.assertRaises(DevError):
            nightly.parse_time("25:00")

    def test_a_night_writes_the_morning_report_notifies_and_alerts_on_a_failure(self):
        doc = {"summary": {"passed": 1, "failed": 1}, "exitCode": 1, "durationMs": 180000, "checks": [
            {"name": "Paywall", "file": ".mobster/checks/paywall.yaml", "results": [
                {"status": "failed", "device": "Test iPhone · iOS 26.4",
                 "reason": {"message": "text “Choose your plan” failed"}}]},
            {"name": "Today", "file": ".mobster/checks/today.yaml", "results": [{"status": "passed"}]}]}
        seen, said, alerted = [], [], []

        def runner(argv, cwd, env):
            seen.append(argv)
            return 1, "progress\n" + json.dumps(doc)
        code, text = nightly.run_night(self.root, ["--device", "all"], runner=runner, today=datetime.date(2026, 10, 15),
                                       notifier=lambda title, line: said.append((title, line)),
                                       alert=lambda url, data: alerted.append((url, data)),
                                       environ={"MOBSTER_ALERT_WEBHOOK_URL": "https://hooks.slack.com/x"})
        self.assertEqual(code, 1)
        argv = seen[0]
        self.assertEqual(argv[argv.index("test"):argv.index("test") + 3], ["test", "--video", "--json"])
        self.assertEqual(argv[-2:], ["--device", "all"])
        self.assertIn("# Overnight checks, 2026-10-15", text)
        self.assertIn("- **failed**: Paywall on Test iPhone, iOS 26.4. text “Choose your plan” failed", text)
        self.assertIn("Run it again: `mobster test .mobster/checks/paywall.yaml`", text)
        self.assertNotIn("Today", text)
        self.assertTrue((self.root / ".mobster/nightly/2026-10-15/morning.md").is_file())
        self.assertEqual(json.loads((self.root / ".mobster/nightly/latest.json").read_text())["exitCode"], 1)
        self.assertEqual(said[0][0], "Overnight checks: Habits")
        self.assertEqual(alerted[0][1]["status"], "failed")
        nightly.run_night(self.root, [], runner=lambda *a: (0, json.dumps({"summary": {"passed": 2}, "checks": []})),
                          notifier=lambda *a: None, alert=lambda *a: alerted.append(a),
                          environ={"MOBSTER_ALERT_WEBHOOK_URL": "https://hooks.slack.com/x"})
        self.assertEqual(len(alerted), 1)  # a night that passed alerts nobody


class EveryPhoneTests(unittest.TestCase):
    def test_all_means_every_ready_iphone_beside_the_simulators(self):
        phones = [{"id": "usb:1", "name": "Test iPhone", "kind": "usb"}, {"id": "usb:2", "name": "Lab", "kind": "usb"}]
        named = {p["id"]: p for p in phones}
        found = targets.resolve(sims=("iPhone 17 Pro",), devices=("all",), every_phone=lambda: phones,
                                named_device=lambda name: named.get(name))
        self.assertEqual([t.key for t in found], ["sim:iPhone 17 Pro@", "device:usb:1", "device:usb:2"])
        with self.assertRaises(targets.TargetError):
            targets.resolve(devices=("all",), every_phone=lambda: [], named_device=lambda name: None)


class CliTests(unittest.TestCase):
    def test_at_goes_with_nightly(self):
        from mobile_agent.cli_parser import build_parser
        from mobile_agent.testkit import cli
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.run(build_parser().parse_args(["test", "--at", "02:00"]))
        self.assertEqual(code, 3)
        self.assertIn("--at goes with nightly", err.getvalue())
        args = SimpleNamespace(sim=None, device=["all"], tag=["smoke"], testflight=None, parallel=2, retries=None,
                               keyless=False)
        self.assertEqual(cli._night_flags(args), ["--device", "all", "--tag", "smoke", "--parallel", "2"])


def screen(*buttons, version="1.4.2", build="89"):
    from mobile_agent.tests.test_verify_fixtures import app, node
    return app(*(node("Button", word) for word in buttons), node("StaticText", f"Version {version} ({build})"))


BUNDLE = "com.example.habits"


def build(id_, number, *, processing="VALID", expired=False, internal="IN_BETA_TESTING", external=None,
          version="v1"):
    return {"id": id_, "attributes": {"version": number, "uploadedDate": "2026-10-1%sT09:30:00Z" % number[-1],
                                       "processingState": processing, "expired": expired},
            "relationships": {"preReleaseVersion": {"data": {"type": "preReleaseVersions", "id": version}},
                              "buildBetaDetail": {"data": {"type": "buildBetaDetails", "id": "d" + id_}}}}


def detail(id_, internal, external=None):
    return {"type": "buildBetaDetails", "id": "d" + id_,
            "attributes": {"internalBuildState": internal, "externalBuildState": external}}


ROUTES = {"/v1/apps": APP, "/v1/builds": {
    "data": [build("b90", "90", processing="PROCESSING"), build("b89", "89"), build("b88", "88"),
             build("b70", "70", expired=True, internal="EXPIRED")],
    "included": [{"type": "preReleaseVersions", "id": "v1", "attributes": {"version": "1.4.2"}},
                 detail("b90", "PROCESSING"), detail("b89", "IN_BETA_TESTING"), detail("b88", "IN_BETA_TESTING"),
                 detail("b70", "EXPIRED")]}}


PHONE = {"name": "Test iPhone", "kind": "usb"}


def fast_clock():
    ticks = itertools.count(0, 100)
    return lambda: next(ticks)


class KeyTests(unittest.TestCase):
    def test_a_projects_key_wins_over_the_shared_one_and_other_projects_dont_see_it(self):
        store = Keychain()
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            asc.save_key("SHAREDKEY1", None, p8_for(3), store=store)
            asc.save_key("PROJECTKEY", None, p8_for(4), store=store, project=one)
            self.assertEqual(asc.load_key(store=store, environ={}, project=one).key_id, "PROJECTKEY")
            self.assertEqual(asc.load_key(store=store, environ={}, project=one).source, "Keychain (this project)")
            self.assertEqual(asc.load_key(store=store, environ={}, project=two).key_id, "SHAREDKEY1")
            asc.remove_key(store=store, project=one)
            self.assertEqual(asc.load_key(store=store, environ={}, project=one).key_id, "SHAREDKEY1")

    def test_a_bad_key_is_refused_before_it_is_saved_and_a_revoked_one_says_so(self):
        store = Keychain()
        with self.assertRaises(DevError):
            asc.save_key("ABCDEFGH12", None, "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----",
                         store=store, project="/tmp")
        with self.assertRaises(DevError):
            asc.save_key("short", None, p8_for(5), store=store)
        self.assertEqual(store.items, {})

        def refuse(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(b"{}"))
        api = asc.Client(asc.Key("ABCDEFGH12", None, p8_for(7), "test"), opener=refuse)
        with self.assertRaises(DevError) as caught:
            testflight.builds(api, BUNDLE)
        self.assertIn("refused the key", caught.exception.message)
        self.assertIn("Issuer ID", caught.exception.fix)


class TestflightBuildTests(unittest.TestCase):
    def rows(self):
        api, _ = client(ROUTES)
        return testflight.builds(api, BUNDLE)[1]

    def test_each_build_says_whether_it_installs(self):
        rows = {row["build"]: row for row in self.rows()}
        self.assertEqual([rows[n]["installable"] for n in ("90", "89", "88", "70")], [False, True, True, False])
        self.assertIn("processing", rows["90"]["why"])
        self.assertIn("expired", rows["70"]["why"])
        self.assertEqual(testflight.choose(self.rows())["build"], "89")  # the newest that installs, not the newest

    def test_an_expired_or_processing_build_is_refused_by_number(self):
        for number, word in (("70", "expired"), ("90", "processing")):
            with self.assertRaises(DevError) as caught:
                testflight.choose(self.rows(), build=number)
            self.assertIn(word, caught.exception.message)
        with self.assertRaises(DevError) as caught:
            testflight.choose(self.rows(), build="12")
        self.assertIn("no build 12", caught.exception.message)
        self.assertEqual(testflight.choose(self.rows(), build="88", version="1.4.2")["id"], "b88")

    def test_no_builds_and_nothing_installable(self):
        api, _ = client({"/v1/apps": APP, "/v1/builds": {"data": [], "included": []}})
        _, rows = testflight.builds(api, BUNDLE)
        with self.assertRaises(DevError) as caught:
            testflight.choose(rows)
        self.assertIn("no builds", caught.exception.message)
        api, _ = client({"/v1/apps": APP, "/v1/builds": {"data": [build("b70", "70", expired=True)],
                                                         "included": [detail("b70", "EXPIRED")]}})
        with self.assertRaises(DevError) as caught:
            testflight.choose(testflight.builds(api, BUNDLE)[1])
        self.assertIn("expired", caught.exception.message)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".mobster").mkdir()
        mock.patch("mobile_agent.wda.system_dialogs.read", return_value=None).start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self._tmp.cleanup)

    def install(self, phone, **kwargs):
        api, apple = client(ROUTES)
        self.apple = apple
        return testflight.install(api, PHONE, phone, BUNDLE, root=self.root, sleep=lambda s: None,
                                  clock=kwargs.pop("clock", None) or fast_clock(), **kwargs)

    def test_the_newest_build_is_installed_and_recorded_with_its_number(self):
        phone = Phone([screen("Back"), screen("Update"), screen("Open")])
        done = self.install(phone, now=lambda: __import__("datetime").datetime(2026, 10, 18, 9, 0, 0))
        self.assertEqual((done["result"], phone.taps), ("installed", ["Update"]))
        self.assertEqual(testflight.recorded_build(self.root, BUNDLE),
                         {"bundleId": BUNDLE, "version": "1.4.2", "build": "89", "installedAt": "2026-10-18T09:00:00Z"})
        self.assertTrue(all(method == "GET" for method, _, _ in self.apple.seen))

    def test_a_different_build_on_the_phone_is_never_attributed_to_the_requested_build(self):
        phone = Phone([screen("Open", build="88")])
        with self.assertRaises(DevError):
            self.install(phone)
        self.assertEqual(phone.taps, [])
        self.assertIsNone(testflight.recorded_build(self.root, BUNDLE))

    def test_an_unreadable_install_record_refuses_before_touching_the_phone(self):
        path = testflight._record_path(self.root, BUNDLE)
        path.parent.mkdir(parents=True)
        path.write_text("broken")
        phone = Phone([screen("Install"), screen("Open")])
        with self.assertRaises(DevError):
            self.install(phone)
        self.assertEqual(phone.opened, [])

    def test_an_unknown_older_build_is_not_relabelled_with_the_newest_api_build(self):
        testflight._write_record(self.root, BUNDLE, {"status": "installing", "buildId": "b88", "build": "88"})
        phone = Phone([screen("Open")])
        with self.assertRaises(DevError):
            self.install(phone)
        self.assertEqual(testflight.read_record(self.root, BUNDLE)["buildId"], "b88")
        self.assertEqual(phone.opened, [])

    def test_an_older_build_is_never_picked_for_you(self):
        phone = Phone([screen("Update")])
        with self.assertRaises(DevError) as caught:
            self.install(phone, build="88")
        self.assertIn("Previous Builds", caught.exception.fix)
        self.assertIn("1.4.2 (88)", caught.exception.fix)
        self.assertEqual((phone.taps, phone.opened), ([], []))
        self.assertIsNone(testflight.read_record(self.root, BUNDLE))

    def test_an_expired_build_never_reaches_the_phone(self):
        phone = Phone([screen("Update")])
        with self.assertRaises(DevError):
            self.install(phone, build="70")
        self.assertEqual((phone.taps, phone.opened), ([], []))

    def test_a_tap_that_never_finishes_is_unknown_and_is_never_tapped_again(self):
        phone = Phone([screen("Install")])  # TestFlight keeps saying Install
        with self.assertRaises(asc.UnknownOutcome) as caught:
            self.install(phone, timeout=500)
        self.assertEqual(phone.taps, ["Install"])
        self.assertIn("unknown", caught.exception.message)
        self.assertEqual(testflight.read_record(self.root, BUNDLE)["status"], "installing")
        self.assertIsNone(testflight.recorded_build(self.root, BUNDLE))  # a report names no build that may not be there
        again = Phone([screen("Install")])
        with self.assertRaises(asc.NeedsTap) as caught:
            self.install(again)
        self.assertEqual(again.taps, [])
        self.assertIn("Look at the iPhone", caught.exception.fix)
        self.assertEqual(testflight.read_record(self.root, BUNDLE)["status"], "installing")
        finished = Phone([screen("Open")])  # the install did finish: Open settles it, still with no tap
        self.assertEqual(self.install(finished)["result"], "already")
        self.assertEqual(finished.taps, [])
        self.assertEqual(testflight.recorded_build(self.root, BUNDLE)["build"], "89")

    def test_terms_and_a_simulator_leave_the_record_as_it_was(self):
        phone = Phone([screen("Accept", "Decline")])
        with self.assertRaises(DevError):
            self.install(phone)
        self.assertEqual(phone.taps, [])
        self.assertIsNone(testflight.read_record(self.root, BUNDLE))

    def test_the_record_stays_out_of_git(self):
        self.install(Phone([screen("Open")]))
        self.assertIn("runs", (self.root / ".mobster" / ".gitignore").read_text())


class ReportTests(unittest.TestCase):
    def test_the_builds_go_on_results_json_junit_and_the_report(self):
        from mobile_agent.testkit import reports
        built = [{"bundleId": BUNDLE, "version": "1.4.2", "build": "89", "installedAt": "2026-10-18T09:00:00Z"}]
        one = {"name": "Home", "file": "home.yaml", "results": [{"deviceKey": "d", "device": "Test iPhone",
                                                                 "status": "passed", "durationMs": 10}]}
        doc = document({"checks": [one], "summary": {}, "devices": [{"key": "d", "name": "Test iPhone"}]},
                       suite_id="s1", options={}, version="9", testflight=built)
        self.assertEqual(doc["testflight"], built)
        self.assertNotIn("testflight", document({}, suite_id="s1", options={}, version="9"))
        self.assertIn("TestFlight 1.4.2 (89)", reports.render_html(doc, tempfile.gettempdir()))
        self.assertIn(f'name="testflight.{BUNDLE}" value="1.4.2 (89)"', reports.junit(doc).decode())


    def test_a_different_iphone_never_inherits_the_recorded_build(self):
        from mobile_agent.testkit.api import _testflight_builds
        with tempfile.TemporaryDirectory() as tmp:
            testflight._write_record(tmp, BUNDLE, {"bundleId": BUNDLE, "version": "1.4.2", "build": "89",
                                                   "status": "installed", "devices": ["Phone A"]})
            self.assertEqual(_testflight_builds(tmp, (BUNDLE,), ("Phone B",)), [])
            self.assertEqual(len(_testflight_builds(tmp, (BUNDLE,), ("Phone A",))), 1)

    def test_a_run_names_the_recorded_build_of_each_testflight_app_and_only_that(self):
        from mobile_agent.testkit.api import _testflight_builds
        with tempfile.TemporaryDirectory() as tmp:
            testflight._write_record(tmp, BUNDLE, {"bundleId": BUNDLE, "version": "1.4.2", "build": "89",
                                                   "status": "installed", "installedAt": "t", "buildId": "b89"})
            testflight._write_record(tmp, "com.example.other", {"bundleId": "com.example.other", "build": "5",
                                                                "status": "installing"})
            found = _testflight_builds(Path(tmp), (BUNDLE, "com.example.other", "com.example.none"))
        self.assertEqual(found, [{"bundleId": BUNDLE, "version": "1.4.2", "build": "89", "installedAt": "t"}])


class TestflightCliTests(unittest.TestCase):
    def run_cli(self, command, tmp, phone=None, **fields):
        api, _ = client(ROUTES)
        args = SimpleNamespace(testflight_command=command, app=BUNDLE, json=True, **fields)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch("mobile_agent.wda.system_dialogs.read", return_value=None):
            code = testflight_cli.run(args, client=api, cwd=tmp, devices=lambda name: PHONE, wda=phone)
        return code, out.getvalue()

    def test_list_and_install_print_the_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".mobster").mkdir()
            code, out = self.run_cli("list", tmp, limit=10)
            self.assertEqual(code, 0)
            self.assertEqual([row["installable"] for row in json.loads(out)["builds"]], [False, True, True, False])
            phone = Phone([screen("Install"), screen("Open")])
            with mock.patch.object(asc_module.time, "sleep"):
                code, out = self.run_cli("install", tmp, phone=phone, build=None, version=None, device=None,
                                         timeout=600)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["build"], {"version": "1.4.2", "build": "89"})

    def test_a_missing_key_says_how_to_add_one(self):
        args = SimpleNamespace(testflight_command="list", app=BUNDLE, json=True, limit=10)
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(err), \
                mock.patch.dict("os.environ", {}, clear=False), \
                mock.patch("mobile_agent.devkit.asc._store", return_value=Keychain()):
            code = testflight_cli.run(args, cwd=tmp)
        self.assertEqual(code, 3)
        self.assertIn("mobster asc key add", err.getvalue())


class RouteTests(unittest.TestCase):
    def request(self, method, body=None, query=None, gate=None):
        return SimpleNamespace(method=method, body=body, query=query or {}, runtime=SimpleNamespace(plan_gate=gate))

    def test_the_key_route_never_returns_the_key(self):
        store = Keychain()
        with tempfile.TemporaryDirectory() as tmp, mock.patch("mobile_agent.devkit.asc._store", return_value=store), \
                mock.patch.dict("os.environ", {"MOBSTER_ASC_KEY_ID": ""}):
            saved = testflight_routes.key(self.request("POST", {"project": tmp, "keyId": "ABCDEFGH12",
                                                                "p8": p8_for(9)}))
            self.assertEqual(saved.status, 200)
            shown = testflight_routes.key(self.request("GET", query={"project": [tmp]}))
            self.assertEqual(shown.value["keyId"], "ABCDEFGH12")
            self.assertNotIn("BEGIN", json.dumps(saved.value) + json.dumps(shown.value))
            testflight_routes.key(self.request("DELETE", query={"project": [tmp]}))
            self.assertFalse(testflight_routes.key(self.request("GET", query={"project": [tmp]})).value["configured"])
            with self.assertRaises(ValueError):
                testflight_routes.key(self.request("POST", {"project": "relative", "keyId": "ABCDEFGH12",
                                                            "p8": p8_for(9)}))

    def test_install_needs_the_plan_and_a_project_and_answers_unknown_outcomes_with_409(self):
        gate = SimpleNamespace(require=mock.Mock(side_effect=PermissionError("plan_required")))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PermissionError):
                testflight_routes.install(self.request("POST", {"project": tmp, "bundle": BUNDLE}, gate=gate))
            with self.assertRaises(ValueError):
                testflight_routes.install(self.request("POST", {"bundle": BUNDLE}))
            api, _ = client(ROUTES)
            with mock.patch.object(testflight_routes, "_client", return_value=api), \
                    mock.patch("mobile_agent.phone_io.choose_device", return_value=PHONE), \
                    mock.patch.object(testflight, "install", side_effect=asc.UnknownOutcome("unknown", "look")):
                response = testflight_routes.install(self.request("POST", {"project": tmp, "bundle": BUNDLE}))
            self.assertEqual((response.status, response.value["code"]), (409, "outcome_unknown"))


SCHEME = Path(__file__).parent / "fixtures" / "xctest" / "Daybreak.xcscheme"


class HookTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name) / "Daybreak.xcodeproj"
        target = self.project / "xcshareddata" / "xcschemes"
        target.mkdir(parents=True)
        shutil.copy(SCHEME, target / "Daybreak.xcscheme")
        self.file = target / "Daybreak.xcscheme"

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_scheme_round_trips_byte_for_byte(self):
        import xml.etree.ElementTree as ET
        text = '<?xml version="1.0" encoding="UTF-8"?>\n' + xcode.xcode_xml(ET.parse(SCHEME).getroot()) + "\n"
        self.assertEqual(text, SCHEME.read_text())

    def test_hook_adds_one_post_action_with_the_app_s_build_settings_and_unhook_restores_the_file(self):
        original = self.file.read_text()
        xcode.hook(self.project, "Daybreak", tags=("smoke",),
                   agent="/Applications/Mobster.app/Contents/MacOS/mobster-agent")
        xcode.hook(self.project, "Daybreak", tags=("smoke",),
                   agent="/Applications/Mobster.app/Contents/MacOS/mobster-agent")
        text = self.file.read_text()
        self.assertEqual(text.count('title = "Mobster checks"'), 1)
        self.assertIn('scriptText = "/Applications/Mobster.app/Contents/MacOS/mobster-agent xcode after-build '
                      '--background --tag smoke &gt; /dev/null 2&gt;&amp;1"', text)
        self.assertLess(text.index("<PostActions>"), text.index("<BuildActionEntries>"))
        self.assertIn("<EnvironmentBuildable>", text)
        self.assertIn('BuildableName = "Daybreak.app"', text.split("<EnvironmentBuildable>")[1])
        added = set(text.splitlines()) - set(original.splitlines())
        self.assertLessEqual(len(added), 19)
        self.assertTrue(xcode.unhook(self.project, "Daybreak"))
        self.assertEqual(self.file.read_text(), original)
        self.assertFalse(xcode.unhook(self.project, "Daybreak"))

    def test_a_scheme_that_isnt_shared(self):
        with self.assertRaises(DevError) as caught:
            xcode.hook(self.project, "Other")
        self.assertIn("Shared", caught.exception.fix)


class AfterBuildTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.app = self.root / "Build" / "Daybreak.app"
        self.app.mkdir(parents=True)
        (self.app / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "dev.mobster.daybreak"}))

    def tearDown(self):
        self._tmp.cleanup()

    def entry(self, bundle, app_path=None):
        from mobile_agent.verify.checks import check_from_dict
        data = {"version": 1, "name": f"check of {bundle}", "expect": [{"text": "x"}], "app": {"bundle": bundle}}
        if app_path:
            data["app"]["path"] = str(app_path)
        from mobile_agent.testkit.discover import Entry
        return Entry(path=self.root / f"{bundle}.yaml", file=f"{bundle}.yaml", check=check_from_dict(data))

    def test_only_the_built_apps_checks_run_on_that_build_and_the_verdict_is_a_notification(self):
        from mobile_agent.testkit.api import Planned
        mine, other = self.entry("dev.mobster.daybreak", "/old/Daybreak.app"), self.entry("com.other")
        ran, said = [], []

        def plan(options, cwd=None):
            return Planned(root=self.root, entries=[mine, other], targets=[], options=options)

        def execute(planned, progress=None):
            ran.extend(planned.entries)
            return {"summary": {"passed": 1}, "exitCode": 0}, {}
        code, headline = xcode.run_checks(self.root, self.app, plan=plan, execute=execute,
                                          notifier=lambda title, text: said.append((title, text)))
        self.assertEqual(code, 0)
        self.assertEqual([entry.check.app_path for entry in ran], [str(self.app)])
        self.assertEqual(said[0][0], "Mobster checks: Daybreak")
        self.assertTrue((self.root / ".mobster" / "xcode" / "last.json").is_file())

    def test_a_device_build_is_skipped_and_the_background_run_replaces_the_older_one(self):
        self.assertEqual(xcode.built_app({"BUILT_PRODUCTS_DIR": "/b", "FULL_PRODUCT_NAME": "A.app",
                                          "PLATFORM_NAME": "iphoneos"}), (Path("/b/A.app"), "iphoneos"))
        with self.assertRaises(DevError):
            xcode.built_app({})
        killed, started = [], []

        class Process:
            pid = 99
        (self.root / ".mobster" / "xcode").mkdir(parents=True)
        (self.root / ".mobster" / "xcode" / "run.pid").write_text("42\n")
        pid = xcode.spawn_background(self.root, ["mobster"], {"SDKROOT": "/x", "ARCHS": "arm64", "PATH": "/bin",
                                                              "ANTHROPIC_API_KEY": "k", "SRCROOT": "/p"},
                                     popen=lambda argv, **k: started.append(k) or Process(),
                                     kill=lambda pid, sig: killed.append(pid))
        self.assertEqual((pid, killed), (99, [-42]))
        self.assertTrue(started[0]["start_new_session"])
        self.assertEqual(started[0]["env"], {"PATH": "/bin", "ANTHROPIC_API_KEY": "k", "SRCROOT": "/p"})


class AfterBuildGuardTests(unittest.TestCase):
    def run_after(self, platform):
        import contextlib
        import io
        from unittest import mock

        from mobile_agent.devkit import xcode_cli
        environ = {"BUILT_PRODUCTS_DIR": "/b", "FULL_PRODUCT_NAME": "A.app", "SRCROOT": "/p"}
        if platform is not None:
            environ["PLATFORM_NAME"] = platform
        args = SimpleNamespace(background=True, sim=None, tag=None)
        out = io.StringIO()
        with mock.patch.object(xcode, "spawn_background", return_value=7) as spawn, \
                mock.patch.object(xcode_cli, "self_argv", return_value=(["mobster"], {})), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = xcode_cli._after(args, Path("/p"), environ)
        return code, out.getvalue(), spawn

    def test_only_a_simulator_build_runs_and_an_unknown_platform_is_skipped(self):
        code, text, spawn = self.run_after("iphonesimulator")
        self.assertEqual(code, 0)
        spawn.assert_called_once()
        for platform in (None, "", "iphoneos"):
            code, text, spawn = self.run_after(platform)
            self.assertEqual(code, 0)
            self.assertIn("Skipped: Xcode built for", text)
            spawn.assert_not_called()


class XcodePlanTests(unittest.TestCase):
    def test_only_after_build_needs_the_plan(self):
        for verb, needs in (("hook", False), ("unhook", False), ("after-build", True)):
            self.assertEqual(plan_gate.cli_needs_plan("xcode", SimpleNamespace(xcode_command=verb)), needs)


if __name__ == "__main__":
    unittest.main()
