"""Bounded, normalized UI snapshots. Coordinates are always fractions of the screen."""

from dataclasses import dataclass, field
import hashlib
import json
import math
import re
import time
import unicodedata
import xml.etree.ElementTree as ET


def finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


ACTION_OPERATIONS = frozenset({
    "TAP", "TYPE", "SWIPE_UP", "SWIPE_DOWN", "SWIPE_LEFT", "SWIPE_RIGHT",
    "BACK", "HOME", "VOLUME_UP", "VOLUME_DOWN", "LAUNCH_APP", "INCREMENT", "DECREMENT", "SUBMIT", "TYPE_SUBMIT"})
# Operations that write generated text into a field.
TEXT_OPERATIONS = frozenset({"TYPE", "TYPE_SUBMIT"})
# Host keys and app switches, never accessibility-node actions.
ELEMENT_OPERATIONS = ACTION_OPERATIONS - {"HOME", "VOLUME_UP", "VOLUME_DOWN", "LAUNCH_APP"}
# Host hardware keys. Each driver names its own keys; WDA's pressButton takes camelCase.
HOST_KEY_OPERATIONS = frozenset({"HOME", "VOLUME_UP", "VOLUME_DOWN"})
WDA_HOST_KEY_NAMES = {"HOME": "home", "VOLUME_UP": "volumeUp", "VOLUME_DOWN": "volumeDown"}


# What a snapshot source guarantees, by ``Snapshot.source``:
#   exact_actions  every element lists exactly the actions it accepts (an in-app accessibility
#                  bridge); nothing is offered beyond them. WDA's actions are inferred from roles.
#   stable_ids     element ids stay the same across observations of one screen.
#   host_keys      hardware keys (Home, volume) may be offered on this source's screens.
#   app_scoped     host keys also need the observation's bundle id.
# The core knows WDA and the offline fixtures; an extension adds its own sources here.
SOURCE_TRAITS = {
    "wda": frozenset({"host_keys"}),
    "synthetic_fixture": frozenset({"stable_ids"}),
}


def has_trait(snapshot_or_source, trait):
    source = getattr(snapshot_or_source, "source", snapshot_or_source)
    return trait in SOURCE_TRAITS.get(source, ())


def validate_bundle_id(value):
    """Reverse-DNS app identity. A dot is required, so a bundle never collides
    with an element id (UUIDs, small integers, and system:back contain none)."""
    if (not isinstance(value, str) or len(value) > 255 or "." not in value
            or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*[A-Za-z0-9_]", value)):
        raise ValueError("Invalid app bundle identifier")
    return value
NATIVE_BLOCK_REASONS = frozenset({"not_hittable", "covered_or_not_hittable", "covered_by_window"})


def validate_input_text(value, multiline=False):
    """Text entry must not carry submission/control keystrokes, including Unicode controls.

    ``multiline``: the target is a text view (a note, a document body), where Return starts
    a new line rather than submitting, so line breaks are text. Measured (iOSWorld mem-041,
    24 Sep): every multi-line note body was rejected and the note was saved empty.
    """
    allowed = {"\n"} if multiline else set()
    limit = 4000 if multiline else 1000
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(unicodedata.category(c) in {"Cc", "Cs", "Zl", "Zp"} and c not in allowed for c in value)):
        raise ValueError("TYPE requires bounded nonempty text without control characters")
    return value


@dataclass(frozen=True)
class Element:
    id: str
    label: str
    role: str
    rect: tuple[float, float, float, float]
    editable: bool = False
    locator: str = ""
    value: str = ""
    actions: tuple[str, ...] = ("TAP",)
    # Where a tap lands (screen fractions) when not the centre: a wide row whose centre hits none
    # of its own content (see from_wda_root). () = the centre.
    hit: tuple = ()
    # Read only from a rich source (from_wda_root(rich=True)): a disabled control is listed as disabled
    # instead of left out, and a selected one (its "Selected" trait) says so. Plain sources never set
    # them, so Jev's rows and fingerprints do not change.
    enabled: bool = True
    selected: bool = False
    # A field's placeholder (rich sources): XCUITest reports an empty field's value as its placeholder,
    # so a field whose value equals it is empty.
    placeholder: str = ""

    def __post_init__(self):
        if (not isinstance(self.id, str) or not self.id or len(self.id) > 256
                or not isinstance(self.label, str) or len(self.label) > 500
                or not isinstance(self.role, str) or not self.role or len(self.role) > 500
                or not isinstance(self.value, str) or len(self.value) > 500
                or not isinstance(self.locator, str) or len(self.locator) > 16000
                or type(self.editable) is not bool or type(self.enabled) is not bool
                or type(self.selected) is not bool or not isinstance(self.placeholder, str)
                or len(self.placeholder) > 500):
            raise ValueError("Invalid element identity or metadata")
        try:
            for value in (self.id, self.label, self.role, self.value, self.locator):
                value.encode("utf-8")
        except UnicodeError:
            raise ValueError("Element metadata contains invalid Unicode") from None
        if (not isinstance(self.rect, (list, tuple)) or len(self.rect) != 4
                or not isinstance(self.actions, (list, tuple))
                or any(not isinstance(action, str) or action not in ELEMENT_OPERATIONS for action in self.actions)
                or len(set(self.actions)) != len(self.actions)
                or (bool({"TYPE", "TYPE_SUBMIT", "SUBMIT"} & set(self.actions)) and not self.editable)):
            raise ValueError("Invalid element bounds or capabilities")
        x, y, w, h = self.rect
        if not all(finite(n) and 0 <= n <= 1 for n in self.rect):
            raise ValueError("Element rect must use finite normalized coordinates")
        if w <= 0 or h <= 0 or x + w > 1.00001 or y + h > 1.00001:
            raise ValueError("Element is outside the screen")
        object.__setattr__(self, "rect", tuple(self.rect))
        object.__setattr__(self, "actions", tuple(self.actions))
        if self.hit and (len(self.hit) != 2 or not all(finite(n) and x - 1e-6 <= n <= x + d + 1e-6
                                                        for n, x, d in zip(self.hit, (x, y), (w, h)))):
            raise ValueError("Element tap point must lie inside its frame")
        object.__setattr__(self, "hit", tuple(self.hit))

    @property
    def center(self):
        x, y, w, h = self.rect
        return x + w / 2, y + h / 2

    @property
    def text(self):
        """A field's text as read: "" when it shows its placeholder (rich sources know it)."""
        return "" if self.placeholder and self.value == self.placeholder else self.value

    @property
    def tap_point(self):
        return self.hit or self.center


