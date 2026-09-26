"""Dataflow plans: a multi-app request compiled once into steps that pass typed values.

"Find this iPhone's Model Name in Settings > General > About, then in Safari open
the Wikipedia article about that model and report the year it was released."
Measured on MobsterBench (24 Sep): the step agent read the model name, launched
Safari and stopped (BLOCKED): nothing turned "that model" into text it could type.

A plan is one helper call:

    steps:  [{app, request, finds: {name: description}}, ...]   2-4 steps, one app each
    answer: {field: "{name}"}                                   every answer field is one found value

Each step runs as an ordinary run of the step agent in its own app, with its
``finds`` as the output schema, so every value is a cited, verified literal
before any later step sees it. Later requests get earlier values substituted
verbatim. The user's own prohibitions are copied into every step by code, not
by the helper, and a step may only use apps the request already allowed.
"""

import json
import re

PLAN_MAX_STEPS = 4
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
_SLOT = re.compile(r"\{([a-z][a-z0-9_]{0,40})(?::(major|number|year))?\}")
# Deterministic reshaping of a verified literal for a later step (never for the answer):
# "26.0.1" -> major "26"; "Released September 2023" -> year "2023"; "$1,234.50" -> number "1234.50".
TRANSFORMS = {
    "major": lambda text: (re.search(r"\d+", text) or [None])[0],
    "number": lambda text: (lambda m: m.group(0).replace(",", "") if m else None)(re.search(r"-?\d[\d,]*(?:\.\d+)?", text)),
    "year": lambda text: (re.search(r"\b(1[5-9]\d\d|2[01]\d\d)\b", text) or [None])[0],
}
# The request's own prohibitions, carried into every step verbatim.
_PROHIBITION = re.compile(r"[^.!?]*\b(?:do not|don't|never|only look|without)\b[^.!?]*[.!?]", re.I)
# A cheap gate before the helper is asked: a value found in one place is used in another.
_DATAFLOW = re.compile(r"\b(then|that|this|those|it|its|the result|the product|the count|the number)\b", re.I)

PLAN_INSTRUCTIONS = (
    "Split one phone request into the fewest ordered steps, each done in ONE app, where a later step "
    "uses a value an earlier step finds. Return only JSON: {\"plan\": true, \"steps\": [{\"app\": "
    "\"<bundle id from apps>\", \"request\": \"<what to do in that app; self-contained; write {name} for a "
    "value found by an earlier step>\", \"finds\": {\"<snake_case name>\": \"<the value to report>\"}}], "
    "\"answer\": {\"<each field of answer_fields>\": \"{<name found by a step>}\"}}. Return {\"plan\": false} "
    "when one app suffices or no value passes between apps. Rules: only the listed apps; 2 to 4 steps; "
    "every {name} is found by an earlier step; each step finds 1 to 3 values, which it reports exactly as "
    "shown on screen; the last step finds the answer; keep the user's names, numbers and addresses "
    "verbatim; never add an action the user did not ask for; a calculation is its own step in the app "
    "the user named for it. A step finds a value exactly as the screen shows it (\"26.0.1\", not \"26\"); when a "
    "later step needs part of it, write {name:major} (leading number, 26.0.1 -> 26), {name:number} or {name:year}.")


class PlanError(ValueError):
    pass


def plan_candidate(goal, allowed_bundles, answer_fields):
    return (bool(answer_fields) and allowed_bundles is not None and len(allowed_bundles) >= 2
            and bool(_DATAFLOW.search(goal or "")))


def prohibitions(goal):
    return " ".join(match.group(0).strip() for match in _PROHIBITION.finditer(goal or ""))


def validate_plan(data, allowed_bundles, answer_fields):
    """The plan, normalized; PlanError for anything this module would not run as written."""
    if not isinstance(data, dict):
        raise PlanError("no plan")
    if data.get("plan") is not True:
        return None
    steps, answer = data.get("steps"), data.get("answer")
    if not isinstance(steps, list) or not 2 <= len(steps) <= PLAN_MAX_STEPS or not isinstance(answer, dict):
        raise PlanError("a plan has 2-4 steps and an answer map")
    found, normalized = set(), []
    for step in steps:
        if not isinstance(step, dict):
            raise PlanError("malformed step")
        app, request, finds = step.get("app"), step.get("request"), step.get("finds")
        if app not in allowed_bundles:
            raise PlanError("a step uses an app the request did not allow")
        if not isinstance(request, str) or not 8 <= len(request) <= 600:
            raise PlanError("a step needs a request")
        if not isinstance(finds, dict) or not 1 <= len(finds) <= 3 or not all(
                isinstance(name, str) and _NAME.match(name) and isinstance(text, str) and len(text) <= 200
                for name, text in finds.items()):
            raise PlanError("a step finds 1-3 named values")
        if any(slot not in found for slot, _transform in _SLOT.findall(request)):
            raise PlanError("a step uses a value no earlier step finds")
        found.update(finds)
        normalized.append({"app": app, "request": request.strip(), "finds": dict(finds)})
    mapping = {}
    for field in answer_fields:
        template = answer.get(field)
        slot = _SLOT.fullmatch(template.strip()) if isinstance(template, str) else None
        if slot is None or slot.group(1) not in found or slot.group(2):
            raise PlanError("every answer field must be exactly one found value")
        mapping[field] = slot.group(1)
    if not set(normalized[-1]["finds"]) & set(mapping.values()):
        raise PlanError("the last step must find the answer")
    return {"steps": normalized, "answer": mapping}


def step_request(step, values, goal):
    """A step's request with earlier values filled in and the user's prohibitions appended."""
    def fill(match):
        value = str(values[match.group(1)])
        if match.group(2):
            shaped = TRANSFORMS[match.group(2)](value)
            if not shaped:
                raise PlanError(f"{match.group(1)} has no {match.group(2)} part")
            return shaped
        return value
    request = _SLOT.sub(fill, step["request"]).strip()
    extra = prohibitions(goal)
    if not extra or extra in request:
        return request
    # A sentence break first: "find the Model Name Do not change any setting." made
    # "Model Name Do" the row the route looked for (MobsterBench, 24 Sep).
    return f"{request if request[-1:] in '.!?' else request + '.'} {extra}"


def step_schema(step):
    return {"type": "object", "properties": {name: {"type": "string"} for name in step["finds"]},
            "required": list(step["finds"]), "additionalProperties": False}


def compile_plan(helper, goal, apps, answer_fields, *, timeout=20):
    """A validated plan, None when the request is not one, or PlanError."""
    from .models import AUTHORITY_RULES
    from .transport import decode_json
    payload = {"request": goal, "apps": apps, "answer_fields": list(answer_fields)}
    result = helper.complete(
        [{"role": "system", "content": PLAN_INSTRUCTIONS + " " + AUTHORITY_RULES},
         {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], 900, timeout, "plan_compile")
    try:
        data = decode_json(result["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError, ValueError):
        raise PlanError("The helper returned no usable plan") from None
    return validate_plan(data, {app["bundle_id"] for app in apps}, answer_fields)
