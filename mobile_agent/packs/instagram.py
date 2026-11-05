"""The Instagram pack: Instagram's screens, how to reach them and how to read them, from its accessibility tree."""

import re

from . import core, insights, rows, social
from .core import Control, PickRow, Reader, ReadResult, Route, ScreenSpec, Tap, TypeIn, Verified, sig
from .social import AD_CTA, COUNT, parse_inbox_row

BUNDLE = "com.burbn.instagram"
TAB = (.85, 1.0)
TOP = (0.0, .2)
CONTENT = (.1, .9)
HEAD = (0.0, .16)
RIGHT = (.84, 1.0)
AGO = r"\b\d+ (seconds?|minutes?|hours?|days?|weeks?) ago$"

TAB_LABEL = r"^(Main feed|Home|Reels|Direct messages\b.*|Messages|Explore|Search|Profile)$"
HANDLE = r"[A-Za-z0-9._]{1,30}"
AUDIO = re.compile(r"(original audio|♫|♪|\baudio\b|\bsong\b)", re.I)
# Text that is chrome on a Reel or a post, never its caption.
CHROME = re.compile(r"^(more, button, tap to see the whole caption|…|more|see translation|follow|following|"
                    rf"{COUNT} (likes?|comments?|shares?|views?|plays?)|liked by .*|followed by .*|"
                    rf"{HANDLE}, clips_viewer_clips_tab|.* liked this reel\.?|.* follows this profile\.?|"
                    r"seen too often|not too often|video progress: .*|ad|sponsored|paid partnership.*|"
                    r"\d+ (hours?|minutes?|days?|weeks?) ago|\w+ \d{1,2}|.* and \d[\d,]* others joined)$", re.I)


tab = rows.tab_tap(TAB)


