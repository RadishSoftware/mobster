import re
import unittest
from types import SimpleNamespace

from mobile_agent.packs import core, social, tools
from mobile_agent.packs import instagram as ig
from mobile_agent.tests.packs_support import Phone, el, recorded, rows_file, snap

B = ig.BUNDLE


RECORDED = {"reel-collab": "reel-collab.json", "dm-inbox-recorded": "dm-inbox-recorded.json"}


def screen(name):
    return recorded(f"instagram/{RECORDED[name]}", B) if name in RECORDED else rows_file(f"instagram/{name}.json", B)


FIXTURES = {"feed": "feed", "feed-2": "feed", "reel": "reels", "reel-audio": "reels", "reel-ad": "reels",
            "reel-collab": "reels", "explore": "explore", "search-results": "search", "comments": "comments",
            "dm-inbox": "dm_inbox", "dm-inbox-recorded": "dm_inbox", "dm-thread": "dm_thread", "profile": "profile",
            "saved": "saved",
            "saved-all": "saved", "insights": "insights", "profile-own": "profile"}
POST = snap(B, [el("Back", rect=(.02, .07, .08, .04)), el("Posts", "StaticText", (.4, .07, .2, .03)),
                el("bakery.daily", rect=(.13, .14, .6, .02)), el("Post options", rect=(.89, .12, .11, .07)),
                el("Photo by bakery.daily", "Image", (0, .18, 1, .45)), el("action-cell", "Cell", (0, .63, 1, .06)),
                el("Like", rect=(.01, .63, .1, .06)), el("880 likes", rect=(.11, .65, .1, .02)),
                el("bakery.daily Rye, day two", "StaticText", (.03, .7, .94, .02)),
                el("2 days ago", "StaticText", (.03, .73, .94, .02))])


def phone(start, links=None):
    screens = {name: screen(name) for name in FIXTURES}
    screens["springboard"] = snap("com.apple.springboard", [el("Instagram", "Icon", (.1, .1, .2, .1))])
    screens["post"] = POST
    screens["reel-pushed"] = snap(B, [el("Back", rect=(.02, .07, .08, .04)),
                                      *[dict(label=e.label, role=e.role, rect=e.rect, value=e.value)
                                        for e in screen("reel").elements if not e.label.startswith(
                                            ("Main feed", "Reels", "Direct", "Explore", "Profile", "6"))]])
    screens["composer"] = snap(B, [el("Close", rect=(.02, .07, .08, .04)),
                                   el("New post", "StaticText", (.35, .07, .3, .03)),
                                   el("Next", rect=(.85, .07, .12, .04)), el("Recents", rect=(.04, .5, .3, .03)),
                                   el("POST", rect=(.3, .9, .15, .03)), el("STORY", rect=(.5, .9, .15, .03))])
    dms = "Direct messages , 6 unread messages"
    moves = {("*", "LAUNCH_APP", B): "feed"}
    for here in ("feed", "reel", "explore", "dm-inbox", "profile"):
        moves.update({(here, "TAP", "Main feed"): "feed", (here, "TAP", "Reels"): "reel",
                      (here, "TAP", dms): "dm-inbox",
                      (here, "TAP", "Explore"): "explore", (here, "TAP", "Profile"): "profile"})
    moves.update({
        ("explore", "TAP", "Saved"): "saved", ("saved", "TAP", "All posts"): "saved-all",
        ("saved-all", "TAP", "Photo by bakery.daily"): "post",
        ("saved-all", "TAP", "Reel by dance.studio"): "reel-pushed",
        ("reel-pushed", "TAP", "Back"): "saved-all",
        ("saved-all", "TAP", "3 photos or videos from travel.notes"): "post", ("post", "TAP", "Back"): "saved-all",
        ("explore", "TYPE", "Search with Meta AI"): "search-results",
        ("search-results", "TAP", "space • 104M followers"): "profile", ("profile", "TAP", "Back"): "explore",
        ("dm-inbox", "TAP", "Unread, Sam Lee, Delete, 4 new messages, 3 hours ago"): "dm-thread",
        ("dm-thread", "TAP", "Back"): "dm-inbox",
        ("reel", "TAP", "Comment"): "comments", ("feed", "TAP", "Create a post, story or live video"): "composer",
        ("reel", "SWIPE_UP", None): "reel-ad", ("reel-ad", "SWIPE_UP", None): "reel-audio",
        ("reel-audio", "SWIPE_UP", None): "reel-collab", ("reel-collab", "SWIPE_UP", None): "reel-collab",
        ("feed", "SWIPE_UP", None): "feed-2", ("feed-2", "SWIPE_UP", None): "feed-2",
        ("comments", "SWIPE_UP", None): "comments", ("dm-inbox", "SWIPE_UP", None): "dm-inbox",
        ("dm-inbox-recorded", "SWIPE_UP", None): "dm-inbox-recorded",
    })
    for name in ("reel", "reel-ad", "reel-audio", "reel-collab"):
        moves[(name, "TAP", "Comment")] = "comments"
        moves[(name, "TAP", "Main feed")] = "feed"
        moves[(name, "TAP", "Explore")] = "explore"
        moves[(name, "TAP", dms)] = "dm-inbox"
    return Phone(screens, moves, start, links=links)


