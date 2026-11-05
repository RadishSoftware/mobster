"""Photos and videos between the Mac and the iPhone's Photos library.

**Mac to iPhone (``send``).** iOS has no way to add to Photos from a Mac: files copied into DCIM over AFC sit on disk
and Photos never shows them (discuss.appium.io/t/28882). So Mobster's own runner on the phone (the WebDriverAgent it
builds, patched: wda.patches.WDA_PHOTOS_COMMANDS) saves the file with PhotoKit, as any app that adds to Photos
does. The file goes over the runner's connection as base64 (at most 100 MB). The first time, iOS asks whether the
runner may use Photos; "Limit Access" is enough, since what an app adds joins its selection. A runner built before
this support answers 404: rebuild it once in Setup.

**iPhone to Mac (``pull``).** The newest photos, screenshots and screen recordings come from the camera roll's folder
(DCIM) through Apple's file service on the cable (AFC, the media root: ``afcclient`` without ``--documents``), newest
first, by count or since a time. With iCloud Photos' Optimize iPhone Storage on, older originals live in iCloud and
only what is on the phone comes back. Edits (.AAE sidecars) and folders are skipped; a HEIC can be converted to JPEG
on the Mac with ``sips``.

Who approves: the agent asks before it adds to Photos (PUT_FILE's destination ``photos``), the Mac app's drop on the
phone is the user's own gesture, ``mobster phone photos`` is the user's command, and MCP lists ``send_media`` only
with ``--allow-files`` and ``pull_media`` only with ``--allow-photos`` (the camera roll is the user's most private
folder).
"""

import base64
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from . import PhoneIOError

MAX_SEND_BYTES = 100 * 1024 * 1024    # a runner built before transfers: one piece
MAX_CHUNKED_BYTES = 4 * 1024 ** 3     # a runner built for transfers: in pieces
CHUNK_BYTES = 8 * 1024 * 1024
MAX_RECENT = 50
MAX_PULL = 20
MAX_PULL_BYTES = 500 * 1024 * 1024
AUTHORIZATION_WAIT_S = 60
AUTHORIZE_WAIT_S = 20   # Setup's "Allow": how long the helper waits for the person to answer iOS's question
PHOTO_TYPES = {"jpg": "photo", "jpeg": "photo", "heic": "photo", "heif": "photo", "png": "photo", "gif": "photo",
               "webp": "photo", "tif": "photo", "tiff": "photo", "dng": "photo",
               "mov": "video", "mp4": "video", "m4v": "video", "hevc": "video"}
# Magic numbers: a file is what its first bytes say, not its name.
SIGNATURES = ((b"\xff\xd8\xff", "jpg"), (b"\x89PNG\r\n\x1a\n", "png"), (b"GIF8", "gif"), (b"II*\x00", "tif"),
              (b"MM\x00*", "tif"))
MEDIA_ROOT = "/DCIM"
DCIM_FOLDER = re.compile(r"^\d{3}[A-Z_]{0,12}$")
PULLED = re.compile(r"\.(heic|heif|jpe?g|png|gif|mov|mp4|m4v|dng|webp)$", re.I)
NEEDS_REBUILD = ("Mobster's runner on {name} was built before it could add to Photos. Rebuild it once in Setup › "
                 "Runner (about 2 minutes), then try again.")
STATUS_WORDS = {
    "denied": "Photos access is off for Mobster on {name}. Turn it on in Settings › Privacy & Security › Photos › "
              "WebDriverAgentRunner-Runner (Limit Access is enough), then try again.",
    "restricted": "Photos is restricted on {name} (Screen Time or a profile), so Mobster can't add to it.",
    "not_determined": "{name} is asking whether Mobster may use Photos. Answer it on the iPhone (Limit Access is "
                      "enough), then try again.",
}


