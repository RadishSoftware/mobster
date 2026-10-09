"""Proof-gated completion (frontier Bet 1): the task contract, the declared-commit policy, the source
ledger and the DONE gate in code.

One text-only call (reasoning none, run while the first screen is read) compiles the request into typed
items: READ a source, WRITE a value, COMMIT an act other people see or that spends, REPORT a detail,
FORBID an act the request rules out. Code then does three jobs the prompt used to do:

- Commits pass only when declared. A tap or long press on a control whose first words name a commit
  ("Send", "Pay $42.00", "Place Order"), or Return in a message composer, passes only when every commit
  verb in it belongs to the act family of an open COMMIT item of the current app; its amount must meet
  the item's bounds ("under $50"); a message needs text the agent typed in a field on screen (a feed's
  share "Send" has none); an object commit (Follow) needs its object's name on screen. A COMMIT whose
  quote is not verbatim in the request is dropped. F1 replay (26 Sep, 88 runs): the regex guard refused
  63 required commits; this policy allows all 63 and refuses every unrequested one.
- Receipts close items: a sent message shows in a non-editable row and has left the composer; a booking,
  order, payment or follow shows a new confirmation row; a write reads back in its app; a reported value
  was on a screen at or before the turn it was recorded (the ledger); a named source was seen in its app.
  A closed commit refuses repeats (a second Follow tap unfollows); "each" items stay open.
- DONE is accepted when every item has its receipt, or with at most LOW_TURNS turns left, when the
  answer names what is unproven. A missing receipt reopens its item with a one-line reason (at most
  MAX_REOPENS times an item, then it is marked unproven so the gate cannot eat the budget).

Elements are duck-typed (label, role, rect as screen fractions, editable, value): state.Element in the
agent, recorded rows in the offline replay.
"""

import re
from dataclasses import dataclass, field

KINDS = ("READ", "WRITE", "COMMIT", "REPORT", "FORBID")
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

GUARD_WORDS = 4
# The verbs of task_policy.COMMIT_CONTROL plus the words F1 found ungated (request, cancel, unfollow,
# archive, save, like, react, unsubscribe). The added words are common in prose ("Feels Like", "Save on
# rides", "Your requests"), so they count only as a label's first word, where a control names its act.
COMMIT_WORDS = re.compile(
    r"\b(send|post|publish|reply|comment|share|tweet|buy|purchase|place order|order|pay|checkout|transfer|"
    r"donate|tip|subscribe|unsubscribe|book|reserve|delete|erase|remove|reset|install|upload|redeem|call|dial|"
    r"facetime|unfollow|follow|sign[ -]?up|submit|confirm|request|cancel|archive|save|like|react)\b", re.I)
FIRST_WORD_ONLY = frozenset({"request", "cancel", "unfollow", "unsubscribe", "archive", "save", "like", "react"})
# Controls that only open a sheet, a thread or a comment list (whose committing control is gated in
# turn), and the bare Cancel that closes one.
OPENERS = frozenset({"share", "share…", "share...", "comment", "comments", "comment left", "reply",
                     "reply in thread", "cancel"})
# SF Symbol names used as labels ("pencil.tip" is a drawing tool, not a tip).
SF_SYMBOL = re.compile(r"[a-z0-9]+(?:\.[a-z0-9]+)+")
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
                        r"order number|on (its|the) way|being prepared|estimated (delivery|arrival)|\barriving\b", re.I),
    "book": re.compile(r"\b(confirmed|confirmation|reserved|booked|you.re all set|ride requested|finding (you a|your) "
                       r"driver|driver (is|on)|arriving|on the way)\b|\bRSV[- ]?\w+|\b[A-Z]{2,4}-?\d{4,}\b", re.I),
    "pay": re.compile(r"\b(paid|payment (sent|complete|successful)|you paid|money sent|sent \$|completed)\b", re.I),
    "transfer": re.compile(r"\b(transfer(red)? (complete|sent|scheduled|successful)|transferred|completed|sent \$)\b", re.I),
    "request_money": re.compile(r"\b(requested|request sent|you requested|request (is )?pending)\b", re.I),
    "follow": re.compile(r"^(following|unfollow|followed|subscribed)\b", re.I),
    "unfollow": re.compile(r"^(follow|unfollowed|subscribe)\b", re.I),
    # An install has begun or finished: the button turned to its progress ring or to Open. Apple's own sheet
    # ("Double Click to Install", Face ID, the Apple ID password) changes the screen too, and is not one: the
    # app is not installed until the user confirms there.
    "install": re.compile(r"^(open|downloading|installing|waiting|redownloading|stop)\b|\b(stop|pause|cancel) "
                          r"download", re.I),
}
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
LOW_TURNS = 3
# Reopens an item may cost before it is marked unproven: one (B6: the gate twice sent cal-tomorrow, weather and
# appstore-duolingo back for proof of a "no" that no screen could quote).
MAX_REOPENS = 1
MAX_ITEMS = 24


def plain(text):
    """``text`` for containment checks: case, quote style and spacing ignored."""
    return " ".join(str(text or "").casefold().replace("’", "'").replace("‘", "'").replace("“", '"')
                    .replace("”", '"').replace(" ", " ").split())


def _float(number):
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


def is_opener(label):
    return " ".join((label or "").split()).casefold() in OPENERS


