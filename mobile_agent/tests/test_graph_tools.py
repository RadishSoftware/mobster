import importlib.util
import io
import json
import unittest
from contextlib import nullcontext
from types import SimpleNamespace

from mobile_agent.graph import nodes as graph_nodes
from mobile_agent.mcp_server.tools import ToolError
from mobile_agent.tests.test_graph_run import GraphBase, edge, node, task
from mobile_agent.threads import chat_once
from mobile_agent.threads.client import NoService, ServiceError
from mobile_agent.threads.mcp import WorkflowsProvider, describe_workflow

GRAPH = {"nodes": [{"id": "start", "type": "trigger.manual"}, {"id": "read", "type": "app.read"},
                   {"id": "check", "type": "branch"}, {"id": "say", "type": "phone.task"}]}
DMS = {"id": "aaaaaaaaaaaa", "name": "Answer DMs", "goal": "Answer {who}", "graph": GRAPH, "published": {"revision": 2},
       "asTool": True, "blanks": [{"name": "who", "default": None, "type": "string"},
                                  {"name": "count", "default": "3", "type": "number"}]}
OTHER = {"id": "bbbbbbbbbbbb", "name": "Quiet one", "goal": "Read the news", "graph": GRAPH,
         "published": {"revision": 1},
         "blanks": []}
ENDED = {"id": "cccccccccccc", "graph": 2, "status": "completed", "reason": None, "result": {"sent": 2}, "nodes": {
    "start": {"instance": "start", "type": "trigger.manual", "state": "done", "seq": 1},
    "read": {"instance": "read", "type": "app.read", "state": "done", "seq": 2, "output": {"title": "2 DMs"}},
    "say": {"instance": "say", "type": "phone.task", "state": "skipped", "reason": "Not taken", "seq": 3}}}


class Client:
    def __init__(self, listed=(DMS, OTHER), fail=False):
        self.listed, self.fail, self.ran = list(listed), fail, []

    def workflows(self):
        if self.fail:
            raise ConnectionError("no Mobster")
        return self.listed

    def workflow_tools(self):
        from mobile_agent.workflows.service import tools
        return tools([(workflow, workflow["graph"]) for workflow in self.workflows()])

    def find_workflow(self, wanted):
        return next(w for w in self.listed if w["id"] == wanted or w["name"].casefold() == str(wanted).casefold())

    def run_workflow(self, workflow_id, **fields):
        self.ran.append((workflow_id, fields))
        return "dddddddddddd", {"run": None, "batch": {"id": ENDED["id"], "graph": 2, "status": "running"}}

    def wait_batch(self, workflow_id, batch_id, seconds, on_node=None):
        for item in ENDED["nodes"].values():
            if on_node:
                on_node(item)
        return ENDED


