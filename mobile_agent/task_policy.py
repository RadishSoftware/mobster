"""Typed user-policy questions; inferred output never grants action authority."""

from dataclasses import replace
from enum import StrEnum
import re


class RiskTier(StrEnum):
    """Named dispatch risk for confidence-gated actions."""
    SCROLL = "scroll"
    NAVIGATION = "navigation"
    TYPE = "type"
    SIDE_EFFECT = "side_effect"


# Calibrated claim acceptance (evals/verifier_calibration.py, 24 Sep): 80 answers
# replayed from MobsterBench evidence, 23 right and 57 decoys (other literals of the same
# shape on the same screens). claim_field separated them with AUROC 0.98: every decoy
# scored <= 0.34; 21 of 23 right answers scored >= 0.6. The overall verdict rejected
# right answers it could not tie to the final screen. Accepted this way are logged as
# "claims_calibrated" and re-audited by the same script on every new run.
CALIBRATED_CLAIM_FLOORS = {"claim_field": .6, "claim_entity": .7}
CALIBRATED_MAX_UNCLEAR = .2


def claims_calibrated(signals):
    """The per-claim scores alone accept the answer (see CALIBRATED_CLAIM_FLOORS)."""
    return (all(isinstance(signals.get(name), float) and signals[name] >= floor
                for name, floor in CALIBRATED_CLAIM_FLOORS.items())
            and isinstance(signals.get("p_unclear"), float) and signals["p_unclear"] < CALIBRATED_MAX_UNCLEAR)


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


# Roles whose tap only moves between screens. Switches, sliders, pickers,
# steppers, segmented controls and text inputs change state and are excluded.
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


# Keys of an on-screen keypad (Calculator, a PIN or dial pad): each press is input, like a
# typed character, not an effect to verify or de-duplicate ("5 × 5" presses 5 twice; the
# duplicate-effect guard stopped text.calc_multiply). Commit keys ("Call") are not here.
KEYPAD_KEY = re.compile(r"^(\d|[+\-×x*÷/=.%,]|plus|minus|add|subtract|multiply|times|divide|equals|decimal|point|"
                        r"percent|clear|all clear|delete|backspace|negate|change sign|"
                        r"zero|one|two|three|four|five|six|seven|eight|nine)$", re.I)


def is_keypad_key(operation, role, label):
    return operation == "TAP" and role in ("Key", "Button") and bool(KEYPAD_KEY.match((label or "").strip()))


# The lock screen's passcode keypad: SpringBoard's own digit keys ("1", "2, A B C" ...). The model never acts on
# it: Mobster handles the lock screen itself (lockscreen.py), and every model tap there is refused (B2: a run
# tapped around the passcode screen, and the lock screen's Now Playing card).
SPRINGBOARD_BUNDLE = "com.apple.springboard"
_DIGIT_KEY = re.compile(r"^\s*(\d)(?!\d)")
_PASSCODE_MARKER = re.compile(r"\b(enter passcode|passcode|emergency|touch id|face id)\b", re.I)


def springboard_keypad(snapshot):
    """True when ``snapshot`` is SpringBoard showing a digit keypad: at least six distinct digit keys, with a
    passcode marker ("Enter Passcode", "Emergency") or all ten digits. Errs toward True: it only refuses."""
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


# Controls whose tap commits something the user would want to see first:
# money, messages, publishing and deletion. Narrower than SIDE_EFFECT_CONTROL,
# which also covers routine commits like "Done" and "Save" in Settings.
COMMIT_CONTROL = re.compile(
    r"\b(send|post|publish|reply|comment|share|tweet|buy|purchase|order|pay|checkout|place order|"
    r"transfer|donate|tip|subscribe|book|reserve|delete|erase|remove|reset|install|upload|redeem|"
    r"call|dial|facetime|follow|sign[ -]?up|submit|confirm)\b",
    re.I)


APPROVAL_OPERATIONS = frozenset({"TAP", "SUBMIT", "TYPE_SUBMIT"})