class Instagram(core.Pack):
    name, key, prefix, bundle = "Instagram", "instagram", "IG", BUNDLE
    verified = Verified("448.0.0", "2026-10-13", "iPhone over USB, iOS 26")
    tabs = sig(TAB_LABEL, "Button", region=TAB)
    # Instagram's own words for acts, on top of core.NEVER: a pack taps none of them.
    never = re.compile(r"^(like comment|reply to this comment|add to story|share to|restrict|close friends|"
                       r"unfollow|remove follower|post comment|send message|ufi-(like|send|save)-button|"
                       r"like-button|send-button|save-button|repost-button)\b", re.I)
    mentions = re.compile(r"\b(instagram|insta|\bIG\b|reels?)\b", re.I)
    photos_notice = social.LIMITED_PHOTOS

    screens = (
        ScreenSpec("feed", "your feed", (
            sig(r"^Main feed$", "Button", region=TAB, selected=True, must=True, weight=2),
            sig(r"^Instagram Main Feed$"), sig(r"^Stories Tray$"), sig(r"^action-cell$|^Post options$"),
            sig(r"^Create a post, story or live video$")), "recorded",
            ocr=(sig(r"^Instagram$", region=HEAD, weight=3), sig(r"^Your\s?story$"), sig(rf"^Liked by |{AGO}"),
                   sig(r"^(Create|Notifications)$", "Button", region=HEAD), sig(r"^Reels\b", region=HEAD, never=True))),
        ScreenSpec("reels", "Reels", (
            sig(r"^sundial-viewer-video-cell$", weight=2), sig(r"^ufi-like-button$"),
            sig(r"^Reels$", "Button", region=TAB, selected=True),
            sig(rf"^(Reel|Post) by {HANDLE}\.?$", field="text"), sig(r"^Video progress:", field="text"),
            sig(r"^action-cell$", never=True)), "recorded",
            ocr=(sig(r"^Reels\b", region=HEAD), sig(rf"^{COUNT}$", region=(.35, .86), column=RIGHT, weight=3),
                   sig(r"^Follow$", region=(.65, .92), weight=2), sig(r"^(Liked by |Add comment)"),
                   sig(r"^Instagram$", region=HEAD, never=True))),
        ScreenSpec("explore", "Explore", (
            sig(r"^Explore$", "Button", region=TAB, selected=True, must=True, weight=2),
            sig(r"^Search with Meta AI$|^Search$", "SearchField"), sig(r"^Saved$", "Button", region=TOP),
            sig(rf"^(Photo|Video|Reel) by {HANDLE}$")), "recorded",
            ocr=(sig(r"Search with Meta A[Il]", region=HEAD, weight=2), sig(r"^For you$", region=(.1, .22)),
                     sig(rf"^\W{{0,2}}\s?{COUNT}$", region=(.2, .88)), sig(r"^(Save|Saved)$", "Button", region=HEAD),
                     sig(r"^Search$", "Button", region=TAB, selected=True))),
        ScreenSpec("search", "search results", (
            sig(r"^search-bar-text-view$", weight=2), sig(r"^Clear$", "Button", region=TOP),
            sig(r"^search-collection-view-cell", weight=1), sig(rf" • {COUNT} followers$", field="text"),
            sig(r"^(Searching|Summarizing)\.\.\.$", field="text")), "recorded",
            ocr=(sig(rf"^{HANDLE} • {COUNT}", weight=2), sig(r"followers?$|• \d[\d.,]*[KMB]? fol"),
                    sig(r"^Back$", "Button", region=HEAD), sig(r"^Instagram$", region=HEAD, never=True))),
        ScreenSpec("comments", "the comments", (
            sig(r"^Comments$", "StaticText", must=True, weight=2), sig(r"^Add a comment$"),
            sig(r"^Reply to this comment$"), sig(r"^Like comment$"), sig(rf"^{HANDLE} said ", "Cell")), "recorded",
            ocr=(sig(r"^Comments$", region=(0, .5), must=True, weight=2), sig(r"^Add a comment"),
                      sig(r"^Reply$"), sig(r"^View \d+ more repl"), sig(rf"^{HANDLE}\s+\d+[smhdw]\b"))),
        ScreenSpec("post", "a post", (
            sig(r"^action-cell$", must=True), sig(r"^Like$", "Button"), sig(r"^Back$", "Button", region=TOP),
            sig(r"^Main feed$", "Button", region=TAB, selected=True, never=True),
            sig(r"^Explore$", "Button", region=TAB, selected=True, never=True)), "recorded",
            ocr=(sig(r"^Posts$", region=HEAD, must=True, weight=2), sig(r"^Back$", "Button", region=HEAD),
                  sig(r"^Liked by |likes$"), sig(AGO))),
        ScreenSpec("dm_inbox", "your DMs", (
            sig(r"^Direct messages\b", "Button", region=TAB, selected=True, weight=2),
            sig(r"^(Primary|General|Requests)\b", weight=2), sig(r"^New message$|^Messages$|^Chats$"),
            sig(r"^Unread, ", field="text"), sig(r"\b(ago|now)$", "Cell"),
            sig(r"^Comments$", "StaticText", never=True)), "recorded",
            ocr=(sig(r"^(Primary|General|Requests)\b", weight=2), sig(r"^Messages$", region=(0, .3)),
                      sig(r"^(Ask Meta AI|Search)", region=(0, .3)),
                      sig(r"(new messages?|Active (now|\d)|Sent \d|Seen)"),
                      sig(r"^Messages$", "Button", region=TAB, selected=True), sig(r"^Comments$", never=True))),
        ScreenSpec("dm_thread", "a DM thread", (
            sig(r"^Message\W*$", must=True, weight=2), sig(r"^Back$", "Button", region=TOP),
            sig(r"^(Voice message|Send voice message|Gallery|Camera|Choose a GIF or sticker|Sticker)$"),
            sig(r"^(Audio call|Video call|Start audio call|Start video call)$"),
            sig(TAB_LABEL, "Button", region=TAB, never=True)), "inferred",
            ocr=(sig(r"^Message\W*$", must=True, weight=2), sig(r"^Active (now|\d)", region=HEAD),
                       sig(r"^Back$", "Button", region=HEAD),
                       sig(r"^(Voice message|Gallery|Camera|Sticker|Photo)$", "Button"))),
        ScreenSpec("profile", "a profile", (
            sig(rf"^{COUNT}\s*posts?$|^posts?$", weight=2), sig(rf"^{COUNT}\s*followers$|^followers$", weight=2),
            sig(rf"^{COUNT}\s*following$|^following$"),
            sig(r"^(Edit profile|Share profile|Follow|Following|Follow back|Message|Requested)$", "Button"),
            sig(r"^Comments$", "StaticText", never=True)), "inferred",
            ocr=(sig(r"^posts?$", weight=2), sig(r"^followers$", weight=2), sig(r"^following$"),
                     sig(r"^(Edit profile|Share profile|Follow|Following|Follow back|Message)$"))),
        ScreenSpec("insights", "your Professional dashboard", (
            sig(r"^(Professional dashboard|Insights)$", "StaticText", region=TOP, weight=2),
            sig(r"^(Views|Accounts reached|Profile visits|Net follower change)\b", weight=2),
            sig(insights.PERIOD.pattern), sig(r"^Edit profile$", "Button", never=True)), "inferred",
            ocr=(sig(r"^(Professional dashboard|Insights)$", region=HEAD, weight=2),
                 sig(r"^(Views|Accounts reached|Profile visits)$", weight=2))),
        ScreenSpec("saved", "your saved posts", (
            sig(r"^(Saved|All posts)$", "StaticText", region=TOP, weight=2), sig(r"^All posts$", weight=2),
            sig(r"^(New collection|Create collection|\+)$"), sig(rf"^(Photo|Video|Reel) by {HANDLE}$"),
            sig(r"^Explore$", "Button", region=TAB, selected=True, never=True)), "inferred",
            ocr=(sig(r"^(Saved|All posts)$", region=HEAD, weight=2), sig(r"^All posts$", weight=2),
                   sig(r"^(New collection|Create collection)$"))),
        ScreenSpec("notifications", "your notifications", (
            sig(r"^Notifications$", "StaticText", region=TOP, must=True, weight=2),
            sig(r"^(Today|Yesterday|This week|Last 7 days|This month|Last 30 days|Earlier)$"),
            sig(r"(started following you|liked your|commented|mentioned you)", field="text")), "inferred",
            ocr=(sig(r"^Notifications$", region=HEAD, must=True, weight=2),
                           sig(r"^(Today|Yesterday|This week|Last 7 days|This month|Last 30 days|Earlier)$"),
                           sig(r"(started following you|liked your|commented|mentioned you)"))),
        ScreenSpec("composer", "the post composer", (
            sig(r"^(New post|New reel|POST|STORY|REEL|LIVE|Post|Story|Reel|Live)$", weight=2),
            sig(r"^(Next|Recents|Gallery|Select multiple|Select)$"), sig(r"^(Close|Cancel|Back)$", "Button")),
            "inferred",
            ocr=(sig(r"^(New post|New reel|POST|STORY|REEL|LIVE)$", weight=2), sig(r"^(Next|Recents|Gallery)$"),
                      sig(r"^(Close|Cancel)$"))),
    )

    routes = {
        "feed": Route("feed", "your feed", ("feed",), (tab(r"^(Main feed|Home)$", "the Main feed tab"),)),
        "reels": Route("reels", "Reels", ("reels",), (tab(r"^Reels$", "the Reels tab"),)),
        "dms": Route("dms", "your DMs", ("dm_inbox",),
                     (tab(r"^(Direct messages\b.*|Messages)$", "the Direct messages tab"),)),
        "thread": Route("thread", "a DM thread", ("dm_thread",),
                        (tab(r"^(Direct messages\b.*|Messages)$", "the Direct messages tab"),
                         PickRow("DM thread", roles=("Cell", "Button", "Other"), region=(.15, .9))), arg="name"),
        "explore": Route("explore", "Explore", ("explore",), (tab(r"^(Explore|Search)$", "the Explore tab"),)),
        "saved": Route("saved", "your saved posts", ("saved",),
                       (tab(r"^(Explore|Search)$", "the Explore tab"),
                        Tap(r"^Saved$", "Saved", role="Button", region=TOP,
                                                                wait=.4))),
        "profile": Route("profile", "your profile", ("profile",), (tab(r"^Profile$", "the Profile tab"),)),
        "insights": Route("insights", "your Professional dashboard", ("insights",),
                          (tab(r"^Profile$", "the Profile tab"),
                           Tap(r"^(Professional dashboard|Insights)$", "Professional dashboard", opens=True, wait=.4))),
        "user": Route("user", "a profile", ("profile",),
                      (tab(r"^(Explore|Search)$", "the Explore tab"),
                       TypeIn(r"^Search with Meta A[Il]$|^Search$|^search-bar-text-view$", "the search field"),
                       PickRow("search result", roles=("StaticText", "Other", "Cell", "Button"), region=(.1, .8),
                               template=(r"^@?{arg} • ", r"^@?{arg}\. Profile picture$", r"^@?{arg}$"))),
                      link="instagram://user?username={arg}", arg="user"),
        "search": Route("search", "search results", ("search", "explore"),
                        (tab(r"^(Explore|Search)$", "the Explore tab"),
                         TypeIn(r"^Search with Meta A[Il]$|^Search$|^search-bar-text-view$", "the search field",
                                submit=True)), arg="query"),
        "notifications": Route("notifications", "your notifications", ("notifications",),
                               (tab(r"^(Main feed|Home)$", "the Main feed tab"),
                                Tap(r"^Notifications$", "Notifications", role="Button", region=TOP))),
        "post": Route("post", "the post", ("post", "reels"), (), link="{arg}", arg="url", from_tabs=False),
        "comments": Route("comments", "the comments", ("comments",),
                          (Tap(r"^Comment$", "Comment", role="Button", opens=True, wait=.3),), from_tabs=False),
        "compose": Route("compose", "the post composer", ("composer",),
                         (tab(r"^(Main feed|Home)$", "the Main feed tab"),
                          Tap(r"^Create a post, story or live video$", "the create button", opens=True, wait=.5))),
        "story": Route("story", "the story camera", ("composer",),
                       (tab(r"^(Main feed|Home)$", "the Main feed tab"),
                        Tap(r"^Create a post, story or live video$", "the create button", opens=True, wait=.5),
                        Tap(r"^(STORY|Story)$", "Story", opens=True, optional=True))),
        "here": Route("here", "a post or Reel", ("feed", "reels", "post"),
                      (tab(r"^(Main feed|Home)$", "the Main feed tab"),)),
    }
    route_with_arg = {"profile": "user"}
    hidden_routes = ("user", "here")
    sites = ("www.instagram.com", "instagram.com")
    # A post's or Reel's address opens it. instagram.com/direct/inbox/ and instagram.com/reels/ open the home feed
    # (iPhone 15 Pro, iOS 26.0.1, 2026-10-14), so they are no link here: DMs and Reels are one tap on the tab bar,
    # and deeplinks.APP_FORMS maps the two addresses to instagram://direct-inbox and instagram://reels_home.
    link_paths = re.compile(r"^/(?:p|reel|tv)/[\w-]+|^/reels/[\w-]{6,}")

    controls = {
        "like": Control(route="here", control=r"^Like$", role="Button", title="Like"),
        "follow": Control(route="user", control=r"^Follow( back)?$", role="Button", region=(0, .7), title="Follow"),
        "comment": Control(route="comments", control=r"^Add a comment", title="comment field"),
        "reply": Control(route="thread", control=r"^Message\W*$", title="message field"),
        "compose": Control(route="compose", control=r"^(Next|Share)$", role="Button", title="Next"),
        "story": Control(route="story", control=r"^(Your story|Share|Next)$", title="Your story"),
    }
    open_aliases = {"dm": "dms", "inbox": "dms", "messages": "dms", "home": "feed", "reel": "reels",
                    "search": "search", "user": "user", "@": "user", "camera": "story", "post": "post",
                    "new": "compose", "create": "compose", "notification": "notifications", "chat": "thread",
                    "conversation": "thread"}
    read_aliases = {"dm": "dms", "inbox": "dms", "messages": "dms", "reel": "reels", "posts": "feed",
                    "home": "feed", "comment": "comments", "chat": "thread", "conversation": "thread",
                    "bookmarks": "saved", "user": "profile", "stats": "profile", "analytics": "insights",
                    "dashboard": "insights"}
    read_default = {"reels": 10, "feed": 10, "dms": 15, "thread": 20, "comments": 20, "saved": 10}
    read_titles = {"reels": "Reels", "feed": "feed posts", "dms": "DMs", "thread": "messages", "comments": "comments",
                   "saved": "saved posts", "profile": "profile", "insights": "insights", "post": "post"}
    nouns = {"reels": "Reel", "feed": "feed post", "saved": "saved post", "post": "post", "dms": "DM",
             "thread": "message", "comments": "comment"}
    prompts = {
        "OPEN": ("- IG_OPEN where: go to an Instagram screen by code, no taps from you: feed | reels | dms | thread "
                 "<name> | saved | profile [@user] | search <words> | notifications | post <link> | comments; or to "
                 "an act's control: like | follow @user | comment | reply <name> | compose | story (it names the "
                 "control and never taps it; you tap it, the person approves). If it can't, go on the usual way."),
        "READ": ("- IG_READ what [n]: read Instagram by code from the screen's tree, no screenshots: reels n (swipes "
                 "through n Reels) | feed n | dms n | thread [name] | comments n | saved n (opens each) | profile "
                 "[@user] | post. Gives author, caption, audio, likes, comments, collab and ad flags, or DM rows. "
                 "Opens the screen it needs."),
    }
    mcp_open_hint = "DMs, a thread, Reels, saved posts, a profile, search, a post's link"
    mcp_read_hint = "Reels, feed posts, DMs, a thread, comments, saved posts, a profile or insights"
    required = {"dms": ("from", "last", "when")}   # a DM row that lacks one says "not on the screen", not a stand-in

    @property
    def readers(self):
        return {"reels": Reader(read_reels, ("reels",), "reels"), "feed": Reader(read_feed, ("feed",), "feed"),
                "dms": Reader(read_dms, ("dm_inbox",), "dms"),
                "thread": Reader(social.read_thread, ("dm_thread",), "thread"),
                "comments": Reader(social.comments_reader(parse_comments), ("comments",), "comments"),
                "saved": Reader(read_saved, ("saved",), "saved"),
                "profile": Reader(read_profile, ("profile",), "profile"),
                "insights": Reader(insights.reader, ("insights",), "insights"),
                "post": Reader(read_post, ("post", "feed", "reels"))}

    def read(self, io, what, n=10, arg=None):
        if what == "insights":
            return insights.read(self, io, what, n, arg)
        return super().read(io, what, n, arg)


