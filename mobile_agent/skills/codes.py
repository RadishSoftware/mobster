"""USE_CODE: a verification code from the phone, typed into the code field by code, never seen by the model.

``prepare`` checks the field and the app first (code-enforced refusals), then looks for one code at most
10 minutes old, in this order: the keyboard's QuickType suggestion ("From Messages", "From Mail"), Notification
Center, and the newest previews in the Messages list. It always goes back to the app it started in, and returns
the approval title ``Use the code <sender> sent <n> minutes ago to sign in to <App>?``, which never holds the code.
``perform`` checks again that the same app is in front and the code is still fresh, taps the code field (the
keyboard may be up for another field), types the code into it, and returns it as a secret (frontier masks it in
every later row, note, history line and answer) with the field's frame (blurred in screenshots until the field
changes). A typing that fails part way still returns the code and the frame: it may be in the field already.

Refusals, all in code: a search field, a composer, a password, PIN or payment field, a field that doesn't look
like a code field (or 4 to 8 one-character boxes), Messages, Mail, Notes or the Home Screen, an app in front that
can't be named, the app in front changing, two different codes with no sender to tell them apart, and a code
older than 10 minutes.

The MCP tool ``use_code`` uses the same ``find_code`` and ``enter_code``.
"""

from dataclasses import dataclass, field as dataclass_field
import datetime
import re
import time
from typing import Optional

from ..agent_hooks import MASK, Prepared, SkillResult

MAX_AGE_S = 600
SPRINGBOARD = "com.apple.springboard"
MESSAGES = "com.apple.MobileSMS"
# Where a code is read, never typed: a code typed into a message, a mail or a note would send or keep it.
REFUSED_APPS = {MESSAGES: "Messages", "com.apple.mobilemail": "Mail", "com.apple.mobilenotes": "Notes",
                SPRINGBOARD: "the Home Screen"}

# Words that make a message a verification code's. "passcode" here is a bank's one-time passcode in a text;
# a *field* named passcode is the device's or the account's, and is refused (SECRET_FIELD).
STRONG = re.compile(r"\b(?:codes?|verification|verify|one[- ]?time|otp|2fa|two[- ]factor|passcode)\b", re.I)
# Masking reaches further: any number in a sign-in or security message is hidden from the model.
BROAD = re.compile(r"\b(?:codes?|verification|verify|one[- ]?time|otp|2fa|two[- ]factor|passcode|log[- ]?in|"
                   r"sign[- ]?in|security|authenticat\w*)\b", re.I)
# A digit group that can be a code: 4 to 8 digits, or two halves (482 913, 482-913), alone or after a one- to
# three-letter prefix (G-482913). Not money ($1234), a phone number (+1 555…), a time (10:42), a decimal, a
# path or part of a word.
CANDIDATE = re.compile(r"(?:(?<=\b[A-Z]-)|(?<=\b[A-Z]{2}-)|(?<=\b[A-Z]{3}-)|(?<![\w$€£¥#+./:-]))"
                       r"(\d{3,4}[ -]\d{3,4}|\d{4,8})(?![\w%]|[.:,]\d)")
PHONE_BEFORE = re.compile(r"(?:\(\d{3}\)\s?|\d[-. ])$")

CODE_FIELD = re.compile(r"\b(?:codes?|verification|verify|one[- ]?time|otp|2fa|two[- ]factor|digits?|"
                        r"confirmation|authenticat\w*)\b|onetimecode|one_time|otc", re.I)
SECRET_FIELD = re.compile(r"password|passcode|\bpin\b|card|cvv|cvc|csc|expir|ssn|social security|account number|"
                          r"routing", re.I)
NOT_VERIFICATION = re.compile(r"promo|discount|coupon|gift|referral|invit|zip|postal|country|area code|voucher|"
                              r"redeem|qr|bar ?code", re.I)
