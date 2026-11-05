import re
import unittest
from types import SimpleNamespace

from mobile_agent.harness.tools import PROMPT_CHARS
from mobile_agent.packs import (
    core,
    linkedin,
    reddit,
    social,
    threads,
    tools,
    x,
    youtube,
)
from mobile_agent.packs import (
    instagram as ig,
)
from mobile_agent.packs import (
    tiktok as tt,
)
from mobile_agent.tests import packs_pictured as pics
from mobile_agent.tests.packs_support import Phone, el, rows_file, snap

ACTS = re.compile(r"^(Like|Follow|Subscribe|Repost|Upvote|Downvote|Join|Connect|Send|Post|Reply)\b")


def tabs(labels, selected):
    width = 1 / len(labels)
    return [el(label, "Button", (i * width, .92, width, .06), **({"selected": True} if label == selected else {}))
            for i, label in enumerate(labels)]


def rows(pack_rows, top=.15):
    return [el(label, "Cell", (0, top + .12 * i, 1, .11)) for i, label in enumerate(pack_rows)]


X_TABS = ("Home", "Search and Explore", "Grok", "Notifications", "Messages")
YT_TABS = ("Home", "Shorts", "Create", "Subscriptions", "You")
TH_TABS = ("Home", "Search", "Create", "Activity", "Profile")
LI_TABS = ("Home", "Video", "Post", "Notifications", "Jobs")
RD_TABS = ("Home", "Communities", "Create", "Chat", "Inbox")

CASES = {
    "x": dict(pack=x.PACK, tabs=X_TABS, top=[el("For you", "Button", (.1, .07, .3, .04), selected=True),
                                              el("Following", "Button", (.5, .07, .3, .04))],
              page1=["Space Agency @space.agency 2h The Moon is bright tonight. 12 replies 40 reposts 1,203 likes",
                     "Promoted, Shoe Brand, @shoebrand, Run further this fall, 3 replies, 2 reposts, 88 likes",
                     "Cook Daily @cook.daily 5h Ten minute ramen, recipe below 4 replies 9 reposts 310 likes"],
              page2=["Cook Daily @cook.daily 5h Ten minute ramen, recipe below 4 replies 9 reposts 310 likes",
                     "Trail Runner @trail.runner 1d Hill repeats, week 3 1 reply 0 reposts 42 likes"],
              read="home", first=("Space Agency", 1203, "likes"), profile_link="https://x.com/"),
    "threads": dict(pack=threads.PACK, tabs=TH_TABS, top=[el("For you", "Button", (.1, .07, .3, .04))],
                    page1=["Plant Mom @plant.mom 3h Repotting a monstera today 22 likes 3 replies",
                           "City Walks @city.walks 6h Tokyo at 5am 410 likes 12 replies"],
                    page2=["Lift Daily @lift.daily 1d Deadlift form check 15 likes 2 replies"],
                    read="home", first=("Plant Mom", 22, "likes"), profile_link="https://www.threads.net/@"),
    "linkedin": dict(pack=linkedin.PACK, tabs=LI_TABS, top=[el("Messaging", "Button", (.85, .07, .1, .04)),
                                                            el("Search", "SearchField", (.1, .07, .7, .04),
                                                               editable=True)],
                     page1=["Dana Park, Product designer at Radish Software, 3d, "
                           "Excited to share our new app!, 120 reactions, "
                            "14 comments",
                            "Promoted, Cloud Co, Scale your team this quarter, 40 reactions, 2 comments"],
                     page2=["Sam Rivera, Engineer, 1w, We are hiring in Lisbon, 88 reactions, 9 comments"],
                     read="home", first=("Dana Park", 120, "likes"), profile_link="https://www.linkedin.com/in/"),
    "reddit": dict(pack=reddit.PACK, tabs=RD_TABS, top=[el("Search Reddit", "Button", (.2, .07, .6, .04))],
                   page1=["r/running, Posted by u/trail_fan, 5h, What shoes for a first marathon?, 1.2K upvotes, "
                          "340 comments",
                          "r/cooking, Posted by u/ramen_lab, 9h, My ten minute ramen, 860 upvotes, 75 comments"],
                   page2=["r/plants, Posted by u/fern_fan, 1d, Monstera after repotting, 2.3K upvotes, 120 comments"],
                   read="home", first=("u/trail_fan", 1200, "score"), profile_link="https://www.reddit.com/user/"),
}


