"""The pack framework: what a pack declares, and the code that recognizes screens, navigates and reads with it."""

import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

from .. import harness_api, hints
from . import pins

# The confidence at which a pack is sure which screen it is on; below it the general agent takes the step.
SURE = hints.SURE  # one threshold for every source of hints (hints.py)
# A tie closer than this between the two best screens is no answer.
MARGIN = .1
SETTLE_SECONDS = .6
BACK_LABEL = re.compile(r"^\s*(back|close|dismiss|cancel|‹|<|chevron[ .]?left|navigate up)\s*$", re.I)
# An act's own control, by its whole label: never tapped by a pack, whatever the step says.
ACT_LABEL = re.compile(r"^(like|liked|unlike|follow|follow back|following|unfollow|requested|send|send message|share|"
                       r"repost|post|post comment|reply|save|save post|delete|remove|block|report|mute|restrict|"
                       r"subscribe|buy|pay|order|book|accept|decline|approve|unsend|add to story|go live|"
                       r"share to story|done|submit|confirm)$", re.I)
# Words a pack never taps in a control it doesn't know only opens a screen.
NEVER = re.compile(r"\b(like|likes|liked|unlike|follow|unfollow|send|share|repost|post|delete|remove|block|report|"
                   r"restrict|mute|subscribe|buy|pay|order|book|accept|decline|approve|unsend|save post|"
                   r"add to story|go live)\b", re.I)


class Lost(Exception):
    """Navigation or a read couldn't go on: the message says where it stopped, in plain words."""


class PinnedAside(Lost):
    """The team pinned this pack to another version, or turned it off (pins.py): the pack steps aside."""


class Refused(Lost):
    """A tap or a keystroke the pack may not make (it names an act, or the caller's check said no)."""


@dataclass(frozen=True)
class Signal:
    pattern: re.Pattern
    role: Optional[str] = None              # the element's role ("Button"), or any
    region: Optional[tuple] = None          # (top, bottom): screen fractions the element's centre lies between
    selected: Optional[bool] = None         # True: the element is the current tab (its Selected trait or value "1")
    weight: float = 1.0
    must: bool = False                      # the screen is never this one without it
    never: bool = False                     # the screen is never this one with it
    field: str = "label"                    # "label", "value", or "text" (any line of the screen's text)
    column: Optional[tuple] = None          # (left, right): screen fractions the element's centre lies between


def sig(pattern, role=None, *, region=None, selected=None, weight=1.0, must=False, never=False, field="label",
        column=None, flags=re.I):
    """A ``Signal``; ``pattern`` is a regex string, searched."""
    return Signal(re.compile(pattern, flags), role, region, selected, weight, must, never, field, column)


@dataclass(frozen=True)
class ScreenSpec:
    name: str                               # "dm_inbox"
    title: str                              # "DM inbox": how a person names it
    signals: tuple
    evidence: str = "inferred"              # "recorded": checked on a tree recorded from the real app
    # The same screen as an image-only connection reads it (iPhone Mirroring, USB video: perception.py): only the
    # words drawn on screen, named icons as Buttons, the current tab when the pixels show it. No identifiers, no
    # accessibility names of icons that aren't drawn. A screen without them is never named on such a read.
    ocr: tuple = ()


@dataclass(frozen=True)
class Seen:
    screen: Optional[str]                   # the screen's name, or None when the pack isn't sure
    confidence: float
    title: str = ""
    app: str = ""                           # the bundle id in front
    matched: tuple = ()                     # the signals that matched, as their patterns (tests, logs)
    runner_up: Optional[str] = None
    evidence: str = ""                      # the matched spec's: "recorded", "inferred" or "learned" (heal.py)

    @property
    def sure(self):
        return self.screen is not None


def is_selected(element):
    return bool(getattr(element, "selected", False)) or str(getattr(element, "value", "") or "").strip() == "1"


def centre_y(element):
    x, y, w, h = element.rect
    return y + h / 2


