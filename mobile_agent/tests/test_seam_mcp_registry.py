"""Seam S10: a track's MCP tool provider. Its tools are listed after the core's, validated and called under the same
rules, masked; a clash with a core tool raises; file tools need --allow-files. Offline."""

import tempfile
import unittest

from mobile_agent.mcp_server import registry
from mobile_agent.mcp_server.protocol import ProtocolError, ToolResult
from mobile_agent.mcp_server.tools import ACTS, READ_ONLY, ToolError, ToolSet
from mobile_agent.tests.seam_support import isolate


class Threads:
    name = "threads"

    def __init__(self):
        self.calls = []

    def definitions(self, toolset):
        return [{"name": "phone_task", "description": "Run a task on the phone in a conversation.",
                 "inputSchema": {"type": "object", "properties": {"goal": {"type": "string", "minLength": 1}},
                                 "required": ["goal"], "additionalProperties": False}},
                {"name": "list_files", "description": "List files.",
                 "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}]

    def annotations(self):
        return {"phone_task": ACTS, "list_files": READ_ONLY}

    def instructions(self):
        return "phone_task runs a task on the user's iPhone and keeps the conversation."

    def call(self, name, arguments, call, toolset):
        self.calls.append((name, arguments, toolset))
        if arguments.get("goal") == "fail":
            raise ToolError("That didn't work.")
        return ToolResult(f"Done: {arguments.get('goal')}")


class McpRegistryTests(unittest.TestCase):
    def setUp(self):
        isolate(self)

    def toolset(self, **kwargs):
        tools = ToolSet(runs_dir=tempfile.mkdtemp(prefix="mobster-seam-mcp-"), keyless=True, **kwargs)
        self.addCleanup(tools.shutdown)
        return tools

    def test_a_providers_tool_is_listed_and_called(self):
        provider = Threads()
        registry.register_provider(provider)
        tools = self.toolset()
        listed = {tool["name"]: tool for tool in tools.list_tools()}
        self.assertIn("phone_task", listed)
        self.assertEqual(listed["phone_task"]["annotations"], ACTS)
        self.assertNotIn("list_files", listed)  # file tools need --allow-files
        self.assertTrue(tools.instructions().endswith("phone_task runs a task on the user's iPhone and keeps the "
                                                      "conversation."))
        result = tools.call("phone_task", {"goal": "Text Sam"}, None)
        self.assertEqual(result.text, "Done: Text Sam")
        self.assertIs(provider.calls[0][2], tools)
        self.assertTrue(tools.call("phone_task", {"goal": "fail"}, None).is_error)
        with self.assertRaises(ProtocolError):  # validated like a core tool
            tools.call("phone_task", {"goal": ""}, None)
        with self.assertRaises(ProtocolError):
            tools.call("list_files", {}, None)  # not listed, so not callable

    def test_file_tools_need_allow_files(self):
        registry.register_provider(Threads())
        tools = self.toolset(allow_files=True)
        self.assertIn("list_files", [tool["name"] for tool in tools.list_tools()])

    def test_a_clash_with_a_core_tool_raises(self):
        class Clash(Threads):
            name = "clash"

            def annotations(self):
                return {"screen": READ_ONLY}
        with self.assertRaisesRegex(ValueError, "core Mobster tool"):
            registry.register_provider(Clash())
        registry.register_provider(Threads())
        with self.assertRaises(ValueError):
            registry.register_provider(Threads())  # the same name twice
        with self.assertRaises(ValueError):
            registry.register_provider(object())
        self.assertEqual([p.name for p in registry.providers()], ["threads"])


if __name__ == "__main__":
    unittest.main()
