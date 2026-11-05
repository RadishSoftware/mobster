"""Typed user-policy questions; inferred output never grants action authority."""

import re
from dataclasses import replace
from enum import StrEnum


class RiskTier(StrEnum):
    """Named dispatch risk for confidence-gated actions."""
    SCROLL = "scroll"
    NAVIGATION = "navigation"
    TYPE = "type"
    SIDE_EFFECT = "side_effect"


# AUTHORITY_RULES spirit: send/post/buy/follow/like are never implicit.
SIDE_EFFECT_REQUEST = re.compile(
    r"\b(send|post|like|follow|buy|purchase|order|pay|submit|publish|comment|share|"
    r"delete|remove|confirm|checkout|transfer|message|email|install|upload|redeem|"
    r"book|reserve|donate|tip|subscribe|set|save|create|update|apply|"
    r"sign[ -]?up|log[ -]?in)\b",
    re.I)

SIDE_EFFECT_CONTROL = re.compile(
    r"\b(send|post|like|follow|buy|purchase|order|pay|submit|save|publish|comment|"
    r"share|delete|remove|confirm|checkout|done|transfer|donate|tip|subscribe|"
    r"accept|approve|install|upload|redeem|book|reserve|create|sign[ -]?up|log[ -]?in)\b",
    re.I)


# Roles whose tap only moves between screens.
NAVIGATION_TAP_ROLES = frozenset({"Button", "Cell", "Link", "StaticText", "Image", "Icon", "Other"})
# Verbs that make even a plain button state-changing; any match keeps the verifier.
STATE_CHANGING_LABEL = re.compile(
    r"\b(erase|reset|turn\s+(on|off)|enable|disable|sign\s*(in|out|up)|log\s*(in|out)|restart|"
    r"shut\s*down|power|clear|delete|remove|allow|deny|block|unblock|forget|disconnect|connect|"
    r"pair|unpair|update|install|download|upgrade|agree|accept|decline|join|leave|end|stop|start|"
    r"call|dial|emergency|sos|pay|buy|purchase|send|share|trust|approve|confirm|submit|save|"
    r"done|add|create|new|edit|change|set|reply|post|follow|like|subscribe|unsubscribe|report|"
    r"archive|mute|unmute|hide|lock|unlock|restore|backup|sync|transfer|verify)\b",
    re.I)


# Keys of an on-screen keypad (Calculator, a PIN or dial pad): each press is input, not an effect to de-duplicate.
KEYPAD_KEY = re.compile(r"^(\d|[+\-×x*÷/=.%,]|plus|minus|add|subtract|multiply|times|divide|equals|decimal|point|"
                        r"percent|clear|all clear|delete|backspace|negate|change sign|"
                        r"zero|one|two|three|four|five|six|seven|eight|nine)$", re.I)


def is_keypad_key(operation, role, label):
    return operation == "TAP" and role in ("Key", "Button") and bool(KEYPAD_KEY.match((label or "").strip()))


# SpringBoard's passcode keypad: lockscreen.py handles the lock screen, and every model tap there is refused.
SPRINGBOARD_BUNDLE = "com.apple.springboard"
_DIGIT_KEY = re.compile(r"^\s*(\d)(?!\d)")
_PASSCODE_MARKER = re.compile(r"\b(enter passcode|passcode|emergency|touch id|face id)\b", re.I)


def springboard_keypad(snapshot):
    """True when ``snapshot`` is SpringBoard showing a digit keypad. Errs toward True: it only refuses."""
    if getattr(snapshot, "bundle_id", "") != SPRINGBOARD_BUNDLE:
        return False
    digits, marked = set(), False
    for element in getattr(snapshot, "elements", ()) or ():
        label = element.label or ""
        found = _DIGIT_KEY.match(label)
        if found and element.role in ("Button", "Key", "Other") and len(label) <= 12:
            digits.add(found.group(1))
        elif _PASSCODE_MARKER.search(label):
            marked = True
    return len(digits) >= 10 or len(digits) >= 6 and marked


# Controls whose tap commits something the user would want to see first: money, messages, publishing and deletion.
COMMIT_CONTROL_WORDS = (
    "send", "post", "publish", "reply", "comment", "share", "tweet", "buy", "purchase", "order", "pay", "checkout",
    "place order", "transfer", "donate", "tip", "subscribe", "book", "reserve", "delete", "erase", "remove", "reset",
    "install", "upload", "redeem", "call", "dial", "facetime", "follow", "sign[ -]?up", "submit", "confirm")
