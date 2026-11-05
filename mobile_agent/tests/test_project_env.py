import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mobile_agent import api_routes, paths, project_env, project_env_cli, project_env_routes
from mobile_agent.agent_hooks import MASK
from mobile_agent.tests.test_verify_runner import (
    KEY,
    PLANS,
    Agent,
    Client,
    Driver,
    Manager,
    RunnerCase,
    recorded_events,
)

SECRET = "hunter2-Zq9xK"
EMAIL = "qa+ci@example.com"


class Memory:
    def __init__(self):
        self.data = {}

    def load(self):
        return dict(self.data)

    def save(self, record):
        self.data = dict(record)


class ProjectCase(RunnerCase):
    def setUp(self):
        super().setUp()
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        for patch in (mock.patch.dict(os.environ, {"MOBSTER_VAULT": "file"}),
                      mock.patch.object(paths, "user_data_dir", lambda: Path(home.name))):
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop("TEST_PASSWORD", None)
        os.environ.pop("TEST_EMAIL", None)
        (self.root / ".mobster").mkdir(exist_ok=True)
        (self.root / ".mobster" / "env.example").write_text("# test account\nTEST_EMAIL=\nTEST_PASSWORD\n")
        self.env = project_env.ProjectEnv(self.root)

    def everything_written(self):
        return "\n".join(path.read_text(errors="ignore") for path in self.root.rglob("*")
                         if path.is_file() and path.suffix in (".json", ".jsonl", ".log", ".yaml", ".html", ".txt"))


