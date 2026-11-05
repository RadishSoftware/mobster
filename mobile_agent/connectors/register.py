"""Track ``connectors``' registrations (tracks.load calls ``register(api)`` once per process, staged)."""

from . import routes, service, skill

MAX_NAMED = 8
TOOL_ORDER = 6       # after the harness's ASK_USER (0), LISTEN (1) and longform's tools (5)
PROVIDER_ORDER = 40
SHAPE = "connectors is a list of up to 8 connector names or ids"


def validate_connectors(value, runtime):
    live = service.service_for(runtime)
    if live is None:
        raise ValueError(routes.OFF)
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_NAMED:
        raise ValueError(SHAPE)
    ids = []
    for wanted in value:
        row = live.find(wanted)
        if not row["enabled"]:
            raise ValueError(f"{row['name']} is turned off. Turn it on in Settings › Connectors.")
        if row["id"] not in ids:
            ids.append(row["id"])
    return ids


class Listener:
    def on_finished(self, run, summary, private):
        skill.forget(getattr(run, "id", None))


def register(api):
    """``api`` is mobile_agent.harness_api."""
    from .. import journal
    from . import store
    journal.register_schema(store.SCHEMA, store.VERSION, store.migrate)
    api.register_tool(skill.tool_factory, order=TOOL_ORDER)
    api.register_context_provider(skill.provider_factory, order=PROVIDER_ORDER)
    api.register_run_field("connectors", validate_connectors, public=True)
    api.register_run_listener(Listener())
    api.register_service(service.start, service.close)
    routes.register_routes()
    from ..workflows import records as batch
    from . import steps
    if "connector" not in batch.step_kinds():
        batch.register_step("connector", steps.validate, steps.run)
