"""The X-ray crawler: explores an app with no model, and every tap, replayed ones too, goes through the policy."""

import base64
import time
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

from ..packs.core import BACK_LABEL
from . import paywall as paywalls
from . import policy, screens

SCHEMA = "mobster.xray/1"
SETTLE_SECONDS = .5
SETTLE_TIMEOUT = 5.0
POLL_SECONDS = .1
THUMB_WIDTH = 300
BOXES_KEPT = 60
STUCK_LIMIT = 3
TOP_STEPS = 6
# A list whose detail pages look alike (fonts, products, contacts) is sampled: after this many look-alike pages, its
# other rows are left closed.
CATALOG_ALIKE = 2
# The depth the first pass stops at; screens there are explored deeper afterwards while the caps allow.
FIRST_PASS_DEPTH = 2
ALIKE = .6
# A read older than this is read again before a tap on it (WebDriverAgent refuses taps on reads over 1 s old).
FRESH_SECONDS = .7
QUIET_ROLES = frozenset({"StaticText", "Image", "Icon", "Other", "NavigationBar", "Toolbar", "TabBar", "Text",
                         "Window", "ScrollView", "Table", "CollectionView", "Keyboard", "Key"})
QUIET_REASONS = ("outside the content area", "under a bar", "not tappable", "no label")


class Stop(Exception):
    """The crawl can't go on: the message says why, in plain words."""


class Gone(Exception):
    """The control to tap is no longer on screen; nothing was tapped. ``snapshot``: the fresh read."""

    def __init__(self, snapshot):
        super().__init__("the control is gone")
        self.snapshot = snapshot


@dataclass
class Caps:
    screens: int = 60
    depth: int = 6
    seconds: float = 300.0
    pages: int = 4            # pages of a long list it scrolls through per screen
    taps: int = 800
    children: int = 8         # new screens opened from one screen two or more taps in (the first two levels: all)


@dataclass
class Screen:
    id: str
    title: str                # the screen's own title (its identity)
    tokens: frozenset
    union: set
    lines: list
    depth: int
    path: list                # [{"label", "kind"}] from the app's first screen
    parent: str = ""
    via: str = ""
    seen_s: float = 0.0
    thumb: str = ""
    boxes: list = field(default_factory=list)
    paywall: object = None
    frame: bytes = None       # the full capture until announce() hands it to the run
    frame_id: str = None      # the run's stored frame of this screen (the result's gallery)
    image_only: bool = False
    done: set = field(default_factory=set)
    refused: dict = field(default_factory=dict)
    noop: list = field(default_factory=list)
    changed: list = field(default_factory=list)   # taps that changed the screen in place: the rest is left alone
    choice: bool = False      # seen as a list of options: its rows are never tapped
    kids: list = field(default_factory=list)       # the identity tokens of each new screen opened from it
    alike: int = 0            # how many of those look like an earlier one (a catalog's detail pages)
    held_back: int = 0        # rows not opened because of the children cap or a catalog
    pending: bool = False     # reached at the first pass's depth with something left to open
    unreachable: bool = False

    @property
    def name(self):
        """What the report calls it: its title, or the row that opens it when the title is missing or a badge."""
        if self.title and not (self.title.isupper() and len(self.title) <= 6):
            return self.title
        return self.via or self.title or "Untitled"

    def to_json(self):
        return {"id": self.id, "title": self.title, "name": self.name, "depth": self.depth, "path": self.path,
                "parent": self.parent,
                "via": self.via, "seen_s": round(self.seen_s, 2), "thumb": self.thumb, "frame_id": self.frame_id,
                "lines": self.lines,
                "tokens": sorted(self.union), "boxes": self.boxes,
                "paywall": self.paywall.to_json() if self.paywall else None,
                "refused": [{"label": label, "reason": reason} for label, reason in self.refused.items()],
                "no_change": self.noop, "changed_in_place": self.changed, "choice_list": self.choice,
                "catalog": self.alike >= CATALOG_ALIKE, "held_back": self.held_back,
                "unreachable": self.unreachable}


