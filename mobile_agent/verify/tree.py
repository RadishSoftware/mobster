"""The evidence read: the app's accessibility tree as WebDriverAgent reports it, with each node's visibility."""

import hashlib
import json
import math
import time
from dataclasses import dataclass

from ..state import _parse_wda
from ..wda.observe import WDA_RICH_SOURCE_PATH

# Keeps ``visible`` and ``traits``: p50 0.76-1.09 s on Settings (28 Sep, n=20), under the spec's 1.5 s.
EVIDENCE_SOURCE_PATH = "/source?format=xml&excluded_attributes=accessible,index"
# No ``visible``: 0.14-0.5 s against 1.4-1.9 s (17 Oct). It says whether the screen moved; it never decides.
GLANCE_SOURCE_PATH = WDA_RICH_SOURCE_PATH
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


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _text(value):
    value = value or ""
    if len(value) > TEXT_LIMIT:
        raise TreeError(f"The accessibility tree has text longer than {TEXT_LIMIT:,} characters")
    return value


class AXTree:
    """One read of the app's tree: ``nodes`` in document order."""

    def __init__(self, bundle_id, size, nodes):
        self.bundle_id = bundle_id
        self.size = tuple(size)
        self.nodes = tuple(nodes)
        self._shown = None
        self.glance = False  # read without ``visible``: it may order a re-read, never decide a verdict

    def on_screen(self, node):
        """The node's frame overlaps the app's frame with positive area."""
        x, y, w, h = node.rect
        width, height = self.size
        return min(width, x + w) - max(0.0, x) > 0 and min(height, y + h) - max(0.0, y) > 0

    def is_laid_out(self, node):
        """Every ``shown`` rule except WDA's own ``visible``."""
        return node.role not in STRUCTURAL and not node.inner_toggle and self.on_screen(node)

    def is_shown(self, node):
        # The node's own ``visible``: WDA calls containers not visible while their content is on screen.
        return node.visible and self.is_laid_out(node)

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

    def _digest(self, nodes):
        rows = [self.bundle_id, self.size, [
            [node.path, node.role, node.identifier, node.label, node.value, node.placeholder,
             node.enabled, node.selected, [round(node.rect[0]), round(node.rect[1]), *node.rect[2:]]]
            for node in nodes]]
        return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()

    def _evidence_digest(self, nodes):
        # These are the complete AXNode fields consumed by selectors and screen audits, including exact dimensions.
        rows = [self.bundle_id, self.size, [vars(node) for node in nodes]]
        return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()

    def fingerprint(self):
        """Identity of the shown assertion inputs on a full evidence read."""
        return self._evidence_digest(self.shown())

    def audit_fingerprint(self):
        """Identity of every input a screen audit may read, including offscreen nodes."""
        return self._evidence_digest(self.nodes)

    def layout(self):
        """The fingerprint over the nodes that pass every ``shown`` rule except WDA's ``visible``."""
        return self._digest([node for node in self.nodes if self.is_laid_out(node)])


def parse_tree(xml):
    """The evidence read as an AXTree."""
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
        # A SwiftUI Toggle reads as a labelled Switch around an unlabelled knob (iOS 26.4): the knob is inner_toggle.
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


def read_source(driver, timeout=30, pause=None, retry_slow=True, path=EVIDENCE_SOURCE_PATH):
    # Right after a relaunch WDA can answer "not running" for a moment, so the one retry waits RETRY_PAUSE. A read that
    # used all of ``timeout`` was slow, not caught in a moment: with retry_slow False it is not tried again.
    own = getattr(driver, "evidence_source", None)  # a connection with its own tree read (Android)
    if own is not None:
        return own(timeout)
    last = None
    for attempt in range(2):
        if attempt:
            (pause or time.sleep)(RETRY_PAUSE)
        started = time.monotonic()
        try:
            xml = driver.call("GET", path, timeout=timeout)
            if not isinstance(xml, str):
                raise TreeError("WebDriverAgent did not return the accessibility tree")
            return xml
        except TreeError:
            raise
        except Exception as error:  # transport errors, timeouts, a runner that went away
            last = error
            if not retry_slow and time.monotonic() - started >= timeout - .1:
                break
    raise last


def read_tree(driver, timeout=30, pause=None, retry_slow=True, path=EVIDENCE_SOURCE_PATH):
    return parse_tree(read_source(driver, timeout=timeout, pause=pause, retry_slow=retry_slow, path=path))


def tree_from_pixels(snapshot):
    # Paths start with /pixels, which no Element locator does, so a tap lands on the frame and never on another read's.
    width, height = snapshot.width, snapshot.height
    nodes = [AXNode(path=f"/pixels/{element.role}[{index}]", role=element.role, identifier="", label=element.label,
                    value=element.value, placeholder=element.placeholder,
                    rect=(element.rect[0] * width, element.rect[1] * height,
                          element.rect[2] * width, element.rect[3] * height),
                    enabled=element.enabled, selected=element.selected, visible=True)
             for index, element in enumerate(snapshot.elements, 1)]
    return AXTree(snapshot.bundle_id, (width, height), nodes)


def screen_title(tree):
    """The title a screen shows: its navigation bar's, else the top-most text in the upper third."""
    shown = tree.shown()
    for node in shown:
        if node.role == "NavigationBar":
            title = (node.label or node.identifier).strip()
            if title:
                return title[:120]
    top = tree.size[1] / 3
    texts = [node for node in shown if node.role == "StaticText" and node.label.strip() and 0 <= node.rect[1] < top]
    return min(texts, key=lambda node: (node.rect[1], node.rect[0])).label.strip()[:120] if texts else ""
