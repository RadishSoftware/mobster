"""Track `testing`'s registrations (tracks.load calls ``register(api)`` once per process, staged: a register that
raises leaves nothing registered and is shown in GET /api/status "extensions").

- ``POST /api/tests/checks {runId, name?}`` → ``{yaml, name, app, steps, expect}``: a finished task as a check file,
  for the Mac app's "Copy as check" (nothing is written on this Mac).
- The MCP tools ``run_tests`` and ``record_check`` (mcp.py).
"""

NAME_LIMIT = 120


def register(api):
    """``api`` is mobile_agent.harness_api."""
    from ..api_routes import Route, register as add_route
    from ..mcp_server.registry import register_provider
    from .mcp import TestTools
    add_route(Route("testkit", "POST", "/api/tests/checks", copy_as_check, max_bytes=4096))
    register_provider(TestTools())


def copy_as_check(request):
    from ..api_routes import Response
    from .record import RUN_ID, RecordError, check_data, check_yaml
    body = request.body if isinstance(request.body, dict) else {}
    unknown = [key for key in body if key not in ("runId", "name")]
    if unknown:
        return Response.error(f"Unknown field {unknown[0]}: send runId and, if you like, name.", 400, "bad_request")
    run_id, name = body.get("runId"), body.get("name")
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        return Response.error("runId must be a task's 12-character id.", 400, "bad_request")
    if name is not None and (not isinstance(name, str) or len(name) > NAME_LIMIT):
        return Response.error(f"name must be text of at most {NAME_LIMIT} characters.", 400, "bad_request")
    runtime = request.runtime
    with runtime.lock:
        run = runtime.runs.get(run_id)
    if run is None:
        return Response.error("That task isn't in Mobster's history.", 404, "run_not_found")
    record = {**run.public(), "app": dict(run.app or {})}  # public() leaves out the app's bundle ID
    try:
        data = check_data(record, name=name)
        text = check_yaml(record, name=name)
    except RecordError as error:
        return Response.error(error.message, error.status, error.code)
    return Response.json({"yaml": text, "name": data["name"], "app": data["app"]["bundle"],
                          "steps": len(data.get("steps") or ()), "expect": len(data["expect"])})
