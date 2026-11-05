"""The files track's HTTP routes (seam S3; documented in docs/files.mdx)."""

import tempfile
import uuid
from pathlib import Path

from ..api_errors import APIError
from ..api_routes import Response, Route
from ..phone_io import PhoneIOError
from . import service
from .store import FILE_MAX, ID, StoreError

DESTINATION_APP = "app:"


class _Copied(Exception):
    def __init__(self, result):
        self.result = result


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


def phone_put(request):
    # The user's own click in the Mac app: api_http.APP_ONLY_ROUTES needs its session, so a process holding only the
    # token file can't put files on the phone.
    body = request.body or {}
    unknown = set(body) - {"device", "attachmentId", "destination"}
    if unknown:
        raise ValueError("Unsupported field: " + ", ".join(sorted(unknown)))
    key = request.headers.get("Idempotency-Key")
    if key is not None and not 0 < len(key) <= 200:
        raise ValueError("Idempotency-Key is 1 to 200 characters")
    store = _store(request)
    attachment = _found(store, body.get("attachmentId") if isinstance(body.get("attachmentId"), str) else "")
    destination = body.get("destination")
    if not isinstance(destination, str) or not (destination in ("clipboard", "photos")
                                                or destination.startswith(DESTINATION_APP)):
        raise ValueError("destination is clipboard, photos or app:<bundle id>")
    device = body.get("device")
    if device is not None and (not isinstance(device, str) or not 0 < len(device) <= 128):
        raise ValueError("device must be a device id or name")
    runtime = request.runtime
    phone = runtime.resolve_device(device)
    key = key or uuid.uuid4().hex
    fingerprint = {"device": phone.id, "attachmentId": attachment.id, "destination": destination}
    earlier = store.copy_result(key, fingerprint)
    if earlier is not None:
        return Response.json(earlier)
    reserved = False
    def before_copy():
        nonlocal reserved
        if reserved:
            return
        previous = store.reserve_copy(key, fingerprint)
        if previous is not None:
            raise _Copied(previous)
        reserved = True

    from ..journal import JournalError, Lease
    with runtime.lock:
        if getattr(runtime, "closing", False):
            raise APIError("The service is shutting down", 503, "shutting_down")
        if runtime.run_on(phone) is not None:
            raise APIError(f"{phone.name or 'The iPhone'} is busy with a task. Try again when it ends.", 409,
                           "device_busy")
        url = (runtime.config.wda_url if getattr(phone, "primary", False) else phone.wda_url) or ""
        if not url:
            raise APIError(f"{phone.name or 'The device'} isn't set up yet.", 409, "no_device")
        try:
            lease = Lease.device(url.rstrip("/"))
        except JournalError:
            raise APIError("Your iPhone is busy. Wait for it to finish, then try again.", 409, "device_busy") from None
    try:
        earlier = store.copy_result(key, fingerprint)
        if earlier is not None:
            return Response.json(earlier)
        if destination == "clipboard":
            result = _put_clipboard(request, store, attachment, phone.id, before_copy)
        elif destination == "photos":
            result = _put_photos(request, store, attachment, phone.id, before_copy)
        else:
            bundle = destination[len(DESTINATION_APP):]
            record, files = _phone(request, phone.id)
            original = store.path(attachment, "original")
            if original is None:
                raise APIError("That file's content is gone. Attach it again.", 404, "attachment_not_found")
            try:
                copied = files.put(bundle, original, name=attachment.name, before_copy=before_copy)
            except PhoneIOError as error:
                raise _phone_error(error) from None
            result = {"status": "copied", "name": copied["name"], "path": copied["path"], "destination": destination,
                      "device": record["id"]}
        store.finish_copy(key, result)
        return Response.json(result)
    except _Copied as previous:
        return Response.json(previous.result)
    finally:
        lease.close()


def _wda(request, device):
    """(fleet phone, call(method, path, body, timeout)) for a device's WebDriverAgent session, refused while a task runs
    on it."""
    import json
    import urllib.request

    from ..phone_io import checked_request
    runtime = request.runtime
    phone = runtime.resolve_device(device)
    if runtime.device_busy(phone.id):
        raise APIError(f"{phone.name or 'The iPhone'} is busy with a task. Try again when it ends.", 409,
                       "device_busy")
    url = (phone.wda_url or "").rstrip("/")
    if not url:
        raise APIError(f"{phone.name or 'The device'} isn't set up yet.", 409, "no_device")

    def send(method, path, body, timeout):
        session = phone.wda_session()
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{url}/session/{session}{path}", data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.load(response)
    return phone, checked_request(send)


def _put_clipboard(request, store, attachment, device, before_copy):
    """A text file onto the phone's clipboard through WebDriverAgent (any device), never while a task runs on it."""
    from ..phone_io.clipboard import set_text
    if attachment.kind not in ("text", "csv"):
        raise APIError("Only a text file goes on the clipboard.", 400, "not_text")
    phone, call = _wda(request, device)
    text = store.whole_text(attachment)  # the file itself, never the excerpt the agent reads
    if text is None:
        raise APIError("That file's content is gone. Attach it again.", 404, "attachment_not_found")
    try:
        set_text(_copy_call(call, before_copy), text)
    except PhoneIOError as error:
        raise _phone_error(error) from None
    except OSError:
        raise APIError(f"{phone.name or 'The iPhone'} didn't answer. Keep it unlocked and connected, then try again.",
                       502, "phone_unreachable") from None
    return {"status": "copied", "name": attachment.name, "path": None, "destination": "clipboard",
            "device": phone.id}


def _put_photos(request, store, attachment, device, before_copy):
    """A picture or video into the phone's Photos library (phone_io.photos.send: Mobster's runner saves it), never while
    a task runs on the phone."""
    from ..phone_io.photos import send
    if attachment.kind != "image":
        raise APIError("Only a photo or a video goes into Photos.", 400, "not_media")
    original = store.path(attachment, "original")
    if original is None:
        raise APIError("That file's content is gone. Attach it again.", 404, "attachment_not_found")
    phone, call = _wda(request, device)
    try:
        added = send(call, original, name=phone.name or "the iPhone",
                     activate=lambda bundle: call("POST", "/wda/apps/activate", {"bundleId": bundle}, 15),
                     before_send=before_copy)
    except PhoneIOError as error:
        status = {"needs_rebuild": 409, "photos_denied": 409, "photos_restricted": 409,
                  "photos_not_determined": 409, "usage": 400}.get(error.code, 502)
        raise APIError(str(error) + (f" {error.fix}" if error.fix else ""), status, error.code) from None
    except OSError:
        raise APIError(f"{phone.name or 'The iPhone'} didn't answer. Keep it unlocked and connected, then try again.",
                       502, "phone_unreachable") from None
    return {"status": "copied", "name": attachment.name, "path": None, "destination": "photos", "device": phone.id,
            "kind": added["kind"]}


def _copy_call(call, before_copy):
    """Journal intent after local validation and read-only preflight, before the first WDA mutation."""
    def dispatch(method, path, body=None, timeout=15):
        if method != "GET":
            before_copy()
        return call(method, path, body, timeout)
    return dispatch


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
