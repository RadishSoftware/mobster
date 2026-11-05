"""Projects (mobile_agent/projects, track ``projects``): the folders the Mac app works in, read from disk each time,
added by path or by cloning a URL, forgotten without touching the folder.

A temp folder stands in for the home folder, a real journal for the records, and a small Python program stands in for
`git clone` (it writes git's progress lines, makes the folder, fails or hangs as told), so the real stream reading and
process-group stop run. Real `git init` repositories are read for the branch. No network.
"""

import base64
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from mobile_agent import api_routes, harness_api
from mobile_agent import journal as journal_module
from mobile_agent.api_errors import APIError
from mobile_agent.projects import clone as cloning
from mobile_agent.projects import folder, register, routes
from mobile_agent.projects import service as project_service
from mobile_agent.projects import store as project_store
from mobile_agent.projects.service import ProjectService
from mobile_agent.tests.seam_support import isolate
from mobile_agent.tests.test_workflows import FakeRuntime
from mobile_agent.tests.timing import bound

PBXPROJ = """// !$*UTF8*$!
{ objects = {
  A = { buildSettings = { PRODUCT_BUNDLE_IDENTIFIER = com.example.firefox-ios; }; };
  B = { buildSettings = { PRODUCT_BUNDLE_IDENTIFIER = com.example.firefox-ios.widget; }; };
  C = { buildSettings = { PRODUCT_BUNDLE_IDENTIFIER = com.example.DailiesTests; }; };
  D = { buildSettings = { PRODUCT_BUNDLE_IDENTIFIER = "$(PRODUCT_NAME:rfc1034identifier)"; }; };
}; }
"""

PNG = b"\x89PNG\r\n\x1a\n"

# Stands in for git: argv is MODE TARGET. It writes git's progress the way git does (\r while a phase goes on).
FAKE_GIT = r"""
import os, sys, time
mode, target = sys.argv[1], sys.argv[2]
err = sys.stderr
if mode in ("ok", "hang", "denied", "slow"):
    os.makedirs(os.path.join(target, ".git"))
if mode in ("ok", "slow"):
    err.write("Cloning into '%s'...\n" % os.path.basename(target)); err.flush()
    for share in (0, 45, 100):
        err.write("Receiving objects: %3d%% (%d/100)\r" % (share, share)); err.flush()
        time.sleep(0.3 if mode == "slow" else 0.01)
    err.write("Resolving deltas: 100% (5/5), done.\n"); err.flush()
    open(os.path.join(target, "README.md"), "w").write("hello\n")
    sys.exit(0)
if mode == "denied":
    err.write("remote: Repository not found.\n"
              "fatal: repository 'https://github.com/mozilla-mobile/secret.git/' not found\n")
    sys.exit(128)
if mode == "offline":
    err.write("fatal: unable to access 'https://github.com/mozilla-mobile/app.git/': "
              "Could not resolve host: github.com\n")
    sys.exit(128)
if mode == "hostkey":
    err.write("Host key verification failed.\nfatal: Could not read from remote repository.\n")
    sys.exit(128)
if mode == "hang":
    err.write("Receiving objects:  12% (12/100)\r"); err.flush()
    time.sleep(60)
"""


class FakeGit:
    """subprocess.Popen for the clone: runs FAKE_GIT in ``mode`` on the clone's target, with the clone's options."""

    def __init__(self, mode="ok"):
        self.mode, self.calls = mode, []

    def __call__(self, argv, **options):
        self.calls.append((argv, options))
        if self.mode == "missing":
            raise FileNotFoundError("git")
        return subprocess.Popen([sys.executable, "-c", FAKE_GIT, self.mode, argv[-1]], **options)


def git(*argv, cwd):
    subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull})


class Base(unittest.TestCase):
    def setUp(self):
        isolate(self)
        self.dir = tempfile.TemporaryDirectory(prefix="mobster-projects-test-")
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name).resolve()
        self.home = self.root / "home"
        (self.home / "Developer").mkdir(parents=True)
        (self.home / "Documents").mkdir()
        self.data = self.home / "Library" / "Application Support" / "app.mobster.desktop"
        self.data.mkdir(parents=True)
        with harness_api.owned_by("projects"):
            journal_module.register_schema(project_store.SCHEMA, project_store.VERSION, project_store.migrate)
        self.journal = journal_module.Journal(self.root / "runs.sqlite3")
        self.addCleanup(self.journal.close)
        self.runtime = FakeRuntime(self.journal)
        self.events = []
        self.runtime.publish = lambda topic, event: self.events.append((topic, event))
        self.last_runs = {}
        self.git = FakeGit()
        self.service = ProjectService(self.runtime, home=self.home, data_dir=self.data, popen=self.git,
                                      last_runs=lambda: self.last_runs)
        self.addCleanup(self.service.close)

    def app_folder(self, name="Dailies", mobster=True):
        path = self.home / "Developer" / name
        (path / f"{name}.xcodeproj").mkdir(parents=True)
        (path / f"{name}.xcodeproj" / "project.pbxproj").write_text(PBXPROJ)
        if mobster:
            (path / ".mobster" / "checks").mkdir(parents=True)
        return path

    def refused(self, path, code):
        with self.assertRaises(APIError) as caught:
            self.service.add({"path": str(path)})
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        return str(caught.exception)

    def ended(self, clone_id, timeout=30):
        deadline = time.monotonic() + bound(timeout)    # a wait, not a speed claim: generous on a busy Mac
        while time.monotonic() < deadline:
            clone = self.service.clone_status(clone_id)
            if clone["state"] != "cloning":
                return clone
            time.sleep(0.02)
        self.fail("the clone didn't end")


