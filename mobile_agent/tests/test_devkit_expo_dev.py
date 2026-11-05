import contextlib
import io
import json
import signal
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mobile_agent import api_routes, plan_gate
from mobile_agent.api_errors import APIError
from mobile_agent.cli_parser import build_parser
from mobile_agent.devkit import DevError, expo, expo_cli, expo_dev, expo_routes
from mobile_agent.tests.test_devkit import Project, Wda, write
from mobile_agent.tests.test_verify_fixtures import app as tree_xml
from mobile_agent.tests.test_verify_fixtures import node


class Response:
    def __init__(self, body=b"packager-status:running", root=None):
        self.body, self.headers = body, ({"X-React-Native-Project-Root": root} if root else {})

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n=-1):
        return self.body[:n] if n > 0 else self.body


def metro(root=None, bundle=None):
    def opener(url, timeout=None):
        if url.endswith("/status"):
            return Response(root=root)
        if bundle == "slow":
            raise TimeoutError()
        if bundle:
            raise urllib.error.HTTPError(url, 500, "error", {}, io.BytesIO(json.dumps(bundle).encode()))
        return Response(b"var x;")
    return opener


UNRESOLVED = {"type": "UnableToResolveError", "message": "Unable to resolve module ./Missing from /app/App.tsx",
              "errors": [{"description": "x", "filename": "/app/App.tsx", "lineNumber": 3}]}


class ProcessOwnership(unittest.TestCase):
    def test_only_a_metro_group_in_the_same_project_is_owned(self):
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(expo.os, "getpgid", return_value=4242), \
                mock.patch("mobile_agent.supervised.command_of", return_value="npm exec expo start --port 8081"), \
                mock.patch.object(expo.subprocess, "run") as probe:
            probe.return_value.stdout = "p4242\nn" + str(Path(root).resolve()) + "\n"
            self.assertTrue(expo.owns_metro(root, 4242))
            probe.return_value.stdout = "p4242\nn/another/project\n"
            self.assertFalse(expo.owns_metro(root, 4242))
        with mock.patch.object(expo.os, "getpgid", return_value=4242), \
                mock.patch("mobile_agent.supervised.command_of", return_value="python unrelated.py"), \
                mock.patch.object(expo.subprocess, "run") as probe:
            self.assertFalse(expo.owns_metro("/app", 4242))
            probe.assert_not_called()


class ProjectResolution(Project):
    def test_explicit_project_and_current_directory_resolve_to_the_app(self):
        self.expo_app()
        self.assertEqual(expo.resolve_project(str(self.root)).root, self.root.resolve())
        with mock.patch.object(Path, "cwd", return_value=self.root):
            self.assertEqual(expo.resolve_project().root, self.root.resolve())

    def test_relative_and_missing_projects_are_refused_before_discovery(self):
        for path, message in (("relative/project", "Pass the absolute path"),
                              (str(self.root / "missing"), "isn't a folder")):
            with self.subTest(path=path), mock.patch.object(expo, "find_app") as discover:
                with self.assertRaises(DevError) as caught:
                    expo.resolve_project(path)
                self.assertIn(message, caught.exception.message)
                discover.assert_not_called()


class DetectByConfig(Project):
    def test_an_expo_config_makes_an_expo_project(self):
        write(self.root / "package.json", {"name": "x"})
        write(self.root / "app.json", {"name": "plain"})
        self.assertIsNone(expo.detect(self.root))
        write(self.root / "app.json", {"expo": {"name": "Habits", "slug": "habits"}})
        app = expo.detect(self.root)
        self.assertEqual((app.expo, app.name), (True, "Habits"))
        (self.root / "app.json").unlink()
        write(self.root / "app.config.js", "module.exports = {}")
        self.assertTrue(expo.detect(self.root, runner=lambda *a, **k: (1, "")).expo)


