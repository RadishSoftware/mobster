"""What the X-ray crawler may tap. It fails closed: only a navigation-shaped control whose words name no act."""

import re
from dataclasses import dataclass

from .. import harness_api
from ..packs.core import ACT_LABEL, NEVER
from ..secret_filter import redact

# Words a crawler never taps, on top of the engine's commit words, wherever they sit in a label. Whole words, case
# folded.
# Screen text is data: a label is matched against these lists and never adds to them.
DENY = re.compile(
    r"\b(delete|erase|reset|wipe|remove|sign ?out|log ?out|sign ?off|deactivate|disable account|"
    r"buy|purchase|subscribe|subscription|trial|try (?:it )?free|"
    r"upgrade|go premium|get premium|get pro|unlock|pay|checkout|check out|order|book|donate|tip|"
    r"call|dial|facetime|message|send|post|share|invite|publish|upload|reply|comment|"
    r"allow|don'?t allow|accept|agree|decline|approve|confirm|submit|verify|authenticate|"
    r"sign ?in|log ?in|sign ?up|register|create (?:an )?account|"
    r"block|mute|unfollow|follow|unmatch|cancel subscription|install|download|"
    r"turn (?:on|off)|enable|disable|disconnect|pair|forget|"
    r"emergency|sos|power off|shut ?down|restart|redeem|claim|activate)\b", re.I)
# Act verbs common on screens as nouns ("Feels like 67°", "Clear"): they count only as a label's first word.
LEADING_ONLY = ("like", "likes", "liked", "react", "get", "request", "cancel", "clear", "next", "update", "report",
                "done", "continue", "start", "begin", "restore", "apply", "join", "connect", "save", "open in", "go")
_LEADING_ONLY = re.compile(r"\b(" + "|".join(re.escape(word) for word in LEADING_ONLY) + r")\b", re.I)
# A label that starts with any of these, or with a word above, reads as a command: never tapped, whatever follows.
LEADING = re.compile(r"^(?:" + "|".join(re.escape(word) for word in LEADING_ONLY) + r"|add|new|create|edit|compose|"
                     r"write|record|scan|play|shuffle|try|rate|leave|end|archive|hide|pin|unpin|mark|empty|free up|"
                     r"offload|translate|retry|skip|ok|okay|yes|no|tap|swipe|press|click|accept|send)\b", re.I)
BUTTON_VERB = re.compile(r"^(edit|add|new|create|open|get|go|try|start|join|sign in|log in|sign up|learn more|"
                         r"skip|close|ok|yes|no|retry|refresh|search|filter|sort|select|view|show|hide|play|"
                         r"pause|stop|record|scan)\b", re.I)
SEE_ALL = re.compile(r"^(see all|show all|view all|more|show more|see more|all)\b", re.I)
# A developer's identifier standing in for a label: where it leads is unknown.
IDENTIFIER = re.compile(r"^(?=.*[_.]|[a-z0-9]+(?:-[a-z0-9]+){2,}$)[A-Za-z0-9_.\-]+$")
# Roles never tapped.
CONTROL_ROLES = frozenset({"Switch", "Toggle", "Slider", "Stepper", "Picker", "PickerWheel", "DatePicker",
                           "SegmentedControl", "TextField", "SecureTextField", "SearchField", "TextView", "Key",
                           "Keyboard", "Menu", "MenuItem", "Alert", "Sheet", "Image", "Icon", "StatusBar",
                           "NavigationBar", "Toolbar", "TabBar", "ProgressIndicator", "ActivityIndicator",
                           "PageIndicator", "ScrollBar", "Map", "WebView"})
TOGGLES = frozenset({"Switch", "Toggle"})
CHROME_ROLES = frozenset({"Toolbar", "TabBar", "SearchField", "Keyboard"})
CHEVRON = re.compile(r"^(chevron\.(forward|right)|disclosure|›|>)$", re.I)
# A row's own info button ("More Info", the circled i): the button opens the details, and the row itself may act
# (a Wi-Fi network joins, a Bluetooth device connects). Such a row is never tapped.
INFO = re.compile(r"^(more info|info|info\.circle|i)$", re.I)
CARD_MIN_H, CARD_MIN_W = .1, .3
DATA_ROW_MIN_W, DATA_ROW_PARTS = .7, 3
ICON_WORDS = frozenset({"heart", "star", "bookmark", "plus", "minus", "ellipsis", "more", "menu", "close", "xmark",
                        "trash", "camera", "mic", "microphone", "play", "pause", "share", "cart", "bag", "bell",
                        "search", "filter", "gift", "favorite", "favourite", "settings", "gear", "info"})
# A row outside the content area (under the bars) is reached by scrolling, not tapped where it sits.
CONTENT_TOP, CONTENT_BOTTOM = .12, .88
TAB_BAR_TOP = .88
OCR_ROW_LEFT_MAX = .4
OCR_ROW_MAX_WORDS = 5
OCR_ALIGN = .03
OCR_CENTRED = .1


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str   # why: "cell", "chevron row", "tab", "link", "see all", "list row" when ok; the refusal otherwise