@dataclass
class Candidate:
    key: str
    label: str
    element: object
    reason: str


def key_of(label):
    """A control's identity on its screen: its label, case folded, without a trailing state (", Off")."""
    return screens.STATES.sub("", policy.plain(label)).casefold()[:120]


def boxes(snapshot):
    """The screen's layout for a wireframe: [x, y, w, h, kind, text] for its structural elements."""
    out = []
    for element in sorted(snapshot.elements, key=lambda e: (e.rect[1], e.rect[0])):
        if element.role in ("Image", "Icon") or not policy.plain(element.label):
            continue
        kind = ("row" if element.role in ("Cell", "Button") and element.rect[2] > .5 else
                "control" if element.role in ("Button", "Link", "Switch", "Toggle", "SearchField", "TextField")
                else "bar" if element.role in ("NavigationBar", "TabBar", "Toolbar") else "text")
        out.append([round(v, 4) for v in element.rect] + [kind, policy.plain(element.label)[:60]])
        if len(out) >= BOXES_KEPT:
            break
    return out


CHROME = ("XCUIElementTypeToolbar", "XCUIElementTypeTabBar", "XCUIElementTypeKeyboard", "XCUIElementTypeSearchField",
          "XCUIElementTypeNavigationBar")


def more_below(snapshot):
    """Whether content runs past the bottom of the content area (a long list): only then is a scroll worth it."""
    for element in snapshot.elements:
        if element.role in policy.CONTROL_ROLES or any(part in (element.locator or "") for part in CHROME):
            continue
        if element.rect[1] + element.rect[3] > policy.CONTENT_BOTTOM - .02 and element.rect[1] < .995:
            return True
    return False


def state(snapshot):
    """What the screen shows, controls' values and checkmarks included, for telling whether a tap changed it."""
    return frozenset((e.role, policy.plain(e.label), e.value, e.selected) for e in snapshot.elements
                     if e.rect[1] >= screens.STATUS_BAR)


def is_dialog_element(element):
    return element.role in ("Alert", "Sheet") or "XCUIElementTypeAlert" in (element.locator or "")


class _Timer:
    def __init__(self, spent, kind, clock):
        self.spent, self.kind, self.clock = spent, kind, clock

    def __enter__(self):
        self.started = self.clock()

    def __exit__(self, *exc):
        self.spent[self.kind] = self.spent.get(self.kind, 0.0) + self.clock() - self.started