class OneMetroPerProject(Project):
    def test_a_stale_pid_never_signals_an_unowned_process(self):
        self.expo_app()
        folder = self.root / expo.STATE_DIR
        folder.mkdir(parents=True)
        (folder / "metro.pid").write_text("4242")
        kill = mock.Mock()
        with mock.patch.object(expo, "owns_metro", return_value=False):
            self.assertIsNone(expo_dev.stop(self.root, kill=kill))
        kill.assert_not_called()

    def test_another_projects_metro_is_not_ours(self):
        app = self.expo_app()
        self.assertTrue(expo.metro_running(8081, opener=metro(root=str(self.root)), root=app.root))
        self.assertFalse(expo.metro_running(8081, opener=metro(root="/somewhere/else"), root=app.root))
        self.assertFalse(expo.metro_running(8081, opener=metro(), root=app.root))

    def test_the_port_moves_on_when_another_apps_metro_has_8081(self):
        app = self.expo_app()
        seen = {8081: "/somewhere/else"}

        def opener(url, timeout=None):
            port = int(url.split(":")[2].split("/")[0])
            if port not in seen:
                raise OSError()
            return Response(root=seen[port])
        port = expo_dev.pick_port(app, opener=opener, free=lambda p: p == 8082)
        self.assertEqual(port, 8082)

    def test_a_named_port_that_is_taken_is_refused_not_moved(self):
        app = self.expo_app()
        with self.assertRaises(DevError):
            expo_dev.pick_port(app, 9000, opener=lambda *a, **k: (_ for _ in ()).throw(OSError()), free=lambda p: False)

    def test_ensure_reuses_our_metro_and_says_it_did_not_start_it(self):
        app = self.expo_app()
        start = mock.Mock()
        server, started = expo_dev.ensure(app, start=start, opener=metro(root=str(self.root)), free=lambda p: False)
        self.assertEqual((server["state"], started), ("running", False))
        start.assert_not_called()

    def test_ensure_starts_metro_when_nothing_answers(self):
        app = self.expo_app()
        down = lambda url, timeout=None: (_ for _ in ()).throw(OSError())  # noqa: E731
        start = mock.Mock(return_value=77)
        server, started = expo_dev.ensure(app, start=start, opener=down, free=lambda p: True)
        self.assertEqual((server["pid"], server["port"], started), (77, 8081, True))
        self.assertTrue(start.call_args.kwargs["stop_on_fail"])


class Lifecycle(Project):
    def started(self, app, pid=4242):
        folder = self.root / expo.STATE_DIR
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "metro.pid").write_text(f"{pid}\n")
        (folder / "metro.port").write_text("8081\n")

    def test_stop_ends_the_process_group_waits_and_forgets_it(self):
        app = self.expo_app()
        self.started(app)
        alive = {"yes": True}
        calls = []

        def kill(pid, sig):
            calls.append((pid, sig))
            if sig == signal.SIGTERM:
                alive["yes"] = False
            if sig == 0 and not alive["yes"]:
                raise ProcessLookupError()
        self.assertEqual(expo_dev.stop(self.root, kill=kill, sleep=lambda s: None), 4242)
        self.assertIn((-4242, signal.SIGTERM), calls)
        self.assertNotIn((-4242, signal.SIGKILL), calls)
        self.assertFalse((self.root / expo.STATE_DIR / "metro.pid").exists())
        self.assertFalse((self.root / expo.STATE_DIR / "metro.port").exists())

    def test_a_metro_that_ignores_sigterm_is_killed(self):
        app = self.expo_app()
        self.started(app)
        calls = []
        ticks = iter(range(100))
        alive = True
        def kill(pid, sig):
            nonlocal alive
            calls.append((pid, sig))
            if sig == signal.SIGKILL:
                alive = False
            if sig == 0 and not alive:
                raise ProcessLookupError()
        expo_dev.stop(self.root, kill=kill, grace=1, sleep=lambda s: None, clock=lambda: next(ticks))
        self.assertIn((-4242, signal.SIGKILL), calls)

    def test_cleanup_does_not_stop_a_replacement_metro(self):
        app = self.expo_app()
        self.started(app, pid=4242)
        kill = mock.Mock()
        self.assertIsNone(expo_dev.stop(self.root, kill=kill, expected_pid=9999))
        kill.assert_not_called()

    def test_invalid_pid_cannot_signal_a_process_group(self):
        app = self.expo_app()
        for pid in (0, -1, 1):
            self.started(app, pid=pid)
            kill = mock.Mock()
            self.assertIsNone(expo_dev.stop(self.root, kill=kill))
            kill.assert_not_called()

    def test_stop_with_nothing_started_is_quiet(self):
        self.expo_app()
        self.assertIsNone(expo_dev.stop(self.root, kill=mock.Mock()))

    def test_a_metro_that_never_answers_is_stopped_with_stop_stop_on_fail(self):
        app = self.expo_app()

        class Slow:
            pid = 31

            def poll(self):
                return None
        killed = []
        ticks = iter(range(1000))
        with self.assertRaises(DevError):
            expo.start_metro(app, popen=lambda argv, **k: Slow(), opener=lambda u, timeout=None: (_ for _ in ()).throw(
                OSError()), sleep=lambda s: None, clock=lambda: next(ticks), wait=3, stop_on_fail=True,
                kill=lambda pid, sig: killed.append(pid))
        self.assertIn(-31, killed)
        self.assertFalse((self.root / expo.STATE_DIR / "metro.pid").exists())

    def test_a_metro_that_exited_leaves_no_pid_file_behind(self):
        app = self.expo_app()

        class Dead:
            pid = 32

            def poll(self):
                return 1
        with self.assertRaises(DevError):
            expo.start_metro(app, popen=lambda argv, **k: Dead(), opener=lambda u, timeout=None: (_ for _ in ()).throw(
                OSError()), sleep=lambda s: None)
        self.assertFalse((self.root / expo.STATE_DIR / "metro.pid").exists())

    def test_status_names_stopped_starting_running_and_failed(self):
        app = self.expo_app()
        down = lambda url, timeout=None: (_ for _ in ()).throw(OSError())  # noqa: E731
        self.assertEqual(expo_dev.status(app, opener=down)["state"], "stopped")
        self.started(app)
        self.assertEqual(expo_dev.status(app, opener=down, kill=lambda p, s: None)["state"], "starting")
        self.assertEqual(expo_dev.status(app, opener=metro(root=str(app.root)), kill=lambda p, s: None)["state"],
                         "running")
        (self.root / expo.STATE_DIR / "metro.log").write_text("\x1b[31merror\x1b[0m: Cannot find module 'foo'\n")

        def gone(pid, sig):
            raise ProcessLookupError()
        dead = expo_dev.status(app, opener=down, kill=gone)
        self.assertEqual(dead["state"], "failed")
        self.assertIn("Cannot find module 'foo'", dead["error"]["message"])
        self.assertNotIn("\x1b", dead["error"]["message"])


