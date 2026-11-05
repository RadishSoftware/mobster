"""Which commits ask the person and what a tap at a point means: the plan line and approval titles the Mac app shows,
and the check for TAP_XY (``point_ask``). Built on the screen readers of contract_screen.py."""

import re

from .contract_screen import (
    ACTS,
    COMMIT_WORDS,
    SWITCH_ROLES,
    Item,
    internal_name,
    label_verbs,
    plain,
    point_in,
    recipient,
    same_party,
)

# ---------------------------------------------------------------- plan line and approvals (the Mac app)

# Commits that wait for the user's OK in the app while Ask before acting is on: what other people see, what
# spends or moves money, what books, follows, shares, calls, cancels or deletes. A save or an archive is the
# user's own change (a reminder, a note, a filed email): it never asks.
PRIVATE_ACTS = frozenset({"save", "archive", "none"})
ASK_ACTS = frozenset(ACTS) - PRIVATE_ACTS
# (singular, plural) nouns and the verb a plan line uses for each act.
PLAN_WORDS = {
    "send_message": ("send", "message", "messages"), "post": ("post", "post", "posts"),
    "comment": ("post", "comment", "comments"), "reply": ("send", "reply", "replies"),
    "react": ("add", "reaction", "reactions"), "pay": ("make", "payment", "payments"),
    "request_money": ("send", "money request", "money requests"), "transfer": ("make", "transfer", "transfers"),
    "order": ("place", "order", "orders"), "book": ("make", "booking", "bookings"),
    "follow": ("follow", "account", "accounts"), "unfollow": ("unfollow", "account", "accounts"),
    "share": ("share", "item", "items"), "call": ("place", "call", "calls"),
    "delete": ("delete", "item", "items"), "cancel": ("cancel", "item", "items"),
    "install": ("install", "app", "apps"),
    "other": ("do", "thing that needs your OK", "things that need your OK")}


def asks(item):
    """Whether a declared COMMIT waits for the user's OK (see ASK_ACTS)."""
    return item is not None and item.kind == "COMMIT" and item.act in ASK_ACTS


def plan_commits(contract):
    """[{act, app, count, target}] of the commits that will ask, in the request's order."""
    return [{"act": i.act, "app": i.app, "count": i.needed(), "target": recipient(i) or i.what}
            for i in (contract.items if contract else ()) if asks(i)]


def plan_line(contract):
    """One sentence that says up front what the task will commit ("This task will send 1 message to Sam."),
    or None when it commits nothing that asks."""
    parts = []
    for item in (contract.items if contract else ()):
        if not asks(item):
            continue
        verb, one, many = PLAN_WORDS.get(item.act, PLAN_WORDS["other"])
        count = item.needed()
        noun = f"{count} {one if count == 1 else many}" if count is not None else f"one or more {many}"
        who = recipient(item)
        if item.act in ("follow", "unfollow", "call", "delete", "cancel", "share", "install") and who:
            parts.append(f"{verb} {who}")
            continue
        target = f" to {who}" if who and item.act in ("send_message", "reply", "pay", "transfer", "request_money") \
            else f" for {who}" if who and item.act in ("order", "book") else ""
        conditional = " if " + item.condition if item.condition else ""
        parts.append(f"{verb} {noun}{target}{conditional}")
    if not parts:
        return None
    body = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return f"This task will {body}."


