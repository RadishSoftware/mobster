"""The memory context provider (SPEC §3.3 F2): key "memory", order 20, one stable block of at most 12 facts and
1,200 characters, inside the cached part of the prompt.

It runs for tasks a person is watching (the Mac app, the terminal UI, `mobster chat`) and for their workflows, never
for tasks another agent started over MCP or the HTTP API, and only while Settings › Memory › Use memory in tasks is
on. A task sends only the facts it uses; ``memory_used`` names them by id (never their words).
"""

from .. import harness_api
from . import retrieval
from .store import default_store

KEY = "memory"
ORDER = 20
ORIGINS = frozenset(harness_api.INTERACTIVE | {"workflow", "schedule"})


class MemoryProvider:
    key = KEY
    max_chars = retrieval.MAX_CHARS

    def __init__(self, store):
        self.store = store

    def blocks(self, ctx):
        if not self.store.settings().get("useInTasks", True):
            return ()
        facts = retrieval.select(self.store.all_facts(), ctx.goal, apps_in_play(ctx))
        if not facts:
            return ()
        ids = [fact["id"] for fact in facts]
        ctx.emit({"event": "memory_used", "ids": ids})
        self.store.mark_used(ids)
        return (harness_api.ContextBlock(KEY, retrieval.TITLE, retrieval.render(facts), stable=True),)

    def turn_blocks(self, ctx, turn):
        return ()


def apps_in_play(ctx):
    """The start app and the apps of the conversation's last task (their bundle ids)."""
    apps = [ctx.app_bundle] if ctx.app_bundle else []
    for bundle in thread_apps(ctx):
        if bundle not in apps:
            apps.append(bundle)
    return apps


def thread_apps(ctx):
    """The start app of the latest earlier task in this conversation, and the apps its plan named, as bundle ids.
    Read from the runs this Mobster holds (never another track's store); () outside a conversation."""
    thread_id = ctx.thread_id
    runtime = ctx.runtime
    if not thread_id or runtime is None or not hasattr(runtime, "runs"):
        return ()
    lock = getattr(runtime, "lock", None)
    if lock is not None:
        with lock:
            runs = list(runtime.runs.values())
    else:
        runs = list(runtime.runs.values())
    earlier = [run for run in runs if run.id != ctx.run_id and (getattr(run, "extras", None) or {}).get("threadId")
               == thread_id]
    if not earlier:
        return ()
    last = max(earlier, key=lambda run: run.created_at)
    out = []
    bundle = (getattr(last, "app", None) or {}).get("bundleId")
    if bundle:
        out.append(bundle)
    names = {}
    from ..catalog import APPS, _EXTRA_NAMES
    for app in APPS:
        names.setdefault(app["name"].lower(), app["bundleId"])
    for known, name in _EXTRA_NAMES.items():
        names.setdefault(name.lower(), known)
    for event in reversed(list(getattr(last, "events", ()) or ())):
        if event.get("event") == "plan":
            for app in event.get("apps") or ():
                app = str(app)
                found = app if "." in app else names.get(app.lower())
                if found and found not in out:
                    out.append(found)
            break
    return tuple(out)


def factory(ctx):
    """The provider for this run, or None: another agent's task, no store, or nothing remembered yet."""
    if ctx.origin not in ORIGINS:
        return None
    store = default_store()
    if store is None or not store.exists():
        return None
    return MemoryProvider(store)