def label_verbs(label, role="Button"):
    """The commit verbs a control's label names (set, empty when it names none)."""
    if (label or "").strip().casefold() == "following":
        return {"unfollow"}  # a Follow button after the tap: tapping it again unfollows
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
    x, y, w, h = bar.rect
    inside = [e for e in elements if e.role in ("StaticText", "Button") and (e.label or "").strip()
              and y <= e.rect[1] + e.rect[3] / 2 <= y + min(h, .07) and x + .2 * w <= e.rect[0] + e.rect[2] / 2
              <= x + .8 * w]
    if not inside:
        return None
    centred = min(inside, key=lambda e: abs(e.rect[0] + e.rect[2] / 2 - (x + w / 2)))
    return re.sub(r",\s*Actions Menu$", "", " ".join(centred.label.split()), flags=re.I) or None


def thread_title(snapshot, app=None):
    """The person or place a message screen is with, as its title shows it ("+1 (555) 564-8583"), or None for a
    list's or a blank composer's title, and for mail: a reply's sheet is titled with its subject, and "Send this
    reply to Re: Dinner plans?" names no one."""
    title = screen_title(snapshot)
    if not title:
        return None
    folded = plain(title)
    if folded in GENERIC_TITLES or app and folded == plain(app):
        return None
    if SUBJECT.match(title) or app and MAIL_APPS.search(plain(app)):
        return None
    return title


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
    placeholder = value if value and not any(_overlaps(value, t) for t in typed_texts) else ""
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


def _shows(key, text):
    """Whether a row's ``text`` shows the short value ``key``: whole, or its numbers and times with most of
    its words ("6:45 AM" in "6:45, gym on"; "Home (410 Brannan St)" in "410 brannan st")."""
    if key in text:
        return True
    if len(key) >= 40:
        return False
    atoms = re.findall(r"\d{1,2}:\d{2}|\d[\d,.]*", key)
    words = [w for w in re.findall(r"[a-z]{3,}", key) if w not in ("the", "and", "for")]
    if atoms:  # set by pickers and taps: "6 people" shows as "6 guests", "7 PM Friday" as "7:00 pm"
        return all(a in text for a in atoms)
    return bool(words) and all(w in text for w in words)


def message_key(payload):
    """What identifies a sent message on screen: its first non-empty line, folded, cut to 40 characters."""
    return plain(next((line for line in str(payload or "").split("\n") if line.strip()), payload or ""))[:40]


def _overlaps(a, b):
    a, b = plain(a), plain(b)
    if len(a) < 3 or len(b) < 3:
        return a == b and bool(a)
    return a[:30] in b or b[:30] in a


# ---------------------------------------------------------------- amounts

def bounds(item):
    """(low, high) money bounds from the item's condition and quote ("under $50" -> (None, 49.99...)). A
    money act that names one amount and no bound moves exactly that ("requests ... for $15")."""
    low = high = None
    text = plain(item.condition + " " + item.quote)
    if item.act in ("pay", "transfer", "request_money") and not BOUND.search(text):
        amounts = {_float(m) for m in MONEY.findall(text)} - {None}
        if len(amounts) == 1:
            value = amounts.pop()
            return value - .005, value + .005
    for match in BOUND.finditer(text):
        value = _float(match.group(2))
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


def _center_y(element):
    return element.rect[1] + element.rect[3] / 2


def screen_amounts(elements):
    """[(amount, y, element)] of the money shown on screen; a "$" drawn apart from its digits
    (SplitPay's keypad amount: "$" then "52") is joined."""
    out = []
    ordered = [e for e in elements if not getattr(e, "editable", False) or MONEY.search(e.value or "")]
    for index, element in enumerate(ordered):
        text = element_text(element)
        for match in MONEY.finditer(text):
            value = _float(match.group(1))
            if value is not None:
                out.append((value, _center_y(element), element))
        if text.strip() == "$":
            for other in ordered[index + 1:index + 3]:
                digits = element_text(other).strip()
                if re.fullmatch(r"\d[\d,]*(?:\.\d+)?", digits) and abs(_center_y(other) - _center_y(element)) < .06:
                    value = _float(digits)
                    if value is not None:
                        out.append((value, _center_y(other), other))
                    break
    return out


def commit_amount(element, elements):
    """The amount a commit control moves: in its label, else the money shown nearest to it."""
    match = MONEY.search(element.label or "")
    if match:
        return _float(match.group(1))
    shown = [(abs(y - _center_y(element)), value) for value, y, e in screen_amounts(elements) if e is not element]
    return min(shown)[1] if shown else None


def counterparty(element, elements):
    """Name tokens of the person a money commit goes to: the nearest row holding two capitalized words."""
    best = None
    for other in list(elements) + [element]:
        text = element_text(other)
        words = [w for w in re.findall(r"[A-Z][a-z]+", text) if w.casefold() not in NAME_STOP]
        if len(words) < 2 or not re.search(r"[A-Z][a-z]+ [A-Z][a-z]+", text):
            continue
        distance = 0 if other is element else abs(_center_y(other) - _center_y(element))
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

    def family(self):
        fam = set(FAMILIES.get(self.act, ()))
        if self.act == "other":
            # A commit the families do not name ("reset my filters", "save it") allows the verbs its
            # own verbatim quote uses.
            fam |= {v.casefold() for v in COMMIT_WORDS.findall(self.quote)}
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


# ---------------------------------------------------------------- compile

