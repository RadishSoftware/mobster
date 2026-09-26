"""Dataflow plans: steps across apps, values passed only as verified answers. Offline."""

import json
import unittest

from mobile_agent import plans
from mobile_agent.agent import Agent

SETTINGS, SAFARI = "com.apple.Preferences", "com.apple.mobilesafari"
GOAL = ("Find this iPhone's Model Name in Settings > General > About, then in Safari open the Wikipedia "
        "article about that model and report the year it was released. Do not change any setting.")
PLAN = {"plan": True, "steps": [
    {"app": SETTINGS, "request": "In Settings > General > About, report the Model Name.",
     "finds": {"model_name": "the Model Name"}},
    {"app": SAFARI, "request": "Open the Wikipedia article about the {model_name} and report its release year.",
     "finds": {"release_year": "the year it was released"}}],
    "answer": {"release_year": "{release_year}"}}


class ValidatePlanTests(unittest.TestCase):
    def test_a_valid_plan_is_normalized(self):
        plan = plans.validate_plan(PLAN, {SETTINGS, SAFARI}, ["release_year"])
        self.assertEqual(plan["answer"], {"release_year": "release_year"})
        request = plans.step_request(plan["steps"][1], {"model_name": "iPhone 15 Pro"}, GOAL)
        self.assertIn("about the iPhone 15 Pro", request)
        self.assertTrue(request.endswith("Do not change any setting."))

    def test_plans_that_overreach_are_refused(self):
        def variant(**changes):
            data = json.loads(json.dumps(PLAN))
            for path, value in changes.items():
                target = data
                *head, last = path.split(".")
                for key in head:
                    target = target[int(key)] if key.isdigit() else target[key]
                target[int(last) if last.isdigit() else last] = value
            return data
        for bad in (variant(**{"steps.1.app": "com.apple.MobileSMS"}),                    # an app not allowed
                    variant(**{"steps.1.request": "Search the web for {owner_name} now."}),  # an unknown value
                    variant(**{"answer.release_year": "{model_name} in {release_year}"}),    # a composed answer
                    variant(steps=[PLAN["steps"][0]])):                                     # a single step
            with self.subTest(bad=bad), self.assertRaises(plans.PlanError):
                plans.validate_plan(bad, {SETTINGS, SAFARI}, ["release_year"])
        self.assertIsNone(plans.validate_plan({"plan": False}, {SETTINGS, SAFARI}, ["release_year"]))

    def test_only_multi_app_questions_with_dataflow_are_candidates(self):
        self.assertTrue(plans.plan_candidate(GOAL, {SETTINGS, SAFARI}, ["release_year"]))
        self.assertFalse(plans.plan_candidate(GOAL, {SETTINGS}, ["release_year"]))
        self.assertFalse(plans.plan_candidate(GOAL, {SETTINGS, SAFARI}, []))


class Helper:
    calls = 0

    def complete(self, messages, tokens, timeout, purpose):
        self.calls += 1
        return {"choices": [{"message": {"content": json.dumps(PLAN)}, "finish_reason": "stop"}]}


class FakeSub:
    """Stands in for each step's run: answers from a table keyed by the step's app."""

    answers = {SETTINGS: {"model_name": "iPhone 15 Pro"}, SAFARI: {"release_year": "2023"}}
    seen = []

    def __init__(self, driver, model, helper, **options):
        self.app = options["allowed_bundles"][0]

    def run(self, goal, **kwargs):
        FakeSub.seen.append(goal)
        return {"status": "completed_unverified", "data": self.answers[self.app], "actions": 2, "decisions": 3}


class AgentPlanTests(unittest.TestCase):
    def test_values_flow_from_one_app_to_the_next(self):
        import mobile_agent.agent as agent_module
        schema = {"type": "object", "properties": {"release_year": {"type": "string"}},
                  "required": ["release_year"], "additionalProperties": False}
        original, agent_module.Agent = agent_module.Agent, FakeSub
        try:
            events = []
            agent = original(object(), object(), Helper(), emit=events.append, allowed_bundles=[SETTINGS, SAFARI])
            agent.driver = type("D", (), {"launch": lambda self, bundle, timeout=20: None,
                                          "observe": lambda self, timeout=10: None})()
            result = agent.run(GOAL, execute=True, output_schema=schema, output_format="json")
        finally:
            agent_module.Agent = original
        self.assertEqual(result["data"], {"release_year": "2023"})
        self.assertEqual((result["actions"], result["plan_steps"]), (4, 2))
        self.assertIn("iPhone 15 Pro", FakeSub.seen[-1])
        self.assertIn("plan_compiled", [event["event"] for event in events])


if __name__ == "__main__":
    unittest.main()
