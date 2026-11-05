
import contextlib
import io
import json
import shlex
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import yaml

from mobile_agent import plan_gate
from mobile_agent.devkit import DevError, ci, ci_cli
from mobile_agent.testkit import pr_report as R


class Temp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class InitTests(Temp):
    def test_github_workflow_runs_on_a_mobster_runner_and_comments_after_the_upload(self):
        path, notes = ci.init(self.root, host="github")
        self.assertEqual(path, self.root / ".github/workflows/mobster.yml")
        self.assertEqual(notes, [])
        flow = yaml.safe_load(path.read_text())
        job = flow["jobs"]["checks"]
        self.assertEqual(job["runs-on"], ["self-hosted", "macOS", "mobster"])
        self.assertEqual(flow["permissions"], {"contents": "read", "pull-requests": "write"})
        names = [step.get("name") or step.get("uses") for step in job["steps"]]
        self.assertEqual(names, ["actions/checkout@v4", "Test what this PR changed", "Upload the report",
                                 "Comment on the PR"])
        self.assertEqual(job["steps"][0]["with"]["fetch-depth"], 0)
        self.assertIn('"$MOBSTER" ci run', job["steps"][1]["run"])
        self.assertEqual(job["steps"][2]["if"], "always()")
        comment = job["steps"][3]
        self.assertEqual(comment["env"]["MOBSTER_ARTIFACT_URL"], "${{ steps.upload.outputs.artifact-url }}")
        self.assertNotIn("${{", comment["run"])
        self.assertEqual(flow["env"]["MOBSTER"], ci.APP_AGENT)

    def test_gitlab_job_reports_junit_and_comments_in_after_script(self):
        path, _ = ci.init(self.root, host="gitlab", devices=("Studio iPhone",), testflight=("com.example.app",))
        self.assertEqual(path, self.root / ".gitlab-ci.yml")
        job = yaml.safe_load(path.read_text())["mobster"]
        self.assertEqual(job["tags"], ["mobster"])
        self.assertEqual(job["artifacts"]["reports"]["junit"], ".mobster/ci/junit.xml")
        self.assertEqual(job["artifacts"]["when"], "always")
        self.assertEqual(job["variables"]["GIT_DEPTH"], "0")
        self.assertEqual(job["script"][0].strip(),
                         '"$MOBSTER" ci run --device \'Studio iPhone\' --testflight com.example.app')
        self.assertEqual(job["after_script"][0].strip(), '"$MOBSTER" ci comment')

    def test_an_existing_gitlab_file_gets_an_include_and_nothing_is_overwritten_without_force(self):
        (self.root / ".gitlab-ci.yml").write_text("stages: [build]\n")
        path, notes = ci.init(self.root, host="gitlab")
        self.assertEqual(path, self.root / ".gitlab/mobster.gitlab-ci.yml")
        self.assertIn("- local: .gitlab/mobster.gitlab-ci.yml", notes[0])
        self.assertEqual((self.root / ".gitlab-ci.yml").read_text(), "stages: [build]\n")
        with self.assertRaises(DevError) as caught:
            ci.init(self.root, host="gitlab")
        self.assertIn("--force", caught.exception.fix)
        ci.init(self.root, host="gitlab", force=True)
        with self.assertRaises(DevError):
            ci.init(self.root, host=None)

    def test_the_starter_kit_holds_what_init_writes(self):
        kit = Path(__file__).resolve().parents[2] / "examples" / "dev"
        if not kit.is_dir():
            self.skipTest("examples are not part of this copy")
        self.assertEqual((kit / "github" / "mobster.yml").read_text(), ci.github_workflow())
        self.assertEqual((kit / "gitlab" / "mobster.gitlab-ci.yml").read_text(), ci.gitlab_job())


