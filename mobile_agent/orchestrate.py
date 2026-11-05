"""Orchestration shapes: the interpretation line, the plan card, proposals reviewed together and per-app sub-runs."""

import json
import re

PLAN_APPS = 8
PLAN_STEPS = 8
PLAN_POLICY = 3
PROPOSALS = 20
PINNED_APPS = 5
DRAFT_CHARS = 4000
SUBRUN_OUTCOMES = ("done", "check", "couldnt_finish", "stopped", "skipped")
PROPOSAL_STATUS = ("sent", "failed", "skipped")
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,40}")


def _text(value, limit, what, required=True):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        if required:
            raise ValueError(f"{what} is text")
        return None
    return " ".join(value.split())[:limit]


def _draft(value, what):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{what} is text")
    value = value.replace("\r\n", "\n").strip()
    if len(value) > DRAFT_CHARS:
        raise ValueError(f"{what} is at most {DRAFT_CHARS:,} characters")
    return value


def _ident(value, what):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{what} is 1-40 letters, digits or _.:-")
    return value


def _bundle(value):
    return value if isinstance(value, str) and 0 < len(value) <= 155 else None


def app_ref(value):
    if isinstance(value, str):
        return {"name": _text(value, 80, "An app's name"), "bundleId": None}
    if not isinstance(value, dict):
        raise ValueError("An app is its name, or a dict with its name")
    return {"name": _text(value.get("name"), 80, "An app's name"), "bundleId": _bundle(value.get("bundleId"))}


def interpretation(text, apps=(), read_only=True):
    if type(read_only) is not bool:
        raise ValueError("read_only is true or false")
    return {"event": "interpretation", "text": _text(text, 160, "An interpretation"),
            "apps": [app_ref(app) for app in list(apps or ())[:PLAN_APPS]], "readOnly": read_only}


LONG_PLAN_BYTES = 16_000


def plan(value):
    if not isinstance(value, dict):
        raise ValueError("A plan is a dict")
    long_plan = value.get("long")
    if long_plan is not None:
        if not isinstance(long_plan, dict) or len(json.dumps(long_plan, default=str)) > LONG_PLAN_BYTES:
            raise ValueError("A long task's plan is a dict of at most 16,000 characters")
    apps = value.get("apps")
    if not isinstance(apps, list) or not (0 if long_plan is not None else 1) <= len(apps) <= PLAN_APPS:
        raise ValueError(f"A plan names 1-{PLAN_APPS} apps")
    clean_apps = []
    for app in apps:
        ref = app_ref(app)
        does = (_text(app.get("does"), 120, "What a plan does in an app", required=False)
                if isinstance(app, dict) else None)
        clean_apps.append({**ref, "does": does})
    steps = value.get("steps") or []
    if not isinstance(steps, list) or len(steps) > PLAN_STEPS:
        raise ValueError(f"A plan has at most {PLAN_STEPS} steps")
    clean_steps = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError("A plan's step is a dict")
        clean_steps.append({"id": _ident(str(step.get("id", index + 1)), "A step's id"),
                            "title": _text(step.get("title"), 120, "A step's title"),
                            "app": _text(step.get("app"), 80, "A step's app", required=False)})
    policy = value.get("policy") or []
    if not isinstance(policy, list) or len(policy) > PLAN_POLICY:
        raise ValueError(f"A plan says at most {PLAN_POLICY} limits")
    read_only = value.get("readOnly", False)
    if type(read_only) is not bool:
        raise ValueError("readOnly is true or false")
    out = {"summary": _text(value.get("summary"), 200, "A plan's summary"), "apps": clean_apps, "steps": clean_steps,
           "policy": [_text(line, 120, "A plan's limit") for line in policy], "readOnly": read_only}
    if long_plan is not None:
        out["long"] = long_plan
    return out


def long_plan(card):
    from .longform.plan import public
    shown = public(card)
    policy = shown.get("policy") or {}
    return plan({"summary": shown.get("summary") or shown.get("title") or "A long task",
                 "apps": [{"name": app.get("name"), "bundleId": app.get("bundleId")} for app in shown.get("apps") or ()
                          if isinstance(app, dict) and app.get("name")][:PLAN_APPS],
                 "steps": [{"id": str(step.get("id", index + 1)), "title": step.get("title"), "app": step.get("app")}
                           for index, step in enumerate(shown.get("steps") or ()) if isinstance(step, dict)
                           and step.get("title")][:PLAN_STEPS],
                 "policy": [line for line in policy.get("lines") or () if isinstance(line, str) and line.strip()][
                     :PLAN_POLICY],
                 "readOnly": policy.get("readOnly") is True, "long": shown})


