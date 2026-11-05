"""Saved programs for Smart: a request with the same words as one that worked, but other values, repeats that
run with code doing the checking, and the model only where code can't.

Replaying only a run's navigation before the model starts (#215's procedure memory, removed 12 Oct 2026: off by
default, and its A/B left the median unchanged) still paid the contract call, every typing and commit turn and the
DONE turn. A saved program keeps the whole run:

- **Learning.** A Smart run that ended done (the task contract proved every item, a write only from screens after
  it) is kept as a program:
  - its request as a template: each typed text, each tapped control's name and each row a SCROLL_TO looked for that
    appears in the request (case folded, whole words) is a blank ``{pN}``;
  - its steps (replay_core path steps): the action, the app, the target's role and label, and the screen before as
    hashed role/label pairs (``StepRecord.shape``; no screen text). A step may type only a blank's value or a
    search typed from the request's own words; the steps end at the first one that types anything else, uses a
    point, a macro or a skill (the model does it each run). A SCROLL_TO is kept only when the row it looked for is a
    blank: "Open Settings › General › Fonts" scrolls to "Fonts" on the General page, and its siblings to their own
    page (11 Oct, on an iPhone: the scroll ended the program, "Fonts" stayed literal and no sibling matched);
  - its task contract's items, with each blank's value as its blank. An item whose quote or payload holds words
    the template doesn't is not kept: then no contract is kept, and the run compiles one.
  Never kept: answers, notes, screen text, text the model wrote, secrets.
- **Matching** (``match``): programs from the same start app and phone are candidates. Code accepts a match only
  when every value is words of the new request and the filled template is the request (case, spacing and end
  punctuation aside): typed values come only from the new request, and none runs across a clause (``crosses``). One
  call to the run's model picks and fills only when code finds more than one way (below).
- **Replay** (``FrontierAgent(saved_program=...)``): the program's steps are decisions with no model call, through
  the same per-action path as the model's (guards, the contract's commit check, approvals, the journal); each one
  only on a screen like its recorded one with its target there exactly once. The contract is the program's, filled.
  When a step doesn't fit, the model takes over with the program's remaining steps as a suggested plan; a run whose
  contract is proven with nothing to report ends with no DONE call.
- **Trust.** A program is learned *provisional*: a contract can be proven on screen by a run whose write didn't land
  (a CalTrack log that the app never kept, 11 Oct). Its first replay is supervised: the steps run, but the model's
  DONE turn and the DONE gate decide the end, never code alone. It becomes *trusted* only after a replay that took
  every step and whose run ended done with every contract item proven (a write only from screens after the replay's
  own actions, #230). A provisional program that fails is forgotten at once; only a trusted one finishes in code.
- **Forgetting.** For a trusted program, a replay that stops before its last step or a run that doesn't end done is
  a miss; missed twice in a row, it is forgotten. A done run after a replay is learned again.
- **Matching without a call.** When code fills exactly one template so that it is the request, with one way to split
  the values, that's the match: no call. The call is made only when that's ambiguous (two templates, or two ways to
  split one); when no template can be the request, there's no call either.

``MOBSTER_PROGRAMS``: ``on`` learns and replays, ``learn`` only learns, ``off`` turns it off. Unset, it is on for runs
from Mobster for Mac, ``mobster serve``'s API, your coding agent's ``phone_task`` through ``mobster mcp`` and Mobster
Remote (DEFAULT_ON_ORIGINS) and off for the rest; any other value is off.
On by default since 11 Oct 2026: on an iPhone 15 Pro (iOS 26.0.1), exact repeats took 7.87 -> 3.91 s (Settings ›
General › About) and 4.37 -> 1.10 s (Clock's Stopwatch tab) at the median of 4 runs each, 16/16 passed, no unsafe act,
approvals unchanged; on iOS Simulators, code-graded siblings took 2.33 calls instead of 5.06 at the same pass rate.
Programs live in the data folder's ``replays/programs/`` (0700, files 0600), at most MAX_PROGRAMS.
"""