class AddTests(Base):
    def test_a_folder_with_an_xcode_app_becomes_a_project_read_from_disk(self):
        path = self.app_folder()
        (path / ".mobster" / "checks" / "paywall.yaml").write_text("version: 1\nsteps: [Open the paywall]\n")
        (path / ".mobster" / "checks" / "nested").mkdir()
        (path / ".mobster" / "checks" / "nested" / "login.yml").write_text("version: 1\n")
        (path / ".mobster" / "checks" / "matrix.yaml").write_text("simulators: []\n")   # not a check
        (path / ".mobster" / "matrix.yaml").write_text('simulators: ["iPhone 17 Pro@iOS 26.4", "iPhone SE"]\n')
        (path / ".mobster" / "workflows").mkdir()
        (path / ".mobster" / "workflows" / "signup.yaml").write_text("name: Sign up\n")
        project, created = self.service.add({"path": str(path)})
        self.assertTrue(created)
        self.assertEqual((project["name"], project["path"], project["shownPath"]),
                         ("Dailies", str(path), "~/Developer/Dailies"))
        self.assertEqual((project["state"], project["problem"], project["mobster"]), ("ready", None, True))
        self.assertEqual(project["apps"], [{"kind": "xcode", "name": "Dailies", "path": "Dailies.xcodeproj",
                                            "bundle": "com.example.firefox-ios"}])
        self.assertEqual((project["checks"], project["workflows"]), (2, 1))
        self.assertEqual(project["simulators"], ["iPhone 17 Pro@iOS 26.4", "iPhone SE"])
        self.assertEqual(project["source"], {"kind": "folder"})
        self.assertIsNone(project["git"])
        self.assertEqual(self.events, [("projects", {"event": "project_added", "projectId": project["id"]})])
        detail = self.service.get(project["id"])
        self.assertEqual(detail["checkFiles"], [".mobster/checks/nested/login.yml", ".mobster/checks/paywall.yaml"])

    def test_the_same_folder_twice_is_one_project_opened_again(self):
        path = self.app_folder()
        first, _ = self.service.add({"path": str(path)})
        link = self.home / "Developer" / "link"
        link.symlink_to(path)
        again, created = self.service.add({"path": str(link) + "/.mobster"})   # a link, and .mobster inside it
        self.assertFalse(created)
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(len(self.service.list()["projects"]), 1)
        self.assertEqual(self.events[-1], ("projects", {"event": "project_opened", "projectId": first["id"]}))

    def test_an_expo_app_is_read_from_app_json_without_starting_node(self):
        path = self.home / "Developer" / "stride"
        (path / "ios" / "Stride.xcworkspace").mkdir(parents=True)
        (path / "package.json").write_text('{"name": "stride", "dependencies": {"expo": "~54.0.0"}}')
        (path / "app.json").write_text(
            '{"expo": {"name": "Stride", "ios": {"bundleIdentifier": "com.example.stride"}}}')
        (path / "app.config.js").write_text("throw new Error('node must not run')")
        project, _ = self.service.add({"path": str(path)})
        self.assertEqual(project["apps"], [{"kind": "expo", "name": "Stride", "path": ".",
                                            "bundle": "com.example.stride"}])
        self.assertFalse(project["mobster"])

    def test_a_workspace_and_its_project_are_one_app_with_the_projects_bundle(self):
        path = self.app_folder("Pods")
        (path / "Pods.xcworkspace").mkdir()
        (path / "node_modules" / "dep" / "Dep.xcodeproj").mkdir(parents=True)   # never searched
        project, _ = self.service.add({"path": str(path)})
        self.assertEqual(project["apps"], [{"kind": "xcode", "name": "Pods", "path": "Pods.xcworkspace",
                                            "bundle": "com.example.firefox-ios"}])

    def test_a_git_repository_shows_its_branch_and_github_repository_never_a_token(self):
        path = self.app_folder()
        git("init", "-q", "-b", "main", cwd=path)
        git("remote", "add", "origin", "https://x-access-token:ghs_secret@github.com/mozilla-mobile/firefox-ios.git",
            cwd=path)
        git("checkout", "-q", "-b", "feature/paywall", cwd=path)
        project, _ = self.service.add({"path": str(path)})
        self.assertEqual(project["git"], {"branch": "feature/paywall", "remote": "https://github.com/mozilla-mobile/firefox-ios.git",
                                          "repo": "mozilla-mobile/firefox-ios"})
        (path / "README.md").write_text("one\n")
        git("add", "README.md", cwd=path)
        git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "one", cwd=path)
        (path / "README.md").write_text("two\n")
        detail = self.service.get(project["id"])["git"]
        self.assertEqual((detail["branch"], detail["changed"], detail["upstream"]), ("feature/paywall", 1, None))
        self.assertRegex(detail["head"], r"^[0-9a-f]{12}$")
        self.assertFalse((path / ".git" / "index.lock").exists())

    def test_the_apps_own_icon_is_found_in_its_asset_catalog_and_served(self):
        path = self.app_folder()
        icons = path / "Dailies" / "Assets.xcassets" / "AppIcon.appiconset"
        icons.mkdir(parents=True)
        (icons / "big.png").write_bytes(PNG + b"big")
        (icons / "small.png").write_bytes(PNG + b"small")
        (icons / "dark.png").write_bytes(PNG + b"dark")
        (icons / "Contents.json").write_text(json.dumps({"images": [
            {"filename": "small.png", "idiom": "iphone", "scale": "3x", "size": "60x60"},
            {"filename": "big.png", "idiom": "universal", "platform": "ios", "size": "1024x1024"},
            {"filename": "dark.png", "idiom": "universal", "size": "1024x1024", "appearances": [{"value": "dark"}]},
            {"filename": "../../../../outside.png", "idiom": "universal", "size": "2048x2048"},
            {"idiom": "mac", "size": "512x512", "scale": "2x", "filename": "small.png"}]}))
        (path.parent / "outside.png").write_bytes(PNG + b"outside")
        project, _ = self.service.add({"path": str(path)})
        self.assertRegex(project["icon"], r"^[0-9a-f]+$")
        self.assertEqual(self.service.icon(project["id"]), PNG + b"big")

    def test_an_expo_icon_comes_from_app_json_and_a_project_without_one_says_so(self):
        path = self.home / "Developer" / "stride"
        (path / "assets").mkdir(parents=True)
        (path / "assets" / "icon.png").write_bytes(PNG + b"expo")
        (path / "package.json").write_text('{"dependencies": {"expo": "~54.0.0"}}')
        (path / "app.json").write_text('{"expo": {"name": "Stride", "icon": "./assets/icon.png"}}')
        project, _ = self.service.add({"path": str(path)})
        self.assertEqual(self.service.icon(project["id"]), PNG + b"expo")
        bare, _ = self.service.add({"path": str(self.app_folder("Bare"))})
        self.assertIsNone(bare["icon"])
        with self.assertRaises(APIError) as caught:
            self.service.icon(bare["id"])
        self.assertEqual((caught.exception.status, caught.exception.code), (404, "no_icon"))

    def test_a_link_out_of_the_project_or_a_file_that_isnt_a_png_is_never_served(self):
        path = self.home / "Developer" / "stride"
        path.mkdir(parents=True)
        (self.home / "secret.png").write_bytes(b"not a png at all")
        (path / "icon.png").symlink_to(self.home / "secret.png")
        (path / "fake.png").write_text("plain text")
        (path / "package.json").write_text('{"dependencies": {"expo": "1"}}')
        (path / "app.json").write_text('{"expo": {"name": "S", "ios": {"icon": "icon.png"}, "icon": "fake.png"}}')
        project, _ = self.service.add({"path": str(path)})
        with self.assertRaises(APIError):
            self.service.icon(project["id"])

    def test_a_cached_icon_cannot_be_replaced_with_a_link_outside_the_project(self):
        path = self.app_folder()
        icon = path / "icon.png"
        icon.write_bytes(PNG + b"project icon")
        (path / "app.json").write_text('{"expo": {"icon": "icon.png"}}')
        project, _ = self.service.add({"path": str(path)})
        self.assertEqual(self.service.icon(project["id"]), PNG + b"project icon")
        outside = self.home / "outside.png"
        outside.write_bytes(PNG + b"outside")
        icon.unlink()
        icon.symlink_to(outside)
        with self.assertRaises(APIError) as caught:
            self.service.icon(project["id"])
        self.assertEqual(caught.exception.code, "no_icon")

    def test_a_bad_matrix_says_so_and_the_project_still_opens(self):
        path = self.app_folder()
        (path / ".mobster" / "matrix.yaml").write_text("simulators: [\n")
        project, _ = self.service.add({"path": str(path)})
        self.assertEqual(project["simulators"], [])
        self.assertIn(".mobster/matrix.yaml", project["matrixProblem"])
        self.assertNotIn(str(path), project["matrixProblem"])

    def test_a_name_can_be_given_and_is_checked(self):
        path = self.app_folder()
        project, _ = self.service.add({"path": str(path), "name": "  Dailies   iOS "})
        self.assertEqual(project["name"], "Dailies iOS")
        with self.assertRaises(ValueError):
            self.service.add({"path": str(self.app_folder("Other")), "name": "x" * 81})
        with self.assertRaises(ValueError):
            self.service.add({"path": str(path), "token": "x"})


