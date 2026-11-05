"""MCP continuation handles remain usable without exposing other code-like output."""

from mobile_agent.mcp_server import registry
from mobile_agent.mcp_server.protocol import READ_ONLY, Call, ToolResult
from mobile_agent.tests.test_mcp_tools import ToolsHarness
from mobile_agent.threads.mcp import schema_graph_status


class ContinuationHandleTests(ToolsHarness):
    def test_graph_handles_survive_code_masking_but_arbitrary_output_does_not(self):
        workflow_id, batch_id = "abcdefabcdef", "abcdef123456"

        class Provider:
            name = "workflows"

            def definitions(self, toolset):
                return [{"name": "graph_status", "description": "read a graph", "inputSchema": schema_graph_status()}]

            def annotations(self):
                return {"graph_status": READ_ONLY}

            def instructions(self):
                return ""

            def call(self, name, arguments, call, toolset):
                return ToolResult("Graph abcdef accepted; detail abcdef is secret", structured={
                    "workflow_id": workflow_id, "batch_id": batch_id, "status": "running",
                    "status_uri": (f"/api/workflows/{workflow_id}/runs" if not arguments.get("wait_seconds") else
                                   "/api/workflows/ffffffffffff/runs?note=abcdef"),
                    "result": {"note": "secret abcdef", "runId": "abcdefabcdef"}})

        tools = self.make(keyless=True)
        registry.register_provider(Provider())
        tools.codes.all_secrets = lambda: ["abcdef"]
        result = tools.call("graph_status", {"workflow_id": workflow_id, "batch_id": batch_id}, Call(1))
        self.assertFalse(result.is_error, result.text)
        self.assertEqual((result.structured["workflow_id"], result.structured["batch_id"],
                          result.structured["status_uri"]),
                         (workflow_id, batch_id, f"/api/workflows/{workflow_id}/runs"))
        self.assertNotIn("abcdef", result.text)
        self.assertNotIn("abcdef", str(result.structured["result"]))
        wrong = tools.call("graph_status", {"workflow_id": "f" * 12, "batch_id": batch_id}, Call(2))
        self.assertNotEqual(wrong.structured["workflow_id"], workflow_id)
        self.assertNotIn("abcdef", wrong.structured["status_uri"])
        foreign = tools.call("graph_status", {"workflow_id": workflow_id, "batch_id": batch_id,
                                              "wait_seconds": 1}, Call(3))
        self.assertEqual(foreign.structured["workflow_id"], workflow_id)
        self.assertNotIn("abcdef", foreign.structured["status_uri"])
