import unittest

from mobile_agent.tests import test_server_hardening as hardening


class RestartRecoveryTests(unittest.TestCase):
    setUp, tearDown = hardening.ServerHardeningTests.setUp, hardening.ServerHardeningTests.tearDown
    runtime = hardening.ServerHardeningTests.runtime

    def restored(self, key, *events):
        before = self.runtime(persisted=True)
        run, _ = before.create("settings", "Open About", "live", key)
        for event in events:
            run.emit(event)
        run.lease.close()
        run.lease = None
        before.close()
        return self.runtime(persisted=True).runs[run.id]

    DECISION = {"event": "frontier_decision", "step": 0, "operation": "TAP", "ops": ["TAP"], "target_label": "About"}

    def test_a_run_that_journaled_a_frontier_action_may_have_acted(self):
        restored = self.restored("restart-key", self.DECISION, {"event": "frontier_action", "step": 0, "index": 0,
                                                                "operation": "TAP", "target_label": "About"})
        self.assertEqual(restored.status, "interrupted")
        self.assertEqual((restored.summary["actions_may_have_run"], restored.summary["actions"]), (True, 1))

    def test_a_decision_journaled_before_its_action_was_sent_may_have_acted(self):
        self.assertTrue(self.restored("restart-decided", self.DECISION).summary["actions_may_have_run"])

    def test_a_run_that_only_decided_to_finish_took_no_action(self):
        restored = self.restored("restart-done", {"event": "frontier_decision", "step": 0, "operation": "DONE",
                                                  "ops": ["DONE"]})
        self.assertEqual(restored.status, "interrupted")
        self.assertEqual((restored.summary["actions_may_have_run"], restored.summary["actions"]), (False, 0))


if __name__ == "__main__":
    unittest.main()