class TestResultsTests(Base):
    def suite(self, root, suite_id, status="failed"):
        folder = root / ".mobster" / "test-results" / suite_id
        folder.mkdir(parents=True)
        doc = {"schema": "mobster.test/1", "suite": suite_id, "startedAt": 1, "durationMs": 2000,
               "exitCode": 0 if status == "passed" else 1, "summary": {"passed": int(status == "passed"), "total": 1},
               "checks": [{"name": "Paywall opens", "file": ".mobster/checks/paywall.yaml", "results": [
                   {"device": "iPhone 17 Pro", "deviceKey": "sim:a", "status": status, "durationMs": 900,
                    "runs": []}]}]}
        (folder / "results.json").write_text(json.dumps(doc))
        return doc, folder

    def test_a_projects_results_are_its_own_and_another_folders_runs_never_show(self):
        from mobile_agent.testkit import history, recent
        data = self.root / "dev"
        mine, other = self.app_folder(), self.app_folder("Elsewhere")
        with mock.patch.dict(os.environ, {"MOBSTER_DATA_DIR": str(data)}):
            for index, (root, status) in enumerate(((mine, "failed"), (other, "failed"), (mine, "passed"))):
                doc, folder = self.suite(root, f"20261018-10000{index}-000{index}", status)
                recent.remember(doc, folder, root)
                history.record(root, doc)
            project, _ = self.service.add({"path": str(mine)})
            found = self.service.tests(project["id"])
        self.assertTrue(found["ok"])
        self.assertEqual([suite["suite"] for suite in found["suites"]],
                         ["20261018-100002-0002", "20261018-100000-0000"])
        self.assertEqual({suite["project"] for suite in found["suites"]}, {"Dailies"})
        self.assertEqual([(check["name"], check["outcomes"]) for check in found["checks"]],
                         [("Paywall opens", ["failed", "passed"])])

    def test_a_result_with_a_text_reason_leaves_the_rest_of_the_page_readable(self):
        data = self.root / "dev"
        mine = self.app_folder()
        with mock.patch.dict(os.environ, {"MOBSTER_DATA_DIR": str(data)}):
            for index, status in enumerate(("passed", "failed")):
                doc, folder = self.suite(mine, f"20261018-10000{index}-000{index}", status)
                if index:
                    doc["checks"][0]["results"][0]["reason"] = "Expected X; found Y."
                    doc["checks"][0]["results"].append("not a result")
                    (folder / "results.json").write_text(json.dumps(doc))
            project, _ = self.service.add({"path": str(mine)})
            found = self.service.tests(project["id"])
        self.assertEqual([(check["name"], check["outcomes"]) for check in found["checks"]],
                         [("Paywall opens", ["passed", "failed"])])

    def test_a_missing_folder_has_no_results_and_says_nothing_more(self):
        path = self.app_folder()
        project, _ = self.service.add({"path": str(path)})
        path.rename(path.with_name("Gone"))
        self.assertEqual(self.service.tests(project["id"]), {"checks": [], "ok": True, "suites": []})