class StoreTests(ProjectCase):
    def test_names_come_from_the_example_and_values_from_the_store(self):
        self.assertEqual(self.env.names(), ["TEST_EMAIL", "TEST_PASSWORD"])
        self.env.set("TEST_PASSWORD", SECRET)
        rows = {row["name"]: row for row in self.env.status()}
        self.assertEqual((rows["TEST_PASSWORD"]["set"], rows["TEST_PASSWORD"]["source"]), (True, "keychain"))
        self.assertFalse(rows["TEST_EMAIL"]["set"])
        self.assertNotIn(SECRET, json.dumps(self.env.status()))
        self.assertEqual(self.env.resolve(["TEST_PASSWORD"]), {"TEST_PASSWORD": SECRET})
        self.assertTrue(self.env.unset("TEST_PASSWORD"))
        self.assertFalse(self.env.unset("TEST_PASSWORD"))

    def test_set_adds_only_the_name_to_the_example_and_never_the_value(self):
        self.env.set("API_TOKEN", SECRET)
        text = (self.root / ".mobster" / "env.example").read_text()
        self.assertIn("API_TOKEN\n", text)
        self.assertNotIn(SECRET, self.everything_written())
        self.assertNotIn(SECRET, text)

    def test_an_interrupted_file_write_keeps_the_previous_values(self):
        import contextlib
        self.env.set("TEST_PASSWORD", SECRET)
        original = project_env.env_file.open_private
        @contextlib.contextmanager
        def failing(path, mode):
            with original(path, mode) as stream:
                class Broken:
                    def write(self, text):
                        stream.write(text[:4])
                        raise OSError("interrupted write")
                yield Broken()
        with mock.patch.object(project_env.env_file, "open_private", failing):
            with self.assertRaises(OSError):
                self.env.set("TEST_PASSWORD", "replacement")
        self.assertEqual(self.env.resolve(["TEST_PASSWORD"]), {"TEST_PASSWORD": SECRET})

    def test_values_are_kept_per_project(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        (Path(other.name) / ".mobster").mkdir()
        self.env.set("TEST_PASSWORD", SECRET)
        elsewhere = project_env.ProjectEnv(other.name)
        elsewhere.set("TEST_PASSWORD", "other-value")
        self.assertEqual(self.env.resolve(["TEST_PASSWORD"])["TEST_PASSWORD"], SECRET)
        self.assertEqual(elsewhere.resolve(["TEST_PASSWORD"])["TEST_PASSWORD"], "other-value")

    def test_refusals_name_the_variable(self):
        with self.assertRaises(project_env.Missing) as caught:
            self.env.resolve(["TEST_PASSWORD"])
        self.assertIn("TEST_PASSWORD has no value", str(caught.exception))
        with self.assertRaises(project_env.Missing) as caught:
            self.env.resolve(["NOT_LISTED"])
        self.assertIn("NOT_LISTED isn't listed", str(caught.exception))
        for bad in ("", "a\nb", "x" * 5000, None):
            with self.assertRaises(project_env.EnvError):
                self.env.set("TEST_PASSWORD", bad)
        with self.assertRaises(project_env.EnvError):
            self.env.set("lower", "x")

    def test_an_empty_process_variable_is_not_a_value(self):
        with mock.patch.dict(os.environ, {"TEST_PASSWORD": ""}):
            with self.assertRaises(project_env.Missing):
                self.env.resolve(["TEST_PASSWORD"])
        with mock.patch.dict(os.environ, {"TEST_PASSWORD": "from-ci"}):
            self.assertEqual(self.env.resolve(["TEST_PASSWORD"]), {"TEST_PASSWORD": "from-ci"})

    def test_expand_and_mask(self):
        values = {"A": "aaa-secret", "B": "bbb-secret"}
        self.assertEqual(project_env.expand({"k": ["x ${A}", "${B}!"]}, values), {"k": ["x aaa-secret", "bbb-secret!"]})
        self.assertEqual(project_env.refs("${A} and ${B}", ["${A}"], {"x": "${C}"}), ["A", "B", "C"])
        masked = project_env.mask_deep({"a": ["see aaa-secret"], "bbb-secret": 1}, values)
        self.assertEqual(masked, {"a": [f"see {MASK}"], MASK: 1})

    def test_json_and_html_escaped_values_are_masked_in_written_files(self):
        import html
        secret = 'token"Q\\R<&>'
        (self.root / "event.json").write_text(json.dumps({"value": secret}))
        (self.root / "report.html").write_text(html.escape(secret))
        project_env.sweep_files(self.root, {"TOKEN": secret})
        self.assertEqual(json.loads((self.root / "event.json").read_text())["value"], MASK)
        self.assertEqual((self.root / "report.html").read_text(), MASK)

    def test_the_sweep_masks_text_files_under_a_folder(self):
        (self.root / "a.log").write_text(f"token {SECRET}")
        (self.root / "b.png").write_bytes(SECRET.encode())
        self.assertEqual(project_env.sweep_files(self.root, {"X": SECRET}), 1)
        self.assertEqual((self.root / "a.log").read_text(), f"token {MASK}")
        self.assertEqual((self.root / "b.png").read_bytes(), SECRET.encode())


class RunTests(ProjectCase):
    def setUp(self):
        super().setUp()
        self.env.set("TEST_EMAIL", EMAIL)
        self.env.set("TEST_PASSWORD", SECRET)

    def test_a_secret_check_runs_live_even_when_a_cached_path_exists(self):
        from mobile_agent.verify.runner import VerifyRun
        check = self.check(steps=["Search for ${TEST_EMAIL}"], expect=[{"text": "Choose"}])
        run = VerifyRun(check, mode="smart", runs_dir=self.runs, key=KEY, replay="auto")
        run.replay.lookup = mock.Mock(return_value={"steps": []})
        run.run_smart, run.run_replay = mock.Mock(), mock.Mock()
        run._load_secrets()
        run.run_flow()
        run.run_smart.assert_called_once()
        run.run_replay.assert_not_called()

    def test_launch_gets_the_values_and_no_file_holds_them(self):
        check = self.check(launch_args=["--user", "${TEST_EMAIL}"], launch_env={"PASSWORD": "${TEST_PASSWORD}"},
                           open_url="firefox://login?pw=${TEST_PASSWORD}", expect=[{"text": "Choose"}])
        driver = Driver([PLANS])
        result, manager = self.verify(check, driver)
        self.assertEqual(result["verdict"], "passed")
        launch = next(call for call in manager.calls if call[0] == "launch")
        self.assertEqual(launch[2], ("--user", EMAIL))
        self.assertEqual(launch[3], {"PASSWORD": SECRET})
        self.assertEqual(driver.opened[0]["url"], f"firefox://login?pw={SECRET}")
        written = self.everything_written()
        self.assertNotIn(SECRET, written)
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertIn("${TEST_PASSWORD}", (Path(result["run_dir"]) / "check.yaml").read_text())

    def test_smart_steps_are_expanded_for_the_model_and_masked_everywhere_else(self):
        seen = []
        events = recorded_events()[:-1] + [
            {"event": "frontier_action", "step": 2, "index": 0, "operation": "TYPE", "target_label": "Password",
             "changed": True, "text": SECRET, "field": "field", "app_name": "Firefox", "t": 6.0}]
        check = self.check(steps=["Sign in as ${TEST_EMAIL} with ${TEST_PASSWORD}"], expect=[{"text": "Choose"}])
        lines = []
        driver = Driver([PLANS], after_flow=[PLANS])
        manager = Manager()
        self.watch(driver, manager)
        from mobile_agent import engines
        from mobile_agent.verify import runner
        from mobile_agent.verify.runner import verify

        def build_frontier(driver_, client, *, apps, emit, **_):
            made = Agent(driver_, client, emit, None, None,
                         outcome={"status": "completed", "answer": f"Signed in with {SECRET}."}, events=events)
            seen.append(made)
            return made
        with mock.patch.object(runner, "build_driver", lambda *a: driver), \
                mock.patch.object(engines, "build_client", lambda key=None: Client()), \
                mock.patch.object(engines, "build_frontier", build_frontier):
            result = verify(check, mode="smart", runs_dir=self.runs, manager=manager, key=KEY, progress=lines.append)
        self.assertIn(f"{EMAIL} with {SECRET}", seen[0].requests[0])
        self.assertEqual(result["steps"][-1]["typed"], MASK)
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn(SECRET, "\n".join(lines))
        self.assertNotIn(SECRET, self.everything_written())
        self.assertEqual(result["check"]["steps"], ["Sign in as ${TEST_EMAIL} with ${TEST_PASSWORD}"])

    def test_a_missing_value_refuses_before_a_simulator_and_names_the_variable(self):
        self.env.unset("TEST_PASSWORD")
        check = self.check(launch_env={"PASSWORD": "${TEST_PASSWORD}"}, expect=[{"text": "Choose"}])
        result, manager = self.verify(check, Driver([PLANS]))
        self.assertEqual((result["verdict"], result["reason"]["class"]), ("couldnt_run", "usage"))
        self.assertIn("TEST_PASSWORD has no value", result["reason"]["message"])
        self.assertEqual(manager.names(), [])
        self.assertNotIn(EMAIL, json.dumps(result))

    def test_an_unlisted_name_is_refused_even_when_the_environment_has_it(self):
        check = self.check(launch_env={"X": "${NOT_LISTED}"}, expect=[{"text": "Choose"}])
        with mock.patch.dict(os.environ, {"NOT_LISTED": "v"}):
            result, manager = self.verify(check, Driver([PLANS]))
        self.assertEqual(result["reason"]["class"], "usage")
        self.assertIn("NOT_LISTED isn't listed", result["reason"]["message"])
        self.assertEqual(manager.names(), [])

    def test_a_check_with_no_references_is_untouched(self):
        result, manager = self.verify(self.check(launch_env={"MODE": "demo"}, expect=[{"text": "Choose"}]),
                                      Driver([PLANS]))
        self.assertEqual(result["verdict"], "passed")
        self.assertEqual(next(c for c in manager.calls if c[0] == "launch")[3], {"MODE": "demo"})

    def test_a_run_outside_a_project_with_references_refuses(self):
        self.runs = self.root / "elsewhere" / "runs"
        result, manager = self.verify(self.check(launch_env={"X": "${TEST_PASSWORD}"}, expect=[{"text": "Choose"}]),
                                      Driver([PLANS]))
        self.assertEqual(result["reason"]["class"], "usage")
        self.assertEqual(manager.names(), [])


class SurfaceTests(ProjectCase):
    def test_the_cli_never_prints_or_takes_a_value_as_an_argument(self):
        def run(*argv, stdin=""):
            out, err = io.StringIO(), io.StringIO()
            args = SimpleNamespace(project=str(self.root), json=False, stdin=bool(stdin), env_command=argv[0],
                                   name=argv[1] if len(argv) > 1 else None)
            with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err), mock.patch("sys.stdin",
                                                                                          io.StringIO(stdin)):
                code = project_env_cli.run(args)
            return code, out.getvalue() + err.getvalue()
        code, text = run("set", "TEST_PASSWORD", stdin=SECRET + "\n")
        self.assertEqual(code, 0)
        self.assertNotIn(SECRET, text)
        code, text = run("list")
        self.assertIn("TEST_PASSWORD  set (keychain)", text)
        self.assertIn("TEST_EMAIL     not set", text)
        self.assertNotIn(SECRET, text)
        code, _ = run("set", "TEST_EMAIL")  # no stdin, not a terminal: refused, nothing saved
        self.assertEqual(code, 2)
        self.assertFalse(project_env.ProjectEnv(self.root).status()[0]["set"])
        self.assertEqual(run("unset", "TEST_PASSWORD")[0], 0)
        self.assertNotIn(SECRET, json.dumps(self.env.status()))

    def test_the_api_lists_names_and_never_returns_a_value(self):
        def call(handler, method, query=None, body=None):
            request = api_routes.Request(runtime=None, method=method, path=project_env_routes.PATH, params={},
                                         query=query or {}, headers={}, body=body, content_type=None, origin="app",
                                         app_session=True)
            return handler(request)
        saved = call(project_env_routes._set, "POST", body={"root": str(self.root), "name": "TEST_PASSWORD",
                                                           "value": SECRET})
        listed = call(project_env_routes._list, "GET", {"root": [str(self.root)]})
        for response in (saved, listed):
            self.assertNotIn(SECRET, json.dumps(response.value))
        self.assertTrue(next(r for r in listed.value["names"] if r["name"] == "TEST_PASSWORD")["set"])
        from mobile_agent.api_errors import APIError
        with self.assertRaises(APIError) as caught:
            call(project_env_routes._list, "GET", {"root": ["relative/path"]})
        self.assertEqual(caught.exception.code, "bad_root")
        with self.assertRaises(APIError):
            call(project_env_routes._set, "POST", body={"root": str(self.root), "name": "TEST_PASSWORD", "value": ""})
        removed = call(project_env_routes._unset, "DELETE", {"root": [str(self.root)], "name": ["TEST_PASSWORD"]})
        self.assertTrue(removed.value["removed"])

    def test_the_routes_register_once_under_their_prefix(self):
        saved = list(api_routes._routes)
        self.addCleanup(lambda: (api_routes._routes.clear(), api_routes._routes.extend(saved)))
        api_routes._routes.clear()
        project_env_routes.register_routes()
        project_env_routes.register_routes()
        self.assertEqual(sorted(r.method for r in api_routes._routes), ["DELETE", "GET", "POST"])


if __name__ == "__main__":
    unittest.main()
