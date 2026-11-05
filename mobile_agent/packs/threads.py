"""The Threads pack: your feed, a thread and its replies, profiles, search and activity."""

import re

from . import core, rows, social
from .core import Control, PickRow, Reader, Route, ScreenSpec, TypeIn, Verified, sig

BUNDLE = "com.burbn.barcelona"
TAB = (.86, 1.0)
TOP = (0.0, .2)
TAB_LABEL = r"^(Home|Feed|Search|Create|New thread|Activity|Notifications|Profile)\b"
COLUMNS = (("author", "Author", "text"), ("text", "Post", "text"), ("when", "When", "text"),
           ("likes", "Likes", "number"), ("replies", "Replies", "number"), ("reposts", "Reposts", "number"),
           ("ad", "Ad", "text"))


tab = rows.tab_tap(TAB)


class Threads(core.Pack):
    name, key, prefix, bundle = "Threads", "threads", "TH", BUNDLE
    verified = Verified("359.1.0", "2026-10-14", "inferred from Threads' usual labels; no recorded tree yet")
    tabs = sig(TAB_LABEL, "Button", region=TAB)
    never = re.compile(r"^(repost|quote|post|reply|send|share|unfollow|mute|block|report|hide)\b", re.I)
    mentions = re.compile(r"\b(threads app|on threads|my threads|threads feed|threads profile)\b", re.I)
    sites = ("www.threads.net", "threads.net", "www.threads.com", "threads.com")

    screens = (
        ScreenSpec("home", "your Threads feed", (
            sig(r"^(Home|Feed)$", "Button", region=TAB, selected=True, must=True, weight=2),
            sig(r"^(For you|Following)$", region=TOP), sig(r"\b\d[\d,.]*[KMB]? (likes?|replies)\b")),
            ocr=(sig(r"^(For you|Following)$", region=TOP, weight=2), sig(r"^\w[\w.]{1,29}\s+\d+[smhdw]$"),
                 sig(r"^What's new\?"), sig(r"^Thread$", region=TOP, never=True))),
        ScreenSpec("post", "a thread", (
            sig(r"^Thread$", "StaticText", region=TOP, must=True, weight=2), sig(r"^Back$", "Button", region=TOP),
            sig(r"^Reply to ")),
            ocr=(sig(r"^Thread$", region=TOP, must=True, weight=2), sig(r"^Reply to "))),
        ScreenSpec("profile", "a profile", (
            sig(r"\bfollowers\b", weight=2), sig(r"^(Threads|Replies|Media|Reposts)$"),
            sig(r"^(Follow|Following|Edit profile|Mention)$", "Button")),
            ocr=(sig(r"\bfollowers\b", weight=2), sig(r"^(Threads|Replies|Media|Reposts)$"),
                 sig(r"^(Follow|Following|Edit profile|Mention)$"))),
        ScreenSpec("search", "search", (
            sig(r"^Search$", "Button", region=TAB, selected=True, weight=2), sig(r"^Search", "SearchField", must=True)),
            ocr=(sig(r"^Search", "SearchField", must=True, weight=2), sig(r"^(Follow|Following)$"))),
        ScreenSpec("activity", "your activity", (
            sig(r"^(Activity|Notifications)$", "Button", region=TAB, selected=True, must=True, weight=2),
            sig(r"^(All|Follows|Replies|Mentions|Quotes|Reposts|Verified)$", region=TOP)),
            ocr=(sig(r"^Activity$", region=TOP, must=True, weight=2),
                 sig(r"^(All|Follows|Replies|Mentions|Quotes|Reposts|Verified)$", region=(0, .3)))),
    )
    routes = {
        "home": Route("home", "your Threads feed", ("home",), (tab(r"^(Home|Feed)$", "the Home tab"),)),
        "activity": Route("activity", "your activity", ("activity",),
                          (tab(r"^(Activity|Notifications)$", "the Activity tab"),)),
        "profile": Route("profile", "your profile", ("profile",), (tab(r"^Profile$", "the Profile tab"),)),
        "search": Route("search", "search", ("search",), (tab(r"^Search$", "the Search tab"),
                                                          TypeIn(r"^Search", "the search field", submit=True)),
                        arg="query"),
        "user": Route("user", "a profile", ("profile",),
                      (tab(r"^Search$", "the Search tab"), TypeIn(r"^Search", "the search field"),
                       PickRow("account", roles=("Cell", "Button", "StaticText"), template=(r"^@?{arg}\b",))),
                      link="https://www.threads.net/@{arg}", arg="user"),
        "post": Route("post", "the thread", ("post",), (), link="{arg}", arg="url", from_tabs=False),
        "here": Route("here", "a post", ("home", "post", "profile"), (tab(r"^(Home|Feed)$", "the Home tab"),)),
    }
    route_with_arg = {"profile": "user"}
    hidden_routes = ("user", "here")
    controls = {
        "like": Control(route="here", control=r"^Like\b", role="Button", title="Like"),
        "follow": Control(route="user", control=r"^Follow\b", role="Button", region=(0, .6), title="Follow"),
    }
    open_aliases = {"feed": "home", "notifications": "activity", "link": "post"}
    read_aliases = {"feed": "home", "posts": "home", "user": "profile", "stats": "profile", "replies": "post"}
    read_default = {"home": 10, "post": 10, "search": 10}
    read_titles = {"home": "posts", "post": "replies", "search": "results", "profile": "profile"}
    nouns = {"home": "post", "post": "reply", "search": "result"}
    prompts = {
        "OPEN": ("- TH_OPEN where: go to a Threads screen by code, no taps from you: home | activity | profile "
                 "[@user] | search <words> | post <link>; or to an act's control: like | follow @user (it names the "
                 "control and never taps it; you tap it, the person approves). If it can't, go on the usual way."),
        "READ": ("- TH_READ what [n]: read Threads by code from the screen's tree: home n | post (a thread and its "
                 "replies) | search n | profile [@user] (followers). Gives author, words, time, likes, replies and "
                 "reposts. Opens the screen it needs."),
    }
    mcp_open_hint = "your feed, activity, a profile, search, a post's link"
    mcp_read_hint = "your feed, a thread and its replies, search results or a profile's followers"

    @property
    def readers(self):
        return {"home": Reader(social.feed_reader("home", "posts", COLUMNS), ("home",), "home"),
                "post": Reader(social.feed_reader("post", "posts and replies", COLUMNS), ("post",), None),
                "search": Reader(social.feed_reader("search", "results", COLUMNS), ("search",), None),
                "profile": Reader(social.profile_reader(("followers",)), ("profile",), "profile")}


PACK = Threads()
