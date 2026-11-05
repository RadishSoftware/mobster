"""The Reddit pack: your home feed, a post and its comments, communities, chat, your inbox and search."""

import re

from . import core, rows, social
from .core import PickRow, Reader, Route, ScreenSpec, Tap, TypeIn, Verified, sig

BUNDLE = "com.reddit.Reddit"
TAB = (.86, 1.0)
TOP = (0.0, .2)
TAB_LABEL = r"^(Home|Communities|Answers|Create|Chat|Inbox)\b"
COLUMNS = (("place", "Community", "text"), ("handle", "Author", "text"), ("text", "Post", "text"),
           ("when", "When", "text"), ("score", "Score", "number"), ("comments", "Comments", "number"),
           ("ad", "Ad", "text"))
COMMENT_COLUMNS = (("handle", "Author", "text"), ("text", "Comment", "text"), ("when", "When", "text"),
                   ("score", "Score", "number"))


tab = rows.tab_tap(TAB)


class Reddit(core.Pack):
    name, key, prefix, bundle = "Reddit", "reddit", "RD", BUNDLE
    verified = Verified("2025.44.0", "2026-10-14", "inferred from Reddit's usual labels; no recorded tree yet")
    tabs = sig(TAB_LABEL, "Button", region=TAB)
    never = re.compile(r"^(upvote|downvote|join|leave|award|give award|share|crosspost|save|hide|report|block|"
                       r"reply|post|send|follow)\b", re.I)
    mentions = re.compile(r"\b(reddit|subreddits?|r/[A-Za-z0-9_]+|upvotes?)\b", re.I)
    sites = ("www.reddit.com", "reddit.com", "old.reddit.com", "redd.it")

    screens = (
        ScreenSpec("home", "your Reddit home feed", (
            sig(r"^Home$", "Button", region=TAB, selected=True, must=True, weight=2),
            sig(r"\br/[A-Za-z0-9_]{2,}", "Cell", weight=2), sig(r"\b\d[\d,.]*[KM]? (upvotes?|comments?|votes?)\b")),
            ocr=(sig(r"\br/[A-Za-z0-9_]{2,}", weight=2),
                 sig(r"\b\d[\d,.]*[KM]?\s*(upvotes?|comments?|votes?)\b|^\d[\d.,]*[KM]?$"),
                 sig(r"^(Home|Popular|Latest)$", region=TOP), sig(r"^(Join|Joined)$", never=True))),
        ScreenSpec("community", "a community", (
            sig(r"^r/[A-Za-z0-9_]{2,}$", region=TOP, must=True, weight=2), sig(r"^(Join|Joined)$", "Button"),
            sig(r"\b(members|online)\b")),
            ocr=(sig(r"^r/[A-Za-z0-9_]{2,}$", region=TOP, must=True, weight=2), sig(r"^(Join|Joined)$"),
                 sig(r"\b(members|online)\b"))),
        ScreenSpec("post", "a post and its comments", (
            sig(r"^(Add a comment|Join the conversation)", must=True, weight=2), sig(r"^Back$", "Button", region=TOP),
            sig(r"^Sort by|^(Best|Top|New) comments")),
            ocr=(sig(r"^(Add a comment|Join the conversation)", must=True, weight=2),
                 sig(r"^Back$", "Button", region=TOP), sig(r"^Sort by|^(Best|Top|New) comments"))),
        ScreenSpec("chat", "your chats", (
            sig(r"^Chat$", "Button", region=TAB, selected=True, must=True, weight=2),
            sig(r"^(Threads|Requests|Start chat)\b")),
            ocr=(sig(r"^Chat$", region=TOP, must=True, weight=2), sig(r"^(Threads|Requests|Start chat)\b"))),
        ScreenSpec("inbox", "your inbox", (
            sig(r"^Inbox$", "Button", region=TAB, selected=True, must=True, weight=2),
            sig(r"^(Notifications|Messages)$", region=TOP)),
            ocr=(sig(r"^Inbox$", region=TOP, must=True, weight=2),
                 sig(r"^(Notifications|Messages)$", region=(0, .25)))),
        ScreenSpec("search", "search results", (
            sig(r"^Search", "SearchField", must=True),
            sig(r"^(Posts|Communities|Comments|Media|People)$", region=(0, .3), weight=2)),
            ocr=(sig(r"^Search", "SearchField", must=True),
                 sig(r"^(Posts|Communities|Comments|Media|People)$", region=(0, .3), weight=2))),
    )
    routes = {
        "home": Route("home", "your Reddit home feed", ("home",), (tab(r"^Home$", "the Home tab"),)),
        "chat": Route("chat", "your chats", ("chat",), (tab(r"^Chat$", "the Chat tab"),)),
        "inbox": Route("inbox", "your inbox", ("inbox",), (tab(r"^Inbox$", "the Inbox tab"),)),
        "thread": Route("thread", "a chat", ("chat",), (tab(r"^Chat$", "the Chat tab"),
                                                        PickRow("chat", roles=("Cell", "Button"))), arg="name"),
        "community": Route("community", "a community", ("community",), (),
                           link="https://www.reddit.com/r/{arg}", arg="name", from_tabs=False),
        "user": Route("user", "a profile", ("community", "home"), (), link="https://www.reddit.com/user/{arg}",
                      arg="user", from_tabs=False),
        "search": Route("search", "search results", ("search",),
                        (tab(r"^Home$", "the Home tab"), Tap(r"^Search", "Search", region=TOP),
                         TypeIn(r"^Search", "the search field", submit=True)), arg="query"),
        "post": Route("post", "the post", ("post",), (), link="{arg}", arg="url", from_tabs=False),
        "comments": Route("comments", "the comments", ("post",), (), from_tabs=False),
    }
    hidden_routes = ("user", "comments")
    open_aliases = {"feed": "home", "subreddit": "community", "r": "community", "dms": "chat", "messages": "inbox",
                    "link": "post", "chats": "chat"}
    read_aliases = {"feed": "home", "posts": "home", "subreddit": "community", "comment": "comments",
                    "dms": "chat", "chats": "chat"}
    read_default = {"home": 10, "community": 10, "comments": 20, "search": 10, "chat": 15}
    read_titles = {"home": "posts", "community": "posts", "comments": "comments", "search": "results",
                   "chat": "chats"}
    nouns = {"home": "post", "community": "post", "comments": "comment", "search": "result", "chat": "chat"}
    prompts = {
        "OPEN": ("- RD_OPEN where: go to a Reddit screen by code, no taps from you: home | community <name> | chat "
                 "| inbox | thread <name> | search <words> | post <link>. If it can't, go on the usual way. It never "
                 "votes, joins, comments or posts: those are your taps, and the person approves them."),
        "READ": ("- RD_READ what [n]: read Reddit by code from the screen's tree: home n | community n (the one "
                 "open) | comments n (on the post open) | search n | chat n. Gives community, author, title, time, "
                 "score and comments. Opens the screen it needs."),
    }
    mcp_open_hint = "your home feed, a community, chats, your inbox, search, a post's link"
    mcp_read_hint = "your home feed, a community, a post's comments, search results or your chats"

    @property
    def readers(self):
        not_a_chat = re.compile(r"^(threads|requests|start chat|new chat|search)\b", re.I)
        return {"home": Reader(social.feed_reader("home", "posts", COLUMNS), ("home",), "home"),
                "community": Reader(social.feed_reader("community", "posts", COLUMNS), ("community",), None),
                "comments": Reader(social.feed_reader("comments", "comments", COMMENT_COLUMNS, roles=("Cell", "Other")),
                             ("post",), None),
                "search": Reader(social.feed_reader("search", "results", COLUMNS), ("search",), None),
                "chat": Reader(social.dm_reader(not_a_chat), ("chat",), "chat")}


PACK = Reddit()