def _lines(snapshot):
    return [line.strip() for line in str(snapshot.text or "").split("\n") if line.strip()]


def _count(element_or_text, word):
    text = getattr(element_or_text, "label", element_or_text) or ""
    match = re.match(rf"^({COUNT})\s+{word}", text, re.I)
    return core.number(match.group(1)) if match else None


def ad_reason(snapshot, region=None):
    """Why this item is an ad ("Ad", "Sponsored", a call to action), or None."""
    for element in snapshot.elements:
        if region and not region[0] <= core.centre_y(element) <= region[1]:
            continue
        label = core.plain(element.label)
        if re.fullmatch(r"(ad|sponsored)", label, re.I):
            return label
        if element.role == "Button" and AD_CTA.match(label):
            return label
    return None


def read_reel_pictured(snapshot):
    """One Reel read from a picture (iPhone Mirroring): the handle beside Follow, the caption under it, the right
    column's counts top to bottom (likes, comments, reposts, shares), the audio line."""
    lines = [e for e in snapshot.elements if e.role != "Button" or not str(e.locator).startswith("icon:")]
    follow = next((e for e in lines if core.plain(e.label) == "Follow" and core.centre_y(e) > .6), None)
    author = None
    row_y = core.centre_y(follow) if follow is not None else .8
    for e in sorted(lines, key=lambda e: e.rect[0]):
        if abs(core.centre_y(e) - row_y) < .025 and e.rect[0] < (follow.rect[0] if follow is not None else .7):
            found = re.match(rf"^@?({HANDLE}?)\s*[•·o]?$", core.plain(e.label))
            if found and len(found.group(1)) >= 3:
                author = found.group(1).rstrip(".")
    caption = next((core.plain(e.label) for e in sorted(lines, key=core.centre_y)
                    if row_y + .015 < core.centre_y(e) < .92 and e.rect[0] < .8
                    and not CHROME.match(core.plain(e.label))
                    and not re.match(r"^(liked by|add comment)", e.label, re.I)),
                   None)
    counts = sorted((e for e in lines if re.fullmatch(COUNT, core.plain(e.label))
                     and e.rect[0] + e.rect[2] / 2 > .84 and .35 < core.centre_y(e) < .86), key=core.centre_y)
    numbers = [core.number(core.plain(e.label)) for e in counts]
    audio = next((core.plain(e.label) for e in lines if AUDIO.search(e.label or "")), None)
    ad = next((core.plain(e.label) for e in lines if re.fullmatch(r"(sponsored|ad)", core.plain(e.label), re.I)), None)
    return {"author": author, "collab": None, "caption": caption, "captionHidden": None, "audio": audio,
            "likes": numbers[0] if numbers else None, "comments": numbers[1] if len(numbers) > 1 else None,
            "reposts": numbers[2] if len(numbers) > 2 else None, "shares": numbers[3] if len(numbers) > 3 else None,
            "ad": ad, "following": False if follow is not None else None, "social": None}


