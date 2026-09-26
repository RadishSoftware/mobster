"""Route compiler: the navigation a request spells out, executed without a model call per hop.

"In Settings > General > About, report ..." and "open Privacy & Security, then
Location Services" name their screens. Measured on MobsterBench-iOS pass 1
(24 Sep): Jev chose WAIT at 0.2 confidence on the Settings root when the named
row was below the fold, then detoured through Settings search and ran out of
helper calls; 9 of 25 fixture-free failures were this.

A compiled route is a list of hops (row names). Each step it answers from the
screen alone:
  * the screen's title (its NavigationBar) says how far the route has come;
  * the next hop visible as a row -> TAP it (exact row name, never a switch);
  * not visible on a list screen -> SWIPE_UP, a bounded number of times;
  * anything else (finished, lost, exhausted) -> None: Jev decides as before.

Route actions go through the agent's ordinary action path (effect ledger,
monitor, observation, no-op detection); only the choice skips the model.
"""

import re
from dataclasses import dataclass, field

from .keypad import CLEAR_KEYS, calculator_keys

# Roles a navigation row is published as. Switches, sliders and text fields never are.
ROW_ROLES = ("Cell", "Button", "Link", "StaticText")
# Swipes spent looking for one hop on one screen before the route hands over to Jev.
MAX_SEARCH_SWIPES = 5
# Swipes spent bringing the asked-about row into view once the route has arrived.
MAX_REVEAL_SWIPES = 4
# A screen is a scrollable list worth searching when it shows at least this many rows.
LIST_MIN_ROWS = 4

_CONTACT_OF = re.compile(r"\b(?i:find|open|view)\s+(?P<name>(?:[A-Z][\w-]*\s+){0,3}[A-Z][\w-]*)['’]s\s+"
                         r"contact\b")
_VERB = re.compile(r"\b(?:open|go to|navigate to|switch to|show the contents of)\s+", re.I)
_END = re.compile(r"\s*(?:[.;:(]|,(?!\s*then\b)|\band\b|\bto\b\s|\bfor\b\s|\bthen\b\s+(?:report|read|use|search)).*$",
                  re.I)
# "the contact Bench Tester", "the list named X", "the note titled 'X'": the noun is not the row's name.
# Only after "the", so a row that starts with one ("Location Services") keeps its name.
_GENERIC = re.compile(r"^the\s+(?:(?:contact|list|note|album|event|folder|tab|screen|section|location|"
                      r"page|menu|option|setting|settings pane)\s+(?:(?:named|titled|called)\s+)?)?", re.I)
_TRAIL = re.compile(r"\s+(?:screen|tab|location|page|menu|section|pane|list|folder)$", re.I)
_REJECT = re.compile(r"(?:^(?:her|his|its|their|that|this|it|a|an|each|every|any|all)\b|\bin\s|/|"
                     r"\.[a-z]{2,}\b|\bwikipedia\b|\barticle\b|\blink\b|\bweb\b)", re.I)


def norm(text):
    text = (text or "").replace("’", "'").replace(" ", " ")
    return re.sub(r"\s+", " ", text).strip().strip("'\"").casefold()


def row_name(label):
    """A row's name without the summary iOS appends ("Wi-Fi, Home" is the Wi-Fi row)."""
    return norm((label or "").split(",")[0])


def _clean(hop):
    hop = hop.strip().strip(",")
    hop = _GENERIC.sub("", hop, count=1).strip()
    quoted = re.fullmatch(r"['\"‘“](.+?)['\"’”]", hop)
    if quoted:
        return quoted.group(1).strip()
    hop = _TRAIL.sub("", hop).strip()
    if not hop or len(hop.split()) > 5 or _REJECT.search(hop):
        return None
    return hop


