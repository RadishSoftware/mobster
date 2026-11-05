"""Bounded, normalized UI snapshots. Coordinates are always fractions of the screen."""

import hashlib
import json
import math
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field


def finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


ACTION_OPERATIONS = frozenset({
    "TAP", "TYPE", "SWIPE_UP", "SWIPE_DOWN", "SWIPE_LEFT", "SWIPE_RIGHT",
    "BACK", "HOME", "VOLUME_UP", "VOLUME_DOWN", "LAUNCH_APP", "INCREMENT", "DECREMENT", "SUBMIT", "TYPE_SUBMIT"})
TEXT_OPERATIONS = frozenset({"TYPE", "TYPE_SUBMIT"})
ELEMENT_OPERATIONS = ACTION_OPERATIONS - {"HOME", "VOLUME_UP", "VOLUME_DOWN", "LAUNCH_APP"}
HOST_KEY_OPERATIONS = frozenset({"HOME", "VOLUME_UP", "VOLUME_DOWN"})
WDA_HOST_KEY_NAMES = {"HOME": "home", "VOLUME_UP": "volumeUp", "VOLUME_DOWN": "volumeDown"}


# A 1,300-character note timed out part-typed at a flat 15 s and was typed again (iOSWorld mem-048, 24 Sep).
TYPING_CHARS_PER_SECOND = 10


def typing_timeout(text):
    return 15 + len(text or "") / TYPING_CHARS_PER_SECOND


SIGNATURE_LIMIT = 200
SHAPE_LIMIT = 200


def signature(snapshot):
    pairs = sorted({(element.role, element.label) for element in snapshot.elements if element.label})
    return [list(pair) for pair in pairs[:SIGNATURE_LIMIT]]


def screen_shape(pairs):
    out = {hashlib.sha1(json.dumps(list(pair), ensure_ascii=False).encode()).hexdigest()[:8] for pair in pairs or ()}
    return sorted(out)[:SHAPE_LIMIT]


# perception.snapshot_from adds each pixel-only driver's source here.
PIXEL_SOURCES = {"mirroring"}
PIXEL_LOCATORS = ("ocr:", "icon:")


def icon_glyph(locator):
    parts = str(locator or "").split(":")
    return parts[2].replace("-", " ") if len(parts) >= 3 and parts[0] == "icon" else ""


def pixel_read(snapshot):
    # Fails closed: one element read from pixels makes the whole screen a pixel read.
    if getattr(snapshot, "source", None) in PIXEL_SOURCES:
        return True
    return any(str(getattr(e, "locator", "") or "").startswith(PIXEL_LOCATORS)
               for e in getattr(snapshot, "elements", ()) or ())


