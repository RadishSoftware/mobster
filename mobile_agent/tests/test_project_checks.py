"""A project's checks from its page (mobile_agent/projects/setup.py and runs.py): Set up checks writes `.mobster/` and
a starter check, and Run checks builds the app and runs every check on the simulators.

The starter check is read back by verify's own loader and planned by testkit's own plan, so the file is one
`mobster test` runs. The build and the suite are fakes that record what they were given; one build is a real process
tree, so Stop is shown to end it. No simulator, no xcodebuild, no phone, no network.
"""

import os
import signal
import stat
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mobile_agent.api_errors import APIError
from mobile_agent.projects import routes
from mobile_agent.projects import service as project_service
from mobile_agent.projects.service import ProjectService
from mobile_agent.testkit.prbuild import BuildError
from mobile_agent.testkit.register import PAGE_FLAG
from mobile_agent.tests.test_projects import Base
from mobile_agent.tests.timing import bound
from mobile_agent.verify.checks import load_check

# A build that starts a child and waits: Stop must end both.
TREE = r"""
import subprocess, sys, time
child_code = "import os, signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(60)"
child = subprocess.Popen([sys.executable, "-c", child_code, sys.argv[1]])
time.sleep(60)
"""  # noqa: E501


class Fakes:
    """testkit's build and execute, recording what each got. ``hold`` keeps the suite running until it is set."""

    def __init__(self):
        self.builds, self.suites = [], []
        self.hold = None
        self.build_error = None

    def build(self, root, pr_options, options, *, runner, progress, bundle):
        self.builds.append({"root": Path(root), "options": options, "bundle": bundle, "runner": runner})
        progress("Building Dailies for the simulator")
        if self.build_error is not None:
            raise self.build_error
        app = Path(root) / ".mobster" / "build" / "Build" / "Products" / "Debug-iphonesimulator" / "Dailies.app"
        return {"bundle": "com.example.firefox-ios", "path": str(app), "platform": "sim",
                "log": str(Path(root) / ".mobster" / "build" / "pr-build.log")}

    def execute(self, planned, *, progress, stop, suite_id, on_suite, cwd):
        self.suites.append({"planned": planned, "suite": suite_id, "cwd": cwd})
        progress("Dailies opens to its first screen on iPhone 17 Pro: launching")
        if self.hold is not None:
            while not (self.hold.is_set() or stop.is_set()):
                time.sleep(0.01)
        if stop.is_set():
            return {"suite": suite_id, "summary": {"passed": 0}, "exitCode": 130, "stopped": True}, {}
        return {"suite": suite_id, "summary": {"total": 1, "passed": 1}, "exitCode": 0}, {}


