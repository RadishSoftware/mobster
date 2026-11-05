"""The X pack (formerly Twitter): the timeline, a post and its replies, profiles, search, notifications and messages."""

import re

from . import core, rows, social
from .core import Control, PickRow, Reader, Route, ScreenSpec, TypeIn, Verified, sig

BUNDLE = "com.atebits.Tweetie2"
TAB = (.86, 1.0)
TOP = (0.0, .2)
TAB_LABEL = r"^(Home|Search( and Explore)?|Explore|Grok|Communities|Notifications|Messages|Direct Messages)\b"
COLUMNS = (("author", "Author", "text"), ("handle", "Handle", "text"), ("text", "Post", "text"),
           ("when", "When", "text"), ("replies", "Replies", "number"), ("reposts", "Reposts", "number"),
           ("likes", "Likes", "number"), ("views", "Views", "number"), ("ad", "Ad", "text"))
NOT_A_POST = re.compile(r"^(show more|show this thread|who to follow|trending|what's happening|subscribe to premium|"
                        r"relevant people|discover more)\b", re.I)


tab = rows.tab_tap(TAB)


class X(core.Pack):
    name, key, prefix, bundle = "X", "x", "X", BUNDLE
    verified = Verified("12.21", "2026-10-14", "inferred from X's usual labels; no recorded tree yet")
    tabs = sig(TAB_LABEL, "Button", region=TAB)
    never = re.compile(r"^(repost|undo repost|quote|bookmark|remove bookmark|post|reply|send|unfollow|mute|block|"
                       r"report|not interested)\b", re.I)
    mentions = re.compile(r"\b(twitter|tweets?|x\.com|on x|my x|x timeline|x feed|x profile|x dms?)\b", re.I)
    sites = ("x.com", "twitter.com", "www.x.com", "mobile.twitter.com")

    screens = (
        ScreenSpec("home", "your timeline", (
            sig(r"^Home$", "Button", region=TAB, selected=True, must=True, weight=2),
            sig(r"^(For you|Following)$", region=TOP), sig(r"\b\d[\d,.]*[KMB]? (likes?|replies|reposts?)\b", "Cell")),
            ocr=(sig(r"^For you$", region=TOP, weight=2), sig(r"^Following$", region=TOP),
                 sig(r"@\w{2,}\s*[·•,]\s*\d+[smhd]\b", weight=2), sig(r"^(Post|Tweet)$", region=TOP, never=True))),
        ScreenSpec("post", "a post", (
            sig(r"^(Post|Tweet)$", "StaticText", region=TOP, must=True, weight=2), sig(r"^Back$", "Button", region=TOP),
            sig(r"^(Post your reply|Tweet your reply)", weight=2)),
            ocr=(sig(r"^(Post|Tweet)$", region=TOP, must=True, weight=2), sig(r"^(Post your reply|Tweet your reply)"),
                 sig(r"\bViews?\b"))),
        ScreenSpec("profile", "a profile", (
            sig(r"\bFollowers\b", weight=2), sig(r"\bFollowing\b"), sig(r"^(Posts|Replies|Highlights|Media|Likes)$"),
            sig(r"^(Follow|Following|Edit profile)$", "Button"),
            sig(r"^Home$", "Button", region=TAB, selected=True, never=True)),
            ocr=(sig(r"\bFollowers$", weight=2), sig(r"\bFollowing$"), sig(r"^(Posts|Replies|Highlights|Media|Likes)$"),
                 sig(r"^(Follow|Following|Edit profile)$"))),
        ScreenSpec("search", "search", (
            sig(r"^(Search and Explore|Search)$", "Button", region=TAB, selected=True, weight=2),
            sig(r"^Search", "SearchField", must=True), sig(r"^(Top|Latest|People|Media|Lists)$", region=(0, .3))),
            ocr=(sig(r"^Search", "SearchField", must=True),
                 sig(r"^(Top|Latest|People|Media|Lists|Trending|For you)$", region=(0, .3), weight=2))),
        ScreenSpec("notifications", "your notifications", (
            sig(r"^Notifications$", "Button", region=TAB, selected=True, must=True, weight=2),
            sig(r"^(All|Verified|Mentions)$", region=TOP)),
            ocr=(sig(r"^Notifications$", region=TOP, must=True, weight=2),
                 sig(r"^(All|Verified|Mentions)$", region=TOP))),
        ScreenSpec("messages", "your messages", (
            sig(r"^(Messages|Direct Messages)$", "Button", region=TAB, selected=True, must=True, weight=2),
            sig(r"^(Message requests|New message|Search Direct Messages)", weight=1)),
            ocr=(sig(r"^Messages$", region=TOP, must=True, weight=2),
                 sig(r"^(Message requests|Search Direct Messages)"))),
        ScreenSpec("thread", "a conversation", (
            sig(r"^(Start a message|Start a new message|Message)\W*$", must=True, weight=2),
            sig(r"^Back$", "Button", region=TOP), sig(TAB_LABEL, "Button", region=TAB, never=True)),
            ocr=(sig(r"^(Start a message|Start a new message)", must=True, weight=2),
                 sig(r"^Back$", "Button", region=TOP))),
    )
    routes = {
        "home": Route("home", "your timeline", ("home",), (tab(r"^Home$", "the Home tab"),)),
        "search": Route("search", "search", ("search",), (tab(r"^(Search( and Explore)?|Explore)$", "the Search tab"),
                                                          TypeIn(r"^Search", "the search field", submit=True)),
                        arg="query"),
        "notifications": Route("notifications", "your notifications", ("notifications",),
                               (tab(r"^Notifications$", "the Notifications tab"),)),
        "messages": Route("messages", "your messages", ("messages",),
                          (tab(r"^(Messages|Direct Messages)$", "the Messages tab"),)),
        "thread": Route("thread", "a conversation", ("thread",),
                        (tab(r"^(Messages|Direct Messages)$", "the Messages tab"),
                         PickRow("conversation", roles=("Cell", "Button")),), arg="name"),
        "user": Route("user", "a profile", ("profile",),
                      (tab(r"^(Search( and Explore)?|Explore)$", "the Search tab"),
                       TypeIn(r"^Search", "the search field"),
                       PickRow("account", roles=("Cell", "Button", "StaticText"), template=(r"@{arg}\b",))),
                      link="https://x.com/{arg}", arg="user"),
        "post": Route("post", "the post", ("post",), (), link="{arg}", arg="url", from_tabs=False),
        "here": Route("here", "a post", ("home", "post", "profile"), (tab(r"^Home$", "the Home tab"),)),
    }
    hidden_routes = ("user", "here")
    controls = {
        "like": Control(route="here", control=r"^Like\b", role="Button", title="Like"),
        "follow": Control(route="user", control=r"^Follow\b", role="Button", region=(0, .6), title="Follow"),
        "reply": Control(route="post", control=r"^(Post your reply|Tweet your reply|Reply)\b", title="reply field"),
    }
    open_aliases = {"timeline": "home", "feed": "home", "profile": "user", "dms": "messages", "dm": "messages",
                    "inbox": "messages", "chat": "thread", "conversation": "thread", "link": "post"}
    read_aliases = {"timeline": "home", "feed": "home", "posts": "home", "tweets": "home", "dms": "messages",
                    "dm": "messages", "user": "profile", "stats": "profile", "replies": "post"}
    read_default = {"home": 10, "messages": 15, "post": 10, "search": 10}
    read_titles = {"home": "posts", "messages": "messages", "post": "replies", "search": "results",
                   "profile": "profile"}
    nouns = {"home": "post", "post": "reply", "search": "result", "messages": "message"}
    prompts = {
        "OPEN": ("- X_OPEN where: go to an X screen by code, no taps from you: home | search <words> | "
                 "notifications | messages | thread <name> | user @handle | post <link>; or to an act's control: "
                 "like | follow @handle | reply <link> (it names the control and never taps it; you tap it, the "
                 "person approves). If it can't, go on the usual way."),
        "READ": ("- X_READ what [n]: read X by code from the screen's tree: home n (your timeline) | post (a post "
                 "and its replies) | search n | messages n | profile @handle (followers, following). Gives author, "
                 "handle, words, time, replies, reposts, likes, views and Promoted flags. Opens the screen it needs."),
    }
    mcp_open_hint = "your timeline, search, notifications, messages, a profile, a post's link"
    mcp_read_hint = "your timeline, a post and its replies, search results, messages or a profile's numbers"

    @property
    def readers(self):
        posts = social.feed_reader("home", "posts", COLUMNS, skip=NOT_A_POST)
        return {"home": Reader(posts, ("home",), "home"),
                "post": Reader(social.feed_reader("post", "posts and replies", COLUMNS, skip=NOT_A_POST), ("post",),
                               None),
                "search": Reader(social.feed_reader("search", "results", COLUMNS, skip=NOT_A_POST), ("search",), None),
                "messages": Reader(social.dm_reader(re.compile(r"^(message requests|new message|search)\b", re.I)),
                             ("messages",), "messages"),
                "profile": Reader(social.profile_reader(("followers", "following")), ("profile",), "user")}


PACK = X()
