"""The YouTube pack: Home, Shorts, Subscriptions, a video and its comments, channels, search and your history."""

import re

from . import core, rows
from .core import Control, Reader, ReadResult, Route, ScreenSpec, Tap, TypeIn, Verified, sig
from .social import WORDED_COUNT

BUNDLE = "com.google.ios.youtube"
TAB = (.86, 1.0)
TOP = (0.0, .2)
TAB_LABEL = r"^(Home|Shorts|Create|Subscriptions|You|Library)\b"
VIDEO = re.compile(r"^(?P<title>.+?) - (?P<channel>[^-]{2,60}?) - (?P<views>[\d.,]+\s?[KMB]? views?)"
                   r"(?: - (?P<when>[^-]+? ago|Streamed [^-]+|Premiered [^-]+))?(?: - (?P<length>[^-]+))?"
                   r"(?: - .*)?$", re.I)
VIDEO_COLUMNS = (("title", "Title", "text"), ("channel", "Channel", "text"), ("views", "Views", "number"),
                 ("when", "When", "text"), ("length", "Length", "text"), ("ad", "Ad", "text"))
SHORT_COLUMNS = (("channel", "Channel", "text"), ("title", "Title", "text"), ("sound", "Sound", "text"),
                 ("likes", "Likes", "number"), ("comments", "Comments", "number"), ("ad", "Ad", "text"))


tab = rows.tab_tap(TAB)


def parse_video(label):
    found = VIDEO.match(core.plain(label))
    if not found:
        # A picture's row: "Title, Channel, 2.1M views, 3 days ago" (core.rows joins the lines).
        parts = [p.strip() for p in core.plain(label).split(", ")]
        views = next((i for i, p in enumerate(parts) if re.match(r"^[\d.,]+\s?[KMB]? views?$", p, re.I)), None)
        if views is None or views < 2:
            return None
        number = re.match(r"([\d.,]+\s?[KMB]?)", parts[views]).group(1).replace(" ", "")
        when = next((p for p in parts[views + 1:] if re.search(r"\bago$|^(Streamed|Premiered)\b", p)), None)
        return {"title": ", ".join(parts[:views - 1]), "channel": parts[views - 1], "views": core.number(number),
                "when": when, "length": None,
                "ad": "Sponsored" if re.search(r"\b(sponsored|ad)\b", label, re.I) else None}
    views = re.match(r"([\d.,]+\s?[KMB]?)", found.group("views"))
    return {"title": found.group("title").strip(), "channel": found.group("channel").strip(),
            "views": core.number(views.group(1).replace(" ", "")) if views else None,
            "when": (found.group("when") or "").strip() or None,
            "length": (found.group("length") or "").strip() or None,
            "ad": "Sponsored" if re.search(r"\b(sponsored|ad)\b", label, re.I) else None}


def video_rows(snapshot):
    rows = []
    for e in core.rows(snapshot):
        if not .1 < core.centre_y(e) < .9 or e.role not in ("Cell", "Button", "Other", "Link"):
            continue
        row = parse_video(e.label)
        if row and row not in rows:
            rows.append(row)
    return rows


def read_videos(kind, noun):
    def read(pack, io, snapshot, n, arg):
        rows, stopped, _ = core.collect(io, snapshot, video_rows, lambda r: (r["channel"], r["title"]), n,
                                        max_swipes=min(n, 20))
        return ReadResult(kind, f"{len(rows)} {noun}", VIDEO_COLUMNS, rows, stopped)
    return read


def read_short(snapshot):
    channel = next((m.group(1) for e in snapshot.elements if .55 < core.centre_y(e) < .92
                    for m in [re.fullmatch(r"(@[\w.-]{2,30})", core.plain(e.label))] if m), None)
    title = next((core.plain(e.label) for e in snapshot.elements if e.role in ("StaticText", "Button")
                  and .6 < core.centre_y(e) < .92 and e.rect[0] < .75 and len(core.plain(e.label)) > 8
                  and not re.match(r"^(@|subscribe|like|dislike|comments?|share|remix)", core.plain(e.label), re.I)),
                 None)
    like = core.find(snapshot, r"^Like this video along with ([\d,.]+\s?[KMB]?)")
    likes = core.number(re.search(r"([\d,.]+\s?[KMB]?)", like.label).group(1).replace(" ", "")) if like else None
    comments = core.find(snapshot, r"^(View )?([\d,.]+[KMB]?) comments?$|^Comments,? [\d,.]+")
    sound = next((core.plain(e.label) for e in snapshot.elements if re.search(r"(original sound|♪|♫|\bmusic\b)",
                                                                            core.plain(e.label), re.I)), None)
    return {"channel": channel, "title": title, "sound": sound, "likes": likes,
            "comments": core.number(re.search(r"(\d[\d,.]*[KMB]?)", comments.label).group(1)) if comments else None,
            "ad": "Sponsored" if core.find(snapshot, r"^(Sponsored|Ad)$") else None}