SYSTEM = """Compile a phone agent's task contract from the user's request, before any work starts. Code enforces \
the contract: the agent may only perform the commits you declare, and the task counts as done only when every \
item has on-screen proof.

Item kinds:
- READ: every app, folder, list, record or tab the request names as a place to look, check or review (one item \
per source). When the request involves email in the Mail app, add its Sent and Archive folders as separate READs.
- WRITE: every text or value the agent must enter or change (a note, a document section, a comment body, a \
message body, a cell, an alarm, a form field). payload = the exact text only when the request dictates it word \
for word (a title in quotes, a time), else "".
- COMMIT: every act the request asks for that other people can see, that spends, moves or requests money, that \
orders, books or reserves, that follows or subscribes, that reacts to a post, that saves, archives, cancels or \
deletes, shares or calls, that gets, installs or buys an app from the App Store (act install, what = the app's \
name): anything a user would want to approve before it happens. One item per act and object. \
Declare only acts the request asks for; never infer one from a goal ("plan my trip" does not ask to book). A \
conditional act ("if rain, message X") is still declared, with its condition. A message, reply, comment or post \
the request asks the agent to compose, write or draft is also to be sent or posted, unless the request says not \
to: declare that COMMIT too, quoting the words that ask for the message.
- REPORT: every separate detail the user asks to be told or to have confirmed.
- FORBID: every act the request rules out ("do not delete anything", "don't send it yet"), with that act.

Fields:
- app: the app's name from the list, or "" if none.
- what: the source (READ), the target (WRITE), the object acted on (COMMIT, FORBID), or the detail (REPORT). \
Name the object as the request does (a person, a company, a document title).
- act: for a COMMIT or FORBID, its kind; "none" for other items.
- count: for a COMMIT, "1", a number, or "each" when the request says each, all or any; "" for other items.
- condition: the request's condition for the item, else "".
- quote: the request's own words that ask for this item, copied verbatim as one contiguous span. For a COMMIT \
the quote must include the words that ask for that act.
Keep the request's order; at most 24 items."""

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["items"], "properties": {
    "items": {"type": "array", "maxItems": MAX_ITEMS, "items": {
        "type": "object", "additionalProperties": False,
        "required": ["kind", "app", "what", "payload", "act", "count", "condition", "quote"],
        "properties": {
            "kind": {"type": "string", "enum": list(KINDS)},
            "app": {"type": "string"}, "what": {"type": "string"}, "payload": {"type": "string"},
            "act": {"type": "string", "enum": list(ACTS)}, "count": {"type": "string"},
            "condition": {"type": "string"}, "quote": {"type": "string"}}}}}}


def compile_contract(client, request, apps=(), timeout=60):
    """The request's Contract (one text-only call at reasoning "none"), or None if the call fails or
    yields no items: the agent then keeps the legacy checklist and regex guard."""
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": [
        {"type": "text", "text": f"Request: {request}\n\nApps: " + ", ".join(apps)}]}]
    # A client that takes reasoning per call is not touched: the first decision may be in flight on it.
    per_call = getattr(client, "per_call_reasoning", False) is True
    saved = None if per_call else getattr(client, "reasoning", None)
    if saved is not None:
        client.reasoning = "none"
    try:
        out, _ = (client.complete(messages, SCHEMA, timeout=timeout, reasoning="none") if per_call
                  else client.complete(messages, SCHEMA, timeout=timeout))
    except Exception:
        return None
    finally:
        if saved is not None:
            client.reasoning = saved
    contract = Contract.from_items((out or {}).get("items") or (), request)
    return contract if contract.items else None


# ---------------------------------------------------------------- the contract

