"""The TikTok pack: TikTok's screens, how to reach them and how to read them, from its accessibility tree."""

import functools
import re

from . import core, insights, rows, social
from .core import Control, PickRow, Reader, ReadResult, Route, ScreenSpec, Tap, TypeIn, Verified, sig
from .social import AD_CTA, COUNT
from .social import parse_inbox_row as _inbox_row

BUNDLE = "com.zhiliaoapp.musically"
TAB = (.85, 1.0)
TOP = (0.0, .2)
CONTENT = (.1, .88)
HANDLE = r"[A-Za-z0-9._]{2,24}"
TAB_LABEL = r"^(Home|Friends|Shop|Discover|Create|\+|Inbox|Profile|Me)\b"
SOUND = re.compile(r"(♬|♫|♪|original sound|\bsound\b|\baudio\b)", re.I)
RIGHT = (.78, 1.0)
CHROME = re.compile(r"^(like|liked|comment|comments|favorites|add to favorites|share|follow|following|for you|live|"
                    r"search|friends|inbox|profile|home|shop|explore|sponsored|ad|see more|more|see translation|"
                    rf"{COUNT}|{COUNT} (likes?|comments?|favorites?|shares?|views?)|@?{HANDLE}|"
                    r"video progress.*|.*(♬|♫).*)$", re.I)


tab = rows.tab_tap(TAB)


