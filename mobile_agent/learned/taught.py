"""What a stopped lesson becomes (learned/teach.py records it live): a taught program saved in the shape a done run's
program has, a lesson merged into one already taught, and a practice run's corrections kept in it."""

import re
import time

from ..replay_core.path import POINT_OPS, TEXT_OPS, new_path
from ..replay_core.recorder import typed_plainly
from ..replay_core.store import env_of
from .programs import (
    BLANK,
    MAX_NAME,
    MAX_TAUGHT,
    MIN_LITERAL,
    PROVISIONAL,
    TRUSTED,
    blank_for,
    code_fills,
    folded,
    holds_secret,
    normalized,
    shown,
)

OPENS = re.compile(r"^(?:please\s+)?(?:open|go to|show me|take me to|navigate to|launch)\b", re.I)
ASKS = re.compile(r"^(?:please\s+)?(?:what|which|how|when|where|who|is|are|does|do|tell me|read|check|find|get)\b",
                  re.I)
LOOKING = frozenset({"HOME", "LAUNCH_APP", "TAP", "SCROLL_TO", "SWIPE_UP", "SWIPE_DOWN"})


def final_title(final):
    """The title (navigation bar) of the screen a lesson ended on, from its recorded role/label pairs, else None."""
    pairs = (final or {}).get("pairs")
    for role, label in pairs or ():
        if role == "NavigationBar" and normalized(label) and not str(label).startswith(("_", "UI")):
            return normalized(label)[:160]
    return normalized((final or {}).get("title")) or None


def lesson_items(request, steps, final=None):
    """A taught lesson's contract, by code; None for a point, a step chosen each run or a commit the request lacks."""
    from ..catalog import app_name
    from ..contract import FAMILIES
    request = normalized(request)
    if not request or BLANK.search(request) or not steps:
        return None
    if any(step.get("decide") or step.get("op") == "TAP_XY" for step in steps):
        return None
    words = re.findall(r"\w+", request.casefold())
    commits = [step for step in steps if step.get("commit")]
    app = next((app_name(step.get("app")) for step in reversed(steps) if step.get("app")), None)
    if not app:
        return None
    if commits:
        items = []
        for step in commits:
            act = step["commit"]
            verb = next((word for word in words if word in FAMILIES.get(act, ())), None)
            if act not in FAMILIES or act in ("none", "other") or verb is None:
                return None
            items.append({"kind": "COMMIT", "app": app_name(step.get("app")) or app, "what": request[:160],
                          "payload": "", "act": act, "count": "", "condition": "", "quote": verb})
        return items
    if any(step.get("op") not in LOOKING for step in steps):
        return None
    last = next((step for step in reversed(steps) if step.get("op") in ("TAP", "SCROLL_TO") and step.get("app")), None)
    if OPENS.match(request):
        label = final_title(final) or normalized((((last or {}).get("target") or {}).get("label")) or "")
        if not label or not (final_title(final) or set(re.findall(r"\w+", label.casefold())) <= set(words)):
            return None
        return [{"kind": "READ", "app": app, "what": label[:160], "payload": "", "act": "none", "count": "",
                 "condition": "", "quote": label[:160]}]
    if ASKS.match(request):
        return [{"kind": "REPORT", "app": app, "what": request[:160], "payload": "", "act": "none", "count": "",
                 "condition": "", "quote": ""}]
    return None