class McpTests(unittest.TestCase):
    def provider(self, client):
        return WorkflowsProvider(connect=lambda: client)

    def test_a_workflow_made_a_tool_is_its_own_typed_tool(self):
        client = Client()
        provider = self.provider(client)
        tools = {tool["name"]: tool for tool in provider.definitions(None)}
        self.assertEqual(set(tools), {"list_workflows", "run_workflow", "graph_status", "workflow_answer_dms"})
        schema = tools["workflow_answer_dms"]["inputSchema"]
        self.assertEqual((schema["properties"]["who"]["type"], schema["properties"]["count"]["type"],
                          schema["required"]), ("string", "number", ["who"]))
        self.assertIn("workflow_answer_dms", provider.annotations())
        self.assertTrue(provider.annotations()["graph_status"]["readOnlyHint"])
        result = provider.call("workflow_answer_dms", {"who": "Sam", "count": 2, "wait_seconds": 0}, None, None)
        self.assertEqual(client.ran[0][0], DMS["id"])
        self.assertEqual(client.ran[0][1]["values"], {"who": "Sam", "count": 2})
        self.assertEqual((result.structured["status"], result.structured["result"]), ("completed", {"sent": 2}))
        self.assertEqual([s["instance"] for s in result.structured["steps"]], ["read", "say"])
        self.assertIn("“Answer DMs” completed: 1 of 2 steps done.", result.text)

    def test_with_no_mobster_the_tool_list_is_the_fixed_one(self):
        provider = self.provider(Client(fail=True))
        self.assertEqual([tool["name"] for tool in provider.definitions(None)],
                         ["list_workflows", "run_workflow", "graph_status"])

    def test_list_workflows_says_each_graphs_shape(self):
        self.assertIn("Graph: 3 steps, 1 branch.", describe_workflow(DMS))
        result = self.provider(Client()).call("list_workflows", {}, None, None)
        listed = {w["id"]: w for w in result.structured["workflows"]}
        self.assertEqual((listed[DMS["id"]]["nodes"], listed[DMS["id"]]["branches"], listed[DMS["id"]]["published"]),
                         (3, 1, 2))

    def test_restricted_workflows_refuse_static_dynamic_and_thread_starts_before_post(self):
        client = Client()
        provider = self.provider(client)
        restricted = SimpleNamespace(allow_devices={"allowed phone"})
        starts = (("run_workflow", {"workflow": DMS["id"]}),
                  ("run_workflow", {"workflow": DMS["id"], "thread_id": "d" * 12}),
                  ("workflow_answer_dms", {"who": "Sam"}))
        for name, arguments in starts:
            with self.subTest(name=name, arguments=arguments):
                with self.assertRaisesRegex(ToolError, "--allow-device"):
                    provider.call(name, arguments, None, restricted)
        self.assertEqual(client.ran, [])
        self.assertEqual(provider.call("graph_status", {"workflow_id": DMS["id"], "batch_id": ENDED["id"]},
                                       None, restricted).structured["status"], "completed")

    def test_graph_status_reads_an_exact_batch_without_another_start(self):
        client = Client()
        provider = self.provider(client)
        result = provider.call("graph_status", {"workflow_id": DMS["id"], "batch_id": ENDED["id"]}, None, None)
        self.assertEqual((result.structured["workflow_id"], result.structured["batch_id"],
                          result.structured["status_uri"]),
                         (DMS["id"], ENDED["id"], f"/api/workflows/{DMS['id']}/runs"))
        self.assertEqual(client.ran, [])
        client.wait_batch = lambda *args: None
        with self.assertRaisesRegex(ToolError, "cannot find that graph batch"):
            provider.call("graph_status", {"workflow_id": DMS["id"], "batch_id": "f" * 12}, None, None)
        self.assertEqual(client.ran, [])

    def test_accepted_graph_keeps_its_batch_when_the_status_read_fails(self):
        for error in (NoService("status read lost"), ServiceError(503, {"error": "status read failed"}),
                      KeyError("batches")):
            with self.subTest(error=type(error).__name__):
                client = Client()

                def lost(*args):
                    raise error

                client.wait_batch = lost
                with (self.assertLogs("mobster.threads", "WARNING") if isinstance(error, KeyError) else nullcontext()):
                    result = self.provider(client).call("run_workflow", {"workflow": DMS["id"]}, None, None)
                self.assertEqual((result.structured["workflow_id"], result.structured["batch_id"],
                                  result.structured["status"]), (DMS["id"], ENDED["id"], "status_unavailable"))
                self.assertIn("graph_status", result.text)
                self.assertEqual(len(client.ran), 1)

    def test_accepted_legacy_workflow_keeps_its_run_when_the_status_read_fails(self):
        client = Client()
        client.ran = []

        def accepted(workflow_id, **fields):
            client.ran.append(workflow_id)
            return "d" * 12, {"run": {"id": "e" * 12}}

        client.run_workflow = accepted

        def lost(*args):
            raise NoService("status read lost")

        client.wait_run = lost
        result = self.provider(client).call("run_workflow", {"workflow": DMS["id"]}, None, None)
        self.assertEqual((result.structured["thread_id"], result.structured["run_id"],
                          result.structured["status_uri"]),
                         ("d" * 12, "e" * 12, "/api/runs/" + "e" * 12))
        self.assertIn("do not run it again", result.text)
        self.assertEqual(client.ran, [DMS["id"]])

    def test_a_workflow_start_mobster_did_not_confirm_is_unknown_and_a_refusal_still_says_why(self):
        journal = {"error": "Mobster couldn't save this to its history on this Mac. Check that the disk has free "
                            "space, then try again.", "code": "persistence_unavailable"}
        for error in (NoService("reply lost"), ServiceError(503, journal), ServiceError(408, {"error": "Too long."}),
                      ServiceError(500, {"error": "Internal error"})):
            with self.subTest(error=error):
                client, posts = Client(), []

                def lost(*args, **kwargs):
                    posts.append(args)
                    raise error

                client.run_workflow = lost
                with self.assertRaisesRegex(ToolError, "whether it started is unknown") as raised:
                    self.provider(client).call("run_workflow", {"workflow": DMS["id"]}, None, None)
                self.assertNotIn("try again", str(raised.exception).lower())
                self.assertEqual(len(posts), 1)
        for answer in ([], {"batch": {"graph": 2, "status": "running"}}, {"run": "e" * 12},
                       {"batch": {"graph": 2, "id": 12}}):
            with self.subTest(answer=answer):
                client = Client()
                client.run_workflow = lambda *args, answer=answer, **kwargs: ("d" * 12, answer)
                with self.assertRaisesRegex(ToolError, "whether it started is unknown"), \
                        self.assertLogs("mobster.threads", "WARNING"):
                    self.provider(client).call("run_workflow", {"workflow": DMS["id"]}, None, None)
        client = Client()

        def refused(*args, **kwargs):
            raise ServiceError(400, {"error": "Fill in who."})

        client.run_workflow = refused
        with self.assertRaisesRegex(ToolError, "^Fill in who.$"):
            self.provider(client).call("run_workflow", {"workflow": DMS["id"]}, None, None)

        def shutting_down(*args, **kwargs):
            raise ServiceError(503, {"error": "The service is shutting down", "code": "shutting_down",
                                     "started": False})

        client.run_workflow = shutting_down
        with self.assertRaisesRegex(ToolError, "^The service is shutting down$"):
            self.provider(client).call("run_workflow", {"workflow": DMS["id"]}, None, None)

    def test_a_workflow_start_with_no_time_left_refuses_before_it_posts(self):
        import time
        client = Client()
        toolset = SimpleNamespace(allow_devices=None, call_budget=1.0)
        for name, arguments in (("run_workflow", {"workflow": DMS["id"]}), ("workflow_answer_dms", {"who": "Sam"})):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ToolError, f"{name} ran out of time before it sent the workflow run"):
                    self.provider(client).call(name, arguments, SimpleNamespace(started=time.monotonic()), toolset)
        self.assertEqual(client.ran, [])
        toolset.call_budget = 44.0
        result = self.provider(client).call("run_workflow", {"workflow": DMS["id"], "wait_seconds": 0},
                                            SimpleNamespace(started=time.monotonic()), toolset)
        self.assertEqual((result.structured["status"], len(client.ran)), ("completed", 1))

