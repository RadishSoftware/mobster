import io
import json
from email.message import Message

from mobile_agent.graph import chain
from mobile_agent.journal import encode
from mobile_agent.server import make_handler
from mobile_agent.tests.test_workflows import WorkflowCase
from mobile_agent.workflows import runs as workflow_runs
from mobile_agent.workflows.service import Workflows


def node(ident, kind, config=None, **extra):
    return {"id": ident, "type": kind, "config": config or {}, "position": {"x": 0, "y": 0}, **extra}


def edge(ident, source, target, source_port="out", target_port="in"):
    return {"id": ident, "source": source, "sourcePort": source_port, "target": target, "targetPort": target_port}


FREE = {"version": 2, "nodes": [
    node("start", "trigger.manual"), node("a", "phone.task", {"text": "Read the weather"}),
    node("b", "phone.task", {"text": "Read the news"}), node("j", "merge"),
    node("s", "send", {"to": "clipboard"})],
    "edges": [edge("e1", "start", "a"), edge("e2", "start", "b"), edge("e3", "a", "j"), edge("e4", "b", "j"),
              edge("e5", "j", "s")]}
STEPS = [{"id": "a", "kind": "task", "text": "Open Messages and read {who: Sam}", "appId": None},
         {"id": "b", "kind": "wait", "seconds": 60}]


class GraphCase(WorkflowCase):
    def get(self, path):
        handler = object.__new__(make_handler(self.runtime))
        handler.headers = Message()
        handler.headers["Host"] = "127.0.0.1:8765"
        handler.path, handler.rfile, handler.wfile = path, io.BytesIO(), io.BytesIO()
        statuses = []
        handler.send_response = statuses.append
        handler.send_header = lambda *_: None
        handler.end_headers = lambda: None
        handler.do_GET()
        return statuses[-1], json.loads(handler.wfile.getvalue())

    def chained(self, **fields):
        return self.workflows.create({"name": "Two steps", "steps": STEPS, **fields})


class UpgradeTests(GraphCase):
    def test_a_version_3_record_becomes_a_published_linear_graph_with_no_loss(self):
        record = {"id": "0123456789ab", "name": "Morning", "appId": None, "appName": None,
                  "goal": "Open Messages and read {who: Sam}", "outputSchema": None, "outputFormat": "auto",
                  "engine": None, "device": None, "cron": "0 9 * * *", "timezone": "UTC", "enabled": True,
                  "revision": 7, "createdAt": 1, "updatedAt": 1, "nextAt": 2_000_000_000_000, "lastRunId": None,
                  "lastOutcome": None, "version": 3, "sameConversation": False, "homeThreadId": None,
                  "lastThreadId": None, "steps": STEPS, "repeat": {"kind": "times", "times": 2},
                  "limits": {"maxSpendUsd": 3.0, "maxMinutes": 60}}
        with self.journal.transaction() as connection:
            connection.execute("INSERT INTO workflows VALUES (?,?)", (record["id"], encode(record)))
        opened = Workflows(self.runtime, lambda: self.time).get(record["id"])
        self.assertEqual((opened["version"], opened["revision"], opened["published"]), (4, 7, {"revision": 7}))
        self.assertEqual((opened["steps"], opened["repeat"], opened["limits"]), (STEPS, record["repeat"],
                                                                                record["limits"]))
        self.assertEqual((opened["cron"], opened["timezone"], opened["goal"]), ("0 9 * * *", "UTC", record["goal"]))
        self.assertEqual(chain.as_chain(opened["graph"]),
                         (STEPS, record["repeat"], record["limits"], "0 9 * * *", "UTC"))
        self.assertFalse(opened["draft"])
        self.assertEqual(opened["blanks"], [{"name": "who", "default": "Sam"}])