def long_choices(card):
    budget = (card or {}).get("budget") or {}
    choices = [{"id": "default", "label": str(budget.get("label") or "As planned")[:120]}]
    for item in (card or {}).get("alternatives") or ():
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item.get("label"):
            choices.append({"id": item["id"][:40], "label": str(item["label"])[:120]})
    return choices[:6]


# Apple's apps whose names are everyday words count only when written with their capital ("check my messages" isn't
# the Messages app; "across Messages, WhatsApp and Instagram" is).
_EVERYDAY = {"home", "news", "photos", "music", "clock", "files", "phone", "mail", "find my", "messages", "notes",
             "maps",
             "books", "health", "wallet", "camera", "weather", "calendar", "contacts", "reminders"}


def named_apps(goal, apps):
    found = []
    for app in sorted((a for a in apps or () if isinstance(a, dict) and a.get("name")), key=lambda a: -len(a["name"])):
        name = app["name"]
        if len(name) < 3:
            continue
        flags = 0 if name.casefold() in _EVERYDAY else re.IGNORECASE
        match = re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", goal or "", flags)
        if match and not any(name.casefold() in other["name"].casefold() for _, other in found):
            found.append((match.start(), app))
    return [app for _, app in sorted(found, key=lambda pair: pair[0])]


_DRAFTS = re.compile(r"\b(draft|reply|replies|respond|answer them|write back)\b", re.I)


def apps_plan(goal, apps, asks):
    drafts = bool(_DRAFTS.search(goal or ""))
    does = "Read and draft replies" if drafts else None
    names = [app["name"] for app in apps]
    first = re.split(r"(?<=[.!?])\s", (goal or "").strip())[0]
    summary = f"Read your messages in {joined(names)} and draft replies" if drafts else first[:200]
    return plan({"summary": summary or f"Work in {joined(names)}",
                 "apps": [{"name": app["name"], "bundleId": app.get("bundleId"), "does": does}
                          for app in apps][:PLAN_APPS],
                 "policy": ["Nothing sends until you OK it"] if drafts and asks else [], "readOnly": not drafts})



def proposals(items):
    if not isinstance(items, (list, tuple)) or not 1 <= len(items) <= PROPOSALS:
        raise ValueError(f"Propose 1-{PROPOSALS} items")
    clean, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("A proposal is a dict")
        ident = _ident(item.get("id"), "A proposal's id")
        if ident in seen:
            raise ValueError("Each proposal has its own id")
        seen.add(ident)
        clean.append({"id": ident, "app": _text(item.get("app"), 80, "A proposal's app"),
                      "bundleId": _bundle(item.get("bundleId")),
                      "act": _text(item.get("act"), 40, "A proposal's act").lower(),
                      "to": _text(item.get("to"), 120, "Who a proposal goes to", required=False),
                      "title": _text(item.get("title"), 160, "A proposal's title"),
                      "text": _draft(item.get("text"), "A draft"),
                      "context": _text(item.get("context"), 300, "What a proposal answers", required=False),
                      "decision": None, "edited": False})
    return clean


def public_proposal(item):
    # Never the draft or what it answers: those stay in the pending approval.
    return {key: item.get(key) for key in ("id", "app", "bundleId", "act", "to", "title")}


def subrun(app, bundle_id=None, title=None, subrun_id=None, run_id=None):
    out = {"subrun": _ident(subrun_id, "A sub-run's id") if subrun_id is not None else None,
           "app": _text(app, 80, "A sub-run's app"), "bundleId": _bundle(bundle_id),
           "title": _text(title, 120, "A sub-run's title", required=False)}
    if run_id is not None:
        if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{12}", run_id):
            raise ValueError("run_id is a task's id")
        out["runId"] = run_id
    return out


def counts(value):
    if value is None:
        return None
    if not isinstance(value, dict) or len(value) > 6 or not all(
            isinstance(key, str) and re.fullmatch(r"[a-z][a-zA-Z]{0,23}", key) and type(number) is int and number >= 0
            for key, number in value.items()):
        raise ValueError("counts is up to 6 names with whole numbers")
    return dict(value)


def joined(names):
    names = [name for name in names if name]
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}" if names else ""
