"""The content queue's and transfers' HTTP routes (docs/content-queue.mdx › HTTP API)."""

import os
import re
import shutil
import uuid
from pathlib import Path

from .. import phone_ready
from ..api_errors import APIError
from ..api_routes import Response, Route
from ..phone_io import PhoneIOError
from . import client, importing
from .store import ID, QueueError, store_for

ITEM = r"(?P<item>[a-f0-9]{12})"
PIECE_MAX = 32 * 1024 * 1024
MEDIA_MAX = 4 * 1024 ** 3
UPLOAD = re.compile(r"[a-f0-9]{16}")
MEDIA_DIR = "queue-media"


def _store(request):
    return store_for(request.runtime)


def _profiles(request):
    from ..accounts.store import store_for as profiles_for
    return profiles_for(request.runtime).all()


def _refuse(error):
    return Response.error(str(error), error.status, error.code)


def _item_public(item):
    return {**item, "clientApproval": client.public(item)}


def _queue_public(queue):
    counts = {}
    for item in queue["items"]:
        counts[item["state"]] = counts.get(item["state"], 0) + 1
    upcoming = [i["scheduledAt"] for i in queue["items"] if i["state"] == "ready" and i.get("scheduledAt")]
    return {**queue, "items": [_item_public(item) for item in queue["items"]], "counts": counts,
            "next": min(upcoming) if upcoming else None,
            "batchCovers": sorted((queue.get("batch") or {}).get("items") or {})}


def _summary(queue):
    public = _queue_public(queue)
    return {key: public[key] for key in ("id", "name", "approval", "armed", "counts", "next", "createdAt",
                                         "updatedAt")} | {"total": len(queue["items"])}


def _query(request, name):
    values = request.query.get(name) or []
    return values[0] if values else None


def _guard(handler):
    def wrapped(request):
        try:
            return handler(request)
        except QueueError as error:
            return _refuse(error)
    return wrapped


@_guard
def list_queues(request):
    return Response.json({"queues": [_summary(q) for q in _store(request).all()]})


@_guard
def create_queue(request):
    body = request.body or {}
    if set(body) - {"name", "approval"}:
        raise ValueError("A new queue takes name and approval")
    queue = _store(request).create(body.get("name"), body.get("approval") or "each")
    return Response.json({"queue": _queue_public(queue)}, 201)


@_guard
def get_queue(request):
    store = _store(request)
    queue = store.get(request.params["id"])
    return Response.json({"queue": _queue_public(queue), "history": store.history(queue["id"], 200)})


@_guard
def update_queue(request):
    queue = _store(request).update_queue(request.params["id"], request.body or {}, _profiles(request))
    return Response.json({"queue": _queue_public(queue)})


@_guard
def delete_queue(request):
    _store(request).delete_queue(request.params["id"])
    return Response.json({"deleted": True})


@_guard
def add_items(request):
    body = request.body or {}
    items = body.get("items")
    if not isinstance(items, list) or not items or not all(isinstance(i, dict) for i in items):
        raise ValueError("items is a list of posts")
    added = _store(request).add_items(request.params["id"], items, _profiles(request))
    return Response.json({"items": [_item_public(item) for item in added]}, 201)


@_guard
def edit_item(request):
    item = _store(request).edit_item(request.params["id"], request.params["item"], request.body or {},
                                     _profiles(request))
    return Response.json({"item": _item_public(item)})


@_guard
def delete_item(request):
    _store(request).remove_item(request.params["id"], request.params["item"])
    return Response.json({"deleted": True})


@_guard
def move_item(request):
    index = (request.body or {}).get("index")
    if isinstance(index, bool) or not isinstance(index, int):
        raise ValueError("index is a number")
    _store(request).move_item(request.params["id"], request.params["item"], index)
    return Response.json({"moved": True})


@_guard
def mark_item(request):
    item = _store(request).mark(request.params["id"], request.params["item"], (request.body or {}).get("outcome"))
    return Response.json({"item": _item_public(item)})


@_guard
def send_for_approval(request):
    if request.body:
        raise ValueError("Send for approval takes no fields")
    item, link = client.send(request.runtime, request.params["id"], request.params["item"], _profiles(request))
    return Response.json({"item": _item_public(item), "link": link})


@_guard
def revoke_approval(request):
    item = client.revoke(request.runtime, request.params["id"], request.params["item"])
    return Response.json({"item": _item_public(item)})


def _profile_scope(request):
    """``?profile=<id>``: a profile's Queue tab starts or stops that profile's posts alone."""
    profile = _query(request, "profile")
    if profile is not None and not ID.fullmatch(profile):
        raise ValueError("profile is a profile's id")
    return profile