def read_reel(snapshot):
    """One Reel on screen as a row: author (or authors, for a collab), caption, audio, counts, ad, social."""
    if core.image_only(snapshot):
        return read_reel_pictured(snapshot)
    lines = _lines(snapshot)
    authors = []
    for line in lines:
        found = re.match(rf"^(?:Reel|Post) by ({HANDLE}?)\.?$", line)
        if found and found.group(1) not in authors:
            authors.append(found.group(1))
            break
    collab = next((re.match(rf"^({HANDLE}) and ({HANDLE})$", line) for line in lines
                   if re.match(rf"^({HANDLE}) and ({HANDLE})$", line)), None)
    if collab:
        authors = [collab.group(1), collab.group(2)]
    if not authors:
        # The creator's picture beside the byline (a collab shows two): never the "Followed by" row under it.
        follow_row = core.find(snapshot, r"^Followed by |^Liked by ", region=(.6, .95))
        cut = core.centre_y(follow_row) - .02 if follow_row else .9
        for element in core.find_all(snapshot, rf"^{HANDLE}\. Profile picture$|^{HANDLE}'s profile picture$",
                                     region=(.55, cut)):
            name = re.match(rf"^({HANDLE}?)(?:\. |'s )", element.label).group(1)
            if name not in authors:
                authors.append(name)
    following = None
    if authors and core.find(snapshot, rf"^Follow {re.escape(authors[0])}$", role="Button"):
        following = False
    caption = None
    byline = core.find(snapshot, r"^(Post|Reel) by ", region=(.5, .95))
    low = core.centre_y(byline) if byline else .7
    for element in snapshot.elements:
        if element.role not in ("StaticText", "Other") or not element.label:
            continue
        y = core.centre_y(element)
        if not low < y < .9 or element.rect[0] > .8:
            continue
        if CHROME.match(core.plain(element.label)) or re.fullmatch(HANDLE, element.label):
            continue
        caption = core.plain(element.label)
        break
    hidden = caption is None and bool(core.find(snapshot, r"^More, button, tap to see the whole caption$"))
    likes = [e for e in core.find_all(snapshot, rf"^{COUNT} likes?$", role="Button")]
    likes.sort(key=core.centre_y)
    audio = None
    for line in lines:
        if AUDIO.search(line) and not line.startswith("ufi-") and len(line) < 120:
            audio = re.sub(r"^audio\s*[,:]\s*", "", line, flags=re.I) or line
            break
    social = next((core.plain(e.label)
                   for e in core.find_all(snapshot, r" liked this reel\.?$| follows this profile\.?$")),
                  None)
    comments = next((_count(line, "comments?") for line in lines if _count(line, "comments?") is not None), None)
    shares = next((_count(line, "shares?") for line in lines if _count(line, "shares?") is not None), None)
    return {"author": authors[0] if authors else None, "collab": ", ".join(authors[1:]) or None,
            "caption": caption, "captionHidden": hidden or None, "audio": audio,
            "likes": _count(likes[0], "likes?") if likes else None,
            "comments": comments, "shares": shares,
            "reposts": _count(likes[1], "likes?") if len(likes) > 1 else None,
            "ad": ad_reason(snapshot), "following": following, "social": social}


