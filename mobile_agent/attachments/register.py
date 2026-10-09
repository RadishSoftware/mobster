"""Track `files`'s registrations (tracks.load calls ``register(api)`` once per process, staged: a register that
raises leaves nothing registered and is shown in GET /api/status "extensions").

- Journal schema ``attachments`` v1 (store.migrate).
- Run field ``attachmentIds``: at most 10 files that exist on this Mac (public, so the run page can show them).
- Context provider ``attachments`` (order 30) and the tools READ_ATTACHMENT and PUT_FILE, for runs with files only.
- The clean-up service, the HTTP routes and the MCP tools.

No run listener: a run listener starts the core's listener thread for every run, and the provider records which task
used which file when it reads them (AttachmentsProvider.blocks), which is the moment that matters.
"""

import re

PENDING = "uploading"
ID = re.compile(r"[a-f0-9]{12}")


def validate_attachment_ids(value, runtime):
    """``attachmentIds``: a list of at most 10 distinct attachment ids that exist here. ValueError otherwise."""
    from . import service
    from .store import PER_RUN
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError("attachmentIds must be a list of file ids")
    if PENDING in value:
        raise ValueError("A file is still uploading. Run the task again when it's ready.")
    if len(value) > PER_RUN:
        raise ValueError(f"Attach at most {PER_RUN} files to a task.")
    ids = []
    for item in value:
        if not isinstance(item, str) or not ID.fullmatch(item):
            raise ValueError("attachmentIds must be a list of file ids")
        if item not in ids:
            ids.append(item)
    if not ids:
        return []
    store = service.store_for(runtime)
    if store is None:
        raise ValueError("Attaching files needs Mobster's data folder.")
    found = {a.id for a in store.many(ids)}
    if len(found) != len(ids):
        raise ValueError("A file attached to this task is gone. Attach it again.")
    return ids


def register(api):
    """``api`` is mobile_agent.harness_api."""
    from .. import api_routes, journal
    from ..mcp_server import registry as mcp_registry
    from . import service, store
    from .agent import AttachmentsProvider, PutFile, ReadAttachment
    from .mcp import FilesTools
    from .routes import ROUTES

    journal.register_schema(store.SCHEMA, store.VERSION, store.migrate)
    api.register_run_field("attachmentIds", validate_attachment_ids, public=True)

    def files_store(ctx):
        if not ctx.extras.get("attachmentIds"):
            return None
        return service.store_for(ctx.runtime)

    def provider(ctx):
        found = files_store(ctx)
        return AttachmentsProvider(found) if found is not None else None

    def read_tool(ctx):
        found = files_store(ctx)
        return ReadAttachment(found, ctx) if found is not None else None

    def put_tool(ctx):
        found = files_store(ctx)
        return PutFile(found, ctx) if found is not None else None

    api.register_context_provider(provider, order=30)
    api.register_tool(read_tool, order=30)
    api.register_tool(put_tool, order=31)
    api.register_service(service.start, service.close)
    for route in ROUTES:
        api_routes.register(route)
    mcp_registry.register_provider(FilesTools())
