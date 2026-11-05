"""The Photos pack: your library's size, your albums and how many items each holds, by code (read-only)."""

import re

from . import core, rows
from .core import PickRow, ReadResult, Route, ScreenSpec, Tap, Verified, sig

BUNDLE = "com.apple.mobileslideshow"
TABS = (.86, 1.0)
TOP = (0.0, .16)
# Vision may read "0 Items" as "• O ltems": a count's O and an I read as l are tolerated.
ITEMS = r"^[•·]?\s*([\dO][\d,O]*) ([Il]tems?|Photos?|Videos?)$"
TILE = re.compile(r"^(?P<name>.+?)(?: Album)?, (?P<count>\d[\d,]*) (assets?|items?|photos?)\.?$", re.I)


def tab(label, what):
    return Tap(label, what, region=TABS)


class Photos(core.Pack):
    name, key, prefix, bundle = "Photos", "photos", "PH", BUNDLE
    verified = Verified("iOS 26.0.1", "2026-10-12", "iPhone 15 Pro over USB (test device)")
    tabs = rows.tabbar(("Library", "Collections"))
    never = re.compile(r"^(sign in|log in|delete|remove|share|edit|hide|favorite|add to favorites|love|add photo|add|"
                       r"add to album|duplicate|play|select|create a memory|move|recover|copy)\b", re.I)
    mentions = re.compile(r"\b(photos?|pictures?|pics|albums?|videos? in my|camera roll|photo library|"
                          r"screenshots)\b", re.I)

    screens = (
        ScreenSpec("library", "your library", (
            sig(r"^Library$", region=TOP, weight=2, must=True),
            sig(ITEMS, region=TOP, field="label", weight=2),
            sig(r"^(Select|Sort and Filter)$", region=TOP),
            sig(r"^Collections$", region=TABS),
        ), "recorded",
            ocr=(
                sig(r"^Library$", region=TOP, weight=2, must=True),
                sig(ITEMS, region=TOP, field="label", weight=2),
                sig(r"^Collections$", region=TABS),
            )),
        ScreenSpec("collections", "Collections", (
            sig(r"^Collections$", region=TOP, weight=2, must=True),
            sig(r"^(Memories|Pinned|Albums)\b", weight=2),
            sig(r"^Library$", region=TABS),
        ), "recorded",
            ocr=(
                sig(r"^Collections$", region=TOP, weight=2, must=True),
                sig(r"^(Memories|Pinned|Albums)\b", weight=2),
                sig(r"^Library$", region=TABS),
            )),
        ScreenSpec("albums", "your albums", (
            sig(r"^Albums$", region=TOP, weight=2, must=True),
            sig(r"^Collections$", "Button", region=TOP),
            sig(r", \d[\d,]* assets?\.?$", weight=2),
            sig(r"^(Add|More)$", region=TOP),
        ), "recorded",
            ocr=(
                sig(r"^Albums$", region=TOP, weight=2, must=True),
            )),
        ScreenSpec("album", "an album", (
            sig(ITEMS, region=(.1, .7), weight=2, must=True),
            sig(r"^(Play|Add Photo)$"),
            sig(r"^Back$", region=TOP),
            sig(r"^PhotosPagingView|PhotosUICore"),
            sig(r"^Library$", region=TOP, never=True),
        ), "recorded",
            ocr=(
                sig(ITEMS, region=(.1, .7), weight=2, must=True),
                sig(r"^Library$", region=TOP, never=True),
            )),
    )

    routes = {
        "library": Route("library", "your library", ("library",), (tab(r"^Library$", "the Library tab"),)),
        "collections": Route("collections", "Collections", ("collections",),
                             (tab(r"^Collections$", "the Collections tab"),)),
        "albums": Route("albums", "your albums", ("albums",), (
            tab(r"^Collections$", "the Collections tab"),
            Tap(r"^Albums( ›)?$", "Albums", region=(.15, .95), opens=True, wait=.3))),
        "album": Route("album", "an album", ("album",), (
            tab(r"^Collections$", "the Collections tab"),
            Tap(r"^Albums( ›)?$", "Albums", region=(.15, .95), opens=True, wait=.3),
            PickRow("album", roles=("Button", "Cell", "StaticText"), region=(.12, .95),
                    template=(r"^{arg}( Album)?, \d", r"^{arg}$", r"^{arg}\b"), scrolls=4)), arg="name"),
    }
    open_aliases = {"photos": "library", "all": "library", "collection": "collections"}
    read_aliases = {"count": "library", "photos": "library", "all": "library", "albums": "albums",
                    "folder": "album"}
    read_default = {"albums": 20}
    read_titles = {"library": "library", "albums": "albums", "album": "album"}
    nouns = {"albums": "album", "album": "album", "library": "library"}
    prompts = {
        "OPEN": ("- PH_OPEN where: go to a Photos screen by code, no taps from you: library | collections | albums | "
                 "album <name>. It never opens, shares or deletes a photo. If it can't, go on the usual way."),
        "READ": ("- PH_READ what [n]: read Photos by code: library (how many items) | albums n (each album with its "
                 "count) | album <name> (opens it: how many items). Opens the screen it needs."),
    }
    mcp_open_hint = "the library, Collections, your albums, or one album"
    mcp_read_hint = "how many items your library or an album holds, or every album with its count"
    read_columns = {
        "library": (("what", "What", "text"), ("items", "Items", "number")),
        "albums": (("album", "Album", "text"), ("items", "Items", "number")),
        "album": (("album", "Album", "text"), ("items", "Items", "number")),
    }

    @property
    def readers(self):
        return {"library": core.Reader(read_count, ("library",), "library"),
                "albums": core.Reader(read_albums, ("albums", "collections"), "albums"),
                "album": core.Reader(read_count, ("album",), "album")}

    back_control = rows.back_unless_act

    def confirm(self, route, arg, snapshot):
        if route != "album" or not arg:
            return None
        title = album_title(snapshot)
        if title and core.plain(arg).casefold() in title.casefold():
            return None
        return f"the album on screen is “{(title or '')[:40]}”, not “{core.plain(arg)[:40]}”"