COMPOSER = re.compile(r"\b(?:message|imessage|text message|reply|comment|compose|post|tweet|caption|note|"
                      r"subject|to:|search|ask)\b", re.I)
PAYMENT_SCREEN = re.compile(r"card number|expiration|expiry|\bcvv\b|\bcvc\b|billing address", re.I)
QUICKTYPE = re.compile(r"\bfrom (messages|mail)\b", re.I)
EDITABLE = {"TextField", "SecureTextField"}


class Clock:
    """Time as the skills read it; tests pass their own."""

    monotonic = staticmethod(time.monotonic)
    sleep = staticmethod(time.sleep)
    time = staticmethod(time.time)


CLOCK = Clock()


class CodeRefused(Exception):
    """One plain sentence: why no code was typed. It never holds a code."""


# -- text ------------------------------------------------------------------------------------------------------

def _candidates(text):
    found = []
    for match in CANDIDATE.finditer(text or ""):
        start = match.start(1)
        if PHONE_BEFORE.search(text[max(0, start - 6):start]):
            continue
        digits = re.sub(r"\D", "", match.group(1))
        if 4 <= len(digits) <= 8:
            found.append((start, match.end(1), digits))
    if len(found) > 1:  # a year beside a real code is not the code
        found = [item for item in found if not re.fullmatch(r"(?:19|20)\d\d", item[2])] or found
    return found


