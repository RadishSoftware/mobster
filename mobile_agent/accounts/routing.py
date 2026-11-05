"""A task that names a profile (an account in the code): its run field, and where it runs."""

import hashlib
import json
from types import MappingProxyType

from .context import fill
from .store import AccountNotFound, AmbiguousAccount, display, resolve, store_for

SMART_ONLY = ("A task for a profile runs on Mobster's agent, which confirms the profile on screen before it posts. "
              "Choose Mobster, or leave the profile out.")


def validate(value, runtime):
    if isinstance(value, dict):
        value = value.get("id")
    try:
        account = resolve(store_for(runtime).all(), value)
    except (AccountNotFound, AmbiguousAccount) as error:
        raise ValueError(str(error)) from None
    return {"id": account["id"], "handle": account["handle"], "app": dict(account["app"]),
            "device": account.get("device"), "deviceName": account.get("deviceName")}


def capture(runtime, account):
    """Detach the selected writing context from the editable profile at admission."""
    stored = store_for(runtime).get(account["id"])
    if stored is None:
        raise ValueError("This profile was removed. Choose a saved profile again.")
    identity = ("id", "handle", "app", "device", "deviceName")
    if any(stored.get(key) != account.get(key) for key in identity):
        raise ValueError("This profile changed while the task was starting. Choose it again.")
    selected = {key: stored.get(key) for key in identity}
    selected.update(notes=stored.get("notes") or "", signature=stored.get("signature") or "",
                    links=stored.get("links") or [], variables=stored.get("variables") or {})
    fingerprint = digest(selected)
    frozen = MappingProxyType({**selected, "app": MappingProxyType(dict(selected["app"])),
                               "links": tuple(MappingProxyType(dict(link)) for link in selected["links"]),
                               "variables": MappingProxyType(dict(selected["variables"]))})
    return frozen, fingerprint


def digest(account):
    plain = {**account, "app": dict(account["app"]), "links": [dict(link) for link in account["links"]],
             "variables": dict(account["variables"])}
    return hashlib.sha256(json.dumps(plain, sort_keys=True, ensure_ascii=True,
                                     allow_nan=False).encode()).hexdigest()


def route(runtime, account, goal, device):
    """(goal, device) for a task as ``account``. ValueError when the task names another phone."""
    stored = account
    wanted = stored.get("device")
    if wanted and device is not None:
        fleet = getattr(runtime, "fleet", None)
        named = None
        if fleet is not None:
            try:
                named = fleet.find(device).id
            except LookupError:
                named = None
        if named != wanted:
            where = stored.get("deviceName") or "another phone"
            raise ValueError(f"{display(stored)} is on {where}. Leave the phone out and Mobster uses that one, or "
                             "change the profile's phone in Settings › Profiles.")
    if (wanted and device is None and getattr(runtime, "fleet", None) is not None
            and getattr(runtime, "pool", 1) is None):
        device = wanted
    return fill(goal, stored), device
