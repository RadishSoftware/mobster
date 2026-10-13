"""Proof-gated completion (frontier Bet 1): the task contract, the declared-commit policy, the source
ledger and the DONE gate in code.

One text-only call (reasoning none, run while the first screen is read) compiles the request into typed
items: READ a source, WRITE a value, COMMIT an act other people see or that spends, REPORT a detail,
FORBID an act the request rules out. Code then does three jobs the prompt used to do:

- Commits pass only when declared. A tap or long press on a control whose first words name a commit
  ("Send", "Pay $42.00", "Place Order"), or Return in a message composer, passes only when every commit
  verb in it belongs to the act family of an open COMMIT item of the current app, or is the first commit
  word of that item's verbatim quote ("Request a CityRideX" allows "Request" whatever act it compiled
  to); its amount must meet
  the item's bounds ("under $50"); a message needs text the agent typed in a field on screen (a feed's
  share "Send" has none); an object commit (Follow) needs its object's name on screen. A COMMIT whose
  quote is not verbatim in the request is dropped. F1 replay (26 Sep, 88 runs): the regex guard refused
  63 required commits; this policy allows all 63 and refuses every unrequested one.
- Receipts close items: a sent message shows in a non-editable row and has left the composer; a booking,
  order, payment or follow shows a new confirmation row; a write reads back in its app on a screen recorded
  after this run first wrote or committed there (typing, a picker, a tap on a control that names an act), with
  numbers as whole tokens, or the model cites the row that showed it already so ("already: <row>"); a write that adds
  with nothing typed for it needs a new row per object it names; a reported value
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
from types import SimpleNamespace

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
UI_WORDS = frozenset({"view", "views", "controller", "vc", "bar", "navbar", "navigationbar", "toolbar", "tabbar", "field",
                      "textfield", "textview", "button", "btn", "cell", "label", "header", "footer", "screen",
                      "container", "sheet", "modal", "input", "window", "panel", "identifier", "nav"})
# The first part of a bundle identifier ("com.apple.MobileSMS").
BUNDLE_ROOTS = frozenset({"com", "org", "net", "io", "app", "co", "dev", "me", "ai", "us", "uk", "de", "fr", "jp"})
# A To field's token that is not a party yet: Messages' "No Name, Searching" while it looks the name up.
UNRESOLVED = re.compile(r"\b(?:no name|searching|loading)\b", re.I)
# Controls that sit in a To field's row and name no one.
TO_ROW_CONTROLS = frozenset({"add", "add contact", "contact add", "add recipient", "add recipients", "to", "to:",
                             "cc/bcc, from:", "cc", "bcc", "clear", "remove", "more", "info"})

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
    names = {" ".join(e.label.split()).casefold() for e in snapshot.elements if e.role in _TITLE_ROLES and e.label.strip()}
    bundle = (snapshot.bundle_id or "").rsplit(".", 1)[-1]
    if bundle:
        names.add(bundle.casefold())
    try:
        from . import harness_api
        catalog = harness_api.hook("app_catalog")  # None in the open-source engine: no app names beyond the bundle's
        apps = catalog() if catalog is not None else ()
        names.update(" ".join(app["name"].split()).casefold() for app in apps if app.get("bundleId") == snapshot.bundle_id)
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
        if (element.editable or not label or len(label) > 40 or " ".join(label.split()).casefold() in generic or internal_name(label)
                or not top <= y + h / 2 <= bottom or y >= above or element.role in {"Image", "Icon", "Key", "Switch", "Toggle"}
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


def _squash(text):
    return re.sub(r"[^0-9a-z]", "", plain(text))


def name_match(row, name):
    """How a row's name matches the name a request gave: "exact" (the same words, case and spacing aside), "close" (a
    handle made of it, "jordanlee" for "Jordan Lee"; a longer or shorter name holding it; a shared word), or None."""
    r, n = plain(row), plain(name)
    if not r or not n:
        return None
    if r == n:
        return "exact"
    rs, ns = _squash(row), _squash(name)
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


def _shows(key, text):
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


# ---------------------------------------------------------------- compile

SYSTEM = """Compile a phone agent's task contract from the user's request, before any work starts. Code enforces \
the contract: the agent may only perform the commits you declare, and the task counts as done only when every \
item has on-screen proof.

