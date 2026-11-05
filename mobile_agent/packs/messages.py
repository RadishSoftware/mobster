"""The Messages pack (iMessage and SMS): the conversation list, a conversation, unread counts, by code."""

import re

from . import core, rows
from .core import PickRow, ReadResult, Route, ScreenSpec, Tap, Verified, sig

BUNDLE = "com.apple.MobileSMS"
TOP = (0.0, .17)
FIELD = r"^(iMessage|Text Message|RCS|Message)\b"
LIST_TITLE = r"^(Messages|Unread|Known Senders|Unknown Senders|Spam|Recently Deleted|Drafts)$"
NOT_A_ROW = re.compile(r"^(edit|filter|compose|search|messages|unread|syncing with icloud.*|pinned|cancel|done|"
                       r"new message|to:|no results|contact photo.*|unknown senders.*|known senders|spam|"
                       r"recently deleted|drafts|manage filtering|filter by)$", re.I)


class Messages(core.Pack):
    name, key, prefix, bundle = "Messages", "messages", "MS", BUNDLE
    verified = Verified("iOS 26.0.1", "2026-10-12", "iPhone 15 Pro over USB (test device)")
    tabs = sig(r"^Edit$", region=TOP)          # the conversation list: Back from a conversation lands on it
    never = re.compile(r"^(send|undo send|edit|delete|delete all|recover|report junk|block.*|hide alerts|show alerts|"
                       r"pin|unpin|mark as (read|unread)|leave this conversation|remove|tapback.*|react.*|"
                       r"call|facetime|video|audio|share my location|stop sharing my location)\b", re.I)
    mentions = re.compile(r"\b(i ?messages?|texts?|texted|texting|text messages?|sms|the messages app|"
                          r"(in|on|my|unread) messages)\b", re.I)

    screens = (
        ScreenSpec("inbox", "your conversations", (
            sig(LIST_TITLE, region=TOP, weight=2, must=True),
            sig(r"^Edit$", region=TOP, weight=2),
            sig(r"^(filter|Compose)$", region=TOP),
            sig(r"^Search$", region=(.8, 1.0)),
            sig(r",\s*\d{1,2}:\d{2}\s?[AP]M$|,\s*Yesterday$|^\d{1,2}:\d{2}\s?[AP]M$|^Yesterday$", field="text"),
            sig(r", Pinned$"),
            sig(FIELD, region=(.45, 1.0), never=True),
            sig(r"^To:", never=True),
        ), "recorded",
            ocr=(
                sig(LIST_TITLE, region=TOP, weight=2, must=True),
                sig(r"^Edit$", region=TOP, weight=2),
                sig(r"^Search$", region=(.8, 1.0)),
                sig(r",\s*\d{1,2}:\d{2}\s?[AP]M$|,\s*Yesterday$|^\d{1,2}:\d{2}\s?[AP]M$|^Yesterday$", field="text"),
                sig(r"^To:", never=True),
            )),
        ScreenSpec("thread", "a conversation", (
            sig(FIELD, region=(.45, 1.0), weight=3, must=True),
            sig(r"^(Back|Messages|\d+ unread)$", region=TOP),
            sig(r"^(Delivered|Read( \d.*)?|Today \d.*|Yesterday \d.*|iMessage|Text Message)$", field="text"),
            sig(r"^Edit$", region=TOP, never=True),
            sig(r"^To:", never=True),
            sig(r"^New Message$", never=True),
        ),
            ocr=(
                sig(FIELD, region=(.45, 1.0), weight=3, must=True),
                sig(r"^(Delivered|Read( \d.*)?|Today \d.*|Yesterday \d.*|iMessage|Text Message)$", field="text"),
                sig(r"^Edit$", region=TOP, never=True),
                sig(r"^To:", never=True),
                sig(r"^New Message$", never=True),
            )),
        ScreenSpec("compose", "a new message", (
            sig(r"^New Message$", weight=2, must=True),
            sig(r"^To:", weight=2),
            sig(r"^Cancel$", region=TOP),
            sig(r"^Add Contact$"),
        ), "recorded",
            ocr=(
                sig(r"^New Message$", weight=2, must=True),
                sig(r"^To:", weight=2),
            )),
    )

    routes = {
        "inbox": Route("inbox", "your conversations", ("inbox",)),
        "thread": Route("thread", "a conversation", ("thread",),
                        (PickRow("conversation", roles=("Cell", "StaticText", "Button"), region=(.12, .9),
                                 template=(r"^{arg}(,|$)", r"^{arg}\b"), scrolls=3),), arg="name"),
        "compose": Route("compose", "a new message", ("compose",),
                         (Tap(r"^Compose$", "Compose", region=TOP, opens=True, wait=.3),)),
    }
    controls = {
        "reply": core.Control("thread", FIELD, "message field", region=(.45, 1.0)),
        "compose": core.Control("compose", r"^To:", "To field"),
    }
    open_aliases = {"conversations": "inbox", "list": "inbox", "messages": "inbox", "texts": "inbox",
                    "conversation": "thread", "chat": "thread", "text": "reply", "new": "compose", "write": "compose"}
    read_aliases = {"conversations": "inbox", "messages": "inbox", "texts": "inbox", "list": "inbox",
                    "recent": "inbox", "chats": "inbox", "conversation": "thread", "chat": "thread"}
    read_default = {"inbox": 10, "unread": 10, "thread": 20}
    read_titles = {"inbox": "conversations", "unread": "unread conversations", "thread": "messages"}
    nouns = {"inbox": "conversation", "unread": "unread conversation", "thread": "message"}
    prompts = {
        "OPEN": ("- MS_OPEN where: go to a Messages screen by code, no taps from you: inbox | thread <name> | compose; "
                 "or to an act's control: reply <name> (it opens that conversation, checks the name on screen and "
                 "names the message field; it never types or sends). If it can't, go on the usual way."),
        "READ": ("- MS_READ what [n]: read Messages by code from the screen, no screenshots: inbox n (newest "
                 "conversations: who, unread, last message, when; pinned ones flagged) | unread n | thread <name> n "
                 "(opens it, which marks it read). Opens the screen it needs."),
    }
    mcp_open_hint = "the conversation list, a conversation by name, a new message"
    mcp_read_hint = "the newest conversations with unread flags, only the unread ones, or one conversation"
    read_columns = {
        "inbox": (("from", "From", "text"), ("unread", "Unread", "bool"), ("last", "Last message", "text"),
                  ("when", "When", "text"), ("pinned", "Pinned", "bool"), ("muted", "Muted", "bool")),
        "unread": (("from", "From", "text"), ("last", "Last message", "text"), ("when", "When", "text")),
        "thread": (("from", "From", "text"), ("text", "Message", "text"), ("when", "When", "text")),
    }

    @property
    def readers(self):
        return {"inbox": core.Reader(read_inbox, ("inbox",), "inbox"),
                "unread": core.Reader(read_unread, ("inbox",), "inbox"),
                "thread": core.Reader(read_thread, ("thread",), "thread")}

    back_control = rows.back_unless_act

    def confirm(self, route, arg, snapshot):
        if route != "thread" or not arg:
            return None
        shown = thread_name(snapshot)
        if shown and same_person(shown, arg):
            return None
        return (f"the conversation on screen is {('“' + shown[:40] + '”') if shown else 'one it can’t name'}, "
                f"not “{core.plain(arg)[:40]}”")


