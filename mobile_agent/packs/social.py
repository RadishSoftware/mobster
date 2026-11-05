"""What the social packs read the same way."""

import re

from . import core
from .core import ReadResult

COUNT = r"\d[\d,.]*[KMB]?"
TIME = re.compile(r"(\bago\b|\bnow\b|^yesterday$|^just now$|"
                  r"^\d+\s?[smhdwy]$|^(mon|tues|wednes|thurs|fri|satur|sun)day$|"
                  r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w* \d{1,2}(, \d{4})?$|^\d{1,2}/\d{1,2}/\d{2,4}$|"
                  r"^\d{1,2}:\d{2}\s?(am|pm)?$)", re.I)
# The notice an app shows when iOS gave it only some of the person's photos (Photos access "Limit Access" for that app).
# Whole labels, English UI. Inferred from iOS's own limited-library wording and the apps' usual labels: no composer or
# picker tree has been recorded from Instagram or TikTok yet (the audit's open item), so a notice worded otherwise is
# not recognised, and the Mac-side preview check (content_queue/media_check.py) is the second line.
LIMITED_PHOTOS = re.compile(
    r"^(?=.*\b(?:photos?|library|videos?)\b)(?=.*\b(?:access|permission|allowed|can see|has|have)\b)"
    r"(?=.*\b(?:limited|only some|select(?:ed)?|a few|some of)\b).{8,160}$"
    r"|^allow access to all photos$|^select more photos$|^manage photos? access$", re.I)


AD_CTA = re.compile(r"^(install now|shop now|learn more|sign up|book now|order now|get offer|download|apply now|"
                    r"watch more|get quote|contact us|subscribe|visit (site|website|profile)|send message|get app)$",
                    re.I)


ROW_ACTIONS = re.compile(r"^(delete|mute|unmute|pin|unpin|more|archive|flag|hide|mark as unread|mark as read)$", re.I)
NOT_A_THREAD = re.compile(r"^(primary|general|requests|search|ask meta ai.*|new message|your note|leave a note.*|"
                          r"notes|messages|chats|channels|edit|filter|back|close|direct messages.*|main feed|reels|"
                          r"explore|profile|[^,]+ shared a note\b)", re.I)
SENT_WHEN = re.compile(r"^(?:sent|seen|delivered)\s+(.+)$", re.I)


def parse_inbox_row(label):
    """One inbox row's label as {from, group, unread, last, when}, or None when it isn't a conversation."""
    text = core.plain(label)
    if not text or NOT_A_THREAD.match(text) or "," not in text:
        return None
    tokens = [t.strip() for t in text.split(", ") if t.strip()]
    unread = tokens[0].casefold() == "unread"
    if unread:
        tokens = tokens[1:]
    # The people end where the first row action starts. Without one the first token is the name (a variant seen in
    # earlier runs' steps).
    cut = next((i for i, t in enumerate(tokens) if i and ROW_ACTIONS.match(t)), 1)
    people, rest = tokens[:cut], tokens[cut:]
    while rest and ROW_ACTIONS.match(rest[0]):
        rest = rest[1:]
    if not people:
        return None
    stamp = SENT_WHEN.match(rest[-1]) if rest else None
    if stamp and TIME.search(stamp.group(1)):
        rest = rest[:-1] + [stamp.group(1)]   # "Sent Tuesday": a row with no preview, only the day it was sent
    when = rest[-1] if rest and TIME.search(rest[-1]) else None
    last = ", ".join(rest[:-1] if when else rest) or None
    if when is None and last is None:
        return None
    new = re.match(r"^(\d+\+?) new messages?$", last or "", re.I)
    unread = unread or bool(new)   # a picture shows no unread dot; "4 new messages" says it
    return {"from": ", ".join(people), "group": len(people) > 1, "unread": unread, "last": last,
            "new": int(new.group(1).rstrip("+")) if new else None, "when": when}