def approval_title(item, amount=None, shown=None):
    """The question an approval asks, naming the act ("Send this message to Sam?", "Pay $42.00 to Maya?").
    ``shown``: who the screen says a message goes to (visible_recipient: its To field's tokens or its thread's
    title). The title names that party, and says so when it is not the one the request named: "Send this message to
    +1 (555) 564-8583 (Kate Bell in your request)?" (O2). When the screen names no one, the title keeps the request's
    words as the request's ("Send this message to the person you named (Kate Bell)?"), or names no one ("Send this
    message?"). A developer's name for a view is never shown as a party (internal_name)."""
    act, who = item.act, recipient(item)
    shown = None if internal_name(shown) else shown
    money = f"${amount:,.2f}" if isinstance(amount, (int, float)) else None
    to = f" to {who}" if who else ""
    if act in ("send_message", "reply"):
        noun = "reply" if act == "reply" else "message"
        if shown and who and not same_party(shown, who):
            return f"Send this {noun} to {shown} ({who} in your request)?"
        if shown:
            return f"Send this {noun} to {shown}?"
        return f"Send this {noun} to the person you named ({who})?" if who else f"Send this {noun}?"
    if act == "install":
        name = who or item.what
        return f"Buy {name} for {money}?" if money and amount else f"Install {name} from the App Store?"
    if act in ("post", "comment"):
        return f"Post this {'comment' if act == 'comment' else ''}".rstrip() + "?"
    if act == "pay":
        return f"Pay {money or 'this'}{to}?"
    if act == "transfer":
        return f"Transfer {money or 'this'}{to}?"
    if act == "request_money":
        return f"Request {money or 'money'}{' from ' + who if who else ''}?"
    if act == "order":
        return f"Place this order{' for ' + money if money else ''}?"
    if act == "book":
        return f"Book this{' for ' + money if money else ''}?"
    if act == "react":
        return "Add this reaction?"
    if act in ("follow", "unfollow", "call", "delete", "cancel", "share"):
        return f"{act.capitalize()} {who or item.what}?"
    return f"Do this in {item.app}?" if item.app else "Do this now?"


# ---------------------------------------------------------------- taps at a point (TAP_XY)

# Elements that hold others rather than being one control: a point inside one says nothing about what it hits.
CONTAINER_ROLES = frozenset({"Other", "Window", "Application", "ScrollView", "Table", "CollectionView", "WebView",
                             "Map",
                             "Group", "LayoutArea", "LayoutItem", "NavigationBar", "TabBar", "Toolbar", "Keyboard",
                             "Sheet", "Alert", "Popover"})
# A row's trailing quarter is where its own controls sit (a heart, a Follow, a switch): a point there is not the row.
ROW_TRAILING = .25


def point_spot(snapshot, x, y):
    """(kind, element) for a tap at (x, y) among the listed elements, read from where the point is, never from what the
    model called it. kind is "commit" (inside a control whose words commit: element), "nav" (inside a plainly
    navigational control: a tab, a back button, a list row with no commit verb, its trailing quarter left out), "plain"
    (inside a listed element that names no commit) or None (inside no listed control: an unlisted heart). Containers
    (a scroll view, a table, a bar) and anything as large as half the screen are no control."""
    elements = list(getattr(snapshot, "elements", ()) or ())
    inside = [e for e in elements if point_in(e.rect, x, y) and e.role not in CONTAINER_ROLES
              and e.rect[2] * e.rect[3] <= .5]
    if not inside:
        return None, None
    for e in inside:
        if label_verbs(e.label, e.role) or e.role in SWITCH_ROLES:
            return "commit", e
    controls = [e for e in inside if e.role not in ("StaticText", "Image", "Icon")] or inside
    innermost = min(controls, key=lambda e: e.rect[2] * e.rect[3])
    ex, ey, ew, eh = innermost.rect
    if innermost.role == "Tab" or innermost.role == "Button" and any(
            b.role == "TabBar" and point_in(b.rect, ex + ew / 2, ey + eh / 2) for b in elements):
        return "nav", innermost
    if innermost.role == "Button" and (plain(innermost.label) == "back" or any(
            b.role == "NavigationBar" and point_in(b.rect, ex + ew / 2, ey + eh / 2)
            and ex + ew / 2 <= b.rect[0] + b.rect[2] / 4 for b in elements)):
        return "nav", innermost
    if innermost.role == "Cell" and eh <= .12 and x <= ex + ew * (1 - ROW_TRAILING):
        return "nav", innermost
    return "plain", innermost


def open_commits(contract, request=""):
    """The commits a point tap could be, by what the task asks: the contract's open COMMIT items that ask (asks), or
    without a contract one "other" item when the request's words name a commit."""
    if contract is not None:
        return [i for i in contract.items if asks(i) and i.status != "closed"]
    if COMMIT_WORDS.search(request or "") or re.match(r"\s*(text|message|email|dm|tell|ask)\b", request or "", re.I):
        return [Item(0, "COMMIT", act="other")]
    return []


# How far from a point a labeled element may be and still name it on the card ("near “Close”"), in screen widths.
NEAR_WIDTHS = .12
CARD_LABEL_CHARS = 40