class RefuseTests(Base):
    def test_a_path_that_isnt_a_readable_folder_is_refused_with_what_to_do(self):
        self.assertIn("full path", self.refused("Developer/Dailies", "invalid_path"))
        self.assertIn("full path", self.refused("", "invalid_path"))
        self.assertIn("There's no folder at ~/Developer/Gone",
                      self.refused(self.home / "Developer" / "Gone", "missing"))
        (self.home / "Developer" / "notes.txt").write_text("x")
        self.assertIn("is a file", self.refused(self.home / "Developer" / "notes.txt", "not_folder"))
        with self.assertRaises(APIError):
            self.service.add({"path": "/tmp/\0x"})

    def test_folders_that_hold_everything_are_refused(self):
        for path in (self.home, self.home / "Documents", Path("/"), Path("/Users"), Path("/tmp")):
            self.assertIn("Choose your app's own folder", self.refused(path, "too_broad"))
        self.assertIn("Mobster's own data folder", self.refused(self.data, "mobster_data"))
        holder = self.home / "Developer"
        for name in ("one", "two"):
            (holder / name / ".git").mkdir(parents=True)
        self.assertIn("holds 2 repositories", self.refused(holder, "too_broad"))
        self.assertEqual(self.service.list()["projects"], [])

    @unittest.skipIf(os.geteuid() == 0, "root reads every folder")
    def test_a_folder_mobster_cant_read_is_refused(self):
        path = self.app_folder()
        path.chmod(0)
        self.addCleanup(path.chmod, stat.S_IRWXU)
        self.assertIn("System Settings", self.refused(path, "unreadable"))

    def test_a_project_whose_folder_went_away_says_so_and_can_be_forgotten(self):
        path = self.app_folder()
        project, _ = self.service.add({"path": str(path)})
        path.rename(path.with_name("Moved"))
        listed = self.service.list()["projects"][0]
        self.assertEqual(listed["state"], "missing")
        self.assertIn("isn't there any more", listed["problem"])
        self.assertEqual((listed["apps"], listed["checks"]), ([], 0))
        self.assertEqual(self.service.forget(project["id"]), project["id"])
        self.assertTrue(path.with_name("Moved").is_dir())
        with self.assertRaises(APIError) as caught:
            self.service.get(project["id"])
        self.assertEqual(caught.exception.status, 404)

    def test_forgetting_leaves_every_file(self):
        path = self.app_folder()
        (path / ".mobster" / "checks" / "a.yaml").write_text("version: 1\n")
        project, _ = self.service.add({"path": str(path)})
        self.service.forget(project["id"])
        self.assertTrue((path / ".mobster" / "checks" / "a.yaml").is_file())
        self.assertEqual(self.service.list()["projects"], [])

    def test_the_hundred_and_first_project_is_refused_as_a_sentence_not_a_journal_failure(self):
        store = self.service.store
        for index in range(project_store.MAX_PROJECTS):
            store.add({"id": f"{index:012x}", "path": f"/p/{index}", "name": "p", "source": {"kind": "folder"},
                       "added_at": 1, "opened_at": 1})
        with self.assertRaises(ValueError) as caught:
            self.service.add({"path": str(self.app_folder())})
        self.assertIn("at most 100 projects", str(caught.exception))


class ListTests(Base):
    def test_the_project_opened_last_comes_first(self):
        clock = iter(range(100, 200))
        self.service.clock = lambda: next(clock)
        first, _ = self.service.add({"path": str(self.app_folder("One"))})
        second, _ = self.service.add({"path": str(self.app_folder("Two"))})
        self.assertEqual([p["name"] for p in self.service.list()["projects"]], ["Two", "One"])
        opened = self.service.open(first["id"])
        self.assertEqual(opened["openedAt"], 102_000)
        self.assertEqual([p["name"] for p in self.service.list()["projects"]], ["One", "Two"])
        self.assertEqual(second["openedAt"], 101_000)

    def test_folders_mobster_test_ran_in_are_suggested_until_they_are_added(self):
        path = self.app_folder()
        gone = self.home / "Developer" / "Gone"
        run = {"suite": "20261017-101500-3f9a", "startedAt": 5, "exitCode": 1, "summary": {"failed": 1, "total": 3}}
        self.last_runs = {str(path): run, str(gone): {**run, "startedAt": 9}}
        listing = self.service.list()
        self.assertEqual(listing["suggestions"], [{"path": str(path), "name": "Dailies",
                                                   "shownPath": "~/Developer/Dailies", "lastRun": run}])
        self.assertEqual((listing["cloneFolder"], listing["cloneFolderShown"]),
                         (str(self.home / "Mobster" / "Projects"), "~/Mobster/Projects"))
        project, _ = self.service.add({"path": str(path)})
        listing = self.service.list()
        self.assertEqual(listing["suggestions"], [])
        self.assertEqual(listing["projects"][0]["lastRun"], run)


class CloneParseTests(unittest.TestCase):
    def test_https_ssh_and_short_forms_are_read(self):
        for text, expected in (
                ("https://github.com/mozilla-mobile/firefox-ios.git",
                 ("https://github.com/mozilla-mobile/firefox-ios.git", "github.com", "firefox-ios")),
                ("https://gitlab.example.com:8443/team/ios/app", ("https://gitlab.example.com:8443/team/ios/app",
                                                                  "gitlab.example.com", "app")),
                ("git@github.com:mozilla-mobile/firefox-ios.git", ("git@github.com:mozilla-mobile/firefox-ios.git",
                    "github.com", "firefox-ios")),
                ("ssh://git@bitbucket.org/mozilla-mobile/app.git",
                 ("ssh://git@bitbucket.org/mozilla-mobile/app.git", "bitbucket.org", "app")),
                ("mozilla-mobile/firefox-ios", ("https://github.com/mozilla-mobile/firefox-ios.git", "github.com",
                    "firefox-ios")),
                ("github.com/mozilla-mobile/firefox-ios", ("https://github.com/mozilla-mobile/firefox-ios.git",
                    "github.com", "firefox-ios")),
                ("  mozilla-mobile/firefox-ios.git ", ("https://github.com/mozilla-mobile/firefox-ios.git",
                    "github.com", "firefox-ios"))):
            self.assertEqual(cloning.parse(text), expected, text)

    def test_anything_else_is_refused_before_git_runs(self):
        for text in ("file:///etc", "/Users/me/repo", "ext::sh -c touch% /tmp/x", "-uhttps://github.com/a/b",
                     "https://github.com/a/../b", "https://github.com/a/-b", "http://github.com/a/b", "a b/c",
                     "https://github.com/", "", None, "x" * 3000, "git@github.com:-oProxyCommand=x/y"):
            with self.assertRaises(APIError, msg=text) as caught:
                cloning.parse(text)
            self.assertEqual(caught.exception.code, "invalid_url", text)

    def test_a_password_or_token_in_the_url_is_refused_and_never_echoed(self):
        with self.assertRaises(APIError) as caught:
            cloning.parse("https://me:ghp_secret@github.com/mozilla-mobile/firefox-ios.git")
        self.assertEqual(caught.exception.code, "credentials_in_url")
        self.assertNotIn("ghp_secret", str(caught.exception))

    def test_git_failures_read_as_what_to_do(self):
        for lines, code in ((["remote: Repository not found.", "fatal: repository 'x' not found"], "no_access"),
                            (["fatal: could not read Username for 'https://github.com': terminal prompts disabled"],
                             "no_access"),
                            (["git@github.com: Permission denied (publickey)."], "no_access"),
                            (["fatal: unable to access 'x': Could not resolve host: github.com"], "offline"),
                            (["Host key verification failed."], "host_key"),
                            (["fatal: could not create work tree dir 'x': Permission denied"], "unwritable"),
                            (["error: no space left on device"], "disk_full"),
                            (["fatal: https://tok@github.com/x weird"], "failed")):
            got, sentence = cloning.classify(lines, "https://github.com/mozilla-mobile/app.git", "github.com")
            self.assertEqual(got, code, lines)
            self.assertNotIn("tok@", sentence)