REEL_COLUMNS = (("author", "Creator", "text"), ("collab", "With", "text"), ("caption", "Caption", "text"),
                ("audio", "Audio", "text"), ("likes", "Likes", "number"), ("comments", "Comments", "number"),
                ("shares", "Shares", "number"), ("ad", "Ad", "text"))


def read_reels(pack, io, snapshot, n, arg):
    """Swipe through ``n`` Reels (ads are kept, flagged, and don't count), reading each one's tree."""
    rows, keys, stuck = [], set(), 0
    shown = 0
    stopped = ""
    while shown < n:
        row = read_reel(snapshot)
        if row["captionHidden"]:
            snapshot, row = _expand_caption(pack, io, snapshot, row)
        key = (row["author"], row["likes"], row["comments"], row["shares"])
        if row["author"] or row["caption"]:
            if key in keys:
                stuck += 1
                if stuck >= 2:
                    stopped = "the Reels stopped moving"
                    break
            else:
                stuck = 0
                keys.add(key)
                rows.append(row)
                shown += 0 if row["ad"] else 1
        if shown >= n:
            break
        try:
            snapshot = io.swipe(snapshot, "up")
        except core.Lost as lost:
            stopped = str(lost)
            break
        seen = pack.recognize(snapshot)
        if seen.screen != "reels":
            stopped = "it left Reels"
            break
    return ReadResult("reels", f"{shown} Reels" if shown != 1 else "1 Reel", REEL_COLUMNS, rows, stopped,
                      block="media_list")


