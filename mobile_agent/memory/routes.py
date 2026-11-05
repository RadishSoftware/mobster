"""Memory's HTTP routes (SPEC §3.3 API), under /api/memory (seam S3). The Mac app's Memory page, the run view, the
composer and the thread cards use them; ``mobster memory`` reads and writes the same file directly.

Every change publishes on the bus topic ``memory``: ``fact_changed {op, fact|id}`` (op: added, updated, kept,
forgotten, deleted, cleared), ``proposal_created {proposal}`` (the listener), ``proposal_resolved {id, accepted,
factId?}``, ``settings_changed {settings}`` and ``learned {runId, facts}`` (a finished run's new facts).
"""

from .. import harness_api
from ..api_errors import APIError
from ..api_routes import Response, Route
from . import model, project, retrieval
from . import store as memory_store
from .listener import publish
from .retrieval import MAX_CHARS, MAX_FACTS
from .store import CONFIRM

OWNER = "memory"
LIMITS = {"factChars": memory_store.FACT_CHARS, "facts": memory_store.MAX_FACTS, "pinned": memory_store.MAX_PINNED,
          "pinnedChars": memory_store.PINNED_CHARS, "perTask": MAX_FACTS, "perTaskChars": MAX_CHARS,
          "suggestionDays": memory_store.PROPOSAL_DAYS, "staleDays": memory_store.STALE_DAYS,
          "forgetDays": memory_store.QUIET_DAYS}
VOCABULARY = {"kinds": list(model.KINDS), "sources": list(model.SOURCES),
              "scopes": list(model.SCOPE_KINDS) + ["global"]}


def _store():
    store = memory_store.default_store()
    if store is None:
        raise APIError("Mobster can't keep memory here: this Mac user has no home folder.", 503,
                       "memory_unavailable")
    return store


def _query(request, name):
    values = request.query.get(name) or []
    return values[0] if values else None


def _body(request, allowed):
    body = request.body or {}
    for key in body:
        if key not in allowed:
            raise APIError(f"Unsupported field {key}.", 400, "invalid_field")
    return body


def _flag(body, key):
    value = body.get(key)
    if value is not None and not isinstance(value, bool):
        raise APIError(f"{key} is true or false.", 400, "invalid_field")
    return value


def list_facts(request):
    """?scope=<scope>[,<scope>]&kind=&source=&new=1&q=. ``new`` counts what Mobster learned since the page was last
    opened (mark_looked) and the person hasn't kept."""
    store = _store()
    facts = store.list_facts(scope=_query(request, "scope"), q=_query(request, "q"), kind=_query(request, "kind"),
                             source=_query(request, "source"), new=_query(request, "new") in ("1", "true"))
    return Response.json({"facts": facts, "count": store.count(), "limits": LIMITS, **VOCABULARY})


def new_facts(request):
    store = _store()
    facts = store.list_facts(scope=_query(request, "scope"), new=True)
    return Response.json({"facts": facts, "count": len(facts)})


def looked(request):
    """The person opened the page: what was new stops being new (kept facts never were)."""
    return Response.json({"lookedAt": _store().mark_looked()})


def add_fact(request):
    body = _body(request, ("text", "scope", "pinned", "kind"))
    fact = _store().add_fact(body.get("text"), body.get("scope") or "global", pinned=bool(_flag(body, "pinned")),
                             kind=body.get("kind"))
    publish({"event": "fact_changed", "op": "added", "fact": fact})
    return Response.json({"fact": fact}, 201)


def update_fact(request):
    body = _body(request, ("text", "scope", "pinned", "kind"))
    if not body:
        raise APIError("Change the text, the scope, the kind or whether it's pinned.", 400, "invalid_field")
    fact = _store().update_fact(request.params["id"], text=body.get("text"), scope=body.get("scope"),
                                pinned=_flag(body, "pinned"), kind=body.get("kind"))
    publish({"event": "fact_changed", "op": "updated", "fact": fact})
    return Response.json({"fact": fact})


def keep_fact(request):
    fact = _store().keep(request.params["id"])
    publish({"event": "fact_changed", "op": "kept", "fact": fact})
    return Response.json({"fact": fact})


def forget_fact(request):
    """Forget: remove the fact and stop every run and check teaching it again for 90 days."""
    fact = _store().forget(request.params["id"])
    publish({"event": "fact_changed", "op": "forgotten", "id": fact["id"]})
    return Response.json({"forgotten": True, "fact": fact})


def preview(request):
    """The facts a task would get: ?goal=&app=<bundle>[&app=...]&profile=&project=&phone=. Marks nothing used."""
    store = _store()
    context = {kind: _query(request, kind) for kind in ("profile", "project", "phone") if _query(request, kind)}
    chosen = retrieval.select(store.all_facts(), _query(request, "goal") or "", request.query.get("app") or (),
                              context)
    by_id = {fact["id"]: fact for fact in store.list_facts()}
    return Response.json({"facts": [by_id[fact["id"]] for fact in chosen if fact["id"] in by_id],
                          "chars": len(retrieval.render(chosen)), "limits": LIMITS})