def world(case):
    pack = case["pack"]
    home = case["tabs"][0]
    screens = {
        "springboard": snap("com.apple.springboard", [el(pack.name, "Icon", (.1, .1, .2, .1))]),
        "home": snap(pack.bundle, [*case["top"], *rows(case["page1"]), *tabs(case["tabs"], home)]),
        "home2": snap(pack.bundle, [*case["top"], *rows(case["page2"]), *tabs(case["tabs"], home)]),
        "other": snap(pack.bundle, [*tabs(case["tabs"], case["tabs"][-1])]),
    }
    moves = {("*", "LAUNCH_APP", pack.bundle): "other", ("other", "TAP", home): "home",
             ("home", "SWIPE_UP", None): "home2", ("home2", "SWIPE_UP", None): "home2"}
    return Phone(screens, moves, "springboard")


class SocialPackTests(unittest.TestCase):
    def test_each_home_is_recognized_reached_and_read(self):
        for key, case in CASES.items():
            with self.subTest(key):
                device = world(case)
                io = core.PackIO(device, case["pack"], sleep=lambda s: None)
                result = case["pack"].read(io, case["read"], 3)
                self.assertEqual(result.screen, "home")
                self.assertEqual(len(result.rows), 3, result.rows)
                author, count, column = case["first"]
                self.assertEqual((result.rows[0].get("author") if key != "reddit" else result.rows[0]["handle"],
                                  result.rows[0][column]), (author, count))
                self.assertEqual([a[0] for a in device.actions][:2], ["LAUNCH_APP", "TAP"])
                for label in device.taps:
                    self.assertFalse(ACTS.match(label or ""), f"{key} tapped an act: {label}")

    def test_ads_are_flagged(self):
        for key in ("x", "linkedin"):
            rows_ = social.post_rows(snap(CASES[key]["pack"].bundle, rows(CASES[key]["page1"])))
            self.assertEqual(rows_[1]["ad"], "Promoted", key)

    def test_a_profile_opens_by_its_link(self):
        for key, case in CASES.items():
            with self.subTest(key):
                pack = case["pack"]
                route = pack.routes["user"]
                url = core.link_for(pack, route, "@space.agency")
                self.assertTrue(url.startswith(case["profile_link"]), url)

    def test_youtube_rows_and_shorts(self):
        label = "How big is the Sun, really - Space Facts - 2.1M views - 3 days ago - 12 minutes, 4 seconds"
        row = youtube.parse_video(label)
        self.assertEqual((row["title"], row["channel"], row["views"], row["when"]),
                         ("How big is the Sun, really", "Space Facts", 2_100_000, "3 days ago"))
        short = snap(youtube.BUNDLE, [el("Like this video along with 12K other people", "Button", (.85, .45, .1, .05)),
                                      el("Dislike this video", "Button", (.85, .52, .1, .05)),
                                      el("Comments, 410", "Button", (.85, .58, .1, .05)),
                                      el("@space.facts", "Button", (.04, .78, .3, .03)),
                                      el("Why the Sun looks yellow", "StaticText", (.04, .82, .7, .03)),
                                      *tabs(YT_TABS, "Shorts")])
        self.assertEqual(youtube.PACK.recognize(short).screen, "shorts")
        got = youtube.read_short(short)
        self.assertEqual((got["channel"], got["title"], got["likes"], got["comments"]),
                         ("@space.facts", "Why the Sun looks yellow", 12_000, 410))
        home = snap(youtube.BUNDLE, [el(label, "Cell", (0, .2, 1, .3)), *tabs(YT_TABS, "Home")])
        self.assertEqual(youtube.PACK.recognize(home).screen, "home")

    def test_words_fit_the_tool_policy_and_never_shadow_another_pack(self):
        packs = [case["pack"] for case in CASES.values()] + [youtube.PACK]
        for pack in packs:
            for kind in ("OPEN", "READ"):
                line = pack.prompts[kind].removeprefix("- ")
                self.assertTrue(line.startswith(f"{pack.prefix}_{kind}"), pack.key)
                self.assertLessEqual(len(line), PROMPT_CHARS, pack.key)
            self.assertEqual({s.evidence for s in pack.screens}, {"inferred"}, pack.key)
        offered = lambda goal: [p.key for p in packs if tools.factory(p, "READ")(  # noqa: E731
            SimpleNamespace(engine="smart", goal=goal, app_bundle=None))]
        self.assertEqual(offered("List my 10 newest tweets"), ["x"])
        self.assertEqual(offered("Top posts in r/running on Reddit"), ["reddit"])
        self.assertEqual(offered("Watch 5 YouTube Shorts"), ["youtube"])
        self.assertEqual(offered("Turn on Wi-Fi"), [])


