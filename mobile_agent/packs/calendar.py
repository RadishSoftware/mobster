"""The Calendar pack: what's coming up and one event's details, by code (read-only)."""

import re

from . import core, rows
from .core import PickRow, ReadResult, Route, ScreenSpec, Tap, Verified, sig

BUNDLE = "com.apple.mobilecal"
TOP = (0.0, .14)
BOTTOM = (.86, 1.0)
DAY = re.compile(r"^((mon|tues|wednes|thurs|fri|satur|sun)day)\s*[-–]\s*([a-z]{3,9}\.? \d{1,2})(, \d{4})?$", re.I)
TIME = r"\d{1,2}(:\d{2})?\s?[AP]M"
SPAN = re.compile(rf"^(?:from )?({TIME})\s*(?:to|–|-)\s*({TIME})$", re.I)
EVENT_DATE = re.compile(r"^(mon|tues|wednes|thurs|fri|satur|sun)day, [a-z]{3,9}\.? \d{1,2}, \d{4}", re.I)


class Calendar(core.Pack):
    name, key, prefix, bundle = "Calendar", "calendar", "CA", BUNDLE
    verified = Verified("iOS 26.0.1", "2026-10-12", "iPhone 15 Pro over USB (test device)")
    tabs = sig(r"^Today$", region=BOTTOM)
    never = re.compile(r"^(sign in|log in|add|edit|delete.*|accept|decline|maybe|tentative|new event|new calendar|"
                       r"add calendar|share|invite.*|subscribe.*|done)\b", re.I)
    mentions = re.compile(r"\b(calendar|schedule|agenda|events?|appointments?|meetings?|"
                          r"what('s| is) (on|coming up)|when (and where )?is my|busy)\b", re.I)

    screens = (
        ScreenSpec("year", "the year", (
            sig(r"^YearViewContainerView$", weight=2),
            sig(r"^Current month, "),
            sig(r"^20\d\d$", region=(.1, .22), weight=2),
            sig(r"^Today$", region=BOTTOM),
        ), "recorded",
            ocr=(
                sig(r"^20\d\d$", region=(.1, .22), weight=2),
                sig(r"^(Jan|Feb|Mar)$"),
                sig(r"^Today$", region=BOTTOM),
            )),
        ScreenSpec("month", "the month", (
            sig(r"^MonthViewContainerView$", weight=2),
            sig(r"^(January|February|March|April|May|June|July|August|September|October|November|December)$",
                region=(.1, .22), weight=2),
            sig(r"^(<\s*)?20\d\d$", region=TOP),
            sig(r"^List$", region=TOP),
            sig(r"^Today$", region=BOTTOM),
            sig(r"^(Compact|Stacked)$", never=True),
        ), "recorded",
            ocr=(
                sig(r"^(January|February|March|April|May|June|July|August|September|October|November|December)$",
                    region=(.1, .22), weight=2),
                sig(r"^(<\s*)?20\d\d$", region=TOP),
                sig(r"^Today$", region=BOTTOM),
                sig(r"^(Compact|Stacked)$", never=True),
            )),
        ScreenSpec("list", "your upcoming events", (
            sig(r"^ListViewContainerView$", weight=3),
            sig(DAY.pattern, field="text", weight=2),
            sig(r"^all-day$", field="text"),
            sig(r", from .+ to .+", "Cell"),
            sig(r"^Today$", region=BOTTOM),
            sig(r"^(Compact|Stacked)$", never=True),
            sig(r"^EKEventView$", never=True),
        ), "recorded",
            ocr=(
                sig(DAY.pattern, field="text", weight=2),
                sig(r"^all-day$", field="text"),
                sig(rf"^{TIME}$", field="text"),
                sig(r"^Today$", region=BOTTOM),
                sig(r"^(Compact|Stacked)$", never=True),
                sig(r"^EKEventView$", never=True),
            )),
        ScreenSpec("event", "an event", (
            sig(r"^EKEventView$", weight=2),
            sig(EVENT_DATE.pattern, field="text", weight=2),
            sig(r"^Delete Event$", weight=2),
            sig(r"^Edit$", region=TOP),
            sig(r"^(Calendar|Alert|Second Alert|Travel Time|Invitees)$"),
        ), "recorded",
            ocr=(
                sig(EVENT_DATE.pattern, field="text", weight=2),
                sig(r"^Delete Event$", weight=2),
                sig(r"^Edit$", region=TOP),
                sig(r"^(Calendar|Alert|Second Alert|Travel Time|Invitees)$"),
            )),
        ScreenSpec("views", "the view menu", (
            sig(r"^Compact$", must=True, weight=2),
            sig(r"^Stacked$", weight=2),
            sig(r"^Details$"),
            sig(r"^List$"),
        ), "recorded",
            ocr=(
                sig(r"^Compact$", must=True, weight=2),
                sig(r"^Stacked$", weight=2),
                sig(r"^Details$"),
                sig(r"^List$"),
            )),
    )

    routes = {
        "today": Route("today", "today", ("month", "list"), (Tap(r"^Today$", "Today", region=BOTTOM),)),
        "list": Route("list", "your upcoming events", ("list",), (
            Tap(r"^Today$", "Today", region=BOTTOM, wait=.3),
            Tap(r"^List$", "the List button", region=TOP, opens=True),
            Tap(r"^List$", "List in the view menu", region=(.15, .4), opens=True, optional=True, wait=.3))),
        "event": Route("event", "an event", ("event",), (
            Tap(r"^Today$", "Today", region=BOTTOM, wait=.3),
            Tap(r"^List$", "the List button", region=TOP, opens=True),
            Tap(r"^List$", "List in the view menu", region=(.15, .4), opens=True, optional=True, wait=.3),
            PickRow("event", roles=("Cell", "StaticText"), region=(.12, .9), template=(r"^{arg}(,|$)", r"^{arg}\b"),
                    scrolls=6)), arg="name"),
    }
    open_aliases = {"upcoming": "list", "events": "list", "agenda": "list", "schedule": "list", "day": "today"}
    read_aliases = {"upcoming": "events", "agenda": "events", "schedule": "events", "list": "events",
                    "next": "events", "details": "event"}
    read_default = {"events": 10}
    read_titles = {"events": "upcoming events", "event": "event"}
    nouns = {"events": "event", "event": "event"}
    prompts = {
        "OPEN": ("- CA_OPEN where: go to a Calendar screen by code, no taps from you: today | list (your upcoming "
                 "events) | event <title>. It only reads: it never adds, edits or deletes. If it can't, go on the "
                 "usual way."),
        "READ": ("- CA_READ what [n]: read Calendar by code: events n (your next n events from today: title, date, "
                 "time or all-day, place) | event <title> (opens it: its date, time, place, calendar, alert). Opens "
                 "the screen it needs."),
    }
    mcp_open_hint = "today, the list of upcoming events, or one event by its title"
    mcp_read_hint = "your next events with their dates, times and places, or one event's details"
    read_columns = {
        "events": (("title", "Event", "text"), ("date", "Date", "text"), ("time", "Time", "text"),
                   ("place", "Where", "text"), ("calendar", "Calendar", "text")),
        "event": (("title", "Event", "text"), ("date", "Date", "text"), ("time", "Time", "text"),
                  ("place", "Where", "text"), ("calendar", "Calendar", "text"), ("alert", "Alert", "text")),
    }

    @property
    def readers(self):
        return {"events": core.Reader(read_events, ("list",), "list"),
                "event": core.Reader(read_event, ("event",), "event")}

    back_control = rows.back_unless_act

    def confirm(self, route, arg, snapshot):
        if route != "event" or not arg:
            return None
        title = event_title(snapshot)
        if title and core.plain(arg).casefold() in title.casefold():
            return None
        return f"the event on screen is “{(title or '')[:40]}”, not “{core.plain(arg)[:40]}”"