class CliTests(unittest.TestCase):
    def test_chat_follows_a_graph_run_step_by_step(self):
        out = io.StringIO()
        code = chat_once.follow_graph(Client(), DMS, {"id": ENDED["id"]}, "dddddddddddd", as_json=False, wait=5,
                                      out=out)
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().splitlines(), ["read: done", "say: skipped (Not taken)",
                                                       "“Answer DMs” completed."])
        out = io.StringIO()
        chat_once.follow_graph(Client(), DMS, {"id": ENDED["id"]}, None, as_json=True, wait=5, out=out)
        printed = json.loads(out.getvalue())
        self.assertEqual((printed["status"], printed["nodes"]["say"]["state"]), ("completed", "skipped"))

    def test_a_run_that_didnt_complete_exits_non_zero(self):
        client = Client()
        client.wait_batch = lambda *a, **k: {**ENDED, "status": "approval_denied"}
        self.assertEqual(chat_once.follow_graph(client, DMS, {"id": "x"}, None, as_json=True, wait=1,
                                                out=io.StringIO()), 1)


class ShowTests(unittest.TestCase):

    PUBLISHED = {"version": 2, "nodes": [
        {"id": "start", "type": "trigger.manual"}, {"id": "each", "type": "loop", "config": {"over": "times"}},
        {"id": "say", "type": "phone.task", "parent": "each", "config": {"text": "Reply to   {who}"}},
        {"id": "ok", "type": "approval", "title": "Check first", "config": {"message": "Send it?"}},
        {"id": "post", "type": "phone.task", "config": {"text": "Post it"}}],
        "edges": [{"id": "e1", "source": "start", "target": "each"}, {"id": "e2", "source": "each", "target": "ok"},
                  {"id": "e3", "source": "ok", "sourcePort": "approved", "target": "post"}]}

    def show(self, *flags):
        from unittest import mock

        from mobile_agent.threads import workflow_cli
        calls = []

        class Fake:
            def __init__(self, url=None):
                pass

            def find_workflow(self, wanted):
                if wanted != "Answer DMs":
                    raise LookupError("No workflow is called x.")
                return {**DMS, "graph": {"version": 2, "nodes": [{"id": "start", "type": "trigger.manual"}],
                                         "edges": []}}

            def call(self, method, path):
                calls.append(path)
                return {"workflow": {"graph": ShowTests.PUBLISHED}}
        parser = __import__("argparse").ArgumentParser()
        workflow_cli.add_arguments(parser, {})
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(workflow_cli, "Client", Fake), mock.patch("sys.stdout", out), \
                mock.patch("sys.stderr", err):
            code = workflow_cli.run(parser.parse_args(["show", *flags]))
        return code, out.getvalue(), err.getvalue(), calls

    def test_it_prints_the_published_steps_in_run_order_with_what_each_waits_for(self):
        code, out, _err, calls = self.show("Answer DMs")
        self.assertEqual((code, calls), (0, [f"/api/workflows/{DMS['id']}/versions/2"]))
        self.assertEqual(out.splitlines(), [
            "“Answer DMs” (aaaaaaaaaaaa): published revision 2, 5 steps",
            "  start  Manual (trigger.manual)",
            "  each   Loop (loop), after start",
            "    say  Phone task (phone.task): “Reply to {who}”",
            "  ok     Check first (approval), after each: “Send it?”",
            "  post   Phone task (phone.task), after ok (approved): “Post it”"])
        code, out, _err, calls = self.show("Answer DMs", "--draft", "--json")
        printed = json.loads(out)
        self.assertEqual((code, calls, printed["draft"], [s["id"] for s in printed["steps"]]), (0, [], True, ["start"]))
        code, _out, err, _calls = self.show("x")
        self.assertEqual((code, err.strip()), (1, "mobster workflow: No workflow is called x."))