# Controls that put something out in the world beyond COMMIT_CONTROL's words, for "ask before acting"
# only (COMMIT_CONTROL also steers navigation and the action check, so it stays as it is): a ride
# request, the trash, clearing a list, answering an invite, joining or leaving a group, blocking,
# reporting, unsubscribing, an App Store Get, a rental, a mail archive. Each exclusion is a control
# of the same word that only opens, reads or edits ("Request Desktop Website", "Clear text",
# "Get Directions"). The PM probe of 26 Sep found every one of these went through unasked.
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


# Roles whose label is what they show, not what a tap on them does: a list row (the reminder "Call the dentist",
# a message preview), text, a field, a picture. QA on f27d229: tapping the reminder "Call the dentist, Incomplete"
# asked "Place this call?". Only a tap reads a label this way: Return in a field does what the field's label says
# ("Reply to Sam" sends the reply), so a submit never passes the role.
CONTENT_ROLES = frozenset({"Cell", "StaticText", "TextField", "TextView", "SecureTextField", "SearchField", "Image",
                           "Icon"})
# A tap on a field only focuses it, whatever its label ("Order more coffee filters", "Comment").
EDITABLE_ROLES = frozenset({"TextField", "TextView", "SecureTextField", "SearchField"})
# The state iOS appends to a reminder's row ("Buy oat milk, Incomplete"): that label is always content.
ROW_STATE = re.compile(r",\s*(?:Incomplete|Completed)$", re.I)
# Acts that name a person or a chore in a row's words as often as they name a control ("Call the dentist",
# "Follow up with Dana", "Reply to Sam's note"): in content they count only in a label of at most CONTROL_WORDS
# words ("Call", "Call Sam", "Reply All"). Every other act counts in content at any length: money, messages,
# posts and deletions ("Place your order", "Confirm and pay", "Send My Current Location", "Transfer to Checking",
# "Block this Caller", "Erase All Content and Settings"). Review 2 of #41 found those four money and message rows
# going through unasked when only deletions counted at any length.
TALK_ACTS = frozenset({"call", "dial", "facetime", "reply", "comment", "follow"})
CONTROL_WORDS = 2
# The checkout button's own words, which COMMIT_CONTROL's "place order" does not read.
PLACE_ORDER = re.compile(r"^place\s+(?:(?:your|the|this|my)\s+)?order\b", re.I)


def commit_label(label, role=None):
    """The commit a control's own label names, as it reads in the label, or None.

    A control (a Button, a Link, any role but CONTENT_ROLES; None counts as one) names it anywhere in its label
    ("Pay with Apple Pay", "Place Order · $42.00"). Content counts only when its label reads like a control's:
    the commit word first and no comma (iOS joins a row's parts with commas), and for TALK_ACTS at most
    CONTROL_WORDS words ("Call Sam", not "Call the dentist"). A field (EDITABLE_ROLES) never names one: a tap
    there only focuses it. A reminder's row ("Call the dentist, Incomplete") never names one, whatever its role.
    A spurious ask on a plain row ("Pay the rent on Friday") is the safe failure; a missed one is not."""
    label = " ".join((label or "").split())
    if ROW_STATE.search(label):
        return None
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


def _same_words(a, b):
    return " ".join((a or "").split()).casefold() == " ".join((b or "").split()).casefold()