class GuardTests(unittest.TestCase):
    def test_a_job_reaches_only_the_phones_and_testflight_apps_the_runner_lists(self):
        job = {"GITHUB_ACTIONS": "true", "MOBSTER_RUNNER_DEVICES": "Test iPhone, Lab iPhone",
               "MOBSTER_RUNNER_TESTFLIGHT": "com.example.app"}
        self.assertIsNone(ci.guard(("test iphone",), ("com.example.app",), job))
        self.assertIsNone(ci.guard(("Test iPhone", "lab iphone"), (), job))
        with self.assertRaises(DevError) as caught:
            ci.guard(("Alex's iPhone",), (), job)
        self.assertIn("MOBSTER_RUNNER_DEVICES", caught.exception.fix)
        with self.assertRaises(DevError):
            ci.guard(("Test iPhone",), ("com.other.app",), job)
        with self.assertRaises(DevError):
            ci.guard(("Test iPhone",), (), {"GITLAB_CI": "true"})

    def test_one_phone_the_runner_doesnt_list_stops_the_listed_ones_too(self):
        job = {"GITHUB_ACTIONS": "true", "MOBSTER_RUNNER_DEVICES": "Test iPhone"}
        with self.assertRaises(DevError) as caught:
            ci.guard(("Test iPhone", "Alex's iPhone"), (), job)
        self.assertEqual(caught.exception.message,
                         "This runner doesn't allow the iPhone \"Alex's iPhone\", so nothing ran.")

    def test_a_terminal_is_the_owner(self):
        self.assertIsNone(ci.guard(("Alex's iPhone",), ("com.other.app",), {}))


class PhoneFlagsTests(Temp):
    """The phones of `mobster ci run` and `ci init`: several, each on the runner's own list, named as YAML holds."""

    def ci(self, *argv, environ=None):
        """(exit code, stdout, stderr, each `mobster test pr` it would start) for `mobster ci ARGV`."""
        from mobile_agent.cli_parser import build_parser
        started = []

        def run(argv, **kwargs):
            started.append(list(argv))
            return mock.Mock(returncode=3, stdout="")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(ci.subprocess, "run", side_effect=run), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = ci_cli.run(build_parser().parse_args(["ci", *argv]), cwd=self.root, environ=environ or {})
        tests = [argv[argv.index("test"):] for argv in started if argv[0] != "git"]
        return code, out.getvalue(), err.getvalue(), tests

    def test_ci_run_tests_on_every_phone_named_when_the_runner_lists_each(self):
        job = {"GITHUB_ACTIONS": "true", "MOBSTER_RUNNER_DEVICES": "Test iPhone, Lab iPhone"}
        _, _, _, tests = self.ci("run", "--device", "Test iPhone", "--device", "Lab iPhone", "--no-video", "--",
                                 "--spread", environ=job)
        self.assertEqual(tests, [["test", "pr", "--json", "--device", "Test iPhone", "--device", "Lab iPhone",
                                  "--spread"]])

    def test_ci_run_runs_nothing_when_one_named_phone_isnt_on_the_runners_list(self):
        job = {"GITHUB_ACTIONS": "true", "MOBSTER_RUNNER_DEVICES": "Test iPhone"}
        code, _, err, tests = self.ci("run", "--device", "Alex's iPhone", "--device", "Test iPhone", environ=job)
        self.assertEqual((code, tests), (3, []))
        self.assertIn("doesn't allow the iPhone \"Alex's iPhone\", so nothing ran.", err)

    def test_ci_init_writes_every_phone_into_the_workflow_and_the_runners_list(self):
        code, out, _, _ = self.ci("init", "--host", "gitlab", "--device", "Test iPhone", "--device", "Lab iPhone")
        self.assertEqual(code, 0)
        job = yaml.safe_load((self.root / ".gitlab-ci.yml").read_text())["mobster"]
        self.assertEqual(job["script"][0].strip(), '"$MOBSTER" ci run --device \'Test iPhone\' --device \'Lab iPhone\'')
        self.assertIn("On the runner, allow the phones: MOBSTER_RUNNER_DEVICES=Test iPhone,Lab iPhone", out)

    def test_a_phone_name_with_quotes_and_emoji_reaches_the_workflow_intact(self):
        name = "Alex\u2019s iPhone \U0001F468\u200D\U0001F469\u200D\U0001F467 $(id) `x`"
        code, _, _, _ = self.ci("init", "--host", "github", "--device", name, "--testflight", "com.example.app")
        self.assertEqual(code, 0)
        run = yaml.safe_load((self.root / ".github/workflows/mobster.yml").read_text())["jobs"]["checks"]["steps"][1]
        self.assertEqual(shlex.split(run["run"].strip()),
                         ["$MOBSTER", "ci", "run", "--device", name, "--testflight", "com.example.app"])

    def test_a_line_break_tab_or_control_character_in_a_phone_or_app_is_refused_before_anything_is_written(self):
        for flags in (("--device", "Test\niPhone"), ("--device", "Test\u2028iPhone"), ("--device", "Test\tiPhone"),
                      ("--device", "Test iPhone", "--testflight", "com.example\r.app")):
            with self.subTest(flags=flags):
                code, out, err, _ = self.ci("init", "--host", "gitlab", *flags)
                self.assertEqual((code, out), (3, ""))
                self.assertIn("holds a line break, a tab or another control character, so Mobster did nothing.", err)
                self.assertFalse((self.root / ".gitlab-ci.yml").exists())
        code, _, _, tests = self.ci("run", "--device", "Test\niPhone")
        self.assertEqual((code, tests), (3, []))

    def test_print_refuses_what_init_refuses(self):
        for flags, said in ((("--host", "github", "--hosted", "--device", "Test iPhone"),
                             "A hosted runner has no iPhone to test on"),
                            (("--host", "circleci", "--device", "Test iPhone"), "A hosted runner has no iPhone"),
                            (("--host", "gitlab", "--hosted"), "Mobster has no hosted runner setup for GitLab CI")):
            with self.subTest(flags=flags):
                code, out, err, _ = self.ci("init", "--print", *flags)
                self.assertEqual((code, out), (3, ""))
                self.assertIn(said, err)
        code, out, _, _ = self.ci("init", "--print", "--host", "github", "--device", "Test iPhone")
        self.assertEqual(code, 0)
        self.assertIn("ci run --device 'Test iPhone'", out)
        self.assertFalse((self.root / ".github").exists())


