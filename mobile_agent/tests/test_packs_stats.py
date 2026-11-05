import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from mobile_agent.accounts import stats
from mobile_agent.api_errors import APIError
from mobile_agent.packs import insights, register
from mobile_agent.packs import instagram as ig
from mobile_agent.packs import tiktok as tt
from mobile_agent.tests import test_packs_instagram as igt
from mobile_agent.tests import test_packs_tiktok as ttt
from mobile_agent.tests.packs_support import el, rows_file, snap

DAY = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)


def ig_phone(start="feed", own=True):
    device = igt.phone(start)
    device.screens["profile-own"] = rows_file("instagram/profile-own.json", ig.BUNDLE)
    device.screens["insights"] = rows_file("instagram/insights.json", ig.BUNDLE)
    for here in ("feed", "reel", "explore", "dm-inbox", "profile", "profile-own", "insights"):
        device.moves[(here, "TAP", "Profile")] = "profile-own" if own else "profile"
    device.moves[("profile-own", "TAP", "Professional dashboard")] = "insights"
    return device


def tt_phone(start="for-you"):
    device = ttt.phone(start)
    own = device.screens["profile-own"]
    device.screens["profile-own"] = snap(tt.BUNDLE,
                                         [el(e.label, e.role, e.rect, **({"selected": True} if e.selected else {}))
                                          for e in own.elements]
                                         + [el("Profile menu", "Button", (.9, .06, .08, .04))])
    device.screens["menu"] = snap(tt.BUNDLE, [el("TikTok Studio", "Button", (.1, .3, .8, .05))])
    device.screens["studio"] = snap(tt.BUNDLE, [el("Analytics", "Button", (.1, .3, .8, .05))])
    device.screens["insights"] = rows_file("tiktok/insights.json", tt.BUNDLE)
    device.moves.update({("profile-own", "TAP", "Profile menu"): "menu", ("menu", "TAP", "TikTok Studio"): "studio",
                         ("studio", "TAP", "Analytics"): "insights"})
    return device


class ReaderTests(unittest.TestCase):
    def test_instagram_gives_the_overview_numbers_and_the_period(self):
        screen = rows_file("instagram/insights.json", ig.BUNDLE)
        self.assertEqual(ig.PACK.recognize(screen).screen, "insights")
        row = insights.parse(screen)
        self.assertEqual(row, {"period": "Last 30 days", "views": 12400, "reach": 3050, "profile_visits": 418,
                               "follower_change": 37})

    def test_tiktok_gives_a_negative_change_and_none_for_reach(self):
        screen = rows_file("tiktok/insights.json", tt.BUNDLE)
        self.assertEqual(tt.PACK.recognize(screen).screen, "insights")
        row = insights.parse(screen)
        self.assertEqual((row["period"], row["views"], row["profile_visits"], row["follower_change"]),
                         ("Last 7 days", 8900, 260, -4))
        self.assertIsNone(row["reach"])

    def test_a_screen_with_no_numbers_says_so(self):
        result = insights.reader(ig.PACK, None, snap(ig.BUNDLE, [el("Professional dashboard", "StaticText")]), 1, None)
        self.assertIn("aren't in the screen's tree", result.stopped)

    def test_instagram_reads_insights_from_the_profile_tab(self):
        device = ig_phone()
        result = ig.PACK.read(igt.io_for(device), "insights", 1, None)
        self.assertEqual(device.taps, ["Profile", "Profile", "Professional dashboard"])
        self.assertEqual((result.rows[0]["handle"], result.rows[0]["views"]), ("me.test", 12400))
        self.assertEqual([c[0] for c in result.columns], [c[0] for c in insights.COLUMNS])

    def test_tiktok_reads_insights_through_studio(self):
        device = tt_phone()
        result = tt.PACK.read(ttt.io_for(device), "insights", 1, None)
        self.assertEqual(device.taps, ["Profile", "Profile", "Profile menu", "TikTok Studio", "Analytics"])
        self.assertEqual((result.rows[0]["handle"], result.rows[0]["views"]), ("me.test", 8900))

    def test_profile_numbers_cover_followers_following_posts_and_likes(self):
        row = ig.read_profile(ig.PACK, None, rows_file("instagram/profile-own.json", ig.BUNDLE), 1, None).rows[0]
        self.assertEqual((row["followers"], row["following"], row["posts"], row["state"]), (1204, 310, 12, "yours"))
        row = tt.read_profile(tt.PACK, None, rows_file("tiktok/profile-own.json", tt.BUNDLE), 1, None).rows[0]
        self.assertEqual((row["followers"], row["following"], row["likes"]), (96, 180, 1024))


