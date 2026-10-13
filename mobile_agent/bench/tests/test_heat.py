"""Heats score a run by the milestones its screens showed, before or without a finish. Offline."""

import unittest

from mobile_agent.bench.heat import reached
from mobile_agent.bench.heldout import build_hard, milestones


class Milestones(unittest.TestCase):
    def setUp(self):
        self.task = next(t for t in build_hard() if t.id == "hard.four_apps")

    def test_every_hard_task_has_milestones_and_a_long_budget(self):
        for task in build_hard():
            self.assertTrue(milestones(task), task.id)
            self.assertGreaterEqual(task.max_seconds, 300)

    def test_a_milestone_counts_only_in_its_own_app(self):
        events = [{"event": "frontier_screen", "app": "Calculator", "rows": 'e1 StaticText "4817"', "t_ms": 3000},
                  {"event": "frontier_screen", "app": "Notes", "rows": 'e1 Cell "Locker code: 4817"', "t_ms": 9000},
                  {"event": "frontier_screen", "app": "Calculator", "rows": 'e1 StaticText "202440"', "t_ms": 60000}]
        self.assertEqual(reached(self.task, events), {0: 9.0, 4: 60.0})

    def test_no_screens_no_progress(self):
        self.assertEqual(reached(self.task, [{"event": "frontier_action"}]), {})


if __name__ == "__main__":
    unittest.main()
