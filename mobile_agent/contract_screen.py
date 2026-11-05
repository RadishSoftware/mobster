"""What the task contract reads off a screen: the words that name an act, a screen's title and recipient, receipts,
amounts, and the contract's items (Item, Gate, Control, Pending).

Contract (contract.py) proves the items; contract_asks.py decides which commits ask the person. Elements are duck-typed
(label, role, rect as screen fractions, editable, value): state.Element in the agent, recorded rows in the offline
replay.
"""

import re
from dataclasses import dataclass, field

from .task_policy import COMMIT_CONTROL_WORDS, GUARD_WORDS, UNSEND

KINDS = ("READ", "WRITE", "COMMIT", "REPORT", "FORBID")
# Long reads whose notebook proves their summary item (Contract.notebook): a chain's REPORT items keep screen proof.
NOTEBOOK_KINDS = ("feed", "survey", "browse")
SUMMARY_ITEM = re.compile(r"\b(learn(?:ed|t|ing)?|found|summar(?:y|ies|i[sz]e)|shortlist|most promising|"
                          r"takeaways?|insights?|what (?:you|it) (?:saw|found|learned))\b", re.I)
MESSAGE = frozenset({"send", "post", "publish", "reply", "comment", "tweet", "submit"})
# Act -> the commit verbs a control may carry to perform it.
FAMILIES = {
    "send_message": MESSAGE, "post": MESSAGE, "comment": MESSAGE, "reply": MESSAGE,
    "react": frozenset({"like", "react"}),
    "pay": frozenset({"pay", "send", "transfer", "confirm", "submit"}),
    "request_money": frozenset({"request", "send", "confirm", "submit"}),
    "transfer": frozenset({"transfer", "send", "confirm", "submit"}),
    "order": frozenset({"order", "place order", "buy", "purchase", "checkout", "pay", "confirm", "submit", "tip"}),
    # "Request CityRideX · $8.00" books a ride (cityride-001, required twice in the F1 runs).
    "book": frozenset({"book", "reserve", "confirm", "submit", "request"}),
    "follow": frozenset({"follow", "subscribe", "sign up"}),
    "unfollow": frozenset({"unfollow", "unsubscribe"}),
    "share": frozenset({"share", "send"}),
    "call": frozenset({"call", "dial", "facetime"}),
    "delete": frozenset({"delete", "erase", "remove"}),
    "save": frozenset({"save"}),
    "archive": frozenset({"archive"}),
    "cancel": frozenset({"cancel", "confirm", "submit"}),
    # An app from the App Store (C3): its Get, Install, price or Redownload button, only in the App Store.
    "install": frozenset({"get", "install", "buy", "purchase", "redownload"}),
    "other": frozenset(), "none": frozenset()}
ACTS = tuple(FAMILIES)
MESSAGE_ACTS = frozenset({"send_message", "post", "comment", "reply"})
MONEY_ACTS = frozenset({"pay", "transfer", "request_money", "order"})
# Commits bound to a named object: its name must be on screen (feed Follow buttons of other people).
OBJECT_ACTS = frozenset({"follow", "unfollow", "share", "delete", "save", "archive", "react", "call", "cancel",
                         "install"})
# A FORBID that rules out only the acts the request doesn't name ("Delete these three notes. Delete nothing else."):
# its words exclude others, so a declared COMMIT of the same act still goes to its own checks and the approval card.
# Any other FORBID ("do not delete anything") refuses every act of its family, a declared COMMIT or not.
OTHERS = re.compile(r"\b(?:else|other|others|besides|except|apart from|aside from|but (?:the|these|those|that|this))\b",
                    re.I)
# Acts that turn a control on (a like, a follow). Only that control turning on after the agent's own tap proves one:
# selected, its words for "on", or its count up by one. MobsterBench Real (11 Oct): "Like the first post" ended done
# after a tap on a point and a banner closing; any change on screen, or the post still being there, proved it.
STATE_ACTS = frozenset({"react", "follow"})
# A control's words once its act is on ("Liked", "Unlike"; "Following", "Requested"), in its label or value.
STATE_ON = {"react": re.compile(r"\b(liked|unlike|reacted|remove (?:like|reaction)|undo like)\b", re.I),
            "follow": re.compile(r"^(following|followed|unfollow|requested|subscribed)\b", re.I)}
OFF_VALUES, ON_VALUES = frozenset({"0", "off", "false"}), frozenset({"1", "on", "true"})
TOGGLE_ROLES = frozenset({"Button", "Toggle", "Switch"})
# A count on or beside a control: "7", "1,204", "8 likes"; a rounded one ("1.2K") never moves by one.
COUNT = re.compile(r"^(\d{1,3}(?:,\d{3})+|\d+)(?:\s+(?:likes?|reactions?|hearts?|followers?))?$", re.I)
APP_STORE = "com.apple.AppStore"
# The App Store's commit buttons, read only there and only as a label's first word ("Get", "Redownload"), or a
# price ("$4.99"): "Get Directions" elsewhere is not a purchase.
STORE_VERBS = {"get": "get", "install": "install", "buy": "buy", "purchase": "purchase", "redownload": "redownload",
               "re-download": "redownload", "download": "redownload"}
PRICE = re.compile(r"^\s*(?:buy,?\s*)?\$\s?\d")
# A message's delivery mark under its bubble (O2): "Delivered", "Read 10:42 AM", "Sent as Text Message".
DELIVERY = re.compile(r"^(delivered|read|sent)\b")
# Screen titles that name a list or a blank composer, not the person a thread is with.
# A mail sheet's title is its subject ("Re: Dinner plans"), never the person it goes to.
SUBJECT = re.compile(r"^\s*(re|fwd?|aw|sv|tr)\s*:", re.I)
MAIL_APPS = re.compile(r"\b(mail|gmail|outlook|spark|superhuman|proton ?mail|hey)\b")
GENERIC_TITLES = frozenset({"messages", "new message", "imessage", "chats", "inbox", "conversations", "details", "info",
                            "edit", "mail", "whatsapp", "signal"})
# The last word of a view's or a field's developer name ("ChatView", "chat_compose_field"): words of a UI's
# structure, which no person's or place's name ends with.
UI_WORDS = frozenset({"view", "views", "controller", "vc", "bar", "navbar", "navigationbar", "toolbar", "tabbar",
                      "field", "textfield", "textview", "button", "btn", "cell", "label", "header", "footer", "screen",
                      "container", "sheet", "modal", "input", "window", "panel", "identifier", "nav"})
