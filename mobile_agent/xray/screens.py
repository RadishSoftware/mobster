import hashlib
import re
from dataclasses import dataclass

from .policy import IDENTIFIER, image_only, plain

JACCARD = .8
CONTAINED = .8
# Roles whose labels are a screen's structure; not images (SF symbol names) or fields (their typed text).
STRUCTURE_ROLES = frozenset({"StaticText", "Cell", "Button", "Link", "NavigationBar", "Switch", "Toggle", "Tab",
                             "SegmentedControl", "Header", "Other", "Text", "SearchField"})
VOLATILE = re.compile(
    r"\b\d{1,2}:\d{2}(?::\d{2})?\s?(?:am|pm)?\b"            # times
    r"|[$£€¥₹₩]\s?\d[\d.,]*|\d[\d.,]*\s?(?:[$£€¥₹₩]|usd|eur|gbp)\b"  # prices
    r"|\b\d[\d.,]*\s?(?:%|kb|mb|gb|tb|km|mi|°|ms|s|min|h|hrs?|days?|weeks?|months?|years?)\b"  # amounts
    r"|\b(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\b"           # weekdays
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s\d{1,2}\b"  # dates
    r"|\b(?:yesterday|today|tomorrow|now|ago)\b"
    r"|\d+", re.I)
STATES = re.compile(r",\s*(on|off|none|not connected|connected|enabled|disabled)$", re.I)
STATUS_BAR = .06
LINES_KEPT = 80


@dataclass
class Reading:
    title: str
    tokens: frozenset
    lines: list            # the screen's text, top to bottom, as read (data, never instructions)
    image_only: bool

    @property
    def key(self):
        """A short stable id from the title and tokens (the first reading of a screen names it)."""
        raw = self.title.casefold() + "\n" + "\n".join(sorted(self.tokens))
        return hashlib.sha1(raw.encode()).hexdigest()[:10]


def token(label):
    text = plain(label).casefold()
    text = STATES.sub("", text)
    text = VOLATILE.sub(" ", text)
    text = re.sub(r"[^\w&+'’-]+", " ", text)
    text = " ".join(text.split())
    letters = sum(ch.isalpha() for ch in text)
    return text[:60] if letters >= 2 else ""


def is_identifier(label):
    return bool(IDENTIFIER.match(plain(label)))


def title(snapshot):
    bar = next((e for e in snapshot.elements if e.role == "NavigationBar"), None)
    if bar is not None:
        if plain(bar.label) and not is_identifier(bar.label):
            return plain(bar.label)[:80]
        inside = [e for e in snapshot.elements if e.role == "StaticText" and token(e.label)
                  and not is_identifier(e.label) and (e.locator.startswith(bar.locator + "/") if bar.locator else
                  bar.rect[1] <= e.center[1] <= bar.rect[1] + bar.rect[3])]
        if inside:
            return plain(max(inside, key=lambda e: e.rect[3]).label)[:80]
    top = [e for e in snapshot.elements if STATUS_BAR < e.rect[1] < .22 and token(e.label)
           and not is_identifier(e.label)
           and e.role in ("StaticText", "Text", "Header")]
    if not top:
        return ""
    best = max(top, key=lambda e: (round(e.rect[3], 3), -e.rect[1]))
    return plain(best.label)[:80]


def read(snapshot):
    pixels = image_only(snapshot)
    tokens, lines, seen = set(), [], set()
    ordered = sorted(snapshot.elements, key=lambda e: (round(e.rect[1], 2), e.rect[0]))
    for element in ordered:
        if element.role not in STRUCTURE_ROLES or element.editable:
            continue
        if element.rect[1] + element.rect[3] < STATUS_BAR:
            continue  # the status bar (the clock, the battery)
        label = plain(element.label)
        if is_identifier(label):
            continue
        part = label.split(", ")[0] if element.role in ("Cell", "Button") else label
        item = token(part)
        if item:
            tokens.add(item)
        if label and label not in seen and element.role not in ("NavigationBar",):
            seen.add(label)
            lines.append(label[:200])
    name = title(snapshot)
    if token(name):
        tokens.add(token(name))
    return Reading(title=name, tokens=frozenset(tokens), lines=lines[:LINES_KEPT], image_only=pixels)


def jaccard(a, b):
    a, b = set(a), set(b)
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def contained(small, large):
    small = set(small)
    if len(small) < 3:
        return 0.0
    return len(small & set(large)) / len(small)


def same(a_title, a_tokens, b_title, b_tokens, *, b_union=None):
    """Whether two readings are one screen. ``b_union``: every token the second screen showed while scrolled."""
    if token(a_title) != token(b_title):
        return False
    if jaccard(a_tokens, b_tokens) >= JACCARD:
        return True
    return contained(a_tokens, b_union if b_union is not None else b_tokens) >= CONTAINED