def count_on(snapshot):
    found = core.find(snapshot, ITEMS, region=(.05, .7))
    if found is None:
        return None
    number = re.match(ITEMS, core.plain(found.label))
    return int(number.group(1).replace("O", "0").replace(",", "")) if number else None


def album_title(snapshot):
    count = core.find(snapshot, ITEMS, region=(.05, .7))
    if count is None:
        return None
    above = [e for e in rows.lines(snapshot, (.05, core.centre_y(count))) if e is not count and e.rect[0] < .5
             and .008 < core.centre_y(count) - core.centre_y(e) < .05]
    return core.plain(above[-1].label) if above else None


def read_count(pack, io, snapshot, n, arg):
    seen = pack.recognize(snapshot).screen
    name = "Library" if seen == "library" else (album_title(snapshot) or core.plain(arg) or "This album")
    number = count_on(snapshot)
    kind = "library" if seen == "library" else "album"
    return ReadResult(kind, name, pack.read_columns[kind],
                      [{"album" if seen != "library" else "what": name, "items": number}] if number is not None else [],
                      "" if number is not None else "the count isn't on the screen yet")


def parse_tiles(snapshot):
    out = []
    for element in rows.labelled(snapshot, ("Button", "Cell"), (.12, .97)):
        tile = TILE.match(core.plain(element.label))
        if tile:
            out.append({"album": tile.group("name"), "items": int(tile.group("count").replace(",", ""))})
    return out


def read_albums(pack, io, snapshot, n, arg):
    found, stopped, _ = core.collect(io, snapshot, parse_tiles, lambda r: r["album"], n, max_swipes=min(n // 6 + 1, 6))
    if stopped == "it reached the end of the list":
        stopped = ""
    return ReadResult("albums", f"{len(found)} albums", pack.read_columns["albums"], found, stopped)


PACK = Photos()
