"""Workflows as files (mobile_agent/projects/workflow_files.py, sync.py): a project's .mobster/workflows/*.yaml and the
workflows Mobster runs, kept in step both ways, with every conflict left for you to settle and nothing a project
mustn't hold (a real iPhone, a secret) ever written.

The real Workflows on a fake runtime and a real journal, real files in a temp folder; each test calls the sync's
look by hand instead of waiting for its loop. No phone, no network.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mobile_agent import api_routes, harness_api
from mobile_agent import journal as journal_module
from mobile_agent.api_errors import APIError
from mobile_agent.graph import dispatch as graph_dispatch
from mobile_agent.projects import routes, workflow_files
from mobile_agent.projects import service as project_service
from mobile_agent.projects import store as project_store
from mobile_agent.projects.service import ProjectService
from mobile_agent.tests.seam_support import isolate
from mobile_agent.tests.test_workflows import FakeRuntime
from mobile_agent.workflows.service import Workflows

GRAPH = {"version": 2, "inputs": [{"name": "who", "type": "string", "default": "Sam"}], "vars": {},
         "nodes": [
             {"id": "start", "type": "trigger.manual", "position": {"x": 0, "y": 0}},
             {"id": "read", "type": "phone.task", "position": {"x": 0, "y": 152},
              "config": {"text": "Open Instagram and read the newest DM from {who}"}},
             {"id": "check", "type": "branch", "position": {"x": 0, "y": 304},
              "config": {"cases": [{"id": "asked", "label": "A question", "when": "contains($input.answer, '?')"}]}},
             {"id": "tell", "type": "send", "position": {"x": 0, "y": 456}, "config": {"to": "notification"}}],
         "edges": [
             {"id": "e1", "source": "start", "sourcePort": "out", "target": "read", "targetPort": "in"},
             {"id": "e2", "source": "read", "sourcePort": "out", "target": "check", "targetPort": "in"},
             {"id": "e3", "source": "check", "sourcePort": "asked", "target": "tell", "targetPort": "in"}],
         "limits": {"maxSpendUsd": 2, "maxMinutes": 60}}

FILE = """version: 1
name: Answer DMs
nodes:
- id: start
  type: trigger.manual
  at: [0, 0]
- id: read
  type: phone.task
  at: [0, 152]
  config:
    text: Open Instagram and read the newest DM
edges:
- from: start
  to: read
