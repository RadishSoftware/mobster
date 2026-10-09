"""The frozen seams between the loop, the phone guard and skills (agent_hooks.py)."""

import dataclasses
import unittest

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
    def test_the_frozen_shapes(self):
        verdict = H.GuardVerdict("stop", "phone_locked", "Your iPhone is locked.")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            verdict.code = "x"
        self.assertEqual(H.GuardVerdict("ready"), H.READY)
        self.assertEqual(H.Prepared("Use it?").state, None)
        result = H.SkillResult("Entered the code.")
        self.assertEqual((result.changed, result.secrets, result.secret_rects, result.stop), (False, (), (), None))
        context = H.SkillContext(driver=None, request="r", app_bundle=None, emit=lambda e: None)
        self.assertIsNone(context.guard)
        self.assertEqual([f.name for f in dataclasses.fields(H.SkillContext)],
                         ["driver", "request", "app_bundle", "emit", "guard", "approve", "clarify", "run"])
        self.assertIn("blocked_app", H.STOP_CODES)
        self.assertEqual(H.GUARD_CAUSES, ("preflight", "launch_failed", "lock_suspected", "resume", "sheet_suspected"))

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
        self.assertEqual(H.safe_check(Guard(verdict="nonsense"), None, "resume"), H.READY)
        guard = Guard(verdict=H.GuardVerdict("unlocked"))
        self.assertEqual(H.safe_check(guard, None, "launch_failed").state, "unlocked")
        self.assertEqual(guard.causes, ["launch_failed"])

    def test_attached_and_allows_fail_open_only_where_harmless(self):
        self.assertIsNone(H.safe_attached(None))
        self.assertIs(H.safe_attached(Guard(attached=False)), False)
        self.assertIsNone(H.safe_attached(Guard(attached=OSError())))
        self.assertTrue(H.safe_allows(Guard(), "com.apple.MobileSMS"))  # no C7 list
        guard = Guard()
        guard.allows_app = lambda bundle: bundle != "com.bank.app"
        self.assertFalse(H.safe_allows(guard, "com.bank.app"))
        self.assertTrue(H.safe_allows(guard, "com.apple.Preferences"))


class NarrationTests(unittest.TestCase):
    def test_guard_and_skill_events_read_as_one_plain_line_without_digits(self):
        from mobile_agent.narrate import Narrator
        narrator = Narrator()
        lines = []
        for event in ({"event": "unlock_finished", "ok": True, "code": ""},
                      {"event": "unlock_finished", "ok": False, "code": "passcode_failed"},
                      {"event": "handoff_waiting", "kind": "face_id", "seconds": 60},
                      {"event": "skill_finished", "op": "USE_CODE", "ok": True},
                      {"event": "skill_finished", "op": "USE_CODE", "ok": False}):
            lines += [item.text for item in narrator.feed(event)]
        self.assertEqual(lines, ["Unlocked the iPhone", "The saved passcode didn't unlock the iPhone",
                                 "Waiting for you: approve Face ID on your iPhone", "Entered the verification code"])
        self.assertFalse(any(ch.isdigit() for line in lines for ch in line))


if __name__ == "__main__":
    unittest.main()
