"""Parts of saved programs, switched on by MOBSTER_PROGRAMS=on alone (`parts_on`)."""

import json
import os
import re
import time

from ..replay_core.path import TEXT_OPS
from .programs import BLANK, FREE_FIELDS, TRUSTED, Matched, crosses, filled_steps, fits, folded, holds_secret

# On only with MOBSTER_PROGRAMS=on set explicitly, until it is measured on a phone.

MAX_PART_RUNS = 6
ANCHOR, MAX_VALUE_TOKENS, MIN_VALUE = 2, 12, 2
EXIT_WAIT, EXIT_POLL, EXIT_SHOWN = 2.0, .25, .25
FAMILY = {"READ": "look", "REPORT": "look", "WRITE": "write", "COMMIT": "commit"}
# Controls whose tap sets a value rather than going somewhere: a part never holds one.
VALUE_ROLES = frozenset({"Switch", "Toggle", "Slider", "Stepper", "PickerWheel", "Picker", "SegmentedControl",
                         "Adjustable", "CheckBox", "RadioButton"})
TOKEN = re.compile(r"\w+(?:['’]\w+)*|[^\w\s]")
START, END, HERE, OTHER = "^", "|", "zzherezz", "zzotherzz"


def parts_on(env=None):
    env = os.environ if env is None else env
    return (env.get("MOBSTER_PROGRAMS") or "").strip().casefold() == "on"


def tokens(text):
    text, out = text or "", []
    for found in TOKEN.finditer(text):
        token, after = folded(found.group(0)) or found.group(0), text[found.end():found.end() + 1]
        out.append((token, found.start(), found.end(),
                    token in ",;:?!" or token == "." and (not after or after.isspace())))
    return out


def cue(text, name):
    toks = tokens(BLANK.sub(lambda found: HERE if found.group(1) == name else OTHER, text or ""))
    at = next((index for index, token in enumerate(toks) if token[0] == HERE), None)
    if at is None:
        return None
    left, right = [], []
    for index in range(at - 1, at - 1 - ANCHOR, -1):
        if index < 0 or toks[index][3]:
            left.insert(0, START if index < 0 else END)
            break
        left.insert(0, toks[index][0])
    for index in range(at + 1, at + 1 + ANCHOR):
        if index >= len(toks) or toks[index][3]:
            right.append(END)
            break
        right.append(toks[index][0])
    return left, right


def _at(toks, index, mark):
    if mark == START:
        return index == -1
    if mark == END:
        return index == len(toks) or 0 <= index < len(toks) and toks[index][3]
    return 0 <= index < len(toks) and toks[index][0] == mark


def proposals(text, anchors):
    toks, (left, right) = tokens(text), anchors
    for weight, lefts, rights in ((3, left, right), (2, left, right[:1]), (2, left[-1:], right),
                                  (1, left[-1:], right[:1])):
        if not any(mark not in (START, END) and re.match(r"\w", mark) for mark in lefts + rights) \
                and not (weight == 3 and lefts == [START] and rights == [END]):
            continue
        out = []
        for start in range(len(toks)):
            if not all(_at(toks, start - len(lefts) + offset, mark) for offset, mark in enumerate(lefts)):
                continue
            for stop in range(start + 1, min(len(toks), start + MAX_VALUE_TOKENS) + 1):
                if toks[stop - 1][3] or stop - 1 > start and toks[stop - 1][0] in ("'", '"'):
                    break
                if all(_at(toks, stop + offset, mark) for offset, mark in enumerate(rights)):
                    value = text[toks[start][1]:toks[stop - 1][2]].strip(" '\"")
                    if len(value) >= MIN_VALUE:
                        out.append((value, weight))
                    break
        if out:
            return out
    return []


def _starts_or_ends(short, long):
    return len(short) <= len(long) and (long[:len(short)] == short or long[len(long) - len(short):] == short)


def _seq(text):
    return [token[0] for token in tokens(text)]