def io_for(device):
    return core.PackIO(device, ig.PACK, sleep=lambda s: None)


ACTS = re.compile(r"^(Like|Follow\b.*|Send|Save post|Repost|Post comment|Share|Ad|Install now)$")


class ScreenTests(unittest.TestCase):
    def test_every_screen_the_pack_knows_is_recognized_on_its_fixture(self):
        for name, expected in FIXTURES.items():
            seen = ig.PACK.recognize(screen(name))
            self.assertEqual(seen.screen, expected, f"{name}: {seen}")
            self.assertGreaterEqual(seen.confidence, core.SURE)
        covered = {screen_ for screen_ in FIXTURES.values()}
        recorded_ = {s.name for s in ig.PACK.screens if s.evidence == "recorded"} - {"post"}
        self.assertLessEqual(recorded_, covered)

    def test_another_app_and_a_half_drawn_screen_are_not_instagram_screens(self):
        self.assertIsNone(ig.PACK.recognize(snap("com.zhiliaoapp.musically", [el("For You", "Button")])).screen)
        self.assertIsNone(ig.PACK.recognize(snap(B, [el("Loading...", "Cell", (0, .3, 1, .1))])).screen)

    def test_the_pack_says_what_it_was_checked_on(self):
        self.assertEqual(ig.PACK.verified.version, "448.0.0")
        self.assertEqual({s.evidence for s in ig.PACK.screens}, {"recorded", "inferred"})