COMMIT_CONTROL = re.compile(r"\b(" + "|".join(COMMIT_CONTROL_WORDS) + r")\b", re.I)
# Leading words of a label that can name a commit.
GUARD_WORDS = 4
# Controls that only open a sheet of choices (whose committing choice is guarded itself).
SHEET_OPENERS = frozenset({"share", "share…", "share..."})


APPROVAL_OPERATIONS = frozenset({"TAP", "SUBMIT", "TYPE_SUBMIT"})

# Commits beyond COMMIT_CONTROL's words, for "ask before acting" only; a lookahead spares a control that opens.
APPROVAL_COMMIT = re.compile(
    r"\b(?:request(?!\s+(?:desktop|mobile)\b)|(?:move\s+to\s+)?trash|"
    r"clear(?!\s+(?:text|search|query|field|filters?|selection)\b)|accept|decline|join|leave|"
    r"block|report|unsubscribe|unfollow|unmatch|archive|mark\s+as\s+(?:junk|spam)|rent|"
    r"cancel\s+(?:ride|trip|order|subscription|booking|reservation|membership|plan|appointment|delivery)|"
    r"end\s+(?:call|trip|ride)|hang\s+up)\b"
    r"|^\s*get\b(?!\s+(?:started|directions|help|info|more|support|details|tickets|the\s+app)\b)",
    re.I)
# The approval card's act for each of those words ("request", "trash"...).
APPROVAL_ACT_ALIASES = {"move to trash": "trash", "mark as junk": "junk", "mark as spam": "junk", "hang up": "end call"}


# Roles whose label is what they show, not what a tap on them does.
CONTENT_ROLES = frozenset({"Cell", "StaticText", "TextField", "TextView", "SecureTextField", "SearchField", "Image",
                           "Icon"})
# A tap on a field only focuses it, whatever its label ("Order more coffee filters", "Comment").
EDITABLE_ROLES = frozenset({"TextField", "TextView", "SecureTextField", "SearchField"})
# The state iOS appends to a reminder's row ("Buy oat milk, Incomplete"): that label is always content.
ROW_STATE = re.compile(r",\s*(?:Incomplete|Completed)$", re.I)
# Acts a row names as often as a control ("Call the dentist"): in content, only in labels of CONTROL_WORDS words.
TALK_ACTS = frozenset({"call", "dial", "facetime", "reply", "comment", "follow"})
CONTROL_WORDS = 2
# The checkout button's own words, which COMMIT_CONTROL's "place order" does not read.
PLACE_ORDER = re.compile(r"^place\s+(?:(?:your|the|this|my)\s+)?order\b", re.I)


# Bare labels that only open a composer or a sheet (a tap on them sends nothing).
TAP_OPENERS = frozenset({"share", "share…", "share...", "comment", "comments", "reply"})
UNSEND = re.compile(r"\b(?:unsend|undo send)\b", re.I)


def commit_label(label, role=None):
    """The commit a control's label names, or None; a spurious ask on a plain row is the safe failure."""
    label = " ".join((label or "").split())
    if ROW_STATE.search(label):
        return None
    folded = label.casefold()
    if role is not None and folded in TAP_OPENERS:
        return None  # it opens a composer or a sheet, whose own Send or Post asks
    unsend = UNSEND.search(label)
    if unsend and role not in EDITABLE_ROLES:
        return unsend.group(0)
    match = COMMIT_CONTROL.search(label) or APPROVAL_COMMIT.search(label)
    if role not in CONTENT_ROLES:
        return match.group(0) if match else None
    if role in EDITABLE_ROLES or "," in label:
        return None
    first = PLACE_ORDER.match(label) or COMMIT_CONTROL.match(label) or APPROVAL_COMMIT.match(label)
    if first is None or _act(first.group(0)) in TALK_ACTS and len(label.split()) > CONTROL_WORDS:
        return None
    return first.group(0)


# Roles that hold other elements rather than act on a tap: panes, bars, sheets. Text inside one is not its label.
CONTAINER_ROLES = frozenset({"Application", "Window", "ScrollView", "Table", "CollectionView", "WebView", "Group",
                             "NavigationBar", "TabBar", "Toolbar", "StatusBar", "Keyboard", "Alert", "Sheet",
                             "Menu", "MenuBar"})


def _holds(outer, point):
    x, y, w, h = outer.rect
    return x - 1e-6 <= point[0] <= x + w + 1e-6 and y - 1e-6 <= point[1] <= y + h + 1e-6


def same_words(a, b):
    return " ".join((a or "").split()).casefold() == " ".join((b or "").split()).casefold()


