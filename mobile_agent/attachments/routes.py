"""The files track's HTTP routes (seam S3; documented in docs/files.md).

| Route | Answer |
|---|---|
| ``POST /api/attachments?name=&thread=`` | raw body (≤ 25 MB) → 201 ``{attachment}``; 413, 415 |
| ``GET /api/attachments/{id}`` | ``{attachment}`` |
| ``GET /api/attachments/{id}/content``, ``/thumb`` | the bytes (no-store; ``?token=`` works, for ``<img>``) |
| ``DELETE /api/attachments/{id}`` | ``{deleted: true}`` |
| ``GET /api/phone/files?device=&app=`` | ``{device, apps:[{bundleId,name}], files?:[{name,bytes,modifiedAt,folder}]}`` |
| ``POST /api/phone/files/put`` | ``{device?, attachmentId, destination}`` + Idempotency-Key → ``{status:"copied", name, path, destination}`` |
| ``POST /api/phone/files/get`` | ``{device?, app, name, threadId?}`` → 201 ``{attachment}`` |

``put`` is the user's own click in the Mac app ("Put on iPhone…"): with an app session (the Mac app's sidecar) it
needs ``X-Mobster-App-Session`` (the core answers 403 app_only otherwise), so a process holding only the token file
can't put files on the phone. Everything about the phone goes over USB only (409 ``needs_cable`` on Wi-Fi).
"""

import threading
import time
from pathlib import Path
import tempfile

from ..api_errors import APIError
from ..api_routes import Response, Route
from ..phone_io import PhoneIOError
from . import service
from .store import FILE_MAX, ID, StoreError

DESTINATION_APP = "app:"
_idempotent = {}
_idempotent_lock = threading.Lock()
IDEMPOTENT_TTL = 600


def _store(request):
    store = service.store_for(request.runtime)
    if store is None:
        raise APIError("Attaching files needs Mobster's data folder. Run Mobster with its task history on.", 503,
                       "files_unavailable")
    return store


def _found(store, attachment_id):
    found = store.get(attachment_id)
    if found is None:
        raise APIError("That file isn't here any more. Attach it again.", 404, "attachment_not_found")
    return found


def _refuse(error):
    if isinstance(error, StoreError):
        return Response.error(str(error), error.status, error.code)
    raise error


def _query(request, name):
    values = request.query.get(name) or []
    if len(values) > 1:
        raise ValueError(f"Supply {name} once")
    return values[0] if values else None


def upload(request):
    store = _store(request)
    name = (_query(request, "name") or "").strip()
    if len(name) > 1024:
        raise ValueError("That file name is too long")
    thread = _query(request, "thread")
    if thread is not None and not ID.fullmatch(thread):
        raise ValueError("thread must be a conversation id")
    try:
        attachment = store.create(request.body or b"", name or "file", thread_id=thread)
    except StoreError as error:
        return _refuse(error)
    return Response.json({"attachment": attachment.public()}, 201)


def metadata(request):
    store = _store(request)
    return Response.json({"attachment": _found(store, request.params["id"]).public()})


def content(request):
    store = _store(request)
    attachment = _found(store, request.params["id"])
    path = store.path(attachment, "original")
    if path is None:
        raise APIError("That file's content is gone. Attach it again.", 404, "attachment_not_found")
    mime = attachment.data.get("mime") or "application/octet-stream"
    if attachment.kind in ("text", "csv"):
        mime = "text/plain; charset=utf-8"  # never served as HTML, JSON or a script
    return Response.bytes(path.read_bytes(), mime)


def thumb(request):
    store = _store(request)
    attachment = _found(store, request.params["id"])
    path = store.path(attachment, "thumb")
    if path is None:
        raise APIError("This file has no thumbnail.", 404, "no_thumbnail")
    return Response.bytes(path.read_bytes(), "image/jpeg")


def delete(request):
    store = _store(request)
    if not store.delete(request.params["id"]):
        raise APIError("That file isn't here any more.", 404, "attachment_not_found")
    return Response.json({"deleted": True})


# -- the phone's files -------------------------------------------------------------------------------------------

def _phone(request, device):
    """(fleet phone record, PhoneFiles) for ``device`` (None: the default device)."""
    if device is not None and (not isinstance(device, str) or not 0 < len(device) <= 128):
        raise ValueError("device must be a device id or name")
    record = service.device_record(request.runtime, device)
    try:
        return record, service.phone_files(record)
    except PhoneIOError as error:
        raise _phone_error(error) from None


def _phone_error(error):
    status = {"needs_cable": 409, "no_file_sharing": 409, "no_device": 409, "locked": 409, "not_found": 404,
              "tools": 503, "usage": 400}.get(error.code, 502)
    message = str(error) + (f" {error.fix}" if error.fix else "")
    return APIError(message, status, error.code)


def phone_list(request):
    from .agent import file_sharing_apps
    record, phone = _phone(request, _query(request, "device"))
    app = _query(request, "app")
    try:
        out = {"device": {"id": record["id"], "name": record["name"], "kind": record["kind"]},
               "apps": file_sharing_apps(phone)}
        if app:
            out["files"] = phone.ls(app)
    except PhoneIOError as error:
        raise _phone_error(error) from None
    return Response.json(out)


def _remember(key):
    now = time.monotonic()
    with _idempotent_lock:
        for stale in [k for k, (at, _) in _idempotent.items() if now - at > IDEMPOTENT_TTL]:
            _idempotent.pop(stale, None)
        return _idempotent.get(key, (None, None))[1]