class RecordTests(GraphBase):
    def test_as_tool_is_a_setting_and_typed_values_run(self):
        workflow = self.save_graph([task("a", "Text the people in the input", input="{= $inputs.people }")],
                                   [edge("e1", "start", "a")],
                                   inputs=[{"name": "people", "type": "list", "required": True}])
        saved = self.workflows.update(workflow["id"], {"revision": 1, "asTool": True})
        self.assertEqual((saved["asTool"], saved["published"]), (True, {"revision": 1}))
        with self.assertRaises(ValueError):
            self.workflows.update(workflow["id"], {"revision": 2, "asTool": "yes"})
        self.go(saved, values={"people": ["Sam", "Kate"]})
        self.assertEqual(json.loads(self.phone.calls[0].extras["workflowInput"]), ["Sam", "Kate"])


class RestTests(GraphBase):

    def request(self, method, path, body=None, **headers):
        from email.message import Message

        from mobile_agent.server import make_handler
        handler = object.__new__(make_handler(self.phone))
        raw = json.dumps(body).encode() if body is not None else b""
        handler.headers = Message()
        for name, value in (("Host", "127.0.0.1:8765"), ("Content-Type", "application/json"),
                            ("Content-Length", str(len(raw))), *headers.items()):
            handler.headers[name.replace("_", "-")] = value
        handler.path, handler.rfile, handler.wfile = path, io.BytesIO(raw), io.BytesIO()
        statuses = []
        handler.send_response = statuses.append
        handler.send_header = lambda *_: None
        handler.end_headers = lambda: None
        getattr(handler, f"do_{method}")()
        return statuses[-1], json.loads(handler.wfile.getvalue())

    def tool(self, name="Text people", **extra):
        workflow = self.workflows.create({"name": name, "graph": self.graph(
            [task("a", "Text the people in the input", input="{= $inputs.people }"),
             node("out", "output", {"value": "{= {'texted': $inputs.people} }"})],
            [edge("e1", "start", "a"), edge("e2", "a", "out")],
            inputs=[{"name": "people", "type": "list", "required": True}], **extra)})
        return self.workflows.update(workflow["id"], {"revision": 1, "asTool": True})

    def test_a_workflow_made_a_tool_runs_by_its_slug_with_its_tools_schema(self):
        saved = self.tool()
        status, listed = self.request("GET", "/v1/workflows/tools")
        tool = listed["tools"][0]
        self.assertEqual((status, tool["name"], tool["path"], tool["workflowId"]),
                         (200, "workflow_text_people", "/v1/workflows/text_people", saved["id"]))
        self.assertEqual(self.request("GET", "/v1/workflows/text_people")[1]["tool"], tool)
        status, ran = self.request("POST", "/v1/workflows/text_people", {"people": ["Sam", "Kate"]},
                                   Idempotency_Key="rest-1")
        self.assertEqual((status, ran["status"], ran["result"]), (200, "completed", {"texted": ["Sam", "Kate"]}))
        self.assertEqual([step["instance"] for step in ran["steps"]], ["a", "out"])
        self.assertEqual(json.loads(self.phone.calls[0].extras["workflowInput"]), ["Sam", "Kate"])
        self.request("POST", "/v1/workflows/text_people", {"people": ["Sam", "Kate"]}, Idempotency_Key="rest-1")
        self.assertEqual(len(self.phone.calls), 1)

    def test_the_route_refuses_what_its_schema_doesnt_take_and_a_name_that_isnt_a_tool(self):
        self.tool()
        for body, headers in (({}, {"Idempotency_Key": "k"}), ({"people": [], "extra": 1}, {"Idempotency_Key": "k"}),
                              ({"people": [], "wait_seconds": 999}, {"Idempotency_Key": "k"}),
                              ({"people": []}, {})):
            self.assertEqual(self.request("POST", "/v1/workflows/text_people", body, **headers)[0], 400)
        self.assertEqual(self.request("POST", "/v1/workflows/someone_else", {}, Idempotency_Key="k")[0], 404)
        self.assertEqual(self.phone.calls, [])

    def test_slugs_stay_put_and_never_shadow_a_route(self):
        first, second, reserved = self.tool("Text people"), self.tool("Text people"), self.tool("Draft")
        slugs = {tool["workflowId"]: tool["slug"] for tool in self.workflows.tools()}
        self.assertEqual((slugs[first["id"]], slugs[second["id"]], slugs[reserved["id"]]),
                         ("text_people", f"text_people_{second['id'][:4]}", f"draft_{reserved['id'][:4]}"))

    def test_a_shortcut_enters_at_its_trigger_with_its_input_as_data(self):
        graph_nodes.register()
        workflow = self.workflows.create({"name": "From a Shortcut", "graph": {
            "version": 2, "nodes": [node("start", "trigger.manual"), node("short", "trigger.shortcut"),
                                    task("a", "Save what the Shortcut sent", input="{= $input.input }")],
            "edges": [edge("e1", "start", "a"), edge("e2", "short", "a")]}})
        self.go(workflow, trigger="shortcut", input="Buy milk")
        record = self.workflows.batch_runs(workflow["id"])[0]
        self.assertEqual((record["trigger"], record["nodes"]["start"]["state"]), ("trigger.shortcut", "skipped"))
        self.assertEqual(self.phone.calls[0].extras["workflowInput"], "Buy milk")
        self.assertEqual(self.phone.calls[0].goal, "Save what the Shortcut sent")
        status, started = self.request("POST", f"/api/workflows/{workflow['id']}/run",
                                       {"trigger": "shortcut", "input": "Buy eggs"}, Idempotency_Key="s1",
                                       X_Mobster_Origin="app")
        self.assertEqual(status, 201)
        self.wait(workflow)
        self.assertEqual(self.phone.calls[-1].extras["workflowInput"], "Buy eggs")
        status, _ = self.request("POST", f"/api/workflows/{workflow['id']}/run",
                                 {"trigger": "shortcut", "input": "x"}, Idempotency_Key="s2", X_Mobster_Origin="mcp")
        self.assertEqual(status, 403)
        with self.assertRaises(ValueError):
            self.workflows.start(workflow["id"], "s3", input="no trigger")
        plain = self.workflows.create({"name": "One task", "steps": [
            {"id": "a", "kind": "task", "text": "Add it to my list", "appId": None}]})
        self.workflows.start(plain["id"], "s4", trigger="shortcut", input="Buy bread")
        self.assertEqual((self.phone.calls[-1].goal, self.phone.calls[-1].extras["workflowInput"]),
                         ("Add it to my list", "Buy bread"))


