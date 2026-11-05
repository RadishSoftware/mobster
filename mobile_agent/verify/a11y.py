"""The accessibility audit (docs/accessibility.mdx): rules on the trees a run reads, with no model or pixels."""

import json
import re

from .conditions import CONTENT_SIZES
from .tree import SCROLL_CONTAINERS, STRUCTURAL

NAME = "a11y"
MIN_TARGET = 44.0
EDGE = 1.0   # points of slack for rounding in frames
CONTROLS = frozenset({"Button", "Switch", "Toggle", "Link", "Slider", "Stepper", "TextField", "SecureTextField",
                      "SearchField", "TextView"})
# Controls whose own value is not a name ("0", "1", "50%"), so only a label names them.
ENTRY = frozenset({"TextField", "SecureTextField", "SearchField", "TextView"})
TARGETS = frozenset({"Button", "Link", "Switch", "Toggle"})
TEXT = frozenset({"StaticText", "Button", "Link", "TextField", "SecureTextField", "SearchField", "TextView"})
LARGE_SIZES = CONTENT_SIZES[CONTENT_SIZES.index("extra-large"):]
GUIDELINES = {"control-no-label": "WCAG 4.1.2", "target-too-small": "HIG 44 pt", "image-no-label": "WCAG 1.1.1",
              "text-clipped": "WCAG 1.4.4", "duplicate-label": "WCAG 2.4.6", "label-unhelpful": "WCAG 2.4.6",
              "unreachable-element": "WCAG 2.1.1"}
LEVELS = ("warn", "strict")
GENERIC = frozenset({"button", "image", "icon", "img", "picture", "photo", "untitled", "label", "view", "item"})
FILE_LABEL = re.compile(r".+\.(png|jpe?g|pdf|svg|gif|heic|webp)", re.I)
CODE_LABEL = re.compile(r"[a-z][a-z0-9]*(?:[_-][a-z0-9]+)+")


def large_text(content_size):
    return content_size in LARGE_SIZES


def _describe(node):
    name = f"{node.role} \"{node.label}\"" if node.label.strip() else (
        f"{node.role} (id {node.identifier})" if node.identifier else node.role)
    x, y, w, h = node.rect
    return name, f"at {x:g}, {y:g}, {w:g} x {h:g} pt"


def _finding(rule, node, message, frame):
    name, where = _describe(node)
    x, y, w, h = node.rect
    return {"id": rule, "audit": NAME, "guideline": GUIDELINES[rule], "level": "fail", "frame": frame,
            "message": f"{name} {where}: {message}", "evidence": f"{name} {where}",
            "element": {"role": node.role, "label": node.label, "identifier": node.identifier, "path": node.path,
                        "rect": [x, y, w, h]}}


def _ancestors(path):
    while "/" in path.strip("/"):
        path = path.rsplit("/", 1)[0]
        yield path


def audit_tree(tree, *, content_size=None, frame=None):
    """The findings of one screen's tree."""
    shown = tree.shown()
    by_path = {node.path: node for node in tree.nodes}
    labelled_controls = {node.path for node in shown if node.role in CONTROLS and node.label.strip()}
    width = tree.size[0]
    out = []
    for node in shown:
        x, y, w, h = node.rect
        if node.role in CONTROLS and not node.label.strip() and not (node.role in ENTRY and node.placeholder.strip()):
            out.append(_finding("control-no-label", node,
                                "it has no accessibility label, so VoiceOver can't say what it does. Give it one "
                                "(SwiftUI: .accessibilityLabel; UIKit: accessibilityLabel).", frame))
        if node.role in TARGETS and w > 0 and h > 0 and (w < MIN_TARGET or h < MIN_TARGET):
            out.append(_finding("target-too-small", node,
                                f"the tap target is {w:g} x {h:g} pt, under {MIN_TARGET:g} x {MIN_TARGET:g}. "
                                "Make the control or its tappable area at least 44 pt each way (SwiftUI: "
                                ".frame(minWidth: 44, minHeight: 44) or .contentShape).", frame))
        if node.role == "Image" and not node.label.strip() and w > 1 and h > 1 \
                and not any(path in labelled_controls for path in _ancestors(node.path)):
            out.append(_finding("image-no-label", node,
                                "the image has no label. Label it, or hide it from accessibility if it is "
                                "decoration (SwiftUI: .accessibilityHidden(true)).", frame))
        if (node.role in CONTROLS or node.role == "Image") and node.label.strip() and unhelpful(node.label) \
                and not (node.role in ENTRY):
            out.append(_finding("label-unhelpful", node,
                                f"the label \"{node.label.strip()}\" doesn't say what it is for. Name the action or "
                                "the picture (SwiftUI: .accessibilityLabel).", frame))
        if large_text(content_size) and node.role in TEXT and node.label.strip() and w > 0 and h > 0:
            clipped = _clipped(node, by_path, width)
            if clipped:
                out.append(_finding("text-clipped", node,
                                    f"at the {content_size} text size it is {clipped}. Let the text wrap or the "
                                    "container grow (SwiftUI: remove fixed frames and lineLimit).", frame))
    out.extend(_duplicates(shown, frame))
    out.extend(_unreachable(tree, by_path, frame))
    return out


