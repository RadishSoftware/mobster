"""Track ``feedback``'s registrations (tracks.load calls ``register(api)`` once per process, staged): the routes under
/api/feedback (routes.py)."""

from . import routes


def register(api):
    """``api`` is mobile_agent.harness_api."""
    routes.register_routes()