def approval_subject(elements, target):
    """The element "ask before acting" reads for a tap on ``target``; a commit is never read away."""
    if target is None or target.role not in CONTENT_ROLES or target.role in EDITABLE_ROLES:
        return target
    point = target.tap_point
    around = [e for e in elements if e is not target and e.id != target.id and _holds(e, point)]
    area = lambda e: e.rect[2] * e.rect[3]
    controls = sorted((e for e in around if e.role not in CONTENT_ROLES and e.role not in CONTAINER_ROLES),
                      key=lambda e: (not same_words(e.label, target.label), area(e)))
    if controls:
        control = controls[0]
        if not control.label.strip():
            # An unlabelled button is named by the text on it.
            return replace(control, label=target.label)
        if commit_label(control.label, control.role) or not commit_label(target.label, target.role):
            return control
        return target
    rows = sorted((e for e in around if e.role == "Cell"), key=area)
    if rows and ("," in rows[0].label or ROW_STATE.search(" ".join(rows[0].label.split()))):
        return rows[0]
    return target


def label_role(operation, role):
    """The role ``commit_label`` reads a target's label with: its own for a tap, none for a submit."""
    return role if operation == "TAP" else None


def approval_kind(operation, label, *, risk_tier, uncertain=False, role=None):
    """What "ask before acting" asks: "commit", "unsure" or None; scrolling and typing without Return never commit."""
    if operation in APPROVAL_OPERATIONS and (
            commit_label(label, label_role(operation, role))
            or operation in {"SUBMIT", "TYPE_SUBMIT"} and risk_tier == RiskTier.SIDE_EFFECT.value):
        return "commit"
    return "unsure" if uncertain else None


# The act a commit approval names, from the control's label or else the request, and its title.
APPROVAL_TITLES = {
    "send": "Send this message?", "reply": "Send this reply?", "comment": "Post this comment?",
    "post": "Post this?", "publish": "Publish this?", "tweet": "Post this?", "share": "Share this?",
    "buy": "Buy this?", "purchase": "Buy this?", "order": "Place this order?", "place order": "Place this order?",
    "checkout": "Check out?", "pay": "Make this payment?", "transfer": "Make this transfer?",
    "donate": "Make this donation?", "tip": "Leave this tip?", "subscribe": "Subscribe?",
    "book": "Make this booking?", "reserve": "Make this reservation?", "delete": "Delete this?",
    "erase": "Erase this?", "remove": "Remove this?", "reset": "Reset this?", "install": "Install this?",
    "upload": "Upload this?", "redeem": "Redeem this?", "call": "Place this call?", "dial": "Place this call?",
    "facetime": "Start this FaceTime call?", "follow": "Follow this account?", "sign up": "Sign up?",
    "submit": "Submit this?", "confirm": "Confirm this?",
    "request": "Send this request?", "trash": "Move this to the trash?", "clear": "Clear these?",
    "accept": "Accept this?", "decline": "Decline this?", "join": "Join this?", "leave": "Leave this?",
    "block": "Block this contact?", "report": "Report this?", "unsubscribe": "Unsubscribe?",
    "unfollow": "Unfollow this account?", "unmatch": "Unmatch?", "archive": "Archive this?",
    "junk": "Mark this as junk?", "rent": "Rent this?", "get": "Get this app?", "end call": "End this call?",
    "react": "Send this reaction?", "unsend": "Unsend this message?",
}
MESSAGE_ACTS = frozenset({"send", "reply", "comment", "post", "publish", "tweet", "share"})
# Acts whose card names who it goes to ("Send this message to Sam?").
RECIPIENT_TITLES = {"send": "Send this message to {}?", "reply": "Send this reply to {}?"}


def _act(match):
    act = re.sub(r"[\s-]+", " ", match.casefold()).strip()
    if PLACE_ORDER.match(act):
        return "place order"
    if act.replace(" ", "") == "signup":
        return "sign up"
    if act in ("unsend", "undo send"):
        return "unsend"
    if act.startswith("cancel ") or act.startswith("end "):
        return "end call" if act == "end call" else act
    return APPROVAL_ACT_ALIASES.get(act, act)


def commit_act(label, request="", role=None, operation="TAP"):
    """The commit a control performs, as one lowercase verb ("send", "place order"), or "submit"."""
    named = commit_label(label, label_role(operation, role))
    if named:
        return _act(named)
    match = COMMIT_CONTROL.search(asked_part(request))
    return _act(match.group(0)) if match else "submit"


def approval_title(act, *, unsure=False, recipient=None):
    """The approval card's title: the act as a question ("Send this message to Sam?")."""
    if unsure:
        return "Not sure about this step"
    if recipient and act in RECIPIENT_TITLES:
        return RECIPIENT_TITLES[act].format(recipient)
    if act and act.startswith("cancel "):
        return f"Cancel this {act.split(' ', 1)[1]}?"
    return APPROVAL_TITLES.get(act, "Do this now?")


