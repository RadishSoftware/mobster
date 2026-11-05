"""An app's overview numbers (Instagram's Professional dashboard, TikTok's Analytics overview), read from the tree."""

import re

from . import core
from .core import ReadResult

COLUMNS = (("handle", "Profile", "text"), ("period", "Period", "text"), ("views", "Views", "number"),
           ("reach", "Accounts reached", "number"), ("profile_visits", "Profile visits", "number"),
           ("follower_change", "Follower change", "number"))
METRICS = (("views", r"(?:Total |Video )?views"), ("reach", r"Accounts reached|Reach"),
           ("profile_visits", r"Profile (?:visits|views)"),
           ("follower_change", r"Net follower change|Follower change|New followers|Net followers"))
PERIOD = re.compile(r"^(?:(?:Last|Past) )?\d+ days$|^Last (?:week|month)$", re.I)
VALUE = r"[+\-−]?\d[\d,.]*[KMB]?"


def signed(text):
    """"+118" -> 118, "-4" -> -4, "1.2K" -> 1200; None when it isn't a count."""
    text = core.plain(text).replace("−", "-")
    value = core.number(text.lstrip("+-"))
    return None if value is None else -value if text.startswith("-") else value


def _near(snapshot, element, pattern=VALUE):
    """The number beside ``element`` (one that matches ``pattern``): above, below or to its right, the nearest."""
    x, y = element.rect[0] + element.rect[2] / 2, core.centre_y(element)
    best = None
    for other in snapshot.elements:
        if other is element or not re.fullmatch(pattern, core.plain(other.label) or ""):
            continue
        dy, dx = core.centre_y(other) - y, (other.rect[0] + other.rect[2] / 2) - x
        near = (abs(dx) < .2 and 0 < abs(dy) < .07) or (abs(dy) < .02 and 0 < dx < .5)
        distance = abs(dx) + abs(dy)
        if near and (best is None or distance < best[0]):
            best = (distance, other)
    return signed(best[1].label) if best else None


def parse(snapshot):
    """{period, views, reach, profile_visits, follower_change} from the screen; a number it can't find is None."""
    row = {"period": next((core.plain(e.label) for e in snapshot.elements if PERIOD.match(core.plain(e.label))), None)}
    for key, words in METRICS:
        row[key] = None
        for e in snapshot.elements:
            label = core.plain(e.label)
            inline = re.match(rf"^(?:{words})[,:]?\s+({VALUE})\b", label, re.I)
            if inline:
                row[key] = signed(inline.group(1))
            elif re.fullmatch(words, label, re.I):
                row[key] = _near(snapshot, e)
            if row[key] is not None:
                break
    if row["follower_change"] is None:
        # TikTok's overview names the total "Followers" and shows the change with its sign beside it.
        label = core.find(snapshot, r"^Followers$")
        row["follower_change"] = _near(snapshot, label, r"[+\-\u2212]\d[\d,.]*[KMB]?") if label else None
    return row


def reader(pack, io, snapshot, n, arg):
    row = {"handle": arg, **parse(snapshot)}
    found = any(row[key] is not None for key, _ in METRICS)
    return ReadResult("insights", f"{pack.name} insights" + (f", {row['period']}" if row["period"] else ""), COLUMNS,
                      [row], "" if found else "the overview numbers aren't in the screen's tree")


def read(pack, io, what, n, arg):
    """Insights for the signed-in profile: its handle first (the profile tab), then the overview."""
    profile = core.Pack.read(pack, io, "profile", 1, None)
    mine = (profile.rows[0] if profile.rows else {})
    handle = mine.get("handle") if mine.get("state") == "yours" else None
    result = core.Pack.read(pack, io, "insights", n, handle)
    for row in result.rows:
        row["handle"] = handle
    return result
