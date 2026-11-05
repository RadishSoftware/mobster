"""App API sessions: a phone leased to one client for a while, watched live, recorded, and kept afterwards."""

import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import reading

ID_HEX = 12
DEFAULT_TIMEOUT, MAX_TIMEOUT = 300, 3600
DEFAULT_IDLE, MAX_IDLE = 120, 3600
RECORD_FPS = 2.0
MAX_FRAMES = 3600
MAX_EVENTS = 2000
KEPT_SESSIONS = 50
REAP_EVERY = 5.0
STATUSES = ("starting", "running", "ended", "error", "timed_out")

CREATE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": [],
    "properties": {
        "phone": {"type": "string", "minLength": 1, "maxLength": 128,
                  "description": "The iPhone to hold, by its name or id in Mobster, or \"any\" for the first free one "
                                 "(default: the server's phone)."},
        "timeout_s": {"type": "integer", "minimum": 30, "maximum": MAX_TIMEOUT, "default": DEFAULT_TIMEOUT,
                      "description": "The session ends this many seconds after it starts."},
        "idle_s": {"type": "integer", "minimum": 30, "maximum": MAX_IDLE, "default": DEFAULT_IDLE,
                   "description": "It ends after this many seconds with no call, unless keep_alive."},
        "keep_alive": {"type": "boolean", "default": False,
                       "description": "Keep it until timeout_s even when no call comes."},
        "record": {"type": "boolean", "default": True, "description": "Keep a recording of the screen."},
        "metadata": {"type": "object", "additionalProperties": {"type": "string", "maxLength": 200},
                     "description": "Up to 16 strings of yours, kept with the session."},
    }}
SESSION_SCHEMA = {
    "type": "object", "required": ["id", "status", "phone", "created_at", "expires_at", "live_url", "events"],
    "properties": {
        "id": {"type": "string"}, "status": {"type": "string", "enum": list(STATUSES)},
        "phone": {"type": "object", "properties": {"id": {"type": "string"}, "name": {"type": "string"}}},
        "created_at": {"type": "string", "format": "date-time"},
        "started_at": {"type": ["string", "null"], "format": "date-time"},
        "ended_at": {"type": ["string", "null"], "format": "date-time"},
        "expires_at": {"type": "string", "format": "date-time"},
        "ended_because": {"type": ["string", "null"]},
        "keep_alive": {"type": "boolean"}, "record": {"type": "boolean"},
        "metadata": {"type": "object", "additionalProperties": {"type": "string"}},
        "live_url": {"type": ["string", "null"], "description": "The viewer page, while the session runs"},
        "events": {"type": "integer", "description": "How many events it has"},
        "frames": {"type": "integer", "description": "How many frames its recording has"}}}


def _iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds") if seconds else None


@dataclass
class Session:
    id: str
    client: str                       # the token's client name
    phone: dict                       # {id, name}
    timeout_s: int
    idle_s: int
    keep_alive: bool
    record: bool
    metadata: dict
    folder: Path
    key: str                          # the viewer key (live view), never in logs
    created_at: float
    status: str = "starting"
    started_at: float = 0.0
    ended_at: float = 0.0
    ended_because: str = None
    last_call: float = 0.0
    device_session: object = None     # the ToolSet's direct session
    video: object = None
    frames: int = 0
    events: list = field(default_factory=list)
    changed: threading.Condition = field(default_factory=threading.Condition)

    @property
    def open(self):
        return self.status in ("starting", "running")

    @property
    def expires_at(self):
        return (self.started_at or self.created_at) + self.timeout_s

    def public(self, base):
        return {"id": self.id, "status": self.status, "phone": dict(self.phone),
                "created_at": _iso(self.created_at), "started_at": _iso(self.started_at),
                "ended_at": _iso(self.ended_at), "expires_at": _iso(self.expires_at),
                "ended_because": self.ended_because, "keep_alive": self.keep_alive, "record": self.record,
                "metadata": dict(self.metadata),
                "live_url": f"{base}/v1/sessions/{self.id}/view?key={self.key}" if self.open else None,
                "events": len(self.events), "frames": self.frames}