import json
import os
import re
import time
from pathlib import Path

from ..replay_core.path import ELEMENT_OPS, SWIPES, TEXT_OPS, describe, first_of, new_path
from ..replay_core.recorder import typed_plainly
from ..replay_core.store import PROGRAM_ID, Cache, env_of

MAX_PROGRAMS = 100
MAX_TAUGHT = 100          # taught programs, kept apart: learned ones never push one out
MAX_NAME = 80
PROVISIONAL, TRUSTED = "provisional", "trusted"
MISSES_TO_FORGET = 2
MAX_CANDIDATES = 5
MIN_BLANK = 3             # a tapped name shorter than this never becomes a blank ("OK", "Go")
MIN_LITERAL = 8
ITEM_FIELDS = ("kind", "app", "what", "payload", "act", "count", "condition", "quote")
FREE_FIELDS = frozenset({"what", "payload", "condition", "quote"})
PROGRAM_OPS = ELEMENT_OPS | SWIPES | frozenset({"LAUNCH_APP", "HOME"})
BLANK = re.compile(r"\{(p\d+)\}")
ROW_SPLIT = re.compile(r",|\s[·•|–—-]\s|\n")


# Run origins where saved programs are on while MOBSTER_PROGRAMS is unset: Mobster for Mac, `mobster serve`'s API, a
# coding agent's `phone_task` through `mobster mcp` ("mcp") and Mobster Remote ("remote"). Every one of them is a run in
# Mobster's own agent, with the same approvals as the app's: a replayed step that sends, posts, deletes or buys asks
# like any other (the permission mode also rises to ask-important for "mcp" and "remote", permissions/policy.py).
DEFAULT_ON_ORIGINS = frozenset({"app", "api", "mcp", "remote"})
ORIGINS = frozenset({"app", "tui", "cli", "api", "mcp", "remote"})


def mode(env=None, origin=None):
    env = os.environ if env is None else env
    value = (env.get("MOBSTER_PROGRAMS") or "").strip().casefold()
    if value in ("on", "learn", "off"):
        return value
    return "on" if not value and origin in DEFAULT_ON_ORIGINS else "off"


TAUGHT_ORIGINS = frozenset({"workflow", "schedule"})


def taught_on(env=None, origin=None):
    env = os.environ if env is None else env
    value = (env.get("MOBSTER_PROGRAMS") or "").strip().casefold()
    return value not in ("off", "learn") and origin in (ORIGINS | TAUGHT_ORIGINS)


def programs_dir(data_dir):
    return Path(data_dir) / "replays" / "programs" if data_dir else None


def normalized(text):
    return " ".join(str(text or "").split())


def folded(text):
    """For comparing a request with a filled template: case, spacing, quotes and end punctuation aside."""
    text = normalized(text).casefold().translate(str.maketrans({"‘": "'", "’": "'", "“": '"',
                                                                 "”": '"'}))
    return text.rstrip(" .!?")


def blank_for(template, values, value):
    value = normalized(value)
    if len(value) < MIN_BLANK or "{" in value or "}" in value:
        return template, None
    for name, known in values.items():
        if known.casefold() == value.casefold():
            return template, name
    found = re.search(r"(?<![\w{])" + re.escape(value) + r"(?![\w}])", template, flags=re.IGNORECASE)
    if not found:
        return template, None
    name = f"p{len(values) + 1}"
    values[name] = template[found.start():found.end()]
    return template[:found.start()] + "{" + name + "}" + template[found.end():], name