def suite_folder(root, suite="s1", exit_code=1):
    folder = root / ".mobster" / "test-results" / suite
    (folder / "pr" / "gifs").mkdir(parents=True)
    (folder / "videos").mkdir()
    (folder / "junit.xml").write_text("<testsuites/>")
    (folder / "pr" / "gifs" / "paywall.gif").write_bytes(b"GIF89a")
    (folder / "videos" / "001-paywall-1.mp4").write_bytes(b"mp4")
    pr = {"schema": R.SCHEMA, "suite": suite, "headline": "Mobster checks: 0 passed, 1 failed",
          "exitCode": exit_code, "testedWith": True, "plan": {"screens": [{"name": "PaywallView"}]},
          "rows": [{"check": "Paywall", "file": ".mobster/checks/paywall.yaml", "device": "iPhone 17 Pro · iOS 26.4",
                    "deviceKey": "sim:iPhone 17 Pro@iOS 26.4", "status": "failed", "expected": "text “Pro”",
                    "found": "not on screen", "frame": "pr/frames/paywall.jpg", "gif": "pr/gifs/paywall.gif",
                    "video": "videos/001-paywall-1.mp4", "steps": ["Tap Upgrade"], "openUrl": "firefox://paywall",
                    "repro": "mobster test .mobster/checks/paywall.yaml --sim 'iPhone 17 Pro@iOS 26.4'"}]}
    (folder / "pr.json").write_text(json.dumps(pr))
    return pr


