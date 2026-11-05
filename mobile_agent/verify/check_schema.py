"""The JSON Schema of a check file, built from the loader's names; never stricter than check_from_dict, the judge."""

from .. import links
from . import assertions, business, checks, conditions, dialogs, fixtures, netstubs, snapshots
from .a11y import NAME as A11Y
from .audit import AUDITS
from .device_audit import NAME as AX_DEVICE

DRAFT = "https://json-schema.org/draft/2020-12/schema"
NOT_BLANK = r"\S"
LIMITS = checks.LIMITS


def ref(name, description=None):
    """A reference to ``$defs/name``, with this place's own description."""
    return {"$ref": f"#/$defs/{name}", **({"description": description} if description else {})}


def nullable(schema):
    """``schema`` or null: the loader reads a null value as the key left out."""
    return {"anyOf": [schema, {"type": "null"}]}


def given(*keys):
    """At least one of ``keys`` holds text that isn't blank: the loader's "needs bundle or path"."""
    return {"anyOf": [{"required": [key], "properties": {key: {"type": "string", "pattern": NOT_BLANK}}}
                      for key in keys]}


def _assertion(kind):
    """One kind of assertion: exactly its kind key, the bounds it takes, and an optional name."""
    selector = {"$ref": "#/$defs/selector"}
    shapes = {
        "text": ({"text": ref("texts", "Holds when a shown element's label or value contains this text, ignoring "
                                       "case. A list makes one assertion per string.")}, ["text"]),
        "no_text": ({"no_text": ref("texts", "Holds when no shown element's label or value contains this text. A "
                                             "list makes one assertion per string.")}, ["no_text"]),
        "visible": ({"visible": {**selector, "description": "Holds when at least one shown element matches."}},
                    ["visible"]),
        "absent": ({"absent": {**selector, "description": "Holds when no shown element matches."}}, ["absent"]),
        "value": ({"value": {**selector, "description": "Holds when exactly one shown element matches and its value "
                                                        "equals equals."},
                   "equals": ref("expected")}, ["value", "equals"]),
        "count": ({"count": {**selector, "description": "Holds when the number of shown elements that match meets "
                                                        "the bound."},
                   "equals": ref("whole", "Exactly this many."),
                   "at_least": ref("whole", "This many or more."),
                   "at_most": ref("whole", "This many or fewer.")}, ["count"]),
    }
    properties, required = shapes[kind]
    out = {"type": "object", "required": required, "additionalProperties": False,
           "properties": {**properties, "name": nullable(ref("assertionName"))}}
    if kind == "count":
        out["oneOf"] = [{"required": [bound]} for bound in assertions.BOUNDS]
    return out


def _or_list(item, description):
    """A matrix condition: one value, or a list that makes the check run once per combination."""
    return {"anyOf": [item, {"type": "array", "minItems": 1, "items": item}],
            "description": description + " A list runs the check once for each value."}


def _assertions(description, item):
    return nullable({"type": "array", "maxItems": LIMITS["expect"], "items": ref(item), "description": description})


def _number(limit, description):
    return nullable({"type": "number", "exclusiveMinimum": 0, "maximum": limit, "description": description})