def thread_name(snapshot):
    top = [e for e in snapshot.elements if core.centre_y(e) < .14 and e.role in ("StaticText", "Button", "Other")
           and e.label and not core.BACK_LABEL.match(e.label)
           and not re.match(r"^(audio call|video call|start .* call|info|details|more)", e.label, re.I)]
    return core.plain(top[0].label) if top else None


def parse_thread(snapshot):
    """The messages a DM thread shows, oldest first: {from: "them"|"you", text, when}."""
    field = core.find(snapshot, r"^Message\W*$")
    bottom = core.centre_y(field) - .02 if field is not None else .88
    rows, when = [], None
    for e in snapshot.elements:
        y = core.centre_y(e)
        if not .12 < y < bottom or not e.label or e.role not in ("StaticText", "Cell", "Other", "Button", "TextView"):
            continue
        text = core.plain(e.label)
        if core.BACK_LABEL.match(text) or re.match(r"^(like|react|reply|more|seen|delivered|sent)\b", text, re.I) \
                and len(text) < 20:
            continue
        x0, _, w, _ = e.rect
        centre = x0 + w / 2
        if STAMP.search(text) and len(text) < 30 and .35 < centre < .65 and w < .6:
            when = text
            continue
        side = "you" if x0 > .25 and centre > .55 else "them"
        row = {"from": side, "text": text, "when": when}
        if not rows or rows[-1]["text"] != text:
            rows.append(row)
    return rows


STAMP = re.compile(r"(\b\d{1,2}:\d{2}\s?(am|pm)?\b|\byesterday\b|\btoday\b|\bago\b|"
                   r"^(mon|tue|wed|thu|fri|sat|sun)\w*\b)",
                   re.I)
THREAD_COLUMNS = (("from", "From", "text"), ("text", "Message", "text"), ("when", "When", "text"))


def read_thread(pack, io, snapshot, n, arg, noun="DM thread"):
    """A thread's messages, oldest first. ``noun`` is what the app calls it ("DM thread", "message thread")."""
    name = thread_name(snapshot) or arg
    rows = parse_thread(snapshot)
    swipes = 0
    while len(rows) < n and swipes < 4:
        try:
            after = io.swipe(snapshot, "down")   # older messages are above
        except core.Lost:
            break
        swipes += 1
        if after.content_fingerprint == snapshot.content_fingerprint:
            break
        older = [row for row in parse_thread(after) if row not in rows]
        if not older:
            break
        rows = older + rows
        snapshot = after
    for row in rows:
        if row["from"] == "them" and name:
            row["from"] = name
        elif row["from"] == "you":
            row["from"] = "You"
    rows = rows[-n:]
    return ReadResult("thread", f"Your messages with {name}" if name else f"This {noun}", THREAD_COLUMNS, rows,
                      "" if len(rows) >= n else "it reached the start of what the thread shows")


# Feeds: X, YouTube, Threads, LinkedIn, Reddit

WORDED_COUNT = r"\d[\d,.]*\s?[KkMmBb]?"
# A count's word -> the column it fills.
COUNT_WORDS = {
    "repl": "replies", "comment": "comments", "answer": "comments",
    "repost": "reposts", "retweet": "reposts", "quote": "quotes", "share": "shares",
    "like": "likes", "reaction": "likes", "heart": "likes", "upvote": "score", "point": "score", "vote": "score",
    "view": "views", "play": "views", "watching": "views", "bookmark": "saves", "save": "saves",
}
COUNTS = re.compile(rf"\b({WORDED_COUNT})\s+(repl(?:y|ies)|comments?|answers?|reposts?|retweets?|quotes?|shares?|"
                    r"likes?|"
                    r"reactions?|hearts?|upvotes?|points?|votes?|views?|plays?|watching|bookmarks?|saves?)\b", re.I)
WHEN = re.compile(r"(?<![\w.])(\d{1,2}\s?(?:s|m|min|h|hr|d|w|mo|y)|\d+ (?:seconds?|minutes?|hours?|days?|weeks?|"
                  r"months?|years?) ago|yesterday|just now|now|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w* "
                  r"\d{1,2}(?:, \d{4})?)(?![\w])", re.I)
