"""Track `memory`'s registrations (tracks.load calls ``register(api)`` once per process, staged: a register that
raises leaves nothing registered and is shown in GET /api/status "extensions").

v1 (SPEC §5.4): facts, suggestions, the memory provider, the /api/memory routes and `mobster memory`. Routines
(the replayer, the pre-run hook, RUN_ROUTINE, the routineId and routineParams run fields, /api/routines) are v2.
Registering does no I/O: the store opens on first use.
"""


def register(api):
    """``api`` is mobile_agent.harness_api."""
    from .. import api_routes
    from . import provider, routes
    from .listener import ProposalListener

    api.register_context_provider(provider.factory, order=provider.ORDER)
    api.register_run_listener(ProposalListener())
    for route in routes.ROUTES:
        api_routes.register(route)