def action_risk_tier(operation, target_label="", goal=""):
    """Risk tier for one action. Side-effect parse follows the AUTHORITY_RULES spirit."""
    if operation == "TYPE_SUBMIT":
        # Typing plus Return: a search or address stays TYPE-tier; a send is a side effect.
        if SIDE_EFFECT_CONTROL.search(target_label or "") or SIDE_EFFECT_REQUEST.search(goal or ""):
            return RiskTier.SIDE_EFFECT
        return RiskTier.TYPE
    if operation in {"TYPE", "INCREMENT", "DECREMENT"}:
        return RiskTier.TYPE
    if operation in {"SWIPE_UP", "SWIPE_DOWN"}:
        return RiskTier.SCROLL
    # Horizontal swipes can page media or reveal row actions, so they stay navigation.
    if operation in {"SWIPE_LEFT", "SWIPE_RIGHT", "BACK", "HOME",
                       "VOLUME_UP", "VOLUME_DOWN", "LAUNCH_APP"}:
        return RiskTier.NAVIGATION
    if SIDE_EFFECT_CONTROL.search(target_label or "") or SIDE_EFFECT_REQUEST.search(goal or ""):
        return RiskTier.SIDE_EFFECT
    return RiskTier.NAVIGATION


# The verifier's UNCLEAR ends a run; the request's own words are evidence that the act it names is the right one.
PROHIBITION_SENTENCE = re.compile(r"[^.!?]*\b(?:do not|don't|never|without|only look)\b[^.!?]*(?:[.!?]|$)", re.I)
CHANGE_REQUEST = re.compile(
    r"\b(add|log|create|write|compose|draft|send|reply|message|share|post|book|reserve|order|buy|purchase|"
    r"pay|transfer|set|schedule|change|update|edit|rename|move|archive|delete|remove|mark|flag|star|save|"
    r"favorite|like|follow|connect|invite|accept|decline|join|upload|enable|disable|turn on|turn off|apply)\b",
    re.I)
_TEXT_ASKED = re.compile(r"\b(write|type|enter|message|reply|note|comment|compose|add|search|rename|label|name|"
                         r"title|caption|describe|log|fill|draft|email|text)\b", re.I)


def asked_part(request):
    """The request without its prohibitions ("Do not delete anything" asks for no deletion)."""
    return PROHIBITION_SENTENCE.sub(" ", request or "")


def asks_for_changes(request):
    return bool(CHANGE_REQUEST.search(asked_part(request)))


def _inflections(word):
    word = word.casefold()
    stem = word[:-1] if word.endswith("e") else word
    return {word, word + "s", word + "d", word + "ed", stem + "ing", stem + "ed", word + "ment", stem + "ion"}


# Acts that are the same request in other words.
ACT_SYNONYMS = (
    {"order", "checkout", "purchase", "buy", "pay", "place"},
    {"book", "reserve", "reservation", "confirm"},
    {"send", "message", "post", "reply", "share"},
    {"request", "split", "charge"},
    {"add", "create", "new", "save", "log"},
    {"archive", "file"},
    {"delete", "remove", "trash"},
)


def synonyms(act):
    """Every form of the other words for the same act (see ACT_SYNONYMS)."""
    out = set()
    for group in ACT_SYNONYMS:
        if act in group or act.rstrip("s") in group:
            for word in group:
                out |= _inflections(word)
    return out


def requested_by(request, operation, role, label):
    """Whether this action is one the request's own words ask for (never for a look-only request)."""
    if not asks_for_changes(request):
        return False
    asked = asked_part(request).casefold()
    words = set(re.findall(r"[a-z]+", asked))
    if operation in {"TYPE", "TYPE_SUBMIT", "SUBMIT"} and role == "SearchField":
        return True
    if operation in {"TYPE", "TYPE_SUBMIT"}:
        return bool(_TEXT_ASKED.search(asked))
    if operation not in {"TAP", "SUBMIT"} or role not in NAVIGATION_TAP_ROLES:
        return False
    acts = {m.group(0).casefold() for m in re.finditer(r"[A-Za-z]+", label or "")
            if STATE_CHANGING_LABEL.fullmatch(m.group(0)) or SIDE_EFFECT_CONTROL.fullmatch(m.group(0))
            or COMMIT_CONTROL.fullmatch(m.group(0)) or CHANGE_REQUEST.fullmatch(m.group(0))}
    if not acts:
        return True
    return all((_inflections(act) | synonyms(act)) & words for act in acts)