HANDLE = re.compile(r"(?<![\w@])(@[A-Za-z0-9_.]{2,30}|u/[A-Za-z0-9_-]{2,30})")
PLACE = re.compile(r"(?<![\w/])(r/[A-Za-z0-9_]{2,30})")
SEPARATORS = re.compile(r"\s*[·•|]\s*|\s*,\s*(?=,)|\s{2,}")


def parse_post(label, *, first_token_author=True):
    """One row's label as {author, handle, place, text, when, ad, <count columns>}; None for a label too short to be a
    post."""
    text = core.plain(label)
    if len(text) < 12:
        return None
    row = {"author": None, "handle": None, "place": None, "text": None, "when": None, "ad": None}
    flag = re.search(r"(^|[\s,·•])(promoted|sponsored|ad)(?=[\s,·•]|$)", text, re.I)
    if flag:
        row["ad"] = flag.group(2).capitalize()
        text = (text[:flag.start(2)] + text[flag.end(2):]).strip(" ,·•")
    for match in COUNTS.finditer(text):
        word = match.group(2).casefold()
        key = next((v for k, v in COUNT_WORDS.items() if word.startswith(k)), None)
        if key and key not in row:
            row[key] = core.number(match.group(1).replace(" ", ""))
    rest = COUNTS.sub(" ", text)
    rest = re.sub(r"\bposted by\b", " ", rest, flags=re.I)
    place = PLACE.search(rest)
    if place:
        row["place"] = place.group(1)
        rest = rest[:place.start()] + " " + rest[place.end():]
    when = WHEN.search(rest)
    if when:
        row["when"] = when.group(1)
        rest = rest[:when.start()] + " " + rest[when.end():]
    handle = HANDLE.search(rest)
    if handle:
        row["handle"] = handle.group(1)
        before = rest[:handle.start()].strip(" ,·•|")
        if before and len(before) <= 60:
            row["author"] = before.split(",")[-1].strip() or None
            rest = rest[handle.end():]
        else:
            rest = rest[:handle.start()] + " " + rest[handle.end():]
    elif first_token_author:
        first, _, after = rest.strip(" ,·•|").partition(",")
        if first.strip() and len(first.strip()) <= 40 and after.strip():
            row["author"] = first.strip()
            rest = after
    words = core.plain(SEPARATORS.sub(" ", rest)).strip(" ,.·•|")
    row["text"] = re.sub(r"\s+,", ",", words) or None
    if not row["author"] and row["handle"]:
        row["author"] = row["handle"]
    return row if (row["text"] or row["handle"]) else None


def post_rows(snapshot, roles=("Cell",), region=(.1, .92), skip=None, minimum=20):
    """The posts on a screen: each ``roles`` element in ``region`` whose label is long enough, parsed."""
    rows = []
    for e in core.rows(snapshot):
        if e.role not in roles and not core.image_only(snapshot) or not region[0] <= core.centre_y(e) <= region[1]:
            continue
        label = core.plain(e.label)
        if len(label) < minimum or skip is not None and skip.search(label):
            continue
        row = parse_post(label)
        if row and row not in rows:
            rows.append(row)
    return rows


def post_key(row):
    return (row.get("handle") or row.get("author"), (row.get("text") or "")[:60])


def feed_reader(kind, noun_plural, columns, *, roles=("Cell",), skip=None, max_swipes=None, block="table"):
    """A reader of posts across scrolls: ``fn(pack, io, snapshot, n, arg) -> ReadResult``."""
    def read(pack, io, snapshot, n, arg):
        rows, stopped, _ = core.collect(io, snapshot, lambda s: post_rows(s, roles=roles, skip=skip), post_key, n,
                                        max_swipes=max_swipes if max_swipes is not None else min(2 * n, 30))
        return ReadResult(kind, f"{len(rows)} {noun_plural}", columns, rows, stopped, block=block)
    return read


