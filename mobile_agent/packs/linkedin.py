"""The LinkedIn pack: your feed, messaging, notifications, profiles and search."""

import re

from . import core, rows, social
from .core import Control, PickRow, Reader, Route, ScreenSpec, Tap, TypeIn, Verified, sig

BUNDLE = "com.linkedin.LinkedIn"
TAB = (.86, 1.0)
TOP = (0.0, .2)
TAB_LABEL = r"^(Home|Video|My Network|Post|Notifications|Jobs)\b"
COLUMNS = (("author", "Author", "text"), ("text", "Post", "text"), ("when", "When", "text"),
           ("likes", "Reactions", "number"), ("comments", "Comments", "number"), ("reposts", "Reposts", "number"),
           ("ad", "Ad", "text"))
NOT_A_THREAD = re.compile(r"^(focused|other|unread|my connections|inmail|search messages|compose|new message|"
                          r"messaging)\b", re.I)


tab = rows.tab_tap(TAB)


class LinkedIn(core.Pack):
    name, key, prefix, bundle = "LinkedIn", "linkedin", "LI", BUNDLE
    verified = Verified("2026.0224.0943", "2026-10-14", "inferred from LinkedIn's usual labels; no recorded tree yet")
    tabs = sig(TAB_LABEL, "Button", region=TAB)
    never = re.compile(r"^(connect|accept|ignore|withdraw|endorse|repost|send|post|apply|easy apply|save|"
                       r"follow|unfollow|react|celebrate|support|love|insightful|funny)\b", re.I)
    mentions = re.compile(r"\b(linkedin|linked in)\b", re.I)
    sites = ("www.linkedin.com", "linkedin.com")

    screens = (
        ScreenSpec("home", "your LinkedIn feed", (
            sig(r"^Home$", "Button", region=TAB, selected=True, must=True, weight=2),
            sig(r"\b\d[\d,.]*[KM]? (reactions?|comments?)\b"), sig(r"^(Messaging|Search)", region=TOP)),
            ocr=(sig(r"^Start a post", weight=2), sig(r"\b\d[\d,.]*[KM]? (reactions?|comments?)\b"),
                 sig(r"^(Home|My Network|Notifications|Jobs)$", region=TAB))),
        ScreenSpec("messages", "your messages", (
            sig(r"^Messaging$", "StaticText", region=TOP, must=True, weight=2),
            sig(r"^(Focused|Other|Unread|My Connections|InMail)$"), sig(r"^Search messages")),
            "inferred",
            ocr=(sig(r"^Messaging$", region=TOP, must=True, weight=2),
                 sig(r"^(Focused|Other|Unread|My Connections|InMail)$"), sig(r"^Search messages"))),
        ScreenSpec("thread", "a conversation", (
            sig(r"^Write a message", must=True, weight=2), sig(r"^Back$", "Button", region=TOP)),
            ocr=(sig(r"^Write a message", must=True, weight=2), sig(r"^Back$", "Button", region=TOP))),
        ScreenSpec("notifications", "your notifications", (
            sig(r"^Notifications$", "Button", region=TAB, selected=True, must=True, weight=2),
            sig(r"^(All|Jobs|My posts|Mentions)$", region=TOP)),
            ocr=(sig(r"^Notifications$", region=TOP, must=True, weight=2),
                 sig(r"^(All|Jobs|My posts|Mentions)$", region=(0, .3)))),
        ScreenSpec("profile", "a profile", (
            sig(r"\b(connections|followers)\b", weight=2), sig(r"^(About|Activity|Experience|Education)$"),
            sig(r"^(Connect|Message|Follow|More|Open to)\b", "Button")),
            ocr=(sig(r"\b(connections|followers)\b", weight=2), sig(r"^(About|Activity|Experience|Education)$"),
                 sig(r"^(Connect|Message|Follow|More|Open to)\b"))),
        ScreenSpec("search", "search results", (
            sig(r"^Search", "SearchField", must=True),
            sig(r"^(People|Posts|Companies|Jobs|Groups)$", region=(0, .3), weight=2)),
            ocr=(sig(r"^Search", "SearchField", must=True),
                 sig(r"^(People|Posts|Companies|Jobs|Groups)$", region=(0, .3), weight=2))),
    )
    routes = {
        "home": Route("home", "your LinkedIn feed", ("home",), (tab(r"^Home$", "the Home tab"),)),
        "messages": Route("messages", "your messages", ("messages",),
                          (tab(r"^Home$", "the Home tab"), Tap(r"^Messaging\b", "Messaging", region=TOP))),
        "thread": Route("thread", "a conversation", ("thread",),
                        (tab(r"^Home$", "the Home tab"), Tap(r"^Messaging\b", "Messaging", region=TOP),
                         PickRow("conversation", roles=("Cell", "Button"))), arg="name"),
        "notifications": Route("notifications", "your notifications", ("notifications",),
                               (tab(r"^Notifications$", "the Notifications tab"),)),
        "search": Route("search", "search results", ("search",),
                        (tab(r"^Home$", "the Home tab"), TypeIn(r"^Search", "the search field", submit=True)),
                        arg="query"),
        "user": Route("user", "a profile", ("profile",), (), link="https://www.linkedin.com/in/{arg}", arg="user",
                      from_tabs=False),
        "post": Route("post", "the post", ("home",), (), link="{arg}", arg="url", from_tabs=False),
    }
    hidden_routes = ("user",)
    controls = {"reply": Control(route="thread", control=r"^Write a message", title="message field")}
    open_aliases = {"feed": "home", "dms": "messages", "inbox": "messages", "messaging": "messages",
                    "profile": "user", "chat": "thread", "conversation": "thread", "link": "post"}
    read_aliases = {"feed": "home", "posts": "home", "dms": "messages", "inbox": "messages", "user": "profile"}
    read_default = {"home": 10, "messages": 15, "search": 10}
    read_titles = {"home": "posts", "messages": "messages", "search": "results", "profile": "profile"}
    nouns = {"home": "post", "messages": "message", "search": "result"}
    prompts = {
        "OPEN": ("- LI_OPEN where: go to a LinkedIn screen by code, no taps from you: home | messages | thread "
                 "<name> | notifications | search <words> | user <profile id> | post <link>; or to an act's "
                 "control: reply <name> (it names the control and never taps it; you tap it, the person approves). "
                 "If it can't, go on the usual way."),
        "READ": ("- LI_READ what [n]: read LinkedIn by code from the screen's tree: home n | messages n | search n | "
                 "profile (connections, followers). Gives author, words, time, reactions and comments, or message "
                 "rows. Opens the screen it needs."),
    }
    mcp_open_hint = "your feed, messages, a conversation, notifications, search, a profile, a post's link"
    mcp_read_hint = "your feed, messages, search results or a profile's numbers"

    @property
    def readers(self):
        return {"home": Reader(social.feed_reader("home", "posts", COLUMNS), ("home",), "home"),
                "messages": Reader(social.dm_reader(NOT_A_THREAD), ("messages",), "messages"),
                "search": Reader(social.feed_reader("search", "results", COLUMNS), ("search",), None),
                "profile": Reader(social.profile_reader(("connections", "followers")), ("profile",), None)}


PACK = LinkedIn()