def validate_bundle_id(value):
    # A dot is required, so a bundle never collides with an element id.
    if (not isinstance(value, str) or len(value) > 255 or "." not in value
            or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*[A-Za-z0-9_]", value)):
        raise ValueError("Invalid app bundle identifier")
    return value


def validate_input_text(value, multiline=False):
    # A text view takes line breaks as text: rejecting them saved every multi-line note empty (iOSWorld mem-041).
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
    # () taps the centre.
    hit: tuple = ()
    # Only rich sources set these, so Jev's rows and fingerprints do not change.
    enabled: bool = True
    selected: bool = False
    # XCUITest reports an empty field's value as its placeholder.
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
        return "" if self.placeholder and self.value == self.placeholder else self.value

    @property
    def tap_point(self):
        return self.hit or self.center


# Calculator's display reads "\u200e579": kept, the answer "579" didn't match the screen (MobsterBench pass 6).
_INVISIBLE_MARKS = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")


def invisible_marks(text):
    return _INVISIBLE_MARKS.sub("", text) if text else text


@dataclass
class Snapshot:
    elements: list[Element]
    text: str
    width: float
    height: float
    source: str
    # Looked up per call so a patched clock stamps snapshots too (CI runners with under 1000 s uptime saw them stale).
    captured_at: float = field(default_factory=lambda: time.monotonic())
    image_hash: str = ""
    revision: str = ""
    bundle_id: str = ""
    # Which of these is in front is not in a fast source read, so the driver asks WDA.
    stacked_panes: tuple = ()
    stacked_layers: tuple = ()
    # ((parent path, ((child position, child path), ...)), ...): SwiftUI keeps every tab of a TabView in the tree.
    stacked_pages: tuple = ()
    # "visible", "parked" (a hardware keyboard: keystrokes still reach the focused field) or "".
    keyboard: str = ""
    read_started: float = 0.0
    # What a presented sheet covers: evidence only, never a target, never in the text or the fingerprints.
    behind: tuple = ()
    # Where password fields sit, as (x, y, w, h) fractions of the screen: never their label or value, never a target.
    # An iOS dialog with one is a password prompt (wda/system_dialogs.py).
    secure_fields: tuple = ()

    def __post_init__(self):
        if not all(finite(n) and n > 0 for n in (self.width, self.height)):
            raise ValueError("Invalid screen dimensions")
        if (not isinstance(self.elements, (list, tuple)) or any(not isinstance(e, Element) for e in self.elements)):
            raise ValueError("Invalid snapshot element collection")
        identities = [e.id for e in self.elements]
        if len(identities) > 240 or len(set(identities)) != len(identities):
            raise ValueError("Too many elements or duplicate element IDs")
        if (not isinstance(self.text, str) or len(self.text) > 241000
                or not isinstance(self.source, str) or not self.source or len(self.source) > 128
                or any(not isinstance(value, str) or len(value) > 500
                       for value in (self.image_hash, self.revision, self.bundle_id))):
            raise ValueError("Invalid snapshot metadata")

    def public(self):
        # Not dataclasses.asdict: 3-5x slower, and this runs ~17 times per agent step.
        return {"source": self.source, "bundle_id": self.bundle_id, "text": self.text[:12000],
                "elements": [{"id": e.id, "label": e.label, "role": e.role, "rect": list(e.rect),
                              "editable": e.editable, "value": e.value, "actions": list(e.actions)}
                             for e in self.elements]}

    @property
    def fingerprint(self):
        # Identical labels do not authorize acting on a replacement control.
        return self._fingerprint(include_revision=True)

    @property
    def content_fingerprint(self):
        # Never use this weaker comparison to authorize dispatch on a stale revision.
        return self._fingerprint(include_revision=False)

    def _fingerprint(self, *, include_revision):
        # Read ~60 times a run at 0.4-0.9 ms each; snapshots are mutable, so the cache is stamped with every input.
        stamp = (self.text, self.width, self.height, self.source, self.bundle_id,
                 self.revision if include_revision else None)
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


def pane_key(role, label, x, y, w, h):
    return (role, label or "", round(x), round(y), round(w), round(h))


def from_wda(xml: str, hidden_panes=frozenset(), hidden_paths=frozenset(), rich=False) -> Snapshot:
    return from_wda_root(_parse_wda(xml), hidden_panes, hidden_paths, rich=rich)


def _parse_wda(xml):
    # "<!" first: upper() of a 1 MB Safari source costs ~1 ms, and it cannot create "<!".
    if not isinstance(xml, str) or len(xml) > 8_000_000 or ("<!" in xml and (
            "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper())):
        raise ValueError("Unsafe or oversized UI source")
    return ET.fromstring(xml)


FIELD_HEAD, FIELD_TAIL, FIELD_CUT = 200, 295, " … "


def truncated_field_value(value):
    return (isinstance(value, str) and len(value) == FIELD_HEAD + len(FIELD_CUT) + FIELD_TAIL
            and value[FIELD_HEAD:FIELD_HEAD + len(FIELD_CUT)] == FIELD_CUT)


def _wda_text(a, role):
    label = a.get("label") or ""
    name = a.get("name") or ""
    # A bare identifier is plumbing, not screen text: Safari's root reports "SafariWindow?View=Narrow&UUID=...".
    if not label and not WDA_IDENTIFIER.fullmatch(name):
        label = name
    label, value = invisible_marks(label)[:500], invisible_marks(a.get("value", ""))
    if len(value) > 500 and role in ("TextView", "TextField", "SearchField"):
        # Typed text lands at the end: reading only the head, clouddocs-004 typed one note 45 times (iOSWorld).
        value = value[:FIELD_HEAD] + FIELD_CUT + value[-FIELD_TAIL:]
    return label, value[:500]


def _extent(value):
    # Reminders' spacer reports CGFLOAT_MAX, which parses as inf and used to fail every read (27 Sep).
    number = float(value)
    return number if math.isfinite(number) else 0.0


def from_wda_root(root, hidden_panes=frozenset(), hidden_paths=frozenset(), rich=False) -> Snapshot:
    app = next((n for n in root.iter() if n.tag == "XCUIElementTypeApplication"), None)
    if app is None:
        raise ValueError("WDA source has no application frame")
    width, height = float(app.attrib["width"]), float(app.attrib["height"])
    # The foreground app WDA stamps on the Application node; absent or malformed stays "", never guessed.
    bundle = app.attrib.get("bundleId", "")
    try:
        bundle = validate_bundle_id(bundle) if bundle else ""
    except ValueError:
        bundle = ""
    if not all(math.isfinite(n) and n > 0 for n in (width, height)):
        raise ValueError("Invalid WDA application dimensions")
    nodes, visited, panes, layers, pages = [], 0, [], [], []
    parked, secure = [], []
    other_leaves, selected_paths, placeholders = set(), set(), {}
    editable_roles = {"TextField", "SearchField", "TextView"}
    structural = {"Application", "Window", "Other", "ScrollView", "Table", "CollectionView", "WebView"}

    def walk(node, path, ancestor_visible=True, depth=0, named=None, cell=None):
        nonlocal visited
        visited += 1
        if depth > 64 or visited > WDA_NODE_BUDGET:
            raise ValueError("WDA tree exceeds the traversal budget")
        a = node.attrib
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
                paths = [f"{path}/{child.tag}[{index}]" for index, child in _indexed(node)]
                pages.append((path, tuple((position, paths[position]) for position in group)))
        if role == "Window":
            # A presented sheet is a later screen-sized sibling of the screen it covers (iOSWorld CalTrack, 24 Sep).
            full = [f"{path}/{child.tag}[{index}]" for index, child in _indexed(node)
                    if _extent(child.attrib.get("width", 0)) * _extent(child.attrib.get("height", 0))
                    >= WDA_LAYER_MIN_SCREEN_FRACTION * width * height]
            if len(full) > 1:
                layers.extend(full[:-1])
        x, y = _extent(a.get("x", 0)), _extent(a.get("y", 0))
        w, h = _extent(a.get("width", 0)), _extent(a.get("height", 0))
        if cell is not None and not (cell[0] - 1 <= x + w / 2 <= cell[0] + cell[2] + 1
                                     and cell[1] - 1 <= y + h / 2 <= cell[1] + cell[3] + 1):
            # An unrendered cell's content reports a collapsed frame: Mail's phantom texts filled the budget (mem-004).
            x, y, w, h = cell
        if not visible and ancestor_visible and w * h == 0 and role == "Other":
            # iOS 26 glass toolbars wrap their content in a zero-size "not visible" wrapper (iOSWorld mem-004).
            for index, child in _indexed(node):
                walk(child, f"{path}/{child.tag}[{index}]", True, depth + 1, named, cell)
            return
        if role == "SecureTextField" and visible and w * h > 0:
            secure.append((x / width, y / height, w / width, h / height))
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
        if onscreen:
            label, value = _wda_text(a, role)
            if named and role == "Button" and a.get("accessible") != "true" and named[1] == (x, y, w, h):
                # TasteRank's "Filters" wraps a button named for its SF symbol ("Drag").
                label = invisible_marks(named[0])[:500]
            g = WDA_RECT_GRID_PT
            s_left, s_top, s_right, s_bottom = (round(left / g) * g, round(top / g) * g,
                                                round(right / g) * g, round(bottom / g) * g)
            if s_right > s_left and s_bottom > s_top:
                left, top = s_left, s_top
                right, bottom = min(width, s_right), min(height, s_bottom)
            nodes.append((path, role, label, value, (left, top, right, bottom),
                          a.get("enabled", "true") != "false"))
            if rich:
                # QuickBite's cart bar is an unlabelled Other with an id: multi-088 spent eight turns on it (iOSWorld).
                if (role == "Other" and len(node) == 0 and label and a.get("accessible", "true") != "false"
                        and w >= OTHER_TARGET_MIN_PT and h >= OTHER_TARGET_MIN_PT
                        and w * h < OTHER_TARGET_MAX_SCREEN_FRACTION * width * height):
                    other_leaves.add(path)
                if "Selected" in (a.get("traits") or "").split(", "):
                    selected_paths.add(path)
                if role in editable_roles and a.get("placeholderValue"):
                    placeholders[path] = invisible_marks(a.get("placeholderValue"))[:500]
        wrapper = ((a.get("label"), (x, y, w, h)) if role == "Other" and a.get("accessible", "true") == "true"
                   and a.get("label") else None)
        counts = {}
        for child in node:
            counts[child.tag] = counts.get(child.tag, 0) + 1
            # Never prune by a parent's frame: Safari's web containers scroll away while their children stay.
            walk(child, f"{path}/{child.tag}[{counts[child.tag]}]", visible, depth + 1, wrapper,
                 (x, y, w, h) if role == "Cell" and w * h > 0 else cell)

    walk(root, f"/{root.tag}")
    covered = wda_occlusion(nodes, width, height)
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
        # Unlabelled pickers and buttons stay targets (iOSWorld multi-031 alarm time, multi-068 CloudDocs' "+").
        unlabelled_button = (role == "Button" and right - left >= UNLABELLED_BUTTON_MIN_PT
                             and bottom - top >= UNLABELLED_BUTTON_MIN_PT)
        other_leaf = path in other_leaves
        if (enabled or rich) and (label or role in editable_roles or role in ADJUSTABLE_ROLES and value.strip()
                                  or unlabelled_button) and (role not in structural or other_leaf):
            if len(elements) >= 240:
                continue  # past the target budget the rest stays text
            hit = hits.get(path)
            if bands:
                span = visible_span(path, order[path], left, top, right, bottom, bands)
                if span is None:
                    continue  # drawn over by a bar or the keyboard
                if not span[0] <= (hit[1] if hit else (top + bottom) / 2) <= span[1]:
                    hit = ((hit[0] if hit else (left + right) / 2), (span[0] + span[1]) / 2)
            elements.append(Element(str(len(elements)), label, role,
                (left / width, top / height, (right-left) / width, (bottom-top) / height),
                role in editable_roles, path, value,
                field_actions if role in editable_roles else ("TAP",),
                _hit_inside(hit, left, top, right, bottom, width, height), enabled, path in selected_paths,
                placeholders.get(path, "")))
    return Snapshot(elements, "\n".join(dict.fromkeys(texts))[:12000], width, height, "wda",
                    bundle_id=bundle, stacked_panes=stacked_panes(panes),
                    stacked_layers=tuple(layers), stacked_pages=tuple(pages), keyboard=keyboard,
                    secure_fields=tuple(secure))


# A SwiftUI row with a Spacer answers taps only on its content (iOSWorld mem-048, multi-087: 21 dead taps).
HIT_ROW_MIN_PT = 120
CONTENT_ROLES = frozenset({"StaticText", "Image"})


def content_hits(nodes):
    hits = {}
    for index, (path, role, _, _, (left, top, right, bottom), _) in enumerate(nodes):
        if role not in ("Button", "Cell") or right - left < HIT_ROW_MIN_PT:
            continue
        cx, cy = (left + right) / 2, (top + bottom) / 2
        content, prefix = [], path + "/"
        for other in nodes[index + 1:]:  # preorder
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


BAR_MARGIN_PT, KEYBOARD_BACKDROP_PT = 6, 24
BAR_MIN_BUTTONS, BAR_MIN_SPAN, BAR_MAX_BUTTON_HEIGHT = 3, .6, .09
BAR_LOW, BAR_LINE_PT = .55, 14
BAR_FOOT = .85
# Safari's bottom bar: 98 of 852 pt.
BAR_MAX_HEIGHT = .2
# Bars and the keyboard catch touches past their edges: a 26 pt strip took no taps (live, 25 Sep).
MIN_VISIBLE_PT = 30


def chrome_bands(nodes, width, height, covered=lambda *node: False):
    # SwiftUI tab bars are rows of buttons: QuickChat's sat over a result and 16 taps missed (iOSWorld multi-087).
    bands = []
    for index, (path, role, _, _, (left, top, right, bottom), _) in enumerate(nodes):
        if role == "Keyboard" and bottom > top:
            # Down to the screen's edge: its emoji and dictation row sits below the node.
            bands.append(((path,), len(nodes), True, left, max(0, top - KEYBOARD_BACKDROP_PT), right, height))
        elif role == "TabBar" and not covered(path, left, top, right, bottom):
            bands.append(((path,), index, True, left, top - BAR_MARGIN_PT, right, _bar_bottom(bottom, height)))
    rows = {}
    frames = {node[0]: node[4] for node in nodes}
    for index, (path, role, _, _, (left, top, right, bottom), _) in enumerate(nodes):
        if (role == "Button" and 0 < bottom - top <= BAR_MAX_BUTTON_HEIGHT * height and bottom >= BAR_LOW * height
                and not any(marker in path for marker in SCROLLING)
                and not covered(path, left, top, right, bottom)):
            rows.setdefault(_bar_owner(path, frames, width, height), []).append((index, left, top, right, bottom,
                                                                                path))
    for buttons in rows.values():
        for line in _lines(buttons):
            if (len({round(m[1]) for m in line}) >= BAR_MIN_BUTTONS
                    and max(m[3] for m in line) - min(m[1] for m in line) >= BAR_MIN_SPAN * width):
                # Owned by its buttons only: their parent can hold the screen's content too.
                bands.append((tuple(m[5] for m in line), min(m[0] for m in line), True, min(m[1] for m in line),
                              min(m[2] for m in line) - BAR_MARGIN_PT, max(m[3] for m in line),
                              _bar_bottom(max(m[4] for m in line), height)))
    return bands


def _bar_owner(path, frames, width, height):
    # Safari's bottom bar puts its buttons in separate containers inside one (Quick mode, 10 Oct: 3 of 3 runs).
    parent = path.rsplit("/", 1)[0]
    ancestor = parent
    while ancestor.count("/") > 1:
        frame = frames.get(ancestor)
        if frame is not None:
            left, top, right, bottom = frame
            if (right - left >= BAR_MIN_SPAN * width and top >= BAR_LOW * height
                    and bottom - top <= BAR_MAX_HEIGHT * height):
                return ancestor
            if bottom - top > BAR_MAX_HEIGHT * height:
                break  # past the bar: this frame holds the screen
        ancestor = ancestor.rsplit("/", 1)[0]
    return parent


def _bar_bottom(bottom, height):
    # A bar's background runs under the home indicator (multi-079, 25 Sep).
    return height if bottom >= BAR_FOOT * height else bottom + BAR_MARGIN_PT


def _lines(buttons):
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
    if point is None or not (left <= point[0] <= right and top <= point[1] <= bottom):
        return ()
    return point[0] / width, point[1] / height


def _indexed(node):
    counts = {}
    for child in node:
        counts[child.tag] = counts.get(child.tag, 0) + 1
        yield counts[child.tag], child


def stacked_panes(panes):
    # Notes' search results come first in document order yet are drawn in front (MobsterBench pass 6).
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


# iOS 26 glass controls morph by ~1 pt for a second after a transition, which made every read a new screen.
WDA_RECT_GRID_PT = 4
# Pages mid-load exceeded the old 4000.
WDA_NODE_BUDGET = 12000
WDA_IDENTIFIER = re.compile(r"\S*[?=&]\S*|\S*(Identifier|Controller|ViewController)")
# Without WDA's (8x slower) `visible`, rows scrolled under a translucent bar would be tapped through it.
WDA_OCCLUDING_ROLES = frozenset({"NavigationBar", "TabBar", "Toolbar", "Keyboard"})
WDA_MODAL_ROLES = frozenset({"Alert", "Sheet"})
# iOS keeps the previous list in the tree with on-screen rects (Settings' rows under its search results).
WDA_PANE_ROLES = frozenset({"Table", "CollectionView"})
WDA_PANE_MIN_SCREEN_FRACTION = .4
WDA_LAYER_MIN_SCREEN_FRACTION = .9
# Two same-frame pages are more often an overlay and its host: LockedIn paid a probe on every read (25 Sep).
WDA_PAGE_MIN_SCREEN_FRACTION = .4
WDA_PAGE_MIN_COUNT = 3
ADJUSTABLE_ROLES = frozenset({"PickerWheel"})
UNLABELLED_BUTTON_MIN_PT = 20
# A leaf that fills the screen is a backdrop, not a control.
OTHER_TARGET_MIN_PT = 44
OTHER_TARGET_MAX_SCREEN_FRACTION = .5


def wda_occlusion(nodes, width=None, height=None):
    def inside(path, owner):
        return path == owner or path.startswith(owner + "/")
    chrome = [(node[0], node[4]) for node in nodes if node[1] in WDA_OCCLUDING_ROLES]
    # Document order is back-to-front.
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
        # A sheet presented later sits in front of the old screen's bar (Reminders' New List Done was dropped).
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


STACKED_ARTIFACT = 5
SYMBOL_NAME = re.compile(r"^[a-z][a-z0-9]*(?:\.[a-z0-9]+)+$")  # chevron.forward, not 26.0.1


def redundant_ids(elements):
    # A Settings row is a Button plus its texts and chevron, one tap: offering all four overflowed the model's input.
    def get(element, name):
        return element.get(name) if isinstance(element, dict) else getattr(element, name, None)
    rows = [e for e in elements if get(e, "role") in ("Button", "Cell") and (get(e, "label") or "").strip()]
    redundant = set()
    # Many texts at one frame are a layout artifact: Settings > Notifications put ~180 rows at one rect.
    stacks = {}
    for element in elements:
        if not get(element, "editable"):
            stacks.setdefault(tuple(round(v, 3) for v in get(element, "rect")[:2])
                              + (round(get(element, "rect")[3], 3),),
                              []).append(get(element, "id"))
    for ids in stacks.values():
        if len(ids) >= STACKED_ARTIFACT:
            redundant.update(ids)
    for element in elements:
        role, label = get(element, "role"), (get(element, "label") or "").strip()
        if get(element, "editable") or role not in ("StaticText", "Image"):
            continue
        if role == "Image" and SYMBOL_NAME.match(label):
            redundant.add(get(element, "id"))
            continue
        x, y, w, h = get(element, "rect")
        cx, cy = x + w / 2, y + h / 2
        for row in rows:
            rx, ry, rw, rh = get(row, "rect")
            if (rx <= cx <= rx + rw and ry <= cy <= ry + rh and label
                    and label.casefold() in (get(row, "label") or "").casefold()):
                redundant.add(get(element, "id"))
                break
    return redundant