@_guard
def start_queue(request):
    runner = getattr(request.runtime, "queue_runner", None)
    warnings = []
    if runner is not None and phone_ready.enabled():
        # refuses a phone without the Photos grant
        warnings = runner.preflight(_store(request).get(request.params["id"]))
    queue = _store(request).arm(request.params["id"], True, profile=_profile_scope(request))
    if runner is not None:
        runner.tick()
        runner.start()
    return Response.json({"queue": _queue_public(queue), **({"warnings": warnings} if warnings else {})})


@_guard
def stop_queue(request):
    queue = _store(request).arm(request.params["id"], False, profile=_profile_scope(request))
    return Response.json({"queue": _queue_public(queue)})


@_guard
def approve_batch(request):
    items = (request.body or {}).get("items")
    if not isinstance(items, list) or not all(isinstance(i, str) and ID.fullmatch(i) for i in items):
        raise ValueError("items is a list of post ids")
    queue = _store(request).approve_batch(request.params["id"], items)
    return Response.json({"queue": _queue_public(queue)})


def _zone_of(request):
    """A profile's own time zone (its pacing), for CSV times without an offset."""
    try:
        from ..accounts.pacing import pacing_for
        pacing = pacing_for(request.runtime)
    except Exception:  # noqa: BLE001
        return lambda profile: None
    return lambda profile: pacing.settings(profile).get("timeZone")


@_guard
def import_items(request):
    body = request.body or {}
    profiles = _profiles(request)
    if isinstance(body.get("csvPath"), str):
        source = Path(body["csvPath"]).expanduser()
        if source.suffix.lower() != ".csv" or not source.is_file() or source.stat().st_size > 2_000_000:
            raise QueueError("Choose a CSV file of at most 2 MB.")
        try:
            text = source.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            raise QueueError(f"{source.name} isn't UTF-8 text.") from None
        bodies = importing.from_csv(text, source.parent, profiles, _zone_of(request))
    elif isinstance(body.get("csv"), str):
        base = body.get("base")
        if not isinstance(base, str) or not Path(base).expanduser().is_dir():
            raise QueueError("Name the folder the CSV's media paths start from (base).")
        bodies = importing.from_csv(body["csv"], Path(base).expanduser(), profiles, _zone_of(request))
    elif isinstance(body.get("folder"), str):
        profile = body.get("profile")
        if profile is not None and profile not in {p["id"] for p in profiles}:
            raise QueueError("No profile has that id. Add it in Settings › Profiles.", "profile_not_found")
        bodies = importing.from_folder(body["folder"], profile)
    else:
        raise ValueError("Import takes csvPath, csv and base, or folder")
    added = _store(request).add_items(request.params["id"], bodies, profiles)
    return Response.json({"items": added}, 201)


@_guard
def report(request):
    queue = _store(request).get(request.params["id"])
    data, content_type, filename = importing.report(queue, _profiles(request), _query(request, "format") or "csv")
    return Response.bytes(data, content_type, filename=filename)


PREVIEW_MAX = 40 * 1024 * 1024
PREVIEW_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif",
                 "webp": "image/webp", "heic": "image/heic", "heif": "image/heic", "mov": "video/quicktime",
                 "mp4": "video/mp4", "m4v": "video/mp4"}


@_guard
def media_preview(request):
    """One of an item's photos or videos, for its preview in the Mac app (the person's own file, as queued)."""
    queue = _store(request).get(request.params["id"])
    item = next((i for i in queue["items"] if i["id"] == request.params["item"]), None)
    index = int(request.params["n"])
    if item is None or index >= len(item["media"]):
        raise QueueError("That photo isn't in this post.", "media_missing", 404)
    media = item["media"][index]
    path = Path(media["path"])
    if not path.is_file():
        raise QueueError(f"{media['name']} isn't on this Mac any more.", "media_missing", 404)
    if path.stat().st_size > PREVIEW_MAX:
        raise QueueError("That file is too large to preview.", "too_large", 413)
    kind = PREVIEW_TYPES.get(path.suffix.lower().lstrip("."), "application/octet-stream")
    return Response.bytes(path.read_bytes(), kind)


def _media_dir(request):
    return _store(request).data_dir / MEDIA_DIR