def _expand_caption(pack, io, snapshot, row):
    """A Reel whose caption is folded ("More, button, tap to see the whole caption"): one tap opens it in place, and the
    Reel is read again."""
    more = core.find(snapshot, r"^More, button, tap to see the whole caption$")
    if more is None:
        return snapshot, row
    try:
        after = io.tap(more, snapshot)
    except core.Lost:
        return snapshot, row
    if pack.recognize(after).screen != "reels":
        return after, row
    again = read_reel(after)
    if again["caption"] and again["author"] == row["author"]:
        return after, {**row, "caption": again["caption"], "captionHidden": None}
    return after, row


def _segments(snapshot):
    """The posts on a feed screen: (top, bottom) bands, each from a post's header ("Post options") to the next."""
    heads = sorted(core.centre_y(e) for e in core.find_all(snapshot, r"^Post options$", role="Button"))
    bands = []
    tops = [0.0, *heads] if not heads or heads[0] > .25 else heads
    for index, top in enumerate(tops):
        bottom = tops[index + 1] - .005 if index + 1 < len(tops) else 1.0
        bands.append((max(0.0, top - .03), bottom))
    return bands


def read_post_band(snapshot, band):
    """One feed post (the elements inside ``band``) as a row, or None when the band shows no post."""
    inside = [e for e in snapshot.elements if band[0] <= core.centre_y(e) <= band[1] and core.centre_y(e) < .89]
    if not any(e.role == "Button" and e.label == "Like" for e in inside):
        return None   # a post whose actions aren't on screen yet is read when they are
    author = None
    head = next((e for e in inside if e.label == "Post options"), None)
    if head is not None:
        same_row = [e for e in inside if e.role == "Button" and abs(core.centre_y(e) - core.centre_y(head)) < .03
                    and e is not head and e.label]
        name = next((e.label for e in same_row if re.fullmatch(rf"{HANDLE}( and {HANDLE})?", e.label)), None)
        author = name
    media = next((re.match(rf"^(Photo|Video|Reel) by ({HANDLE})$|^(\d+) photos or videos from ({HANDLE})$",
                           e.label) for e in inside
                  if re.match(rf"^(Photo|Video|Reel) by ({HANDLE})$|^(\d+) photos or videos from ({HANDLE})$",
                              e.label or "")), None)
    kind = None
    if media:
        kind = (media.group(1) or "Album").lower() if media.group(1) else "album"
        author = author or media.group(2) or media.group(4)
    collab = None
    if author and " and " in author:
        author, collab = author.split(" and ", 1)
    caption = None
    for e in inside:
        if e.role == "StaticText" and author and e.label.startswith(author + " "):
            caption = re.sub(r"…\s*more$", "…", e.label[len(author) + 1:]).strip()
            break
    when = next((e.label for e in inside if e.role == "StaticText" and re.search(r"\bago$|^\w+ \d{1,2}$", e.label)),
                None)
    likes = next((_count(e, "likes?") for e in inside if _count(e, "likes?") is not None), None)
    comments = next((_count(e, "comments?") for e in inside if _count(e, "comments?") is not None), None)
    shares = next((_count(e, "shares?") for e in inside if _count(e, "shares?") is not None), None)
    sponsored = next((e.label for e in inside if re.fullmatch(r"sponsored|ad", e.label or "", re.I)), None) or next(
        (e.label for e in inside if e.role == "Button" and AD_CTA.match(e.label or "")), None)
    if not (author or caption):
        return None
    return {"author": author, "collab": collab, "kind": kind, "caption": caption, "likes": likes,
            "comments": comments, "shares": shares, "when": when, "ad": sponsored}


