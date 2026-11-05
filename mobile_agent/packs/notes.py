"""The Notes pack: your folders, a folder's notes, a search, and one note's text, by code (read-only)."""

import re

from . import core, rows
from .core import PickRow, ReadResult, Route, ScreenSpec, TypeIn, Verified, sig

BUNDLE = "com.apple.mobilenotes"
TOP = (0.0, .14)
SEARCH = r"^(Q\s+)?Search$"
DATE = (r"(\d{1,2}:\d{2}\s?[AP]M|yesterday|(mon|tues|wednes|thurs|fri|satur|sun)day|\d{1,2}/\d{1,2}/\d{2,4}|"
        r"(modified )?[A-Z][a-z]+ \d{1,2}, \d{4})")


class Notes(core.Pack):
    name, key, prefix, bundle = "Notes", "notes", "NT", BUNDLE
    verified = Verified("iOS 26.0.1", "2026-10-12", "iPhone 15 Pro over USB (test device)")
    tabs = sig(SEARCH, region=(.84, 1.0))
    never = re.compile(r"^(sign in|log in|delete|move note|move|lock|unlock|pin note|unpin note|share|new note|"
                       r"new folder|"
                       r"compose|checklist|attachments|handwriting|writing tools|markup|scan|edit|remove|"
                       r"recently deleted)\b", re.I)
    mentions = re.compile(r"\b(notes?|notes app|jot(ted)? down|wrote down)\b", re.I)

    screens = (
        ScreenSpec("folders", "your folders", (
            sig(r"^Folders$", region=(.05, .22), weight=2, must=True),
            sig(r"^(New folder|New Folder)$"),
            sig(r"^(All iCloud|Quick Notes|Notes|Shared|Recently Deleted|On My iPhone)$", weight=2),
            sig(r"^\d+ Notes?$", field="value"),
        ), "recorded",
            ocr=(
                sig(r"^Folders$", region=(.05, .22), weight=2, must=True),
                sig(r"^(All iCloud|Quick Notes|Notes|Shared|Recently Deleted|On My iPhone)$", weight=2),
            )),
        ScreenSpec("list", "a folder's notes", (
            sig(r"^\d+ Notes?$", field="text", weight=2),
            sig(rf", {DATE}, ·, ", weight=2),
            sig(r"^Compose$|^New note$", region=(.84, 1.0)),
            sig(r"^(Pinned|Today|Yesterday|Previous 7 Days|Previous 30 Days)$"),
            sig(SEARCH, region=(.84, 1.0)),
            sig(r"^Folders$", region=(.05, .22), never=True),
        ), "recorded",
            ocr=(
                sig(r"^\d+ Notes?$", field="text", weight=2),
                sig(r"^(Pinned|Today|Yesterday|Previous 7 Days|Previous 30 Days)$"),
                sig(SEARCH, region=(.84, 1.0)),
                sig(r"^Folders$", region=(.05, .22), never=True),
            )),
        ScreenSpec("search", "search results", (
            sig(r"^Top Hits$", weight=2),
            sig(r"^\d+ Found$|^None Found$", weight=2),
            sig(r"^Clear text$"),
        ), "recorded",
            ocr=(
                sig(r"^Top Hits$", weight=2),
                sig(r"^\d+ Found$|^None Found$", weight=2),
            )),
        ScreenSpec("note", "a note", (
            sig(r"^(ICTK2NoteEditorView|notes_body_editor|notes_title_field)$", weight=3),
            sig(r"^Note$", "TextView"),
            sig(r"^(Checklist|checklist|Attachments|Handwriting|Writing Tools|Markup|Text Formatting)$"),
            sig(r"^(Share|More)$", region=TOP),
            sig(r"^[A-Z][a-z]+ \d{1,2}, \d{4} at \d", field="text"),
            sig(r"^(Top Hits|Pin Note)$", never=True),
            sig(r"^Folders$", region=(.13, .22), never=True),
        ), "recorded",
            ocr=(
                sig(r"^[A-Z][a-z]+ \d{1,2}, \d{4} at \d", field="text"),
                sig(r"^More$", "Button", region=TOP, weight=2),
                sig(r"^(Top Hits|Pin Note)$", never=True),
                sig(r"^Folders$", region=(.13, .22), never=True),
            )),
        ScreenSpec("menu", "a note's menu", (
            sig(r"^Pin Note$", must=True, weight=2),
            sig(r"^(Find in Note|Move Note|Recent Notes|Lock|Scan)$", weight=2),
        ), "recorded",
            ocr=(
                sig(r"^Pin Note$", must=True, weight=2),
                sig(r"^(Find in Note|Move Note|Recent Notes|Lock|Scan)$", weight=2),
            )),
    )

    routes = {
        "folders": Route("folders", "your folders", ("folders",)),
        "search": Route("search", "search results", ("search",), (
            TypeIn(SEARCH, "the search field", submit=False),), arg="query"),
        "note": Route("note", "a note", ("note",), (
            TypeIn(SEARCH, "the search field"),
            PickRow("note", roles=("Cell", "StaticText", "Button"), region=(.05, .85),
                    template=(r"^{arg}(,|$)", r"^{arg}\b"))), arg="name"),
        "folder": Route("folder", "a folder", ("list",), (
            PickRow("folder", roles=("Cell", "StaticText"), region=(.15, .9), template=(r"^{arg}$",), scrolls=2),),
            arg="name"),
    }
    open_aliases = {"find": "search", "open": "note", "list": "folder"}
    read_aliases = {"list": "notes", "folder": "notes", "find": "search", "open": "note", "text": "note"}
    read_default = {"notes": 10, "search": 10, "folders": 20}
    read_titles = {"notes": "notes", "search": "search results", "note": "note", "folders": "folders"}
    nouns = {"notes": "note", "search": "note", "note": "line", "folders": "folder"}
    prompts = {
        "OPEN": ("- NT_OPEN where: go to a Notes screen by code, no taps from you: folders | folder <name> | search "
                 "<words> | note <title> (found by Notes' search, checked by its title). It only reads: it never "
                 "edits, moves or deletes. If it can't, go on the usual way."),
        "READ": ("- NT_READ what [n]: read Notes by code: note <title> (finds and opens it: its whole text) | search "
                 "<words> n (matching notes: title, date, first line) | notes [folder] n (a folder's notes) | folders "
                 "(each with its count). Opens the screen it needs."),
    }
    mcp_open_hint = "your folders, a folder, a search, or one note by its title"
    mcp_read_hint = "one note's whole text, notes matching words, a folder's notes, or your folders with counts"
    read_columns = {
        "notes": (("title", "Note", "text"), ("when", "Edited", "text"), ("folder", "Folder", "text"),
                  ("preview", "First line", "text")),
        "search": (("title", "Note", "text"), ("when", "Edited", "text"), ("preview", "First line", "text")),
        "note": (("line", "Line", "text"),),
        "folders": (("folder", "Folder", "text"), ("notes", "Notes", "number")),
    }

    @property
    def readers(self):
        return {"note": core.Reader(read_note, ("note",), "note"),
                "search": core.Reader(read_search, ("search",), "search"),
                "notes": core.Reader(read_notes, ("list",), "folder"),
                "folders": core.Reader(read_folders, ("folders",), "folders")}

    back_control = rows.back_unless_act

    def confirm(self, route, arg, snapshot):
        if route != "note" or not arg:
            return None
        title = note_title(snapshot)
        if title and core.plain(arg).casefold() in title.casefold():
            return None
        return f"the note on screen is “{(title or '')[:40]}”, not “{core.plain(arg)[:40]}”"


