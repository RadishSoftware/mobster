"""One open run as the MCP server keeps it, and the screen outline the host agent reads."""

import difflib
import re
import threading
import time

FRIENDLY_ROLES = {
    "Button": "button", "StaticText": "text", "TextField": "field", "SecureTextField": "field",
    "SearchField": "field", "TextView": "field", "Switch": "switch", "Toggle": "switch", "Cell": "cell",
    "Image": "image", "Icon": "image", "Link": "link", "Tab": "tab", "Slider": "slider", "Stepper": "stepper",
    "Picker": "picker", "PickerWheel": "picker", "DatePicker": "picker", "SegmentedControl": "segment",
    "NavigationBar": "navbar", "Alert": "alert",
}
CONTROL_ROLES = frozenset({"button", "field", "switch", "cell", "link", "tab", "slider", "stepper", "picker",
                           "segment"})
SKIPPED_ROLES = frozenset({"Application", "Window", "Keyboard"})
KEYBOARD_MARK = "XCUIElementTypeKeyboard["
TAB_BAR_MARK = "XCUIElementTypeTabBar["
SCROLLING_ROLES = frozenset({"ScrollView", "Table", "CollectionView", "WebView"})
CONTENT_ROLES = frozenset({"StaticText", "Image", "Icon"})

MAX_LINES = 120
OUTLINE_BUDGET = 5000
LABEL_CHARS, VALUE_CHARS, ID_CHARS = 60, 40, 40


def role_name(node):
    role = (node.role or "").removeprefix("XCUIElementType")
    if role == "Button" and TAB_BAR_MARK in (node.path or ""):
        return "tab"
    return FRIENDLY_ROLES.get(role, role.lower() or "other")