# The first part of a bundle identifier ("com.apple.MobileSMS").
BUNDLE_ROOTS = frozenset({"com", "org", "net", "io", "app", "co", "dev", "me", "ai", "us", "uk", "de", "fr", "jp"})
# A To field's token that is not a party yet: Messages' "No Name, Searching" while it looks the name up.
UNRESOLVED = re.compile(r"\b(?:no name|searching|loading)\b", re.I)
# Controls that sit in a To field's row and name no one.
TO_ROW_CONTROLS = frozenset({"add", "add contact", "contact add", "add recipient", "add recipients", "to", "to:",
                             "cc/bcc, from:", "cc", "bcc", "clear", "remove", "more", "info"})

# The taint check (Contract.screen_text_in): a run of this many characters of a message that a screen showed.
TAINT_CHARS = 16
# A row's state iOS appends ("Buy oat milk, Incomplete") and a row's parts joined by commas: content, not a control.
ROW_ROLES_CONTENT = frozenset({"Cell", "StaticText"})
# The words F1 found ungated (request, cancel, unfollow, archive, save, like, react, unsubscribe). They are common
# in prose ("Feels Like", "Save on rides", "Your requests"), so they count only as a label's first word, where a
# control names its act.
FIRST_WORD_ONLY = frozenset({"request", "cancel", "unfollow", "unsubscribe", "archive", "save", "like", "react"})
# The verbs of task_policy.COMMIT_CONTROL plus FIRST_WORD_ONLY.
COMMIT_WORDS = re.compile(r"\b(" + "|".join((*COMMIT_CONTROL_WORDS, *sorted(FIRST_WORD_ONLY))) + r")\b", re.I)
# Controls that only open a sheet, a thread or a comment list (whose committing control is gated in
# turn), and the bare Cancel that closes one.
OPENERS = frozenset({"share", "share…", "share...", "comment", "comments", "comment left", "reply",
                     "reply in thread", "cancel"})
# SF Symbol names used as labels ("pencil.tip" is a drawing tool, not a tip).
SF_SYMBOL = re.compile(r"[a-z0-9]+(?:\.[a-z0-9]+)+")
# A delete no delete word heads: the trash glyph an unlabelled swipe action reads as (its SF Symbol name, "trash",
# "trash.fill", "xmark.bin"), "Move to Trash", and (deletes, with its frame) a narrow button named Trash or Bin.
DELETE_GLYPH = re.compile(r"(?:trash|xmark\.bin|bin\.xmark)(?:\.[a-z0-9]+)*")
MOVE_TO_TRASH = re.compile(r"\s*(?:move|send) (?:it |them |this |these )?to (?:the )?(?:trash|bin)\b", re.I)
TRASH_WORD = re.compile(r"(?:trash|bin)", re.I)
STREET = re.compile(r"\s+(st|street|ave|avenue|rd|road|blvd|way|dr|drive|ln|lane|ct|pl|plaza|sq)\b\.?", re.I)
SWITCH_ROLES = frozenset({"Switch", "Toggle"})

MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)")
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
BOUND = re.compile(r"(under|below|less than|at most|up to|no more than|over|above|more than|at least)\s+"
                   r"\$\s?(\d[\d,]*(?:\.\d+)?)")
SEARCHY = re.compile(r"search|find|query|filter|destination|where to|recipient|phone, name|@username|"
                     r"\bto:|location|address|\bcity\b|zip", re.I)
COMPOSER = re.compile(r"compose|message|chat|comment|reply|write|tweet|caption|say something|on your mind|"
                      r"imessage|\bpost\b", re.I)
# What a new row must say to receipt a commit, by act (rows already on screen before the commit do not count:
# a checkout page's "saved card" once closed an order that was never placed).
RECEIPTS = {
    "order": re.compile(r"\border (placed|confirmed|received|is on|submitted)|thank you for your order|\border #|"
                        r"order number|on (its|the) way|being prepared|estimated (delivery|arrival)|\barriving\b",
                        re.I),
    "book": re.compile(r"\b(confirmed|confirmation|reserved|booked|you.re all set|ride requested|finding (you a|your) "
                       r"driver|driver (is|on)|arriving|on the way)\b|\bRSV[- ]?\w+|\b[A-Z]{2,4}-?\d{4,}\b", re.I),
    "pay": re.compile(r"\b(paid|payment (sent|complete|successful)|you paid|money sent|sent \$|completed)\b", re.I),
    "transfer": re.compile(r"\b(transfer(red)? (complete|sent|scheduled|successful)|transferred|completed|sent \$)\b",
                           re.I),
    "request_money": re.compile(r"\b(requested|request sent|you requested|request (is )?pending)\b", re.I),
    "follow": re.compile(r"^(following|unfollow|followed|subscribed)\b", re.I),
    "unfollow": re.compile(r"^(follow|unfollowed|subscribe)\b", re.I),
    # An install has begun or finished: the button turned to its progress ring or to Open. Apple's own sheet
    # ("Double Click to Install", Face ID, the Apple ID password) changes the screen too, and is not one: the
    # app is not installed until the user confirms there.
    "install": re.compile(r"^(open|downloading|installing|waiting|redownloading|stop)\b|\b(stop|pause|cancel) "
                          r"download", re.I),
}
# Verbs that end many acts ("Confirm", "Submit", "Send"): a control that names only these says nothing about which
# act it performs, so it adds no other act's receipts.
SHARED_VERBS = frozenset({"confirm", "submit", "send"})
NO_RESULTS = re.compile(r"\bno (results|matches|items|messages|emails|mail|orders|transactions|events|"
                        r"trips|notes|documents|files|conversations|contacts)\b|nothing found|0 results|"
                        r"couldn.t find|did not match|didn.t match|no .{1,30} found", re.I)
# A screen that says it is empty: an absence answer ("No events tomorrow") is proven by it as by an empty search
# (B6: cal-tomorrow searched Calendar for a date to satisfy the gate, and still ended "Check the answer").
EMPTY_STATE = re.compile(r"^(no (upcoming )?(events|reminders|alarms|appointments|notifications|photos|videos|"
                         r"recents|favorites|bookmarks|downloads|rides|bookings|reservations|tasks|items|results|"
                         r"messages|emails|mail|orders|transactions|trips|notes|documents|files|contacts)"
                         r"( (today|tomorrow|this week|scheduled|found|yet))?|nothing (scheduled|planned|here)"
                         r"( (today|tomorrow))?|you have no \w+|all done|inbox zero)\b", re.I)
NONE_VALUE = re.compile(r"^(none|no\b|nothing|not found|zero\b|0 results|no one|nobody|there (are|were) no)", re.I)
CONFIRM_ITEM = re.compile(r"\bconfirm|confirmation|\b(was|were|been|is|are) (added|created|sent|saved|updated|"
                          r"posted|booked|made|completed|followed|placed|paid|requested|set|done)\b", re.I)
