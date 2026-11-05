"""Pack pins (packs/pins.py, teams/control_plane.py): a team pins an app pack to the app version it was checked on, or
turns it off, and a Mac whose pack doesn't match steps that pack aside everywhere it is used (agent tools, MCP tools,
the front door, a run that names its pack) with nothing tapped; the Mac reads the pins at start and at each head check,
keeps the last ones when the license API can't answer, drops them when the control plane is off or the Mac leaves, and
tells its team which pack versions it runs. Offline: the toy app of test_packs_framework.py, a scripted phone, the fake
license API."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent import harness_api
from mobile_agent.mcp_server.protocol import Call
from mobile_agent.mcp_server.tools import ToolError
from mobile_agent.packs import front_door, health, pins, register, tools
from mobile_agent.tests.test_packs_framework import APP, PACK, McpTests, phone


class PinsBase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(pins.set_pins, {})
        pins.set_pins({})


class RuleTests(PinsBase):
    def test_a_pack_works_as_shipped_with_no_pin_or_a_pin_to_the_version_it_was_checked_on(self):
        self.assertIsNone(pins.aside(PACK))
        pins.set_pins({"chirp": "1.2.0"})
        self.assertIsNone(pins.aside(PACK))
        pins.set_pins({"other": "9.9"})  # another pack's pin says nothing about this one
        self.assertIsNone(pins.aside(PACK))

    def test_another_version_or_off_steps_it_aside_with_a_sentence_that_says_what_to_do(self):
        pins.set_pins({"chirp": "1.1.0"})
        why = pins.aside(PACK)
        self.assertIn("pinned the Chirp pack to Chirp 1.1.0", why)
        self.assertIn("checked on Chirp 1.2.0", why)
        self.assertIn("Update Mobster", why)
        pins.set_pins({"chirp": "off"})
        self.assertEqual(pins.aside(PACK), "Your team turned off the Chirp pack.")

    def test_a_pack_with_no_checked_version_cannot_match_a_pin(self):
        bare = SimpleNamespace(key="bare", name="Bare", verified=None)
        pins.set_pins({"bare": "1.0"})
        self.assertIn("no checked version", pins.aside(bare))
        pins.set_pins({"bare": "off"})
        self.assertIn("turned off", pins.aside(bare))

    def test_set_pins_replaces_and_ignores_what_is_not_text(self):
        pins.set_pins({"chirp": "off", 3: "x", "bad": 4})
        self.assertEqual(pins.current(), {"chirp": "off"})
        pins.set_pins(None)
        self.assertEqual(pins.current(), {})

    def test_the_state_a_mac_reports_is_the_version_it_has_and_whether_it_steps_aside(self):
        self.assertEqual(pins.state(PACK), {"key": "chirp", "version": "1.2.0", "aside": False})
        pins.set_pins({"chirp": "1.0.0"})
        self.assertEqual(pins.state(PACK), {"key": "chirp", "version": "1.2.0", "aside": True})

    def test_the_health_route_says_what_a_pack_is_pinned_to(self):
        self.assertIsNone(health.status(PACK)["pin"])
        pins.set_pins({"chirp": "1.0.0"})
        self.assertEqual(health.status(PACK)["pin"], {"to": "1.0.0", "aside": True})
        pins.set_pins({"chirp": "1.2.0"})
        self.assertEqual(health.status(PACK)["pin"], {"to": "1.2.0", "aside": False})


class ToolTests(PinsBase):
    def test_a_pinned_aside_pack_is_not_offered_to_a_run_and_comes_back_when_the_pin_matches(self):
        make = tools.factory(PACK, "OPEN")
        about = SimpleNamespace(engine="smart", goal="Check my Chirp DMs", app_bundle=APP, interactive=True)
        self.assertIsNotNone(make(about))
        pins.set_pins({"chirp": "off"})
        self.assertIsNone(make(about))
        pins.set_pins({"chirp": "1.2.0"})
        self.assertIsNotNone(make(about))

    def test_a_pin_set_in_the_middle_of_a_run_has_the_tool_step_aside_without_touching_the_phone(self):
        from mobile_agent.tests.test_packs_framework import context

        tool = tools.PackTool(PACK, "OPEN", sleep=lambda s: None)
        with tempfile.TemporaryDirectory() as folder:
            device = phone("springboard")
            ctx, events = context(device, data_dir=Path(folder))
            prepared = tool.prepare(ctx, {"text": "dms"}, device.observe())
            pins.set_pins({"chirp": "1.0.0"})
            result = tool.perform(ctx, prepared, {"text": "dms"}, device.observe())
            self.assertIn("CH_OPEN steps aside for Chirp", result.feedback)
            self.assertIn("Do it the usual way", result.feedback)
            self.assertEqual(device.actions, [])
            self.assertEqual([e for e in events if e["event"] == "app_pack"], [])


class McpPinTests(PinsBase):
    """The packs' MCP tools refuse a pinned-aside pack before any permission is asked or anything is tapped."""

    def toolset(self, device, folder):
        return McpTests("test_open_and_read_over_mcp").toolset(device, folder)

    def test_a_pinned_aside_pack_answers_an_error_and_taps_nothing_over_mcp(self):
        from mobile_agent.packs.mcp import PacksProvider

        pins.set_pins({"chirp": "off"})
        with tempfile.TemporaryDirectory() as folder:
            device = phone("springboard")
            toolset, _, permits = self.toolset(device, folder)
            for name, arguments in (("chirp_open", {"where": "dms"}), ("chirp_read", {"what": "dms"})):
                with self.assertRaises(ToolError) as caught:
                    PacksProvider([PACK], sleep=lambda s: None).call(name, arguments, Call("test"), toolset)
                self.assertIn("Your team turned off the Chirp pack.", str(caught.exception))
                self.assertIn("by hand", str(caught.exception))
            self.assertEqual((device.actions, permits), ([], []))

    def test_the_same_pack_works_once_the_pin_matches(self):
        from mobile_agent.packs.mcp import PacksProvider

        pins.set_pins({"chirp": "1.2.0"})
        with tempfile.TemporaryDirectory() as folder:
            toolset, _, _ = self.toolset(phone("springboard"), folder)
            opened = PacksProvider([PACK], sleep=lambda s: None).call(
                "chirp_open", {"where": "dms"}, Call("test"), toolset
            )
            self.assertIn("Now on your DMs in Chirp", opened.text)