# Connections that read the screen from pictures (perception.py): their elements' locators start "ocr:" or "icon:".
IMAGE_SOURCES = frozenset({"mirroring", "usb_video", "usb_avf"})


def image_only(snapshot):
    """Whether ``snapshot`` was read from a picture, with no accessibility tree."""
    if getattr(snapshot, "source", "") in IMAGE_SOURCES:
        return True
    elements = getattr(snapshot, "elements", ()) or ()
    return bool(elements) and all(str(e.locator).startswith(("ocr:", "icon:")) for e in elements)


def hits(signal, snapshot):
    """The elements (or text lines) ``signal`` matches on ``snapshot``."""
    if signal.field == "text":
        return [line for line in str(getattr(snapshot, "text", "") or "").split("\n") if signal.pattern.search(line)]
    out = []
    for element in getattr(snapshot, "elements", ()) or ():
        if signal.role and element.role != signal.role:
            continue
        if signal.region and not signal.region[0] <= centre_y(element) <= signal.region[1]:
            continue
        if signal.column and not signal.column[0] <= element.rect[0] + element.rect[2] / 2 <= signal.column[1]:
            continue
        if signal.selected is not None and is_selected(element) != signal.selected:
            continue
        words = element.value if signal.field == "value" else element.label
        if words and signal.pattern.search(words):
            out.append(element)
    return out


def score(spec, snapshot, signals=None):
    """(confidence 0..1, matched patterns) of ``spec`` on ``snapshot``: its tree signals, or its ``ocr`` ones on a
    picture-read screen (``image_only``)."""
    if signals is None:
        signals = spec.ocr if image_only(snapshot) else spec.signals
    total = got = 0.0
    matched = []
    for signal in signals:
        found = bool(hits(signal, snapshot))
        if signal.never:
            if found:
                return 0.0, ()
            continue
        if signal.must and not found:
            return 0.0, ()
        total += signal.weight
        if found:
            got += signal.weight
            matched.append(signal.pattern.pattern)
    return (got / total if total else 0.0), tuple(matched)


def recognize(pack, snapshot):
    """Which of ``pack``'s screens ``snapshot`` shows (``Seen``)."""
    app = str(getattr(snapshot, "bundle_id", "") or "")
    if app and app != pack.bundle:
        return Seen(None, 0.0, app=app)
    pictured = image_only(snapshot)
    specs = [spec for spec in (*pack.screens, *pack.learned_specs()) if spec.ocr or not pictured]
    best_of = {}
    for spec in specs:
        (value, matched) = score(spec, snapshot)
        if spec.name not in best_of or value > best_of[spec.name][0]:
            best_of[spec.name] = (value, matched, spec)
    ranked = sorted(best_of.values(), key=lambda entry: -entry[0])
    if not ranked:
        return Seen(None, 0.0, app=app)
    best, matched, spec = ranked[0]
    second = ranked[1][0] if len(ranked) > 1 else 0.0
    runner = ranked[1][2].name if len(ranked) > 1 and second > 0 else None
    if best < SURE or best - second < MARGIN and second >= SURE:
        return Seen(None, round(best, 2), app=app, matched=matched, runner_up=spec.name)
    return Seen(spec.name, round(best, 2), spec.title, app, matched, runner, spec.evidence)


def find_all(snapshot, pattern, role=None, region=None, selected=None, roles=None, field_="label"):
    """Every element whose label (or value) matches ``pattern`` (a regex or a string, searched case-insensitively)."""
    rx = pattern if isinstance(pattern, re.Pattern) else re.compile(pattern, re.I)
    out = []
    for element in getattr(snapshot, "elements", ()) or ():
        if role and element.role != role or roles and element.role not in roles:
            continue
        if region and not region[0] <= centre_y(element) <= region[1]:
            continue
        if selected is not None and is_selected(element) != selected:
            continue
        words = element.value if field_ == "value" else element.label
        if words and rx.search(words):
            out.append(element)
    return out