class CloneTests(Base):
    def test_a_clone_goes_to_mobster_projects_and_becomes_a_project(self):
        started = self.service.clone({"url": "mozilla-mobile/firefox-ios"})
        self.assertEqual((started["state"], started["name"]), ("cloning", "firefox-ios"))
        clone = self.ended(started["id"])
        target = self.home / "Mobster" / "Projects" / "firefox-ios"
        self.assertEqual((clone["state"], clone["percent"], clone["error"]), ("done", 100, None))
        self.assertEqual(clone["path"], str(target))
        self.assertTrue((target / "README.md").is_file())
        argv, options = self.git.calls[0]
        self.assertEqual(argv, ["git", "-c", "core.fsmonitor=false", "clone", "--progress", "--",
                                "https://github.com/mozilla-mobile/firefox-ios.git", str(target)])
        self.assertEqual((options["env"]["GIT_TERMINAL_PROMPT"], options["env"]["GCM_INTERACTIVE"]), ("0", "never"))
        self.assertTrue(options["start_new_session"])
        self.assertIs(options["stdin"], subprocess.DEVNULL)
        project = self.service.get(clone["projectId"])
        self.assertEqual((project["name"], project["source"]),
                         ("firefox-ios", {"kind": "clone", "url": "https://github.com/mozilla-mobile/firefox-ios.git"}))
        kinds = [event["event"] for topic, event in self.events]
        self.assertEqual((kinds[0], kinds[-2:]), ("clone_changed", ["project_added", "clone_ended"]))
        self.assertEqual(self.events[-1][1]["clone"]["projectId"], project["id"])

    def test_progress_is_told_as_it_goes(self):
        self.git.mode = "slow"
        started = self.service.clone({"url": "mozilla-mobile/firefox-ios", "name": "slow-one"})
        self.ended(started["id"])
        percents = [event["clone"]["percent"] for topic, event in self.events if event["event"] == "clone_changed"]
        self.assertTrue(any(0 < share < 100 for share in percents), percents)
        self.assertEqual(percents, sorted(percents))

    def test_a_repository_without_access_fails_says_why_and_leaves_nothing(self):
        self.git.mode = "denied"
        clone = self.ended(self.service.clone({"url": "https://github.com/mozilla-mobile/secret.git"})["id"])
        self.assertEqual((clone["state"], clone["code"]), ("failed", "no_access"))
        self.assertIn("signed in to an account that can read it", clone["error"])
        self.assertFalse((self.home / "Mobster" / "Projects" / "secret").exists())
        self.assertEqual(self.service.list()["projects"], [])
        self.assertEqual(len(self.git.calls), 1)   # never retried

    def test_offline_and_unknown_host_keys_say_what_to_do(self):
        for mode, code in (("offline", "offline"), ("hostkey", "host_key")):
            self.git.mode = mode
            clone = self.ended(self.service.clone({"url": "git@github.com:mozilla-mobile/app.git", "name": mode})["id"])
            self.assertEqual((clone["state"], clone["code"]), ("failed", code))

    def test_without_git_the_clone_says_how_to_get_it(self):
        self.git.mode = "missing"
        clone = self.ended(self.service.clone({"url": "mozilla-mobile/app"})["id"])
        self.assertEqual(clone["code"], "no_git")
        self.assertIn("xcode-select --install", clone["error"])

    def test_without_the_developer_tools_git_never_runs_so_macos_opens_no_installer(self):
        path = self.app_folder()
        git("init", "-q", cwd=path)
        with mock.patch("mobile_agent.projects.folder.git_ready", return_value=False), \
                mock.patch("mobile_agent.projects.folder.subprocess.run") as run:
            project, _ = self.service.add({"path": str(path)})
            clone = self.ended(self.service.clone({"url": "mozilla-mobile/app"})["id"])
        self.assertIsNone(project["git"])
        run.assert_not_called()
        self.assertEqual((clone["code"], self.git.calls), ("no_git", []))

    def test_git_ready_reads_xcode_select_and_asks_again_after_a_minute(self):
        clock = [1000.0]
        folder._ready.update(at=None)
        self.addCleanup(folder._ready.update, at=None)
        tools = self.root / "CommandLineTools"
        (tools / "usr" / "bin").mkdir(parents=True)
        answer = mock.Mock(returncode=2, stdout=b"")
        with mock.patch("mobile_agent.projects.folder.shutil.which", return_value="/usr/bin/git"), \
                mock.patch("mobile_agent.projects.folder.subprocess.run", return_value=answer) as run:
            self.assertFalse(folder.git_ready(lambda: clock[0]))
            (tools / "usr" / "bin" / "git").write_text("")
            answer.returncode, answer.stdout = 0, str(tools).encode() + b"\n"
            clock[0] += 30
            self.assertFalse(folder.git_ready(lambda: clock[0]))      # remembered for a minute
            clock[0] += 31
            self.assertTrue(folder.git_ready(lambda: clock[0]))
        self.assertEqual(run.call_count, 2)
        with mock.patch("mobile_agent.projects.folder.shutil.which", return_value="/opt/homebrew/bin/git"):
            clock[0] += 61
            self.assertTrue(folder.git_ready(lambda: clock[0]))

    def test_cancel_stops_git_and_removes_only_the_folder_it_made(self):
        self.git.mode = "hang"
        started = self.service.clone({"url": "mozilla-mobile/huge"})
        target = Path(started["path"])
        deadline = time.monotonic() + bound(30)
        while not target.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        clone = self.service.cancel_clone(started["id"])
        self.assertEqual(clone["state"], "canceled")
        self.assertFalse(target.exists())
        self.assertEqual(self.service.list()["projects"], [])

    def test_an_existing_folder_is_never_cloned_over(self):
        target = self.home / "Mobster" / "Projects" / "firefox-ios"
        (target / "mine.txt").parent.mkdir(parents=True)
        (target / "mine.txt").write_text("keep")
        with self.assertRaises(APIError) as caught:
            self.service.clone({"url": "mozilla-mobile/firefox-ios"})
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "exists"))
        self.assertEqual(caught.exception.details["details"]["path"], str(target))
        self.assertEqual((target / "mine.txt").read_text(), "keep")
        self.assertEqual(self.git.calls, [])

    def test_a_folder_created_after_preflight_is_not_given_to_git(self):
        target = self.home / "Mobster" / "Projects" / "raced"

        def ready():
            target.mkdir(parents=True)
            (target / "mine.txt").write_text("keep")
            return True
        with mock.patch.object(folder, "git_ready", side_effect=ready):
            started = self.service.clone({"url": "mozilla-mobile/raced"})
            clone = self.ended(started["id"])
        self.assertEqual((target / "mine.txt").read_text(), "keep")
        self.assertEqual(clone["code"], "exists")
        self.assertEqual(self.git.calls, [])

    def test_a_replacement_folder_is_not_removed_when_git_fails(self):
        original = FakeGit("offline")

        def replace_folder(argv, **options):
            target = Path(argv[-1])
            if target.exists():
                target.rename(target.with_name("original-owned-folder"))
            target.mkdir()
            (target / "mine.txt").write_text("keep")
            return original(argv, **options)
        self.service._popen = replace_folder
        started = self.service.clone({"url": "mozilla-mobile/replaced"})
        clone = self.ended(started["id"])
        self.assertEqual(clone["state"], "failed")
        self.assertEqual((Path(clone["path"]) / "mine.txt").read_text(), "keep")

    def test_a_chosen_folder_must_be_there_and_writable_and_the_name_plain(self):
        with self.assertRaises(APIError) as caught:
            self.service.clone({"url": "mozilla-mobile/a", "folder": str(self.home / "Nowhere")})
        self.assertEqual(caught.exception.code, "missing")
        for name in ("../up", ".", "-x", "a/b"):
            with self.assertRaises(APIError, msg=name) as caught:
                self.service.clone({"url": "mozilla-mobile/a", "name": name})
            self.assertEqual(caught.exception.code, "invalid_name")
        started = self.service.clone({"url": "mozilla-mobile/a", "folder": str(self.home / "Developer"),
            "name": "a-ios"})
        self.assertEqual(self.ended(started["id"])["path"], str(self.home / "Developer" / "a-ios"))
        with self.assertRaises(ValueError):
            self.service.clone({"url": "mozilla-mobile/a", "branch": "main"})

    def test_a_clone_that_cant_become_a_project_keeps_the_folder_and_says_where(self):
        for index in range(project_store.MAX_PROJECTS):
            self.service.store.add({"id": f"{index:012x}", "path": f"/p/{index}", "name": "p",
                                    "source": {"kind": "folder"}, "added_at": 1, "opened_at": 1})
        clone = self.ended(self.service.clone({"url": "mozilla-mobile/firefox-ios"})["id"])
        self.assertEqual(clone["state"], "failed")
        self.assertIn("cloned at ~/Mobster/Projects/firefox-ios", clone["error"])
        self.assertTrue((self.home / "Mobster" / "Projects" / "firefox-ios" / "README.md").is_file())

    def test_at_most_three_clones_at_once_and_closing_stops_them(self):
        self.git.mode = "hang"
        going = [self.service.clone({"url": f"mozilla-mobile/r{index}"}) for index in range(3)]
        with self.assertRaises(APIError) as caught:
            self.service.clone({"url": "mozilla-mobile/r3"})
        self.assertEqual(caught.exception.code, "busy")
        self.service.close()   # waits 2 s at most in all; a loaded Mac may need longer for git to go
        for started in going:
            self.assertEqual(self.ended(started["id"])["state"], "canceled")
            self.assertFalse(Path(started["path"]).exists())
        with self.assertRaises(APIError):
            self.service.clone({"url": "mozilla-mobile/r4"})