def approval_subject(elements, target):
    """The element whose label and role "ask before acting" reads for a tap on ``target``.

    A tap lands on the control that holds what it hits. WebKit exposes a checkout <button> as a Button with a
    StaticText of the same label inside, both tap targets, and Fast may pick the text: review 2 of #41 saw
    "Place your order" go through unasked that way. So content (CONTENT_ROLES) under a control reads as that
    control: of the controls whose frame holds the tap point, the one with the same label, else the innermost.
    A control that names no commit leaves the content its own reading, so a commit is never read away. Content
    with no control around it, inside a row whose label reads as a row (a comma, a reminder's state), reads as
    that row: the title "Pay the rent" inside "Pay the rent, Incomplete" is the reminder, not a payment."""
    if target is None or target.role not in CONTENT_ROLES or target.role in EDITABLE_ROLES:
        return target
    point = target.tap_point
    around = [e for e in elements if e is not target and e.id != target.id and _holds(e, point)]
    area = lambda e: e.rect[2] * e.rect[3]
    controls = sorted((e for e in around if e.role not in CONTENT_ROLES and e.role not in CONTAINER_ROLES),
                      key=lambda e: (not _same_words(e.label, target.label), area(e)))
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
    """What "ask before acting" asks about this action in the Fast engine: "commit", "unsure" or None.

    "commit": the action puts something out in the world. Only a tap, a submit or typing followed
    by Return can, and only when the control's own label names a commit (Send, Buy, Post, Delete,
    Request, Trash, Clear All, Accept, Unsubscribe, Get...: COMMIT_CONTROL and APPROVAL_COMMIT; for a
    tap on a row or text of ``role``, only as ``commit_label`` says) or it is a submit in a task whose
    wording is a side effect (a message, a post). A submit reads the field's label whatever its role:
    Return in "Reply to Sam" sends the reply.
    "unsure": the model or its action check could not establish this step (``uncertain``), so the
    user decides whether it continues; it never reads as a commit.

    A high side-effect score alone does not ask: the audit of 26 Sep saw "Tap New Reminder"
    (which opens a form and commits nothing) put to the user while the step that saved the
    reminder went through. Such a tap still passes the action check, which is the safety net.
    Scrolling, navigation and typing without Return never ask as a commit.
    """
    if operation in APPROVAL_OPERATIONS and (
            commit_label(label, label_role(operation, role))
            or operation in {"SUBMIT", "TYPE_SUBMIT"} and risk_tier == RiskTier.SIDE_EFFECT.value):
        return "commit"
    return "unsure" if uncertain else None


def needs_approval(operation, label, *, risk_tier, side_effect_risk=None, role=None):
    """Whether "ask before acting" pauses this action as a commit (see ``approval_kind``).

    ``side_effect_risk`` is accepted for callers that pass it and no longer decides anything.
    """
    return approval_kind(operation, label, risk_tier=risk_tier, role=role) == "commit"


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
    "react": "Send this reaction?",
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
    if act.startswith("cancel ") or act.startswith("end "):
        return "end call" if act == "end call" else act
    return APPROVAL_ACT_ALIASES.get(act, act)


def commit_act(label, request="", role=None, operation="TAP"):
    """The commit a control performs, as one lowercase verb ("send", "place order"), or "submit".

    The request is read only for COMMIT_CONTROL's words: "Get the iOS version" asks for no App Store Get.
    ``role`` as in ``commit_label``, for a tap only (``label_role``): a row's words name no act.
    """
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


# --- Compiled loops (loops.py) -------------------------------------------------


# -- actions the request itself asks for ---------------------------------------------------
#
# The action verifier is conservative: UNCLEAR ends the run. That is right for a request that
# asks only to look (MobsterBench's questions), and wrong for one that asks to change things:
# on iOSWorld (24 Sep smoke) "archive a QuickBite receipt" and "Send $32.50 to Maya Patel"
# ended at the verifier's UNCLEAR on the very controls named in the request. An UNCLEAR is no
# evidence of a wrong effect; the request's own words are evidence of the right one.

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


def prohibitions(request):
    """The request's prohibition sentences, verbatim, for copying into each milestone."""
    return " ".join(match.group(0).strip() for match in PROHIBITION_SENTENCE.finditer(request or ""))


def asks_for_changes(request):
    return bool(CHANGE_REQUEST.search(asked_part(request)))


def _inflections(word):
    word = word.casefold()
    stem = word[:-1] if word.endswith("e") else word
    return {word, word + "s", word + "d", word + "ed", stem + "ing", stem + "ed", word + "ment", stem + "ion"}


# A control that names no act counts as navigation only below this side-effect score.
REQUESTED_NAVIGATION_MAX_RISK = .7


# Acts that are the same request in other words: "order a cake" is completed by "Go to
# checkout" (iOSWorld multi-006, 24 Sep: refused three times as an unrequested act).
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


def requested_by(request, operation, role, label, side_effect_risk=None):
    """Whether this action is one the request's own words ask for (never for a look-only request).

    Text into a search field is a search. Text elsewhere needs the request to ask for
    writing ("message", "add a note"). A tap on a control whose label names an act
    ("Archive", "Send", "Pay") needs that act in the request; a tap on a control that names
    no act is navigation.
    """
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
        return side_effect_risk is not None and side_effect_risk < REQUESTED_NAVIGATION_MAX_RISK
    return all((_inflections(act) | synonyms(act)) & words for act in acts)