POST_COLUMNS = (("author", "Author", "text"), ("collab", "With", "text"), ("kind", "Kind", "text"),
                ("caption", "Caption", "text"), ("likes", "Likes", "number"), ("comments", "Comments", "number"),
                ("when", "When", "text"), ("ad", "Ad", "text"))


def parse_feed(snapshot):
    return [row for row in (read_post_band(snapshot, band) for band in _segments(snapshot)) if row]


def read_feed(pack, io, snapshot, n, arg):
    rows, stopped, _ = core.collect(io, snapshot, parse_feed,
                                    lambda row: (row["author"], (row["caption"] or "")[:60], row["when"]), n,
                                    max_swipes=min(3 * n, 40))
    return ReadResult("feed", f"{len(rows)} feed posts", POST_COLUMNS, rows, stopped, block="media_list")


def read_post(pack, io, snapshot, n, arg):
    """The post or Reel on screen (the first post on a feed)."""
    if pack.recognize(snapshot).screen == "reels":
        row = read_reel(snapshot)
        return ReadResult("post", "This Reel", REEL_COLUMNS, [row], "")
    rows = parse_feed(snapshot)
    return ReadResult("post", "This post", POST_COLUMNS, rows[:1], "" if rows else "no post is on the screen")


def parse_inbox(snapshot):
    rows = []
    for e in core.rows(snapshot):
        if e.role not in ("Cell", "Button", "Other") or not CONTENT[0] < core.centre_y(e) < CONTENT[1]:
            continue
        row = parse_inbox_row(e.label)
        if row and row not in rows:
            rows.append(row)
    return rows


DM_COLUMNS = (("from", "From", "text"), ("group", "Group", "bool"), ("unread", "Unread", "bool"),
              ("last", "Last message", "text"), ("when", "When", "text"))