def same_person(shown, wanted):
    """Whether the name at the top of a conversation is the person asked for: the same name, the full name when the task
    gave a first name ("Sam Lee" for "Sam"), or a first name alone on screen for a full name asked."""
    a = core.plain(shown).split(",")[0].strip().casefold()
    b = core.plain(wanted).strip().casefold().lstrip("@")
    if not a or not b:
        return False
    return a == b or a.startswith(b + " ") or (len(a.split()) == 1 and b.startswith(a + " "))


COLUMNS = Messages.read_columns["inbox"]


def parse_row(label):
    """One conversation cell ("Sam Lee, Unread, see you at 6, 9:55 PM") as a row, or None."""
    text = core.plain(label)
    if not text or NOT_A_ROW.match(text):
        return None
    tokens, flags = rows.parts(text)
    if not tokens:
        return None
    pinned = "pinned" in flags
    if pinned:      # a pinned cell is the name alone, and a group's name has commas of its own ("Allen, Mom & Dad")
        return {"from": ", ".join(tokens), "unread": "unread" in flags, "last": None, "when": None, "pinned": True,
                "muted": ("muted" in flags) or None}
    when = tokens[-1] if len(tokens) > 1 and rows.WHEN.match(tokens[-1]) else None
    body = tokens[1:-1] if when else tokens[1:]
    if not pinned and not when and not body:
        return None
    return {"from": tokens[0], "unread": "unread" in flags, "last": ", ".join(body) or None, "when": when,
            "pinned": pinned or None, "muted": ("muted" in flags) or None}


def parse_list(snapshot):
    """The conversations a list screen shows, top to bottom (pinned ones first, as the app draws them)."""
    if core.image_only(snapshot):
        out = []
        for row in rows.anchored(snapshot, region=(.14, .9)):
            name = " ".join(row["head"]).strip()
            if not name or NOT_A_ROW.match(name):
                continue
            out.append({"from": name, "unread": None, "last": " ".join(row["body"]) or None, "when": row["anchor"],
                        "pinned": None, "muted": None})
        return out
    out = []
    for element in rows.labelled(snapshot, ("Cell",), (.12, .9)):
        row = parse_row(element.label)
        if row and not any(r["from"] == row["from"] and r["last"] == row["last"] for r in out):
            out.append(row)
    return out