@_guard
def upload_media(request):
    """A photo or video from the Mac app's drop, in pieces"""
    name = os.path.basename((_query(request, "name") or "").strip())[:200]
    upload = _query(request, "upload") or uuid.uuid4().hex[:16]
    if not name or name.startswith("."):
        raise ValueError("name is the file's name")
    if not UPLOAD.fullmatch(upload):
        raise ValueError("upload is 16 hex characters")
    try:
        offset = int(_query(request, "offset") or 0)
    except ValueError:
        raise ValueError("offset is a number") from None
    folder = _media_dir(request) / upload
    folder.mkdir(parents=True, exist_ok=True)
    partial = folder / ".partial"
    have = partial.stat().st_size if partial.exists() else 0
    if offset == 0 and have:
        partial.unlink()
        have = 0
    if offset != have:
        return Response.error("That piece is out of order.", 409, "offset", bytes=have)
    data = request.body or b""
    if have + len(data) > MEDIA_MAX:
        shutil.rmtree(folder, ignore_errors=True)
        return Response.error("Mobster queues photos and videos up to 4 GB.", 413, "too_large")
    with open(partial, "ab") as stream:
        stream.write(data)
    if _query(request, "done") != "1":
        return Response.json({"upload": upload, "bytes": have + len(data)})
    from ..attachments.sniff import clean_name
    target = folder / clean_name(name)
    os.replace(partial, target)
    try:
        from .store import clean_media
        media = clean_media([str(target)])[0]
    except QueueError:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return Response.json({"media": media}, 201)


def _transfers(request):
    from ..phone_io.transfer import transfers_for
    return transfers_for(request.runtime)


def list_transfers(request):
    return Response.json({"transfers": [t.public() for t in _transfers(request).all()[:20]]})


def get_transfer(request):
    found = _transfers(request).get(request.params["id"])
    if found is None:
        raise APIError("That transfer isn't here any more.", 404, "transfer_not_found")
    return Response.json({"transfer": found.public()})


def start_transfer(request):
    from ..phone_io.transfer import Transfer, target
    body = request.body or {}
    known = {"direction", "what", "device", "profile", "via", "paths", "folder", "count", "since", "album", "app",
             "names", "container"}
    if set(body) - known:
        raise ValueError("Unsupported field: " + ", ".join(sorted(set(body) - known)))
    try:
        phone = target(request.runtime, device=body.get("device"), profile=body.get("profile"))
        transfer = Transfer(body.get("direction") or "to_phone", body.get("what") or "photos", phone,
                            via=body.get("via") or "auto", paths=list(body.get("paths") or ()),
                            folder=body.get("folder") or (str(Path.home() / "Downloads" / "Mobster")
                                                          if body.get("direction") == "to_mac" else None),
                            count=int(body.get("count") or 1), since=body.get("since"),
                            album=body.get("album", "Mobster") or None, app=body.get("app"),
                            names=list(body.get("names") or ()), container=bool(body.get("container")))
        _transfers(request).start(transfer)
    except PhoneIOError as error:
        status = 400 if error.code == "usage" else 409
        return Response.error(str(error) + (f" {error.fix}" if error.fix else ""), status, error.code)
    return Response.json({"transfer": transfer.public()}, 202)


ROUTES = (
    Route("queue", "GET", "/api/queue", list_queues, body="none"),
    Route("queue", "POST", "/api/queue", create_queue),
    Route("queue", "POST", "/api/queue/media", upload_media, body="raw", max_bytes=PIECE_MAX),
    Route("queue", "GET", "/api/queue/{id}", get_queue, body="none"),
    Route("queue", "POST", "/api/queue/{id}", update_queue),
    Route("queue", "DELETE", "/api/queue/{id}", delete_queue, body="none"),
    Route("queue", "POST", "/api/queue/{id}/items", add_items),
    Route("queue", "POST", f"/api/queue/{{id}}/items/{ITEM}", edit_item),
    Route("queue", "DELETE", f"/api/queue/{{id}}/items/{ITEM}", delete_item, body="none"),
    Route("queue", "POST", f"/api/queue/{{id}}/items/{ITEM}/move", move_item),
    Route("queue", "POST", f"/api/queue/{{id}}/items/{ITEM}/mark", mark_item),
    Route("queue", "POST", f"/api/queue/{{id}}/items/{ITEM}/client", send_for_approval),
    Route("queue", "DELETE", f"/api/queue/{{id}}/items/{ITEM}/client", revoke_approval, body="none"),
    Route("queue", "GET", f"/api/queue/{{id}}/items/{ITEM}/media/(?P<n>[0-9])", media_preview, body="none"),
    Route("queue", "POST", "/api/queue/{id}/start", start_queue, body="none"),
    Route("queue", "POST", "/api/queue/{id}/stop", stop_queue, body="none"),
    Route("queue", "POST", "/api/queue/{id}/approve", approve_batch),
    Route("queue", "POST", "/api/queue/{id}/import", import_items),
    Route("queue", "GET", "/api/queue/{id}/report", report, body="none"),
    Route("queue", "GET", "/api/transfers", list_transfers, body="none"),
    Route("queue", "POST", "/api/transfers", start_transfer),
    Route("queue", "GET", "/api/transfers/{id}", get_transfer, body="none"),
)
