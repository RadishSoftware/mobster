"""The local route corpus: saved programs keyed by the app version they were learned on.

A route is a saved program (learned/programs.py): a goal template and the steps that did it, as hashed screens plus
the controls' roles and names. The program store already finds a route first (``pre_run`` consults it before the model
plans); what it lacked is the version: a program learned on one build of an app replayed on the next and relied on
the step check to notice. With the flag on, a program is stored under the app's version (replay_core/store.py's env
key, so two builds keep two routes) and a later run is offered only the routes of its own version, or those learned
before versions were kept.

``shareable`` is the record a later opt-in sharing lane can send: the app, its version, a hash of the goal template
and each step's op, role and hashed label and screen. No screen text, no typed text, no template.
"""

import hashlib

from ..learned.programs import BLANK, folded


def goal_fingerprint(template):
    """16 hex digits for a goal template: blanks, case, spacing and end punctuation don't change it."""
    return hashlib.sha256(BLANK.sub("{}", folded(template)).encode()).hexdigest()[:16]


def version_of(runtime, bundle):
    """The version of ``bundle`` on the phone, or None (unknown: routes are then not told apart)."""
    from ..packs.core import app_version
    try:
        return app_version(runtime, bundle)
    except Exception:
        return None


def fits_version(path, version):
    """True when ``path`` may be offered on an app at ``version`` (None: any; a path without a version: always)."""
    known = (path.get("env") or {}).get("app_version")
    return version is None or known is None or known == version


def _digest(text):
    return hashlib.sha1(str(text or "").encode()).hexdigest()[:10]


def shareable(path):
    """The hashes-only record of a route, or None when it has no version or a step it can't describe."""
    identity, env = path.get("identity") or {}, path.get("env") or {}
    if identity.get("kind") != "program" or not env.get("app_version") or not identity.get("app"):
        return None
    steps = []
    for step in path.get("steps") or ():
        target = step.get("target") if isinstance(step.get("target"), dict) else {}
        screen = step.get("screen") if isinstance(step.get("screen"), dict) else {}
        steps.append({"op": step.get("op"), "role": target.get("role") or None,
                      "label": _digest(target.get("label")) if target.get("label") else None,
                      "screen": str(screen.get("hash") or "")[:16] or None})
    return {"app": identity["app"], "version": env["app_version"], "ios": env.get("ios"),
            "goal": goal_fingerprint(identity.get("template") or ""), "steps": steps}
