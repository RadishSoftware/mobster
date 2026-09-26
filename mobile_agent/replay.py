"""Compiled runs: replay a completed run's actions when their preconditions hold.

A run that finished through every gate (completion check, and answer
verification when an answer was requested) is recorded as the sequence of
actions it took. Each step carries its preconditions: the app, the screen's
structural signature (its role/label pairs) and, for a tap, the target's role
and label. When the same request runs again, a step whose screen is identical
or structurally similar, and whose target is uniquely present, reuses the
recorded action instead of asking the model. The first unmet precondition
abandons the rest of the recording and the run continues live. Answers are
never replayed; extraction and answer verification always run on the live
final screen.
"""

import hashlib
import json
import os
from pathlib import Path
import tempfile

# Actions a recording may carry. Text entry is never replayed: typed text is
# generated per run and a duplicate write is a side effect.
REPLAYABLE = frozenset({"TAP", "SWIPE_UP", "SWIPE_DOWN", "SWIPE_LEFT", "SWIPE_RIGHT", "BACK", "LAUNCH_APP"})
MAX_STEPS = 40


def request_key(goal, output_schema, output_format, allowed_bundles):
    identity = {"goal": " ".join(goal.split()), "schema": output_schema, "format": output_format,
                "allowed": sorted(allowed_bundles or ())}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


class ReplayStore:
    """One JSON file per request key, written atomically, private to the user."""

    def __init__(self, directory):
        self.directory = Path(directory)

    def _path(self, key):
        if not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise ValueError("Invalid replay key")
        return self.directory / f"{key}.json"

    def load(self, key):
        try:
            steps = json.loads(self._path(key).read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(steps, list) or not 0 < len(steps) <= MAX_STEPS or not all(map(valid_step, steps)):
            return None
        return steps

    def save(self, key, steps):
        if not steps or len(steps) > MAX_STEPS or not all(map(valid_step, steps)):
            return
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle, temporary = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
        try:
            with os.fdopen(handle, "w") as stream:
                json.dump(steps, stream)
            os.chmod(temporary, 0o600)
            os.replace(temporary, self._path(key))
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def forget(self, key):
        try:
            self._path(key).unlink()
        except OSError:
            pass


def valid_step(step):
    if not isinstance(step, dict) or step.get("operation") not in REPLAYABLE:
        return False
    if not isinstance(step.get("before"), str) or len(step["before"]) != 64:
        return False
    target = step.get("target")
    if step["operation"] == "TAP":
        return (isinstance(target, dict) and set(target) == {"locator", "label", "role"}
                and all(isinstance(target[k], str) for k in target))
    if step["operation"] == "LAUNCH_APP":
        return isinstance(target, str) and "." in target
    return target is None


# Structural similarity (Jaccard over role/label pairs) a screen needs to its
# recorded one. Byte-identical matching broke on a second loading phase or a
# changed badge; task-level replay without preconditions is known to be brittle
# (MAS-Bench: 10%), precondition-gated replay much less so (EchoPath: 87-93%).
REPLAY_SIMILARITY = .8
SIGNATURE_LIMIT = 200


def signature(snapshot):
    """What a screen is, structurally: its elements' role/label pairs."""
    pairs = sorted({(element.role, element.label) for element in snapshot.elements if element.label})
    return [list(pair) for pair in pairs[:SIGNATURE_LIMIT]]


def similarity(recorded, snapshot):
    ours = {tuple(pair) for pair in recorded}
    theirs = {tuple(pair) for pair in signature(snapshot)}
    union = ours | theirs
    return len(ours & theirs) / len(union) if union else 1.0


def record(snapshot_fingerprint, decision, target, snapshot=None):
    """The replayable form of one dispatched action, or None."""
    if decision.operation not in REPLAYABLE:
        return None
    if decision.operation == "TAP":
        if target is None:
            return None
        bound = {"locator": target.locator, "label": target.label, "role": target.role}
    elif decision.operation == "LAUNCH_APP":
        bound = decision.target
    else:
        bound = None
    entry = {"before": snapshot_fingerprint, "operation": decision.operation, "target": bound,
             "confidence": decision.confidence, "risk_tier": decision.risk_tier,
             "side_effect_risk": decision.side_effect_risk}
    if snapshot is not None:
        entry["bundle"] = snapshot.bundle_id
        entry["signature"] = signature(snapshot)
    return entry


def resolve(step, snapshot):
    """The element id this step targets on ``snapshot``, or False if it cannot apply.

    Preconditions: an identical screen, or the same app with a structurally
    similar screen; and, for a tap, exactly one element with the recorded
    role and label (its position in the tree may have moved).
    """
    exact = step["before"] == snapshot.content_fingerprint
    if not exact:
        recorded = step.get("signature")
        if (not isinstance(recorded, list) or step.get("bundle") != snapshot.bundle_id
                or similarity(recorded, snapshot) < REPLAY_SIMILARITY):
            return False
    if step["operation"] != "TAP":
        return step["target"] if step["operation"] == "LAUNCH_APP" else None
    wanted = step["target"]
    matches = [element for element in snapshot.elements
               if (element.locator, element.label, element.role) == (wanted["locator"], wanted["label"], wanted["role"])]
    if len(matches) != 1:
        matches = [element for element in snapshot.elements
                   if (element.label, element.role) == (wanted["label"], wanted["role"])]
    return matches[0].id if len(matches) == 1 else False
