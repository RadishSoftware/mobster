
import datetime
import tempfile
import unittest
from zoneinfo import ZoneInfo

from mobile_agent.accounts.pacing import DEFAULTS, Pacing, PacingError, clean, kind_of, looks_challenged


class Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at


def stamp(text, zone="America/New_York"):
    return datetime.datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(zone)).timestamp()


class PacingTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.clock = Clock(stamp("2026-10-14T10:00"))
        self.pacing = Pacing(folder.name, clock=self.clock)

    def test_the_defaults_are_under_the_pace_people_call_safe(self):
        self.assertLessEqual(DEFAULTS["message"]["daily"], 20)
        self.assertGreaterEqual(DEFAULTS["message"]["gapMinutes"], 4)
        self.assertEqual(kind_of("send_message"), "message")
        self.assertEqual(kind_of("publish"), "post")
        self.assertIsNone(kind_of("pay"))

    def test_a_gap_waits_and_a_full_day_stops_until_midnight_in_the_profiles_zone(self):
        self.pacing.save("p1", {"timeZone": "America/New_York", "limits": {"message": {"daily": 2, "gapMinutes": 4}}})
        self.assertIsNone(self.pacing.check("p1", "send_message"))
        self.pacing.record("p1", "send_message")
        wait = self.pacing.check("p1", "reply")
        self.assertAlmostEqual(wait["wait"], 240, delta=1)
        self.clock.at += 300
        self.assertIsNone(self.pacing.check("p1", "send_message"))
        self.pacing.record("p1", "send_message")
        self.clock.at += 300
        stop = self.pacing.check("p1", "send_message")
        self.assertEqual(stop["code"], "pacing_daily")
        self.assertEqual(stop["until"], stamp("2026-10-15T00:00"))
        self.assertIsNone(self.pacing.check("p1", "comment"))
        self.clock.at = stamp("2026-10-15T00:01")
        self.assertIsNone(self.pacing.check("p1", "send_message"))

    def test_posts_go_inside_the_window_in_the_profiles_zone(self):
        self.pacing.save("p1", {"timeZone": "Asia/Tokyo", "window": {"start": "09:00", "end": "21:00"}})
        self.clock.at = stamp("2026-10-14T23:30", "Asia/Tokyo")
        self.assertEqual(self.pacing.next_post_time("p1"), stamp("2026-10-15T09:00", "Asia/Tokyo"))
        self.clock.at = stamp("2026-10-14T12:00", "Asia/Tokyo")
        self.assertEqual(self.pacing.next_post_time("p1"), self.clock.at)
        self.pacing.record("p1", "post")
        self.assertEqual(self.pacing.next_post_time("p1"), self.clock.at + 3600)

    def test_the_ledger_drops_what_is_older_than_the_days_it_keeps(self):
        from unittest import mock
        self.pacing.save("p1", {"limits": {"comment": {"daily": 50, "gapMinutes": 0}}})
        with mock.patch("mobile_agent.accounts.pacing.COMPACT_BYTES", 1):
            for _ in range(6):
                self.pacing.record("p1", "comment")
                self.clock.at += 86400
        lines = self.pacing.ledger_path.read_text().splitlines()
        self.assertEqual(len(lines), 4, "KEEP_DAYS days back from the last record, and that record")
        self.assertEqual(self.pacing.today("p1")["comment"], 0)
        self.pacing.record("p1", "comment")
        self.assertEqual(self.pacing.today("p1")["comment"], 1)
        self.assertIsNone(self.pacing.check("p1", "comment"))

    def test_failed_compaction_keeps_recorded_actions_and_removes_its_temporary_file(self):
        import os
        from contextlib import contextmanager
        from unittest import mock
        fdopen = os.fdopen

        @contextmanager
        def unwritable(fd, *args, **kwargs):
            with fdopen(fd, *args, **kwargs) as stream:
                with mock.patch.object(stream, "write", side_effect=OSError("write failed")):
                    yield stream

        for failure in ("write", "replace"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as folder:
                pacing = Pacing(folder, clock=self.clock)
                pacing.record("p1", "post")
                target, error = (("os.fdopen", unwritable) if failure == "write" else
                                 ("os.replace", OSError("replace failed")))
                with mock.patch("mobile_agent.accounts.pacing.COMPACT_BYTES", 1), \
                        mock.patch("mobile_agent.accounts.pacing." + target, side_effect=error):
                    pacing.record("p1", "post")
                self.assertEqual(pacing.today("p1")["post"], 2)
                self.assertEqual(list(pacing.data_dir.glob(".pacing.*")), [])

    def test_a_paused_profile_does_nothing_until_resumed(self):
        self.pacing.pause("p1", "Instagram asked for a selfie check.")
        self.assertEqual(self.pacing.check("p1", "like")["code"], "profile_paused")
        self.assertIsNone(self.pacing.next_post_time("p1"))
        self.pacing.resume("p1")
        self.assertIsNone(self.pacing.check("p1", "like"))

    def test_settings_are_checked(self):
        for bad in ({"timeZone": "Mars/Base"}, {"window": {"start": "9", "end": "21:00"}}, {"limits": {"dm": {}}},
                    {"limits": {"post": {"daily": -1}}}, {"other": 1}, {"window": {}}, {"window": []},
                    {"limits": []}, {"limits": None}):
            with self.subTest(bad=bad), self.assertRaises(PacingError):
                clean(bad)
        self.assertEqual(clean({"window": None, "limits": {}}), {"timeZone": None, "window": None, "limits": {}})

    def test_challenge_words(self):
        self.assertTrue(looks_challenged("Instagram says: We suspect automated behavior on your account"))
        self.assertTrue(looks_challenged(None, "TikTok showed a captcha"))
        self.assertFalse(looks_challenged("Posted the Reel."))


class WhereItAppliesTests(unittest.TestCase):
    ACCOUNT = {"id": "aaaaaaaaaaa1", "handle": "getmobster", "app": {"bundleId": "com.burbn.instagram",
        "name": "Instagram"}}

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.clock = Clock(stamp("2026-10-14T10:00"))
        self.pacing = Pacing(folder.name, clock=self.clock)

    def check(self):
        from mobile_agent.accounts.screen import AccountCheck
        events = []
        check = AccountCheck(self.ACCOUNT, emit=events.append)
        check.state = "confirmed"
        check.pacing = self.pacing
        snapshot = type("Snap", (), {"bundle_id": "com.burbn.instagram"})()
        return check, snapshot, events

    def test_a_confirmed_profile_acts_within_its_pace_and_stops_at_its_limit(self):
        check, snapshot, events = self.check()
        self.assertFalse(self.pacing.ledger_path.exists())
        self.assertEqual(check.commit("send_message", snapshot), {"acting_as": "@getmobster on Instagram"})
        self.pacing.save("aaaaaaaaaaa1", {"limits": {"message": {"daily": 1, "gapMinutes": 0}}})
        self.pacing.record("aaaaaaaaaaa1", "send_message")
        verdict = check.commit("send_message", snapshot)
        self.assertEqual(verdict["code"], "pacing_daily")
        self.assertIn("@getmobster", verdict["stop"])
        self.pacing.pause("aaaaaaaaaaa1", "TikTok showed a captcha.")
        self.assertEqual(check.commit("like", snapshot)["code"], "profile_paused")

    def test_unreadable_pacing_stops_the_task_before_the_action(self):
        from pathlib import Path
        from unittest import mock
        check, snapshot, events = self.check()
        self.pacing.save("aaaaaaaaaaa1", {"limits": {"message": {"daily": 1, "gapMinutes": 0}}})
        self.pacing.record("aaaaaaaaaaa1", "send_message")
        self.clock.at += 3600
        self.assertEqual(check.commit("send_message", snapshot)["code"], "pacing_daily")
        read = Path.read_text

        def unreadable(path, *args, **kwargs):
            if path == broken:
                raise OSError("pacing state is unreadable")
            return read(path, *args, **kwargs)

        for broken in (self.pacing.ledger_path, self.pacing.settings_path):
            with self.subTest(file=broken.name), mock.patch.object(Path, "read_text", unreadable):
                verdict = check.commit("send_message", snapshot)
                self.assertEqual(verdict.get("code"), "pacing_unreadable", verdict)
                self.assertIn("stopped before acting", verdict["stop"])
                self.assertNotIn("acting_as", verdict)
                with self.assertRaises(OSError):
                    self.pacing.next_post_time("aaaaaaaaaaa1")

    def test_corrupt_settings_cannot_unpause_a_profile(self):
        check, snapshot, _ = self.check()
        for data in ("{", "[]", '{"profiles":[]}', '{"profiles":{"aaaaaaaaaaa1":[]}}',
                     '{"profiles":{"aaaaaaaaaaa1":{"paused":false}}}',
                     '{"profiles":{"aaaaaaaaaaa1":{"limits":[]}}}',
                     '{"profiles":{"aaaaaaaaaaa1":{"window":[]}}}',
                     '{"profiles":{"aaaaaaaaaaa1":{"window":{}}}}',
                     '{"profiles":{"aaaaaaaaaaa1":{"limits":{"post":{"daily":-1}}}}}'):
            with self.subTest(data=data):
                self.pacing.settings_path.unlink(missing_ok=True)
                self.pacing.pause("aaaaaaaaaaa1", "a challenge")
                self.assertEqual(check.commit("post", snapshot)["code"], "profile_paused")
                self.pacing.settings_path.write_text(data)
                verdict = check.commit("post", snapshot)
                self.assertEqual(verdict.get("code"), "pacing_unreadable", verdict)
                with self.assertRaises(ValueError):
                    self.pacing.next_post_time("aaaaaaaaaaa1")

    def test_corrupt_ledger_refuses_and_compaction_preserves_the_evidence(self):
        from unittest import mock
        check, snapshot, _ = self.check()
        for data in ("not json\n", '{}\n', '{"at":NaN,"profile":"aaaaaaaaaaa1","kind":"post"}\n'):
            with self.subTest(data=data):
                self.pacing.ledger_path.write_text(data)
                verdict = check.commit("post", snapshot)
                self.assertEqual(verdict.get("code"), "pacing_unreadable", verdict)
                with self.assertRaises(ValueError):
                    self.pacing.next_post_time("aaaaaaaaaaa1")
                with mock.patch("mobile_agent.accounts.pacing.COMPACT_BYTES", 1):
                    self.pacing.record("aaaaaaaaaaa1", "post")
                self.assertTrue(self.pacing.ledger_path.read_text().startswith(data))
                self.assertEqual(list(self.pacing.data_dir.glob(".pacing.*")), [])

    def test_a_short_gap_is_waited_out_and_a_long_one_stops(self):
        check, snapshot, events = self.check()
        self.pacing.save("aaaaaaaaaaa1", {"limits": {"comment": {"daily": 50, "gapMinutes": 0.02}}})
        self.pacing.record("aaaaaaaaaaa1", "comment")
        self.assertEqual(check.commit("comment", snapshot), {"acting_as": "@getmobster on Instagram"})
        self.assertEqual(events[-1]["event"], "pacing_wait")
        self.pacing.save("aaaaaaaaaaa1", {"limits": {"comment": {"daily": 50, "gapMinutes": 60}}})
        stop = check.commit("comment", snapshot)
        self.assertEqual(stop["code"], "pacing_gap")
        self.assertGreater(stop["until"], self.clock())
        self.assertEqual(events[-1], {"event": "pacing_stop", "account": "aaaaaaaaaaa1", "code": "pacing_gap",
                                      "until": stop["until"]})
        check.pacing = None
        self.assertEqual(check.commit("comment", snapshot), {"acting_as": "@getmobster on Instagram"})

    def test_the_listener_counts_commits_and_pauses_a_challenged_profile(self):
        from unittest import mock

        from mobile_agent.accounts import register
        listener = register.Listener()
        run = type("Run", (), {"id": "r1", "extras": {"account": {**self.ACCOUNT}}})()
        with mock.patch.object(register, "_pacing", return_value=self.pacing), \
                mock.patch.object(register.alerts.Alerts, "challenge") as alert:
            listener.on_step(run, type("Step", (), {"commit": "send_message"})())
            listener.on_step(run, type("Step", (), {"commit": None})())
            self.assertEqual(self.pacing.today("aaaaaaaaaaa1")["message"], 1)
            listener.on_finished(run, {"status": "blocked", "reason": "Instagram says it suspects automated behavior."},
                                 {})
        self.assertTrue(self.pacing.settings("aaaaaaaaaaa1")["paused"])
        alert.assert_called_once()


if __name__ == "__main__":
    unittest.main()
