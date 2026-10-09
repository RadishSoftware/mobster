"""Track `wireless`'s registrations (tracks.load calls ``register(api)`` once per process, staged: a register that
raises leaves nothing registered and is shown in GET /api/status "extensions").

Wi-Fi registers its routes (``/api/wireless``, ``/api/devices/{device}/wifi``) and one service, the transport
monitor, which runs only in a runtime that manages USB phones and only with MOBSTER_WIFI_TRANSPORT on. It adds no
run field, context, tool or listener: a task runs the same on the cable and on Wi-Fi, with the same approvals.
"""


def register(api):
    """``api`` is mobile_agent.harness_api."""
    from . import api as routes
    routes.register_routes()
    api.register_service(routes.start_service, routes.close_service)
