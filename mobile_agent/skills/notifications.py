"""READ_NOTIFICATIONS: Notification Center, read-only, with every code masked."""

import re
from dataclasses import dataclass
from typing import Optional

from ..agent_hooks import Prepared, SkillResult
from .codes import CLOCK, MESSAGES, SPRINGBOARD, mask_codes, parse_age, row_age

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
    body: str = ""

    def public(self, mask=CODE_MASK, now=None):
        """The row for a reader: app, title, body, an ISO time estimate and the text, with codes masked."""
        at = None
        if self.age_s is not None:
            import datetime
            moment = (now if now is not None else datetime.datetime.now().timestamp()) - self.age_s
            at = datetime.datetime.fromtimestamp(moment).astimezone().isoformat(timespec="minutes")
        sender = mask_codes(self.sender, mask, context=self.text)
        return {"app": mask_codes(self.app, mask, context=self.text), "sender": sender, "title": sender,
                "body": mask_codes(self.body, mask, context=self.text), "time": at, "text": mask_codes(self.text, mask),
                "age_s": self.age_s}


def parse_row(text, now=None, app=""):
    """A row's app, sender (its title), body and age from its accessibility text."""
    parts = [part.strip() for part in text.split(", ") if part.strip()]
    while parts and parts[0].casefold() in ROW_FLAGS:
        parts = parts[1:]
    age = row_age(parts, now)
    if not app and len(parts) >= 3:
        app, parts = parts[0], parts[1:]
    sender = parts[0] if parts else ""
    rest = parts[1:]
    if rest and age is not None and len(rest[-1]) <= 24 and parse_age(rest[-1], now) is not None:
        rest = rest[:-1]  # the time part ("2m ago") is the age, not the body
    return Row(text=" ".join(text.split()), app=app or "", sender=sender, age_s=age, body=", ".join(rest))


def since_filter(rows, since, now):
    """The rows at most ``now - since`` seconds old (``since``: epoch seconds); a row with no time is left out."""
    if since is None:
        return list(rows)
    return [row for row in rows if row.age_s is not None and now - row.age_s >= since - 60]


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


def _drag(driver, x1, y1, x2, y2, ms=250, timeout=10):
    driver.call("POST", "/actions", {"actions": [{
        "type": "pointer", "id": "finger", "parameters": {"pointerType": "touch"},
        "actions": [{"type": "pointerMove", "duration": 0, "x": round(x1), "y": round(y1)},
                    {"type": "pointerDown"}, {"type": "pause", "duration": 60},
                    {"type": "pointerMove", "duration": ms, "x": round(x2), "y": round(y2)},
                    {"type": "pointerUp"}]}]}, timeout=timeout)


def _hint(driver, bundle):
    """WDA's default active application: whose tree a read returns."""
    driver.call("POST", "/appium/settings", {"settings": {"defaultActiveApplication": bundle}}, timeout=5)


class NotificationCenter:
    def __init__(self, driver, clock=None):
        self.driver, self.clock = driver, clock or CLOCK
        self.note = ""  # what cut the last read short, in words for the person; empty when it ran through
        self.restored = True  # whether the last read closed Notification Center and brought the app back

    def read(self, *, origin, size, limit=MAX_ROWS, seconds=MAX_SECONDS, keep_partial=False):
        """Up to ``limit`` rows within ``seconds``; Notification Center closed and ``origin`` in front after."""
        driver, clock = self.driver, self.clock
        width, height = float(getattr(size, "width", 390)), float(getattr(size, "height", 844))
        started = clock.monotonic()
        rows, opened, failure = [], False, None
        self.note = ""
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
        except Exception as error:
            failure = error
        finally:
            closing = self._restore(opened, origin, width, height)
        self.restored = closing is None
        problem = failure if failure is not None else closing
        if problem is not None and not (keep_partial and rows):
            raise problem
        if failure is not None:
            self.note = (f"Notification Center was slow to answer ({type(failure).__name__}), so the list may be "
                         "cut short.")
        if closing is not None:
            self.note = (self.note + " " if self.note else "") + (
                f"Mobster couldn't confirm it closed Notification Center and went back to {origin or 'the app'}: "
                "call home.")
        return rows[:limit]

    def open_notification(self, text, *, origin, size):
        """Open the notification whose row text is ``text``: only for a workflow's wait step,
        since opening can mark it read."""
        driver, clock = self.driver, self.clock
        width, height = float(getattr(size, "width", 390)), float(getattr(size, "height", 844))
        tapped = False
        _drag(driver, width * .3, 2, width * .3, height * .6)
        try:
            clock.sleep(.6)
            _hint(driver, SPRINGBOARD)
            for _ in range(3):
                snapshot = driver.observe(timeout=3.0)
                if getattr(snapshot, "bundle_id", "") not in ("", SPRINGBOARD):
                    break
                element = next((e for e in snapshot.elements
                                if " ".join(" ".join(p for p in (e.label, getattr(e, "value", ""))
                                                     if p).split()) == text
                                and (e.role or "").removeprefix("XCUIElementType") in ROW_ROLES), None)
                if element is not None:
                    x, y, w, h = element.rect
                    px, py = round((x + w / 2) * width), round((y + h / 2) * height)
                    driver.call("POST", "/actions", {"actions": [{
                        "type": "pointer", "id": "finger", "parameters": {"pointerType": "touch"},
                        "actions": [{"type": "pointerMove", "duration": 0, "x": px, "y": py}, {"type": "pointerDown"},
                                    {"type": "pause", "duration": 60}, {"type": "pointerUp"}]}]}, timeout=10)
                    tapped = True
                    break
                _drag(driver, width * .5, height * .7, width * .5, height * .4, ms=200)
                clock.sleep(.35)
        finally:
            if tapped:
                self._restore_hint()
            else:
                self._restore(True, origin, width, height)
        return tapped

    def _restore_hint(self):
        try:
            _hint(self.driver, "auto")
        except Exception:  # noqa: BLE001 -- the app that opened is the new front app; the hint is only a read hint
            pass

    def _restore(self, opened, origin, width, height):
        """Close Notification Center and bring ``origin`` back."""
        driver, clock = self.driver, self.clock
        first = None

        def attempt(step):
            nonlocal first
            try:
                step()
            except Exception as error:
                first = first or error

        if opened:
            def close():
                _drag(driver, width * .5, height - 2, width * .5, height * .25, ms=200, timeout=6)
                clock.sleep(.4)
            attempt(close)
        # Back to the app it started in; with no app named, to WebDriverAgent's own choice, never SpringBoard:
        # later reads would return SpringBoard's tree over the app in front.
        attempt(lambda: _hint(driver, origin or "auto"))
        if origin and origin != SPRINGBOARD:
            attempt(lambda: driver.call("POST", "/wda/apps/activate", {"bundleId": origin}, timeout=15))
        return first


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
        from ..harness_api import fence
        origin = getattr(snapshot, "bundle_id", "") or ctx.app_bundle or ""
        try:
            rows = NotificationCenter(ctx.driver, self.clock).read(origin=origin, size=snapshot)
        except Exception as error:
            return SkillResult(feedback=f"Notification Center couldn't be read ({type(error).__name__}).")
        text = describe(rows).replace("<<<", "‹‹‹").replace(">>>", "›››")
        return SkillResult(feedback=fence("Notifications", text), changed=False)
