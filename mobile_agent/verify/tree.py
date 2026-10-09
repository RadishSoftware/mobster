"""The evidence read: the app's accessibility tree as WebDriverAgent reports it, with each node's visibility.

``GET /source?format=xml&excluded_attributes=accessible,index`` keeps ``visible`` and ``traits``, which the agent's
fast read leaves out. It costs more. Measured 28 Sep 2026 on an iPhone 17 Pro simulator (iOS 26.4, M5 Max) while
the Mac ran 10 other simulators (load average 120-190): on Settings' first screen (167 nodes, 42 labelled elements
shown) p50 0.76 s and 1.09 s in two passes of n=20, against 0.10 s for the fast read (n=10); 1.07 s on General and
1.61 s on About (143 nodes, n=10 each). That is under the 1.5 s the spec allows on a 50-element screen, so verdicts
are decided on this read rather than on the fast read plus per-node visibility queries.

What counts as shown (``AXTree.shown``), decided per node (SPEC §3.4 as amended 28 Sep):
1. WDA reports the node itself ``visible``. An ancestor's ``visible="false"`` is not inherited: WDA calls
   containers not visible while their content is on screen (a plain SwiftUI Form's or Settings' CollectionView,
   iOS 26 glass toolbars' zero-size wrappers, the Window that holds SpringBoard's status bar), and content that is
   really covered, such as the screen under a fullScreenCover, carries ``visible="false"`` on each node. On the
   33 trees captured on 28 Sep (Settings, Daybreak's onboarding, paywall and its planted bugs, Today, a plain Form,
   an alert, SpringBoard) this rule and the inherited one it replaces show the same nodes, except on SpringBoard,
   where the inherited rule hid the status bar that is on screen. ``hidden_by_ancestor`` keeps the inherited
   answer as data;
2. its frame overlaps the app's frame with positive area;
3. it is not the Application or a Window;
4. it is not an unlabelled Switch inside a Switch. On iOS 26.4 a SwiftUI Toggle reads as a labelled Switch (the
   row) wrapping an unlabelled one (the knob) with no identifier, so selectors by role counted every toggle twice
   (Daybreak's Today: 6 switches for 3 toggles). The inner one stays in ``nodes`` for paths; drivers.switch_point
   taps a row-wide switch at its knob.

Paths are ``state.from_wda_root``'s ``Element.locator``: ``/{root tag}``, then ``/{child tag}[n]`` where n counts
same-tag siblings from 1, so a node maps to the driver's element by path.
"""

from dataclasses import dataclass
import hashlib
import json
import math
import time

from ..state import _parse_wda

EVIDENCE_SOURCE_PATH = "/source?format=xml&excluded_attributes=accessible,index"
MAX_DEPTH = 64
MAX_NODES = 5000
TEXT_LIMIT = 4000
STRUCTURAL = frozenset({"Application", "Window"})
# Containers WDA reports not visible while their content shows: hidden_by_ancestor leaves them out.
SCROLL_CONTAINERS = frozenset({"CollectionView", "Table", "ScrollView", "WebView"})
TOGGLES = frozenset({"Switch", "Toggle"})
RETRY_PAUSE = .75  # seconds before the evidence read's one retry


class TreeError(ValueError):
    """The source could not be read as an accessibility tree (too large, too deep, unsafe or malformed)."""


@dataclass(frozen=True)
class AXNode:
    path: str
    role: str                    # the XCUIElementType name without its prefix: "Button", "StaticText"
    identifier: str              # the accessibility identifier: WDA's name when it differs from the label
    label: str
    value: str
    placeholder: str
    rect: tuple                  # points: x, y, w, h
    enabled: bool
    selected: bool
    visible: bool                # WDA's own visible attribute (True when the read leaves it out)
    hidden_by_ancestor: bool = False  # an ancestor with area, not a scroll container, is visible="false" (data only)
    inner_toggle: bool = False   # an unlabelled Switch with no identifier inside a Switch: its knob (never shown)

    @property
    def hit_point(self):
        x, y, w, h = self.rect
        return x + w / 2, y + h / 2


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _text(value):
    return (value or "")[:TEXT_LIMIT]


