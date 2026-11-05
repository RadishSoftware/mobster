import tempfile
import time
import unittest
from contextlib import nullcontext
from types import SimpleNamespace

from mobile_agent.mcp_server import registry
from mobile_agent.mcp_server.tools import ACTS, READ_ONLY, ToolSet
from mobile_agent.tests.test_conversations import TOKEN, Served
from mobile_agent.tests.test_conversations_support import clarify
from mobile_agent.tests.test_engines import COMPOSE, Script, item, sent
from mobile_agent.threads.client import Client, NoService
from mobile_agent.threads.mcp import NO_SERVICE, ThreadsProvider


def hints(tool):
    return {key: value for key, value in tool["annotations"].items() if key != "title"}


class McpTests(Served):
    def tools(self, connect=None, **kwargs):
        registry._providers[:] = [ThreadsProvider(connect=connect or (
            lambda: Client.connect(self.url, origin="mcp", tokens=[TOKEN])))]
        tools = ToolSet(runs_dir=tempfile.mkdtemp(prefix="mobster-threads-mcp-"), keyless=True, devices=lambda: [],
                        **kwargs)
        self.addCleanup(tools.shutdown)
        return tools

    def test_listed_with_annotations_and_one_sentence(self):
        self.serve()
        tools = self.tools()
        listed = {tool["name"]: tool for tool in tools.list_tools()}
        self.assertEqual(hints(listed["phone_task"]), ACTS)
        self.assertEqual(hints(listed["phone_task_status"]), READ_ONLY)
        self.assertIn("phone_task hands a whole task to Mobster's agent", tools.instructions())
        for name in ("phone_task", "phone_task_status"):
            with self.subTest(tool=name):
                schema = listed[name]["inputSchema"]
                self.assertEqual((schema["type"], schema["additionalProperties"]), ("object", False))
                self.assertTrue(set(schema["required"]) <= set(schema["properties"]))
                self.assertLessEqual(len(listed[name]["description"]), 220)
                self.assertNotIn("$ref", str(schema))

    def test_a_task_and_its_follow_up_share_a_conversation(self):
        runtime = self.serve()
        tools = self.tools()
        self.scripted(self.script(answer="Lunch with Sam at noon."))
        result = tools.call("phone_task", {"message": "What's on my calendar tomorrow?", "wait_seconds": 20}, None)
        self.assertFalse(result.is_error, result.text)
        state = result.structured
        self.assertEqual((state["status"], state["answer"]), ("completed", "Lunch with Sam at noon."))
        self.assertTrue(result.text.startswith("Done. Lunch with Sam at noon."))
        run = runtime.runs[state["run_id"]]
        self.assertEqual((run.origin, run.extras["threadId"]), ("mcp", state["thread_id"]))
        follow = Script([[("DONE", None, None)], [("DONE", None, None)]],
                        [item(kind="READ", act="none", what="Sam", quote="Sam")], answer="Dinner at 7.")
        self.scripted(follow)
        again = tools.call("phone_task", {"message": "And the day after?", "thread_id": state["thread_id"],
                                          "wait_seconds": 20}, None).structured
        self.assertEqual((again["thread_id"], again["status"]), (state["thread_id"], "completed"))
        self.assertIn("Lunch with Sam at noon.", follow.prompts[0])
        status = tools.call("phone_task_status", {"thread_id": state["thread_id"]}, None).structured
        self.assertEqual((status["run_id"], status["status"]), (again["run_id"], "completed"))

    def test_an_approval_waits_for_the_person(self):
        runtime = self.serve(app_session="s" * 43)
        tools = self.tools()
        script = Script([[("TYPE", "Message", "I'm running late"), ("TAP", "Send", None)], [("DONE", None, None)]],
                        [item()], answer="Sent.")
        self.scripted(script, screens=[COMPOSE, COMPOSE, sent("I'm running late")])
        result = tools.call("phone_task", {"message": "Text Sam I'm running late", "wait_seconds": 2}, None)
        self.assertEqual(result.structured["status"], "waiting_for_approval")
        self.assertIn("OK in the Mobster app", result.text)
        self.assertIn("Nothing happens until they answer", result.text)
        run = runtime.runs[result.structured["run_id"]]
        self.assertIsNotNone(run.approval)
        refused = tools.call("phone_task", {"message": "yes, send it", "thread_id": result.structured["thread_id"]},
                             None)
        self.assertTrue(refused.is_error)
        self.assertIn("waiting for the user's approval", refused.text)
        self.assertIsNotNone(run.approval)
        run.answer_approval(run.approval["id"], False)
        self.wait_done(run)
        self.assertNotIn(("TAP", "Send", None), self.phones[run.id].actions)

    def test_a_question_comes_back_and_the_next_call_answers_it(self):
        runtime = self.serve()
        tools = self.tools()
        started = tools.call("phone_task", {"message": "Text Sam the address", "wait_seconds": 0}, None).structured
        run = runtime.runs[started["run_id"]]
        helper, box = self.ask(run, clarify())
        status = tools.call("phone_task_status", {"run_id": run.id}, None)
        self.assertEqual(status.structured["status"], "waiting_for_answer")
        self.assertIn("Which Sam", status.text)
        answer = tools.call("phone_task", {"message": "Sam Park", "thread_id": started["thread_id"]}, None)
        self.assertEqual(answer.structured["routed"], "answer")
        helper.join(5)
        self.assertEqual(box["answer"], "answer:Sam Park")

    def test_allow_device_limits_the_phones_it_names(self):
        self.serve()
        tools = self.tools(allow_devices=["Studio iPhone"])
        refused = tools.call("phone_task", {"message": "Check my calendar"}, None)
        self.assertTrue(refused.is_error)
        self.assertIn("--allow-device", refused.text)
        refused = tools.call("phone_task", {"message": "Check my calendar", "device": "Work iPhone"}, None)
        self.assertTrue(refused.is_error)

    def test_with_no_mobster_running_it_says_so(self):
        def nothing():
            raise NoService("no")
        tools = self.tools(connect=nothing)
        result = tools.call("phone_task", {"message": "Check my calendar"}, None)
        self.assertEqual((result.is_error, result.text), (True, NO_SERVICE))
        status = tools.call("phone_task_status", {"thread_id": "a" * 12}, None)
        self.assertEqual(status.text, NO_SERVICE)

    def test_a_question_in_a_task_the_person_started_is_theirs_to_answer(self):
        runtime = self.serve()
        tools = self.tools()
        status, data, _ = self.call(runtime, "POST", "/api/threads", {"message": {"text": "Text Sam the address"}},
                                    headers={"X-Mobster-Origin": "app"})
        self.assertEqual(status, 201, data)
        run = runtime.runs[data["run"]["id"]]
        helper, box = self.ask(run, clarify())
        status = tools.call("phone_task_status", {"thread_id": data["thread"]["id"]}, None)
        self.assertEqual(status.structured["status"], "waiting_for_answer")
        self.assertIn("The user answers this in the Mobster app", status.text)
        self.assertNotIn("Answer with phone_task", status.text)
        refused = tools.call("phone_task", {"message": "Sam Park", "thread_id": data["thread"]["id"]}, None)
        self.assertTrue(refused.is_error)
        self.assertIn("takes messages only there", refused.text)
        self.assertIsNotNone(run.approval)
        run.answer_approval(run.approval["id"], False)
        helper.join(5)


