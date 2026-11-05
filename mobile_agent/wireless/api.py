"""Wi-Fi's HTTP routes (seam S3) and the monitor service (SPEC §3.7, API)."""

import os
import threading
import time
from urllib.parse import unquote

from .. import api_routes
from ..api_routes import Response, Route
from . import GATE, result, store, switch, transport, transport_on, words
from .monitor import Monitor, task_on

# id(runtime) -> (runtime, Monitor): only for runtimes that manage USB phones, made when Wi-Fi for iPhones is on.
_monitors = {}
_monitors_lock = threading.Lock()
# GET /api/wireless/network's last answer: (monotonic time, result). One check at a time; reused for NETWORK_FRESH s.
NETWORK_FRESH = 15.0
_network = {"at": None, "result": None}
_network_lock = threading.Lock()
# The Wi-Fi checks Add a device starts (POST /api/wireless/probe): UDID -> the running or last one. A check still
# running after PROBE_SECONDS reads as timed out, and its late end changes nothing.
PROBE_SECONDS = 180.0
_probes = {}
_probes_lock = threading.Lock()


def _managed(runtime):
    return getattr(runtime, "manager", None) is not None and getattr(runtime, "fleet", None) is not None


# -- the service ----------------------------------------------------------------------------------------------------

def start_service(runtime):
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
    return runtime.fleet.usb_managers() if _managed(runtime) else []


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
            "probe": _probe_view(udid, result.get(data_dir, udid)),
            "lastCheck": _check_view(udid),
            "problem": words.problem(problem, name) if problem in words.PROBLEMS else None}


def _probe_view(udid, kept):
    """The kept probe, or while a check runs: ``{state: "running", step, at}`` (the step it is on)."""
    check = _current(udid)
    if check is not None and check["state"] == "running":
        return {"state": "running", "at": kept["at"], "reason": kept["reason"], "step": check["step"]}
    return kept


def _check_view(udid):
    """The check Add a device started on this phone and how it ended, or None."""
    check = _current(udid)
    return None if check is None else {key: check[key] for key in ("state", "step", "startedAt", "reason", "next")}


def _current(udid, clock=time.monotonic):
    with _probes_lock:
        check = _probes.get(str(udid).upper())
        if check is not None and check["state"] == "running" and clock() - check["began"] >= PROBE_SECONDS:
            check.update(state="timeout", reason=None, next=words.problem("probe_timeout", check["name"]))
        return check


# The step that failed -> what to tell the person (words.PROBLEMS).
STEP_PROBLEMS = {"coredevice": "not_seen", "tunnel": "tunnel_down", "wda": "runner", "lan": "not_seen"}


def _next_step(probe, name):
    """The problem the check found, with its one fix: from the step that failed (else warned), else None."""
    usb = next((item for item in probe.steps if item["key"] == "usb"), None)
    if usb is not None and usb["detail"].startswith("plugged in"):
        return words.problem("plugged_in", name)
    for state in ("fail", "warn"):
        step = next((item for item in probe.steps if item["state"] == state), None)
        if step is not None:
            code = STEP_PROBLEMS.get(step["key"])
            return words.problem(code, name) if code else None
    return None


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
    data_dir = getattr(getattr(runtime, "fleet", None), "data_dir", None)
    return {"wifiTransport": transport_on(),
            "probe": result.latest(data_dir) if data_dir else dict(result.NOT_RUN),
            "network": network_view(monitor.network if monitor is not None and monitor.network else checked),
            "devices": [row(runtime, phone, udid, manager) for phone, udid, manager in _phones(runtime)]}


def _find(runtime, ident):
    """(phone, UDID, manager) for a USB phone set up here, or raises a Response-shaped APIError."""
    from ..api_errors import APIError
    from ..devices import AmbiguousDevice
    if not _managed(runtime):
        raise APIError("This Mobster doesn't manage USB iPhones.", 409, "not_managed")
    fleet = runtime.fleet
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
    body = request.body if isinstance(request.body, dict) else {}
    if set(body) != {"wifiTransport"} or type(body["wifiTransport"]) is not bool:
        raise ValueError("Send {\"wifiTransport\": true} or false")
    on = body["wifiTransport"]
    runtime = request.runtime
    setup = getattr(runtime, "setup", None)
    env_file = getattr(setup, "env_file", None) or getattr(runtime.config, "env_file", None)
    if env_file:
        from ..env_secrets import update_env_file
        update_env_file(env_file, {GATE: "1" if on else "0"})
    os.environ[GATE] = "1" if on else "0"
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
    body = request.body if isinstance(request.body, dict) else {}
    if set(body) != {"enabled"} or type(body["enabled"]) is not bool:
        raise ValueError("Send {\"enabled\": true} or false")
    runtime = request.runtime
    phone, udid, manager = _find(runtime, request.params["device"])
    name = _name(phone, manager)
    data_dir = runtime.fleet.data_dir
    listing = next((item for item in _listing(manager) if item.get("udid", "").upper() == udid.upper()), None)
    following = None
    if not body["enabled"]:
        link = transport.link(udid)
        running_on_wifi = ((link is not None and link.transport == "wifi")
                           or getattr(manager, "transport", None) == "wifi")
        if running_on_wifi and task_on(runtime, phone, udid):
            return _refuse("busy", name)
    try:
        if body["enabled"]:
            if switch.turn_on(data_dir, udid, listing) == "xcode":
                following = words.problem("needs_xcode", name)
        elif left_on := switch.turn_off(data_dir, udid, listing):
            following = words.problem(left_on, name)   # off in Mobster; the phone's own switch is still on
    except switch.SwitchRefused as error:
        return _refuse(error.code, name)
    except RuntimeError:
        return Response.error("Devices are off in this Mobster (MOBSTER_NO_DEVICES).", 503, "devices_off")
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


