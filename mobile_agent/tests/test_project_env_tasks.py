"""A saved task's ${NAME}s come from its project's secrets (project_env.py, workflows/runs.py): refused at admission
when a value is missing or the engine can't mask, expanded for Mobster's agent only, masked in the run's record.
Offline."""

import json
import os
import tempfile
from pathlib import Path
from unittest import mock

from mobile_agent import paths, project_env
from mobile_agent.tests.seam_tasks import SmartBase, item
from mobile_agent.tests.test_engines import Script

SECRET = "hunter2-Zq9xK"
GOAL = "Sign in to the app with the password ${TEST_PASSWORD}"


class SecretsBase(SmartBase):
    def setUp(self):
        super().setUp()
        home = tempfile.TemporaryDirectory()
        project = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.addCleanup(project.cleanup)
        for patch in (mock.patch.dict(os.environ, {"MOBSTER_VAULT": "file"}),
                      mock.patch.object(paths, "user_data_dir", lambda: Path(home.name))):
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop("TEST_PASSWORD", None)
        self.root = Path(project.name).resolve()
        (self.root / ".mobster").mkdir()
        (self.root / ".mobster" / "env.example").write_text("TEST_PASSWORD\n")
        self.env = project_env.ProjectEnv(self.root)


    def tearDown(self):
        # Drain services before the project folders, registry and path patches are cleaned up.
        self.close()
        self.runtimes.clear()
        super().tearDown()


class TaskSecretsTests(SecretsBase):
    def test_a_missing_value_refuses_the_task_and_names_the_variable(self):
        runtime = self.runtime()
        with self.assertRaises(ValueError) as caught:
            runtime.create(None, GOAL, "live", extras={"projectEnv": str(self.root)}, origin="workflow")
        self.assertIn("TEST_PASSWORD has no value", str(caught.exception))
        self.assertEqual(len(runtime.runs), 0)

    def test_only_mobsters_agent_and_not_the_plain_api_may_use_them(self):
        self.env.set("TEST_PASSWORD", SECRET)
        runtime = self.runtime()
        for engine, origin in (("codex", "workflow"), ("claude_code", "app"),
                               *((None, origin) for origin in ("api", "mcp", "remote", "hook", "console", "trigger"))):
            with self.subTest(engine=engine, origin=origin), self.assertRaises(ValueError) as caught:
                runtime.create(None, GOAL, "live", engine=engine, extras={"projectEnv": str(self.root)}, origin=origin)
                self.assertIn("project secrets", str(caught.exception))

    def test_a_goal_without_names_runs_as_before(self):
        runtime = self.runtime()
        run = runtime.create(None, "What iOS version is this?", "live", extras={"projectEnv": str(self.root)},
                             origin="workflow")
        self.assertEqual(run.goal, "What iOS version is this?")

    def test_http_origin_labels_cannot_unlock_project_secrets(self):
        from mobile_agent.server import make_handler
        from mobile_agent.tests.seam_support import request
        self.env.set("TEST_PASSWORD", SECRET)
        runtime = self.runtime()
        thread = request(make_handler(runtime), "POST", "/api/threads", {})[1]["thread"]["id"]
        for session in (None, "private-session"):
            handler = make_handler(runtime, app_session=session)
            for origin in ("app", "cli", "tui", "mcp"):
                for path, body in (
                    ("/api/runs", {"goal": GOAL, "projectEnv": str(self.root)}),
                    ("/api/threads", {"message": {"text": GOAL, "projectEnv": str(self.root)}}),
                    (f"/api/threads/{thread}/messages", {"text": GOAL, "projectEnv": str(self.root)}),
                ):
                    with self.subTest(session=session, origin=origin, path=path):
                        status, data, _ = request(handler, "POST", path, body,
                                                  headers={"X-Mobster-Origin": origin})
                        if path == "/api/runs":
                            self.assertEqual((status, data.get("code")), (403, "app_only"), data)
                        else:
                            self.assertEqual(status, 400, data)  # messages do not accept projectEnv
        self.assertEqual(len(runtime.runs), 0)

    def test_the_private_app_session_can_start_a_task_with_project_secrets(self):
        from mobile_agent.server import make_handler
        from mobile_agent.tests.seam_support import request
        self.env.set("TEST_PASSWORD", SECRET)
        runtime = self.runtime()
        status, data, _ = request(make_handler(runtime, app_session="private-session"), "POST", "/api/runs",
                                  {"goal": GOAL, "projectEnv": str(self.root)},
                                  headers={"X-Mobster-Origin": "app",
                                           "X-Mobster-App-Session": "private-session"})
        self.assertEqual(status, 201, data)
        self.assertEqual(data["run"]["goal"], GOAL)

    def test_the_agent_gets_the_value_and_no_record_holds_it(self):
        self.env.set("TEST_PASSWORD", SECRET)
        runtime = self.runtime()
        run = runtime.create(None, GOAL, "live", extras={"projectEnv": str(self.root)}, origin="workflow")
        self.assertEqual(run.goal, GOAL)
        script = Script([[("TYPE", "Message", SECRET)], [("DONE", None, None)], [("DONE", None, None)]],
                        [item(kind="READ", act="none", what="Sam", quote="Sam")], answer=f"Signed in with {SECRET}.")
        self.work(runtime, run, script)
        self.assertIn(SECRET, script.prompts[0])    # the model is asked with the value, to type it
        record = json.dumps({"public": run.public(), "events": list(run.events),
                             "result": getattr(run, "result", None)},
                            default=str)
        self.assertNotIn(SECRET, record)
        self.assertIn("${TEST_PASSWORD}", record)
        for path in Path(runtime.config.state_db).parent.rglob("*"):
            if path.is_file():
                self.assertNotIn(SECRET.encode(), path.read_bytes(), path.name)