def sniff(path):
    """("photo" | "video", extension) for a Mac file, from its first bytes (and, for video and HEIC, its box)."""
    file = Path(path).expanduser()
    try:
        with open(file, "rb") as stream:
            head = stream.read(32)
    except OSError:
        raise PhoneIOError(f"There is no file at {file}.", "usage") from None
    for magic, ext in SIGNATURES:
        if head.startswith(magic):
            return "photo", ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "photo", "webp"
    if head[4:8] == b"ftyp":
        brand = head[8:12].decode("ascii", "replace").lower()
        if brand in ("heic", "heix", "hevc", "heim", "heis", "mif1", "msf1"):
            return "photo", "heic"
        if brand.startswith("qt"):
            return "video", "mov"
        return "video", "mp4"
    raise PhoneIOError(f"{file.name} isn't a photo or a video Photos takes (JPEG, HEIC, PNG, GIF, WebP, MOV or MP4).",
                       "usage")


def _runner_answer(request, method, path, body=None, timeout=15, *, name="the iPhone"):
    """``request``'s value, or the PhoneIOError a runner without the Photos routes deserves."""
    try:
        return request(method, path, body, timeout)
    except PhoneIOError as error:
        if error.code == "unknown_route":
            raise PhoneIOError(NEEDS_REBUILD.format(name=name), "needs_rebuild") from None
        raise
    except Exception as error:
        text = str(error)
        if "404" in text or "unknown command" in text.casefold() or "Unhandled endpoint" in text:
            raise PhoneIOError(NEEDS_REBUILD.format(name=name), "needs_rebuild") from None
        raise PhoneIOError(f"Mobster's runner didn't answer ({type(error).__name__}).", "failed",
                           "Keep the iPhone unlocked and connected, then try again.") from None


def status(request, name="the iPhone"):
    """{"status": authorized | limited | denied | restricted | not_determined, "bundleId": the runner's}."""
    value = _runner_answer(request, "GET", "/mobster/photos/status", None, 10, name=name)
    return value if isinstance(value, dict) else {"status": "not_determined", "bundleId": ""}


def authorize(request, *, name="the iPhone", activate=None):
    """Ask iOS to show its Photos question on the phone and wait AUTHORIZE_WAIT_S for the person to answer it there
    (``POST /mobster/photos/authorize``). It saves nothing, asks only while the status is ``not_determined``, never
    presses the grant and never resets an answer. ``activate(bundle)`` brings the helper forward so the question shows.
    Returns {"status", "asked"}; PhoneIOError ``needs_rebuild`` from a helper built without the route."""
    state = status(request, name)
    if state.get("status") != "not_determined":
        return {"status": state.get("status"), "asked": False}
    _forward(state, activate, None)
    answer = _runner_answer(request, "POST", "/mobster/photos/authorize", {"authorizationTimeout": AUTHORIZE_WAIT_S},
                            AUTHORIZE_WAIT_S + 15, name=name)
    answer = answer if isinstance(answer, dict) else {}
    return {"status": answer.get("status") or "not_determined", "asked": bool(answer.get("asked"))}


def digest(path, chunk=1 << 20):
    """The SHA-256 of the Mac file ``path``, hex."""
    hashed = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(chunk), b""):
            hashed.update(block)
    return hashed.hexdigest()


def count(request, name="the iPhone"):
    try:
        value = _runner_answer(request, "GET", "/mobster/photos/count", None, 20, name=name)
    except PhoneIOError as error:
        if error.code == "needs_rebuild":
            return None
        raise
    if not isinstance(value, dict) or "photos" not in value:
        return None if not isinstance(value, dict) or "status" not in value else {"status": value["status"]}
    return {"photos": int(value.get("photos") or 0), "videos": int(value.get("videos") or 0),
            "status": value.get("status")}


def _forward(state, activate, front):
    """Bring the runner forward for iOS's first Photos question; the bundle that was in front, to give back."""
    if state.get("status") != "not_determined" or activate is None or not state.get("bundleId"):
        return None
    try:
        origin = front() if front is not None else None
        activate(state["bundleId"])
        return origin
    except Exception:
        return None


def _refused(answer, file, name):
    words = STATUS_WORDS.get(answer.get("status"))
    if words:
        return PhoneIOError(words.format(name=name), "photos_" + answer["status"])
    return PhoneIOError(f"Photos didn't take {file.name}" + (f": {answer['error']}" if answer.get("error") else "")
                        + ".", "failed", "Check that it opens on this Mac, then try again.")