class MetroErrors(Project):
    def test_a_bundle_error_has_metros_text_the_file_and_the_line(self):
        app = self.expo_app()
        error = expo_dev.bundle_error(app, 8081, opener=metro(bundle=UNRESOLVED))
        self.assertEqual((error["type"], error["file"], error["line"]), ("UnableToResolveError", "/app/App.tsx", 3))
        self.assertIn("Unable to resolve module ./Missing", error["message"])

    def test_a_bundle_that_builds_or_is_still_building_is_not_an_error(self):
        app = self.expo_app()
        self.assertIsNone(expo_dev.bundle_error(app, 8081, opener=metro()))
        self.assertIsNone(expo_dev.bundle_error(app, 8081, opener=metro(bundle="slow")))

    def test_a_long_error_is_cut_and_stays_one_line(self):
        app = self.expo_app()
        error = expo_dev.bundle_error(app, 8081, opener=metro(bundle={"message": "boom\n" + "x" * 5000}))
        self.assertLessEqual(len(error["message"]), 600)
        self.assertNotIn("\n", error["message"])

    def test_preview_fails_fast_with_metros_error_instead_of_waiting_out_the_timeout(self):
        app = self.expo_app()
        wda = Wda(tree_xml(node("StaticText", "Loading")))
        slept = []
        with mock.patch("mobile_agent.wda.system_dialogs.read", return_value=None):
            result = expo_cli.preview(app, {"name": "Sim", "kind": "simulator"}, wda, url="exp://h:1",
                                      expect=["Welcome back"], wait=600, sleep=slept.append,
                                      metro_error=lambda: {"message": "Syntax error in App.tsx"})
        self.assertEqual(result["error"], "Syntax error in App.tsx")
        self.assertFalse(result["expect"][0]["ok"])
        self.assertEqual(slept, [])

    def test_preview_fails_fast_on_a_dev_clients_red_screen(self):
        app = self.expo_app()
        wda = Wda(tree_xml(node("StaticText", "Unable to resolve module ./Missing")))
        with mock.patch("mobile_agent.wda.system_dialogs.read", return_value=None):
            result = expo_cli.preview(app, {"name": "Sim", "kind": "simulator"}, wda, url="exp://h:1",
                                      expect=["Welcome back"], wait=600, sleep=lambda s: None)
        self.assertIn("Unable to resolve module", result["error"])


class Watching(Project):
    def test_a_saved_source_file_is_a_change_and_builds_and_packages_are_not(self):
        self.expo_app()
        (self.root / "App.tsx").write_text("a")
        (self.root / "node_modules" / "pkg").mkdir(parents=True)
        (self.root / "node_modules" / "pkg" / "index.js").write_text("a")
        watcher = expo_dev.Watcher(self.root)
        self.assertFalse(watcher.changed())
        (self.root / "node_modules" / "pkg" / "index.js").write_text("b")
        (self.root / "notes.txt").write_text("b")
        self.assertFalse(watcher.changed())
        time.sleep(0.01)
        (self.root / "App.tsx").write_text("bb")
        self.assertTrue(watcher.changed())
        self.assertFalse(watcher.changed())
        (self.root / "New.tsx").write_text("c")
        self.assertTrue(watcher.changed())