def find(snapshot, pattern, **options):
    found = find_all(snapshot, pattern, **options)
    return found[0] if found else None


def plain(text):
    return " ".join(str(text or "").split())


def rows(snapshot, gap=.022):
    """What a reader walks: the elements, or on a picture-read screen its lines joined into rows (a list row's name and
    its preview line, read as two lines, become one element labelled "name, preview, time")."""
    if not image_only(snapshot):
        return list(getattr(snapshot, "elements", ()) or ())
    from ..state import Element
    lines = sorted((e for e in snapshot.elements if e.role != "Button" or not str(e.locator).startswith("icon:")),
                   key=lambda e: (e.rect[1], e.rect[0]))
    groups = []
    for e in lines:
        last = groups[-1] if groups else None
        if last and e.rect[1] - (last[-1].rect[1] + last[-1].rect[3]) <= gap and abs(e.rect[0] - last[0].rect[0]) < .08:
            last.append(e)
        else:
            groups.append([e])
    out = []
    for index, group in enumerate(groups):
        x = min(e.rect[0] for e in group)
        y = min(e.rect[1] for e in group)
        right = max(e.rect[0] + e.rect[2] for e in group)
        bottom = max(e.rect[1] + e.rect[3] for e in group)
        label = ", ".join(re.sub(r"\s*[·•]\s*", ", ", plain(e.label)) for e in group)[:500]
        out.append(Element(f"r{index}", label, "Cell", (x, y, max(right - x, 1e-3), max(bottom - y, 1e-3)),
                           locator=group[0].locator))
    return out


@dataclass(frozen=True)
class Tap:
    """Tap the first control matching ``pattern``."""
    pattern: str
    what: str
    role: Optional[str] = None
    region: Optional[tuple] = None
    opens: bool = False
    optional: bool = False
    wait: float = 0.0                       # extra seconds a slow screen takes to fill after the tap


@dataclass(frozen=True)
class TypeIn:
    """Type the route's argument into the field matching ``pattern`` (a search field only)."""
    pattern: str
    what: str
    submit: bool = False
    text: Optional[str] = None              # fixed words to type instead of the route's argument ("About")


@dataclass(frozen=True)
class PickRow:
    """Tap the row whose words hold the route's argument (a DM thread by name, a search result by handle)."""
    what: str
    roles: tuple = ("Cell", "Button", "StaticText", "Other", "Link")
    region: tuple = (.1, .9)
    exact_handle: bool = False              # the argument is a handle: match it as a whole word
    template: object = None                 # a regex with {arg} (escaped) a row's words must match instead, or a
                                            # tuple of them, tried in order
    scrolls: int = 0                        # swipes up to look further down the list when the row isn't showing
    text: Optional[str] = None              # fixed words the row holds instead of the route's argument


@dataclass(frozen=True)
class Control:
    """An act's control (``Pack.controls``): the route that stops at it, the pattern that finds it on the screen (with
    its ``role`` and ``region`` when the pattern alone is loose) and the ``title`` the tools call it."""
    route: str
    control: str
    title: str
    role: Optional[str] = None
    region: Optional[tuple] = None


@dataclass(frozen=True)
class Reader:
    """What one ``Pack.readers`` entry reads: ``read(pack, io, snapshot, n, arg)`` -> ``ReadResult``, the ``screens`` it
    reads, and the ``route`` that gets there from elsewhere (None: only from where the person is)."""
    read: object
    screens: tuple
    route: Optional[str] = None


@dataclass(frozen=True)
class Route:
    name: str
    title: str                              # "your DMs"
    lands: tuple                            # the screens that mean it arrived
    steps: tuple = ()
    link: Optional[str] = None              # a deep link template ({arg}); tried first when the pack trusts its links
    from_tabs: bool = True                  # the steps start on a screen with the app's tab bar
    arg: Optional[str] = None               # "user", "name", "query" or "url": the argument it needs
    then_control: Optional[str] = None      # an act's control to name once there ("like")


