"""The cached turn log (FrontierAgent._logged_prompt) and the planner's screenshot side. A long task keeps its own
notebook prompt. Offline: scripted models."""

import unittest

from mobile_agent import frontier
from mobile_agent.frontier import FrontierAgent
from mobile_agent.state import Element
from mobile_agent.tests.test_frontier import Driver, Script, screen


class Recording(Script):
    model = "claude-sonnet-5-5"

    def __init__(self, steps):
        super().__init__(steps)
        self.prompts = []

    def complete(self, messages, schema, timeout=60, **kwargs):
        if "items" not in schema["properties"]:
            self.prompts.append(messages)
        return super().complete(messages, schema, timeout)


def rows(*labels):
    return screen(*[Element(str(n + 1), label, "Button", (0, .1 + n * .06, 1, .05)) for n, label in enumerate(labels)])


SCREENS = [rows("General", "Privacy"), rows("About", "Fonts"), rows("Name", "Version"), rows("Name", "Version")]
STEPS = [("TAP", "General", None), ("TAP", "About", None), ("DONE", None, None), ("DONE", None, None)]


def run():
    model = Recording(list(STEPS))
    FrontierAgent(Driver(SCREENS), model, screenshots=False, settle_seconds=0).run("Open Settings, General, About.")
    return model.prompts


class TurnLog(unittest.TestCase):
    def test_finished_turns_are_blocks_never_rewritten_and_the_newest_ends_the_cache(self):
        prompts = run()
        self.assertGreaterEqual(len(prompts), 3)
        second, third = prompts[1][1]["content"], prompts[2][1]["content"]
        # Everything before the second prompt's turn text is a prefix of the third prompt, block for block.
        stable = [block for block in second[:-1]]
        self.assertEqual([b["text"] for b in third[:len(stable)]], [b["text"] for b in stable])
        cached = [b for b in third if b.get("cache")]
        self.assertEqual(len(cached), 2)  # the request and the newest turn block (the system prompt is the third)
        self.assertTrue(cached[-1]["text"].startswith("\nT1: TAP 'About'"))
        self.assertIn("T0: TAP 'General' -> screen changed", "".join(b["text"] for b in third))
        turn = third[-1]["text"]
        self.assertIn("Screen elements:", turn)
        self.assertNotIn("Recent actions", turn)  # the log replaces the sliding window

    def test_a_noted_value_and_a_changed_plan_go_into_their_turn_s_block(self):
        model = Recording(list(STEPS))
        model.updates = {}

        def noting(messages, schema, timeout=60, **kwargs):
            out, usage = Recording.complete(model, messages, schema, timeout)
            if "items" not in schema["properties"] and model.usage["calls"] == 1:
                out = {**out, "notes_add": ["Version 26.0"], "plan": "General, then About"}
            return out, usage
        model.complete = noting
        FrontierAgent(Driver(SCREENS), model, screenshots=False, settle_seconds=0).run("Open Settings, General, About.")
        log = "".join(b["text"] for b in model.prompts[1][1]["content"])
        self.assertIn("noted: Version 26.0", log)
        self.assertIn("plan: General, then About", log)


class ScreenshotSide(unittest.TestCase):
    def test_the_planner_sees_a_448_pixel_screenshot(self):
        self.assertEqual(frontier.SCREENSHOT_SIDE, 448)


if __name__ == "__main__":
    unittest.main()