class RunTests(Temp):
    def test_the_run_tests_the_pr_against_the_jobs_target_branch_and_keeps_the_report_in_mobster_ci(self):
        pr = suite_folder(self.root)
        calls, git = [], []
        output = self.root / "github_output"

        def runner(argv, cwd, env):
            calls.append((argv, cwd, env))
            return 1, "progress\n" + json.dumps(pr) + "\n"

        def fake_git(argv, cwd):
            git.append(argv)
            return mock.Mock(returncode=0, stdout="false")
        environ = {"GITHUB_ACTIONS": "true", "GITHUB_BASE_REF": "main", "GITHUB_OUTPUT": str(output),
                   "MOBSTER_RUNNER_DEVICES": "Test iPhone"}
        code, doc = ci.run_checks(self.root, devices=("Test iPhone",), environ=environ, runner=runner, git=fake_git)
        self.assertEqual(code, 1)
        argv = calls[0][0]
        tail = argv[argv.index("test"):]
        self.assertEqual(tail, ["test", "pr", "--json", "--base", "origin/main", "--device", "Test iPhone", "--video"])
        self.assertIn(["git", "fetch", "--quiet", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main"],
                      git)
        folder = self.root / ".mobster" / "ci"
        self.assertEqual((folder / "junit.xml").read_text(), "<testsuites/>")
        self.assertTrue((folder / "videos" / "001-paywall-1.mp4").is_file())
        self.assertEqual(json.loads((folder / "ci.json").read_text())["headline"], "Mobster checks: 0 passed, 1 failed")
        self.assertEqual(output.read_text(), "exit-code=1\nheadline=Mobster checks: 0 passed, 1 failed\n")
        self.assertEqual(doc["host"], "github")

    def test_a_run_that_wrote_no_report_still_leaves_ci_json_and_an_unlisted_phone_runs_nothing(self):
        code, doc = ci.run_checks(self.root, environ={}, base="origin/main", runner=lambda *a: (3, "boom"))
        self.assertEqual((code, doc["suite"]), (3, None))
        self.assertEqual(json.loads((self.root / ".mobster/ci/ci.json").read_text())["exitCode"], 3)
        ran = []
        with self.assertRaises(DevError):
            ci.run_checks(self.root, devices=("Alex's iPhone",), environ={"GITLAB_CI": "true"},
                          runner=lambda *a: ran.append(a) or (0, ""))
        self.assertEqual(ran, [])

    def test_files_mobster_didnt_write_are_never_deleted(self):
        stray = self.root / ".mobster" / "ci" / "notes.txt"
        stray.parent.mkdir(parents=True)
        stray.write_text("mine")
        suite_folder(self.root)
        with self.assertRaises(DevError):
            ci.collect(self.root, "s1", self.root / ".mobster" / "ci")
        self.assertEqual(stray.read_text(), "mine")

    def test_a_run_that_couldnt_run_leaves_files_mobster_didnt_write_alone(self):
        stray = self.root / ".mobster" / "ci" / "notes.txt"
        stray.parent.mkdir(parents=True)
        stray.write_text("mine")
        with self.assertRaises(DevError) as caught:
            ci.run_checks(self.root, environ={}, base="origin/main", runner=lambda *a: (3, "boom"))
        self.assertIn(".mobster/ci", caught.exception.message)
        self.assertFalse((stray.parent / "ci.json").exists())
        suite_folder(self.root)
        pr = {"suite": "s1", "headline": "h"}
        with self.assertRaises(DevError):
            ci.run_checks(self.root, environ={}, base="origin/main", runner=lambda *a: (1, json.dumps(pr)))
        self.assertEqual(stray.read_text(), "mine")

    def test_a_team_key_shares_the_report_and_nothing_else_does(self):
        suite_folder(self.root)
        pr = json.loads((self.root / ".mobster/test-results/s1/pr.json").read_text())
        seen = []

        def runner(argv, cwd, env):
            seen.append(argv)
            return 0, json.dumps(pr)
        ci.run_checks(self.root, environ={"MOBSTER_TEAM_KEY": "x:mtk_y"}, base="origin/main", runner=runner)
        ci.run_checks(self.root, environ={"MOBSTER_TEAM_KEY": " "}, base="origin/main", runner=runner)
        self.assertEqual(["--share" in argv for argv in seen], [True, False])


class Opener:
    def __init__(self, routes):
        self.routes, self.requests = routes, []

    def __call__(self, request, timeout=None):
        path = request.full_url.split("/api/v4", 1)[-1].split("api.github.com", 1)[-1]
        self.requests.append((request.get_method(), path, json.loads(request.data) if request.data else None,
                              dict(request.header_items())))
        answer = self.routes.get((request.get_method(), path.split("?")[0]))
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(json.dumps(answer if answer is not None else {}).encode())


class CommentTests(Temp):
    def gitlab(self):
        return {"GITLAB_CI": "true", "CI_JOB_URL": "https://gitlab.example.com/o/r/-/jobs/7",
                "CI_PROJECT_URL": "https://gitlab.example.com/o/r", "CI_JOB_ID": "7", "CI_PROJECT_ID": "42",
                "CI_MERGE_REQUEST_IID": "5", "CI_API_V4_URL": "https://gitlab.example.com/api/v4",
                "MOBSTER_GITLAB_TOKEN": "glpat-secret"}

    def test_gitlab_links_each_file_of_the_jobs_artifacts(self):
        suite_folder(self.root)
        ci.run_checks(self.root, environ={}, base="origin/main",
                      runner=lambda *a: (1, json.dumps(json.loads((self.root / ".mobster/test-results/s1/pr.json")
                                                                  .read_text()))))
        body = ci.comment_markdown(self.root, self.gitlab())
        files = "https://gitlab.example.com/o/r/-/jobs/7/artifacts/file/.mobster/ci/"
        self.assertIn(f"[paywall.jpg]({files}pr/frames/paywall.jpg)", body)
        self.assertIn(f"Video: [001-paywall-1.mp4]({files}videos/001-paywall-1.mp4)", body)
        self.assertIn("[Report, frames and "
                      "GIFs](https://gitlab.example.com/o/r/-/jobs/7/artifacts/browse/.mobster/ci/)",
                      body)

    def test_a_shared_report_is_what_the_comment_links_on_any_host(self):
        suite_folder(self.root)
        ci.collect(self.root, "s1", self.root / ".mobster" / "ci")
        folder = self.root / ".mobster" / "ci"
        (folder / "ci.json").write_text(json.dumps({"exitCode": 1}))
        link = "https://console.mobster.dev/r/6b1f0a52-3c1e-4d7a-9a40-5be0c2d9e7f1#k=" + "A" * 43
        pr = json.loads((folder / "pr.json").read_text())
        (folder / "pr.json").write_text(json.dumps({**pr, "hostedReport": link}))
        for environ in ({"GITHUB_ACTIONS": "true", "MOBSTER_ARTIFACT_URL": "https://github.com/o/r/artifacts/9"},
                        self.gitlab()):
            body = ci.comment_markdown(self.root, environ)
            self.assertIn(f"[paywall.jpg]({link}&p=pr/frames/paywall.jpg)", body)
            self.assertIn(f"[Report, frames and GIFs]({link})", body)
            self.assertNotIn("artifacts", body)
        (folder / "pr.json").write_text(json.dumps({**pr, "hostedReport": "https://example.com/x"}))
        self.assertIn("https://github.com/o/r/artifacts/9", ci.comment_markdown(
            self.root, {"GITHUB_ACTIONS": "true", "MOBSTER_ARTIFACT_URL": "https://github.com/o/r/artifacts/9"}))

    def test_github_links_the_uploaded_report_and_a_missing_report_says_the_run_failed(self):
        suite_folder(self.root)
        ci.collect(self.root, "s1", self.root / ".mobster" / "ci")
        (self.root / ".mobster/ci/ci.json").write_text(json.dumps({"exitCode": 1}))
        body = ci.comment_markdown(self.root, {"GITHUB_ACTIONS": "true",
                                               "MOBSTER_ARTIFACT_URL": "https://github.com/o/r/actions/runs/1/artifacts/9"})
        self.assertIn("[paywall.gif](https://github.com/o/r/actions/runs/1/artifacts/9)", body)
        (self.root / ".mobster/ci/pr.json").unlink()
        body = ci.comment_markdown(self.root, {"GITHUB_ACTIONS": "true", "GITHUB_RUN_ID": "1",
                                               "GITHUB_REPOSITORY": "o/r"})
        self.assertIn("### Mobster checks couldn't run", body)
        self.assertIn("(https://github.com/o/r/actions/runs/1)", body)
        with self.assertRaises(DevError):
            ci.comment_markdown(self.root / "elsewhere", {})

    def test_github_updates_the_one_comment_that_carries_the_marker(self):
        event = self.root / "event.json"
        event.write_text(json.dumps({"pull_request": {"number": 12}}))
        opener = Opener({("GET", "/repos/o/r/issues/12/comments"): [{"id": 3, "body": "lgtm"},
                                                                    {"id": 4, "body": ci.MARKER + "\nold"}],
                         ("PATCH", "/repos/o/r/issues/comments/4"): {"html_url": "u"}})
        environ = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_PATH": str(event), "GITHUB_TOKEN": "ghs_secret",
                   "GITHUB_REPOSITORY": "o/r"}
        line = ci.post(ci.MARKER + "\nnew", environ, opener)
        self.assertEqual(line, "Updated the PR comment.")
        self.assertEqual(opener.requests[-1][:3],
                         ("PATCH", "/repos/o/r/issues/comments/4", {"body": ci.MARKER + "\nnew"}))
        self.assertNotIn("ghs_secret", line)

    def test_gitlab_creates_a_note_skipping_system_notes_and_uses_the_private_token(self):
        opener = Opener({("GET", "/projects/42/merge_requests/5/notes"): [
            {"id": 1, "system": True, "body": ci.MARKER}, {"id": 2, "body": "nice"}],
            ("POST", "/projects/42/merge_requests/5/notes"): {"id": 9}})
        line = ci.post(ci.MARKER + "\nbody", self.gitlab(), opener)
        self.assertEqual(line, "Created the MR comment.")
        method, path, body, headers = opener.requests[-1]
        self.assertEqual((method, path, body), ("POST", "/projects/42/merge_requests/5/notes",
                                                {"body": ci.MARKER + "\nbody"}))
        self.assertEqual(headers["Private-token"], "glpat-secret")

    def test_a_comment_never_fails_the_job(self):
        self.assertIn("MOBSTER_GITLAB_TOKEN", ci.post("x", {**self.gitlab(), "MOBSTER_GITLAB_TOKEN": ""}))
        refused = urllib.error.HTTPError("u", 403, "no", {}, None)
        opener = Opener({("GET", "/projects/42/merge_requests/5/notes"): refused})
        self.assertIn("answered 403", ci.post("x", self.gitlab(), opener))
        self.assertEqual(ci.post("x", {}), "Not in a CI job, so Mobster posted nothing.")
        self.assertIn("Not a pull request", ci.post("x", {"GITHUB_ACTIONS": "true"}))