class StoreTests(unittest.TestCase):
    def test_a_day_is_one_row_that_later_reads_fill_in(self):
        with tempfile.TemporaryDirectory() as folder:
            stats.record(folder, "instagram", "@Me.Test", {"followers": 10, "views": None}, DAY)
            stats.record(folder, "instagram", "me.test", {"views": 5}, DAY)
            out = stats.series(folder, "instagram", "me.test", 30, DAY)
        self.assertEqual(len(out["snapshots"]), 1)
        self.assertEqual((out["latest"]["followers"], out["latest"]["views"], out["latest"]["likes"]), (10, 5, None))

    def test_days_limits_the_window_and_an_unknown_account_is_empty(self):
        with tempfile.TemporaryDirectory() as folder:
            stats.record(folder, "tiktok", "me.test", {"followers": 1}, datetime(2026, 9, 1, tzinfo=timezone.utc))
            stats.record(folder, "tiktok", "me.test", {"followers": 2}, DAY)
            self.assertEqual([s["followers"] for s in stats.series(folder, "tiktok", "me.test", 7, DAY)["snapshots"]],
                             [2])
            self.assertEqual(stats.series(folder, "tiktok", "nobody", 30, DAY)["latest"], None)

    def test_the_route_reads_stored_rows_and_refuses_what_it_cannot_serve(self):
        from mobile_agent.accounts import routes
        with tempfile.TemporaryDirectory() as folder:
            stats.record(folder, "instagram", "me.test", {"followers": 10})
            store = SimpleNamespace(data_dir=Path(folder))
            runtime = SimpleNamespace()
            from unittest.mock import patch
            with patch.object(routes, "store_for", lambda r: store):
                def call(app, days="30"):
                    return routes.get_stats(SimpleNamespace(runtime=runtime, query={"days": [days]},
                                                            params={"app": app, "handle": "me.test"}))
                body = call("instagram").value
                with self.assertRaises(APIError) as raised:
                    call("myspace")
                self.assertEqual((raised.exception.status, raised.exception.code), (404, "stats_unknown_app"))
                with self.assertRaises(APIError) as raised:
                    call("instagram", "0")
                self.assertEqual(raised.exception.status, 400)
        self.assertEqual(body["latest"]["followers"], 10)


class PreRunTests(unittest.TestCase):
    def run_stats(self, key, device, account=None):
        folder = tempfile.mkdtemp(prefix="mobster-stats-")
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))
        ctx = SimpleNamespace(extras={"packStats": key, **({"account": account} if account else {})},
                              cancelled=lambda: False, emit=lambda e: None, runtime=None, run_id="stats",
                              data_dir=Path(folder))
        from unittest.mock import patch
        with patch.object(register, "SLEEP", lambda s: None):
            done = register.pack_stats(ctx, device, None)
        return done.result, stats.series(folder, key, "me.test", 30)

    def test_it_stores_the_profile_and_insights_for_the_account_on_screen(self):
        result, out = self.run_stats("instagram", ig_phone(), {"id": "a1", "handle": "@me.test"})
        self.assertEqual(result["costUsd"], 0.0)
        latest = out["latest"]
        self.assertEqual((latest["followers"], latest["posts"], latest["views"], latest["follower_change"]),
                         (1204, 12, 12400, 37))

    def test_it_refuses_to_store_for_a_different_handle_or_a_profile_not_yours(self):
        for device, account, said in ((ig_phone(), {"id": "a1", "handle": "@someone.else"}, "not @someone.else"),
                                      (ig_phone(own=False), None, "doesn't show your own profile")):
            with self.subTest(said=said):
                result, out = self.run_stats("instagram", device, account)
                self.assertIsNone(out["latest"])
                self.assertIn(said, result["answer"])

    def test_it_stores_tiktok_numbers_and_taps_no_act(self):
        device = tt_phone()
        result, out = self.run_stats("tiktok", device)
        self.assertEqual((out["latest"]["followers"], out["latest"]["likes"], out["latest"]["views"]),
                         (96, 1024, 8900))
        for label in device.taps:
            self.assertNotRegex(label, r"^(Like|Follow|Send|Post)")

    def test_the_run_is_journaled_as_acting_before_the_first_tap(self):
        from unittest.mock import patch

        from mobile_agent import server
        log, device = [], ig_phone()
        execute = device.execute
        device.execute = lambda *a, **k: (log.append(("execute", a[0])), execute(*a, **k))[1]
        ctx = SimpleNamespace(extras={"packStats": "instagram"}, cancelled=lambda: False, runtime=None,
                              emit=lambda e: log.append(("emit", e)), run_id="stats", data_dir=Path(tempfile.mkdtemp()))
        self.addCleanup(lambda: __import__("shutil").rmtree(ctx.data_dir, ignore_errors=True))
        with patch.object(register, "SLEEP", lambda s: None):
            register.pack_stats(ctx, device, None)
        self.assertEqual(log[0][0], "emit")
        self.assertEqual(log[0][1], {"event": "skill_started", "op": "IG_STATS"})
        self.assertTrue(server.may_have_acted(log[0][1]))
        self.assertIn("execute", [kind for kind, _ in log])

    def test_a_run_without_the_field_or_a_phone_emits_and_taps_nothing(self):
        events, device = [], ig_phone()
        for extras, phone in (({}, device), ({"packStats": "instagram"}, None)):
            ctx = SimpleNamespace(extras=extras, cancelled=lambda: False, emit=events.append, runtime=None,
                                  run_id="stats", data_dir=Path(tempfile.gettempdir()))
            self.assertIsNone(register.pack_stats(ctx, phone, None))
        self.assertEqual((events, device.actions), ([], []))

    def test_taps_alone_do_not_count_as_acting(self):
        from mobile_agent import server
        self.assertFalse(server.may_have_acted({"event": "app_pack_stats", "pack": "instagram"}))

    def test_the_run_field_takes_a_pack_key_only(self):
        self.assertEqual(register.validate_pack_key("packStats", "tiktok", None), "tiktok")
        with self.assertRaisesRegex(ValueError, "^packStats is the key"):
            register.validate_pack_key("packStats", "myspace", None)