def request_hops(goal, app_name=""):
    """The screens a request names, in order; [] when it names none it can be trusted on."""
    framed = re.match(r"^\s*In ([^:]{1,40}):\s*", goal or "")  # the product's "In <App>: " frame
    if framed:
        app_name = app_name or framed.group(1)
        goal = goal[framed.end():]
    goal = goal or ""
    hops = []
    clause = next((c for c in re.split(r"[.,;:(](?=\s|$)", goal) if ">" in c), None)
    if clause is not None:
        clause = re.split(r"\s+(?:and|then)\s+", clause.strip(), maxsplit=1)[0]
        clause = re.sub(r"^(?:.*?\b(?:in|open|go to|navigate to|head to|visit)\s+)", "", clause, count=1, flags=re.I)
        hops = [part.strip() for part in clause.split(">")]
    else:
        match = _VERB.search(goal)
        if match:
            rest = goal[match.end():]
            if re.match(r"\S+\.\S", rest):
                return []  # an address, not a screen (the URL path handles it)
            hops = re.split(r",?\s+then\s+(?:open\s+)?", _END.sub("", rest), flags=re.I)
    if not hops:
        # "Find Bench Tester's contact and get the city": the contact's own row (a plan step's
        # wording; diag-15, multi.contact_state tapped it at 0.86 and the verifier stalled).
        owner = _CONTACT_OF.search(goal)
        if owner:
            hops = [owner.group("name")]
    if not hops:
        # "what is the Auto-Lock time set to (under Display & Brightness)?"
        under = re.search(r"\(\s*(?:it is\s+)?(?:under|in)\s+([^)]{2,60})\)", goal)
        if under:
            hops = [part.strip() for part in re.split(r"\s*>\s*|,\s*then\s+", under.group(1))]
    cleaned = []
    for hop in hops:
        hop = _clean(hop)
        if hop is None:
            return []  # one untrustworthy hop makes the whole route untrustworthy
        cleaned.append(hop)
    app = norm(app_name)
    if cleaned and app and norm(cleaned[0]) == app:
        cleaned = cleaned[1:]
    return cleaned[:6]


_STOP_WORDS = frozenset(norm(word) for word in (
    "In", "Answer", "Do", "Report", "Then", "Open", "Only", "If", "Use", "Search", "Find", "The", "What",
    "Which", "Is", "How", "Count", "According", "Scroll", "Show", "Switch", "Go", "Tap", "View", "This",
    "Wikipedia", "iPhone", "On", "Off"))
_PHRASE = re.compile(r"(?<![\w'])([A-Z][\w&-]*(?:\s+(?:&\s+)?(?:[A-Z]|i[A-Z])[\w&-]*)*)")


def request_targets(goal, hops=(), app_name=""):
    """Row names a request asks about on its final screen ("is 'Set Automatically' on",
    "report the Modem Firmware version"): quoted names first, then Title Case phrases
    that do not start a sentence. Parenthetical negatives ("(not Available)") are skipped."""
    framed = re.match(r"^\s*In ([^:]{1,40}):\s*", goal or "")
    if framed:
        app_name = app_name or framed.group(1)
        goal = goal[framed.end():]
    goal = re.sub(r"\((?:not|e\.g\.|for example)[^)]*\)", " ", goal or "", flags=re.I)
    skip = {norm(hop) for hop in hops} | {norm(app_name)} | _STOP_WORDS | _app_names()
    found = []
    for quoted in re.findall(r"(?<!\w)['\u2018\"]([^'\u2019\"]{3,40})['\u2019\"](?!\w)", goal):
        if norm(quoted) not in skip and norm(quoted) not in ("on", "off", "yes", "no"):
            found.append(quoted.strip())
    for match in _PHRASE.finditer(goal):
        before = goal[:match.start()].rstrip()
        if not before or before[-1] in ".?!:>,":
            continue  # sentence-initial or a breadcrumb/list position: a verb or a hop, not a row
        phrase = match.group(1).strip()
        words = phrase.split()
        while len(words) > 1 and norm(words[-1]) in _STOP_WORDS:
            words.pop()  # "Model Name Do (not change ...)" is the row "Model Name"
        phrase = " ".join(words)
        if (norm(phrase) in skip or len(phrase) < 3 or phrase in found or "_" in phrase
                or any(norm(hop).startswith(norm(phrase)) for hop in hops)
                or any(norm(phrase) in norm(quoted) for quoted in found)):
            continue
        found.append(phrase)
    return found[:4]


def _app_names():
    try:
        from .catalog import APPS
    except ImportError:
        return frozenset()
    return frozenset(norm(app["name"]) for app in APPS)


def target_visible(snapshot, name):
    """The asked-about row or phrase is on screen: a row by that name, or the phrase inside a
    label (a Notes search result "MobsterBench Note, 2:05 AM, Locker code: 4817")."""
    wanted = norm(name)
    pattern = re.compile(r"(?<![\w])" + re.escape(wanted) + r"(?![\w])")
    return any(row_name(e.label) == wanted or pattern.search(norm(e.label))
               for e in snapshot.elements if e.role not in ("SearchField", "TextField"))