class DraftTests(GraphCase):
    def test_a_graph_save_is_a_draft_until_it_is_published(self):
        saved = self.chained(cron="0 9 * * *", timezone="UTC")
        self.assertEqual((saved["published"], saved["draft"]), ({"revision": 1}, False))
        edited = json.loads(json.dumps(saved["graph"]))
        edited["nodes"][2]["config"]["text"] = "Open Messages and read {who: Kate}"
        edited["nodes"][1]["config"]["cron"] = "0 10 * * *"
        draft = self.workflows.update(saved["id"], {"revision": 1, "graph": edited})
        self.assertEqual((draft["revision"], draft["published"], draft["draft"]), (2, {"revision": 1}, True))
        self.assertEqual(draft["graph"], edited)
        self.assertEqual((draft["cron"], draft["steps"], draft["goal"]), ("0 9 * * *", STEPS, STEPS[0]["text"]))
        live = self.workflows.history.publish(saved["id"], {"revision": 2})
        self.assertEqual((live["published"], live["draft"], live["cron"]), ({"revision": 2}, False, "0 10 * * *"))
        self.assertEqual(live["goal"], "Open Messages and read {who: Kate}")
        listed = self.workflows.history.versions(saved["id"])
        self.assertEqual([(v["revision"], v["published"], v["live"])
                          for v in listed], [(2, True, True), (1, True, False)])
        self.assertEqual(listed[0]["change"], "Changed 2 steps")
        diff = self.workflows.history.diff(saved["id"], 1, 2)
        self.assertEqual([item["id"] for item in diff["nodes"]["changed"]], ["schedule", "a"])

    def test_the_card_builder_publishes_as_it_always_has_and_keeps_the_canvas_layout(self):
        saved = self.chained()
        moved = json.loads(json.dumps(saved["graph"]))
        moved["nodes"][1]["position"], moved["nodes"][1]["title"] = {"x": 400, "y": 80}, "Read it"
        self.workflows.update(saved["id"], {"revision": 1, "graph": moved, "publish": True})
        cards = self.workflows.update(saved["id"], {"revision": 2, "steps": STEPS[:1] + [
            {"id": "b", "kind": "wait", "seconds": 120}]})
        self.assertEqual((cards["published"], cards["draft"]), ({"revision": 3}, False))
        kept = {n["id"]: n for n in cards["graph"]["nodes"]}["a"]
        self.assertEqual((kept["position"], kept["title"]), ({"x": 400, "y": 80}, "Read it"))
        self.assertEqual(cards["steps"][1]["seconds"], 120)

    def test_a_free_graph_takes_its_schedule_from_the_old_fields_and_refuses_cards(self):
        saved = self.workflows.create({"name": "Two at once", "graph": FREE})
        self.assertEqual((saved["steps"], saved["repeat"], saved["goal"]),
                         (None, None, "Read the weather\nRead the news"))
        scheduled = self.workflows.update(saved["id"], {"revision": 1, "cron": "0 8 * * *", "timezone": "UTC"})
        trigger = next(n for n in scheduled["graph"]["nodes"] if n["type"] == "trigger.schedule")
        self.assertEqual(trigger["config"]["cron"], "0 8 * * *")
        wired = sorted(e["target"] for e in scheduled["graph"]["edges"] if e["source"] == trigger["id"])
        self.assertEqual(wired, ["a", "b"])                       # it starts what the manual trigger starts
        self.assertEqual(scheduled["cron"], "0 8 * * *")
        cleared = self.workflows.update(saved["id"], {"revision": 2, "cron": None})
        self.assertFalse(any(n["type"] == "trigger.schedule" for n in cleared["graph"]["nodes"]))
        with self.assertRaisesRegex(ValueError, "graph with more than steps in a row"):
            self.workflows.update(saved["id"], {"revision": 3, "steps": STEPS})
        with self.assertRaisesRegex(ValueError, "not both"):
            self.workflows.update(saved["id"], {"revision": 3, "graph": FREE, "steps": STEPS})

    def test_rollback_saves_an_old_version_as_the_newest_and_publishes_it(self):
        saved = self.chained()
        edited = json.loads(json.dumps(saved["graph"]))
        edited["nodes"][2]["config"]["seconds"] = 300
        self.workflows.update(saved["id"], {"revision": 1, "graph": edited, "publish": True, "name": "Renamed"})
        back = self.workflows.history.rollback(saved["id"], {"revision": 1, "current": 2})
        self.assertEqual((back["revision"], back["published"], back["steps"][1]["seconds"]), (3, {"revision": 3}, 60))
        self.assertEqual(back["name"], "Two steps")
        self.assertEqual([v["revision"] for v in self.workflows.history.versions(saved["id"])], [3, 2, 1])
        with self.assertRaises(Exception) as caught:
            self.workflows.history.rollback(saved["id"], {"revision": 1, "current": 2})
        self.assertEqual(caught.exception.code, "revision_conflict")


