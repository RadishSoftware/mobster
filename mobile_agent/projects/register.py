"""Track ``projects``' registrations (tracks.load calls ``register(api)`` once per process, staged).

- Journal schema ``projects`` v2 (store.py): projects, their check runs and approved CI revisions.
- The run field ``project`` (public): the project a task belongs to, saved as {id, path, name}, so its memory can be
  the project's (the memory track reads id and path).
- One service (a ProjectService per Runtime) and the routes under /api/projects. Adding or opening a project tells
  memory its folder (POST /api/memory/projects), when the memory track is here.

No CLI command and no MCP provider of its own yet: `mobster test` and the MCP tools already find a project by its
`.mobster/` folder.
"""

from . import routes, service


def validate_project(value, runtime):
    """The run field ``project``: a project's id or {id}, saved as {id, path, name} (service.run_field)."""
    found = service.service_for(runtime)
    if found is None:
        raise ValueError(service.GONE)
    return found.run_field(value)


def register(api):
    """``api`` is mobile_agent.harness_api."""
    from .. import journal
    from . import store
    journal.register_schema(store.SCHEMA, store.VERSION, store.migrate)
    api.register_run_field("project", validate_project, public=True)
    api.register_service(service.start, service.close)
    routes.register_routes()
