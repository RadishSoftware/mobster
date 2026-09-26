"""One request timing out costs a re-observation, not the run; the run deadline never retries. Offline."""

import time
import unittest
from types import SimpleNamespace

from mobile_agent.agent import STEP_INTERRUPTION_RETRIES, Agent
from mobile_agent.demo import DemoDriver, DemoHelper, DemoModel
from mobile_agent.drivers import DriverRejection
from mobile_agent.run_state import RunDeadline
from mobile_agent.transport import TransportError


class StepInterruptionTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.agent = Agent(DemoDriver(), DemoModel(), DemoHelper(), emit=self.events.append,
                           max_steps=30, max_seconds=120)

    def state(self, **changes):
        base = dict(deadline=time.monotonic() + 100, in_flight_effect=None, action_outcome="acknowledged",
                    loop_fallback_snapshot=None, hint=None)
        base.update(changes)
        return SimpleNamespace(**base)

    def test_a_request_timeout_retries_a_bounded_number_of_times(self):
        state = self.state()
        for _ in range(STEP_INTERRUPTION_RETRIES):
            self.assertTrue(self.agent._retry_interrupted_step(state, 3, TimeoutError()))
        self.assertFalse(self.agent._retry_interrupted_step(state, 3, TimeoutError()))
        self.assertIn("interrupted", state.hint)
        self.assertEqual([e["event"] for e in self.events], ["step_interrupted"] * STEP_INTERRUPTION_RETRIES)

    def test_transport_errors_retry_but_the_deadline_and_native_rejections_do_not(self):
        self.assertTrue(self.agent._retry_interrupted_step(self.state(), 0, TransportError("x")))
        self.assertFalse(self.agent._retry_interrupted_step(self.state(), 0, RunDeadline()))
        self.assertFalse(self.agent._retry_interrupted_step(self.state(), 0, DriverRejection("refused")))
        self.assertFalse(self.agent._retry_interrupted_step(self.state(deadline=time.monotonic() + 1), 0,
                                                            TimeoutError()))

    def test_an_unknown_in_flight_effect_is_recorded_before_retrying(self):
        recorded = []
        effects = SimpleNamespace(record_outcome=lambda *args, outcome: recorded.append((args, outcome)))
        state = self.state(in_flight_effect=("TAP", "Delete", None), action_outcome="unknown", effects=effects)
        self.assertTrue(self.agent._retry_interrupted_step(state, 0, TimeoutError()))
        self.assertEqual(recorded, [(("TAP", "Delete", None), "unknown")])
        self.assertIsNone(state.in_flight_effect)


if __name__ == "__main__":
    unittest.main()
