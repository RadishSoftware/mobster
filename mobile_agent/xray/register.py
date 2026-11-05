from ..api_errors import APIError
from ..api_routes import Response, Route
from ..api_routes import register as add_route
from . import job, policy

HOOK_ORDER = 1       # before memory routines (10): an x-ray run is never a routine


def start(request):
    """POST /api/xray/app: {"appId": bundle, "device"?, "screens"?, "minutes"?, "key"?} -> 201 {"runId"}."""
    # A task like any other: Runtime.create asks the plan gate, the phone's owner and the queue.
    body = request.body if isinstance(request.body, dict) else {}
    if set(body) - {"appId", "device", "screens", "minutes", "key"}:
        raise ValueError("App X-ray takes appId, device, screens, minutes and key")
    bundle = body.get("appId")
    if not isinstance(bundle, str) or not 1 <= len(bundle) <= 160:
        raise ValueError("appId is the app's bundle ID")
    key, device = body.get("key"), body.get("device")
    if key is not None and (not isinstance(key, str) or not 1 <= len(key) <= 100):
        raise ValueError("key is 1 to 100 characters")
    if device is not None and not isinstance(device, str):
        raise ValueError("device must be a device's id, UDID or name")
    refusal = policy.app_refusal(bundle)
    if refusal:
        raise APIError(refusal, 422, "xray_app_refused")
    options = job.clean({name: body[name] for name in ("screens", "minutes") if name in body})
    runtime = request.runtime
    app = job.find_app(runtime, bundle, device)
    if app is None:
        raise ValueError("Choose an app that is installed on the phone")
    origin = request.origin if request.origin in ("app", "tui", "cli") else "api"
    created = runtime.create(app["id"], job.GOAL.format(name=app["name"]), "live", f"xray-{key}" if key else None,
                             **({"device": device} if device is not None
                                else {}), extras={"xray": options}, origin=origin, queue=True)
    run, _ = created if key else (created, False)
    return Response.json({"runId": run.id}, 201)


def register(api):
    """``api`` is mobile_agent.harness_api."""
    api.register_run_field("xray", lambda value, runtime: job.clean(value), model=False)  # it maps with no model
    api.register_pre_run(job.hook, order=HOOK_ORDER)
    add_route(Route("xray", "POST", "/api/xray/app", start))