# A detail the agent works out rather than reads (its numbers are not on any one screen).
COMPUTED_ITEM = re.compile(r"\b(calculat\w*|total\w*|tally|sum|net|average|avg|project\w*|estimat\w*|how many|"
                           r"count|difference|surplus|deficit|budget|compar\w*|rate|percent\w*|per|breakdown|"
                           r"summar\w*|categori[sz]\w*|pattern|trend|frequency|typical|recommend\w*|vs|versus|"
                           r"progress|remaining|left|weekly|monthly|annual\w*|habits?|routine|analy[sz]\w*|"
                           r"plan|schedule|itinerary|preferences?|insights?|takeaways?|should|best|cheapest|"
                           r"closest|most|least)\b", re.I)
ATOM = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?|\d+(?:\.\d+)?%|\b[A-Z]{2,}-?\d{3,}\b|\b\d{1,2}:\d{2}(?::\d{2})?\b"
                  r"|\b\d[\d,]*(?:\.\d+)?\b")
FOLDERS = ("sent", "archive", "drafts", "trash", "junk", "inbox", "outbox")
NAME_STOP = frozenset({"pay", "paid", "request", "requested", "confirm", "cancel", "payment", "send", "you", "your",
                       "the", "a", "an", "to", "from", "for", "using", "balance", "decline", "done", "and", "of"})
# Capitalized words that name no particular object ("New post", "Reply to Rachel").
GENERIC = frozenset({"the", "a", "an", "my", "i", "new", "post", "job", "company", "reply", "comment", "message",
                     "request", "requests", "payment", "order", "reservation", "ride", "note", "document"})
CONFIRM_WORDS = frozenset({"confirm", "confirmation", "confirmed", "that", "were", "have", "been", "added", "created",
                           "sent", "updated", "completed", "done", "made", "with", "both", "all", "successfully"})
# A WRITE that adds (a log entry, an item to a list or a cart), and the words of its target that name no object.
ADDS = re.compile(r"\b(log(?:s|ged|ging)?|add(?:s|ed|ing)?|creat(?:e|es|ed)|new|insert|append|record)\b", re.I)
OBJECT_STOP = frozenset("""log logs logged new add set create entry entries item items list lists note notes post
posts message messages body text section cell value values document documents file files added adding update
updated created reminder reminders alarm alarms timer timers event events page today today's tomorrow morning
evening night with from into onto about this that your their summary details detail info information""".split())
LOW_TURNS = 3
# Reopens an item may cost before it is marked unproven: one (B6: the gate twice sent cal-tomorrow, weather and
# appstore-duolingo back for proof of a "no" that no screen could quote).
MAX_REOPENS = 1
MAX_ITEMS = 24


def plain(text):
    """``text`` for containment checks: case, quote style and spacing ignored."""
    return " ".join(str(text or "").casefold().replace("’", "'").replace("‘", "'").replace("“", '"')
                    .replace("”", '"').replace(" ", " ").split())


def as_float(number):
    try:
        return float(number.replace(",", ""))
    except ValueError:
        return None


# ---------------------------------------------------------------- commit verbs

def label_head(label):
    """The first GUARD_WORDS words of ``label``; an identifier label split into words
    ("mybank.checkout.confirmToggle" -> "mybank checkout confirm Toggle"); "" for an SF Symbol name."""
    text = " ".join((label or "").split()[:GUARD_WORDS])
    if SF_SYMBOL.fullmatch(text):
        return ""
    if " " not in text and re.search(r"[a-z][A-Z]|\.", text):
        text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text).replace(".", " ")
    return text


def quoted(text):
    """Names in quotes ('Budget Tracker', "Gym"); an apostrophe inside a word ("Anand's") opens none."""
    return [a or b for a, b in re.findall(r"(?<![\w])'([^']{2,}?)'(?![\w])|\"([^\"]{2,}?)\"", text or "")]


# A control that acts on several things at once: "Delete All", "Delete 3 Notes", "Remove (2)".
MORE_THAN_ONE = re.compile(r"\b(?:all|everything|every)\b|\(\s*[2-9]\d*\s*\)|\b(?:[2-9]|\d{2,}) "
                           r"(?:items|notes|messages|photos|videos|files|emails|conversations|contacts|reminders|"
                           r"events)\b", re.I)


def row_of(element, elements):
    """The list row ``element`` belongs to: the smallest Cell holding its centre, or (a swipe action revealed beside
    it) a Cell on the same line just left of it. None for a control outside any row (a toolbar's Delete)."""
    if element is None or getattr(element, "role", None) == "Cell":
        return None
    x, y, w, h = element.rect
    cx, cy = x + w / 2, y + h / 2
    holders = [e for e in elements if e is not element and e.role == "Cell"
               and e.rect[0] <= cx <= e.rect[0] + e.rect[2] and e.rect[1] <= cy <= e.rect[1] + e.rect[3]]
    if holders:
        return min(holders, key=lambda e: e.rect[2] * e.rect[3])
    beside = [e for e in elements if e is not element and e.role == "Cell" and abs(center_y(e) - cy) <= e.rect[3] / 2
              and e.rect[0] + e.rect[2] <= x + .02 and e.rect[3] <= .2]
    return max(beside, key=lambda e: e.rect[0] + e.rect[2]) if beside else None


# A reaction control's label, which a feed repeats on every post's action line ("Like", "Liked", "React").
REACTION_CONTROL = re.compile(r"(?:like|liked|unlike|react|reacted|reaction)\b")
# How far a feed's action lines may sit from one column (screen fractions), and how far below its line a post starts.
ACTION_COLUMN, ACTION_LINE = .03, .02


def card_of(element, elements):
    """The post a reaction control closes, on a feed whose posts are not Cells: the elements from just below the
    previous reaction control in its column (the action line of the post above) down to this control's line, or from
    the screen's top. iOSWorld multi-092 (18 Oct, LockedIn): the Like at y .84 closes Devi Anand's card, from her name
    at .42; the Like at .35 closes Rohan's post above it. A reactions summary's "Like" icon is a picture, not a post's
    action line (the same run, on the post's own page)."""
    x, middle = element.rect[0], center_y(element)
    above = [center_y(other) for other in elements if other is not element and other.role != "Image"
             and abs(other.rect[0] - x) <= ACTION_COLUMN and center_y(other) < middle - ACTION_LINE
             and REACTION_CONTROL.match(plain(other.label))]
    top = max(above) + ACTION_LINE if above else -1.0
    return [other for other in elements if top < center_y(other) <= middle + ACTION_LINE / 4]


def is_opener(label):
    return " ".join((label or "").split()).casefold() in OPENERS