def _time(text):
    span = SPAN.match(core.plain(text).replace(" ", " "))
    return f"{span.group(1)} – {span.group(3)}" if span else None


def parse_cell(label):
    """A timed event's cell ("MobsterBench Review, from 2:00PM to 3:00PM, Room 42, Sam") as a row."""
    tokens = [t.strip() for t in core.plain(label).replace(" ", " ").split(", ")]
    span = next((i for i, t in enumerate(tokens) if SPAN.match(t)), None)
    if span is None or span == 0:
        return None
    rest = tokens[span + 1:]
    return {"title": ", ".join(tokens[:span]), "date": None, "time": _time(tokens[span]),
            "place": ", ".join(rest[:-1]) or None if len(rest) > 1 else None,
            "calendar": rest[-1] if rest else None}


def parse_list(snapshot):
    """The events the list shows, top to bottom, each dated by the day header above it when one shows."""
    out, day = [], None
    texts = rows.lines(snapshot, (.12, .9))
    if core.image_only(snapshot):
        for index, line in enumerate(texts):
            text = core.plain(line.label).replace(" ", " ")
            header = DAY.match(text)
            if header:
                day = f"{header.group(1)}, {header.group(3)}"
                continue
            if line.rect[0] > .6 or re.match(r"^[*•]\s", text):
                continue      # a time, "all-day", or a place under the title
            right = [e for e in texts if e.rect[0] > .6 and abs(core.centre_y(e) - core.centre_y(line)) < .015]
            if not right:
                continue
            under = [e for e in texts if e.rect[0] > .6 and .012 < core.centre_y(e) - core.centre_y(line) < .045]
            place = next((re.sub(r"^[*•]\s*", "", core.plain(e.label)) for e in texts
                          if e.rect[0] < .6 and .012 < core.centre_y(e) - core.centre_y(line) < .045), None)
            first = core.plain(right[0].label).replace(" ", " ")
            when = "all-day" if first.casefold() == "all-day" else (
                f"{first} – {core.plain(under[0].label)}" if under else first)
            out.append({"title": text, "date": day, "time": when,
                        "place": place if when != "all-day" else None, "calendar": None})
        return out
    for element in texts:
        text = core.plain(element.label)
        header = DAY.match(text.replace(" ", " "))
        if header:
            day = f"{header.group(1)}, {header.group(3)}"
            continue
        if element.role == "Cell":
            row = parse_cell(text)
            if row:
                out.append({**row, "date": day})
            continue
        if element.role == "StaticText" and text.casefold() != "all-day" and element.rect[0] < .6:
            beside = [e for e in texts if e.rect[0] > .6 and abs(core.centre_y(e) - core.centre_y(element)) < .015
                      and core.plain(e.label).casefold() == "all-day"]
            if beside:
                out.append({"title": text, "date": day, "time": "all-day", "place": None, "calendar": None})
    return out