class RunTests(GraphCase):
    def test_a_step_this_mobster_cant_run_yet_is_refused_before_anything_is_claimed(self):
        from mobile_agent.graph import catalog
        catalog.register_node_type(catalog.NodeType("test.thing", "Test thing", "step", "A track's step.", {},
                                                    lambda node, shapes: {"out": {}}))
        self.addCleanup(catalog.TYPES.pop, "test.thing")
        graph = json.loads(json.dumps(FREE))
        graph["nodes"].append(node("x", "test.thing"))
        graph["edges"].append(edge("e9", "s", "x"))
        saved = self.workflows.create({"name": "A track's step", "graph": graph})
        with self.assertRaises(Exception) as caught:
            self.workflows.run(saved["id"], "k1")
        self.assertEqual(caught.exception.code, "graph_unsupported")
        self.assertIn("Test thing", str(caught.exception))
        self.assertEqual((self.runtime.calls, self.workflows.attempts(saved["id"])), ([], []))

    def test_a_draft_never_runs_and_the_published_graph_does(self):
        unpublished = self.workflows.create({"name": "Draft", "graph": chain.from_chain(STEPS[:1]), "publish": False})
        with self.assertRaises(Exception) as caught:
            self.workflows.run(unpublished["id"], "k1")
        self.assertEqual(caught.exception.code, "not_published")
        saved = self.workflows.create({"name": "One task", "goal": "Read the time"})
        draft = json.loads(json.dumps(saved["graph"]))
        draft["nodes"][1]["config"]["text"] = "Read the date"
        self.workflows.update(saved["id"], {"revision": 1, "graph": draft})
        self.workflows.run(saved["id"], "k2")
        self.assertEqual([call[1] for call in self.runtime.calls], ["Read the time"])

    def test_run_on_a_never_published_draft_is_a_test_run_for_a_person_and_a_refusal_for_anything_else(self):
        draft = self.workflows.create({"name": "Draft", "graph": chain.from_chain(STEPS[:1]), "publish": False})
        for origin in (None, "schedule", "workflow", "hook", "mcp"):
            with self.subTest(origin=origin), self.assertRaises(Exception) as caught:
                self.workflows.start(draft["id"], f"k-{origin}", origin=origin)
            self.assertEqual(caught.exception.code, "not_published")
        self.assertEqual(self.runtime.calls, [])
        self.assertEqual(workflow_runs.DRAFT_TESTERS, {"app", "tui", "cli"})   # a person at the app, terminal or CLI
        self.workflows.start(draft["id"], "person-app", origin="app")
        records = self.workflows.batch_runs(draft["id"])
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["test"])                                  # marked a test, never published history
        saved = self.workflows.create({"name": "One task", "goal": "Read the time"})
        self.workflows.start(saved["id"], "published", origin="app")
        self.assertFalse(any(record.get("test") for record in self.workflows.batch_runs(saved["id"])))


class RouteTests(GraphCase):
    def test_the_canvas_routes(self):
        status, data = self.get("/api/workflows/node-types")
        self.assertEqual(status, 200)
        self.assertIn("phone.task", {item["type"] for item in data["nodeTypes"]})
        self.assertEqual(data["limits"]["nodes"], 100)
        status, data = self.request("/api/workflows/validate", {"graph": {**FREE, "edges": FREE["edges"][:2]}})
        self.assertEqual((status, data["code"]), (400, "invalid_graph"))
        self.assertEqual({item.get("node") for item in data["errors"]}, {"j", "s"})
        status, data = self.request("/api/workflows/validate", {"graph": FREE})
        self.assertEqual((status, data["graph"]["nodes"][1]["config"]["mode"]), (200, "auto"))
        status, data = self.request("/api/workflows/evaluate", {"expression": "{= count($input.rows) * 2 }",
                                                                "scope": {"input": {"rows": [1, 2]}}})
        self.assertEqual((status, data), (200, {"value": 4, "type": "number"}))
        status, data = self.request("/api/workflows/evaluate", {"expression": "{= 1 / 0 }"})
        self.assertEqual((status, data["code"]), (400, "invalid_expression"))
        status, data = self.request("/api/workflows", {"name": "Graph", "graph": FREE})
        self.assertEqual(status, 201)
        ident = data["workflow"]["id"]
        status, data = self.get("/api/workflows")
        self.assertIn("loop", data["nodeTypes"])
        status, data = self.request(f"/api/workflows/{ident}", {"revision": 1, "graph": FREE, "name": "Graph 2"})
        self.assertEqual((status, data["workflow"]["draft"]), (200, False))
        status, data = self.request(f"/api/workflows/{ident}/publish", {"revision": 2})
        self.assertEqual((status, data["workflow"]["published"]), (200, {"revision": 2}))
        status, data = self.get(f"/api/workflows/{ident}/versions")
        self.assertEqual([v["revision"] for v in data["versions"]], [2, 1])
        status, data = self.get(f"/api/workflows/{ident}/versions/1")
        self.assertEqual(data["workflow"]["revision"], 1)
        status, data = self.get(f"/api/workflows/{ident}/versions/1/diff/2")
        self.assertEqual(data["diff"]["nodes"]["changed"], [])
        status, data = self.request(f"/api/workflows/{ident}/rollback", {"revision": 1, "current": 2})
        self.assertEqual((status, data["workflow"]["revision"]), (200, 3))
        status, data = self.get(f"/api/workflows/{ident}/versions/9")
        self.assertEqual((status, data["code"]), (404, "version_not_found"))
