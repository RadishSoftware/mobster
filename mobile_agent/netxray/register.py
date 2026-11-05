"""Network X-ray's routes, for the Mac app's session only: no agent or MCP tool, so no hostname reaches a model."""

from ..api_errors import APIError
from ..api_routes import Response, Route
from ..api_routes import register as add_route
from ..harness_api import STAY_ON_MAC
from . import job, target

APP_ONLY = "Only the Mobster app can record your iPhone's network traffic. Open Mobster and ask there."


def _app_only(request):
    # Fail closed: the capture is of everything the phone does, so only the Mac app's own session starts or reads it.
    if not request.app_session:
        raise APIError(APP_ONLY, 403, "app_only")


def _board(request):
    board = getattr(request.runtime, "netxray_board", None)
    if board is None:
        raise APIError("Network X-ray isn't running", 503, "xray_unavailable")
    return board


def parse(body):
    """(device, seconds, wifi, key) from a start request; ValueError says what's wrong."""
    body = body if isinstance(body, dict) else {}
    device, seconds, wifi, key = (body.get("device"), body.get("seconds", job.DEFAULT_SECONDS),
                                  body.get("wifi", False), body.get("key"))
    if not isinstance(device, str) or not 1 <= len(device) <= 128:
        raise ValueError("device is the phone's id, UDID or name")
    if isinstance(seconds, bool) or not isinstance(seconds, int) or not job.MIN_SECONDS <= seconds <= job.MAX_SECONDS:
        raise ValueError(f"seconds is {job.MIN_SECONDS} to {job.MAX_SECONDS}")
    if not isinstance(wifi, bool):
        raise ValueError("wifi is true or false")
    if key is not None and (not isinstance(key, str) or not 1 <= len(key) <= 100):
        raise ValueError("key is 1 to 100 characters")
    return device, seconds, wifi, key


def start(request):
    _app_only(request)
    device, seconds, wifi, key = parse(request.body)
    board = _board(request)
    try:
        phone = target.pick(device, wifi=wifi)
    except target.TargetError as error:
        raise APIError(str(error), 400, "xray_device") from None
    gate = getattr(request.runtime, "plan_gate", None)
    if gate is not None:
        gate.require()
    started = board.start(phone, seconds, key=f"{phone.udid}-{key}" if key else None)
    return Response.json({"jobId": started.id}, 202)


def status(request):
    _app_only(request)
    found = _board(request).get(request.params["id"])
    if found is None:
        raise APIError("There is no such X-ray", 404, "xray_not_found")
    return Response.json(found.view())


def stop(request):
    _app_only(request)
    return Response.json(_board(request).stop(request.params["id"]).view())


def open_board(runtime):
    runtime.netxray_board = job.Board(runtime)


def close_board(runtime):
    board = getattr(runtime, "netxray_board", None)
    if board is not None:
        board.close()
        runtime.netxray_board = None


def stay_on_mac(value, runtime):
    if value is not True:
        raise ValueError(f"{STAY_ON_MAC} is true or left out")
    return True


def register(api):
    """``api`` is mobile_agent.harness_api."""
    # The finished report's run stays on this Mac: nothing shares, sends or reads it but the app.
    api.register_run_field(STAY_ON_MAC, stay_on_mac)
    api.register_service(open_board, close_board)
    add_route(Route("netxray", "POST", "/api/xray/network", start))
    add_route(Route("netxray", "GET", "/api/xray/network/{id}", status, body="none"))
    add_route(Route("netxray", "POST", "/api/xray/network/{id}/stop", stop, body="none"))