def link_project(request):
    """The Projects page tells memory a project's folder: facts learned before it had an id move to it, and the
    folder's ``.mobster/memory.yaml`` is read in (as repo facts) and written back."""
    body = _body(request, ("id", "path"))
    store = _store()
    project_id, folder = body.get("id"), body.get("path")
    moved = store.link_project(project_id, folder)
    imported = project.import_file(store, project_id)
    project.export_file(store, project_id)
    publish({"event": "fact_changed", "op": "updated", "projectId": project_id})
    return Response.json({"linked": True, "moved": moved, "imported": imported})


def run_memory(request):
    """?runId=<id>: the facts a run used ("from memory") and learned ("Mobster learned N things")."""
    run_id = _query(request, "runId")
    if not run_id:
        raise APIError("Say which run (runId).", 400, "invalid_field")
    found = _store().run_memory(run_id)
    return Response.json({**found, "learnedCount": len(found["learned"])})


def delete_fact(request):
    _store().forget(request.params["id"])
    publish({"event": "fact_changed", "op": "deleted", "id": request.params["id"]})
    return Response.json({"deleted": True})


def list_proposals(request):
    status = _query(request, "status") or "pending"
    proposals = _store().list_proposals(thread_id=_query(request, "thread"), status=status)
    return Response.json({"proposals": proposals})


def answer_proposal(request):
    body = _body(request, ("accept", "text", "pinned"))
    if "accept" not in body:
        raise APIError("Say whether to remember it (accept true or false).", 400, "invalid_answer")
    store = _store()
    proposal_id = request.params["id"]
    thread_id, item_id = store.proposal_item(proposal_id)
    proposal, fact = store.resolve_proposal(proposal_id, body.get("accept"), text=body.get("text"),
                                            pinned=_flag(body, "pinned"))
    if thread_id and item_id:
        harness_api.update_thread_item(thread_id, item_id, {
            "status": proposal["status"], **({"text": fact["text"], "factId": fact["id"]} if fact else {})})
    publish({"event": "proposal_resolved", "id": proposal_id, "accepted": fact is not None,
             **({"factId": fact["id"]} if fact else {})})
    if fact is not None:
        publish({"event": "fact_changed", "op": "added", "fact": fact})
    return Response.json({"proposal": proposal, "fact": fact})


def get_settings(request):
    store = _store()
    return Response.json({"settings": store.settings(), "count": store.count(), "limits": LIMITS})


def set_settings(request):
    settings = _store().update_settings(dict(_body(request, tuple(memory_store.SETTINGS))))
    publish({"event": "settings_changed", "settings": settings})
    return Response.json({"settings": settings})


def clear(request):
    body = _body(request, ("confirm",))
    if body.get("confirm") != CONFIRM:
        raise APIError(f'To delete everything Mobster remembers, send confirm: "{CONFIRM}".', 400,
                       "confirm_required")
    deleted = _store().clear()
    publish({"event": "fact_changed", "op": "cleared"})
    return Response.json({"deleted": deleted})


def export(request):
    """Markdown, or ?format=json: every fact with its kind, source, scope and last use."""
    if _query(request, "format") == "json":
        return Response.json(_store().export_json())
    return Response.bytes(_store().export_markdown().encode(), "text/markdown; charset=utf-8")


ROUTES = (
    Route(OWNER, "GET", "/api/memory/facts", list_facts, body="none"),
    Route(OWNER, "POST", "/api/memory/facts", add_fact, max_bytes=4_000),
    Route(OWNER, "POST", "/api/memory/facts/{id}", update_fact, max_bytes=4_000),
    Route(OWNER, "DELETE", "/api/memory/facts/{id}", delete_fact, body="none"),
    Route(OWNER, "POST", "/api/memory/facts/{id}/keep", keep_fact, body="none"),
    Route(OWNER, "POST", "/api/memory/facts/{id}/forget", forget_fact, body="none"),
    Route(OWNER, "GET", "/api/memory/new", new_facts, body="none"),
    Route(OWNER, "POST", "/api/memory/looked", looked, body="none"),
    Route(OWNER, "GET", "/api/memory/preview", preview, body="none"),
    Route(OWNER, "GET", "/api/memory/run", run_memory, body="none"),
    Route(OWNER, "POST", "/api/memory/projects", link_project, max_bytes=2_000),
    Route(OWNER, "GET", "/api/memory/proposals", list_proposals, body="none"),
    Route(OWNER, "POST", "/api/memory/proposals/{id}", answer_proposal, max_bytes=4_000),
    Route(OWNER, "GET", "/api/memory/settings", get_settings, body="none"),
    Route(OWNER, "POST", "/api/memory/settings", set_settings, max_bytes=1_000),
    Route(OWNER, "POST", "/api/memory/clear", clear, max_bytes=1_000),
    Route(OWNER, "GET", "/api/memory/export", export, body="none"),
)
