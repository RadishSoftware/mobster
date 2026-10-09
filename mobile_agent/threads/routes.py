"""The conversations API (SPEC §3.2), registered under /api/threads (api_routes, seam S3).

| Route | Body / query | Answer |
|---|---|---|
| GET /api/threads | ?limit<=100&before=<updatedAt>&device=&q=&archived= | {threads, next} |
| POST /api/threads | {title?, device?, message?} + Idempotency-Key | 201 {thread, item?, run?} |
| GET /api/threads/{id} | ?after=<seq> | {thread, items, runs} |
| POST /api/threads/{id} | {title?, archived?, pinned?} | {thread} |
| DELETE /api/threads/{id} | ?runs=delete|keep | {deleted, runsDeleted} |
| POST /api/threads/{id}/messages | MessageBody + Idempotency-Key | 201 {item, run} · 202 {item, routed} · 409 |

Errors are {"error": <a sentence>, "code"}: 404 thread_not_found, 409 approval_pending, steer_unsupported,
thread_full, thread_archived, steer_attachments, idempotency_conflict, 503 when conversations is off.
"""

import re

from ..api_errors import APIError
from ..api_routes import Request, Response, Route, register
from . import service as threads_service
from .service import fingerprint
from .store import public_item

KEY = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
OFF = "Conversations aren't available right now. Restart Mobster, then try again."


def service_of(request: Request):
    service = threads_service.service_for(request.runtime)
    if service is None:
        raise APIError(OFF, 503, "unavailable")
    return service


def one(query, name, default=None):
    values = query.get(name) or []
    if len(values) > 1:
        raise ValueError(f"Give {name} once")
    return values[0] if values else default


def idempotency_key(request):
    values = request.headers.get_all("Idempotency-Key", []) if hasattr(request.headers, "get_all") else (
        [request.headers["Idempotency-Key"]] if request.headers.get("Idempotency-Key") else [])
    if not values:
        return None
    if len(values) > 1 or not KEY.fullmatch(values[0]):
        raise ValueError("Invalid Idempotency-Key")
    return values[0]


def list_threads(request):
    service = service_of(request)
    query = request.query
    limit = one(query, "limit", "50")
    if not str(limit).isdigit() or not 1 <= int(limit) <= 100:
        raise ValueError("limit is 1 to 100")
    before = one(query, "before")
    if before is not None:
        try:
            before = float(before)
        except ValueError:
            raise ValueError("before is an updatedAt from a thread (milliseconds)") from None
    archived = one(query, "archived", "false")
    if archived not in ("true", "false", "all"):
        raise ValueError("archived is true, false or all")
    text = one(query, "q")
    if text is not None and len(text) > 200:
        raise ValueError("A search is at most 200 characters")
    threads, cursor = service.store.list(limit=int(limit), before=before, device=one(query, "device"),
                                         query=(text or "").strip() or None,
                                         archived={"true": True, "false": False}.get(archived, "all"))
    return Response.json({"threads": threads, "next": cursor})


def create_thread(request):
    service = service_of(request)
    body = request.body or {}
    if set(body) - {"title", "device", "message"}:
        raise ValueError("A new conversation takes title, device and message")
    title, device, message = body.get("title"), body.get("device"), body.get("message")
    if title is not None and (not isinstance(title, str) or not 1 <= len(" ".join(title.split())) <= 80):
        raise ValueError("A title is 1 to 80 characters")
    if message is not None and not isinstance(message, dict):
        raise ValueError("message is a message body: {text, ...}")
    key = idempotency_key(request)
    store_key = f"create:{key}" if key else None
    with service.lock:
        if store_key:
            saved = service.store.request(store_key)
            if saved is not None:
                if saved[1] != fingerprint(body):
                    raise APIError("This request key belongs to a different conversation", 409,
                                   "idempotency_conflict")
                thread = service.store.get(saved[2]["response"]["thread"]["id"])
                if thread is None:
                    raise APIError("That conversation was deleted; it was not started again", 410, "thread_expired")
                return Response.json({**saved[2]["response"], "thread": thread, "replayed": True}, 200)
        if message is not None and "device" in message and device is not None and message["device"] != device:
            raise ValueError("Name the device once")
        first = (message or {}).get("text")
        thread = service.create_thread(title or (first if isinstance(first, str) else None),
                                       device if device is not None else (message or {}).get("device"))
        response = {"thread": thread}
        if message is not None:
            try:
                status, routed = service.message(thread["id"], {k: v for k, v in message.items() if k != "device"},
                                                 origin=request.origin, key=f"first-{key}" if key else None)
            except BaseException:
                service.store.delete_thread(thread["id"])  # a first message that couldn't start leaves nothing
                service.published_thread({"id": thread["id"]}, "thread_deleted")
                raise
            response.update(item=routed.get("item"), run=routed.get("run"))
            # A first message that only asks Mobster to remember something starts no task: say so, with the offer.
            response.update({k: routed[k] for k in ("routed", "proposals") if k in routed})
            response["thread"] = service.store.get(thread["id"]) or thread
        if store_key:
            service.store.remember(store_key, thread["id"], fingerprint(body), {"status": 201, "response": response})
    return Response.json(response, 201)


def get_thread(request):
    service = service_of(request)
    thread = service.thread(request.params["id"])
    after = one(request.query, "after")
    if after is not None and not re.fullmatch(r"-?\d{1,9}", after):
        raise ValueError("after is an item's seq")
    items = service.store.items(thread["id"], after=int(after) if after is not None else None)
    runs = {}
    runtime = request.runtime
    for item in items:
        run_id = item.get("runId")
        if run_id and run_id not in runs:
            run = runtime.runs.get(run_id)
            if run is not None:
                runs[run_id] = run.public(include_events=False)
    return Response.json({"thread": thread, "items": [public_item(i) for i in items], "runs": runs})


def edit_thread(request):
    service = service_of(request)
    body = request.body or {}
    if not body or set(body) - {"title", "archived", "pinned"}:
        raise ValueError("Change a conversation's title, archived or pinned")
    return Response.json({"thread": service.edit_thread(request.params["id"], body)})


def delete_thread(request):
    service = service_of(request)
    return Response.json(service.delete_thread(request.params["id"], one(request.query, "runs", "delete")))


def post_message(request):
    service = service_of(request)
    status, response = service.message(request.params["id"], request.body or {}, origin=request.origin,
                                       key=idempotency_key(request))
    if response.get("replayed"):
        status = 200
    return Response.json(response, status)


ROUTES = (
    Route("threads", "GET", "/api/threads", list_threads, body="none"),
    Route("threads", "POST", "/api/threads", create_thread, idempotency=True),
    Route("threads", "GET", "/api/threads/{id}", get_thread, body="none"),
    Route("threads", "POST", "/api/threads/{id}", edit_thread),
    Route("threads", "DELETE", "/api/threads/{id}", delete_thread, body="none"),
    Route("threads", "POST", "/api/threads/{id}/messages", post_message, idempotency=True),
)


def register_routes():
    for route in ROUTES:
        register(route)
