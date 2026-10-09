"""Wi-Fi's HTTP routes (seam S3) and the monitor service (SPEC §3.7, API).

| Route | Body | Answer |
|---|---|---|
| ``GET /api/wireless`` | | ``{wifiTransport, network, devices: [{id, udid, name, primary, enabled, transport, tunnel, reachable, lastSeenAt, problem}]}`` |
| ``POST /api/wireless`` | ``{wifiTransport}`` | the same; turns Wi-Fi for iPhones on or off (the hidden setting) |
| ``GET /api/wireless/network`` | | ``{network}``: checks now whether this network lets devices see each other (at most 3 s) |
| ``POST /api/devices/{device}/wifi`` | ``{enabled}`` | ``{device}``; turning it on needs the phone on USB and trusting this Mac (409) |
| ``POST /api/wireless/reconnect`` | ``{device}`` | 202: look again now, without waiting out the backoff |

``problem`` is null or ``{code, message, fix}``: one plain sentence and one fix (words.py). ``network`` is null (not
checked yet) or ``{state, problem}``: state "ok" | "blocked" | "no_permission" | "vpn" | "offline" | "unknown" from
network.peer_check, and the problem a guest, hotel or campus network is ("This network stops devices from seeing each
other." / "Use the cable or your phone's hotspot."). Every route needs the API
token, like every other; none grants anything an approval guards. ``POST /api/wireless/reset`` (Reset connection)
is v2 and not here.
"""

import os
import threading
import time
from urllib.parse import unquote

from .. import api_routes
from ..api_routes import Response, Route
from . import GATE, pair, store, transport, transport_on, words
from .monitor import Monitor, task_on

# id(runtime) -> (runtime, Monitor): only for runtimes that manage USB phones, made when Wi-Fi for iPhones is on.
_monitors = {}
_monitors_lock = threading.Lock()
# GET /api/wireless/network's last answer: (monotonic time, result). One check at a time; reused for NETWORK_FRESH s.
NETWORK_FRESH = 15.0
_network = {"at": None, "result": None}
_network_lock = threading.Lock()


def _managed(runtime):
    return getattr(runtime, "manager", None) is not None and getattr(runtime, "fleet", None) is not None


# -- the service ----------------------------------------------------------------------------------------------------

def start_service(runtime):
    """Runtime-wide: the transport monitor, for a runtime that manages USB phones, while Wi-Fi for iPhones is on.
    Off, nothing is made and no thread runs."""
    if _managed(runtime) and transport_on():
        monitor_for(runtime, create=True).start()


def close_service(runtime):
    with _monitors_lock:
        entry = _monitors.pop(id(runtime), None)
    if entry is not None and entry[0] is runtime:
        entry[1].stop()


def monitor_for(runtime, create=False):
    """The runtime's monitor; with ``create``, made when missing (a runtime that manages USB phones only)."""
    with _monitors_lock:
        entry = _monitors.get(id(runtime))
        if entry is not None and entry[0] is runtime:
            return entry[1]
        if not create or not _managed(runtime):
            return None
        monitor = Monitor(runtime)
        _monitors[id(runtime)] = (runtime, monitor)
        return monitor


# -- what a phone's Wi-Fi looks like ------------------------------------------------------------------------------

def _phones(runtime):
    fleet = getattr(runtime, "fleet", None)
    if fleet is None or getattr(runtime, "manager", None) is None:
        return []
    return fleet.usb_managers()


def _name(phone, manager):
    name = None
    try:
        name = manager.settings().get("device_name") if phone.primary else None
    except Exception:
        name = None
    return phone.name or (name if isinstance(name, str) and name else None)


def row(runtime, phone, udid, manager):
    """One phone's Wi-Fi, as GET /api/wireless lists it."""
    data_dir = runtime.fleet.data_dir
    settings = store.get(data_dir, udid)
    link = transport.link(udid) if transport_on() else None
    name = _name(phone, manager)
    running = manager.runner is not None and manager.runner.running
    now = (link.transport if link is not None else
           getattr(manager, "transport", None) if running else None)
    problem = link.problem if link is not None and settings["enabled"] else None
    return {"id": udid, "udid": udid, "name": name, "primary": bool(getattr(phone, "primary", False)),
            "enabled": settings["enabled"], "transport": now,
            "tunnel": link.tunnel if link is not None else "unknown",
            "reachable": bool(link is not None and link.reachable),
            "lastSeenAt": link.last_seen_at if link is not None else None,
            "problem": words.problem(problem, name) if problem in words.PROBLEMS else None}


def network_view(result):
    """``{state, problem}`` for a network.peer_check result (problem: null or {code, message, fix}); None for None."""
    if not isinstance(result, dict):
        return None
    code = words.NETWORK_PROBLEMS.get(result.get("state"))
    return {"state": result.get("state") or "unknown", "problem": words.problem(code) if code else None}


def overview(runtime):
    monitor = monitor_for(runtime) if transport_on() else None
    with _network_lock:
        checked = _network["result"]
    return {"wifiTransport": transport_on(),
            "network": network_view(monitor.network if monitor is not None and monitor.network else checked),
            "devices": [row(runtime, phone, udid, manager) for phone, udid, manager in _phones(runtime)]}


def _find(runtime, ident):
    """(phone, UDID, manager) for a USB phone set up here, or raises a Response-shaped APIError."""
    from ..api_errors import APIError
    from ..devices import AmbiguousDevice
    fleet = getattr(runtime, "fleet", None)
    if fleet is None or getattr(runtime, "manager", None) is None:
        raise APIError("This Mobster doesn't manage USB iPhones.", 409, "not_managed")
    try:
        phone = fleet.lookup(unquote(ident))
    except AmbiguousDevice as error:
        raise APIError(str(error), 400, "device_ambiguous") from None
    except LookupError as error:
        raise APIError(str(error), 404, "device_not_found") from None
    for candidate, udid, manager in fleet.usb_managers():
        if candidate is phone or (phone.udid and phone.udid.upper() == udid.upper()):
            return candidate, udid, manager
    problem = words.problem("not_set_up", phone.name)
    raise APIError(problem["message"], 409, "not_set_up", fix=problem["fix"])


