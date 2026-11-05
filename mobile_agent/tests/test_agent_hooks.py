"""The frozen seams between the loop, the phone guard and skills (agent_hooks.py)."""

import unittest
from unittest.mock import Mock

from mobile_agent import agent_hooks as H


class Guard:
    def __init__(self, verdict=None, error=None, attached=None, allows=None):
        self.verdict, self.error, self._attached, self._allows = verdict, error, attached, allows
        self.causes = []

    def check(self, driver, *, cause):
        self.causes.append(cause)
        if self.error:
            raise self.error
        return self.verdict

    def attached(self):
        if isinstance(self._attached, Exception):
            raise self._attached
        return self._attached

    def finish(self, driver):
        pass


class AgentHooksTests(unittest.TestCase):
    def test_every_stop_code_has_its_sentence_and_no_sentence_is_orphaned(self):
        from mobile_agent import alerts
        self.assertEqual(set(alerts.CODES), set(H.STOP_CODES))

    def test_mask_replaces_every_secret_longest_first(self):
        self.assertEqual(H.mask("Your code is 482913 (482913)", ("482913",)), f"Your code is {H.MASK} ({H.MASK})")
        self.assertEqual(H.mask("ab 1234567", ("1234", "1234567")), f"ab {H.MASK}")
        self.assertEqual(H.mask(None, ("1",)), None)
        self.assertEqual(H.mask("plain", ()), "plain")

    def test_a_guard_that_raises_is_a_stop_and_none_without_a_guard(self):
        self.assertIsNone(H.safe_check(None, None, "resume"))
        verdict = H.safe_check(Guard(error=RuntimeError("boom 468213")), None, "resume")
        self.assertEqual((verdict.state, verdict.code), ("stop", "phone_locked"))
        self.assertNotIn("468213", verdict.message)
        for invalid in ("nonsense", None, H.GuardVerdict("unknown")):
            self.assertEqual(H.safe_check(Guard(verdict=invalid), None, "resume").state, "stop")
        guard = Guard(verdict=H.GuardVerdict("unlocked"))
        self.assertEqual(H.safe_check(guard, None, "launch_failed").state, "unlocked")
        self.assertEqual(guard.causes, ["launch_failed"])

    def test_optional_hooks_stay_optional_and_an_explicit_app_refusal_is_enforced(self):
        self.assertIsNone(H.safe_attached(None))
        self.assertIs(H.safe_attached(Guard(attached=False)), False)
        self.assertIsNone(H.safe_attached(Guard(attached=OSError())))
        self.assertTrue(H.safe_allows(Guard(), "com.apple.MobileSMS"))  # no C7 list
        guard = Guard()
        guard.allows_app = lambda bundle: bundle != "com.bank.app"
        self.assertFalse(H.safe_allows(guard, "com.bank.app"))
        self.assertTrue(H.safe_allows(guard, "com.apple.Preferences"))

    def test_an_app_allowlist_error_or_malformed_answer_fails_closed(self):
        guard = Guard()
        guard.allows_app = Mock(side_effect=RuntimeError("unavailable"))
        self.assertFalse(H.safe_allows(guard, "com.apple.Preferences"))
        for invalid in (None, "true", 1, []):
            guard.allows_app = lambda bundle: invalid
            self.assertFalse(H.safe_allows(guard, "com.apple.Preferences"))


if __name__ == "__main__":
    unittest.main()