@dataclass(frozen=True)
class ExcludedElement:
    """Known-ineligible AX identity; excluded text never reaches model/evidence inputs."""
    id: str
    reason: str

    def __post_init__(self):
        if (not isinstance(self.id, str) or not self.id or len(self.id) > 256
                or not isinstance(self.reason, str) or self.reason not in NATIVE_BLOCK_REASONS):
            raise ValueError("Invalid native exclusion provenance")


@dataclass(frozen=True)
class OffscreenNode:
    """Labelled content outside the viewport (WebKit exposes the whole page).

    Read-only evidence: it is never an action target. ``rect`` is in screen
    fractions and unclipped, so ``y`` = 2.6 means 2.6 screens below the top.
    A node becomes tappable only by scrolling it into view and observing it
    again as an ``Element`` (see ``WDA.scroll_to``).
    """
    label: str
    role: str
    rect: tuple[float, float, float, float]
    value: str = ""
    locator: str = ""
    web: bool = False

    def __post_init__(self):
        if (not isinstance(self.label, str) or len(self.label) > 500 or not isinstance(self.value, str)
                or len(self.value) > 500 or not isinstance(self.role, str) or not self.role or len(self.role) > 500
                or not isinstance(self.locator, str) or len(self.locator) > 16000 or type(self.web) is not bool
                or not isinstance(self.rect, (list, tuple)) or len(self.rect) != 4
                or not all(finite(n) and abs(n) < 1000 for n in self.rect)):
            raise ValueError("Invalid off-screen node")
        object.__setattr__(self, "rect", tuple(self.rect))

    @property
    def direction(self):
        x, y, w, h = self.rect
        if y >= 1:
            return "below"
        if y + h <= 0:
            return "above"
        return "right" if x >= 1 else "left"

    @property
    def screens_away(self):
        """Distance from the viewport edge, in screen heights (or widths)."""
        x, y, w, h = self.rect
        return round({"below": y - 1, "above": -(y + h), "right": x - 1, "left": -(x + w)}[self.direction], 3)

    def public(self):
        return {"role": self.role, "label": self.label, "value": self.value, "direction": self.direction,
                "screens_away": self.screens_away, "web": self.web}


# Invisible direction and width marks. Calculator's display reads "\u200e579" (a
# left-to-right mark before the digits); kept, the answer "579" was not the literal
# the screen showed (MobsterBench pass 6, text.calc_add).
_INVISIBLE_MARKS = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")


def invisible_marks(text):
    """``text`` without zero-width and bidirectional control characters."""
    return _INVISIBLE_MARKS.sub("", text) if text else text


# Off-screen nodes kept per snapshot (evidence only; the Eiffel Tower article
# had 310 labelled nodes below the viewport).
WDA_OFFSCREEN_LIMIT = 800