@dataclass
class Landing:
    ok: bool
    route: str
    seen: Seen
    how: str = ""                           # "already" | "link" | "code" | ""
    steps: int = 0
    why: str = ""
    snapshot: object = None
    seconds: float = 0.0
    control: object = None                  # the act's control, when the route names one and it is on screen


class PackIO:
    """The phone, as a pack uses it: reads, settles, and the only taps and keystrokes it makes."""

    def __init__(self, driver, pack, *, permit=None, cancelled=None, allows_app=None, clock=time.monotonic,
                 sleep=time.sleep, deadline=60.0):
        self.driver, self.pack = driver, pack
        self.permit = permit                     # (op, element, snapshot, text) -> None, or raise Refused
        self.cancelled = cancelled or (lambda: False)
        self.allows_app = allows_app or (lambda bundle: True)
        self.clock, self.sleep = clock, sleep
        self.started = clock()
        self.deadline = deadline
        self.taps = 0
        self.screens = []                        # every screen read, in order (the run's contract sees them)

    def left(self):
        return self.deadline - (self.clock() - self.started)

    def check_time(self):
        if self.cancelled():
            raise Lost("the task was stopped")
        if self.left() <= 0:
            raise Lost("it ran out of time")

    def observe(self):
        snapshot = self.driver.observe(timeout=10)
        self.screens.append(snapshot)
        return snapshot

    def settle(self, before, extra=0.0):
        """The screen after an action: the driver's own wait when it has one, else a pause and a read."""
        native = getattr(self.driver, "wait_for_change", None)
        if callable(native):
            try:
                after = native(before, wait_seconds=SETTLE_SECONDS + extra, timeout=SETTLE_SECONDS + extra + 1.5)
                self.screens.append(after)
                return after
            except Exception:
                pass
        self.sleep(SETTLE_SECONDS + extra)
        return self.observe()

    def may_tap(self, label, opens=False, pattern=None):
        """Whether the pack may tap a control with ``label``: never an act's own control or one of the app's ``never``
        words; a control the step knows only ``opens`` a screen, matched in full, may name an act's noun ("Comment")."""
        text = plain(label)
        if not text or ACT_LABEL.match(text) or self.pack.never.search(text):
            return False
        if opens and pattern and re.fullmatch(pattern, text, re.I):
            return True
        if NEVER.search(text):
            return False
        return harness_api.commit_act("TAP", text, None, self.pack.bundle) is None

    def tap(self, element, snapshot, *, opens=False, pattern=None, check=None, extra=0.0):
        """Tap ``element`` (a navigation control); the settled screen after."""
        self.check_time()
        if not self.may_tap(check if check is not None else element.label, opens, pattern):
            raise Refused(f"it won't tap “{plain(element.label)[:60]}”: that control may act")
        if self.permit is not None:
            self.permit("TAP", element, snapshot, None)
        self.driver.execute("TAP", element, snapshot, timeout=10)
        self.taps += 1
        return self.settle(snapshot, extra)

    def type(self, element, snapshot, text, submit=False):
        """Type into a search field (the only field a pack types in)."""
        self.check_time()
        words = plain(f"{element.label} {getattr(element, 'placeholder', '')}")
        if not element.editable or not (element.role == "SearchField" or re.search(r"\bsearch\b", words, re.I)):
            raise Refused("it types only into a search field")
        if self.permit is not None:
            self.permit("TYPE", element, snapshot, text)
        self.driver.execute("TYPE", element, snapshot, text=text, timeout=20)
        after = self.settle(snapshot)
        if submit:
            field_ = find(after, re.escape(element.label), role=element.role) or element
            if self.permit is not None:
                self.permit("SUBMIT", field_, after, None)
            if "SUBMIT" in field_.actions:
                self.driver.execute("SUBMIT", field_, after, timeout=10)
                after = self.settle(after)
        return after

    def swipe(self, snapshot, direction="up"):
        self.check_time()
        op = {"up": "SWIPE_UP", "down": "SWIPE_DOWN"}[direction]
        self.driver.execute(op, None, snapshot, timeout=10)
        return self.settle(snapshot)

    def back(self, snapshot):
        """Tap the screen's Back (or Close) control; the screen after, or None when it has none."""
        control = next((e for e in snapshot.elements if e.role == "Button" and BACK_LABEL.match(e.label or "")), None)
        if control is None:
            control = self.pack.back_control(snapshot)
        if control is None:
            return None
        return self.tap(control, snapshot)

    def launch(self, snapshot):
        self.check_time()
        if not self.allows_app(self.pack.bundle):
            raise Refused(f"{self.pack.name} isn't allowed in this task")
        if self.permit is not None:
            self.permit("LAUNCH_APP", None, snapshot, None)
        self.driver.execute("LAUNCH_APP", self.pack.bundle, snapshot, timeout=20)
        return self.settle(snapshot, extra=.6)

    def open_link(self, url, snapshot):
        from ..deeplinks import open_link
        self.check_time()
        if not self.allows_app(self.pack.bundle):
            raise Refused(f"{self.pack.name} isn't allowed in this task")
        if self.permit is not None:
            self.permit("OPEN_URL", None, snapshot, url)
        open_link(self.driver, url, bundle=self.pack.bundle, clock=self.clock, sleep=self.sleep)
        return self.settle(snapshot, extra=.8)