def read_events(pack, io, snapshot, n, arg):
    found, stopped, _ = core.collect(io, snapshot, parse_list, lambda r: (r["title"], r["date"], r["time"]), n,
                                     max_swipes=min(n // 4 + 2, 8))
    return ReadResult("events", f"Your next {len(found)} events", pack.read_columns["events"], found, stopped)


def event_title(snapshot):
    cell = core.find(snapshot, r"\w", role="Cell", region=(.1, .3))
    if cell is not None:
        return core.plain(cell.label)
    first = [e for e in rows.lines(snapshot, (.1, .3)) if not EVENT_DATE.match(core.plain(e.label))]
    return core.plain(first[0].label) if first else None


def read_event(pack, io, snapshot, n, arg):
    texts = [core.plain(e.label).replace(" ", " ") for e in rows.lines(snapshot, (.1, .9))]
    date = next((t for t in texts if EVENT_DATE.match(t)), None)
    time = next((t for t in texts if re.match(rf"^({TIME})\s*[–-]\s*({TIME})$", t, re.I) or t.casefold() == "all-day"),
                None)
    place = next((e.value for e in snapshot.elements if e.role == "TextView" and e.value
                  and core.centre_y(e) < .3), None)
    if place is None and core.image_only(snapshot):
        title = event_title(snapshot)
        after = texts[texts.index(title) + 1:] if title in texts else []
        place = next((t for t in after if t != date and t != time and not EVENT_DATE.match(t)), None)
    found = rows.pairs(snapshot, region=(.4, .9))
    row = {"title": event_title(snapshot), "date": date, "time": time, "place": place,
           "calendar": found.get("Calendar"), "alert": found.get("Alert")}
    return ReadResult("event", row["title"] or "This event", pack.read_columns["event"], [row],
                      "" if date else "the event's date isn't on the screen")


PACK = Calendar()