def send(request, path, *, name="the iPhone", activate=None, front=None, clock=time.monotonic, album=None,
         progress=None, chunk=CHUNK_BYTES, sha256=None, date_now=False, before_send=None):
    """Add the Mac file ``path`` to Photos (and the album ``album``, made if missing). ``request(method, path, body,
    timeout)`` is a WebDriverAgent call (phone_io.wda's, or a driver's). ``activate(bundle)``/``front()`` bring the
    runner forward for iOS's first question and the app that was in front back after (best effort).
    ``progress(sent, total)`` follows the bytes. ``date_now``: dated now in the library (the file is unchanged), so
    it comes first in every app's Recents.

    A runner built for transfers takes the file in pieces of ``chunk`` bytes and checks its SHA-256 against the Mac's
    before Photos sees it (``verified``); an older one takes it in one piece of at most 100 MB with no album, as
    before. Returns {"kind", "bytes", "name", "localIdentifier", "status", "seconds", "sha256", "verified", "album",
    "albumError"}. Sending is never retried here: a save whose answer is lost raises ``unknown`` (it may be in
    Photos). ``before_send()`` runs after preflight and before any activation or transfer;
    a refusal prevents sending."""
    from .files import snapshot
    started = clock()
    with snapshot(path, MAX_CHUNKED_BYTES) as file:
        size = file.stat().st_size
        if size == 0:
            raise PhoneIOError(f"{file.name} is empty.", "usage")
        kind, ext = sniff(file)
        checksum = digest(file)
        if sha256 is not None and checksum != sha256:
            raise PhoneIOError(f"{file.name} changed before it could be sent. Nothing was added to Photos.", "checksum")
        state = status(request, name)
        if before_send is not None:
            before_send()
        origin = _forward(state, activate, front)
        try:
            answer = _send_pieces(request, file, size, kind, ext, checksum, album, progress, chunk, name, date_now)
            legacy = answer is None
            if legacy:
                if size > MAX_SEND_BYTES:
                    raise PhoneIOError(f"Mobster's runner on {name} takes files up to 100 MB. Rebuild it once in "
                                       "Setup › Runner (about 2 minutes) to send larger ones.", "needs_rebuild")
                data = base64.b64encode(file.read_bytes()).decode("ascii")
                try:
                    answer = _runner_answer(request, "POST", "/mobster/photos",
                                            {"data": data, "kind": kind, "extension": ext, "filename": file.name[:120],
                                             "authorizationTimeout": AUTHORIZATION_WAIT_S},
                                            AUTHORIZATION_WAIT_S + 60 + size / 2_000_000, name=name)
                except PhoneIOError as error:
                    if error.code == "failed":
                        raise _lost(file, name) from None
                    raise
                if progress is not None:
                    progress(size, size)
        finally:
            if origin and activate is not None:
                try:
                    activate(origin)
                except Exception:
                    pass
        answer = answer if isinstance(answer, dict) else {}
        if answer.get("error") == "checksum":
            raise PhoneIOError(f"{file.name} didn't arrive whole on {name}: its checksum on the iPhone differs from "
                               "this Mac's. Nothing was added to Photos.", "checksum", "Try again.")
        if not answer.get("saved"):
            raise _refused(answer, file, name)
        album_error = answer.get("albumError") or None
        if album and legacy:
            album_error = "Mobster's runner was built before albums. Rebuild it once in Setup › Runner."
        return {"kind": kind, "bytes": size, "name": file.name,
                "localIdentifier": answer.get("localIdentifier") or None,
                "status": answer.get("status"), "seconds": round(clock() - started, 2), "sha256": checksum,
                "verified": not legacy and answer.get("sha256") == checksum, "album": album if album and not album_error
                else None, "albumError": album_error}


def _lost(file, name):
    return PhoneIOError(f"{name} stopped answering while it saved {file.name}, so it may or may not be in Photos.",
                        "unknown", "Look in Photos on the iPhone before you send it again.")


