import unittest

from mobile_agent.ai_plans import phone_tools
from mobile_agent.app_api.mcp import AppApiProvider
from mobile_agent.mcp_server import protocol, tools
from mobile_agent.packs.mcp import PacksProvider
from mobile_agent.tests.test_app_api import PACK

RISK = ("readOnlyHint", "destructiveHint", "openWorldHint")
SAME_ACTION = {"tap": "tap", "type_text": "type_text", "swipe": "swipe", "open_url": "open_url", "home": "home",
               "open_app": "launch_app", "screen": "screen", "wait": "wait"}


def risk(hints):
    return {key: hints.get(key) for key in RISK}


class ToolRiskTests(unittest.TestCase):
    def test_a_run_s_phone_tools_that_act_are_destructive(self):
        reads = {"screen", "read_list", "wait", "ask_user"}
        for name in phone_tools.NAMES:
            with self.subTest(tool=name):
                hints = phone_tools.ANNOTATIONS[name]
                self.assertEqual(risk(hints), risk(protocol.READ_ONLY if name in reads else protocol.ACTS))

    def test_the_same_action_has_the_same_risk_on_both_surfaces(self):
        for run_tool, mcp_tool in SAME_ACTION.items():
            with self.subTest(tool=run_tool):
                self.assertEqual(risk(phone_tools.ANNOTATIONS[run_tool]), risk(tools.ANNOTATIONS[mcp_tool]))

    def test_pack_and_app_api_tools_act(self):
        for provider in (PacksProvider([PACK]), AppApiProvider([PACK], profiles=lambda: [])):
            annotations = provider.annotations()
            self.assertTrue(annotations)
            for name, hints in annotations.items():
                with self.subTest(tool=name):
                    self.assertEqual(risk(hints), risk(protocol.ACTS))

    def test_one_definition(self):
        self.assertIs(tools.ACTS, protocol.ACTS)
        self.assertIs(tools.READ_ONLY, protocol.READ_ONLY)
        self.assertIs(tools.LOCAL, protocol.LOCAL)


if __name__ == "__main__":
    unittest.main()
