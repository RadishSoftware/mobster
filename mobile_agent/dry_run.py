"""``mobster run --dry-run`` and ``"dryRun": true`` on POST /api/runs: what a task would do, read from its words alone.

Nothing starts: no run, no journal entry, no phone, no model call, no count against a policy's daily cap. The plan
names the apps the task would open, the outward acts its words ask for and whether Mobster would ask about each one
under the person's permission mode and the project's policy, and the estimated cost. The screens are not listed:
Mobster reads them from the phone as the task goes, and a dry run never touches it.
"""

import re

from . import costs
from .app_choice import infer_app
from .permissions import gate as permission_gate
from .permissions import policy as P
from .permissions import project_policy
from .task_policy import COMMIT_CONTROL, asked_part

MAX_ACTS = 12
_NOUN_BEFORE = re.compile(r"\b(?:the|a|an|best|my|your|its|top|new|each|every|one-line|last|\d+)\s*$", re.I)


def _acts(goal):
    """[(word, act)] the task's own words ask for, once each, in order; a prohibition ("don't send") asks for none."""
    from .harness_api import commit_act
    from .task_policy import PROHIBITION_SENTENCE
    # "Draft a reply. Don't send anything." rules out every act of the kind it names.
    ruled_out = {kind for sentence in PROHIBITION_SENTENCE.findall(goal) for found in COMMIT_CONTROL.finditer(sentence)
                 for kind in project_policy.kinds_of("commit", commit_act("TAP", found.group(0), None, None))}
    found, seen = [], set()
    asked = asked_part(goal)
    for match in COMMIT_CONTROL.finditer(asked):
        if _NOUN_BEFORE.search(asked[:match.start()]):
            continue  # "the best post", "needs a reply": the word names a thing
        word = " ".join(match.group(0).casefold().split())
        act = commit_act("TAP", word, None, None) or word
        if act not in seen and not project_policy.kinds_of("commit", act) & ruled_out:
            seen.add(act)
            found.append((word, act))
    return found[:MAX_ACTS]


def _apps(goal, apps):
    """The apps the task would open: the one its words name, then any other app it names."""
    chosen = infer_app(goal, apps)
    named = [app for app in sorted(apps, key=lambda a: -len(a["name"]))
             if len(app["name"]) >= 4 and app is not chosen and re.search(
                 rf"(?<![\w-]){re.escape(app['name'])}(?![\w-])", goal, re.I)]
    return [app for app in [chosen, *named] if app]


def plan(goal, *, apps, mode=P.DEFAULT_MODE, policy=None, profile=None, origin=None, estimator=None, now=None):
    """The dry run of ``goal`` as JSON. ``apps``: the phone's apps (catalog shape). ``estimator(goal, names)``: the cost
    range, default ``costs.smart_estimate``. Reads the policy's counts and never writes them."""
    goal = " ".join(str(goal or "").replace("’", "'").split())
    chosen = _apps(goal, apps)
    names = [app["name"] for app in apps]
    cost = (estimator or costs.smart_estimate)(goal, names)
    mode = P.effective_mode(mode, None, origin)
    bundle = chosen[0]["bundleId"] if chosen else ""
    asks = []
    for word, act in _acts(goal):
        kinds = project_policy.kinds_of("commit", act)
        by_mode = permission_gate.decision_for("commit", mode, existing=True) == "ask"
        by_policy = policy.asks(kinds, bundle, profile, now=now) if policy is not None else None
        if by_mode or by_policy:
            asks.append({"act": act, "words": word, "kinds": sorted(kinds), "bundleId": bundle or None,
                         "why": "your permission mode asks" if by_mode and not by_policy else by_policy,
                         "by": "policy" if by_policy and not by_mode else "mode"})
    return {"goal": goal, "dryRun": True, "sent": False, "apps": [{"id": a["id"], "name": a["name"],
                                                                  "bundleId": a["bundleId"]} for a in chosen],
            "permissionMode": mode, "asks": asks, "policy": _policy(policy),
            "cost": {"lowUsd": cost.get("lowUsd"), "highUsd": cost.get("highUsd"), "model": cost.get("model"),
                     "billing": cost.get("billing") or "api"},
            "screens": "Mobster reads each screen from the phone as the task goes. A dry run never touches it."}


def _policy(policy):
    if policy is None:
        return None
    return {"file": str(policy.path) if policy.path else None, "rules": len(policy.rules), "error": policy.error}


def lines(result):
    """The plan as text for a terminal."""
    out = [f"Dry run: {result['goal']}", "Nothing was sent to your iPhone."]
    apps = ", ".join(a["name"] for a in result["apps"])
    out.append(f"Apps it would open: {apps}." if apps else "It names no app, so it starts on the Home Screen.")
    if result["asks"]:
        out.append("It would ask before:")
        out += [f"  {a['act']}: {a['why']}" for a in result["asks"]]
    else:
        out.append("It would ask about nothing its words name. Mobster still asks about any step that sends, buys, "
                   "posts or deletes.")
    cost = result["cost"]
    if cost["billing"] == "plan":
        out.append("Estimated cost: nothing per call, it runs on your plan.")
    elif cost["lowUsd"] is not None:
        out.append(f"Estimated cost: ${cost['lowUsd']:.2f} to ${cost['highUsd']:.2f}.")
    if result["policy"] and result["policy"]["error"]:
        out.append(f"Your project policy can't be read, so every act asks: {result['policy']['error']}")
    out.append(result["screens"])
    return out