def _on_tabs(pack, snapshot):
    return bool(pack.tabs is not None and hits(pack.tabs, snapshot))


def to_tabs(pack, io, snapshot):
    """A screen with the app's tab bar: launch the app when another is in front, then Back (or Close)
    up to three times."""
    if str(getattr(snapshot, "bundle_id", "") or "") not in ("", pack.bundle):
        snapshot = io.launch(snapshot)
    for _ in range(3):
        if _on_tabs(pack, snapshot):
            return snapshot
        after = io.back(snapshot)
        if after is None:
            break
        snapshot = after
    if _on_tabs(pack, snapshot):
        return snapshot
    raise Lost(f"it couldn't get back to {pack.name}'s tab bar")


def _run_step(pack, io, step, snapshot, arg):
    if isinstance(step, Tap):
        control = find(snapshot, step.pattern, role=step.role, region=step.region)
        if control is None:
            if step.optional:
                return snapshot
            raise Lost(f"{step.what} isn't on the screen")
        return io.tap(control, snapshot, opens=step.opens, pattern=step.pattern, extra=step.wait)
    if isinstance(step, TypeIn):
        control = next((e for e in find_all(snapshot, step.pattern) if e.editable), None)
        if control is None:
            raise Lost(f"{step.what} isn't on the screen")
        return io.type(control, snapshot, step.text or arg, submit=step.submit)
    if isinstance(step, PickRow):
        arg = step.text or arg
        row = pick_row(snapshot, arg, step, pack)
        for _ in range(step.scrolls if row is None else 0):
            after = io.swipe(snapshot, "up")
            if after.content_fingerprint == snapshot.content_fingerprint:
                break
            snapshot = after
            row = pick_row(snapshot, arg, step, pack)
            if row is not None:
                break
        if row is None:
            raise Lost(f"no {step.what} names “{plain(arg)[:40]}”")
        # A row is checked by the name it was picked by: its other words are the app's (a last message, "Delete").
        return io.tap(row, snapshot, opens=True, pattern=r".*", check=plain(arg))
    raise ValueError(f"unknown step {step!r}")


def _names_an_act(text, pack):
    return bool(ACT_LABEL.match(text) or NEVER.search(text) or (pack is not None and pack.never.search(text)))