Item kinds:
- READ: every app, folder, list, record or tab the request names as a place to look, check or review (one item \
per source).
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
    yields no items: the agent then runs without one, under the regex guard (frontier.guard)."""
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
        # A long task's notebook (longform/notebook.py): its REPORT items are proven by what the notebook holds,
        # since the report is written from it. None for every other run.
        self.notebook = None
        # app -> the number of screens recorded when this run first wrote or committed there: a WRITE is proven only
        # by a screen recorded after that (a value already on screen before the run touched the app proves nothing).
        self.first_writes = {}
        # (step, app, rows): the rows of each screen that sit in a list (a Cell, or inside a Cell's frame). A saved
        # location, contact or alarm is a row of its app's list; a page's own title is not.
        self.listed = []

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
            elif item.kind in ("READ", "WRITE") and self.proof(item)[0]:
                # Proven from the screens already seen: shown as such, or the model reopens the source "to verify"
                # (two turns, ~9.5 s, in a 9 Oct run: a READ the search row had already proven stayed "[ ]").
                mark = "[seen on screen]" if item.kind == "READ" else "[done: shown on screen]"
            else:
                mark = "[ ]"
            out.append(f"{item.id}. {mark} {item.label()}" + (f" -- {item.reason}" if item.reason and
                                                                 item.status not in ("closed",)
                                                                 and not mark.startswith(("[seen", "[done")) else ""))
        return out

    HEADER = ("Task contract (code enforces it: a commit it does not list is refused, and DONE is accepted only "
              "when the screens you saw show every item. In checklist_updates mark REPORT items found with the "
              "exact value as shown, or missing; mark other items done when you finish them. Sent messages, "
              "confirmations and saved text are checked on screen. A WRITE that was already so before you acted: mark "
              "it done with value \"already: \" and the exact row that shows it):")

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
                cited = ALREADY.match(value or "") if status == "done" and item.kind == "WRITE" else None
                if cited:
                    item.already = (step, plain(cited.group(1)), False)
                elif status == "done" and item.kind == "WRITE" and value:
                    item.already = (step, plain(value), True)  # a bare value counts only as a whole row
            changed.append(item)
        return changed

    # ------------------------------------------------ the ledger

    def _wrote(self, app):
        self.first_writes.setdefault(plain(app), len(self.screens))

    def record_action(self, app, operation, label):
        """A dispatched action: typing, a picker or a tap on a control that names an act is a write in ``app``."""
        if operation in WRITE_OPS or operation in ("TAP", "LONG_PRESS", "TAP_XY") and ACT_LABEL.search(label or ""):
            self._wrote(app)

    def record_typed(self, app, element, text, step):
        if text and str(text).strip() and not (element is not None and is_search_field(element)):
            self._wrote(app)
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
            listed = list_rows(elements, rows)
            if listed:
                self.listed.append((step, app, listed))
        self.visited.add(app)
        texts = frozenset(t for t, _, _ in rows)
        for pending in list(self.pending):
            receipt = self._receipt(pending, app, rows, texts, title, elements)
            if receipt:
                self._close(pending, receipt, step, originals, title)
        self._empty_search(elements, app, step)

    def _receipt(self, pending, app, rows, texts, title=None, elements=()):
        if app != pending.app:
            return None
        act = pending.item.act
        if act in STATE_ACTS:
            return self._state_receipt(pending, elements)
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
        patterns = receipt_patterns(act, pending.verbs)
        if patterns:
            hit = next((t for t in new if any(pattern.search(t) for pattern in patterns)), None)
            return f"shown: {hit[:120]}" if hit else None
        if act in MESSAGE_ACTS:
            return None  # a message without its typed text has no receipt to look for
        return "screen changed" if texts != pending.before else None

    @staticmethod
    def _state_receipt(pending, elements):
        """A like's or a follow's receipt: the control the agent tapped, where it was, now on (selected, its words for
        on, a value turned on) when it was not before, or the count on or beside it up by one. A change anywhere else
        (a banner closing, a page opening) is none."""
        before = pending.control
        if before is None:
            return None
        on = STATE_ON.get(pending.item.act)
        was_on = bool(on and (on.search(before.label) or on.search(before.value)))
        for e in same_control(before, elements):
            label, value = " ".join((e.label or "").split()), " ".join((getattr(e, "value", "") or "").split())
            if getattr(e, "selected", False) and not before.selected:
                return f"turned on: {label[:100] or e.role}"
            if on and not was_on and (on.search(label) or on.search(value)):
                return f"shown: {label[:100]}"
            if before.value.casefold() in OFF_VALUES and value.casefold() in ON_VALUES:
                return f"turned on: {label[:100] or e.role}"
        if before.count is not None:
            controls = same_control(before, elements)
            after = shown_count(controls[0] if controls else None, elements, before.rect)
            if after == before.count + 1:
                return f"count: {before.count} -> {after}"
        return None

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
        picked = self._recipient_pick(operation, element, elements, app, text)
        if picked is not None:
            return picked
        if operation in ("TAP", "LONG_PRESS"):
            verbs = label_verbs(element.label, element.role)
            if not verbs and deletes(element.label, element.role, getattr(element, "rect", None)):
                verbs = {"delete"}  # a row's swipe action named Trash
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
            if item.act in STATE_ACTS and is_on(item.act, element):
                # A heart already on: a tap would take the like back, which the request never asked for.
                reasons.append(f"{label_head(element.label) or element.role!r} already shows it on (selected, or its "
                               "words say so); tapping it would undo it")
                continue
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
            if bind and item.act in MESSAGE_ACTS and verbs <= MESSAGE and self._made_address(item, elements):
                token, who = self._made_address(item, elements)
                reasons.append(f"the To field holds {token!r}, an address made of the name, not the contact {who!r}: "
                               f"remove it and pick {who!r} from the suggestions")
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

    def _recipient_names(self, app):
        """The names the open message commits in ``app`` go to, as the request gave them ("Jordan Lee")."""
        return [who for who in (recipient(i) for i in self.items if i.kind == "COMMIT" and i.act in MESSAGE_ACTS
                                and i.status != "closed" and self._app_ok(i, app)) if who]

    def _recipient_pick(self, operation, element, elements, app, text=None):
        """A refusal while choosing who a message goes to, else None. MobsterBench Real, 12 Oct: the contact search
        showed nothing yet, Return in the To field made the typed name an address of its own ("benchtester"), and
        the message was about to go there. By names and roles only:
        - Return in a To field holding a typed name (not a phone number or an email) is refused: pick the row.
        - Among suggestions or results for a typed name, a row that isn't the request's name exactly is refused
          when another row is; with no exact row and two or more close ones, the agent asks instead of guessing."""
        names = self._recipient_names(app)
        if not names:
            return None
        if operation in ("SUBMIT", "TYPE_SUBMIT") and getattr(element, "editable", False) and TO_FIELD.match(
                element.label or ""):
            typed = " ".join(str(text).split()) if operation == "TYPE_SUBMIT" and text else typed_query(element)
            if typed and not ADDRESS.match(typed):
                who = max(names, key=lambda n: name_match(typed, n) is not None)
                return self._refuse({"address"}, f"Refused: Return in {element.label.strip()!r} would make {typed!r} "
                                                 f"an address of its own, not the contact {who!r}. TAP {who!r} in the "
                                                 "suggestions, or type the phone number or email the request gives.")
            return None
        if operation != "TAP" or getattr(element, "editable", False):
            return None
        queries = [typed_query(e) for e in elements if getattr(e, "editable", False)]
        for who in names:
            if not any(q and name_match(q, who) for q in queries):
                continue  # no field holds this name typed: not a pick among suggestions for it
            mine = name_match(row_name(element.label), who)
            if mine is None or mine == "exact":
                continue
            fields = [e for e in elements if getattr(e, "editable", False)]
            rows = [e for e in elements if e.role in ROW_ROLES and not getattr(e, "editable", False) and not any(
                _inside(f.rect, e.rect[0] + e.rect[2] / 2, e.rect[1] + e.rect[3] / 2) for f in fields)]
            exact = next((e for e in rows if name_match(row_name(e.label), who) == "exact"), None)
            if exact is not None:
                return self._refuse({"recipient"}, f"Refused: {row_name(element.label)!r} is not {who!r}; "
                                                   f"{row_name(exact.label)!r} is. TAP that row.")
            close = list(dict.fromkeys(row_name(e.label) for e in rows if name_match(row_name(e.label), who)))
            if len(close) >= 2:
                return self._refuse({"recipient"}, f"Refused: no row is exactly {who!r}, and "
                                                   f"{' and '.join(repr(c) for c in close[:3])} are all close. Ask the "
                                                   "person which one they mean (ASK_USER), or finish with BLOCKED; "
                                                   "don't guess.")
        return None

    def _made_address(self, item, elements):
        """(token, name) when the To field holds the request's name squashed into an address ("jordanlee" for "Jordan
        Lee"): Return made the typed name a recipient of its own. Else None."""
        who = recipient(item)
        to = addressed_to(SimpleNamespace(elements=elements)) if who else None
        for token in (to or "").split(", "):
            if token and plain(token) != plain(who) and " " not in token.strip() and " " in who.strip() \
                    and _squash(token) == _squash(who):
                return token, who
        return None

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
        self._wrote(app)
        return self._committed(gate, element, before, app, step)

    def _committed(self, gate, element, before, app, step):
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
        control = control_state(element, elements) if gate.item.act in STATE_ACTS else None
        self.pending.append(Pending(gate.item, (element.label or "")[:80], app, step, rows, gate.payload,
                                    gate.amount, gate.names, screen_title(before), composer, hits, gate.verbs,
                                    control))

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
            state = item.act in STATE_ACTS
            if item.claim in ("missing", "done") and (item.condition or item.needed() is None) and (
                    not state or item.claim == "missing"):
                return True, ""  # a condition not met, or no object left for an "each"
            if item.executions and state:
                return False, ("tapped, but the control never showed it on afterwards (selected, \"Liked\" or "
                               "\"Following\", or its count up by one): look at that control again. Tapping it again "
                               "would undo it if it is on")
            if item.executions:
                return False, "done but not shown: open the screen that shows it (the sent message, the confirmation)"
            if state:
                # A like or a follow is proven only by its own control turning on after the agent tapped it: a tap on
                # a point, or a change elsewhere (a banner closing), leaves it open (MobsterBench Real, 11 Oct).
                return False, ("not shown done: a like or a follow counts once the control you TAP turns on (selected, "
                               "\"Liked\" or \"Following\", or its count up by one). If it already shows on, don't tap "
                               "it: that would undo it")
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
        if item.already is not None:
            # The model quoted the row that shows it already so ("already: <row>", or a value that is a whole row):
            # proven when that row was on a screen of its app by then.
            step, row, whole = item.already
            if row and self._seen(lambda text, e, k: row == text or not whole and row in text, item.app,
                                  until=step) is not None:
                return True, ""
            if not whole and self._listed_already(item, step, row):
                return True, ""  # "already: ..." only: a bare value is what the model set, not a citation
        # Only screens recorded after this run first wrote or committed in the item's app prove it.
        apps = [plain(item.app)] if item.app else list(self.first_writes)
        firsts = [self.first_writes[a] for a in apps if a in self.first_writes]
        if not firsts:
            return False, ("nothing entered or set for it yet" + (f" in {item.app}" if item.app else "")
                           + (f": enter \"{item.payload}\"" if item.payload else ""))
        after = min(firsts)
        keys = []
        if item.payload:
            keys = [(plain(item.payload), min((t["step"] for t in typed if _overlaps(item.payload, t["text"])),
                                              default=0))]
        else:
            keys = [(plain(next((line for line in t["text"].split("\n") if line.strip()), ""))[:40], t["step"])
                    for t in typed if len(t["text"].strip()) >= 3]
        if not keys:
            if item.claim != "done":
                return False, "nothing typed for it yet" if not item.payload else f"\"{item.payload}\" not entered yet"
            # Nothing typed for it: a write that adds (log, add, create) counts only once each object it names has a
            # new row in its app. A row from before proves nothing (iOSWorld dev, 12 Oct: runs logged a shake, took
            # the oatmeal row already in the log for the oatmeal, and ended done). A change of state or a picker keeps
            # the model's "done".
            missing = [] if not ADDS.search(item.what + " " + item.quote) or any(
                t.get("picker") and self._app_ok(item, t["app"]) for t in self.typed) else [
                words for words in self._objects(item) if not self._new_row(item, words, after)]
            return (False, f"no new row naming {' '.join(missing[0])} in {item.app or 'its app'} since you started "
                           "there (a row from before proves nothing): add it, then show it") if missing else (True, "")
        for key, since in keys:
            for index, (step, app, rows, keyboard) in enumerate(self.screens):
                if index < after or step < since or not self._app_ok(item, app):
                    continue
                if any(key and _shows(key, text) and (not editable or not keyboard) for text, editable, _ in rows):
                    return True, ""
        return False, "not read back on screen after it was entered: save it and show the saved text"

    def _objects(self, item):
        """The objects a WRITE names, in the request's words, one list per part split at "and", "&" and commas:
        "protein shake and oatmeal breakfast log" -> [["post-run", "protein", "shake"], ["oatmeal", "breakfast"]]."""
        asked, app = plain(self.request), set(plain(item.app).split())
        parts = ([w for w in re.findall(r"[a-z][a-z'-]{3,}", part) if w in asked and w not in OBJECT_STOP | app]
                 for part in re.split(r"\s*(?:,|&|\band\b)\s*", plain(item.what)))
        return [words for words in parts if words]

    def _new_row(self, item, words, after):
        """More rows name one of ``words`` on a screen of the item's app from ``after`` on than on any before it. A
        screen holding a search the run typed shows what was looked for, not what exists."""
        searches = [plain(t["text"])[:20] for t in self.typed if t["search"]]
        counts = {False: 0, True: 0}
        for index, (_, app, rows, _) in enumerate(self.screens):
            if self._app_ok(item, app) and not any(e and any(q in text for q in searches) for text, e, _ in rows):
                count = max(sum(not e and w in text for text, e, _ in rows) for w in words)
                counts[index >= after] = max(counts[index >= after], count)
        return counts[True] > counts[False]

    def _listed_already(self, item, until, cited):
        """A WRITE the model marked as already so, citing its value but not a row: proven when that value is a whole
        row of a list in the item's app ("New York" in its saved locations), on a screen seen by then and before
        this run typed anything in that app (a list of search results shows what was looked for, not what exists).
        ab1, 11 Oct: "already: New York row in My Locations (59° Mostly Cloudy)" matched no row, though the list
        showed New York; a forecast page titled "New York" is no list row, and proves nothing."""
        value = plain(item.payload)
        if not value or value not in (cited or ""):
            return False
        for step, app, listed in self.listed:
            if step > until:
                break
            if not self._app_ok(item, app) or item.app and plain(app) != plain(item.app):
                continue
            if any(t["step"] <= step and plain(t["app"]) == plain(app) for t in self.typed):
                continue
            if any(text == value or text.startswith(value + ",") for text in listed):
                return True
        return False

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
        book = getattr(self, "notebook", None)
        if book is not None and item.status in ("open", "found") and getattr(book, "kind", None) in NOTEBOOK_KINDS:
            # A long read (feed, survey, browse): its summary item ("what you learned", "the most promising") is the
            # report, written from the kept items (DESIGN §11). Any other value is proven when an item's own text
            # holds it (never the model's notes), else on the screens as for any run. A chain keeps screen proof.
            if SUMMARY_ITEM.search(item.what + " " + item.quote) and book.counters.get("kept"):
                return True, ""
            if item.status == "found" and self._in_notebook(item.value):
                return True, ""
        if item.status == "open":
            return False, "not reported yet: read it on screen and mark it found with its value (or missing)"
        if item.status == "missing":
            if item.app and not any(plain(item.app) == plain(app) for app in self.visited):
                return False, f"marked missing but {item.app} was never opened"
            # An absence is a finding too: proven like a "none" (dev runs, 10-12 Oct: "Terminal not shown; gate E13"
            # after the boarding pass alone, while the trip's own page showed "Terminal 2, Gate E13").
            if self._absence_shown(item):
                return True, ""
            return False, ("marked missing, but no screen showed it absent: look where it would be (open the row "
                           "for its own page, another tab, the app's search) and mark it found, or leave on screen "
                           "the empty search or the app's \"No ...\" that proves it")
        value = item.value or ""
        if NONE_VALUE.match(plain(value)):
            proof = self._absence_shown(item)
            return (proof, "" if proof else "\"none\" needs proof: open the screen that shows it is empty "
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

    def _absence_shown(self, item):
        """Whether a screen of the item's app showed it absent by the time it was marked: a search the run typed that
        came back empty, or the app's own "No Results", "No Events", an empty list."""
        if any(self._app_ok(item, s[1]) for s in self.empty_searches):
            return True
        return self._seen(lambda t, e, k: bool(EMPTY_STATE.match(t) or NO_RESULTS.search(t)),
                          item.app or None, item.value_step) is not None

    def _in_notebook(self, value):
        """Whether the long task's kept items (what they showed: creator, summary, text) hold ``value``: its atoms,
        else its words. The model's own notes don't count: they are what it says, not what a screen showed."""
        book = getattr(self, "notebook", None)
        if book is None or not value:
            return False
        texts = []
        with book.lock:
            for entry in book.items:
                if entry.skip is not None:
                    continue
                texts += [entry.who(), entry.summary(), *(str(v) for v in (entry.text or {}).values()
                                                         if isinstance(v, str))]
        pool = plain(" ".join(t for t in texts if t))
        atoms = ATOM.findall(value)
        if atoms:
            return all(plain(atom) in pool or atom.lstrip("$") in pool for atom in atoms)
        words = re.findall(r"[a-z]{4,}", plain(value))
        return bool(words) and sum(w in pool for w in words) >= max(1, len(words) / 2)

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
                "reports": sum(i.kind == "REPORT" for i in self.items),
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
    "Sam"), else None. Never a developer's name for a view (internal_name)."""
    what = " ".join(str(item.what or "").split())
    found = re.search(r"\b(?:to|for|with)\s+((?:[A-Z][\w'’.&-]*)(?: [A-Z][\w'’.&-]*){0,2})", what)
    names = quoted(what)
    who = found.group(1) if found else names[0] if names else (
        what if NAME.fullmatch(what) and plain(what) not in GENERIC else None)
    return None if internal_name(who) else who


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
CONTAINER_ROLES = frozenset({"Other", "Window", "Application", "ScrollView", "Table", "CollectionView", "WebView", "Map",
                             "Group", "LayoutArea", "LayoutItem", "NavigationBar", "TabBar", "Toolbar", "Keyboard",
                             "Sheet", "Alert", "Popover"})