class DevCommand(Project):
    def run_dev(self, extra=(), *, status=("running", False), bundle=(None,), client=True, **options):
        self.expo_app()
        args = build_parser().parse_args(["expo", "dev", "--device", "Sim", *extra])
        wda = Wda(tree_xml(node("StaticText", "Home")))
        record = {"name": "Sim", "kind": "simulator", "udid": "U"}
        bundles = iter(bundle)
        self.stopped = []
        patches = [mock.patch.object(expo_dev, "ensure",
                                     return_value=({"port": 8081, "pid": 4242, "state": "running"}, status[1])),
                   mock.patch.object(expo_dev, "bundle_error", lambda *a, **k: next(bundles, None)),
                   mock.patch.object(expo_dev, "stop", lambda root, **k: self.stopped.append(root)),
                   mock.patch.object(expo, "reload", lambda port: self.reloads.append(port) or True),
                   mock.patch("mobile_agent.wda.system_dialogs.read", return_value=None)]
        self.reloads = []
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(mock.patch("sys.stdout", out))
            stack.enter_context(mock.patch("sys.stderr", err))
            code = expo_cli._dev(args, self.root, lambda name: record, wda, sleep=lambda s: None, **options)
        return code, out.getvalue(), err.getvalue(), wda

    def test_it_opens_the_app_reloads_on_each_save_and_stops_the_metro_it_started(self):
        changes = iter([False, True, False, True])
        watcher = mock.Mock(changed=lambda: next(changes))
        code, out, err, wda = self.run_dev(status=("running", True), watcher=watcher, rounds=4)
        self.assertEqual(code, 0)
        self.assertEqual(self.reloads, [8081, 8081])
        self.assertEqual(out.count("Reloaded."), 2)
        self.assertEqual(wda.opened, ["exp+habits://expo-development-client/?url=http%3A%2F%2F127.0.0.1%3A8081"])
        self.assertEqual(self.stopped, [self.root.resolve()])

    def test_a_metro_that_was_already_running_is_left_running(self):
        code, *_ = self.run_dev(("--once",))
        self.assertEqual(code, 0)
        self.assertEqual(self.stopped, [])

    def test_ctrl_c_stops_the_metro_it_started(self):
        watcher = mock.Mock(changed=mock.Mock(side_effect=KeyboardInterrupt))
        code, *_ = self.run_dev(status=("running", True), watcher=watcher)
        self.assertEqual(code, 0)
        self.assertEqual(self.stopped, [self.root.resolve()])

    def test_metro_that_cannot_bundle_is_a_failed_state_with_the_error_and_nothing_is_left_running(self):
        error = {"message": "Unable to resolve module ./Missing", "file": "App.tsx", "line": 3}
        code, out, err, wda = self.run_dev(status=("running", True), bundle=(error,))
        self.assertEqual(code, 1)
        self.assertIn("Metro can't bundle the app: Unable to resolve module ./Missing (App.tsx:3)", err)
        self.assertEqual(wda.opened, [])
        self.assertEqual(self.stopped, [self.root.resolve()])

    def test_an_error_after_a_save_is_reported_and_the_next_save_recovers(self):
        error = {"message": "SyntaxError in App.tsx", "file": None, "line": None}
        changes = iter([True, True])
        watcher = mock.Mock(changed=lambda: next(changes))
        code, out, err, wda = self.run_dev(bundle=(None, None, error, None), watcher=watcher, rounds=2)
        self.assertEqual(code, 0)
        self.assertIn("SyntaxError in App.tsx", err)
        self.assertIn("the next save tries again", err)
        self.assertEqual(out.count("Reloaded."), 1)

    def test_checks_run_again_after_each_successful_save(self):
        runs = []
        watcher = mock.Mock(changed=lambda: True)
        code, *_ = self.run_dev(("--check", ".mobster/checks"), watcher=watcher, rounds=2,
                                run_checks=lambda name, paths: runs.append((name, paths)) or 0)
        self.assertEqual(code, 0)
        self.assertEqual(len(runs), 3)

    def test_a_missing_dev_client_is_built_and_installed_once(self):
        calls = []
        failing = {"first": True}
        real = expo_cli.preview

        def preview(app, record, wda, **kw):
            if failing.pop("first", False):
                raise expo_cli.ClientMissing("not installed")
            return real(app, record, wda, **kw)
        with mock.patch.object(expo_cli, "preview", preview), \
                mock.patch.object(expo, "build_sim", lambda app, **k: calls.append("build") or {"path": "/x.app"}), \
                mock.patch("mobile_agent.phone_io.install.install", lambda record, path, **k: calls.append("install")):
            code, *_ = self.run_dev(("--once",))
        self.assertEqual((code, calls), (0, ["build", "install"]))

    def test_no_install_reports_the_missing_client(self):
        def preview(app, record, wda, **kw):
            raise expo_cli.ClientMissing("not installed")
        with mock.patch.object(expo_cli, "preview", preview), self.assertRaises(expo_cli.ClientMissing):
            self.run_dev(("--once", "--no-install"))

    def test_checks_run_on_the_device_and_their_exit_code_is_the_commands(self):
        ran = []
        code, *_ = self.run_dev(("--check", ".mobster/checks"),
                                run_checks=lambda name, paths: ran.append((name, paths)) or 1)
        self.assertEqual((code, ran), (1, [("Sim", [".mobster/checks"])]))

    def test_dev_needs_the_plan(self):
        self.assertTrue(plan_gate.cli_needs_plan("expo", SimpleNamespace(expo_command="dev")))