def deletes(label, role=None, rect=None):
    """Whether a control deletes though no delete word heads its label: the trash glyph a swipe action reads as when
    it has no label of its own ("trash", "trash.fill"), "Move to Trash", or, given its frame, a narrow button named
    Trash or Bin (a row's swipe action). A cell, a text or a full-width row named Trash is the folder, not a delete.
    MobsterBench Real, 12 Oct: a Clock timer's swipe action "trash" went through Smart's commit check; a "Delete" one
    never did."""
    text = " ".join(str(label or "").split())
    if DELETE_GLYPH.fullmatch(text) or MOVE_TO_TRASH.match(text):
        return True
    return bool(rect and TRASH_WORD.fullmatch(text) and role not in ("Cell", "StaticText", "Link", "Image")
                and rect[2] <= .35)


def label_verbs(label, role="Button"):
    """The commit verbs a control's label names (set, empty when it names none)."""
    if (label or "").strip().casefold() == "following":
        return {"unfollow"}  # a Follow button after the tap: tapping it again unfollows
    if deletes(label):
        return {"delete"}  # the trash glyph, or Move to Trash
    if UNSEND.search(label or ""):
        return {"delete"}
    if role in ROW_ROLES_CONTENT and ("," in (label or "") or len((label or "").split()) > 6):
        return set()  # a row's words say what it shows ("Like a Prayer, Madonna", "Call the dentist, Incomplete")
    head = label_head(label)
    if not head or is_opener(label):
        return set()
    out = set()
    for match in COMMIT_WORDS.finditer(head):
        verb = " ".join(match.group(0).casefold().split())
        verb = "sign up" if verb.replace("-", "").replace(" ", "") == "signup" else verb
        if STREET.match(head, match.end()):
            continue  # "490 Post St": an address
        at_start = head[:match.start()].strip(" \t,.:;!-•·") == ""
        if verb in FIRST_WORD_ONLY and not at_start:
            continue
        if role in SWITCH_ROLES and not at_start and " " in (label or "").strip():
            continue  # a switch's label names its setting: the alarm "9:30, Client call, Weekdays"
        out.add(verb)
    return out


def internal_name(text):
    """Whether ``text`` is a developer's name for a view, not words the screen shows a person: a class name
    ("CKComposeChat", "UINavigationBar", "ComposeChatViewController"), an accessibility identifier
    ("composeTextField", "chat_compose_field") or a bundle-like string ("com.apple.MobileSMS"). XCUITest reports a
    view's identifier as its label when it has none: a Messages compose sheet's bar read "CKComposeChat", and the
    approval card named it as who the message went to (MobsterBench Real, 11 Oct). Never one: text with a space,
    a phone number, an email address, a handle ("sam_rivera", "jo.bloggs") or a one-hump name ("McDonald",
    "iPhone")."""
    text = " ".join(str(text or "").split())
    if not text or not re.fullmatch(r"[A-Za-z_][\w.-]*", text):
        return False
    if text.startswith("_"):
        return True  # a Swift-mangled or private name ("_TtC4Chat8ChatView")
    parts = [p for p in re.split(r"[._-]", text) if p]
    camel = any(re.search(r"[a-z][A-Z]", p) for p in parts)
    if len(parts) > 1:
        # Dotted, snake or kebab: a bundle ("com.apple.MobileSMS"), a camel-cased part, or a UI word last.
        return (camel or "." in text and parts[0].casefold() in BUNDLE_ROOTS and len(parts) >= 3
                or parts[-1].casefold() in UI_WORDS)
    humps = re.findall(r"[A-Z][a-z0-9]+", text)
    last = humps[-1].casefold() if humps else ""
    prefix = re.match(r"[A-Z]{2,}(?=[A-Z][a-z])", text)
    if prefix:
        # An Objective-C class prefix ("CK", "UI", "PU") before two words or a UI word ("CKComposeChat").
        return len(re.findall(r"[A-Z][a-z0-9]+", text[prefix.end():])) >= 2 or last in UI_WORDS
    if re.fullmatch(r"[a-z]+(?:[A-Z][a-z0-9]*)+", text):
        return len(humps) >= 2 or last in UI_WORDS  # lowerCamel: "composeTextField", "chatView"; not "iPhone"
    return len(humps) >= 3 or len(humps) == 2 and last in UI_WORDS  # "ComposeChatView", "ChatView"


def _bar_text(bar, elements):
    """The centred text of a navigation bar's top row (its title when the bar is named by a class), or None."""
    x, y, w, h = bar.rect
    inside = [e for e in elements if e.role in ("StaticText", "Button") and (e.label or "").strip()
              and y <= e.rect[1] + e.rect[3] / 2 <= y + min(h, .07) and x + .2 * w <= e.rect[0] + e.rect[2] / 2
              <= x + .8 * w]
    if not inside:
        return None
    centred = min(inside, key=lambda e: abs(e.rect[0] + e.rect[2] / 2 - (x + w / 2)))
    return re.sub(r",\s*Actions Menu$", "", " ".join(centred.label.split()), flags=re.I) or None


def screen_title(snapshot):
    """The screen's title as shown (its navigation bar's label), or None: a bar named by a class name
    ("FullDocumentManagerViewControllerNavigationBar") has the title inside it instead."""
    elements = list(getattr(snapshot, "elements", ()) or ())
    bar = next((e for e in elements if e.role == "NavigationBar" and (e.label or "").strip()), None)
    if bar is None:
        return None
    label = " ".join(bar.label.split())
    if not re.fullmatch(r"[A-Za-z_][\w.]*", label) or len(label) <= 20:
        return re.sub(r",\s*Actions Menu$", "", label, flags=re.I) or None
    return _bar_text(bar, elements)


def thread_title(snapshot, app=None):
    """The person or place a message screen is with, as its title shows it ("+1 (555) 564-8583"), or None for a
    list's or a blank composer's title, and for mail: a reply's sheet is titled with its subject, and "Send this
    reply to Re: Dinner plans?" names no one. A bar named by a class ("CKComposeChat") names no one either: its
    centred text stands for it ("New Message", a blank composer's)."""
    title = screen_title(snapshot)
    if title and internal_name(title):
        bar = next((e for e in getattr(snapshot, "elements", ()) or () if e.role == "NavigationBar"
                    and (e.label or "").strip()), None)
        title = _bar_text(bar, list(snapshot.elements)) if bar is not None else None
    if not title or internal_name(title):
        return None
    folded = plain(title)
    if folded in GENERIC_TITLES or app and folded == plain(app):
        return None
    if SUBJECT.match(title) or app and MAIL_APPS.search(plain(app)):
        return None
    return title