class SetupTests(Base):
    def test_set_up_checks_writes_the_gitignore_and_a_starter_check_mobster_test_reads(self):
        path = self.app_folder(mobster=False)
        project, _ = self.service.add({"path": str(path)})
        self.events.clear()
        detail, created = self.service.setup(project["id"], {})
        self.assertEqual(created, [".mobster/.gitignore", ".mobster/checks/app-opens.yaml"])
        self.assertEqual((detail["mobster"], detail["checks"], detail["checkFiles"]),
                         (True, 1, [".mobster/checks/app-opens.yaml"]))
        self.assertIn("test-results/", (path / ".mobster" / ".gitignore").read_text())
        check = load_check(path / ".mobster" / "checks" / "app-opens.yaml")
        self.assertEqual((check.name, check.bundle_id, check.app_path, check.steps, check.tags),
                         ("Dailies opens to its first screen", "com.example.firefox-ios", None, (), ("smoke",)))
        self.assertEqual([item.render() for item in check.expect], ["visible label=/.+/"])
        from mobile_agent.testkit import api
        planned = api.plan(api.Options(), cwd=path)         # the file is one `mobster test` runs, on a simulator
        self.assertEqual(([entry.name for entry in planned.entries], [t.kind for t in planned.targets]),
                         (["Dailies opens to its first screen"], ["default"]))
        self.assertEqual(self.events, [("projects", {"event": "project_changed", "projectId": project["id"]})])

    def test_a_project_with_checks_or_without_an_app_is_refused_and_nothing_is_written(self):
        path = self.app_folder()
        mine = path / ".mobster" / "checks" / "login.yaml"
        mine.write_text("version: 1\nname: Log in\napp: {bundle: com.example.firefox-ios}\nexpect: [{text: Hi}]\n")
        project, _ = self.service.add({"path": str(path)})
        with self.assertRaises(APIError) as caught:
            self.service.setup(project["id"], {})
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "has_checks"))
        self.assertEqual(sorted(p.name for p in (path / ".mobster").rglob("*")), ["checks", "login.yaml"])

        empty = self.home / "Developer" / "Notes"
        empty.mkdir()
        project, _ = self.service.add({"path": str(empty)})
        with self.assertRaises(APIError) as caught:
            self.service.setup(project["id"], {})
        self.assertEqual(caught.exception.code, "no_app")
        self.assertIn("mobster test add smoke", str(caught.exception))
        self.assertFalse((empty / ".mobster").exists())

    def test_an_app_whose_bundle_id_is_a_build_setting_is_refused(self):
        path = self.home / "Developer" / "Settings"
        (path / "Settings.xcodeproj").mkdir(parents=True)
        (path / "Settings.xcodeproj" / "project.pbxproj").write_text(
            'A = { buildSettings = { PRODUCT_BUNDLE_IDENTIFIER = "$(BASE).app"; }; };')
        project, _ = self.service.add({"path": str(path)})
        with self.assertRaises(APIError) as caught:
            self.service.setup(project["id"], {})
        self.assertEqual(caught.exception.code, "no_bundle")
        self.assertFalse((path / ".mobster").exists())

    def test_the_persons_own_gitignore_stays_as_it_is(self):
        path = self.app_folder(mobster=False)
        (path / ".mobster").mkdir()
        (path / ".mobster" / ".gitignore").write_text("runs/\n# mine\n")
        project, _ = self.service.add({"path": str(path)})
        _, created = self.service.setup(project["id"], None)
        self.assertEqual(created, [".mobster/checks/app-opens.yaml"])
        self.assertEqual((path / ".mobster" / ".gitignore").read_text(), "runs/\n# mine\n")

    def test_a_folder_that_moved_or_cant_be_written_says_so(self):
        path = self.app_folder(mobster=False)
        project, _ = self.service.add({"path": str(path)})
        os.chmod(path, stat.S_IRUSR | stat.S_IXUSR)
        try:
            with self.assertRaises(APIError) as caught:
                self.service.setup(project["id"], {})
        finally:
            os.chmod(path, stat.S_IRWXU)
        self.assertEqual((caught.exception.status, caught.exception.code), (403, "unwritable"))
        path.rename(path.with_name("Moved"))
        with self.assertRaises(APIError) as caught:
            self.service.setup(project["id"], {})
        self.assertEqual((caught.exception.status, caught.exception.code), (404, "missing"))
        for body in ({"app": 3}, {"force": True}, []):
            with self.assertRaises(ValueError):
                self.service.setup(project["id"], body)


