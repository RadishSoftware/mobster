"""Packs that survive an app's redesigns and A/B tests: drift is logged, the general agent takes over at once, and a
screen the pack missed can become a learned variant, used only after it passes a replay check."""

import json
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import core

KIND = "variant"            # the heal file's lines this module owns
MIN_SIGNALS = 3
MAX_SIGNALS = 6
MAX_VARIANTS = 12          # per pack, version and screen
MAX_LABEL = 28
CHROME_TOP, CHROME_BOTTOM = .3, .85
# A name, a handle, a number, a time or a link: never kept as a signal.
PERSONAL = re.compile(r"(@|https?://|\d{2,}|\b\d+\s?[smhdw]\b|\bago\b|[\w.]+\.(com|net|org)\b|"
                      r"^[A-Za-z0-9]+[._][A-Za-z0-9._]+$|^[A-Z][a-z]+ [A-Z][a-z]+$)", re.I)
_stores = {}
_lock = threading.Lock()


def fingerprint(snapshot):
    """The screen's structural hash (replay_core): which roles and label shapes, no text. None when unknown."""
    try:
        from ..replay_core.path import screen_hashes
        hashed = screen_hashes(snapshot)
        return hashed["hash"] if hashed else None
    except Exception:  # noqa: BLE001
        return None


def chrome(snapshot):
    """The app's own words on ``snapshot``: short labels in its top part (titles, filters, the search field), the
    current tab, and named buttons in between, with no name, handle, number, time or link in them."""
    picked, seen = [], set()
    for element in getattr(snapshot, "elements", ()) or ():
        label = core.plain(element.label)
        if not label or len(label) > MAX_LABEL or PERSONAL.search(label) or label.casefold() in seen:
            continue
        y = core.centre_y(element)
        if y > CHROME_BOTTOM:
            if not core.is_selected(element):
                continue
        elif y >= CHROME_TOP and element.role != "Button":
            continue
        seen.add(label.casefold())
        picked.append((label, element.role, round(y, 2)))
    picked.sort(key=lambda item: item[2])
    return picked


@dataclass(frozen=True)
class Variant:
    id: str
    pack: str
    version: str
    screen: str
    fingerprint: str
    signals: tuple                                   # ({"pattern", "role", "region"}, ...)
    pictured: bool
    status: str = "proposed"                         # proposed | confirmed | trusted
    runs: tuple = ()
    at: float = 0.0
    checked_at: float = 0.0

    def spec(self, pack):
        signals = tuple(core.Signal(re.compile(s["pattern"], re.I), s.get("role"), tuple(s["region"])
                                    if s.get("region") else None) for s in self.signals)
        title = pack.screen_title(self.screen)
        if self.pictured:
            return core.ScreenSpec(self.screen, title, (), "learned", ocr=signals)
        return core.ScreenSpec(self.screen, title, signals, "learned")