PACKS = {"instagram": ig.PACK, "tiktok": tt.PACK, "x": x.PACK, "youtube": youtube.PACK, "threads": threads.PACK,
         "linkedin": linkedin.PACK, "reddit": reddit.PACK}


class PicturedTests(unittest.TestCase):
    def test_every_pictured_screen_is_named_as_the_tree_names_it(self):
        for (app, name), (expected, _lines, _icons) in pics.SCREENS.items():
            with self.subTest(app=app, screen=name):
                pack = PACKS[app]
                snapshot = pics.screen(app, name, pack.bundle)
                self.assertTrue(core.image_only(snapshot))
                seen = pack.recognize(snapshot)
                self.assertEqual(seen.screen, expected, seen)

    def test_every_screen_has_picture_signals(self):
        for pack in PACKS.values():
            self.assertEqual([s.name for s in pack.screens if not s.ocr], [], pack.key)

    def test_a_tree_never_uses_the_picture_signals(self):
        snapshot = rows_file("instagram/feed.json", ig.BUNDLE)
        self.assertFalse(core.image_only(snapshot))
        self.assertEqual(ig.PACK.recognize(snapshot).screen, "feed")

    def test_lines_join_into_rows(self):
        snapshot = pics.screen("instagram", "dms", ig.BUNDLE)
        labels = [row.label for row in core.rows(snapshot)]
        self.assertIn("Sam Lee, 4 new messages, 3h", labels)

    def test_navigation_taps_named_icons_and_skips_links_it_cant_open(self):
        class Mirrored(Phone):
            call = None
        screens = {name: pics.screen("instagram", name, ig.BUNDLE) for name in ("feed", "dms", "profile", "explore")}
        device = Mirrored(screens, {("feed", "TAP", "Messages"): "dms", ("feed", "TAP", "Search"): "explore"}, "feed")
        landing = core.navigate(ig.PACK, core.PackIO(device, ig.PACK, sleep=lambda s: None), "dms")
        self.assertEqual((landing.ok, landing.seen.screen, device.taps), (True, "dm_inbox", ["Messages"]))
        landing = core.navigate(ig.PACK, core.PackIO(device, ig.PACK, sleep=lambda s: None), "user", "space")
        self.assertFalse(landing.ok)
        self.assertEqual(device.calls, [])

    def test_readers_read_lines(self):
        reel = ig.read_reel(pics.screen("instagram", "reels", ig.BUNDLE))
        self.assertEqual((reel["author"], reel["caption"], reel["likes"], reel["comments"]),
                         ("dance.studio", "Three moves for beginners #dance", 282_000, 2693))
        self.assertEqual([(r["from"], r["unread"], r["when"]) for r in ig.parse_inbox(pics.screen("instagram", "dms",
                                                                                                 ig.BUNDLE))],
                         [("Sam Lee", True, "3h"), ("Riley", False, "5h")])
        self.assertEqual(ig.parse_comments(pics.screen("instagram", "comments", ig.BUNDLE))[0]["author"], "fan.one")
        profile = ig.read_profile(ig.PACK, None, pics.screen("instagram", "profile", ig.BUNDLE), 1, None).rows[0]
        self.assertEqual((profile["posts"], profile["followers"], profile["following"]), (4021, 104_000_000, 77))
        video = tt.read_video(pics.screen("tiktok", "foryou", tt.BUNDLE))
        self.assertEqual((video["author"], video["likes"], video["comments"], video["sound"]),
                         ("street.chef", 120_400, 1203, "original sound - street.chef"))
        self.assertEqual(tt.parse_inbox(pics.screen("tiktok", "inbox", tt.BUNDLE))[0]["from"], "Mia Chen")

    def test_social_readers_read_lines(self):
        posts = social_read(x.PACK, "x", "home", "home", 2)
        self.assertEqual([(p["handle"], p.get("likes")) for p in posts][:1], [("@space.agency", 1203)])
        videos = youtube.video_rows(pics.screen("youtube", "home", youtube.BUNDLE))
        self.assertEqual((videos[0]["title"], videos[0]["channel"], videos[0]["views"]),
                         ("How big is the Sun, really", "Space Facts", 2_100_000))
        profile = x.PACK.readers["profile"].read(x.PACK, None, pics.screen("x", "profile", x.BUNDLE), 1, None).rows[0]
        self.assertEqual((profile["followers"], profile["following"]), (2_100_000, 12))


def social_read(pack, app, name, what, n):
    from mobile_agent.packs import social
    return social.post_rows(pics.screen(app, name, pack.bundle))[:n]


if __name__ == "__main__":
    unittest.main()
