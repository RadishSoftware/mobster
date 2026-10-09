"""READ_NOTIFICATIONS: Notification Center, read-only, with every code masked.

``NotificationCenter.read`` opens Notification Center with a drag from the top edge (left of the notch, where
Control Center isn't), reads it as SpringBoard's own tree (WDA's default active application set to SpringBoard,
and always set back), at most 30 rows in at most 6 seconds, closes it with a drag up from the bottom edge, and
brings the app it started in back to the front. It never taps a notification: opening one could mark a message
read, accept an invite or open a link.

The skill hands the model the rows with any code masked as ``•••••• (use USE_CODE)``; USE_CODE reads the same
rows for its codes (codes.find_code). ``message_rows`` reads the Messages list's newest previews the same way.
"""

from dataclasses import dataclass
import re
from typing import Optional

from ..agent_hooks import Prepared, SkillResult
from .codes import CLOCK, MESSAGES, SPRINGBOARD, mask_codes, row_age

MAX_ROWS = 30
MAX_SECONDS = 6.0
CODE_MASK = "•••••• (use USE_CODE)"
FEEDBACK_CHARS = 4000
# Notification Center's own controls and headings: not notifications.
CHROME = re.compile(r"^(?:notification center|notification centre|clear|clear all|close|no older notifications|"
                    r"notifications|search|today|yesterday|earlier|show (?:less|more)|options|camera|flashlight|"
                    r"\d{1,2}:\d{2}|[a-z]+day,? [a-z]+ \d{1,2})$", re.I)
# Where a row is a notification or message: these roles carry the row's text (a cell's label joins its parts).
ROW_ROLES = {"Cell", "Button", "Other", "StaticText", "Link"}
# The Messages list's own row prefixes, not the sender.
ROW_FLAGS = {"unread", "pinned", "muted", "new message"}


@dataclass(frozen=True)
class Row:
    text: str
    app: str
    sender: str
    age_s: Optional[int]

    def public(self, mask=CODE_MASK):
        return {"app": self.app, "sender": mask_codes(self.sender, mask), "text": mask_codes(self.text, mask),
                "age_s": self.age_s}


def parse_row(text, now=None, app=""):
    """A row's app, sender and age from its accessibility text ("Messages, Chase, Your code is 482913, 2m ago")."""
    parts = [part.strip() for part in text.split(", ") if part.strip()]
    while parts and parts[0].casefold() in ROW_FLAGS:
        parts = parts[1:]
    age = row_age(parts, now)
    if not app and len(parts) >= 3:
        app, parts = parts[0], parts[1:]
    sender = parts[0] if parts else ""
    return Row(text=" ".join(text.split()), app=app or "", sender=sender, age_s=age)


def screen_rows(snapshot, now=None, app=""):
    """The rows a screen shows: labelled elements that aren't chrome, one per distinct text, top to bottom."""
    seen, rows = set(), []
    elements = sorted(getattr(snapshot, "elements", ()), key=lambda e: (e.rect[1], e.rect[0]))
    for element in elements:
        role = (element.role or "").removeprefix("XCUIElementType")
        text = " ".join(" ".join(p for p in (element.label, getattr(element, "value", "")) if p).split())
        if role not in ROW_ROLES or len(text) < 4 or CHROME.match(text) or text in seen:
            continue
        # A cell's parts are often listed again as its children: keep the fullest text only.
        if any(text in other for other in seen):
            continue
        seen = {other for other in seen if other not in text}
        seen.add(text)
        rows = [row for row in rows if row.text not in text]
        rows.append(parse_row(text, now, app))
    return rows


def _drag(driver, x1, y1, x2, y2, ms=250):
    driver.call("POST", "/actions", {"actions": [{
        "type": "pointer", "id": "finger", "parameters": {"pointerType": "touch"},
        "actions": [{"type": "pointerMove", "duration": 0, "x": round(x1), "y": round(y1)},
                    {"type": "pointerDown"}, {"type": "pause", "duration": 60},
                    {"type": "pointerMove", "duration": ms, "x": round(x2), "y": round(y2)},
                    {"type": "pointerUp"}]}]}, timeout=10)


def _hint(driver, bundle):
    """WDA's default active application: whose tree a read returns."""
    driver.call("POST", "/appium/settings", {"settings": {"defaultActiveApplication": bundle}}, timeout=5)