def read_shorts(pack, io, snapshot, n, arg):
    rows, keys, shown, stuck, stopped = [], set(), 0, 0, ""
    while shown < n:
        row = read_short(snapshot)
        key = (row["channel"], row["title"])
        if row["channel"] or row["title"]:
            if key in keys:
                stuck += 1
                if stuck >= 2:
                    stopped = "the Shorts stopped moving"
                    break
            else:
                stuck, shown = 0, shown + (0 if row["ad"] else 1)
                keys.add(key)
                rows.append(row)
        if shown >= n:
            break
        try:
            snapshot = io.swipe(snapshot, "up")
        except core.Lost as lost:
            stopped = str(lost)
            break
        if pack.recognize(snapshot).screen != "shorts":
            stopped = "it left Shorts"
            break
    return ReadResult("shorts", f"{shown} Shorts", SHORT_COLUMNS, rows, stopped, block="media_list")


class YouTube(core.Pack):
    name, key, prefix, bundle = "YouTube", "youtube", "YT", BUNDLE
    verified = Verified("21.28.3", "2026-10-14", "inferred from YouTube's usual labels; no recorded tree yet")
    tabs = sig(TAB_LABEL, "Button", region=TAB)
    never = re.compile(r"^(subscribe|unsubscribe|dislike|remix|save to playlist|save|download|add to queue|"
                       r"not interested|don't recommend|report|join|thanks)\b", re.I)
    mentions = re.compile(r"\b(youtube|yt|shorts|subscriptions|watch later)\b", re.I)
    sites = ("www.youtube.com", "youtube.com", "m.youtube.com", "youtu.be")

    screens = (
        ScreenSpec("home", "YouTube's Home", (
            sig(r"^Home$", "Button", region=TAB, selected=True, must=True, weight=2),
            sig(r" - [\d.,]+\s?[KMB]? views", weight=2), sig(r"^(All|Music|Gaming|News|Live)$", region=TOP)),
            ocr=(sig(r"^(Music|Gaming|News|Mixes|Podcasts|Recently uploaded|New to you)$", region=(0, .22), weight=2),
                 sig(r"\bviews?\b"), sig(r"^Home$", region=TAB),
                 sig(r"^(Today|Continue watching)$", region=(0, .22), never=True))),
        ScreenSpec("shorts", "Shorts", (
            sig(r"^Shorts$", "Button", region=TAB, selected=True, weight=2),
            sig(r"^Like this video along with", weight=2), sig(r"^(Remix|Dislike this video)\b"),
            sig(r" - [\d.,]+\s?[KMB]? views", never=True)),
            ocr=(sig(rf"^{WORDED_COUNT}$", region=(.35, .88), column=(.84, 1.0), weight=2),
                 sig(r"^Subscribe$", region=(.6, .92), weight=2), sig(r"^(Remix|Dislike|Share)$"),
                 sig(r"\bviews?\b", never=True))),
        ScreenSpec("subscriptions", "your subscriptions", (
            sig(r"^Subscriptions$", "Button", region=TAB, selected=True, must=True, weight=2),
            sig(r" - [\d.,]+\s?[KMB]? views")),
            ocr=(sig(r"^(Today|Continue watching|Unwatched|Posts)$", region=(0, .22), weight=2), sig(r"\bviews?\b"),
                 sig(r"^Subscriptions$", region=TAB))),
        ScreenSpec("you", "You", (
            sig(r"^(You|Library)$", "Button", region=TAB, selected=True, must=True, weight=2),
            sig(r"^(History|Playlists|Watch later|Your videos|Liked videos)$", weight=2)),
            ocr=(sig(r"^(History|Playlists|Watch later|Your videos|Liked videos)$", weight=2),
                 sig(r"^(View all|Switch account)$"))),
        ScreenSpec("video", "a video", (
            sig(r"^(Like this video|Dislike this video|Share|Save|Download|Clip)\b", weight=2),
            sig(r"^(Comments|Comments\.?\s+[\d,.]+)", weight=1), sig(r"\bsubscribers?\b"),
            sig(r"^Like this video along with", "Button", region=(.3, .9), never=True)),
            ocr=(sig(r"\bsubscribers?\b", weight=2), sig(r"^(Subscribe|Subscribed)$"), sig(r"^Comments\b"),
                 sig(r"\bviews?\b"))),
        ScreenSpec("search", "search results", (
            sig(r"^Search YouTube$|^Search$", "SearchField", must=True, weight=2), sig(r" - [\d.,]+\s?[KMB]? views")),
            ocr=(sig(r"^Search YouTube$|^Search$", "SearchField", must=True, weight=2), sig(r"\bviews?\b"))),
    )
    routes = {
        "home": Route("home", "YouTube's Home", ("home",), (tab(r"^Home$", "the Home tab"),)),
        "shorts": Route("shorts", "Shorts", ("shorts",), (tab(r"^Shorts$", "the Shorts tab"),)),
        "subscriptions": Route("subscriptions", "your subscriptions", ("subscriptions",),
                               (tab(r"^Subscriptions$", "the Subscriptions tab"),)),
        "you": Route("you", "You (history, playlists, Watch later)", ("you",),
                     (tab(r"^(You|Library)$", "the You tab"),)),
        "search": Route("search", "search results", ("search",),
                        (Tap(r"^Search$", "Search", role="Button", region=TOP),
                         TypeIn(r"^Search", "the search field", submit=True)), arg="query"),
        "channel": Route("channel", "a channel", ("video", "home"), (), link="https://www.youtube.com/{arg}",
                         arg="user", from_tabs=False),
        "video": Route("video", "the video", ("video", "shorts"), (), link="{arg}", arg="url", from_tabs=False),
        "here": Route("here", "a video", ("video", "shorts"), ()),
    }
    hidden_routes = ("here",)
    controls = {"like": Control(route="here", control=r"^Like this video", role="Button", title="Like"),
                "subscribe": Control(route="here", control=r"^Subscribe\b", role="Button", title="Subscribe")}
    open_aliases = {"library": "you", "history": "you", "watch": "video", "link": "video", "subs": "subscriptions"}
    read_aliases = {"videos": "home", "feed": "home", "short": "shorts", "subs": "subscriptions",
                    "results": "search", "history": "you"}
    read_default = {"home": 10, "shorts": 10, "subscriptions": 10, "search": 10, "you": 10}
    read_titles = {"home": "videos", "shorts": "Shorts", "subscriptions": "videos", "search": "results",
                   "you": "videos", "video": "video"}
    nouns = {"shorts": "Short", "home": "video", "subscriptions": "video", "search": "video", "you": "video"}
    prompts = {
        "OPEN": ("- YT_OPEN where: go to a YouTube screen by code, no taps from you: home | shorts | subscriptions | "
                 "you (history, playlists) | search <words> | channel @name | video <link>; or to an act's control: "
                 "like | subscribe (it names the control and never taps it; you tap it, the person approves). If it "
                 "can't, go on the usual way."),
        "READ": ("- YT_READ what [n]: read YouTube by code from the screen's tree: home n | subscriptions n | "
                 "shorts n (swipes through n Shorts) | search n | you n. Gives each video's title, channel, views, "
                 "when and length, or each Short's channel, title, likes and comments. Opens the screen it needs."),
    }
    mcp_open_hint = "Home, Shorts, Subscriptions, You, search, a channel, a video's link"
    mcp_read_hint = "Home, Subscriptions, Shorts, search results or your history, as rows"

    @property
    def readers(self):
        return {"home": Reader(read_videos("home", "videos"), ("home",), "home"),
                "subscriptions": Reader(read_videos("subscriptions", "videos"), ("subscriptions",), "subscriptions"),
                "shorts": Reader(read_shorts, ("shorts",), "shorts"),
                "search": Reader(read_videos("search", "results"), ("search",), None),
                "you": Reader(read_videos("you", "videos"), ("you",), "you")}


PACK = YouTube()
