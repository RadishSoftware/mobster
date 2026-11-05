import unittest

from mobile_agent.task_policy import RiskTier, action_risk_tier


class RiskTierTests(unittest.TestCase):
    def test_named_risk_tiers_follow_goal_and_control_language(self):
        for op, label, goal, tier in (("TAP", "Search", "Open Search", RiskTier.NAVIGATION),
                                      ("TAP", "Search", "Send a message to Sam", RiskTier.SIDE_EFFECT),
                                      ("TAP", "Buy now", "Open Search", RiskTier.SIDE_EFFECT),
                                      ("TYPE", "Search", "Open Search", RiskTier.TYPE),
                                      ("SWIPE_UP", "", "Read the bio", RiskTier.SCROLL),
                                      ("SWIPE_DOWN", "", "Delete the bio", RiskTier.SCROLL),
                                      ("SWIPE_LEFT", "", "Read the bio", RiskTier.NAVIGATION),
                                      ("BACK", "Back", "Read the bio", RiskTier.NAVIGATION),
                                      ("HOME", "", "Read the bio", RiskTier.NAVIGATION),
                                      ("VOLUME_UP", "", "Read the bio", RiskTier.NAVIGATION)):
            self.assertIs(action_risk_tier(op, label, goal), tier, (op, label, goal))


if __name__ == "__main__":
    unittest.main()