class ReaderTests(unittest.TestCase):
    def test_a_collab_reel_with_no_single_handle_names_both_creators(self):
        row = ig.read_reel(screen("reel-collab"))
        self.assertEqual((row["author"], row["collab"]), ("maker.one", "maker.two"))
        self.assertEqual((row["likes"], row["comments"], row["shares"], row["reposts"]), (58922, 254, 37563, 3551))
        self.assertTrue(row["captionHidden"])
        self.assertIsNone(row["ad"])

    def test_a_reel_its_ad_and_its_audio(self):
        row = ig.read_reel(screen("reel"))
        self.assertEqual((row["author"], row["caption"], row["following"], row["social"]),
                         ("dance.studio", "Three moves for beginners #dance", False, "pal.one liked this reel."))
        self.assertEqual(ig.read_reel(screen("reel-ad"))["ad"], "Ad")
        self.assertEqual(ig.read_reel(screen("reel-audio"))["audio"], "Original audio - chef.at.home")

    def test_feed_posts_only_once_their_actions_show(self):
        rows = ig.parse_feed(screen("feed"))
        self.assertEqual([(r["author"], r["kind"], r["likes"], r["comments"], r["when"]) for r in rows],
                         [("bakery.daily", "photo", 2004, 20, "9 hours ago")])
        self.assertEqual(rows[0]["caption"], "Sourdough fresh out of the oven…")
        later = ig.parse_feed(screen("feed-2"))
        self.assertEqual((later[0]["author"], later[0]["collab"], later[0]["likes"]),
                         ("trail.runner", "coach.kim", 16767))

    def test_comments_dms_a_thread_a_profile_and_saved(self):
        comments = ig.parse_comments(screen("comments"))
        self.assertEqual([(c["author"], c["likes"], c["when"]) for c in comments],
                         [("fan.one", 67, "6 days ago"), ("fan.two", 216, "1 week ago"),
                          ("fan.three", 0, "2 weeks ago")])
        inbox = ig.parse_inbox(screen("dm-inbox"))
        self.assertEqual([(r["from"], r["unread"], r["when"]) for r in inbox],
                         [("Sam Lee", True, "3 hours ago"), ("Riley", False, "5h"), ("Jordan Park", False, "1d"),
                          ("Book Club", True, "2d"), ("Alex", False, "1w")])
        self.assertEqual((inbox[0]["last"], inbox[0]["new"], inbox[2]["last"]),
                         ("4 new messages", 4, "You: sounds good"))
        thread = social.parse_thread(screen("dm-thread"))
        self.assertEqual([(m["from"], m["text"]) for m in thread],
                         [("them", "are we still on for friday?"), ("you", "yes! what time works?"),
                          ("them", "8 at the usual place?")])
        profile = ig.read_profile(ig.PACK, None, screen("profile"), 1, None).rows[0]
        self.assertEqual((profile["handle"], profile["posts"], profile["followers"], profile["following"],
                          profile["verified"], profile["state"]), ("space", 4021, 104_000_000, 77, True, "follow"))
        self.assertEqual([(r["author"], r["kind"]) for r in ig.parse_grid(screen("saved-all"))],
                         [("bakery.daily", "photo"), ("dance.studio", "reel"), ("travel.notes", "album")])

    def test_a_dm_row_label_in_its_variants(self):
        parse = social.parse_inbox_row
        self.assertEqual(parse("Unread, Mo, Mute, Delete, 2 new messages, now")["from"], "Mo")
        self.assertEqual(parse("Ana, Delete, Sent a reel by bakery.daily, Yesterday")["last"],
                         "Sent a reel by bakery.daily")
        for not_a_row in ("Primary", "Requests", "Search", "Direct messages , 6 unread messages", "Riley"):
            self.assertIsNone(parse(not_a_row), not_a_row)

    def test_a_group_row_names_its_people_and_a_notes_strip_cell_with_a_comma_is_no_row(self):
        parse = social.parse_inbox_row
        group = parse("Ana Ruiz, Bo Chen, Cy Park, Delete, Bo Chen: ok, sure, 4 hours ago")
        self.assertEqual((group["from"], group["group"], group["last"], group["when"]),
                         ("Ana Ruiz, Bo Chen, Cy Park", True, "Bo Chen: ok, sure", "4 hours ago"))
        one = parse("Unread, Ana Ruiz, Delete, Sent a reel by bakery.daily, 5 hours ago")
        self.assertEqual((one["from"], one["group"], one["unread"]), ("Ana Ruiz", False, True))
        self.assertEqual(parse("Ana Ruiz, hey there, 2h")["last"], "hey there")
        sent = parse("Sam Lee, Delete, Sent Tuesday")
        self.assertEqual((sent["from"], sent["last"], sent["when"]), ("Sam Lee", None, "Tuesday"))
        self.assertEqual(parse("Sam Lee, Delete, Seen 2h ago")["when"], "2h ago")
        said = parse("Sam Lee, Delete, Sent you the file, 2d")
        self.assertEqual((said["last"], said["when"]), ("Sent you the file", "2d"))
        self.assertIsNone(parse("Sam Lee, Delete, Sent")["when"])
        self.assertIsNone(parse("Ana Ruiz shared a note that says hi, how are you"))
        self.assertIsNone(parse("Your note, 1 of 8"))

    def test_the_recorded_inbox_gives_every_row_its_people_its_last_message_and_its_time(self):
        rows = ig.parse_inbox(screen("dm-inbox-recorded"))
        self.assertEqual([(r["from"], r["group"], r["unread"], r["last"], r["when"]) for r in rows], [
            ("Alex Example, Blake Sample, Casey Demo", True, False, "Blake Sample: placeholder message", "4 hours ago"),
            ("Dana Example", False, True, "Sent a reel by reel.account.one", "4 hours ago"),
            ("Eli Sample", False, True, "Sent a reel by reel.account.two", "5 hours ago"),
            ("Fran Demo, Gale Sample • 例子, Hana Example, Ivy Test", True, False,
             "Ivy Test: placeholder message with emoji 🤔", "1 day ago"),
            ("Jules Example", False, False, "W", "1 day ago")])

    def test_the_dm_reader_gives_a_value_for_each_field_and_names_a_field_it_cannot_read(self):
        device = phone("dm-inbox-recorded")
        result = ig.read_dms(ig.PACK, io_for(device), screen("dm-inbox-recorded"), 10, None)
        self.assertEqual(result.title, "5 newest DMs (2 unread)")
        required = ig.PACK.required["dms"]
        lines = [tools.row_line(i + 1, row, result.columns, required) for i, row in enumerate(result.rows)]
        self.assertEqual(lines[1], "2. From: Dana Example; Unread: yes; Last message: Sent a reel by reel.account.one; "
                                   "When: 4 hours ago")
        for line in lines:
            for field in ("From: ", "Last message: ", "When: "):
                self.assertIn(field, line)
            self.assertNotIn("not on the screen", line)
        no_message = social.parse_inbox_row("Sam Lee, Delete, 3h")
        self.assertEqual(tools.row_line(1, no_message, ig.DM_COLUMNS, required),
                         "1. From: Sam Lee; Last message: not on the screen; When: 3h")
        no_time = social.parse_inbox_row("Sam Lee, Delete, see you there")
        self.assertEqual(tools.row_line(1, no_time, ig.DM_COLUMNS, required),
                         "1. From: Sam Lee; Last message: see you there; When: not on the screen")
        no_preview = social.parse_inbox_row("Sam Lee, Delete, Sent Tuesday")
        self.assertEqual(tools.row_line(1, no_preview, ig.DM_COLUMNS, required),
                         "1. From: Sam Lee; Last message: not on the screen; When: Tuesday")
        self.assertEqual(tools.row_line(1, no_message, ig.DM_COLUMNS), "1. From: Sam Lee; When: 3h")