class RouteTests(Base):
    def request(self, method, path, body=None, params=None, app_session=True):
        return api_routes.Request(runtime=self.runtime, method=method, path=path, params=params or {}, query={},
                                  headers={}, body=body, content_type="application/json", origin="app",
                                  app_session=app_session)

    def setUp(self):
        super().setUp()
        project_service._services[id(self.runtime)] = self.service
        self.addCleanup(project_service._services.pop, id(self.runtime), None)

    def test_the_routes_add_list_open_and_forget(self):
        path = str(self.app_folder())
        added = routes.add(self.request("POST", "/api/projects", {"path": path}))
        self.assertEqual(added.status, 201)
        identifier = added.value["project"]["id"]
        self.assertEqual(routes.add(self.request("POST", "/api/projects", {"path": path})).status, 200)
        self.assertEqual(routes.listed(self.request("GET", "/api/projects")).value["projects"][0]["id"], identifier)
        got = routes.get(self.request("GET", f"/api/projects/{identifier}", params={"id": identifier}))
        self.assertEqual(got.value["project"]["checkFiles"], [])
        with self.assertRaises(APIError):
            routes.icon(self.request("GET", f"/api/projects/{identifier}/icon", params={"id": identifier}))
        (Path(path) / "app.json").write_text('{"expo": {"icon": "icon.png"}}')
        (Path(path) / "icon.png").write_bytes(PNG)
        folder._icons.clear()
        drawn = routes.icon(self.request("GET", f"/api/projects/{identifier}/icon", params={"id": identifier}))
        self.assertEqual((drawn.data, drawn.content_type), (PNG, "image/png"))
        routes.opened(self.request("POST", f"/api/projects/{identifier}/open", params={"id": identifier}))
        gone = routes.forget(self.request("DELETE", f"/api/projects/{identifier}", params={"id": identifier}))
        self.assertEqual(gone.value, {"id": identifier})

    def test_writes_need_the_mac_apps_session_when_it_has_one_and_reads_dont(self):
        self.runtime.local_api = {"app_session": "s3cret"}
        path = str(self.app_folder())
        for call, method, route_path, body in ((routes.add, "POST", "/api/projects", {"path": path}),
                                               (routes.clone, "POST", "/api/projects/clones",
                                                   {"url": "mozilla-mobile/a"})):
            with self.assertRaises(APIError) as caught:
                call(self.request(method, route_path, body, app_session=False))
            self.assertEqual((caught.exception.status, caught.exception.code), (403, "app_only"))
        self.assertEqual(self.service.list()["projects"], [])
        self.assertEqual(self.git.calls, [])
        self.assertEqual(routes.listed(self.request("GET", "/api/projects", app_session=False)).value["projects"], [])
        self.assertEqual(routes.add(self.request("POST", "/api/projects", {"path": path})).status, 201)

    def test_without_the_service_the_routes_answer_503(self):
        project_service._services.pop(id(self.runtime), None)
        with self.assertRaises(APIError) as caught:
            routes.listed(self.request("GET", "/api/projects"))
        self.assertEqual(caught.exception.status, 503)

    def test_the_track_registers_its_routes_without_clashing(self):
        with harness_api.owned_by("projects"):
            journal_module.SCHEMAS.pop("projects", None)
            register.register(harness_api)
        for method, path, handler in (("GET", "/api/projects", routes.listed),
                                      ("POST", "/api/projects/clones", routes.clone),
                                      ("GET", "/api/projects/clones/0123456789ab", routes.clone_status),
                                      ("DELETE", "/api/projects/clones/0123456789ab", routes.cancel_clone),
                                      ("GET", "/api/projects/0123456789ab", routes.get),
                                      ("POST", "/api/projects/0123456789ab/open", routes.opened),
                                      ("DELETE", "/api/projects/0123456789ab", routes.forget)):
            found = api_routes.match(method, path)
            self.assertIsNotNone(found, path)
            self.assertIs(found[0].handler, handler, path)