def agreed(found):
    """The value proposals agree on, or None."""
    if not found:
        return None
    seqs = {folded(value): (value, _seq(value)) for value, _ in reversed(found)}
    support = {key: sum(weight for value, weight in found if _starts_or_ends(seq, seqs[folded(value)][1]))
               for key, (_, seq) in seqs.items()}
    best = max(support, key=lambda key: (support[key], -len(seqs[key][1])))
    rivals = [support[key] for key in support if key != best and not _starts_or_ends(seqs[best][1], seqs[key][1])
              and not _starts_or_ends(seqs[key][1], seqs[best][1])]
    return seqs[best][0] if support[best] >= 2 and not any(rival >= support[best] for rival in rivals) else None


def _words(what):
    return {token[0] for token in tokens(BLANK.sub(" ", what or "")) if not token[3]}


def _about(taught, what):
    """Whether an item's ``what`` is about what the program's item was: two thirds of its words (blanks aside)."""
    want = _words(taught)
    return not want or len(want & _words(what)) / len(want) >= 2 / 3


def _bundle_of(app, apps):
    want = folded(app)
    return next((bundle for bundle, name in (apps or {}).items() if want and want in (folded(bundle), folded(name))),
                None)


def _blank(step):
    return step.get("param") or step.get("label_param") or step.get("label_prefix")


def _writes(step):
    from ..contract_screen import ACT_LABEL
    if step.get("commit"):
        return True
    if step.get("op") in TEXT_OPS:
        return not step.get("search")
    return step.get("op") == "TAP" and bool(ACT_LABEL.search((step.get("target") or {}).get("label") or ""))


def _cut(step):
    """A step no part holds: Home, a long press, a keypad key or a value control (never carried into another task)."""
    from ..task_policy import is_keypad_key
    target = step.get("target") or {}
    return (step.get("op") in ("HOME", "LONG_PRESS") or target.get("role") in VALUE_ROLES
            or is_keypad_key("TAP", target.get("role"), target.get("label"))
            or step.get("op") != "LAUNCH_APP" and not (step.get("screen") or {}).get("shape"))


def _same_screen(before, after):
    a, b = (before.get("screen") or {}), (after.get("screen") or {})
    if a.get("hash") and a.get("hash") == b.get("hash"):
        return True
    ours, theirs = set(a.get("shape") or ()), set(b.get("shape") or ())
    return bool(ours | theirs) and len(ours & theirs) / len(ours | theirs) >= .8


def parts_of(path):
    steps, pieces, current = path.get("steps") or [], [], []
    for index, step in enumerate(steps):
        if _cut(step):
            pieces, current = pieces + ([current] if current else []), []
            continue
        before = steps[current[-1]] if current else None
        if step.get("op") == "LAUNCH_APP" or before is not None and step.get("app") != before.get("app") \
                and before.get("op") != "LAUNCH_APP":
            pieces, current = pieces + ([current] if current else []), []
        current.append(index)
    pieces += [current] if current else []
    out = []
    for piece in pieces:
        reach = {}
        for position, index in enumerate(piece):
            if _blank(steps[index]):
                reach[_blank(steps[index])] = (reach.get(_blank(steps[index]), (position, position))[0], position)
        merged = []
        for first, last in sorted(reach.values()):
            if merged and first <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], last)
            else:
                merged.append([first, last])
        if not merged:
            out.append((piece[0], piece[-1]))
            continue
        merged[0][0], merged[-1][1] = 0, len(piece) - 1
        for number in range(len(merged) - 1):
            last, following = merged[number][1], merged[number + 1][0]
            if following > last + 1 and steps[piece[last]].get("op") in TEXT_OPS:
                merged[number][1] = last + 1
            merged[number + 1][0] = merged[number][1] + 1
        spans = []
        for first, last in merged:
            before = steps[piece[spans[-1][1]]] if spans else None
            if first > last:
                continue
            if before is not None and (before.get("op") in TEXT_OPS and not (before.get("op") == "TYPE_SUBMIT"
                                                                              and before.get("search"))
                                       or _same_screen(before, steps[piece[first]])):
                spans[-1][1] = last
            else:
                spans.append([first, last])
        out += [(piece[first], piece[last]) for first, last in spans]
        if len(spans) > 1:
            out.append((piece[0], piece[-1]))
    return list(dict.fromkeys(out))