def phone_put(request):
    body = request.body or {}
    unknown = set(body) - {"device", "attachmentId", "destination"}
    if unknown:
        raise ValueError("Unsupported field: " + ", ".join(sorted(unknown)))
    key = request.headers.get("Idempotency-Key")
    if key is not None and not 0 < len(key) <= 200:
        raise ValueError("Idempotency-Key is 1 to 200 characters")
    fingerprint = (key, body.get("device"), body.get("attachmentId"), body.get("destination"))
    if key and (earlier := _remember(key)) is not None:
        if earlier[0] != fingerprint:
            raise APIError("This Idempotency-Key was used for another copy.", 409, "idempotency_conflict")
        return Response.json(earlier[1])
    store = _store(request)
    attachment = _found(store, body.get("attachmentId") if isinstance(body.get("attachmentId"), str) else "")
    destination = body.get("destination")
    if not isinstance(destination, str) or not (destination == "clipboard" or destination.startswith(DESTINATION_APP)):
        raise ValueError("destination is clipboard or app:<bundle id>")
    device = body.get("device")
    if destination == "clipboard":
        result = _put_clipboard(request, store, attachment, device)
    else:
        bundle = destination[len(DESTINATION_APP):]
        record, phone = _phone(request, device)
        original = store.path(attachment, "original")
        if original is None:
            raise APIError("That file's content is gone. Attach it again.", 404, "attachment_not_found")
        try:
            copied = phone.put(bundle, original, name=attachment.name)
        except PhoneIOError as error:
            raise _phone_error(error) from None
        result = {"status": "copied", "name": copied["name"], "path": copied["path"], "destination": destination,
                  "device": record["id"]}
    if key:
        with _idempotent_lock:
            _idempotent[key] = (time.monotonic(), (fingerprint, result))
    return Response.json(result)


def _put_clipboard(request, store, attachment, device):
    """A text file onto the phone's clipboard through WebDriverAgent (any device), never while a task runs on it."""
    import json
    import urllib.request
    from ..phone_io.clipboard import set_text
    if attachment.kind not in ("text", "csv"):
        raise APIError("Only a text file goes on the clipboard.", 400, "not_text")
    runtime = request.runtime
    phone = runtime.resolve_device(device)
    if runtime.device_busy(phone.id):
        raise APIError(f"{phone.name or 'The iPhone'} is busy with a task. Try again when it ends.", 409,
                       "device_busy")
    url = (phone.wda_url or "").rstrip("/")
    if not url:
        raise APIError(f"{phone.name or 'The device'} isn't set up yet.", 409, "no_device")

    def call(method, path, body=None, timeout=15):
        session = phone.wda_session()
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{url}/session/{session}{path}", data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            answer = json.load(response)
        value = answer.get("value") if isinstance(answer, dict) else None
        if isinstance(value, dict) and value.get("error"):
            raise PhoneIOError("WebDriverAgent refused the request.", "failed", "Keep the iPhone unlocked.")
        return value

    text = store.whole_text(attachment)  # the file itself, never the excerpt the agent reads
    if text is None:
        raise APIError("That file's content is gone. Attach it again.", 404, "attachment_not_found")
    try:
        set_text(call, text)
    except PhoneIOError as error:
        raise _phone_error(error) from None
    except OSError:
        raise APIError(f"{phone.name or 'The iPhone'} didn't answer. Keep it unlocked and connected, then try again.",
                       502, "phone_unreachable") from None
    return {"status": "copied", "name": attachment.name, "path": None, "destination": "clipboard",
            "device": phone.id}


def phone_get(request):
    from ..harness_api import post_thread_item
    body = request.body or {}
    unknown = set(body) - {"device", "app", "name", "threadId"}
    if unknown:
        raise ValueError("Unsupported field: " + ", ".join(sorted(unknown)))
    app, name, thread = body.get("app"), body.get("name"), body.get("threadId")
    if not isinstance(app, str) or not isinstance(name, str) or not name:
        raise ValueError("Name the app (its bundle ID) and the file")
    if thread is not None and (not isinstance(thread, str) or not ID.fullmatch(thread)):
        raise ValueError("threadId must be a conversation id")
    store = _store(request)
    record, phone = _phone(request, body.get("device"))
    with tempfile.TemporaryDirectory(prefix="mobster-phone-file-") as folder:
        try:
            saved = phone.get(app, name, Path(folder))
        except PhoneIOError as error:
            raise _phone_error(error) from None
        if saved.stat().st_size > FILE_MAX:
            raise APIError("Mobster attaches files of 25 MB or less.", 413, "too_large")
        try:
            attachment = store.create(saved.read_bytes(), saved.name, thread_id=thread, origin="phone")
        except StoreError as error:
            return _refuse(error)
    if thread:
        post_thread_item(thread, {"kind": "file_saved", "attachment": attachment.ref(), "app": app,
                                  "device": record["name"]})
    return Response.json({"attachment": attachment.public()}, 201)


ROUTES = (
    Route("files", "POST", "/api/attachments", upload, body="raw", max_bytes=FILE_MAX),
    Route("files", "GET", "/api/attachments/{id}", metadata, body="none"),
    Route("files", "GET", "/api/attachments/{id}/content", content, body="none"),
    Route("files", "GET", "/api/attachments/{id}/thumb", thumb, body="none"),
    Route("files", "DELETE", "/api/attachments/{id}", delete, body="none"),
    Route("files", "GET", "/api/phone/files", phone_list, body="none"),
    Route("files", "POST", "/api/phone/files/put", phone_put, idempotency=True),
    Route("files", "POST", "/api/phone/files/get", phone_get),
)