def _refuse(code, name, status=409, **details):
    problem = words.problem(code, name)
    return Response.error(problem["message"], status, code, fix=problem["fix"], **details)


# -- handlers -------------------------------------------------------------------------------------------------------

def get_wireless(request):
    return Response.json(overview(request.runtime))


def check_network(request, *, check=None, clock=time.monotonic):
    """Whether this network lets devices see each other, checked now (network.peer_check: at most about 3 s), or
    the answer of the last NETWORK_FRESH seconds. Reads only."""
    from . import network
    with _network_lock:
        now = clock()
        if _network["at"] is None or now - _network["at"] >= NETWORK_FRESH or _network["result"] is None:
            try:
                _network["result"] = (check or network.peer_check)()
            except Exception:  # noqa: BLE001 -- a check that fails says nothing
                _network["result"] = {"state": "unknown", "interface": None, "peers": 0}
            _network["at"] = now
        result = _network["result"]
    return Response.json({"network": network_view(result)})


def set_transport(request):
    """Turn Wi-Fi for iPhones (the hidden ``wifiTransport`` setting) on or off; saved in the env file like the other
    settings."""
    body = request.body if isinstance(request.body, dict) else {}
    if set(body) != {"wifiTransport"} or type(body["wifiTransport"]) is not bool:
        raise ValueError("Send {\"wifiTransport\": true} or false")
    on = body["wifiTransport"]
    runtime = request.runtime
    setup = getattr(runtime, "setup", None)
    env_file = getattr(setup, "env_file", None) or getattr(runtime.config, "env_file", None)
    if env_file:
        from ..device_manager import update_env_file
        update_env_file(env_file, {GATE: "1" if on else None})
    if on:
        os.environ[GATE] = "1"
    else:
        os.environ.pop(GATE, None)
    monitor = monitor_for(runtime, create=on)
    if monitor is not None:
        if on:
            monitor.start()
            monitor.kick()
        else:
            monitor.stop()
            monitor.retire()
    return Response.json(overview(runtime))


def set_device_wifi(request):
    """Turn Wi-Fi on or off for one phone. On needs it on USB and trusting this Mac: mobster-wifi-pair sets its
    lockdown flag, or, without the tool, the answer says to tick Connect via network in Xcode."""
    body = request.body if isinstance(request.body, dict) else {}
    if set(body) != {"enabled"} or type(body["enabled"]) is not bool:
        raise ValueError("Send {\"enabled\": true} or false")
    runtime = request.runtime
    phone, udid, manager = _find(runtime, request.params["device"])
    name = _name(phone, manager)
    data_dir = runtime.fleet.data_dir
    listing = next((item for item in _listing(manager) if item.get("udid", "").upper() == udid.upper()), None)
    following = None
    if body["enabled"]:
        if listing is None:
            return _refuse("needs_cable", name)
        if not listing.get("trusted"):
            return _refuse("not_trusted", name)
        via = "mobster"
        try:
            pair.run(udid, "enable")
        except LookupError:
            via, following = "xcode", words.problem("needs_xcode", name)
        except pair.PairError as error:
            code = error.code if error.code in ("needs_cable", "not_trusted", "refused") else "refused"
            return _refuse(code, name)
        except RuntimeError:
            return Response.error("Devices are off in this Mobster (MOBSTER_NO_DEVICES).", 503, "devices_off")
        store.put(data_dir, udid, True, via)
    else:
        link = transport.link(udid)
        running_on_wifi = (link is not None and link.transport == "wifi") or getattr(manager, "transport", None) == "wifi"
        if running_on_wifi and task_on(runtime, phone, udid):
            return _refuse("busy", name)
        before = store.get(data_dir, udid)
        store.put(data_dir, udid, False)
        if before.get("via") == "mobster" and listing is not None and listing.get("trusted"):
            try:
                pair.run(udid, "disable")   # Mobster set the flag, so Mobster clears it
            except (LookupError, RuntimeError, pair.PairError):
                pass
    monitor = monitor_for(runtime)
    if monitor is not None:
        monitor.kick(udid)
    answer = {"device": row(runtime, phone, udid, manager)}
    if following is not None:
        answer["next"] = following
    return Response.json(answer)


def _listing(manager):
    try:
        return manager.devices()
    except Exception:
        return []


def reconnect(request):
    body = request.body if isinstance(request.body, dict) else {}
    if set(body) != {"device"} or not isinstance(body["device"], str):
        raise ValueError("Send {\"device\": \"<id, UDID or name>\"}")
    runtime = request.runtime
    phone, udid, manager = _find(runtime, body["device"])
    if not transport_on():
        return _refuse("off", _name(phone, manager))
    monitor = monitor_for(runtime)
    if monitor is not None:
        monitor.kick(udid)
    return Response.json({"device": udid, "accepted": True}, 202)


ROUTES = (
    Route("wireless", "GET", "/api/wireless", get_wireless, body="none"),
    Route("wireless", "GET", "/api/wireless/network", check_network, body="none"),
    Route("wireless", "POST", "/api/wireless", set_transport, max_bytes=1024),
    Route("wireless", "POST", "/api/devices/{device}/wifi", set_device_wifi, max_bytes=1024),
    Route("wireless", "POST", "/api/wireless/reconnect", reconnect, max_bytes=1024),
)


def register_routes():
    for route in ROUTES:
        api_routes.register(route)