def program_from(goal, steps):
    template, values, out = normalized(goal), {}, []
    for step in steps:
        op = step.get("op")
        if op == "SCROLL_TO":
            # The row it looked for, when that row is the request's own words: a blank, as a typed value is.
            sought = step.get("scroll")
            if not (isinstance(sought, str) and typed_plainly(sought)):
                break
            template, name = blank_for(template, values, sought)
            if name is None:
                break  # a row the request doesn't name: the model scrolls each run
            screen = step.get("screen") if isinstance(step.get("screen"), dict) else {}
            out.append({"op": op, "app": step.get("app"), "target": None, "param": name, "text": None,
                        "commit": None, "point": None, "live": False,
                        "screen": {"hash": str(screen.get("hash") or "")[:64],
                                   **({"shape": list(screen["shape"])} if screen.get("shape") else {})}})
            continue
        if step.get("live") or op not in PROGRAM_OPS:
            break
        step = {key: value for key, value in step.items() if key not in ("param", "label_param", "label_prefix")}
        target = dict(step.get("target") or {}) if isinstance(step.get("target"), dict) else None
        if op in TEXT_OPS:
            text = step.get("text")
            if not (isinstance(text, str) and typed_plainly(text)):
                break
            template, name = blank_for(template, values, text)
            if name is None:
                break
            step["param"], step["text"] = name, None
        elif op in ("TAP", "LONG_PRESS") and target is not None and not step.get("commit"):
            # A commit's control is never a blank: "Pay" in "pay Maya $12" is the act, not a value to swap.
            label = target.get("label") or ""
            template, name = blank_for(template, values, label)
            if name is not None:
                step["label_param"] = name
            else:
                # A row named by the request, then its details ("Banana Bread, Recipes, ·, …"): the name is a blank
                # and the row is found again by a name that starts the same way (``fits``).
                head = ROW_SPLIT.split(label, 1)[0].strip()
                if head and head != label.strip():
                    template, name = blank_for(template, values, head)
                    if name is not None:
                        step["label_prefix"] = name
                        target["label"] = head
        if target is not None:
            target["id"] = target.get("id")
            step["target"] = target
        screen = step.get("screen") if isinstance(step.get("screen"), dict) else {}
        # No screen text: the hash and the hashed pairs only (replay_core.path.screen_shape).
        step["screen"] = {"hash": str(screen.get("hash") or "")[:64],
                          **({"shape": list(screen["shape"])} if screen.get("shape") else {})}
        out.append(step)
    if not out or len(BLANK.sub("", template).strip()) < MIN_LITERAL:
        return None
    for kept, step in zip(out, steps):
        # Which values the screen before a step showed, by blank name (its text is dropped): a part ending before this
        # step checks them again (``exit_holds``).
        kept["shows"] = shown(values, (step.get("screen") or {}).get("signature"))
    return template, out, values


def shown(values, pairs):
    """The blanks whose values a screen's role/label pairs show."""
    labels = [folded(label) for _, label in pairs or ()]
    return sorted(name for name, value in values.items() if value and any(folded(value) in label for label in labels))


def contract_template(items, values, template):
    """The items with blanks for their values, or None when an item holds words not in the template."""
    out = []
    words = set(re.findall(r"\w+", BLANK.sub(" ", template).casefold()))
    for item in items or ():
        entry = {}
        for name in ITEM_FIELDS:
            text = normalized(getattr(item, name, None) if not isinstance(item, dict) else item.get(name))
            if name in FREE_FIELDS:
                # Whole words only: "pay" is a blank's value, but never the "Pay" inside "SplitPay".
                for blank, value in sorted(values.items(), key=lambda pair: -len(pair[1])):
                    text = re.sub(r"(?<![\w{])" + re.escape(value) + r"(?![\w}])", "{" + blank + "}", text,
                                  flags=re.IGNORECASE)
            entry[name] = text
        for name in ("quote", "payload"):
            rest = set(re.findall(r"\w+", BLANK.sub(" ", entry[name]).casefold()))
            if not rest <= words:
                return None
        out.append(entry)
    return out


def fill(text, values):
    return BLANK.sub(lambda found: values.get(found.group(1), found.group(0)), text or "")


