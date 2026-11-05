
import tempfile
import time
import unittest

from mobile_agent.mcp_server import registry
from mobile_agent.mcp_server.tools import ToolSet
from mobile_agent.tests.test_conversations import TOKEN, Served
from mobile_agent.tests.test_engines import COMPOSE, Script, item, sent
from mobile_agent.tests.test_mcp_devices import PHONE, SIM, FakeDeviceRun, records
from mobile_agent.tests.test_mcp_tools import ToolsHarness
from mobile_agent.threads import mcp as threads_mcp
from mobile_agent.threads.client import Client
from mobile_agent.threads.mcp import ThreadsProvider


class HolderInToolsTests(ToolsHarness):
    def make(self, **options):
        FakeDeviceRun.opened = []
        options.setdefault("devices", records)
        options.setdefault("device_run", FakeDeviceRun)
        return super().make(**options)

    def holder(self, minutes=4, **fields):
        return {"kind": "task", "label": "a task in Mobster", "runId": "abcdef012345",
                "since": int((time.time() - minutes * 60) * 1000), "stopping": False, **fields}

    def test_list_devices_says_who_holds_each_phone_and_who_waits(self):
        self.make()
        line = [{"id": "q1", "kind": "mcp", "label": "Claude Code", "position": 1, "since": 1}]
        self.tools._ownership_source = lambda: {PHONE: {"holder": self.holder(), "queue": line}}
        self.tools._devices_source = records
        result = self.call("list_devices")
        rows = {row["id"]: row for row in result.structured["devices"]}
        self.assertEqual(rows[PHONE]["holder"]["label"], "a task in Mobster")
        self.assertEqual(rows[PHONE]["queue"], line)
        self.assertEqual((rows[SIM]["holder"], rows[SIM]["queue"]), (None, []))
        self.assertIn("A task in Mobster is using this phone, 4 min; 1 waiting", result.text)

    def test_device_status_adds_the_holder_to_the_readings(self):
        from unittest import mock

        from mobile_agent import device_status
        self.make()
        self.tools._ownership_source = lambda: {PHONE: {"holder": self.holder(stopping=True), "queue": []}}
        with mock.patch.object(device_status, "read_phone", return_value={"battery": {"level": 50, "state": "unplugged",
                                                                                      "source": "lockdown"},
                                                                          "readAt": 1}), \
                mock.patch.object(device_status, "read_mac", return_value={}):
            result = self.call("device_status", device="Work iPhone")
        self.assertEqual(result.structured["holder"]["runId"], "abcdef012345")
        self.assertEqual(result.structured["queue"], [])
        self.assertIn("A task in Mobster is using this phone, 4 min, stopping after this step.", result.text)

    def test_a_phone_this_server_drives_is_held_by_its_client(self):
        self.make()
        self.tools.client = {"name": "claude-code", "version": "2"}
        self.call("screen", device="Work iPhone")
        rows = {row["id"]: row for row in self.call("list_devices").structured["devices"]}
        holder = rows[PHONE]["holder"]
        self.assertEqual((holder["kind"], holder["label"], holder["stopping"]), ("mcp", "Claude Code", False))
        self.assertEqual(holder["runId"], rows[PHONE]["run_id"])
        self.assertIsNone(rows[SIM]["holder"])

    def test_without_the_app_the_list_still_works(self):
        self.make()
        self.tools._ownership_source = None
        rows = self.call("list_devices").structured["devices"]
        self.assertTrue(all(row["holder"] is None for row in rows))


class QueuedStatusTests(unittest.TestCase):
    def test_a_queued_run_is_described_with_its_number_and_who_it_waits_for(self):
        run = {"finishedAt": None, "approval": None,
               "queue": {"position": 2, "behind": "Claude Code", "expiresAt": 5,
                         "message": "Waiting for Claude Code on alex, you are number 2."}}
        state = threads_mcp.describe(run)
        self.assertEqual((state["status"], state["position"], state["behind"]), ("queued", 2, "Claude Code"))
        text = threads_mcp.sentence(state, "0123456789ab")
        self.assertIn("Waiting for Claude Code on alex, you are number 2.", text)
        self.assertIn("Nothing has been done on the phone yet", text)
        self.assertIn("phone_task_status with thread_id 0123456789ab", text)

    def test_a_started_run_is_just_running(self):
        self.assertEqual(threads_mcp.describe({"finishedAt": None, "approval": None, "queue": None}),
                         {"status": "running"})

    def test_the_task_tool_takes_queue(self):
        schema = threads_mcp.schema_task()
        self.assertEqual(schema["properties"]["queue"]["type"], "boolean")


class QueuedTaskTests(Served):
    def tools(self):
        registry._providers[:] = [ThreadsProvider(connect=lambda: Client.connect(self.url, origin="mcp",
                                                                                 tokens=[TOKEN]))]
        tools = ToolSet(runs_dir=tempfile.mkdtemp(prefix="mobster-threads-mcp-"), keyless=True, devices=lambda: [])
        self.addCleanup(tools.shutdown)
        return tools

    def test_a_second_task_waits_for_the_first_and_says_so(self):
        runtime = self.serve(app_session="s" * 43)
        tools = self.tools()
        script = Script([[("TYPE", "Message", "I'm running late"), ("TAP", "Send", None)], [("DONE", None, None)]],
                        [item()], answer="Sent.")
        self.scripted(script, screens=[COMPOSE, COMPOSE, sent("I'm running late")])
        started = tools.call("phone_task", {"message": "Text Sam I'm running late", "wait_seconds": 0}, None)
        holder_run = runtime.runs[started.structured["run_id"]]
        guard = time.monotonic() + 120
        while holder_run.approval is None and holder_run.finished_at is None and time.monotonic() < guard:
            time.sleep(.01)
        first = tools.call("phone_task_status", {"run_id": holder_run.id}, None)
        self.assertEqual(first.structured["status"], "waiting_for_approval")
        self.assertEqual(runtime.phone_ownership(None)["holder"]["kind"], "mcp")
        second = tools.call("phone_task", {"message": "Open Settings", "wait_seconds": 0}, None)
        self.assertFalse(second.is_error, second.text)
        self.assertEqual((second.structured["status"], second.structured["position"]), ("queued", 1))
        self.assertIn("you are number 1", second.text)
        self.assertIn("Nothing has been done on the phone yet", second.text)
        waiting = runtime.runs[second.structured["run_id"]]
        status = tools.call("phone_task_status", {"run_id": waiting.id}, None)
        self.assertEqual(status.structured["status"], "queued")
        refused = tools.call("phone_task", {"message": "Open Display", "queue": False}, None)
        self.assertTrue(refused.is_error, refused.text)
        self.assertEqual(runtime.queues.total(), 1)
        runtime.stop_run(waiting.id)
        self.assertEqual((waiting.status, runtime.queues.total()), ("stopped", 0))
        self.assertIsNotNone(holder_run.approval)
        holder_run.answer_approval(holder_run.approval["id"], False)
        self.wait_done(holder_run)


if __name__ == "__main__":
    unittest.main()