def pick_row(snapshot, arg, step, pack=None):
    """The row ``arg`` names: a whole-word match first, then any match; the largest row wins (a cell over its text)."""
    want = plain(arg).lstrip("@").casefold()
    if not want:
        return None
    candidates = [e for e in snapshot.elements if e.role in step.roles and e.label
                  and step.region[0] <= centre_y(e) <= step.region[1]
                  and (e.role == "Cell" or not _names_an_act(plain(e.label), pack))]
    if step.template:
        for template in (step.template,) if isinstance(step.template, str) else step.template:
            rx = re.compile(template.replace("{arg}", re.escape(want)), re.I)
            found = [e for e in candidates if rx.search(plain(e.label))]
            if found:
                return found[0]
        return None
    word = re.compile(r"(?<![\w.])@?" + re.escape(want) + r"(?![\w])", re.I)
    whole = [e for e in candidates if word.search(e.label)]
    loose = [] if step.exact_handle else [e for e in candidates if want in e.label.casefold()]
    for group in (whole, loose):
        if group:
            return max(group, key=lambda e: e.rect[2] * e.rect[3])
    return None


def navigate(pack, io, name, arg=None):
    """Go to ``pack``'s route ``name`` (with its argument): ``Landing``."""
    started = io.clock()
    route = pack.routes.get(name)
    if route is None:
        raise ValueError(f"{pack.name} has no route {name!r}")
    if route.arg and not plain(arg):
        return Landing(False, name, Seen(None, 0.0), why=f"it needs a {route.arg}", seconds=0.0)
    snapshot, how, steps = None, "", 0
    try:
        snapshot = io.observe()
        seen = recognize(pack, snapshot)
        if seen.screen in route.lands and not route.arg:
            return _landed(pack, route, seen, snapshot, "already", 0, io, started, arg)
        if route.link and pack.links_on and callable(getattr(io.driver, "call", None)):
            # A connection with no link opener (iPhone Mirroring) goes straight to the steps.
            url = link_for(pack, route, arg)
            if url:
                try:
                    snapshot = io.open_link(url, snapshot)
                except Refused:
                    raise
                except Exception:  # noqa: BLE001 -- a link that won't open is a route by taps, never a failed step
                    snapshot = io.observe()
                steps += 1
                seen = recognize(pack, snapshot)
                if seen.screen in route.lands:
                    return _landed(pack, route, seen, snapshot, "link", steps, io, started, arg)
        if route.from_tabs:
            snapshot = to_tabs(pack, io, snapshot)
        for step in route.steps:
            before = io.taps
            snapshot = _run_step(pack, io, step, snapshot, arg)
            steps += max(1, io.taps - before)
        seen = recognize(pack, snapshot)
        if seen.screen in route.lands:
            return _landed(pack, route, seen, snapshot, "code", steps, io, started, arg)
        return Landing(False, name, seen, "code", steps, _unsure(pack, seen, route), snapshot,
                       round(io.clock() - started, 2))
    except Lost as lost:
        seen = recognize(pack, snapshot) if snapshot is not None else Seen(None, 0.0)
        return Landing(False, name, seen, how, steps, str(lost), snapshot, round(io.clock() - started, 2))


def link_for(pack, route, arg):
    """The route's deep link with its argument, or None."""
    if route.arg == "url":
        url = plain(arg)
        host = re.match(r"^https://([^/?#]+)/", url + "/")
        if not (host and host.group(1).casefold() in pack.sites and len(url) < 500):
            return None
        return url if pack.link_paths.match(urlsplit(url).path) else None
    from ..deeplinks import Link
    try:
        return Link("pack", pack.name, pack.bundle, route.title, route.link).fill(arg=(arg or "").lstrip("@"))
    except KeyError:
        return None


def _landed(pack, route, seen, snapshot, how, steps, io, started, arg=None):
    why = pack.confirm(route.name, arg, snapshot)
    if why:
        # On the right kind of screen but not the one asked for (another conversation): never name an act's control.
        return Landing(False, route.name, seen, how, steps, why, snapshot, round(io.clock() - started, 2))
    landing = Landing(True, route.name, seen, how, steps, "", snapshot, round(io.clock() - started, 2))
    if route.then_control:
        landing.control = control_for(pack, route.then_control, snapshot)
    return landing


