import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mobile_agent import api_routes, tracks
from mobile_agent.api_errors import APIError
from mobile_agent.integrations import clients, routes
from mobile_agent.tests.integrations_support import Home
from mobile_agent.tests.seam_support import isolate

AGENT = "/Applications/Mobster.app/Contents/MacOS/mobster-agent"


def request(method, path, *, app_session=True, has_session=True, runtime=None):
    found = api_routes.match(method, path)
    assert found is not None, f"no route for {method} {path}"
    route, params = found
    runtime = runtime or SimpleNamespace(local_api={"app_session": has_session}, setup=None, config=None)
    return route.handler(api_routes.Request(runtime=runtime, method=method, path=path, params=params, query={},
                                            headers={}, body=None, content_type=None, origin="app",
                                            app_session=app_session))


class AppClientRouteTests(unittest.TestCase):
    def setUp(self):
        isolate(self)
        routes.register_routes()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Home(folder.name)
        self.server = clients.Server(AGENT, ["mcp"])
        for patcher in (mock.patch.object(routes, "environment", lambda: self.home.env),
                        mock.patch.object(routes, "server", lambda runtime, env: self.server)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def config(self, client_id):
        path = self.home.file(client_id)
        return json.loads(path.read_text()) if path.exists() else {}

    def test_adding_writes_the_bundled_agent_into_the_client(self):
        answer = request("POST", "/api/integrations/clients/claude-desktop")
        self.assertEqual(answer.status, 200)
        self.assertEqual(answer.value["client"]["state"], "added")
        self.assertTrue(answer.value["client"]["restart"])
        self.assertEqual(self.config("claude-desktop")["mcpServers"]["mobster"], {"command": AGENT, "args": ["mcp"]})
        listed = {row["id"]: row for row in request("GET", "/api/integrations/clients").value["clients"]}
        self.assertEqual(listed["claude-desktop"]["state"], "added")
        self.assertEqual(listed["cursor"]["state"], "absent")
        self.assertEqual(set(listed), set(clients.BY_ID))

    def test_removing_takes_out_only_mobster(self):
        path = self.home.file("cursor")
        path.write_text(json.dumps({"mcpServers": {"other": {"command": "/bin/echo"}}}))
        request("POST", "/api/integrations/clients/cursor")
        answer = request("DELETE", "/api/integrations/clients/cursor")
        self.assertEqual(answer.value["client"]["state"], "absent")
        self.assertEqual(self.config("cursor"), {"mcpServers": {"other": {"command": "/bin/echo"}}})

    def test_another_mobster_reads_as_other_and_adding_replaces_it(self):
        path = self.home.file("cursor")
        path.write_text(json.dumps({"mcpServers": {"mobster": {"command": "/opt/homebrew/bin/mobster",
                                                               "args": ["mcp"]}}}))
        listed = {row["id"]: row for row in request("GET", "/api/integrations/clients").value["clients"]}
        self.assertEqual((listed["cursor"]["state"], listed["cursor"]["command"]),
                         ("other", "/opt/homebrew/bin/mobster"))
        request("POST", "/api/integrations/clients/cursor")
        self.assertEqual(self.config("cursor")["mcpServers"]["mobster"]["command"], AGENT)

    def test_readding_selects_app_env_file_without_dropping_saved_phone_policy(self):
        path = self.home.file("cursor")
        old = clients.Server("/old/mobster", ["mcp", "--env-file", "/old/key.env", "--allow-device",
                                              "Test iPhone", "--allow-testflight", "org.example.app"],
                             {"MOBSTER_DATA_DIR": "/private/mobster"})
        path.write_text(clients.BY_ID["cursor"].edit(None, old))
        self.server = clients.Server(AGENT, ["mcp", "--env-file", "/app/key.env"])
        answer = request("POST", "/api/integrations/clients/cursor")
        self.assertEqual(answer.status, 200)
        entry = self.config("cursor")["mcpServers"]["mobster"]
        self.assertEqual(entry["command"], AGENT)
        self.assertEqual(entry["args"], ["mcp", "--allow-device", "Test iPhone", "--allow-testflight",
                                         "org.example.app", "--env-file", "/app/key.env"])
        self.assertEqual(entry["env"], {"MOBSTER_DATA_DIR": "/private/mobster"})

    def test_readding_refuses_an_unrecognized_saved_policy_without_write(self):
        path = self.home.file("cursor")
        path.write_text(clients.BY_ID["cursor"].edit(None, clients.Server("/old/mobster",
                                                                            ["mcp", "--api-key=sensitive-value"])))
        before = path.read_bytes()
        with self.assertRaises(APIError) as caught:
            request("POST", "/api/integrations/clients/cursor")
        self.assertEqual(caught.exception.code, "client_config")
        self.assertNotIn("sensitive-value", str(caught.exception))
        self.assertEqual(path.read_bytes(), before)

    def test_codex_gets_a_toml_table(self):
        request("POST", "/api/integrations/clients/codex")
        text = self.home.file("codex").read_text()
        self.assertIn("[mcp_servers.mobster]", text)
        self.assertIn(AGENT, text)

    def test_without_the_app_session_nothing_is_read_or_written(self):
        for has_session in (True, False):
            for method, path in (("GET", "/api/integrations/clients"), ("POST", "/api/integrations/clients/cursor"),
                                 ("DELETE", "/api/integrations/clients/cursor")):
                with self.subTest(method=method, has_session=has_session), self.assertRaises(APIError) as caught:
                    request(method, path, app_session=False, has_session=has_session)
                self.assertEqual((caught.exception.status, caught.exception.code), (403, "app_only"))
        self.assertFalse(self.home.file("cursor").exists())

    def test_an_unknown_client_is_refused(self):
        with self.assertRaises(APIError) as caught:
            request("POST", "/api/integrations/clients/notepad")
        self.assertEqual((caught.exception.status, caught.exception.code), (404, "unknown_client"))
        self.assertIsNone(api_routes.match("POST", "/api/integrations/clients/../../etc"))

    def test_a_client_that_isnt_installed_is_refused(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Home(folder.name, detect=False)
        with self.assertRaises(APIError) as caught:
            request("POST", "/api/integrations/clients/cursor")
        self.assertEqual(caught.exception.code, "client_missing")
        self.assertIn("Cursor isn't installed on this Mac", str(caught.exception))
        self.assertFalse(self.home.file("cursor").exists())

    def test_a_translocated_app_is_refused(self):
        self.server = clients.Server("/private/var/folders/x/T/AppTranslocation/ABC/d/Mobster.app/Contents/MacOS/"
                                     "mobster-agent", ["mcp"])
        with self.assertRaises(APIError) as caught:
            request("POST", "/api/integrations/clients/cursor")
        self.assertEqual(caught.exception.code, "app_moved")
        self.assertFalse(self.home.file("cursor").exists())

    def test_an_unreadable_config_is_reported_and_left_alone(self):
        path = self.home.file("cursor")
        path.write_text("{ not json")
        with self.assertRaises(APIError) as caught:
            request("POST", "/api/integrations/clients/cursor")
        self.assertEqual(caught.exception.code, "client_config")
        self.assertEqual(path.read_text(), "{ not json")


class ServerEntryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.home = Home(self.folder / "home")

    def runtime_with(self, mode):
        env_file = self.folder / "agent.env"
        env_file.write_text("ANTHROPIC_API_KEY=sk-ant-test\n")
        os.chmod(env_file, mode)
        return SimpleNamespace(setup=SimpleNamespace(env_file=env_file), config=None), env_file

    def test_the_apps_private_settings_file_goes_in_by_path(self):
        runtime, env_file = self.runtime_with(0o600)
        with mock.patch.object(clients.sys, "frozen", True, create=True), \
                mock.patch.object(clients.sys, "executable", AGENT):
            entry = routes.server(runtime, self.home.env)
        self.assertEqual(entry.argv(), [AGENT, "mcp", "--env-file", str(env_file)])
        self.assertNotIn("sk-ant-test", json.dumps(entry.__dict__))

    def test_a_settings_file_others_can_read_is_left_out(self):
        runtime, _ = self.runtime_with(0o644)
        with mock.patch.object(clients.sys, "frozen", True, create=True), \
                mock.patch.object(clients.sys, "executable", AGENT):
            entry = routes.server(runtime, self.home.env)
        self.assertEqual(entry.argv(), [AGENT, "mcp"])

    def test_the_usual_command_folders_are_on_path(self):
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin", "HOME": str(self.home.path)}):
            path = routes.environment().environ["PATH"].split(os.pathsep)
        self.assertEqual(path[:2], ["/usr/bin", "/bin"])
        self.assertIn("/opt/homebrew/bin", path)
        self.assertIn(str(self.home.path / ".local/bin"), path)


class RegistrationTests(unittest.TestCase):
    def test_the_harness_track_registers_the_routes(self):
        isolate(self)
        tracks.load()
        self.assertEqual(tracks.STATUS.get("harness"), "ok")
        self.assertIsNotNone(api_routes.match("GET", "/api/integrations/clients"))
        for method in ("POST", "DELETE"):
            self.assertIsNotNone(api_routes.match(method, "/api/integrations/clients/claude-code"))


if __name__ == "__main__":
    unittest.main()
