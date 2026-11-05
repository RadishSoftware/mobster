import io
import json
import shutil
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mobile_agent.api_errors import APIError
from mobile_agent.connectors import vault as connector_vault
from mobile_agent.plugins import install as sources
from mobile_agent.plugins import manifest
from mobile_agent.plugins import routes as plugin_routes
from mobile_agent.plugins import skills as plugin_skills
from mobile_agent.tests import fake_mcp_server as fake
from mobile_agent.tests.test_sota_e2e import Service

FAKE = str(Path(fake.__file__).resolve())
EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "plugins" / "standup-notes"
SKILL = """---
name: pr-triage
description: Triage GitHub pull requests and review comments
---

TRIAGE-MARKER: list each pull request with its author and what blocks it.
"""


def write_plugin(folder, name="radish-tools", mcp=True, skill=True, workflow=True, destination=False,
    manifest_text=None):
    folder = Path(folder)
    (folder / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (folder / ".claude-plugin" / "plugin.json").write_text(
        manifest_text or json.dumps({"name": name, "version": "1.2.0",
            "description": "Radish Software's tools for Mobster.",
                                     "author": {"name": "Radish Software"}}))
    if mcp:
        (folder / ".mcp.json").write_text(json.dumps({"mcpServers": {"getmobster": {
            "command": sys.executable, "args": [FAKE],
            "env": {"FAKE_MCP_LOG": "${RADISH_LOG}", "RADISH_HOME": "${CLAUDE_PLUGIN_ROOT}"}}}}))
    if skill:
        (folder / "skills" / "pr-triage").mkdir(parents=True, exist_ok=True)
        (folder / "skills" / "pr-triage" / "SKILL.md").write_text(SKILL)
    if workflow:
        (folder / "mobster" / "workflows").mkdir(parents=True, exist_ok=True)
        (folder / "mobster" / "workflows" / "triage.json").write_text(json.dumps(
            {"name": "Triage pull requests", "goal": "Triage my GitHub pull requests", "engine": "smart",
             "cron": "0 9 * * 1-5", "enabled": True}))
    if destination:
        (folder / "mobster" / "destinations").mkdir(parents=True, exist_ok=True)
        (folder / "mobster" / "destinations" / "hook.json").write_text(json.dumps(
            {"name": "Radish Software hook", "url": "${RADISH_HOOK}"}))
    return folder


def zipped(entries):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        for name, content, *mode in entries:
            info = zipfile.ZipInfo(name)
            if mode:
                info.external_attr = mode[0] << 16
            archive.writestr(info, content)
    return data.getvalue()


def zip_folder(folder, prefix=""):
    folder = Path(folder)
    return zipped([(prefix + str(p.relative_to(folder)), p.read_bytes()) for p in folder.rglob("*") if p.is_file()])


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mobster-plugin-test-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_the_example_plugin_reads(self):
        plugin = manifest.read(EXAMPLE)
        summary = plugin.summary()
        self.assertEqual(summary["name"], "standup-notes")
        self.assertEqual([c["name"] for c in summary["connectors"]], ["notes"])
        self.assertEqual([s["name"] for s in summary["skills"]], ["standup"])
        self.assertEqual([w["name"] for w in summary["workflows"]], ["Morning standup"])
        self.assertEqual(summary["asks"], [])

    def test_a_plugin_asks_for_its_placeholders_and_never_the_root(self):
        plugin = manifest.read(write_plugin(self.root, destination=True))
        self.assertEqual([a["key"] for a in plugin.asks], ["RADISH_LOG", "RADISH_HOOK"])
        self.assertEqual(manifest.fill("${CLAUDE_PLUGIN_ROOT}/x", {}, "/p"), "/p/x")
        with self.assertRaises(manifest.PluginError) as caught:
            manifest.fill("${RADISH_LOG}", {}, "/p")
        self.assertEqual(str(caught.exception), "Fill in RADISH_LOG to install this plugin.")

    def test_the_author_is_one_line_from_a_name_or_an_object(self):
        def author(value):
            folder = self.root / f"p{len(list(self.root.iterdir()))}"
            body = {"name": "radish-tools", **({} if value is None else {"author": value})}
            return manifest.read(write_plugin(folder, mcp=False, workflow=False,
                                              manifest_text=json.dumps(body))).summary()["author"]
        self.assertEqual(author("Radish Software  Labs"), "Radish Software Labs")
        self.assertEqual(author({"name": "Radish Software Labs", "email": "dev@mobster.dev"}), "Radish Software Labs")
        self.assertEqual(author({"email": "dev@mobster.dev"}), "")
        self.assertEqual(author(None), "")

    def test_a_skill_description_can_be_a_yaml_block_or_a_loose_line(self):
        def description(frontmatter):
            folder = write_plugin(self.root / f"p{len(list(self.root.iterdir()))}", mcp=False, workflow=False)
            (folder / "skills" / "pr-triage" / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\nBody.\n")
            return manifest.read(folder).summary()["skills"][0]["description"]
        self.assertEqual(description("name: box\ndescription: >\n  Use the Box CLI for\n  shared links."),
                         "Use the Box CLI for shared links.")
        self.assertEqual(description("name: box\ndescription: |\n  Line one\n  line two"), "Line one line two")
        self.assertEqual(description("name: box\ndescription: Use when: the task names Box"),
                         "Use when: the task names Box")
        self.assertEqual(description('name: box\ndescription: "Quoted, with: a colon"'), "Quoted, with: a colon")

    def test_a_bad_manifest_is_refused_with_a_sentence(self):
        for text, said in (('{"name": "Bad Name!"}', "plugin.json doesn't fit the plugin format at name."),
                           ("not json", "plugin.json isn't valid JSON."),
                           ('{"name": "install"}', "A plugin can't be called install. Rename it in plugin.json.")):
            with self.subTest(text=text):
                folder = write_plugin(self.root / text[:4].strip('{"'), manifest_text=text)
                with self.assertRaises(manifest.PluginError) as caught:
                    manifest.read(folder)
                self.assertEqual(str(caught.exception), said)
        with self.assertRaises(manifest.PluginError) as caught:
            manifest.read(self.root / "nothing-here")
        self.assertIn("has no .claude-plugin/plugin.json", str(caught.exception))
        empty = write_plugin(self.root / "empty", mcp=False, skill=False, workflow=False)
        with self.assertRaises(manifest.PluginError) as caught:
            manifest.read(empty)
        self.assertIn("adds nothing", str(caught.exception))

    def test_a_server_header_other_than_authorization_is_refused(self):
        folder = write_plugin(self.root, mcp=False)
        (folder / ".mcp.json").write_text(json.dumps({"mcpServers": {"web": {
            "type": "http", "url": "https://mcp.example.com/mcp", "headers": {"X-Api-Key": "${KEY}"}}}}))
        with self.assertRaises(manifest.PluginError) as caught:
            manifest.read(folder)
        self.assertIn("Mobster sends only Authorization", str(caught.exception))


class StoreTests(unittest.TestCase):
    def test_a_version_1_table_gains_the_author_and_keeps_its_rows(self):
        import sqlite3

        from mobile_agent.plugins import store
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("""CREATE TABLE plugins (name TEXT PRIMARY KEY, version TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '', source TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, path TEXT NOT NULL,
            skills TEXT, added TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL)""")  # noqa: E501
        connection.execute("INSERT INTO plugins(name, source, path, created_at, updated_at) VALUES ('old', 'x', '/p', "
                           "1, 1)")
        store.migrate(connection, 1)
        store.migrate(connection, 1)
        self.assertEqual(connection.execute("SELECT name, author FROM plugins").fetchall(), [("old", "")])


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mobster-plugin-src-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_a_zip_with_the_plugin_in_one_folder_unpacks(self):
        data = zip_folder(write_plugin(self.root / "src"), prefix="radish-tools/")
        into = sources.staging()
        self.addCleanup(shutil.rmtree, into, True)
        root = sources.from_zip(data, into)
        self.assertEqual(manifest.read(root).name, "radish-tools")

    def test_a_zip_that_escapes_its_folder_or_holds_a_link_is_refused(self):
        for entries, said in (([("../evil.txt", b"x")], "outside its folder"),
                              ([("/etc/evil", b"x")], "outside its folder"),
                              ([("link", b"/etc/passwd", 0o120777)], "a link or a special file")):
            with self.subTest(entries=entries[0][0]):
                into = sources.staging()
                self.addCleanup(shutil.rmtree, into, True)
                with self.assertRaises(manifest.PluginError) as caught:
                    sources.from_zip(zipped(entries), into)
                self.assertIn(said, str(caught.exception))
                self.assertFalse((self.root.parent / "evil.txt").exists())

    def test_github_is_downloaded_as_its_tarball_over_https(self):
        folder = write_plugin(self.root / "repo")
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w:gz") as archive:
            for path in folder.rglob("*"):
                if path.is_file():
                    archive.add(path, arcname="radish-tools-main/" + str(path.relative_to(folder)))
        asked = []

        class Answer(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(request, timeout=None):
            asked.append(request.full_url)
            return Answer(data.getvalue())
        into = sources.staging()
        self.addCleanup(shutil.rmtree, into, True)
        root = sources.from_github("RadishSoftware/radish-tools@main", into, opener)
        self.assertEqual(asked, ["https://codeload.github.com/RadishSoftware/radish-tools/tar.gz/main"])
        self.assertEqual(manifest.read(root).name, "radish-tools")
        for text in ("getmobster", "getmobster/../x", "http://github.com/a/b"):
            with self.assertRaises(manifest.PluginError):
                sources.parse_repo(text)
        self.assertEqual(sources.parse_repo("https://github.com/RadishSoftware/tools.git"), ("RadishSoftware",
            "tools", "HEAD"))


def skill_rule(t):
    from mobile_agent.tests import sota_world as world
    return world.done("Done.")


class PluginTasks(Service):
    def setUp(self):
        from mobile_agent.tests import sota_world as world
        self.keys = Path(tempfile.mkdtemp(prefix="mobster-plugin-keys-")) / "items.json"
        self.addCleanup(shutil.rmtree, self.keys.parent, True)
        for patcher in (mock.patch.object(connector_vault, "backend", lambda: connector_vault.FileVault(self.keys)),
                        mock.patch.object(world, "decide", skill_rule)):
            patcher.start()
            self.addCleanup(patcher.stop)
        super().setUp()
        self.src = Path(tempfile.mkdtemp(prefix="mobster-plugin-folder-"))
        self.addCleanup(shutil.rmtree, self.src, True)
        self.log = self.src.parent / f"{self.src.name}-calls.jsonl"
        self.addCleanup(lambda: self.log.unlink(missing_ok=True))

    def preview(self, body):
        status, data = self.call("POST", "/api/plugins/preview", body)
        self.assertEqual(status, 200, data)
        return data

    def install(self, **kwargs):
        folder = write_plugin(self.src / "radish-tools", **kwargs)
        preview = self.preview({"path": str(folder)})
        status, data = self.call("POST", "/api/plugins/install", {"preview": preview["preview"],
                                                                  "values": {"RADISH_LOG": str(self.log)}})
        self.assertEqual(status, 201, data)
        return data["plugin"]

    def task(self, goal, origin_header="app"):
        status, data = self.call("POST", "/api/runs", {"goal": goal, "appId": "any", "mode": "live"},
                                 origin=origin_header)
        self.assertIn(status, (200, 201), data)
        return self.finished(data["run"]["id"])

    def connectors(self):
        return self.call("GET", "/api/connectors")[1]["connectors"]

    def workflows(self):
        return self.call("GET", "/api/workflows")[1]["workflows"]

    def test_preview_shows_what_it_adds_and_installs_nothing_yet(self):
        preview = self.preview({"path": str(write_plugin(self.src / "radish-tools"))})
        plugin = preview["plugin"]
        self.assertEqual((plugin["name"], plugin["version"], plugin["author"]), ("radish-tools", "1.2.0",
            "Radish Software"))
        self.assertEqual(plugin["connectors"][0]["name"], "getmobster")
        self.assertEqual([a["key"] for a in plugin["asks"]], ["RADISH_LOG"])
        self.assertEqual((self.connectors(), self.call("GET", "/api/plugins")[1]["plugins"]), ([], []))

    def test_a_plugins_server_arrives_as_a_connector_whose_tools_all_ask(self):
        plugin = self.install()
        self.assertEqual((plugin["connectors"], plugin["workflows"], plugin["skills"][0]["name"]), (1, 1, "pr-triage"))
        [connector] = self.connectors()
        self.assertEqual((connector["name"], connector["plugin"], connector["transport"]),
                         ("getmobster", "radish-tools", "stdio"))
        self.assertEqual(len(connector["tools"]), 7)
        self.assertEqual({t["mode"] for t in connector["tools"]}, {"ask"})
        env = connector_vault.FileVault(self.keys).get(connector["id"])["env"]
        self.assertEqual(env["FAKE_MCP_LOG"], str(self.log))
        self.assertTrue(env["RADISH_HOME"].endswith("/plugins/radish-tools"))
        [workflow] = self.workflows()
        self.assertEqual((workflow["name"], workflow["enabled"]), ("Triage pull requests", False))
        self.assertNotIn(str(self.log), json.dumps(self.call("GET", "/api/plugins")[1]))

    def test_a_skill_reaches_only_a_task_it_matches(self):
        self.install()
        self.task("Triage my GitHub pull requests from this week")
        prompt = self.prompts("Triage my GitHub pull requests")[0]
        self.assertIn("Instructions from the plugin radish-tools you installed (its skill pr-triage):\nTRIAGE-MARKER",
                      prompt)
        self.task("What iOS version is this iPhone on?")
        self.assertNotIn("TRIAGE-MARKER", self.prompts("What iOS version")[0])

    def test_an_outside_agents_task_gets_no_skill(self):
        self.install()
        run = self.runtime.create("any", "Triage my GitHub pull requests now", "live", origin="mcp")
        self.finished(run["id"] if isinstance(run, dict) else run.id)
        self.assertNotIn("TRIAGE-MARKER", self.prompts("Triage my GitHub pull requests now")[0])

    def test_turning_a_plugin_off_removes_its_tools_workflows_and_skills(self):
        self.install()
        status, data = self.call("POST", "/api/plugins/radish-tools", {"enabled": False})
        self.assertEqual(status, 200, data)
        self.assertEqual((data["plugin"]["enabled"], data["plugin"]["workflows"], data["plugin"]["author"]),
                         (False, 1, "Radish Software"))
        self.assertEqual(self.workflows(), [])
        self.assertFalse(self.connectors()[0]["enabled"])
        self.task("Triage my GitHub pull requests today")
        prompt = self.prompts("Triage my GitHub pull requests today")[0]
        self.assertNotIn("TRIAGE-MARKER", prompt)
        self.assertNotIn("Connected services", prompt)
        status, data = self.call("POST", "/api/plugins/radish-tools", {"enabled": True})
        self.assertEqual(status, 200, data)
        self.assertEqual([w["name"] for w in self.workflows()], ["Triage pull requests"])
        self.assertTrue(self.connectors()[0]["enabled"])

    def test_turning_a_plugin_on_fails_whole_when_its_installed_copy_is_gone(self):
        self.install()
        self.call("POST", "/api/plugins/radish-tools", {"enabled": False})
        home = connector_vault.FileVault(self.keys).get(self.connectors()[0]["id"])["env"]["RADISH_HOME"]
        shutil.rmtree(home)
        status, data = self.call("POST", "/api/plugins/radish-tools", {"enabled": True})
        self.assertEqual(status, 400, data)
        self.assertFalse(self.connectors()[0]["enabled"])
        self.assertEqual(self.workflows(), [])
        [plugin] = self.call("GET", "/api/plugins")[1]["plugins"]
        self.assertFalse(plugin["enabled"])

    def test_removing_a_plugin_leaves_no_connector_workflow_or_keychain_item(self):
        self.install()
        status, _ = self.call("DELETE", "/api/plugins/radish-tools")
        self.assertEqual(status, 200)
        self.assertEqual((self.connectors(), self.workflows()), ([], []))
        self.assertEqual(connector_vault.FileVault(self.keys)._load(), {})
        self.assertEqual(self.call("GET", "/api/plugins")[1]["plugins"], [])

    def test_a_missing_value_or_a_second_copy_installs_nothing(self):
        preview = self.preview({"path": str(write_plugin(self.src / "radish-tools"))})
        status, data = self.call("POST", "/api/plugins/install", {"preview": preview["preview"], "values": {}})
        self.assertEqual(status, 400, data)
        self.assertEqual(data["error"], "Fill in RADISH_LOG to install this plugin")
        self.assertEqual((self.connectors(), self.workflows()), ([], []))
        self.install()
        again = self.preview({"path": str(self.src / "radish-tools")})
        self.assertTrue(again["installed"])
        status, data = self.call("POST", "/api/plugins/install", {"preview": again["preview"],
                                                                  "values": {"RADISH_LOG": "x"}})
        self.assertEqual((status, data["code"]), (409, "plugin_installed"))
        self.assertEqual(len(self.connectors()), 1)

    def test_a_connector_name_already_taken_undoes_the_whole_install(self):
        status, _ = self.call("POST", "/api/connectors", {"name": "getmobster", "url": "https://mcp.getmobster.example/mcp"})
        self.assertEqual(status, 201)
        preview = self.preview({"path": str(write_plugin(self.src / "radish-tools"))})
        status, data = self.call("POST", "/api/plugins/install", {"preview": preview["preview"],
                                                                  "values": {"RADISH_LOG": "x"}})
        self.assertEqual(status, 400, data)
        self.assertIn("already have a connector called getmobster", data["error"])
        self.assertEqual([c["name"] for c in self.connectors()], ["getmobster"])
        self.assertEqual((self.workflows(), self.call("GET", "/api/plugins")[1]["plugins"]), ([], []))

    def test_a_zip_uploaded_from_the_app_previews(self):
        data = zip_folder(write_plugin(self.src / "radish-tools"))
        status, answer = self.call("POST", "/api/plugins/preview-zip", raw=data)
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["plugin"]["name"], "radish-tools")
        status, answer = self.call("POST", "/api/plugins/preview-zip", raw=zipped([("../x", b"x")]))
        self.assertEqual((status, answer["code"]), (400, "plugin_invalid"))

    def test_a_path_with_nothing_there_names_what_was_asked_for(self):
        status, data = self.call("POST", "/api/plugins/preview", {"path": str(self.src / "radish-tools.zip")})
        self.assertEqual((status, data["error"]), (400, "There's no .zip at that path. Check it and try again."))
        status, data = self.call("POST", "/api/plugins/preview", {"path": str(self.src / "radish-tools")})
        self.assertEqual((status, data["error"]), (400, "There's no folder at that path. Check it and try again."))
        self.assertEqual(self.preview({"path": str(write_plugin(self.src / "getmobster.zip"))})["plugin"]["name"],
                         "radish-tools")


class AppSessionTests(unittest.TestCase):
    def test_installing_needs_the_mac_apps_session(self):
        runtime = SimpleNamespace(local_api={"app_session": "s"})
        with self.assertRaises(APIError) as caught:
            plugin_routes.needs_app(SimpleNamespace(runtime=runtime, app_session=False))
        self.assertEqual(caught.exception.status, 403)

    def test_a_skill_matches_its_name_or_two_of_its_words(self):
        skill = {"name": "pr-triage", "description": "Triage GitHub pull requests and review comments"}
        self.assertTrue(plugin_skills.matches(skill, "Do my pr triage"))
        self.assertTrue(plugin_skills.matches(skill, "Look at the pull requests waiting for review"))
        self.assertFalse(plugin_skills.matches(skill, "Text Sam that I'm late"))
        self.assertFalse(plugin_skills.matches(skill, "Open GitHub"))


if __name__ == "__main__":
    unittest.main()
