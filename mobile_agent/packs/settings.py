"""The Settings pack (read-only): the iOS version and the phone's details, storage, battery, and any settings page, by
code."""

import re

from . import core, rows
from .core import PickRow, ReadResult, Route, ScreenSpec, TypeIn, Verified, sig

BUNDLE = "com.apple.Preferences"
TOP = (0.0, .14)
SEARCH = r"^(Q\s+)?(Search|Search Apps|Applications)$"
# Identifiers that name this phone: a reader leaves them out (they are never what a task is about).
PRIVATE = re.compile(r"^(serial number|imei\d?|meid|eid|iccid|wi-?fi address|bluetooth|seid|modem firmware|"
                     r"carrier lock|physical sim|esim|digital sim|udid)\b", re.I)
SIZE = r"(\d[\d,.]*)\s*(KB|MB|GB|TB)"
PAGES = ("About", "Battery", "iPhone Storage", "General", "Display & Brightness", "Wi-Fi", "Bluetooth", "Cellular",
         "Notifications", "Screen Time", "Privacy & Security", "Software Update", "Accessibility", "Power Mode")


def find_route(name, title, lands, words):
    """A route to a page through Settings' search: type its name, open the result that names it."""
    return Route(name, title, lands, (
        TypeIn(SEARCH, "the search field", text=words),
        PickRow("search result", roles=("Button", "Cell", "StaticText"), region=(.08, .9), text=words,
                template=(r"^{arg}(,|$)", r"^{arg}\b")),))


