"""Client approval links: your client approves a queued post from a link, on any device, with no account."""

import base64
import datetime
import hashlib
import io
import logging
import re
import secrets
import subprocess
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path

from ..api_errors import APIError
from .store import EDITABLE, QueueError, fingerprint, now_ms, store_for

log = logging.getLogger("mobster.queue")

LINK_DAYS = 7
DAY_MS = 86400 * 1000
NOTE_CHARS = 1000
MAX_LINKS = 500                  # the relay's LIMITS.approvalsPerMac
PAGE_BYTES = 700_000             # the photos on one page, under the relay's 1 MB message once in base64
HASH = re.compile(r"[0-9a-f]{64}")
APPS = {"com.burbn.instagram": "instagram", "com.zhiliaoapp.musically": "tiktok"}
REMOTE_OFF = ("Turn on Mobster Remote first, in Settings › Remote. A client approval link is Remote's address for "
              "this Mac.")


def state_of(item, stamp=None):
    """The client's say on the post as it is now"""
    link = item.get("clientApproval")
    if not link:
        return None
    if link["revokedAt"] is not None:
        return "revoked"
    decision = link["decision"]
    if decision is not None and decision["fingerprint"] == fingerprint(item):
        return decision["state"]
    return "expired" if (stamp or now_ms()) >= link["expiresAt"] else "waiting"


def holds(item):
    """True while the post waits for its client: it was sent for approval and isn't approved as it is now."""
    return state_of(item) not in (None, "approved")


def public(item):
    """What the Mac app shows of the item's link (never its token)."""
    link = item.get("clientApproval")
    if not link:
        return None
    decision = link["decision"]
    return {"state": state_of(item), "createdAt": link["createdAt"], "expiresAt": link["expiresAt"],
            "decision": None if decision is None else {k: decision[k] for k in ("state", "note", "at")},
            "changed": decision is not None and decision["fingerprint"] != fingerprint(item)}


def _live(link, stamp):
    return link is not None and link["revokedAt"] is None and stamp < link["expiresAt"]


def _remote(runtime):
    try:
        from ..remote import service as remote_service
    except ImportError:
        return None
    return remote_service.service_for(runtime)


def _tell_remote(runtime):
    remote = _remote(runtime)
    if remote is not None:
        try:
            remote.approvals_changed(live(runtime))
        except Exception:  # noqa: BLE001 -- the next hello carries the list
            log.warning("client links: couldn't tell Remote about a change")


def live(runtime):
    """[{h, exp}]: the links that work now, newest first, for the relay."""
    stamp = now_ms()
    links = [item["clientApproval"] for queue in store_for(runtime).all() for item in queue["items"]
             if _live(item.get("clientApproval"), stamp)]
    links.sort(key=lambda link: link["createdAt"], reverse=True)
    return [{"h": link["hash"], "exp": link["expiresAt"]} for link in links[:MAX_LINKS]]


def send(runtime, queue_id, item_id, profiles):
    """(item, link): the post's working link, or a new one when it has none (never sent, expired or revoked)."""
    remote = _remote(runtime)
    base = remote.address("approve") if remote is not None else None
    if not base:
        raise APIError(REMOTE_OFF, 409, "remote_off")
    stamp = now_ms()
    token = secrets.token_urlsafe(32)

    def change(item):
        if item["profile"] not in {profile["id"] for profile in profiles}:
            raise QueueError("Choose a profile for this post first.", "no_profile", 409)
        if item["state"] not in EDITABLE:
            raise QueueError("This post is going out, went out, or may have.", "locked", 409)
        if _live(item.get("clientApproval"), stamp):
            return None
        item["clientApproval"] = {"token": token, "hash": hashlib.sha256(token.encode()).hexdigest(),
                                  "createdAt": stamp, "expiresAt": stamp + LINK_DAYS * DAY_MS, "revokedAt": None,
                                  "decision": None}
        return "client_link"

    item = store_for(runtime).change_item(queue_id, item_id, change)
    if item["clientApproval"]["token"] == token:
        _tell_remote(runtime)
    return item, f"{base}/{item['clientApproval']['token']}"


def revoke(runtime, queue_id, item_id):
    """The link stops working now. The post waits for a new link's OK."""
    def change(item):
        link = item.get("clientApproval")
        if link is None or link["revokedAt"] is not None:
            raise QueueError("This post has no approval link.", "no_link", 404)
        link["revokedAt"] = now_ms()
        return "client_revoked"

    item = store_for(runtime).change_item(queue_id, item_id, change)
    _tell_remote(runtime)
    return item