def _defs():
    audits = [*AUDITS, A11Y, AX_DEVICE]
    return {
        "matcher": {"type": "string", "minLength": 1, "maxLength": 2000,
                    "description": "Exact text, or a /regex/ (/pattern/i ignores case)."},
        "texts": {"anyOf": [ref("matcher"), {"type": "array", "minItems": 1, "items": ref("matcher")}]},
        "selector": {
            "type": "object",
            "description": "The elements to find: every field given must match.",
            "minProperties": 1,
            "additionalProperties": False,
            "properties": {
                "id": ref("matcher", "The accessibility identifier, exact or a /regex/."),
                "label": ref("matcher", "The accessibility label, exact or a /regex/."),
                "value": ref("matcher", "The accessibility value, exact or a /regex/. A switch's value is 1 or 0."),
                "role": {"description": "A role such as button, text or field, or any XCUIElementType name.",
                         "anyOf": [{"enum": [*assertions.FRIENDLY_ROLES, *sorted(assertions.ELEMENT_TYPES)]},
                                   {"type": "string", "pattern": NOT_BLANK}]},
                "enabled": {"type": "boolean", "description": "Whether the element is enabled."},
                "selected": {"type": "boolean", "description": "Whether the element has the Selected trait."},
            },
        },
        "assertionName": {"type": "string", "maxLength": assertions.NAME_MAX, "pattern": NOT_BLANK,
                          "description": f"A label for this assertion in reports, up to {assertions.NAME_MAX} "
                                         "characters."},
        "expected": {"description": "What the value must equal: text, a number, or true or false (1, on and yes "
                                    "are true).",
                     "anyOf": [{"type": "string", "maxLength": 2000}, {"type": "number"}, {"type": "boolean"}]},
        "whole": {"type": "integer", "minimum": 0},
        "snapshot": {
            "type": "object", "required": ["snapshot"], "additionalProperties": False,
            "description": "Compares the final screen to the baseline in .mobster/snapshots/, one per check.",
            "properties": {
                "snapshot": {"type": "string", "pattern": rf"^{snapshots.NAME.pattern}$",
                             "description": "The screen's name, which names the baseline file."},
                "mask": {"type": "array", "maxItems": snapshots.MASK_LIMIT,
                         "description": "Regions the diff ignores: elements by id or label, or a rect in points.",
                         "items": {"type": "object", "minProperties": 1,
                                   "properties": {"id": {"type": "string"}, "label": {"type": "string"},
                                                  "rect": {"type": "array", "minItems": 4, "maxItems": 4,
                                                           "items": {"type": "number"}}}}},
                "threshold": {"type": "number", "minimum": 0, "maximum": 100,
                              "description": f"The percent of the screen that may change; default "
                                             f"{snapshots.DEFAULT_THRESHOLD:g}."},
            },
        },
        "businessScalar": {"anyOf": [{"type": "string", "maxLength": 256}, {"type": "number"},
                                     {"type": "boolean"}, {"type": "null"}]},
        "businessExpectation": {"type": "object", "required": ["field", "equals"],
                                "additionalProperties": False,
                                "properties": {"field": {"type": "string", "pattern": "^" +
                                                         business._FIELD.pattern.replace("\\Z", "$")},
                                               "equals": ref("businessScalar")}},
        "assertion": {"description": "An assertion: exactly one of text, no_text, visible, absent, value and count.",
                      "anyOf": [ref(f"assertion_{kind}") for kind in assertions.KINDS]},
        "expectation": {"description": "An assertion, the check's one snapshot, or {sound: name}: a sound the iPhone "
                                       "must play (experimental, MOBSTER_EXPERIMENTAL_SOUND).",
                        "anyOf": [ref("assertion"), ref("snapshot"),
                                  {"type": "object", "required": ["sound"], "additionalProperties": False,
                                   "properties": {"sound": {"type": "string",
                                                            "pattern": "^[a-z0-9][a-z0-9_-]{0,39}$"}}}]},
        "everyScreenAssertion": {"description": "An assertion that can hold on every screen: no_text or absent.",
                                 "anyOf": [ref(f"assertion_{kind}") for kind in checks.THROUGHOUT_KINDS]},
        **{f"assertion_{kind}": _assertion(kind) for kind in assertions.KINDS},
        "steps": nullable({
            "description": f"The flow in plain English, up to {LIMITS['steps']} steps of up to {LIMITS['step']} "
                           "characters. None means a launch-only check.",
            "anyOf": [{"type": "string", "pattern": NOT_BLANK},
                      {"type": "array", "maxItems": LIMITS["steps"],
                       "items": {"type": "string", "pattern": NOT_BLANK}}],
        }),
        "checkName": nullable({"type": "string", "description": f"The check's name in reports, up to "
                                                                f"{LIMITS['name']} characters."}),
        "device": nullable({"type": "string", "description": "The simulator device type, such as iPhone 17 Pro, up "
                                                             "to 100 characters."}),
        "runtime": nullable({"type": "string", "description": "The iOS runtime, such as iOS 26.4. Default: the newest "
                                                              "installed."}),
        "reset": nullable({"enum": list(checks.RESETS),
                           "description": "How much to reset before the run: none, data or reinstall. Default "
                                          "reinstall with app.path, else data."}),
        "bundle": nullable({"type": "string",
                            "pattern": r"^\s*$|^\s*[A-Za-z0-9_][A-Za-z0-9_.-]*\.[A-Za-z0-9_.-]*[A-Za-z0-9_]\s*$",
                            "description": "The app's bundle ID, such as com.example.yourapp."}),
        "appPath": nullable({"type": "string", "description": "The .app built for the iOS Simulator, relative to the "
                                                              "project root. Mobster installs it before the run."}),
        "args": nullable({"type": "array", "maxItems": LIMITS["args"], "items": {"type": "string", "maxLength": 1000},
                          "description": f"Launch arguments, up to {LIMITS['args']}. Quote a value YAML would read as "
                                         "something else, such as \"YES\"."}),
        "env": nullable({"type": "object", "maxProperties": LIMITS["env"],
                         "propertyNames": {"pattern": rf"^{checks.ENV_KEY.pattern}$"},
                         "additionalProperties": {"type": "string", "maxLength": 1000},
                         "description": f"Launch environment variables, up to {LIMITS['env']}. Each reaches the app "
                                        "as KEY."}),
        "url": nullable({"type": "string", "pattern": rf"^\s*$|^\s*{checks.URL_SCHEME.pattern}",
                         "description": f"A deep link opened after launch, such as yourapp://lesson, up to "
                                        f"{LIMITS['url']:,} characters."}),
        "tag": {"type": "string", "pattern": rf"^\s*{checks.TAG.pattern}\s*$"},
        "audit": {"enum": audits},
        "point": {
            "description": "A location as latitude and longitude, or comma-separated coordinates.",
            "anyOf": [{"type": "object", "required": ["latitude", "longitude"], "additionalProperties": False,
                       "properties": {"latitude": {"type": "number", "minimum": -90, "maximum": 90},
                                      "longitude": {"type": "number", "minimum": -180, "maximum": 180}}},
                      {"type": "string", "pattern": r"^[^,]*,[^,]*$"}],
        },
        "dialogs": nullable({
            "type": "object",
            "description": "How the run answers iOS's own dialogs, with no model. On a simulator every key has a "
                           "default; on a real iPhone only the keys you name apply.",
            "minProperties": 1,
            "additionalProperties": False,
            "properties": {
                "permissions": {"enum": list(dialogs.CHOICES["permissions"]),
                                "description": "Permission alerts (location, photos, notifications, tracking). "
                                               "Default allow on a simulator."},
                "paste": {"enum": list(dialogs.CHOICES["paste"]),
                          "description": "Allow Paste. Default allow on a simulator."},
                "sign_in_with_apple": {"enum": list(dialogs.CHOICES["sign_in_with_apple"]),
                                       "description": "The Sign in with Apple sheet. Default continue on a "
                                                      "simulator."},
                "biometric": {"enum": list(dialogs.CHOICES["biometric"]),
                              "description": "A Face ID or Touch ID prompt: match or nonmatch on a simulator, or "
                                             "cancel. Default match on a simulator."},
                "purchase": {"enum": list(dialogs.CHOICES["purchase"]),
                             "description": "The purchase sheet. Default confirm on a simulator. Never confirmed on "
                                            "a real iPhone."},
            },
        }),
        "conditions": nullable({
            "type": "object",
            "description": "The device's state while the check runs. On a real iPhone only appearance and locale are "
                           "set.",
            "minProperties": 1,
            "additionalProperties": False,
            "properties": {
                "appearance": _or_list({"enum": list(conditions.APPEARANCES)}, "Light or dark mode."),
                "content_size": _or_list({"enum": list(conditions.CONTENT_SIZES)}, "The Dynamic Type size."),
                "locale": _or_list({"type": "string", "pattern": rf"^{conditions.LOCALE.pattern}$"},
                                   "The app's language and region, such as de_DE or fr-CA."),
                "increase_contrast": {"enum": list(conditions.CONTRASTS),
                                      "description": "Increase Contrast on or off."},
                "location": ref("point", "The simulated location."),
                "route": {
                    "type": "object", "required": ["waypoints"], "additionalProperties": False,
                    "description": "Move along the waypoints at the given speed in metres per second.",
                    "properties": {
                        "waypoints": {"type": "array", "minItems": conditions.ROUTE_POINTS[0],
                                      "maxItems": conditions.ROUTE_POINTS[1], "items": ref("point")},
                        "speed": {"type": "number", "exclusiveMinimum": 0, "maximum": 1000},
                    },
                },
                "permissions": {"type": "object", "minProperties": 1,
                                "propertyNames": {"enum": list(conditions.SERVICES)},
                                "additionalProperties": {"enum": list(conditions.ACTIONS)},
                                "description": "Privacy permissions to set before launch, such as {photos: grant}."},
                "media": {"type": "array", "minItems": 1, "maxItems": conditions.MEDIA_LIMIT,
                          "items": {"type": "string", "pattern": NOT_BLANK},
                          "description": f"Photos, videos and contacts to add, up to {conditions.MEDIA_LIMIT} files "
                                         f"relative to the project: {', '.join(conditions.MEDIA_SUFFIXES)}."},
                "biometric": {"enum": list(conditions.BIOMETRICS),
                              "description": "Face ID enrolled or not on the simulator. Mobster puts it back when the "
                                             "run ends."},
                "push": {"type": "string", "pattern": NOT_BLANK,
                         "description": "A push payload file (.json or .apns, with an aps object), relative to the "
                                        "project, sent after launch."},
                "network": {"enum": list(conditions.NETWORKS),
                            "description": "The simulator's connection: offline, slow-3g, lte or flaky. Simulator "
                                           "only; it reaches URLSession traffic."},
                "status_bar": {
                    "type": "object", "minProperties": 1, "additionalProperties": False,
                    "description": "The status bar's overrides, for screenshots.",
                    "properties": {
                        "time": {"type": "string", "pattern": NOT_BLANK,
                                 "description": "Such as \"9:41\", up to 40 characters."},
                        "operator_name": {"type": "string", "pattern": NOT_BLANK,
                                          "description": "Up to 40 characters."},
                        "battery_level": {"type": "integer", "minimum": 0, "maximum": 100},
                        "battery_state": {"enum": list(conditions.BATTERY_STATES)},
                        "wifi_mode": {"enum": list(conditions.WIFI_MODES)},
                        "cellular_mode": {"enum": list(conditions.CELLULAR_MODES)},
                        "wifi_bars": {"type": "integer", "minimum": 0, "maximum": 3},
                        "cellular_bars": {"type": "integer", "minimum": 0, "maximum": 4},
                        "data_network": {"enum": list(conditions.DATA_NETWORKS)},
                    },
                },
            },
        }),
    }