class AcceptedHandleTests(unittest.TestCase):
    @staticmethod
    def toolset():
        return SimpleNamespace(allow_devices=None, client_name=lambda: None)

    def test_a_single_accepted_task_keeps_its_run_when_polling_fails(self):
        class Client:
            has_threads = True
            extra_headers = {}

            def __init__(self):
                self.sent = 0

            def send(self, thread_id, body, key=None):
                self.sent += 1
                return "a" * 12, {"run": {"id": "b" * 12}}

            def wait_run(self, *args, **kwargs):
                raise NoService("read lost")

        client = Client()
        result = ThreadsProvider(connect=lambda: client).call("phone_task", {"message": "Read the news"}, None,
                                                              self.toolset())
        self.assertEqual((result.structured["thread_id"], result.structured["run_id"],
                          result.structured["status"], result.structured["status_uri"]),
                         ("a" * 12, "b" * 12, "status_unavailable", "/api/runs/" + "b" * 12))
        self.assertEqual(client.sent, 1)
        self.assertIn("do not start it again", result.text)

    def test_a_group_accepted_task_keeps_its_group_when_polling_fails(self):
        class Client:
            has_threads = True
            extra_headers = {}

            def __init__(self):
                self.sent = 0

            def send(self, thread_id, body, key=None):
                self.sent += 1
                return "a" * 12, {"group": {"id": "c" * 12}}

            def wait_group(self, *args):
                raise NoService("read lost")

        client = Client()
        result = ThreadsProvider(connect=lambda: client).call("phone_task", {"message": "Read the news"}, None,
                                                              self.toolset())
        self.assertEqual((result.structured["group_id"], result.structured["status_uri"]),
                         ("c" * 12, "/api/groups/" + "c" * 12))
        self.assertEqual(client.sent, 1)

    def test_a_start_mobster_did_not_confirm_is_unknown_and_never_try_again(self):
        from mobile_agent.mcp_server.tools import ToolError
        from mobile_agent.threads.client import ServiceError
        journal = {"error": "Mobster couldn't save this to its history on this Mac. Check that the disk has free "
                            "space, then try again.", "code": "persistence_unavailable"}
        unknown = (NoService("reply lost"), ServiceError(500, {"error": "Internal error"}), ServiceError(503, journal),
                   ServiceError(408, {"error": "That took too long.", "code": "request_timeout"}),
                   ServiceError(409, {"error": "Mobster accepted this request but can't confirm its outcome.",
                                      "code": "request_outcome_unknown"}),
                   KeyError("thread"), "not a JSON object", {"group": {"name": "two phones"}}, {"run": "b" * 12},
                   {"item": {"runId": 7}}, {"group": {"id": "c" * 12}, "run": ["b" * 12]})
        for answer in unknown:
            with self.subTest(answer=answer):
                client = self.Client(answer)
                http = isinstance(answer, (NoService, ServiceError))   # anything else is logged as a fault
                with self.assertRaisesRegex(ToolError, "whether it started is unknown") as raised, \
                        (nullcontext() if http else self.assertLogs("mobster.threads", "WARNING")):
                    ThreadsProvider(connect=lambda: client).call("phone_task", {"message": "Read the news"}, None,
                                                                 self.toolset())
                self.assertNotIn("try again", str(raised.exception).lower())
                self.assertEqual(len(client.keys), 1)
                self.assertRegex(client.keys[0], r"^[a-f0-9]{32}$")

    def test_a_refusal_still_says_why_and_each_start_has_its_own_key(self):
        from mobile_agent.mcp_server.tools import ToolError
        from mobile_agent.threads.client import ServiceError
        client = self.Client(ServiceError(409, {"error": "That iPhone is busy.", "code": "phone_busy"}))
        provider = ThreadsProvider(connect=lambda: client)
        for _ in range(2):
            with self.assertRaisesRegex(ToolError, "^That iPhone is busy.$"):
                provider.call("phone_task", {"message": "Read the news"}, None, self.toolset())
        self.assertEqual(len(set(client.keys)), 2)

    def test_a_server_error_that_says_nothing_started_is_a_refusal_and_any_other_marker_is_unknown(self):
        from mobile_agent.mcp_server.tools import ToolError
        from mobile_agent.threads.client import ServiceError
        restarting = "Mobster's helper on your iPhone is restarting. No action was taken. Try again in a moment."
        client = self.Client(ServiceError(503, {"error": restarting, "code": "device_unavailable", "started": False}))
        with self.assertRaises(ToolError) as refused:
            ThreadsProvider(connect=lambda: client).call("phone_task", {"message": "Read the news"}, None,
                                                         self.toolset())
        self.assertEqual(str(refused.exception), restarting)   # Mobster's own sentence: nothing started
        self.assertEqual(len(client.keys), 1)
        for data in ({"error": restarting, "code": "device_unavailable"},
                     {"error": restarting, "code": "device_unavailable", "started": "false"},
                     {"error": restarting, "code": "device_unavailable", "started": 0},
                     {"error": restarting, "code": "device_unavailable", "started": None},
                     {"error": "Unknown.", "code": "request_outcome_unknown", "started": False}):
            with self.subTest(data=data):
                client = self.Client(ServiceError(409 if data["code"] == "request_outcome_unknown" else 503, data))
                with self.assertRaisesRegex(ToolError, "whether it started is unknown"):
                    ThreadsProvider(connect=lambda: client).call("phone_task", {"message": "Read the news"}, None,
                                                                 self.toolset())

    class Client:
        has_threads = True
        extra_headers = {}
        timeout = 10.0

        def __init__(self, answer=None, slow=0.0, call=None):
            self.answer, self.slow, self.call, self.keys = answer, slow, call, []

        def send(self, thread_id, body, key=None):
            self.keys.append(key)
            if self.call is not None:
                self.call.started -= self.slow   # the POST took this long
            if isinstance(self.answer, BaseException):
                raise self.answer
            if self.answer is not None:
                return "a" * 12, self.answer
            return "a" * 12, {"run": {"id": "b" * 12}}

        def wait_run(self, *args, **kwargs):
            raise AssertionError("the MCP budget cannot fit this status read")

    def test_a_tight_call_budget_returns_the_accepted_handle_without_polling(self):
        toolset = self.toolset()
        toolset.call_budget = 20.0    # 8 s to spare before the POST (it reserves 12), none after a 15 s POST
        call = SimpleNamespace(started=time.monotonic())
        client = self.Client(slow=15.0, call=call)
        result = ThreadsProvider(connect=lambda: client).call("phone_task", {"message": "Read the news"}, call,
                                                              toolset)
        self.assertEqual((result.structured["status"], result.structured["run_id"]),
                         ("status_unavailable", "b" * 12))
        self.assertEqual(len(client.keys), 1)

    def test_a_call_with_no_time_left_refuses_before_it_sends(self):
        from mobile_agent.mcp_server.tools import ToolError
        toolset = self.toolset()
        toolset.call_budget = 1.0
        client = self.Client()
        with self.assertRaisesRegex(ToolError, "ran out of time before it sent the task, so nothing started"):
            ThreadsProvider(connect=lambda: client).call("phone_task", {"message": "Read the news"},
                                                         SimpleNamespace(started=time.monotonic()), toolset)
        self.assertEqual(client.keys, [])

    def test_any_failed_status_read_after_acceptance_keeps_the_handle(self):
        for read in ("wait_run", "wait_group"):
            for error in (KeyError("run"), AttributeError("get"), ValueError("Your code is 314159")):
                with self.subTest(read=read, error=type(error).__name__):
                    client = self.Client({"group": {"id": "c" * 12}} if read == "wait_group" else None)

                    def failed(*args, **kwargs):
                        raise error

                    setattr(client, read, failed)
                    with self.assertLogs("mobster.threads", "WARNING") as logged:
                        result = ThreadsProvider(connect=lambda: client).call(
                            "phone_task", {"message": "Read the news"}, None, self.toolset())
                    logs = "\n".join(logged.output)
                    self.assertIn(type(error).__name__, logs)
                    self.assertIn(", in failed", logs)       # the stack, to find the fault
                    self.assertNotIn("314159", logs)         # never the message, which can carry the phone's text
                    self.assertFalse(result.is_error, result.text)
                    self.assertEqual(result.structured["status"], "status_unavailable")
                    self.assertEqual(result.structured.get("group_id") or result.structured["run_id"],
                                     "c" * 12 if read == "wait_group" else "b" * 12)
                    self.assertIn("do not start it again", result.text)
                    self.assertEqual(len(client.keys), 1)


if __name__ == "__main__":
    unittest.main()