def read_dms(pack, io, snapshot, n, arg):
    rows, stopped, _ = core.collect(io, snapshot, parse_inbox, lambda row: (row["from"], row["last"]), n,
                                    max_swipes=min(n // 4 + 2, 12))
    unread = sum(1 for row in rows if row["unread"])
    return ReadResult("dms", f"{len(rows)} newest DMs ({unread} unread)", DM_COLUMNS, rows, stopped)


COMMENT = re.compile(rf"^(?P<author>{HANDLE}) said (?P<text>.*?)\.? Commented (?P<when>.+?)\.?"
                     r"(?:, (?P<likes>\d[\d,]*) likes?)?$", re.S)


PICTURED_COMMENT = re.compile(rf"^(?P<author>{HANDLE})\s+(?P<when>\d+[smhdw])\b(?:\s*·?\s*Edited)?,?\s*(?P<text>.*?)"
                              r"(?:, Reply.*)?$")


def parse_comments(snapshot):
    if core.image_only(snapshot):
        out = []
        for e in core.rows(snapshot):
            found = PICTURED_COMMENT.match(core.plain(e.label))
            if found and found.group("text"):
                out.append({"author": found.group("author"), "text": found.group("text").strip(", "),
                            "when": found.group("when"), "likes": 0})
        return out
    rows = []
    for e in core.find_all(snapshot, rf"^{HANDLE} said ", role="Cell"):
        found = COMMENT.match(core.plain(e.label))
        if found:
            rows.append({"author": found.group("author"), "text": found.group("text"),
                         "when": found.group("when"), "likes": core.number(found.group("likes")) or 0})
    return rows


def read_profile(pack, io, snapshot, n, arg):
    """A profile's numbers and state: {handle, name, posts, followers, following, verified, following_them}."""
    stats = {}
    elements = [e for e in snapshot.elements if core.centre_y(e) < .6]
    for e in elements:
        for word, rx in (("posts", "posts?"), ("followers", "followers?"), ("following", "following")):
            found = re.match(rf"^({COUNT})\s*{rx}$", core.plain(e.label), re.I)
            if found and word not in stats:
                stats[word] = core.number(found.group(1))
    if len(stats) < 3:
        # A number above its word: "2.7M" over "followers".
        for e in elements:
            word = core.plain(e.label).casefold()
            if (word in ("posts", "post", "followers", "following")
                    and (word if word != "post" else "posts") not in stats):
                above = [o for o in elements if o is not e and re.fullmatch(COUNT, core.plain(o.label) or "")
                         and abs((o.rect[0] + o.rect[2] / 2) - (e.rect[0] + e.rect[2] / 2)) < .08
                         and 0 < core.centre_y(e) - core.centre_y(o) < .06]
                if above:
                    stats[word if word != "post" else "posts"] = core.number(above[0].label)
    title = next((core.plain(e.label) for e in snapshot.elements if core.centre_y(e) < .13
                  and re.fullmatch(HANDLE, core.plain(e.label) or "") and not core.BACK_LABEL.match(e.label)), None)
    handle = (arg or "").lstrip("@") or title
    state = None
    for e in core.find_all(snapshot, r"^(Follow|Following|Follow back|Requested|Edit profile)$", role="Button",
                           region=(0, .7)):
        state = {"Edit profile": "yours"}.get(e.label, e.label.casefold())
        break
    verified = bool(core.find(snapshot, r"^Verified( account)?$", region=(0, .4)))
    row = {"handle": handle, "posts": stats.get("posts"), "followers": stats.get("followers"),
           "following": stats.get("following"), "verified": verified or None, "state": state}
    columns = (("handle", "Profile", "text"), ("posts", "Posts", "number"), ("followers", "Followers", "number"),
               ("following", "Following", "number"), ("verified", "Verified", "bool"), ("state", "You", "text"))
    stopped = "" if stats else "the profile's numbers aren't in the screen's tree"
    return ReadResult("profile", f"@{handle}" if handle else "This profile", columns, [row], stopped)


GRID = re.compile(rf"^(?:(?P<kind>Photo|Video|Reel) by (?P<author>{HANDLE})|(?P<count>\d+) photos or videos from "
                  rf"(?P<album>{HANDLE}))$")


def parse_grid(snapshot):
    rows = []
    for e in snapshot.elements:
        if e.role not in ("Button", "Cell", "Other") or not CONTENT[0] < core.centre_y(e) < CONTENT[1]:
            continue
        found = GRID.match(core.plain(e.label))
        if found:
            rows.append({"author": found.group("author") or found.group("album"),
                         "kind": (found.group("kind") or "album").lower(), "element": e})
    return rows


SAVED_COLUMNS = (("author", "Author", "text"), ("kind", "Kind", "text"), ("caption", "Caption", "text"),
                 ("likes", "Likes", "number"), ("when", "When", "text"))


def read_saved(pack, io, snapshot, n, arg):
    """Your saved posts, newest first: each one opened from the grid and read (author, caption, likes), then back."""
    every = core.find(snapshot, r"^All posts$", roles=("Cell", "Button", "Other", "StaticText"))
    if every is not None and not parse_grid(snapshot):
        snapshot = io.tap(every, snapshot, opens=True, pattern=r"^All posts$", extra=.4)
    rows, opened, stopped = [], set(), ""
    swipes = 0
    while len(rows) < n:
        cells = [c for c in parse_grid(snapshot) if (c["author"], round(c["element"].rect[1], 2),
                                                     round(c["element"].rect[0], 2)) not in opened]
        if not cells:
            if swipes >= 6:
                stopped = "it read as far as it goes in one go"
                break
            after = io.swipe(snapshot, "up")
            swipes += 1
            if after.content_fingerprint == snapshot.content_fingerprint:
                stopped = "it reached the end of your saved posts"
                break
            snapshot, opened = after, set()
            continue
        cell = cells[0]
        opened.add((cell["author"], round(cell["element"].rect[1], 2), round(cell["element"].rect[0], 2)))
        row = {"author": cell["author"], "kind": cell["kind"], "caption": None, "likes": None, "when": None}
        try:
            page = io.tap(cell["element"], snapshot, extra=.3)
            seen = pack.recognize(page)
            if seen.screen == "reels":
                detail = read_reel(page)
                row.update(caption=detail["caption"], likes=detail["likes"])
            else:
                posts = parse_feed(page)
                if posts:
                    row.update(caption=posts[0]["caption"], likes=posts[0]["likes"], when=posts[0]["when"])
            back = io.back(page)
            if back is None or pack.recognize(back).screen not in ("saved", None) and not parse_grid(back):
                stopped = "it couldn't get back to your saved posts"
                rows.append(row)
                break
            snapshot = back
        except core.Lost as lost:
            stopped = str(lost)
            rows.append(row)
            break
        rows.append(row)
    return ReadResult("saved", f"{len(rows)} saved posts", SAVED_COLUMNS, rows, stopped, block="media_list")


# Typed rows for the App API's schemas (app_api/endpoints.py): reader -> columns.
Instagram.read_columns = {"reels": REEL_COLUMNS, "feed": POST_COLUMNS, "post": POST_COLUMNS, "dms": DM_COLUMNS,
                          "thread": social.THREAD_COLUMNS, "comments": social.COMMENT_COLUMNS, "saved": SAVED_COLUMNS,
                          "insights": insights.COLUMNS}
PACK = Instagram()