class NotificationCenter:
    def __init__(self, driver, clock=None):
        self.driver, self.clock = driver, clock or CLOCK

    def read(self, *, origin, size, limit=MAX_ROWS, seconds=MAX_SECONDS):
        """Up to ``limit`` rows within ``seconds``; Notification Center closed and ``origin`` in front after."""
        driver, clock = self.driver, self.clock
        width, height = float(getattr(size, "width", 390)), float(getattr(size, "height", 844))
        started = clock.monotonic()
        rows, opened = [], False
        try:
            _drag(driver, width * .3, 2, width * .3, height * .6)
            opened = True
            clock.sleep(.6)
            _hint(driver, SPRINGBOARD)
            for read in range(3):
                if clock.monotonic() - started > seconds - 1.0:
                    break
                snapshot = driver.observe(timeout=min(3.0, max(.5, seconds - (clock.monotonic() - started))))
                bundle = getattr(snapshot, "bundle_id", "")
                if bundle and bundle != SPRINGBOARD:
                    break  # it didn't open: an app's own screen is not Notification Center
                for row in screen_rows(snapshot, clock.time()):
                    if row.text not in {r.text for r in rows}:
                        rows.append(row)
                if len(rows) >= limit or clock.monotonic() - started > seconds - 1.5:
                    break
                # Older notifications come up from below: a short drag up the list's middle (never from the bottom
                # edge, which closes Notification Center) shows them.
                _drag(driver, width * .5, height * .7, width * .5, height * .4, ms=200)
                clock.sleep(.35)
        finally:
            try:
                if opened:
                    _drag(driver, width * .5, height - 2, width * .5, height * .25, ms=200)
                    clock.sleep(.4)
            finally:
                try:
                    # Back to the app it started in; with no app named, to WebDriverAgent's own choice, never
                    # SpringBoard: later reads would return SpringBoard's tree over the app in front.
                    _hint(driver, origin or "auto")
                finally:
                    if origin and origin != SPRINGBOARD:
                        driver.call("POST", "/wda/apps/activate", {"bundleId": origin}, timeout=15)
        return rows[:limit]


def message_rows(driver, *, origin, clock=None, limit=12):
    """The newest rows of the Messages list (sender, preview, time); ``origin`` in front again after."""
    clock = clock or CLOCK
    try:
        driver.call("POST", "/wda/apps/activate", {"bundleId": MESSAGES}, timeout=15)
        clock.sleep(.5)
        snapshot = driver.observe(timeout=5)
        back = [e for e in getattr(snapshot, "elements", ()) if e.role.removeprefix("XCUIElementType") == "Button"
                and e.label in ("Back", "Messages") and e.rect[1] < .15 and e.rect[0] < .35]
        if back:  # Messages reopened a conversation: back to the list
            x, y, w, h = back[0].rect
            driver.tap_point(x + w / 2, y + h / 2, snapshot, timeout=10)
            clock.sleep(.6)
            snapshot = driver.observe(timeout=5)
        rows = [row for row in screen_rows(snapshot, clock.time(), app="Messages") if row.age_s is not None]
        return rows[:limit]
    finally:
        if origin and origin != MESSAGES:
            driver.call("POST", "/wda/apps/activate", {"bundleId": origin}, timeout=15)


def describe(rows, limit_chars=FEEDBACK_CHARS):
    """The rows as the model reads them: newest first, codes masked."""
    if not rows:
        return "Notification Center has no notifications."
    lines = [f"Notification Center, {len(rows)} notification{'s' if len(rows) != 1 else ''} (read-only; codes "
             "are masked):"]
    for row in rows:
        public = row.public()
        line = "- " + public["text"]
        if len("\n".join(lines + [line])) > limit_chars:
            lines.append(f"… and {len(rows) - (len(lines) - 1)} more")
            break
        lines.append(line)
    return "\n".join(lines)


class ReadNotifications:
    """agent_hooks.Skill: READ_NOTIFICATIONS."""

    op = "READ_NOTIFICATIONS"
    prompt = ("- READ_NOTIFICATIONS: read Notification Center (up to 30 notifications, read-only; codes come "
              "masked); the rows come back next turn and you stay in the current app.")
    needs_target = False

    def __init__(self, clock=None):
        self.clock = clock or CLOCK

    def available(self, ctx):
        driver = ctx.driver
        return callable(getattr(driver, "call", None)) and callable(getattr(driver, "observe", None))

    def prepare(self, ctx, act, snapshot):
        return Prepared(None, None)

    def perform(self, ctx, prepared, act, snapshot):
        origin = getattr(snapshot, "bundle_id", "") or ctx.app_bundle or ""
        try:
            rows = NotificationCenter(ctx.driver, self.clock).read(origin=origin, size=snapshot)
        except Exception as error:
            return SkillResult(feedback=f"Notification Center couldn't be read ({type(error).__name__}).")
        return SkillResult(feedback=describe(rows), changed=False)