class TikTok(core.Pack):
    name, key, prefix, bundle = "TikTok", "tiktok", "TT", BUNDLE
    verified = Verified("42.2.0", "2026-10-14", "inferred from TikTok's usual labels; no recorded tree yet")
    tabs = sig(TAB_LABEL, "Button", region=TAB)
    never = re.compile(r"^(add to favorites|remove from favorites|repost|stitch|duet|send|send to|not interested|"
                       r"report|block|use this sound|add to story|post|post comment|like comment)\b", re.I)
    mentions = re.compile(r"\b(tiktoks?|tik tok|for you page|fyp)\b", re.I)
    photos_notice = social.LIMITED_PHOTOS
    sites = ("www.tiktok.com", "tiktok.com", "vm.tiktok.com")

    screens = (
        ScreenSpec("for_you", "For You", (
            sig(r"^For You$", region=TOP, selected=True, weight=2), sig(r"^Home$", "Button", region=TAB, selected=True),
            sig(r"^(Like|Liked)\b", "Button", region=(.3, .85), weight=1),
            sig(r"^Comments?\b", "Button", region=(.3, .85)),
            sig(r"^(Following|LIVE|Friends)$", region=TOP), sig(SOUND.pattern, field="text"),
            sig(r"^Comments$", "StaticText", region=(.2, .6), never=True)), "inferred",
            ocr=(sig(r"^For You$", region=TOP, weight=2), sig(r"^(Following|LIVE|Friends)$", region=TOP),
                     sig(rf"^{COUNT}$", region=(.35, .88), column=RIGHT, weight=2), sig(SOUND.pattern),
                     sig(r"^(Home|Inbox|Profile)$", region=TAB), sig(rf"^{COUNT}\s+comments?$", never=True))),
        ScreenSpec("following", "Following", (
            sig(r"^Following$", region=TOP, selected=True, must=True, weight=2),
            sig(r"^(Like|Liked)\b", "Button", region=(.3, .85)), sig(r"^Home$", "Button", region=TAB, selected=True)),
            "inferred",
            ocr=(sig(r"^Following$", region=TOP, must=True, weight=2),
                       sig(rf"^{COUNT}$", region=(.35, .88), column=RIGHT, weight=2),
                       sig(r"^For You$", region=TOP, never=True))),
        ScreenSpec("video", "a video", (
            sig(r"^(Like|Liked)\b", "Button", region=(.3, .9), must=True, weight=2),
            sig(r"^Back$", "Button", region=TOP),
            sig(r"^Comments?\b", "Button"), sig(SOUND.pattern, field="text"),
            sig(r"^For You$", region=TOP, never=True), sig(TAB_LABEL, "Button", region=TAB, never=True)), "inferred",
            ocr=(sig(rf"^{COUNT}$", region=(.35, .88), column=RIGHT, must=True, weight=2),
                   sig(r"^Back$", "Button", region=TOP), sig(SOUND.pattern),
                   sig(r"^(For You|Following)$", region=TOP, never=True),
                   sig(r"^(Home|Inbox)$", region=TAB, never=True))),
        ScreenSpec("comments", "the comments", (
            sig(rf"^{COUNT}\s+comments?$|^Comments$", "StaticText", region=(.15, .6), must=True, weight=2),
            sig(r"^Add comment", weight=2), sig(r"^Reply$", "Button"), sig(r"^(Close|Dismiss)$", "Button")), "inferred",
            ocr=(sig(rf"^{COUNT}\s+comments?$|^Comments$", region=(.15, .6), must=True, weight=2),
                      sig(r"^Add comment"), sig(r"^Reply$"), sig(r"^View \d+ more"))),
        ScreenSpec("inbox", "your inbox", (
            sig(r"^Inbox$", "Button", region=TAB, selected=True, must=True, weight=2),
            sig(r"^(Activity|New followers|System notifications|Messages)\b", weight=2),
            sig(r"^(Unread, )?.+, .+, .+", "Cell")), "inferred",
            ocr=(sig(r"^Inbox$", region=TOP, weight=2),
                 sig(r"^(Activity|New followers|System notifications)\b", weight=2),
                 sig(r"^Inbox$", region=TAB), sig(r"·\s*\d+[smhdw]$|\b\d+[smhdw] ago$"))),
        ScreenSpec("dm_thread", "a message thread", (
            sig(r"^(Send a message|Message)\W*$", must=True, weight=2), sig(r"^Back$", "Button", region=TOP),
            sig(r"^(Emoji|Sticker|Photos|Voice message)$"), sig(TAB_LABEL, "Button", region=TAB, never=True)),
            "inferred",
            ocr=(sig(r"^(Send a message|Message)\W*$", must=True, weight=2), sig(r"^Back$", "Button", region=TOP),
                       sig(r"^Active (now|\d)", region=TOP), sig(r"^(Inbox|Home)$", region=TAB, never=True))),
        ScreenSpec("profile", "a profile", (
            sig(rf"^{COUNT}\s*Following$|^Following$", weight=2), sig(rf"^{COUNT}\s*Followers$|^Followers$", weight=2),
            sig(rf"^{COUNT}\s*Likes$|^Likes$"),
            sig(r"^(Edit profile|Share profile|Follow|Following|Follow back|Message|Friends)$", "Button"),
            sig(r"^For You$", region=TOP, selected=True, never=True)), "inferred",
            ocr=(sig(r"^Following$", region=(.2, .6), weight=2), sig(r"^Followers$", weight=2), sig(r"^Likes$"),
                     sig(r"^(Edit profile|Share profile|Follow|Message|Follow back)$"),
                     sig(r"^For You$", region=TOP, never=True))),
        ScreenSpec("favorites", "your favorites", (
            sig(r"^Favorites$", selected=True, must=True, weight=2),
            sig(r"^(Posts|Videos|Sounds|Collections|Effects)$"), sig(rf"^{COUNT}$", "StaticText")), "inferred",
            ocr=(sig(r"^(Favorites|Posts|Videos|Sounds|Collections)$", region=(.4, .6), must=True, weight=2),
                       sig(r"^Followers$", region=(.2, .45)), sig(rf"^{COUNT}( views?)?$", region=(.5, .9)))),
        ScreenSpec("insights", "your Analytics overview", (
            sig(r"^(Analytics|Overview)$", "StaticText", region=TOP, weight=2),
            sig(r"^(Video views|Profile views|Followers|Likes|Comments|Shares)\b", weight=2),
            sig(insights.PERIOD.pattern), sig(r"^Edit profile$", "Button", never=True)), "inferred",
            ocr=(sig(r"^(Analytics|Overview)$", region=TOP, weight=2),
                 sig(r"^(Video views|Profile views)$", weight=2))),
        ScreenSpec("search", "search results", (
            sig(r"^Search$", "SearchField", must=True),
            sig(r"^(Top|Users|Videos|Sounds|LIVE|Places|Hashtags)$", weight=2),
            sig(rf" {COUNT} followers$|^{COUNT} followers$", field="text")), "inferred",
            ocr=(sig(r"^(Top|Users|Videos|Sounds|LIVE|Places|Hashtags)$", region=(0, .2), weight=2),
                    sig(r"^Search$|^Cancel$", region=(0, .15)), sig(rf"{COUNT} followers$"))),
        ScreenSpec("composer", "the camera", (
            sig(r"^(Upload|Templates|Text|Effects|Flip|Speed|Filters|Timer|Add sound)$", weight=2),
            sig(r"^(Record|Capture|Start recording)\b"), sig(r"^(Close|Cancel)$", "Button")), "inferred",
            ocr=(sig(r"^(Upload|Templates|Text|Effects|Flip|Speed|Filters|Timer|Add sound)$", weight=2),
                      sig(r"^(Close|Cancel)$"))),
    )

    routes = {
        "foryou": Route("foryou", "For You", ("for_you",),
                        (tab(r"^Home$", "the Home tab"), Tap(r"^For You$", "For You", region=TOP, optional=True))),
        "following": Route("following", "Following", ("following",),
                           (tab(r"^Home$", "the Home tab"), Tap(r"^Following$", "Following", region=TOP))),
        "inbox": Route("inbox", "your inbox", ("inbox",), (tab(r"^Inbox\b.*$", "the Inbox tab"),)),
        "thread": Route("thread", "a message thread", ("dm_thread",),
                        (tab(r"^Inbox\b.*$", "the Inbox tab"),
                         PickRow("conversation", roles=("Cell", "Button", "Other"), region=(.15, .88))), arg="name"),
        "profile": Route("profile", "your profile", ("profile",), (tab(r"^(Profile|Me)$", "the Profile tab"),)),
        "insights": Route("insights", "your Analytics overview", ("insights",),
                          (tab(r"^(Profile|Me)$", "the Profile tab"),
                           Tap(r"^(Profile menu|Menu)$", "the menu", role="Button", region=TOP, optional=True),
                           Tap(r"^(TikTok Studio|Creator tools)$", "TikTok Studio", opens=True, optional=True, wait=.4),
                           Tap(r"^Analytics$", "Analytics", opens=True, wait=.4))),
        "user": Route("user", "a profile", ("profile",),
                      (tab(r"^(Discover|Friends|Home)$", "the Home tab"), Tap(r"^Search$", "Search", region=TOP),
                       TypeIn(r"^Search$", "the search field", submit=True),
                       Tap(r"^Users$", "Users", optional=True),
                       PickRow("account", roles=("Cell", "Button", "StaticText", "Other"), region=(.15, .88),
                               template=(r"^@?{arg}$", r"^@?{arg}\b"))),
                      link="https://www.tiktok.com/@{arg}", arg="user"),
        "favorites": Route("favorites", "your favorites", ("favorites",),
                           (tab(r"^(Profile|Me)$", "the Profile tab"),
                            # The profile's Favorites tab, under its numbers: never a video's Favorites (a save).
                            Tap(r"^Favorites$", "the Favorites tab", region=(.3, .6), opens=True, wait=.3))),
        "search": Route("search", "search results", ("search",),
                        (tab(r"^Home$", "the Home tab"), Tap(r"^Search$", "Search", region=TOP),
                         TypeIn(r"^Search$", "the search field", submit=True)), arg="query"),
        "video": Route("video", "the video", ("video", "for_you"), (), link="{arg}", arg="url", from_tabs=False),
        "comments": Route("comments", "the comments", ("comments",),
                          (Tap(r"^Comments?$", "Comment", role="Button", opens=True, wait=.3),), from_tabs=False),
        "compose": Route("compose", "the camera", ("composer",),
                         (Tap(r"^(Create|\+|Post)$", "the create button", role="Button", region=TAB, opens=True,
                              wait=.5),)),
        "here": Route("here", "a video", ("for_you", "following", "video"), (tab(r"^Home$", "the Home tab"),)),
    }
    route_with_arg = {"profile": "user"}
    hidden_routes = ("user", "here")

    controls = {
        "like": Control(route="here", control=r"^Like\b", role="Button", title="Like"),
        "follow": Control(route="user", control=r"^Follow( back)?$", role="Button", region=(0, .7), title="Follow"),
        "comment": Control(route="comments", control=r"^Add comment", title="comment field"),
        "reply": Control(route="thread", control=r"^(Send a message|Message)\W*$", title="message field"),
        "compose": Control(route="compose", control=r"^(Record|Capture|Start recording|Upload)\b", title="Record"),
    }
    open_aliases = {"fyp": "foryou", "for": "foryou", "feed": "foryou", "home": "foryou", "dms": "inbox",
                    "dm": "inbox", "messages": "inbox", "saved": "favorites", "favourites": "favorites",
                    "bookmarks": "favorites", "camera": "compose", "create": "compose", "post": "compose",
                    "chat": "thread", "conversation": "thread", "link": "video"}
    read_aliases = {"tiktoks": "videos", "tiktok": "videos", "video": "videos", "fyp": "videos", "feed": "videos",
                    "foryou": "videos", "messages": "dms", "dm": "dms", "inbox": "dms", "comment": "comments",
                    "chat": "thread", "saved": "favorites", "favourites": "favorites", "user": "profile",
                    "stats": "profile", "analytics": "insights"}
    read_default = {"videos": 10, "dms": 15, "thread": 20, "comments": 20, "favorites": 10}
    read_titles = {"videos": "TikToks", "dms": "messages", "thread": "messages", "comments": "comments",
                   "favorites": "favorites", "profile": "profile", "insights": "insights", "post": "video"}
    nouns = {"videos": "TikTok", "favorites": "favorite", "dms": "message", "thread": "message", "comments": "comment",
             "post": "video"}
    prompts = {
        "OPEN": ("- TT_OPEN where: go to a TikTok screen by code, no taps from you: foryou | following | inbox | "
                 "thread <name> | favorites | profile [@user] | search <words> | video <link> | comments; or to an "
                 "act's control: like | follow @user | comment | reply <name> | compose (it names the control and "
                 "never taps it; you tap it, the person approves). If it can't, go on the usual way."),
        "READ": ("- TT_READ what [n]: read TikTok by code from the screen's tree, no screenshots: videos n (swipes "
                 "through n TikToks) | dms n | thread [name] | comments n | favorites n | profile [@user] | insights "
                 "(your Analytics overview) | post. "
                 "Gives creator, caption, sound, likes, comments, saves, shares and ad flags, or message rows. Opens "
                 "the screen it needs."),
    }
    mcp_open_hint = "For You, Following, the inbox, a thread, favorites, a profile, search, a video's link"
    mcp_read_hint = "TikToks, messages, a thread, comments, favorites, a profile or insights"

    @property
    def readers(self):
        return {"videos": Reader(read_videos, ("for_you", "following", "video"), "foryou"),
                "dms": Reader(read_dms, ("inbox",), "inbox"), "thread": Reader(read_thread, ("dm_thread",), "thread"),
                "comments": Reader(social.comments_reader(parse_comments), ("comments",), "comments"),
                "favorites": Reader(read_favorites, ("favorites",), "favorites"),
                "profile": Reader(read_profile, ("profile",), "profile"),
                "insights": Reader(insights.reader, ("insights",), "insights"),
                "post": Reader(read_post, ("for_you", "following", "video"))}

    def read(self, io, what, n=10, arg=None):
        if what == "insights":
            return insights.read(self, io, what, n, arg)
        return super().read(io, what, n, arg)


