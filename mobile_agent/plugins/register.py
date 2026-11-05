"""Track ``plugins``' registrations (tracks.load calls ``register(api)`` once per process, staged)."""

from . import routes, service, skills

PROVIDER_ORDER = 45


def register(api):
    """``api`` is mobile_agent.harness_api."""
    from .. import journal
    from . import store
    journal.register_schema(store.SCHEMA, store.VERSION, store.migrate)
    api.register_context_provider(skills.provider_factory, order=PROVIDER_ORDER)
    api.register_service(service.start, service.close)
    routes.register_routes()