_IDENTIFIER = re.compile(r"^[A-Za-z_][\w.]*$")
# iOS 26 titles that open a menu read "On My iPhone, Actions Menu".
_MENU_SUFFIX = re.compile(r",\s*Actions Menu$", re.I)


def screen_title(snapshot):
    """The screen's title: the navigation bar's label, or when that is a class name
    ("FullDocumentManagerViewControllerNavigationBar" in Files) the text inside the bar."""
    bars = [e for e in snapshot.elements if e.role == "NavigationBar" and e.label.strip()]
    if not bars:
        return None
    bar = bars[0]
    if not (_IDENTIFIER.match(bar.label.strip()) and len(bar.label.strip()) > 20):
        return norm(_MENU_SUFFIX.sub("", bar.label))
    x, y, w, h = bar.rect
    # The title is the centred text of the bar's top row; in Files it is a menu Button
    # ("sunglasses12, Actions Menu"), and the leading Back button is never it.
    inside = [e for e in snapshot.elements if e.role in ("StaticText", "Button") and e.label.strip()
              and y <= e.rect[1] + e.rect[3] / 2 <= y + min(h, .07) and x + .2 * w <= e.rect[0] + e.rect[2] / 2
              <= x + .8 * w]
    if not inside:
        return None
    centred = min(inside, key=lambda e: abs(e.rect[0] + e.rect[2] / 2 - (x + w / 2)))
    return norm(_MENU_SUFFIX.sub("", centred.label))


def visible_rows(snapshot, name):
    wanted = norm(name)
    rows = [e for e in snapshot.elements
            if e.role in ROW_ROLES and "TAP" in e.actions and row_name(e.label) == wanted]
    # A cell or button over its own text; the larger one is the row.
    return sorted(rows, key=lambda e: (ROW_ROLES.index(e.role), -e.rect[2] * e.rect[3]))


def is_list_screen(snapshot):
    rows = {row_name(e.label) for e in snapshot.elements if e.role in ("Cell", "Button") and e.label.strip()}
    return len(rows) >= LIST_MIN_ROWS


