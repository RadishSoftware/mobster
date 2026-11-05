"""Track ``triggers``' registrations (tracks.load calls ``register(api)`` once per process, staged)."""

import re

from . import routes, service

ID = re.compile(r"[0-9a-f]{12}")


def validate_trigger(value, runtime):
    """The run field: {"id": 12 hex characters, "name": 1 to 60 characters}."""
    if not isinstance(value, dict) or set(value) != {"id", "name"}:
        raise ValueError('trigger is {"id", "name"}')
    if not isinstance(value["id"], str) or not ID.fullmatch(value["id"]):
        raise ValueError("A trigger's id is 12 hex characters")
    if not isinstance(value["name"], str) or not 1 <= len(value["name"].strip()) <= 60:
        raise ValueError("A trigger's name is 1 to 60 characters")
    return {"id": value["id"], "name": " ".join(value["name"].split())}


def register(api):
    """``api`` is mobile_agent.harness_api."""
    from .. import full_disk_access, journal
    from . import store
    journal.register_schema(store.SCHEMA, store.VERSION, store.migrate)
    api.register_run_field("trigger", validate_trigger, public=True)
    api.register_service(service.start, service.close)
    routes.register_routes()
    full_disk_access.register_route()
