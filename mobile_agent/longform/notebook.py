"""The findings notebook (DESIGN §5.1): what a long task saw and learned, kept by code as it goes."""

import copy
import json
import re
import threading
from dataclasses import asdict, dataclass, field

VERSION = 1
CAPS = {"caption": 300, "on_screen": 300, "audio": 120, "conversation": 300, "summary": 200, "detail": 120,
        "details": 8, "fact": 300, "title": 160, "creator": 80, "name": 80}
# Past this many items a new one keeps only its source, its extraction's summary and its identity.
LEAN_AFTER = 400
MAX_BYTES = 2_000_000
SKIPS = ("ad", "repeat", "off_topic", "unreadable")
COUNTERS = ("seen", "kept", "ads", "repeats", "off_topic", "unreadable", "extract_failed", "seen_before")
# Identities from earlier long runs in the same conversation that a notebook remembers (oldest dropped first).
MAX_EARLIER = 5000
ITEM_ID = re.compile(r"\bi([1-9]\d{0,3})\b")


# The kinds of video item a feed shows (a post is read like a reel).
FEED_KINDS = frozenset({"reel", "tiktok", "short", "post"})


def cut(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


@dataclass
class Item:
    id: str = ""
    n: int = 0
    kind: str = "reel"
    app: str | None = None
    step: int | None = None
    at: float = 0.0
    identity: str = ""
    source: dict = field(default_factory=dict)
    text: dict = field(default_factory=dict)
    frame_id: str | None = None
    extract: dict | None = None
    skip: str | None = None

    @property
    def kept(self):
        return self.skip is None

    def summary(self):
        """One short line: the extraction's summary, else the caption, else the on-screen text."""
        extract = self.extract or {}
        return cut(extract.get("summary") or self.text.get("caption") or self.text.get("on_screen")
                   or self.source.get("title") or "", CAPS["summary"])

    def who(self):
        return self.source.get("creator") or self.source.get("name") or self.source.get("title") or ""


@dataclass
class Fact:
    id: str
    text: str
    item_ids: tuple = ()
    kind: str = "fact"          # "fact" | "insight" | "theme" | "step_result"
    source: str = "model"       # "model" | "digest" | "step"
    turn: int = 0


class Notebook:
    def __init__(self, run_id="", request="", kind="feed", focus=None, preferences=(), steps=(), mask=None):
        self.version = VERSION
        self.run_id, self.request, self.kind, self.focus = run_id, request, kind, focus
        self.preferences = list(preferences or ())
        self.steps = [{"id": s["id"], "title": s.get("title", ""), "app": s.get("app"), "state": s.get("state", "next"),
                       "result": s.get("result"), "started_turn": s.get("started_turn"),
                       "ended_turn": s.get("ended_turn")} for s in steps or ()]
        self.items, self.facts, self.log = [], [], []
        self.receipts = []        # bounded contract snapshots, one per source run
        self.receipt_runs_omitted = 0
        self.digest = None
        self.counters = {name: 0 for name in COUNTERS}
        self.phone = {"first_action_at": None, "released_at": None, "waited_s": 0.0}
        self.mask = mask or (lambda text: text)
        self.plan = None          # the plan card the run started from (kept with the notebook for the API)
        self.lock = threading.RLock()
        self._identities = {}     # identity -> item id of the first (kept) item with it
        # Identities an earlier long task in this conversation already showed the person ("scroll again" must not
        # repeat itself): such an item is skipped as a repeat and counted in seen_before.
        self.earlier = set()
        self.revision = 0         # bumped on every change (the journal writer and the epoch read it)

    def _clean_text(self, text):
        out = {}
        for key in ("caption", "on_screen", "audio", "conversation"):
            if text.get(key):
                out[key] = cut(self.mask(str(text[key])), CAPS[key])
        details = [cut(self.mask(str(d)), CAPS["detail"]) for d in (text.get("details") or ()) if str(d).strip()]
        if details:
            out["details"] = details[:CAPS["details"]]
        return out

    def _clean_source(self, source):
        return {key: cut(self.mask(str(source[key])), CAPS["title" if key == "title" else "creator"])
                for key in ("creator", "title", "name") if source.get(key)}

    def add_item(self, item):
        """Add ``item``: assigns ``id`` and ``n``, marks a repeat, updates the counters. Returns the stored Item."""
        with self.lock:
            n = len(self.items) + 1
            stored = Item(id=f"i{n}", n=n, kind=item.kind, app=item.app, step=item.step, at=round(item.at, 2),
                          identity=str(item.identity or "")[:200], source=self._clean_source(item.source or {}),
                          text=self._clean_text(item.text or {}), frame_id=item.frame_id, extract=None,
                          skip=item.skip if item.skip in SKIPS else None)
            if stored.identity and stored.skip is None and stored.identity in self._identities:
                stored.skip = "repeat"
            elif stored.identity and stored.skip is None and stored.identity in self.earlier:
                stored.skip = "repeat"
                self.counters["seen_before"] += 1
            if n > LEAN_AFTER:
                stored.text = {}
            if item.extract:
                stored.extract = self._clean_extract(item.extract)
            self.items.append(stored)
            self.counters["seen"] += 1
            self.counters[self.SKIP_COUNTERS[stored.skip]] += 1
            if stored.identity and stored.skip is None:
                self._identities.setdefault(stored.identity, stored.id)
            self.revision += 1
            return stored

    SKIP_COUNTERS = {None: "kept", "ad": "ads", "repeat": "repeats", "off_topic": "off_topic",
                     "unreadable": "unreadable"}

    def _clean_extract(self, extract):
        def clean(value, depth=0):
            if isinstance(value, str):
                return cut(self.mask(value), 300)
            if isinstance(value, list):
                return [clean(v, depth + 1) for v in value[:12]]
            if isinstance(value, dict) and depth < 3:
                return {str(k)[:40]: clean(v, depth + 1) for k, v in list(value.items())[:24]}
            if isinstance(value, (int, float, bool)) or value is None:
                return value
            return cut(str(value), 120)
        out = clean(dict(extract))
        if isinstance(out.get("summary"), str):
            out["summary"] = cut(out["summary"], CAPS["summary"])
        if len(self.items) > LEAN_AFTER:
            out = {"summary": out.get("summary"), "relevant": out.get("relevant"), "tags": out.get("tags")}
        return out

    def item(self, item_id):
        with self.lock:
            index = _index(item_id)
            return self.items[index] if index is not None and index < len(self.items) else None

    def update_item(self, item_id, *, extract=None, skip=None, frame_id=None):
        """Merge an extraction (or a late skip: the extractor saw an ad) into an item. Returns it, or None."""
        with self.lock:
            stored = self.item(item_id)
            if stored is None:
                return None
            if extract is not None:
                stored.extract = self._clean_extract(extract)
            if frame_id is not None:
                stored.frame_id = frame_id
            if skip is not None and skip in SKIPS and stored.skip != skip:
                names = self.SKIP_COUNTERS
                self.counters[names[stored.skip]] = max(0, self.counters[names[stored.skip]] - 1)
                stored.skip = skip
                self.counters[names[skip]] += 1
                if stored.identity and self._identities.get(stored.identity) == stored.id:
                    del self._identities[stored.identity]
            self.revision += 1
            return stored

    def extract_failed(self, item_id):
        with self.lock:
            self.counters["extract_failed"] += 1
            self.revision += 1

    def remember_earlier(self, identities):
        """Identities an earlier long task in this conversation showed: they are skipped as repeats here."""
        with self.lock:
            for identity in identities:
                if identity and len(self.earlier) < MAX_EARLIER:
                    self.earlier.add(str(identity)[:200])

    def watched(self):
        """Items that count toward the budget: every item a person would have seen in full (kept or off the topic);
        ads and repeats are swiped past and don't count."""
        with self.lock:
            return self.counters["kept"] + self.counters["off_topic"]

    def kept_items(self):
        with self.lock:
            return [item for item in self.items if item.skip is None]

    def add_fact(self, text, item_ids=(), kind="fact", source="model", turn=0):
        """Add a fact (masked, capped)."""
        text = cut(self.mask(str(text or "")), CAPS["fact"])
        if not text:
            return None
        with self.lock:
            folded = text.casefold()
            if any(f.text.casefold() == folded for f in self.facts[-200:]):
                return None
            cited = [f"i{m}" for m in ITEM_ID.findall(text)] + [str(i) for i in item_ids or ()]
            known, ids = len(self.items), []
            for ident in cited:
                index = _index(ident)
                if index is not None and index < known and ident not in ids:
                    ids.append(ident)
            fact = Fact(id=f"f{len(self.facts) + 1}", text=text, item_ids=tuple(ids), kind=kind, source=source,
                        turn=int(turn or 0))
            self.facts.append(fact)
            self.revision += 1
            return fact

    def add_log(self, line):
        with self.lock:
            self.log.append(cut(self.mask(line), 300))
            self.revision += 1

    def set_digest(self, digest):
        with self.lock:
            self.digest = digest
            self.revision += 1

    def step(self, step_id):
        return next((s for s in self.steps if s["id"] == step_id), None)

    def current_step(self):
        return next((s for s in self.steps if s["state"] == "now"), None)

    def to_dict(self, items=True):
        with self.lock:
            out = {"version": self.version, "run_id": self.run_id, "request": self.request, "kind": self.kind,
                   "focus": self.focus, "preferences": list(self.preferences), "steps": copy.deepcopy(self.steps),
                   "facts": [asdict(f) for f in self.facts], "log": list(self.log),
                   "receipts": copy.deepcopy(self.receipts), "receipt_runs_omitted": self.receipt_runs_omitted,
                   "digest": copy.deepcopy(self.digest), "counters": dict(self.counters), "phone": dict(self.phone),
                   "plan": copy.deepcopy(self.plan), "earlier": sorted(self.earlier)[:MAX_EARLIER]}
            if items:
                out["items"] = [asdict(i) for i in self.items]
            return out

    def public(self, items=True):
        """The API's camelCase view (GET /api/runs/{id}/longform)."""
        data = self.to_dict(items=items)
        counters = data["counters"]
        out = {"counters": {"seen": counters["seen"], "kept": counters["kept"], "ads": counters["ads"],
                            "repeats": counters["repeats"], "offTopic": counters["off_topic"],
                            "unreadable": counters["unreadable"], "extractFailed": counters["extract_failed"]},
               "seenBefore": counters.get("seen_before", 0),
               "steps": [{"id": s["id"], "title": s["title"], "app": s["app"], "state": s["state"],
                          "result": s["result"]} for s in data["steps"]],
               "facts": [{"id": f["id"], "text": f["text"], "itemIds": list(f["item_ids"]), "kind": f["kind"],
                          "source": f["source"]} for f in data["facts"]],
               "digest": data["digest"], "phone": data["phone"], "focus": data["focus"], "kind": data["kind"],
               "receipts": data["receipts"], "receiptRunsOmitted": data["receipt_runs_omitted"]}
        if items:
            out["items"] = [public_item(i) for i in data["items"]]
        return out

    @classmethod
    def from_dict(cls, data, mask=None):
        book = cls(data.get("run_id", ""), data.get("request", ""), data.get("kind", "feed"), data.get("focus"),
                   data.get("preferences") or (), data.get("steps") or (), mask=mask)
        book.version = int(data.get("version") or VERSION)
        for raw in data.get("items") or ():
            fields = {k: v for k, v in raw.items() if k in Item.__dataclass_fields__}
            item = Item(**fields)
            book.items.append(item)
            if item.identity and item.skip is None:
                book._identities.setdefault(item.identity, item.id)
        for raw in data.get("facts") or ():
            book.facts.append(Fact(id=raw["id"], text=raw["text"], item_ids=tuple(raw.get("item_ids") or ()),
                                   kind=raw.get("kind", "fact"), source=raw.get("source", "model"),
                                   turn=int(raw.get("turn") or 0)))
        book.log = list(data.get("log") or ())
        from .receipts import MAX_RUNS
        book.receipts = [copy.deepcopy(r) for r in (data.get("receipts") or ())[-MAX_RUNS:] if isinstance(r, dict)]
        book.receipt_runs_omitted = int(data.get("receipt_runs_omitted") or 0)
        book.earlier = {str(i) for i in data.get("earlier") or () if i}
        book.plan = data.get("plan")
        book.digest = data.get("digest")
        book.counters.update({k: int(v) for k, v in (data.get("counters") or {}).items() if k in book.counters})
        book.phone.update(data.get("phone") or {})
        return book

    def dumps(self):
        """The notebook as redacted JSON for the journal, cut to MAX_BYTES (oldest items' texts go first)."""
        from ..secret_filter import redact
        data = _redacted(self.to_dict(), redact)
        text = json.dumps(data, ensure_ascii=False, allow_nan=False)
        index = 0
        while len(text.encode()) > MAX_BYTES and index < len(data["items"]):
            data["items"][index]["text"] = {}
            data["items"][index]["extract"] = ({"summary": (data["items"][index].get("extract") or {}).get("summary")}
                                               if data["items"][index].get("extract") else None)
            index += 1
            if index % 20 == 0 or index == len(data["items"]):
                text = json.dumps(data, ensure_ascii=False, allow_nan=False)
        return json.dumps(data, ensure_ascii=False, allow_nan=False)


def public_item(raw):
    extract = raw.get("extract") or {}
    return {"id": raw["id"], "n": raw["n"], "kind": raw["kind"], "app": raw.get("app"), "step": raw.get("step"),
            "at": raw.get("at"), "skip": raw.get("skip"), "source": raw.get("source") or {},
            "text": raw.get("text") or {}, "frameId": raw.get("frame_id"), "extract": raw.get("extract"),
            "summary": extract.get("summary"), "tags": extract.get("tags"), "relevant": extract.get("relevant")}


def _index(item_id):
    found = ITEM_ID.fullmatch(str(item_id or "").strip())
    return int(found.group(1)) - 1 if found else None


def _redacted(value, redact):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: _redacted(item, redact) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redacted(item, redact) for item in value]
    return value
