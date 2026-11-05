"""The Maps pack (Apple Maps): find a place and read its card, or a search's results, by code (read-only)."""

import re

from . import core, rows
from .core import ReadResult, Route, ScreenSpec, TypeIn, Verified, sig

BUNDLE = "com.apple.Maps"
SEARCH = r"^(Q\s+)?(Apple Maps|Search Maps|Search)$"
DRIVE = r"^(\d+ (hr|min)( \d+ min)?)(,? (driving|walking|transit|cycling|by car))?$"


class Maps(core.Pack):
    name, key, prefix, bundle = "Maps", "maps", "MP", BUNDLE
    verified = Verified("iOS 26.0.1", "2026-10-12", "iPhone 15 Pro over USB (test device)")
    tabs = sig(SEARCH, region=(.4, 1.0))
    never = re.compile(r"^(edit|sign in|log in|directions|go|start|end route|call|share|add|favorite|save|"
                       r"rate this place|"
                       r"report an issue|tickets|order|reserve|book|look around|add to guide|remove)\b", re.I)
    mentions = re.compile(r"\b(maps?|address of|directions to|how far|drive to|nearest|near me|opening hours|"
                          r"open now|phone number of|where is (the|a) )", re.I)

    screens = (
        ScreenSpec("home", "Maps", (
            sig(SEARCH, "SearchField", region=(.4, 1.0), must=True, weight=2),
            sig(r"^(Places|Recents)\b", weight=2),
            sig(r"^(Home|Work)(, Add)?$"),
            sig(r"^Close$", "Button", region=(.4, .7), never=True),
        ), "recorded",
            ocr=(
                sig(SEARCH, region=(.4, 1.0), must=True, weight=2),
                sig(r"^(Places|Recents)\b", weight=2),
                sig(r"^(Home|Work)(, Add)?$"),
            )),
        ScreenSpec("place", "a place", (
            sig(r"^Card controller$"),
            sig(DRIVE, weight=2),
            sig(r"^(Call|Website|Directions)$", weight=2),
            sig(r"^(Hours|About|Open|Closed|Ratings & Guides)$"),
            sig(r"^(Address|Phone|Website), "),
        ), "recorded",
            ocr=(
                sig(DRIVE, weight=2),
                sig(r"^(Call|Website|Directions)$", weight=2),
                sig(r"^(Hours|About|Open|Closed|Ratings & Guides)$"),
            )),
        ScreenSpec("results", "search results", (
            sig(SEARCH, "SearchField", must=True),
            sig(r"^Close$", region=(.4, 1.0)),
            sig(r"^(Searching…|In progress)$", never=True),
            sig(r"^(Call|Website)$", never=True),
            sig(r"\b\d+(\.\d)? (mi|km|ft|m)\b", field="text", weight=2),
        ), "recorded",
            ocr=(
                sig(r"^Close$", region=(.4, 1.0)),
                sig(r"^(Call|Website)$", never=True),
                sig(r"\b\d+(\.\d)? (mi|km|ft|m)\b", field="text", weight=2),
            )),
    )

    routes = {
        "home": Route("home", "Maps", ("home",)),
        "search": Route("search", "search results", ("results", "place"), (
            TypeIn(SEARCH, "the search field", submit=True),), link="maps://?q={arg}", arg="query"),
        "place": Route("place", "a place", ("place",), (
            TypeIn(SEARCH, "the search field", submit=True),), link="maps://?q={arg}", arg="query"),
    }
    open_aliases = {"find": "search", "look": "search"}
    read_aliases = {"address": "place", "info": "place", "details": "place", "find": "results", "search": "results"}
    read_default = {"results": 8}
    read_titles = {"place": "place", "results": "places"}
    nouns = {"place": "detail", "results": "place"}
    prompts = {
        "OPEN": ("- MP_OPEN where: go to a Maps screen by code: home | search <words> | place <name or address> (its "
                 "card). It never starts directions or calls. If it can't, go on the usual way."),
        "READ": ("- MP_READ what: read Maps by code: place <name> (finds it and reads its card: address, kind, drive "
                 "time, hours, phone, website, rating) | results <words> n (the places a search lists). Opens what "
                 "it needs."),
    }
    mcp_open_hint = "a search, or one place's card"
    mcp_read_hint = "a place's address, phone, hours, website and drive time, or a search's places"
    read_columns = {
        "place": (("field", "Field", "text"), ("value", "Value", "text")),
        "results": (("place", "Place", "text"), ("detail", "Detail", "text"), ("distance", "Distance", "text")),
    }

    @property
    def readers(self):
        return {"place": core.Reader(read_place, ("place",), "place"),
                "results": core.Reader(read_results, ("results",), "search")}

    def read(self, io, what, n=10, arg=None):
        if what == "place" and arg:
            # A search that lands on its results, not on a card, opens the first place.
            landing = core.navigate(self, io, "place", arg)
            if not landing.ok and landing.seen.screen != "results":
                raise core.Lost(landing.why)
            snapshot = landing.snapshot if landing.ok else open_first(self, io, landing.snapshot)
            result = read_place(self, io, snapshot, n, arg)
            result.screen = "place"
            return result
        return super().read(io, what, n, arg)