def extract_code(text, require_words=True):
    """The one code a message carries, or None: the digit group nearest a code word, a 6-digit one first."""
    text = text or ""
    words = [m.start() for m in STRONG.finditer(text)]
    if require_words and not words:
        return None
    found = _candidates(text)
    if not found:
        return None

    def rank(item):
        distance = min(abs(item[0] - at) for at in words) if words else 0
        return (distance // 40, len(item[2]) != 6, distance, item[0])
    return min(found, key=rank)[2]


def mask_codes(text, mask=MASK):
    """``text`` with every possible code in a sign-in or security message replaced by ``mask``."""
    if not text or not BROAD.search(text):
        return text
    out = text
    for start, end, _digits in sorted(_candidates(text), reverse=True):
        out = out[:start] + mask + out[end:]
    return out


def parse_age(text, now=None):
    """Seconds since a notification's or a message row's time (now, 2m ago, 10:42 AM, Yesterday); None when it
    has none. Absolute times are today's in the Mac's own time zone, or yesterday's when that is in the future."""
    lowered = (text or "").lower()
    if re.search(r"\b(?:just now|now)\b", lowered):
        return 0
    for pattern, unit in ((r"\b(\d{1,3})\s*(?:s|sec|secs|seconds?)\b\.?\s*ago", 1),
                          (r"\b(\d{1,3})\s*(?:m|min|mins|minutes?)\b\.?\s*ago", 60),
                          (r"\b(\d{1,2})\s*(?:h|hr|hrs|hours?)\b\.?\s*ago", 3600)):
        match = re.search(pattern, lowered)
        if match:
            return int(match.group(1)) * unit
    if re.search(r"\byesterday\b|\b(?:mon|tues|wednes|thurs|fri|satur|sun)day\b", lowered):
        return 86400
    now = time.time() if now is None else now
    local = datetime.datetime.fromtimestamp(now)
    match = re.search(r"\b(\d{1,2}):([0-5]\d)\s*([ap])\.?\s*m\b\.?", lowered)
    if match:
        hour = int(match.group(1)) % 12 + (12 if match.group(3) == "p" else 0)
    else:
        match = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", lowered)
        if not match:
            return None
        hour = int(match.group(1))
    then = local.replace(hour=hour, minute=int(match.group(2)), second=0, microsecond=0)
    seconds = (local - then).total_seconds()
    if seconds < -60:
        seconds += 86400
    return max(0, int(seconds))


def row_age(parts, now=None):
    """The age a row's own time part gives (the last part that parses), else its whole text's."""
    for part in reversed(parts):
        age = parse_age(part, now) if len(part) <= 24 else None
        if age is not None:
            return age
    return None


# -- the field and the app -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Field:
    """The field a code goes into: what the checks read, and where it is (screen fractions)."""
    label: str
    role: str
    rect: tuple
    identifier: str = ""
    placeholder: str = ""
    locator: str = ""

    @classmethod
    def of(cls, element):
        role = (getattr(element, "role", "") or "").removeprefix("XCUIElementType")
        return cls(label=getattr(element, "label", "") or "", role=role, rect=tuple(element.rect),
                   identifier=str(getattr(element, "identifier", "") or ""),
                   placeholder=getattr(element, "placeholder", "") or "", locator=getattr(element, "locator", "") or "")

    @property
    def words(self):
        return " ".join(part for part in (self.label, self.placeholder, self.identifier) if part)

    @property
    def center(self):
        x, y, w, h = self.rect
        return x + w / 2, y + h / 2


def _role(element):
    return (getattr(element, "role", "") or "").removeprefix("XCUIElementType")


def code_boxes(target, elements):
    """The row of 4 to 8 one-character boxes ``target`` is one of, left to right; () when it isn't."""
    tx, ty, tw, th = target.rect
    if tw >= .2:
        return ()
    row = [e for e in elements if _role(e) in EDITABLE and e.rect[2] < .2
           and abs((e.rect[1] + e.rect[3] / 2) - (ty + th / 2)) < .03 and abs(e.rect[3] - th) < .03]
    if not any(Field.of(e).rect == target.rect for e in row):
        row.append(target)
    return tuple(sorted(row, key=lambda e: e.rect[0])) if 4 <= len(row) <= 8 else ()


def check_app(bundle):
    if not bundle:
        # Without the app's name none of the checks below can hold: it might be Messages.
        raise CodeRefused("Mobster couldn't tell which app is in front, so it typed no code.")
    if bundle in REFUSED_APPS:
        raise CodeRefused(f"Mobster never types a code into {REFUSED_APPS[bundle]}: it only goes into the code "
                          "field of the app you're signing in to.")


def check_field(target, snapshot):
    """Raise CodeRefused unless ``target`` looks like a verification-code field of the screen ``snapshot``."""
    words = target.words
    if target.role == "SearchField":
        raise CodeRefused("That is a search field, not a code field, so Mobster typed nothing.")
    if target.role == "TextView":
        raise CodeRefused("That is a message or text box, not a code field, so Mobster typed nothing.")
    if target.role not in EDITABLE:
        raise CodeRefused("The target isn't a text field. Point USE_CODE at the code field.")
    if SECRET_FIELD.search(words):
        raise CodeRefused("That looks like a password, PIN or card field, not a code field, so Mobster typed "
                          "nothing.")
    if PAYMENT_SCREEN.search(getattr(snapshot, "text", "") or ""):
        raise CodeRefused("This looks like a payment form, so Mobster typed no code.")
    if NOT_VERIFICATION.search(words):
        raise CodeRefused("That is a promo, gift, postal or other code field, not a sign-in code field, so Mobster "
                          "typed nothing.")
    if CODE_FIELD.search(words):
        return ()
    if COMPOSER.search(words):
        raise CodeRefused("That is a message, comment or search box, not a code field, so Mobster typed nothing.")
    boxes = code_boxes(target, getattr(snapshot, "elements", ()))
    if boxes:
        return boxes
    raise CodeRefused("That field doesn't look like a verification-code field, so Mobster typed nothing.")


def app_name(bundle):
    from ..catalog import APPS
    for app in APPS:
        if app.get("bundleId") == bundle:
            return app["name"]
    return "this app"


# -- finding a code --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    code: str
    sender: str
    age_s: Optional[int]
    source: str           # quicktype | notification | messages
    text: str = dataclass_field(default="", repr=False)


@dataclass(frozen=True)
class Found:
    """A code, found for one field of one app. Held in memory only (Prepared.state)."""
    code: str = dataclass_field(repr=False)
    sender: str
    age_s: Optional[int]
    source: str
    bundle: str
    field: Field
    found_at: float
    boxes: tuple = ()

    def __repr__(self):  # never the code, even in a traceback's locals
        return f"Found(source={self.source!r}, sender={self.sender!r}, age_s={self.age_s!r})"

    @property
    def frame(self):
        """Where the code shows once typed (screen fractions): the row of boxes, else the field."""
        return _union(self.boxes) if self.boxes else tuple(self.field.rect)


def _union(elements):
    rects = [tuple(element.rect) for element in elements]
    left, top = min(r[0] for r in rects), min(r[1] for r in rects)
    right, bottom = max(r[0] + r[2] for r in rects), max(r[1] + r[3] for r in rects)
    return (left, top, right - left, bottom - top)


def _front(driver, snapshot=None):
    active = getattr(driver, "active_app", None)
    if callable(active):
        try:
            return active(timeout=5)
        except Exception:
            pass
    snapshot = snapshot if snapshot is not None else driver.observe(timeout=5)
    return getattr(snapshot, "bundle_id", "") or ""


def _activate(driver, bundle):
    if bundle and bundle != SPRINGBOARD:
        driver.call("POST", "/wda/apps/activate", {"bundleId": bundle}, timeout=15)


def _texts(snapshot):
    seen, out = set(), []
    for element in getattr(snapshot, "elements", ()):
        for text in (" ".join(p for p in (element.label, getattr(element, "value", "")) if p).strip(),):
            if text and text not in seen:
                seen.add(text)
                out.append(text)
    for line in (getattr(snapshot, "text", "") or "").splitlines():
        if line.strip() and line.strip() not in seen:
            seen.add(line.strip())
            out.append(line.strip())
    return out


def _clean_sender(sender, code):
    sender = " ".join((sender or "").split())[:60]
    if not sender or (code and code in re.sub(r"\D", "", sender)):
        return "Messages"
    return sender


def quicktype_candidates(snapshot):
    """Codes the keyboard offers (QuickType: "From Messages 482913")."""
    out = []
    for text in _texts(snapshot):
        match = QUICKTYPE.search(text)
        if not match:
            continue
        code = extract_code(QUICKTYPE.sub(" ", text), require_words=False)
        if code:
            out.append(Candidate(code, match.group(1).title(), None, "quicktype", text))
    return out


def row_candidates(rows, source, now=None):
    """Codes in notification or message rows: (code, sender, age) per row with code wording."""
    out = []
    for row in rows:
        code = extract_code(row.text)
        if code:
            out.append(Candidate(code, _clean_sender(row.sender, code), row.age_s, source, row.text))
    return out


def _focus(driver, target, snapshot, clock, seconds=2.0):
    """Tap the field (only when no keyboard is up) and wait for the keyboard; the latest screen."""
    if getattr(snapshot, "keyboard", "") == "visible":
        return snapshot
    x, y = target.center
    driver.tap_point(x, y, snapshot, timeout=10)
    end = clock.monotonic() + seconds
    while True:
        clock.sleep(.25)
        current = driver.observe(timeout=5)
        if getattr(current, "keyboard", "") or clock.monotonic() >= end:
            return current


def find_code(driver, *, field, bundle, snapshot, hint=None, clock=None,
              sources=("quicktype", "notification", "messages")):
    """The one fresh code for ``field`` of ``bundle`` (Found), or CodeRefused. Checks the app and the field
    before it looks anywhere; always ends with ``bundle`` in front again."""
    from .notifications import NotificationCenter, message_rows
    clock = clock or CLOCK
    check_app(bundle)
    boxes = check_field(field, snapshot)
    hint = " ".join(str(hint or "").split()).casefold() or None
    stale = hinted_out = False
    for source in sources:
        try:
            if source == "quicktype":
                snapshot = _focus(driver, field, snapshot, clock)
                found = quicktype_candidates(snapshot)
            elif source == "notification":
                found = row_candidates(NotificationCenter(driver, clock).read(origin=bundle, size=snapshot),
                                       "notification")
            else:
                found = row_candidates(message_rows(driver, origin=bundle, clock=clock), "messages")
        finally:
            if source != "quicktype" and _front(driver) != bundle:
                _activate(driver, bundle)
        fresh = [c for c in found if c.source == "quicktype" or (c.age_s is not None and c.age_s <= MAX_AGE_S)]
        if found and not fresh:
            stale = True
            continue
        if hint:
            matching = [c for c in fresh if hint in (c.sender + " " + c.text).casefold()]
            hinted_out = hinted_out or (bool(fresh) and not matching)
            fresh = matching
        codes = {c.code for c in fresh}
        if len(codes) > 1:
            raise CodeRefused("Two different codes arrived in the last 10 minutes"
                              + (" from that sender" if hint else "")
                              + ", so Mobster typed neither. Name the sender in USE_CODE's text, or ask for a new "
                                "code.")
        if codes:
            best = min(fresh, key=lambda c: c.age_s if c.age_s is not None else 0)
            return Found(code=best.code, sender=best.sender, age_s=best.age_s, source=best.source, bundle=bundle,
                         field=field, found_at=clock.monotonic(), boxes=boxes)
    if hinted_out:
        raise CodeRefused("No code from that sender arrived in the last 10 minutes. Ask for a new code, then try "
                          "again.")
    if stale:
        raise CodeRefused("The newest code is older than 10 minutes, so Mobster won't use it. Ask for a new code, "
                          "then try again.")
    raise CodeRefused("No verification code arrived in the last 10 minutes (keyboard suggestions, Notification "
                      "Center and Messages). Ask for a new code, then try again.")


def say_age(seconds):
    """A code's age as people say it: "just now", "40 seconds ago", "2 minutes ago"."""
    seconds = max(0, int(seconds))
    if seconds < 5:
        return "just now"
    if seconds < 60:
        return f"{seconds} seconds ago"
    minutes = seconds // 60
    return f"{minutes} minute{'s' if minutes != 1 else ''} ago"


def approval_title(found, app):
    """The approval question: the sender, the age, the app; never the code."""
    if found.age_s is None:
        return f"Use the code the keyboard suggests from {found.sender} to sign in to {app}?"
    return f"Use the code {found.sender} sent {say_age(found.age_s)} to sign in to {app}?"


def _refind(snapshot, target):
    elements = list(getattr(snapshot, "elements", ()))
    if target.locator:
        for element in elements:
            if getattr(element, "locator", "") == target.locator:
                return element
    for element in elements:
        if _role(element) == target.role and all(abs(a - b) < .02 for a, b in zip(element.rect, target.rect)):
            return element
    return None


def enter_code(driver, found, *, clock=None):
    """Type ``found``'s code into its field, after checking the app in front and the code's age again. Returns the
    field's frame (screen fractions). Every CodeRefused comes before a key is sent."""
    clock = clock or CLOCK
    fresh = driver.observe(timeout=5)
    if _front(driver, fresh) != found.bundle:
        raise CodeRefused("The app in front changed since Mobster found the code, so it typed nothing.")
    if found.age_s is not None and found.age_s + (clock.monotonic() - found.found_at) > MAX_AGE_S:
        raise CodeRefused("The code is older than 10 minutes now, so Mobster typed nothing. Ask for a new code, "
                          "then try again.")
    element = _refind(fresh, found.field)
    if element is None:
        raise CodeRefused("The code field is no longer on the screen, so Mobster typed nothing.")
    target = Field.of(element)
    boxes = code_boxes(target, getattr(fresh, "elements", ())) if found.boxes else ()
    if boxes:
        target = Field.of(boxes[0])
    # Always the code field's own tap: a keyboard that is up may belong to another field (the email above it),
    # and keys go to whichever field has the focus.
    keyboard = getattr(fresh, "keyboard", "") == "visible"
    x, y = target.center
    driver.tap_point(x, y, fresh, timeout=10)
    if keyboard:
        clock.sleep(.15)
    else:
        end = clock.monotonic() + 2.0
        while clock.monotonic() < end:
            clock.sleep(.2)
            if getattr(driver.observe(timeout=5), "keyboard", ""):
                break
    driver.call("POST", "/wda/keys", {"value": [found.code]}, timeout=10)
    return _union(boxes) if boxes else tuple(element.rect)


# -- the skill -------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class _Pending:
    found: Optional[Found] = None
    refusal: str = ""


def _target(act, snapshot):
    element = act.get("element") if isinstance(act, dict) else None
    if element is not None:
        return element
    elements = list(getattr(snapshot, "elements", ()))
    wanted = act.get("target") if isinstance(act, dict) else None
    if wanted:
        for element in elements:
            if element.id == wanted:
                return element
    label = (act.get("target_label") if isinstance(act, dict) else None) or None
    if label:
        named = [e for e in elements if e.label == label and _role(e) in EDITABLE | {"SearchField", "TextView"}]
        if len(named) == 1:
            return named[0]
    return None


class UseCode:
    """agent_hooks.Skill: USE_CODE."""

    op = "USE_CODE"
    prompt = ("- USE_CODE target [text]: a sign-in asks for a verification code sent by text or email: Mobster finds "
              "it itself (keyboard suggestion, Notification Center, Messages), asks the user, and types it into "
              "target, the code field; text may name the sender (\"Chase\"). You never see the code, so never open "
              "Messages or Mail to read one.")
    needs_target = True

    def __init__(self, clock=None):
        self.clock = clock or CLOCK

    def available(self, ctx):
        driver = ctx.driver
        return callable(getattr(driver, "call", None)) and callable(getattr(driver, "observe", None))

    def prepare(self, ctx, act, snapshot):
        element = _target(act, snapshot)
        if element is None:
            return Prepared(None, _Pending(refusal="USE_CODE needs the code field as its target."))
        bundle = getattr(snapshot, "bundle_id", "") or ctx.app_bundle or ""
        hint = act.get("text") if isinstance(act, dict) else None
        try:
            found = find_code(ctx.driver, field=Field.of(element), bundle=bundle, snapshot=snapshot, hint=hint,
                              clock=self.clock)
        except CodeRefused as refusal:
            return Prepared(None, _Pending(refusal=str(refusal)))
        except Exception as error:
            return Prepared(None, _Pending(refusal=f"Mobster couldn't look for the code ({type(error).__name__})."))
        return Prepared(approval_title(found, app_name(bundle)), _Pending(found=found))

    def perform(self, ctx, prepared, act, snapshot):
        pending = prepared.state if isinstance(prepared.state, _Pending) else _Pending(refusal="USE_CODE found no code.")
        if pending.found is None:
            return SkillResult(feedback=f"USE_CODE typed nothing: {pending.refusal}")
        found = pending.found
        try:
            rect = enter_code(ctx.driver, found, clock=self.clock)
        except CodeRefused as refusal:
            return SkillResult(feedback=f"USE_CODE typed nothing: {refusal}")
        except Exception as error:
            # Only the error's kind: a driver's message is never echoed next to a code. The code may be in the
            # field already (a timeout after the keys landed), so it is masked and its field blurred all the same.
            return SkillResult(feedback=f"USE_CODE couldn't type the code ({type(error).__name__}). Look at the "
                                        "field before trying again.", changed=True, secrets=(found.code,),
                               secret_rects=(found.frame,))
        try:
            ctx.emit({"event": "code_used", "source": found.source, "sender": found.sender, "age_s": found.age_s})
        except Exception:
            pass
        return SkillResult(feedback=f"Entered the code from {found.sender}.", changed=True,
                           secrets=(found.code,), secret_rects=(tuple(rect),))
