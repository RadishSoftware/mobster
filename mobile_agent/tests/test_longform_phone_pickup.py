"""Pickup retains cumulative phone time, including interrupted and repeated segments."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent.longform import register
from mobile_agent.longform import session as S
from mobile_agent.longform.budget import Budget
from mobile_agent.longform.notebook import Notebook

PLAN = {"id": "p_test", "kind": "feed", "steps": []}


def pickup(test, source, destination, notebook, checkpoint, *, cap=240):
    runtime = SimpleNamespace(journal=None, runs={source: SimpleNamespace(cost_usd=0)})
    context = SimpleNamespace(run_id=destination, extras={"resumeFrom": source}, runtime=runtime,
                              emit=lambda event: None)
    models = SimpleNamespace(digest=None, synth=None, synth_fallback=None)
    with patch("mobile_agent.longform.store.load_notebook", return_value=notebook), \
            patch("mobile_agent.harness.checkpoints.load", return_value=checkpoint), \
            patch("mobile_agent.longform.models.model_set", return_value=models):
        register.pickup(context, None, None)
        result = S.start(run_id=destination, goal="Watch and report", plan=PLAN,
                         budget=Budget("time", 10, None, True, cap, "items", "10 minutes", "row"),
                         origin="app", nav_client=None, key="test", inference=lambda event: None,
                         ledger=None, cap_usd=None, emit=lambda event: None)
    test.addCleanup(result.close)
    return result


class PhonePickupTests(unittest.TestCase):
    def test_cumulative_total_is_authoritative_across_two_pickups_and_old_waits(self):
        old = Notebook("a" * 12, "Watch and report")
        old.phone.update(first_action_at=100, released_at=900, waited_s=600, cumulative_seconds=200)
        first = pickup(self, old.run_id, "b" * 12, old.to_dict(),
                       {"step": 8, "longform": {"phoneSeconds": 220}})
        self.assertEqual(first.phone_seconds(), 220)
        tick = [first.started]
        first.clock = lambda: tick[0]
        first.mark_action()
        tick[0] += 10
        first.add_wait(3)
        self.assertEqual(first.phone_seconds(), 227)
        first.save()  # the notebook persists the total even without a journal writer
        self.assertEqual(first.notebook.phone["cumulative_seconds"], 227)
        second = pickup(self, first.run_id, "c" * 12, first.notebook.to_dict(),
                        {"step": 9, "longform": {"phoneSeconds": 227}})
        self.assertEqual(second.phone_seconds(), 227)
        self.assertEqual(second.notebook.phone["cumulative_seconds"], 227)
        self.assertEqual(second.checkpoint()["phoneSeconds"], 227)

    def test_crash_checkpoint_without_release_and_exact_cap_refuse_more_time(self):
        old = Notebook("a" * 12, "Watch and report")
        old.phone.update(first_action_at=2, released_at=None, waited_s=30)
        resumed = pickup(self, old.run_id, "b" * 12, old.to_dict(),
                         {"step": 4, "longform": {"phoneSeconds": 235}}, cap=240)
        self.assertEqual(resumed.phone_seconds(), 235)
        tick = [resumed.started]
        resumed.clock = lambda: tick[0]
        resumed.mark_action()
        tick[0] += 7
        resumed.add_wait(2)
        self.assertEqual(resumed.phone_seconds(), 240)
        self.assertEqual(resumed.covered(), "phone_time")
        self.assertEqual(resumed.final_reason(), "phone_time")
        self.assertEqual(resumed.budget_left()["phone_seconds"], 0)

    def test_malformed_totals_exhaust_budget_instead_of_resetting_elapsed(self):
        old = Notebook("a" * 12, "Watch and report")
        old.phone["cumulative_seconds"] = float("nan")
        damaged_book = pickup(self, old.run_id, "b" * 12, old.to_dict(),
                              {"step": 1, "longform": {"phoneSeconds": 100}})
        self.assertEqual(damaged_book.final_reason(), "phone_time")
        old.phone["cumulative_seconds"] = 100
        damaged_checkpoint = pickup(self, old.run_id, "c" * 12, old.to_dict(),
                                    {"step": 1, "longform": {"phoneSeconds": float("inf")}})
        self.assertEqual(damaged_checkpoint.final_reason(), "phone_time")
        damaged_shape = pickup(self, old.run_id, "d" * 12, old.to_dict(), {"step": 1, "longform": []})
        self.assertEqual(damaged_shape.final_reason(), "phone_time")
        old.phone["cumulative_seconds"] = -1
        damaged_negative = pickup(self, old.run_id, "e" * 12, old.to_dict(), {"step": 1})
        self.assertEqual(damaged_negative.final_reason(), "phone_time")

    def test_legacy_segment_falls_back_only_when_no_cumulative_total_exists(self):
        old = Notebook("a" * 12, "Watch and report")
        old.phone.update(first_action_at=1, released_at=61, waited_s=10)
        restored = pickup(self, old.run_id, "b" * 12, old.to_dict(), {"step": 1})
        self.assertEqual(restored.phone_seconds(), 50)
        self.assertEqual(restored.notebook.phone["cumulative_seconds"], 50)
        without_action = pickup(self, restored.run_id, "c" * 12, restored.notebook.to_dict(),
                                {"step": 1, "longform": {"phoneSeconds": 50}})
        self.assertEqual(without_action.phone_seconds(), 50)
        old.phone.update(first_action_at=1, released_at=None, waited_s=0)
        interrupted_legacy = pickup(self, old.run_id, "d" * 12, old.to_dict(), {"step": 1})
        self.assertEqual(interrupted_legacy.final_reason(), "phone_time")


if __name__ == "__main__":
    unittest.main()