def save_from_demo(store, steps, request, blanks, *, device, name=None, final=None, typed=None, purposes=None,
                   rules=None, decide=None, report=None):
    """Keep a lesson as a provisional saved program; a secret in the request, a blank or a kept text saves nothing."""
    from ..replay_core.path import screen_shape, signature_hash, validate
    from ..secret_filter import is_secret
    request = normalized(request)
    if not request:
        raise ValueError("Say in a few words what this does, such as Log coffee in CalTrack")
    if len(request) > 500:
        raise ValueError("Keep the words under 500 characters")
    if is_secret(request) or holds_secret(request):
        raise ValueError("Leave passwords and codes out of the words: Mobster never keeps them")
    name = " ".join(str(name or "").split()) or request
    if len(name) > MAX_NAME:
        raise ValueError(f"A program's name is 1 to {MAX_NAME} characters")
    typed = dict(typed or {})
    if not isinstance(blanks, (list, tuple)) or len(set(blanks)) != len(blanks) \
            or not all(type(index) is int and index in typed for index in blanks):
        raise ValueError("Each blank must be a step where you typed something")
    if not steps:
        raise ValueError("This lesson has no step Mobster can repeat. Teach it again, tapping named controls")
    purposes, rules, decide = dict(purposes or {}), dict(rules or {}), set(decide or ())
    for index, text in list(purposes.items()) + [(i, r) for i, given in rules.items() for r in given]:
        if type(index) is not int or not 0 <= index < len(steps) or not isinstance(text, str) \
                or not 1 <= len(normalized(text)) <= 160 or holds_secret(text):
            raise ValueError("A step's purpose or rule is 1 to 160 characters, with no password or code in it")
    if not all(type(index) is int and 0 <= index < len(steps) for index in decide):
        raise ValueError("A step Mobster chooses each run must be one of the lesson's steps")
    into = _taught_into(store, request, typed, device)
    if into is not None:
        # The same program, other values: its blanks are the typed values that fill them now.
        blanks = [index for index, text in typed.items() if isinstance(text, str)
                  and any(folded(text) == folded(value) for value in into[1].values())]
    template, values, out = request, {}, []
    for index, step in enumerate(steps):
        step = {key: value for key, value in step.items() if key != "pairs"}
        if index in purposes:
            step["purpose"] = normalized(purposes[index])
        if rules.get(index):
            step["rules"] = [normalized(rule) for rule in rules[index]][:3]
        if index in decide:
            step["decide"] = True
        if step.get("purpose") and holds_secret(step["purpose"]):
            step["purpose"] = None  # a drafted purpose never keeps what the filter would mask
        if step.get("op") in TEXT_OPS:
            text = typed.get(index)
            if not (isinstance(text, str) and typed_plainly(text)) or holds_secret(text):
                raise ValueError("A typed value can't be kept. Teach it again without it")
            if index in blanks and into is not None:
                # Named as the saved program names it: the blank this value fills in its words.
                blank = next(name for name, value in into[1].items() if folded(value) == folded(text))
                values[blank] = into[1][blank]
                step["param"], step["text"] = blank, None
            elif index in blanks:
                template, blank = blank_for(template, values, text)
                if blank is None:
                    raise ValueError(f'Put "{normalized(text)[:60]}" in the words, so a later task can change it')
                step["param"], step["text"] = blank, None
                if step.get("purpose"):
                    # "Search for “coffee”" becomes "Search for “{p1}”": each run reads its own value there.
                    step["purpose"] = re.sub(re.escape(normalized(text)), "{" + blank + "}", step["purpose"],
                                             flags=re.IGNORECASE)
            else:
                step["text"] = text
        out.append(step)
    if len(BLANK.sub("", template).strip()) < MIN_LITERAL:
        raise ValueError("Say a little more than the values: a few words that name the task")
    for kept, step in zip(out, steps):
        kept["shows"] = shown(values, step.get("pairs"))
    if report is not None:
        report.clear()
    if into is None:
        try:
            same = store.cache.load(store.identity(None, template, device), env_of(None, None, device))
        except (OSError, ValueError):
            same = None
        into = (same, None) if isinstance(same, dict) and same.get("taught") else None
    if into is not None:
        merged, added, changed = merge_lesson(into[0], out, purposes=set(purposes), answered=set(rules) | decide)
        validate(merged)
        store.cache.save(merged)
        if report is not None:
            report.update(into=merged.get("name") or template, added=added, changed=changed)
        return merged
    if sum(1 for path in store.all() if path.get("taught")) >= MAX_TAUGHT:
        raise ValueError(f"Mobster keeps {MAX_TAUGHT} taught programs. Forget one in Workflows, then save this")
    identity = store.identity(None, template, device)
    path = new_path(out, kind="program", identity=identity, env=env_of(None, None, device), source_run=None)
    path["contract"] = lesson_items(request, out, final)
    path["blanks"] = sorted(values)
    path["name"] = name
    path["example"] = dict(values)  # the person's own typed words (never a secret), for Run in the Programs list
    path["taught"] = {"at": round(time.time(), 3), "points": sum(1 for s in out if s.get("op") == "TAP_XY"),
                      "asks": sum(1 for s in out if s.get("commit"))}
    pairs = (final or {}).get("pairs")
    if pairs:
        path["final"] = {"hash": signature_hash(pairs), "shape": screen_shape(pairs), "shows": shown(values, pairs),
                         "title": final_title(final)}
    # A lesson that only opens and looks, with a recorded final screen, has nothing a write could fail to land: it is
    # trusted on save and finishes in code (frontier._screen_goal_finish).
    # Anything else keeps its supervised first replay.
    opens_only = bool(path["contract"]) and all(item["kind"] == "READ" for item in path["contract"]) \
        and bool(path.get("final"))
    path["status"], path["misses"], path["used_at"] = TRUSTED if opens_only else PROVISIONAL, 0, round(time.time(), 3)
    validate(path)
    store.cache.save(path)
    return path