"""


class Base(unittest.TestCase):
    def setUp(self):
        isolate(self)
        self.dir = tempfile.TemporaryDirectory(prefix="mobster-project-workflows-")
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name).resolve()
        self.home = self.root / "home"
        self.app = self.home / "Developer" / "Dailies"
        (self.app / "Dailies.xcodeproj").mkdir(parents=True)
        self.folder = self.app / ".mobster" / "workflows"
        self.folder.mkdir(parents=True)
        with harness_api.owned_by("projects"):
            journal_module.register_schema(project_store.SCHEMA, project_store.VERSION, project_store.migrate)
        self.journal = journal_module.Journal(self.root / "runs.sqlite3")
        self.addCleanup(self.journal.close)
        self.runtime = FakeRuntime(self.journal)
        self.runtime.workflows = self.workflows = Workflows(self.runtime)
        self.events = []
        self.runtime.publish = lambda topic, event: self.events.append((topic, event))
        clock = iter(range(1000, 100000))
        self.service = ProjectService(self.runtime, home=self.home, data_dir=self.root / "data", last_runs=lambda: {},
                                      clock=lambda: next(clock))
        self.addCleanup(self.service.close)
        self.sync = self.service.sync
        self.project, _ = self.service.add({"path": str(self.app)})

    def look(self):
        self.sync.tick()

    def write(self, name, text):
        path = self.folder / name
        path.write_text(text)
        return path

    def only(self):
        listed = self.workflows.list()
        self.assertEqual(len(listed), 1, [w["name"] for w in listed])
        return listed[0]

    def record(self, identifier):
        return self.workflows._get(identifier)

    def file_events(self):
        return [event for topic, event in self.events if topic == "workflows" and event["event"] == "workflow_file"]


class FormatTests(unittest.TestCase):
    def setUp(self):
        isolate(self)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.journal = journal_module.Journal(Path(self.dir.name) / "runs.sqlite3")
        self.addCleanup(self.journal.close)
        self.workflows = Workflows(FakeRuntime(self.journal))

    def test_a_graph_goes_to_a_file_a_review_can_read_and_comes_back_the_same(self):
        saved = self.workflows.create({"name": "Answer DMs", "graph": GRAPH})
        graph = self.workflows._get(saved["id"])["graph"]
        text = workflow_files.render("Answer DMs", graph)
        self.assertTrue(text.startswith("# A Mobster workflow."))
        self.assertIn("  at: [0, 152]\n", text)
        self.assertIn("- from: check.asked\n  to: tell\n", text)
        self.assertNotIn("mode:", text)          # at its default
        self.assertNotIn("device:", text)
        self.assertNotIn("e1", text)             # edges are their ends
        self.assertIn("maxSpendUsd: 2\n", text)
        name, parsed = workflow_files.parse(text, ".mobster/workflows/answer-dms.yaml", previous=graph)
        self.assertEqual(name, "Answer DMs")
        self.assertEqual(self.workflows.check_graph(parsed, saved["id"]), graph)

    def test_moving_a_node_changes_one_line(self):
        saved = self.workflows.create({"name": "Answer DMs", "graph": GRAPH})
        graph = self.workflows._get(saved["id"])["graph"]
        moved = json.loads(json.dumps(graph))
        moved["nodes"][1]["position"] = {"x": 40, "y": 160}
        before = workflow_files.render("Answer DMs", graph).splitlines()
        after = workflow_files.render("Answer DMs", moved).splitlines()
        self.assertEqual([(a, b) for a, b in zip(before, after) if a != b], [("  at: [0, 152]", "  at: [40, 160]")])

    def test_file_keeps_required_inputs_and_scalar_defaults(self):
        graph = json.loads(json.dumps(GRAPH))
        graph["inputs"] = [
            {"name": "who", "type": "string", "required": True},
            {"name": "enabled", "type": "boolean", "default": False, "required": False},
            {"name": "count", "type": "number", "default": 0, "required": True},
        ]
        saved = self.workflows.create({"name": "Answer DMs", "graph": graph})
        canonical = self.workflows._get(saved["id"])["graph"]
        text = workflow_files.render("Answer DMs", canonical)
        _, parsed = workflow_files.parse(text, "answer-dms.yaml")
        checked = self.workflows.check_graph(parsed, saved["id"])
        self.assertEqual(checked["inputs"], canonical["inputs"])
        self.assertIn("required: true", text)
        self.assertIn("required: false", text)
        self.assertIn("default: false", text)
        self.assertIn("default: 0", text)
        with self.assertRaisesRegex(ValueError, "who"):
            graph_dispatch.check_values(checked, {}, None)

    def test_a_file_that_isnt_a_workflow_says_where(self):
        for text, expected in (("nodes: [\n", "line 2"), ("- a\n", "isn't a workflow"),
                               ("version: 1\nname: X\nnodes: []\nedges: []\nschedule: daily\n", "'schedule'"),
                               ("version: 2\nname: X\n", "version 2"), ("version: 1\nname: ''\n", "name is"),
                               ("version: 1\nname: X\nnodes:\n- {id: a, type: trigger.manual, at: 3}\n",
                                "at is [x, y]"),
                               ("version: 1\nname: X\nnodes:\n- {id: a, type: trigger.manual}\n"
                                "edges:\n- {from: a, to: b}\n", "no node has that id"),
                               ("version: 1\nname: X\nnodes:\n- {id: a, type: trigger.manual, colour: red}\n",
                                "'colour'")):
            with self.assertRaises(workflow_files.FileError, msg=text) as caught:
                workflow_files.parse(text, "w.yaml")
            self.assertIn(expected, str(caught.exception), text)
            self.assertTrue(str(caught.exception).startswith("w.yaml"), text)

    def test_a_graph_that_names_an_iphone_or_holds_a_secret_never_goes_in_a_repository(self):
        graph = json.loads(json.dumps(GRAPH))
        self.assertIsNone(workflow_files.blocked(graph))
        graph["nodes"][1]["config"]["device"] = "00008110-001A2B3C4D5E6F7G"
        self.assertIn("never name a real phone", workflow_files.blocked(graph))
        for device in ("any", "loop", None):
            graph["nodes"][1]["config"]["device"] = device
            self.assertIsNone(workflow_files.blocked(graph))
        graph["nodes"][1]["config"]["text"] = "Sign in with sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123"
        self.assertIn("looks like a key", workflow_files.blocked(graph))
        graph["nodes"][1]["config"]["text"] = "Sign in with the test password from the project's secrets"
        self.assertIsNone(workflow_files.blocked(graph))

    def test_secrets_in_defaults_variables_and_notes_cannot_be_exported(self):
        key = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123"
        for field in ("default", "vars", "notes", "nested"):
            with self.subTest(field=field):
                graph = json.loads(json.dumps(GRAPH))
                if field == "default":
                    graph["inputs"][0]["default"] = key
                elif field == "vars":
                    graph["vars"] = {"token": key}
                elif field == "notes":
                    graph["nodes"][1]["notes"] = key
                else:
                    value = key
                    for _ in range(12):
                        value = {"nested": value}
                    graph["vars"] = {"token": value}
                self.assertIn("looks like a key", workflow_files.blocked(graph) or "")

    def test_file_names_are_the_workflows_name_and_never_collide(self):
        self.assertEqual(workflow_files.slug("Answer DMs!"), "answer-dms.yaml")
        self.assertEqual(workflow_files.slug("Answer DMs", {"answer-dms.yaml", "answer-dms-2.yaml"}),
                         "answer-dms-3.yaml")
        self.assertEqual(workflow_files.slug("¿?"), "workflow.yaml")


class FileToMobsterTests(Base):
    def test_a_new_file_becomes_a_published_workflow_with_its_schedule_off(self):
        self.write("answer-dms.yaml", FILE)
        self.look()
        workflow = self.only()
        self.assertEqual((workflow["name"], workflow["enabled"], workflow["draft"]), ("Answer DMs", False, False))
        self.assertEqual(workflow["file"]["path"], ".mobster/workflows/answer-dms.yaml")
        self.assertEqual((workflow["file"]["state"], workflow["file"]["projectName"]), ("synced", "Dailies"))
        self.assertNotIn("hash", workflow["file"])
        self.assertEqual(self.file_events()[-1]["state"], "synced")
        self.look()
        self.assertEqual(len(self.workflows.list()), 1)            # looking again changes nothing

    def test_an_edited_file_is_published_as_a_new_revision_and_the_old_one_stays_in_versions(self):
        path = self.write("answer-dms.yaml", FILE)
        self.look()
        before = self.only()
        path.write_text(FILE.replace("read the newest DM", "read the three newest DMs"))
        self.look()
        after = self.only()
        self.assertEqual(after["revision"], before["revision"] + 1)
        self.assertEqual(after["published"]["revision"], after["revision"])
        self.assertIn("three newest", after["graph"]["nodes"][1]["config"]["text"])
        self.assertEqual(len(self.workflows.history.versions(after["id"])), 2)

    def test_a_file_that_doesnt_parse_or_check_is_listed_with_its_problem_and_taken_once_fixed(self):
        path = self.write("broken.yaml", "version: 1\nname: Broken\nnodes: [\n")
        self.look()
        self.assertEqual(self.workflows.list(), [])
        listed = self.service.get(self.project["id"])["workflowFiles"]
        self.assertEqual(listed[0]["path"], ".mobster/workflows/broken.yaml")
        self.assertEqual(listed[0]["state"], "invalid")
        self.assertIn("line", listed[0]["problem"])
        path.write_text(FILE.replace("trigger.manual", "trigger.sometimes"))
        self.look()
        self.assertEqual(self.workflows.list(), [])
        problem = self.service.get(self.project["id"])["workflowFiles"][0]["problem"]
        self.assertIn(".mobster/workflows/broken.yaml", problem)
        path.write_text(FILE)
        self.look()
        self.assertEqual(self.only()["file"]["state"], "synced")
        self.assertEqual(self.service.get(self.project["id"])["workflowFiles"][0]["state"], "synced")

    def test_a_bad_edit_to_a_synced_file_leaves_the_workflow_running_as_it_was(self):
        path = self.write("answer-dms.yaml", FILE)
        self.look()
        before = self.only()
        path.write_text(FILE.replace("- from: start\n  to: read\n", "- from: start\n  to: nowhere\n"))
        self.look()
        after = self.only()
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["file"]["state"], "invalid")
        self.assertIn("nowhere", after["file"]["problem"])
        path.write_text(FILE)
        self.look()
        self.assertEqual(self.only()["file"]["state"], "synced")

    def test_an_edit_keeps_the_canvas_edge_ids_and_pins_for_what_didnt_change(self):
        path = self.write("answer-dms.yaml", FILE)
        self.look()
        workflow = self.only()
        graph = json.loads(json.dumps(workflow["graph"]))
        graph["edges"][0]["id"] = "canvas1"
        graph["nodes"][1]["pin"] = {"out": {"answer": "hi"}}
        self.workflows.update(workflow["id"], {"revision": workflow["revision"], "graph": graph, "publish": True})
        self.look()                                                   # Mobster writes it
        path.write_text(path.read_text().replace("at: [0, 152]", "at: [0, 200]"))
        self.look()
        after = self.record(workflow["id"])
        self.assertEqual(after["graph"]["edges"][0]["id"], "canvas1")
        self.assertEqual(after["graph"]["nodes"][1]["pin"], {"out": {"answer": "hi"}})
        self.assertEqual(after["graph"]["nodes"][1]["position"], {"x": 0, "y": 200})


class MobsterToFileTests(Base):
    def setUp(self):
        super().setUp()
        self.path = self.write("answer-dms.yaml", FILE)
        self.look()
        self.workflow = self.only()

    def edit(self, publish, text="Read every DM"):
        current = self.only()
        graph = json.loads(json.dumps(current["graph"]))
        graph["nodes"][1]["config"]["text"] = text
        return self.workflows.update(current["id"], {"revision": current["revision"], "graph": graph,
                                                     "publish": publish})

    def test_publishing_writes_the_file_and_a_draft_never_does(self):
        self.edit(publish=False, text="A draft only")
        self.look()
        self.assertEqual(self.path.read_text(), FILE)
        self.assertEqual(self.only()["file"]["state"], "synced")
        self.edit(publish=True, text="Read every DM")
        self.look()
        self.assertIn("text: Read every DM", self.path.read_text())
        self.assertTrue(self.path.read_text().startswith(workflow_files.HEADER))
        self.assertEqual(self.only()["file"]["revision"], self.only()["published"]["revision"])
        self.look()
        self.assertEqual(self.only()["file"]["state"], "synced")       # its own write isn't read as an edit

    def test_a_rename_rewrites_the_name_in_the_file_and_keeps_its_path(self):
        current = self.only()
        self.workflows.update(current["id"], {"revision": current["revision"], "name": "Reply to DMs"})
        self.look()
        self.assertIn("name: Reply to DMs\n", self.path.read_text())
        self.assertEqual(self.only()["file"]["path"], ".mobster/workflows/answer-dms.yaml")

    def test_a_graph_that_names_an_iphone_is_blocked_and_the_file_is_left_as_it_was(self):
        with mock.patch("mobile_agent.projects.sync.blocked", return_value="names a real phone"):
            self.edit(publish=True)
            self.look()
        self.assertEqual(self.path.read_text(), FILE)
        file = self.only()["file"]
        self.assertEqual((file["state"], file["problem"]), ("blocked", "names a real phone"))

    def test_a_deleted_file_is_missing_until_the_workflow_is_published_again(self):
        self.path.unlink()
        self.look()
        self.assertEqual(self.only()["file"]["state"], "missing")
        self.assertEqual(len(self.workflows.list()), 1)
        self.edit(publish=True)
        self.look()
        self.assertTrue(self.path.is_file())
        self.assertEqual(self.only()["file"]["state"], "synced")


class ConflictTests(Base):
    def setUp(self):
        super().setUp()
        self.path = self.write("answer-dms.yaml", FILE)
        self.look()

    def edit(self, publish):
        current = self.only()
        graph = json.loads(json.dumps(current["graph"]))
        graph["nodes"][1]["config"]["text"] = "Mobster's words"
        return self.workflows.update(current["id"], {"revision": current["revision"], "graph": graph,
                                                     "publish": publish})

    def test_a_file_changed_under_a_draft_is_a_conflict_and_nothing_is_overwritten(self):
        self.edit(publish=False)
        self.path.write_text(FILE.replace("read the newest DM", "the file's words"))
        self.look()
        workflow = self.only()
        self.assertEqual(workflow["file"]["state"], "conflict")
        self.assertIn("Keep the file's version or Mobster's", workflow["file"]["problem"])
        self.assertEqual(workflow["graph"]["nodes"][1]["config"]["text"], "Mobster's words")
        self.assertIn("the file's words", self.path.read_text())
        self.look()
        self.assertEqual(self.only()["file"]["state"], "conflict")     # it waits

    def test_keep_the_file_publishes_it_and_the_draft_stays_in_versions(self):
        self.edit(publish=False)
        self.path.write_text(FILE.replace("read the newest DM", "the file's words"))
        self.look()
        identifier = self.only()["id"]
        resolved = self.sync.resolve(identifier, "file")
        self.assertEqual(resolved["file"]["state"], "synced")
        self.assertIn("the file's words", resolved["graph"]["nodes"][1]["config"]["text"])
        self.assertFalse(resolved["draft"])
        texts = [self.workflows.history.version(identifier, item["revision"])["graph"]["nodes"][1]["config"]["text"]
                 for item in self.workflows.history.versions(identifier)]
        self.assertIn("Mobster's words", texts)

    def test_keep_mobster_publishes_the_draft_over_the_file(self):
        self.edit(publish=False)
        self.path.write_text(FILE.replace("read the newest DM", "the file's words"))
        self.look()
        resolved = self.sync.resolve(self.only()["id"], "mobster")
        self.assertEqual(resolved["file"]["state"], "synced")
        self.assertIn("Mobster's words", self.path.read_text())
        self.assertFalse(resolved["draft"])

    def test_a_file_changed_after_mobster_published_is_a_conflict_too(self):
        self.edit(publish=True)
        self.path.write_text(FILE.replace("read the newest DM", "the file's words"))
        self.look()
        self.assertEqual(self.only()["file"]["state"], "conflict")
        self.assertIn("the file's words", self.path.read_text())

    def test_resolve_refuses_what_it_cant_do(self):
        with self.assertRaises(ValueError):
            self.sync.resolve(self.only()["id"], "both")
        other = self.workflows.create({"name": "No file", "graph": GRAPH})
        with self.assertRaises(APIError) as caught:
            self.sync.resolve(other["id"], "file")
        self.assertEqual(caught.exception.status, 404)


class AddAndDeleteTests(Base):
    def test_a_workflow_added_to_a_project_gets_its_own_file(self):
        self.write("answer-dms.yaml", FILE)
        self.look()
        made = self.workflows.create({"name": "Answer DMs", "graph": GRAPH})
        added = self.sync.add(self.project, made["id"])
        self.assertEqual((added["file"]["path"], added["file"]["state"]),
                         (".mobster/workflows/answer-dms-2.yaml", "pending"))
        self.look()
        written = self.folder / "answer-dms-2.yaml"
        self.assertTrue(written.is_file())
        self.assertEqual(self.workflows.get(made["id"])["file"]["state"], "synced")
        self.assertEqual(len(self.workflows.list()), 2)               # its own file isn't imported again
        with self.assertRaises(APIError) as caught:
            self.sync.add(self.project, made["id"])
        self.assertEqual(caught.exception.code, "exists")

    def test_pending_workflow_files_reserve_their_names(self):
        made = [self.workflows.create({"name": "Answer DMs", "graph": GRAPH}) for _ in range(2)]
        paths = [self.sync.add(self.project, workflow["id"])["file"]["path"] for workflow in made]
        self.assertEqual(paths, [".mobster/workflows/answer-dms.yaml", ".mobster/workflows/answer-dms-2.yaml"])
        self.look()
        self.assertEqual([self.workflows.get(workflow["id"])["file"]["state"] for workflow in made], ["synced"] * 2)

    def test_file_names_do_not_collide_with_an_uppercase_existing_file(self):
        original = self.write("ANSWER-DMS.yaml", FILE)
        self.look()
        made = self.workflows.create({"name": "Answer DMs", "graph": GRAPH})
        added = self.sync.add(self.project, made["id"])
        self.assertEqual(added["file"]["path"], ".mobster/workflows/answer-dms-2.yaml")
        self.look()
        self.assertEqual(original.read_text(), FILE)
        self.assertTrue((self.folder / "answer-dms-2.yaml").is_file())

    def test_deleting_the_workflow_deletes_its_file_only_when_it_is_as_mobster_left_it(self):
        path = self.write("answer-dms.yaml", FILE)
        self.look()
        self.workflows.delete(self.only()["id"])
        self.assertFalse(path.exists())
        path = self.write("other.yaml", FILE.replace("Answer DMs", "Other"))
        self.look()
        identifier = self.only()["id"]
        path.write_text(FILE.replace("Answer DMs", "Other, edited"))      # edited, not yet looked at
        self.workflows.delete(identifier)
        self.assertTrue(path.exists())

    def test_removing_the_project_keeps_its_workflows_without_files(self):
        path = self.write("answer-dms.yaml", FILE)
        self.look()
        self.service.forget(self.project["id"])
        self.assertIsNone(self.only()["file"])
        self.assertTrue(path.exists())


class FileBoundaryTests(Base):
    def test_a_workflow_folder_symlink_never_redirects_a_publish(self):
        path = self.write("answer-dms.yaml", FILE)
        self.look()
        workflow = self.only()
        outside = self.root / "outside"
        self.folder.rename(outside)
        self.folder.symlink_to(outside, target_is_directory=True)
        self.workflows.update(workflow["id"], {"revision": workflow["revision"], "name": "Changed"})
        self.look()
        self.assertEqual((outside / path.name).read_text(), FILE)
        self.assertEqual(self.only()["file"]["state"], "blocked")

    def test_a_file_symlink_is_not_imported(self):
        outside = self.root / "outside.yaml"
        outside.write_text(FILE)
        (self.folder / "answer-dms.yaml").symlink_to(outside)
        self.look()
        self.assertEqual(self.workflows.list(), [])

    def test_a_preexisting_temporary_symlink_never_redirects_a_write(self):
        outside = self.root / "outside.txt"
        outside.write_text("keep this")
        (self.folder / "answer-dms.yaml.tmp").symlink_to(outside)
        made = self.workflows.create({"name": "Answer DMs", "graph": GRAPH, "publish": True})
        self.sync.add(self.project, made["id"])
        self.look()
        self.assertEqual(outside.read_text(), "keep this")
        self.assertFalse((self.folder / "answer-dms.yaml").is_symlink())
        self.assertIn("name: Answer DMs", (self.folder / "answer-dms.yaml").read_text())

    def test_a_malformed_node_id_does_not_block_the_other_workflow_files(self):
        self.write("a-invalid.yaml", "version: 1\nname: Invalid\nnodes: [{id: [a], type: trigger.manual}]\n")
        self.write("z-valid.yaml", FILE)
        self.look()
        self.assertEqual(self.only()["name"], "Answer DMs")
        invalid = [item for item in self.sync.files(self.project["id"]) if item["state"] == "invalid"]
        self.assertEqual(len(invalid), 1)
        self.assertIn("id", invalid[0]["problem"])


class RunTests(Base):
    def test_a_project_workflows_tasks_start_in_its_project_and_others_dont(self):
        with harness_api.owned_by("projects"):
            harness_api.register_run_field("project", lambda value, runtime: value)
        self.write("answer-dms.yaml", FILE)
        self.look()
        linked = self.only()
        loose = self.workflows.create({"name": "Loose", "graph": GRAPH})
        self.workflows.start(linked["id"], "k-linked")
        self.workflows.start(loose["id"], "k-loose")
        # A graph's phone step starts a task keyed workflow:<id>:…
        extras = {call[3].split(":")[1]: call[4].get("extras") or {} for call in self.runtime.calls}
        self.assertEqual(extras[linked["id"]]["project"], {"id": self.project["id"]})
        self.assertNotIn("project", extras[loose["id"]])


class RouteTests(Base):
    def request(self, method, path, body=None, params=None, app_session=True):
        return api_routes.Request(runtime=self.runtime, method=method, path=path, params=params or {}, query={},
                                  headers={}, body=body, content_type="application/json", origin="app",
                                  app_session=app_session)

    def setUp(self):
        super().setUp()
        project_service._services[id(self.runtime)] = self.service
        self.addCleanup(project_service._services.pop, id(self.runtime), None)

    def test_add_and_resolve_need_the_mac_apps_session_and_check_their_bodies(self):
        made = self.workflows.create({"name": "Answer DMs", "graph": GRAPH})
        self.runtime.local_api = {"app_session": "s"}
        with self.assertRaises(APIError) as caught:
            routes.add_workflow(self.request("POST", "", {"workflowId": made["id"]}, {"id": self.project["id"]},
                                             app_session=False))
        self.assertEqual(caught.exception.code, "app_only")
        with self.assertRaises(ValueError):
            routes.add_workflow(self.request("POST", "", {"id": made["id"]}, {"id": self.project["id"]}))
        answer = routes.add_workflow(self.request("POST", "", {"workflowId": made["id"]}, {"id": self.project["id"]}))
        self.assertEqual(answer.value["workflow"]["file"]["state"], "pending")
        with self.assertRaises(ValueError):
            routes.resolve(self.request("POST", "", {"keep": "file", "also": 1}, {"id": made["id"]}))


if __name__ == "__main__":
    unittest.main()
