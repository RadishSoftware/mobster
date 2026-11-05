"""Track `harness`'s registrations, staged so a register that raises leaves nothing registered."""


def _bool(value, runtime):
    if type(value) is not bool:
        raise ValueError("askUser must be true or false")
    return value


def hints_route(request):
    """GET /api/harness/hints: {apps: hints.health}: every app's hints by source (packs, replays,
    the iOS knowledge base)."""
    from pathlib import Path

    from .. import hints
    from ..api_routes import Response
    state_db = getattr(getattr(request.runtime, "config", None), "state_db", None)
    return Response.json({"apps": hints.health(Path(state_db).parent if state_db else None)})


def register(api):
    """``api`` is mobile_agent.harness_api."""
    from .. import api_routes, journal, project_env
    from . import checkpoints, prewarm, routes
    from .tools import ask_user, watch

    journal.register_schema(checkpoints.SCHEMA, checkpoints.VERSION, checkpoints.migrate)
    api.register_run_field("resumeFrom", checkpoints.validate_resume_from)
    api.register_run_field("askUser", _bool)
    # A saved task's ${NAME}s come from this project's secrets (project_env.py); the goal keeps the placeholder.
    api.register_run_field("projectEnv", project_env.validate_root)
    api.register_pre_run(checkpoints.pre_run, order=0)
    api.register_frontier_options(checkpoints.frontier_options)
    api.register_tool(ask_user.factory, order=0)
    api.register_tool(watch.factory, order=1)
    api.register_service(lambda runtime: None, lambda runtime: (checkpoints.close(runtime), prewarm.close_all(runtime)))
    for route in routes.ROUTES:
        api_routes.register(route)
    from .. import hints
    api.register_run_listener(hints.HintListener())
    api_routes.register(api_routes.Route("harness", "GET", "/api/harness/hints", hints_route, body="none"))
    from ..integrations import routes as client_routes
    client_routes.register_routes()
