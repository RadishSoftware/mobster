"""Track ``waiting``'s registrations: no agent or MCP tool, since its result would carry message text to a model."""

from ..api_errors import APIError
from ..api_routes import Response, Route
from ..api_routes import register as add_route
from . import job

APP_ONLY = "Only the Mobster app can read your Messages. Open Mobster and ask there."


def start(request):
    """POST /api/waiting: {"key"?} -> 201 {"runId"}. The Mac app only (its session), like Settings routes."""
    # Fail closed: the result holds message text, so only the Mac app's own session starts it (not an API token alone).
    if not request.app_session:
        raise APIError(APP_ONLY, 403, "app_only")
    body = request.body if isinstance(request.body, dict) else {}
    key = body.get("key")
    if key is not None and (not isinstance(key, str) or not 1 <= len(key) <= 100):
        raise ValueError("key is 1 to 100 characters")
    run = job.run(request.runtime, key=f"waiting-{key}" if key else None)
    return Response.json({"runId": run.id}, 201)


def register(api):
    """``api`` is mobile_agent.harness_api."""
    add_route(Route("waiting", "POST", "/api/waiting", start))