class Crawler:
    def __init__(self, driver, bundle, *, caps=None, out_dir=None, shots=True, clock=time.monotonic,
                 sleep=time.sleep, log=None, emit=None, cancelled=None):
        self.driver, self.bundle = driver, bundle
        self.emit, self.cancelled = emit, cancelled
        self.caps = caps or Caps()
        self.out = Path(out_dir) if out_dir else None
        self.shots = shots and self.out is not None and callable(getattr(driver, "capture_preview", None))
        self.clock, self.sleep = clock, sleep
        self.log = log or (lambda message: None)
        self.screens = []
        self.edges = []
        self.stats = {"taps": 0, "scrolls": 0, "backs": {"control": 0, "close": 0, "swipe": 0, "relaunch": 0},
                      "dialogs_dismissed": 0, "left_app": 0, "no_change": 0, "changed_in_place": 0,
                      "relaunch_failures": 0}
        self.stuck = 0
        self.before = None
        self.limit = self.caps.depth
        self.spent = {}
        self.settles = {}
        self.stopped = ""
        self.started = 0.0

    def timed(self, kind):
        return _Timer(self.spent, kind, self.clock)

    def note(self, event):
        if self.emit is not None:
            self.emit(event)

    def sent(self, operation, label, send):
        # Journaled before it is sent and acknowledged after; a send that raises is neither acknowledged nor retried.
        self.note({"event": "action_started", "operation": operation,
                   "target": policy.plain(label)[:120] if label else None})
        began = self.clock()
        try:
            result = send()
        except Exception as error:
            if "stale_revision" in str(error):
                self.note({"event": "action_not_dispatched", "operation": operation})  # the driver sent nothing
            raise
        self.note({"event": "action_acknowledged", "act_ms": round((self.clock() - began) * 1000)})
        return result

    def elapsed(self):
        return self.clock() - self.started

    def capped(self):
        if self.stopped:
            return True
        if self.cancelled is not None and self.cancelled():
            self.stopped = "stopped by you"
        elif len(self.screens) >= self.caps.screens:
            self.stopped = f"reached {self.caps.screens} screens"
        elif self.elapsed() >= self.caps.seconds:
            self.stopped = f"reached the time limit ({round(self.caps.seconds)} s)"
        elif self.stats["taps"] >= self.caps.taps:
            self.stopped = f"reached {self.caps.taps} taps"
        return bool(self.stopped)

    def observe(self):
        with self.timed("observe"):
            return self.driver.observe(timeout=10)

    def settle(self, before, kind="tap"):
        started = self.clock()
        with self.timed("settle"):
            try:
                return self._settle(before)
            finally:
                detail = self.settles.setdefault(kind, {"n": 0, "seconds": 0.0})
                detail["n"] += 1
                detail["seconds"] += self.clock() - started

    def _settle(self, before):
        wait = getattr(self.driver, "wait_for_change", None)
        if callable(wait):
            try:
                # ``early``: the first changed read that agrees with the read before it, without the driver's extra
                # quiet period. Two agreeing reads are still required, so a moving screen is not returned.
                try:
                    return wait(before, timeout=SETTLE_TIMEOUT, wait_seconds=SETTLE_SECONDS, early=True)
                except TypeError:
                    return wait(before, timeout=SETTLE_TIMEOUT, wait_seconds=SETTLE_SECONDS)
            except Exception:
                pass
        return self._poll_settle(before)

    def _poll_settle(self, before):
        started = self.clock()
        previous = None
        while True:
            read_at = self.clock()
            current = self.observe()
            now = self.clock()
            if current.content_fingerprint == before.content_fingerprint:
                previous = None
                if now - started >= SETTLE_SECONDS:
                    return current
            else:
                if previous is not None and previous.content_fingerprint == current.content_fingerprint:
                    return current
                previous = current
                if now - started >= SETTLE_TIMEOUT:
                    return current
            pause = POLL_SECONDS - (now - read_at)
            if pause > 0:
                self.sleep(pause)

    def regrounded(self, element, snapshot):
        if time.monotonic() - getattr(snapshot, "captured_at", 0) <= FRESH_SECONDS:
            return element, snapshot
        now = self.observe()
        key, (cx, cy) = key_of(element.label), element.center
        same = [e for e in now.elements if e.role == element.role and key_of(e.label) == key]
        same.sort(key=lambda e: abs(e.center[1] - cy) + abs(e.center[0] - cx))
        return (same[0] if same else None), now

    def tap(self, element, snapshot):
        """Taps ``element`` (checked by the policy here, every time) and returns the settled screen after."""
        element, snapshot = self.regrounded(element, snapshot)
        if element is None:
            raise Gone(snapshot)
        verdict = policy.decide(element, snapshot)
        if not verdict.ok:  # the one place every tap passes, replayed taps included
            # The fresh read says no: nothing was tapped, so the control is skipped like one that has gone.
            self.log(f"refused at the tap: {policy_refusal(element, verdict)}")
            raise Gone(snapshot)
        try:
            with self.timed("tap"):
                self.sent("TAP", element.label, lambda: self.driver.execute("TAP", element, snapshot, timeout=10))
        except Exception as error:
            # The driver refused before sending anything (the screen moved since the read): nothing happened.
            if "stale_revision" in str(error):
                raise Gone(self.observe()) from None
            raise
        self.stats["taps"] += 1
        self.before = snapshot  # the read the tap was made on, for telling what it changed
        return self.settle(snapshot)

    def tap_back(self, element, snapshot):
        """A back or close control, checked on its own: its words must name no act."""
        if policy.act_words(element.label) and not BACK_LABEL.match(policy.plain(element.label)):
            return None
        element, snapshot = self.regrounded(element, snapshot)
        if element is None:
            return None
        try:
            self.sent("TAP", element.label, lambda: self.driver.execute("TAP", element, snapshot, timeout=10))
        except Exception as error:
            if "stale_revision" in str(error):
                return None
            raise
        return self.settle(snapshot, "back")

    def scroll(self, snapshot, op="SWIPE_UP"):
        self.sent(op, None, lambda: self.driver.execute(op, None, snapshot, timeout=10))
        self.stats["scrolls"] += 1
        return self.settle(snapshot, "scroll")

    def edge_back(self, snapshot):
        """An edge swipe from the left (iOS's back gesture), when the driver can make one; None when it can't."""
        gesture = getattr(self.driver, "edge_back", None)
        if callable(gesture):
            swipe = lambda: gesture(snapshot)  # noqa: E731
        elif callable(getattr(self.driver, "call", None)) and snapshot.source == "wda":
            w, h = snapshot.width, snapshot.height
            swipe = lambda: self.driver.call("POST", "/actions", {"actions": [{  # noqa: E731
                "type": "pointer", "id": "finger", "parameters": {"pointerType": "touch"},
                "actions": [{"type": "pointerMove", "duration": 0, "x": 1, "y": round(h * .45)},
                            {"type": "pointerDown"},
                            {"type": "pointerMove", "duration": 250, "x": round(w * .8), "y": round(h * .45)},
                            {"type": "pointerUp"}]}]}, timeout=10)
        elif hasattr(getattr(self.driver, "helper", None), "request"):
            swipe = lambda: self.driver.helper.request("drag", x1=.005, y1=.45, x2=.8, y2=.45, ms=250)  # noqa: E731
        else:
            return None
        try:
            self.sent("EDGE_BACK", None, swipe)
        except Exception as error:
            self.log(f"edge swipe failed: {error}")
            return None
        return self.settle(snapshot, "swipe")

    def launch(self, fresh=False):
        """The app's first screen. ``fresh``: closed and reopened (back to its start), when the driver can."""
        self.note({"event": "app_launch_started", "app": self.bundle})
        with self.timed("launch"):
            snapshot = self._launch(fresh)
        self.note({"event": "app_launch_acknowledged", "app": self.bundle})
        return snapshot

    def _launch(self, fresh):
        if fresh:
            relaunch = getattr(self.driver, "relaunch_app", None)
            if callable(relaunch):
                if relaunch(self.bundle) is False:
                    raise Stop("the connection stopped answering")
                ready = getattr(self.driver, "observe_ready", None)
                return ready(timeout=15) if callable(ready) else self.observe()
        launch = getattr(self.driver, "launch", None)
        if callable(launch):
            result = launch(self.bundle, timeout=30)
            return result[0] if isinstance(result, tuple) else result
        before = self.observe()
        self.driver.execute("LAUNCH_APP", self.bundle, before, timeout=20)
        self.sleep(1.5)
        return self.observe()

    def thumbnail(self, screen):
        if not self.shots:
            return
        with self.timed("frames"):
            self._thumbnail(screen)

    def _thumbnail(self, screen):
        try:
            url = self.driver.capture_preview(timeout=5)
            from PIL import Image
            data = base64.b64decode(url.split(",", 1)[1])
            screen.frame = data  # announce() hands it to the run, which stores it beside the run
            with Image.open(BytesIO(data)) as image:
                image = image.convert("RGB")
                height = max(1, round(image.height * THUMB_WIDTH / image.width))
                image = image.resize((THUMB_WIDTH, height), Image.LANCZOS)
                frames = self.out / "frames"
                frames.mkdir(parents=True, exist_ok=True)
                name = f"frames/{screen.id}.jpg"
                image.save(self.out / name, "JPEG", quality=78)
                screen.thumb = name
        except Exception as error:
            self.log(f"no frame for {screen.id}: {error}")

    def dialog(self, snapshot, system=False):
        """Gets a dialog out of the way with Not Now, Cancel or Close; the screen after."""
        inside = [e for e in snapshot.elements if is_dialog_element(e)]
        if inside:
            alert = inside[0]
            buttons = [e for e in snapshot.elements if e.role == "Button" and (
                e.locator.startswith(alert.locator + "/") if alert.locator else policy._contains(alert.rect, e.rect))]
            safe = policy.safe_dismiss([e.label for e in buttons])
            if safe is None:
                raise Stop(f"a dialog needs you: “{policy.plain(alert.label)[:80]}”")
            button = next(e for e in buttons if e.label == safe)
            self.sent("TAP", button.label, lambda: self.driver.execute("TAP", button, snapshot, timeout=10))
            self.stats["dialogs_dismissed"] += 1
            return self.settle(snapshot, "dialog")
        if system and callable(getattr(self.driver, "call", None)) and snapshot.source == "wda":
            from ..wda import system_dialogs
            with self.timed("dialog_checks"):
                found = system_dialogs.read(self.driver, timeout=2)
            if found is not None:
                safe = policy.safe_dismiss(found.buttons)
                if safe is None:
                    raise Stop(f"iOS asks something only you can answer: “{found.text[:80]}”")
                self.sent("TAP", safe, lambda: self.driver.call("POST", "/alert/accept", {"name": safe}, timeout=10))
                self.stats["dialogs_dismissed"] += 1
                return self.settle(snapshot, "dialog")
        return snapshot

    def identify(self, reading):
        for screen in self.screens:
            if screens.same(reading.title, reading.tokens, screen.title, screen.tokens, b_union=screen.union):
                return screen
        return None

    def is_on(self, screen, snapshot):
        reading = screens.read(snapshot)
        return screens.same(reading.title, reading.tokens, screen.title, screen.tokens, b_union=screen.union)

    def register(self, snapshot, reading, depth, path, parent="", via=""):
        screen = Screen(id=f"s{len(self.screens) + 1:03d}", title=reading.title, tokens=reading.tokens,
                        union=set(reading.tokens), lines=list(reading.lines), depth=depth, path=path, parent=parent,
                        via=via, seen_s=self.elapsed(), boxes=boxes(snapshot), image_only=reading.image_only)
        screen.paywall = paywalls.detect(reading.lines)
        self.screens.append(screen)
        self.thumbnail(screen)
        self.announce(screen)
        self.log(f"{screen.id} depth {depth} “{screen.title or '(untitled)'}”"
                 + (f" via “{via}”" if via else "") + (" PAYWALL" if screen.paywall else ""))
        return screen

    def announce(self, screen):
        """A ``step`` in the run for a screen just mapped, with its capture as the step's frame."""
        frame, screen.frame = screen.frame, None
        if self.emit is None:
            return
        event = {"event": "step", "n": len(self.screens), "step": len(self.screens),
                 "text": f"Mapped {screen.name}" + (" (a paywall)" if screen.paywall else ""),
                 **({"_frame": frame} if frame else {})}
        self.emit(event)
        screen.frame_id = event.get("frameId")  # Run.emit stored the frame and put its id in the event

    def learn(self, screen, snapshot):
        """Adds what a scrolled read of ``screen`` shows to it."""
        reading = screens.read(snapshot)
        new = reading.tokens - screen.union
        screen.union |= reading.tokens
        for line in reading.lines:
            if line not in screen.lines and len(screen.lines) < screens.LINES_KEPT * 2:
                screen.lines.append(line)
        return new

    def saturated(self, screen):
        """Whether ``screen`` has opened enough: a catalog sampled, or the children cap reached two levels in."""
        return (screen.alike >= CATALOG_ALIKE
                or screen.depth >= 2 and len(screen.kids) >= self.caps.children)

    def candidates(self, screen, snapshot):
        found, keys = [], set()
        if screen.changed:
            return found
        if policy.choice_list(snapshot):
            screen.choice = True
        for element in sorted(snapshot.elements, key=lambda e: (round(e.rect[1], 3), -e.rect[2])):
            verdict = policy.decide(element, snapshot)
            key = key_of(element.label)
            if not verdict.ok:
                if (key and key not in screen.refused and verdict.reason not in QUIET_REASONS
                        and not (element.role in QUIET_ROLES and not policy.act_words(element.label)
                                 and not screen.image_only)):
                    screen.refused[policy.plain(element.label)[:80]] = verdict.reason
                continue
            if not key or key in keys or key in screen.done:
                continue
            if verdict.reason == "tab" and screen.depth > 0:
                continue  # tabs are top-level: taken from a tab's first screen only
            if screen.choice and verdict.reason == "cell":
                screen.refused.setdefault(policy.plain(element.label)[:80], "an option in a choice list")
                continue
            keys.add(key)
            label = policy.plain(element.label)
            if element.role in ("Cell", "Button") and ", " in label:
                label = label.split(", ")[0]  # "Apple Account, Sign in to access..." is the row "Apple Account"
            found.append(Candidate(key, label, element, verdict.reason))
        return found

    def back_controls(self, snapshot):
        from ..frontier import back_control
        control = back_control(snapshot)
        if control is not None:
            yield "control", control
        for element in snapshot.elements:
            label = policy.plain(element.label)
            if (element is not control and BACK_LABEL.match(label) and element.center[1] < .2
                    and (element.role == "Button" or policy.image_only(snapshot) and element.center[0] < .3)):
                yield ("control" if label.casefold() in ("back", "<", "‹") else "close"), element

    def go_back(self, snapshot):
        """One step back by a back or close control, else an edge swipe; the new snapshot,
        or None when neither moved."""
        for kind, control in self.back_controls(snapshot):
            moved = self.tap_back(control, snapshot)
            if moved is not None and moved.content_fingerprint != snapshot.content_fingerprint:
                self.stats["backs"][kind] += 1
                return moved
        swiped = self.edge_back(snapshot)
        if swiped is not None and swiped.content_fingerprint != snapshot.content_fingerprint:
            self.stats["backs"]["swipe"] += 1
            return swiped
        return None

    def to_top(self, snapshot, root=None):
        """After the app opens: its first screen. Some apps (Settings on an iPhone) reopen on the last one shown."""
        for _ in range(TOP_STEPS):
            if root is not None and self.is_on(root, snapshot):
                break
            if root is None and not any(kind == "control" for kind, _ in self.back_controls(snapshot)):
                break
            moved = self.go_back(snapshot)
            if moved is None:
                break
            snapshot = self.dialog(moved)
            if self.bundle_left(snapshot):
                break
        return snapshot

    def return_to(self, screen, snapshot):
        """``snapshot`` on ``screen`` by Back, an edge swipe, or a relaunch and the path again; None when it can't."""
        if self.is_on(screen, snapshot):
            return snapshot
        for _ in range(4):
            moved = None
            for kind, control in self.back_controls(snapshot):
                moved = self.tap_back(control, snapshot)
                if moved is not None:
                    self.stats["backs"][kind] += 1
                    break
            if moved is None or moved.content_fingerprint == snapshot.content_fingerprint:
                swiped = self.edge_back(snapshot)
                if swiped is not None and swiped.content_fingerprint != snapshot.content_fingerprint:
                    self.stats["backs"]["swipe"] += 1
                    moved = swiped
                else:
                    moved = None
            if moved is None:
                break
            snapshot = self.dialog(moved)
            if self.is_on(screen, snapshot):
                return snapshot
            if self.bundle_left(snapshot):
                break
        self.log(f"relaunching to reach {screen.id}: no back control or swipe brought it up")
        return self.replay(screen)

    def bundle_left(self, snapshot):
        bundle = str(getattr(snapshot, "bundle_id", "") or "")
        return bool(bundle) and bundle != self.bundle

    def find(self, snapshot, key, pages):
        """The control ``key`` names on this screen, scrolling down for it; (element, snapshot) or (None, snapshot)."""
        for page in range(pages + 1):
            for element in snapshot.elements:
                if key_of(element.label) == key and policy.decide(element, snapshot).ok:
                    return element, snapshot
            if page < pages:
                after = self.scroll(snapshot)
                if after.content_fingerprint == snapshot.content_fingerprint:
                    break
                snapshot = after
        return None, snapshot

    def replay(self, screen):
        """Relaunch the app and tap the path to ``screen`` again; the snapshot on it, or None."""
        if self.capped():
            return None
        self.stats["backs"]["relaunch"] += 1
        try:
            snapshot = self.dialog(self.launch(fresh=True), system=True)
            snapshot = self.to_top(snapshot, self.screens[0] if self.screens else None)
            for step in screen.path:
                element, snapshot = self.find(snapshot, step["key"], self.caps.pages)
                if element is None:
                    raise Stop(f"“{step['label']}” is gone")
                snapshot = self.dialog(self.tap(element, snapshot))
        except Gone:
            snapshot = None
        except Stop as error:
            if "needs you" in str(error) or "only you" in str(error) or "stopped answering" in str(error):
                raise
            self.log(f"couldn't get back to {screen.id}: {error}")
            snapshot = None
        if snapshot is not None and self.is_on(screen, snapshot):
            self.stuck = 0
            return snapshot
        self.stats["relaunch_failures"] += 1
        self.stuck += 1
        if self.stuck >= STUCK_LIMIT:
            raise Stop(f"couldn't get back to a screen {STUCK_LIMIT} times in a row")
        return None

    def run(self):
        self.started = self.clock()
        try:
            refusal = policy.app_refusal(self.bundle)
            if refusal:
                raise Stop(refusal)  # before anything is launched or tapped
            snapshot = self.to_top(self.dialog(self.launch(fresh=True), system=True))
            reading = screens.read(snapshot)
            root = self.register(snapshot, reading, 0, [])
            if root.paywall is None:
                # The first levels first: a long list deep in one branch (Settings' fonts) can't use up the screens
                # before the app's other sections are mapped. Then each screen left at that depth, deeper.
                self.limit = min(FIRST_PASS_DEPTH, self.caps.depth)
                self.explore(root, snapshot)
                if self.caps.depth > self.limit:
                    frontier = [s for s in self.screens if s.depth == self.limit and s.pending]
                    self.limit = self.caps.depth
                    for screen in frontier:
                        if self.capped():
                            break
                        found = self.replay(screen)
                        if found is not None:
                            self.explore(screen, found)
            if not self.stopped:
                self.stopped = "explored every screen it could reach"
        except Stop as error:
            self.stopped = str(error)
        except KeyboardInterrupt:
            self.stopped = "stopped by you"
        return self.map()

    def explore(self, screen, snapshot):
        """Explores ``screen`` (``snapshot`` shows it); the last snapshot on it, or None when it lost the screen."""
        scrolls = 0
        while not self.capped():
            found = self.candidates(screen, snapshot)
            if found and self.saturated(screen):
                screen.held_back += len(found)
                break
            if not found:
                if scrolls >= self.caps.pages - 1 or screen.changed or not more_below(snapshot):
                    break
                after = self.scroll(snapshot)
                scrolls += 1
                moved = after.content_fingerprint != snapshot.content_fingerprint
                self.learn(screen, after)
                snapshot = after
                if not moved:
                    break  # the end of the list
                continue
            pick = found[0]
            screen.done.add(pick.key)
            snapshot = self.visit(screen, snapshot, pick)
            if snapshot is None:
                screen.unreachable = True
                return None
        return snapshot

    def visit(self, screen, snapshot, pick):
        """Taps ``pick`` on ``screen``, maps where it leads, explores it when new, and comes back."""
        try:
            after = self.tap(pick.element, snapshot)
            after = self.dialog(after, system=state(after) == state(self.before))
        except Gone as gone:
            return gone.snapshot  # it moved; the next pass reads the screen again
        if self.bundle_left(after):
            self.stats["left_app"] += 1
            self.edges.append({"from": screen.id, "to": "", "label": pick.label, "kind": "leaves app",
                               "app": after.bundle_id})
            return self.return_to(screen, after)
        if self.is_on(screen, after):
            if state(after) != state(self.before):
                # The tap changed this screen in place: it may have set something, so nothing else on it is tapped.
                screen.changed.append(pick.label)
                self.stats["changed_in_place"] = self.stats.get("changed_in_place", 0) + 1
                self.log(f"“{pick.label}” changed {screen.id} in place: leaving the rest of it alone")
            else:
                self.stats["no_change"] += 1
                screen.noop.append(pick.label)
            self.learn(screen, after)
            return after
        reading = screens.read(after)
        target = self.identify(reading)
        kind = "tab" if pick.reason == "tab" else "tap"
        if target is None:
            depth = 0 if kind == "tab" else screen.depth + 1
            path = ([] if kind == "tab" else list(screen.path)) + [{"label": pick.label, "key": pick.key,
                                                                    "kind": kind}]
            target = self.register(after, reading, depth, path, parent=screen.id, via=pick.label)
            shape = target.tokens - {screens.token(target.title)}
            if any(screens.jaccard(shape, other) >= ALIKE for other in screen.kids):
                screen.alike += 1
            screen.kids.append(shape)
            self.edges.append({"from": screen.id, "to": target.id, "label": pick.label, "kind": kind})
            if target.paywall is None and target.depth < self.limit and not self.capped():
                explored = self.explore(target, after)
                after = explored if explored is not None else self.observe()
            elif target.paywall is None:
                target.pending = bool(self.candidates(target, after)) or more_below(after)
        else:
            self.edges.append({"from": screen.id, "to": target.id, "label": pick.label, "kind": kind})
        if self.stopped:
            return after
        return self.return_to(screen, after)

    def map(self):
        wall = max(self.elapsed(), 1e-6)
        refused = [{"screen": s.id, "label": label, "reason": reason}
                   for s in self.screens for label, reason in s.refused.items()]
        safety = [item for item in refused if item["reason"] not in QUIET_REASONS]
        paywalled = [s for s in self.screens if s.paywall]
        linked = {edge["from"] for edge in self.edges}
        stats = {
            "screens": len(self.screens), "edges": len(self.edges),
            "max_depth": max((s.depth for s in self.screens), default=0),
            "wall_seconds": round(wall, 1),
            "screens_per_minute": round(len(self.screens) / (wall / 60), 1),
            "refused": len(safety),
            "paywalls": len(paywalled),
            "dead_ends": sum(1 for s in self.screens if s.id not in linked and not s.paywall),
            "model_usd": 0.0,
            **self.stats,
            "settles": {kind: {"n": d["n"], "seconds": round(d["seconds"], 1)}
                        for kind, d in sorted(self.settles.items())},
            "seconds_in": {kind: round(value, 1) for kind, value in sorted(self.spent.items())},
        }
        return {"schema": SCHEMA, "app": {"bundle": self.bundle}, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "connection": "pixels" if any(s.image_only for s in self.screens) else "tree",
                "caps": {"screens": self.caps.screens, "depth": self.caps.depth, "seconds": self.caps.seconds,
                         "pages": self.caps.pages},
                "stopped": self.stopped, "stats": stats,
                "screens": [s.to_json() for s in self.screens], "edges": self.edges}


def policy_refusal(element, verdict):
    return Stop(f"the policy refused “{policy.plain(element.label)[:60]}”: {verdict.reason}")
