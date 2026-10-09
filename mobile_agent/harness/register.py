"""Track `harness`'s registrations (tracks.load calls ``register(api)`` once per process, staged: a register that
raises leaves nothing registered and is shown in GET /api/status "extensions")."""


def _bool(value, runtime):
    if type(value) is not bool:
        raise ValueError("askUser must be true or false")
    return value


def register(api):
    """``api`` is mobile_agent.harness_api."""
    from .. import api_routes, journal
    from . import checkpoints, prewarm, routes
    from .tools import ask_user

    journal.register_schema(checkpoints.SCHEMA, checkpoints.VERSION, checkpoints.migrate)
    api.register_run_field("resumeFrom", checkpoints.validate_resume_from)
    api.register_run_field("askUser", _bool)
    api.register_pre_run(checkpoints.pre_run, order=0)
    api.register_frontier_options(checkpoints.frontier_options)
    api.register_tool(ask_user.factory, order=0)
    api.register_service(lambda runtime: None, lambda runtime: (checkpoints.close(runtime), prewarm.close_all(runtime)))
    for route in routes.ROUTES:
        api_routes.register(route)