class Programs:
    """Programs in ``directory``: replay_core paths of kind "program"."""

    def __init__(self, directory, version=None):
        self.directory = Path(directory)
        self.cache = Cache(directory)
        # The app's version when the route corpus is on (compiled_read/routes.py): saved under it, offered for it.
        self.version = version

    @staticmethod
    def identity(app, template, device):
        return {"kind": "program", "app": app or None, "template": template, "device": device or None}

    def all(self):
        from ..replay_core.path import PathError, validate
        out = []
        try:
            files = list(self.directory.glob("*.json"))
        except OSError:
            return out
        for file in files:
            try:
                path = validate(json.loads(file.read_text(encoding="utf-8")))
            except (OSError, ValueError, PathError):
                continue
            if (path.get("identity") or {}).get("kind") == "program":
                out.append(path)
        return sorted(out, key=lambda p: -float(p.get("used_at") or p.get("created_at") or 0))

    def candidates(self, app, device, goal, limit=MAX_CANDIDATES, taught_only=False):
        """Programs started in ``app`` on ``device``, the ones whose template holds most of ``goal``'s words first."""
        words = set(re.findall(r"\w+", folded(goal)))
        found = []
        for path in self.all():
            identity = path.get("identity") or {}
            if taught_only and not path.get("taught"):
                continue
            if identity.get("app") != (app or None) and not path.get("taught") \
                    or identity.get("device") != (device or None):
                continue
            if self.version is not None and (path.get("env") or {}).get("app_version") not in (None, self.version):
                continue
            own = set(re.findall(r"\w+", folded(BLANK.sub(" ", identity.get("template") or ""))))
            found.append((len(own & words) / max(1, len(own)), path))
        found.sort(key=lambda pair: -pair[0])
        return [path for _, path in found[:limit]]

    def save(self, app, goal, device, steps, items, *, run_id, trusted=None, final_pairs=None, first=None):
        """Keep a done run's program: trusted only as the program this run proved or one already trusted."""
        made = program_from(goal, steps)
        if made is None:
            return None
        template, steps, values = made
        identity = self.identity(app, template, device)
        env = env_of(self.version, None, device)
        path = new_path(steps, kind="program", identity=identity, env=env, source_run=run_id)
        path["contract"] = contract_template(items, values, template)
        path["blanks"] = sorted(values)
        if final_pairs:
            from ..replay_core.path import screen_shape, signature_hash
            path["final"] = {"hash": signature_hash(final_pairs), "shape": screen_shape(final_pairs),
                             "shows": shown(values, final_pairs)}
        path["used_at"], path["misses"] = round(time.time(), 3), 0
        try:
            # Learned again after a replay: the count of replays and the trust carry over.
            previous = self.cache.load(identity, env)
        except (OSError, ValueError):
            previous = None
        if isinstance(previous, dict) and previous.get("taught"):
            # What the person taught stays as they taught it: a run that replayed it settles it (``note``), and never
            # replaces its steps with the run's own (which end at the first coordinate step).
            return None
        if isinstance(previous, dict):
            path["replays"] = int(previous.get("replays") or 0)
            path["parts"] = previous.get("parts") or {}
        kept = first_of(previous if isinstance(previous, dict) else None, first)
        if kept is not None:
            path["first"] = kept
        proven = trusted is not None and dict(trusted) == identity
        path["status"] = TRUSTED if proven or (isinstance(previous, dict) and previous.get("status") == TRUSTED) \
            else PROVISIONAL
        saved = self.cache.save(path)
        for old in [other for other in self.all() if not other.get("taught")][MAX_PROGRAMS:]:
            self.forget(old)
        return saved

    def note(self, path, *, missed, run_id=None):
        """After a replay."""
        path = dict(path)
        if path.get("taught"):
            return self._note_taught(path, missed=missed, run_id=run_id)
        if path.get("status") != TRUSTED:
            if missed:
                self.forget(path)
                return None
            path["status"] = TRUSTED
        path["misses"] = int(path.get("misses") or 0) + 1 if missed else 0
        path["replays"] = int(path.get("replays") or 0) + 1
        path["used_at"], path["last_run"] = round(time.time(), 3), run_id
        if path["misses"] >= MISSES_TO_FORGET:
            self.forget(path)
            return None
        try:
            self.cache.save(path)
        except (OSError, ValueError):
            pass
        return path

    def _note_taught(self, path, *, missed, run_id):
        try:
            current = self.cache.load(path.get("identity") or {}, path.get("env") or {}) or path
        except (OSError, ValueError):
            current = path
        current = dict(current)
        current["replays"] = int(current.get("replays") or 0) + 1
        current["misses"] = int(current.get("misses") or 0) + 1 if missed else 0
        if not missed:
            current["status"] = TRUSTED
        elif current["misses"] >= MISSES_TO_FORGET:
            current["status"], current["misses"] = PROVISIONAL, 0
        current["used_at"], current["last_run"] = round(time.time(), 3), run_id
        current["last"] = {"run": run_id, "at": round(time.time(), 3), "ok": not missed}
        try:
            self.cache.save(current)
        except (OSError, ValueError):
            pass
        return current

    def forget(self, path):
        return self.cache.forget(path.get("identity") or {}, path.get("env") or {})

    @staticmethod
    def id_of(path):
        from ..replay_core.store import file_name
        return file_name(path.get("identity") or {}, path.get("env") or {})[:-len(".json")]

    def by_id(self, program_id):
        if not isinstance(program_id, str) or not PROGRAM_ID.fullmatch(program_id):
            return None
        return next((path for path in self.all() if self.id_of(path) == program_id), None)

    def rename(self, program_id, name):
        name = " ".join(str(name or "").split())
        if not name or len(name) > MAX_NAME:
            raise ValueError(f"A program's name is 1 to {MAX_NAME} characters")
        path = self.by_id(program_id)
        if path is None:
            raise LookupError("No saved program has that id")
        path = dict(path)
        path["name"] = name
        self.cache.save(path)
        return path

    def note_part(self, path, key, outcome, *, trusted):
        path = self.cache.load(path.get("identity") or {}, path.get("env") or {})
        if path is None:
            return None
        stats = dict((path.get("parts") or {}).get(key) or {})
        stats["hits"] = int(stats.get("hits") or 0) + (outcome == "hit")
        stats["misses"] = int(stats.get("misses") or 0) + 1 if outcome == "miss" else 0
        stats["status"] = TRUSTED if outcome == "hit" or trusted and stats["misses"] < MISSES_TO_FORGET else "forgotten"
        path["parts"] = {**(path.get("parts") or {}), key: stats}
        try:
            self.cache.save(path)
        except (OSError, ValueError):
            pass
        return stats


