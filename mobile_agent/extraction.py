"""Bounded schema validation and literal-grounded result extraction.

This intentionally supports a safe JSON Schema subset. It never fetches references,
executes regular expressions, infers missing values, or treats citations as a task oracle.
"""

import json
from types import SimpleNamespace
import re

from .transport import decode_json


KEYWORDS = frozenset({"$schema", "type", "properties", "required", "additionalProperties",
    "items", "minItems", "maxItems", "uniqueItems", "minLength", "maxLength", "minimum",
    "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "enum", "const",
    "title", "description"})
TYPES = frozenset({"object", "array", "string", "number", "integer", "boolean", "null"})


class InsufficientEvidence(ValueError):
    """The helper explicitly abstained; this is never a schema-valid success claim."""


def bounded_json(value, *, max_bytes=32000, max_nodes=2000, max_depth=12):
    pending, count = [(value, 0)], 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if count > max_nodes or depth > max_depth:
            raise ValueError("JSON exceeds structural limits")
        if type(node) is dict:
            if any(not isinstance(key, str) for key in node):
                raise ValueError("JSON object keys must be strings")
            pending.extend((child, depth + 1) for child in node.values())
        elif type(node) is list:
            pending.extend((child, depth + 1) for child in node)
        elif node is not None and type(node) not in (str, int, float, bool):
            raise ValueError("Result contains a non-JSON value")
    try:
        wire = json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))
        if len(wire.encode()) > max_bytes:
            raise ValueError("JSON exceeds byte limit")
        return decode_json(wire)
    except (TypeError, OverflowError, UnicodeError):
        raise ValueError("Invalid JSON value") from None