@unittest.skipUnless(importlib.util.find_spec("mobile_agent.remote"), "requires the app remote extension")
class RemoteTests(unittest.TestCase):
    def tools(self, client):
        from mobile_agent.remote.tools import RemoteTools
        return RemoteTools(grant={"id": "g1"}, client_factory=lambda: client, device_names=lambda: {},
                           webhooks=lambda: {DMS["id"]})

    def test_remote_follows_a_graph_run_to_its_end(self):
        result = self.tools(Client())._run_workflow({"id": "g1"}, {"workflow": "Answer DMs", "values": {"who": "Sam"},
                                                                   "wait_seconds": 1}, None)
        self.assertEqual((result.structured["status"], result.structured["result"]), ("completed", {"sent": 2}))
        self.assertIn("1 of 2 steps done", result.text)

    def test_remote_keeps_an_accepted_graphs_batch_when_its_status_read_fails(self):
        client = Client()

        def lost(*args):
            raise NoService("status read lost")

        client.wait_batch = lost
        result = self.tools(client)._run_workflow({"id": "g1"}, {"workflow": "Answer DMs", "values": {"who": "Sam"}},
                                                  None)
        self.assertFalse(result.is_error, result.text)
        self.assertEqual((result.structured["status"], result.structured["workflow_id"], result.structured["batch_id"]),
                         ("status_unavailable", DMS["id"], ENDED["id"]))
        self.assertIn("ask the user to check it in Mobster on their Mac", result.text)
        self.assertNotIn("try again", result.text.lower())
        self.assertEqual(len(client.ran), 1)

    def test_remote_calls_a_graph_answer_with_no_batch_id_unknown(self):
        client = Client()
        client.run_workflow = lambda *args, **kwargs: ("d" * 12, {"run": None, "batch": {"graph": 2}})
        with self.assertRaisesRegex(ToolError, "whether it started is unknown. Ask the user to check Mobster on "
                                               "their Mac"), self.assertLogs("mobster.threads", "WARNING"):
            self.tools(client)._run_workflow({"id": "g1"}, {"workflow": "Answer DMs", "values": {"who": "Sam"}},
                                             None)


class FrameTests(GraphBase):
    def test_a_steps_screens_list_for_scrubbing(self):
        workflow = self.save_graph([task("a", "Read the time")], [edge("e1", "start", "a")])
        _, record = self.go(workflow)
        run = self.phone.runs[record["nodes"]["a"]["runId"]]
        run.events = [{"event": "frontier_screen", "frameId": "f1", "timestamp": 1.0}, {"event": "step"},
                      {"event": "frontier_action", "frameId": "f2", "timestamp": 2.0}]
        listed = self.workflows.node_frames(workflow["id"], record["id"], "a")
        self.assertEqual(listed, {"runId": run.id, "frames": [{"at": 1.0, "frameId": "f1", "event": "frontier_screen"},
                                                              {"at": 2.0, "frameId": "f2",
                                                               "event": "frontier_action"}]})