def _taught_into(store, request, typed, device):
    found = []
    for path in store.all():
        identity = path.get("identity") or {}
        if not path.get("taught") or identity.get("device") != (device or None) \
                or not BLANK.search(identity.get("template") or ""):
            continue
        fills = code_fills(identity.get("template") or "", request)
        if len(fills) == 1:
            found.append((path, fills[0]))
    if len(found) != 1:
        return None
    path, values = found[0]
    typed_values = {folded(text) for text in (typed or {}).values() if isinstance(text, str)}
    if not all(folded(value) in typed_values for value in values.values()):
        return None
    return path, values


def _merge_key(step):
    op = step.get("op")
    if op in ("TAP", "TAP_XY", "LONG_PRESS"):
        return ("tap",)
    if op == "LAUNCH_APP":
        return ("open", step.get("app"))
    if op in TEXT_OPS:
        return (op, step.get("param") or ("text", step.get("text")))
    return (op,)


def _screen_key(step):
    screen = step.get("screen") or {}
    field = "fingerprint" if step.get("op") in POINT_OPS else "hash"
    return field, screen.get(field)


def _likeness(one, other):
    """Points require exact geometry; element steps compare their structural screen hashes and pairs."""
    if one.get("op") in POINT_OPS or other.get("op") in POINT_OPS:
        key = _screen_key(one)
        return 1.0 if key[1] and key == _screen_key(other) else 0.0
    one, other = one.get("screen") or {}, other.get("screen") or {}
    if one.get("hash") and one.get("hash") == other.get("hash"):
        return 1.0
    ours, theirs = set(one.get("shape") or ()), set(other.get("shape") or ())
    union = ours | theirs
    return len(ours & theirs) / len(union) if union else 0.0


def _same_action(one, other):
    if one.get("op") != other.get("op"):
        return False
    a, b = one.get("target") or {}, other.get("target") or {}
    if (a.get("label"), a.get("role")) != (b.get("label"), b.get("role")):
        return False
    p, q = one.get("point"), other.get("point")
    return p == q or (isinstance(p, list) and isinstance(q, list) and max(abs(p[0] - q[0]), abs(p[1] - q[1])) < .02)


