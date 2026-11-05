import re
import unittest
from types import SimpleNamespace

from mobile_agent.packs import core, tools
from mobile_agent.packs import tiktok as tt
from mobile_agent.tests.packs_support import Phone, el, rows_file, snap

B = tt.BUNDLE
FIXTURES = {"for-you": "for_you", "for-you-2": "for_you", "for-you-ad": "for_you", "following": "following",
            "video": "video", "comments": "comments", "inbox": "inbox", "dm-thread": "dm_thread", "profile": "profile",
            "profile-own": "profile", "favorites": "favorites", "search-results": "search", "insights": "insights"}
ACTS = re.compile(r"^(Like|Follow\b.*|Favorites|Add to Favorites|Share|Send|Post|Repost|Shop now)$")


def screen(name):
    return rows_file(f"tiktok/{name}.json", B)


def phone(start, links=None):
    screens = {name: screen(name) for name in FIXTURES}
    screens["springboard"] = snap("com.apple.springboard", [el("TikTok", "Icon", (.1, .1, .2, .1))])
    search = snap(B, [el("Search", "SearchField", (.12, .06, .7, .04), editable=True),
                      el("Back", rect=(.02, .06, .08, .04))])
    screens["search-empty"] = search
    moves = {("*", "LAUNCH_APP", B): "for-you"}
    for here in ("for-you", "for-you-2", "for-you-ad", "following", "inbox", "profile-own", "favorites"):
        moves.update({(here, "TAP", "Home"): "for-you", (here, "TAP", "Inbox"): "inbox",
                      (here, "TAP", "Profile"): "profile-own"})
    moves.update({
        ("for-you", "TAP", "Following"): "following", ("following", "TAP", "For You"): "for-you",
        ("for-you", "SWIPE_UP", None): "for-you-ad", ("for-you-ad", "SWIPE_UP", None): "for-you-2",
        ("for-you-2", "SWIPE_UP", None): "for-you-2", ("for-you", "TAP", "Comments"): "comments",
        ("comments", "SWIPE_UP", None): "comments", ("inbox", "SWIPE_UP", None): "inbox",
        ("inbox", "TAP", "Unread, Mia Chen, sent a video, 2h"): "dm-thread", ("dm-thread", "TAP", "Back"): "inbox",
        ("profile-own", "TAP", "Favorites"): "favorites",
        ("favorites", "TAP", "Video by street.chef, 1.2M views"): "video",
        ("favorites", "TAP", "Video by desk.setup, 330K views"): "video", ("video", "TAP", "Back"): "favorites",
        ("for-you", "TAP", "Search"): "search-empty", ("search-empty", "TYPE", "Search"): "search-results",
        ("search-results", "TAP", "space.facts"): "profile",
    })
    return Phone(screens, moves, start, links=links)


def io_for(device):
    return core.PackIO(device, tt.PACK, sleep=lambda s: None)


class ScreenTests(unittest.TestCase):
    def test_every_screen_the_pack_knows_is_recognized_on_its_fixture(self):
        for name, expected in FIXTURES.items():
            seen = tt.PACK.recognize(screen(name))
            self.assertEqual(seen.screen, expected, f"{name}: {seen}")
        self.assertIsNone(tt.PACK.recognize(snap("com.burbn.instagram", [el("Reels", "Button")])).screen)

    def test_every_screen_says_it_is_inferred_until_a_tree_checks_it(self):
        self.assertEqual({s.evidence for s in tt.PACK.screens}, {"inferred"})


class ReaderTests(unittest.TestCase):
    def test_a_video_its_counts_and_sound_and_an_ad(self):
        row = tt.read_video(screen("for-you"))
        self.assertEqual((row["author"], row["caption"], row["sound"]),
                         ("street.chef", "Smash burger in 90 seconds #food", "original sound - street.chef"))
        self.assertEqual((row["likes"], row["comments"], row["saves"], row["shares"]), (120400, 1203, 8812, 2450))
        self.assertEqual(tt.read_video(screen("for-you-ad"))["ad"], "Sponsored")

    def test_messages_comments_and_a_profile(self):
        self.assertEqual([(r["from"], r["unread"], r["when"]) for r in tt.parse_inbox(screen("inbox"))],
                         [("Mia Chen", True, "2h"), ("Leo", False, "1d"), ("Priya", False, "3d")])
        self.assertEqual([(c["author"], c["likes"]) for c in tt.parse_comments(screen("comments"))],
                         [("grill.fan", 1021), ("veggie.life", 88), ("night.owl", 0)])
        row = tt.read_profile(tt.PACK, None, screen("profile"), 1, None).rows[0]
        self.assertEqual((row["handle"], row["following"], row["followers"], row["likes"], row["state"]),
                         ("space.facts", 12, 2_100_000, 48_500_000, "follow"))