def addressed_to(snapshot):
    """Who a composer's To field addresses, as its tokens show them ("Jordan Lee"; "Sam Rivera, Alex Kim"), or None
    when it holds none. Messages reads the field as "To:\\ufffcJordan Lee" (each token an attachment mark) and draws
    each token as a button on the field's row ("Jordan Lee, +15550100000"); a token still being looked up ("No Name,
    Searching") names no one yet."""
    elements = list(getattr(snapshot, "elements", ()) or ())
    for to_field in elements:
        if not getattr(to_field, "editable", False) or not re.match(r"^\s*to\b(?![-'’])", to_field.label or "", re.I):
            continue
        value = getattr(to_field, "value", "") or ""
        if value and value == (getattr(to_field, "placeholder", "") or None):
            value = ""
        value = re.sub(r"^\s*to\s*:", "", value, flags=re.I)
        names = [" ".join(part.split()).strip(" ,;") for part in re.split(r"[\ufffc,;\n]", value)]
        names = [n for n in names if n and not UNRESOLVED.search(n) and not internal_name(n)]
        if not names:
            # The tokens as buttons on the field's row, each "name, address".
            x, y, w, h = to_field.rect
            for e in elements:
                label = " ".join((e.label or "").split())
                cx, cy = e.rect[0] + e.rect[2] / 2, e.rect[1] + e.rect[3] / 2
                if (e is to_field or e.role != "Button" or not label or not (x <= cx <= x + w and y <= cy <= y + h)
                        or plain(label) in TO_ROW_CONTROLS or SF_SYMBOL.fullmatch(label) or UNRESOLVED.search(label)):
                    continue
                name = label.split(",")[0].strip()
                if name and not internal_name(name):
                    names.append(name)
        names = list(dict.fromkeys(names))
        if names:
            return ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
    return None


_TITLE_ROLES = frozenset({"NavigationBar", "Header", "Heading", "TabBar", "Toolbar", "Key", "Keyboard"})


def _screen_titles(snapshot):
    """What names the screen or the app: navigation titles and the app's own name (never proof)."""
    names = {" ".join(e.label.split()).casefold() for e in snapshot.elements if e.role in _TITLE_ROLES
             and e.label.strip()}
    bundle = (snapshot.bundle_id or "").rsplit(".", 1)[-1]
    if bundle:
        names.add(bundle.casefold())
    try:
        from . import harness_api
        apps = harness_api.hooks().app_catalog()  # () in the open-source engine: no app names beyond the bundle's
        names.update(" ".join(app["name"].split()).casefold() for app in apps
                     if app.get("bundleId") == snapshot.bundle_id)
    except Exception:
        pass
    return names


def header_recipient(snapshot, target=None):
    """Who a message goes to, from a filled To field's tokens or the conversation's header (its title, or the
    contact label near the top); None when the screen does not say, or names more than one candidate. A
    developer's name for a view ("CKComposeChat", a bar's identifier) is never one (contract.internal_name)."""
    if snapshot is None:
        return None
    to = addressed_to(snapshot)
    if to and len(to) <= 60:
        return to
    generic = _screen_titles(snapshot) | {"messages", "back", "contact photo", "details", "info", "edit", "cancel",
                                   "done", "facetime", "video", "call", "audio call", "video call", "search",
                                   "new message", "chats", "inbox", "compose", "more", "menu", "options"}
    above = target.rect[1] if target is not None else 1
    # The conversation header is the navigation bar when there is one: "iMessage · Encrypted" and the
    # day's timestamp sit just under it and are not who the message goes to.
    bars = [e.rect for e in snapshot.elements if e.role == "NavigationBar" and e.rect[1] < .22]
    top, bottom = (min(r[1] for r in bars), max(r[1] + r[3] for r in bars)) if bars else (0, .22)
    candidates = []
    for element in snapshot.elements:
        label = " ".join((element.label or "").split())
        x, y, w, h = element.rect
        if (element.editable or not label or len(label) > 40 or " ".join(label.split()).casefold() in generic
                or internal_name(label)
                or not top <= y + h / 2 <= bottom or y >= above
                or element.role in {"Image", "Icon", "Key", "Switch", "Toggle"}
                or re.fullmatch(r"[\d:. ]+(?:AM|PM)?|Today|Yesterday", label, re.I)):
            continue
        candidates.append(label)
    unique = list(dict.fromkeys(candidates))
    return unique[0] if len(unique) == 1 else None


def visible_recipient(snapshot, app=None, target=None):
    """Who the screen shows a message going to: the To field's tokens, else the thread's title (thread_title), else
    the one name atop a thread whose bar has no title ("Sam" above the messages: header_recipient; ``target``: the
    control about to be tapped, below it), else None. Never a developer's name for a view (internal_name), and never a
    mail's top line, which is its subject."""
    found = addressed_to(snapshot) or thread_title(snapshot, app)
    if found or app and MAIL_APPS.search(plain(app)) or not getattr(snapshot, "elements", None):
        return found
    name = header_recipient(snapshot, target)
    return None if not name or SUBJECT.match(name) or internal_name(name) else name


# An address typed whole: a phone number or an email. Return in a To field makes it a recipient of its own, as asked.
ADDRESS = re.compile(r"^\s*(?:\+?[\d\s().-]{7,}|[^@\s]+@[^@\s]+\.[^@\s]+)\s*$")
TO_FIELD = re.compile(r"^\s*(?:to|recipients?)\b(?![-'’])", re.I)
ROW_ROLES = frozenset({"Cell", "Button", "StaticText", "Other", "Link"})


def squash(text):
    return re.sub(r"[^0-9a-z]", "", plain(text))


def name_match(row, name):
    """How a row's name matches the name a request gave: "exact" (the same words, case and spacing aside), "close" (a
    handle made of it, "jordanlee" for "Jordan Lee"; a longer or shorter name holding it; a shared word), or None."""
    r, n = plain(row), plain(name)
    if not r or not n:
        return None
    if r == n:
        return "exact"
    rs, ns = squash(row), squash(name)
    if rs and ns and min(len(rs), len(ns)) >= 3 and (ns in rs or rs in ns):
        return "close"
    if set(re.findall(r"[a-z0-9]{3,}", r)) & set(re.findall(r"[a-z0-9]{3,}", n)):
        return "close"
    return None


def row_name(label):
    """A row's name: its label's first part ("Jordan Lee, (555) 010-0000, mobile" -> "Jordan Lee")."""
    return " ".join(str(label or "").split(",")[0].split())


def typed_query(element):
    """What a field holds as typed text, without its "To:" and its tokens (each an attachment mark)."""
    value = getattr(element, "text", None)
    value = value if isinstance(value, str) else (getattr(element, "value", "") or "")
    if value and value == (getattr(element, "placeholder", "") or None):
        value = ""
    value = re.sub(r"^\s*(?:to|recipients?)\s*:", "", value, flags=re.I)
    return " ".join(value.split("\ufffc")[-1].split()).strip(" ,;")