class LinkTests(unittest.TestCase):
    POST_ROUTE = ig.PACK.routes["post"]

    def test_the_post_route_opens_a_posts_address_and_no_address_that_lands_on_the_feed(self):
        for url in ("https://www.instagram.com/p/abc123/", "https://instagram.com/reel/Cx1_2-3/?igsh=x",
                    "https://www.instagram.com/tv/abc123", "https://www.instagram.com/reels/Cx12345abc/"):
            self.assertEqual(core.link_for(ig.PACK, self.POST_ROUTE, url), url, url)
        for url in ("https://www.instagram.com/direct/inbox/", "https://www.instagram.com/reels/",
                    "https://www.instagram.com/reels", "https://www.instagram.com/",
                    "https://www.instagram.com/explore/",
                    "https://www.instagram.com/reels/audio/123/", "http://www.instagram.com/p/abc123/",
                    "https://example.com/p/abc123/"):
            self.assertIsNone(core.link_for(ig.PACK, self.POST_ROUTE, url), url)

    def test_a_post_route_given_the_inbox_address_opens_no_link(self):
        device = phone("feed", links={"https://www.instagram.com/": "feed"})
        landing = core.navigate(ig.PACK, io_for(device), "post", "https://www.instagram.com/direct/inbox/")
        self.assertFalse(landing.ok)
        self.assertEqual([call for call in device.calls if call[1] == "/url"], [])

    def test_the_pack_reaches_dms_and_reels_by_the_tab_bar_never_by_a_web_address(self):
        for route in ("dms", "reels"):
            self.assertFalse(ig.PACK.routes[route].link, route)


class NavigateTests(unittest.TestCase):
    def go(self, start, route, arg=None, links=None):
        device = phone(start, links)
        landing = core.navigate(ig.PACK, io_for(device), route, arg)
        for label in device.taps:
            self.assertFalse(ACTS.match(label or ""), f"tapped an act: {label}")
        return landing, device

    def test_tabs_are_one_tap_and_saved_is_two(self):
        landing, device = self.go("reel", "dms")
        self.assertEqual((landing.ok, landing.seen.screen, device.taps), (True, "dm_inbox",
                                                                          ["Direct messages , 6 unread messages"]))
        landing, device = self.go("feed", "saved")
        self.assertEqual((landing.ok, device.taps), (True, ["Explore", "Saved"]))
        landing, device = self.go("springboard", "reels")
        self.assertEqual((landing.ok, [a[0] for a in device.actions]), (True, ["LAUNCH_APP", "TAP"]))

    def test_a_profile_by_link_and_by_search_when_the_link_lands_nowhere(self):
        landing, device = self.go("feed", "user", "@space", links={"instagram://user?username=space": "profile"})
        self.assertEqual((landing.ok, landing.how, device.taps), (True, "link", []))
        landing, device = self.go("feed", "user", "space")
        self.assertTrue(landing.ok, landing.why)
        self.assertEqual(landing.how, "code")
        self.assertEqual(device.taps, ["Explore", "space • 104M followers"])
        self.assertIn(("TYPE", "Search with Meta AI", "space"), device.actions)

    def test_a_thread_by_name_and_the_comments_by_their_opener(self):
        landing, device = self.go("feed", "thread", "sam lee")
        self.assertTrue(landing.ok, landing.why)
        self.assertEqual(device.taps[-1], "Unread, Sam Lee, Delete, 4 new messages, 3 hours ago")
        landing, _ = self.go("reel", "comments")
        self.assertEqual((landing.ok, landing.seen.screen), (True, "comments"))

    def test_acts_stop_at_their_control(self):
        device = phone("reel")
        result = tools.PackTool(ig.PACK, "OPEN", sleep=lambda s: None)
        ctx = SimpleNamespace(driver=device, emit=lambda e: None, guard=None,
                              run=SimpleNamespace(run_id="acts", data_dir=None, cancelled=lambda: False, runtime=None))
        said = result.perform(ctx, result.prepare(ctx, {"text": "like"}, device.observe()), {}, device.observe())
        self.assertIn("The Like control is “Like”", said.feedback)
        said = result.perform(ctx, result.prepare(ctx, {"text": "follow @space"}, device.observe()), {},
                              device.observe())
        self.assertIn("The Follow control is “Follow”", said.feedback)
        said = result.perform(ctx, result.prepare(ctx, {"text": "compose"}, device.observe()), {}, device.observe())
        self.assertIn("The Next control is “Next”", said.feedback)
        for label in device.taps:
            self.assertFalse(ACTS.match(label or ""), f"tapped an act: {label}")
        self.assertNotIn("Follow", device.taps)

    def test_a_post_link_opens_only_instagrams_own_site(self):
        landing, device = self.go("feed", "post", "https://www.instagram.com/p/abc123/",
                                  links={"https://www.instagram.com/p/": "post"})
        self.assertEqual((landing.ok, landing.how), (True, "link"))
        landing, device = self.go("feed", "post", "https://evil.example/p/abc123/")
        self.assertFalse(landing.ok)
        self.assertEqual(device.calls, [])