def _unsure(pack, seen, route):
    wanted = " or ".join(next((s.title for s in pack.screens if s.name == name), name) for name in route.lands)
    if seen.screen:
        return f"it reached {seen.title}, not {wanted}"
    return f"it couldn't tell it reached {wanted}"


def control_for(pack, act, snapshot):
    """The control for ``act`` on ``snapshot`` (``Pack.controls``), or None."""
    spec = pack.controls.get(act)
    if spec is None:
        return None
    return find(snapshot, spec.control, role=spec.role, region=spec.region)


@dataclass
class ReadResult:
    kind: str                               # "dms", "reels" ...
    title: str                              # "Your 10 newest DMs"
    columns: tuple                          # ((key, label, type), ...) for the table card
    rows: list = field(default_factory=list)
    stopped: str = ""                       # why it read fewer than asked ("end of the list"), or ""
    block: str = "table"                    # "table" or "media_list"
    screen: Optional[str] = None            # the screen it read on


def collect(io, snapshot, parse, key, n, *, max_swipes=8, direction="up"):
    """Rows ``parse(snapshot)`` gives, across scrolls, de-duplicated by ``key(row)``, until ``n`` or the end."""
    rows, keys = [], set()
    for swipe in range(max_swipes + 1):
        for row in parse(snapshot):
            k = key(row)
            if k in keys:
                continue
            keys.add(k)
            rows.append(row)
        if len(rows) >= n:
            return rows[:n], "", snapshot
        if swipe == max_swipes:
            return rows, "it scrolled as far as it reads in one go", snapshot
        try:
            after = io.swipe(snapshot, direction)
        except Lost as lost:
            return rows, str(lost), snapshot
        if after.content_fingerprint == snapshot.content_fingerprint:
            return rows, "it reached the end of the list", after
        snapshot = after
    return rows, "", snapshot


@dataclass(frozen=True)
class Verified:
    version: str                            # the app version the recognizers were checked on ("412.0.0")
    date: str                               # "2026-10-14"
    device: str = ""                        # "iPhone 15 Pro, iOS 26.0"