def learn(store, *, app, goal, device, steps, items, done, run_id=None, trusted=None, origin=None, final_pairs=None,
          first=None):
    """Keep a run as a program when it ended done; returns the saved path or None."""
    if not done or mode(origin=origin) == "off" or not steps:
        return None
    try:
        return store.save(app, goal, device, steps, items, run_id=run_id, trusted=trusted, final_pairs=final_pairs,
                          first=first)
    except (OSError, ValueError):
        return None


def public(path, workflows=()):
    """A program as the Programs list shows it: no screen text, no step details beyond plain lines."""
    from ..replay_core.path import describe
    identity = path.get("identity") or {}
    template = identity.get("template") or ""
    example = path.get("example") if isinstance(path.get("example"), dict) else {}
    taught = path.get("taught") if isinstance(path.get("taught"), dict) else None
    linked = [workflow["id"] for workflow in workflows if isinstance(workflow, dict)
              and code_fills(template, workflow_words(workflow))]
    last = path.get("last") if isinstance(path.get("last"), dict) else None
    return {"id": Programs.id_of(path), "name": path.get("name") or template, "template": template,
            "request": fill(template, example) if example else template,
            "blanks": [{"name": name, "example": example.get(name)} for name in path.get("blanks") or ()],
            "taught": taught is not None, "status": path.get("status") or PROVISIONAL,
            "steps": len(path.get("steps") or ()), "coordinateSteps": (taught or {}).get("points", 0),
            "asks": sum(1 for step in path.get("steps") or () if step.get("commit")),
            "lines": [describe({**step, "text": None}) for step in (path.get("steps") or ())[:MAX_LINES]],
            "replays": int(path.get("replays") or 0), "device": identity.get("device"),
            "createdAt": _ms(path.get("created_at")), "usedAt": _ms(path.get("used_at")),
            "last": {"ok": bool(last.get("ok")), "at": _ms(last.get("at"))} if last else None,
            "first": first_of(path), "workflowIds": linked}


