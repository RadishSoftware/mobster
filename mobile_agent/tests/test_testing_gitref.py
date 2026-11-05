"""The branch and commit a suite tested (testkit/gitref.py): in results.json, JUnit's suite properties and the recent
suites the Mac app groups by branch. Real `git init` repositories in temporary folders; offline."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mobile_agent.testkit import gitref, recent, reports
from mobile_agent.tests.test_testing import validate_junit

GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
           "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com", "GIT_COMMITTER_NAME": "Test",
           "GIT_COMMITTER_EMAIL": "test@example.com"}


def git(*argv, cwd):
    return subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True, env=GIT_ENV,
                          text=True).stdout.strip()


class CheckoutTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve() / "Firefox"
        self.root.mkdir()

    def repo(self, branch="onboarding-v2"):
        git("init", "-q", "-b", branch, cwd=self.root)
        (self.root / "README.md").write_text("hi\n")
        git("add", ".", cwd=self.root)
        git("commit", "-q", "-m", "first", cwd=self.root)
        return git("rev-parse", "HEAD", cwd=self.root)

    def test_a_checkout_names_its_branch_and_commit_from_the_project_or_a_folder_in_it(self):
        head = self.repo()
        (self.root / ".mobster").mkdir()
        self.assertEqual(gitref.checkout(self.root, env={}), {"branch": "onboarding-v2", "head": head})
        self.assertEqual(gitref.checkout(self.root / ".mobster", env={}), {"branch": "onboarding-v2", "head": head})

    def test_a_detached_ci_checkout_takes_its_branch_from_the_ci(self):
        head = self.repo("main")
        git("checkout", "-q", "--detach", cwd=self.root)
        self.assertEqual(gitref.checkout(self.root, env={}), {"branch": None, "head": head})
        pull = {"GITHUB_HEAD_REF": "fix/streak", "GITHUB_REF_NAME": "41/merge", "GITHUB_REF_TYPE": "branch"}
        self.assertEqual(gitref.checkout(self.root, env=pull)["branch"], "fix/streak")
        self.assertEqual(gitref.checkout(self.root, env={"BITRISE_GIT_BRANCH": "release/2.4"})["branch"], "release/2.4")
        tag = {"GITHUB_REF_NAME": "v2.4.0", "GITHUB_REF_TYPE": "tag"}   # a tag isn't a branch
        self.assertEqual(gitref.checkout(self.root, env=tag)["branch"], None)
        self.assertIsNone(gitref.checkout(self.root, env={"CIRCLE_BRANCH": "bad\nname"})["branch"])

    def test_outside_a_checkout_git_never_runs(self):
        with mock.patch("mobile_agent.testkit.gitref.subprocess.run") as run:
            self.assertIsNone(gitref.checkout(self.root, env={"GITHUB_HEAD_REF": "main"}))
        run.assert_not_called()

    def test_what_git_answers_is_checked(self):
        (self.root / ".git").mkdir()
        answers = {"junk": (0, "not-a-commit\nHEAD\n"), "failed": (128, ""), "fine": (0, "a" * 40 + "\nmain\n")}
        found = {name: gitref.checkout(self.root, env={}, run=lambda argv, cwd, out=out: out)
                 for name, out in answers.items()}
        self.assertEqual(found, {"junk": None, "failed": None, "fine": {"branch": "main", "head": "a" * 40}})


class ReportTests(unittest.TestCase):
    def test_results_json_junit_and_recent_suites_carry_the_branch(self):
        source = {"branch": "onboarding-v2", "head": "b" * 40}
        results = {"startedAt": 1, "exitCode": 0, "summary": {"passed": 1, "total": 1}, "devices": [
            {"key": "sim:a", "name": "iPhone 17 Pro · iOS 26.4"}], "checks": [{"name": "Opens", "file": "a.yaml",
            "results": [{"device": "iPhone 17 Pro · iOS 26.4", "deviceKey": "sim:a", "status": "passed",
                         "attempts": 1, "runs": []}]}]}
        doc = reports.document(results, suite_id="20261018-180000-0001", options={}, version="0.4.0", git=source)
        self.assertEqual(doc["git"], source)
        self.assertNotIn("git", reports.document(results, suite_id="x", options={}, version="0.4.0"))
        props = {p.get("name"): p.get("value") for p in validate_junit(reports.junit(doc))[0].find("properties")}
        self.assertEqual((props["git.branch"], props["git.head"]), ("onboarding-v2", "b" * 40))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "Firefox"
            suite = root / ".mobster" / "test-results" / doc["suite"]
            suite.mkdir(parents=True)
            (suite / "results.json").write_text(json.dumps(doc))
            recent.remember(doc, suite, root, data_dir=Path(folder))
            self.assertEqual(recent.recent(data_dir=Path(folder))[0]["git"], source)
            del doc["git"]   # a suite from before git was written
            (suite / "results.json").write_text(json.dumps(doc))
            self.assertIsNone(recent.recent(data_dir=Path(folder))[0]["git"])


if __name__ == "__main__":
    unittest.main()