class Settings(core.Pack):
    name, key, prefix, bundle = "Settings", "settings", "ST", BUNDLE
    verified = Verified("iOS 26.0.1", "2026-10-12", "iPhone 15 Pro over USB (test device)")
    tabs = sig(SEARCH, region=(.84, 1.0))     # the search field at the bottom: every page that has it can search
    never = re.compile(r"^(sign in|log in|edit|erase|reset|sign out|turn off|turn on|shut down|update "
                       r"now|install|download and install|"
                       r"delete|offload|forget|empty|remove|restore|transfer|disable|enable|allow|don't allow|"
                       r"change passcode|turn passcode|emergency|sos)\b", re.I)
    mentions = re.compile(r"\b(settings?|ios version|version of ios|which ios|ios is|software version|storage|free "
                          r"space|space left|"
                          r"battery( health| percentage| level)?|low power|model name|serial|airplane mode|"
                          r"dark mode|true tone|wi-?fi network|bluetooth|screen time|about this (phone|iphone))\b",
                          re.I)

    screens = (
        ScreenSpec("root", "Settings", (
            sig(r"^Settings$", region=(.1, .22), weight=2, must=True),
            sig(r"Apple Account|Sign in to your iPhone|^Family$|^General$|^Accessibility$|^Wi-Fi\b"),
            sig(SEARCH, region=(.84, 1.0)),
            sig(r"^Settings$", "Button", region=TOP, never=True),
        ), "recorded",
            ocr=(
                sig(r"^Settings$", region=(.1, .22), weight=2, must=True),
                sig(r"Apple Account|Sign in to your iPhone|^Family$|^General$|^Accessibility$|^Wi-Fi\b"),
                sig(SEARCH, region=(.84, 1.0)),
            )),
        ScreenSpec("search", "search results", (
            sig(r"^Clear text$", weight=2),
            sig(r"^close$", region=(.4, 1.0)),
            sig(r", [A-Z][\w &]+( → .*)?$", region=(.08, .7), weight=2),
            sig(SEARCH, "SearchField", must=True),
            sig(r"^\S", "NavigationBar", never=True),
        ), "recorded"),
        ScreenSpec("about", "About", (
            sig(r"^About$", region=TOP, weight=2, must=True),
            sig(r"^(iOS Version|Model Name|Capacity|Available)\b"),
            sig(r"^(Name|Songs|Videos|Photos|Applications)\b"),
        ), "recorded",
            ocr=(
                sig(r"^About$", region=TOP, weight=2, must=True),
                sig(r"^(iOS Version|Model Name|Capacity|Available)\b"),
                sig(r"^(Name|Songs|Videos|Photos|Applications)\b"),
            )),
        ScreenSpec("storage", "iPhone Storage", (
            sig(r"^iPhone Storage$", region=TOP, weight=2, must=True),
            sig(rf"{SIZE} of {SIZE} used", field="text", weight=2),
            sig(r"^(Recommendations|Show All|System Data)$"),
        ), "recorded",
            ocr=(
                sig(r"^iPhone Storage$", region=TOP, weight=2, must=True),
                sig(rf"{SIZE} of {SIZE} used", field="text", weight=2),
                sig(r"^(Recommendations|Show All|System Data)$"),
            )),
        ScreenSpec("battery", "Battery", (
            sig(r"^Battery$", region=TOP, weight=2, must=True),
            sig(r"^Battery Health\b"),
            sig(r"^(Charging|Power Mode|Battery Percentage)\b"),
            sig(r"^\d{1,3}%$", field="text"),
        ), "recorded",
            ocr=(
                sig(r"^Battery$", region=TOP, weight=2, must=True),
                sig(r"^Battery Health\b"),
                sig(r"^(Charging|Power Mode|Battery Percentage)\b"),
                sig(r"^\d{1,3}%$", field="text"),
            )),
        ScreenSpec("page", "a settings page", (
            sig(r"^(Settings|General|Battery|Privacy & Security|Accessibility|Apps|Display & Brightness|Screen "
                r"Time|" r"Notifications|Wi-Fi|Bluetooth|Cellular|Sounds & Haptics)$", "Button", region=TOP, weight=2),
            sig(r"^\S", "NavigationBar", region=TOP),
            sig(r"^chevron$"),
            sig(r"^(About|iPhone Storage|Battery)$", "NavigationBar", never=True),
            sig(r"^Settings$", region=(.1, .22), never=True),
        ), "recorded",
            ocr=(
                sig(r"^Back$", "Button", region=TOP, weight=2),
                sig(r"^(About|iPhone Storage|Battery)$", region=TOP, never=True),
                sig(r"^Settings$", region=(.1, .22), never=True),
            )),
    )

    routes = {
        "root": Route("root", "Settings", ("root", "search")),
        "about": find_route("about", "About", ("about",), "About"),
        "storage": find_route("storage", "iPhone Storage", ("storage",), "iPhone Storage"),
        "battery": find_route("battery", "Battery", ("battery",), "Battery"),
        "find": Route("find", "a settings page", ("page", "about", "storage", "battery"), (
            TypeIn(SEARCH, "the search field"),
            PickRow("search result", roles=("Button", "Cell", "StaticText"), region=(.08, .9),
                    template=(r"^{arg}(,|$)", r"^{arg}\b")),), arg="query"),
    }
    hidden_routes = ()
    open_aliases = {"settings": "root", "home": "root", "version": "about", "ios": "about", "phone": "about",
                    "space": "storage", "page": "find", "search": "find", "go": "find"}
    read_aliases = {"version": "about", "ios": "about", "phone": "about", "device": "about", "space": "storage",
                    "free": "storage", "power": "battery", "setting": "page", "pane": "page"}
    read_default = {}
    read_titles = {"about": "phone's details", "storage": "storage", "battery": "battery", "page": "settings"}
    nouns = {"about": "detail", "storage": "app", "battery": "setting", "page": "setting"}
    prompts = {
        "OPEN": ("- ST_OPEN where: go to a Settings page by code through Settings' search, no taps from you: root | "
                 "about | storage | battery | page <name> (\"page Low Power Mode\"). It only goes there: it never "
                 "flips a switch or picks an option. If it can't, go on the usual way."),
        "READ": ("- ST_READ what: read Settings by code: about (iOS version, model, capacity, free space) | storage "
                 "(used, free, the biggest apps) | battery (level, health, charging) | page <name> (every row and "
                 "switch on that page, read-only). Opens the page it needs."),
    }
    mcp_open_hint = "About, iPhone Storage, Battery, or any settings page by name"
    mcp_read_hint = "the iOS version and phone details, storage used and free, battery, or a page's rows"
    read_columns = {
        "about": (("setting", "Setting", "text"), ("value", "Value", "text")),
        "storage": (("item", "Item", "text"), ("size", "Size", "text"), ("gb", "GB", "number")),
        "battery": (("setting", "Setting", "text"), ("value", "Value", "text")),
        "page": (("setting", "Setting", "text"), ("value", "Value", "text")),
    }

    @property
    def readers(self):
        return {"about": core.Reader(read_about, ("about",), "about"),
                "storage": core.Reader(read_storage, ("storage",), "storage"),
                "battery": core.Reader(read_battery, ("battery",), "battery"),
                "page": core.Reader(read_page, ("page", "about", "storage", "battery"), "find")}

    back_control = rows.back_unless_act

    def confirm(self, route, arg, snapshot):
        if route != "find" or not arg:
            return None
        title = page_title(snapshot)
        if title and title.casefold() != core.plain(arg).casefold() and not core.find(
                snapshot, rf"^{re.escape(core.plain(arg))}\b", region=(.1, .95)):
            return f"it opened {title}, which doesn't show “{core.plain(arg)[:40]}”"
        return None


