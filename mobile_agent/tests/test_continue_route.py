"""An answer that looks right before the request's route is finished continues once. Offline."""

import time
import unittest
from types import SimpleNamespace

from mobile_agent.agent import Agent, ContinueTask
from mobile_agent.demo import DemoDriver, DemoHelper, DemoModel


class ContinueRouteTests(unittest.TestCase):
    def setUp(self):
        self.agent = Agent(DemoDriver(), DemoModel(), DemoHelper(), max_steps=30, max_seconds=120)

    def state(self, **changes):
        base = dict(deadline=time.monotonic() + 100, metrics=[None] * 3, continued=False)
        base.update(changes)
        return SimpleNamespace(**base)

    def test_a_skipped_step_with_a_plausible_answer_continues_once(self):
        state = self.state()
        with self.assertRaises(ContinueTask) as raised:
            self.agent._continue_if_route_unfinished(state, {"claim_final_state": .1, "claim_field": .9})
        self.assertIn("remaining steps", str(raised.exception))
        self.assertTrue(state.continued)
        self.agent._continue_if_route_unfinished(state, {"claim_final_state": .1, "claim_field": .9})  # not twice

    def test_a_wrong_answer_or_a_finished_route_ends_as_before(self):
        for signals in ({"claim_final_state": .1, "claim_field": .2}, {"claim_final_state": .9, "claim_field": .9},
                        {}, {"claim_final_state": None, "claim_field": .9}):
            self.agent._continue_if_route_unfinished(self.state(), signals)

    def test_no_continuation_without_enough_time_or_steps(self):
        signals = {"claim_final_state": .1, "claim_field": .9}
        self.agent._continue_if_route_unfinished(self.state(deadline=time.monotonic() + 10), signals)
        self.agent._continue_if_route_unfinished(self.state(metrics=[None] * 28), signals)


if __name__ == "__main__":
    unittest.main()