def part_info(path, start, end):
    steps = (path.get("steps") or [])[start:end + 1]
    contract = [entry for entry in path.get("contract") or () if isinstance(entry, dict)]
    blanks = list(dict.fromkeys(_blank(step) for step in steps if _blank(step)))
    serves = [entry for entry in contract if any("{" + name + "}" in " ".join(str(entry.get(field) or "")
                                                                              for field in FREE_FIELDS)
                                                 for name in blanks)] if blanks else list(contract)
    for position, step in enumerate(steps):
        if step.get("commit") and not (step.get("op") in ("TYPE_SUBMIT", "SUBMIT") and step.get("search")):
            if not any(entry.get("kind") == "COMMIT" and entry.get("act") == step["commit"] for entry in serves):
                steps, end = steps[:position], start + position - 1  # a commit only for a commit its run declared
            break
    if not serves or not steps or len(steps) < 2 and not any(step.get("op") in TEXT_OPS for step in steps):
        return None
    following = (path.get("steps") or [])[end + 1] if end + 1 < len(path.get("steps") or []) else None
    screen = (following or {}).get("screen") or (path.get("final") if following is None else None) or {}
    app = next((step.get("app") for step in steps if step.get("op") != "LAUNCH_APP"), steps[0].get("app"))
    import hashlib
    key = hashlib.sha1(json.dumps([[step.get("op"), (step.get("target") or {}).get("role"), _blank(step)
                                    or (step.get("target") or {}).get("label")] for step in steps]).encode()
                       ).hexdigest()[:12]
    return {"start": start, "end": end, "app": app, "blanks": blanks, "serves": serves, "key": key,
            "writes": any(_writes(step) for step in steps), "launch": steps[0].get("op") == "LAUNCH_APP",
            "commit": next((step.get("commit") for step in steps if step.get("commit") and not step.get("search")),
                           None),
            "exit": {"app": (following or {}).get("app") if following and following.get("op") != "LAUNCH_APP"
                     else steps[-1].get("app"), "hash": screen.get("hash"), "shape": screen.get("shape"),
                     "shows": [name for name in (following or path.get("final") or {}).get("shows") or ()
                               if name in blanks]}}


def exit_holds(exit_, values, snapshot):
    from ..replay_core.engine import WEAK
    from ..replay_core.path import screen_shape, signature_hash
    from ..state import signature
    app, now = exit_.get("app"), getattr(snapshot, "bundle_id", "") or ""
    if app and now and app != now:
        return False, f"{now} is in front, not {app}"
    labels = [folded(element.label) for element in getattr(snapshot, "elements", ()) or () if element.label]
    shows = [name for name in exit_.get("shows") or () if values.get(name)]
    for name in shows:
        if not any(folded(values[name]) in label for label in labels):
            return False, f'"{values[name]}" isn\'t on the screen'
    if exit_.get("shape"):
        pairs = signature(snapshot)
        if not (exit_.get("hash") and signature_hash(pairs) == exit_["hash"]):
            ours, theirs = set(exit_["shape"]), set(screen_shape(pairs))
            likeness = len(ours & theirs) / len(ours | theirs) if ours | theirs else 1.0
            if likeness < (EXIT_SHOWN if shows else WEAK):
                return False, f"the screen is different ({round(likeness * 100)}% alike)"
    return True, ""


class PartRun(Matched):
    """A part filled for one request, run as the replay in progress (FrontierAgent's saved-program path)."""

    kind, noun = "part", "a part of a saved program"

    def __init__(self, path, info, values, item, launch=None):
        super().__init__({**path, "steps": (path.get("steps") or [])[info["start"]:info["end"] + 1],
                          "contract": None}, values)
        self.whole, self.info, self.item, self.outcome = path, info, item, None
        for step in self.steps:
            name = step.get("label_param") or step.get("label_prefix")
            if name and not self.values.get(name):
                step["target"] = {**step["target"], "label": "", "id": None}  # never the run that taught it
        if self.steps and self.steps[0].get("op") == "LAUNCH_APP" and launch is False:
            self.steps = self.steps[1:]
        elif launch:
            self.steps.insert(0, {"op": "LAUNCH_APP", "app": launch, "target": None, "screen": None, "text": None,
                                  "param": None, "commit": None, "point": None, "live": False})
        self.trusted = path.get("status") == TRUSTED or \
            ((path.get("parts") or {}).get(info["key"]) or {}).get("status") == TRUSTED

    def unbound(self, index):
        step = self.steps[index] if 0 <= index < len(self.steps) else {}
        return bool(_blank(step)) and not self.values.get(_blank(step))