class Learned:
    """The variants this Mac learned, by pack, app version and screen (a JSON file beside the journal)."""

    def __init__(self, path, clock=time.time):
        self.path = Path(path) if path else None
        self.clock = clock
        self.lock = threading.Lock()
        self.version_on_phone = {}                   # pack key -> the app version the phone has, when known
        newest = {}
        if self.path is not None and self.path.exists():
            try:
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if item.get("kind") != KIND:
                        continue
                    item = {k: v for k, v in item.items() if k in Variant.__dataclass_fields__}
                    newest[item["id"]] = Variant(**{**item, "signals": tuple(item.get("signals") or ()),
                                                    "runs": tuple(item.get("runs") or ())})
            except (OSError, TypeError, KeyError):
                newest = {}
        self.variants = list(newest.values())

    def _write(self, variant):
        """One line for ``variant``'s new state in the heal file (its newest line is its state)."""
        if self.path is None:
            return
        from .. import hints
        hints._append(self.path, {"kind": KIND, "source": "pack", **variant.__dict__, "signals": list(variant.signals),
                                  "runs": list(variant.runs)})

    def _replace(self, old, new):
        self.variants = [new if v is old else v for v in self.variants]
        self._write(new)

    def propose(self, pack, snapshot, screen, version, run_id):
        """A candidate variant of ``screen`` from ``snapshot`` (the pack missed it): its id, or None when the screen
        shows too few of the app's own words to learn from."""
        words = chrome(snapshot)[:MAX_SIGNALS]
        if len(words) < MIN_SIGNALS:
            return None
        pictured = core.image_only(snapshot)
        signals = tuple({"pattern": "^" + re.escape(label) + "$", "role": None if pictured else role,
                         "region": [max(0.0, round(y - .06, 2)), min(1.0, round(y + .06, 2))]}
                        for label, role, y in words)
        mark = fingerprint(snapshot) or ""
        with self.lock:
            same = next((v for v in self.variants if (v.pack, v.version, v.screen) == (pack.key, version or "", screen)
                         and (v.fingerprint == mark and mark or v.signals == signals)), None)
            if same is not None:
                if run_id and run_id not in same.runs:
                    self._replace(same, Variant(**{**same.__dict__, "runs": (*same.runs, run_id)[-8:]}))
                return same.id
            kept = [v for v in self.variants if (v.pack, v.version, v.screen) == (pack.key, version or "", screen)]
            if len(kept) >= MAX_VARIANTS:
                return None
            variant = Variant(f"{pack.key}-{screen}-{int(self.clock() * 1000) % 10**10}", pack.key, version or "",
                              screen, mark, signals, pictured, "proposed", (run_id,) if run_id else (), self.clock())
            self.variants.append(variant)
            self._write(variant)
            return variant.id

    def confirm_run(self, run_id):
        """The run finished completed: the general agent finished from the screens the pack missed in it."""
        changed = 0
        with self.lock:
            for variant in list(self.variants):
                if variant.status == "proposed" and run_id in variant.runs:
                    self._replace(variant, Variant(**{**variant.__dict__, "status": "confirmed"}))
                    changed += 1
        return changed

    def check(self, pack, snapshot, screen, version):
        """The replay check, on a fresh screen the pack missed again: a confirmed variant of ``screen`` that names it,
        while no built-in screen of another name does, becomes trusted."""
        trusted = []
        with self.lock:
            others = [spec for spec in pack.screens if spec.name != screen]
            rival = max((core.score(spec, snapshot)[0] for spec in others), default=0.0)
            for variant in list(self.variants):
                if (variant.pack, variant.version, variant.screen, variant.status) != (pack.key, version or "", screen,
                                                                                      "confirmed"):
                    continue
                if core.image_only(snapshot) != variant.pictured:
                    continue
                value = core.score(variant.spec(pack), snapshot)[0]
                if value >= core.SURE and rival < core.SURE:
                    new = Variant(**{**variant.__dict__, "status": "trusted", "checked_at": self.clock()})
                    self._replace(variant, new)
                    trusted.append(new.id)
        return trusted

    def specs(self, pack):
        """The trusted variants of ``pack`` as ScreenSpecs, for the app version on the phone (all, when unknown). An
        act's control is never found through one: core.control_for reads the pack's own patterns."""
        version = self.version_on_phone.get(pack.key)
        return tuple(v.spec(pack) for v in self.variants if v.pack == pack.key and v.status == "trusted"
                     and (not version or v.version in (version, "")))

    def summary(self, pack_key):
        counts = {}
        for variant in self.variants:
            if variant.pack == pack_key:
                counts[variant.status] = counts.get(variant.status, 0) + 1
        return counts


def store_for(data_dir):
    """The one Learned store of a data folder. Without one (a benchmark, a test), a fresh store that keeps nothing."""
    if not data_dir:
        return Learned(None)
    from .. import hints
    key = str(Path(data_dir) / hints.HEALS)
    with _lock:
        if key not in _stores:
            _stores[key] = Learned(key)
        return _stores[key]


def confirm_run(run_id):
    """A run that finished completed: confirm, in every store, the variants proposed from screens it missed."""
    with _lock:
        stores = list(_stores.values())
    for store in stores:
        store.confirm_run(run_id)


def attach(packs, data_dir):
    """Give ``packs`` the store of ``data_dir``: core.recognize then uses its trusted variants."""
    store = store_for(data_dir)
    if data_dir:
        for pack in packs:
            pack.learned = store
    return store