def _send_pieces(request, file, size, kind, ext, checksum, album, progress, chunk, name, date_now=False):
    """The runner's answer to the save, or None when the runner has no piece route (built before transfers)."""
    upload = uuid.uuid4().hex[:16]
    sent = 0
    resumed = False
    with open(file, "rb") as stream:
        while sent < size:
            stream.seek(sent)
            block = stream.read(chunk)
            try:
                answer = _runner_answer(request, "POST", "/mobster/photos/chunk",
                                        {"upload": upload, "offset": sent,
                                         "data": base64.b64encode(block).decode("ascii")},
                                        60 + len(block) / 1_000_000, name=name)
            except PhoneIOError as error:
                if error.code == "needs_rebuild" and sent == 0:
                    return None
                raise
            if sent == 0 and (not isinstance(answer, dict) or "ok" not in answer):
                return None  # not the piece route's answer: a runner built before transfers
            if not isinstance(answer, dict) or not answer.get("ok"):
                have = answer.get("bytes") if isinstance(answer, dict) else None
                if not resumed and isinstance(have, int) and 0 <= have <= sent and answer.get("error") == "offset":
                    # A piece got lost on the way (the file isn't in Photos yet: nothing to undo). Carry on once
                    # from where the runner's copy stands.
                    resumed, sent = True, have
                    continue
                raise PhoneIOError(f"{file.name} didn't reach {name} whole.", "failed",
                                   "Keep the iPhone unlocked and connected, then try again.")
            sent += len(block)
            if progress is not None:
                progress(sent, size)
    body = {"upload": upload, "kind": kind, "extension": ext, "filename": file.name[:120], "sha256": checksum,
            "authorizationTimeout": AUTHORIZATION_WAIT_S}
    if album:
        body["album"] = album
    if date_now:
        body["dateNow"] = True
    try:
        return _runner_answer(request, "POST", "/mobster/photos/save", body,
                              AUTHORIZATION_WAIT_S + 120 + size / 20_000_000, name=name)
    except PhoneIOError as error:
        if error.code == "failed":
            raise _lost(file, name) from None
        raise


def recent(request, count=1, since=None, name="the iPhone"):
    """The newest photos and videos in the phone's library, newest first."""
    body = {"limit": max(1, min(int(count or 1), MAX_RECENT))}
    if since is not None:
        body["since"] = float(since)
    value = _runner_answer(request, "POST", "/mobster/photos/recent", body, 30, name=name)
    if not isinstance(value, dict):
        raise PhoneIOError(NEEDS_REBUILD.format(name=name), "needs_rebuild")
    if value.get("status") not in ("authorized", "limited"):
        raise _refused({"status": value.get("status")}, Path("the library"), name)
    items = []
    for item in value.get("items") or ():
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]:
            items.append({"id": item["id"][:200], "filename": str(item.get("filename") or "")[:200],
                          "kind": "video" if item.get("kind") == "video" else "photo",
                          "created": float(item.get("created") or 0), "width": int(item.get("width") or 0),
                          "height": int(item.get("height") or 0), "duration": float(item.get("duration") or 0)})
    return items


def checksum(request, item_id, name="the iPhone"):
    try:
        value = _runner_answer(request, "POST", "/mobster/photos/export", {"id": item_id, "offset": 0, "length": 0},
                               900, name=name)
    except PhoneIOError as error:
        if error.code == "needs_rebuild":
            return None
        raise
    if not isinstance(value, dict) or value.get("error") or not value.get("sha256"):
        return None
    return {"bytes": int(value.get("bytes") or 0), "sha256": str(value["sha256"]),
            "filename": str(value.get("filename") or "")}


