"""A recorded action path: what a passing run did, step by step, with what each step needs to find on the screen.

A step is a plain dict, so it goes to JSON and YAML as it is::

    {"op": "TAP", "app": "com.example.app",
     "target": {"label": "Continue", "role": "Button", "id": "/0/3/1", "frame": [.1, .8, .8, .06]},
     "screen": {"hash": "9f2c...", "signature": [["Button", "Continue"], ["StaticText", "Welcome"]]},
     "text": null, "param": null, "commit": null, "point": null, "live": false}

- ``target``: the element acted on (label, role, the tree path as ``id``, its frame in screen fractions).
- ``screen``: the screen before the action: state.py's structural signature (role/label pairs) and its hash.
  Coordinate steps also hold its exact content fingerprint, including geometry.
- ``text``: typed text a replay may type again (a literal from the check's own steps), or None.
- ``param``: for a learned workflow, the name of the blank the typed text fills; ``text`` is then None.
- ``commit``: the act (harness_api.commit_act) when the step sends, posts, deletes, buys or submits.
- ``point``: (x, y) in screen fractions for TAP_XY and DISMISS, which have no element.
- ``live``: the step can't be replayed (text the model wrote, a macro read): the model does it each run.
"""

# A screen's role/label pairs as short hashes: the frontier's StepRecord.shape uses the same rule.
from ..state import SHAPE_LIMIT, SIGNATURE_LIMIT, screen_shape, signature_hash  # noqa: F401

VERSION = 1
MAX_STEPS = 60
MAX_ALTS = 4

ELEMENT_OPS = frozenset({"TAP", "LONG_PRESS", "TYPE", "TYPE_SUBMIT", "SET_TEXT", "SUBMIT"})
TEXT_OPS = frozenset({"TYPE", "TYPE_SUBMIT", "SET_TEXT"})
SWIPES = frozenset({"SWIPE_UP", "SWIPE_DOWN", "SWIPE_LEFT", "SWIPE_RIGHT"})
POINT_OPS = frozenset({"TAP_XY", "DISMISS"})
OPS = ELEMENT_OPS | SWIPES | POINT_OPS | frozenset({"LAUNCH_APP", "HOME", "WAIT"})
LIVE_OPS = frozenset({"READ_LIST", "SCROLL_TO"})

VERBS = {"TAP": "Tap", "LONG_PRESS": "Press and hold", "TYPE": "Type in", "TYPE_SUBMIT": "Type and submit in",
         "SET_TEXT": "Set the text of", "SUBMIT": "Submit", "SWIPE_UP": "Swipe up", "SWIPE_DOWN": "Swipe down",
         "SWIPE_LEFT": "Swipe left", "SWIPE_RIGHT": "Swipe right", "LAUNCH_APP": "Open",
         "HOME": "Go to the Home Screen",
         "TAP_XY": "Tap a point", "DISMISS": "Dismiss", "WAIT": "Wait", "READ_LIST": "Read the list",
         "SCROLL_TO": "Scroll to"}


class PathError(ValueError):
    """A stored path that can't be used: it is ignored, and the next run records a new one."""


def screen_hashes(snapshot):
    """{"hash", "shape", "app"} for ``snapshot`` as hashes, with no screen text; None when there's no screen."""
    from ..state import signature
    if snapshot is None:
        return None
    try:
        pairs = signature(snapshot)
    except Exception:
        return None
    return {"hash": signature_hash(pairs), "shape": screen_shape(pairs),
            "app": getattr(snapshot, "bundle_id", None) or None}


def target_of(element):
    """A step's ``target`` from a state.Element."""
    if element is None:
        return None
    return {"label": element.label or "", "role": element.role or "", "id": element.locator or None,
            "frame": [round(float(value), 4) for value in element.rect]}


def describe(step):
    if not isinstance(step, dict):
        return "an unknown step"
    if isinstance(step.get("purpose"), str) and step["purpose"].strip():
        return step["purpose"].strip()
    op = step.get("op") or "?"
    verb = VERBS.get(op, op.title())
    target = step.get("target") or {}
    label = target.get("label") if isinstance(target, dict) else None
    if op == "LAUNCH_APP":
        return f"{verb} {step.get('app') or step.get('bundle') or 'the app'}"
    if label:
        return f'{verb} "{label}"'
    if op in ELEMENT_OPS and isinstance(target, dict) and target.get("role"):
        return f"{verb} the {target['role']}"
    return verb


def anchor(step):
    """What a heal diff shows for a step: op, label and role (no tree path, no frame)."""
    target = step.get("target") if isinstance(step.get("target"), dict) else {}
    out = {"op": step.get("op")}
    if target.get("label"):
        out["label"] = target["label"]
    if target.get("role"):
        out["role"] = target["role"]
    if step.get("op") == "LAUNCH_APP" and step.get("app"):
        out["app"] = step["app"]
    if step.get("param"):
        out["text"] = "{" + step["param"] + "}"
    elif step.get("text") is not None:
        out["text"] = step["text"]
    return out


def _string(value, limit=500):
    return isinstance(value, str) and len(value) <= limit