class ReadTests(unittest.TestCase):
    def read(self, start, what, n, arg=None):
        device = phone(start)
        result = ig.PACK.read(io_for(device), what, n, arg)
        for label in device.taps:
            self.assertFalse(ACTS.match(label or ""), f"tapped an act: {label}")
        return result, device

    def test_reels_swipe_by_code_and_ads_dont_count(self):
        result, device = self.read("feed", "reels", 3)
        self.assertEqual([r["author"] for r in result.rows], ["dance.studio", "trade.app", "chef.at.home", "maker.one"])
        self.assertEqual([r["ad"] for r in result.rows], [None, "Ad", None, None])
        self.assertEqual(result.title, "3 Reels")
        self.assertEqual(sum(1 for a in device.actions if a[0] == "SWIPE_UP"), 3)
        block = tools.block_for(ig.PACK, result)
        self.assertEqual((block["type"], block["items"][0]["creator"], block["items"][2]["sounds"]),
                         ("media_list", "dance.studio", ["Original audio - chef.at.home"]))

    def test_dms_a_thread_and_a_profile(self):
        result, _ = self.read("reel", "dms", 3)
        self.assertEqual(([r["from"] for r in result.rows], result.title),
                         (["Sam Lee", "Riley", "Jordan Park"], "3 newest DMs (1 unread)"))
        result, device = self.read("feed", "thread", 5, "Sam Lee")
        self.assertEqual([m["from"] for m in result.rows], ["Sam Lee", "You", "Sam Lee"])
        result, _ = self.read("feed", "profile", 1, "@space")
        self.assertEqual(result.rows[0]["followers"], 104_000_000)

    def test_saved_posts_are_opened_one_by_one_and_read(self):
        result, device = self.read("feed", "saved", 3)
        self.assertEqual([(r["author"], r["kind"]) for r in result.rows],
                         [("bakery.daily", "photo"), ("dance.studio", "reel"), ("travel.notes", "album")])
        self.assertEqual(result.rows[0]["caption"], "Rye, day two")
        self.assertEqual(result.rows[1]["caption"], "Three moves for beginners #dance")
        self.assertEqual(device.taps[:3], ["Explore", "Saved", "All posts"])

    def test_feed_and_comments(self):
        result, _ = self.read("feed", "feed", 2)
        self.assertEqual([r["author"] for r in result.rows], ["bakery.daily", "trail.runner"])
        result, _ = self.read("reel", "comments", 3)
        self.assertEqual(len(result.rows), 3)