# What a point tap may do, named on its card ("Tap at this spot — it may send a message to Sam?"), by act.
ACT_PHRASES = {"send_message": "send a message", "post": "post", "comment": "post a comment", "reply": "send a reply",
               "react": "like or react", "pay": "pay", "request_money": "request money", "transfer": "transfer money",
               "order": "place an order", "book": "book", "follow": "follow", "unfollow": "unfollow", "share": "share",
               "call": "place a call", "delete": "delete", "cancel": "cancel", "install": "install an app",
               "other": "do something that needs your OK"}
# A row's trailing quarter is where its own controls sit (a heart, a Follow, a switch): a point there is not the row.
ROW_TRAILING = .25


def _inside(rect, x, y):
    ex, ey, ew, eh = rect
    return ex <= x <= ex + ew and ey <= y <= ey + eh


def point_spot(snapshot, x, y):
    """(kind, element) for a tap at (x, y) among the listed elements, read from where the point is, never from what the
    model called it. kind is "commit" (inside a control whose words commit: element), "nav" (inside a plainly
    navigational control: a tab, a back button, a list row with no commit verb, its trailing quarter left out), "plain"
    (inside a listed element that names no commit) or None (inside no listed control: an unlisted heart). Containers
    (a scroll view, a table, a bar) and anything as large as half the screen are no control."""
    elements = list(getattr(snapshot, "elements", ()) or ())
    inside = [e for e in elements if _inside(e.rect, x, y) and e.role not in CONTAINER_ROLES
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
            b.role == "TabBar" and _inside(b.rect, ex + ew / 2, ey + eh / 2) for b in elements):
        return "nav", innermost
    if innermost.role == "Button" and (plain(innermost.label) == "back" or any(
            b.role == "NavigationBar" and _inside(b.rect, ex + ew / 2, ey + eh / 2)
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


def act_phrase(item):
    """What a commit does, in a few words, for a point tap's card: "like first post in feed", "follow Netflix", "send a
    message to Sam". The verb is the request's own when it uses one of the act's words."""
    what = " ".join(str(item.what or "").split())
    if item.act in MESSAGE_ACTS or item.act in ("pay", "transfer"):
        who = recipient(item)
        return ACT_PHRASES[item.act] + (f" to {who}" if who else "")
    quote = plain(item.quote)
    found = sorted((m.start(), m.group(0)) for w in item.family() for m in [re.search(rf"\b{re.escape(w)}\b", quote)] if m)
    if found and what and item.act != "other":
        return f"{found[0][1]} {what}"
    return ACT_PHRASES.get(item.act, ACT_PHRASES["other"])


def point_ask(contract, request, snapshot, x, y):
    """(title, act) when a tap at (x, y) asks first, else None. A point tap is never bound to a listed element, so what
    the model calls it unlocks nothing: inside a plainly navigational control it goes ahead; while the task has an
    open commit that asks, it asks, naming that act ("Tap at this spot — it may like first post in feed?"); anywhere
    no listed control is, it asks too. MobsterBench Real, 11 Oct: Instagram drew the heart outside the tree, a point
    tap liked a post with Ask before acting on, and no card was shown."""
    kind, _ = point_spot(snapshot, x, y)
    if kind == "nav":
        return None
    items = open_commits(contract, request)
    if items:
        phrases = list(dict.fromkeys(act_phrase(i) for i in items))[:2]
        return f"Tap at this spot — it may {' or '.join(phrases)}?", items[0].act or "other"
    if kind is None:
        return f"Tap at this spot — it may {ACT_PHRASES['other']}?", "other"
    return None