def export(request, item, folder, *, progress=None, chunk=CHUNK_BYTES, name="the iPhone"):
    """Copy one library item into ``folder`` over the runner, in pieces, checked against the phone's SHA-256."""
    from .files import land
    folder = Path(folder).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    offset, total, expected, filename = 0, None, None, item.get("filename") or ""
    hashed = hashlib.sha256()
    with tempfile.TemporaryDirectory(prefix="mobster-pull-", dir=folder) as scratch:
        partial = Path(scratch) / "file"
        with open(partial, "wb") as out:
            while True:
                value = _runner_answer(request, "POST", "/mobster/photos/export",
                                       {"id": item["id"], "offset": offset, "length": chunk},
                                       900 if offset == 0 else 120, name=name)
                if not isinstance(value, dict):
                    raise PhoneIOError(NEEDS_REBUILD.format(name=name), "needs_rebuild")
                if value.get("error"):
                    if value["error"] == "not_found":
                        raise PhoneIOError(f"That item isn't in Photos on {name} any more.", "not_found")
                    if value["error"] == "status":
                        raise _refused({"status": value.get("status")}, Path("the library"), name)
                    raise PhoneIOError(f"{name} couldn't read that item ({value['error']}).", "failed",
                                       "Keep the iPhone unlocked and connected, then try again.")
                if total is None:
                    total, expected = int(value.get("bytes") or 0), str(value.get("sha256") or "")
                    filename = filename or str(value.get("filename") or "")
                    if total > MAX_CHUNKED_BYTES:
                        raise PhoneIOError("That item is over 4 GB.", "usage")
                if int(value.get("offset") or 0) != offset or value.get("sha256") != expected:
                    raise PhoneIOError(f"The copy from {name} went out of step.", "failed", "Try again.")
                data = base64.b64decode(value.get("data") or "")
                out.write(data)
                hashed.update(data)
                offset += len(data)
                if progress is not None:
                    progress(offset, total)
                if value.get("done") or not data:
                    break
        if offset != total or hashed.hexdigest() != expected:
            raise PhoneIOError(f"{filename or 'That item'} didn't arrive whole from {name}: its checksum differs.",
                               "checksum", "Try again.")
        from ..attachments.sniff import clean_name
        wanted = clean_name(filename or f"IMG_{int(item.get('created') or 0)}")
        target = land(partial, folder, wanted)
    created = float(item.get("created") or 0)
    if created > 0:
        try:
            os.utime(target, (created, created))
        except OSError:
            pass
    return {"path": str(target), "name": target.name, "bytes": total, "kind": item.get("kind") or "photo",
            "sha256": expected, "verified": True, "modified": created or None}


