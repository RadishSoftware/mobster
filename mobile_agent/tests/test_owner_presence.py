import unittest

from mobile_agent import owner_presence as op
from mobile_agent.owner_presence import OwnerPresence, Poller

HOME, SAFARI, NOTES = "com.apple.springboard", "com.apple.mobilesafari", "com.apple.mobilenotes"


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


def make():
    clock = Clock()
    return OwnerPresence(clock=clock), clock


class States(unittest.TestCase):
    def test_a_first_read_is_unknown_until_it_has_held_still(self):
        presence, clock = make()
        self.assertEqual(presence.observe(locked=True, front=None), "unknown")
        clock.advance(op.IDLE_AFTER_S - 1)
        self.assertEqual(presence.observe(locked=True, front=None), "unknown")
        clock.advance(2)
        self.assertEqual(presence.observe(locked=True, front=None), "idle")

    def test_an_unlock_with_no_mobster_run_is_the_owner(self):
        presence, clock = make()
        presence.observe(locked=True)
        clock.advance(60)
        self.assertEqual(presence.observe(locked=False, front=HOME), "owner_active")
        self.assertTrue(presence.is_owner_active())

    def test_a_foreground_app_change_is_the_owner(self):
        presence, clock = make()
        presence.observe(locked=False, front=SAFARI)
        clock.advance(20)
        self.assertEqual(presence.observe(locked=False, front=NOTES), "owner_active")

    def test_owner_active_ends_after_a_quiet_stretch(self):
        presence, clock = make()
        presence.observe(locked=True)
        clock.advance(20)
        presence.observe(locked=False, front=HOME)
        clock.advance(op.OWNER_QUIET_S - 1)
        self.assertEqual(presence.observe(locked=False, front=HOME), "owner_active")
        clock.advance(2)
        self.assertEqual(presence.observe(locked=False, front=HOME), "idle")

    def test_a_change_just_after_a_mobster_action_is_mobsters(self):
        presence, clock = make()
        presence.observe(locked=False, front=SAFARI)
        clock.advance(100)
        presence.note_mobster_action()
        clock.advance(op.ACTION_QUIET_S - 5)
        self.assertNotEqual(presence.observe(locked=False, front=NOTES), "owner_active")
        clock.advance(op.ACTION_QUIET_S + 1)
        self.assertEqual(presence.observe(locked=False, front=SAFARI), "owner_active")

    def test_since_is_epoch_milliseconds_of_the_change(self):
        presence, clock = make()
        presence.observe(locked=True)
        clock.advance(10)
        presence.observe(locked=False, front=HOME)
        self.assertEqual(presence.snapshot()["since"], int(clock.now * 1000))


class Release(unittest.TestCase):
    def test_release_fires_once_after_the_idle_timeout(self):
        presence, clock = make()
        presence.run_finished(locked=True)
        self.assertFalse(presence.should_release())
        clock.advance(op.IDLE_RELEASE_S + 1)
        self.assertTrue(presence.should_release())
        self.assertFalse(presence.should_release())

    def test_no_release_while_a_run_holds_or_the_owner_is_active(self):
        presence, clock = make()
        presence.run_started()
        clock.advance(1000)
        self.assertFalse(presence.should_release())
        presence.run_finished(locked=True)
        clock.advance(10)
        presence.observe(locked=False, front=HOME)
        clock.advance(1000)
        presence.observe(locked=False, front=NOTES)
        self.assertFalse(presence.should_release())


class Polling(unittest.TestCase):
    def test_no_read_is_made_while_a_run_holds_the_phone(self):
        presence, _ = make()
        reads = []
        poller = Poller(presence, "http://127.0.0.1:8100", busy=lambda: True,
                        reader=lambda url: reads.append(url) or (True, False, HOME), lease=lambda url: False,
                        monotonic=iter(range(0, 1000, 100)).__next__)
        self.assertEqual(poller.poll()["state"], "mobster")
        self.assertEqual(reads, [])

    def test_a_held_lease_is_never_read_either(self):
        presence, _ = make()
        reads = []
        poller = Poller(presence, "u", reader=lambda url: reads.append(url) or (True, False, HOME),
                        lease=lambda url: True, monotonic=iter(range(0, 1000, 100)).__next__)
        poller.poll()
        self.assertEqual(reads, [])

    def test_a_lease_check_that_raises_counts_as_held(self):
        presence, _ = make()

        def broken(url):
            raise RuntimeError

        poller = Poller(presence, "u", lease=broken, reader=lambda url: self.fail("read while unsure"),
                        monotonic=iter(range(0, 1000, 100)).__next__)
        self.assertEqual(poller.poll()["state"], "mobster")

    def test_polls_are_throttled(self):
        presence, _ = make()
        reads, ticks = [], iter([0.0, 5.0, 16.0])
        poller = Poller(presence, "u", reader=lambda url: reads.append(1) or (True, True, None),
                        lease=lambda url: False, monotonic=lambda: next(ticks))
        poller.poll()
        poller.poll()
        poller.poll()
        self.assertEqual(len(reads), 2)

    def test_an_unanswered_phone_is_unknown(self):
        presence, _ = make()
        poller = Poller(presence, "u", reader=lambda url: (False, None, None), lease=lambda url: False,
                        monotonic=lambda: 0.0)
        self.assertEqual(poller.poll()["state"], "unknown")

    def test_the_default_reader_makes_only_the_two_read_only_requests(self):
        calls = []

        def read_kept(url, path, timeout=None):
            calls.append((url, path, timeout))
            return {"value": False if path == "/wda/locked" else {"bundleId": SAFARI}}

        from unittest.mock import patch
        with patch("mobile_agent.transport.read_kept", read_kept):
            self.assertEqual(op.read_phone("http://127.0.0.1:8100"), (True, False, SAFARI))
        self.assertEqual([p for _, p, _ in calls], ["/wda/locked", "/wda/activeAppInfo"])
        self.assertTrue(all(t == op.READ_TIMEOUT_S for _, _, t in calls))


class Registry(unittest.TestCase):
    def test_lookup_by_wda_address(self):
        presence, clock = make()
        op.register("http://127.0.0.1:8100/", presence)
        self.assertFalse(op.is_owner_active("http://127.0.0.1:8100"))
        presence.observe(locked=True)
        clock.advance(5)
        presence.observe(locked=False, front=HOME)
        self.assertTrue(op.is_owner_active("http://127.0.0.1:8100"))
        op.forget("http://127.0.0.1:8100")
        self.assertFalse(op.is_owner_active("http://127.0.0.1:8100"))


if __name__ == "__main__":
    unittest.main()