class Parts:

    def __init__(self, store, *, device=None, run_id=None):
        self.store, self.device, self.run_id = store, device, run_id
        self.apps, self.pairs, self.last_pairs = {}, {}, None
        self.active, self.runs, self.tried, self._bound, self._paths = None, [], set(), {}, None

    def programs(self):
        if self._paths is None:
            try:
                # A coordinate step (a taught tap on a spot with no label) never runs inside another task's words.
                self._paths = [path for path in self.store.all()
                               if (path.get("identity") or {}).get("device") == (self.device or None)
                               and not any(step.get("op") == "TAP_XY" for step in path.get("steps") or ())]
            except Exception:
                self._paths = []
        return self._paths

    def candidates(self):
        """(path, info) for every part of every program on this phone that isn't forgotten."""
        for path in self.programs():
            for start, end in parts_of(path):
                info = part_info(path, start, end)
                if info and ((path.get("parts") or {}).get(info["key"]) or {}).get("status") != "forgotten":
                    yield path, info

    def _saw(self, agent, snapshot):
        from ..replay_core.path import signature_hash
        from ..state import signature
        self.apps = dict(getattr(agent, "apps", {}) or {})
        try:
            pairs = signature(snapshot)
        except Exception:
            return
        self.pairs[signature_hash(pairs)], self.last_pairs = pairs, pairs
        while len(self.pairs) > 64:
            self.pairs.pop(next(iter(self.pairs)))

    def bind(self, path, info, request, item=None, peers=()):
        """{blank: value} by the words around each blank: never a secret, never one value for two blanks."""
        key = (info["key"], (path.get("identity") or {}).get("template"), request, getattr(item, "id", None),
               tuple(getattr(peer, "id", None) for peer in peers))
        if key in self._bound:
            return self._bound[key]
        template, out = (path.get("identity") or {}).get("template") or "", {}
        for name in info["blanks"]:
            said = proposals(request, cue(template, name)) if cue(template, name) else []
            own, near = [], []
            for entry in path.get("contract") or ():
                for field in ("what", "payload", "quote"):
                    anchors = cue(entry.get(field), name)
                    if not anchors:
                        continue
                    for other in ([item] if item is not None else []) + [p for p in peers if p is not item]:
                        if other.kind == entry.get("kind") and _about(entry.get("what"), other.what):
                            (own if other is item else near).extend(proposals(getattr(other, field, ""), anchors))
            value = agreed(own) if own else agreed(near) if near else agreed(said)
            if value is None or folded(value) not in folded(request) or holds_secret(value) \
                    or crosses(template, name, value) or len(tokens(value)) > MAX_VALUE_TOKENS:
                continue
            if (info["writes"] or info["commit"]) and not any(_starts_or_ends(_seq(value), _seq(hit))
                                                              for hit, _ in said):
                continue  # what a part types, the request must say where its program's request said it
            out[name] = value
        twice = {folded(v) for v in out.values() if sum(folded(w) == folded(v) for w in out.values()) > 1}
        self._bound[key] = {name: value for name, value in out.items() if folded(value) not in twice}
        return self._bound[key]

    def may_start(self, agent, snapshot, request):
        self._saw(agent, snapshot)
        if not parts_on():
            return False
        front = getattr(snapshot, "bundle_id", "") or ""
        for path, info in self.candidates():
            steps = (path.get("steps") or [])[info["start"]:info["end"] + 1]
            if info["app"] != front or info["launch"]:
                continue
            template, guess = (path.get("identity") or {}).get("template") or "", {}
            for name in _needed(info):
                said = proposals(request, cue(template, name)) if cue(template, name) else []
                if said:
                    guess[name] = said[0][0]
            if _needed(info) <= set(guess) and fits(filled_steps({"steps": steps[:1]}, guess)[0], snapshot)[0]:
                return True
        return False

    def point(self, agent, snapshot, request, calls, actions):
        """(result, feedback, run, snapshot): a part that just ended is checked, then a new one may start."""
        self._saw(agent, snapshot)
        if not parts_on():
            return None, "", None, snapshot
        feedback, chain, run = "", True, self.active
        if run is not None:
            self.active = None
            if run.stopped is None and run.at >= len(run.steps):
                ok, why, snapshot = self._exit(agent, run, snapshot, calls)
                run.stopped = "done" if ok else why
                self._settle(agent, run, "hit" if ok else "miss")
                if not ok:
                    return None, (f"Mobster ran {run.noun} ({len(run.steps)} steps, no model), but its end check "
                                  f"failed: {why}. Carry on with the request from the screen you see."), None, snapshot
                if run.trusted:
                    ended = self._finish(agent, actions, calls)
                    if ended is not None:
                        return ended, "", None, snapshot
                chain = run.trusted  # after a provisional part the model looks first
                feedback = (f"Mobster ran {run.noun} with no model: {len(run.steps)} steps, its end checked on the "
                            "screen. Carry on with the request from the screen you see.")
            else:
                self._settle(agent, run, "partial" if run.unbound(run.at) else "miss")
        if not chain or len(self.runs) >= MAX_PART_RUNS:
            return None, feedback, None, snapshot
        pick = self._pick(agent, snapshot, request)
        if pick is not None:
            self.active = pick
            self.runs.append(pick)
            self.tried.add((pick.info["key"], getattr(pick.item, "id", None)))
            self._event(agent, phase="start", step=calls, part=pick.info["key"], steps=len(pick.steps),
                        trusted=pick.trusted, item=getattr(pick.item, "id", None), blanks=sorted(pick.values))
        return None, feedback, pick, snapshot

    def _event(self, agent, **fields):
        try:
            agent._emit({"event": "frontier_part", **fields})
        except Exception:
            pass

    def _pick(self, agent, snapshot, request):
        """The part to run now, or None: it serves the earliest open item (more steps, trusted, more hits first)."""
        contract = getattr(agent, "_contract", None)
        if contract is None:
            return None
        open_items, visited = [], set()
        for item in contract.items:
            if item.kind == "FORBID" or item.kind == "REPORT" and item.status != "open":
                continue
            try:
                if item.kind != "REPORT" and contract.proof(item)[0]:
                    if item.kind != "READ":
                        continue
                    visited.add(item.id)  # a READ a visit counts as done: a look while its value was never on screen
            except Exception:
                pass
            open_items.append(item)
        apps, front = dict(getattr(agent, "apps", {}) or {}), getattr(snapshot, "bundle_id", "") or ""
        busy_here = not getattr(agent, "_actions", 0) or any(
            _bundle_of(item.app, apps) in (front, None) for item in open_items if item.id not in visited)
        order = {item.id: index for index, item in enumerate(open_items)}
        best, best_rank = None, None
        for path, info in self.candidates():
            if info["app"] not in apps:
                continue
            item, values = self._serves(path, info, request, open_items, apps, contract, visited)
            if item is None or (info["key"], item.id) in self.tried:
                continue
            if info["commit"] and not (item.kind == "COMMIT" and item.act == info["commit"]):
                continue
            if _written(info, (path.get("steps") or [])[info["start"]:info["end"] + 1], values, contract, apps):
                continue  # this run already wrote it: never twice
            launch = None
            first = filled_steps({"steps": (path.get("steps") or [])[info["start"]:info["start"] + 2]}, values)
            if info["launch"]:
                if front != info["app"] and busy_here:
                    continue
                launch = False if front == info["app"] else None
            elif front == info["app"]:
                if not fits(first[0], snapshot)[0]:
                    continue
            elif not busy_here:
                launch = info["app"]  # nothing open is left where the run is: open this app first
            else:
                continue
            stats = (path.get("parts") or {}).get(info["key"]) or {}
            rank = (order[item.id], -(info["end"] - info["start"]), path.get("status") != TRUSTED,
                    -int(stats.get("hits") or 0))
            if best_rank is None or rank < best_rank:
                best, best_rank = PartRun(path, info, values, item, launch=launch), rank
        return best

    def _serves(self, path, info, request, open_items, apps, contract, visited):
        families = {FAMILY.get(entry.get("kind"), entry.get("kind")) for entry in info["serves"]}
        mine = [item for item in open_items if _bundle_of(item.app, apps) in (info["app"], None)
                and FAMILY.get(item.kind, item.kind) in families]
        texts = {item.id: folded(" ".join((item.what, item.payload, item.quote))) for item in mine}
        for item in mine:
            if not info["blanks"]:
                if item.id not in visited and any(_about(entry.get("what"), item.what) for entry in info["serves"]):
                    return item, {}
                continue
            values = self.bind(path, info, request, item, mine)
            if item.id in visited and any(contract._seen(lambda text, *_: folded(v) in text, item.app) is not None
                                          for v in values.values()):
                continue
            if values and _needed(info) <= set(values) and any(folded(v) in texts[item.id] for v in values.values()) \
                    and all(any(folded(v) in texts[other.id] for other in mine) for v in values.values()):
                return item, values
        return None, {}

    def _exit(self, agent, run, snapshot, calls):
        end, current = time.monotonic() + EXIT_WAIT, snapshot
        while True:
            ok, why = exit_holds(run.info["exit"], run.values, current)
            if ok or time.monotonic() >= end:
                return ok, why, current
            time.sleep(EXIT_POLL)
            try:
                current = agent._read_once()
                if getattr(agent, "_contract", None) is not None:
                    agent._contract.observe(agent._masked(current), agent._app_name(current), calls)
            except Exception:
                return ok, why, current

    def _finish(self, agent, actions, calls):
        import copy
        contract = getattr(agent, "_contract", None)
        if contract is None or actions < 1 or getattr(agent, "longform", None) is not None \
                or any(item.kind == "REPORT" for item in contract.items):
            return None
        said = "Done: Mobster repeated parts of saved programs for this request."
        if not copy.deepcopy(contract).done_gate(said, agent.max_steps - calls)[0]:
            return None
        accepted, _, answer = contract.done_gate(said, agent.max_steps - calls)
        if not accepted:
            return None
        self._event(agent, phase="finish", step=calls)
        return {"status": "completed", "answer": answer}

    def _settle(self, agent, run, outcome):
        run.outcome = outcome
        self._event(agent, phase="end", part=run.info["key"], outcome=outcome, at=run.at, steps=len(run.steps),
                    why=agent._mask(run.stopped or "")[:200] if hasattr(agent, "_mask") else "")
        if outcome == "miss" or outcome == "hit" and run.trusted:
            self.store.note_part(run.whole, run.info["key"], outcome, trusted=run.trusted)

    def settle(self, done):
        for run in self.runs:
            if run.outcome is None and run.stopped is None and run.at >= len(run.steps):
                run.outcome = "hit" if done else "miss"
                self.store.note_part(run.whole, run.info["key"], run.outcome, trusted=run.trusted)
            elif run.outcome == "hit" and not run.trusted:
                self.store.note_part(run.whole, run.info["key"], "hit" if done else "miss", trusted=False)

    def report(self):
        return {"started": len(self.runs), "hits": sum(run.outcome == "hit" for run in self.runs),
                "misses": sum(run.outcome == "miss" for run in self.runs),
                "partial": sum(run.outcome == "partial" for run in self.runs),
                "steps": sum(min(run.at, len(run.steps)) for run in self.runs)}


def _needed(info):
    """A part that commits needs every blank bound; another its first (one left unbound later hands the turn back)."""
    return set(info["blanks"]) if info["commit"] else set(info["blanks"][:1])


def _written(info, steps, values, contract, apps):
    typed = [values.get(step.get("param")) for step in steps if step.get("op") in TEXT_OPS]
    before = [folded(entry.get("text")) for entry in getattr(contract, "typed", ()) or ()]
    if any(value and folded(value) in before for value in typed):
        return True
    if typed or not info["writes"]:
        return False
    return folded(apps.get(info["app"]) or info["app"]) in (getattr(contract, "first_writes", {}) or {})