def _right_counts(snapshot):
    """The counts in the right-hand column, top to bottom: like, comment, favorites, share."""
    controls = [("likes", r"^(Like|Liked)\b"), ("comments", r"^Comments?\b"),
                ("saves", r"^(Favorites|Add to Favorites|Save|Bookmark)\b"), ("shares", r"^Share\b")]
    numbers = sorted((e for e in snapshot.elements if e.rect[0] >= RIGHT[0] and re.fullmatch(COUNT, core.plain(e.label)
                                                                                              or "")),
                     key=core.centre_y)
    out = {}
    for key, rx in controls:
        control = core.find(snapshot, rx, region=(.25, .9))
        if control is None or control.rect[0] < RIGHT[0] - .1:
            continue
        inline = re.search(rf"({COUNT})", core.plain(control.label) + " " + core.plain(control.value))
        if inline:
            out[key] = core.number(inline.group(1))
            continue
        below = [e for e in numbers if 0 <= core.centre_y(e) - core.centre_y(control) < .07]
        if below:
            out[key] = core.number(below[0].label)
    if not out and core.image_only(snapshot):
        # A picture whose icons weren't named: the column's numbers, top to bottom.
        column = [e for e in numbers if .3 < core.centre_y(e) < .9]
        for (key, _rx), e in zip(controls, column):
            out[key] = core.number(core.plain(e.label))
    return out