class Sessions:
    """Every session of one App API, and the thread that ends the ones past their time."""

    def __init__(self, toolset, folder, *, clock=time.time, video_factory=None, start_threads=True):
        self.toolset = toolset
        self.folder = Path(folder)
        self.clock = clock
        self.video_factory = video_factory or _wda_video
        self.items = {}
        self.lock = threading.RLock()
        self.closing = threading.Event()
        self._reaper = None
        if start_threads:
            self._reaper = threading.Thread(target=self._reap_loop, name="mobster-api-sessions", daemon=True)
            self._reaper.start()

    def create(self, inputs, client):
        """A new session for ``client`` on the phone ``inputs`` names: the Session, or reading.Failure."""
        from ..mcp_server.tools import ToolError
        metadata = inputs.get("metadata") or {}
        if len(metadata) > 16:
            raise reading.Failure(400, "bad_input", "metadata holds up to 16 strings.")
        phone = self._pick(inputs.get("phone"))
        now = self.clock()
        session = Session(secrets.token_hex(ID_HEX // 2), client, {}, inputs.get("timeout_s", DEFAULT_TIMEOUT),
                          inputs.get("idle_s", DEFAULT_IDLE), bool(inputs.get("keep_alive", False)),
                          bool(inputs.get("record", True)), dict(metadata), Path(), secrets.token_urlsafe(24), now,
                          last_call=now)
        with self.lock:
            if self.held(phone) is not None:
                raise reading.Failure(409, "busy", "A session already holds that phone. End it, or pick another "
                                                   "phone.")
            session.folder = self._folder(session.id)
            self.items[session.id] = session
        try:
            device = self.toolset._device_session(phone) if phone else self.toolset._target_session({})
        except ToolError as error:
            self._finish(session, "error", str(error))
            raise reading.Failure(503, "no_phone", str(error)) from None
        record = getattr(device.run, "record", None) or {}
        claimed = {"id": record.get("id") or getattr(device.run, "device_id", "") or "",
                   "name": record.get("name") or getattr(device.run, "device_name", "") or ""}
        with self.lock:
            other = next((s for s in self.items.values() if s is not session and s.open
                          and s.phone.get("id") == claimed["id"]), None)
            if other is None:
                session.phone, session.device_session = claimed, device
        if other is not None:
            # Refused before it owns the device session, so ending it leaves the other session's alone.
            self._finish(session, "error", "another session holds the phone")
            raise reading.Failure(409, "busy", "A session already holds that phone. End it, or pick another phone.")
        session.status, session.started_at = "running", self.clock()
        self.event(session, "session.started", phone=session.phone, expires_at=_iso(session.expires_at))
        if session.record:
            self._start_recording(session)
        return session

    def end(self, session, why="the client ended it", status="ended"):
        """End ``session`` (idempotent): its phone goes back to the Mac app and other clients."""
        if not session.open:
            return session
        self._finish(session, status, why)
        return session

    def _finish(self, session, status, why):
        with session.changed:
            if not session.open:
                return
            session.status, session.ended_at, session.ended_because = status, self.clock(), why
        video = session.video
        if video is not None:
            try:
                video.close()
            except Exception:
                pass
        device = session.device_session
        if device is not None and getattr(device, "mode", "") == "direct":
            try:
                self.toolset._close_direct(device, f"The App API session ended: {why}.", timeout=5)
            except Exception:
                pass
        self.event(session, "session.ended", status=status, because=why)
        self._save(session)

    def close(self):
        self.closing.set()
        for session in list(self.items.values()):
            self.end(session, "the App API stopped")

    def get(self, ident, client):
        """The session ``ident`` names that ``client`` made: reading.Failure 404 otherwise (another client's session is
        answered as missing)."""
        with self.lock:
            session = self.items.get(str(ident or ""))
        if session is None or session.client != client:
            raise reading.Failure(404, "unknown_session", "No session has that id. GET /v1/sessions lists yours.")
        return session

    def for_client(self, client):
        with self.lock:
            return sorted((s for s in self.items.values() if s.client == client), key=lambda s: -s.created_at)

    def held(self, phone):
        """The open session holding the phone ``phone`` names (its id or name, any case), or None."""
        folded = str(phone or "").strip().casefold()
        if not folded:
            return None
        with self.lock:
            return next((s for s in self.items.values() if s.open and folded in
                         {str(s.phone.get("id") or "").casefold(), str(s.phone.get("name") or "").casefold()}), None)

    def by_key(self, ident, key):
        """The open session ``ident`` names whose viewer key is ``key``, or None."""
        import hmac
        with self.lock:
            session = self.items.get(str(ident or ""))
        if session is None or not session.open or not key or not hmac.compare_digest(str(key), session.key):
            return None
        return session

    def video_for(self, session):
        """The session phone's video relay, started on first use (a session made with record false has none yet); None
        for a device with no stream."""
        with self.lock:
            if session.video is None and session.open:
                record = getattr(getattr(session.device_session, "run", None), "record", None) or {}
                try:
                    session.video = self.video_factory(record)
                except Exception:
                    session.video = None
            return session.video

    def touch(self, session):
        session.last_call = self.clock()
        device = session.device_session
        if device is not None and hasattr(device, "touch"):
            device.touch()

    def event(self, session, kind, **data):
        """Add one event (``{seq, at, type, ...}``) and wake the session's event streams. No screen text."""
        with session.changed:
            item = {"seq": len(session.events) + 1, "at": _iso(self.clock()), "type": kind, **data}
            if len(session.events) < MAX_EVENTS:
                session.events.append(item)
            session.changed.notify_all()
        return item

    def stream(self, session, after=0, heartbeat=10.0):
        """Server-sent events of ``session`` from sequence ``after``, until it ends: lines of bytes."""
        sent = after
        while True:
            with session.changed:
                pending = [e for e in session.events if e["seq"] > sent]
                if not pending and session.open:
                    session.changed.wait(heartbeat)
                    pending = [e for e in session.events if e["seq"] > sent]
                done = not session.open
            if not pending:
                if done:
                    return
                yield b": keepalive\n\n"
                continue
            for item in pending:
                sent = item["seq"]
                yield f"id: {item['seq']}\nevent: {item['type']}\ndata: {json.dumps(item)}\n\n".encode()
            if done and all(e["seq"] <= sent for e in session.events):
                return

    def _start_recording(self, session):
        if self.video_for(session) is None:
            self.event(session, "recording.off", why="this phone has no video stream")
            return
        (session.folder / "frames").mkdir(mode=0o700, exist_ok=True)
        threading.Thread(target=self._record, args=(session,), name="mobster-api-recorder", daemon=True).start()

    def _record(self, session):
        video = session.video
        try:
            viewer = video.subscribe()
        except Exception:
            return
        try:
            for frame in video.paced_frames(viewer, lambda: RECORD_FPS):
                if not session.open or session.frames >= MAX_FRAMES:
                    break
                session.frames += 1
                at_ms = int((self.clock() - session.started_at) * 1000)
                path = session.folder / "frames" / f"{session.frames:05d}-{at_ms:09d}.jpg"
                handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(handle, "wb") as out:
                    out.write(frame[2])
        except Exception:
            pass
        finally:
            try:
                video.unsubscribe(viewer)
            except Exception:
                pass

    def recording(self, session):
        """The session's recording as MP4 bytes. reading.Failure when it has no frames or ffmpeg is missing."""
        import io

        from PIL import Image

        from .. import run_gif
        frames = sorted((session.folder / "frames").glob("*.jpg")) if (session.folder / "frames").is_dir() else []
        if not frames:
            raise reading.Failure(404, "no_recording", "This session has no recording: it was made with record "
                                                       "false, or its phone has no video stream.")
        ffmpeg = run_gif.ffmpeg_path()
        if ffmpeg is None:
            raise reading.Failure(503, "no_ffmpeg", "Making the MP4 needs ffmpeg. Install it with brew install "
                                                    "ffmpeg, then ask again.")
        stamps = [int(p.stem.split("-")[1]) for p in frames]
        end_ms = int(((session.ended_at or self.clock()) - session.started_at) * 1000)
        images = []
        for index, path in enumerate(frames):
            following = stamps[index + 1] if index + 1 < len(frames) else max(end_ms, stamps[index] + 500)
            image = Image.open(io.BytesIO(path.read_bytes())).convert("RGB")
            if image.width % 2 or image.height % 2:
                image = image.crop((0, 0, image.width - image.width % 2, image.height - image.height % 2))
            images.append((image, max(1, following - stamps[index])))
        data = run_gif.encode_mp4(images, ffmpeg)
        if not data:
            raise reading.Failure(502, "encode_failed", "ffmpeg couldn't encode the recording. Try again.")
        return data

    def logs(self, session):
        """The session's events and its device run's action journal (operations and outcomes, no arguments)."""
        actions = []
        run_dir = getattr(getattr(session.device_session, "run", None), "run_dir", None)
        path = Path(run_dir) / "actions.jsonl" if run_dir else None
        if path is not None and path.is_file():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-2000:]:
                try:
                    actions.append(json.loads(line))
                except ValueError:
                    continue
        return {"session": session.id, "events": list(session.events), "actions": actions}

    def _pick(self, phone):
        """The phone to hold: the one named, or for "any" the first ready device no session holds."""
        if str(phone or "").strip().casefold() != "any":
            return phone
        records = self.toolset._device_records(fresh=True)
        for record in records:
            if record.get("state") != "ready" or not record.get("wdaUrl") or not self.toolset._allowed(record):
                continue
            if self.held(record["id"]) is None:
                return record["id"]
        raise reading.Failure(503, "no_free_phone", "Every phone is held or isn't ready. End a session, or try "
                                                    "again when a phone is free.")

    def _folder(self, ident):
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        kept = sorted((p for p in self.folder.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)
        for old in kept[:max(0, len(kept) - KEPT_SESSIONS + 1)]:
            if old.name not in self.items:
                import shutil
                shutil.rmtree(old, ignore_errors=True)
        folder = self.folder / ident
        folder.mkdir(mode=0o700)
        return folder

    def _save(self, session):
        try:
            data = {**session.public(""), "live_url": None, "client": session.client, "events": session.events}
            (session.folder / "session.json").write_text(json.dumps(data, indent=1))
            os.chmod(session.folder / "session.json", 0o600)
        except OSError:
            pass

    def reap(self):
        now = self.clock()
        for session in list(self.items.values()):
            if not session.open:
                continue
            if now >= session.expires_at:
                self.end(session, f"it reached its {session.timeout_s}-second timeout", "timed_out")
            elif not session.keep_alive and now - session.last_call >= session.idle_s:
                self.end(session, f"no call came for {session.idle_s} seconds", "timed_out")
            else:
                device = session.device_session
                if device is not None and hasattr(device, "touch"):
                    device.touch()   # the ToolSet releases a direct device idle 90 s: the session keeps it

    def _reap_loop(self):
        while not self.closing.wait(REAP_EVERY):
            try:
                self.reap()
            except Exception:
                pass


def _wda_video(record):
    """The phone's MJPEG relay (wda.video.WdaVideo), or None for a device with no stream."""
    from ..wda.video import WdaVideo
    url = record.get("wdaUrl")
    if not url:
        return None
    return WdaVideo(url, mjpeg_url=record.get("mjpegUrl"), screenshot_fallback=False)