def merge_lesson(path, steps, *, purposes=(), answered=()):
    from ..replay_core.engine import STRONG
    from ..replay_core.path import MAX_ALTS
    old = [dict(step) for step in path.get("steps") or ()]
    lead_old = next((i for i, step in enumerate(old) if step.get("op") != "HOME"), len(old))
    lead_new = next((i for i, step in enumerate(steps) if step.get("op") != "HOME"), len(steps))
    if [_merge_key(step) for step in old[lead_old:]] != [_merge_key(step) for step in steps[lead_new:]]:
        name = path.get("name") or (path.get("identity") or {}).get("template") or "this task"
        raise ValueError(f"These steps differ from the ones saved for “{name}”, not only on another screen. Use "
                         f"other words to keep both, or forget “{name}” in Programs first")
    added, changed = [], []
    for offset, new in enumerate(steps[lead_new:]):
        step, lesson_index = old[lead_old + offset], lead_new + offset
        if new.get("rules"):
            joined = [rule for rule in step.get("rules") or () if rule not in new["rules"]] + list(new["rules"])
            step["rules"] = joined[-3:]
        if lesson_index in purposes and new.get("purpose"):
            step["purpose"] = new["purpose"]
        if lesson_index in answered:
            step["decide"] = bool(new.get("decide"))
            if not step["decide"]:
                step.pop("decide")
        if step.get("op") in ("LAUNCH_APP", "HOME") or not (new.get("screen") or {}).get("hash"):
            continue
        version = {"op": new["op"], "target": new.get("target"), "point": new.get("point"), "screen": new["screen"]}
        if _likeness(step, new) >= STRONG:
            if not _same_action(step, new):
                step.update(version)
                changed.append(lesson_index)
            continue
        alts = [dict(alt) for alt in step.get("alts") or ()]
        like = next((i for i, alt in enumerate(alts) if _likeness(alt, new) >= STRONG), None)
        if like is not None and _same_action(alts[like], new):
            continue
        if like is not None:
            alts.pop(like)
        step["alts"] = (alts + [version])[-MAX_ALTS:]
        added.append(lesson_index)
    merged = {**path, "steps": old, "status": PROVISIONAL, "misses": 0, "used_at": round(time.time(), 3),
              "merged_at": round(time.time(), 3)}
    merged["taught"] = {**(path.get("taught") or {}), "points": sum(1 for s in old if s.get("op") == "TAP_XY"),
                        "asks": sum(1 for s in old if s.get("commit"))}
    return merged, added, changed


def apply_corrections(store, matched):
    """Keep a practice run's corrections and make the program provisional again, so its next run is checked."""
    from ..replay_core.path import MAX_ALTS
    corrections = list(getattr(matched, "corrections", None) or ())
    if not corrections or not getattr(matched, "taught", False):
        return None
    path = store.cache.load(matched.path.get("identity") or {}, matched.path.get("env") or {})
    if path is None:
        return None
    steps, skipped = [dict(step) for step in path.get("steps") or ()], set()
    for correction in corrections:
        index = correction.get("index")
        if not isinstance(index, int) or not 0 <= index < len(steps):
            continue
        if correction.get("kind") == "skip":
            skipped.add(index)
            continue
        change = {"op": correction["op"], "target": correction.get("target"), "point": correction.get("point"),
                  "screen": correction.get("screen")}
        step = steps[index]
        if correction.get("same"):
            step.update(change)
            step.pop("label_param", None), step.pop("label_prefix", None)
        else:
            alts = [alt for alt in step.get("alts") or () if _screen_key(alt) != _screen_key(change)]
            step["alts"] = (alts + [change])[-MAX_ALTS:]
    kept = [step for index, step in enumerate(steps) if index not in skipped]
    if not kept:
        store.forget(path)
        return None
    path = {**path, "steps": kept, "status": PROVISIONAL, "misses": 0, "corrected_at": round(time.time(), 3)}
    path["taught"] = {**(path.get("taught") or {}), "points": sum(1 for s in kept if s.get("op") == "TAP_XY"),
                      "asks": sum(1 for s in kept if s.get("commit"))}
    try:
        store.cache.save(path)
    except (OSError, ValueError):
        return None
    return path