def file_shape():
    """The check file as docs/checks.mdx writes it: app, launch and budget nested."""
    return {
        "type": "object",
        "required": ["app"],
        "additionalProperties": False,
        "properties": {
            "version": {"const": 1, "description": "The file format's version: 1, the only one."},
            "name": ref("checkName"),
            "app": {"type": "object", "additionalProperties": False, **given("bundle", "path"),
                    "description": "The app to check: its bundle ID, its .app, or both.",
                    "properties": {"bundle": ref("bundle"), "path": ref("appPath")}},
            "device": ref("device"),
            "runtime": ref("runtime"),
            "reset": ref("reset"),
            "tags": nullable({"anyOf": [ref("tag"), {"type": "array", "maxItems": LIMITS["tags"],
                                                     "items": ref("tag")}],
                              "description": f"Up to {LIMITS['tags']} words (a-z, 0-9, - and _) that mobster test "
                                             "--tag picks checks by."}),
            "launch": nullable({"type": "object", "additionalProperties": False,
                                "description": "How the app launches: arguments, environment and a deep link.",
                                "properties": {"args": ref("args"), "env": ref("env"), "url": ref("url")}}),
            "steps": ref("steps"),
            "expect": _assertions("The declared expectations, including accessibility-tree assertions and at most one "
                                  "snapshot comparison.", "expectation"),
            "throughout": _assertions("Assertions that must hold on every screen the run reads: no_text and absent.",
                                      "everyScreenAssertion"),
            "somewhere": _assertions("Assertions that must hold on at least one screen the run reads.", "assertion"),
            "audit": nullable({"description": "Audits by name: usage-descriptions and privacy-manifest read the built "
                                              "app, a11y the accessibility trees the run reads, ax-device Apple's own "
                                              "audit on a real iPhone.",
                               "anyOf": [ref("audit"), {"type": "array", "items": ref("audit")}]}),
            "a11y": nullable({"description": "The accessibility audit: warn reports findings and passes, strict fails "
                                             "the check. true is warn.",
                              "anyOf": [{"enum": ["warn", "strict"]}, {"type": "boolean"}]}),
            "fixture": nullable({
                "description": "A fixture name or a list of up to ten names from .mobster/fixtures.",
                "anyOf": [{"type": "string", "pattern": rf"^{fixtures.NAME.pattern}$"},
                          {"type": "array", "minItems": 1, "maxItems": 10,
                           "items": {"type": "string", "pattern": rf"^{fixtures.NAME.pattern}$"}}],
            }),
            "business": nullable({"type": "object", "required": ["adapter", "fixture", "expect"],
                         "additionalProperties": False,
                         "description": "A locally registered simulator app-state fixture and comparisons. "
                                        "The app must export its persisted state after launch.",
                         "properties": {
                             "adapter": {"type": "string", "pattern": "^" + business._ID.pattern.replace("\\Z", "$")},
                             "fixture": {"type": "string", "pattern": "^" + business._ID.pattern.replace("\\Z", "$")},
                             "expect": {"type": "array", "maxItems": 20,
                                        "items": ref("businessExpectation")}}}),
            "conditions": ref("conditions"),
            "dialogs": ref("dialogs"),
            "stubs": nullable({
                "type": "array", "minItems": 1, "maxItems": netstubs.STUB_LIMIT,
                "description": "Fixed answers to named requests, served by the run's proxy. Simulator only.",
                "items": {
                    "type": "object", "required": ["match", "respond"], "additionalProperties": False,
                    "properties": {
                        "match": {"type": "object", "required": ["host"], "additionalProperties": False,
                                  "properties": {
                                      "host": {"type": "string", "pattern": NOT_BLANK,
                                               "description": "A host such as api.example.com or *.example.com."},
                                      "path_prefix": {"type": "string", "pattern": "^/",
                                                      "description": ("Requests whose path starts with this. "
                                                                      "Default /.")},
                                      "method": {"enum": list(netstubs.METHODS)}}},
                        "respond": {"type": "object", "additionalProperties": False,
                                    "properties": {
                                        "status": {"type": "integer", "minimum": 100, "maximum": 599},
                                        "json": {"description": "The JSON body."},
                                        "file": {"type": "string", "pattern": NOT_BLANK,
                                                 "description": "A body file, relative to the project."},
                                        "delay_ms": {"type": "integer", "minimum": 0,
                                                     "maximum": netstubs.DELAY_LIMIT}}}}},
            }),
            "budget": nullable({"type": "object", "additionalProperties": False,
                                "description": ("Smart's limits for the flow, and the launch time, memory and hangs "
                                                "the app must stay inside."),
                                "properties": {
                                    "max_seconds": _number(LIMITS["max_seconds"], "Smart's time limit. Default "
                                                           f"{checks.DEFAULT_MAX_SECONDS}, at most "
                                                           f"{LIMITS['max_seconds']}."),
                                    "max_usd": _number(LIMITS["max_usd"], "Smart's spend cap. Default "
                                                       f"{checks.DEFAULT_MAX_USD:.2f}, at most "
                                                       f"{LIMITS['max_usd']:.2f}."),
                                    "launch_ms": _number(600_000, "Fail the check when the app takes longer than this "
                                                         "to reach its first stable screen after a cold launch."),
                                    "memory_mb": _number(65_536, "Fail the check when the app's resident memory peaks "
                                                         "above this many megabytes."),
                                    "hangs": nullable({"type": "integer", "minimum": 0, "maximum": 1000,
                                                       "description": "Fail the check when the app logs more hangs of "
                                                       "250 ms or longer than this."})}}),
            "schedule": nullable({"type": "string", "description": "Run this check on an interval or five-field cron "
                                  "while Mobster is running, up to 128 characters."}),
        },
    }