class AXTree:
    """One read of the app's tree: ``nodes`` in document order."""

    def __init__(self, bundle_id, size, nodes):
        self.bundle_id = bundle_id
        self.size = tuple(size)
        self.nodes = tuple(nodes)
        self._shown = None

    def on_screen(self, node):
        """The node's frame overlaps the app's frame with positive area."""
        x, y, w, h = node.rect
        width, height = self.size
        return min(width, x + w) - max(0.0, x) > 0 and min(height, y + h) - max(0.0, y) > 0

    def is_shown(self, node):
        return node.visible and node.role not in STRUCTURAL and not node.inner_toggle and self.on_screen(node)

    def shown(self):
        if self._shown is None:
            self._shown = [node for node in self.nodes if self.is_shown(node)]
        return list(self._shown)

    def select(self, selector):
        """Shown nodes matching ``selector`` (an ``assertions.Selector``, or a selector object)."""
        from .assertions import Selector, parse_selector
        if not isinstance(selector, Selector):
            selector = parse_selector(selector)
        return [node for node in self.shown() if selector.matches(node)]

    def by_path(self, path):
        return next((node for node in self.nodes if node.path == path), None)

    def fingerprint(self):
        """SHA-256 of the shown nodes' (role, identifier, label, value, frame rounded to 1 pt)."""
        rows = [[node.role, node.identifier, node.label, node.value, [round(n) for n in node.rect]]
                for node in self.shown()]
        return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()


def parse_tree(xml):
    """The evidence read as an AXTree. Raises TreeError (a ValueError) when the source is unsafe, too large
    (8 MB), too deep (64 levels) or has more than 5,000 nodes."""
    try:
        root = _parse_wda(xml)
    except ValueError as error:
        raise TreeError(f"The accessibility tree could not be read: {error}") from None
    except Exception as error:  # xml.etree.ElementTree.ParseError and friends
        raise TreeError(f"The accessibility tree could not be read: {type(error).__name__}") from None
    app = next((node for node in root.iter() if node.tag == "XCUIElementTypeApplication"), None)
    if app is None:
        raise TreeError("The accessibility tree has no application")
    width, height = _number(app.attrib.get("width")), _number(app.attrib.get("height"))
    if width <= 0 or height <= 0:
        raise TreeError("The accessibility tree's application has no size")
    nodes = []

    def walk(node, path, depth, hidden, parent_role):
        if depth > MAX_DEPTH:
            raise TreeError(f"The accessibility tree is deeper than {MAX_DEPTH} levels")
        if len(nodes) >= MAX_NODES:
            raise TreeError(f"The accessibility tree has more than {MAX_NODES:,} elements")
        a = node.attrib
        role = node.tag.removeprefix("XCUIElementType")
        x, y = _number(a.get("x")), _number(a.get("y"))
        w, h = _number(a.get("width")), _number(a.get("height"))
        visible = a.get("visible", "true") != "false"
        label = _text(a.get("label"))
        name = _text(a.get("name"))
        traits = [trait.strip() for trait in (a.get("traits") or "").split(",")]
        nodes.append(AXNode(
            path=path, role=role, identifier=name if name and name != label else "", label=label,
            value=_text(a.get("value")), placeholder=_text(a.get("placeholderValue")),
            rect=(x, y, w, h), enabled=a.get("enabled", "true") != "false",
            selected="Selected" in traits or a.get("selected") == "true", visible=visible,
            hidden_by_ancestor=hidden,
            inner_toggle=role in TOGGLES and parent_role in TOGGLES and not label.strip() and not name.strip()))
        hides = not visible and w * h > 0 and role not in SCROLL_CONTAINERS  # for hidden_by_ancestor only
        counts = {}
        for child in node:
            counts[child.tag] = counts.get(child.tag, 0) + 1
            walk(child, f"{path}/{child.tag}[{counts[child.tag]}]", depth + 1, hidden or hides, role)

    walk(root, f"/{root.tag}", 0, False, None)
    return AXTree(app.attrib.get("bundleId", "") or "", (width, height), nodes)


def read_source(driver, timeout=30, pause=None):
    """The evidence read's XML from the driver's WDA session; one retry on a transport error, RETRY_PAUSE seconds
    later (``pause(seconds)`` waits; default time.sleep). Right after a relaunch WDA can answer "Application … is
    not running" for a moment, and a retry sent at once lands in the same moment."""
    last = None
    for attempt in range(2):
        if attempt:
            (pause or time.sleep)(RETRY_PAUSE)
        try:
            xml = driver.call("GET", EVIDENCE_SOURCE_PATH, timeout=timeout)
            if not isinstance(xml, str):
                raise TreeError("WebDriverAgent did not return the accessibility tree")
            return xml
        except TreeError:
            raise
        except Exception as error:  # transport errors, timeouts, a runner that went away
            last = error
    raise last


def read_tree(driver, timeout=30, pause=None):
    return parse_tree(read_source(driver, timeout=timeout, pause=pause))