class NavigateAndReadTests(unittest.TestCase):
    def check(self, device, allow=()):
        for label in device.taps:
            if label not in allow:
                self.assertFalse(ACTS.match(label or ""), f"tapped an act: {label}")

    def test_tabs_links_and_search(self):
        device = phone("springboard")
        landing = core.navigate(tt.PACK, io_for(device), "inbox")
        self.assertEqual((landing.ok, landing.seen.screen), (True, "inbox"))
        device = phone("for-you", links={"https://www.tiktok.com/@space.facts": "profile"})
        landing = core.navigate(tt.PACK, io_for(device), "user", "@space.facts")
        self.assertEqual((landing.ok, landing.how), (True, "link"))
        device = phone("for-you")
        landing = core.navigate(tt.PACK, io_for(device), "user", "space.facts")
        self.assertTrue(landing.ok, landing.why)
        self.assertEqual(device.taps[-1], "space.facts")
        self.check(device)

    def test_a_profile_with_a_name_is_read_through_the_user_route(self):
        device = phone("for-you", links={"https://www.tiktok.com/@space.facts": "profile"})
        result = tt.PACK.read(io_for(device), "profile", 1, "@space.facts")
        self.assertEqual((result.rows[0]["handle"], result.screen), ("space.facts", "profile"))
        self.assertEqual(device.calls[0][1], "/url")
        self.check(device)

    def test_videos_swipe_by_code_and_ads_dont_count(self):
        device = phone("for-you")
        result = tt.PACK.read(io_for(device), "videos", 2)
        self.assertEqual([r["author"] for r in result.rows], ["street.chef", "shoe.brand", "desk.setup"])
        self.assertEqual(result.title, "2 TikToks")
        block = tools.block_for(tt.PACK, result)
        self.assertEqual(block["items"][0]["sounds"], ["original sound - street.chef"])
        self.check(device)

    def test_messages_a_thread_comments_and_favorites(self):
        result = tt.PACK.read(io_for(phone("for-you")), "dms", 3)
        self.assertEqual(result.title, "3 newest messages (1 unread)")
        result = tt.PACK.read(io_for(phone("for-you")), "thread", 5, "Mia Chen")
        self.assertEqual([m["from"] for m in result.rows], ["Mia Chen", "You"])
        result = tt.PACK.read(io_for(phone("for-you")), "comments", 3)
        self.assertEqual(len(result.rows), 3)
        device = phone("for-you")
        result = tt.PACK.read(io_for(device), "favorites", 2)
        self.assertEqual([r["author"] for r in result.rows], ["space.facts", "space.facts"])
        self.assertEqual(device.taps[:2], ["Profile", "Favorites"])
        self.check(device, allow=("Favorites",))

    def test_acts_stop_at_their_control(self):
        device = phone("for-you")
        tool = tools.PackTool(tt.PACK, "OPEN", sleep=lambda s: None)
        ctx = SimpleNamespace(driver=device, emit=lambda e: None, guard=None,
                              run=SimpleNamespace(run_id="tt-acts", data_dir=None, cancelled=lambda: False,
                                                  runtime=None))
        said = tool.perform(ctx, tool.prepare(ctx, {"text": "like"}, device.observe()), {}, device.observe())
        self.assertIn("The Like control is “Like”", said.feedback)
        self.check(device)


class SafetyTests(unittest.TestCase):
    def test_a_videos_favorites_button_is_never_the_favorites_tab(self):
        landing_step = tt.PACK.routes["favorites"].steps[1]
        video = screen("for-you")
        self.assertIsNone(core.find(video, landing_step.pattern, region=landing_step.region))


class WordsTests(unittest.TestCase):
    def test_prompts_and_words(self):
        from mobile_agent.harness.tools import PROMPT_CHARS
        for kind in ("OPEN", "READ"):
            line = tt.PACK.prompts[kind].removeprefix("- ")
            self.assertTrue(line.startswith(f"TT_{kind}"))
            self.assertLessEqual(len(line), PROMPT_CHARS)
        self.assertEqual(tools.parse_open(tt.PACK, "fyp"), ("foryou", None, None))
        self.assertEqual(tools.parse_open(tt.PACK, "profile @nasa"), ("user", "@nasa", None))
        self.assertEqual(tools.parse_read(tt.PACK, "tiktoks 20"), ("videos", 20, None))
        about = SimpleNamespace(engine="smart", goal="Watch 10 TikToks about cooking", app_bundle=None)
        self.assertIsNotNone(tools.factory(tt.PACK, "READ")(about))
        self.assertIsNone(tools.factory(tt.PACK, "READ")(SimpleNamespace(engine="smart", goal="Watch 10 Reels",
                                                                        app_bundle=None)))

    def test_the_track_and_mcp_list_tiktok(self):
        import tempfile

        from mobile_agent import tracks
        from mobile_agent.mcp_server import tools as mcp
        from mobile_agent.tests.seam_support import isolate
        isolate(self)
        tracks.load()
        with tempfile.TemporaryDirectory() as folder:
            listed = [t["name"] for t in mcp.ToolSet(runs_dir=folder, version="0", devices=lambda: []).list_tools()]
        self.assertIn("tiktok_open", listed)
        self.assertIn("tiktok_read", listed)


if __name__ == "__main__":
    unittest.main()
