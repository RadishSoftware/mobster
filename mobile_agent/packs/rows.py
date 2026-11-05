"""Rows read from list screens, the same way on an accessibility tree and on a picture (image-only connections)."""

import re

from . import core

# A time or a date as list rows show them: "9:55 PM", "Yesterday", "Monday", "10/1/26", "Oct 3", "2h", "3 days ago".
WHEN = re.compile(r"^(\d{1,2}:\d{2}\s?([AaPp]\.?[Mm]\.?)?|yesterday|today|now|just now|"
                  r"(mon|tues|wednes|thurs|fri|satur|sun)day|\d{1,2}/\d{1,2}(/\d{2,4})?|"
                  r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.? \d{1,2}(, \d{4})?|"
                  r"\d{1,2} (jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*|"
                  r"\d+\s?(s|m|min|h|hr|d|w|mo|y)|\d+ (seconds?|minutes?|hours?|days?|weeks?|months?|years?) ago)$",
                  re.I)
FLAGS = ("unread", "pinned", "muted", "time sensitive", "draft", "starred", "important", "verified", "new")


def labelled(snapshot, roles=("Cell",), region=(.1, .92), minimum=2):
    """The elements in ``region`` with ``roles`` and a label of at least ``minimum`` characters, each label once
    (largest
    element first when two share a label at the same place)."""
    out, seen = [], set()
    for element in sorted(core.find_all(snapshot, r".", roles=roles, region=region),
                          key=lambda e: (round(core.centre_y(e), 2), -e.rect[2] * e.rect[3])):
        label = core.plain(element.label)
        key = (label, round(core.centre_y(element), 2))
        if len(label) < minimum or key in seen:
            continue
        seen.add(key)
        out.append(element)
    return out


def parts(label):
    """(parts, flags): a row label's comma parts with iOS's flags ("Unread", "Pinned"...) taken out, lowercased."""
    tokens = [t.strip() for t in core.plain(label).split(",")]
    flags = {t.casefold() for t in tokens if t.casefold() in FLAGS}
    return [t for t in tokens if t and t.casefold() not in FLAGS], flags


def lines(snapshot, region=(0.0, 1.0)):
    """A picture's text lines in ``region``, top to bottom then left to right."""
    return sorted((e for e in getattr(snapshot, "elements", ()) or ()
                   if e.label and region[0] <= core.centre_y(e) <= region[1]),
                  key=lambda e: (round(core.centre_y(e), 3), e.rect[0]))


def anchored(snapshot, anchor=WHEN, region=(.1, .92), right=.55, same_line=.012):
    """On a picture: rows found by ``anchor`` (a regex on a whole line) at the right of their first line."""
    texts = lines(snapshot, region)
    marks = [e for e in texts if e.rect[0] >= right and anchor.match(core.plain(e.label))]
    rows = []
    for index, mark in enumerate(marks):
        top = core.centre_y(mark)
        bottom = core.centre_y(marks[index + 1]) - same_line if index + 1 < len(marks) else region[1]
        head = [e for e in texts if e is not mark and abs(core.centre_y(e) - top) <= same_line
                and e.rect[0] < mark.rect[0]]
        body = [e for e in texts if e is not mark and e not in head and top + same_line < core.centre_y(e) < bottom]
        rows.append({"anchor": core.plain(mark.label), "head": [core.plain(e.label) for e in head],
                     "body": [core.plain(e.label) for e in body], "top": round(top, 3)})
    return rows


def pairs(snapshot, region=(.1, .95), roles=("Cell", "Button", "StaticText", "Other")):
    """{label: value} of a settings-style list: "iOS Version, 26.0.1" on a tree; on a picture, a line on the left and a
    line on the right at the same height."""
    out = {}
    if core.image_only(snapshot):
        texts = lines(snapshot, region)
        for left in (e for e in texts if e.rect[0] < .45):
            right = [e for e in texts if e.rect[0] >= .45 and abs(core.centre_y(e) - core.centre_y(left)) < .012]
            value = re.sub(r"\s*[>›]$", "", core.plain(right[-1].label)) if right else ""
            if value and not re.fullmatch(r"[>›<‹]", value):
                out.setdefault(re.sub(r"\s*[>›]$", "", core.plain(left.label)), value)
        return out
    for element in labelled(snapshot, roles, region):
        label, value = core.plain(element.label), core.plain(element.value)
        if value and value not in ("0", "1") and label:
            out.setdefault(label, value)
        elif ", " in label:
            key, _, rest = label.partition(", ")
            if key and rest:
                out.setdefault(key, re.sub(r"(\d), (\d{3})\b", r"\1,\2", rest))
    return out


def tabbar(names, region=(.86, 1.0)):
    """A tab bar's signal: any of ``names`` (whole labels) at the bottom, a button on a tree, a line on a picture."""
    return core.sig(rf"^({'|'.join(names)})$", region=region)


def top_left_back(snapshot, top=(.04, .14), never=core.NEVER):
    """A navigation bar's Back button: a button at the top left whose label names the screen before ("Settings",
    "General", "Search", "Oct 15"), never one whose words name an act."""
    if core.image_only(snapshot):
        return None
    for element in sorted(core.find_all(snapshot, r"\S", role="Button", region=top), key=lambda e: e.rect[0]):
        if element.rect[0] < .2 and element.rect[2] < .4 and not never.search(element.label or "") \
                and not core.ACT_LABEL.match(core.plain(element.label)):
            return element
    return None


def back_unless_act(pack, snapshot):
    return top_left_back(snapshot, never=pack.never)


def tab_tap(region):
    return lambda label, what: core.Tap(label, what, role="Button", region=region)