COMMENT_COLUMNS = (("author", "Author", "text"), ("text", "Comment", "text"), ("likes", "Likes", "number"),
                   ("when", "When", "text"))


def comments_reader(parse):
    """A reader of comments across scrolls (``parse`` gives a screen's rows), shaped like feed_reader's."""
    def read(pack, io, snapshot, n, arg):
        rows, stopped, _ = core.collect(io, snapshot, parse, lambda row: (row["author"], row["text"][:80]), n,
                                        max_swipes=min(n // 3 + 2, 12))
        return ReadResult("comments", f"{len(rows)} comments", COMMENT_COLUMNS, rows, stopped)
    return read


def stats(snapshot, words, top=.6):
    """{word: number} for each of ``words`` ("followers", "following"...): "2.1M Followers", "Followers 2.1M", or a
    number right above or left of its word."""
    out = {}
    elements = [e for e in snapshot.elements if core.centre_y(e) < top]
    for e in elements:
        label = core.plain(e.label)
        for word in words:
            found = re.search(rf"({WORDED_COUNT})\s+{word}\b|\b{word}\s*[:,]?\s*({WORDED_COUNT})", label, re.I)
            if found and word not in out:
                out[word] = core.number((found.group(1) or found.group(2)).replace(" ", ""))
    for e in elements:
        word = core.plain(e.label).casefold()
        if word in words and word not in out:
            near = [o for o in elements if o is not e and re.fullmatch(WORDED_COUNT, core.plain(o.label) or "")
                    and abs(core.centre_y(e) - core.centre_y(o)) < .06 and abs(o.rect[0] - e.rect[0]) < .25]
            if near:
                out[word] = core.number(core.plain(near[0].label).replace(" ", ""))
    return out


def profile_reader(words, labels=None):
    """A profile reader: the handle (from the argument or the screen) and ``words``' numbers."""
    labels = labels or {w: w.capitalize() for w in words}

    def read(pack, io, snapshot, n, arg):
        found = stats(snapshot, words)
        handle = (arg or "").strip() or next((m.group(1) for e in snapshot.elements if core.centre_y(e) < .4
                                             for m in [HANDLE.search(core.plain(e.label))] if m), None)
        follow = next((e.label for e in core.find_all(snapshot, r"^(Follow|Following|Follow back|Subscribe|"
                                                     r"Subscribed|Join|Joined|Connect|Message|Edit profile)$",
                                                     role="Button", region=(0, .6))), None)
        row = {"handle": handle, **{w: found.get(w) for w in words}, "state": follow}
        columns = (("handle", "Profile", "text"), *((w, labels[w], "number") for w in words), ("state", "You", "text"))
        return ReadResult("profile", handle or "This profile", columns, [row],
                          "" if found else "the profile's numbers aren't in the screen's tree")
    return read


def dm_reader(not_a_thread):
    """A conversations-list reader on the same row shape as Instagram's ("Unread, Name, last line, 2h")."""
    def parse(snapshot):
        rows = []
        for e in core.rows(snapshot):
            if e.role not in ("Cell", "Button", "Other") or not .1 < core.centre_y(e) < .9:
                continue
            if not_a_thread.match(core.plain(e.label)):
                continue
            row = parse_inbox_row(e.label)
            if row and row not in rows:
                rows.append(row)
        return rows

    def read(pack, io, snapshot, n, arg):
        rows, stopped, _ = core.collect(io, snapshot, parse, lambda row: (row["from"], row["last"]), n,
                                        max_swipes=min(n // 4 + 2, 12))
        unread = sum(1 for row in rows if row["unread"])
        return ReadResult("dms", f"{len(rows)} newest messages ({unread} unread)",
                          (("from", "From", "text"), ("unread", "Unread", "bool"), ("last", "Last message", "text"),
                           ("when", "When", "text")), rows, stopped)
    return read