class DoctorTests(Temp):
    def outputs(self, **over):
        answers = {"xcodebuild": (0, "Xcode 26.4\nBuild version 17E1"),
                   "xcrun": (0, json.dumps({"runtimes": [{"name": "iOS 26.4", "isAvailable": True},
                                                         {"name": "watchOS 26.4", "isAvailable": True}]})),
                   "pmset": (0, "System-wide power settings:\n sleep                0\n autorestart          1\n"),
                   "defaults": (0, "alex\n"), "launchctl": (0, "123\t0\tactions.runner.o-r.mac-mini\n")}
        answers.update(over)
        return lambda argv: answers[argv[0]]

    def doctor(self, **over):
        agent = self.root / "mobster-agent"
        agent.write_text("")
        return {row.name: row for row in ci.doctor(run=self.outputs(**over), home=self.root, environ={},
                                                   machine="arm64", plan=lambda: None, agent=str(agent),
                                                   disk=lambda path: 80e9)}

    def test_a_ready_mac(self):
        rows = self.doctor()
        self.assertTrue(all(row.status == "ok" for row in rows.values()), [r.public() for r in rows.values()])
        self.assertEqual(rows["iOS Simulator runtime"].detail, "iOS 26.4")
        self.assertEqual(rows["Runner service"].detail, "GitHub Actions runner")

    def test_sleep_and_a_missing_xcode_fail_with_the_fix(self):
        rows = self.doctor(pmset=(0, " sleep 10\n"), xcodebuild=(1, ""))
        self.assertEqual(rows["System sleep"].status, "fail")
        self.assertIn("sudo pmset -a sleep 0", rows["System sleep"].fix)
        self.assertEqual(rows["Xcode"].status, "fail")
        self.assertEqual(rows["Restart after a power cut"].status, "warn")