def image_only(snapshot):
    # Unsure is image-only, where stricter rules apply: it fails closed.
    if getattr(snapshot, "source", "") in ("mirroring", "screen", "ocr"):
        return True
    roles = {element.role for element in snapshot.elements}
    return not roles - {"StaticText", "SearchField", "TextField"}


def plain(text):
    """A label on one line, masked here at the source (secret_filter), so no map, event or report holds a secret."""
    return redact(" ".join(str(text or "").replace("\u00a0", " ").split()))


def _first_word_only(text):
    """``text`` with the LEADING_ONLY words after its first word taken out: what the act lists read."""
    first, _, rest = text.partition(" ")
    return " ".join((first + " " + _LEADING_ONLY.sub(" ", rest)).split())


def act_words(label):
    text = plain(label)
    if not text:
        return ""
    lead = LEADING.match(text)
    if lead:
        return f"never tapped: “{lead.group(0).lower()}”"
    read = _first_word_only(text)
    act = harness_api.commit_act("TAP", read, None, None)
    if act:
        return f"names an act ({act})"
    if ACT_LABEL.match(text) or NEVER.search(read):
        return "names an act"
    # An identifier's words count too: "AUTH_PROX_SIGN_IN_CELL" says "sign in".
    spaced = re.sub(r"[_\-.]+", " ", text)
    spaced_read = _first_word_only(spaced)
    found = (DENY.search(read) or DENY.search(spaced_read) or LEADING.match(spaced)
             or (NEVER.search(spaced_read) if spaced != text else None))
    if found:
        return f"never tapped: “{found.group(0).lower()}”"
    return ""


def _inside(point, rect, pad=0.0):
    x, y = point
    rx, ry, rw, rh = rect
    return rx - pad <= x <= rx + rw + pad and ry - pad <= y <= ry + rh + pad


def _contains(outer, inner):
    return (outer[0] - .005 <= inner[0] and outer[1] - .005 <= inner[1]
            and inner[0] + inner[2] <= outer[0] + outer[2] + .005 and inner[1] + inner[3] <= outer[1] + outer[3] + .005)


def in_tab_bar(element, snapshot):
    if "XCUIElementTypeTabBar" in (element.locator or ""):
        return True
    bars = [e for e in snapshot.elements if e.role == "TabBar"]
    return any(_inside(element.center, bar.rect) for bar in bars)


def holds_toggle(element, snapshot):
    """A cell or row with a switch inside it: tapping it could flip the switch."""
    for other in snapshot.elements:
        if other is element or other.role not in TOGGLES:
            continue
        if _inside(other.center, element.rect) or (element.locator and other.locator.startswith(element.locator + "/")):
            return True
    return False


def has_chevron(element, snapshot):
    """A row with a disclosure chevron: an image named like one inside it (Settings' rows: chevron.forward)."""
    for other in snapshot.elements:
        if other is element or not CHEVRON.match(plain(other.label)):
            continue
        if (element.locator and other.locator.startswith(element.locator + "/")) or _inside(other.center, element.rect):
            return True
    return False


def holds_info(element, snapshot):
    """A row with an info button inside it (Settings' Wi-Fi networks and Bluetooth devices)."""
    for other in snapshot.elements:
        if other is element or other.role not in ("Button", "Image") or not INFO.match(plain(other.label)):
            continue
        if (element.locator and other.locator.startswith(element.locator + "/")) or _inside(other.center, element.rect):
            return True
    return False


CHECKMARK = re.compile(r"^(selected|checkmark|checked|checkmark\.circle(\.fill)?)$", re.I)


def choice_list(snapshot):
    # A tap on a row of a list of options picks one, which changes a setting. A selected tab or segment is not one.
    for element in snapshot.elements:
        if in_tab_bar(element, snapshot) or "SegmentedControl" in (element.locator or ""):
            continue
        if element.selected and element.role == "Cell":
            return True
        if element.role in ("Image", "Button") and CHECKMARK.match(plain(element.label)):
            return True
    return False


def under_chrome(element, snapshot):
    """Whether the tap would land on a toolbar, a search bar or the keyboard drawn over the content."""
    point = element.tap_point
    for other in snapshot.elements:
        if other is element or other.role not in CHROME_ROLES:
            continue
        if _inside(point, other.rect):
            return True
    return False