MAX_LINES = 12


def _ms(seconds):
    return round(float(seconds) * 1000) if isinstance(seconds, (int, float)) and seconds else None


def workflow_words(workflow):
    """A saved workflow's words as its run sends them with each blank's default (starters.fill)."""
    from ..workflows import starters
    try:
        return starters.fill(workflow.get("goal") or "", None)
    except Exception:  # noqa: BLE001
        return workflow.get("goal") or ""


def settle(store, *, matched, done, replayed_all, run_id=None):
    """After a run: note its replayed program (a miss unless it took every step and the run ended done)."""
    if matched is None:
        return None
    proven = bool(done and replayed_all)
    store.note(matched.path, missed=not proven, run_id=run_id)
    return dict(matched.path.get("identity") or {}) if proven else None


MATCH_SYSTEM = """You match a phone task request to saved programs: each is the template of a request that was \
done before, with blanks like {p1} where that request had its own values. A program fits when the new request is \
its template with other values in the blanks: the same task, the same words, other values. Pick the one that fits \
and fill each of its blanks with the new request's words, copied exactly. If none fits, answer null."""

MATCH_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["program", "values"], "properties": {
    "program": {"type": ["integer", "null"]},
    "values": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                          "required": ["blank", "value"],
                                          "properties": {"blank": {"type": "string"},
                                                         "value": {"type": "string"}}}}}}


def blank_roles(path):
    """How each blank is used, for the matching call: 'typed into "Title"', 'tapped "…" (Button)'."""
    roles = {}
    for step in path.get("steps") or ():
        target = step.get("target") or {}
        if step.get("param"):
            roles.setdefault(step["param"],
                             f'typed into the {target.get("role") or "field"} "{target.get("label") or ""}"')
        if step.get("label_param"):
            roles.setdefault(step["label_param"], f'the name of a {target.get("role") or "control"} it tapped')
        if step.get("label_prefix"):
            roles.setdefault(step["label_prefix"], f'the name of a list row ({target.get("role") or "row"}) it opened')
    return roles


