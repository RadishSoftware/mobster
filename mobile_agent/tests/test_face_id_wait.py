import unittest
from unittest.mock import patch

from mobile_agent import lockscreen
from mobile_agent.agent_hooks import READY
from mobile_agent.tests.test_lockscreen import FIXTURES, FakeWda
from mobile_agent.tests.test_lockscreen_wait import waiting_guard


class FaceIdWaitTests(unittest.TestCase):
    def test_owner_clears_sheet_and_the_same_run_can_continue(self):
        phone = FakeWda(source=FIXTURES["face_id"])
        guard, events, clock = waiting_guard(wait_s=3)
        def sleep(seconds):
            clock.sleep(seconds)
            phone.source = FIXTURES["home"]
        guard.sleep = sleep
        self.assertEqual(guard.check(phone, cause="sheet_suspected"), READY)
        self.assertEqual(events[0]["kind"], "face_id")
        self.assertEqual(events[-1]["waited_s"], 1)
        self.assertTrue(events[-1]["ok"])
        self.assertEqual(phone.writes(), [])

    def test_timeout_cancel_unreadable_and_another_sheet_never_continue(self):
        for outcome in ("timeout", "cancel", "unreadable", "locked", "app_store", "app_locked"):
            with self.subTest(outcome=outcome):
                phone = FakeWda(source=FIXTURES["face_id"])
                guard, events, clock = waiting_guard(wait_s=3)
                def sleep(seconds):
                    clock.sleep(seconds)
                    if outcome == "cancel":
                        guard.cancelled = lambda: True
                    elif outcome == "unreadable":
                        phone.source_error = OSError("unreadable")
                    elif outcome == "locked":
                        phone.locked = True
                    elif outcome in FIXTURES:
                        phone.source = FIXTURES[outcome]
                guard.sleep = sleep
                self.assertEqual(guard.check(phone, cause="resume").state, "stop")
                self.assertFalse(events[-1]["ok"])
                self.assertLessEqual(clock.now - 1000, 3)
                self.assertEqual(phone.writes(), [])

    def test_unattended_runs_still_stop_without_waiting(self):
        phone = FakeWda(source=FIXTURES["face_id"])
        guard, events, clock = waiting_guard(wait_s=0)
        self.assertEqual(guard.check(phone, cause="resume").code, "face_id")
        self.assertEqual(events, [])
        self.assertEqual(clock.slept, [])

    def test_stop_during_the_final_sheet_read_never_resumes(self):
        phone = FakeWda(source=FIXTURES["face_id"])
        guard, events, clock = waiting_guard(wait_s=3)
        read_sheet = lockscreen.read_springboard

        def read(driver):
            labels = read_sheet(driver)
            if clock.slept:
                guard.cancelled = lambda: True
            return labels

        def sleep(seconds):
            clock.sleep(seconds)
            phone.source = FIXTURES["home"]

        guard.sleep = sleep
        with patch.object(lockscreen, "read_springboard", read):
            self.assertEqual(guard.check(phone, cause="resume").state, "stop")
        self.assertFalse(events[-1]["ok"])
        self.assertEqual(phone.writes(), [])