def read_video(snapshot):
    """One TikTok on screen as a row: creator, caption, sound, counts, ad."""
    author = None
    for e in snapshot.elements:
        if e.rect[0] < .7 and .55 < core.centre_y(e) < .92:
            found = re.fullmatch(rf"@?({HANDLE})", core.plain(e.label))
            if found and e.role in ("Button", "StaticText", "Link", "Other") and core.plain(e.label).lower() not in (
                    "follow", "following", "sponsored", "live", "more"):
                author = found.group(1)
                break
    if author is None:
        for e in core.find_all(snapshot, rf"^(?:Profile picture of |@)?({HANDLE})'?s? (?:profile|avatar)",
                               region=(.2, .9)):
            author = re.match(rf"^(?:Profile picture of |@)?({HANDLE})", e.label).group(1)
            break
    caption = None
    for e in snapshot.elements:
        if e.role not in ("StaticText", "Other", "Button") or e.rect[0] > .7 or not .6 < core.centre_y(e) < .92:
            continue
        text = core.plain(e.label)
        if not text or CHROME.match(text) or (author and text.lstrip("@") == author) or SOUND.search(text):
            continue
        if len(text) >= 4:
            caption = text
            break
    sound = next((core.plain(line) for line in str(snapshot.text or "").split("\n") if SOUND.search(line)
                  and len(line) < 120), None)
    if sound:
        sound = re.sub(r"^[♬♫♪]\s*", "", sound)
    ad = None
    for e in snapshot.elements:
        label = core.plain(e.label)
        if re.fullmatch(r"(sponsored|ad|promoted)", label, re.I) or e.role == "Button" and AD_CTA.match(label):
            ad = label
            break
    following = False if author and core.find(snapshot, r"^Follow$|^Follow " + re.escape(author), role="Button") \
        else None
    return {"author": author, "caption": caption, "sound": sound, **_right_counts(snapshot), "ad": ad,
            "following": following}