def valid_step(step):
    """True for a step a replay can trust. A path with any invalid step is dropped as a whole."""
    if not isinstance(step, dict):
        return False
    op = step.get("op")
    if not isinstance(op, str):
        return False
    # A saved program's scroll to a blank's value (learned/programs.py) is kept, not live: the frontier runs it as its
    # SCROLL_TO with the request's value. Nothing else replays it (Replayer.act refuses an op it can't run).
    scroll = op == "SCROLL_TO" and _string(step.get("param"), 16) and step.get("target") is None
    if op not in OPS and not scroll and not (step.get("live") is True and _string(op, 40) and op.isupper()):
        return False
    if step.get("app") is not None and not _string(step["app"], 255):
        return False
    target = step.get("target")
    if target is not None:
        if not isinstance(target, dict) or not _string(target.get("label", ""), 500) \
                or not _string(target.get("role", ""), 500):
            return False
        if target.get("id") is not None and not _string(target["id"], 16000):
            return False
        frame = target.get("frame")
        if frame is not None and (not isinstance(frame, list) or len(frame) != 4
                                  or not all(isinstance(v, (int, float)) and 0 <= v <= 1.0001 for v in frame)):
            return False
    if op in ELEMENT_OPS and not step.get("live") and target is None:
        return False
    if op == "LAUNCH_APP" and not (_string(step.get("app"), 255) and "." in step["app"]):
        return False
    screen = step.get("screen")
    if screen is not None:
        if not isinstance(screen, dict) or not _string(screen.get("hash", ""), 64):
            return False
        signature = screen.get("signature")
        if signature is not None and (not isinstance(signature, list) or len(signature) > SIGNATURE_LIMIT or not all(
                isinstance(pair, list) and len(pair) == 2 and all(_string(v) for v in pair) for pair in signature)):
            return False
        shape = screen.get("shape")
        if shape is not None and (not isinstance(shape, list) or len(shape) > SHAPE_LIMIT
                                  or not all(_string(v, 16) for v in shape)):
            return False
        fingerprint = screen.get("fingerprint")
        if fingerprint is not None and (not isinstance(fingerprint, str) or len(fingerprint) != 64
                                        or any(char not in "0123456789abcdef" for char in fingerprint)):
            return False
    if step.get("text") is not None and not _string(step["text"], 4000):
        return False
    if step.get("param") is not None and not _string(step["param"], 40):
        return False
    if op in TEXT_OPS and not step.get("live") and step.get("text") is None and step.get("param") is None:
        return False
    point = step.get("point")
    if point is not None and (not isinstance(point, list) or len(point) != 2
                              or not all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in point)):
        return False
    if op in POINT_OPS and not step.get("live") and point is None:
        return False
    if step.get("commit") is not None and not _string(step["commit"], 40):
        return False
    # A taught step (learned/teach.py): what it is for, the person's rules for it, and other screens it was shown on.
    if step.get("purpose") is not None and not _string(step["purpose"], 200):
        return False
    rules = step.get("rules")
    if rules is not None and (not isinstance(rules, list) or len(rules) > 3 or not all(_string(r, 200) for r in rules)):
        return False
    # ``decide``: the person asked Mobster to choose this step each run (the model does it, by its purpose and rules).
    if step.get("decide") is not None and not isinstance(step["decide"], bool):
        return False
    alts = step.get("alts")
    if alts is not None and (not isinstance(alts, list) or len(alts) > MAX_ALTS or not all(
            isinstance(alt, dict) and _alt_ok(step, alt) for alt in alts)):
        return False
    return True


def _alt_ok(step, alt):
    op = alt.get("op")
    if op in ("TAP", "TAP_XY", "LONG_PRESS"):
        return valid_step({**step, **alt, "alts": None, "param": None, "text": None})
    if op == step.get("op") and (op in SWIPES or op in TEXT_OPS):
        return valid_step({**step, **alt, "alts": None})
    return False


def validate(data):
    """A stored path dict, checked; raises PathError. Returns it."""
    if not isinstance(data, dict) or data.get("version") != VERSION:
        raise PathError("not a replay path of this version")
    steps = data.get("steps")
    if not isinstance(steps, list) or not 0 < len(steps) <= MAX_STEPS:
        raise PathError(f"a path has 1 to {MAX_STEPS} steps")
    for index, step in enumerate(steps):
        if not valid_step(step):
            raise PathError(f"step {index + 1} is not a step a replay can trust")
    final = data.get("final")
    if final is not None and (not isinstance(final, dict) or not _string(final.get("hash", ""), 64)
                              or final.get("app") is not None and not _string(final["app"], 255)
                              or final.get("shape") is not None and (
                                  not isinstance(final["shape"], list) or len(final["shape"]) > SHAPE_LIMIT
                                  or not all(_string(v, 16) for v in final["shape"]))):
        raise PathError("the final screen is not valid")
    strict = data.get("strict", [])
    if not isinstance(strict, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in strict):
        raise PathError("strict must be a list of step numbers")
    return data


def new_path(steps, *, kind, identity, env, final=None, source_run=None, created_at=None):
    """A path dict from recorded ``steps``."""
    import time
    return {"version": VERSION, "kind": kind, "identity": identity, "env": dict(env), "steps": list(steps),
            "final": final, "source_run": source_run, "created_at": created_at or round(time.time(), 3),
            "strict": [], "replays": 0}


def first_run(run_id, seconds, cost_usd, at=None):
    """What the run a path was first learned from took; ``costUsd`` is None when unreported, never a guess."""
    import time
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not 0 <= seconds < 86_400:
        return None
    cost = cost_usd if isinstance(cost_usd, (int, float)) and not isinstance(cost_usd, bool) and cost_usd >= 0 else None
    return {"runId": run_id if isinstance(run_id, str) else None, "seconds": round(float(seconds), 2),
            "costUsd": round(float(cost), 5) if cost is not None else None, "at": round(at or time.time(), 3)}


def first_of(path, given=None):
    kept = (path or {}).get("first") if isinstance(path, dict) else None
    for value in (kept, given):
        if isinstance(value, dict) and isinstance(value.get("seconds"), (int, float)):
            return first_run(value.get("runId"), value["seconds"], value.get("costUsd"), value.get("at"))
    return None


def replayable_count(path):
    return sum(1 for step in path.get("steps") or () if not step.get("live"))