def tool_shape():
    """The MCP tools' flat arguments (checks.TOOL_KEYS), which a check file may also hold."""
    return {
        "type": "object",
        "additionalProperties": False,
        **given("bundle_id", "app_path"),
        "properties": {
            "name": ref("checkName"),
            "app_path": ref("appPath"),
            "bundle_id": ref("bundle"),
            "device": ref("device"),
            "runtime": ref("runtime"),
            "reset": ref("reset"),
            "launch_args": ref("args"),
            "launch_env": ref("env"),
            "open_url": ref("url"),
            "steps": ref("steps"),
            "expect": _assertions("Accessibility-tree assertions checked at the end of the run.", "assertion"),
            "max_usd": _number(LIMITS["max_usd"], "Smart's spend cap."),
            "max_seconds": _number(LIMITS["max_seconds"], "Smart's time limit."),
            **{key: {"description": "Read by the MCP tool, not part of the check."} for key in checks.TOOL_ONLY},
        },
    }


def schema():
    """The check file's JSON Schema, as a dict."""
    return {
        "$schema": DRAFT,
        "$id": links.CHECK_SCHEMA,
        "title": "Mobster check",
        "description": "A Mobster check: a flow in plain English and declared expectations, saved in .mobster/checks. "
                       "See https://docs.mobster.dev/checks.",
        "type": "object",
        "if": {"anyOf": [{"required": [key]} for key in checks.TOOL_SHAPE_KEYS]},
        "then": tool_shape(),
        "else": file_shape(),
        "$defs": _defs(),
    }