VIDEO_COLUMNS = (("author", "Creator", "text"), ("caption", "Caption", "text"), ("sound", "Sound", "text"),
                 ("likes", "Likes", "number"), ("comments", "Comments", "number"), ("saves", "Saves", "number"),
                 ("shares", "Shares", "number"), ("ad", "Ad", "text"))


def read_videos(pack, io, snapshot, n, arg):
    """Swipe through ``n`` TikToks (ads kept, flagged, not counted), reading each one's tree."""
    rows, keys, stuck, shown, stopped = [], set(), 0, 0, ""
    while shown < n:
        row = read_video(snapshot)
        key = (row["author"], row["caption"], row.get("likes"), row.get("comments"))
        if row["author"] or row["caption"]:
            if key in keys:
                stuck += 1
                if stuck >= 2:
                    stopped = "the videos stopped moving"
                    break
            else:
                stuck = 0
                keys.add(key)
                rows.append({**{k: row.get(k) for k, _l, _t in VIDEO_COLUMNS}, "following": row["following"]})
                shown += 0 if row["ad"] else 1
        if shown >= n:
            break
        try:
            snapshot = io.swipe(snapshot, "up")
        except core.Lost as lost:
            stopped = str(lost)
            break
        if pack.recognize(snapshot).screen not in ("for_you", "following", "video"):
            stopped = "it left the feed"
            break
    return ReadResult("videos", f"{shown} TikToks" if shown != 1 else "1 TikTok", VIDEO_COLUMNS, rows, stopped,
                      block="media_list")