def parse_result(label):
    """One search result's label ("Name, kind · 1.2 mi, address") as {place, detail, distance}, or None."""
    text = core.plain(label)
    if re.match(r"^(close|share|call|website|directions|card controller|dictate|more)$", text, re.I):
        return None
    parts = [p.strip() for p in re.split(r",\s|\s·\s", text) if p.strip()]
    if len(parts) < 2:
        return None
    distance = next((p for p in parts if re.fullmatch(r"\d+(\.\d+)? ?(mi|km|ft|m)", p)), None)
    return {"place": parts[0], "detail": ", ".join(p for p in parts[1:] if p != distance) or None,
            "distance": distance}


def result_rows(snapshot):
    """The places a search lists, top to bottom."""
    return [row for row in (parse_result(e.label) for e in rows.labelled(snapshot, ("Cell", "Button"), (.6, .97)))
            if row]


def open_first(pack, io, snapshot):
    """From a search's results, open the first place's card."""
    first = next((e for e in rows.labelled(snapshot, ("Cell", "Button"), (.6, .97)) if parse_result(e.label)), None)
    if first is None:
        raise core.Lost("the search listed no place it could open")
    after = io.tap(first, snapshot, opens=True, pattern=r".*", check=parse_result(first.label)["place"])
    if pack.recognize(after).screen != "place":
        raise core.Lost("the first result didn't open a place's card")
    return after


def read_results(pack, io, snapshot, n, arg):
    found = result_rows(snapshot)[:n]
    return ReadResult("results", f"{len(found)} places", pack.read_columns["results"], found,
                      "" if found else "the search listed no places")


def card(snapshot):
    """What a place's card shows: name, kind, drive time, and its "Address, …"-style rows."""
    out = {}
    texts = rows.lines(snapshot, (.5, .97))
    close = core.find(snapshot, r"^(Close|Share)$", region=(.4, .97))
    top = core.centre_y(close) if close is not None else .55
    names = [e for e in texts if e.role == "StaticText" and .25 < e.rect[0] + e.rect[2] / 2 < .75
             and -.01 < core.centre_y(e) - top < .06 and not re.match(DRIVE, core.plain(e.label))]
    if names:
        out["Name"] = core.plain(names[0].label)
        kind = next((core.plain(e.value or e.label) for e in texts
                     if 0 < core.centre_y(e) - core.centre_y(names[0]) < .05 and e is not names[0]
                     and core.plain(e.value or e.label) not in (out["Name"], "LinkViewTextView")), None)
        if kind and not re.match(DRIVE, kind):
            out["Kind"] = kind
    drive = next((re.match(DRIVE, core.plain(e.label)) for e in texts if re.match(DRIVE, core.plain(e.label))), None)
    if drive:
        out["Drive time"] = drive.group(1)
    for element in core.find_all(snapshot, r"^(Address|Phone|Website|Hours|Rating|Ratings), ", region=(.1, 1.0)):
        key, _, value = core.plain(element.label).partition(", ")
        out.setdefault(key, value)
    if core.image_only(snapshot):
        for key in ("Address", "Phone", "Website", "Hours"):
            label = core.find(snapshot, rf"^{key}$")
            if label is not None and key not in out:
                under = [e for e in texts if 0 < core.centre_y(e) - core.centre_y(label) < .06
                         and abs(e.rect[0] - label.rect[0]) < .1]
                if under:
                    out[key] = " ".join(core.plain(e.label) for e in under[:3])
    status = core.find(snapshot, r"^(Open|Closed|Open 24 hours|Closes .*|Opens .*)$")
    if status is not None:
        out.setdefault("Open now", core.plain(status.label))
    return out


def read_place(pack, io, snapshot, n, arg):
    """A place's card, swiped up until its address shows (at most three swipes)."""
    found = card(snapshot)
    for _ in range(3):
        if "Address" in found:
            break
        try:
            after = io.swipe(snapshot, "up")
        except core.Lost:
            break
        if after.content_fingerprint == snapshot.content_fingerprint:
            break
        snapshot = after
        found = {**found, **{k: v for k, v in card(snapshot).items() if k not in found}}
    order = ("Name", "Kind", "Address", "Drive time", "Open now", "Hours", "Phone", "Website", "Rating")
    out = [{"field": key, "value": found[key]} for key in order if found.get(key)]
    return ReadResult("place", found.get("Name") or core.plain(arg) or "This place", pack.read_columns["place"], out,
                      "" if "Address" in found else "its address isn't on the card")


PACK = Maps()