def same_party(a, b):
    """Whether two names of a party agree: one contains the other, or the same phone digits."""
    a, b = plain(a), plain(b)
    if not a or not b:
        return False
    digits_a, digits_b = re.sub(r"\D", "", a), re.sub(r"\D", "", b)
    if len(digits_a) >= 7 and len(digits_b) >= 7:
        return digits_a[-10:] == digits_b[-10:]
    return a in b or b in a


def store_verbs(label):
    """The App Store's commit verbs a control's label names (only meant for com.apple.AppStore)."""
    text = " ".join((label or "").split())
    if PRICE.match(text):
        return {"buy"}
    first = text.split(" ", 1)[0].strip(",.:;").casefold() if text else ""
    return {STORE_VERBS[first]} if first in STORE_VERBS else set()


def element_text(element):
    label, value = (element.label or ""), (getattr(element, "value", "") or "")
    return (label + (" " + value if value and value != label else "")).strip()


def is_search_field(element):
    return element.role == "SearchField" or bool(SEARCHY.search(element.label or ""))


def composer_field(element, elements, typed_texts=()):
    """Whether Return in ``element`` would send a message: a non-search editable field named like a
    composer ("chat_compose_field", placeholder "Add a comment"), or one beside a Send/Post button."""
    if element is None or not getattr(element, "editable", False) or is_search_field(element):
        return False
    value = getattr(element, "value", "") or ""
    placeholder = value if value and not any(overlaps(value, t) for t in typed_texts) else ""
    if SEARCHY.search(placeholder):
        return False
    if COMPOSER.search((element.label or "") + " " + placeholder):
        return True
    y = element.rect[1] + element.rect[3] / 2
    for other in elements:
        if other is element or getattr(other, "editable", False) or is_opener(other.label):
            continue
        verbs = label_verbs(other.label, other.role)
        if verbs and verbs <= MESSAGE and abs(other.rect[1] + other.rect[3] / 2 - y) <= .12:
            return True
    return False


DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b")
CLOCK = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?::(\d{2}))?(?![\d:])")
TIME_OF_DAY = re.compile(r"(?<![\d:])(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s?m\b")
UNIT_SECONDS = {"h": 3600, "m": 60, "s": 1}


def _durations(text):
    """Durations a text states, in seconds: "1 hour", "90 min", and clock readings "1:00:00" or "59:58"."""
    out = {round(float(n) * UNIT_SECONDS[u[0]]) for n, u in DURATION.findall(text)}
    for h, m, sec in CLOCK.findall(text):
        out.add(int(h) * 3600 + int(m) * 60 + int(sec) if sec else int(h) * 60 + int(m))
    return out


def _times(text):
    """Times of day a text states, as minutes past midnight modulo 12 hours: "7 PM", "7:00 pm", "19:00", "6:45"."""
    out = {(int(h) % 12) * 60 + int(m or 0) for h, m, _ in TIME_OF_DAY.findall(text)}
    out |= {(int(h) % 12) * 60 + int(m) for h, m, sec in CLOCK.findall(text) if not sec}
    return out


def _token(atom, text):
    """``atom`` (a number or a time) in ``text`` as a whole token, not inside a longer number or time."""
    return re.search(rf"(?<![\d.:,]){re.escape(atom)}(?![\d:]|[.,]\d)", text) is not None


def shows(key, text):
    """Whether a row's ``text`` shows the short value ``key``: whole, or its numbers and times with most of
    its words ("6:45 AM" in "6:45, gym on"; "Home (410 Brannan St)" in "410 brannan st").

    Numbers count only as whole tokens: the "1" in "+10:30HRS" does not show "1 hour" (10 Oct, iOSWorld: a World
    Clock row proved a 1-hour timer that was never set). A duration also matches an equal one in another unit or a
    running clock within a minute or 5% ("1 hour": "1 hr", "60 min", "1:00:00", "59:58"), and a time of day its
    other spellings ("7 PM": "7:00 pm", "19:00")."""
    if len(key) >= 40:
        return key in text
    if not re.search(r"\d", key):
        if key in text:
            return True
        words = [w for w in re.findall(r"[a-z]{3,}", key) if w not in ("the", "and", "for")]
        return bool(words) and all(w in text for w in words)
    durations = _durations(key) if DURATION.search(key) else set()
    if durations:
        return any(abs(seen - want) <= max(60, .05 * want) for seen in _durations(text) for want in durations)
    rest = key
    for match in TIME_OF_DAY.finditer(key):
        want = (int(match.group(1)) % 12) * 60 + int(match.group(2) or 0)
        if want not in _times(text):
            return False
        rest = rest.replace(match.group(0), " ")
    atoms = re.findall(r"\d{1,2}:\d{2}|\d[\d,.]*\d|\d", rest)
    # set by pickers and taps: "6 people" shows as "6 guests"
    return all(_token(atom, text) for atom in atoms)


# A tap on a control that names an act writes or commits (the first write in its app counts from it).
ACT_LABEL = re.compile(r"\b(save|send|add|book|confirm|delete|remove|post|pay|submit|create|reserve|request|buy|order|"
                       r"checkout|check out|start|apply|update|rename|accept|decline|join|follow|like|archive|mark|"
                       r"done|set|transfer|split|place|schedule|share|upload|reply|rsvp|log|clear|favorite|wishlist|"
                       r"enable|disable|turn on|turn off)\b", re.I)
WRITE_OPS = frozenset({"TYPE", "TYPE_SUBMIT", "SET_TEXT", "SUBMIT"})
ALREADY = re.compile(r"^already\b\W*(.+)$", re.I)


def receipt_patterns(act, verbs=frozenset()):
    """The rows that receipt a commit of ``act`` made by a control naming ``verbs``: the act's own, and those of
    every act the control's own verb names (an act's family holds it), since the control is what ran. "Request
    CityRideX · $12.80" books a ride whatever act the contract compiled for it: a ride compiled as request_money
    was requested ("Driver arriving") and never counted as done (ac2, 11 Oct). An act with no receipt row of its
    own keeps "the screen changed"; a control naming only SHARED_VERBS adds nothing."""
    own = RECEIPTS.get(act)
    if own is None:
        return []
    named = frozenset(verbs) - SHARED_VERBS
    others = [RECEIPTS[other] for other, family in FAMILIES.items()
              if other != act and other in RECEIPTS and named and named <= family]
    return [own] + others


def list_rows(elements, rows):
    """The folded rows of ``elements`` that sit in a list: a Cell, or an element whose centre is inside a Cell."""
    cells = [e.rect for e in elements if e.role == "Cell" and e.rect[2] * e.rect[3] > 0]
    if not cells:
        return frozenset()

    def inside(element):
        x, y = element.rect[0] + element.rect[2] / 2, element.rect[1] + element.rect[3] / 2
        return any(cx <= x <= cx + cw and cy <= y <= cy + ch for cx, cy, cw, ch in cells)
    return frozenset(text for element, (text, editable, role) in zip(elements, rows)
                     if text and not editable and (role == "Cell" or inside(element)))


