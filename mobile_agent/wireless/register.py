"""Wi-Fi's routes and transport monitor, and nothing a task sees: a task runs the same on the cable and on Wi-Fi."""


def register(api):
    """``api`` is mobile_agent.harness_api."""
    from . import api as routes
    routes.register_routes()
    api.register_service(routes.start_service, routes.close_service)
