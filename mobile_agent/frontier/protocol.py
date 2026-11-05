"""The model's protocol: the operations, the reply schema and the turn budget."""

import json
import re

from ..state import TEXT_OPERATIONS

OPERATIONS = ("TAP", "LONG_PRESS", "TYPE", "TYPE_SUBMIT", "SET_TEXT", "SUBMIT", "SWIPE_UP", "SWIPE_DOWN", "SWIPE_LEFT",
              "SWIPE_RIGHT", "DISMISS", "HOME", "LAUNCH_APP", "WAIT", "READ_LIST", "SCROLL_TO", "TAP_XY", "DRAG_XY",
              "HOLD_XY", "FLICK_XY", "DONE", "BLOCKED")
ELEMENT_OPS = {"TAP", "LONG_PRESS", "TYPE", "TYPE_SUBMIT", "SET_TEXT", "SUBMIT"}
TYPED_OPS = TEXT_OPERATIONS | {"SET_TEXT"}
HOUSEKEEPING_OPS = frozenset({"DISMISS", "HOME", "SWIPE_UP", "SWIPE_DOWN", "SWIPE_LEFT", "SWIPE_RIGHT"})
MAX_CHUNK = 6
WRAP_UP_SHARE = .8
FINAL_CALL_SECONDS = 45


DATA_JSON = {"type": ["string", "null"], "description": "only with DONE: the answer as JSON text matching the "
                                                       "requested schema; else null"}


def output_instruction(schema):
    return ("\n\nWhen you finish with DONE, also put the answer in data_json as JSON text that matches this JSON "
            "schema exactly (use null where a value was not shown):\n" + json.dumps(schema, ensure_ascii=False)[:4000])


ID = re.compile(r"e[0-9]+")


def normalize_reply(out):
    if not out or not any("op" in act for act in out.get("actions") or ()):
        return out
    actions = []
    for act in out.get("actions") or ():
        op, target = act.get("op"), act.get("target")
        full = {"operation": op, "target": None, "target_label": None, "text": act.get("text"), "app": None}
        if op == "LAUNCH_APP" or op == "OPEN_URL" and target and not ID.fullmatch(target):
            # OPEN_URL's target is the browser a web page opens in, when the model names one.
            full["app"] = target
        elif target and ID.fullmatch(target):
            full["target"] = target
        else:
            full["target_label"] = target
        actions.append(full)
    return {**out, "actions": actions}


STEP_FIELD = {"type": ["integer", "null"], "description": "the plan step you are working on"}


def _schema(apps, operations=OPERATIONS, data=False, step=False):
    # One schema all task long: it is in the cached prompt prefix, so it never enumerates the screen's ids.
    schema = _compact_schema(apps, operations)
    if data:
        schema = {**schema, "required": list(schema["required"]) + ["data_json"],
                  "properties": {**schema["properties"], "data_json": DATA_JSON}}
    if step:
        schema = {**schema, "required": list(schema["required"]) + ["step"],
                  "properties": {**schema["properties"], "step": STEP_FIELD}}
    return schema


def _compact_schema(apps, operations):
    action = {
        "type": "object", "additionalProperties": False, "required": ["op", "target", "text"],
        "properties": {
            "op": {"type": "string", "enum": list(operations)},
            "target": {"type": ["string", "null"], "description": "an element id on the CURRENT screen (e7); for a "
                       "later action the exact label it will have; for LAUNCH_APP the app's bundle id; for "
                       "TAP_XY x,y in percent of the screen"},
            "text": {"type": ["string", "null"]}}}
    return {
        "type": "object", "additionalProperties": False,
        "required": ["thought", "plan", "notes_add", "checklist_updates", "actions", "answer"],
        "properties": {
            "thought": {"type": "string", "description": "at most 12 words"},
            "plan": {"type": ["string", "null"],
                     "description": "the remaining steps, only when they changed; else null"},
            "notes_add": {"type": "array", "items": {"type": "string"},
                          "description": "facts not already in your notes; usually none"},
            "checklist_updates": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["id", "status", "value"],
                "properties": {"id": {"type": "integer"},
                               "status": {"type": "string", "enum": ["done", "found", "missing"]},
                               "value": {"type": ["string", "null"]}}}},
            "actions": {"type": "array", "items": action, "minItems": 1, "maxItems": MAX_CHUNK},
            "answer": {"type": ["string", "null"]}}}


SCROLL_OPS = frozenset({"SWIPE_UP", "SWIPE_DOWN"})