def decide(element, snapshot):
    """Whether the crawler may tap ``element`` on ``snapshot``, and why."""
    label = plain(element.label)
    words = act_words(label) or act_words(element.value if element.role not in TOGGLES else "")
    if words:
        return Verdict(False, words)
    if element.role in TOGGLES or element.role in CONTROL_ROLES:
        return Verdict(False, "a control, not navigation" if element.role not in TOGGLES else "a switch")
    if element.editable:
        return Verdict(False, "a text field")
    if not element.enabled or "TAP" not in element.actions:
        return Verdict(False, "not tappable")
    if not label:
        return Verdict(False, "no label")
    if holds_toggle(element, snapshot):
        return Verdict(False, "holds a switch")
    if image_only(snapshot):
        return _decide_ocr(element, snapshot, label)
    if IDENTIFIER.match(label):
        return Verdict(False, "no readable label")
    x, y = element.tap_point
    tab = in_tab_bar(element, snapshot)
    if not tab and not CONTENT_TOP <= y <= CONTENT_BOTTOM:
        return Verdict(False, "outside the content area")
    if not tab and under_chrome(element, snapshot):
        return Verdict(False, "under a bar")
    if tab and element.role == "Button":
        return Verdict(True, "tab")
    if choice_list(snapshot) and not has_chevron(element, snapshot) and element.role != "Link":
        return Verdict(False, "an option in a choice list")
    if holds_info(element, snapshot):
        return Verdict(False, "a row with its own info button: the row itself may join or connect")
    if element.role == "Cell":
        return Verdict(True, "cell")
    if element.role == "Link":
        return Verdict(True, "link")
    if element.role == "Button" and has_chevron(element, snapshot):
        return Verdict(True, "chevron row")
    if element.role == "Button" and "XCUIElementTypeCell[" in (element.locator or ""):
        return Verdict(True, "cell")
    if element.role in ("Button", "StaticText") and SEE_ALL.match(label):
        return Verdict(True, "see all")
    if element.role in ("Button", "Other") and card_shaped(element, label):
        return Verdict(True, "card")
    return Verdict(False, "not navigation-shaped")


def card_shaped(element, label):
    """A card (tall) or a full-width row of data whose label isn't one icon's word: Weather's modules and days."""
    x, y, w, h = element.rect
    if label.casefold() in ICON_WORDS or len(re.findall(r"[A-Za-z]{2,}", label)) == 0:
        return False
    if " " not in label and re.search(r"[a-z][A-Z]", label):
        return False  # one token with capitals inside it ("KxLxpHjhdScjmXV"): an id, not words
    if h >= CARD_MIN_H and w >= CARD_MIN_W:
        return True
    return w >= DATA_ROW_MIN_W and len([part for part in label.split(",") if part.strip()]) >= DATA_ROW_PARTS


def _decide_ocr(element, snapshot, label):
    """Image-only: a list row or a tab label, from where the line sits and what sits beside it."""
    x, y, w, h = element.rect
    cx, cy = element.center
    words = label.split()
    if any(ch.isdigit() for ch in label) and not re.search(r"[A-Za-z]{3}", label):
        return Verdict(False, "a number")
    if re.fullmatch(r"[A-Z0-9 &'’.,-]{2,}", label) and len(label) > 3:
        return Verdict(False, "a heading")
    if BUTTON_VERB.match(label) and len(words) <= 3:
        return Verdict(False, "reads like a button")
    if label.endswith((".", "!", "?", ":")) or len(words) > OCR_ROW_MAX_WORDS:
        return Verdict(False, "a sentence")
    lines = [e for e in snapshot.elements if e is not element and e.label]
    if cy >= TAB_BAR_TOP:
        row = [e for e in lines if abs(e.center[1] - cy) < .015 and len(plain(e.label).split()) <= 2]
        if len(row) >= 2 and len(words) <= 2:
            return Verdict(True, "tab")
        return Verdict(False, "outside the content area")
    if not CONTENT_TOP <= cy <= CONTENT_BOTTOM:
        return Verdict(False, "outside the content area")
    if abs(cx - .5) < OCR_CENTRED and x > .15:
        return Verdict(False, "centred, reads like a button")
    if SEE_ALL.match(label):
        return Verdict(True, "see all")
    if x > OCR_ROW_LEFT_MAX:
        return Verdict(False, "not a list row")
    aligned = [e for e in lines if abs(e.rect[0] - x) < OCR_ALIGN and e.center[1] != cy
               and abs(e.center[1] - cy) < .12 and len(plain(e.label).split()) <= OCR_ROW_MAX_WORDS]
    if not aligned:
        return Verdict(False, "not a list row")
    return Verdict(True, "list row")


SAFE_DISMISS = re.compile(r"^(not now|cancel|close)$", re.I)


def safe_dismiss(buttons):
    """The one dialog button the crawler may press to get it out of the way, or None: "Not Now", "Cancel" or "Close"."""
    # Anything else (Allow, Don't Allow, OK, Continue) is the user's to answer.
    for name in buttons or ():
        if SAFE_DISMISS.match(plain(name)):
            return name
    return None


# Apps where a tap on a plain row acts in a way words and shapes can't show: App X-ray doesn't start in them.
ACTING_APPS = {
    "com.apple.mobilephone": ("Phone", "a tap on a row there places a call"),
    "com.apple.facetime": ("FaceTime", "a tap on a row there places a call"),
    "com.apple.shortcuts": ("Shortcuts", "a tap on a tile there runs a shortcut"),
    "com.apple.Home": ("Home", "a tap on a tile there switches an accessory"),
}


def app_refusal(bundle):
    """One sentence for an app X-ray won't map (what happened, then what to do), or None for any other app."""
    found = ACTING_APPS.get(bundle if isinstance(bundle, str) else "")
    if found is None:
        return None
    name, why = found
    return f"App X-ray doesn't map {name}: {why}. Pick another app."
