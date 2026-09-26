"""A request of several actions runs as ordered milestones, so a finished-looking screen cannot skip one. Offline."""

import json
import unittest
from unittest.mock import Mock

from mobile_agent.agent import Agent
from mobile_agent.milestones import MilestoneError, milestone_candidate, validate_milestones
from mobile_agent.models import Decision
from mobile_agent.state import Element, Snapshot
from mobile_agent.task_policy import StopGate

CALTRACK = ("Log today's breakfast in CalTrack — search the food database for 'oatmeal', add a serving, and give "
            "me the calories and macros.")
STEPS = ["Search the food database for 'oatmeal' -- done when oatmeal results are shown",
         "Add one serving of oatmeal to breakfast and save it -- done when it is in today's breakfast",
         "Give the calories and macros -- done when they are on screen"]


class Helper:
    def __init__(self, milestones):
        self.milestones, self.calls, self.purposes = milestones, 0, []

    def complete(self, messages, token_limit, timeout, purpose):
        self.calls += 1
        self.purposes.append(purpose)
        return {"choices": [{"finish_reason": "stop",
                             "message": {"content": json.dumps({"milestones": self.milestones})}}]}


def done():
    return Decision("DONE", None, .95, .95, .02, "t", 0, {}, StopGate.CONTINUE)


class CandidateTests(unittest.TestCase):
    def test_several_actions_with_a_change_are_candidates(self):
        self.assertTrue(milestone_candidate(CALTRACK))
        self.assertTrue(milestone_candidate("Search lockedin for a platform engineer and send them a message."))

    def test_questions_and_prohibitions_are_not(self):
        self.assertFalse(milestone_candidate("In Settings > General > About, what is the iOS version? "
                                             "Do not change any setting."))
        self.assertFalse(milestone_candidate("Open the note 'MobsterBench Note' and find the web link. Only look; "
                                             "do not open, favorite, share, edit or delete anything."))


class ValidationTests(unittest.TestCase):
    def test_prohibitions_are_copied_into_every_milestone(self):
        goal = CALTRACK + " Do not delete anything."
        items = validate_milestones({"milestones": STEPS}, goal)
        self.assertEqual(len(items), 3)
        self.assertTrue(all(item.endswith("Do not delete anything.") for item in items))

    def test_a_single_step_or_a_bad_list_compiles_nothing(self):
        self.assertEqual(validate_milestones({"milestones": []}, CALTRACK), [])
        for bad in ({"milestones": ["only one"]}, {"milestones": "x"}, {}, {"milestones": [""] * 3}):
            with self.assertRaises(MilestoneError):
                validate_milestones(bad, CALTRACK)


class AgentTests(unittest.TestCase):
    def test_each_milestone_must_be_done_before_the_request_is(self):
        screen = Snapshot([Element("1", "Add Food", "Button", (.1, .5, .3, .05))], "CalTrack\nAdd Food", 393, 852,
                          "synthetic_fixture", bundle_id="com.iosworld.benchmark.caltrack")
        driver = Mock(spec=["observe", "execute", "close", "can_type"], can_type=False)
        driver.observe.return_value = screen
        model = Mock(spec=["decide", "stable_completion"], stable_completion=True)
        model.decide.return_value = done()
        helper = Helper(STEPS)
        events = []
        result = Agent(driver, model, helper, settle_seconds=0, emit=events.append).run(CALTRACK, execute=True)
        self.assertEqual(helper.purposes[0], "milestone_compile")
        compiled = next(e for e in events if e["event"] == "milestones_compiled")
        self.assertEqual(compiled["count"], 3)
        goals = [call.args[1] if len(call.args) > 1 else call.kwargs.get("goal") for call in model.decide.call_args_list]
        seen = [g for g in goals if isinstance(g, str)]
        self.assertTrue(any("Current milestone: " + STEPS[0] in g for g in seen))
        self.assertTrue(any("Current milestone: " + STEPS[2] in g for g in seen))
        self.assertEqual(result["subgoals"], 4)

    def test_a_request_of_one_action_compiles_nothing(self):
        driver = Mock(spec=["observe", "execute", "close", "can_type"], can_type=False)
        driver.observe.return_value = Snapshot([], "Settings", 393, 852, "synthetic_fixture")
        model = Mock(spec=["decide", "stable_completion"], stable_completion=True)
        model.decide.return_value = done()
        helper = Helper(STEPS)
        Agent(driver, model, helper, settle_seconds=0).run("Open Settings", execute=True)
        self.assertNotIn("milestone_compile", helper.purposes)


if __name__ == "__main__":
    unittest.main()