@dataclass
class Snapshot:
    elements: list[Element]
    text: str
    width: float
    height: float
    source: str
    # Looked up per call (not bound at import), so a patched clock stamps snapshots too: the
    # settle tests fake time.monotonic from 1000 s and CI runners with <1000 s uptime otherwise
    # saw every snapshot as stale.
    captured_at: float = field(default_factory=lambda: time.monotonic())
    image_hash: str = ""
    revision: str = ""
    bundle_id: str = ""
    excluded_elements: tuple[ExcludedElement, ...] = ()
    # Content outside the viewport: read-only evidence, never in ``elements``,
    # ``text``, ``public()`` or the fingerprints (screen identity is what is on screen).
    offscreen: tuple[OffscreenNode, ...] = ()
    # Screen-sized list panes drawn over one another (WDA pane keys, see from_wda):
    # which one is in front is not in a fast source read, so the driver asks WDA.
    stacked_panes: tuple = ()
    # Screen-sized layers directly under a window, all but the last (the presentation in
    # front): source paths whose visibility the driver asks WDA about.
    stacked_layers: tuple = ()
    # Sibling pages sharing one large frame below a common parent (SwiftUI keeps every tab of a
    # TabView in the tree): ((parent path, ((child position, child path), ...)), ...). Which one
    # shows is not in a fast source read, so the driver asks WDA.
    stacked_pages: tuple = ()
    # "visible": a software keyboard is on screen; "parked": the keyboard node sits off screen
    # (a hardware keyboard: simulators, a paired keyboard), where keystrokes still reach the
    # focused field; "": none.
    keyboard: str = ""
    # When the read that produced this snapshot began (0: unknown): the screen it shows is the
    # screen from then, so a caller that proves nothing moved since may reuse it.
    read_started: float = 0.0

    def __post_init__(self):
        if not all(finite(n) and n > 0 for n in (self.width, self.height)):
            raise ValueError("Invalid screen dimensions")
        if (not isinstance(self.offscreen, (list, tuple)) or len(self.offscreen) > WDA_OFFSCREEN_LIMIT
                or any(not isinstance(node, OffscreenNode) for node in self.offscreen)):
            raise ValueError("Invalid off-screen evidence")
        self.offscreen = tuple(self.offscreen)
        if (not isinstance(self.elements, (list, tuple)) or any(not isinstance(e, Element) for e in self.elements)
                or not isinstance(self.excluded_elements, (list, tuple))
                or any(not isinstance(e, ExcludedElement) for e in self.excluded_elements)):
            raise ValueError("Invalid snapshot element collection")
        identities = [e.id for e in (*self.elements, *self.excluded_elements)]
        if len(identities) > 240 or len(set(identities)) != len(identities):
            raise ValueError("Too many elements or duplicate element IDs")
        self.excluded_elements = tuple(self.excluded_elements)
        if (not isinstance(self.text, str) or len(self.text) > 241000
                or not isinstance(self.source, str) or not self.source or len(self.source) > 128
                or any(not isinstance(value, str) or len(value) > 500
                       for value in (self.image_hash, self.revision, self.bundle_id))):
            raise ValueError("Invalid snapshot metadata")

    def offscreen_evidence(self, limit=200, web_only=False):
        """Off-screen content for extraction and scroll-to-target decisions,
        nearest first: ``[{"id": "o3", "role", "label", "value", "direction",
        "screens_away", "web"}]``. Ids index ``offscreen`` and are only valid
        for this snapshot. Never a tap target: scroll it into view first."""
        ranked = sorted(((node.screens_away, index) for index, node in enumerate(self.offscreen)
                         if node.web or not web_only))
        return [{"id": f"o{index}", **self.offscreen[index].public()} for _, index in ranked[:limit]]

    def public(self):
        # Eligibility means not known excluded, never independently verified visibility.
        # Built field by field (not dataclasses.asdict: 3-5x slower, and this runs
        # ~17 times per agent step); the same keys in the same order.
        return {"source": self.source, "bundle_id": self.bundle_id, "text": self.text[:12000],
                "elements": [{"id": e.id, "label": e.label, "role": e.role, "rect": list(e.rect),
                              "editable": e.editable, "value": e.value, "actions": list(e.actions)}
                             for e in self.elements],
                "excluded_elements": [{"id": e.id, "reason": e.reason} for e in self.excluded_elements],
                "visibility_verified": False}

    @property
    def fingerprint(self):
        # Routing identity, revision, orientation, and text beyond the model's display cap
        # also matter: identical labels do not authorize acting on a replacement control.
        return self._fingerprint(include_revision=True)

    @property
    def content_fingerprint(self):
        # A revision changing is not proof an action changed observable content.
        # Never use this weaker comparison to authorize dispatch on a stale revision.
        return self._fingerprint(include_revision=False)

    def _fingerprint(self, *, include_revision):
        # Cached per snapshot (the agent reads fingerprints ~60 times per run,
        # 0.4-0.9 ms each). Snapshots are mutable, so the cache is stamped with
        # every input by identity (elements are frozen) and value, and is
        # recomputed whenever any of them changed.
        stamp = (self.text, self.width, self.height, self.source, self.bundle_id,
                 self.revision if include_revision else None, self.excluded_elements)
        elements = self.elements
        cache = self.__dict__.setdefault("_fingerprints", {})
        hit = cache.get(include_revision)
        if (hit is not None and hit[1] == stamp and len(hit[0]) == len(elements)
                and all(a is b for a, b in zip(hit[0], elements))):
            return hit[2]
        grounding = {**self.public(), "text": self.text, "width": self.width, "height": self.height,
                     "locators": [e.locator for e in elements]}
        if include_revision:
            grounding["revision"] = self.revision
        value = hashlib.sha256(json.dumps(grounding, sort_keys=True, allow_nan=False).encode()).hexdigest()
        cache[include_revision] = (tuple(elements), stamp, value)
        return value


def same_screen(ax_fingerprint, visual_fingerprint, other_ax, other_visual):
    """Screen-state identity match. AX is required; visual refines only when both sides have one.

    A missing visual component falls back to AX-only comparison, so an
    unavailable capture can never claim two AX-identical screens differ --
    which would relax the ineffective-action replay guard. A present visual
    component is change-detection identity only: never an action target and
    never a prior fact for later decisions.
    """
    if ax_fingerprint != other_ax:
        return False
    if visual_fingerprint is not None and other_visual is not None:
        return visual_fingerprint == other_visual
    return True


def pane_key(role, label, x, y, w, h):
    """A list pane's identity in both a source read and WDA's element API."""
    return (role, label or "", round(x), round(y), round(w), round(h))


def from_wda(xml: str, hidden_panes=frozenset(), hidden_paths=frozenset(), rich=False) -> Snapshot:
    """``hidden_panes``: pane keys WDA reports not visible; ``hidden_paths``: source paths of
    window layers it reports not visible. Their subtrees are skipped. ``rich``: see from_wda_root."""
    return from_wda_root(_parse_wda(xml), hidden_panes, hidden_paths, rich=rich)