def answer(runtime, link_hash, decide=None):
    """One view of the page (``decide`` None) or one answer from it, as the relay passed it on"""
    if not isinstance(link_hash, str) or not HASH.fullmatch(link_hash):
        return {"status": 404, "error": "not_found"}
    store = store_for(runtime)
    found = next(((queue["id"], item) for queue in store.all() for item in queue["items"]
                  if (item.get("clientApproval") or {}).get("hash") == link_hash), None)
    if found is None or found[1]["clientApproval"]["revokedAt"] is not None:
        return {"status": 404, "error": "not_found"}
    queue_id, item = found
    if now_ms() >= item["clientApproval"]["expiresAt"]:
        return {"status": 410, "error": "expired"}
    from ..accounts.store import store_for as profiles_for
    profile = next((p for p in profiles_for(runtime).all() if p["id"] == item["profile"]), None)
    if profile is None:
        return {"status": 409, "error": "no_profile"}
    if decide is not None:
        try:
            item = store.change_item(queue_id, item["id"], _decision(decide))
        except QueueError as error:
            out = {"status": error.status, "error": error.code}
            return out | {"view": view(item, profile)} if error.code == "changed" else out
    return {"status": 200, "view": view(item, profile)}


def _decision(decide):
    """The change one answer makes, checked against the post as it is when the answer lands."""
    choice = decide.get("decision") if isinstance(decide, dict) else None
    if choice not in ("approve", "changes"):
        raise QueueError("Approve the post, or ask for changes.", "invalid", 422)
    state = "approved" if choice == "approve" else "changes"
    note, version = decide.get("note", ""), decide.get("v")
    if not isinstance(note, str) or len(note) > NOTE_CHARS:
        raise QueueError(f"A note is at most {NOTE_CHARS:,} characters.", "invalid", 422)
    note = note.strip()
    if state == "changes" and not note:
        raise QueueError("Say what should change.", "invalid", 422)

    def change(item):
        if item["state"] not in EDITABLE:
            raise QueueError("This post is already going out, so it can't change here.", "locked", 409)
        current = fingerprint(item)
        if version != current[:16]:
            raise QueueError("This post changed since you opened it. Look at it again.", "changed", 409)
        item["clientApproval"]["decision"] = {"state": state, "note": note if state == "changes" else "",
                                              "at": now_ms(), "fingerprint": current}
        return "client_" + state
    return change


def view(item, profile):
    """Only what the client's page shows"""
    link = item["clientApproval"]
    state = state_of(item)
    decision = link["decision"]
    return {"app": APPS.get(profile["app"]["bundleId"], "other"), "appName": profile["app"]["name"],
            "handle": profile["handle"], "when": _when(item["scheduledAt"]), "day": _day(item["scheduledAt"]),
            "caption": item["caption"], "hashtags": item["hashtags"], "sound": item["sound"],
            "format": item["format"], "media": stills(item), "v": fingerprint(item)[:16],
            "expires": _day(link["expiresAt"]),
            "status": "open" if item["state"] in EDITABLE else "posted" if item["state"] == "posted" else "going",
            "decision": {"state": state, "note": decision["note"], "at": _when(decision["at"])}
            if state in ("approved", "changes") else None,
            "changed": state == "waiting" and decision is not None}


def _local(ms):
    return datetime.datetime.fromtimestamp(ms / 1000).astimezone()


def _when(ms):
    return None if ms is None else _local(ms).strftime("%a, %b %-d at %-I:%M %p %Z")


def _day(ms):
    return _local(ms if ms is not None else now_ms()).strftime("%B %-d")


_stills = OrderedDict()           # (sha256, width) → (JPEG bytes, width / height): a second view reads no file
_stills_lock = threading.Lock()
STILLS_KEPT = 64


def stills(item):
    """[{kind, src, ratio}]: each photo or video's first frame as a JPEG data: URI, src None when it can't fit."""
    out, used = [], 0
    count = len(item["media"])
    for index, media in enumerate(item["media"]):
        got = _still(media, 1080 if index == 0 else 720 if count <= 4 else 480)
        src = None
        if got is not None and used + len(got[0]) <= PAGE_BYTES:
            used += len(got[0])
            src = "data:image/jpeg;base64," + base64.b64encode(got[0]).decode()
        out.append({"kind": media["kind"], "src": src, "ratio": round(got[1], 4) if got else None})
    return out


def _still(media, width):
    key = (media["sha256"], width)
    with _stills_lock:
        if key in _stills:
            _stills.move_to_end(key)
            return _stills[key]
    from PIL import Image, ImageOps
    path = Path(media["path"])
    try:
        with tempfile.TemporaryDirectory() as folder:
            source = path
            if media["kind"] == "video" or path.suffix.lower() in (".heic", ".heif"):
                source = _quick_look(path, width, Path(folder))
            if source is None:
                return None
            with Image.open(source) as opened:
                image = ImageOps.exif_transpose(opened)
                image.thumbnail((width, width * 2))
                out = io.BytesIO()
                image.convert("RGB").save(out, "JPEG", quality=78, optimize=True)
                got = (out.getvalue(), image.width / image.height)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    with _stills_lock:
        _stills[key] = got
        while len(_stills) > STILLS_KEPT:
            _stills.popitem(last=False)
    return got


def _quick_look(path, width, folder):
    """macOS Quick Look's still of a video's first frame or a HEIC photo (Pillow reads neither), as a PNG."""
    subprocess.run(["qlmanage", "-t", "-s", str(width), "-o", str(folder), str(path)], capture_output=True,
                   timeout=20, check=False)
    return next(folder.glob("*.png"), None)