class Pack:
    """One app's pack. Subclasses (instagram.py) fill in the class attributes and the readers."""

    name = ""                               # "Instagram"
    key = ""                                # "instagram": the MCP tools' prefix
    prefix = ""                             # "IG": the agent tools' prefix (IG_OPEN, IG_READ)
    bundle = ""
    verified: Optional[Verified] = None
    screens: tuple = ()
    routes: dict = {}
    controls: dict = {}                     # act -> Control
    readers: dict = {}                      # what -> Reader
    tabs: Optional[Signal] = None           # the tab bar's signal (to_tabs)
    never = re.compile(r"(?!x)x")           # the app's own words never tapped (matches nothing by default)
    links_on = True                         # try a route's deep link before its steps
    sites: tuple = ()                       # hosts whose https links open in the app ("www.instagram.com")
    link_paths = re.compile(r"")            # the paths on those hosts a route may open (a post's, not the home feed's)
    route_with_arg: dict = {}               # a route that takes an argument when one is given ("profile" -> "user")
    mentions = re.compile(r"(?!x)x")        # words in a task that make it about this app
    photos_notice = re.compile(r"(?!x)x")   # the label of the app's notice that it sees only some photos
    # The tools' words (tools.py, mcp.py): each tool's one-line prompt, the words OPEN and READ take, and how a read's
    # rows are named.
    prompts: dict = {}                      # "OPEN" | "READ" -> "- IG_OPEN ..."
    open_aliases: dict = {}                 # a word the model may write -> a route or an act
    read_aliases: dict = {}
    read_default: dict = {}                 # what -> n when the model gives none
    read_titles: dict = {}                  # what -> "DMs"
    required: dict = {}                     # what -> the column keys every row has: a row without one says so
    nouns: dict = {}                        # what -> "Reel": one item, for a card's title
    mcp_open_hint = ""
    mcp_read_hint = ""

    hidden_routes: tuple = ()               # routes the tools reach only through another word ("user" by "profile @x")

    @property
    def open_words(self):
        return [*(r for r in self.routes if r not in self.hidden_routes),
                *(c for c in self.controls if c not in self.routes)]

    @property
    def read_words(self):
        return list(self.readers)

    def noun(self, what):
        return self.nouns.get(what, "Post")

    def recognize(self, snapshot):
        return recognize(self, snapshot)

    def limits_photos(self, snapshot):
        """Whether ``snapshot`` is this app showing its limited-photos notice (a Button or text whose whole label is
        ``photos_notice``): the app can't see most of the library, so a post's media may not be found in its picker."""
        if str(getattr(snapshot, "bundle_id", "") or "") != self.bundle:
            return False
        elements = getattr(snapshot, "elements", ()) or ()
        return any(e.role in ("StaticText", "Button", "Other") and e.label and len(e.label) <= 160
                   and self.photos_notice.search(" ".join(e.label.split())) for e in elements)

    learned = None                          # heal.Learned: variants this Mac learned and checked, or None

    def learned_specs(self):
        """The trusted variants heal.py learned for the app version on the phone (never for an act's control)."""
        store = self.learned
        return store.specs(self) if store is not None else ()

    def back_control(self, snapshot):
        """The app's own Back control when its label isn't "Back" (iOS names it after the screen it goes back to:
        "Settings", "Search"), or None."""
        return None

    def confirm(self, route, arg, snapshot):
        """None when ``snapshot`` is the screen ``route`` with ``arg`` asked for; else why not, in plain words (a
        messaging pack checks the conversation on screen names the person asked for before it names Send)."""
        return None

    def offered_to(self, ctx):
        """Whether this pack's tools are offered to the run ``ctx``: a Smart run that starts in the app or whose task
        names it."""
        if getattr(ctx, "engine", None) != "smart" or pins.aside(self):
            return False
        return (getattr(ctx, "app_bundle", None) == self.bundle
                or bool(self.mentions.search(str(getattr(ctx, "goal", "") or ""))))

    def screen_title(self, name):
        return next((s.title for s in self.screens if s.name == name), name or "")

    def read(self, io, what, n=10, arg=None):
        """``ReadResult`` for ``what``."""
        reader = self.readers[what]
        route = self.route_with_arg.get(reader.route, reader.route) if arg else reader.route
        snapshot = io.observe()
        seen = recognize(self, snapshot)
        goes_to_arg = bool(arg) and route in self.routes and bool(self.routes[route].arg)
        if goes_to_arg or seen.screen not in reader.screens:
            if reader.route is None:
                raise Lost(f"this isn't {' or '.join(self.screen_title(s) for s in reader.screens)}")
            landing = navigate(self, io, route, arg)
            if not landing.ok:
                raise Lost(landing.why)
            snapshot, seen = landing.snapshot, landing.seen
        result = reader.read(self, io, snapshot, n, arg)
        result.screen = result.screen or seen.screen
        return result


def number(text):
    """"2.7M" -> 2700000, "16,767" -> 16767, "1.1K" -> 1100; None when it isn't a count."""
    match = re.fullmatch(r"\s*(\d[\d,]*(?:\.\d+)?)\s*([KkMmBb])?\s*", str(text or ""))
    if not match:
        return None
    value = float(match.group(1).replace(",", "")) * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[
        (match.group(2) or "").upper()]
    return int(round(value))


def app_version(runtime, bundle):
    """The version of ``bundle`` installed on the phone (installed_apps' last listing), or None."""
    installed = getattr(runtime, "installed_apps", None)
    try:
        inventory = installed.snapshot() if installed is not None else None
    except Exception:
        return None
    for app in getattr(inventory, "user", ()) or ():
        if isinstance(app, dict) and app.get("bundleId") == bundle:
            return app.get("version") or None
    return None