def unhelpful(label):
    text = label.strip()
    return text.lower() in GENERIC or bool(FILE_LABEL.fullmatch(text)) or bool(CODE_LABEL.fullmatch(text))


def _duplicates(shown, frame):
    groups = {}
    for node in shown:
        if node.role in TARGETS and node.label.strip() and node.rect[2] > 0 and node.rect[3] > 0:
            groups.setdefault((node.role, " ".join(node.label.split()).casefold()), []).append(node)
    return [_finding("duplicate-label", nodes[0],
                     f"{len(nodes)} {nodes[0].role.lower()}s on this screen are all labelled "
                     f"\"{nodes[0].label.strip()}\", "
                     "so VoiceOver reads them alike. Say what each one acts on (SwiftUI: .accessibilityLabel).", frame)
            for nodes in groups.values() if len(nodes) > 1 and len({tuple(n.rect) for n in nodes}) > 1]


def _unreachable(tree, by_path, frame):
    width, height = tree.size
    out = []
    for node in tree.nodes:
        x, y, w, h = node.rect
        if node.role not in CONTROLS or not node.label.strip() or not node.enabled or w <= 0 or h <= 0:
            continue
        if not (x + w <= 0 or x >= width or y + h <= 0 or y >= height):
            continue
        if any(by_path[path].role in SCROLL_CONTAINERS for path in _ancestors(node.path) if path in by_path):
            continue
        out.append(_finding("unreachable-element", node,
                            "its frame is wholly off the screen and it is in no scroll view, so nothing can reach it. "
                            "Move it on screen, or remove it from the layout when it is meant to be hidden.", frame))
    return out


def _clipped(node, by_path, screen_width):
    x, y, w, h = node.rect
    ancestors = [by_path[path] for path in _ancestors(node.path) if path in by_path]
    in_scroll = any(item.role in SCROLL_CONTAINERS for item in ancestors)
    if x < -EDGE or x + w > screen_width + EDGE:
        if not in_scroll:
            return "wider than the screen"
    parent = next((item for item in ancestors if item.role not in STRUCTURAL and item.rect[2] * item.rect[3] > 0), None)
    if parent is not None and parent.role not in SCROLL_CONTAINERS:
        px, py, pw, ph = parent.rect
        if x < px - EDGE or x + w > px + pw + EDGE or y < py - EDGE or y + h > py + ph + EDGE:
            return f"outside its {parent.role}"
    return None


def baseline_key(scope, finding):
    """A finding's baseline key: the check's file, the rule, the role, label and identifier. Not the frame, so a layout
    shift doesn't make an old finding new."""
    element = finding["element"]
    return json.dumps((scope, finding["id"], element["role"], element["label"], element["identifier"]),
                      ensure_ascii=False, separators=(",", ":"))


class Collector:
    LIMIT = 60

    def __init__(self, content_size=None, *, level="strict", baseline=frozenset(), scope=""):
        self.content_size, self.level, self.baseline, self.scope = content_size, level, baseline, scope
        self._seen, self._found = {}, []
        self._keys = set()

    def add(self, tree, frame=None):
        fingerprint = tree.audit_fingerprint()
        if fingerprint in self._seen:
            if frame is not None:
                start, end = self._seen[fingerprint]
                for finding in self._found[start:end]:
                    if finding["frame"] is None:
                        finding["frame"] = frame
            return True
        if len(self._seen) >= self.LIMIT:
            return False
        start = len(self._found)
        for finding in audit_tree(tree, content_size=self.content_size, frame=frame):
            element = finding["element"]
            key = (finding["id"], element["role"], element["label"], element["identifier"],
                   tuple(round(n) for n in element["rect"]))
            if key not in self._keys:
                self._keys.add(key)
                finding["key"] = baseline_key(self.scope, finding)
                known = finding["key"] in self.baseline
                finding["level"] = "known" if known else ("fail" if self.level == "strict" else "warn")
                finding["baseline"] = known
                self._found.append(finding)
        self._seen[fingerprint] = (start, len(self._found))
        return True

    @property
    def screens(self):
        return len(self._seen)

    def findings(self):
        return list(self._found)
