"""Memory's HTTP routes (SPEC §3.3 API), under /api/memory (seam S3). The Mac app's Settings › Memory and the thread
cards use them; ``mobster memory`` reads and writes the same file directly.

Every change publishes on the bus topic ``memory``: ``fact_changed {op, fact|id}``, ``proposal_created {proposal}``
(the listener), ``proposal_resolved {id, accepted, factId?}`` and ``settings_changed {settings}``.
"""

from .. import harness_api
from ..api_errors import APIError
from ..api_routes import Response, Route
from . import store as memory_store
from .listener import publish

OWNER = "memory"
LIMITS = {"factChars": memory_store.FACT_CHARS, "facts": memory_store.MAX_FACTS, "pinned": memory_store.MAX_PINNED,
          "pinnedChars": memory_store.PINNED_CHARS, "perTask": 12, "perTaskChars": 1200,
          "suggestionDays": memory_store.PROPOSAL_DAYS}
CONFIRM = "delete everything"


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
    store = _store()
    facts = store.list_facts(scope=_query(request, "scope"), q=_query(request, "q"))
    return Response.json({"facts": facts, "count": store.count(), "limits": LIMITS})


def add_fact(request):
    body = _body(request, ("text", "scope", "pinned"))
    fact = _store().add_fact(body.get("text"), body.get("scope") or "global", pinned=bool(_flag(body, "pinned")))
    publish({"event": "fact_changed", "op": "added", "fact": fact})
    return Response.json({"fact": fact}, 201)


def update_fact(request):
    body = _body(request, ("text", "scope", "pinned"))
    if not body:
        raise APIError("Change the text, the app or whether it's pinned.", 400, "invalid_field")
    fact = _store().update_fact(request.params["id"], text=body.get("text"), scope=body.get("scope"),
                                pinned=_flag(body, "pinned"))
    publish({"event": "fact_changed", "op": "updated", "fact": fact})
    return Response.json({"fact": fact})


def delete_fact(request):
    _store().delete_fact(request.params["id"])
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
    return Response.bytes(_store().export_markdown().encode(), "text/markdown; charset=utf-8")


ROUTES = (
    Route(OWNER, "GET", "/api/memory/facts", list_facts, body="none"),
    Route(OWNER, "POST", "/api/memory/facts", add_fact, max_bytes=4_000),
    Route(OWNER, "POST", "/api/memory/facts/{id}", update_fact, max_bytes=4_000),
    Route(OWNER, "DELETE", "/api/memory/facts/{id}", delete_fact, body="none"),
    Route(OWNER, "GET", "/api/memory/proposals", list_proposals, body="none"),
    Route(OWNER, "POST", "/api/memory/proposals/{id}", answer_proposal, max_bytes=4_000),
    Route(OWNER, "GET", "/api/memory/settings", get_settings, body="none"),
    Route(OWNER, "POST", "/api/memory/settings", set_settings, max_bytes=1_000),
    Route(OWNER, "POST", "/api/memory/clear", clear, max_bytes=1_000),
    Route(OWNER, "GET", "/api/memory/export", export, body="none"),
)