def point_target(snapshot, x, y):
    """(label, how) naming what a tap at (x, y) hits, from the screen, never from the model: "on" the innermost
    labeled element under the point, else "near" the closest labeled one within NEAR_WIDTHS, else (None, None).
    Containers and anything as large as half the screen name nothing."""
    elements = [e for e in getattr(snapshot, "elements", ()) or () if e.role not in CONTAINER_ROLES
                and e.rect[2] * e.rect[3] <= .5 and " ".join(str(e.label or "").split())]
    under = [e for e in elements if point_in(e.rect, x, y)]
    if under:
        return " ".join(min(under, key=lambda e: e.rect[2] * e.rect[3]).label.split()), "on"
    width, height = getattr(snapshot, "width", 0) or 1, getattr(snapshot, "height", 0) or 1
    tall = height / width if width and height else 2.0

    def gap(e):
        ex, ey, ew, eh = e.rect
        dx = max(ex - x, 0, x - (ex + ew))
        dy = max(ey - y, 0, y - (ey + eh)) * tall  # in screen widths, so a step across counts as one down
        return (dx * dx + dy * dy) ** .5
    near = [e for e in elements if gap(e) <= NEAR_WIDTHS]
    if near:
        return " ".join(min(near, key=gap).label.split()), "near"
    return None, None


def point_title(snapshot, x, y):
    """A point tap's card title, naming what is under it: 'Tap “Close” at this spot?', 'Tap at this spot, near
    “Close”?', or 'Tap at this spot?' when nothing is known. Never the task's words: the card shows the spot itself."""
    label, how = point_target(snapshot, x, y)
    if label and len(label) > CARD_LABEL_CHARS:
        label = label[:CARD_LABEL_CHARS - 1].rstrip() + "…"
    if how == "on":
        return f"Tap “{label}” at this spot?"
    if how == "near":
        return f"Tap at this spot, near “{label}”?"
    return "Tap at this spot?"


def point_ask(contract, request, snapshot, x, y):
    """(title, act) when a tap at (x, y) asks first, else None. A point tap is never bound to a listed element, so what
    the model calls it unlocks nothing: inside a plainly navigational control it goes ahead; while the task has an
    open commit that asks, it asks; anywhere no listed control is, it asks too. The title names what is under the
    point (point_title), never the task's words (real iPhone, 13 Oct: a tap on a sheet's Close read "it may delete
    every note whose text is…"). ``act`` is the commit the tap may be, for the permission gate: an open commit's,
    else "other". MobsterBench Real, 11 Oct: Instagram drew the heart outside the tree, a point tap liked a post with
    Ask before acting on, and no card was shown."""
    kind, _ = point_spot(snapshot, x, y)
    if kind == "nav":
        return None
    items = open_commits(contract, request)
    if items:
        return point_title(snapshot, x, y), items[0].act or "other"
    if kind is None and top_bar_navigation(snapshot, x, y):
        return None
    if kind is None:
        return point_title(snapshot, x, y), "other"
    return None


# The band at the top of a screen that holds its navigation bar (under the status bar): menus, Back, a profile's ≡,
# Search, Notifications. Real iPhone, 13 Oct: Instagram's profile menu was drawn outside the tree, so the read-only
# "saved posts" job's first tap at it asked "Tap at this spot?" and stopped when declined.
TOP_BAR = .13
OVERLAY_ROLES = frozenset({"Alert", "Sheet", "Popover", "Keyboard"})


def top_bar_navigation(snapshot, x, y):
    """A point tap off every listed control that is plainly navigation: in the top bar band, with no keyboard, alert,
    sheet or popover on screen, and no commit named on or near it ("Share", "Post", "Done" sit there in a composer).
    Only for a task with no open commit (point_ask checks that first): anything else still asks."""
    if y > TOP_BAR or y < 0:
        return False
    elements = list(getattr(snapshot, "elements", ()) or ())
    if any(e.role in OVERLAY_ROLES for e in elements) or getattr(snapshot, "keyboard", "") == "visible":
        return False
    label, _ = point_target(snapshot, x, y)
    if label and (label_verbs(label) or COMMIT_WORDS.search(label)):
        return False
    # Any commit control in the band itself (a composer's Share or Done) keeps every unlisted spot there asking.
    return not any(e.rect[1] <= TOP_BAR and (label_verbs(e.label, e.role) or e.role in SWITCH_ROLES)
                   for e in elements if e.role not in CONTAINER_ROLES)
