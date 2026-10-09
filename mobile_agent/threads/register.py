"""Track `conversations`'s registrations (tracks.load calls ``register(api)`` once per process, staged: a register that
raises leaves nothing registered and is shown in GET /api/status "extensions").

- Journal schema ``threads`` v1 (threads, thread_items, thread_requests).
- Run fields ``threadId`` (public) and ``via`` (not public).
- The context provider ``thread`` (order 10), only for a run in a thread.
- One run listener, one service (a ThreadService per Runtime), the thread sink.
- Routes under /api/threads, and the MCP provider ``threads`` (phone_task, phone_task_status).
"""

import re

from . import context, service, store

THREAD_ID = re.compile(r"[a-f0-9]{12}")


def validate_thread(value, runtime):
    """``threadId``: a conversation of this Runtime that isn't archived."""
    if not isinstance(value, str) or not THREAD_ID.fullmatch(value):
        raise ValueError("threadId is a conversation's 12-character id")
    live = service.service_for(runtime)
    thread = live.store.get(value) if live is not None else None
    if thread is None:
        raise ValueError(store.NOT_FOUND)
    if thread.get("archived"):
        raise ValueError(service.ARCHIVED)
    return value


def validate_via(value, runtime):
    if value not in service.VIA:
        raise ValueError("via is composer, voice, cli, tui or mcp")
    return value


def provider(ctx):
    if not ctx.thread_id:
        return None
    live = service.service_for(ctx.runtime)
    return context.ThreadProvider(live) if live is not None else None


def register(api):
    """``api`` is mobile_agent.harness_api."""
    from .. import journal
    from ..mcp_server import registry as mcp_registry
    from . import mcp, routes
    journal.register_schema(store.SCHEMA, store.VERSION, store.migrate)
    api.register_run_field("threadId", validate_thread, public=True)
    api.register_run_field("via", validate_via, public=False)
    api.register_context_provider(provider, order=context.ORDER)
    api.register_run_listener(service.Listener())
    api.register_service(service.start, service.close)
    api.register_thread_sink(service.sink_append, service.sink_update)
    routes.register_routes()
    mcp_registry.register_provider(mcp.ThreadsProvider())
