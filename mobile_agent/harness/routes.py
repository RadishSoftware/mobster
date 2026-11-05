"""The harness track's HTTP API (seam S3, mobile_agent/docs/harness.md); none of its routes approves anything."""

from ..api_errors import APIError
from ..api_routes import HEADER_ORIGINS, Response, Route
from ..steering import SteeringFull
from . import checkpoints, control, prewarm, settings
from .control import PAUSE_LIMIT_SECONDS

NO_MESSAGES = "This task can't take messages while it works. Stop it, or wait for it to finish."
NO_PAUSE = "This task can't pause. Stop it instead."
FULL = "Mobster's agent has 10 messages it hasn't read yet. Wait for it to catch up."


def _run(request):
    runtime = request.runtime
    with runtime.lock:
        run = runtime.runs.get(request.params["id"])
    if run is None:
        raise APIError("Task not found", 404, "not_found")
    return run


def _steerable_now(run, unsupported, code):
    if run.finished_at is not None:
        raise APIError("This task has finished.", 409, "run_not_active")
    if not run.steerable:
        raise APIError(unsupported, 409, code)


def source_of(request, given):
    """The message's source: the request's origin label, or "voice" from the Mac app."""
    origin = request.origin if request.origin in HEADER_ORIGINS else "cli"
    if given is None or given == origin:
        return origin
    if given == "voice" and origin == "app":
        return "voice"
    raise ValueError("source can only be \"voice\", from the Mac app")


def messages(request):
    run = _run(request)
    body = request.body or {}
    if "control" in body:
        if set(body) != {"control"} or body["control"] != "stop_after_step":
            raise ValueError("The only control is \"stop_after_step\", on its own")
        _steerable_now(run, NO_MESSAGES, "steer_unsupported")
        control.request_stop_after(run.steering)
        run.emit({"event": "stop_after_step_requested"})
        return Response.json({"control": "stop_after_step"}, 202)
    if set(body) - {"text", "source"} or not isinstance(body.get("text"), str) \
            or body.get("source") is not None and not isinstance(body["source"], str):
        raise ValueError("Send the message as text, 1 to 2,000 characters")
    source = source_of(request, body.get("source"))
    _steerable_now(run, NO_MESSAGES, "steer_unsupported")
    try:
        message = run.steer(body["text"], source)
    except SteeringFull:
        raise APIError(FULL, 429, "steering_full") from None
    return Response.json({"message": message}, 202)


def pause(request):
    run = _run(request)
    body = request.body or {}
    if set(body) != {"paused"} or type(body["paused"]) is not bool:
        raise ValueError("Send {\"paused\": true} to pause or {\"paused\": false} to continue")
    _steerable_now(run, NO_PAUSE, "pause_unsupported")
    control.set_paused(run.steering, body["paused"])
    return Response.json({"run": run.public(include_events=False), "paused": body["paused"]})


def pickup(request):
    if request.body:
        raise ValueError("Send an empty JSON object")
    keys = request.headers.get_all("Idempotency-Key", []) if hasattr(request.headers, "get_all") else []
    key = keys[0] if keys else None
    run, replayed = checkpoints.pick_up(request.runtime, request.params["id"], idempotency_key=key,
                                        origin=request.origin)
    return Response.json({"run": run.public(), "replayed": replayed}, 200 if replayed else 201)


def prewarm_route(request):
    body = request.body or {}
    if set(body) - {"device"} or body.get("device") is not None and not isinstance(body["device"], str):
        raise ValueError("Send {} or {\"device\": \"<a device's id, UDID or name>\"}")
    return Response.json(prewarm.prewarm(request.runtime, body.get("device")), 202)


def state(runtime):
    return {"askUser": settings.ask_user(runtime),
            "askUserLimit": settings.ASK_USER_LIMIT, "pauseLimitSeconds": PAUSE_LIMIT_SECONDS,
            "ledger": False, "earlyLaunch": False, "longRun": None}


def harness_get(request):
    return Response.json(state(request.runtime))


def harness_post(request):
    body = request.body or {}
    if set(body) != {"askUser"} or type(body["askUser"]) is not bool:
        raise ValueError("Send {\"askUser\": true} or {\"askUser\": false}")
    settings.save(request.runtime, {"askUser": body["askUser"]})
    return Response.json(state(request.runtime))


ROUTES = (
    Route("harness", "POST", r"/api/runs/{id}/messages", messages, max_bytes=8_000),
    Route("harness", "POST", r"/api/runs/{id}/pause", pause, max_bytes=1_000),
    Route("harness", "POST", r"/api/runs/{id}/pickup", pickup, max_bytes=1_000, idempotency=True),
    Route("harness", "POST", r"/api/prewarm", prewarm_route, max_bytes=1_000),
    Route("harness", "GET", r"/api/harness", harness_get, body="none"),
    Route("harness", "POST", r"/api/harness", harness_post, max_bytes=1_000),
)