class CaptionTests(unittest.TestCase):
    def test_a_folded_caption_is_opened_with_one_tap_and_read(self):
        folded = screen("reel-collab")
        opened = snap(B, [*[dict(label=e.label, role=e.role, rect=e.rect, value=e.value) for e in folded.elements
                            if not e.label.startswith("More, button")],
                          el("Behind the scenes of our collab #studio", "StaticText", (.04, .8, .79, .05))])
        opened.text = folded.text + "\nBehind the scenes of our collab #studio"
        device = Phone({"reel": folded, "open": opened},
                       {("reel", "TAP", "More, button, tap to see the whole caption"): "open"}, "reel")
        result = ig.PACK.read(io_for(device), "reels", 1)
        self.assertEqual(result.rows[0]["caption"], "Behind the scenes of our collab #studio")
        self.assertEqual(device.taps, ["More, button, tap to see the whole caption"])


class RegisteredTests(unittest.TestCase):
    def test_the_track_offers_the_tools_to_a_run_about_instagram_and_mcp_lists_them(self):
        import tempfile

        from mobile_agent import harness_api, tracks
        from mobile_agent.mcp_server import tools as mcp
        from mobile_agent.tests.seam_support import isolate
        isolate(self)
        tracks.load()
        self.assertEqual(tracks.STATUS.get("packs"), "ok")

        def ctx(goal, app=None):
            return harness_api.RunContext("r1", goal, "smart", "app", app, None, "usb", {}, None, lambda e: None,
                                          lambda r: "", lambda: False, None)
        ops = [getattr(t, "op", None) for t in harness_api.tools(ctx("Summarize my saved posts on Instagram"))]
        self.assertIn("IG_OPEN", ops)
        self.assertIn("IG_READ", ops)
        self.assertNotIn("IG_OPEN", [getattr(t, "op", None) for t in harness_api.tools(ctx("Turn on Wi-Fi"))])
        with tempfile.TemporaryDirectory() as folder:
            listed = [t["name"] for t in mcp.ToolSet(runs_dir=folder, version="0", devices=lambda: []).list_tools()]
        self.assertIn("instagram_open", listed)
        self.assertIn("instagram_read", listed)


class DocsTests(unittest.TestCase):
    def test_the_docs_show_what_instagram_read_prints(self):
        from pathlib import Path

        from mobile_agent.packs.mcp import PacksProvider
        page = (Path(__file__).resolve().parents[2] / "docs" / "app-packs.mdx").read_text()
        shown = re.search(r"`instagram_read` answers:\n\n```text\n(.*?)\n```", page, re.S).group(1)
        text, _ = PacksProvider._read(ig.PACK, io_for(phone("dm-inbox")), "dms", 3, None)
        self.assertEqual(text, shown)


class WordsTests(unittest.TestCase):
    def test_prompts_pass_the_tool_policy_and_name_every_word(self):
        from mobile_agent.harness.tools import PROMPT_CHARS, offered
        for kind in ("OPEN", "READ"):
            line = ig.PACK.prompts[kind].removeprefix("- ")
            self.assertTrue(line.startswith(f"IG_{kind}"))
            self.assertLessEqual(len(line), PROMPT_CHARS)
        about = SimpleNamespace(engine="smart", goal="Watch 10 Reels and tell me what's trending", app_bundle=None,
                                interactive=True)
        made = [tools.factory(ig.PACK, kind)(about) for kind in ("OPEN", "READ")]
        self.assertEqual([t.op for t in offered(made, run_context=about, driver=phone("feed"))], ["IG_OPEN", "IG_READ"])
        self.assertIsNone(tools.factory(ig.PACK, "OPEN")(SimpleNamespace(engine="smart", goal="Text Sam hi",
                                                                         app_bundle="com.apple.MobileSMS")))

    def test_open_and_read_words(self):
        self.assertEqual(tools.parse_open(ig.PACK, "profile @nasa"), ("user", "@nasa", None))
        self.assertEqual(tools.parse_open(ig.PACK, "profile"), ("profile", None, None))
        self.assertEqual(tools.parse_open(ig.PACK, "inbox"), ("dms", None, None))
        self.assertEqual(tools.parse_open(ig.PACK, "follow @nasa"), ("user", "@nasa", "follow"))
        self.assertEqual(tools.parse_open(ig.PACK, "reply Sam Lee"), ("thread", "Sam Lee", "reply"))
        self.assertEqual(tools.parse_read(ig.PACK, "reels 20"), ("reels", 20, None))
        self.assertEqual(tools.parse_read(ig.PACK, "thread Sam Lee"), ("thread", 20, "Sam Lee"))
        self.assertEqual(tools.parse_read(ig.PACK, "inbox"), ("dms", 15, None))


if __name__ == "__main__":
    unittest.main()
