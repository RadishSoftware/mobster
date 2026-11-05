"""A trigger's fields, checked. Every refusal is a sentence that says what to change."""

import re

from ..notify_triggers.rules import own_app

NAME_MAX = 60
WORDS_MAX = 10
WORD_MAX = 40
TASK_MAX = 1000
MAX_TRIGGERS = 50
SOURCES = ("iphone", "mac", "any")
ACTIONS = ("destination", "workflow", "task")
BUNDLE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,158}[A-Za-z0-9_]")
ID = re.compile(r"[0-9a-f]{12}")
FIELDS = {"name", "enabled", "app", "appName", "words", "source", "action"}
ACTION_FIELDS = {"destination": {"kind", "outletId"}, "workflow": {"kind", "workflowId"},
                 "task": {"kind", "text", "allowed"}}
TASK_OFF = ("Starting a task from a notification is off for this trigger. Turn it on in the trigger's settings, "
            "then it runs and asks before acting.")


def name_of(value):
    text = " ".join(value.split()) if isinstance(value, str) else ""
    if not 1 <= len(text) <= NAME_MAX:
        raise ValueError(f"Give the trigger a name of 1 to {NAME_MAX} characters.")
    return text


def app_of(value):
    app = value.strip() if isinstance(value, str) else ""
    if not BUNDLE.fullmatch(app):
        raise ValueError("Choose the iPhone app this trigger watches.")
    if own_app(app) or app.lower().startswith("app.mobster"):
        raise ValueError("A trigger can't watch Mobster's own notifications, because the notification it sends "
                         "would start it again.")
    return app


def words_of(value):
    """The words, as a list of 0 to 10 short phrases. Text with commas or new lines splits into words."""
    if value is None:
        return []
    if isinstance(value, str):
        value = re.split(r"[,\n]", value)
    if not isinstance(value, list) or not all(isinstance(word, str) for word in value):
        raise ValueError("Words is a list of words or phrases to look for in the notification.")
    words = []
    for word in value:
        word = " ".join(word.split())
        if not word:
            continue
        if len(word) > WORD_MAX:
            raise ValueError(f"A word or phrase is at most {WORD_MAX} characters.")
        if word.casefold() not in [w.casefold() for w in words]:
            words.append(word)
    if len(words) > WORDS_MAX:
        raise ValueError(f"Use at most {WORDS_MAX} words or phrases.")
    return words


def source_of(value):
    if value is None:
        return "iphone"
    if value not in SOURCES:
        raise ValueError("Source is iphone, mac or any.")
    return value


def enabled_of(value):
    if value is None:
        return False
    if type(value) is not bool:
        raise ValueError("Enabled is true or false.")
    return value


def action_of(value):
    """The action, cleaned. It names its destination or workflow by id;
    whether that exists is the service's to check."""
    if not isinstance(value, dict) or value.get("kind") not in ACTIONS:
        raise ValueError("Choose what the trigger does: send to a destination, run a workflow or start a task.")
    kind = value["kind"]
    extra = set(value) - ACTION_FIELDS[kind]
    if extra:
        raise ValueError(f"A {kind} action doesn't take {', '.join(sorted(extra))}.")
    if kind == "destination":
        if not isinstance(value.get("outletId"), str) or not ID.fullmatch(value["outletId"]):
            raise ValueError("Choose a destination you saved in Connectors.")
        return {"kind": kind, "outletId": value["outletId"]}
    if kind == "workflow":
        if not isinstance(value.get("workflowId"), str) or not ID.fullmatch(value["workflowId"]):
            raise ValueError("Choose a saved workflow.")
        return {"kind": kind, "workflowId": value["workflowId"]}
    text = value.get("text")
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= TASK_MAX:
        raise ValueError(f"Write the task in 1 to {TASK_MAX} characters.")
    allowed = value.get("allowed", False)
    if type(allowed) is not bool:
        raise ValueError("Allowed is true or false.")
    return {"kind": kind, "text": text.strip(), "allowed": allowed}


def clean(body, current=None):
    """The fields to save from a create or update body."""
    if not isinstance(body, dict):
        raise ValueError("A trigger has a name, an app and an action.")
    extra = set(body) - FIELDS
    if extra:
        raise ValueError(f"A trigger doesn't take {', '.join(sorted(extra))}.")
    out = dict(current or {})
    if current is None or "name" in body:
        out["name"] = name_of(body.get("name"))
    if current is None or "app" in body:
        out["app"] = app_of(body.get("app"))
    if "appName" in body or current is None:
        name = body.get("appName")
        if name is not None and (not isinstance(name, str) or len(name.strip()) > 80):
            raise ValueError("The app's name is at most 80 characters.")
        out["appName"] = " ".join(name.split()) if isinstance(name, str) and name.strip() else None
    if current is None or "words" in body:
        out["words"] = words_of(body.get("words"))
    if current is None or "source" in body:
        out["source"] = source_of(body.get("source"))
    if current is None or "enabled" in body:
        out["enabled"] = enabled_of(body.get("enabled"))
    if current is None or "action" in body:
        action = action_of(body.get("action"))
        # "Start a task" is off until the person turns it on for this trigger. A change that leaves "allowed" out keeps
        # what was saved, unless the task's words changed: new words are off until they are turned on again.
        before = (current or {}).get("action") or {}
        if action["kind"] == "task" and "allowed" not in body["action"]:
            same = before.get("kind") == "task" and before.get("text") == action["text"]
            action["allowed"] = bool(before.get("allowed")) if same else False
        out["action"] = action
    return out


def word_pattern(words, regex=None):
    """The compiled search for a trigger"""
    if regex:
        return re.compile(regex, re.IGNORECASE | re.MULTILINE)
    if not words:
        return None
    alternatives = "|".join(re.escape(" ".join(word.split())).replace(r"\ ", r"\s+") for word in words)
    return re.compile(r"(?<!\w)(?:" + alternatives + r")(?!\w)", re.IGNORECASE)
