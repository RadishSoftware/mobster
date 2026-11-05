import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ACTION = ROOT / "actions" / "test"


def load():
    spec = importlib.util.spec_from_file_location("mobster_test_action", ACTION / "action.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


A = load()


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeGitHub:
    def __init__(self, comments=(), fail=None):
        self.comments = [dict(item) for item in comments]
        self.calls, self.fail = [], fail

    def __call__(self, request, timeout=None):
        method, url = request.get_method(), request.full_url
        body = json.loads(request.data) if request.data else None
        self.calls.append((method, url, body))
        assert request.headers["Authorization"] == "Bearer t0ken"
        if self.fail:
            raise urllib.error.HTTPError(url, self.fail, "no", {}, None)
        if method == "GET":
            page = int(url.rsplit("page=", 1)[1])
            items = self.comments[(page - 1) * A.PAGE: page * A.PAGE]
            return Response(json.dumps(items).encode())
        if method == "PATCH":
            return Response(json.dumps({"html_url": "https://github.com/o/r/pull/7#issuecomment-1"}).encode())
        return Response(json.dumps({"html_url": "https://github.com/o/r/pull/7#issuecomment-2"}).encode())


class Env(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.event = self.folder / "event.json"
        self.event.write_text(json.dumps({"pull_request": {"number": 7}}))
        self.summary = self.folder / "summary.md"
        self.output = self.folder / "output.txt"
        self.report = self.folder / "suite"
        self.report.mkdir()
        (self.report / "pr-comment.md").write_text(
            f"{A.MARKER}\n### Mobster checks: 0 passed, 1 failed\n\n| Check | Device | Expected | Found | Frame |\n"
            "| --- | --- | --- | --- | --- |\n| **Failed** Paywall | iPhone | x | y | `paywall.jpg` |\n\n"
            "GIF of the failure: `paywall.gif`, in the suite folder.\n\n"
            "<sub>Tested with [Mobster](https://mobster.dev/s/pr)</sub>\n")

    def environ(self, **extra):
        return {"GITHUB_EVENT_PATH": str(self.event), "GITHUB_REPOSITORY": "o/r", "GITHUB_STEP_SUMMARY":
                str(self.summary), "GITHUB_OUTPUT": str(self.output), "INPUT_GITHUB_TOKEN": "t0ken",
                "MOBSTER_REPORT_DIR": str(self.report), **extra}


class ActionFileTests(unittest.TestCase):
    def test_action_yml_names_the_script_steps_and_passes_every_input_it_reads(self):
        import yaml
        action = yaml.safe_load((ACTION / "action.yml").read_text())
        self.assertEqual(action["runs"]["using"], "composite")
        self.assertIn("INPUT_ARGS", action["runs"]["steps"][0]["env"])
        runs = " ".join(step.get("run", "") for step in action["runs"]["steps"])
        for step in ("guard", "run", "comment", "finish"):
            self.assertIn(f'action.py" {step}', runs)
        uses = [step["uses"] for step in action["runs"]["steps"] if "uses" in step]
        self.assertEqual(uses, ["actions/upload-artifact@v4"])
        passed = {key for step in action["runs"]["steps"] for key in (step.get("env") or {})}
        script = (ACTION / "action.py").read_text()
        for name in passed:
            self.assertIn(name.replace("INPUT_", "INPUT_"), script, name)
        for name in action["inputs"]:
            self.assertTrue(any(f"inputs.{name}" in str(step) for step in action["runs"]["steps"]), name)


class GuardTests(unittest.TestCase):
    def test_extra_flags_cannot_bypass_the_device_or_testflight_inputs(self):
        for args in ('--device "Other iPhone"', '--device=Other', '--testflight com.example.other',
                     '--testflight=com.example.other', '--retries "2'):
            with self.subTest(args=args):
                self.assertEqual(A.guard({"INPUT_ARGS": args}), 1)
        self.assertEqual(A.guard({"INPUT_ARGS": "--retries 2 --parallel 2"}), 0)

    def test_a_device_or_testflight_app_runs_only_when_the_runner_lists_it(self):
        self.assertEqual(A.guard({}), 0)
        self.assertEqual(A.guard({"INPUT_DEVICE": "Test iPhone"}), 1)
        self.assertEqual(A.guard({"INPUT_DEVICE": "Test iPhone", "MOBSTER_RUNNER_DEVICES": "Lab, test iphone"}), 0)
        both = {"INPUT_DEVICE": "Test iPhone\nLab", "MOBSTER_RUNNER_DEVICES": "Lab, test iphone"}
        self.assertEqual(A.guard(both), 0)
        for devices in ("Test iPhone\nLab", "Lab\nTest iPhone"):  # one phone the runner doesn't list stops them all
            with self.subTest(devices=devices), contextlib.redirect_stdout(io.StringIO()) as said:
                self.assertEqual(A.guard({"INPUT_DEVICE": devices, "MOBSTER_RUNNER_DEVICES": "Test iPhone"}), 1)
            self.assertIn('doesn\'t allow the iPhone "Lab", so nothing ran.', said.getvalue())
        allowed = {"INPUT_DEVICE": "Test iPhone", "MOBSTER_RUNNER_DEVICES": "Test iPhone"}
        self.assertEqual(A.guard({**allowed, "INPUT_TESTFLIGHT": "org.mozilla.ios.Firefox"}), 1)
        self.assertEqual(A.guard({**allowed, "INPUT_TESTFLIGHT": "org.mozilla.ios.Firefox",
                                  "MOBSTER_RUNNER_TESTFLIGHT": "org.mozilla.ios.Firefox"}), 0)
        self.assertEqual(A.guard({"INPUT_TESTFLIGHT": "org.mozilla.ios.Firefox",
                                  "MOBSTER_RUNNER_TESTFLIGHT": "org.mozilla.ios.Firefox"}), 1)  # no device


class RunTests(Env):
    def test_run_refuses_unlisted_devices_before_calling_mobster(self):
        from unittest.mock import Mock
        runner = Mock()
        for inputs in ({"INPUT_DEVICE": "Other iPhone"}, {"INPUT_ARGS": "--device=Other"}):
            self.assertEqual(A.run(self.environ(**inputs), runner=runner), 0)
        runner.assert_not_called()
        self.assertIn("exit-code=3\n", self.output.read_text())
        runner.return_value = (0, json.dumps({"suite": "allowed", "headline": "Passed"}))
        A.run(self.environ(INPUT_DEVICE="Lab iPhone", MOBSTER_RUNNER_DEVICES="Lab iPhone"), runner=runner)
        runner.assert_called_once()

    def test_inputs_become_flags(self):
        argv = A.argv_for({"INPUT_BASE": "origin/main", "INPUT_SIM": "iPhone 17 Pro\niPhone SE (3rd generation)",
                           "INPUT_DEVICE": "Test iPhone\nLab iPhone", "INPUT_TESTFLIGHT": "a.b, c.d",
                           "INPUT_BUILD": "false", "INPUT_SCHEME": "Firefox", "INPUT_DRAFTS": "write",
                           "INPUT_TESTED_WITH": "true", "INPUT_ARGS": "--retries 2"})
        self.assertEqual(argv, ["mobster", "test", "pr", "--json", "--base", "origin/main", "--sim", "iPhone 17 Pro",
                                "--sim", "iPhone SE (3rd generation)", "--device", "Test iPhone", "--device",
                                "Lab iPhone", "--testflight", "a.b", "--testflight", "c.d", "--scheme", "Firefox",
                                "--drafts", "write", "--no-build", "--tested-with", "--retries", "2"])
        self.assertNotIn("--tested-with", A.argv_for({}))  # the line is opt-in
        self.assertNotIn("--tested-with", A.argv_for({"INPUT_TESTED_WITH": "false"}))

    def test_run_sets_the_outputs_from_mobsters_json(self):
        (self.folder / ".mobster").mkdir()
        environ = self.environ(INPUT_WORKING_DIRECTORY=str(self.folder))
        pr = {"suite": "20261009-120000-abcd", "headline": "Mobster checks: 1 passed"}
        self.assertEqual(A.run(environ, runner=lambda argv, cwd: (1, json.dumps(pr) + "\n")), 0)
        output = self.output.read_text()
        self.assertIn("exit-code=1\n", output)
        self.assertIn("headline=Mobster checks: 1 passed\n", output)
        self.assertIn(f"report-dir={self.folder / '.mobster' / 'test-results' / pr['suite']}\n", output)

    def test_no_report_leaves_report_dir_empty(self):
        A.run(self.environ(), runner=lambda argv, cwd: (127, ""))
        self.assertIn("report-dir=\n", self.output.read_text())


class CommentTests(Env):
    def test_the_first_run_creates_the_comment_with_links_to_the_artifact(self):
        github = FakeGitHub([{"id": 1, "body": "LGTM"}])
        environ = self.environ(MOBSTER_ARTIFACT_URL="https://github.com/o/r/actions/runs/9/artifacts/5")
        self.assertEqual(A.comment(environ, opener=github), 0)
        method, url, body = github.calls[-1]
        self.assertEqual((method, url), ("POST", "https://api.github.com/repos/o/r/issues/7/comments"))
        text = body["body"]
        self.assertTrue(text.startswith(A.MARKER))
        self.assertIn("| [paywall.jpg](https://github.com/o/r/actions/runs/9/artifacts/5) |", text)
        self.assertIn("GIF of the failure: [paywall.gif](https://github.com/o/r/actions/runs/9/artifacts/5), "
                      "[in the report](https://github.com/o/r/actions/runs/9/artifacts/5).", text)
        self.assertIn("[Report, frames and GIFs](https://github.com/o/r/actions/runs/9/artifacts/5)\n\n<sub>Tested "
                      "with", text)
        self.assertIn("### Mobster checks: 0 passed, 1 failed", self.summary.read_text())
        self.assertNotIn(A.MARKER, self.summary.read_text())

    def test_later_runs_update_the_one_comment_even_past_the_first_page(self):
        comments = [{"id": n, "body": "chatter"} for n in range(150)] + [{"id": 999, "body": A.MARKER + "\nold"}]
        github = FakeGitHub(comments)
        A.comment(self.environ(), opener=github)
        self.assertEqual([call[0] for call in github.calls], ["GET", "GET", "PATCH"])
        self.assertEqual(github.calls[-1][1], "https://api.github.com/repos/o/r/issues/comments/999")

    def test_a_read_only_token_warns_and_never_fails_the_job(self):
        out = io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(out):
            self.assertEqual(A.comment(self.environ(), opener=FakeGitHub(fail=403)), 0)
        self.assertIn("::warning title=Mobster::Mobster couldn't comment on the PR", out.getvalue())

    def test_outside_a_pr_or_with_comment_off_only_the_summary_is_written(self):
        self.event.write_text(json.dumps({"push": {}}))
        github = FakeGitHub()
        A.comment(self.environ(), opener=github)
        self.assertEqual(github.calls, [])
        self.event.write_text(json.dumps({"pull_request": {"number": 7}}))
        A.comment(self.environ(INPUT_COMMENT="false"), opener=github)
        self.assertEqual(github.calls, [])
        self.assertIn("Mobster checks", self.summary.read_text())

    def test_when_mobster_wrote_no_report_the_comment_says_it_couldnt_run(self):
        github = FakeGitHub()
        A.comment(self.environ(MOBSTER_REPORT_DIR="", MOBSTER_EXIT_CODE="3", GITHUB_RUN_ID="42"), opener=github)
        self.assertIn("### Mobster checks couldn't run", github.calls[-1][2]["body"])
        self.assertIn("/o/r/actions/runs/42", github.calls[-1][2]["body"])


class FinishTests(unittest.TestCase):
    def test_the_job_fails_with_mobsters_code_unless_told_not_to(self):
        self.assertEqual(A.finish({"MOBSTER_EXIT_CODE": "0"}), 0)
        self.assertEqual(A.finish({"MOBSTER_EXIT_CODE": "1"}), 1)
        self.assertEqual(A.finish({"MOBSTER_EXIT_CODE": ""}), 3)
        self.assertEqual(A.finish({"MOBSTER_EXIT_CODE": "1", "INPUT_FAIL_ON_FAILURE": "false"}), 0)


class CommentFormatTests(unittest.TestCase):
    def test_the_action_links_what_mobster_writes(self):
        from mobile_agent.testkit import pr_report as R
        pr = {"headline": "Mobster checks: 0 passed, 1 failed", "testedWith": True, "plan": {},
              "rows": [{"check": "Paywall", "device": "iPhone", "status": "failed", "expected": "x", "found": "y",
                        "frame": "pr/frames/paywall.jpg", "gif": "pr/gifs/paywall.gif"}]}
        linked = A.link_artifacts(R.comment(pr), "https://example.com/a")
        self.assertEqual(linked.count("](https://example.com/a)"), 6)
        self.assertEqual(A.MARKER, R.MARKER)

    def test_a_team_key_shares_the_report_and_its_comment_keeps_the_hosted_links(self):
        from mobile_agent.testkit import pr_report as R
        self.assertIn("--share", A.argv_for({"MOBSTER_TEAM_KEY": "x:mtk_y"}))
        self.assertNotIn("--share", A.argv_for({}))
        self.assertNotIn("--share", A.argv_for({"MOBSTER_TEAM_KEY": "  "}))
        pr = {"headline": "h", "testedWith": False, "plan": {},
              "rows": [{"check": "Paywall", "device": "iPhone", "status": "failed", "expected": "x", "found": "y",
                        "frame": "pr/frames/paywall.jpg"}]}
        link = "https://console.mobster.dev/r/6b1f0a52-3c1e-4d7a-9a40-5be0c2d9e7f1#k=" + "A" * 43
        hosted = R.comment(pr, artifact_url=link, file_url=lambda path: f"{link}&p={path}")
        self.assertIn(f"]({link}&p=pr/frames/paywall.jpg)", hosted)
        self.assertEqual(A.link_artifacts(hosted, "https://example.com/a"), hosted)


if __name__ == "__main__":
    unittest.main()


class ActionReferenceTests(unittest.TestCase):
    FOREIGN = "mobster" + "-dev/"       # another person's GitHub account, not ours: never a `uses:` line

    def test_no_file_names_the_foreign_organization_and_the_action_is_ours(self):
        import subprocess
        files = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout.split(b"\0")
        found = []
        for name in filter(None, files):
            path = ROOT / name.decode()
            try:
                if path.is_file() and self.FOREIGN in path.read_text(encoding="utf-8", errors="ignore").lower():
                    found.append(name.decode())
            except OSError:
                continue
        self.assertEqual(found, [], "these files name an action in an account that isn't Mobster's")
        readme = (ACTION / "README.md").read_text()
        self.assertEqual(readme.count("uses: RadishSoftware/test-action@v1"), 1)