def _parse_wda(xml):
    """The checked, parsed WDA source tree. Kept apart from ``from_wda_root`` so a
    re-read with hidden panes or layers reuses it (the parse costs ~13 ms at 1.5 MB)."""
    # "<!" first: upper() of a 1 MB Safari source costs ~1 ms, and it cannot create "<!".
    if not isinstance(xml, str) or len(xml) > 8_000_000 or ("<!" in xml and (
            "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper())):
        raise ValueError("Unsafe or oversized UI source")
    return ET.fromstring(xml)


# A long field value is read as its head, " … ", and its tail (see _wda_text).
FIELD_HEAD, FIELD_TAIL, FIELD_CUT = 200, 295, " … "


def truncated_field_value(value):
    """True when a field's ``value`` is _wda_text's head-and-tail cut, not the whole text."""
    return (isinstance(value, str) and len(value) == FIELD_HEAD + len(FIELD_CUT) + FIELD_TAIL
            and value[FIELD_HEAD:FIELD_HEAD + len(FIELD_CUT)] == FIELD_CUT)


def _wda_text(a, role):
    """A WDA node's (label, value) as screen text, both capped at 500 characters."""
    label = a.get("label") or ""
    name = a.get("name") or ""
    # A bare accessibility identifier is developer plumbing, not screen
    # text: Safari's root reports "SafariWindow?View=Narrow&UUID=...".
    if not label and not WDA_IDENTIFIER.fullmatch(name):
        label = name
    label, value = invisible_marks(label)[:500], invisible_marks(a.get("value", ""))
    if len(value) > 500 and role in ("TextView", "TextField", "SearchField"):
        # Typed text lands at the end of a long field: keep its end (iOSWorld clouddocs-004
        # typed one note 45 times because only the first 500 characters were read).
        value = value[:FIELD_HEAD] + FIELD_CUT + value[-FIELD_TAIL:]
    return label, value[:500]


def _extent(value):
    """A frame coordinate from WDA, with a non-finite one (Reminders' spacer reports CGFLOAT_MAX, which parses
    as inf, 27 Sep) as 0: such a view has no size on screen, and one of them used to fail every read."""
    number = float(value)
    return number if math.isfinite(number) else 0.0


def from_wda_root(root, hidden_panes=frozenset(), hidden_paths=frozenset(), rich=False) -> Snapshot:
    """``from_wda`` on an already parsed tree; it only reads the tree.

    ``rich`` (the frontier policy; Jev and the plain driver leave it off, so their rows are as before):
    - a labelled or identified ``Other`` leaf of touch size is a target. QuickBite's cart bar is an
      accessible view with the id ``view_cart_button`` and no label: never listed, multi-088 spent eight
      turns looking for it (iOSWorld, 25 Sep);
    - a disabled control is listed (``enabled=False``) instead of dropped, and a control whose traits
      say "Selected" (a source read with traits) is marked ``selected``.
    """
    app = next((n for n in root.iter() if n.tag == "XCUIElementTypeApplication"), None)
    if app is None:
        raise ValueError("WDA source has no application frame")
    width, height = float(app.attrib["width"]), float(app.attrib["height"])
    # WDA stamps the foreground app on the Application node; bind it to the
    # observation so identity checks work on USB phones too. Absent or
    # malformed stays unknown (""), never guessed.
    bundle = app.attrib.get("bundleId", "")
    try:
        bundle = validate_bundle_id(bundle) if bundle else ""
    except ValueError:
        bundle = ""
    if not all(math.isfinite(n) and n > 0 for n in (width, height)):
        raise ValueError("Invalid WDA application dimensions")
    nodes, offscreen, visited, panes, layers, pages = [], [], 0, [], [], []
    parked = []
    other_leaves, selected_paths, placeholders = set(), set(), {}
    editable_roles = {"TextField", "SearchField", "TextView"}
    structural = {"Application", "Window", "Other", "ScrollView", "Table", "CollectionView", "WebView"}

    def walk(node, path, ancestor_visible=True, depth=0, named=None, cell=None):
        nonlocal visited
        visited += 1
        if depth > 64 or visited > WDA_NODE_BUDGET:
            raise ValueError("WDA tree exceeds the traversal budget")
        a = node.attrib
        # Honored when present; fast sources exclude it and rely on occlusion below.
        visible = ancestor_visible and a.get("visible", "true") != "false"
        role = node.tag.removeprefix("XCUIElementType")
        if path in hidden_paths:
            return
        if role != "Window" and len(node) > 1:
            same = {}
            for position, child in enumerate(node):
                c = child.attrib
                cw, ch = _extent(c.get("width", 0)), _extent(c.get("height", 0))
                if (child.tag == "XCUIElementTypeOther" and math.isfinite(cw * ch)
                        and cw * ch >= WDA_PAGE_MIN_SCREEN_FRACTION * width * height):
                    frame = tuple(round(_extent(c.get(k, 0)) / 2) for k in ("x", "y", "width", "height"))
                    same.setdefault(frame, []).append(position)
            group = max(same.values(), key=len, default=[])
            if len(group) >= WDA_PAGE_MIN_COUNT:
                paths = [f"{path}/{child.tag}[{index}]" for index, child in _indexed(node)]  # child order
                pages.append((path, tuple((position, paths[position]) for position in group)))
        if role == "Window":
            # A presented sheet or cover is a later screen-sized sibling of the screen it covers
            # (iOSWorld CalTrack, 24 Sep: "Search Food" over "Today", both in one read).
            full = [f"{path}/{child.tag}[{index}]" for index, child in _indexed(node)
                    if _extent(child.attrib.get("width", 0)) * _extent(child.attrib.get("height", 0))
                    >= WDA_LAYER_MIN_SCREEN_FRACTION * width * height]
            if len(full) > 1:
                layers.extend(full[:-1])
        x, y = _extent(a.get("x", 0)), _extent(a.get("y", 0))
        w, h = _extent(a.get("width", 0)), _extent(a.get("height", 0))
        if cell is not None and not (cell[0] - 1 <= x + w / 2 <= cell[0] + cell[2] + 1
                                     and cell[1] - 1 <= y + h / 2 <= cell[1] + cell[3] + 1):
            # Content of a cell UIKit has not rendered reports a collapsed frame (at its table's
            # top) while the cell itself sits off screen. Fast sources carry no visibility, and
            # Mail's 190 phantom texts filled the target budget, pushing its toolbar and "Search
            # Mail" field out (iOSWorld mem-004/010/048, 24 Sep): it takes the cell's frame.
            x, y, w, h = cell
        if not visible and ancestor_visible and w * h == 0 and role == "Other":
            # A zero-size wrapper is "not visible" by having no area, not by hiding its children:
            # iOS 26 glass toolbars wrap their content in one, and Mail's on-screen "Search Mail"
            # field was dropped with it (iOSWorld mem-004: 25 taps on the wrong row, 24 Sep).
            for index, child in _indexed(node):
                walk(child, f"{path}/{child.tag}[{index}]", True, depth + 1, named, cell)
            return
        if role == "SecureTextField" or not visible:
            return
        if role in WDA_PANE_ROLES and w * h >= WDA_PANE_MIN_SCREEN_FRACTION * width * height:
            key = pane_key(role, a.get("label"), x, y, w, h)
            if key in hidden_panes:
                return
            panes.append(key)
        left, top = max(0, x), max(0, y)
        right, bottom = min(width, x + w), min(height, y + h)
        onscreen = right > left and bottom > top
        if role == "Keyboard" and not onscreen:
            parked.append(path)
        # Text is read only for a node that can use it: on large WebKit pages most
        # nodes sit off screen past the evidence cap (measured on synthetic Safari
        # trees: 1.5 MB source 35.4 -> 25.3 ms, 625 KB 14.3 -> 11.2 ms).
        keep_offscreen = (not onscreen and role not in structural
                          and len(offscreen) < WDA_OFFSCREEN_LIMIT and w > 0 and h > 0)
        if onscreen or keep_offscreen:
            label, value = _wda_text(a, role)
            if named and role == "Button" and a.get("accessible") != "true" and named[1] == (x, y, w, h):
                # An inaccessible control filling an accessible, labelled wrapper is that control:
                # TasteRank's "Filters" wraps a button named for its SF symbol ("Drag").
                label = invisible_marks(named[0])[:500]
        if onscreen:
            g = WDA_RECT_GRID_PT
            s_left, s_top, s_right, s_bottom = (round(left / g) * g, round(top / g) * g,
                                                round(right / g) * g, round(bottom / g) * g)
            if s_right > s_left and s_bottom > s_top:
                left, top = s_left, s_top
                right, bottom = min(width, s_right), min(height, s_bottom)
            nodes.append((path, role, label, value, (left, top, right, bottom),
                          a.get("enabled", "true") != "false"))
            if rich:
                if (role == "Other" and len(node) == 0 and label and a.get("accessible", "true") != "false"
                        and w >= OTHER_TARGET_MIN_PT and h >= OTHER_TARGET_MIN_PT
                        and w * h < OTHER_TARGET_MAX_SCREEN_FRACTION * width * height):
                    other_leaves.add(path)
                if "Selected" in (a.get("traits") or "").split(", "):
                    selected_paths.add(path)
                if role in editable_roles and a.get("placeholderValue"):
                    placeholders[path] = invisible_marks(a.get("placeholderValue"))[:500]
        elif keep_offscreen and (label or value.strip()):
            # Entirely outside the viewport: evidence only (WebKit keeps the whole page).
            offscreen.append((path, role, label, value, (x, y, w, h)))
        # (Fast sources carry no accessibility flag: a labelled Other is then taken as accessible.)
        wrapper = ((a.get("label"), (x, y, w, h)) if role == "Other" and a.get("accessible", "true") == "true"
                   and a.get("label") else None)
        counts = {}
        for child in node:
            counts[child.tag] = counts.get(child.tag, 0) + 1
            # Never prune by a parent's frame: Safari's web containers keep a
            # screen-sized frame that scrolls away (y=-1189) while their
            # children stay on screen, and pruning emptied every scrolled page.
            walk(child, f"{path}/{child.tag}[{counts[child.tag]}]", visible, depth + 1, wrapper,
                 (x, y, w, h) if role == "Cell" and w * h > 0 else cell)

    walk(root, f"/{root.tag}")
    covered = wda_occlusion(nodes, width, height)
    # Return/Go/Search exists only while a keyboard is up; then any visible
    # editable field can be submitted.
    keyboard_up = any(node[1] == "Keyboard" and not covered(node[0], *node[4]) for node in nodes)
    keyboard = "visible" if keyboard_up else "parked" if parked else ""
    field_actions = ("TAP", "TYPE", "TYPE_SUBMIT", "SUBMIT") if keyboard else ("TAP", "TYPE", "TYPE_SUBMIT")
    hits = content_hits(nodes)
    bands = chrome_bands(nodes, width, height, covered)
    order = {node[0]: index for index, node in enumerate(nodes)} if bands else {}
    elements, texts = [], []
    for path, role, label, value, (left, top, right, bottom), enabled in nodes:
        if covered(path, left, top, right, bottom):
            continue
        if label or value:
            texts.append(label if value.strip() == label.strip() else (label + " " + value).strip())
        # Clip partially visible controls; never present an off-screen center.
        # Picker wheels carry no label, only their value ("6 o'clock"): without this the alarm
        # time could not be seen or set (iOSWorld multi-031 saved 5:21 PM for "6:30 AM").
        # Unlabelled buttons of touch size stay targets: CloudDocs' floating "+" has no label, so
        # the create flow was unreachable (iOSWorld multi-068, 24 Sep); position identifies it.
        unlabelled_button = (role == "Button" and right - left >= UNLABELLED_BUTTON_MIN_PT
                             and bottom - top >= UNLABELLED_BUTTON_MIN_PT)
        other_leaf = path in other_leaves
        if (enabled or rich) and (label or role in editable_roles or role in ADJUSTABLE_ROLES and value.strip()
                                  or unlabelled_button) and (role not in structural or other_leaf):
            if len(elements) >= 240:
                # A dense page (Wikipedia's Mount Everest infobox) ended runs here. Past the
                # target budget the rest stays readable evidence, never an action target.
                if (label or value.strip()) and len(offscreen) < WDA_OFFSCREEN_LIMIT:
                    offscreen.append((path, role, label, value, (left, top, right - left, bottom - top)))
                continue
            hit = hits.get(path)
            if bands:
                span = visible_span(path, order[path], left, top, right, bottom, bands)
                if span is None:
                    continue  # drawn over by a bar or the keyboard: text, not a target
                if not span[0] <= (hit[1] if hit else (top + bottom) / 2) <= span[1]:
                    hit = ((hit[0] if hit else (left + right) / 2), (span[0] + span[1]) / 2)
            elements.append(Element(str(len(elements)), label, role,
                (left / width, top / height, (right-left) / width, (bottom-top) / height),
                role in editable_roles, path, value,
                field_actions if role in editable_roles else ("TAP",),
                _hit_inside(hit, left, top, right, bottom, width, height), enabled, path in selected_paths,
                placeholders.get(path, "")))
    evidence, seen = [], set()
    for path, role, label, value, (x, y, w, h) in offscreen:
        # WebKit nests the same text (a link and its static text): keep one.
        key = (label or value, round(y / height, 2))
        if key in seen:
            continue
        seen.add(key)
        evidence.append(OffscreenNode(label, role, (x / width, y / height, w / width, h / height),
                                      value, path, "XCUIElementTypeWebView" in path))
    return Snapshot(elements, "\n".join(dict.fromkeys(texts))[:12000], width, height, "wda",
                    bundle_id=bundle, offscreen=tuple(evidence), stacked_panes=stacked_panes(panes),
                    stacked_layers=tuple(layers), stacked_pages=tuple(pages), keyboard=keyboard)


# A row at least this wide (points) whose centre is empty is tapped on its content instead.
HIT_ROW_MIN_PT = 120
CONTENT_ROLES = frozenset({"StaticText", "Image"})


def content_hits(nodes):
    """{path: (x, y) points} for wide Buttons and Cells whose centre hits none of their own
    content: the centre of their first text (else image). A SwiftUI row laid out with a Spacer
    answers taps only on its content; TeamChat's "#general, 2" row ignored centre taps (iOSWorld
    mem-048 and multi-087: 21 taps with no effect, 24 Sep)."""
    hits = {}
    for index, (path, role, _, _, (left, top, right, bottom), _) in enumerate(nodes):
        if role not in ("Button", "Cell") or right - left < HIT_ROW_MIN_PT:
            continue
        cx, cy = (left + right) / 2, (top + bottom) / 2
        content, prefix = [], path + "/"
        for other in nodes[index + 1:]:  # preorder: descendants follow their ancestor
            if not other[0].startswith(prefix):
                break
            if other[1] in CONTENT_ROLES:
                content.append(other)
        if not content or any(l <= cx <= r and t <= cy <= b for _, _, _, _, (l, t, r, b), _ in content):
            continue
        texts = [c for c in content if c[1] == "StaticText" and (c[2] or c[3])] or content
        l, t, r, b = texts[0][4]
        hits[path] = ((l + r) / 2, (t + b) / 2)
    return hits


# Glass bars draw a little past their buttons; the keyboard's backdrop starts above its node.
BAR_MARGIN_PT, KEYBOARD_BACKDROP_PT = 6, 24
# A bar built from plain buttons: at least this many small siblings on one line, this wide in all.
BAR_MIN_BUTTONS, BAR_MIN_SPAN, BAR_MAX_BUTTON_HEIGHT = 3, .6, .09
# ...low on the screen (its bottom edge past this share: tab bars, or one pushed up by the keyboard),
# one line meaning centres within BAR_LINE_PT.
BAR_LOW, BAR_LINE_PT = .55, 14
# A bar whose buttons end below this share of the screen reaches its bottom edge.
BAR_FOOT = .85
# The least visible height (points) a target keeps; below it, it is not a target. A 26 pt strip of a
# QuickChat result between the tab bar and the keyboard took no taps (live, 25 Sep): bars and the
# keyboard catch touches past their drawn edges.
MIN_VISIBLE_PT = 30


def chrome_bands(nodes, width, height, covered=lambda *node: False):
    """[(owner paths, draw order, over scrolling content, left, top, right, bottom)] drawn over earlier
    content (navigation bars and toolbars stay with wda_occlusion's centre rule): tab bars, the
    keyboard (with its backdrop), and rows of small sibling buttons low on the
    screen, which is how a SwiftUI tab bar is read (no TabBar node). QuickChat's tab bar sat over a
    search result above the keyboard, and 16 taps on it did nothing (iOSWorld multi-087, 24 Sep).
    A tab bar precedes its screens in QuickChat's source, so it covers scrolling content (lists,
    grids: always beneath fixed bars) whatever the order; anything else it covers only when drawn
    after it (a tooltip over the tab bar stays: FreshCart's "Got It!")."""
    bands = []
    for index, (path, role, _, _, (left, top, right, bottom), _) in enumerate(nodes):
        if role == "Keyboard" and bottom > top:
            # Always in front, and down to the screen's edge (its emoji and dictation row sits below the node).
            bands.append(((path,), len(nodes), True, left, max(0, top - KEYBOARD_BACKDROP_PT), right, height))
        elif role == "TabBar" and not covered(path, left, top, right, bottom):
            bands.append(((path,), index, True, left, top - BAR_MARGIN_PT, right, _bar_bottom(bottom, height)))
    rows = {}
    for index, (path, role, _, _, (left, top, right, bottom), _) in enumerate(nodes):
        if (role == "Button" and 0 < bottom - top <= BAR_MAX_BUTTON_HEIGHT * height and bottom >= BAR_LOW * height
                and not any(marker in path for marker in SCROLLING)  # a row in a list is content, not a bar
                and not covered(path, left, top, right, bottom)):
            rows.setdefault(path.rsplit("/", 1)[0], []).append((index, left, top, right, bottom, path))
    for buttons in rows.values():
        # One line: centres within BAR_LINE_PT of the line's first (tab labels of two lines sit higher).
        for line in _lines(buttons):
            if (len({round(m[1]) for m in line}) >= BAR_MIN_BUTTONS
                    and max(m[3] for m in line) - min(m[1] for m in line) >= BAR_MIN_SPAN * width):
                # Owned by its buttons only: their parent can hold the screen's content too.
                bands.append((tuple(m[5] for m in line), min(m[0] for m in line), True, min(m[1] for m in line),
                              min(m[2] for m in line) - BAR_MARGIN_PT, max(m[3] for m in line),
                              _bar_bottom(max(m[4] for m in line), height)))
    return bands


def _bar_bottom(bottom, height):
    """A bar near the screen's foot covers down to its edge (its background runs under the home
    indicator): a QuickChat result below the tab bar's buttons took no taps (multi-079, 4 runs, 25 Sep)."""
    return height if bottom >= BAR_FOOT * height else bottom + BAR_MARGIN_PT


def _lines(buttons):
    """``buttons`` grouped into horizontal lines by their centres."""
    lines = []
    for button in sorted(buttons, key=lambda m: (m[2] + m[4]) / 2):
        centre = (button[2] + button[4]) / 2
        if lines and centre - (lines[-1][0][2] + lines[-1][0][4]) / 2 <= BAR_LINE_PT:
            lines[-1].append(button)
        else:
            lines.append([button])
    return lines


SCROLLING = ("XCUIElementTypeScrollView[", "XCUIElementTypeTable[", "XCUIElementTypeCollectionView[")


def visible_span(path, position, left, top, right, bottom, bands):
    """(top, bottom) of the tallest part of a frame no band in front covers, or None when less than
    MIN_VISIBLE_PT (or 60% of a shorter frame) remains. A band covers only what it overlaps for most of its width."""
    spans, scrolls = [(top, bottom)], any(marker in path for marker in SCROLLING)
    for owners, order, over_scroll, b_left, b_top, b_right, b_bottom in bands:
        if order <= position and not (over_scroll and scrolls) or any(
                path == owner or path.startswith(owner + "/") for owner in owners):
            continue
        if min(right, b_right) - max(left, b_left) < .5 * (right - left):
            continue
        spans = [piece for s_top, s_bottom in spans for piece in
                 ((s_top, min(s_bottom, b_top)), (max(s_top, b_bottom), s_bottom)) if piece[1] - piece[0] > 0]
    best = max(spans, key=lambda span: span[1] - span[0], default=None)
    return best if best and best[1] - best[0] >= min(MIN_VISIBLE_PT, .6 * (bottom - top)) else None


def _hit_inside(point, left, top, right, bottom, width, height):
    """``point`` as screen fractions if inside the (clipped) frame, else ()."""
    if point is None or not (left <= point[0] <= right and top <= point[1] <= bottom):
        return ()
    return point[0] / width, point[1] / height


def _indexed(node):
    """(per-tag index, child) as the source path numbers them ("XCUIElementTypeOther[2]")."""
    counts = {}
    for child in node:
        counts[child.tag] = counts.get(child.tag, 0) + 1
        yield counts[child.tag], child


def stacked_panes(panes):
    """The pane keys that overlap another pane by at least half the smaller one's area.

    Notes' search results and its folder list are both full-screen collection views;
    the results come first in document order yet are drawn in front, so the
    back-to-front rule hid them (MobsterBench pass 6: text.notes_search, ret.note_code,
    multi.note_link). Only WDA's visibility says which one is shown.
    """
    stacked = []
    for key in panes:
        _, _, x, y, w, h = key
        for other in panes:
            if other is key or other == key:
                continue
            _, _, ox, oy, ow, oh = other
            overlap = max(0, min(x + w, ox + ow) - max(x, ox)) * max(0, min(y + h, oy + oh) - max(y, oy))
            if overlap >= .5 * min(w * h, ow * oh):
                stacked.append(key)
                break
    return tuple(stacked)


# iOS 26 glass controls morph by ~1 pt for about a second after a transition
# (measured: the Settings search field), which made every read a "new" screen.
# Edges snap to this grid so identity tracks layout, not animation noise; a tap
# lands at most 2 pt from the true center of a >=44 pt target.
WDA_RECT_GRID_PT = 4
# Nodes read per WDA source; pages mid-load exceeded the old 4000.
WDA_NODE_BUDGET = 12000
# Identifier-shaped names (query strings, *Identifier, *Controller) with no
# human label are dropped from screen text and element labels.
WDA_IDENTIFIER = re.compile(r"\S*[?=&]\S*|\S*(Identifier|Controller|ViewController)")
# Chrome that draws over scrolled content. Measured on iOS 26 Settings: without
# WDA's (8x slower) `visible` attribute, rows scrolled under the translucent
# navigation bar appear in the tree and would be tapped through the bar.
WDA_OCCLUDING_ROLES = frozenset({"NavigationBar", "TabBar", "Toolbar", "Keyboard"})
# Modal surfaces: while one is up, nothing outside it can receive a tap.
WDA_MODAL_ROLES = frozenset({"Alert", "Sheet"})
# Opaque list surfaces. One drawn later over a large part of the screen hides
# what is behind it: iOS keeps the previous list in the tree with on-screen
# rects, e.g. Settings' root rows under its search-results list.
WDA_PANE_ROLES = frozenset({"Table", "CollectionView"})
WDA_PANE_MIN_SCREEN_FRACTION = .4
WDA_LAYER_MIN_SCREEN_FRACTION = .9
# Same-frame sibling pages at least this share of the screen, and this many, are candidate tab pages
# (two are an overlay and its host more often: LockedIn paid a visibility probe on every read, 25 Sep).
WDA_PAGE_MIN_SCREEN_FRACTION = .4
WDA_PAGE_MIN_COUNT = 3
# Controls chosen by value rather than typed or tapped (WDA sets them: WDA.set_value).
ADJUSTABLE_ROLES = frozenset({"PickerWheel"})
UNLABELLED_BUTTON_MIN_PT = 20
# An ``Other`` leaf (rich sources) is a target from touch size (Apple's 44 pt) up to this share of the
# screen: a leaf that fills the screen is a backdrop, not a control.
OTHER_TARGET_MIN_PT = 44
OTHER_TARGET_MAX_SCREEN_FRACTION = .5


def wda_occlusion(nodes, width=None, height=None):
    """Geometric stand-in for WDA's visibility: a node is covered when its center
    lies under chrome it does not belong to, outside the frontmost modal, or
    under a large list pane drawn after it (document order is back-to-front).
    Chrome (navigation bars, toolbars, the keyboard) is never hidden by a pane."""
    def inside(path, owner):
        return path == owner or path.startswith(owner + "/")
    chrome = [(node[0], node[4]) for node in nodes if node[1] in WDA_OCCLUDING_ROLES]
    # Document order is back-to-front, so the last modal is the frontmost.
    modal = next((node[0] for node in reversed(nodes) if node[1] in WDA_MODAL_ROLES), None)
    order = {node[0]: index for index, node in enumerate(nodes)}
    screen = (width * height) if width and height else None
    panes = [(index, node[0], node[4]) for index, node in enumerate(nodes)
             if node[1] in WDA_PANE_ROLES and screen
             and (node[4][2] - node[4][0]) * (node[4][3] - node[4][1]) >= WDA_PANE_MIN_SCREEN_FRACTION * screen]

    def covered(path, left, top, right, bottom):
        if modal and not inside(path, modal) and not inside(modal, path):
            return True
        cx, cy = (left + right) / 2, (top + bottom) / 2
        # Chrome hides what is drawn before it; a sheet or menu presented later sits in front
        # of the old screen's bar (Reminders' New List Done, Files' Select were dropped).
        position = order.get(path, -1)
        if any(l <= cx <= r and t <= cy <= b and not inside(path, owner) and not inside(owner, path)
               and position < order.get(owner, -1)
               for owner, (l, t, r, b) in chrome):
            return True
        if len(panes) < 2 or any(inside(path, owner) for owner, _ in chrome):
            return False
        index = order.get(path, -1)
        return any(pane_index > index and l <= cx <= r and t <= cy <= b
                   and not inside(path, pane) and not inside(pane, path)
                   for pane_index, pane, (l, t, r, b) in panes)
    return covered


def from_ocr(rows, width, height, image_hash="", source="ocr") -> Snapshot:
    if not isinstance(rows, list) or len(rows) > 240:
        raise ValueError("Invalid or truncated OCR observation")
    elements = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("text"), str):
            raise ValueError("Malformed OCR observation")
        label = row["text"][:500].strip()
        if not label:
            continue
        # Apple Vision has a bottom-left origin; the Swift helper converts to top-left.
        elements.append(Element(str(len(elements)), label, "ocr_text", tuple(row["rect"])))
    return Snapshot(elements, "\n".join(e.label for e in elements), width, height,
                    source, image_hash=image_hash)
