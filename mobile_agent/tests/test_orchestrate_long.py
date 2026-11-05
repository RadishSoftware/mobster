import os
import time
from unittest.mock import patch

from mobile_agent.tests.test_longform import INSTAGRAM
from mobile_agent.tests.test_longform import BaseServer as Base

WHATSAPP = {"id": "whatsapp", "name": "WhatsApp", "bundleId": "net.whatsapp.WhatsApp", "installed": True,
            "available": True}
MESSAGES = {"id": "messages", "name": "Messages", "bundleId": "com.apple.MobileSMS", "installed": True,
            "available": True}
INBOX = "Go through my messages across Messages, WhatsApp and Instagram and draft replies"


class OnePlanTests(Base):
    def setUp(self):
        super().setUp()
        apps = self.runtime.apps
        self.runtime.apps = lambda: [WHATSAPP, MESSAGES] + [app for app in apps() if app["id"] not in ("messages",)]

    def plan_shown(self, run, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pending = run.public()["approval"]
            if pending and pending.get("kind") == "plan":
                return pending
            time.sleep(.02)
        self.fail("no plan was shown")

    def frontier_kwargs(self):
        from mobile_agent import engines
        seen = []
        build = engines.build_frontier
        patcher = patch.object(engines, "build_frontier", lambda *a, **kw: seen.append(kw) or build(*a, **kw))
        patcher.start()
        self.addCleanup(patcher.stop)
        return seen

    def test_a_goal_across_apps_gets_the_orchestrators_plan_not_a_long_read_of_one(self):
        status, data, _ = self.call("POST", "/api/runs", {"goal": INBOX, "engine": "smart"})
        self.assertEqual(status, 201, data)
        run = self.runtime.runs[data["run"]["id"]]
        self.assertIsNone(run.longform)
        pending = self.plan_shown(run)
        plan = pending["plan"]
        self.assertEqual([app["name"] for app in plan["apps"]], ["Messages", "WhatsApp", "Instagram"])
        self.assertEqual((plan["summary"], plan["policy"], plan.get("long")),
                         ("Read your messages in Messages, WhatsApp and Instagram and draft replies",
                          ["Nothing sends until you OK it"], None))
        # Skip: the task ends with nothing done on the phone.
        self.assertTrue(run.answer_approval(pending["id"], False))
        self.finished(run)
        self.assertEqual((run.summary["status"], run.summary["outcome"]), ("approval_denied", "declined"))
        self.assertEqual(self.state["actions"], [])

    def test_a_long_task_shows_its_budget_on_the_plan_and_asks_before_each_act_once_started(self):
        seen = self.frontier_kwargs()
        status, data, _ = self.call("POST", "/api/runs", {"goal": "Watch 40 Reels on Instagram", "engine": "smart"})
        self.assertEqual(status, 201, data)
        run = self.runtime.runs[data["run"]["id"]]
        pending = self.plan_shown(run)
        long = pending["plan"]["long"]
        self.assertEqual((long["kind"], long["budget"]["items"]), ("feed", 40))
        self.assertEqual(pending["plan"]["apps"], [{"name": "Instagram", "bundleId": INSTAGRAM, "does": None}])
        self.assertEqual([choice["id"] for choice in pending["choices"]][0], "default")
        self.assertEqual(pending["choices"][0]["label"], long["budget"]["label"])
        # Nothing touches the phone while the plan waits.
        time.sleep(.2)
        self.assertEqual(self.state["actions"], [])
        self.assertTrue(run.answer_approval(pending["id"], True, "default"))
        self.finished(run)
        self.assertEqual(run.plan["long"]["budget"]["items"], 40)
        # The long run's general asker is mounted: each reply, like or message asks, with Ask before acting off too.
        self.assertTrue(seen and seen[0]["longform"] is not None and callable(seen[0]["forced_approve"]))
        kinds = [event["event"] for event in run.events]
        self.assertLess(kinds.index("approval_resolved"), kinds.index("longform_plan"))

    def test_a_long_run_still_asks_with_ask_before_acting_off(self):
        os.environ["MOBSTER_ASK_BEFORE_ACTING"] = "0"
        seen = self.frontier_kwargs()
        status, data, _ = self.call("POST", "/api/runs", {"goal": "Watch 40 Reels on Instagram", "engine": "smart"})
        run = self.runtime.runs[data["run"]["id"]]
        pending = self.plan_shown(run)
        self.assertTrue(run.answer_approval(pending["id"], True))
        self.finished(run)
        # Ask before acting off is full access (permissions): no approver, and the long run's own asker is mounted.
        self.assertIsNone(seen[0]["approve"])
        self.assertEqual(seen[0]["permissions"].mode, "dont_ask")
        self.assertTrue(callable(seen[0]["forced_approve"]))

    def test_another_budget_and_the_proposed_raise_are_chosen_on_the_plan(self):
        os.environ["MOBSTER_TASK_COST_LIMIT"] = "0.05"
        status, data, _ = self.call("POST", "/api/runs", {"goal": "Scroll Instagram Reels for 10 minutes and tell me "
                                                                  "what you learned", "engine": "smart"})
        run = self.runtime.runs[data["run"]["id"]]
        pending = self.plan_shown(run)
        long = pending["plan"]["long"]
        self.assertTrue(long["alternatives"], long)
        other = long["alternatives"][0]["id"]
        # The raise goes only with Start on this plan.
        with self.assertRaises(ValueError):
            run.answer_approval(pending["id"], False, allow_cost=True)
        self.assertTrue(run.answer_approval(pending["id"], True, other, allow_cost=True))
        # The plan is settled as it starts; the full time itself needn't run here.
        deadline = time.monotonic() + 10
        while run.plan is None and time.monotonic() < deadline:
            time.sleep(.02)
        self.runtime.stop_run(run.id)
        self.finished(run)
        chosen = next(item for item in long["alternatives"] if item["id"] == other)
        self.assertEqual(run.plan["long"]["budget"]["kind"], "time")
        proposed = chosen.get("proposedUsd") or long["cap"]["proposedUsd"]
        self.assertEqual(run.longform["capUsd"], proposed)

if __name__ == "__main__":
    import unittest
    unittest.main()