def message_key(payload):
    """What identifies a sent message on screen: its first non-empty line, folded, cut to 40 characters."""
    return plain(next((line for line in str(payload or "").split("\n") if line.strip()), payload or ""))[:40]


def overlaps(a, b):
    a, b = plain(a), plain(b)
    if len(a) < 3 or len(b) < 3:
        return a == b and bool(a)
    return a[:30] in b or b[:30] in a


NAME = re.compile(r"^(?:[A-Z][\w'’.&-]*)(?: [A-Z][\w'’.&-]*){0,2}$")


def recipient(item):
    """The person or place a commit is for, when ``what`` names one ("message to Sam" -> "Sam", "Sam" ->
    "Sam"), else None. Never a developer's name for a view (internal_name)."""
    what = " ".join(str(item.what or "").split())
    found = re.search(r"\b(?:to|for|with)\s+((?:[A-Z][\w'’.&-]*)(?: [A-Z][\w'’.&-]*){0,2})", what)
    names = quoted(what)
    who = found.group(1) if found else names[0] if names else (
        what if NAME.fullmatch(what) and plain(what) not in GENERIC else None)
    return None if internal_name(who) else who


def point_in(rect, x, y):
    ex, ey, ew, eh = rect
    return ex <= x <= ex + ew and ey <= y <= ey + eh


# ---------------------------------------------------------------- amounts

def bounds(item):
    """(low, high) money bounds from the item's condition and quote ("under $50" -> (None, 49.99...)). A
    money act that names one amount and no bound moves exactly that ("requests ... for $15")."""
    low = high = None
    text = plain(item.condition + " " + item.quote)
    if item.act in ("pay", "transfer", "request_money") and not BOUND.search(text):
        amounts = {as_float(m) for m in MONEY.findall(text)} - {None}
        if len(amounts) == 1:
            value = amounts.pop()
            return value - .005, value + .005
    for match in BOUND.finditer(text):
        value = as_float(match.group(2))
        if value is None:
            continue
        word = match.group(1)
        if word in ("under", "below", "less than"):
            high = value - 1e-9
        elif word in ("at most", "up to", "no more than"):
            high = value
        elif word in ("over", "above", "more than"):
            low = value + 1e-9
        else:
            low = value
    return low, high


def center_y(element):
    return element.rect[1] + element.rect[3] / 2


def screen_amounts(elements):
    """[(amount, y, element)] of the money shown on screen; a "$" drawn apart from its digits
    (SplitPay's keypad amount: "$" then "52") is joined."""
    out = []
    ordered = [e for e in elements if not getattr(e, "editable", False) or MONEY.search(e.value or "")]
    for index, element in enumerate(ordered):
        text = element_text(element)
        for match in MONEY.finditer(text):
            value = as_float(match.group(1))
            if value is not None:
                out.append((value, center_y(element), element))
        if text.strip() == "$":
            for other in ordered[index + 1:index + 3]:
                digits = element_text(other).strip()
                if re.fullmatch(r"\d[\d,]*(?:\.\d+)?", digits) and abs(center_y(other) - center_y(element)) < .06:
                    value = as_float(digits)
                    if value is not None:
                        out.append((value, center_y(other), other))
                    break
    return out


AMOUNT_FIELD = re.compile(r"[$€£]?\s?(\d[\d,]*(?:\.\d+)?)")


def typed_amount(element, elements, typed):
    """The amount in a field the run typed into, nearest the control: what a form's Send or Pay moves. ``typed``:
    (the field's label, the text) for each thing the run typed, the label None for text typed earlier in this model
    call's chain, which counts only where a field holds exactly that text. The field must be named Amount and its whole
    value must be an amount ("50", "$50.00"): a numeric memo or account number is not one. None when no such field is on
    screen. A bank's form shows the account's balance beside the amount field ("Available $15,621.00"), and the nearest
    money is that, not the 50 that was typed (iOSWorld mybank-003, 18 Oct)."""
    found = []
    for field_ in elements:
        value = (getattr(field_, "value", "") or "").strip()
        if not getattr(field_, "editable", False) or not value or value == (getattr(field_, "placeholder", "") or ""):
            continue
        if plain(field_.label) != "amount":
            continue
        whole = AMOUNT_FIELD.fullmatch(value)
        amount = as_float(whole.group(1)) if whole else None
        mine = any(label == field_.label if label is not None else plain(text) == plain(value)
                   for label, text in typed)
        if amount is not None and mine:
            found.append((abs(center_y(field_) - center_y(element)), amount))
    return min(found)[1] if found else None


# The words a bank puts beside money an account holds. "Balance due", "statement balance", an amount or a total are
# what a payment moves, and are not these.
HELD = re.compile(r"(?:available (?:balance|credit|funds|cash|to spend)|current balance)")
HELD_SECTION = re.compile(r"(?:bank accounts|credit cards)(?: \(\d+\))?")


def held_figures(elements):
    """Labelled account balances on a non-editable dashboard, including an account section's aligned total.
    A bare payment can equal one balance or their sum: only its labels and row establish what the figure means.
    Unknown figures remain checked as payment amounts; any editable field keeps the normal form checks."""
    sections = [e for e in elements if HELD_SECTION.fullmatch(plain(element_text(e)))]
    if not sections or any(getattr(e, "editable", False) for e in elements):
        return set()
    held, bare = set(), []
    for element in elements:
        text = element_text(element)
        if MONEY.fullmatch(text.strip()):
            bare.append((element, as_float(MONEY.fullmatch(text.strip()).group(1))))
            continue
        parts = [p.strip() for p in re.split(r",\s+|\n", text)]
        for index, part in enumerate(parts):
            for match in MONEY.finditer(part):
                beside = [plain(part.replace(match.group(0), "")).strip(" :,"), *(
                    plain(parts[i]) for i in (index - 1, index + 1) if 0 <= i < len(parts))]
                if any(HELD.fullmatch(words) or HELD_SECTION.fullmatch(words) for words in beside):
                    held.add((id(element), as_float(match.group(1))))
    for element, value in bare:
        if value is None:
            continue
        for section in sections:
            tolerance = min(element.rect[3], section.rect[3]) / 4
            if tolerance > 0 and abs(center_y(element) - center_y(section)) <= tolerance:
                held.add((id(element), value))
                break
    return held