class MemoryAndRunFieldTests(Base):
    """Memory learns a project's facts under its id (POST /api/memory/projects), and a task in a project carries
    {id, path, name} in its run field ``project``."""

    def listen(self, answer=None):
        calls = []

        def handler(request):
            calls.append((request.method, request.body))
            if isinstance(answer, Exception):
                raise answer
            return api_routes.Response.json({"linked": True})
        api_routes.register(api_routes.Route("memory", "POST", "/api/memory/projects", handler))
        return calls

    def told(self):
        self.service._memory_work.submit(lambda: None).result(5)

    def test_adding_and_opening_a_project_tell_memory_its_folder(self):
        calls = self.listen()
        project, _ = self.service.add({"path": str(self.app_folder())})
        self.told()
        self.service.open(project["id"])
        self.told()
        self.assertEqual(calls, [("POST", {"id": project["id"], "path": project["path"]})] * 2)

    def test_a_memory_that_fails_or_isnt_here_never_stops_a_project(self):
        calls = self.listen(RuntimeError("memory is down"))
        with self.assertLogs(harness_api.log, "ERROR") as logged:
            project, created = self.service.add({"path": str(self.app_folder())})
            self.told()
        self.assertTrue(created)
        self.assertIn("memory couldn't link", logged.output[0])
        api_routes._routes.clear()
        self.service.open(project["id"])
        self.told()
        self.assertEqual(len(calls), 1)                  # no route, nothing called

    def test_repeated_opens_do_not_start_parallel_memory_workers(self):
        entered, concurrent, release = threading.Event(), threading.Event(), threading.Event()
        lock, active = threading.Lock(), 0

        def handler(request):
            nonlocal active
            with lock:
                active += 1
                if active > 1:
                    concurrent.set()
            entered.set()
            release.wait(10)
            with lock:
                active -= 1
            return api_routes.Response.json({"linked": True})
        api_routes.register(api_routes.Route("memory", "POST", "/api/memory/projects", handler))
        try:
            project, _ = self.service.add({"path": str(self.app_folder())})
            self.assertTrue(entered.wait(5))
            for _ in range(3):
                self.service.open(project["id"])
            self.assertFalse(concurrent.wait(.2))
        finally:
            release.set()
            self.told()

    def test_the_run_field_is_the_projects_id_path_and_name_and_refuses_one_mobster_doesnt_have(self):
        project_service._services[id(self.runtime)] = self.service
        self.addCleanup(project_service._services.pop, id(self.runtime), None)
        project, _ = self.service.add({"path": str(self.app_folder())})
        expected = {"id": project["id"], "path": project["path"], "name": "Dailies"}
        for value in (project["id"], {"id": project["id"]}, {"id": project["id"], "path": "/elsewhere"}):
            self.assertEqual(register.validate_project(value, self.runtime), expected)
        for value in ("0123456789ab", {"id": "0123456789ab"}, {"id": project["id"], "branch": "main"}, 7, None):
            with self.assertRaises(ValueError, msg=value):
                register.validate_project(value, self.runtime)
        project_service._services.pop(id(self.runtime))
        with self.assertRaises(ValueError):
            register.validate_project(project["id"], self.runtime)

    def test_the_track_registers_the_public_run_field(self):
        with harness_api.owned_by("projects"):
            journal_module.SCHEMAS.pop("projects", None)
            register.register(harness_api)
        validate, public = harness_api.run_fields()["project"][:2]
        self.assertTrue(public)
        self.assertIs(validate, register.validate_project)


class FakeResponse:
    def __init__(self, value):
        self.body = json.dumps(value).encode()

    def read(self, limit):
        return self.body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def github_row(full_name, **extra):
    return {"full_name": full_name, "private": False, "description": "An app", "language": "Swift",
            "default_branch": "main", "pushed_at": "2026-10-17T09:00:00Z", **extra}