@dataclass
class Route:
    hops: list
    reveal: list = field(default_factory=list)
    next: int = 0
    swipes: int = 0
    title: str | None = None
    failed: str | None = None

    revealed: bool = False
    search: str | None = None   # text the request says to search for ("use search to find ... 'Locker code'")
    searched: set = field(default_factory=set)
    keys: list | None = None    # a keypad press sequence (keypad.py), pressed once the hops are done
    key_index: int = 0
    cleared: bool = False
    pressing: str | bool = False   # "clear" or "key" while a keypad press awaits its screen
    typed: str | None = None    # the text of the route's last TYPE_SUBMIT
    resent: bool = False

    @property
    def active(self):
        return self.failed is None and (self.next < len(self.hops) or self._keying or self._revealing)

    @property
    def _keying(self):
        return self.keys is not None and self.next >= len(self.hops) and self.key_index < len(self.keys)

    def _key_step(self, snapshot):
        keys = [e for e in snapshot.elements if e.role in ("Key", "Button")]
        if not self.cleared:
            self.cleared = True
            clear = next((e for name in CLEAR_KEYS for e in keys if e.label == name), None)
            if clear is not None:
                self.pressing = "clear"
                return "TAP", clear
        wanted = self.keys[self.key_index]
        key = next((e for e in keys if e.label == wanted), None)
        if key is None:
            self.failed = "key_missing"
            return None
        self.pressing = "key"
        return "TAP", key

    @property
    def _revealing(self):
        return bool(self.reveal) and not self.revealed and self.next >= len(self.hops)

    def step(self, snapshot):
        """('TAP', element) | ('SWIPE_UP', None) | ('TYPE_SUBMIT', field, text) | None; advances what the screen proves."""
        if self.search and self.search not in self.searched and self.failed is None:
            field_ = search_field(snapshot)
            if field_ is not None:
                self.searched.add(self.search)
                self.typed = self.search
                return "TYPE_SUBMIT", field_, self.search
        if not self.active:
            return None
        title = screen_title(snapshot)
        # The title is the route's odometer: the deepest hop it names is done.
        for index in range(len(self.hops) - 1, -1, -1):
            if title is not None and title == norm(self.hops[index]) and index >= self.next - 1:
                self.next = index + 1
                break
        if title != self.title:
            self.title, self.swipes = title, 0
        if not self.active:
            return None
        if self._keying:
            return self._key_step(snapshot)
        if self._revealing:
            # The named screen is open; bring the row the request asks about into view.
            if any(target_visible(snapshot, name) for name in self.reveal):
                self.revealed = True
                return None
            if snapshot_has_title(snapshot) and self.swipes < MAX_REVEAL_SWIPES and is_list_screen(snapshot):
                self.swipes += 1
                return "SWIPE_UP", None
            self.revealed = True  # failed is already None: the route is active
            return None
        rows = visible_rows(snapshot, self.hops[self.next])
        while rows and (rows[0].value or "").strip() == "1" and rows[0].role == "Button":
            # A selected tab or segment ("Stopwatch" already showing): that hop is taken.
            self.next += 1
            if not self.active:
                return None
            rows = visible_rows(snapshot, self.hops[self.next])
        if rows:
            return "TAP", rows[0]
        if self.swipes < MAX_SEARCH_SWIPES and is_list_screen(snapshot):
            self.swipes += 1
            return "SWIPE_UP", None
        hop = self.hops[self.next]
        field_ = search_field(snapshot)
        if field_ is not None and hop not in self.searched:
            # Not in the list: ask the app's own search for the row by its name (a note, a contact).
            self.searched.add(hop)
            self.swipes = MAX_SEARCH_SWIPES
            self.typed = hop
            return "TYPE_SUBMIT", field_, hop
        self.failed = "not_found" if self.swipes else "not_a_list"
        return None

    def not_sent(self):
        """The route's last search was refused before dispatch (a stale screen): offer it once more."""
        if self.typed is not None and not self.resent:
            self.searched.discard(self.typed)
            self.resent = True

    def tapped(self, changed):
        """After a route TAP: a screen change is the hop taken even when the new screen has no title.
        No change hands the screen back to Jev; the same tap is never repeated."""
        if self.pressing:
            # A key press is input: the next key follows whether or not this one redrew
            # the display (Clear on an empty display changes nothing).
            if self.pressing == "key":
                self.key_index += 1
            self.pressing = False
            return
        if changed and self.active:
            self.next += 1
            self.swipes = 0
        elif not changed:
            self.failed = "tap_no_effect"

    def swiped(self, changed):
        if not changed and self.active:
            if self._revealing:
                self.revealed = True  # the end of the list: the row is not on this screen
            else:
                # The end of the list: the next step searches for the hop if the app can,
                # and gives up only if it cannot.
                self.swipes = MAX_SEARCH_SWIPES


def search_field(snapshot):
    fields = [e for e in snapshot.elements if e.role == "SearchField" and e.editable and "TYPE_SUBMIT" in e.actions]
    return fields[0] if fields else None


_SEARCH_TEXT = re.compile(r"\b(?:use\s+(?:the\s+)?search\s+(?:field\s+)?to\s+find|search\s+for|search)\b[^'\"\u2018\u201c]{0,60}?"
                          r"['\"\u2018\u201c]([^'\"\u2019\u201d]{2,60})['\"\u2019\u201d]", re.I)
_SEARCH_PLAIN = re.compile(r"\bsearch\s+for\s+([A-Z][\w'&.-]*(?:\s+[A-Z][\w'&.-]*){0,4})\b")


def request_search_text(goal):
    """The text a request says to search an app for ("use search to find the note containing
    'Locker code'", "search for Bench"); web searches are the URL path's, not this."""
    goal = goal or ""
    if re.search(r"\bsearch\s+(?:the\s+web|online|the\s+internet|google)\b", goal, re.I):
        return None
    match = _SEARCH_TEXT.search(goal) or _SEARCH_PLAIN.search(goal)
    return match.group(1).strip() if match else None


def snapshot_has_title(snapshot):
    return screen_title(snapshot) is not None


def compile_route(goal):
    """The request's route, or None. A question with no named screens still gets its rows
    revealed on the screen it starts on (a short search: the start screen was not chosen)."""
    hops = request_hops(goal)
    keys = calculator_keys(goal)
    if keys:
        return Route(hops if not all("calculator" in norm(hop) for hop in hops) else [], keys=keys)
    targets = request_targets(goal, hops)
    search = request_search_text(goal)
    if search:
        return Route(hops, reveal=targets, search=search)
    if hops:
        return Route(hops, reveal=targets)
    if targets and "?" in (goal or ""):
        return Route([], reveal=targets, swipes=MAX_REVEAL_SWIPES - 2)
    return None