class WorkflowAdmissionTests(SecretsBase):
    def workflow(self, runtime, goal=GOAL, **fields):
        from mobile_agent.projects.service import service_for
        service = service_for(runtime)
        service.sync.close()  # this test owns the file binding; no background synchronizer
        project, _ = service.add({"path": str(self.root)})
        workflow = runtime.workflows.create({"name": "Sign in", **({"goal": goal} if "steps" not in fields else {}),
                                             **fields})
        with runtime.journal.lock, runtime.journal.transaction() as connection:
            record = runtime.workflows._get(workflow["id"])
            record["file"] = {"projectId": project["id"], "path": "sign-in.yaml"}
            runtime.workflows._save(connection, record)
        return workflow

    def test_a_registered_projects_workflow_keeps_the_placeholder_and_resolves_its_folder(self):
        self.env.set("TEST_PASSWORD", SECRET)
        runtime = self.runtime()
        workflow = self.workflow(runtime)
        run, _ = runtime.workflows.run(workflow["id"], "local", origin="cli")
        self.assertEqual(run.goal, GOAL)
        self.assertEqual(run.extras["projectEnv"], str(self.root))
        self.assertNotIn(SECRET, json.dumps(run.public()))

    def test_workflow_http_origin_labels_cannot_unlock_secrets_in_steps_or_supplied_values(self):
        from mobile_agent.server import make_handler
        from mobile_agent.tests.seam_support import request
        self.env.set("TEST_PASSWORD", SECRET)
        runtime = self.runtime()
        handler = make_handler(runtime, app_session="private-session")
        for goal, values in ((GOAL, None), ("Sign in with {password}", {"password": "${TEST_PASSWORD}"})):
            workflow = self.workflow(runtime, goal)
            for origin in ("app", "cli", "tui"):
                with self.subTest(goal=goal, origin=origin):
                    status, data, _ = request(handler, "POST", f"/api/workflows/{workflow['id']}/run",
                                              {"values": values}, headers={"X-Mobster-Origin": origin,
                                              "Idempotency-Key": f"http-{origin}"})
                    self.assertEqual(status, 400, data)
                    self.assertIn("project secrets", data["error"])
        self.assertEqual(len(runtime.runs), 0)

    def test_private_app_session_can_run_a_registered_projects_workflow(self):
        from mobile_agent.server import make_handler
        from mobile_agent.tests.seam_support import request
        self.env.set("TEST_PASSWORD", SECRET)
        runtime = self.runtime()
        workflow = self.workflow(runtime)
        status, data, _ = request(make_handler(runtime, app_session="private-session"), "POST",
                                  f"/api/workflows/{workflow['id']}/run", {},
                                  headers={"Idempotency-Key": "private", "X-Mobster-Origin": "app",
                                           "X-Mobster-App-Session": "private-session"})
        self.assertEqual(status, 201, data)
        self.assertEqual(data["run"]["goal"], GOAL)

    def test_workflow_tools_do_not_inherit_local_secret_access(self):
        self.env.set("TEST_PASSWORD", SECRET)
        runtime = self.runtime()
        workflow = self.workflow(runtime, asTool=True)
        slug = next(tool["slug"] for tool in runtime.workflows.tools() if tool["workflowId"] == workflow["id"])
        with self.assertRaisesRegex(ValueError, "project secrets"):
            runtime.workflows.run_tool(slug, {"wait_seconds": 0}, "tool", origin="app")
        self.assertEqual(len(runtime.runs), 0)

    def test_http_multi_step_workflow_cannot_start_a_secret_task(self):
        from mobile_agent.server import make_handler
        from mobile_agent.tests.seam_support import request
        self.env.set("TEST_PASSWORD", SECRET)
        runtime = self.runtime()
        workflow = self.workflow(runtime, steps=[{"id": "a", "kind": "task", "text": GOAL, "appId": None},
                                                  {"id": "b", "kind": "task", "text": "Open Settings", "appId": None}])
        _, data, _ = request(make_handler(runtime, app_session="private-session"), "POST",
                             f"/api/workflows/{workflow['id']}/run", {},
                             headers={"Idempotency-Key": "batch", "X-Mobster-Origin": "app"})
        self.assertIn("project secrets", json.dumps(data))
        self.assertEqual(len(runtime.runs), 0)

    def test_http_project_workflow_without_secret_references_still_runs(self):
        from mobile_agent.server import make_handler
        from mobile_agent.tests.seam_support import request
        runtime = self.runtime()
        workflow = self.workflow(runtime, "Open Settings")
        status, data, _ = request(make_handler(runtime), "POST", f"/api/workflows/{workflow['id']}/run", {},
                                  headers={"Idempotency-Key": "ordinary"})
        self.assertEqual(status, 201, data)
        self.assertEqual(data["run"]["goal"], "Open Settings")
