"""Upgrade-safe MCP client installation regressions."""

import io
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from mobile_agent import __main__ as main
from mobile_agent.integrations import clients
from mobile_agent.tests.integrations_support import MOBSTER, SERVER, Home, TempHomeCase, run_install


class McpReinstallTests(TempHomeCase):
    def seed(self, client_id, server):
        client = clients.BY_ID[client_id]
        path = self.home.file(client_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(client.edit(None, server))
        return path

    def test_reinstall_keeps_each_clients_phone_policy_and_environment(self):
        for client_id in ("claude-code", "codex", "cursor", "opencode", "goose"):
            with self.subTest(client=client_id):
                old = clients.Server("/old/mobster", ["mcp", "--allow-device", client_id,
                                                    "--allow-testflight", "org.example.app", "--keyless"],
                                     {"MOBSTER_DATA_DIR": f"/private/{client_id}"})
                path = self.seed(client_id, old)
                code, out = run_install(self.home, client_id)
                self.assertEqual(code, 0, out)
                entry = clients.BY_ID[client_id].read(path)
                options, environment = clients._configured_server(clients.BY_ID[client_id], entry)
                self.assertIn(("--allow-device", client_id), options)
                self.assertIn(("--allow-testflight", "org.example.app"), options)
                self.assertIn(("--keyless", None), options)
                self.assertEqual(environment["MOBSTER_DATA_DIR"], f"/private/{client_id}")
                self.assertEqual(clients.BY_ID[client_id].command_of(entry), MOBSTER)
                before = path.read_bytes()
                code, out = run_install(self.home, client_id)
                self.assertEqual(code, 0, out)
                self.assertEqual(path.read_bytes(), before)
                self.assertIn("unchanged", out)

    def test_explicit_flags_replace_only_their_own_groups(self):
        path = self.seed("cursor", clients.Server("/old/mobster", ["mcp", "--allow-device", "one",
                                                                     "--allow-device", "two",
                                                                     "--allow-testflight", "org.example.app",
                                                                     "--keyless"], {"MOBSTER_DATA_DIR": "/private/a"}))
        code, out = run_install(self.home, "cursor", "--allow-device", "three", "--allow-device", "four")
        self.assertEqual(code, 0, out)
        options, environment = clients._configured_server(clients.BY_ID["cursor"], clients.BY_ID["cursor"].read(path))
        self.assertEqual([value for flag, value in options if flag == "--allow-device"], ["three", "four"])
        self.assertIn(("--allow-testflight", "org.example.app"), options)
        self.assertIn(("--keyless", None), options)
        self.assertEqual(environment, {"MOBSTER_DATA_DIR": "/private/a"})

    def test_explicit_env_file_and_testflight_replace_saved_values(self):
        env_file = self.home.path / "new.env"
        env_file.write_text("OPENAI_API_KEY=example\n")
        env_file.chmod(0o600)
        path = self.seed("cursor", clients.Server("/old/mobster", ["mcp", "--env-file", "/old.env",
                                                                     "--allow-testflight", "org.example.old",
                                                                     "--allow-device", "one"]))
        code, out = run_install(self.home, "cursor", "--env-file", str(env_file),
                                "--allow-testflight", "org.example.new")
        self.assertEqual(code, 0, out)
        options, _ = clients._configured_server(clients.BY_ID["cursor"], clients.BY_ID["cursor"].read(path))
        self.assertIn(("--env-file", str(env_file)), options)
        self.assertIn(("--allow-testflight", "org.example.new"), options)
        self.assertIn(("--allow-device", "one"), options)
        self.assertNotIn(("--env-file", "/old.env"), options)
        self.assertNotIn(("--allow-testflight", "org.example.old"), options)

    def test_reset_server_options_is_an_explicit_clean_replacement(self):
        path = self.seed("goose", clients.Server("/old/mobster", ["mcp", "--allow-device", "one", "--keyless"],
                                                {"MOBSTER_DATA_DIR": "/private/a"}))
        code, out = run_install(self.home, "goose", "--reset-server-options")
        self.assertEqual(code, 0, out)
        self.assertEqual(clients.BY_ID["goose"].read(path), clients.BY_ID["goose"].entry(SERVER))

    def test_unknown_or_malformed_launch_options_refuse_without_writing(self):
        for args in (["mcp", "--unknown"], ["mcp", "--allow-device"], ["--unknown", "mcp"],
                     ["other"], ["mcp", "--allow-device", "one", "--", "other"]):
            with self.subTest(args=args):
                path = self.seed("cursor", clients.Server("/old/mobster", args))
                before = path.read_bytes()
                code, out = run_install(self.home, "cursor", "--json")
                self.assertEqual(code, 1)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(json.loads(out)["changes"][0]["action"], "error")
        code, out = run_install(self.home, "cursor", "--reset-server-options")
        self.assertEqual(code, 0, out)

    def test_unknown_saved_option_refusal_does_not_echo_its_value(self):
        secret = "sensitive-existing-value"
        path = self.seed("cursor", clients.Server("/old/mobster", ["mcp", f"--api-key={secret}"]))
        before = path.read_bytes()
        code, out = run_install(self.home, "cursor", "--dry-run", "--json")
        self.assertEqual(code, 1)
        self.assertNotIn(secret, out)
        self.assertEqual(path.read_bytes(), before)

    def test_unmanaged_client_setting_refuses_upgrade(self):
        path = self.seed("opencode", clients.Server("/old/mobster", ["mcp", "--allow-device", "one"]))
        entry = json.loads(path.read_text())
        entry["mcp"]["mobster"]["enabled"] = False
        path.write_text(json.dumps(entry))
        before = path.read_bytes()
        code, out = run_install(self.home, "opencode")
        self.assertEqual(code, 1)
        self.assertIn("client settings aren't managed", out)
        self.assertEqual(path.read_bytes(), before)

    def test_source_launcher_and_root_options_are_kept_after_mcp(self):
        path = self.seed("cursor", clients.Server("/old/python", ["-m", "mobile_agent", "--env-file", "/a.env",
                                                                       "mcp", "--allow-device=one"],
                                                  {"PYTHONPATH": "/old/source"}))
        code, out = run_install(self.home, "cursor")
        self.assertEqual(code, 0, out)
        entry = clients.BY_ID["cursor"].read(path)
        self.assertEqual(entry["args"], ["mcp", "--env-file", "/a.env", "--allow-device", "one"])
        self.assertEqual(entry["env"], {"PYTHONPATH": "/old/source"})

    def test_root_option_value_named_mcp_is_not_the_command(self):
        path = self.seed("cursor", clients.Server("/old/mobster", ["--device", "mcp", "mcp",
                                                                     "--allow-device", "one"]))
        code, out = run_install(self.home, "cursor")
        self.assertEqual(code, 0, out)
        self.assertEqual(clients.BY_ID["cursor"].read(path)["args"],
                         ["mcp", "--device", "mcp", "--allow-device", "one"])

    def test_dry_run_never_prints_neighbor_or_environment_secrets(self):
        secret = "secret-outside-mobster"
        path = self.seed("cursor", clients.Server("/old/mobster", ["mcp", "--allow-device", "one"],
                                                 {"MOBSTER_DATA_DIR": secret}))
        text = json.loads(path.read_text())
        text["mcpServers"]["other"] = {"env": {"TOKEN": "secret-other-client"}}
        path.write_text(json.dumps(text))
        code, out = run_install(self.home, "cursor", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertNotIn(secret, out)
        self.assertNotIn("secret-other-client", out)
        self.assertEqual(json.loads(path.read_text()), text)



class McpCliReinstallTests(TempHomeCase):
    def setUp(self):
        super().setUp()
        self.home.env.which = lambda name, path=None: f"/bin/{name}" if name in ("claude", "codex") else None

    def test_reinstall_passes_saved_policy_and_environment_through_client_commands(self):
        old = clients.Server("/old/mobster", ["mcp", "--allow-device", "one", "--keyless"],
                             {"MOBSTER_DATA_DIR": "/private/team"})
        for client_id in ("claude-code", "codex"):
            client = clients.BY_ID[client_id]
            self.home.file(client_id).write_text(client.edit(None, old))
        with mock.patch.object(clients.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            code, out = run_install(self.home, "claude-code", "codex", "--json")
        self.assertEqual(code, 0, out)
        calls = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(calls), 3)  # Claude removes first; Codex replaces in one call.
        for argv in (calls[1], calls[2]):
            self.assertEqual(argv[-4:], ["mcp", "--allow-device", "one", "--keyless"])
            self.assertIn("MOBSTER_DATA_DIR=/private/team", argv)
        self.assertNotIn("/private/team", out)

    def test_dry_run_redacts_saved_cli_environment(self):
        old = clients.Server("/old/mobster", ["mcp", "--allow-device", "one"],
                             {"MOBSTER_DATA_DIR": "secret-data-root"})
        self.home.file("claude-code").write_text(clients.BY_ID["claude-code"].edit(None, old))
        with mock.patch.object(clients.subprocess, "run") as run:
            code, out = run_install(self.home, "claude-code", "--dry-run", "--json")
        self.assertEqual(code, 0, out)
        run.assert_not_called()
        self.assertNotIn("secret-data-root", out)
        self.assertIn("MOBSTER_DATA_DIR=<redacted>", out)


class McpXcodeReinstallTests(TempHomeCase):
    def test_reinstall_keeps_both_xcode_agents_phone_restriction(self):
        code, out = run_install(self.home, "xcode", "--allow-device", "Lab iPhone")
        self.assertEqual(code, 0, out)
        code, out = run_install(self.home, "xcode")
        self.assertEqual(code, 0, out)
        xcode = clients.BY_ID["xcode"]
        for part in xcode.parts:
            entry = part.read(part.paths(self.home.env)[0])
            self.assertEqual(entry["args"], ["mcp", "--allow-device", "Lab iPhone"])


class McpMainDispatchTests(unittest.TestCase):
    def test_environment_env_file_is_default_only_but_explicit_flag_overrides(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Home(folder)
            saved = home.path / "saved.env"
            inherited = home.path / "inherited.env"
            for path in (saved, inherited):
                path.write_text("OPENAI_API_KEY=example\n")
                path.chmod(0o600)
            cursor = clients.BY_ID["cursor"]
            config = home.file("cursor")
            config.write_text(cursor.edit(None, clients.Server("/old/mobster", ["mcp", "--env-file", str(saved),
                                                                              "--allow-device", "one"])))
            with mock.patch.dict(os.environ, {"HOME": folder, "XDG_CONFIG_HOME": f"{folder}/.config",
                                                 "CODEX_HOME": f"{folder}/.codex", "PATH": "/usr/bin:/bin",
                                                 "MOBSTER_ENV_FILE": str(inherited)}), \
                    mock.patch.object(clients.Env, "current", classmethod(lambda cls: home.env)), \
                    mock.patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(main.main(["mcp", "install", "cursor", "zed", "--json"]), 0)
                cursor_options, _ = clients._configured_server(cursor, cursor.read(config))
                self.assertIn(("--env-file", str(saved)), cursor_options)
                self.assertIn(("--allow-device", "one"), cursor_options)
                zed = clients.BY_ID["zed"]
                zed_options, _ = clients._configured_server(zed, zed.read(home.file("zed")))
                self.assertIn(("--env-file", str(inherited)), zed_options)
                self.assertEqual(main.main(["mcp", "install", "cursor", "--env-file", str(inherited),
                                            "--json"]), 0)
                cursor_options, _ = clients._configured_server(cursor, cursor.read(config))
                self.assertIn(("--env-file", str(inherited)), cursor_options)
                self.assertNotIn(("--env-file", str(saved)), cursor_options)