class Contract:
    def __init__(self, items, request, dropped=()):
        self.items, self.request, self.dropped = list(items), request, list(dropped)
        self.screens = []           # (step, app, [(text, editable, role)], keyboard)
        self.typed = []             # {step, app, text, field, search}
        self.empty_searches = []    # (step, app, query)
        self.visited = set()
        self.pending = []
        self.money = []             # receipted money commits: (amount, names, step)
        self.events = []            # receipts and refusals, for the trace
        # Parallel to ``screens``: (step, app, title, {row: the row as shown}). The rows above are folded for
        # matching; the Mac app quotes proof as the screen showed it, in the place it showed it.
        self.shown = []

    @classmethod
    def from_items(cls, raw, request):
        """Validated items: kinds and acts known; a COMMIT whose quote is not verbatim in the request is
        dropped (the model cannot declare an act the user never asked for)."""
        items, dropped, asked = [], [], plain(request)
        for entry in list(raw)[:MAX_ITEMS]:
            if not isinstance(entry, dict) or entry.get("kind") not in KINDS:
                continue
            text = {k: " ".join(str(entry.get(k) or "").split()) for k in
                    ("app", "what", "payload", "act", "count", "condition", "quote")}
            if not text["what"]:
                continue
            item = Item(id=len(items) + 1, kind=entry["kind"], **text)
            if item.kind in ("COMMIT", "FORBID") and item.act not in FAMILIES:
                dropped.append(item)
                continue
            if item.kind == "COMMIT" and not (plain(item.quote) and plain(item.quote) in asked):
                dropped.append(item)
                continue
            if item.kind != "COMMIT":
                item.count = ""
            items.append(item)
        for index, item in enumerate(items):
            item.id = index + 1
        return cls(items, request, dropped)

    # ------------------------------------------------ prompt

    def lines(self):
        """The contract as the model sees it: ``6. [open] COMMIT pay (SplitPay): pending requests [each]``."""
        out = []
        for item in self.items:
            if item.kind == "FORBID":
                mark = "[never]"
            elif item.kind == "REPORT":
                mark = {"found": f"[found: {item.value}]", "missing": "[missing]"}.get(item.status, "[ ]")
            elif item.status == "closed":
                mark = "[done: shown on screen]"
            elif item.receipts:
                mark = f"[{len(item.receipts)} done, open]"
            else:
                mark = "[ ]"
            out.append(f"{item.id}. {mark} {item.label()}" + (f" -- {item.reason}" if item.reason and
                                                                 item.status not in ("closed",) else ""))
        return out

    HEADER = ("Task contract (code enforces it: a commit it does not list is refused, and DONE is accepted only "
              "when the screens you saw show every item. In checklist_updates mark REPORT items found with the "
              "exact value as shown, or missing; mark other items done when you finish them. Sent messages, "
              "confirmations and saved text are checked on screen):")

    # ------------------------------------------------ the model's marks

    def update(self, updates, step):
        """Apply a turn's checklist_updates; returns the items that changed."""
        by_id, changed = {item.id: item for item in self.items}, []
        for update in updates or ():
            item = by_id.get(update.get("id")) if isinstance(update, dict) else None
            status = update.get("status") if isinstance(update, dict) else None
            if item is None or status not in ("done", "found", "missing"):
                continue
            value = " ".join(str(update.get("value") or "").split())[:300] or None
            if item.kind == "REPORT":
                if status == "found" and not value:
                    continue
                item.status = "found" if status in ("found", "done") and value else "missing"
                item.value, item.value_step = value, step
            else:
                item.claim = status
                if value and item.kind != "COMMIT":
                    item.value, item.value_step = value, step
            changed.append(item)
        return changed

    # ------------------------------------------------ the ledger

    def record_typed(self, app, element, text, step):
        if text and str(text).strip():
            self.typed.append({"step": step, "app": app, "text": str(text),
                               "field": getattr(element, "label", "") or "",
                               "search": bool(element is not None and is_search_field(element)),
                               "picker": getattr(element, "role", "") in ("PickerWheel", "Picker", "Slider")})

    def typed_texts(self, app=None):
        return [t["text"] for t in self.typed if app is None or t["app"] == app]

    def saw_lines(self, app, step, lines):
        """Rows read by a macro (READ_LIST) are seen too."""
        self.screens.append((step, app, [(plain(line), False, "StaticText") for line in lines], False))
        self.shown.append((step, app, None, {plain(line): " ".join(str(line).split()) for line in lines}))
        self.visited.add(app)

    def observe(self, snapshot, app, step):
        """Record a settled screen: the ledger, pending receipts, empty-search proofs."""
        elements = list(getattr(snapshot, "elements", ()) or ())
        rows = [(plain(element_text(e)), bool(getattr(e, "editable", False)), e.role) for e in elements]
        keyboard = getattr(snapshot, "keyboard", "") == "visible"
        originals = {}
        for element, (text, _, _) in zip(elements, rows):
            originals.setdefault(text, " ".join(element_text(element).split()))
        title = screen_title(snapshot)
        if not self.screens or self.screens[-1][1:] != (app, rows, keyboard):
            self.screens.append((step, app, rows, keyboard))
            self.shown.append((step, app, title, originals))
        self.visited.add(app)
        texts = frozenset(t for t, _, _ in rows)
        for pending in list(self.pending):
            receipt = self._receipt(pending, app, rows, texts, title)
            if receipt:
                self._close(pending, receipt, step, originals, title)
        self._empty_search(elements, app, step)

    def _receipt(self, pending, app, rows, texts, title=None):
        if app != pending.app:
            return None
        act = pending.item.act
        if act in MESSAGE_ACTS and pending.payload:
            key = message_key(pending.payload)

            def composing(text, editable):
                # The composer still holding the text: unsent. Known by its label; else any field (as before).
                return editable and (not pending.composer or text.startswith(pending.composer))
            held = any(key and key in t for t, editable, _ in rows if composing(t, editable))
            if held:
                return None
            shown = [t for t, editable, _ in rows if key and key in t and not editable]
            if shown:
                return f"sent: {shown[-1][:120]}"
            # The bubble itself (a text view in iOS 26 Messages) with its delivery mark, in the thread where the
            # send was approved: "Delivered" under the exact text (O2, Kate Bell: sent, then "not confirmed").
            bubbles = [t for t, editable, _ in rows if key and key in t and not composing(t, editable)]
            same_thread = not pending.title or not title or plain(title) == plain(pending.title)
            if len(bubbles) > pending.hits and same_thread and any(DELIVERY.match(t) for t, _, _ in rows):
                return f"sent: {bubbles[-1][:120]}"
            return None
        new = [t for t in texts if t not in pending.before]
        pattern = RECEIPTS.get(act)
        if pattern is not None:
            hit = next((t for t in new if pattern.search(t)), None)
            return f"shown: {hit[:120]}" if hit else None
        if act in MESSAGE_ACTS:
            return None  # a message without its typed text has no receipt to look for
        return "screen changed" if texts != pending.before else None

    def _close(self, pending, receipt, step, originals=None, title=None):
        item = pending.item
        self.pending.remove(pending)
        item.receipts.append({"step": step, "label": pending.label, "receipt": receipt, "amount": pending.amount,
                              "payload": pending.payload})
        item.reason = ""
        if pending.amount is not None and item.act in ("pay", "transfer") and pending.names:
            self.money.append((pending.amount, pending.names, step))
        needed = item.needed()
        if needed is not None and len(item.receipts) >= needed:
            item.status = "closed"
        # For the Mac app's proof: the receipt's row as shown (the receipt itself is folded and cut), the
        # commit's exact text and the screen's title.
        row = receipt.split(": ", 1)[1] if receipt.startswith(("sent: ", "shown: ")) else None
        shown = next((original for text, original in (originals or {}).items() if row and text.startswith(row)), None)
        self.events.append({"event": "receipt", "item": item.id, "step": step, "receipt": receipt, "shown": shown,
                            "payload": pending.payload, "label": pending.label, "app": pending.app, "screen": title})

    def _empty_search(self, elements, app, step):
        typed = self.typed_texts(app)
        for field_ in elements:
            if not getattr(field_, "editable", False) or not is_search_field(field_) and not any(
                    t["search"] for t in self.typed if t["app"] == app):
                continue
            query = getattr(field_, "value", "") or ""
            if not query or not any(_overlaps(query, t) for t in typed):
                continue
            below = [e for e in elements if e is not field_ and e.rect[1] > field_.rect[1] + field_.rect[3]
                     and e.rect[1] < .85 and e.role not in ("Key", "Keyboard") and not getattr(e, "editable", False)]
            if any(NO_RESULTS.search(element_text(e)) for e in elements) or not below:
                self.empty_searches.append((step, app, query))
                return

    # ------------------------------------------------ the commit gate

    def check(self, operation, element, snapshot, app, *, text=None, chain_typed=(), bind=True):
        """Gate one action. Not a commit: Gate(True). A commit: allowed only for an open declared item.
        ``chain_typed``: text typed earlier in this model call's chain. ``bind=False`` skips the message
        and object bindings (label-level measurement only)."""
        elements = list(getattr(snapshot, "elements", ()) or ())
        if element is None:
            return Gate(True)
        payload = None
        store = getattr(snapshot, "bundle_id", "") == APP_STORE
        if operation in ("TAP", "LONG_PRESS"):
            verbs = label_verbs(element.label, element.role)
            if store:
                verbs = (verbs - {"install", "buy", "purchase"}) | store_verbs(element.label) if store_verbs(
                    element.label) else verbs
            if not verbs and element.role in SWITCH_ROLES and COMMIT_WORDS.search(element.label or ""):
                return self._toggle(element, app)
        elif operation in ("SUBMIT", "TYPE_SUBMIT") and composer_field(element, elements, self.typed_texts()):
            verbs = {"send"}
            value = getattr(element, "value", "") or ""
            if operation == "TYPE_SUBMIT" and text:
                payload = text
            elif value and any(_overlaps(value, t) for t in self.typed_texts()):
                payload = value
            else:  # the read shows the placeholder: the text typed just before is what Return sends
                payload = chain_typed[-1] if chain_typed else next(
                    (t["text"] for t in reversed(self.typed) if t["app"] == app and not t["search"]), None)
        else:
            return Gate(True)
        if not verbs:
            return Gate(True)
        verbs = frozenset(verbs)
        what = f"{'Return in' if operation in ('SUBMIT', 'TYPE_SUBMIT') else 'tapping'} {label_head(element.label) or element.label!r}"
        for item in self.items:
            if item.kind == "FORBID" and self._app_ok(item, app) and verbs & item.family():
                return self._refuse(verbs, f"Refused: {what} would {item.act} -- the request rules it out "
                                           f"(\"{item.quote}\").")
        declared = [i for i in self.items if i.kind == "COMMIT" and self._app_ok(i, app) and verbs <= i.family()
                    and (i.act != "install" or store)]  # an app is installed from the App Store only
        # "save" also completes a requested write (an alarm's or a form's Save) in the write's app.
        if verbs == {"save"} and not [i for i in declared if i.status != "closed"] and any(
                i.kind == "WRITE" and self._app_ok(i, app) for i in self.items):
            return Gate(True, verbs=verbs)
        if not declared:
            return self._refuse(verbs, f"Refused: {what} performs an act the request does not ask for "
                                       f"({', '.join(sorted(verbs))}{' in ' + app if app else ''}). Choose an "
                                       "action that does what the user asked.")
        open_ = [i for i in declared if i.status != "closed"]
        if not open_:
            done = declared[0]
            return self._refuse(verbs, f"Refused: item {done.id} ({done.what}) is already done and shown on "
                                       "screen; doing it again would repeat or undo it.")
        amount = commit_amount(element, elements) if any(i.act in MONEY_ACTS for i in open_) else None
        if amount is None and any(i.act == "install" for i in open_):
            # An app's price is on its own button ("$4.99"); a Get button is free whatever else the page shows.
            price = MONEY.search(element.label or "")
            amount = _float(price.group(1)) if price else None
        names = frozenset(counterparty(element, elements)) if amount is not None else frozenset()
        reasons = []
        for item in open_:
            low, high = bounds(item)
            if amount is not None and (low is not None and amount < low or high is not None and amount > high):
                reasons.append(f"the amount ${amount:,.2f} is outside the request's bound (\"{item.quote}\")")
                continue
            if item.act in ("pay", "transfer") and amount is not None and names and any(
                    abs(a - amount) < .005 and names & n for a, n, _ in self.money):
                step = next(s for a, n, s in self.money if abs(a - amount) < .005 and names & n)
                reasons.append(f"${amount:,.2f} to {' '.join(sorted(names)).title()} was already paid (turn {step}); "
                               "a request can still show pending after a direct payment")
                continue
            if bind and item.act in MESSAGE_ACTS and verbs <= MESSAGE:
                bound = payload if operation == "TYPE_SUBMIT" and payload else \
                    self._composer_text(elements, chain_typed, payload)
                if bound is None:
                    reasons.append("no text you typed is in a field on this screen, so this would send or share "
                                   "something else (type the message in its composer first)")
                    continue
                payload = payload or bound
            if bind and item.act in OBJECT_ACTS and not self._object_on_screen(
                    item, elements, list(chain_typed) + self.typed_texts(app)):
                reasons.append(f"{item.what!r} is not on this screen, so this {', '.join(sorted(verbs))} is for "
                               "something else")
                continue
            if bind and item.act == "install" and not self._beside_object(item, element, elements):
                # A results list puts a Get button on every row (and an ad above them): it must be the named app's.
                reasons.append(f"this button is not beside {item.what!r}, so it would install something else")
                continue
            return Gate(True, item=item, verbs=verbs, amount=amount, names=names, payload=payload)
        return self._refuse(verbs, f"Refused: {what}: " + "; ".join(dict.fromkeys(reasons)) + ".")

    def _toggle(self, element, app):
        """A switch whose label names a thing, not an act ("9:30, Client call, Weekdays" is an alarm): it
        changes that thing, so it passes only when a WRITE or COMMIT of this app names it."""
        tokens = {t for t in re.findall(r"\d{1,2}:\d{2}|[a-z]{3,}", plain(element.label))} - {"the", "and", "for"}
        for item in self.items:
            if item.kind in ("WRITE", "COMMIT") and self._app_ok(item, app):
                named = set(re.findall(r"\d{1,2}:\d{2}|[a-z]{3,}", plain(" ".join((item.what, item.payload, item.quote)))))
                if len(tokens & named) >= 2 or any(":" in t for t in tokens & named):
                    return Gate(True, verbs=frozenset({"toggle"}))
        return self._refuse({"toggle"}, f"Refused: switching {element.label[:40]!r} changes something the request "
                                        "does not name. Choose an action that does what the user asked.")

    def _refuse(self, verbs, reason):
        self.events.append({"event": "refused", "reason": reason[:200]})
        return Gate(False, reason=reason, verbs=frozenset(verbs))

    @staticmethod
    def _app_ok(item, app):
        return not item.app or not app or plain(item.app) == plain(app)

    def _composer_text(self, elements, chain_typed, payload):
        """Text the agent typed this task (or earlier in this chain) that an editable field on screen holds."""
        if chain_typed:
            return chain_typed[-1]
        if payload and any(_overlaps(payload, t) for t in self.typed_texts()):
            return payload
        for element in elements:
            value = getattr(element, "value", "") or ""
            if getattr(element, "editable", False) and len(value.strip()) >= 2:
                for typed in reversed(self.typed_texts()):
                    if _overlaps(value, typed):
                        return value
        return None

    @staticmethod
    def distinctive(item):
        """Tokens that name the commit's object: quoted names, else capitalized words of ``what``."""
        names = quoted(item.what)
        if names:
            return [plain(n) for n in names]
        words = re.findall(r"(?<![\w'])([A-Z][\w&.-]+)", item.what)
        skip = {plain(item.app)} | GENERIC
        return [plain(w) for w in words if plain(w) not in skip]

    def _object_on_screen(self, item, elements, typed=()):
        """The commit's object is named on screen, or is one the agent is making (a new alarm labeled
        'Gym' it typed the label of)."""
        tokens = self.distinctive(item)
        if not tokens:
            return True
        texts = [plain(element_text(e)) for e in elements] + [plain(t) for t in typed]
        return any(token in text for token in tokens for text in texts)

    def _beside_object(self, item, element, elements):
        """The control names its object itself, or is the App Store button nearest a row that names it (within
        an app row's height). Near is not enough: a compact list puts a Get on every row, .09 of the screen
        apart, and the row above's Get is as near the name as its own; the approval would name one app while
        the tap installed another. A tie is refused."""
        tokens = self.distinctive(item)
        if not tokens or any(token in plain(element.label) for token in tokens):
            return True
        buttons = [e for e in elements if e is not element and store_verbs(e.label)] + [element]

        def gap(button, name):
            return (round(abs(_center_y(button) - _center_y(name)), 3),
                    round(abs(button.rect[0] + button.rect[2] / 2 - name.rect[0] - name.rect[2] / 2), 3))
        for other in elements:
            if (other is element or other.rect[3] > .25 or abs(_center_y(other) - _center_y(element)) > .12
                    or not any(token in plain(element_text(other)) for token in tokens)):
                continue
            if min(buttons, key=lambda button: gap(button, other)) is element:
                return True
        return False

    def committed(self, gate, element, before, app, step):
        """A declared commit ran: wait for its receipt on the screens that follow."""
        if gate.item is None:
            return
        gate.item.executions += 1
        elements = list(getattr(before, "elements", ()) or ())
        rows = frozenset(plain(element_text(e)) for e in elements)
        composer, hits = "", 0
        if gate.item.act in MESSAGE_ACTS and gate.payload:
            key = message_key(gate.payload)
            field_ = element if getattr(element, "editable", False) else next(
                (e for e in elements if getattr(e, "editable", False) and _overlaps(getattr(e, "value", "") or "",
                                                                                   gate.payload)), None)
            composer = plain(field_.label) if field_ is not None and field_.label else ""
            hits = sum(1 for e in elements if key and key in plain(element_text(e))
                       and not (getattr(e, "editable", False) and (not composer
                                                                    or plain(element_text(e)).startswith(composer))))
        self.pending = [p for p in self.pending if p.item is not gate.item or gate.item.needed() is None
                        and p.payload != gate.payload]
        self.pending.append(Pending(gate.item, (element.label or "")[:80], app, step, rows, gate.payload,
                                    gate.amount, gate.names, screen_title(before), composer, hits))

    # ------------------------------------------------ receipts per item

    def _seen(self, predicate, app=None, until=None):
        for step, screen_app, rows, keyboard in self.screens:
            if until is not None and step > until:
                break
            if app and plain(screen_app) != plain(app):
                continue
            for text, editable, role in rows:
                if predicate(text, editable, keyboard):
                    return step
        return None

    def proof(self, item):
        """(proven, reason when not). FORBID is always proven."""
        if item.kind == "FORBID":
            return True, ""
        if item.kind == "COMMIT":
            if item.receipts:
                return True, ""
            if item.claim in ("missing", "done") and (item.condition or item.needed() is None):
                return True, ""  # a condition not met, or no object left for an "each"
            if item.executions:
                return False, "done but not shown: open the screen that shows it (the sent message, the confirmation)"
            if item.claim == "done" and item.act not in MESSAGE_ACTS and (
                    not item.app or any(plain(item.app) == plain(a) for a in self.visited)):
                # Its control named no commit word ("Add to Wishlist"), so no receipt was awaited: the
                # object must still be on a screen of its app. Not a message: its Send is always a commit
                # word, and a chat that already holds the same words from before proves nothing (27 Sep:
                # a declined draft's twin in the history was taken for the message sent, and nothing was).
                tokens = self.distinctive(item)
                if not tokens or self._seen(lambda t, e, k: any(x in t for x in tokens), item.app) is not None:
                    return True, ""
            return False, "not done yet"
        if item.kind == "WRITE":
            return self._write_proof(item)
        if item.kind == "READ":
            return self._read_proof(item)
        return self._report_proof(item)

    def _write_proof(self, item):
        typed = [t for t in self.typed if not t["search"] and not t.get("picker") and self._app_ok(item, t["app"])]
        # A message body is proven by the message's own receipt: the sent bubble may be a text view, which never
        # counts as shown while the keyboard is up (O2).
        texts = [item.payload] if item.payload else [t["text"] for t in typed]
        if any(other.kind == "COMMIT" and other.act in MESSAGE_ACTS and self._app_ok(item, other.app)
               and any(receipt.get("payload") and _overlaps(receipt["payload"], text)
                       for receipt in other.receipts for text in texts if text) for other in self.items):
            return True, ""
        keys = []
        if item.payload:
            keys = [(plain(item.payload), min((t["step"] for t in typed if _overlaps(item.payload, t["text"])),
                                              default=0))]
        else:
            keys = [(plain(next((line for line in t["text"].split("\n") if line.strip()), ""))[:40], t["step"])
                    for t in typed if len(t["text"].strip()) >= 3]
        if not keys:
            if item.claim == "done" and (not item.app or item.app in self.visited):
                return True, ""
            return False, "nothing typed for it yet" if not item.payload else f"\"{item.payload}\" not entered yet"
        for key, since in keys:
            for step, app, rows, keyboard in self.screens:
                if step < since or not self._app_ok(item, app):
                    continue
                if any(key and _shows(key, text) and (not editable or not keyboard) for text, editable, _ in rows):
                    return True, ""
        return False, "not read back on screen: save it and show the saved text"

    def _read_proof(self, item):
        if item.app and not any(plain(item.app) == plain(app) for app in self.visited):
            return False, f"{item.app} not opened yet"
        names = [plain(n) for n in quoted(item.what)]
        folder = next((f for f in FOLDERS if re.search(rf"\b{f}\b", plain(item.what))), None)
        if folder:
            names = [folder]
        for name in names:
            if self._seen(lambda text, e, k: (text == name or text.startswith(name + " ") or name in text
                                              and folder is None), item.app) is None:
                return False, f"'{name}' not seen in {item.app or 'any app'}"
        return True, ""

    def _report_proof(self, item):
        what = item.what + " " + item.quote
        if CONFIRM_ITEM.search(item.what):
            related = [i for i in self.items if i.kind in ("COMMIT", "WRITE") and (
                not item.app or not i.app or plain(i.app) == plain(item.app))]
            # "confirm the job was saved" is about the save, not the reply in the same app
            words = set(re.findall(r"[a-z]{4,}", plain(item.what))) - CONFIRM_WORDS
            named = [i for i in related if words & set(re.findall(r"[a-z]{4,}", plain(i.what + " " + i.act + " " + i.quote)))]
            related = named or related
            unproven = [i for i in related if not self.proof(i)[0] and not i.unproven]
            return (not unproven), ("" if not unproven else "its action is not shown yet: " +
                                    ", ".join(f"item {i.id}" for i in unproven))
        if item.status == "open":
            return False, "not reported yet: read it on screen and mark it found with its value (or missing)"
        if item.status == "missing":
            if item.app and not any(plain(item.app) == plain(app) for app in self.visited):
                return False, f"marked missing but {item.app} was never opened"
            return True, ""
        value = item.value or ""
        if NONE_VALUE.match(plain(value)):
            proof = [s for s in self.empty_searches if self._app_ok(item, s[1])]
            if not proof and self._seen(lambda t, e, k: bool(EMPTY_STATE.match(t) or NO_RESULTS.search(t)),
                                        item.app or None, item.value_step) is not None:
                return True, ""  # the app said so itself: "No Events", "No Results", an empty list
            return (bool(proof), "" if proof else "\"none\" needs proof: open the screen that shows it is empty "
                                                  "(or search for it so the empty result is on screen)")
        if COMPUTED_ITEM.search(what):
            return True, ""
        until = item.value_step
        atoms = ATOM.findall(value)
        if atoms:
            missing = [a for a in atoms if self._seen_atom(a, until) is None]
            if not missing:
                return True, ""
            return False, (f"{', '.join(missing[:3])} not seen on any screen before you recorded it: read it where "
                           "it is shown")
        phrase = plain(value)
        words = re.findall(r"[a-z]{4,}", phrase)
        if len(phrase.split()) > 6 or not words:
            return True, ""  # prose the agent wrote from what it read: no single source to point at
        if self._seen(lambda t, e, k: phrase in t or all(w in t for w in words), None, until) is not None:
            return True, ""
        long_words = [w for w in words if len(w) >= 5] or words
        pool = " ".join(t for step, _, rows, _ in self.screens if until is None or step <= until for t, _, _ in rows)
        if sum(w in pool for w in long_words) >= max(1, len(long_words) / 2):
            return True, ""  # a name said in full ("Seattle Seahawks") where the screen shows part of it
        return False, f"\"{value[:60]}\" not seen on any screen before you recorded it: read it where it is shown"

    def _seen_atom(self, atom, until):
        if atom.startswith("$") or re.fullmatch(r"\d[\d,]*(?:\.\d+)?", atom):
            number = _float(atom.lstrip("$").strip())
            if number is None:
                return None
            return self._seen(lambda t, e, k: any(abs((_float(n) or -1e18) - number) < .005
                                                  for n in NUMBER.findall(t)), None, until)
        key = plain(atom)
        return self._seen(lambda t, e, k: key in t, None, until)

    # ------------------------------------------------ DONE

    def done_gate(self, answer, turns_left):
        """(accepted, feedback, answer). Every item needs its receipt; with LOW_TURNS or fewer turns
        left the answer is accepted and names what is unproven."""
        open_ = []
        for item in self.items:
            proven, reason = self.proof(item)
            if not proven and not item.unproven:
                open_.append((item, reason))
        if open_ and turns_left > LOW_TURNS:
            lines = []
            for item, reason in open_:
                item.reopens += 1
                item.reason = reason
                if item.reopens > MAX_REOPENS:
                    item.unproven = True
                lines.append(f"{item.id}. {item.label()} -- {reason}")
            if all(item.unproven for item, _ in open_):
                return True, "", self.final_answer(answer)
            return False, ("DONE refused: not shown on screen yet:\n" + "\n".join(lines) +
                           "\nDo these now (or mark a REPORT missing if it is shown nowhere), then DONE again."), None
        for item, reason in open_:
            item.unproven, item.reason = True, reason
        return True, "", self.final_answer(answer)

    def unproven(self):
        return [item for item in self.items if item.unproven and not self.proof(item)[0]]

    def final_answer(self, answer):
        """The answer, with found values it leaves out appended from the ledger, and unproven items named."""
        answer = (answer or "").strip()
        text = plain(answer)
        extra = []
        for item in self.items:
            if item.kind != "REPORT" or item.status != "found" or not item.value or CONFIRM_ITEM.search(item.what):
                continue
            atoms = ATOM.findall(item.value)
            present = all(plain(a) in text or a.lstrip("$") in text for a in atoms) if atoms else plain(item.value) in text
            if not present:
                extra.append(f"- {item.what}: {item.value}")
        # Actions the screen never confirmed are named (the user must know); a reported value stays as
        # given, since a doubt added to a right answer costs more than it tells.
        unproven = [item for item in self.unproven() if item.kind in ("COMMIT", "WRITE")]
        if unproven:
            extra.append("Not confirmed on screen: " + "; ".join(item.what for item in unproven) + ".")
        return (answer + ("\n\n" if answer and extra else "") + "\n".join(extra)).strip()

    def summary(self):
        return {"items": len(self.items), "dropped": len(self.dropped),
                "commits": sum(i.kind == "COMMIT" for i in self.items),
                "receipted": sum(bool(i.receipts) for i in self.items),
                "unproven": [i.id for i in self.unproven()]}


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
NAME = re.compile(r"^(?:[A-Z][\w'’.&-]*)(?: [A-Z][\w'’.&-]*){0,2}$")


def recipient(item):
    """The person or place a commit is for, when ``what`` names one ("message to Sam" -> "Sam", "Sam" ->
    "Sam"), else None."""
    what = " ".join(str(item.what or "").split())
    found = re.search(r"\b(?:to|for|with)\s+((?:[A-Z][\w'’.&-]*)(?: [A-Z][\w'’.&-]*){0,2})", what)
    if found:
        return found.group(1)
    names = quoted(what)
    if names:
        return names[0]
    return what if NAME.fullmatch(what) and plain(what) not in GENERIC else None


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
    ``shown``: who the screen says a message goes to (its thread's title). The title names that party, and says
    so when it is not the one the request named: "Send this message to +1 (555) 564-8583 (Kate Bell in your
    request)?" (O2)."""
    act, who = item.act, recipient(item)
    money = f"${amount:,.2f}" if isinstance(amount, (int, float)) else None
    to = f" to {who}" if who else ""
    if act in ("send_message", "reply"):
        noun = "reply" if act == "reply" else "message"
        if shown and who and not same_party(shown, who):
            return f"Send this {noun} to {shown} ({who} in your request)?"
        if shown:
            return f"Send this {noun} to {shown}?"
        return f"Send this {noun}{to}?"
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