def read_post(pack, io, snapshot, n, arg):
    return ReadResult("post", "This TikTok", VIDEO_COLUMNS, [read_video(snapshot)], "")


NOT_A_THREAD = re.compile(r"^(activity|new followers|system notifications|messages|inbox|search|add friends|"
                          r"direct messages|home|friends|shop|profile|create|back|stories)\b", re.I)


def parse_inbox(snapshot):
    rows = []
    for e in core.rows(snapshot):
        if e.role not in ("Cell", "Button", "Other") or not CONTENT[0] < core.centre_y(e) < CONTENT[1]:
            continue
        if NOT_A_THREAD.match(core.plain(e.label)):
            continue
        row = _inbox_row(e.label)
        if row and row not in rows:
            rows.append(row)
    return rows


DM_COLUMNS = (("from", "From", "text"), ("unread", "Unread", "bool"), ("last", "Last message", "text"),
              ("when", "When", "text"))


def read_dms(pack, io, snapshot, n, arg):
    rows, stopped, _ = core.collect(io, snapshot, parse_inbox, lambda row: (row["from"], row["last"]), n,
                                    max_swipes=min(n // 4 + 2, 12))
    unread = sum(1 for row in rows if row["unread"])
    return ReadResult("dms", f"{len(rows)} newest messages ({unread} unread)", DM_COLUMNS, rows, stopped)


read_thread = functools.partial(social.read_thread, noun="message thread")


COMMENT = re.compile(rf"^(?P<author>@?{HANDLE})[,:]?\s+(?P<text>.+?)(?:[,.]?\s+(?P<when>\d+[smhdw]|\d+ \w+ ago|"
                     r"\d{1,2}-\d{1,2}|yesterday))?(?:[,.]?\s+(?P<likes>" + COUNT + r") likes?)?\.?$", re.I)


def parse_comments(snapshot):
    rows = []
    for e in snapshot.elements:
        if e.role != "Cell" or not .2 < core.centre_y(e) < .9:
            continue
        found = COMMENT.match(core.plain(e.label))
        if found and not CHROME.match(core.plain(e.label)):
            rows.append({"author": found.group("author").lstrip("@"), "text": found.group("text"),
                         "when": found.group("when"), "likes": core.number(found.group("likes")) or 0})
    return rows


def read_profile(pack, io, snapshot, n, arg):
    """A profile's numbers: following, followers, likes (TikTok shows them in that order), and your state with it."""
    stats = {}
    elements = [e for e in snapshot.elements if core.centre_y(e) < .6]
    for e in elements:
        for word in ("following", "followers", "likes"):
            found = re.match(rf"^({COUNT})\s*{word}$", core.plain(e.label), re.I)
            if found and word not in stats:
                stats[word] = core.number(found.group(1))
    for e in elements:
        word = core.plain(e.label).casefold()
        if word in ("following", "followers", "likes") and word not in stats:
            above = [o for o in elements if o is not e and re.fullmatch(COUNT, core.plain(o.label) or "")
                     and abs((o.rect[0] + o.rect[2] / 2) - (e.rect[0] + e.rect[2] / 2)) < .08
                     and 0 < core.centre_y(e) - core.centre_y(o) < .06]
            if above:
                stats[word] = core.number(above[0].label)
    handle = (arg or "").lstrip("@") or next(
        (core.plain(e.label).lstrip("@") for e in elements if re.fullmatch(rf"@{HANDLE}", core.plain(e.label) or "")),
        None)
    state = None
    for e in core.find_all(snapshot, r"^(Follow|Following|Follow back|Friends|Requested|Edit profile)$", role="Button",
                           region=(0, .6)):
        state = {"Edit profile": "yours"}.get(e.label, e.label.casefold())
        break
    row = {"handle": handle, "following": stats.get("following"), "followers": stats.get("followers"),
           "likes": stats.get("likes"), "state": state}
    columns = (("handle", "Profile", "text"), ("following", "Following", "number"),
               ("followers", "Followers", "number"), ("likes", "Likes", "number"), ("state", "You", "text"))
    return ReadResult("profile", f"@{handle}" if handle else "This profile", columns, [row],
                      "" if stats else "the profile's numbers aren't in the screen's tree")


GRID = re.compile(rf"^(?:Video by @?(?P<author>{HANDLE})|@?(?P<who>{HANDLE})'s video)(?:[,.] (?P<views>{COUNT})"
                  r"(?: views?| plays?)?)?$", re.I)


def parse_grid(snapshot):
    rows = []
    for e in snapshot.elements:
        if e.role not in ("Button", "Cell", "Other") or not .2 < core.centre_y(e) < CONTENT[1]:
            continue
        found = GRID.match(core.plain(e.label))
        if found:
            rows.append({"author": found.group("author") or found.group("who"),
                         "views": core.number(found.group("views")), "element": e})
    return rows


FAVORITE_COLUMNS = (("author", "Creator", "text"), ("caption", "Caption", "text"), ("sound", "Sound", "text"),
                    ("likes", "Likes", "number"))


def read_favorites(pack, io, snapshot, n, arg):
    """Your favorites, newest first: each video opened from the grid and read, then back."""
    rows, opened, stopped, swipes = [], set(), "", 0
    while len(rows) < n:
        cells = [c for c in parse_grid(snapshot)
                 if (c["author"], round(c["element"].rect[1], 2), round(c["element"].rect[0], 2)) not in opened]
        if not cells:
            if swipes >= 6:
                stopped = "it read as far as it goes in one go"
                break
            after = io.swipe(snapshot, "up")
            swipes += 1
            if after.content_fingerprint == snapshot.content_fingerprint:
                stopped = "it reached the end of your favorites"
                break
            snapshot, opened = after, set()
            continue
        cell = cells[0]
        opened.add((cell["author"], round(cell["element"].rect[1], 2), round(cell["element"].rect[0], 2)))
        row = {"author": cell["author"], "caption": None, "sound": None, "likes": None}
        try:
            page = io.tap(cell["element"], snapshot, extra=.3)
            detail = read_video(page)
            row.update(caption=detail["caption"], sound=detail["sound"], likes=detail.get("likes"),
                       author=detail["author"] or row["author"])
            back = io.back(page)
            if back is None or not parse_grid(back):
                stopped = "it couldn't get back to your favorites"
                rows.append(row)
                break
            snapshot = back
        except core.Lost as lost:
            stopped = str(lost)
            rows.append(row)
            break
        rows.append(row)
    return ReadResult("favorites", f"{len(rows)} favorites", FAVORITE_COLUMNS, rows, stopped, block="media_list")


# Typed rows for the App API's schemas (app_api/endpoints.py): reader -> columns.
TikTok.read_columns = {"videos": VIDEO_COLUMNS, "post": VIDEO_COLUMNS, "dms": DM_COLUMNS,
                       "comments": social.COMMENT_COLUMNS,
                       "favorites": FAVORITE_COLUMNS, "insights": insights.COLUMNS}
PACK = TikTok()