def validate_schema(schema):
    schema = bounded_json(schema, max_bytes=16000, max_nodes=500, max_depth=12)
    if not isinstance(schema, dict):
        raise ValueError("Output schema must be a JSON Schema object")
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        raise ValueError("Structured output requires mobile_agent/requirements.txt") from None

    def inspect(node, depth=0):
        if not isinstance(node, dict) or depth > 6 or set(node) - KEYWORDS:
            raise ValueError("Unsupported output schema keyword or nesting depth")
        declared = node.get("type")
        kinds = declared if isinstance(declared, list) else [declared]
        if not kinds or any(not isinstance(kind, str) or kind not in TYPES for kind in kinds):
            raise ValueError("Every output schema node requires an explicit supported type")
        if "$schema" in node and node["$schema"] != "https://json-schema.org/draft/2020-12/schema":
            raise ValueError("Only JSON Schema Draft 2020-12 is supported")
        if "object" in kinds:
            properties = node.get("properties", {})
            if not isinstance(properties, dict) or len(properties) > 40 or node.get("additionalProperties") is not False:
                raise ValueError("Objects require bounded properties and additionalProperties:false")
            required = node.get("required", [])
            if not isinstance(required, list) or any(not isinstance(key, str) or key not in properties for key in required):
                raise ValueError("Required properties must have a defined schema")
            for child in properties.values():
                inspect(child, depth + 1)
        elif "properties" in node or "required" in node or "additionalProperties" in node:
            raise ValueError("Object keywords require object type")
        if "array" in kinds:
            if "items" not in node:
                raise ValueError("Arrays require a bounded item schema")
            inspect(node["items"], depth + 1)
            if type(node.get("maxItems")) is not int or not 0 <= node["maxItems"] <= 100:
                raise ValueError("Arrays require maxItems between 0 and 100")
        elif "items" in node:
            raise ValueError("Array items require array type")
        if "enum" in node and (not isinstance(node["enum"], list) or not 1 <= len(node["enum"]) <= 100):
            raise ValueError("Enum exceeds supported bounds")

    inspect(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except Exception:
        raise ValueError("Invalid JSON Schema") from None
    return schema


EVIDENCE_MAX_ENTRIES = 400
EVIDENCE_TEXT_BYTES = 48000
EVIDENCE_CONTEXT_BYTES = 96000
ROW_CONTEXT_CHARS = 200
# Off-screen page nodes offered as evidence per observation, nearest first.
OFFSCREEN_EVIDENCE_NODES = 300
# A request word found in more than this share of a page's off-screen rows ("Apollo"
# on the Apollo 11 article) tells nothing about which rows answer it.
FOCUS_TERM_SHARE = .25
_FOCUS_STOP = frozenset("""about according after answer article before being cannot could does from have into
    many much only open page report shown show should than that their them then there these they this those
    using what when where which while wikipedia with would year date name number""".split())


def focus_terms(goal):
    """The request's content words as short stems ("land" for "land", "landing", "landed")."""
    words = re.findall(r"[^\W\d_]{4,}", (goal or "").casefold())
    return tuple(dict.fromkeys(word[:5] for word in words if word not in _FOCUS_STOP))


_STOPWORDS = frozenset("""the and for with this that from into what which when where does report open article
    according page year about their there these those have been will would could should your
    shown show find then also only just than more most very""".split())


def request_words(goal):
    """Content words of a request, for ranking evidence by relevance."""
    return {word for word in re.findall(r"[a-z0-9]{4,}", (goal or "").casefold()) if word not in _STOPWORDS}


def row_contexts(elements):
    """Text sharing each element's visual row, in reading order.

    Tables, forms and web infoboxes split a label and its value into separate
    nodes ("Height" | "330" | "m (1,083" | "ft)"); their only link is the row.
    """
    rows = []
    ordered = sorted(elements, key=lambda e: e.rect[1] + e.rect[3] / 2)
    # Rows are appended in centre order, so a row more than the largest half-height
    # above this element can match neither it nor any later one: skip past it. Same
    # first match as scanning every row; 9.5 -> 0.9 ms on an 800-node web page.
    reach, start = max((max(e.rect[3] / 2, .006) for e in ordered), default=0), 0
    for element in ordered:
        center, half = element.rect[1] + element.rect[3] / 2, max(element.rect[3] / 2, .006)
        while start < len(rows) and center - rows[start]["center"] > reach:
            start += 1
        row = next((r for r in rows[start:] if abs(r["center"] - center) <= max(r["half"], half)), None)
        if row is None:
            rows.append({"center": center, "half": half, "members": [element]})
        else:
            row["members"].append(element)
    contexts = {}
    for row in rows:
        members = sorted(row["members"], key=lambda e: e.rect[0])
        texts = list(dict.fromkeys(t for e in members for t in ((e.label or "").strip(), (e.value or "").strip()) if t))
        if len(texts) < 2:
            continue
        text = " ".join(texts)[:ROW_CONTEXT_CHARS]
        for e in members:
            contexts[e.id] = text
    return contexts


SWITCH_ROLES = frozenset({"Switch", "Toggle"})


def switch_state(element):
    """'on'/'off' for a switch whose accessibility value is 1/0, else None.

    A switch's state is observed, but only as "1"/"0"; an answer of "off"
    could never cite it (MobsterBench pass 1: every on/off Settings question
    failed extraction). This is a fixed rendering of that value, not a reading.
    """
    if element.role not in SWITCH_ROLES:
        return radio_row_state(element)
    return {"1": "on", "0": "off"}.get((element.value or "").strip())


# Settings' Wi-Fi and Bluetooth rows carry no switch: the row reads "Off" when the radio
# is off and otherwise its status ("Not Connected", a network or device name). Live
# (pass 6, state.wifi): "Wi-Fi, HomeNet_5G" could not be cited as "on".
RADIO_ROW = re.compile(r"^(Wi-Fi|WLAN|Bluetooth), (?P<status>.+)$")


def radio_row_state(element):
    """'on'/'off' for a Settings Wi-Fi or Bluetooth row, else None."""
    if element.role not in ("Button", "Cell"):
        return None
    match = RADIO_ROW.match((element.label or "").strip())
    if not match:
        return None
    return "off" if match.group("status").strip().casefold() == "off" else "on"


# Measured: 62 KB of full-form evidence plus the current screen exceeded the
# verifier's token limit; the compact form of the same run is ~15 KB.
VERIFICATION_EVIDENCE_BYTES = 24000


class Evidence:
    def __init__(self):
        self.entries = []
        self.seen = {}
        self.bytes = 0
        self.context_bytes = 0
        self.truncated = False
        self.latest_step = 0
        self.visual_sources = []
        self.next_id = 0
        self.keys = {}
        # Serialized size of each on-screen entry as counted in context_bytes, so a
        # re-seen element costs one json.dumps, not two (1.85 -> 1.4 ms per add, 120 elements).
        self.sizes = {}
        self.focus = ()  # the request's content words (focus_terms); ranks off-screen rows

    def __copy__(self):
        # agent.fork_evidence shallow-copies, then adds to the fork (speculation). A shared
        # sizes dict would let the fork's re-seen sizes skew the original's context_bytes.
        clone = object.__new__(type(self))
        clone.__dict__.update(self.__dict__)
        clone.sizes = dict(self.sizes)
        return clone

    def add(self, snapshot, step):
        # Exact text already observed by the agent; no screenshot interpretation or invented data.
        self.latest_step = step
        rows = row_contexts(snapshot.elements)
        for element in snapshot.elements:
            fields = [("label", element.label), ("value", element.value)]
            state = switch_state(element)
            if state is not None:
                fields.append(("state", state))
            for field, text in fields:
                # WDA reports static text with value == label; one literal is one fact.
                if not text.strip() or field == "value" and text == element.label:
                    continue
                key = (snapshot.source, snapshot.bundle_id, element.id, field, text, element.label)
                if key in self.seen:
                    entry = self.seen[key]
                    updated = {**entry, "last_seen_step": step, "rect": element.rect}
                    size = len(json.dumps(updated).encode())
                    old = self.sizes.get(entry["id"])
                    delta = size - (len(json.dumps(entry).encode()) if old is None else old)
                    if self.context_bytes + delta > EVIDENCE_CONTEXT_BYTES and not self._evict(0, delta, step):
                        self.truncated = True
                        continue
                    entry.update(updated)
                    self.context_bytes += delta
                    self.sizes[entry["id"]] = size
                    continue
                entry = {"id": f"e{self.next_id}", "text": text,
                    "source": snapshot.source, "bundle_id": snapshot.bundle_id, "step": step,
                    "last_seen_step": step, "element_id": element.id, "field": field,
                    "label_context": element.label, "role": element.role, "rect": element.rect}
                if rows.get(element.id):
                    entry["row_context"] = rows[element.id]
                size = len(json.dumps(entry).encode())
                if not self._evict(len(text.encode()), size, step):
                    self.truncated = True
                    continue
                self.next_id += 1
                self.seen[key] = entry
                self.keys[entry["id"]] = key
                self.entries.append(entry)
                self.bytes += len(text.encode())
                self.context_bytes += size
                self.sizes[entry["id"]] = size
        self._add_offscreen(snapshot, step)

    def _add_offscreen(self, snapshot, step):
        """Page text outside the viewport (WebKit publishes the whole page), as citable evidence.

        Measured (MobsterBench diag, 24 Sep): answers in a Wikipedia infobox were
        reached by 3-5 swipes at ~2.5 s each, while the same text sat in this
        snapshot's off-screen nodes. Entries are marked ``offscreen``; they never
        evict anything already observed (earlier screens hold multi-hop context),
        and the nearest nodes are kept first.
        """
        nodes = [n for n in getattr(snapshot, "offscreen", ()) if (n.label or n.value).strip()]
        if not nodes:
            return
        proxies = [SimpleNamespace(id="off:" + (node.locator or f"{node.role}:{node.label}")[-240:], label=node.label,
                                   value=node.value, rect=node.rect) for node in nodes]
        rows = row_contexts(proxies)
        # Rows that share a word with the request come first, then the nearest. Live
        # (pass 6, web.apollo_landing): the Moon-landing row sat past the nearest 300
        # nodes of a long infobox, and only the splashdown "Landing date" was kept.
        texts = [f"{node.label} {node.value} {rows.get(proxy.id, '')}".casefold()
                 for node, proxy in zip(nodes, proxies)]
        terms = [term for term in self.focus if sum(term in text for text in texts) <= FOCUS_TERM_SHARE * len(texts)]
        ranked = sorted(range(len(nodes)), key=lambda i: (not any(term in texts[i] for term in terms),
                                                          nodes[i].screens_away))[:OFFSCREEN_EVIDENCE_NODES]
        # Added in rank order: when the evidence budget runs out, the far relevant rows are kept.
        nodes, proxies = [nodes[i] for i in ranked], [proxies[i] for i in ranked]
        for node, proxy in zip(nodes, proxies):
            for field, text in (("label", node.label), ("value", node.value)):
                if not text.strip() or field == "value" and text == node.label:
                    continue
                key = (snapshot.source, snapshot.bundle_id, proxy.id, field, text, node.label)
                if key in self.seen:
                    self.seen[key]["last_seen_step"] = step
                    self.sizes.pop(self.seen[key]["id"], None)  # no longer its counted size
                    continue
                entry = {"id": f"e{self.next_id}", "text": text, "source": snapshot.source,
                         "bundle_id": snapshot.bundle_id, "step": step, "last_seen_step": step,
                         "element_id": proxy.id, "field": field, "label_context": node.label, "role": node.role,
                         "rect": node.rect, "offscreen": True}
                if rows.get(proxy.id):
                    entry["row_context"] = rows[proxy.id]
                size = len(json.dumps(entry).encode())
                if (len(self.entries) >= EVIDENCE_MAX_ENTRIES or self.bytes + len(text.encode()) > EVIDENCE_TEXT_BYTES
                        or self.context_bytes + size > EVIDENCE_CONTEXT_BYTES):
                    self.truncated = True
                    return
                self.next_id += 1
                self.seen[key] = entry
                self.keys[entry["id"]] = key
                self.entries.append(entry)
                self.bytes += len(text.encode())
                self.context_bytes += size

    def _evict(self, text_bytes, size, step):
        """Make room by dropping the least recently seen entries, never the current screen.

        The old policy refused new entries once full, so a long scroll lost its
        latest screens -- the ones holding the answer (measured: the Eiffel
        Tower infobox height). Returns False only if the current screen alone
        exceeds the budget.
        """
        def full():
            return (len(self.entries) >= EVIDENCE_MAX_ENTRIES
                    or self.bytes + text_bytes > EVIDENCE_TEXT_BYTES
                    or self.context_bytes + size > EVIDENCE_CONTEXT_BYTES)
        while full():
            candidates = [entry for entry in self.entries if entry["last_seen_step"] < step]
            if not candidates:
                return False
            oldest = min(candidates, key=lambda entry: entry["last_seen_step"])
            self.entries.remove(oldest)
            self.seen.pop(self.keys.pop(oldest["id"], None), None)
            self.sizes.pop(oldest["id"], None)
            self.bytes -= len(oldest["text"].encode())
            self.context_bytes -= len(json.dumps(oldest).encode())
            self.truncated = True
        return True

    def add_visual(self, observation):
        """Supplemental read-only screen text, kept distinguishable from the accessibility tree.

        Native accessibility can omit text that is genuinely on screen -- a
        rendered result, a canvas, a remote web body. These entries carry that
        text so it can be cited, and are labeled so a reader can tell recognized
        pixels from an app-reported value. They name no element, expose no
        locator, and are never offered as an action target or decision context.
        """
        added = 0
        for entry in observation["entries"]:
            text = entry["text"]
            key = ("local_screenshot_ocr", observation["bundle_id"], entry["observation_id"], "text", text, "")
            if not text.strip() or key in self.seen:
                continue
            record = {"id": f"v{len(self.visual_sources)}_{added}", "text": text,
                "source": "local_screenshot_ocr", "bundle_id": observation["bundle_id"],
                "step": self.latest_step, "last_seen_step": self.latest_step,
                "element_id": entry["observation_id"], "field": "screen_text",
                "label_context": "", "role": "RecognizedText", "rect": tuple(entry["rect"]),
                "visual": True, "confidence": entry["confidence"], "actionable": False}
            size = len(json.dumps(record).encode())
            if not self._evict(len(text.encode()), size, self.latest_step):
                self.truncated = True
                continue
            self.seen[key] = record
            self.keys[record["id"]] = key
            self.entries.append(record)
            self.bytes += len(text.encode())
            self.context_bytes += size
            added += 1
        self.visual_sources.append({name: observation[name] for name in (
            "frame_source", "image_hash", "bundle_id", "native_revision", "native_fingerprint",
            "capture_age_ms", "observation_ms", "discarded_low_confidence", "truncated")}
            | {"entries_added": added, "step": self.latest_step})
        return added

    def public(self):
        return {"entries": [{**entry, "rect": list(entry["rect"])} for entry in self.entries],
                "truncated": self.truncated, "latest_step": self.latest_step,
                "visual_sources": list(self.visual_sources),
                "visibility_verified": False}

    def for_verification(self, citations, max_bytes=VERIFICATION_EVIDENCE_BYTES, goal=""):
        """Compact evidence for the one answer-verification call.

        The full record (every field of every entry) exceeded the verifier's
        token limit on a four-screen USB iPhone run and failed every answer
        (HTTP 400 max_tokens_exceeded). Entries keep their ids, text, context,
        role, step and row; cited entries always survive, then the newest.
        """
        cited = {c.get("evidence_id") for c in citations if isinstance(c, dict)}
        def compact(entry):
            fact = {"id": entry["id"], "text": entry["text"], "role": entry["role"],
                    "step": entry["step"], "last_seen_step": entry["last_seen_step"],
                    "row": round(entry["rect"][1] * 100)}
            if entry["label_context"] != entry["text"]:
                fact["label_context"] = entry["label_context"]
            if entry["field"] != "label":
                fact["field"] = entry["field"]
            if entry.get("row_context"):
                fact["row_context"] = entry["row_context"]
            if entry.get("visual"):
                fact["visual"] = True
            return fact
        # Cited entries first, then what the request is about, then the newest.
        # Multi-hop answers depend on an older screen ("named after the
        # engineer Gustave Eiffel"); recency alone dropped it and the verifier
        # rejected a correct answer about half the time.
        words = request_words(goal)
        def relevance(entry):
            text = (entry["text"] + " " + entry.get("row_context", "")).casefold()
            return sum(word in text for word in words)
        ranked = sorted(self.entries, key=lambda entry: (entry["id"] not in cited, -relevance(entry),
                                                          -entry["last_seen_step"]))
        kept, size, truncated = set(), 0, self.truncated
        for entry in ranked:
            entry_bytes = len(json.dumps(compact(entry)).encode()) + 2  # list separator
            if size + entry_bytes > max_bytes and entry["id"] not in cited:
                truncated = True
                continue
            kept.add(entry["id"])
            size += entry_bytes
        return {"entries": [compact(entry) for entry in self.entries if entry["id"] in kept],
                "truncated": truncated, "latest_step": self.latest_step,
                "visual_sources": list(self.visual_sources), "visibility_verified": False}

    def decision_context(self, snapshot):
        """Bounded prior facts, separate from the current screen and action targets."""
        current = {(snapshot.source, snapshot.bundle_id, element.id, field): (text, element.label)
                   for element in snapshot.elements
                   for field, text in (("label", element.label), ("value", element.value))}
        groups, latest = {}, {}
        for entry in self.entries:
            if entry.get("visual"):
                continue
            group = (entry["source"], entry["bundle_id"], entry["element_id"])
            groups.setdefault(group, entry["id"])
            key = (*group, entry["field"])
            latest[key] = max(latest.get(key, -1), entry["last_seen_step"])
        entries, size, truncated = [], 0, self.truncated
        for entry in sorted(self.entries, key=lambda item: item["last_seen_step"], reverse=True):
            if entry.get("visual") or entry.get("offscreen"):
                # Off-screen page text is for answers; hundreds of nodes of the current page
                # would crowd earlier screens out of the decision's history.
                continue
            group = (entry["source"], entry["bundle_id"], entry["element_id"])
            key = (*group, entry["field"])
            if current.get(key) == (entry["text"], entry["label_context"]):
                continue
            # Historic element IDs/geometry are deliberately not offered as targets.
            fact = {name: entry[name] for name in ("id", "text", "label_context", "role", "source", "bundle_id", "field", "step", "last_seen_step")}
            fact["group"] = groups[group]
            fact["superseded"] = key in current or entry["last_seen_step"] < latest[key]
            entry_bytes = len(json.dumps(fact).encode())
            if len(entries) >= 64 or size + entry_bytes > 12000:
                truncated = True
                continue
            entries.append(fact)
            size += entry_bytes
        return {"entries": entries, "truncated": truncated}


def scalar_leaves(value, path=""):
    if isinstance(value, dict) and value:
        for key, child in value.items():
            escaped = key.replace("~", "~0").replace("/", "~1")
            yield from scalar_leaves(child, path + "/" + escaped)
    elif isinstance(value, list) and value:
        for index, child in enumerate(value):
            yield from scalar_leaves(child, path + "/" + str(index))
    else:
        yield path, value


def _field_name(path):
    """Semantic field key from a data-relative JSON Pointer; '' for root scalars.

    A list element ("/items/0") is named by its list; a generic list ("items",
    the counted-answer list) names nothing, so its elements are quoted content."""
    if not path:
        return ""
    segments = [part.replace("~1", "/").replace("~0", "~") for part in path.strip("/").split("/")]
    while segments and segments[-1].isdigit():
        segments.pop()
    name = segments[-1] if segments else ""
    return "" if name in GENERIC_LIST_FIELDS else name


GENERIC_LIST_FIELDS = frozenset({"items", "item", "values", "entries", "list", "results"})


def _tokens(text):
    """Lowercase alphanumeric tokens; splits snake_case and camelCase alike.

    Apple's i-brands stay whole: a word-initial lowercase ``i`` immediately
    followed by an uppercase letter (iOS, iPhone, iCloud, iMessage) is a brand,
    not a word boundary. Splitting it (iOS -> {i, os}) made the wrong-field
    guard read ``ios_version`` vs "iOS Version" as a conflict and reject the
    correct answer (live harness settings.about.version 0/3, 2026-09-21).
    """
    protected = re.sub(r"(?<![A-Za-z0-9])i(?=[A-Z])", "\x00", text or "")
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", protected)
    return frozenset(token for token in re.split(r"[^a-zA-Z0-9]+", spaced.replace("\x00", "i").lower()) if token)


# Absence / placeholder wording that is never a concrete fact value for a
# named secret-or-id field. Quoting the message itself (availability text) is
# a root/content claim and is not blocked; see _CONTENT_FIELD_TOKENS.
_ABSENCE_TOKENS = frozenset({"hidden", "unavailable", "unset", "not", "set", "missing",
    "none", "na", "redacted", "locked", "disabled", "off", "blank", "empty",
    "placeholder", "classified", "confidential", "secret", "unknown"})
_CONTENT_FIELD_TOKENS = frozenset({"title", "text", "name", "label", "message", "bio",
    "description", "note", "body", "caption", "quote", "status", "availability", "hint",
    "placeholder", "error", "warning", "content", "string", "value", "item", "row"})


def _binary_state(entry, value):
    """A setting row's own On/Off (a switch's state, or a cell's value) is a fact, not an absence marker."""
    return entry.get("field") in ("state", "value") and value.strip().casefold() in ("on", "off")


def _looks_like_absence(text):
    tokens = _tokens(text)
    return bool(tokens) and tokens <= _ABSENCE_TOKENS or (
        bool(tokens & frozenset({"hidden", "unavailable", "redacted", "placeholder", "blank", "empty"}))
        and len(tokens) <= 3)


def pack_extraction_context(evidence, goal, schema=None, *, max_entries=120, max_bytes=24000):
    """Filter evidence to entries relevant to the requested fields, keeping history.

    Selection is field-key token overlap with schema property names and/or goal
    tokens, plus every label/value field row: sibling rows (label_context) are
    what let extraction bind model_name vs model_number correctly. Recency
    orders candidates but never keeps noise by itself. Multi-screen facts whose
    tokens overlap stay even when older; label/value group pairs travel
    together. Never drops the only copy of a needed fact. This reduces context
    rot (Jev jaggedness: large state hurts).
    """
    entries = list((evidence or {}).get("entries") or ())
    if not entries:
        return evidence
    targets = set(_tokens(goal or ""))
    if isinstance(schema, dict):
        for key in ((schema.get("properties") or {}) if isinstance(schema.get("properties"), dict) else {}):
            targets |= _tokens(key)
        for key in (schema.get("required") or []) if isinstance(schema.get("required"), list) else []:
            targets |= _tokens(key)
    latest = (evidence or {}).get("latest_step") or 0

    def score(entry):
        haystack = _tokens(entry.get("text") or "") | _tokens(entry.get("label_context") or "") | _tokens(entry.get("field") or "")
        overlap = len(targets & haystack)
        recency = 1 if entry.get("last_seen_step") == latest else 0
        return overlap, overlap * 4 + recency

    scored = []
    for entry in entries:
        overlap, s = score(entry)
        if overlap or (entry.get("label_context") or "").strip() or not targets:
            scored.append((s, entry.get("last_seen_step") or 0, entry))
    if not scored:
        scored = [(0, e.get("last_seen_step") or 0, e) for e in entries]
    scored.sort(key=lambda item: (-item[0], -item[1]))
    keep, size, ids = [], 0, set()
    groups = {}
    for entry in entries:
        group = (entry.get("source"), entry.get("bundle_id"), entry.get("element_id"), entry.get("group"))
        groups.setdefault(entry.get("id"), group)
    for _, _, entry in scored:
        if len(keep) >= max_entries:
            break
        if entry["id"] in ids:
            continue
        wire = len(json.dumps({k: entry[k] for k in entry if k != "rect"}, default=str).encode())
        if size + wire > max_bytes and keep:
            break
        keep.append(entry)
        ids.add(entry["id"])
        size += wire
        group = groups.get(entry["id"])
        if group is not None:
            for peer in entries:
                if peer["id"] in ids or groups.get(peer["id"]) != group:
                    continue
                peer_wire = len(json.dumps({k: peer[k] for k in peer if k != "rect"}, default=str).encode())
                if len(keep) < max_entries and size + peer_wire <= max_bytes:
                    keep.append(peer)
                    ids.add(peer["id"])
                    size += peer_wire
    order = {entry["id"]: index for index, entry in enumerate(entries)}
    keep.sort(key=lambda entry: order.get(entry["id"], 0))
    return {**(evidence or {}), "entries": keep, "packed_for_extraction": True,
            "packed_dropped": max(0, len(entries) - len(keep))}


def check_field_claims(candidate, evidence):
    """Per-field claim check: local structural/lexical binding, never an LLM oracle.

    For each non-null scalar field the cited literal must sit on a coherent
    observation binding (label_context / group / recency) that matches the
    field's semantic name/key. Reject when the only citation for ``model_name``
    comes from a label/group clearly about ``model_number``. Fail closed:
    an ambiguous binding never ships a plausible mis-binding.
    """
    data, citations = candidate["data"], candidate["citations"]
    if data is None:
        return  # explicit abstention {"data":null,"citations":[]}
    entries = {entry["id"]: entry for entry in evidence.get("entries") or ()}
    by_path = {}
    for citation in citations:
        by_path.setdefault(citation["path"], citation)
    for path, value in scalar_leaves(data):
        if value is None or type(value) not in (str, int, float, bool):
            continue  # null is unknown; empty containers are not scalar claims
        citation = by_path.get(path)
        if citation is None:
            continue  # missing citation already fails closed in validate_extraction
        entry = entries.get(citation["evidence_id"])
        if entry is None:
            raise ValueError("Citation does not match observed evidence")
        if entry.get("superseded"):
            raise ValueError("Field claim cites a superseded observation binding")
        group = entry.get("group")
        if group is not None:
            for other in evidence.get("entries") or ():
                if (other.get("group") == group and other.get("field") == entry.get("field")
                        and other.get("id") != entry.get("id")
                        and other.get("last_seen_step", -1) > entry.get("last_seen_step", -1)):
                    raise ValueError("Field claim cites a stale observation-group binding")
        name = _field_name(path)
        name_tokens = _tokens(name)
        label_text = entry.get("label_context") or ""
        if not label_text.strip() and entry.get("field") == "label":
            # Packed label rows carry their identity in their own text
            # ("iOS Version, 26.6.1"): the row text is the label context when no
            # separate label_context was published. The divergent-head and
            # label-as-value rules below still reject a wrong-field or
            # label-as-value binding against this context.
            label_text = entry.get("text") or ""
        label_tokens = _tokens(label_text)
        # A compound semantic field name needs a labelled binding to identify it.
        if name and not label_tokens:
            if "_" in name or len(name_tokens) >= 2:
                raise ValueError("Field claim lacks label_context for a compound field name")
            if entry.get("field") == "value":
                raise ValueError("Field claim lacks label_context for a value binding")
        if not name_tokens or not label_tokens:
            # Placeholder/absence text is never a concrete named fact value.
            # Content-ish field names (title/message/...) keep the content-as-label
            # exemption (title <- "Coffee brewing guide"); root scalars are quoted
            # content and stay allowed.
            if name and name_tokens and type(value) is str and _looks_like_absence(value) and not _binary_state(entry, value):
                if not (name_tokens & _CONTENT_FIELD_TOKENS):
                    raise ValueError("Field claim binds an absence marker as a concrete value")
            continue
        shared = name_tokens & label_tokens
        name_only = name_tokens - label_tokens
        label_only = label_tokens - name_tokens
        # Shared discriminative context with divergent heads (model_name vs Model Number).
        if shared and name_only and label_only:
            raise ValueError("Field claim binding conflicts with the field's semantic name")
        # A value entry's label is its identity: no overlap means wrong field.
        if entry.get("field") == "value" and not shared:
            raise ValueError("Field claim binding does not match the field's semantic name")
        # A label row is not a value: when the value row is missing, filling the
        # field with its own label text (model_name <- "Model Name") fabricates
        # the fact. Only a field named exactly by the label it cites trips this:
        # a content element (title <- "Coffee brewing guide" on a result card)
        # is its own context and stays shippable. Fields whose name asks for the
        # label ("..._label") and root scalars keep the quote exemption.
        if (entry.get("field") == "label" and type(value) is str
                and name_tokens <= label_tokens and "label" not in name_tokens
                and value.strip() == label_text.strip()):
            raise ValueError("Field claim ships a field label as its value")
        if (name and type(value) is str and _looks_like_absence(value) and not _binary_state(entry, value)
                and not (name_tokens & _CONTENT_FIELD_TOKENS)):
            raise ValueError("Field claim binds an absence marker as a concrete value")


_OPTION = r"(?:'[^'\n]{1,40}'|\"[^\"\n]{1,40}\"|\u2018[^\u2019\n]{1,40}\u2019|\u201c[^\u201d\n]{1,40}\u201d)"
_ANSWER_OPTIONS = re.compile(r"\banswer(?:\s+(?:with|only|just))?\s+(" + _OPTION + r"(?:\s*(?:,|/|or|,\s*or)\s*"
                             + _OPTION + r")+)", re.I)


def closed_answers(goal, schema):
    """The schema with the request's own answer set, when it names one ("Answer 'on' or 'off'").

    Applies to a one-field string schema only; the options become that field's
    ``enum``. An enum value is a closed-set answer (see validate_extraction).
    """
    match = _ANSWER_OPTIONS.search(goal or "")
    if not match or not isinstance(schema, dict) or schema.get("type") != "object":
        return schema
    properties = schema.get("properties") or {}
    if len(properties) != 1:
        return schema
    (name, spec), = properties.items()
    if not isinstance(spec, dict) or spec.get("type") != "string" or "enum" in spec:
        return schema
    options = list(dict.fromkeys(option[1:-1].strip() for option in re.findall(_OPTION, match.group(1))))
    if len(options) < 2:
        return schema
    return {**schema, "properties": {name: {**spec, "enum": options}}}


def _leaf_schema(schema, path):
    node = schema
    for segment in [part.replace("~1", "/").replace("~0", "~") for part in path.split("/")[1:]]:
        if not isinstance(node, dict):
            return None
        if node.get("type") == "array" or "items" in node:
            node = node.get("items")
        else:
            node = (node.get("properties") or {}).get(segment)
    return node if isinstance(node, dict) else None


def is_closed_leaf(schema, path):
    """A value chosen from a fixed set (enum, boolean): a judgment about the evidence, never a copy of it."""
    leaf = _leaf_schema(schema, path)
    return bool(leaf) and ("enum" in leaf or leaf.get("type") == "boolean")


def _canonical_enum_case(data, schema):
    """"Off" for an answer set of "on"/"off" is that answer: take the set's own spelling."""
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(data, dict) or not isinstance(properties, dict):
        return data
    fixed = dict(data)
    for name, value in data.items():
        options = (properties.get(name) or {}).get("enum")
        if isinstance(value, str) and isinstance(options, list):
            same = [option for option in options if isinstance(option, str)
                    and option.casefold() == value.strip().casefold()]
            if len(same) == 1:
                fixed[name] = same[0]
    return fixed


# A citation whose quote is not in the entry it names is re-pointed to the entry that
# holds the quote, when that entry is at most this many ids away (the same paragraph,
# split by WebKit into link and text runs). Live (pass 6, multi.eiffel_calc): the
# helper cited "1889" at e74 while it sits in e76, in 2 of 5 replays.
NEIGHBOUR_CITATION_IDS = 3


def repair_citations(result, evidence):
    """The helper's citations with two slips undone; the value itself is never touched.

    - citations keyed by field ({"year": {...}}) become the list the contract asks for;
    - a quote cited at a neighbouring entry is re-pointed to the nearest entry that
      contains it. Anything else is left for validate_extraction to reject.
    """
    if not isinstance(result, dict):
        return result
    citations = result.get("citations")
    if isinstance(citations, dict) and all(isinstance(c, dict) for c in citations.values()):
        citations = [{"path": c.get("path") or "/" + str(key).lstrip("/"), "evidence_id": c.get("evidence_id"),
                      "quote": c.get("quote")} for key, c in citations.items()]
    if not isinstance(citations, list):
        return result
    order = {entry["id"]: index for index, entry in enumerate(evidence["entries"])}
    texts = [entry["text"] for entry in evidence["entries"]]
    repaired = []
    for citation in citations:
        if isinstance(citation, dict) and isinstance(citation.get("quote"), str) and citation["quote"]:
            identifier, quote = citation.get("evidence_id"), citation["quote"]
            at = order.get(identifier)
            if at is not None and quote not in texts[at]:
                near = [i for i in range(max(0, at - NEIGHBOUR_CITATION_IDS),
                                         min(len(texts), at + NEIGHBOUR_CITATION_IDS + 1)) if quote in texts[i]]
                if near:
                    best = min(near, key=lambda i: abs(i - at))
                    citation = {**citation, "evidence_id": evidence["entries"][best]["id"]}
        repaired.append(citation)
    return {**result, "citations": repaired}


def validate_extraction(result, schema, evidence):
    from jsonschema import Draft202012Validator
    from referencing import Registry

    result = bounded_json(result, max_bytes=48000, max_nodes=2500)
    if not isinstance(result, dict) or set(result) != {"data", "citations"}:
        raise ValueError("Extraction requires exactly data and citations")
    data, citations = result["data"], result["citations"]
    data = _canonical_enum_case(data, schema)
    result = {**result, "data": data}
    validator = Draft202012Validator(schema, registry=Registry())
    if next(validator.iter_errors(data), None) is not None:
        raise ValueError("Extracted data does not match the requested schema")
    if not isinstance(citations, list) or len(citations) > 400:
        raise ValueError("Invalid result citations")
    entries = {entry["id"]: entry["text"] for entry in evidence["entries"]}
    leaves = dict(scalar_leaves(data))
    cited = set()
    for citation in citations:
        if not isinstance(citation, dict) or set(citation) != {"path", "evidence_id", "quote"}:
            raise ValueError("Invalid evidence citation")
        path, identifier, quote = citation["path"], citation["evidence_id"], citation["quote"]
        if (not isinstance(path, str) or path not in leaves or path in cited
                or not isinstance(identifier, str) or identifier not in entries
                or not isinstance(quote, str) or not quote or quote not in entries[identifier]):
            raise ValueError("Citation does not match observed evidence")
        value = leaves[path]
        if is_closed_leaf(schema, path):
            # "on" for a Wi-Fi row that shows its network, "yes"/true for a question: the
            # cited quote is the evidence and the value a judgment about it, which the
            # answer verifier must accept outright (Agent: no agreement shortcut).
            cited.add(path)
            continue
        literal = value if isinstance(value, str) else json.dumps(value, allow_nan=False, separators=(",", ":"))
        if not literal or literal not in quote:
            raise ValueError("Result transforms or invents an observed literal")
        if type(value) in (int, float, bool) and not re.search(r"(?<![\w.])" + re.escape(literal) + r"(?![\w.])", quote):
            raise ValueError("Numeric or boolean result is not an exact visible literal")
        cited.add(path)
    # Null is explicitly unknown, not an observed fact; every other leaf needs a citation.
    if any(path not in cited and value is not None for path, value in leaves.items()):
        raise ValueError("Every extracted value requires observed evidence")
    # Local per-field claim check: reject a plausible mis-binding before it ships.
    check_field_claims({"data": data, "citations": citations}, evidence)
    return {"data": data, "citations": citations}