class RunTests(Base):
    def setUp(self):
        super().setUp()
        self.fakes = Fakes()
        self.service.close()
        self.service = ProjectService(self.runtime, home=self.home, data_dir=self.data, popen=self.git,
                                      last_runs=lambda: self.last_runs,
                                      run_parts={"build": self.fakes.build, "execute": self.fakes.execute})
        self.addCleanup(self.service.close)
        self.path = self.app_folder(mobster=False)
        self.project, _ = self.service.add({"path": str(self.path)})
        self.service.setup(self.project["id"], {})
        self.events.clear()

    def finished(self, run_id, timeout=30):
        deadline = time.monotonic() + bound(timeout)
        while time.monotonic() < deadline:
            run = self.service.run_status(run_id)
            if run["state"] not in ("building", "running"):
                return run
            time.sleep(0.02)
        self.fail("the run didn't end")

    def test_run_checks_builds_the_app_then_runs_every_check_with_that_build_on_the_simulators(self):
        run = self.finished(self.service.run_checks(self.project["id"], {})["id"])
        self.assertEqual((run["state"], run["summary"], run["exitCode"], run["code"]),
                         ("done", {"total": 1, "passed": 1}, 0, None))
        self.assertEqual((run["targets"], run["log"]), (["each check's simulator"], ".mobster/build/pr-build.log"))
        build = self.fakes.builds[0]
        self.assertEqual((build["root"], build["bundle"]), (self.path, "com.example.firefox-ios"))
        self.assertEqual((build["options"].devices, build["options"].sims), ((), ()))   # never a phone
        planned = self.fakes.suites[0]["planned"]
        self.assertTrue(planned.entries[0].check.app_path.endswith("Debug-iphonesimulator/Dailies.app"))
        self.assertEqual(self.fakes.suites[0]["suite"], run["suite"])
        self.assertEqual(self.service.get(self.project["id"])["run"]["id"], run["id"])
        kinds = [event["event"] for topic, event in self.events if topic == "projects"]
        self.assertEqual(kinds[-1], "run_ended")
        self.assertIn("run_changed", kinds)

    def test_a_build_that_fails_ends_the_run_with_the_build_error_and_runs_nothing(self):
        self.fakes.build_error = BuildError("The build failed: Dailies/App.swift:3: error: cannot find 'x'.",
                                            "Fix the build, or pass --no-build.")
        run = self.finished(self.service.run_checks(self.project["id"], {})["id"])
        self.assertEqual((run["state"], run["code"]), ("failed", "build_failed"))
        self.assertIn("cannot find 'x'", run["error"])
        self.assertEqual(run["fix"], "Fix the build, then run the checks again.")    # no CLI flag on the page
        self.assertEqual(run["log"], ".mobster/build/pr-build.log")
        self.assertEqual(self.fakes.suites, [])

    def test_one_run_per_project_and_a_project_without_checks_is_refused(self):
        self.fakes.hold = threading.Event()
        first = self.service.run_checks(self.project["id"], {})
        with self.assertRaises(APIError) as caught:
            self.service.run_checks(self.project["id"], {})
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "running"))
        self.fakes.hold.set()
        self.assertEqual(self.finished(first["id"])["state"], "done")
        bare = self.app_folder("Bare", mobster=False)
        other, _ = self.service.add({"path": str(bare)})
        with self.assertRaises(APIError) as caught:
            self.service.run_checks(other["id"], {})
        self.assertEqual(caught.exception.code, "no_checks")
        with self.assertRaises(ValueError):
            self.service.run_checks(self.project["id"], {"device": "Michelle's iPhone"})

    def test_with_the_tests_page_on_a_run_takes_simulators_and_tags_or_the_projects_links(self):
        with mock.patch.dict(os.environ, {PAGE_FLAG: "1"}):
            run = self.finished(self.service.run_checks(self.project["id"], {"sims": ["iPhone SE"], "tags": ["smoke"]})
                                ["id"])
            self.assertEqual((run["state"], run["choice"]), ("done", {"sims": ["iPhone SE"], "device": None,
                                                                      "tags": ["smoke"], "links": False}))
            options = self.fakes.builds[0]["options"]
            self.assertEqual((options.sims, options.devices, options.tags), (("iPhone SE",), (), ("smoke",)))
            self.assertEqual(run["targets"], ["iPhone SE"])
            (self.path / ".mobster" / "links.yaml").write_text("links:\n  cart: dailies://cart\n  call: tel:5550100\n")
            links = self.finished(self.service.run_checks(self.project["id"], {"links": True})["id"])
            self.assertEqual(links["state"], "done")
            planned = self.fakes.suites[-1]["planned"]
            self.assertEqual([entry.file for entry in planned.entries], [".mobster/links.yaml#cart"])   # tel: skipped
            self.assertTrue(planned.entries[0].check.app_path.endswith("Dailies.app"))
            self.assertEqual(self.fakes.builds[-1]["bundle"], "com.example.firefox-ios")

    def test_an_iphone_runs_only_with_a_plan_and_no_task_on_it(self):
        phone = {"id": "usb-1", "kind": "usb", "name": "Test iPhone"}
        self.runtime.plan_gate = SimpleNamespace(require=lambda: None)
        self.runtime.device_busy = lambda device_id: False
        with mock.patch.dict(os.environ, {PAGE_FLAG: "1"}), \
                mock.patch("mobile_agent.device_targets.named_device", return_value=phone):
            run = self.finished(self.service.run_checks(self.project["id"], {"device": "Test iPhone"})["id"])
            self.assertEqual(self.fakes.builds[-1]["options"].devices, ("Test iPhone",))
            self.assertEqual(run["choice"]["device"], "Test iPhone")

            def refused(code, body=None):
                with self.assertRaises(APIError) as caught:
                    self.service.run_checks(self.project["id"], body or {"device": "Test iPhone"})
                self.assertEqual(caught.exception.code, code)
            self.runtime.device_busy = lambda device_id: device_id == "usb-1"
            refused("device_busy")
            del self.runtime.device_busy
            refused("device_busy")                           # a runtime that can't say: refused
            self.runtime.device_busy = lambda device_id: False

            def no_plan():
                raise APIError("Your trial ended.", 403, "plan_required")
            self.runtime.plan_gate = SimpleNamespace(require=no_plan)
            refused("plan_required")
            self.runtime.plan_gate = SimpleNamespace(require=lambda: None)
            with mock.patch("mobile_agent.device_targets.named_device", return_value=None):
                refused("device_not_found")
            with mock.patch("mobile_agent.device_targets.named_device",
                            return_value={"id": "sim-1", "kind": "simulator", "name": "iPhone 17 Pro"}):
                refused("device_not_found", {"device": "iPhone 17 Pro"})
            for body in ({"device": "Test iPhone", "sims": ["iPhone SE"]}, {"links": True, "tags": ["smoke"]},
                         {"sims": "iPhone SE"}, {"extra": 1}):
                with self.subTest(body=body), self.assertRaises(ValueError):
                    self.service.run_checks(self.project["id"], body)
        self.assertEqual(len(self.fakes.builds), 1)
        with self.assertRaises(ValueError):                  # the page is off: no options at all
            self.service.run_checks(self.project["id"], {"sims": ["iPhone SE"]})

    def test_stop_ends_a_suite_between_checks(self):
        self.fakes.hold = threading.Event()
        run = self.service.run_checks(self.project["id"], {})
        deadline = time.monotonic() + bound(15)
        while not self.fakes.suites and time.monotonic() < deadline:
            time.sleep(0.01)
        stopped = self.service.stop_run(run["id"])
        self.assertEqual((stopped["state"], stopped["line"]), ("stopped", "Stopped"))
        self.assertFalse(self.service.stop_run(run["id"])["state"] in ("building", "running"))

    def test_the_build_runner_gives_back_the_exit_code_and_output_and_keeps_the_log(self):
        from mobile_agent.projects.runs import CheckRun
        run = CheckRun("0123456789ab", self.project["id"], self.path)
        log = self.path / ".mobster" / "build" / "pr-build.log"
        script = "import sys; print('** BUILD SUCCEEDED **'); sys.exit(int(sys.argv[1]))"
        self.assertEqual(run._runner([sys.executable, "-c", script, "0"], self.path, log),
                         (0, "** BUILD SUCCEEDED **\n"))
        self.assertEqual(log.read_text(), "** BUILD SUCCEEDED **\n")
        self.assertEqual(run._runner([sys.executable, "-c", script, "65"], self.path)[0], 65)

    def test_stop_during_the_build_ends_its_whole_process_tree(self):
        marker = self.root / "child.pid"

        def build(root, pr_options, options, *, runner, progress, bundle):
            log = Path(root) / ".mobster" / "build" / "b.log"
            code, _ = runner([sys.executable, "-c", TREE, str(marker)], root, log)
            raise BuildError(f"The build failed: xcodebuild exited {code}.", "")
        self.service._run_parts["build"] = build
        run = self.service.run_checks(self.project["id"], {})
        deadline = time.monotonic() + bound(30)
        while not (marker.exists() and marker.read_text()) and time.monotonic() < deadline:
            time.sleep(0.02)
        child = int(marker.read_text())
        try:
            stopped = self.service.stop_run(run["id"])
            self.assertEqual(stopped["state"], "stopped")
            deadline = time.monotonic() + bound(3)
            while _alive(child) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(_alive(child), "the build's child is still running")
        finally:
            if _alive(child):
                os.kill(child, signal.SIGKILL)


    def test_closing_while_a_run_starts_waits_for_its_worker(self):
        from unittest.mock import patch

        from mobile_agent.projects.runs import CheckRun
        entered, release = threading.Event(), threading.Event()
        original = CheckRun.start
        errors = []

        def starting(run):
            entered.set()
            release.wait(5)
            original(run)

        def call(action):
            try:
                action()
            except Exception as error:
                errors.append(error)

        with patch.object(CheckRun, "start", starting):
            starter = threading.Thread(target=call, args=(lambda: self.service.run_checks(self.project["id"], {}),))
            closer = threading.Thread(target=call, args=(self.service.close,))
            starter.start()
            try:
                self.assertTrue(entered.wait(5))
                closer.start()
            finally:
                release.set()
                starter.join(5)
                if closer.ident is not None:
                    closer.join(5)
        self.assertFalse(starter.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(any(run.going for run in self.service._runs.values()))

    def test_closing_mobster_stops_a_run(self):
        self.fakes.hold = threading.Event()
        run = self.service.run_checks(self.project["id"], {})
        self.service.close()
        self.assertEqual(self.service.run_status(run["id"])["state"], "stopped")
        with self.assertRaises(APIError) as caught:
            self.service.run_checks(self.project["id"], {})
        self.assertEqual(caught.exception.status, 503)


class CheckRouteTests(Base):
    def request(self, method, path, body=None, params=None, app_session=True):
        from mobile_agent import api_routes
        return api_routes.Request(runtime=self.runtime, method=method, path=path, params=params or {}, query={},
                                  headers={}, body=body, content_type="application/json", origin="app",
                                  app_session=app_session)

    def test_set_up_and_run_need_the_mac_apps_session_and_reading_a_run_doesnt(self):
        project_service._services[id(self.runtime)] = self.service
        self.addCleanup(project_service._services.pop, id(self.runtime), None)
        path = self.app_folder(mobster=False)
        project, _ = self.service.add({"path": str(path)})
        self.runtime.local_api = {"app_session": "s"}
        params = {"id": project["id"]}
        for call, route_path in ((routes.set_up, "setup"), (routes.run_checks, "runs")):
            with self.assertRaises(APIError) as caught:
                call(self.request("POST", f"/api/projects/{project['id']}/{route_path}", {}, params, False))
            self.assertEqual(caught.exception.code, "app_only")
        self.assertFalse((path / ".mobster").exists())
        made = routes.set_up(self.request("POST", f"/api/projects/{project['id']}/setup", {}, params))
        self.assertEqual((made.status, made.value["created"][-1]), (201, ".mobster/checks/app-opens.yaml"))
        with self.assertRaises(APIError) as caught:
            routes.run_status(self.request("GET", "/api/projects/runs/0123456789ab", params={"id": "0123456789ab"},
                                           app_session=False))
        self.assertEqual(caught.exception.code, "not_found")

    def test_the_routes_are_registered(self):
        from mobile_agent import api_routes, harness_api
        from mobile_agent import journal as journal_module
        from mobile_agent.projects import register
        with harness_api.owned_by("projects"):
            journal_module.SCHEMAS.pop("projects", None)
            register.register(harness_api)
        for method, path, handler in (("POST", "/api/projects/0123456789ab/setup", routes.set_up),
                                      ("POST", "/api/projects/0123456789ab/runs", routes.run_checks),
                                      ("GET", "/api/projects/runs/0123456789ab", routes.run_status),
                                      ("DELETE", "/api/projects/runs/0123456789ab", routes.stop_run)):
            found = api_routes.match(method, path)
            self.assertIsNotNone(found, path)
            self.assertIs(found[0].handler, handler, path)



class FrameTests(Base):
    """ICP-3: a failure shows the screen it failed on, read from the project's own .mobster/runs and nothing else."""

    def test_a_runs_frame_is_served_and_nothing_outside_its_frames(self):
        path = self.app_folder()
        project, _ = self.service.add({"path": str(path)})
        frames = path / ".mobster" / "runs" / "20261016-100000-aaaa" / "frames"
        frames.mkdir(parents=True)
        (frames / "02-verdict.jpg").write_bytes(b"\xff\xd8\xff\xe0frame")
        (frames / "notes.png").write_bytes(b"not a picture")
        (path / ".mobster" / "secret.png").write_bytes(b"\x89PNG\r\n\x1a\nsecret")
        self.assertEqual(self.service.frame(project["id"], "20261016-100000-aaaa", "frames/02-verdict.jpg"),
                         (b"\xff\xd8\xff\xe0frame", "image/jpeg"))
        for run, name, code in (("20261016-100000-aaaa", "frames/../../secret.png", "bad_frame"),
                                ("../runs", "frames/02-verdict.jpg", "bad_frame"),
                                ("20261016-100000-aaaa", "frames/missing.jpg", "no_frame"),
                                ("20261016-100000-aaaa", "frames/notes.png", "no_frame"),
                                (None, None, "bad_frame")):
            with self.assertRaises(APIError) as caught:
                self.service.frame(project["id"], run, name)
            self.assertEqual(caught.exception.code, code, (run, name))

def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a zombie child of this process counts as ended
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    return bool(out.strip()) and not out.strip().startswith("Z")


if __name__ == "__main__":
    unittest.main()
