"""The locale audit: what `mobster test --locale de,ja,ar` reads in the trees of a check run in a language."""

import re

from .a11y import EDGE, TEXT, _clipped, _describe
from .tree import SCROLL_CONTAINERS

NAME = "l10n"
RTL = frozenset({"ar", "he", "fa", "ur", "ps", "sd", "ug", "yi", "dv"})
# Languages whose own script isn't Latin: an ASCII sentence in one of them was not translated.
NON_LATIN = RTL | frozenset({"ja", "ko", "zh", "ru", "uk", "bg", "sr", "mk", "be", "el", "th", "hi", "bn", "ta", "te",
                             "ml", "kn", "gu", "pa", "mr", "ne", "si", "my", "km", "lo", "ka", "hy", "am", "mn"})
GUIDELINES = {"text-truncated": "Localisation: long strings",
              "untranslated-string": "Localisation: missing translation",
              "rtl-not-mirrored": "Localisation: right-to-left layout"}
ELLIPSIS = re.compile(r".*(…|\.\.\.)\s*$", re.S)
ASCII_WORDS = re.compile(r"[A-Za-z]{2,}(?:[ '\-][A-Za-z]{2,})+|[A-Za-z]{5,}")
RTL_SCRIPT = re.compile(r"[֐-ࣿיִ-﷿ﹰ-﻿]")
WIDE = 0.6      # a container at least this share of the screen's width counts as a full-width row
NARROW = 0.7    # text narrower than this share of its row can show which edge it is flush to


def language(locale):
    return (locale or "").replace("-", "_").split("_")[0].lower()


def is_rtl(locale):
    return language(locale) in RTL


def _finding(rule, node, message, frame):
    name, where = _describe(node)
    x, y, w, h = node.rect
    return {"id": rule, "audit": NAME, "guideline": GUIDELINES[rule], "level": "warn", "frame": frame,
            "message": f"{name} {where}: {message}", "evidence": f"{name} {where}", "baseline": False,
            "element": {"role": node.role, "label": node.label, "identifier": node.identifier, "path": node.path,
                        "rect": [x, y, w, h]}}


def _ancestors(path):
    while "/" in path.strip("/"):
        path = path.rsplit("/", 1)[0]
        yield path


def audit_tree(tree, locale, *, frame=None):
    """The findings of one screen's tree in ``locale``."""
    shown = tree.shown()
    by_path = {node.path: node for node in tree.nodes}
    width = tree.size[0]
    lang = language(locale)
    out = []
    for node in shown:
        label = node.label.strip()
        x, y, w, h = node.rect
        if node.role in TEXT and label and w > 0 and h > 0:
            if ELLIPSIS.fullmatch(label):
                out.append(_finding("text-truncated", node,
                                    "the text ends in an ellipsis, so it was cut short. Let it wrap or give it room "
                                    "(SwiftUI: remove .lineLimit; UIKit: numberOfLines = 0).", frame))
            else:
                clipped = _clipped(node, by_path, width)
                if clipped:
                    out.append(_finding("text-truncated", node,
                                        f"in {locale} it is {clipped}. Let the text wrap or the container grow.",
                                        frame))
        if lang in NON_LATIN and node.role in TEXT and label and _ascii_sentence(label):
            out.append(_finding("untranslated-string", node,
                                f"the text is plain English in {locale}. Translate it, or ignore this if it is a "
                                "brand name.", frame))
    if is_rtl(locale):
        out.extend(_not_mirrored(shown, by_path, width, frame))
    return out


def _ascii_sentence(label):
    letters = re.sub(r"[^A-Za-z]", "", label)
    return bool(ASCII_WORDS.fullmatch(" ".join(label.split()))) and len(letters) >= 5 \
        and len(re.sub(r"[A-Za-z '\-]", "", label)) == 0


def _not_mirrored(shown, by_path, width, frame):
    out = []
    for bar in (node for node in shown if node.role == "NavigationBar"):
        bx, _, bw, _ = bar.rect
        buttons = [node for node in shown if node.role == "Button" and node.path.startswith(bar.path + "/")
                   and node.rect[2] > 0]
        if buttons and all(node.rect[0] + node.rect[2] / 2 < bx + bw / 2 for node in buttons) and bw >= width * WIDE:
            out.append(_finding("rtl-not-mirrored", bar,
                                "every button sits in the left half, so the back button didn't move to the right. "
                                "Use leading and trailing edges, not left and right.", frame))
    for node in shown:
        label = node.label.strip()
        x, y, w, h = node.rect
        if node.role != "StaticText" or not label or not RTL_SCRIPT.search(label) or w <= 0 or h <= 0:
            continue
        row = next((by_path[path] for path in _ancestors(node.path) if path in by_path
                    and by_path[path].rect[2] >= width * WIDE and by_path[path].role not in SCROLL_CONTAINERS), None)
        if row is None or w > row.rect[2] * NARROW:
            continue
        left, right = x - row.rect[0], (row.rect[0] + row.rect[2]) - (x + w)
        if left + EDGE < right and left < row.rect[2] * 0.1:
            out.append(_finding("rtl-not-mirrored", node,
                                "right-to-left text is flush to the left edge of its row. Align it to the leading "
                                "edge (SwiftUI: .multilineTextAlignment(.leading); UIKit: .natural).", frame))
    return out


class Collector:
    """A run's screens, audited once each (``add(tree, frame)``), like a11y.Collector."""

    LIMIT = 60

    def __init__(self, locale):
        self.locale = locale
        self._seen, self._found, self._keys = {}, [], set()

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
        for finding in audit_tree(tree, self.locale, frame=frame):
            element = finding["element"]
            key = (finding["id"], element["role"], element["label"], element["identifier"],
                   tuple(round(n) for n in element["rect"]))
            if key not in self._keys:
                self._keys.add(key)
                self._found.append(finding)
        self._seen[fingerprint] = (start, len(self._found))
        return True

    def findings(self):
        return list(self._found)