def _run_probe(runtime, check, probe, manager_udid):
    from ..mac_tools import usb_attached
    try:
        probe.run(usb_attached)
    except Exception as error:  # noqa: BLE001 -- the check ends with its words, never a trace
        probe.step("probe", "fail", "Wi-Fi check", f"stopped: {type(error).__name__}")
    finally:
        try:
            probe.stop_launched()
        except Exception:  # noqa: BLE001
            pass
    with _probes_lock:
        if check["state"] != "running":
            return  # it timed out: a late end changes nothing
        usb = next((item for item in probe.steps if item["key"] == "usb"), None)
        if usb is not None and usb["detail"].startswith("plugged in"):
            state = "plugged_in"
        else:
            state = "passed" if probe.ok else "failed"
            result.record(runtime.fleet.data_dir, manager_udid, probe.public())
        check.update(state=state, reason=None if probe.ok else result.reason_of(probe.steps),
                     next=None if probe.ok else _next_step(probe, check["name"]))


def probe_wifi(request):
    """Check one phone's Wi-Fi in the background, the way ``mobster wifi probe`` does (wireless/probe.py)."""
    body = request.body if isinstance(request.body, dict) else {}
    if set(body) - {"device", "start"} or not isinstance(body.get("device", ""), str) \
            or type(body.get("start", False)) is not bool:
        raise ValueError("Send {\"device\": \"<id, UDID or name>\"} (optional) and {\"start\": true} (optional)")
    runtime = request.runtime
    from .. import native_helpers
    if not native_helpers.devices_allowed():
        return Response.error("Devices are off in this Mobster (MOBSTER_NO_DEVICES).", 503, "devices_off")
    if body.get("device"):
        phone, udid, manager = _find(runtime, body["device"])
    else:
        phones = _phones(runtime)
        if len(phones) != 1:
            from ..api_errors import APIError
            raise APIError("Name the iPhone to check." if phones else "Mobster sees no iPhone set up here.",
                           400 if phones else 409, "device_ambiguous" if phones else "no_device")
        phone, udid, manager = phones[0]
    name = _name(phone, manager)
    if task_on(runtime, phone, udid):
        return _refuse("busy", name)
    from .. import devices
    from .probe import Probe
    key = udid.upper()
    with _probes_lock:
        others = list(_probes)
    if any(check["state"] == "running" for check in map(_current, others) if check):
        return _refuse("probe_running", name)
    check = {"state": "running", "step": None, "startedAt": int(time.time() * 1000), "began": time.monotonic(),
             "reason": None, "next": None, "name": name or "your iPhone", "udid": udid}
    probe = Probe(
        udid=udid, name=name or "your iPhone", slot_port=devices.port_of(getattr(phone, "wda_url", None)),
        data_dir=runtime.fleet.data_dir, start=bool(body.get("start")), manager=manager,
        out=lambda step: check.update(step=step["title"]))
    with _probes_lock:
        _probes[key] = check
    threading.Thread(target=_run_probe, args=(runtime, check, probe, udid), name="mobster-wifi-probe",
                     daemon=True).start()
    return Response.json({"device": udid, "accepted": True, "check": _check_view(udid)}, 202)


# Every route needs the API token, like every other, and none grants anything an approval guards.
ROUTES = (
    Route("wireless", "GET", "/api/wireless", get_wireless, body="none"),
    Route("wireless", "GET", "/api/wireless/network", check_network, body="none"),
    Route("wireless", "POST", "/api/wireless", set_transport, max_bytes=1024),
    Route("wireless", "POST", "/api/devices/{device}/wifi", set_device_wifi, max_bytes=1024),
    Route("wireless", "POST", "/api/wireless/reconnect", reconnect, max_bytes=1024),
    Route("wireless", "POST", "/api/wireless/probe", probe_wifi, max_bytes=1024),
)


def register_routes():
    for route in ROUTES:
        api_routes.register(route)