def parse_row(label):
    """A note's row ("Shopping List, 8:45 AM, ·, Notes, ·, Groceries for this week:" or "MobsterBench Note, modified
    September 24, 2026, Locker code:") as {title, when, folder, preview}, or None."""
    text = core.plain(label)
    parts = [p.strip() for p in text.split(", ")]
    if len(parts) < 2:
        return None
    when_at = next((i for i, p in enumerate(parts[1:], 1) if re.fullmatch(DATE, p, re.I)), None)
    if when_at is None:
        joined = re.match(rf"^(.*?), ({DATE}), (.*)$", text, re.I)
        if not joined:
            return None
        return {"title": joined.group(1), "when": re.sub(r"^modified ", "", joined.group(2)), "folder": None,
                "preview": joined.group(joined.lastindex) or None}
    rest = [p for p in parts[when_at + 1:] if p != "·"]
    folder = rest[0] if len(rest) > 1 else None
    return {"title": ", ".join(parts[:when_at]), "when": re.sub(r"^modified ", "", parts[when_at]),
            "folder": folder, "preview": ", ".join(rest[1:] if folder else rest) or None}


def parse_list(snapshot):
    if core.image_only(snapshot):
        out, texts = [], rows.lines(snapshot, (.1, .9))
        for index, line in enumerate(texts):
            under = texts[index + 1] if index + 1 < len(texts) else None
            first = re.match(rf"^({DATE})\s+(.*)$", core.plain(under.label) if under else "", re.I)
            if first and under.rect[0] - line.rect[0] < .05 and 0 < core.centre_y(under) - core.centre_y(line) < .04:
                out.append({"title": core.plain(line.label), "when": first.group(1), "folder": None,
                            "preview": first.group(first.lastindex) or None})
        return out
    out = []
    for element in rows.labelled(snapshot, ("Cell", "Button"), (.1, .9)):
        row = parse_row(element.label)
        if row and row["title"] not in [r["title"] for r in out]:
            out.append(row)
    return out