class Request:
    def __init__(self, body=None, query=None, app_session=True):
        self.body, self.query, self.app_session = body, query or {}, app_session
        self.runtime = mock.Mock(local_api={"app_session": True})


class Api(Project):
    def test_status_reports_the_server_and_the_app(self):
        self.expo_app()
        with mock.patch.object(expo_dev, "status", return_value={"state": "failed", "error": {"message": "boom"}}):
            answer = expo_routes.status_route(Request(query={"project": [str(self.root)]}))
        self.assertEqual(answer.value["server"]["state"], "failed")
        self.assertEqual(answer.value["app"]["slug"], "habits")

    def test_a_folder_that_is_not_an_expo_app_is_a_400(self):
        with self.assertRaises(APIError) as caught:
            expo_routes.status_route(Request(query={"project": [str(self.root)]}))
        self.assertEqual(caught.exception.code, "bad_project")
        with self.assertRaises(APIError):
            expo_routes.status_route(Request(query={}))
        with self.assertRaises(APIError):
            expo_routes.status_route(Request(query={"project": ["relative/path"]}))

    def test_starting_and_stopping_acts_only_for_the_app_and_stops_what_it_started(self):
        self.expo_app()
        body = {"project": str(self.root)}
        for route in (expo_routes.dev_route, expo_routes.reload_route, expo_routes.stop_route):
            with self.subTest(route=route.__name__), self.assertRaises(APIError) as caught:
                route(Request(body, app_session=False))
            self.assertEqual((caught.exception.code, caught.exception.status), ("app_only", 403))
        stops = []
        with mock.patch.object(expo_dev, "ensure",
                               return_value=({"state": "running", "port": 8081, "pid": 4242}, True)), \
                mock.patch.object(expo_dev, "stop", lambda root, **k: stops.append(root) or 9):
            self.assertTrue(expo_routes.dev_route(Request(body)).value["started"])
            self.assertEqual(expo_routes.stop_route(Request(body)).value, {"stopped": 9})
            expo_routes.dev_route(Request(body))
            expo_routes._stop_all()
        self.assertEqual(len(stops), 2)

    def test_reload_refuses_when_no_metro_runs_and_never_reloads_a_stopped_one(self):
        self.expo_app()
        with mock.patch.object(expo_dev, "status", return_value={"state": "stopped", "port": 8081}), \
                mock.patch.object(expo, "reload") as reload, self.assertRaises(APIError):
            expo_routes.reload_route(Request({"project": str(self.root)}))
        reload.assert_not_called()

    def test_a_port_outside_the_range_is_refused(self):
        self.expo_app()
        with self.assertRaises(APIError):
            expo_routes.dev_route(Request({"project": str(self.root), "port": 80}))

    def test_the_routes_register_under_the_testkit_owner(self):
        api_routes.reset_for_tests()
        self.addCleanup(api_routes.reset_for_tests)
        expo_routes.register_routes()
        self.assertIsNotNone(api_routes.match("POST", "/api/tests/expo/dev"))
        self.assertIsNotNone(api_routes.match("GET", "/api/tests/expo"))


if __name__ == "__main__":
    unittest.main()
