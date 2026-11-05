"""When a Smart turn needs the screenshot: only when the accessibility tree can't carry the screen.

The rows the model reads (frontier.screen_rows)
say what is on most screens: every label, value, switch state, the selected tab, where each control sits. The picture
adds what rows can't say. ``decide`` keeps it exactly then, and drops it on the turns where the rows are the whole
screen:

- the first turn, a new app in front, or ``MAX_BLIND`` turns without one (the picture never goes stale for long);
- the turn after a miss: the last action got feedback, or its outcome is unproven (the screen may not be what the
  model thinks it is);
- a skill's pictures waiting to be shown, or a driver that reads pixels (its rows come from the picture already);
- a request about how something looks (a colour, an icon, a photo, a layout);
- a screen the rows can't describe (``tree_gap``): too few rows, a control with no label, a web view, a map or a
  video, a large picture or unnamed area, a switch-like control that shows no state (a segmented control, a date
  picker, a tab bar with no selected tab), a text field with no label.

Every reason is a word in ``REASONS`` that goes on the turn's ``frontier_prompt`` event, so a run says why each turn
had its picture or went without. ``MOBSTER_IMAGE_POLICY=tree`` turns the policy on; the default is ``always``, a
picture every turn as before, until the paired A/B on real phones passes (tuning/PLAN.md).

Code only: it reads rows and counters, calls no model, and fails toward sending the picture.
"""

import os
import re
from dataclasses import dataclass

MODES = ("always", "tree")
MAX_BLIND = 3                   # turns in a row that may go without a picture
MIN_ROWS = 5                    # a screen with fewer rows than this isn't described by them
HERO_AREA = 30.0                # percent of the screen: a picture or unnamed area this big is content, not chrome
CHROME = re.compile(r"overlay|dimming|background|shadow|separator|scrim|backdrop|divider", re.I)
HERO_ROLES = frozenset({"Image", "Other"})
OPAQUE_ROLES = frozenset({"WebView", "Map", "Video", "Canvas", "MapView", "WebKit"})
# Controls whose state the rows show as ``value=`` or ``selected``; one that shows neither can't be read from rows.
STATE_ROLES = frozenset({"SegmentedControl", "DatePicker", "TabBar"})
GROUPS = frozenset({"Toolbar", "TabBar"})
VISUAL = re.compile(r"\b(colou?rs?|icons?|logos?|images?|photos?|pictures?|screenshots?|look(?:s|ing)? like|appears?|"
                    r"shapes?|draw(?:ing)?|sketch|designs?|layouts?|pixels?|emoji|videos?|maps?|charts?|graphs?|"
                    r"badges?|animations?|wallpapers?|fonts?|brightness|dark mode|themes?|stickers?|memes?|gifs?)\b",
                    re.I)
ROW = re.compile(r'^(e\d+) (\S+) (?:"((?:[^"\\]|\\.)*)"|\(no label[^)]*\))(.*?)@(-?\d+),(-?\d+)(?:,(\d+),(\d+))?\s*$')
VALUE = re.compile(r'\bvalue="')

REASONS = ("off", "first turn", "new app", "refresh", "after a miss", "skill pictures", "pixel read", "visual request",
           "long task", "too few rows", "unlabeled control", "opaque view", "large picture", "state not in the rows",
           "tree carries the screen")


@dataclass(frozen=True)
class Row:
    alias: str
    role: str
    label: str                  # "" when the row has no label
    labeled: bool
    flags: str                  # what follows the label: value="...", selected, editable, disabled
    x: int
    y: int
    w: int
    h: int

    @property
    def area(self):
        return self.w * self.h / 100.0

    @property
    def has_value(self):
        return bool(VALUE.search(self.flags))

    @property
    def selected(self):
        return bool(re.search(r"\bselected\b", self.flags))


@dataclass(frozen=True)
class Turn:
    """What the loop knows about this turn besides the rows."""
    first: bool = False
    new_app: bool = False
    blind: int = 0                  # turns in a row without a picture before this one
    missed: bool = False            # the last action got feedback, or its outcome is unproven
    skill_pictures: bool = False
    pixel_read: bool = False
    long_task: bool = False
    request: str = ""


def mode():
    value = os.environ.get("MOBSTER_IMAGE_POLICY", "always").strip().lower()
    return value if value in MODES else "always"


def parse_rows(lines):
    """``Row``s from the "Screen elements" lines of a prompt; a line that isn't a row is skipped."""
    rows = []
    for line in lines:
        found = ROW.match(str(line).strip())
        if not found:
            if re.match(r"^e\d+\s", str(line).strip()):
                rows.append(Row(str(line).split()[0], "Unknown", "", False, "", 0, 0, 0, 0))
            continue
        alias, role, label, rest, x, y, w, h = found.groups()
        rows.append(Row(alias, role, (label or "").replace('\\"', '"'), bool(label and label.strip()), rest.strip(),
                        int(x), int(y),
                        int(w or 0), int(h or 0)))
    return rows


def tree_gap(rows):
    """Why the rows can't carry this screen (one of ``REASONS``), or None when they can."""
    if len(rows) < MIN_ROWS:
        return "too few rows"
    if any(row.role in OPAQUE_ROLES for row in rows):
        return "opaque view"
    for row in rows:
        if not row.labeled and row.role != "StaticText":
            return "unlabeled control"     # the rows say "find it by position in the screenshot"
        if row.role in HERO_ROLES and row.area >= HERO_AREA and not CHROME.search(row.label):
            return "large picture"
    for row in rows:
        if row.role in STATE_ROLES and not row.has_value and not row.selected:
            return "state not in the rows"
    if any(row.role in GROUPS for row in rows) and not any(row.selected for row in rows):
        return "state not in the rows"
    return None


def decide(rows, turn):
    """(send the picture?, the reason) for a Smart turn showing ``rows``. Fails toward sending it."""
    for flag, why in ((turn.long_task, "long task"), (turn.first, "first turn"), (turn.new_app, "new app"),
                      (turn.pixel_read, "pixel read"), (turn.skill_pictures, "skill pictures"),
                      (turn.missed, "after a miss"), (turn.blind >= MAX_BLIND, "refresh"),
                      (bool(VISUAL.search(turn.request or "")), "visual request")):
        if flag:
            return True, why
    gap = tree_gap(rows)
    if gap is not None:
        return True, gap
    return False, "tree carries the screen"
