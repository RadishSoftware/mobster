"""MCP's phone_task and phone_task_status (provider ``threads``): through a running Mobster over HTTP with origin mcp,
never answering an approval, with --allow-device honoured, and a plain sentence when no Mobster runs. Offline."""

import tempfile
import unittest

from mobile_agent.mcp_server import registry
from mobile_agent.mcp_server.tools import ACTS, READ_ONLY, ToolSet
from mobile_agent.tests.test_conversations_chat import TOKEN, Served
from mobile_agent.tests.test_conversations_support import clarify
from mobile_agent.tests.test_engines import COMPOSE, Script, item, sent
from mobile_agent.threads.client import Client, NoService
from mobile_agent.threads.mcp import NO_SERVICE, ThreadsProvider


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
        self.assertEqual(listed["phone_task"]["annotations"], ACTS)
        self.assertEqual(listed["phone_task_status"]["annotations"], READ_ONLY)
        self.assertIn("phone_task hands a whole task to Mobster's agent", tools.instructions())
        for name in ("phone_task", "phone_task_status"):  # the core tools' rules (test_mcp_tools.SchemaTests)
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
        self.assertIn("Lunch with Sam at noon.", follow.prompts[0])  # the follow-up saw the first answer
        status = tools.call("phone_task_status", {"thread_id": state["thread_id"]}, None).structured
        self.assertEqual((status["run_id"], status["status"]), (again["run_id"], "completed"))

    def test_an_approval_waits_for_the_person(self):
        runtime = self.serve(app_session="s" * 43)
        tools = self.tools()
        script = Script([[("TYPE", "Message", "I'm running late"), ("TAP", "Send", None)], [("DONE", None, None)]],
                        [item()], answer="Sent.")
        self.scripted(script, screens=[COMPOSE, COMPOSE, sent("I'm running late")])
        result = tools.call("phone_task", {"message": "Text Sam I'm running late", "wait_seconds": 20}, None)
        self.assertEqual(result.structured["status"], "waiting_for_approval")
        self.assertIn("approve this in the Mobster app", result.text)
        run = runtime.runs[result.structured["run_id"]]
        self.assertIsNotNone(run.approval)
        # A message to that conversation can't answer it either.
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
        self.assertIsNotNone(run.approval)  # still the person's question
        run.answer_approval(run.approval["id"], False)
        helper.join(5)

    def test_allow_device_limits_the_phones_it_names(self):
        self.serve()
        tools = self.tools(allow_devices=["Sam's iPhone"])
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


if __name__ == "__main__":
    unittest.main()
