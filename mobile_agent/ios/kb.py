"""The iOS knowledge base."""

import math
import re
from dataclasses import dataclass
from functools import lru_cache

NEWEST = 26
OLDEST = 18
# BM25's usual constants; titles and keywords count twice (they name the task, the text explains it).
K1, B = 1.2, .75
TITLE_WEIGHT = 2
MAX_K = 8
SLICE_CHARS = 900
# A request match joins a screen's slice at this share of the best match's score.
RELATED = .6
WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
STOP = frozenset("a an and are as at be by can do does for from how i if in into is it its me my of on or so that "
                 "the then this to up what when where which with you your".split())
SPRINGBOARD = "com.apple.springboard"
SETTINGS = "com.apple.Preferences"
SPOTLIGHT = "com.apple.Spotlight"


@dataclass(frozen=True)
class Entry:
    id: str
    topic: str
    title: str
    ios: tuple            # (min, max) iOS majors, inclusive
    text: str
    keywords: tuple = ()
    screens: tuple = ()
    source: tuple = ()

    def holds_for(self, major):
        return self.ios[0] <= major <= self.ios[1]

    def line(self):
        """The entry as one line for the model: its title, then its text."""
        return f"{self.title}: {self.text}"

    def public(self):
        return {"id": self.id, "topic": self.topic, "title": self.title, "ios": list(self.ios), "text": self.text}


def major(version):
    """The iOS major of "26.0.1", "iOS 18.4", 26 or None; None when it can't be read."""
    if isinstance(version, bool):
        return None
    if isinstance(version, int):
        return version
    found = re.search(r"(\d{1,2})(?:\.\d+)*", str(version or ""))
    return int(found.group(1)) if found else None


def known(version):
    """The iOS major the base answers for."""
    wanted = major(version)
    if wanted is None or wanted > NEWEST:
        return NEWEST
    return max(OLDEST, wanted)


def words(text):
    return [w for w in WORD.findall(str(text or "").casefold().replace("’", "'")) if w not in STOP]


@lru_cache(maxsize=1)
def entries():
    """Every entry, checked: unique ids, a valid version range, text present."""
    from .kb_data import ENTRIES
    out, seen = [], set()
    for raw in ENTRIES:
        low, high = raw["ios"]
        entry = Entry(id=raw["id"], topic=raw["topic"], title=raw["title"], ios=(int(low), int(high)),
                      text=" ".join(raw["text"].split()), keywords=tuple(raw.get("keywords") or ()),
                      screens=tuple(raw.get("screens") or ()), source=tuple(raw.get("source") or ()))
        if entry.id in seen or not entry.text or not OLDEST <= entry.ios[0] <= entry.ios[1] <= NEWEST:
            raise ValueError(f"bad knowledge-base entry {entry.id}")
        seen.add(entry.id)
        out.append(entry)
    return tuple(out)


class Index:
    """BM25 over a set of entries."""

    def __init__(self, items):
        self.items = tuple(items)
        self.docs = []
        for entry in self.items:
            terms = words(entry.text) + TITLE_WEIGHT * (words(entry.title) + words(" ".join(entry.keywords)))
            counts = {}
            for term in terms:
                counts[term] = counts.get(term, 0) + 1
            self.docs.append((counts, len(terms)))
        self.average = sum(length for _, length in self.docs) / max(1, len(self.docs))
        frequency = {}
        for counts, _ in self.docs:
            for term in counts:
                frequency[term] = frequency.get(term, 0) + 1
        n = len(self.docs)
        self.idf = {term: math.log(1 + (n - f + .5) / (f + .5)) for term, f in frequency.items()}

    def scores(self, text):
        terms = set(words(text))
        out = []
        for entry, (counts, length) in zip(self.items, self.docs):
            score = 0.0
            for term in terms:
                tf = counts.get(term)
                if tf:
                    score += self.idf[term] * tf * (K1 + 1) / (tf + K1 * (1 - B + B * length / self.average))
            if score > 0:
                out.append((score, entry))
        out.sort(key=lambda pair: -pair[0])
        return out


@lru_cache(maxsize=4)
def index_for(ios_major):
    return Index(entry for entry in entries() if entry.holds_for(ios_major))