class RunHookTests(PinsBase):
    """The pre-run hooks of a run that names its pack, and the front door."""

    def ctx(self, **extras):
        events = []
        return SimpleNamespace(
            extras=extras,
            emit=events.append,
            events=events,
            cancelled=lambda: False,
            runtime=None,
            run_id="r1",
            data_dir=None,
            goal="my DMs on Chirp",
            app_bundle=APP,
            engine="smart",
        ), events

    def test_a_run_that_names_a_pinned_aside_pack_ends_saying_so_before_anything_acts(self):
        pins.set_pins({"chirp": "1.0.0"})
        with patch.object(register, "all_packs", lambda: [PACK]):
            for hook, field in (
                (register.pack_read, "packRead"),
                (register.pack_check, "packCheck"),
                (register.pack_stats, "packStats"),
            ):
                spec = {"pack": "chirp", "reader": "dms", "n": 3, "arg": None} if field == "packRead" else "chirp"
                ctx, events = self.ctx(**{field: spec})
                device = phone("springboard")
                result = register.stepping_aside(hook)(ctx, device, None)
                self.assertIsInstance(result, harness_api.PreRun, field)
                self.assertEqual(
                    (result.result["outcome"], result.result["status"]), ("check", "completion_not_confirmed")
                )
                self.assertIn("pinned the Chirp pack to Chirp 1.0.0", result.result["reason"])
                self.assertEqual(result.result["costUsd"], 0.0)
                self.assertEqual([e for e in events if e["event"] == "skill_started"], [], field)  # nothing acted
                self.assertEqual(device.actions, [], field)

    def test_a_run_that_is_not_about_a_pack_goes_on_untouched_by_the_wrapper(self):
        pins.set_pins({"chirp": "off"})
        ctx, _ = self.ctx()
        self.assertIsNone(register.stepping_aside(register.pack_read)(ctx, phone(), None))

    def test_the_front_door_leaves_the_request_to_the_general_agent_and_says_why(self):
        pins.set_pins({"chirp": "off"})
        plan = SimpleNamespace(pack=PACK, what="dms", kind="read")
        ctx, events = self.ctx()
        with (
            patch.object(front_door, "eligible", lambda c: True),
            patch.object(front_door, "parse", lambda *a, **k: plan),
        ):
            self.assertIsNone(front_door.hook(ctx, phone(), None))
        self.assertEqual(
            [(e["event"], e["routed"], e["why"]) for e in events],
            [("front_door", False, "Your team turned off the Chirp pack.")],
        )




if __name__ == "__main__":
    unittest.main()
