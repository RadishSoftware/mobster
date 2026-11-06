"""Agent host setup, documentation and the public test action. Offline; temp homes only."""

import json
import re
import unittest
from pathlib import Path

from mobile_agent.integrations import clients
from mobile_agent.tests.integrations_support import MOBSTER, TempHomeCase, run_install

ROOT = Path(__file__).resolve().parents[2]
HOST_PAGES = ("windsurf", "codex", "opencode", "gemini-cli", "copilot-cli")
IDS = {"gemini-cli": "gemini"}


class HostPageTests(unittest.TestCase):
    def test_every_host_has_a_page_that_names_its_install_command_and_file(self):
        docs_json = (ROOT / "docs" / "docs.json").read_text()
        for host in HOST_PAGES:
            with self.subTest(host=host):
                client = clients.BY_ID[IDS.get(host, host)]
                page = (ROOT / "docs" / "agents" / f"{host}.mdx").read_text()
                self.assertIn(f"mobster mcp install {client.id}", page)
                self.assertIn(f'"agents/{host}"', docs_json)
                home = Path("/h")
                env = clients.Env(home=home, environ={"HOME": "/h"})
                shown = "~/" + str(client.paths(env)[0].relative_to(home))
                self.assertIn(shown, page)


class CopilotCLITests(TempHomeCase):
    def test_the_entry_is_a_local_server_with_every_tool_offered(self):
        code, _ = run_install(self.home, "copilot-cli")
        self.assertEqual(code, 0)
        path = self.home.path / ".copilot" / "mcp-config.json"
        entry = json.loads(path.read_text())["mcpServers"]["mobster"]
        self.assertEqual(entry, {"type": "local", "command": MOBSTER, "args": ["mcp"], "env": {}, "tools": ["*"]})

    def test_copilot_names_the_cli_and_vs_code_keeps_its_own(self):
        self.assertIs(clients.lookup("copilot"), clients.BY_ID["copilot-cli"])
        self.assertIs(clients.lookup("code"), clients.BY_ID["vscode"])

    def test_copilot_home_moves_the_file_and_other_servers_stay(self):
        folder = self.home.path / "elsewhere"
        folder.mkdir()
        (folder / "mcp-config.json").write_text('{"mcpServers": {"other": {"type": "local", "command": "x"}}}')
        self.home.env.environ["COPILOT_HOME"] = str(folder)
        run_install(self.home, "copilot-cli")
        servers = json.loads((folder / "mcp-config.json").read_text())["mcpServers"]
        self.assertEqual(set(servers), {"other", "mobster"})
        run_install(self.home, "copilot-cli", "--remove")
        self.assertEqual(set(json.loads((folder / "mcp-config.json").read_text())["mcpServers"]), {"other"})

    def test_a_repository_gets_github_mcp_json_and_not_claude_codes_file(self):
        project = self.home.path / "repo"
        project.mkdir()
        run_install(self.home, "copilot-cli", "--project", str(project))
        self.assertTrue((project / ".github" / "mcp.json").is_file())
        self.assertFalse((project / ".mcp.json").exists())

    def test_install_all_covers_the_five_hosts_and_a_second_run_changes_nothing(self):
        run_install(self.home, "--all")
        for host in ("windsurf", "codex", "opencode", "gemini", "copilot-cli"):
            self.assertTrue(clients.BY_ID[host].status(self.home.env)["configured"], host)
        _, out = run_install(self.home, "--all", "--json")
        rows = json.loads(out)["changes"]
        self.assertTrue(all(row["action"] == "unchanged" for row in rows), rows)


class ActionMetadataTests(unittest.TestCase):
    def test_the_action_has_marketplace_metadata_and_a_replay_example(self):
        action = (ROOT / "actions" / "test" / "action.yml").read_text()
        for key in ("name:", "description:", "author:", "branding:"):
            self.assertRegex(action, re.compile(rf"^{key}", re.M))
        readme = (ROOT / "actions" / "test" / "README.md").read_text()
        self.assertIn('build: "false"', readme)
        self.assertIn("--no-heal", readme)


if __name__ == "__main__":
    unittest.main()