class CameraRoll:
    """The camera roll's files over the cable (AFC's media root)."""

    def __init__(self, files):
        self.files = files

    def _afc(self, *command, timeout=60):
        from .files import ANSI, _explain
        argv = [self.files._afcclient(), "-u", self.files.udid, "--", *map(str, command)]
        code, output = self.files.run(argv, timeout)
        output = ANSI.sub("", output)
        if code != 0:
            raise _explain(output, self.files.name)
        errors = [line for line in output.splitlines() if line.startswith(("Error:", "ERROR:"))]
        return output, errors

    def _ls(self, folder):
        from ..attachments.sniff import clean_name
        from .files import LINE
        output, errors = self._afc("ls", "-l", folder, timeout=30)
        if errors:
            from .files import _explain
            raise _explain("\n".join(errors), self.files.name)
        entries = []
        for line in output.splitlines():
            found = LINE.match(line.rstrip("\n"))
            if not found:
                continue
            kind, size, stamp, name = found.groups()
            if clean_name(name) != name:
                continue  # A device's listing cannot name a path outside the destination folder.
            try:
                modified = time.mktime(time.strptime(stamp, "%d %b %Y %H:%M:%S"))
            except (ValueError, OverflowError):
                modified = 0.0
            entries.append({"name": name, "bytes": int(size), "modified": modified, "folder": kind == "d"})
        return entries

    def media(self):
        """Every photo and video file in the camera roll, for a before and after count (AirDrop keeps file times)."""
        found = {}
        for folder in (e["name"] for e in self._ls(MEDIA_ROOT) if e["folder"] and DCIM_FOLDER.match(e["name"])):
            for entry in self._ls(f"{MEDIA_ROOT}/{folder}"):
                if not entry["folder"] and PULLED.search(entry["name"]):
                    found[f"{MEDIA_ROOT}/{folder}/{entry['name']}"] = entry["bytes"]
        return found

    def fetch(self, remote, folder, size=None):
        """Copy one camera roll file (a ``media`` path) into ``folder``; its local Path. Checked by size."""
        from .files import _explain, land
        folder = Path(folder).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        name = remote.rsplit("/", 1)[-1]
        with tempfile.TemporaryDirectory(prefix="mobster-pull-", dir=folder) as scratch:
            partial = Path(scratch) / "file"
            _, errors = self._afc("get", remote, str(partial), timeout=120 + (size or 0) / 5_000_000)
            if errors or not partial.is_file():
                raise _explain("\n".join(errors) or "Error: nothing arrived", self.files.name)
            if size is not None and partial.stat().st_size != size:
                raise PhoneIOError(f"{name} didn't arrive whole.", "failed", "Try again.")
            return land(partial, folder, name)

    def newest(self, count=1, since=None):
        count = max(1, min(int(count or 1), MAX_PULL))
        folders = sorted((e["name"] for e in self._ls(MEDIA_ROOT) if e["folder"] and DCIM_FOLDER.match(e["name"])),
                         reverse=True)
        found = []
        for folder in folders:
            entries = [e for e in self._ls(f"{MEDIA_ROOT}/{folder}") if not e["folder"] and PULLED.search(e["name"])]
            for entry in entries:
                if since is not None and entry["modified"] < since:
                    continue
                ext = entry["name"].rsplit(".", 1)[-1].lower()
                found.append({"path": f"{MEDIA_ROOT}/{folder}/{entry['name']}", "name": entry["name"],
                              "bytes": entry["bytes"], "modified": entry["modified"],
                              "kind": PHOTO_TYPES.get(ext, "photo")})
        found.sort(key=lambda item: (item["modified"], item["name"]), reverse=True)
        return found[:count]

    def pull(self, folder, count=1, since=None, convert_heic=False, sips=None):
        """Copy the newest media into the Mac folder ``folder``; a name already there gets a new one."""
        from .files import land
        items = self.newest(count, since)
        if sum(item["bytes"] for item in items) > MAX_PULL_BYTES:
            raise PhoneIOError("Those files are over 500 MB together. Ask for fewer.", "usage")
        folder = Path(folder).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        out = []
        with tempfile.TemporaryDirectory(prefix="mobster-pull-", dir=folder) as scratch:
            for item in items:
                partial = self.fetch(item["path"], scratch, item["bytes"])
                name = item["name"]
                if convert_heic and name.lower().endswith((".heic", ".heif")):
                    converted = _to_jpeg(partial, sips=sips)
                    if converted is not None:
                        partial, name = converted, name.rsplit(".", 1)[0] + ".jpg"
                target = land(partial, folder, name)
                try:
                    os.utime(target, (item["modified"], item["modified"]))
                except OSError:
                    pass
                out.append({"path": str(target), "name": target.name, "bytes": target.stat().st_size,
                            "kind": item["kind"], "modified": item["modified"],
                            "sha256": digest(target)})
        return out


def _to_jpeg(path, sips=None):
    """A JPEG next to ``path`` made with macOS's sips, or None when sips isn't there or fails."""
    tool = sips or shutil.which("sips") or "/usr/bin/sips"
    target = Path(str(path) + ".jpg")
    try:
        done = subprocess.run([tool, "-s", "format", "jpeg", str(path), "--out", str(target)], capture_output=True,
                              timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return target if done.returncode == 0 and target.is_file() else None


def parse_since(value, now=None):
    """Epoch seconds from "10m", "2h", "1d", an ISO time or epoch seconds; None for None. ValueError otherwise."""
    if value is None or value == "":
        return None
    now = time.time() if now is None else now
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip()
    found = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhd])", text, re.I)
    if found:
        return now - float(found.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[found.group(2).lower()]
    import datetime
    try:
        stamp = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("since takes 10m, 2h, 1d or an ISO time such as 2026-10-09T14:00") from None
    if stamp.tzinfo is None:
        stamp = stamp.astimezone()
    return stamp.timestamp()
