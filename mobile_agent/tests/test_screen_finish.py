"""Screen finish: an open-a-screen request ends once code proves the screen (its title names the contract's goal)
with no DONE turn; anything to report, or a screen titled otherwise, keeps the normal turn. Offline: scripted model,
fake driver."""

import unittest

from mobile_agent import contract as K
from mobile_agent.frontier import FrontierAgent, screen_goal, titled_with
from mobile_agent.state import Element
from mobile_agent.tests.test_frontier import Driver, screen

SETTINGS = "com.apple.Preferences"


class Model:
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "calls": 0}

    def __init__(self, items):
        self.items, self.turns = items, 0

    def complete(self, messages, schema, timeout=60, reasoning=None):
        if "items" in schema["properties"]:
            return {"items": self.items}, {}
        self.turns += 1
        if self.turns == 1:
            act = {"op": "TAP", "target": "e2", "text": None}
            return {"thought": "", "plan": "", "notes_add": [], "checklist_updates": [], "actions": [act],
                    "answer": None}, {}
        return {"thought": "", "plan": "", "notes_add": [], "checklist_updates": [], "answer": "Opened Camera.",
                "actions": [{"op": "DONE", "target": None, "text": None}]}, {}


def run(items, title="Camera"):
    root = screen(Element("1", "Settings", "NavigationBar", (0, .07, 1, .06)),
                  Element("2", "Camera", "Button", (0, .4, 1, .06)), bundle=SETTINGS)
    page = screen(Element("3", title, "NavigationBar", (0, .07, 1, .06)),
                  Element("4", "Formats", "Button", (0, .3, 1, .06)), bundle=SETTINGS)
    driver = Driver([root, page, page, page, page])
    model, events = Model(items), []
    result = FrontierAgent(driver, model, apps={SETTINGS: "Settings"}, emit=events.append, screenshots=False,
                           settle_seconds=0).run("In Settings, open Camera. Do not change any setting.")
    return result, model, [e for e in events if e.get("event") == "frontier_screen_finish"]


OPEN = [{"kind": "READ", "app": "Settings", "what": "Camera"},
        {"kind": "FORBID", "app": "Settings", "what": "any setting", "act": "other"}]


class ScreenFinish(unittest.TestCase):
    def test_a_screen_titled_with_the_goal_ends_the_run_with_no_done_turn(self):
        result, model, finishes = run(OPEN)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(model.turns, 1)  # the tapping turn only
        self.assertEqual(len(finishes), 1)

    def test_another_title_or_something_to_report_keeps_the_done_turn(self):
        for items, title in ((OPEN, "Camera Modes"),
                             (OPEN + [{"kind": "REPORT", "app": "Settings", "what": "the camera format"}], "Camera")):
            result, model, finishes = run(items, title)
            self.assertEqual(finishes, [], (title, len(items)))
            self.assertGreaterEqual(model.turns, 2)  # a planner turn decided the end


class GoalRule(unittest.TestCase):
    def test_the_goal_is_the_last_reads_name_and_only_for_navigation_contracts(self):
        nav = K.Contract.from_items([{"kind": "READ", "app": "Settings", "what": "General"},
                                     {"kind": "READ", "app": "Settings", "what": "iPhone Storage"}], "x")
        self.assertEqual(screen_goal(nav), ["iphone storage"])
        report = K.Contract.from_items([{"kind": "READ", "what": "contact Bench Tester"},
                                        {"kind": "REPORT", "what": "city"}], "x")
        self.assertIsNone(screen_goal(report))

    def test_a_title_matches_the_name_or_its_screen(self):
        page = screen(Element("1", "Wallpaper", "NavigationBar", (0, .07, 1, .06)))
        self.assertTrue(titled_with(page, ["wallpaper screen via the settings search field"]))
        self.assertFalse(titled_with(page, ["wallpaper settings"]))
        self.assertFalse(titled_with(screen(Element("1", "Camera", "Button", (0, .3, 1, .06))), ["camera"]))


if __name__ == "__main__":
    unittest.main()