def _key(row):
    return (row["from"], row["last"], row["when"])


def read_inbox(pack, io, snapshot, n, arg):
    """The ``n`` newest conversations (pinned ones are flagged and don't count: a pinned cell shows no time)."""
    def recent(screen):
        return [row for row in parse_list(screen) if not row["pinned"]]
    pinned = [row for row in parse_list(snapshot) if row["pinned"]]
    found, stopped, _ = core.collect(io, snapshot, recent, _key, n, max_swipes=min(n // 5 + 2, 10))
    unread = sum(1 for row in found + pinned if row["unread"])
    title = f"{len(found)} newest conversations" + (f" ({unread} unread)" if not core.image_only(snapshot) else "")
    return ReadResult("inbox", title, COLUMNS, found + pinned, stopped)


def read_unread(pack, io, snapshot, n, arg):
    """Only the unread conversations, scrolling until ``n`` or the list's end (a tree only: a picture can't show the
    unread dot)."""
    if core.image_only(snapshot):
        raise core.Lost("an unread dot isn't text, so it can't tell unread from read on this connection")
    found, stopped, _ = core.collect(io, snapshot, lambda s: [r for r in parse_list(s) if r["unread"]], _key, n,
                                     max_swipes=8)
    if stopped == "it reached the end of the list":
        stopped = ""
    return ReadResult("unread", f"{len(found)} unread conversations", pack.read_columns["unread"], found, stopped)


STAMP = re.compile(r"^((today|yesterday|mon|tue|wed|thu|fri|sat|sun)\w*\s+)?\d{1,2}:\d{2}\s?[AP]M$|"
                   r"^(today|yesterday)$|^\w{3}, \w{3} \d{1,2}( at \d{1,2}:\d{2}\s?[AP]M)?$", re.I)
STATUS = re.compile(r"^(delivered|read( .*)?|sent as text message|not delivered|edited|kept|imessage|"
                    r"text message.*|sms|rcs|typing.*)$", re.I)


def thread_name(snapshot):
    """The name at the top of a conversation (its header button, or the title on a picture)."""
    for element in sorted(core.find_all(snapshot, r".", region=TOP), key=lambda e: abs(e.rect[0] + e.rect[2] / 2 - .5)):
        label = core.plain(element.label)
        if not label or core.BACK_LABEL.match(label) or re.match(
                r"^(\d+ unread|messages|details|facetime|video|audio call|call|info|\d{1,2}:\d{2}.*)$", label, re.I):
            continue
        if (element.role in ("Button", "StaticText", "Other", "NavigationBar")
                and .25 < element.rect[0] + element.rect[2] / 2 < .75):
            return label.split(",")[0].strip()
    return None


def parse_thread(snapshot):
    """The messages a conversation shows, oldest first: {from: "them"|"you", text, when}."""
    field = core.find(snapshot, FIELD, region=(.45, 1.0))
    bottom = core.centre_y(field) - .015 if field is not None else .88
    out, when = [], None
    for element in rows.lines(snapshot, (TOP[1], bottom)):
        if element.role not in ("StaticText", "Cell", "Other", "TextView") or element.editable:
            continue
        text = core.plain(element.label)
        x, _, w, _ = element.rect
        centre = x + w / 2
        if STAMP.match(text) and .3 < centre < .7:
            when = text
            continue
        if STATUS.match(text) or core.BACK_LABEL.match(text):
            continue
        side = "you" if x > .3 and centre > .55 else "them"
        if not out or out[-1]["text"] != text:
            out.append({"from": side, "text": text, "when": when})
    return out


def read_thread(pack, io, snapshot, n, arg):
    name = thread_name(snapshot) or arg
    found = parse_thread(snapshot)
    swipes = 0
    while len(found) < n and swipes < 4:
        try:
            after = io.swipe(snapshot, "down")      # older messages are above
        except core.Lost:
            break
        swipes += 1
        if after.content_fingerprint == snapshot.content_fingerprint:
            break
        older = [row for row in parse_thread(after) if row not in found]
        if not older:
            break
        found, snapshot = older + found, after
    for row in found:
        row["from"] = (name or "Them") if row["from"] == "them" else "You"
    found = found[-n:]
    return ReadResult("thread", f"Your messages with {name}" if name else "This conversation",
                      pack.read_columns["thread"], found, "" if len(found) >= n else "it reached the start of what "
                                                                                       "the conversation shows")


PACK = Messages()