class PlanTests(unittest.TestCase):
    def test_only_ci_run_needs_the_plan(self):
        from types import SimpleNamespace
        self.assertTrue(plan_gate.cli_needs_plan("ci", SimpleNamespace(ci_command="run")))
        for verb in ("init", "doctor", "comment"):
            self.assertFalse(plan_gate.cli_needs_plan("ci", SimpleNamespace(ci_command=verb)))


class CliTests(Temp):
    def parse(self, *argv):
        from mobile_agent.cli_parser import build_parser
        return build_parser().parse_args(["ci", *argv])

    def test_init_prints_the_next_steps_and_testflight_needs_a_device(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = ci_cli.run(self.parse("init", "--host", "github"), cwd=self.root, environ={})
        self.assertEqual(code, 0)
        self.assertIn("Wrote .github/workflows/mobster.yml.", out.getvalue())
        self.assertIn("1. On the Mac that runs the checks: mobster ci doctor", out.getvalue())
        self.assertIn("This project has no checks yet.", out.getvalue())
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            code = ci_cli.run(self.parse("init", "--host", "gitlab", "--testflight", "com.x"), cwd=self.root,
                              environ={})
        self.assertEqual(code, 3)
        self.assertIn("--testflight goes with --device", err.getvalue())

    def test_comment_outside_a_job_prints_and_posts_nothing(self):
        suite_folder(self.root)
        ci.collect(self.root, "s1", self.root / ".mobster" / "ci")
        (self.root / ".mobster/ci/ci.json").write_text("{}")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = ci_cli.run(self.parse("comment"), cwd=self.root, environ={})
        self.assertEqual(code, 0)
        self.assertIn("<details><summary>Repro: Paywall (failed)</summary>", out.getvalue())
        self.assertIn("posted nothing", err.getvalue())
        self.assertTrue((self.root / ".mobster/ci/comment.md").is_file())


class ReproTests(Temp):
    def test_repro_steps_url_and_gif_follow_the_verdict_run(self):
        runs = []
        for name in ("failed", "passed"):
            folder = self.root / name
            folder.mkdir()
            (folder / f"{name}.jpg").write_bytes(name.encode())
            (folder / "result.json").write_text(json.dumps({
                "steps": [{"text": f"{name} step", "frame": f"{name}.jpg", "at_ms": 9000}],
                "frames": [f"{name}.jpg"]}))
            (folder / "check.yaml").write_text(f"launch:\n  url: yourapp://{name}\n")
            runs.append({"runId": name, "runDir": str(folder)})
        selected = {"status": "failed", "runId": "failed", "runDir": runs[0]["runDir"],
                    "runs": runs, "reason": {"message": "The check failed."}}
        doc = {"checks": [{"name": "Paywall", "results": [selected]}]}
        row = R.document({}, doc, folder=self.root, frames={}, gifs={})["rows"][0]
        self.assertEqual((row["steps"], row["openUrl"]), (["failed step"], "yourapp://failed"))

        def render(beats, read, **_kwargs):
            self.assertEqual(beats[1].caption, "failed step")
            self.assertEqual(read(beats[1].frame_id), b"failed")
            return mock.Mock(gif=b"GIF89a")

        with mock.patch("mobile_agent.run_gif.render_clip", side_effect=render):
            self.assertEqual(R.check_gif(doc["checks"][0], selected), b"GIF89a")

        without_dir = {**selected, "runDir": None}
        self.assertEqual(R.document({}, {"checks": [{"results": [without_dir]}]}, folder=self.root,
                                    frames={}, gifs={})["rows"][0]["steps"], ["failed step"])
        missing = {**selected, "runDir": str(self.root / "missing")}
        row = R.document({}, {"checks": [{"results": [missing]}]}, folder=self.root,
                         frames={}, gifs={})["rows"][0]
        self.assertEqual((row["steps"], row["openUrl"]), ([], None))
        self.assertIsNone(R.check_gif(doc["checks"][0], missing))

    def test_failed_business_and_finding_rows_explain_the_failure(self):
        cases = [
            ("business", "1 of 2 app-state expectations failed.", [{"text": "Plan is visible", "ok": True}]),
            ("finding", "One accessibility finding failed.", [{"text": "Plan is visible", "ok": True}]),
            ("assertion", "1 of 1 expectations failed.",
             [{"text": "Plan is visible", "ok": False, "observed": "Plan was absent"}]),
        ]
        rows = []
        for klass, message, assertions in cases:
            result = {"status": "failed", "assertions": assertions,
                      "reason": {"class": klass, "message": message}}
            rows.append(R.document({}, {"checks": [{"name": klass, "results": [result]}]}, folder=self.root,
                                   frames={}, gifs={})["rows"][0])
        self.assertEqual([row["found"] for row in rows],
                         ["1 of 2 app-state expectations failed.", "One accessibility finding failed.",
                          "Plan was absent"])
        self.assertEqual([row["expected"] for row in rows[:2]], ["", ""])
        self.assertIn("the check failed: 1 of 2 app-state expectations failed.", rows[0]["why"])
        self.assertNotIn("as expected", R.comment({"rows": rows}))
        passed = {"status": "passed", "assertions": [{"text": "Plan is visible", "ok": True}]}
        self.assertEqual(R._expected_found(passed)[1], "as expected")
        self.assertEqual(R._expected_found({**passed, "status": "failed"})[1], "not confirmed")

    def test_a_failed_check_gets_its_device_link_steps_media_and_command(self):
        run_dir = self.root / "runs" / "r1"
        run_dir.mkdir(parents=True)
        (run_dir / "result.json").write_text(json.dumps({
            "check": {"name": "Paywall", "steps": ["Tap Upgrade"]},
            "steps": [{"text": "Tap Upgrade", "at_ms": 9000},
                      {"text": 'Type "hunter2" into Password', "typed": True, "at_ms": 74500}]}))
        (run_dir / "check.yaml").write_text("version: 1\nname: Paywall\nlaunch:\n  url: firefox://paywall\n")
        (self.root / "videos").mkdir()
        (self.root / "videos" / "v.mp4").write_bytes(b"x")
        doc = {"checks": [{"name": "Paywall", "file": ".mobster/checks/paywall.yaml", "results": [{
            "device": "iPhone 17 Pro · iOS 26.4", "deviceKey": "sim:iPhone 17 Pro@iOS 26.4", "status": "failed",
            "video": "videos/v.mp4", "videoOffsetMs": 4000, "runs": [{"runDir": str(run_dir)}],
            "assertions": [{"text": "text “Pro”", "ok": False, "observed": "not on screen"}]}]},
            {"name": "Today", "file": ".mobster/checks/today.yaml", "results": [{
                "device": "Test iPhone · iOS 26.4", "deviceKey": "device:00008110", "status": "couldnt_run",
                "reason": {"message": "The iPhone is locked."}}]}]}
        pr = R.document({}, doc, folder=self.root, frames={(0, 0): "pr/frames/paywall.jpg"}, gifs={})
        row = pr["rows"][0]
        self.assertEqual(row["steps"], ["Tap Upgrade", "Type “•••” into Password"])
        self.assertNotIn("hunter2", json.dumps(row))
        self.assertEqual(row["openUrl"], "firefox://paywall")
        self.assertEqual(row["video"], "videos/v.mp4")
        self.assertEqual(row["repro"], "mobster test .mobster/checks/paywall.yaml --sim 'iPhone 17 Pro@iOS 26.4'")
        self.assertEqual(pr["rows"][1]["repro"], "mobster test .mobster/checks/today.yaml --device 'Test iPhone'")
        text = R.comment(pr, artifact_url="https://x/artifacts/1")
        self.assertEqual(row["videoAt"], ["0:05", "1:10"])
        self.assertIn("1. Use iPhone 17 Pro, iOS 26.4.\n2. Open `firefox://paywall`.\n3. Tap Upgrade (video 0:05)\n"
                      "4. Type “•••” into Password (video 1:10)", text)
        self.assertIn("**Why:** After its last step, Type “•••” into Password, text “Pro” was not on screen.\n", text)
        self.assertIn("**Why:** The check couldn't run: The iPhone is locked.\n", text)
        self.assertIn("Video: [v.mp4](https://x/artifacts/1), Frame: [paywall.jpg](https://x/artifacts/1)", text)
        self.assertIn("```sh\nmobster test .mobster/checks/paywall.yaml --sim 'iPhone 17 Pro@iOS 26.4'\n```", text)
        self.assertIn("<details><summary>Repro: Today (couldn't run)</summary>", text)


if __name__ == "__main__":
    unittest.main()