class GitHubTests(Base):
    """Import from GitHub: the connector's sign-in lists the repositories it can open, and clones a private one
    without the token ever reaching a command line, a URL or a log."""

    def setUp(self):
        super().setUp()
        self.requests = []
        self.pages = [[github_row("mozilla-mobile/firefox-ios", private=True), github_row("mozilla-mobile/web",
            language="TypeScript")]]

        def opener(request, timeout):
            self.requests.append((request.full_url, dict(request.header_items())))
            page = int(urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)["page"][0])
            return FakeResponse(self.pages[page - 1] if page <= len(self.pages) else [])
        self.service._opener = opener

    def signed_in(self, token="gho_signedin"):
        patcher = mock.patch("mobile_agent.projects.github.token_of", return_value=token)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_without_a_github_sign_in_it_says_so_and_asks_nothing(self):
        self.signed_in(None)
        self.assertEqual(self.service.github(), {"signedIn": False, "repos": []})
        self.assertEqual(self.requests, [])

    def test_the_repositories_come_newest_first_marked_when_one_is_a_project_already(self):
        self.signed_in()
        path = self.app_folder()
        git("init", "-q", cwd=path)
        git("remote", "add", "origin", "git@github.com:mozilla-mobile/firefox-ios.git", cwd=path)
        project, _ = self.service.add({"path": str(path)})
        listed = self.service.github()
        self.assertTrue(listed["signedIn"])
        self.assertEqual([(repo["repo"], repo["private"], repo["language"], repo["projectId"])
                          for repo in listed["repos"]],
                         [("mozilla-mobile/firefox-ios", True, "Swift", project["id"]), ("mozilla-mobile/web", False,
                             "TypeScript", None)])
        url, headers = self.requests[0]
        self.assertIn("sort=pushed", url)
        self.assertEqual(headers["Authorization"], "Bearer gho_signedin")
        self.service.github()
        self.assertEqual(len(self.requests), 1)                   # kept for a minute
        self.service.github(refresh=True)
        self.assertEqual(len(self.requests), 2)

    def test_more_than_a_page_is_read_and_nothing_odd_gets_through(self):
        self.signed_in()
        self.pages = [[github_row(f"mozilla-mobile/r{index}") for index in range(100)],
                      [github_row("mozilla-mobile/last"), {"full_name": 7}, "junk"]]
        repos = self.service.github()["repos"]
        self.assertEqual((len(repos), repos[-1]["repo"]), (101, "mozilla-mobile/last"))

    def test_concurrent_sign_ins_keep_their_own_repository_list(self):
        from concurrent.futures import ThreadPoolExecutor
        local = threading.local()
        both_reading = threading.Barrier(2)

        def projects():
            both_reading.wait(timeout=5)
            return []

        def read(token):
            local.token = token
            return self.service.github(refresh=True)["repos"][0]["repo"]

        with mock.patch("mobile_agent.projects.github.token_of", side_effect=lambda _: local.token), \
                mock.patch("mobile_agent.projects.github.repositories",
                           side_effect=lambda token, **_: [{"repo": f"{token}/private"}]), \
                mock.patch.object(self.service.store, "projects", side_effect=projects), ThreadPoolExecutor(2) as pool:
            first = pool.submit(read, "first")
            second = pool.submit(read, "second")
            self.assertEqual((first.result(), second.result()), ("first/private", "second/private"))

    def test_a_refused_or_unreachable_github_says_what_to_do(self):
        import urllib.error
        self.signed_in()
        for raised, code in ((urllib.error.HTTPError("u", 401, "no", {}, None), "github_sign_in"),
                             (urllib.error.URLError("down"), "offline")):
            def opener(request, timeout, raised=raised):
                raise raised
            self.service._opener = opener
            with self.assertRaises(APIError) as caught:
                self.service.github(refresh=True)
            self.assertEqual(caught.exception.code, code)

    def test_a_github_clone_uses_the_sign_in_through_gits_environment_only(self):
        self.signed_in("gho_secret_token")
        clone = self.ended(self.service.clone({"url": "mozilla-mobile/firefox-ios"})["id"])
        self.assertEqual(clone["state"], "done")
        argv, options = self.git.calls[0]
        env = options["env"]
        before = int(os.environ.get("GIT_CONFIG_COUNT", "0"))
        self.assertEqual((env["GIT_CONFIG_COUNT"], env[f"GIT_CONFIG_KEY_{before}"]),
                         (str(before + 1), "http.https://github.com/.extraheader"))
        self.assertIn(base64.b64encode(b"x-access-token:gho_secret_token").decode(), env[f"GIT_CONFIG_VALUE_{before}"])
        for index in range(before):            # what was already in git's environment stays
            self.assertEqual(env[f"GIT_CONFIG_KEY_{index}"], os.environ[f"GIT_CONFIG_KEY_{index}"])
        self.assertNotIn("gho_secret_token", " ".join(argv) + json.dumps(clone))
        self.assertEqual(self.service.get(clone["projectId"])["source"],
                         {"kind": "clone", "url": "https://github.com/mozilla-mobile/firefox-ios.git"})

    def test_the_sign_in_never_goes_to_another_host_or_over_ssh(self):
        self.signed_in("gho_secret_token")
        for url in ("https://gitlab.com/mozilla-mobile/app.git", "git@github.com:mozilla-mobile/ssh-app.git"):
            self.ended(self.service.clone({"url": url})["id"])
            env = self.git.calls[-1][1]["env"]
            self.assertFalse([key for key, value in env.items() if "AUTHORIZATION" in value], url)

    def test_a_repository_the_sign_in_cant_open_says_so(self):
        self.signed_in()
        self.git.mode = "denied"
        clone = self.ended(self.service.clone({"url": "mozilla-mobile/secret"})["id"])
        self.assertEqual(clone["code"], "no_access")
        self.assertIn("with your GitHub sign-in", clone["error"])

    def test_listing_needs_the_mac_apps_session(self):
        self.signed_in()
        project_service._services[id(self.runtime)] = self.service
        self.addCleanup(project_service._services.pop, id(self.runtime), None)
        self.runtime.local_api = {"app_session": "s"}
        request = api_routes.Request(runtime=self.runtime, method="GET", path="/api/projects/github", params={},
                                     query={}, headers={}, body=None, content_type=None, origin="app",
                                     app_session=False)
        with self.assertRaises(APIError) as caught:
            routes.github_repos(request)
        self.assertEqual(caught.exception.code, "app_only")
        self.assertEqual(self.requests, [])


class FolderTests(unittest.TestCase):
    def test_shown_paths_use_the_tilde(self):
        self.assertEqual(folder.shown("/Users/me/Developer/app", "/Users/me"), "~/Developer/app")
        self.assertEqual(folder.shown("/Users/me", "/Users/me"), "~")
        self.assertEqual(folder.shown("/Volumes/Work/app", "/Users/me"), "/Volumes/Work/app")

    def test_github_remotes_name_their_repository(self):
        for url in ("https://github.com/mozilla-mobile/app.git", "git@github.com:mozilla-mobile/app.git", "ssh://git@github.com/mozilla-mobile/app",
                    "https://github.com/mozilla-mobile/app/"):
            self.assertEqual(folder.repo_of(url), "mozilla-mobile/app", url)
        self.assertIsNone(folder.repo_of("https://gitlab.com/mozilla-mobile/app.git"))
        self.assertEqual(folder.clean_url("https://tok:x@github.com/a/b.git"), "https://github.com/a/b.git")


if __name__ == "__main__":
    unittest.main()