def commit_amount(element, elements, typed=()):
    """The amount a commit control moves: in its label, else in a field the run typed it into (``typed``:
    ``typed_amount``), else the money shown nearest to it that isn't money an account holds (``held_figures``)."""
    match = MONEY.search(element.label or "")
    if match:
        return as_float(match.group(1))
    entered = typed_amount(element, elements, typed)
    if entered is not None:
        return entered
    held = held_figures(elements)
    shown = [(abs(y - center_y(element)), value) for value, y, e in screen_amounts(elements)
             if e is not element and (id(e), value) not in held]
    return min(shown)[1] if shown else None


def counterparty(element, elements):
    """Name tokens of the person a money commit goes to: the nearest row holding two capitalized words."""
    best = None
    for other in list(elements) + [element]:
        text = element_text(other)
        words = [w for w in re.findall(r"[A-Z][a-z]+", text) if w.casefold() not in NAME_STOP]
        if len(words) < 2 or not re.search(r"[A-Z][a-z]+ [A-Z][a-z]+", text):
            continue
        distance = 0 if other is element else abs(center_y(other) - center_y(element))
        if best is None or distance < best[0]:
            best = (distance, {w.casefold() for w in words})
    return best[1] if best and best[0] <= .4 else set()


# ---------------------------------------------------------------- items

@dataclass
class Item:
    id: int
    kind: str
    app: str = ""
    what: str = ""
    payload: str = ""
    act: str = "none"
    count: str = ""
    condition: str = ""
    quote: str = ""
    status: str = "open"            # open | closed (commits) | found | missing (reports)
    value: str = None               # a REPORT's recorded value
    value_step: int = None
    claim: str = None               # the model's own mark: done | missing
    receipts: list = field(default_factory=list)
    executions: int = 0
    reopens: int = 0
    unproven: bool = False
    reason: str = ""
    already: tuple = None           # (step, row, whole) the model cited as showing a WRITE was already so

    def family(self):
        fam = set(FAMILIES.get(self.act, ()))
        if self.act == "other":
            # A commit the families do not name ("reset my filters", "save it") allows the verbs its
            # own verbatim quote uses.
            fam |= {v.casefold() for v in COMMIT_WORDS.findall(self.quote)}
        elif self.kind == "COMMIT":
            # The request's own word for the act names it too: "Request a CityRideX" compiled as an order
            # met a "Request CityRideX · $12.80" button the order family refused 5-21 times a run (iOSWorld
            # A/A, 10 Oct: 6 of 37 failures). The quote is verbatim in the request (from_items drops one that
            # is not), and only its first commit word is taken: the word that asks for this act, not one of a
            # later clause the quote ran into.
            first = COMMIT_WORDS.search(self.quote)
            if first:
                fam.add(" ".join(first.group(1).casefold().split()))
        return fam

    def needed(self):
        """Receipts that close the item: None for "each" (closed only by the model's mark)."""
        count = str(self.count or "1").strip().casefold()
        if count in ("each", "all", "any", "every"):
            return None
        return int(count) if count.isdigit() and int(count) > 0 else 1

    def label(self):
        where = f" ({self.app})" if self.app else ""
        act = f" {self.act}" if self.kind in ("COMMIT", "FORBID") and self.act not in ("none", "") else ""
        extra = (f" [{self.count}]" if self.kind == "COMMIT" and str(self.count).strip() not in ("", "1") else "") \
            + (f" if {self.condition}" if self.condition else "") \
            + (f' = "{self.payload}"' if self.payload else "")
        return f"{self.kind}{act}{where}: {self.what}{extra}"


@dataclass
class Gate:
    """A commit check's outcome. ``item`` is the declared commit it serves (None: not a commit)."""
    allowed: bool
    item: Item = None
    reason: str = ""
    verbs: frozenset = frozenset()
    amount: float = None
    names: frozenset = frozenset()
    payload: str = None


@dataclass(frozen=True)
class Control:
    """A control as one screen showed it: where it was, what it said, whether it was on, and its count."""
    role: str
    rect: tuple
    label: str
    value: str
    selected: bool
    count: int = None


def shown_count(element, elements, rect=None):
    """The count a control shows (``rect``: where it was): its own label's or value's last number ("Like, 7"), else
    the nearest count just right of it in its row ("7" beside a heart), else None."""
    for text in (getattr(element, "value", "") or "", element.label or "") if element is not None else ():
        found = re.search(r"(?:^|[\s,])(\d{1,3}(?:,\d{3})+|\d+)(?:\s+(?:likes?|reactions?|hearts?|followers?))?\s*$",
                          " ".join(text.split()), re.I)
        if found:
            return int(found.group(1).replace(",", ""))
    x, y, w, h = rect or element.rect
    beside = []
    for other in elements:
        if other is element or getattr(other, "editable", False):
            continue
        ox, oy, ow, oh = other.rect
        text = " ".join((other.label or getattr(other, "value", "") or "").split())
        found = COUNT.match(text)
        if found and abs(oy + oh / 2 - (y + h / 2)) <= max(h / 2, .015) and x + w - .01 <= ox <= x + w + .08:
            beside.append((ox - (x + w), int(found.group(1).replace(",", ""))))
    return min(beside)[1] if beside else None


def control_state(element, elements):
    return Control(element.role, tuple(element.rect), " ".join((element.label or "").split()),
                   " ".join((getattr(element, "value", "") or "").split()), bool(getattr(element, "selected", False)),
                   shown_count(element, elements))


def is_on(act, element):
    """Whether a like's or a follow's control already shows its act on: selected, or its words say so."""
    on = STATE_ON.get(act)
    return bool(getattr(element, "selected", False) or on and (on.search(element.label or "") or on.search(
        getattr(element, "value", "") or "")))


def same_control(control, elements):
    """The elements of a later screen where ``control`` was: its role (or another toggle's), its centre within half
    its size (at least .03 across, .02 down) of where it was."""
    x, y, w, h = control.rect
    cx, cy = x + w / 2, y + h / 2
    out = []
    for e in elements:
        ex, ey, ew, eh = e.rect
        if (e.role == control.role or e.role in TOGGLE_ROLES and control.role in TOGGLE_ROLES) and not getattr(
                e, "editable", False) and abs(ex + ew / 2 - cx) <= max(w / 2, .03) and abs(ey + eh / 2 - cy) <= max(
                h / 2, .02):
            out.append(e)
    return out


@dataclass
class Pending:
    item: Item
    label: str
    app: str
    step: int
    before: frozenset
    payload: str = None
    amount: float = None
    names: frozenset = frozenset()
    title: str = None           # the screen's title at the commit: a message is proven in that thread (O2)
    composer: str = ""          # the composer's own label (folded): only that field holding the text is unsent
    hits: int = 0               # rows outside the composer that held the text before the commit
    verbs: frozenset = frozenset()  # the commit verbs the tapped control named ("request" in "Request CityRideX")
    control: Control = None     # a state act's tapped control as it was before the tap (STATE_ACTS)