def query(text, ios=None, k=4):
    """The ``k`` entries that best answer ``text`` on that iOS version (BM25), best first; () for no match."""
    k = max(1, min(int(k or 4), MAX_K))
    return tuple(entry for _, entry in index_for(known(ios)).scores(text)[:k])


def answer(text, ios=None, k=4):
    """``query`` as text for a person or a model: one line per entry, with the version it holds for."""
    found = query(text, ios, k)
    version = known(ios)
    head = f"iOS {version}" + ("" if major(ios) == version else
                               f" (the phone's version {'is unknown' if major(ios) is None else 'is ' + str(ios)})")
    if not found:
        return f"{head}: nothing in Mobster's iOS guide matches “{str(text)[:80]}”."
    return head + ":\n" + "\n".join(f"- {entry.line()}" for entry in found)


# Which screen of iOS's own is in front, from its labels: (name, the entries' topic or id prefixes, a test).
def screen_of(bundle, labels, keyboard=""):
    text = {" ".join(str(label or "").split()).casefold() for label in labels or ()}
    # OCR reads a search field's magnifier as a "Q" before its placeholder ("Q App Library", "Q Search").
    text |= {label[2:] for label in text if label.startswith("q ")}
    if bundle == SPOTLIGHT or "spotlightsearchfield" in text or "top hit" in text and bundle in (SPRINGBOARD, ""):
        return "spotlight"
    if bundle == SPRINGBOARD or not bundle:
        if "app library" in text or "dewey-search-field" in text:
            return "app_library"
        if "add controls" in text or {"focus", "screen mirroring"} <= text:
            return "control_center"
        if any(label.startswith("page ") and " of " in label for label in text) or "page control" in text \
                or "q search" in text:
            return "home"
    if bundle == SETTINGS:
        return "settings"
    if keyboard == "visible":
        return "keyboard"
    return ""


SLICES = {
    "home": ("home.spotlight.hidden_app", "home.app_library.search"),
    "app_library": ("home.app_library.search",),
    "spotlight": ("home.spotlight.hidden_app", "home.app_library.search"),
    "control_center": ("panels.control_center.controls", "panels.control_center.wifi_bluetooth"),
    "settings": ("settings.search", "settings.search.no_results"),
    "keyboard": ("keyboard.dismiss", "keyboard.return_key"),
}
# Where an entry about the request may join a screen's slice: entries written for that screen (or for any).
SCREEN_BUNDLES = {"home": SPRINGBOARD, "app_library": SPRINGBOARD, "spotlight": SPRINGBOARD,
                  "control_center": SPRINGBOARD, "settings": SETTINGS, "keyboard": "keyboard"}


def slice_for(bundle, labels, ios=None, keyboard="", request="", k=3):
    """The entries about the iOS screen in front, at most ``k``."""
    screen = screen_of(bundle, labels, keyboard)
    prefixes = SLICES.get(screen)
    if not prefixes:
        return ()
    version = known(ios)
    out = []
    if request:
        where = {SCREEN_BUNDLES.get(screen), "any"} | ({SPOTLIGHT} if screen == "spotlight" else set())
        scored = index_for(version).scores(request)
        best = scored[0][0] if scored else 0
        for score, entry in scored[:8]:
            if score >= RELATED * best and where & set(entry.screens) and len(out) < 2:
                out.append(entry)
    for prefix in prefixes:
        for entry in entries():
            if entry.holds_for(version) and (entry.id == prefix or entry.id.startswith(prefix + ".")) \
                    and entry not in out:
                out.append(entry)
                break
    return tuple(out[:k])


def slice_text(bundle, labels, ios=None, keyboard="", request=""):
    """``slice_for`` as a short prompt section of whole entries, at most SLICE_CHARS; "" off iOS's own screens."""
    lines = []
    for entry in slice_for(bundle, labels, ios, keyboard, request):
        line = f"- {entry.line()}"
        if sum(len(x) + 1 for x in lines) + len(line) > SLICE_CHARS:
            break
        lines.append(line)
    if not lines:
        return ""
    return f"\n\nHow iOS {known(ios)} works here (Mobster's iOS guide):\n" + "\n".join(lines)