class Matched:
    """A program filled for one request: its steps and contract items with the request's values."""

    def __init__(self, path, values):
        self.path, self.values = path, dict(values)
        self.steps = filled_steps(path, values)
        contract = path.get("contract")
        if not contract and path.get("taught"):
            # A program taught before its contract was made at save time: made now, by code, from its lesson.
            from .taught import lesson_items
            contract = lesson_items((path.get("identity") or {}).get("template") or "", path.get("steps") or [],
                                    path.get("final"))
        self.items = ([{name: fill(entry.get(name), values) for name in ITEM_FIELDS} for entry in contract]
                      if isinstance(contract, list) and contract else None)
        # Only a trusted program finishes in code; a provisional one's first replay ends through the model's DONE turn.
        self.trusted = path.get("status") == TRUSTED
        # Taught by hand on the live view (save_from_demo): the run says so, by the program's name.
        self.taught = bool(path.get("taught"))
        self.name = path.get("name") or None
        # A practice run (the person watches and answers each step): ``ask`` is the run's approval request, and
        # their corrections are kept here, then applied to the program after the run (apply_corrections).
        self.practice, self.ask, self.corrections = False, None, []
        self.paused_at, self.rejoins, self.helped = None, 0, False
        self.deciding = False
        self.at = 0
        self.stopped = None
        self.expect = None

    @staticmethod
    def read_by_code(snapshot, request):
        """A question the screen in front answers, read by code (learned/code_read.py), else None."""
        from .code_read import answer
        return answer(snapshot, request)

    @property
    def template(self):
        return (self.path.get("identity") or {}).get("template") or ""

    # How FrontierAgent runs a saved program's steps: it matches each to the screen and words it with these, so the
    # engine needs nothing from this package.
    @staticmethod
    def fits(step, snapshot):
        return fits(step, snapshot)

    @staticmethod
    def describe(step):
        return describe(step)

    @staticmethod
    def correction(snapshot, x, y, among=None):
        from ..replay_core.path import target_of
        from .teach import element_at, screen_of
        under = element_at(snapshot, x, y, among=among)
        screen, _ = screen_of(snapshot)
        if under is None:
            screen["fingerprint"] = snapshot.content_fingerprint
        return ({"op": "TAP" if under is not None else "TAP_XY", "target": target_of(under),
                 "point": None if under is not None else [round(x, 4), round(y, 4)], "screen": screen}, under)

    def variant(self, step, snapshot):
        alts = step.get("alts") or ()
        if not alts:
            return step
        from ..replay_core.engine import screen_likeness
        best, score = step, -1.0
        try:
            for candidate in [step] + [{**step, **alt, "alts": None} for alt in alts]:
                if fits(candidate, snapshot)[0]:
                    likeness = screen_likeness(candidate, snapshot)
                    if likeness > score:
                        best, score = candidate, likeness
        except Exception:  # noqa: BLE001 -- the step as taught: its own check decides
            return step
        return best

    def remaining(self, limit=8):
        """The steps not taken yet, as plain lines (no typed text): the model's suggested plan when it takes over."""
        left = self.steps[self.at:]
        lines = [describe({**step, "text": None}) for step in left[:limit]]
        return "; ".join(lines) + ("; …" if len(left) > limit else "")


def filled_steps(path, values):
    out = []
    for step in path.get("steps") or ():
        step = dict(step)
        if isinstance(step.get("purpose"), str):
            step["purpose"] = fill(step["purpose"], values)
        name = step.get("label_param") or step.get("label_prefix")
        if name and values.get(name):
            target = dict(step.get("target") or {})
            target["label"], target["id"] = values[name], None
            step["target"] = target
        out.append(step)
    return out


def fits(step, snapshot):
    from ..replay_core.engine import WEAK, match, plain, screen_likeness
    if not step.get("label_prefix"):
        return match(step, snapshot, settled=True)
    target = step.get("target") or {}
    want, role = plain(target.get("label")), target.get("role") or ""
    app, now = step.get("app"), getattr(snapshot, "bundle_id", "") or ""
    if app and now and app != now:
        return False, None, f"{now} is in front, not {app}"
    found = []
    for element in getattr(snapshot, "elements", ()) or ():
        head = ROW_SPLIT.split(normalized(element.label), 1)[0].strip().casefold()
        if want and head == want and (not role or element.role == role):
            found.append(element)
    if len(found) != 1:
        return False, None, (f'no single row named "{target.get("label")}" is on the screen')
    likeness = screen_likeness(step, snapshot)
    if likeness < WEAK:
        return False, None, f"the screen is different ({round(likeness * 100)}% alike)"
    return True, found[0], ""


# A value never runs across a clause: a quote mark (not an apostrophe in a word), ";", a sentence end, or "then"
# outside quotes. A longer request once filled a shorter template's last blank with its other clauses (12 Oct 2026).
CLAUSE = re.compile(r"(?<![^\W_])['\"‘’“”]|['\"‘’“”](?![^\W_])|;|[.!?](?=\s)")


