"""Track ``outlets``' registrations (tracks.load calls ``register(api)`` once per process, staged)."""

import re

from . import hooks, inbound, inbound_routes, routes, service

ID = re.compile(r"[0-9a-f]{12}")
BLANKS_MAX = 12
SHAPE = ('sendTo is a saved destination\'s name or id, or {"destination": "…"}')


def validate_send_to(value, runtime):
    live = service.service_for(runtime)
    if live is None:
        raise ValueError(routes.OFF)
    if isinstance(value, str):
        value = {"destination": value}
    if not isinstance(value, dict) or set(value) - {"destination", "workflow"} or "destination" not in value:
        raise ValueError(SHAPE)
    out = {"destination": live.find(value["destination"])["id"]}
    workflow = value.get("workflow")
    if workflow is not None:
        out["workflow"] = workflow_of(workflow)
    return out


def workflow_of(value):
    """The workflow a run came from, as its result carries it: {"id", "name", "blanks": {name: value}}."""
    if not isinstance(value, dict) or set(value) - {"id", "name", "blanks"}:
        raise ValueError('sendTo\'s workflow is {"id", "name", "blanks"}')
    identifier, name, blanks = value.get("id"), value.get("name"), value.get("blanks") or {}
    if identifier is not None and (not isinstance(identifier, str) or not ID.fullmatch(identifier)):
        raise ValueError("A workflow's id is 12 hex characters")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
        raise ValueError("A workflow's name is 1 to 120 characters")
    if not isinstance(blanks, dict) or len(blanks) > BLANKS_MAX or not all(
            isinstance(k, str) and 1 <= len(k) <= 40 and isinstance(v, str) and len(v) <= 300
            for k, v in blanks.items()):
        raise ValueError("A workflow's blanks are at most 12 names with short text values")
    return {"id": identifier, "name": " ".join(name.split()), "blanks": dict(blanks)}


def register(api):
    """``api`` is mobile_agent.harness_api."""
    from .. import journal
    from . import store
    journal.register_schema(store.SCHEMA, store.VERSION, store.migrate)
    journal.register_schema(hooks.SCHEMA, hooks.VERSION, hooks.migrate)
    journal.register_schema(inbound.SCHEMA, inbound.VERSION, inbound.migrate)
    api.register_run_field("sendTo", validate_send_to, public=True)
    api.register_run_listener(service.Listener())
    api.register_service(service.start, service.close)
    api.register_service(hooks.start, hooks.close)
    api.register_service(inbound.start, inbound.close)
    routes.register_routes()
    inbound_routes.register_routes()