def _key(row):
    return (row["title"], row["when"])


def read_notes(pack, io, snapshot, n, arg):
    found, stopped, _ = core.collect(io, snapshot, parse_list, _key, n, max_swipes=min(n // 6 + 2, 8))
    return ReadResult("notes", f"{len(found)} notes", pack.read_columns["notes"], found, stopped)


def read_search(pack, io, snapshot, n, arg):
    found = [{k: v for k, v in row.items() if k != "folder"} for row in parse_list(snapshot)][:n]
    return ReadResult("search", f"{len(found)} notes match “{core.plain(arg)[:40]}”" if arg else
                      f"{len(found)} notes found", pack.read_columns["search"], found,
                      "" if found else "no note matched")


def note_text(snapshot):
    """An open note's text: the editor's value on a tree, the lines under the title on a picture."""
    editor = next((e for e in snapshot.elements if e.role == "TextView" and e.value
                   and (e.label in ("Note", "notes_body_editor") or e.rect[3] > .3)), None)
    if editor is not None:
        title = next((e.value for e in snapshot.elements if e.label == "notes_title_field" and e.value), None)
        return ([title] if title else []) + [line for line in str(editor.value).replace("\\n", "\n").split("\n")]
    return [core.plain(e.label) for e in rows.lines(snapshot, (.1, .9)) if not re.fullmatch(r"[•.\s0-]+", e.label)]


def note_title(snapshot):
    lines = [line.strip() for line in note_text(snapshot) if line.strip()]
    return lines[0] if lines else None


def read_note(pack, io, snapshot, n, arg):
    lines = [line.strip() for line in note_text(snapshot) if line.strip()]
    return ReadResult("note", lines[0] if lines else "This note", pack.read_columns["note"],
                      [{"line": line} for line in lines[:max(n, 200)]], "" if lines else "the note is empty")


def read_folders(pack, io, snapshot, n, arg):
    found = []
    for element in rows.labelled(snapshot, ("Cell",), (.15, .95)):
        count = re.match(r"^(\d+) Notes?$", core.plain(element.value))
        if count:
            found.append({"folder": core.plain(element.label), "notes": int(count.group(1))})
    if core.image_only(snapshot):
        for name, value in rows.pairs(snapshot, region=(.15, .95)).items():
            number = re.match(r"^(\d+)", value)
            if number:
                found.append({"folder": name, "notes": int(number.group(1))})
    return ReadResult("folders", f"{len(found)} folders", pack.read_columns["folders"], found[:n], "")


PACK = Notes()
