"""The ``surfaces`` block of a check: which system surfaces to drive and what must show on each."""

import difflib
import os
from dataclasses import dataclass
from typing import Optional

from ..assertions import CheckError, expand_assertion, parse_selector, reject_unknown_keys

FLAG = "MOBSTER_EXPERIMENTAL_SYSTEM_SURFACES"
LIMIT = 10
WHERE = ("lock_screen", "dynamic_island")
# App Clip invocations that need a phone, a tag or a place; only the URL has a simulator path.
VIA_UNSUPPORTED = ("nfc", "qr", "banner", "maps")
VIA = ("url", *VIA_UNSUPPORTED)

COMMON = ("surface", "name", "expect")
# kind -> (keys it takes besides COMMON, keys it needs)
KINDS = {
    "intent": (("shortcut", "siri", "input"), ()),
    "widget": (("add",), ()),
    "live_activity": (("where", "expanded"), ()),
    "control_center": (("pick", "then"), ()),
    "app_clip": (("bundle", "url", "via", "path"), ("bundle", "url")),
    "spotlight": (("query", "pick", "then"), ("query",)),
    "share_sheet": (("open", "pick", "then"), ("open",)),
}


def enabled():
    """Whether system surfaces are switched on (``MOBSTER_EXPERIMENTAL_SYSTEM_SURFACES=1``)."""
    return os.environ.get(FLAG, "").strip().casefold() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Surface:
    kind: str
    name: str
    params: dict                                # the validated keys as written, minus expect and then
    expect: tuple = ()                          # of assertions: what shows once the surface is up
    then: tuple = ()                            # of assertions: what shows after `pick`
    pick: Optional[object] = None               # assertions.Selector: the element to tap
    open: Optional[object] = None               # assertions.Selector: the in-app control that presents the surface

    def label(self):
        return self.name or self.kind.replace("_", " ")


def _text(value, where):
    if isinstance(value, bool) or not isinstance(value, str) or not value.strip():
        raise CheckError(f"{where} must be text")
    if len(value) > 500:
        raise CheckError(f"{where} is longer than 500 characters")
    return value.strip()


def _selector(value, where):
    """A label as text, or a selector object."""
    return parse_selector({"label": value} if isinstance(value, str) else value, where)


def _assertions(value, where):
    if value is None:
        return ()
    if not isinstance(value, list):
        raise CheckError(f"{where} must be a list of assertions, such as [{{text: 12 day streak}}]")
    out = []
    for index, item in enumerate(value):
        out.extend(expand_assertion(item, f"{where}[{index}]"))
    return tuple(out)


def _one(index, item, base_dir):
    where = f"surfaces[{index}]"
    if not isinstance(item, dict):
        raise CheckError(f"{where} must be an object such as {{surface: widget, expect: [{{text: Streak}}]}}")
    kind = item.get("surface")
    if kind not in KINDS:
        close = difflib.get_close_matches(str(kind), list(KINDS), n=1)
        raise CheckError(f"{where}.surface {kind!r} isn't a system surface; "
                         + (f"did you mean {close[0]}? " if close else "") + f"Use {', '.join(KINDS)}.")
    keys, required = KINDS[kind]
    reject_unknown_keys(item, (*COMMON, *keys), where)
    for key in required:
        if item.get(key) is None:
            raise CheckError(f"{where}.{key} is required for a {kind.replace('_', ' ')}")
    params = {key: item[key] for key in keys if item.get(key) is not None}
    if kind == "intent" and ("shortcut" in params) == ("siri" in params):
        raise CheckError(f"{where} needs exactly one of shortcut (a shortcut's name) and siri (a phrase to say)")
    if "input" in params and "shortcut" not in params:
        raise CheckError(f"{where}.input goes with shortcut")
    if "where" in params and params["where"] not in WHERE:
        raise CheckError(f"{where}.where must be one of {', '.join(WHERE)}")
    if "expanded" in params and not isinstance(params["expanded"], bool):
        raise CheckError(f"{where}.expanded must be true or false")
    if "via" in params and params["via"] not in VIA:
        raise CheckError(f"{where}.via must be one of {', '.join(VIA)}")
    for key in ("shortcut", "siri", "input", "add", "query", "bundle", "url", "path"):
        if key in params:
            params[key] = _text(params[key], f"{where}.{key}")
    if "path" in params and base_dir is not None and not os.path.isabs(params["path"]):
        params["path"] = os.path.normpath(os.path.join(base_dir, params["path"]))
    pick = _selector(params["pick"], f"{where}.pick") if "pick" in params else None
    opens = _selector(params["open"], f"{where}.open") if "open" in params else None
    then = _assertions(item.get("then"), f"{where}.then")
    if then and pick is None:
        raise CheckError(f"{where}.then is read after pick, and there is no pick")
    expect = _assertions(item.get("expect"), f"{where}.expect")
    if not (expect or then):
        raise CheckError(f"{where} checks nothing. Add expect: what must show on the {kind.replace('_', ' ')}")
    name = item.get("name")
    return Surface(kind, _text(name, f"{where}.name") if name is not None else "", params,
                   expect, then, pick, opens)


def parse(value, base_dir=None):
    """The ``surfaces`` block as a tuple of Surface: () when absent. Raises CheckError; off unless the flag is on."""
    if value is None:
        return ()
    if not enabled():
        raise CheckError(f"surfaces is experimental and off. Set {FLAG}=1 to use it.")
    if not isinstance(value, list) or not value:
        raise CheckError("surfaces must list at least one surface, such as [{surface: widget, expect: [...]}]")
    if len(value) > LIMIT:
        raise CheckError(f"surfaces has more than {LIMIT} entries")
    return tuple(_one(index, item, base_dir) for index, item in enumerate(value))


def to_dict(surfaces):
    """The file shape of ``surfaces``, as ``parse`` reads it."""
    out = []
    for surface in surfaces:
        item = {"surface": surface.kind, **({"name": surface.name} if surface.name else {}), **surface.params}
        if surface.expect:
            item["expect"] = [assertion.to_dict() for assertion in surface.expect]
        if surface.then:
            item["then"] = [assertion.to_dict() for assertion in surface.then]
        out.append(item)
    return out