def _plain(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _quoted(text, limit):
    return '"' + _plain(text, limit).replace('"', "'") + '"'


def worth_listing(node):
    role = (node.role or "").removeprefix("XCUIElementType")
    if role in SKIPPED_ROLES or KEYBOARD_MARK in (node.path or ""):
        return False
    return bool((node.label or "").strip() or (node.value or "").strip() or node.identifier
                or role_name(node) in CONTROL_ROLES)


def _bare(node):
    return (node.role or "").removeprefix("XCUIElementType")


def _is_scroll_indicator(node):
    label = node.label or ""
    return _bare(node) == "Other" and (label.startswith("Vertical scroll bar") or
                                       label.startswith("Horizontal scroll bar"))


def _normal(text):
    return " ".join((text or "").split()).casefold()


# An SF Symbol's name, as a glyph with no label of its own reports it: "chevron", "chevron.forward".
_SYMBOL_NAME = re.compile(r"[a-z]+(?:\.[a-z0-9]+)*")
GLYPH_POINTS = 30


def _glyph_sized(node):
    rect = node.rect or ()
    return len(rect) == 4 and max(rect[2], rect[3]) <= GLYPH_POINTS


def _repeats(node, owner):
    if role_name(owner) not in CONTROL_ROLES:
        return False
    if role_name(node) in CONTROL_ROLES:
        label, value = (node.label or "").strip(), (node.value or "").strip()
        if not label and not node.identifier and role_name(node) == role_name(owner) \
                and value == (owner.value or "").strip():
            return True  # SwiftUI's Toggle: a switch holding an unnamed switch with the same value
        if not node.enabled and not node.identifier and value in ("", label) \
                and (not label or _SYMBOL_NAME.fullmatch(label) and _glyph_sized(node)):
            return True  # a disabled accessory with no name of its own, such as Settings' "chevron"
        return (bool(node.label) and _normal(node.label) == _normal(owner.label)
                and node.identifier in ("", owner.identifier)
                and (node.value or "") in ("", owner.value or "", node.label))
    if _bare(node) not in CONTENT_ROLES:
        return False
    if node.value and node.value != node.label:
        return False
    label = " ".join((node.label or "").split()).casefold()
    if not label:
        return _bare(node) != "StaticText" or not node.identifier
    if node.identifier and _bare(node) == "StaticText" and node.identifier != node.label:
        return False
    return label in " ".join((owner.label or "").split()).casefold() or \
        label in " ".join((owner.value or "").split()).casefold()


def _twin_key(node):
    return (_bare(node), node.label, node.value, node.identifier, tuple(round(v) for v in (node.rect or ())))


def _ancestors(path):
    while "/" in path.strip("/"):
        path = path.rsplit("/", 1)[0]
        yield path


def collapse(nodes):
    paths = {node.path for node in nodes}
    outer = [node for node in nodes if not any(parent in paths for parent in _ancestors(node.path))]
    seen, kept = set(), []
    for node in outer:
        key = _twin_key(node)
        if key not in seen:
            seen.add(key)
            kept.append(node)
    return kept


def node_summary(node):
    parts = [role_name(node)]
    if node.label:
        parts.append(_quoted(node.label, LABEL_CHARS))
    if node.value and node.value != node.label:
        parts.append("value=" + _quoted(node.value, VALUE_CHARS))
    if node.identifier:
        parts.append("id=" + _plain(node.identifier, ID_CHARS))
    return " ".join(parts)


def node_target(node):
    return {"id": node.identifier or "", "label": node.label or "",
            "role": (node.role or "").removeprefix("XCUIElementType")}


def node_name(node):
    return _plain(node.label or node.identifier or node.value or role_name(node), LABEL_CHARS)


# Ref numbers never name another node, so a ref planned from an earlier outline is an error, never a different element.
class RefBook:
    def __init__(self):
        self.issued = {}
        self.count = 0
        self._fingerprint, self._latest = None, {}

    def widest(self, rows):
        return len(f"e{self.count + rows}")

    def number(self, fingerprint, paths):
        keep = self._latest if fingerprint == self._fingerprint else {}
        refs = {}
        for path in paths:
            ref = keep.get(path)
            if ref is None:
                self.count += 1
                ref = f"e{self.count}"
                self.issued[ref] = (path, fingerprint)
            refs[ref] = path
        self._fingerprint, self._latest = fingerprint, {path: ref for ref, path in refs.items()}
        return refs


class Outline:
    def __init__(self, tree, book=None):
        self.tree = tree
        self.book = book if book is not None else RefBook()
        self.fingerprint = tree.fingerprint()
        shown = {node.path for node in tree.shown()}
        width, height = (list(tree.size) + [0, 0])[:2] if tree.size else (0, 0)
        self.width, self.height = width, height
        self.nodes = self._listed(tree, shown)
        self.below, self.above = self._off_screen(tree, shown, height)
        self.keyboard = any((node.role or "").removeprefix("XCUIElementType") == "Keyboard"
                            for node in tree.nodes if node.path in shown)
        self.lines, self.refs, self.listed = self._lines()

    @staticmethod
    def _listed(tree, shown):
        listed, by_path, twins = [], {}, set()
        for node in tree.nodes:
            if node.path not in shown or not worth_listing(node) or _is_scroll_indicator(node):
                continue
            owner = next((by_path[p] for p in _ancestors(node.path) if p in by_path), None)
            if owner is not None and _repeats(node, owner):
                continue
            key = _twin_key(node)
            if key in twins:
                continue
            twins.add(key)
            listed.append(node)
            by_path[node.path] = node
        holders = {parent for node in listed for parent in _ancestors(node.path)}
        return [node for node in listed if not (node.path in holders and not (node.label or "").strip()
                                                and not (node.value or "").strip() and not node.identifier)]

    @staticmethod
    def _off_screen(tree, shown, height):
        if not height:
            return 0, 0
        scrollers = {node.path for node in tree.nodes if _bare(node) in SCROLLING_ROLES}
        span = {}
        for node in tree.nodes:
            if node.path in shown and worth_listing(node) and node.rect:
                scroller = next((p for p in _ancestors(node.path) if p in scrollers), None)
                centre = node.rect[1] + node.rect[3] / 2
                low, high = span.get(scroller, (centre, centre))
                span[scroller] = (min(low, centre), max(high, centre))
        below = above = 0
        hidden = [node for node in tree.nodes if node.path not in shown and worth_listing(node) and node.rect
                  and not _is_scroll_indicator(node) and KEYBOARD_MARK not in node.path]
        for node in collapse(hidden):  # a row counts once, not once per text in it
            y, h = node.rect[1], node.rect[3]
            scroller = next((p for p in _ancestors(node.path) if p in scrollers), None)
            if scroller is not None and scroller in span:
                low, high = span[scroller]
                if y + h / 2 > high and y >= 0:
                    below += 1
                elif y + h / 2 < low and y + h <= height:
                    above += 1
            elif y >= height:
                below += 1
            elif y + h <= 0:
                above += 1
        return below, above

    def _lines(self):
        rows = []
        for node in self.nodes[:MAX_LINES]:
            label = _quoted(node.label, LABEL_CHARS) if node.label else ""
            if node.placeholder and node.value == node.placeholder:
                value = "placeholder=" + _quoted(node.placeholder, VALUE_CHARS)
            elif node.value and node.value != node.label:
                value = "value=" + _quoted(node.value, VALUE_CHARS)
            else:
                value = ""
            extra = []
            if node.identifier:
                extra.append("id=" + _plain(node.identifier, ID_CHARS))
            if not node.enabled:
                extra.append("disabled")
            if node.selected:
                extra.append("selected")
            rows.append((node, role_name(node), label, value, "  ".join(extra)))
        label_width = min(max((len(row[2]) for row in rows), default=0), 30)
        value_width = min(max((len(row[3]) for row in rows), default=0), 26)
        # The ref column fits the longest ref these lines could take, so a line's length is known before
        # its ref is issued, and only the lines that fit get one.
        ref_width = max(4, self.book.widest(len(rows)) + 2)
        bodies, used = [], 0
        for node, role, label, value, extra in rows:
            parts = [label.ljust(label_width) if (value or extra) else label]
            if value_width:
                parts.append(value.ljust(value_width) if extra else value)
            parts.append(extra)
            body = f"{role:<8}" + "  ".join(parts).rstrip()
            if used + ref_width + len(body) + 1 > OUTLINE_BUDGET:
                break
            used += ref_width + len(body) + 1
            bodies.append((node.path, body))
        refs = self.book.number(self.fingerprint, [path for path, _body in bodies])
        lines = [f"{ref:<{ref_width}}{body}" for ref, (_path, body) in zip(refs, bodies)]
        return lines, refs, len(lines)

    def node(self, ref):
        path = self.refs.get(ref)
        return next((node for node in self.nodes if node.path == path), None) if path else None

    def ref_of(self, node):
        return next((ref for ref, path in self.refs.items() if path == node.path), None)

    def footer(self):
        total = len(self.nodes)
        parts = [f"{total} shown" if self.listed == total else f"{total} shown, {self.listed} listed"]
        if self.below:
            parts.append(f"{self.below} more below: swipe up")
        if self.above:
            parts.append(f"{self.above} more above: swipe down")
        return "(" + "; ".join(parts) + ")"

    def render(self, alert=None):
        size = f"{self.width:g}×{self.height:g} pt" if self.width and self.height else "size unknown"
        header = (f"Screen  {self.tree.bundle_id or 'unknown app'}  {size}  "
                  f"keyboard: {'shown' if self.keyboard else 'none'}  alert: {alert_text(alert)}")
        return "\n".join([header, *self.lines, self.footer()])

    def structured(self, alert=None):
        # Claude Code shows the model this JSON rather than the text outline, so an element leaves out its defaults.
        elements = []
        for ref, path in self.refs.items():
            node = next(node for node in self.nodes if node.path == path)
            element = {"ref": ref, "role": role_name(node)}
            if node.label:
                element["label"] = _plain(node.label, 200)
            if node.placeholder and node.value == node.placeholder:
                element["placeholder"] = _plain(node.placeholder, 200)
            elif node.value and node.value != node.label:
                element["value"] = _plain(node.value, 200)
            if node.identifier:
                element["id"] = node.identifier
            if not node.enabled:
                element["enabled"] = False
            if node.selected:
                element["selected"] = True
            elements.append(element)
        return {"bundle_id": self.tree.bundle_id or "", "size": [self.width, self.height],
                "keyboard": "shown" if self.keyboard else "none", "alert": alert,
                "elements": elements, "shown": len(self.nodes), "below": self.below, "above": self.above}

    def _stand_in(self, node, by_path):
        if node.path in by_path:
            return by_path[node.path], "own"
        parent = next((p for p in _ancestors(node.path) if p in by_path), None)
        if parent is not None:
            return by_path[parent], "inside"
        prefix = node.path.rstrip("/") + "/"
        inner = next((n for path, n in by_path.items() if path.startswith(prefix)), None)
        if inner is not None:
            return inner, "around"
        twin = next((n for n in by_path.values() if _twin_key(n) == _twin_key(node)), None)
        return (twin, "own") if twin is not None else (None, None)

    def match_lines(self, matches, limit=5):
        lined = set(self.refs.values())
        by_path = {node.path: node for node in self.nodes if node.path in lined}
        groups = {}
        for match in matches:
            stand, how = self._stand_in(match, by_path)
            if stand is None:
                continue
            group = groups.setdefault(stand.path, {"node": stand, "how": how, "count": 0})
            group["count"] += 1
        lines, named = [], 0
        for group in list(groups.values())[:limit]:
            node, how, count = group["node"], group["how"], group["count"]
            note = ""
            if how == "inside":
                note = " (a match inside it)" if count == 1 else f" ({count} matches inside it)"
            elif how == "around":
                note = " (inside a match)"
            lines.append(f"{self.ref_of(node)} {node_summary(node)}{note}")
            named += count
        return lines, named

    def candidates(self, nodes=None, *, near=None, limit=5):
        if nodes is None:
            listed = [self.node(ref) for ref in self.refs]
            words = [w for w in (near or []) if isinstance(w, str) and w]
            scored = []
            for node in listed:
                texts = [t for t in (node.label, node.identifier, node.value) if t]
                score = max((difflib.SequenceMatcher(None, w.casefold(), t.casefold()).ratio()
                             for w in words for t in texts), default=0)
                scored.append((score, node))
            nodes = [node for score, node in sorted(scored, key=lambda pair: -pair[0]) if score >= .5]
        lines = []
        for node in nodes:
            ref = self.ref_of(node)
            if ref:
                lines.append(f"{ref} {node_summary(node)}")
            if len(lines) >= limit:
                break
        return lines


def alert_text(alert):
    if not alert:
        return "none"
    buttons = " ".join(f"[{_plain(b, 30)}]" for b in alert.get("buttons") or [])
    return (_quoted(alert.get("text") or "", 120) + (" " + buttons if buttons else "")).strip()


def overlap(a, b):
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


class Session:
    def __init__(self, run, mode, *, clock, image=True):
        self.run = run
        self.run_id = run.run_id
        self.mode = mode
        self.clock = clock
        self.state = "preparing"
        self.result = None
        self.started = self.last_used = clock()
        self.image = bool(image)
        self.message = "Preparing the simulator."
        self.cancelled = threading.Event()
        self.stop_reason = None
        self.outline = None
        self.book = RefBook()
        self.ready_reported = False
        self.anchor = None
        self.point_screen = None
        self.busy = 0
        self._changed = threading.Condition()
        self.worker = None
        # Codes use_code typed (held in memory only): masked in every result of the server while it runs; and
        # their fields' frames (screen fractions), blacked out of screenshots until the code is off the screen.
        self.secrets = []
        self.secret_rects = []

    def forget_outline(self):
        self.outline = self.anchor = None

    def add_secret(self, secret, rect=None):
        if secret and secret not in self.secrets:
            self.secrets.append(secret)
        if rect is not None:
            self.secret_rects.append(tuple(rect))

    def secret_shown(self, tree):
        if not self.secret_rects:
            return False
        nodes = getattr(tree, "nodes", ())
        texts = " ".join(f"{getattr(n, 'label', '')} {getattr(n, 'value', '')}" for n in nodes)
        shown = any(secret in texts for secret in self.secrets)
        width, height = getattr(tree, "size", (0, 0)) or (0, 0)
        for node in nodes if not shown and width and height else ():
            if getattr(node, "role", "") not in ("TextField", "SecureTextField") or not getattr(node, "value", ""):
                continue
            x, y, w, h = node.rect
            box = (x / width, y / height, w / width, h / height)
            if any(overlap(box, rect) for rect in self.secret_rects):
                shown = True
                break
        if not shown:
            self.secret_rects = []
        return shown

    @property
    def open(self):
        return self.state != "finished"

    def touch(self):
        self.last_used = self.clock()

    def set_state(self, state, result=None):
        with self._changed:
            if self.state == "finished":
                return False
            self.state = state
            if result is not None:
                self.result = result
            self._changed.notify_all()
            return True

    def note(self, message):
        if isinstance(message, str) and message.strip():
            self.message = message.strip()[:300]

    def wait_while(self, states, timeout, cancelled=None, tick=.25):
        end = time.monotonic() + max(0.0, timeout)
        with self._changed:
            while self.state in states:
                left = end - time.monotonic()
                if left <= 0 or (cancelled is not None and cancelled.is_set()):
                    return False
                self._changed.wait(min(tick, left))
            return True

    def steps_so_far(self, limit=20):
        steps = list(getattr(self.run, "steps", None) or [])
        return [str(step.get("text") or step.get("op") or "") for step in steps[-limit:] if isinstance(step, dict)]