def page_title(snapshot):
    """A page's title: the navigation bar's, or the line at the top centre."""
    bar = next((e for e in snapshot.elements if e.role == "NavigationBar" and e.label
                and not e.label.startswith(("_", "UI"))), None)
    if bar is not None:
        return core.plain(bar.label)
    top = [e for e in core.find_all(snapshot, r"\w", region=(.05, .12)) if .25 < e.rect[0] + e.rect[2] / 2 < .75]
    return core.plain(top[0].label) if top else None


def switches(snapshot):
    """{label: "on"|"off"} of the switches a page shows (a tree only: a picture can't tell a switch's state)."""
    return {core.plain(e.label): ("on" if str(e.value) in ("1", "on") else "off")
            for e in core.find_all(snapshot, r".", role="Switch") if e.label} if not core.image_only(snapshot) else {}


def page_rows(snapshot):
    """Every "setting: value" a page shows, private identifiers left out."""
    found = []
    for key, value in {**rows.pairs(snapshot, region=(.12, .9)), **switches(snapshot)}.items():
        if PRIVATE.match(key) or key in ("chevron",) or len(key) > 60:
            continue
        found.append({"setting": key, "value": value})
    return found


def _collect(io, snapshot, n, swipes):
    out, stopped, last = core.collect(io, snapshot, page_rows, lambda row: row["setting"], n, max_swipes=swipes)
    return out, ("" if stopped == "it reached the end of the list" else stopped), last


def read_about(pack, io, snapshot, n, arg):
    found, stopped, _ = _collect(io, snapshot, max(n, 12), 2)
    return ReadResult("about", "About this iPhone", pack.read_columns["about"], found, stopped)


def read_page(pack, io, snapshot, n, arg):
    found, stopped, _ = _collect(io, snapshot, max(n, 20), 2)
    return ReadResult("page", page_title(snapshot) or core.plain(arg) or "This page", pack.read_columns["page"],
                      found, stopped)


def gb(size, unit):
    value = float(size.replace(",", ""))
    return round(value * {"KB": 1e-6, "MB": 1e-3, "GB": 1, "TB": 1000}[unit.upper()], 2)


def read_storage(pack, io, snapshot, n, arg):
    """Used, total and free space, then the biggest apps (as the page sorts them, by size)."""
    text = str(snapshot.text or "")
    found = []
    used = re.search(rf"{SIZE} of {SIZE} used", text, re.I)
    if used:
        used_gb, total_gb = gb(used.group(1), used.group(2)), gb(used.group(3), used.group(4))
        found += [{"item": "Used", "size": f"{used.group(1)} {used.group(2)}", "gb": used_gb},
                  {"item": "Capacity", "size": f"{used.group(3)} {used.group(4)}", "gb": total_gb},
                  {"item": "Free", "size": f"{round(total_gb - used_gb, 2)} GB", "gb": round(total_gb - used_gb, 2)}]

    def apps(screen):
        out = []
        for key, value in rows.pairs(screen, region=(.3, .9)).items():
            size = re.fullmatch(SIZE, value or "")
            if size and not PRIVATE.match(key):
                out.append({"item": key, "size": value, "gb": gb(size.group(1), size.group(2))})
        return out
    listed, stopped, _ = core.collect(io, snapshot, apps, lambda row: row["item"], max(1, n), max_swipes=3)
    if stopped == "it reached the end of the list":
        stopped = ""
    if not used:
        stopped = stopped or "the page didn't show how much is used yet"
    return ReadResult("storage", "iPhone Storage", pack.read_columns["storage"], found + listed, stopped)


def read_battery(pack, io, snapshot, n, arg):
    found = []
    level = next((core.plain(e.label) for e in core.find_all(snapshot, r"^\d{1,3}%$", region=(.12, .3))), None)
    if level:
        found.append({"setting": "Level", "value": level})
    health = core.find(snapshot, r"^Battery Health$", region=(.3, .95))
    if health is not None:
        beside = [e for e in core.find_all(snapshot, r"^(Normal|Service|Maximum Capacity.*|\d{1,3}%)$")
                  if abs(core.centre_y(e) - core.centre_y(health)) < .03 and e is not health]
        if beside:
            found.append({"setting": "Battery Health", "value": core.plain(beside[0].label)})
    for row in page_rows(snapshot):
        if row["setting"] not in [r["setting"] for r in found]:
            found.append(row)
    return ReadResult("battery", "Battery", pack.read_columns["battery"], found,
                      "" if level else "the battery level isn't on the page")


PACK = Settings()
