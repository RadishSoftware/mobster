"""Learned extractors: a screen the model read into rows is read again by code.

After the model reads a screen (``read_screen``) every value it returned is found, word for word, on one line of the
screen or in one of the ", " parts iOS joins a row's texts with. The extractor keeps which line (and part) fed which
field of which row, and the screen's *skeleton*: each line's role, and for a line no value came from, a hash of its
words (digit runs folded, so a clock or a count doesn't change it). A later screen with the same skeleton is read by
taking those lines. Anything else is a miss and the model reads it.

The confidence check is the skeleton itself: the same number of lines, the same roles, the same hashed labels
where no value was, and every number field still a number. A value the model reworded (not a line's words) is not
learned. The store keeps no screen text: roles, hashes and line numbers only, in a replay_core path of kind
"extractor" (replay_core/store.py's Cache: 0600 files, written atomically).
"""

import hashlib
import re

from ..replay_core.path import signature_hash
from ..replay_core.store import Cache, env_of

MAX_PER_KEY = 8
MAX_LINES = 400
NUMBER = re.compile(r"([+-]?(?:\d+\.?\d*|\.\d+))(?:\s+[^\W\d_]+)?")   # "4.8", "4.8 stars"


def _hash(words):
    return hashlib.sha1(re.sub(r"\d+", "#", words).encode()).hexdigest()[:10]


def _number(words):
    found = NUMBER.fullmatch(words)
    return float(found.group(1)) if found else None


def _kind(value):
    return "n" if isinstance(value, (int, float)) and not isinstance(value, bool) else "s"


def _parts(words):
    """A composite label's parts: iOS joins a row's texts with ", " ("Rye loaf, $6.50, 4.8 stars")."""
    return words.split(", ")


def _matches(value, words):
    if isinstance(value, str):
        return words == value
    number = _number(words)
    return number is not None and number == value


def _where(value, lines, taken, after):
    """(line, part) holding ``value`` exactly, the whole line (part None) before a part of it; the first after the
    previous pick, else the first unused. None when no line has it."""
    found = []
    for index, (_, words) in enumerate(lines):
        if (index, None) not in taken and _matches(value, words):
            found.append((index, None))
        parts = _parts(words)
        if len(parts) > 1:
            found.extend((index, k) for k, part in enumerate(parts)
                         if (index, k) not in taken and _matches(value, part))
    later = [spot for spot in found if (spot[0], -1 if spot[1] is None else spot[1]) > after]
    return (later or found or [None])[0]


def learn(lines, rows, fields):
    """An extractor for ``rows`` read off ``lines`` ([(role, words)]), or None when a value isn't a line's words
    (or one of its ", " parts)."""
    if not rows or len(lines) > MAX_LINES:
        return None
    taken, picks, kinds, after = set(), [], {}, (-1, -1)
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(fields):
            return None
        pick = []
        for name in fields:
            value = row[name]
            if value is None:
                pick.append(None)
                continue
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                return None
            kind = _kind(value)
            if kinds.setdefault(name, kind) != kind or (kind == "s" and not value):
                return None
            spot = _where(value, lines, taken, after)
            if spot is None:
                return None
            taken.add(spot)
            after = (spot[0], -1 if spot[1] is None else spot[1])
            pick.append(list(spot))
        picks.append(pick)
    whole = {name: kind == "n" and all(float(row[name]).is_integer() for row in rows if row[name] is not None)
             for name, kind in kinds.items()}
    used = {line for line, _ in taken}
    skeleton = [[role, None if index in used else _hash(words)] for index, (role, words) in enumerate(lines)]
    return {"skeleton": skeleton, "rows": picks, "kinds": [kinds.get(name, "s") for name in fields],
            "whole": [bool(whole.get(name)) for name in fields]}


def _text(lines, spot):
    line, part = spot
    words = lines[line][1]
    return words if part is None else _parts(words)[part]


def extract(extractor, lines, fields):
    """The rows ``extractor`` reads from ``lines``, or None when the screen isn't the one it was learned on."""
    skeleton = extractor.get("skeleton")
    if not isinstance(skeleton, list) or len(skeleton) != len(lines):
        return None
    for (role, words), (want_role, want_hash) in zip(lines, skeleton):
        if role != want_role or (want_hash is not None and _hash(words) != want_hash):
            return None
    kinds, whole = extractor["kinds"], extractor["whole"]
    rows = []
    for pick in extractor["rows"]:
        row = {}
        for position, name in enumerate(fields):
            if pick[position] is None:
                row[name] = None
                continue
            try:
                text = _text(lines, pick[position])
            except IndexError:      # a composite label with fewer parts than it had
                return None
            if kinds[position] == "n":
                number = _number(text)
                if number is None:
                    return None
                row[name] = int(number) if whole[position] else number
            else:
                row[name] = text
        rows.append(row)
    return rows


class Extractors:
    """The extractors in ``directory``, one replay_core path per (app, fields); counts of hits and misses kept in it."""

    def __init__(self, directory):
        self.cache = Cache(directory)

    @staticmethod
    def identity(app, fields):
        return {"kind": "extractor", "app": app or None, "fields": list(fields)}

    @staticmethod
    def env(version=None, runtime=None, device=None):
        return env_of(version, runtime, device)

    def _load(self, identity, env):
        return self.cache.load(identity, env)

    def read(self, app, fields, lines, env=None):
        """The rows read by code, or None (no extractor fits: the model reads, then ``remember``)."""
        env = env or self.env()
        identity = self.identity(app, fields)
        path = self._load(identity, env)
        if path is None:
            return None
        rows = None
        for extractor in path.get("extractors") or ():
            try:
                rows = extract(extractor, lines, fields)
            except (KeyError, IndexError, TypeError, ValueError):
                rows = None
            if rows is not None:
                break
        path["hits" if rows is not None else "misses"] = int(path.get("hits" if rows is not None else "misses")
                                                              or 0) + 1
        self._save(path)
        return rows

    def remember(self, app, fields, lines, rows, env=None):
        """Keep the extractor for ``rows`` the model read off ``lines``; True when one was learned."""
        extractor = learn(lines, rows, fields)
        if extractor is None:
            return False
        env = env or self.env()
        identity = self.identity(app, fields)
        path = self._load(identity, env)
        if path is None:
            from ..replay_core.path import new_path
            step = {"op": "READ_LIST", "app": app or None, "live": True,
                    "screen": {"hash": signature_hash(extractor["skeleton"])}}
            path = new_path([step], kind="extractor", identity=identity, env=env)
            path["extractors"], path["hits"], path["misses"] = [], 0, 0
        known = [e for e in path["extractors"] if e.get("skeleton") != extractor["skeleton"]]
        path["extractors"] = [extractor, *known][:MAX_PER_KEY]
        self._save(path)
        return True

    def counts(self, app, fields, env=None):
        path = self._load(self.identity(app, fields), env or self.env())
        return {"hits": int((path or {}).get("hits") or 0), "misses": int((path or {}).get("misses") or 0)}

    def _save(self, path):
        try:
            self.cache.save(path)
        except (OSError, ValueError):
            pass
