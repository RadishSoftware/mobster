"""The device side of a surface: WebDriverAgent gestures and reads on a simulator, with nothing retried after an
unknown outcome."""

import time

from ..assertions import evaluate_on, folded
from ..tree import read_tree
from ..verdict import CouldntRun

# WDA reads the app it was told is active while that app runs, even under Control Center or the Lock Screen, and the
# run names the app under test (drivers.py). A name no app has makes WDA read the app at its detection point, a
# system overlay included (measured 26 Oct 2026, iOS 26.4 simulator, WDA 16.12.10).
IN_FRONT = "auto"
READ_POLL = .5
TAP_MS = 40
HOLD_MS = 1200
DRAG_MS = 350


class SurfaceMiss(Exception):
    """The surface couldn't be reached or didn't show; the check fails with this sentence and goes on."""


class Stage:
    """One simulator's screen, driven from outside the app under test. Every call is logged before it is sent."""

    def __init__(self, driver, *, log, sleep=time.sleep, clock=time.monotonic):
        self.driver, self._log, self.sleep, self.clock = driver, log, sleep, clock
        self.size = (0.0, 0.0)
        self.shot = None    # called with no arguments when a settle ends: the frame of the surface as it stood

    def act(self, what, method, path, body=None, timeout=20):
        """One action. An answer that never came is an unknown outcome: the run stops and nothing is sent again."""
        self._log(f"surface: {what}")
        try:
            return self.driver.call(method, path, body, timeout)
        except Exception as error:
            raise CouldntRun("unknown_outcome",
                             f"WebDriverAgent didn't confirm whether this happened: {what} ({type(error).__name__}).",
                             "Look at the simulator before starting another run; Mobster didn't send it again.") \
                from None

    def follow_the_front(self):
        """Reads come from whatever is in front until ``activate`` names an app again."""
        self.act("read whatever is in front", "POST", "/appium/settings",
                 {"settings": {"defaultActiveApplication": IN_FRONT}})

    def read(self):
        """The accessibility tree of whatever is in front, the Home Screen and Lock Screen included."""
        try:
            tree = read_tree(self.driver, pause=self.sleep)
        except Exception as error:
            raise CouldntRun("wda", f"WebDriverAgent stopped answering while a surface was read "
                                    f"({type(error).__name__}).", "Run `mobster sim doctor --fix`, then try again.") \
                from None
        self.size = tree.size
        return tree

    def settle(self, assertions, timeout=8):
        """Read until every assertion holds or ``timeout`` passes: (results, tree) from the last read."""
        deadline = self.clock() + timeout
        while True:
            tree = self.read()
            results = evaluate_on(assertions, tree)
            if all(result.ok for result in results) or self.clock() >= deadline:
                if self.shot is not None:
                    self.shot()
                return results, tree
            self.sleep(READ_POLL)

    def find(self, tree, *needles):
        """The first shown node whose label or identifier contains one of ``needles``, ignoring case."""
        wanted = [folded(needle) for needle in needles]
        for node in tree.shown():
            text = (folded(node.label), folded(node.identifier))
            if any(needle and needle in part for needle in wanted for part in text):
                return node
        return None

    def wait_for(self, needles, what, timeout=6, selector=None):
        """The node for ``needles`` (or ``selector``) once it shows; SurfaceMiss when it never does."""
        deadline = self.clock() + timeout
        while True:
            tree = self.read()
            node = (next(iter(tree.select(selector)), None) if selector is not None
                    else self.find(tree, *([needles] if isinstance(needles, str) else needles)))
            if node is not None:
                return node
            if self.clock() >= deadline:
                raise SurfaceMiss(f"{what} didn't show; the screen had {self._seen(tree)}")
            self.sleep(READ_POLL)

    @staticmethod
    def _seen(tree):
        labels = [node.label for node in tree.shown() if node.label.strip()][:6]
        return ", ".join(f'"{label[:40]}"' for label in labels) or "no labelled elements"

    def tap_node(self, node, what):
        x, y, w, h = node.rect
        self.tap(x + w / 2, y + h / 2, what)

    def tap(self, x, y, what):
        self._pointer(f"tap {what}", [{"type": "pointerMove", "duration": 0, "x": round(x), "y": round(y)},
                                      {"type": "pointerDown"}, {"type": "pause", "duration": TAP_MS},
                                      {"type": "pointerUp"}])

    def hold(self, x, y, what):
        self._pointer(f"press and hold {what}", [{"type": "pointerMove", "duration": 0, "x": round(x), "y": round(y)},
                                                 {"type": "pointerDown"}, {"type": "pause", "duration": HOLD_MS},
                                                 {"type": "pointerUp"}])

    def drag(self, start, end, what):
        self._pointer(f"swipe {what}", [{"type": "pointerMove", "duration": 0, "x": round(start[0]),
                                         "y": round(start[1])}, {"type": "pointerDown"},
                                        {"type": "pointerMove", "duration": DRAG_MS, "x": round(end[0]),
                                         "y": round(end[1])}, {"type": "pointerUp"}])

    def _pointer(self, what, steps):
        self.act(what, "POST", "/actions", {"actions": [{"type": "pointer", "id": "finger",
                                                         "parameters": {"pointerType": "touch"}, "actions": steps}]})

    def home(self):
        self.act("press Home", "POST", "/wda/pressButton", {"name": "home"})

    def lock(self):
        self.act("lock the simulator", "POST", "/wda/lock")

    def unlock(self):
        self.act("unlock the simulator", "POST", "/wda/unlock")

    def type(self, text):
        self.act("type into the search field", "POST", "/wda/keys", {"value": list(text)})

    def siri(self, phrase):
        self.act("ask Siri", "POST", "/wda/siri/activate", {"text": phrase})

    def activate(self, bundle):
        self.act(f"bring {bundle} forward", "POST", "/wda/apps/activate", {"bundleId": bundle})
