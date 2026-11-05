"""What the picture shows: which rows a screen's bars hide, and the row a recorded value hides under."""

import re

from .contract_screen import ATOM, NUMBER, as_float, center_y, element_text, plain

# Bars and sticky footers sit in the screen's bottom band (from BOTTOM_BAND of its height down). A row a bar lies over
# by more than OVER_ROW, or that the screen's foot cuts (rows are clipped to the screen), is in the tree but not all in
# the picture. A row taller than TALL_ROW is a pane: no scroll brings all of it into view. Rows hidden under a top bar
# are not counted: no stroke reliably starts inside the content under a header (TeamChat's pinned chips took both the
# drag's and the glide's start, live, 18-19 Oct).
BOTTOM_BAND, OVER_ROW, TALL_ROW, EDGE = .8, .005, .5, .005
BAR_ROLES = frozenset({"NavigationBar", "TabBar", "Toolbar"})
# A control this wide in the band is a bar (a sheet's "Request CityRideX" footer), and so is one of a line of BAR_LINE
# controls spanning as much (a SwiftUI tab bar is only its buttons, centres within BAR_LINE_Y).
BAR_WIDE, BAR_LINE, BAR_LINE_Y = .6, 3, .02
# A name in a recorded value: two or more capitalized words ("Seaport Raw Bar", "Rowan Byrne").
NAMES = re.compile(r"\b[A-Z][a-z]+(?: [A-Z][a-z]+)+")


def value_atoms(value):
    """What a screen row must hold to be a recorded value's source: its numbers, codes, times (ATOM) and names."""
    return ATOM.findall(value or "") + NAMES.findall(value or "")


def holds_atom(atom, text):
    """Whether ``text`` (plain) holds ``atom``: a number as an equal number ("$1,200" in "1200.00"), else its text."""
    if atom.startswith("$") or re.fullmatch(r"\d[\d,]*(?:\.\d+)?", atom):
        number = as_float(atom.lstrip("$").strip())
        return number is not None and any(abs((as_float(n) or -1e18) - number) < .005 for n in NUMBER.findall(text))
    return plain(atom) in text


def within(inner, outer, slack=.005):
    ix, iy, iw, ih = inner
    ox, oy, ow, oh = outer
    return ix >= ox - slack and iy >= oy - slack and ix + iw <= ox + ow + slack and iy + ih <= oy + oh + slack


def screen_bars(elements):
    """[(index, bar)]: the bars and sticky footers of the screen's bottom band, by their place in ``elements``
    (document order, back to front). A tab bar or toolbar, a control BAR_WIDE wide, or one of a line of controls as
    wide; texts and pictures are content, never a bar."""
    banded = [(index, element) for index, element in enumerate(elements)
              if element.role not in ("StaticText", "Image") and element.rect[2] > 0 and element.rect[3] > 0
              and element.rect[1] >= BOTTOM_BAND]
    bars = []
    for index, element in banded:
        line = [other for _, other in banded if abs(center_y(other) - center_y(element)) <= BAR_LINE_Y]
        if (element.role in BAR_ROLES or element.rect[2] >= BAR_WIDE or len(line) >= BAR_LINE
                and max(o.rect[0] + o.rect[2] for o in line) - min(o.rect[0] for o in line) >= BAR_WIDE):
            bars.append((index, element))
    return bars


def hidden_below(row, index, bars):
    """Whether the screen's foot, or a bar of its bottom band (screen_bars), lies over part of ``row`` (at ``index`` in
    the screen's elements). A tab bar or toolbar lies over any row not inside it; any other bar only over rows drawn
    before it (its own texts come after it). iOSWorld multi-069 (18 Oct): "Comfort, $22.40" and its "$22.40" were
    read from the tree under the ride sheet's "Request CityRideX" footer, and no picture of the run showed them."""
    x, y, w, h = row.rect
    if w <= 0 or h <= 0 or h > TALL_ROW:
        return False
    if y + h >= 1 - EDGE:
        return True
    return any(bar is not row and min(x + w, bx + bw) > max(x, bx) and min(y + h, by + bh) - max(y, by) > OVER_ROW
               and (not within(row.rect, bar.rect) if bar.role in BAR_ROLES else at > index)
               for at, bar in bars for bx, by, bw, bh in [bar.rect])


def hidden_row(items, elements, step):
    """The row of ``elements`` that holds a value marked at ``step`` when every row holding it is hidden below
    (hidden_below), else None. The agent scrolls that row into view before it leaves the screen, so the run's frames
    show what its answer cites."""
    bars = None
    for item in items:
        if item.value_step != step or not item.value or item.kind == "REPORT" and item.status != "found":
            continue
        bars = bars if bars is not None else screen_bars(elements)
        for atom in value_atoms(item.value):
            rows = [(i, e) for i, e in enumerate(elements) if holds_atom(atom, plain(element_text(e)))]
            if rows and all(hidden_below(row, i, bars) for i, row in rows):
                return rows[0][1]
    return None