def crosses(template, name, value):
    quoted = re.search(r"['\"‘’“”]\{" + name + r"\}['\"‘’“”]", template)
    return bool(CLAUSE.search(value or "") or not quoted and re.search(r"\bthen\b", value or "", re.I))


def code_fills(template, goal):
    """The ways code fills ``template`` into ``goal``; a value crossing a clause is no fill: the run is the model's."""
    template, goal = normalized(template).rstrip(" .!?"), normalized(goal).rstrip(" .!?")
    parts = re.split(r"(\{p\d+\})", template)
    if not BLANK.search(template):
        return [{}] if folded(template) == folded(goal) else []
    fills = []
    for lazy in (True, False):
        pattern = ""
        for part in parts:
            found = re.fullmatch(r"\{(p\d+)\}", part)
            pattern += f"(?P<{found.group(1)}>.+{'?' if lazy else ''})" if found else re.escape(part)
        hit = re.fullmatch(pattern, goal, flags=re.IGNORECASE | re.S)
        if hit:
            fill_ = {name: normalized(value) for name, value in hit.groupdict().items()}
            if fill_ not in fills and not any(crosses(template, name, v) for name, v in fill_.items()):
                fills.append(fill_)
    return fills


def match(client, store, *, goal, app, device, secret=None, timeout=30, taught_only=False):
    """(Matched, usage) for ``goal``, or (None, usage)."""
    fillable = []
    for path in store.candidates(app, device, goal, taught_only=taught_only):
        fills = code_fills((path.get("identity") or {}).get("template") or "", goal)
        if fills:
            fillable.append((path, fills))
    if not fillable:
        return None, {}
    if len(fillable) == 1 and len(fillable[0][1]) == 1:
        return accept(fillable[0][0], fillable[0][1][0], goal, secret=secret), {}
    candidates = [path for path, _ in fillable]
    listing = []
    for index, path in enumerate(candidates):
        template = (path.get("identity") or {}).get("template") or ""
        roles = blank_roles(path)
        uses = "; ".join(f"{{{name}}}: {roles.get(name, 'a value')}" for name in path.get("blanks") or ())
        listing.append(f"{index}. {template}" + (f"  ({uses})" if uses else ""))
    messages = [{"role": "system", "content": MATCH_SYSTEM}, {"role": "user", "content": [
        {"type": "text", "text": f"Request: {normalized(goal)}\n\nPrograms:\n" + "\n".join(listing)}]}]
    try:
        out, usage = client.complete(messages, MATCH_SCHEMA, timeout=timeout)
    except Exception:
        return None, {}
    pick = (out or {}).get("program")
    if not isinstance(pick, int) or isinstance(pick, bool) or not 0 <= pick < len(candidates):
        return None, usage
    path = candidates[pick]
    values = {}
    for pair in (out or {}).get("values") or ():
        if isinstance(pair, dict) and isinstance(pair.get("blank"), str) and isinstance(pair.get("value"), str):
            values[pair["blank"].strip("{} ")] = normalized(pair["value"])
    return accept(path, values, goal, secret=secret), usage


def accept(path, values, goal, *, secret=None):
    """A Matched when ``values`` fill the template into ``goal``, each the request's words and no secret; else None."""
    template = (path.get("identity") or {}).get("template") or ""
    names = set(BLANK.findall(template))
    if set(values) != names:
        return None
    asked = folded(goal)
    for name, value in values.items():
        if not value or folded(value) not in asked or (secret is not None and secret(value)) \
                or crosses(template, name, value):
            return None
    if folded(fill(template, values)) != asked:
        return None
    return Matched(path, values)


def holds_secret(value):
    """True for a value secret_filter would mask: it never fills a blank."""
    try:
        from ..secret_filter import redact
        return redact(value) != value
    except Exception:
        return True


__all__ = ["Programs", "programs_dir", "public", "code_fills", "workflow_words", "Matched", "PROVISIONAL", "TRUSTED",
           "mode", "taught_on"]
