"""Loopback-only dashboard API with SSE, cancellation, and one device owner at a time."""

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hashlib
import hmac
import os
import re
import secrets
import sys
import shutil
import signal
import threading
from pathlib import Path
import time
from urllib.parse import parse_qs, unquote, urlsplit
import uuid

from . import __version__
from . import api_routes, engines, eventbus, harness_api, tracks
from .agent import Agent
from .catalog import APPS, register_app_names
from .installed_apps import InstalledApps, merge as merge_installed
from .app_icons import AppIcons, IconUnavailable
from .drivers import DriverRejection, WDA, resolve_wda_session
from .loops import DATING_REFUSAL, LoopStore, dating_loop as dating_app_loop
from .replay import ReplayStore
from .decision_memo import DecisionMemo
from .latency_trace import Trace
from .compose import build_models, build_target_driver, build_vision_judge, build_visual, close_all, warm_clients
from .frame_clock import attach_frame_clock
from .extensions import load as load_extensions
from .transport import HTTP, decode_json
from .pool import NoDeviceAvailable, default_mjpeg_url
from .journal import Journal, JournalError, Lease
from .extraction import validate_schema
from .state import validate_bundle_id
from .gemini import configured as helper_configured
from .device_manager import DeviceManager, save_env_value, update_env_file
from .manual_control import ManualControl
from .keys import Keys
from .setup_service import SetupService
from .wda_video import (BOUNDARY as WDA_BOUNDARY, DEFAULT_QUALITY, VIDEO_DETAIL, VIDEO_FPS,
                        VideoUnavailable as WdaVideoUnavailable, WdaVideo, mjpeg_settings)
from .costs import NANODOLLARS, SpendLedger, planning_estimate, helper_model_rates, usd_to_nanodollars
from .device_info import DeviceInfo
from .devices import AmbiguousDevice, DeviceNotFound
from .fleet import Fleet
from .output_contract import FORMATS as OUTPUT_FORMATS, validate_output
from .api_errors import APIError
from .steering import SteeringQueue
from .lockscreen import PHONE_LOCKED, UNLOCK_WAIT_S
from .paths import user_data_dir
from .workflows import Workflows, WorkflowDispatchUncertain
from .workflow_scheduler import WorkflowScheduler
from .schedule import preview as schedule_preview
from .helper_models import configured_model, model_options, resolve_helper_model, validate_model_request

EXPORT_MAX_BYTES = 8_000_000
EXPORT_TYPES = {"json", "csv", "yaml", "yml", "md", "txt"}
DOWNLOADS_DENIED = ("Mobster can’t save to Downloads. Allow it in System Settings › Privacy & Security › "
                    "Files and Folders.")


def save_export(body, directory=None):
    """Save a result the dashboard exports into ~/Downloads, never overwriting.

    The desktop webview cannot be relied on to download blob links, so the
    agent writes the file. The name is reduced to a safe basename with a known
    extension; the directory is fixed. In the Mac app the agent writes as
    Mobster.app, so macOS asks before the first write to Downloads; a refusal
    (or a folder the user may not write) is a 403 that says where to allow it.
    """
    if not isinstance(body, dict) or set(body) != {"filename", "text"}:
        raise ValueError("An export needs a filename and text")
    name, text = body["filename"], body["text"]
    if not isinstance(name, str) or not isinstance(text, str) or len(text.encode()) > EXPORT_MAX_BYTES:
        raise ValueError("An export needs a filename and at most 8 MB of text")
    stem, dot, extension = Path(name).name.rpartition(".")
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "-", stem).strip(" .-")[:80]
    if not dot or extension.lower() not in EXPORT_TYPES or not stem:
        raise ValueError("Exports are .json, .csv, .yaml, .md or .txt files")
    return _save_new(directory, stem, extension.lower(), text.encode("utf-8"))


# Files the agent renders itself (a task's GIF and MP4): a name made from the task, never one a request chose.
MEDIA_TYPES = {"gif", "mp4"}
# One render at a time: a GIF takes a second or two of CPU, which a running task shares.
GIF_RENDERS = threading.BoundedSemaphore(1)


def save_export_bytes(stem, extension, data, directory=None):
    """Save rendered media (``extension`` gif or mp4) into ~/Downloads as save_export does: a cleaned name
    (letters in any script, no path separators), never overwriting, a 403 when macOS refuses."""
    if extension not in MEDIA_TYPES or not isinstance(data, (bytes, bytearray)):
        raise ValueError("Only a .gif or .mp4 is saved this way")
    stem = re.sub(r"[^\w .,'’()&+–-]+", "-", str(stem or "")).strip(" .-")[:80] or "Mobster task"
    return _save_new(directory, stem, extension, bytes(data))


def _save_new(directory, stem, extension, data):
    """Write ``data`` as "{stem}.{extension}" in ``directory`` (default ~/Downloads), or "{stem} (1)…" when that
    name is taken: an existing file is never touched."""
    folder = Path(directory) if directory else Path.home() / "Downloads"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for index in range(1000):
            target = folder / (f"{stem}.{extension}" if index == 0 else f"{stem} ({index}).{extension}")
            try:
                with open(target, "xb") as stream:
                    stream.write(data)
                return {"name": target.name, "path": str(target)}
            except FileExistsError:
                continue
    except PermissionError:
        raise APIError(DOWNLOADS_DENIED, 403, "downloads_denied") from None
    raise ValueError("Too many files with that name in Downloads")


class PhoneNotReady(RuntimeError):
    """The phone cannot be driven right now; the message says what the user can do, ``code`` which stop
    it is (api_errors.PHONE_STOP_CODES: phone_locked, unplugged, face_id ...)."""

    def __init__(self, message, code="phone_locked"):
        super().__init__(message)
        self.code = code


# Seconds a preflight check may take. Locked phones and system overlays (Control
# Center, the iOS 26 Siri glow) block WDA's accessibility calls; measured: reads
# then hang 8-20 s and a task failed only with "TransportError".
PREFLIGHT_TIMEOUT = 4


def prepare_wda_phone(driver, guard=None):
    """Before a task: refuse a locked, unplugged or unresponsive phone plainly, then clear overlays.

    The phone guard (lockscreen) decides, in code and before any model call: a locked phone stops here with
    its sentence in one /wda/locked read. Home dismisses Control Center, Notification Center, Siri and the
    app switcher; the task opens its own app next, so leaving whatever was in front is harmless.
    """
    from .agent_hooks import safe_check
    from .lockscreen import DetectGuard
    verdict = safe_check(guard if guard is not None else DetectGuard(), driver, "preflight")
    if verdict.state == "stop":
        raise PhoneNotReady(verdict.message, verdict.code or "phone_locked")
    try:
        driver.call("POST", "/wda/pressButton", {"name": "home"}, PREFLIGHT_TIMEOUT)
    except Exception:
        raise PhoneNotReady("Your iPhone isn't responding. Unlock it, close Control Center or Siri if they're "
                            "open, then try again. It may have gone to the Home Screen; nothing else was done.") from None


def phone_locked(manager):
    """Whether a device manager's last probe found its phone locked (DeviceManager.health)."""
    health = getattr(manager, "health", None)
    return isinstance(health, dict) and health.get("locked") is True


def waits_for_unlock(origin, idempotency_key=None):
    """Whether a task on a locked iPhone waits for the person to unlock it (Look at your iPhone, WOW §4.1): one a person
    just started and is watching (the Mac app, the terminal UI, `mobster chat`: harness_api.INTERACTIVE). A schedule,
    MCP, a script on the API and `mobster run` stop at once, as before."""
    key = idempotency_key or ""
    scheduled = key.startswith("workflow:") and ":scheduled:" in key
    return origin in harness_api.INTERACTIVE and not scheduled


def frontier_guard(guard):
    """``{"guard": guard}`` when the Smart loop takes a phone guard (engines.build_frontier(guard=...)), else {}:
    the guard rides along to every lock and sheet check the loop makes."""
    import inspect
    if guard is None:
        return {}
    try:
        accepts = "guard" in inspect.signature(engines.build_frontier).parameters
    except (TypeError, ValueError):
        accepts = False
    return {"guard": guard} if accepts else {}


# How long a paused action waits for the user before the task ends without it.
APPROVAL_TIMEOUT_SECONDS = 600
# Settings › Usage: a task stops at $1 unless changed; amounts outside these ranges are refused.
DEFAULT_TASK_COST_LIMIT = 1.00
TASK_LIMIT_RANGE = (0.05, 100.0)
MONTH_LIMIT_RANGE = (1.0, 10000.0)
# A redirect's instruction ("Decline and say what to do instead").
INSTRUCTION_MAX_CHARS = 500
ANSWER_MAX_CHARS = 500
STEER_MAX_CHARS = 2000
APPROVAL_PENDING = "Choose Send or Don't send above, or say what to do instead there."
NOT_UNATTENDED = ("Mobster's agent works in social and dating apps only when you ask it yourself in the "
                  "Mobster app.")
APP_ONLY = "Do this in the Mobster app."
FRAME_ID = re.compile(r"f[0-9a-f]{10}")
# A Smart task with no "start in" app: it begins on the Home Screen and opens what it needs.
ANY_APP = {"id": "any", "name": "Any app", "bundleId": None, "available": True}
# send_stream / send_frame with no video given serve the primary phone's (the /api/device/* paths). A device's own
# path passes its video, and None there is "this device has none", never the primary's picture.
PRIMARY_VIDEO = object()
# Apps a Smart task may open, listed in its prompt (the start app first).
MAX_SMART_APPS = 120


class FrameStore:
    """Screens a run points at (steps, receipts, proof), as JPEGs no more than MAX_SIDE px on their long
    side. A frame is kept in memory at once and downscaled, then written beside the journal, on one worker
    thread: the agent never waits for an image. Without durable history frames live in memory only."""

    MAX_SIDE = 480
    MAX_PER_RUN = 400
    MAX_MEMORY_RUNS = 4

    def __init__(self, root=None):
        self.root = Path(root) if root else None
        self.memory = OrderedDict()  # run id -> {frame id: bytes (raw until processed)}
        self.lock = threading.Lock()
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mobster-frames")

    def put(self, run_id, data):
        """Keep ``data`` (JPEG bytes) for ``run_id``; returns its frame id, or None when the run is full."""
        frame_id = "f" + uuid.uuid4().hex[:10]
        with self.lock:
            frames = self.memory.setdefault(run_id, {})
            self.memory.move_to_end(run_id)
            if len(frames) >= self.MAX_PER_RUN:
                return None
            frames[frame_id] = bytes(data)
            # Older runs' frames are on disk (or, without durable history, gone with the oldest runs).
            while len(self.memory) > (self.MAX_MEMORY_RUNS if self.root is not None else 20):
                self.memory.popitem(last=False)
        self.worker.submit(self._process, run_id, frame_id)
        return frame_id

    @classmethod
    def downscale(cls, data):
        try:
            from PIL import Image
            image = Image.open(io.BytesIO(data))
            if max(image.size) <= cls.MAX_SIDE and image.format == "JPEG":
                return data
            image = image.convert("RGB")
            image.thumbnail((cls.MAX_SIDE, cls.MAX_SIDE))
            out = io.BytesIO()
            image.save(out, "JPEG", quality=72)
            return out.getvalue()
        except Exception:
            return None

    def _process(self, run_id, frame_id):
        with self.lock:
            raw = self.memory.get(run_id, {}).get(frame_id)
        small = self.downscale(raw) if raw is not None else None
        if small is None:
            with self.lock:
                self.memory.get(run_id, {}).pop(frame_id, None)
            return
        with self.lock:
            if frame_id in self.memory.get(run_id, {}):
                self.memory[run_id][frame_id] = small
        if self.root is not None:
            try:
                folder = self.root / run_id
                folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                write_private(folder / f"{frame_id}.jpg", small, binary=True)
            except OSError:
                pass

    def get(self, run_id, frame_id):
        if not FRAME_ID.fullmatch(frame_id or ""):
            return None
        with self.lock:
            data = self.memory.get(run_id, {}).get(frame_id)
        if data is not None:
            small = self.downscale(data)
            return small
        if self.root is not None:
            try:
                return (self.root / run_id / f"{frame_id}.jpg").read_bytes()
            except OSError:
                return None
        return None

    def delete(self, run_id):
        with self.lock:
            self.memory.pop(run_id, None)
        if self.root is not None and re.fullmatch(r"[a-f0-9]{12}", run_id or ""):
            shutil.rmtree(self.root / run_id, ignore_errors=True)

    def close(self):
        self.worker.shutdown(wait=True, cancel_futures=False)


@dataclass
class Run:
    app: dict
    goal: str
    mode: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    events: list = field(default_factory=list)
    summary: dict | None = None
    image: str | None = None
    image_at: float = 0
    image_lock: threading.Lock = field(default_factory=threading.Lock)
    capture: object = None
    image_captured_at: float | None = None
    idempotency_key: str | None = None
    output_schema: dict | None = None
    output_format: str = "auto"
    helper_model: str | None = None
    allowed_bundles: frozenset | None = None
    # A compiled loop that judges and logs every item but never taps (scrolling
    # stays allowed); any other request previews its first decision only.
    dry_run: bool = False
    journal: object = None
    lease: object = None
    device: object = None  # the phone held for this run, released when it finishes
    guard: object = None  # the phone guard (lockscreen.guard_for), from the preflight on
    # The device the run is on (fleet.py): its id and name, kept with the run; ``phone`` is the live object.
    device_id: str | None = None
    device_name: str | None = None
    phone: object = None
    stop: threading.Event = field(default_factory=threading.Event)
    condition: threading.Condition = field(default_factory=threading.Condition)
    # The action waiting for the user's answer. Held in memory only: it can
    # carry the exact text to be sent, which is never written to the journal.
    approval: dict | None = None
    approval_answer: str | None = None
    # smart | fast for a live run (engines.py); None for a demo replay or a run saved before engines.
    engine: str | None = None
    # Published-rate spend so far (every priced inference_finished), and the estimate shown before it ran.
    cost_nanodollars: int = 0
    cost_known: bool = False
    estimate_usd: float | None = None
    reviewed_ok: bool = False
    frames: object = None  # FrameStore
    # Seam S5: validated run fields (harness_api.register_run_field; public ones journaled in metadata["extras"]),
    # where the run came from (harness_api.ORIGINS), the user's mid-task messages (S8), and the agent's masked notes
    # and plan for listeners (in memory only, never journaled).
    extras: dict = field(default_factory=dict)
    origin: str = "api"
    steering: SteeringQueue = field(default_factory=SteeringQueue)
    private: dict = field(default_factory=dict)
    listeners: object = None  # the runtime's harness_api.ListenerQueue (S6.4); None: no listener calls

    def request_approval(self, request, timeout=None):
        """Block until the user answers. Returns approved, denied, timeout or stopped.

        A question (``kind`` "loop" or "question", from a compiled loop) also
        carries ``choices``; picking one returns "choice:<id>", and a plain
        approval means its ``default_choice``. The label states what will
        happen and its limits, so the plain approve/decline prompt reads right.

        A clarifying question (``kind`` "clarify", seam S5) carries ``operation`` "ASK_USER", ``label`` (the
        question, at most 80 characters), ``question`` (at most 300), optional ``choices`` (at most 6) and
        ``allow_text``; a typed answer returns "answer:<text>", which is never journaled.
        """
        timeout = APPROVAL_TIMEOUT_SECONDS if timeout is None else timeout
        now = time.time()
        pending = {"id": uuid.uuid4().hex[:12], "operation": request["operation"], "label": request["label"],
                   "role": request.get("role") or "", "text": request.get("text"),
                   "app": str(request.get("app_name") or self.app["name"])[:80],
                   "requestedAt": now * 1000, "expiresAt": (now + timeout) * 1000}
        kind = request.get("kind") or "action"
        target = request.get("target")
        pending.update(kind=kind, title=str(request["title"])[:160] if request.get("title") else None,
                       act=str(request["act"])[:40] if request.get("act") else None,
                       target=({key: float(target[key]) for key in ("x", "y", "w", "h")}
                               if isinstance(target, dict) and all(isinstance(target.get(k), (int, float))
                                                                   for k in ("x", "y", "w", "h")) else None))
        if kind in ("loop", "question"):
            choices = [{"id": str(c["id"])[:40], "label": str(c["label"])[:120]}
                       for c in request.get("choices") or () if isinstance(c, dict) and c.get("id")][:6]
            default = request.get("default_choice")
            pending.update(kind=kind, question=str(request.get("question") or "")[:300], choices=choices,
                           defaultChoice=default if any(c["id"] == default for c in choices) else None,
                           limits=request.get("limits"))
        elif kind == "clarify":
            choices = [{"id": str(c["id"])[:40], "label": str(c.get("label") or c["id"])[:120]}
                       for c in request.get("choices") or () if isinstance(c, dict) and c.get("id")][:6]
            pending.update(label=str(request["label"])[:80],
                           question=str(request.get("question") or request["label"])[:300], choices=choices,
                           allowText=bool(request.get("allow_text", not choices)))
        with self.condition:
            self.approval, self.approval_answer = pending, None
        self.emit({"event": "approval_requested", "approval_id": pending["id"], "step": request.get("step"),
                   "operation": pending["operation"], "label": pending["label"],
                   "text_present": pending["text"] is not None, "kind": kind,
                   "title": pending["title"], "act": pending["act"], "target": pending["target"],
                   "choices": [c["id"] for c in pending.get("choices", ())]})
        deadline = time.monotonic() + timeout
        with self.condition:
            while self.approval_answer is None and not self.stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.condition.wait(timeout=min(remaining, 1))
            answer = ("stopped" if self.stop.is_set() and self.approval_answer is None else
                      self.approval_answer or "timeout")
            self.approval, self.approval_answer = None, None
        # A redirect's instruction and a typed answer are the user's own text: they go to the agent, never into
        # the journal.
        self.emit({"event": "approval_resolved", "approval_id": pending["id"],
                   "decision": "redirected" if answer.startswith("redirected:") else
                   "answered" if answer.startswith("answer:") else answer})
        return answer

    def answer_approval(self, approval_id, approve, choice=None, instruction=None, answer=None):
        """Answer the pending request. ``instruction`` with approve false is a redirect: the task goes on
        with it ("Decline and say what to do instead"), and request_approval returns "redirected:<text>".
        ``answer`` (a clarifying question only, with approve true): the user's typed answer, 1–500 characters;
        request_approval returns "answer:<text>"."""
        with self.condition:
            if self.approval is None or self.approval["id"] != approval_id or self.approval_answer is not None:
                return False
            if answer is not None:
                text = " ".join(str(answer).split())
                if self.approval.get("kind") != "clarify":
                    raise ValueError("Only a question from Mobster's agent takes a typed answer")
                if not approve or choice is not None or instruction is not None \
                        or not 1 <= len(text) <= ANSWER_MAX_CHARS:
                    raise ValueError("An answer says in 1–500 characters what Mobster's agent asked, with approve true")
                self.approval_answer = f"answer:{text}"
            elif instruction is not None:
                text = " ".join(str(instruction).split())
                if approve or choice is not None or not 1 <= len(text) <= INSTRUCTION_MAX_CHARS:
                    raise ValueError("A redirect declines the action and says in 1–500 characters what to do instead")
                self.approval_answer = f"redirected:{text}"
            elif choice is not None:
                if not approve or choice not in {c["id"] for c in self.approval.get("choices", ())}:
                    raise ValueError("That choice is not one of this question's answers")
                self.approval_answer = f"choice:{choice}"
            else:
                self.approval_answer = "approved" if approve else "denied"
            self.condition.notify_all()
            return True

    def emit(self, event):
        entry = self._append(event)
        listeners = self.listeners
        if listeners is not None:
            # After the append and outside run.condition (S6.4): listeners never hold up the run.
            listeners.post("on_event", self, entry)

    def _append(self, event):
        if "_frame" in event:
            # Screens are stored beside the run and referenced by id, never inlined in events. The caller's
            # dict gets the id too, so it can point at the same frame later (a receipt in the result's proof).
            data = event.pop("_frame")
            event["frameId"] = self.frames.put(self.id, data) if self.frames is not None and data else None
        if event.get("event") == "inference_finished" and type(event.get("cost_nanodollars")) is int:
            with self.condition:
                self.cost_nanodollars += event["cost_nanodollars"]
                self.cost_known = True
        with self.condition:
            entry = {**event, "seq": len(self.events), "timestamp": time.time() * 1000}
            if self.journal:
                self.journal.append(self.metadata(), entry)
            self.events.append(entry)
            self.condition.notify_all()
        return entry

    def steer(self, text, source="app"):
        """Queue a mid-task message (S8) and emit {"event": "user_message", "id", "text", "source"} (journaled).
        APIError 409 run_not_active once the run has finished; 409 approval_pending while an approval other than a
        clarifying question waits (nothing is queued); 403 steer_not_allowed when ``source`` may not steer this run
        (a run started over MCP takes messages only from MCP or the Mac app; any other run never from MCP).
        ``source`` comes from the request's origin, never from its body."""
        if source not in ("app", "tui", "cli", "mcp", "voice"):
            raise ValueError("Unknown message source")
        allowed = ("mcp", "app") if self.origin == "mcp" else ("app", "tui", "cli", "voice")
        if source not in allowed:
            raise APIError("This task takes messages only where it was started.", 403, "steer_not_allowed")
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= STEER_MAX_CHARS:
            raise ValueError("A message is 1 to 2,000 characters")
        with self.condition:
            if self.finished_at is not None or self.steering.closed:
                raise APIError("This task has finished.", 409, "run_not_active")
            if self.approval is not None and self.approval.get("kind") != "clarify":
                raise APIError(APPROVAL_PENDING, 409, "approval_pending", approvalId=self.approval["id"])
            message = self.steering.put(text, source=source)
        self.emit({"event": "user_message", "id": message.id, "text": message.text, "source": message.source})
        return {"id": message.id, "text": message.text, "source": message.source, "at": message.at * 1000}

    def finish(self, summary):
        """Publish terminal status, summary and event as one durable transition."""
        if self.finished_at is None:
            unread = self.steering.close()
            if unread:
                try:
                    self.emit({"event": "steer_unread", "ids": [message.id for message in unread]})
                except Exception:
                    pass  # the terminal record below still goes in
        with self.condition:
            if self.finished_at is not None:
                return
            finished_at = time.time()
            status = summary["status"]
            if status in {"queued", "running"}:
                raise ValueError("A terminal run cannot remain active")
            entry = {"event": "run_finished", "status": status, "summary": summary,
                     "seq": len(self.events), "timestamp": finished_at * 1000}
            metadata = {**self.metadata(), "status": status, "summary": summary,
                        "finishedAt": finished_at * 1000}
            try:
                if self.journal:
                    self.journal.finish(metadata, entry)
            except JournalError:
                # A real storage failure is visible, but never presented as durable.
                # Failed ordinary events were not appended, so saved sequence IDs remain contiguous.
                summary = {**summary, "persistence_failed": True}
                entry.update(summary=summary, persistence_failed=True)
            self.status, self.summary, self.finished_at = status, summary, finished_at
            self.events.append(entry)
            self.condition.notify_all()

    def metadata(self):
        return {"id": self.id, "app": self.app, "appId": self.app["id"], "appName": self.app["name"],
                "goal": self.goal, "mode": self.mode, "status": self.status,
                "createdAt": self.created_at * 1000,
                "finishedAt": self.finished_at * 1000 if self.finished_at is not None else None,
                "summary": self.summary, "idempotencyKey": self.idempotency_key,
                "outputSchema": self.output_schema, "outputFormat": self.output_format,
                "helperModel": self.helper_model,
                "allowedBundles": sorted(self.allowed_bundles) if self.allowed_bundles is not None else None,
                "dryRun": self.dry_run, "engine": self.engine, "costUsd": self.cost_usd,
                "estimateUsd": self.estimate_usd, "reviewedOk": self.reviewed_ok,
                "device": self.device_id, "deviceName": self.device_name,
                "extras": harness_api.public_extras(self.extras), "origin": self.origin}

    @property
    def cost_usd(self):
        """Published-rate spend so far; None until a priced call has finished."""
        return round(self.cost_nanodollars / NANODOLLARS, 6) if self.cost_known else None

    def public(self, include_events=True):
        with self.condition:
            return {**{k: v for k, v in self.metadata().items() if k != "app"},
                    "approval": dict(self.approval) if self.approval else None,
                    "events": list(self.events) if include_events else [], "eventCount": len(self.events)}


class UnattendedGuard:
    """A phone guard that also refuses the apps on harness_api.UNATTENDED_DENY (agent_hooks.safe_allows is False
    for them: LAUNCH_APP and the foreground check stop the run). Everything else is the inner guard's."""

    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def allows_app(self, bundle_id):
        if not harness_api.unattended_allowed(bundle_id):
            return False
        inner = getattr(self.inner, "allows_app", None)
        return bool(inner(bundle_id)) if callable(inner) else True


@dataclass(frozen=True)
class RunSetup:
    """Callbacks ``Runtime.open_driver`` reports through (see there)."""
    phase: object
    hold: object


class NoAwakeLease:
    """The awake lease of a target that has none: WDA keeps the phone awake itself."""

    def acquire(self, owner):
        pass

    def release(self, owner):
        pass

    def close(self, timeout=5.0):
        pass

    def status(self):
        return {"status": "unsupported", "owners": 0, "bundleId": None, "error": None}


class NoVideo:
    """Live video when the target streams none of its own (a USB iPhone streams through WDA)."""
    closed = False

    def status(self):
        return {"status": "unavailable", "reason": "Live video source is not configured",
                "configured": False, "viewers": 0}

    def set_quality(self, *args, **kwargs):
        pass

    def subscribe(self):
        raise WdaVideoUnavailable("Live video source is not configured")

    def close(self):
        self.closed = True


class PooledLease:
    """A pool reservation that closes exactly like a direct device lease.

    A run has always owned one closable handle to its phone, and restart,
    cancellation and failure paths all rely on that. Pooling changes who tracks
    the device, not that contract.
    """

    def __init__(self, pool, owner):
        self.pool, self.owner = pool, owner

    def close(self):
        pool, self.pool = self.pool, None
        if pool is not None:
            pool.release(self.owner)


class Runtime:
    # True for a runtime around one explicit target (`mobster run`, the terminal UI): a task that names no device
    # runs on that target, without reading the device list first.
    one_target = False

    def __init__(self, config):
        # The SOTA tracks register first (seam S1): their schemas migrate when the journal opens below.
        tracks.load()
        self.config = config
        self.runs = {}
        self.lock = threading.Lock()
        # Run listeners' calls, on one thread of their own (S6.4).
        self.listeners = harness_api.ListenerQueue()
        self.bus = eventbus.bus
        # run id -> device id. Capacity is the number of phones, so two tasks on
        # two devices run at once; two tasks on one device still cannot.
        self.active_runs = {}
        # A pool of several phones, when a target provides one (``make_pool``).
        self.pool = self.make_pool(config)
        self.inventory = {}
        self.inventory_at = 0
        self.inventory_lock = threading.Lock()
        self.inventory_ok = False
        self.wda_health_at, self.wda_ready = 0, False
        # The WDA session in use. Re-resolved from WDA itself, because a
        # runner restart replaces it and a fixed --session then fails.
        self.wda_session_id, self.wda_session_lock = getattr(config, "session", None), threading.Lock()
        self.closing = False
        self.threads = set()
        self.streams = threading.BoundedSemaphore(8)
        self.device_info = self.make_device_info(config)
        self.awake = self.make_awake_lease()
        # A USB iPhone streams through WDA's MJPEG server, on the port paired with WDA's: 8100 -> 9100,
        # and a simulator's 8203 -> 9203, never another device's stream.
        self.video = (WdaVideo(config.wda_url, mjpeg_url=default_mjpeg_url(config.wda_url), session=self.current_wda_session,
                               on_activity=self.video_activity, quality=self.video_quality())
                      if getattr(config, "wda_url", None) else self.make_video(config))
        # The user's own taps and typing on the live view (WDA only).
        self.control = ManualControl(config.wda_url, self.wda_session) if getattr(config, "wda_url", None) else None
        self.journal = Journal(getattr(config, "state_db", None))
        # Compiled runs live beside the private journal; no journal, no recordings.
        state_db = getattr(config, "state_db", None)
        self.replay_store = ReplayStore(Path(state_db).parent / "replays") if state_db else None
        # Exact decision memo (decision_memo.py), next to the replays.
        self.decision_memo = DecisionMemo(Path(state_db).parent / "decision-memo" if state_db else None)
        # Compiled loop programs and their per-item ledgers (exactly once across restarts).
        self.loop_store = LoopStore(Path(state_db).parent / "loops") if state_db else None
        # Screens runs point at (GET /api/runs/{id}/frames/{frameId}), beside the journal.
        self.frames = FrameStore(Path(state_db).parent / "frames" if state_db else None)
        # Whether Smart's key (OpenAI or Anthropic) reaches its model; ``serve`` turns the background check on.
        self.model_reach = engines.ModelReach()
        self.scheduler = None
        # The USB iPhone lifecycle, when this server owns it (desktop app).
        self.manager = self.setup = None
        # The phone's own apps (USB iPhone only; the device manager knows which phone).
        self.installed_apps = None
        data_dir = Path(getattr(config, "data_dir", None) or default_data_dir())
        # Icons for those apps; only the apps the picker lists are ever looked up.
        self.icons = AppIcons(data_dir / "app-icons",
                              allowed=lambda: {app["bundleId"] for app in self.apps() if app.get("foundOnPhone")})
        if getattr(config, "manage_device", False):
            env_file = getattr(config, "env_file", None) or data_dir / "agent.env"
            self.manager = DeviceManager(data_dir, config.wda_url or "http://127.0.0.1:8100")
            self.setup = SetupService(self, self.manager, env_file)
            self.device_info = DeviceInfo(manager=self.manager)
            threading.Thread(target=self.manager.watch, args=(self.manager.watching,), name="mobster-device-watch",
                             daemon=True).start()
            # --socket exists only with the optional extension, which the signed app doesn't ship.
            if not getattr(config, "socket", None):
                self.installed_apps = InstalledApps(self.manager.device)
        # Every device this runtime can run tasks on: the primary one above, other USB phones set up here,
        # Mobster's simulators and WDA addresses (fleet.py).
        self.fleet = Fleet(self)
        try:
            self.workflows = Workflows(self)
            for item in self.journal.load():
                run = Run(item["app"], item["goal"], item["mode"], id=item["id"],
                    status=item["status"], created_at=item["createdAt"] / 1000,
                    finished_at=item["finishedAt"] / 1000 if item["finishedAt"] is not None else None,
                    events=item["events"], summary=item["summary"],
                    idempotency_key=item.get("idempotencyKey"), output_schema=item.get("outputSchema"),
                    output_format=item.get("outputFormat", "json"), helper_model=item.get("helperModel"),
                    allowed_bundles=(frozenset(item["allowedBundles"]) if item.get("allowedBundles") else None),
                    journal=self.journal, engine=item.get("engine"), estimate_usd=item.get("estimateUsd"),
                    reviewed_ok=item.get("reviewedOk") is True, frames=self.frames,
                    device_id=item.get("device"), device_name=item.get("deviceName"),
                    extras=dict(item.get("extras") or {}),
                    origin=item.get("origin") if item.get("origin") in harness_api.ORIGINS else "api")
                if isinstance(item.get("costUsd"), (int, float)):
                    run.cost_nanodollars, run.cost_known = round(item["costUsd"] * NANODOLLARS), True
                self.runs[run.id] = run
                if run.finished_at is None:
                    run.finish({"event": "result", "status": "interrupted", "independently_verified": False,
                        "reason": "The service restarted. An in-flight action may have run; this task was not resumed.",
                        "actions": sum(e["event"] == "action_acknowledged" for e in run.events),
                        "actions_may_have_run": any(e["event"] in {"action_started", "app_launch_started"} for e in run.events)})
                    if run.summary.get("persistence_failed"):
                        raise JournalError("Interrupted task recovery could not be saved")
        except Exception:
            self.journal.close()
            raise
        self.start_services()

    # -- seam S6: tracks' services, listeners and the stable Runtime API --------------------------------------------

    def start_services(self):
        """``start(runtime)`` for each registered service (after the fleet and Workflows exist). Logged on error."""
        self._services = list(harness_api.services())
        for start, _close in self._services:
            try:
                start(self)
            except Exception:  # noqa: BLE001 -- a track's service never stops Mobster from starting
                harness_api.log.exception("a service failed to start")

    def close_services(self):
        for _start, close in reversed(getattr(self, "_services", ())):
            try:
                close(self)
            except Exception:  # noqa: BLE001
                harness_api.log.exception("a service failed to close")
        self._services = []

    def device_busy(self, device_id):
        """Whether a task is running on ``device_id`` (a fleet id) now; wireless switches transport only when
        it's False."""
        with self.lock:
            return any(value == device_id for value in self.active_runs.values()) or any(
                getattr(run, "device_id", None) == device_id and run.id in self.active_runs
                for run in self.runs.values())

    def run_context(self, run):
        """The harness_api.RunContext tracks see for ``run``."""
        phone = self.phone_for(run) if getattr(self, "fleet", None) is not None else None
        kind = getattr(phone, "kind", None) if phone is not None else None
        if kind is None and getattr(self.config, "wda_url", None):
            kind = "usb" if getattr(self, "manager", None) is not None else "wda"
        state_db = getattr(self.config, "state_db", None)
        return harness_api.RunContext(
            run_id=run.id, goal=run.goal, engine=run.engine or engines.SMART, origin=run.origin,
            app_bundle=run.app.get("bundleId"), device_id=run.device_id, device_kind=kind,
            extras=harness_api.frozen_mapping(run.extras), data_dir=Path(state_db).parent if state_db else None,
            emit=run.emit, clarify=harness_api.clarify_only(run.request_approval), cancelled=run.stop.is_set,
            runtime=self)

    def current_wda_session(self):
        """The session to tune the stream through.

        A fresh runner has none, and without one the stream stays at full
        resolution and its default framerate (measured: 1178x2556 at ~9.5 fps
        on an iPhone 15 Pro). WDA keeps the MJPEG settings server-wide, so
        creating a session here changes nothing a later run relies on.
        """
        try:
            return self.wda_session()
        except Exception:
            return None

    def video_activity(self, active):
        self.awake.acquire("video") if active else self.awake.release("video")

    @property
    def active(self):
        """The current run, or the oldest when several devices are busy."""
        return next(iter(self.active_runs), None)

    @active.setter
    def active(self, value):
        if value is None:
            self.active_runs.clear()
        else:
            self.active_runs.setdefault(value, None)

    def capacity(self):
        """How many more live runs this runtime can admit right now on the device a task that names none
        runs on (with a pool: on any of its phones)."""
        if self.pool is None:
            fleet = getattr(self, "fleet", None)
            if fleet is None:
                return 0 if self.active_runs else 1
            return 0 if self.run_on(fleet.primary) else 1
        free = self.pool.position()
        return max(0, free["healthy"] - free["busy"])

    def run_on(self, phone):
        """The id of the live run on ``phone`` (a fleet.Phone), or None. A run recorded with no device (the
        ``active`` setter, a demo replay) is on the primary phone."""
        for run_id, key in list(self.active_runs.items()):
            if key == phone.key or (key is None and phone.primary):
                return run_id
        return None

    def phone_for(self, run):
        """The fleet.Phone a run drives: its own, else the primary."""
        phone = getattr(run, "phone", None)
        fleet = getattr(self, "fleet", None)
        return phone if phone is not None else fleet.primary if fleet is not None else None

    def apps_for(self, phone):
        """The apps a task on ``phone`` may start in: the primary's are ``apps()``."""
        return self.apps() if phone is None or phone.primary else phone.apps()

    # -- target hooks: the core serves WDA; a subclass (``extensions.Hooks.runtime_for``)
    # overrides these for a target of its own. ---------------------------------------------

    def make_pool(self, config):
        """A ``pool.DevicePool`` of several phones, or None for one phone."""
        return None

    def make_device_info(self, config):
        return DeviceInfo()

    def make_awake_lease(self):
        """Keeps the foreground app from idling while a task or viewer needs it. WDA has none."""
        return NoAwakeLease()

    def make_video(self, config):
        """Live video for a target that is not a WDA iPhone."""
        return NoVideo()

    def device_preview(self):
        """An operator preview frame (data URL) read from the device itself, or None."""
        return None

    def target_status(self):
        """This target's part of ``status()``: whether a device is configured and ready, and why not."""
        wda = bool(self.config.wda_url)
        ready, health = False, None
        if wda:
            with self.inventory_lock:
                if not self.wda_health_at or time.monotonic() - self.wda_health_at > 5:
                    client = None
                    try:
                        client = HTTP(self.config.wda_url)
                        response = client.request("GET", "/status", timeout=2)
                        self.wda_ready = response.get("value", {}).get("ready") is True
                        if isinstance(response.get("sessionId"), str) and response["sessionId"]:
                            self.wda_session_id = response["sessionId"]
                    except Exception:
                        self.wda_ready = False
                    finally:
                        if client:
                            client.close()
                        self.wda_health_at = time.monotonic()
                ready = self.wda_ready
                # /status answers even when WDA cannot reach a locked or overlaid phone.
                health = self.manager.health_error() if self.manager is not None else None
                if ready and health:
                    ready = False
        return {"device": wda, "ready": ready, "health": health, "can_act": wda, "driver": "wda",
                "screen_reading": False, "notes": []}

    def make_guard(self, run, phone):
        """The run's phone guard (agent_hooks.PhoneGuard): a scheduled run's mode is "schedule". A USB
        phone's guard also knows within seconds when its cable is pulled."""
        from .lockscreen import guard_for
        primary = phone is None or phone.primary
        udid = None
        if primary and self.manager is not None:
            try:
                udid = getattr(self.manager, "pinned", None) or self.manager.settings().get("udid")
            except Exception:
                udid = None  # an unreadable device.json: the guard still checks the lock, not the cable
        elif not primary and getattr(phone, "kind", None) == "usb":
            udid = phone.udid
        key = run.idempotency_key or ""
        mode = "schedule" if key.startswith("workflow:") and ":scheduled:" in key else "interactive"
        # Look at your iPhone: a task a person just started waits for them to unlock the phone; nothing else does.
        wait_s = UNLOCK_WAIT_S if waits_for_unlock(getattr(run, "origin", None), run.idempotency_key) else 0
        guard = guard_for(device_id=udid, wda_url=self.config.wda_url if primary else phone.wda_url, mode=mode,
                          approve=run.request_approval, emit=run.emit, usb=bool(udid), wait_s=wait_s,
                          cancelled=run.stop.is_set)
        if getattr(run, "origin", None) == "mcp":
            # A run an MCP client started never opens a social or dating app (harness_api.UNATTENDED_DENY).
            guard = UnattendedGuard(guard)
        return guard

    def check_app(self, app):
        """Raise ValueError when a live run cannot use ``app`` on this target."""
        if app.get("installed") is False:
            raise ValueError(f"{app['name']} isn't installed on your iPhone")

    def open_driver(self, run, trace, setup):
        """Build this run's driver, check the phone and bring the app forward.

        ``setup.hold(driver)`` hands the driver to the caller, which closes it however
        this ends; ``setup.phase(name)`` names the step a failure happened in. Returns
        the driver to run with, or None when the run was stopped meanwhile.
        """
        phone = self.phone_for(run)
        primary = phone is None or phone.primary
        wda_url = self.config.wda_url if primary else phone.wda_url
        with trace.span("setup.driver"):
            session = (self.wda_session() if primary else phone.wda_session()) if wda_url else None
            driver = setup.hold(build_target_driver(wda_url=wda_url, session=session,
                                                    expected_bundle=run.app["bundleId"]))
        state_db = getattr(self.config, "state_db", None)
        if isinstance(driver, WDA) and state_db and WDA.overlay_hint_path is None:
            # Apps that needed WDA's foreground hint, kept across restarts: the
            # first read otherwise fails for ~9 s under the Siri overlay.
            WDA.load_overlay_hints(str(Path(state_db).parent / "wda-overlay-hints.json"))
        video = self.video if primary else phone.video
        if isinstance(driver, WDA) and isinstance(video, WdaVideo):
            # Pixel settle for this run (MOBSTER_FRAME_CLOCK); subscribing keeps the
            # relay's MJPEG stream up, and closing the driver releases it. Each device's own stream.
            attach_frame_clock(driver, video, log_dir=Path(state_db).parent if state_db else None)
        if run.stop.is_set():
            return None
        setup.phase("preflight")
        run.guard = self.make_guard(run, phone)
        with trace.span("setup.preflight"):
            prepare_wda_phone(driver, run.guard)
        if run.stop.is_set():
            return None  # Stop pressed during the check (up to PREFLIGHT_TIMEOUT per call): the app stays shut.
        if not run.app.get("bundleId"):
            # A Smart run with no "start in" app starts on the Home Screen (the preflight pressed Home).
            setup.phase("reading_app")
            return driver
        run.emit({"event": "app_launch_started", "app": run.app["name"]})
        setup.phase("launching")
        with trace.span("launch.activate"):
            driver.call("POST", "/wda/apps/activate", {"bundleId": run.app["bundleId"]})
        run.emit({"event": "app_launch_acknowledged", "app": run.app["name"]})
        setup.phase("reading_app")
        return driver

    def open_models(self, run, on_inference):
        """The run's decision model, text helper and image judge (the last two may be None).

        Their connections start warming here. A subclass may substitute its own
        (the terminal UI's offline demo does); ``work`` closes whatever this returns.
        """
        model, helper = build_models(
            helper=run.helper_model is not None and self.status()["helper_configured"],
            helper_model=run.helper_model, on_inference=on_inference)
        judge = build_vision_judge(enabled=helper is not None, on_inference=on_inference)
        warm_clients(model, helper, judge)
        return model, helper, judge

    def visual_options(self, run):
        """Where this run's opt-in screen reader reads from (``compose.build_visual``)."""
        return {}

    def apps(self):
        # WDA opens any app by bundle ID: the catalog, then every app found on the phone.
        inventory = self.installed_apps.snapshot() if self.installed_apps else None
        apps = merge_installed(APPS, inventory)
        if inventory is not None:
            register_app_names({app["bundleId"]: app["name"] for app in apps if app.get("foundOnPhone")})
        return apps

    def preview(self, run, final=False):
        # Operator-only preview, never fed to Jev or required for action decisions.
        # Captured on the request thread, independently of the accessibility hot path.
        if (run.mode == "live" and (callable(run.capture) or self.device_preview is not Runtime.device_preview)
                and (final or run.status in {"running", "queued"})):
            if not run.image_lock.acquire(blocking=False):
                return {"image": run.image, "source": run.mode, "capturedAt": run.image_captured_at}
            try:
                if final or time.monotonic() - run.image_at > 1:
                    try:
                        captured = None
                        if callable(run.capture):
                            captured = run.capture(timeout=3)
                        else:
                            captured = self.device_preview()
                        if captured:
                            run.image = captured
                            run.image_captured_at = time.time() * 1000
                    except DriverRejection as exc:
                        if exc.code in {"app_preview_uniform", "app_preview_unavailable", "app_preview_oversized"}:
                            run.image, run.image_captured_at = None, None
                    except Exception:
                        pass  # Preview failure never blocks the agent's observations.
                    run.image_at = time.monotonic()
            finally:
                run.image_lock.release()
        return {"image": run.image, "source": run.mode, "capturedAt": run.image_captured_at}

    def wda_session(self):
        """The session WDA is serving now, creating one only if it has none."""
        with self.wda_session_lock:
            self.wda_session_id = resolve_wda_session(self.config.wda_url, preferred=self.wda_session_id)
            return self.wda_session_id

    def limitations(self, *, key, helper, device, ready, wda, can_act=None, notes=(), health=None, engine=engines.FAST,
                    engine_reason=None):
        """Why live runs are limited, most actionable first (the dashboard shows the first). ``key``: the
        default ``engine`` can run; ``engine_reason`` says why not."""
        reasons = []
        managed = getattr(self, "setup", None) is not None
        if not key:
            if engine == engines.SMART:
                reasons.append(engine_reason if engine_reason and engine_reason != engines.NO_OPENAI_KEY
                               else engines.no_key_reason(managed))
            else:
                reasons.append(engines.NO_JEV_KEY if managed else "Add TYPESAFE_API_KEY to the agent's env file")
        if not self.config.enable_live:
            reasons.append("Live runs are off; enable them to act on the phone")
        manager = getattr(self, "manager", None)
        if manager is not None and manager.expired():
            reasons.append("Mobster's helper needs a refresh: its signature expired. Refresh it in Setup")
        if device and not ready:
            reasons.append(health or (f"WebDriverAgent is not answering at {self.config.wda_url}" if wda
                                      else "Your iPhone is currently unreachable"))
        if notes:
            reasons += list(notes)
        elif not (wda or can_act):
            reasons.append("No iPhone attached: start the agent with --manage-device or --wda-url")
        if not helper and engine == engines.FAST:
            reasons.append("Text/recovery helper is not configured")
        return reasons

    def control_device(self, body, phone=None):
        """One gesture from the live view of ``phone`` (fleet.Phone; None: the primary phone). Never while a task
        holds that phone; tasks on other phones don't stop it."""
        fleet = getattr(self, "fleet", None)
        primary = phone is None or phone.primary
        control = self.control if primary else phone.control
        if control is None:
            if not primary and phone.kind == "usb":  # plugged in, not set up here yet
                raise APIError("Set this iPhone up in Setup first, then you can control it here", 409,
                               "control_unavailable")
            raise APIError("Direct control needs a USB iPhone", 404, "control_unavailable")
        if not self.config.enable_live:
            raise APIError("Turn on “Allow actions on your iPhone” in Settings to control it", 409, "live_disabled")
        with self.lock:
            busy = (bool(self.active_runs) if fleet is None else
                    self.run_on(fleet.primary if primary else phone) is not None)
        if busy:
            raise APIError("A task is using your iPhone. Stop it to take over.", 409, "device_busy")
        try:
            return control.perform(body)
        except ValueError:
            raise
        except Exception as exc:
            # The class and WDA's short reason only: no gesture content or typed text.
            print(f"mobster: direct control failed: {type(exc).__name__}: {str(exc)[:160]}", file=sys.stderr, flush=True)
            raise APIError("Your iPhone did not accept that. Check it is unlocked and connected.", 503,
                           "control_failed") from None

    def ask_before_acting(self):
        """Pause consequential actions for the user's approval (on unless turned off)."""
        return os.environ.get("MOBSTER_ASK_BEFORE_ACTING", "1") != "0"

    def bypass_checks(self):
        """Act when a check would stop an action the model is fairly sure of (off unless turned on)."""
        return os.environ.get("MOBSTER_BYPASS_CHECKS", "0") == "1"

    @staticmethod
    def video_quality():
        """(frames per second, detail) for the live view, from the saved settings."""
        try:
            fps = int(os.environ.get("MOBSTER_VIDEO_FPS", DEFAULT_QUALITY[0]))
        except ValueError:
            fps = DEFAULT_QUALITY[0]
        detail = os.environ.get("MOBSTER_VIDEO_DETAIL", DEFAULT_QUALITY[1])
        return (fps, detail) if fps in VIDEO_FPS and detail in VIDEO_DETAIL else DEFAULT_QUALITY

    def scripted_models(self):
        """True for a runtime that substitutes its own models (the terminal's scripted demo): it runs them
        on the Fast path and needs no key."""
        return type(self).open_models is not Runtime.open_models

    def default_engine(self):
        """The engine a task runs when it names none (engines.default_engine)."""
        return engines.FAST if self.scripted_models() else engines.default_engine()

    @staticmethod
    def task_cost_limit():
        """Settings › Usage › "Stop a task that reaches $N": USD, or None when turned off. On at $1 by default."""
        raw = os.environ.get("MOBSTER_TASK_COST_LIMIT", "").strip()
        if raw.casefold() == "off":
            return None
        try:
            value = float(raw) if raw else DEFAULT_TASK_COST_LIMIT
        except ValueError:
            return DEFAULT_TASK_COST_LIMIT
        return value if TASK_LIMIT_RANGE[0] <= value <= TASK_LIMIT_RANGE[1] else DEFAULT_TASK_COST_LIMIT

    @staticmethod
    def monthly_limit():
        """Settings › Usage › Monthly limit: USD, or None (no limit, the default)."""
        raw = os.environ.get("MOBSTER_MONTHLY_LIMIT", "").strip()
        try:
            value = float(raw) if raw else None
        except ValueError:
            return None
        return value if value is not None and MONTH_LIMIT_RANGE[0] <= value <= MONTH_LIMIT_RANGE[1] else None

    def month_usd(self):
        journal = getattr(self, "journal", None)
        try:
            return round(journal.month_usd(), 6) if journal is not None else 0.0
        except Exception:
            return 0.0

    def task_cap(self):
        """The spend cap a task stops at: the lower of the user's per-task limit and the operator's cap."""
        caps = [cap for cap in (self.task_cost_limit(), getattr(self.config, "spend_cap_usd", None)) if cap is not None]
        return min(caps) if caps else None

    def settings(self):
        value = {"askBeforeActing": self.ask_before_acting(), "bypassChecks": self.bypass_checks(),
                 "defaultEngine": self.default_engine(), "taskCostLimitUsd": self.task_cost_limit(),
                 "monthlyLimitUsd": self.monthly_limit()}
        if isinstance(getattr(self, "video", None), WdaVideo):
            value["videoFps"], value["videoDetail"] = self.video_quality()
        return value

    @property
    def keys(self):
        """Bring-your-own keys, saved to the same private env file as other settings."""
        env_file = self.setup.env_file if getattr(self, "setup", None) else getattr(self.config, "env_file", None)
        return Keys(env_file)

    def save_keys(self, body):
        """Keys.update. Saving Smart's key (the same one again, too) asks its provider afresh whether it can use
        Smart's model: a project the user just fixed must not wait out ModelReach's cached refusal."""
        state = self.keys.update(body)
        reach = getattr(self, "model_reach", None)
        if reach is not None and {"openai", "anthropic", "helper"} & set(body):
            reach.forget(engines.smart_key())
        return state

    def check_key(self, target):
        """Keys.test. Smart's key that passes can run Smart (the test generates, so it has credit too), so Smart
        takes tasks again at once; one whose account has no credit makes Smart unavailable until one passes."""
        model = engines.smart_model()
        # The key the test is about to read, when it is the one Smart runs on.
        key = engines.smart_key() if target == engines.model_provider(model) else None
        result = self.keys.test(target)
        reach = getattr(self, "model_reach", None)
        # The test takes up to a minute: a key saved meanwhile was never tested, and the next status checks it.
        if key and reach is not None and engines.smart_key() == key:
            if result.get("ok"):
                reach.record(key, True)
            elif result.get("problem") == "no_credit":
                reach.record(key, False, engines.no_credit(model))
        return result

    def update_settings(self, changes):
        allowed = {"askBeforeActing", "bypassChecks", "videoFps", "videoDetail", "defaultEngine", "taskCostLimitUsd",
                   "monthlyLimitUsd"}
        if not isinstance(changes, dict) or not changes or set(changes) - allowed:
            raise ValueError("Unsupported setting")
        saved = {}
        if "askBeforeActing" in changes:
            if type(changes["askBeforeActing"]) is not bool:
                raise ValueError("askBeforeActing must be true or false")
            saved["MOBSTER_ASK_BEFORE_ACTING"] = "1" if changes["askBeforeActing"] else "0"
        if "bypassChecks" in changes:
            if type(changes["bypassChecks"]) is not bool:
                raise ValueError("bypassChecks must be true or false")
            saved["MOBSTER_BYPASS_CHECKS"] = "1" if changes["bypassChecks"] else "0"
        if "defaultEngine" in changes:
            if changes["defaultEngine"] not in engines.ENGINES:
                raise ValueError("defaultEngine must be smart or fast")
            saved["MOBSTER_DEFAULT_ENGINE"] = changes["defaultEngine"]
        for name, env, (low, high) in (("taskCostLimitUsd", "MOBSTER_TASK_COST_LIMIT", TASK_LIMIT_RANGE),
                                       ("monthlyLimitUsd", "MOBSTER_MONTHLY_LIMIT", MONTH_LIMIT_RANGE)):
            if name not in changes:
                continue
            amount = changes[name]
            if amount is None:
                saved[env] = "off" if name == "taskCostLimitUsd" else ""
                continue
            if type(amount) not in (int, float) or not low <= amount <= high:
                raise ValueError(f"{name} must be null or between ${low:g} and ${high:,g}")
            saved[env] = f"{round(float(amount), 2):.2f}"
        if {"videoFps", "videoDetail"} & set(changes):
            if not isinstance(getattr(self, "video", None), WdaVideo):
                raise ValueError("Live video settings need a USB iPhone")
            fps, detail = self.video_quality()
            fps, detail = changes.get("videoFps", fps), changes.get("videoDetail", detail)
            if type(fps) is not int:
                raise ValueError("videoFps must be 15, 30 or 60")
            mjpeg_settings(fps, detail)  # validates both
            saved.update(MOBSTER_VIDEO_FPS=str(fps), MOBSTER_VIDEO_DETAIL=detail)
        env_file = self.setup.env_file if getattr(self, "setup", None) else getattr(self.config, "env_file", None)
        if env_file:
            update_env_file(env_file, saved)
        os.environ.update(saved)
        if "MOBSTER_VIDEO_FPS" in saved:
            self.video.set_quality(*self.video_quality())
            for video in (self.fleet.videos() if getattr(self, "fleet", None) is not None else ()):
                video.set_quality(*self.video_quality())
        return self.settings()

    def engines_status(self):
        """{smart, fast}: whether each engine can run now, why not, and its model."""
        managed = getattr(self, "setup", None) is not None
        key, model = engines.smart_key(), engines.smart_model()
        reach = getattr(self, "model_reach", None)
        if not key:
            smart = (False, engines.no_key_reason(managed, model))
        else:
            smart = reach.state(key, model) if reach is not None else (True, None)
        fast = (True, None) if engines.fast_ready() or self.scripted_models() else (
            False, engines.NO_JEV_KEY if managed else "Add TYPESAFE_API_KEY to the agent's env file")
        return {engines.SMART: {"available": smart[0], "reason": smart[1], "model": model,
                                "provider": engines.provider_name(model)},
                engines.FAST: {"available": fast[0], "reason": fast[1],
                               "model": os.environ.get("TYPESAFE_MODEL", "jev-latest"), "provider": engines.FAST_PROVIDER}}

    def status(self):
        jev = bool(os.environ.get("TYPESAFE_API_KEY"))
        helper = helper_configured()
        target = self.target_status()
        device, ready, health = target["device"], target["ready"], target["health"]
        wda = bool(self.config.wda_url)
        available = self.engines_status()
        default = self.default_engine()
        key = available[default]["available"]
        return {"api_version": "v1", "version": __version__, "jev_configured": jev, "helper_configured": helper,
                "engines": available, "defaultEngine": default,
                "helper_model": configured_model(), "helper_models": model_options(),
                "helper_model_rates": helper_model_rates(model_options(), os.environ.get("GOOGLE_CLOUD_LOCATION", "global"))
                    if os.environ.get("TEXT_MODEL_PROVIDER") == "vertex" else {},
                "live_enabled": bool(not self.closing and self.config.enable_live and key and ready and target["can_act"]),
                "device_ready": ready, "durable_history": getattr(self.config, "state_db", None) is not None,
                "screen_reading": target["screen_reading"], "ask_before_acting": self.ask_before_acting(),
                "bypass_checks": self.bypass_checks(),
                "spend_cap_usd": getattr(self.config, "spend_cap_usd", None),
                "devices": self.pool.public() if self.pool else [],
                "concurrency": {"capacity": self.pool.size if self.pool else self.device_count(),
                                "running": len(self.active_runs),
                                "available": self.capacity()},
                "device": "Your iPhone" if device else None,
                "driver": target["driver"],
                "extensions": dict(tracks.STATUS),
                "limitations": self.limitations(key=key, helper=helper, device=device, ready=ready, health=health,
                                                wda=wda, can_act=target["can_act"], notes=target["notes"],
                                                engine=default, engine_reason=available[default]["reason"])}

    def device_count(self):
        """How many devices this runtime can run tasks on at once (one task each), from what it knows already:
        the primary, plus every other device with a WebDriverAgent address. Never lists the devices."""
        fleet = getattr(self, "fleet", None)
        if fleet is None:
            return 1
        with fleet.lock:
            others = sum(1 for phone in fleet.phones.values() if phone.wda_url)
        return 1 + others

    def default_device_id(self):
        """The id of the device a task that names none runs on, or None while it has no identity."""
        fleet = getattr(self, "fleet", None)
        if fleet is None or self.pool is not None:
            return None
        try:
            return fleet.default().id
        except Exception:
            return None

    def resolve_default(self, sticky=True):
        """The device a task that names none runs on (Fleet.default)."""
        try:
            return self.fleet.default(sticky=sticky)
        except LookupError as error:
            raise APIError(str(error), 404, "device_not_found") from None

    def resolve_device(self, device):
        """The fleet.Phone a task runs on: the one ``device`` names (id, UDID or name), or the default device.
        APIError 404 device_not_found or 400 device_ambiguous."""
        try:
            return self.fleet.for_run(device)
        except DeviceNotFound as error:
            raise APIError(str(error), 404, "device_not_found") from None
        except AmbiguousDevice as error:
            raise APIError(str(error), 400, "device_ambiguous") from None

    def create(self, app_id, goal, mode, idempotency_key=None, output_schema=None, output_format="auto", helper_model=None,
                 allowed_bundles=None, dry_run=False, engine=None, device=None, default_device="last_used",
                 extras=None, origin="api"):
        """Admit a task. ``engine`` is smart or fast (None: the default engine, see ``default_engine``). A
        Smart task may open any app; its ``app_id`` is only where it starts (None: the Home Screen). ``device``
        names the device it runs on; one task per device, tasks on different devices run at once. With no
        ``device``, ``default_device`` says where it runs: "last_used" (a task someone starts now: the default
        device, Fleet.default) or "primary" (a saved task, which never follows the phone used last:
        Fleet.default(sticky=False)).

        ``extras``: registered run fields (harness_api.register_run_field), each validated, part of the
        fingerprint and kept on the run. ``origin``: where the request came from (harness_api.ORIGINS); an "mcp"
        run can't start in an app on harness_api.UNATTENDED_DENY, and its guard never opens one."""
        if engine is not None and engine not in engines.ENGINES:
            raise ValueError("engine must be smart or fast")
        if origin not in harness_api.ORIGINS:
            raise ValueError("Unknown task origin")
        extras = harness_api.clean_extras(extras, self)
        if default_device not in ("last_used", "primary"):
            raise ValueError("default_device must be last_used or primary")
        fleet = getattr(self, "fleet", None)
        phone = None
        if device is not None and (fleet is None or self.pool is not None):
            raise ValueError("This Mobster runs every task on its own device; leave device out")
        if fleet is not None and self.pool is None:
            # Resolved before the runtime's lock: naming a device reads the USB listing and the registries.
            if device is not None:
                phone = self.resolve_device(device)
            elif mode != "live" or self.one_target:
                phone = fleet.primary  # a demo; or `mobster run` and the terminal UI, whose target is explicit
            elif default_device == "primary":
                phone = self.resolve_default(sticky=False)
            else:
                phone = self.resolve_device(None)
        primary = phone is None or phone.primary
        chosen = engine
        if mode == "live" and chosen is None:
            # Dry runs preview a compiled loop, which only Fast has.
            chosen = engines.FAST if dry_run is True else self.default_engine()
        if chosen == engines.SMART and dry_run is True:
            raise ValueError("A dry run needs the Fast engine")
        if app_id is None and chosen == engines.SMART and mode == "live":
            app_id = ANY_APP["id"]
        if (not isinstance(app_id, str) or not isinstance(goal, str) or not 1 <= len(goal.strip()) <= 4000
                or not isinstance(mode, str) or mode not in {"demo", "live"}):
            raise ValueError("Choose an app, a task (1–4000 characters), and demo/live mode")
        if idempotency_key is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", idempotency_key):
            raise ValueError("Invalid Idempotency-Key")
        output_schema = validate_output(output_format, output_schema)
        validate_model_request(helper_model)
        if allowed_bundles is not None:
            if not isinstance(allowed_bundles, list) or not allowed_bundles or len(allowed_bundles) > 31:
                raise ValueError("allowedBundles must be 1-31 bundle identifiers")
            try:
                extra_bundles = frozenset(validate_bundle_id(bundle) for bundle in allowed_bundles)
            except ValueError:
                raise ValueError("allowedBundles must be valid bundle identifiers") from None
        else:
            extra_bundles = None
        identity = [app_id, goal.strip(), mode, output_schema]
        # Preserve existing JSON request tombstones; another format is a distinct request.
        if output_format != "json":
            identity.append({"outputFormat": output_format})
        if helper_model is not None:
            identity.append({"helperModel": helper_model})
        if extra_bundles is not None:
            identity.append({"allowedBundles": sorted(extra_bundles)})
        if type(dry_run) is not bool:
            raise ValueError("dryRun must be true or false")
        if dry_run:
            identity.append({"dryRun": True})
        if engine is not None:
            identity.append({"engine": engine})
        if device is not None and phone is not None:
            identity.append({"device": phone.id})  # by its id, however the request named it
        if extras:
            identity.append({"extras": extras})
        fingerprint = hashlib.sha256(json.dumps(identity,
            ensure_ascii=True, sort_keys=True, allow_nan=False).encode()).hexdigest()
        with self.lock:
            if self.closing:
                raise APIError("The service is shutting down", 503, "shutting_down")
            if idempotency_key:
                prior = self.journal.lookup(idempotency_key)
                if prior:
                    if prior[1] != fingerprint:
                        raise APIError("This request key belongs to a different task", code="idempotency_conflict")
                    if prior[0] not in self.runs:
                        raise APIError("This previous task has expired from history; it was not restarted", 410, "run_expired")
                    return self.runs[prior[0]], True
            selected_helper_model = resolve_helper_model(helper_model)
            if phone is not None:
                busy = self.run_on(phone)
                if busy is not None:
                    raise APIError("A task is running on this device; stop it or wait for it to finish, or "
                                   "choose another device" if not primary or len(self.active_runs) > 1 else
                                   "Every assigned phone is busy; stop a run or wait for one to finish",
                                   code="run_active", activeRunId=busy)
            elif not self.capacity():
                raise APIError("Every assigned phone is busy; stop a run or wait for one to finish",
                               code="run_active", activeRunId=self.active)
            catalog = self.apps_for(phone)
            app = ANY_APP if app_id == ANY_APP["id"] and chosen == engines.SMART else next(
                (a for a in catalog if a["id"] == app_id), None)
            if not app:
                raise ValueError("Choose an app from your catalog")
            if origin == "mcp" and not harness_api.unattended_allowed(app.get("bundleId")):
                raise APIError(NOT_UNATTENDED, 400, "not_unattended")
            if dating_app_loop(goal, app.get("bundleId")):
                # Every engine, before any tap: compiled loops and Quick mode refuse it too, but Smart has no loop
                # compiler, so the refusal is here, where every task starts. One action there is not a loop.
                raise APIError(DATING_REFUSAL, 400, "dating_app_loop")
            allowed = None
            if extra_bundles is not None:
                known = {a["bundleId"] for a in catalog if isinstance(a.get("bundleId"), str)}
                unknown = sorted(extra_bundles - known)
                if unknown:
                    raise ValueError("Unknown app bundle: " + ", ".join(unknown[:5]))
                allowed = frozenset({app["bundleId"]} | extra_bundles)
            if mode == "live" and primary:
                status = self.status()
                state = status.get("engines", {}).get(chosen) or {"available": True, "reason": None}
                if not state["available"]:
                    # Never another engine instead: the user chose this one (or it is their default).
                    raise APIError(state["reason"] or "This engine can't run right now", 409, "engine_unavailable",
                                   engine=chosen)
                usable = status["live_enabled"] if chosen == status.get("defaultEngine", chosen) else (
                    status.get("device_ready") and self.config.enable_live and not self.closing)
                if not usable:
                    locked = self.config.enable_live and not self.closing and phone_locked(self.manager)
                    if locked and not waits_for_unlock(origin, idempotency_key):
                        # The device manager's probe saw the lock: say so, not "not ready" (PLAN §3.9).
                        raise APIError(PHONE_LOCKED, 503, "device_unavailable", stop="phone_locked")
                    if not locked:
                        raise APIError("Your iPhone is not ready for live tasks. Try again when it is available.",
                                       503, "device_unavailable")
                    # Locked, and nothing else is wrong: a person started this task, so it waits for them to unlock
                    # the phone at its preflight (lockscreen.UNLOCK_WAIT_S) instead of being refused.
            elif mode == "live":
                # Another device: its own readiness, the engines and settings every device shares.
                state = self.engines_status().get(chosen) or {"available": True, "reason": None}
                if not state["available"]:
                    raise APIError(state["reason"] or "This engine can't run right now", 409, "engine_unavailable",
                                   engine=chosen)
                if not self.config.enable_live:
                    raise APIError("Live runs are off; enable them to act on the phone", 503, "device_unavailable")
                target = phone.target_status()
                if not (target["ready"] and target["can_act"]):
                    locked = phone_locked(getattr(phone, "manager", None))
                    if locked and not waits_for_unlock(origin, idempotency_key):
                        raise APIError(f"{phone.name or 'This iPhone'} is locked. Unlock it and try again. "
                                       "No action was taken.", 503, "device_unavailable", device=phone.id,
                                       stop="phone_locked")
                    if not (locked and target["can_act"]):
                        raise APIError(f"{phone.name or 'This device'} is not ready for live tasks. "
                                       + (target.get("health") or "Try again when it is available."), 503,
                                       "device_unavailable", device=phone.id)
                    # Locked only: waits for the unlock at its preflight, like the first iPhone's.
            if mode == "live":
                limit, spent = self.monthly_limit(), None
                if limit is not None:
                    spent = self.month_usd()
                    if spent >= limit:
                        raise APIError(f"This month's spend has reached your ${limit:,.2f} limit. Raise it in "
                                       "Settings › Usage to run more tasks.", 409, "monthly_limit_reached",
                                       monthUsd=spent, monthlyLimitUsd=limit)
                if app is not ANY_APP:
                    if primary:
                        self.check_app(app)
                    elif app.get("installed") is False:
                        raise ValueError(f"{app['name']} isn't installed on {phone.name or 'this device'}")
            lease = None
            if mode == "live" and self.pool is None:
                # No pool means a WDA target, which still needs its own exclusive lease: one per device.
                try:
                    lease = Lease.device((self.config.wda_url if primary else phone.wda_url).rstrip("/"))
                except JournalError:
                    raise APIError("Your iPhone is busy in another Mobster process" if primary else
                                   f"{phone.name or 'This device'} is busy in another Mobster process",
                                   code="device_busy") from None
            run = Run(app, goal.strip(), mode, idempotency_key=idempotency_key, output_schema=output_schema,
                      output_format=output_format, helper_model=selected_helper_model, allowed_bundles=allowed,
                      journal=self.journal, lease=lease, dry_run=dry_run,
                      phone=None if primary else phone,
                      device_id=phone.id if phone is not None and mode == "live" else None,
                      device_name=phone.name if phone is not None and mode == "live" else None,
                      engine=chosen if mode == "live" else None, frames=getattr(self, "frames", None),
                      estimate_usd=self.estimate_for(chosen, goal.strip(), output_schema, selected_helper_model,
                                                     output_format) if mode == "live" else None,
                      extras=extras, origin=origin, listeners=getattr(self, "listeners", None))
            device = None
            if mode == "live" and self.pool is not None:
                # Reserve the phone at admission, not at dispatch: a task that cannot
                # be given a device must be refused now, while the caller is waiting.
                try:
                    device = self.pool.acquire(run.id, timeout=0)
                except NoDeviceAvailable:
                    elsewhere = any("another Mobster process" in item["reason"] for item in self.pool.public())
                    if lease:
                        lease.close()
                    raise APIError(
                        "Your iPhone is busy in another Mobster process" if elsewhere else
                        "Every assigned phone is busy; stop a run or wait for one to finish",
                        code="device_busy" if elsewhere else "run_active",
                        activeRunId=self.active) from None
                run.device = device
                run.lease = PooledLease(self.pool, run.id)
            try:
                expired = self.journal.create(run.metadata(), idempotency_key, fingerprint)
            except Exception:
                if run.lease:
                    run.lease.close()
                raise
            for identifier in expired:
                self.runs.pop(identifier, None)
                if getattr(self, "frames", None) is not None:
                    self.frames.delete(identifier)
            self.runs[run.id] = run
            self.active_runs[run.id] = device.id if device else phone.key if phone is not None else None
            if run.listeners is not None:
                run.listeners.post("on_created", run, self)
            self.publish("runs", {"event": "run_created", "runId": run.id,
                                  **({"threadId": extras["threadId"]} if extras.get("threadId") else {}),
                                  **({"device": run.device_id} if run.device_id else {})})
            thread = threading.Thread(target=self.work, args=(run,), daemon=True)
            self.threads.add(thread)
            try:
                thread.start()
            except Exception:
                self.threads.discard(thread)
                self.active_runs.pop(run.id, None)
                if lease:
                    lease.close()
                run.finish({"status": "interrupted", "independently_verified": False,
                            "reason": "The task worker could not start. No agent action was dispatched."})
                raise APIError("The task could not start; check its saved status before retrying", 503, "start_failed") from None
        if phone is not None and mode == "live":
            fleet.mark_used(phone)
        return (run, False) if idempotency_key else run

    def smart_apps(self, run):
        """{bundle: name} a Smart task may open: every app the phone has (or, without an inventory, the
        catalog's), the start app first."""
        apps = {}
        if run.app.get("bundleId"):
            apps[run.app["bundleId"]] = run.app["name"]
        for app in self.apps_for(self.phone_for(run)):
            bundle = app.get("bundleId")
            if bundle and app.get("installed") is not False and bundle not in apps:
                apps[bundle] = app["name"]
        return dict(list(apps.items())[:MAX_SMART_APPS])

    def estimate_for(self, engine, goal, output_schema=None, helper_model=None, output_format=None):
        """The single figure the dashboard showed before the task (USD), for comparison after it."""
        try:
            estimates = self.engine_estimates(goal, output_schema, helper_model, output_format=output_format)
            return engines.midpoint(estimates[engine]) if engine in estimates else None
        except Exception:
            return None

    def planning(self, goal="", output_schema=None, helper_model=None):
        """Fast's published-rate planning range (costs.planning_estimate)."""
        return planning_estimate(os.environ.get("TYPESAFE_MODEL", "jev-latest"), helper_model,
                                 os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
                                 if os.environ.get("TEXT_MODEL_PROVIDER") == "vertex" else None,
                                 goal=goal, output_schema=output_schema)

    def engine_estimates(self, goal="", output_schema=None, helper_model=None, planning=None, output_format=None):
        """{smart, fast}: each engine's range for ``goal`` with whether it can run (EngineEstimate). Smart adds
        its structuring call for a schema, or for CSV, JSON or YAML it structures itself (``output_format``)."""
        state = self.engines_status()
        names = [app["name"] for app in self.apps() if app.get("installed") is not False]
        planning = planning if planning is not None else self.planning(goal, output_schema, helper_model)
        structured = output_schema is not None or output_format in engines.AUTOMATIC_FORMATS
        return {engines.SMART: engines.smart_estimate(goal, names, available=state[engines.SMART]["available"],
                                                      reason=state[engines.SMART]["reason"], structured=structured,
                                                      model=state[engines.SMART]["model"]),
                engines.FAST: engines.fast_estimate(planning, available=state[engines.FAST]["available"],
                                                    reason=state[engines.FAST]["reason"],
                                                    model=state[engines.FAST]["model"])}

    def run_smart(self, run, trace, setup):
        """A Smart task: the frozen frontier loop (engines.SMART_CONFIG, on engines.smart_model) on this phone.
        Returns its result, or None when it was stopped before starting. ``setup`` as in ``open_driver``.

        Seam S6.2: the tracks' context providers gather while the phone gets ready (2 s budget), their tools join
        the default skills, pre-run hooks may finish the run or hand it a starting state, and the frontier gets
        the steering queue, the clarify asker and the step and checkpoint hooks. With nothing registered this is
        the loop as before."""
        key = engines.smart_key()
        ledger = SpendLedger()
        with trace.span("setup.models"):
            client = engines.acquire_client(key)
        events = engines.SmartEvents(run.emit)

        def inference(event):
            ledger.add_event(event)
            events.inference(event)

        engines.meter(client, inference)
        ok = False
        try:
            ctx = self.run_context(run)
            gather = ContextGather(ctx, run.emit)
            driver = self.open_driver(run, trace, setup)
            if driver is None:
                return None
            run.capture = getattr(driver, "capture_preview", None)
            events.driver, events.apps = driver, self.smart_apps(run)
            approve = events.approving(run.request_approval) if self.ask_before_acting() else None
            handoff = None
            for hook in harness_api.pre_runs(ctx):
                if run.stop.is_set():
                    return None
                try:
                    done = hook(ctx, driver, approve)
                except Exception as error:  # noqa: BLE001 -- a track's hook never breaks the run
                    harness_api.log.exception("a pre-run hook failed")
                    run.emit({"event": "context_error", "key": "pre_run", "error": type(error).__name__})
                    continue
                if done is None:
                    continue
                if done.result is not None:
                    result = {**done.result, "costUsd": run.cost_usd}
                    run.emit(result)
                    ok = True
                    return result
                if done.handoff is not None:
                    handoff = done.handoff
                    break
            blocks, providers = gather.join()
            hooks = {}
            if blocks:
                hooks["context_blocks"] = blocks
            if providers:
                hooks["turn_context"] = gather.turn_context(providers)
            extra_tools = harness_api.tools(ctx)
            if extra_tools:
                from .frontier import default_skills
                hooks["skills"] = tuple(default_skills()) + tuple(extra_tools)
            try:
                hooks.update(harness_api.frontier_options(ctx))
            except Exception as error:  # noqa: BLE001
                harness_api.log.exception("frontier options failed")
                run.emit({"event": "context_error", "key": "frontier_options", "error": type(error).__name__})
            listeners = run.listeners if harness_api.listeners() else None
            if listeners is not None:
                # Only the hooks a registered listener takes: the checkpoint hook puts each turn's state through the
                # secret filter on the agent's thread (S7.3), a cost no task should carry for listeners that never
                # read it (harness keeps its own checkpoints off that thread).
                wanted = {name for listener in harness_api.listeners() for name in ("on_step", "on_checkpoint")
                          if callable(getattr(listener, name, None))}
                if "on_step" in wanted:
                    hooks["step_observer"] = lambda record: listeners.post("on_step", run, record)
                if "on_checkpoint" in wanted:
                    hooks["checkpoint"] = lambda state: listeners.post("on_checkpoint", run, state)
            agent = engines.build_frontier(driver, client, apps=events.apps, emit=events, max_cost_usd=self.task_cap(),
                                           approve=approve, cancelled=run.stop.is_set, steering=run.steering,
                                           clarify=ctx.clarify, run_context=ctx, **hooks,
                                           **frontier_guard(run.guard))
            events.agent = agent
            if run.stop.is_set():
                return None  # The loop's first step is a model call (the task contract), billed even if stopped.
            setup.phase("running")
            request = f"In {run.app['name']}: {run.goal}" if run.app.get("bundleId") else run.goal
            with trace.span("smart.run"):
                outcome = agent.run(request, initial=handoff) if handoff is not None else agent.run(request)
            events.finish()
            run.private = {"notes": list(outcome.get("notes") or ()), "plan": outcome.get("plan") or ""}
            if run.stop.is_set() and outcome.get("status") not in ("approval_denied", "approval_timeout"):
                outcome = {**outcome, "status": "stopped"}
            result = engines.summarize(outcome, agent, events, request=request, output_format=run.output_format,
                                       output_schema=run.output_schema, cost_usd=run.cost_usd)
            self.learn_reach(key, client, outcome.get("reason"), result.get("structure_error"))
            result["costUsd"] = run.cost_usd
            run.emit(result)
            ok = True
            return result
        finally:
            engines.release_client(client, ok)

    def learn_reach(self, key, client, *errors):
        """What a Smart run says about its key (ModelReach): an error that says the key can't run Smart (a
        rejection, or an empty account) makes Smart unavailable with that reason; a run whose model calls went
        through, the last one included, clears any refusal, an empty account's too."""
        reach = getattr(self, "model_reach", None)
        if reach is None:
            return
        for error in errors:
            ok, why = engines.model_failure(error)
            if ok is False:
                reach.record(key, False, why)
                return
        if (getattr(client, "usage", None) or {}).get("calls") and not getattr(client, "model_failed", True):
            reach.record(key, True)

    def work(self, run):
        driver = model = helper = judge = None
        trace = Trace()
        phase = "preparing"
        result = {"status": "stopped", "independently_verified": False,
                  "reason": "The task stopped before executing agent actions."}

        def set_phase(value):
            nonlocal phase
            phase = value

        def hold(value):
            nonlocal driver
            driver = value
            return value

        try:
            if run.stop.is_set():
                return
            run.status = "running"
            run.emit({"event": "run_started", "mode": run.mode, "app": run.app["name"], "engine": run.engine})
            if run.mode == "demo":
                result = self.demo(run)
                return
            if run.engine == engines.SMART:
                done = self.run_smart(run, trace, RunSetup(phase=set_phase, hold=hold))
                if done is not None:
                    result = done
                return
            # Model clients first: their connections (and the gcloud token) open
            # while the driver is configured, the phone is checked and the app
            # launches, instead of during the first observation.
            ledger = SpendLedger()

            def track(event):
                ledger.add_event(event)
                run.emit(event)

            with trace.span("setup.models"):
                model, helper, judge = self.open_models(run, track)

            ready = self.open_driver(run, trace, RunSetup(phase=set_phase, hold=hold))
            if ready is None:
                return
            driver = ready
            run.capture = getattr(driver, "capture_preview", None)

            def emit(event):
                if driver.last_image:
                    run.image = driver.last_image
                run.emit(event)

            # Screen reading is an operator gate on the whole runtime, applied per
            # run against the driver that is actually observing the phone.
            visual = build_visual(enabled=getattr(self.config, "read_screen", False),
                                  driver=driver, cancelled=run.stop.is_set, **self.visual_options(run))
            agent = Agent(driver, model, helper, emit=emit, cancelled=run.stop.is_set, visual=visual,
                          spend_cap_usd=self.task_cap(), spend_ledger=ledger,
                          allowed_bundles=run.allowed_bundles, replay_store=self.replay_store,
                          approve=run.request_approval if self.ask_before_acting() else None,
                          ask=run.request_approval, vision_judge=judge, loop_store=self.loop_store,
                          loop_mode="dry_run" if run.dry_run else "auto", trace=trace,
                          decision_memo=self.decision_memo, launch_bundle=run.app["bundleId"],
                          bypass=self.bypass_checks())
            # Screens for the step list and proof (the agent attaches ``_frame``; Run.emit stores it).
            agent.capture_frames = True
            phase = "running"
            result = agent.run(f"In {run.app['name']}: {run.goal}", execute=not run.dry_run,
                               output_schema=run.output_schema, output_format=run.output_format)
            result = {**result, "engine": engines.FAST, "costUsd": run.cost_usd}
        except Exception as exc:
            inactive_app = phase == "reading_app" and isinstance(exc, DriverRejection) and exc.code == "app_not_active"
            outdated_bridge = phase == "reading_app" and isinstance(exc, DriverRejection) and exc.code == "bridge_update_required"
            result = {"event": "result", "status": "stopped" if run.stop.is_set() else "error",
                "independently_verified": False, "error_type": type(exc).__name__,
                "native_code": exc.code if isinstance(exc, DriverRejection) else None,
                "actions_may_have_run": phase in {"running", "launching"},
                "reason": ("The task was stopped while preparing the app." if run.stop.is_set() and phase == "preparing" else
                           str(exc) if isinstance(exc, PhoneNotReady) else
                           "The app launch could not be confirmed. It may have opened; the launch was not retried." if phase == "launching" else
                           f"iOS has not activated {run.app['name']}. Its accessibility bridge is responding, but the app is inactive. No agent action was dispatched." if inactive_app else
                           f"{run.app['name']} is still running an outdated interaction bridge. Close and reopen the app to load the installed update. No agent action was dispatched." if outdated_bridge else
                           f"Could not read {run.app['name']} on your iPhone. Check for a locked screen or a system prompt, then try again. No agent action was dispatched." if phase == "reading_app" else
                           "The task could not prepare your app. No agent action was dispatched." if phase == "preparing" else
                           "The task stopped after a service error. An in-flight action may have run; it was not retried.")}
            if isinstance(exc, PhoneNotReady) and not run.stop.is_set():
                result["stop_code"] = exc.code  # phone_locked, unplugged ... (api_errors.PHONE_STOP_CODES)
            if run.mode == "live":
                result.update(engine=run.engine, costUsd=run.cost_usd,
                              outcome="stopped" if run.stop.is_set() else "couldnt_finish", answer=None, proof=[])
        finally:
            self.awake.release(run.id)
            if run.mode == "live" and driver and not run.stop.is_set() and not self.closing:
                with trace.span("finish.preview"):
                    self.preview(run, final=True)
            if run.guard is not None and driver is not None:
                try:
                    run.guard.finish(driver)  # relocks only a phone this guard unlocked
                except Exception:
                    pass
            with trace.span("finish.close"):
                close_all(judge, model, helper, driver)
            if run.mode != "demo":
                try:
                    run.emit(trace.event())
                except Exception:
                    pass  # Telemetry never changes a run's outcome.
            try:
                run.finish(result)
            finally:
                if run.lease:
                    run.lease.close()
                    run.lease = None
                with self.lock:
                    self.active_runs.pop(run.id, None)
                    self.threads.discard(threading.current_thread())
                try:
                    self.finished(run)
                except Exception:  # noqa: BLE001 -- listeners and the bus never change a run's outcome
                    harness_api.log.exception("run_finished listeners failed")

    def finished(self, run):
        """After ``run.finish``: on_finished(run, summary, private) for the listeners, then the bus's run_finished,
        in that order, on the listeners' thread (S6.4)."""
        listeners = getattr(run, "listeners", None)
        thread_id = (run.extras or {}).get("threadId") if isinstance(getattr(run, "extras", None), dict) else None
        event = {"event": "run_finished", "runId": run.id, "status": run.status,
                 **({"threadId": thread_id} if thread_id else {})}
        if listeners is None:
            self.publish("runs", event)
            return
        listeners.post("on_finished", run, run.summary, dict(run.private or {}))
        listeners.call(lambda: self.publish("runs", event))

    def publish(self, topic, event):
        """Publish on the event bus (``GET /api/events``); never raises."""
        try:
            (getattr(self, "bus", None) or eventbus.bus).publish(topic, event)
        except Exception:  # noqa: BLE001
            harness_api.log.exception("event bus publish failed")

    def stop_run(self, identifier):
        with self.lock:
            run = self.runs.get(identifier)
            if not run:
                raise APIError("Task not found", 404, "not_found")
            with run.condition:
                if run.finished_at is None and not run.stop.is_set():
                    run.stop.set()
                    run.emit({"event": "stop_requested"})
            return run

    def delete_run(self, identifier):
        """Remove a finished run from history (its events and frames). A running task must be stopped first."""
        with self.lock:
            run = self.runs.get(identifier)
            if not run:
                raise APIError("Task not found", 404, "not_found")
            if run.finished_at is None:
                raise APIError("Stop this task before deleting it", 409, "run_active")
            self.journal.delete(identifier)
            self.runs.pop(identifier, None)
        if getattr(self, "frames", None) is not None:
            self.frames.delete(identifier)

    def save_run_gif(self, identifier, theme="paper", redact=False, mp4=False, directory=None):
        """A finished task as a captioned GIF in ~/Downloads (run_gif), and an MP4 beside it with ``mp4`` when
        ffmpeg is installed. Returns {name, path, frames, bytes, seconds, mp4}: ``mp4`` is {name, path, bytes},
        or None when it wasn't asked for or ffmpeg is missing. APIError 409 run_active while it runs, 404
        no_frames without screens, 422 gif_too_long, 429 export_busy while another is rendering, 403
        downloads_denied."""
        from . import run_gif
        with self.lock:
            run = self.runs.get(identifier)
        if not run:
            raise APIError("Task not found", 404, "not_found")
        if run.finished_at is None:
            raise APIError("This task is still running. Save it as a GIF once it ends.", 409, "run_active")
        frames = getattr(self, "frames", None)

        def read(frame_id):
            return frames.get(run.id, frame_id) if frames is not None else None

        if not GIF_RENDERS.acquire(blocking=False):
            raise APIError("Mobster is saving another GIF. Try again in a moment.", 429, "export_busy")
        try:
            public = run.public()
            try:
                clip = run_gif.build_clip(public, read, theme=theme, redact=redact)
            except ValueError as error:
                if str(error) == run_gif.NO_SCREENS:
                    raise APIError(str(error), 404, "no_frames") from None
                raise APIError(str(error), 422, "gif_too_long") from None
            stem = run_gif.file_stem(public.get("goal"))
            saved = save_export_bytes(stem, "gif", clip.gif, directory)
            result = {**saved, "frames": clip.frames, "bytes": len(clip.gif), "seconds": round(clip.seconds, 1),
                      "mp4": None}
            if mp4:
                data = run_gif.build_mp4(public, read, theme=theme, redact=redact)
                if data is not None:
                    # The GIF's own name, so the two sit together ("… (1).gif" and "… (1).mp4").
                    video = save_export_bytes(Path(saved["name"]).stem, "mp4", data, directory)
                    result["mp4"] = {**video, "bytes": len(data)}
            return result
        finally:
            GIF_RENDERS.release()

    def review_run(self, identifier, ok):
        """Mark a finished run's result as checked by the user ("Mark as OK")."""
        with self.lock:
            run = self.runs.get(identifier)
        if not run:
            raise APIError("Task not found", 404, "not_found")
        if run.finished_at is None:
            raise APIError("This task is still running", 409, "run_active")
        with run.condition:
            if run.journal:
                run.journal.annotate(run.id, {"reviewedOk": ok})
            run.reviewed_ok = ok
        return run

    def close(self, timeout=25):
        with self.lock:
            self.closing = True
        if self.scheduler and not self.scheduler.close(timeout):
            return False  # Keep journal/device ownership while a scheduler dispatch may still be in flight.
        self.video.close()
        if getattr(self, "control", None) is not None:
            self.control.close()
        if getattr(self, "fleet", None) is not None:
            self.fleet.close(keep_runner=getattr(self.config, "keep_runner", False))
        if self.manager is not None:
            if getattr(self.config, "keep_runner", False):
                # A development reload: the runner and relay run in their own process
                # groups and outlive this process; the next server adopts them.
                self.manager.watching.set()
            else:
                self.manager.close()
        with self.lock:
            self.closing = True
            threads = list(self.threads)
            for run in self.runs.values():
                if run.finished_at is None:
                    run.stop.set()
        self.awake.close()
        deadline = time.monotonic() + timeout
        for thread in threads:
            thread.join(max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in threads):
            # Keep leases/journal alive until process exit: never permit a new owner while
            # an old worker might still dispatch. Restart recovery will mark it interrupted.
            return False
        self.close_services()
        if getattr(self, "listeners", None) is not None:
            self.listeners.close()
        if getattr(self, "frames", None) is not None:
            self.frames.close()
        self.journal.close()
        return True

    def demo(self, run):
        # An interaction preview, explicitly synthetic and independent of the submitted goal.
        # No fabricated Jev confidence, inference timing, cost, or task-success claims.
        steps = [
            {"event": "observation", "source": "demo", "text": "Example app home screen", "elements": []},
            {"event": "decision", "operation": "TAP", "target": "Search", "model": "Demo replay",
             "confidence": None, "latency_ms": None},
            {"event": "action_started", "operation": "TAP", "target": "Search"},
            {"event": "action_acknowledged", "act_ms": None},
            {"event": "observation_after_action", "changed": True, "settle_ms": None},
            {"event": "helper", "purpose": "Example task planning", "calls": 0},
            {"event": "observation", "source": "demo", "text": "Example search screen", "elements": []},
            {"event": "decision", "operation": "TYPE", "target": "Search field", "model": "Demo replay",
             "confidence": None, "latency_ms": None},
            {"event": "action_started", "operation": "TYPE", "target": "Search field"},
            {"event": "action_acknowledged", "act_ms": None},
            {"event": "observation_after_action", "changed": True, "settle_ms": None},
        ]
        for step in steps:
            if run.stop.wait(.35):
                status = "stopped"
                break
            run.emit({**step, "synthetic": True})
        else:
            status = "demo_complete"
        result = {"event": "result", "status": status,
                       "actions": sum(e["event"] == "action_acknowledged" for e in run.events),
                       "decisions": sum(e["event"] == "decision" for e in run.events), "elapsed_ms": (time.time() - run.created_at) * 1000,
                       "reason": "Synthetic interface replay. Your task was not executed on a device.",
                       "independently_verified": False, "helper_calls": 0}
        run.emit(result)
        return result


class ContextGather:
    """The tracks' context providers for one Smart run (seam S6.2): each provider's ``blocks(ctx)`` runs on its own
    thread while the phone gets ready, and ``join`` waits for them until 2 s after the gather began. A provider that
    misses that is skipped with {"event": "context_error", "key", "error": "timeout"}; one that raises, with its
    exception's type. {"event": "context_used", "keys", "chars"} says what was used (never the text)."""

    BUDGET = 2.0

    def __init__(self, ctx, emit, budget=None):
        self.ctx, self.emit = ctx, emit
        self.budget = self.BUDGET if budget is None else budget
        self.started = time.monotonic()
        self.providers = harness_api.context_providers(ctx)
        self.jobs = []
        for provider in self.providers:
            box = {}
            thread = threading.Thread(target=self._collect, args=(provider, box), daemon=True,
                                      name=f"mobster-context-{getattr(provider, 'key', '?')}")
            thread.start()
            self.jobs.append((provider, box, thread))

    def _collect(self, provider, box):
        try:
            box["blocks"] = list(provider.blocks(self.ctx) or ())
        except BaseException as error:  # noqa: BLE001
            box["error"] = type(error).__name__

    def join(self):
        """(stable and first-turn blocks, the providers that answered in time)."""
        deadline = self.started + self.budget
        blocks, ready, used, chars = [], [], [], 0
        for provider, box, thread in self.jobs:
            thread.join(max(0.0, deadline - time.monotonic()))
            key = str(getattr(provider, "key", "") or "")[:40]
            if thread.is_alive():
                self.emit({"event": "context_error", "key": key, "error": "timeout"})
                continue
            if "error" in box:
                self.emit({"event": "context_error", "key": key, "error": box["error"]})
                continue
            ready.append(provider)
            cap = getattr(provider, "max_chars", None)
            for block in box.get("blocks", ()):
                if not isinstance(block, harness_api.ContextBlock) or not block.text:
                    continue
                if isinstance(cap, int) and cap > 0 and len(block.text) > cap:
                    block = harness_api.ContextBlock(block.key, block.title, block.text[:cap - 1] + "…",
                                                     block.stable, block.untrusted, block.images)
                blocks.append(block)
                chars += len(block.text)
                if key not in used:
                    used.append(key)
        if blocks:
            self.emit({"event": "context_used", "keys": used, "chars": chars})
        return blocks, ready

    def turn_context(self, providers):
        """``turn_context(TurnState)`` for the frontier: every ready provider's turn blocks; one that raises is
        skipped for that turn."""
        ctx = self.ctx

        def turn(state):
            out = []
            for provider in providers:
                try:
                    out.extend(provider.turn_blocks(ctx, state) or ())
                except Exception:  # noqa: BLE001
                    harness_api.log.exception("a provider's turn_blocks failed")
            return [block for block in out if isinstance(block, harness_api.ContextBlock) and block.text]
        return turn


DEVICE_PATH = re.compile(r"/api/devices/([^/]{1,128})(?:/(stream|frame|video/status|info|setup|control)"
                         r"|/setup/(build|start|stop|refresh))?")


def api_path(path):
    """Route /v1/* to the same handlers as /api/*. Unversioned paths keep working."""
    return "/api/" + path[len("/v1/"):] if path.startswith("/v1/") else path


# Registered routes that need the Mac app's session when the sidecar has one (S6.6).
APP_ONLY_ROUTES = frozenset({("POST", "/api/phone/files/put"), ("POST", "/api/wireless/reset")})


def make_handler(runtime, token=None, app_session=None):
    """The API's request handler. ``serve`` always passes the per-launch ``token``;
    ``None`` (tests that build a handler directly) leaves requests unauthenticated.

    ``app_session`` (seam S6.6): the Mac app's second per-launch secret, which only its webview holds (never the
    token file). With one, approving an action, a choice on a question, changing Ask before acting or the checks
    bypass, putting a file on the phone and resetting the Wi-Fi link need ``X-Mobster-App-Session``; everything
    else stays token-only. Without one (`mobster serve`), the API is as documented."""
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def send_json(self, value, status=200):
            data = json.dumps(value, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (OSError, TimeoutError):
                self.close_connection = True

        def allowed_origins(self):
            return {f"http://localhost:{runtime.config.port}", f"http://127.0.0.1:{runtime.config.port}",
                    # The desktop app's webview (macOS/Linux, then Windows).
                    *DESKTOP_ORIGINS}

        def authorized(self):
            """The per-launch token: ``Authorization: Bearer`` on any request, or ``?token=``
            on a GET (EventSource and <img> cannot send headers)."""
            if token is None:
                return True
            header = self.headers.get("Authorization", "")
            supplied = header[7:] if header[:7].lower() == "bearer " else ""
            if not supplied and self.command == "GET":
                supplied = (parse_qs(urlsplit(self.path).query).get("token") or [""])[0]
            return hmac.compare_digest(supplied.encode(), token.encode())

        def has_app_session(self):
            """Whether the request carries the Mac app's session (constant-time); False without one configured."""
            if not app_session:
                return False
            supplied = self.headers.get("X-Mobster-App-Session", "")
            return hmac.compare_digest(supplied.encode(), app_session.encode())

        def needs_app(self):
            """True (after answering 403 app_only) when this sidecar has an app session and the request lacks it."""
            if not app_session or self.has_app_session():
                return False
            self.send_json({"error": APP_ONLY, "code": "app_only"}, 403)
            return True

        def origin_label(self):
            """X-Mobster-Origin: app, tui, cli or mcp; "api" without it. A label for the UI only: it grants
            nothing. ValueError for another value."""
            values = self.headers.get_all("X-Mobster-Origin", []) or []
            if not values:
                return "api"
            if len(values) != 1 or values[0] not in api_routes.HEADER_ORIGINS:
                raise ValueError("X-Mobster-Origin must be app, tui, cli or mcp")
            return values[0]

        def extension(self, method, path):
            """Answer a route a track registered (api_routes); False when none matches ``path``."""
            found = api_routes.match(method, path)
            if found is None:
                return False
            route, params = found
            self.close_connection = True
            try:
                if api_routes.track_off(route):
                    state = tracks.STATUS.get(api_routes.TRACK.get(route.owner), "")
                    message = api_routes.NEWER_DATA if state == "error: newer data" else (
                        "This part of Mobster couldn't open its data. Restart Mobster, then try again.")
                    return self.send_json({"error": message, "code": "unavailable"}, 503) or True
                if (method, path) in APP_ONLY_ROUTES and self.needs_app():
                    return True
                origin = self.origin_label()
                if route.idempotency and len(self.headers.get_all("Idempotency-Key", [])) > 1:
                    raise ValueError("Supply only one Idempotency-Key")
                body = self.route_body(route) if method == "POST" else None
                if body is _ANSWERED:
                    return True
                query = parse_qs(urlsplit(self.path).query)
                request = api_routes.Request(runtime=runtime, method=method, path=path, params=params, query=query,
                                             headers=self.headers, body=body,
                                             content_type=self.headers.get_content_type()
                                             if self.headers.get("Content-Type") else None,
                                             origin=origin, app_session=self.has_app_session())
                response = route.handler(request)
                if not isinstance(response, api_routes.Response):
                    raise TypeError("A route handler returns a Response")
            except (ValueError, TypeError) as exc:
                return self.send_json({"error": str(exc)}, 400) or True
            except APIError as exc:
                return self.send_json({"error": str(exc), "code": exc.code, **exc.details}, exc.status) or True
            except JournalError:
                return self.send_json({"error": "Mobster could not save this. Try again.",
                                       "code": "persistence_unavailable"}, 503) or True
            except TimeoutError:
                return self.send_json({"error": "Request timed out", "code": "request_timeout"}, 408) or True
            except OSError:
                return self.send_json({"error": "Mobster could not read or write a file for this request.",
                                       "code": "io_error"}, 500) or True
            self.send_response_object(response)
            return True

        def route_body(self, route):
            """A registered POST route's body: a dict (json), bytes (raw) or None (none); _ANSWERED after an error
            reply (415, 413, 400)."""
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) > 1:
                raise ValueError("Supply one Content-Length and no Transfer-Encoding")
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise ValueError("Invalid body length") from None
            if route.body == "none":
                if size:
                    raise ValueError("This request takes no body")
                return None
            if route.body == "json" and self.headers.get_content_type() != "application/json":
                self.send_json({"error": "Use application/json"}, 415)
                return _ANSWERED
            if size > route.max_bytes:
                self.send_json({"error": f"This is larger than the {route.max_bytes:,}-byte limit",
                                "code": "too_large", "limit": route.max_bytes}, 413)
                return _ANSWERED
            if size <= 0:
                raise ValueError("Invalid body length")
            if route.body == "raw":
                self.connection.settimeout(30)
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError("Incomplete request body")
            if route.body == "raw":
                return raw
            body = decode_json(raw)
            if not isinstance(body, dict):
                raise ValueError("JSON body must be an object")
            return body

        def send_response_object(self, response):
            if response.is_stream():
                return self.send_sse(response.events)
            if response.data is not None:
                self.send_response(response.status)
                self.send_header("Content-Type", response.content_type or "application/octet-stream")
                self.send_header("Content-Length", str(len(response.data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                try:
                    self.wfile.write(response.data)
                except (OSError, TimeoutError):
                    self.close_connection = True
                return None
            return self.send_json(response.value, response.status)

        def send_sse(self, events):
            """A text/event-stream from ``events`` (dicts, and None for a keepalive); counts against
            runtime.streams (8)."""
            if not runtime.streams.acquire(blocking=False):
                return self.send_json({"error": "Too many event streams", "code": "stream_limit"}, 429)
            self.close_connection = True
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                for event in events:
                    if event is None:
                        self.wfile.write(b": keepalive\n\n")
                    else:
                        ident = event.get("seq", event.get("id"))
                        self.wfile.write((f"id: {ident}\n" if ident is not None else "").encode()
                                         + f"data: {json.dumps(event)}\n\n".encode())
                    self.wfile.flush()
                    if runtime.closing:
                        break
            except (OSError, TimeoutError):
                pass
            finally:
                close = getattr(events, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
                runtime.streams.release()

        def send_events(self):
            """GET /api/events?topics=runs,thread:<id> (S4): the event bus as SSE, resumed from Last-Event-ID."""
            query = parse_qs(urlsplit(self.path).query)
            try:
                topics = eventbus.parse_topics((query.get("topics") or [""])[0])
            except ValueError as exc:
                return self.send_json({"error": str(exc), "code": "invalid_topics"}, 400)
            last = self.headers.get("Last-Event-ID") or (query.get("lastEventId") or [None])[0]
            bus = getattr(runtime, "bus", None) or eventbus.bus
            keepalive = getattr(runtime, "event_keepalive_s", 5.0)
            return self.send_sse(bus.stream(topics, last, keepalive_s=keepalive, closed=lambda: runtime.closing))

        def refused(self):
            """Answer 403 to a request that is not local and 401 to one without the token."""
            if not self.local_request():
                self.send_json({"error": "Local origin required"}, 403)
            elif not self.authorized():
                self.send_json({"error": "Missing or wrong API token", "code": "unauthorized"}, 401)
            else:
                return False
            return True

        def local_request(self):
            if len(self.headers.get_all("Host", [])) != 1:
                return False
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            return bool(re.fullmatch(r"(?:localhost|127\.0\.0\.1)(?::[0-9]{1,5})?", host)) and (
                not origin or origin in self.allowed_origins())

        def end_headers(self):
            # Cross-origin reads are allowed only for allowlisted local origins;
            # the desktop webview is cross-origin to this loopback API.
            origin = self.headers.get("Origin") if hasattr(self, "headers") and self.headers else None
            if origin and origin in self.allowed_origins():
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            super().end_headers()

        def do_OPTIONS(self):
            self.close_connection = True
            if not self.local_request() or not self.headers.get("Origin"):
                return self.send_json({"error": "Local origin required"}, 403)
            self.send_response(204)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Idempotency-Key, "
                                                             "X-Mobster-Origin, X-Mobster-App-Session, Last-Event-ID")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            if self.refused():
                return
            path = api_path(urlsplit(self.path).path)
            if self.extension("GET", path):
                return
            if path == "/api/events":
                return self.send_events()
            if path == "/api/status":
                return self.send_json(runtime.status())
            if path == "/api/apps":
                device = (parse_qs(urlsplit(self.path).query).get("device") or [None])[0]
                if device is None:
                    return self.send_json({"apps": runtime.apps()})
                phone = self.device_phone(device)
                return phone and self.send_json({"apps": runtime.apps_for(phone)})
            if path == "/api/devices":
                return self.send_devices()
            device_match = DEVICE_PATH.fullmatch(path)
            if device_match and not device_match[3] and device_match[2] != "control":
                return self.send_device(device_match[1], device_match[2])
            if path == "/api/apps/icon":
                return self.send_icon()
            if path == "/api/workflows":
                try:
                    return self.send_json({"workflows": runtime.workflows.list(), "schedulerError": runtime.workflows.scheduler_error})
                except JournalError:
                    return self.send_json({"error": "Saved tasks are temporarily unavailable", "code": "persistence_unavailable"}, 503)
            if path == "/api/usage":
                return self.send_json({**runtime.journal.usage(), "monthUsd": runtime.month_usd(),
                                       "monthlyLimitUsd": runtime.monthly_limit()})
            if path == "/api/usage/estimate":
                planning = runtime.planning(helper_model=configured_model())
                return self.send_json({**planning, "engines": runtime.engine_estimates(planning=planning)})
            if path == "/api/settings":
                return self.send_json(runtime.settings())
            if path == "/api/keys":
                return self.send_json(runtime.keys.state())
            if path == "/api/setup":
                if runtime.setup is None:
                    return self.send_json({"error": "Setup is managed outside this app", "code": "setup_unmanaged"}, 404)
                return self.send_json(runtime.setup.state())
            if path == "/api/device/video/status":
                return self.send_json({**runtime.video.status(), "awakeLease": runtime.awake.status()})
            if path == "/api/device/info":
                return self.send_json(runtime.device_info.snapshot())
            if path == "/api/device/video":
                return self.send_video()
            if path == "/api/device/stream":
                return self.send_stream()
            if path == "/api/device/frame":
                return self.send_frame()
            if path == "/api/runs":
                with runtime.lock:
                    runs = list(runtime.runs.values())
                device = (parse_qs(urlsplit(self.path).query).get("device") or [None])[0]
                if device is not None:
                    phone = self.device_phone(device)
                    if phone is None:
                        return
                    # Runs saved before devices had one ran on the primary phone.
                    runs = [r for r in runs if r.device_id == phone.id or (phone.primary and r.device_id is None)]
                return self.send_json({"runs": [r.public(include_events=False) for r in reversed(runs)]})
            frame = re.fullmatch(r"/api/runs/([a-f0-9]{12})/frames/(f[0-9a-f]{10})", path)
            if frame:
                return self.send_run_frame(frame[1], frame[2])
            match = re.fullmatch(r"/api/runs/([a-f0-9]{12})(?:/(events|screenshot|usage))?", path)
            run = runtime.runs.get(match[1]) if match else None
            if not run:
                return self.send_json({"error": "Not found"}, 404)
            if match[2] == "usage":
                return self.send_json(runtime.journal.usage(run.id))
            if match[2] == "screenshot":
                return self.send_json(runtime.preview(run))
            if match[2] != "events":
                return self.send_json({"run": run.public()})
            if not runtime.streams.acquire(blocking=False):
                return self.send_json({"error": "Too many event streams", "code": "stream_limit"}, 429)
            self.close_connection = True
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    index = max(0, int(self.headers.get("Last-Event-ID", "-1")) + 1)
                except ValueError:
                    index = 0
                with run.condition:
                    if index > len(run.events):
                        index = 0
                while True:
                    with run.condition:
                        events = list(run.events[index:])
                        finished = run.finished_at is not None
                        if not events and not finished:
                            run.condition.wait(timeout=5)
                    if events:
                        for event in events:
                            self.wfile.write(f"id: {event['seq']}\ndata: {json.dumps(event)}\n\n".encode())
                        index += len(events)
                    else:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    if finished:
                        break
            except (OSError, TimeoutError):
                pass
            finally:
                runtime.streams.release()

        def device_phone(self, ident):
            """The fleet.Phone a path or query names, or None after answering 404 (or 400 for an ambiguous name)."""
            fleet = getattr(runtime, "fleet", None)
            if fleet is None:
                self.send_json({"error": "This Mobster has no device list", "code": "device_not_found"}, 404)
                return None
            try:
                return fleet.lookup(unquote(ident))
            except DeviceNotFound as error:
                self.send_json({"error": str(error), "code": "device_not_found"}, 404)
            except AmbiguousDevice as error:
                self.send_json({"error": str(error), "code": "device_ambiguous"}, 400)
            return None

        def send_devices(self):
            fleet = getattr(runtime, "fleet", None)
            devices = fleet.public() if fleet is not None and runtime.pool is None else []
            return self.send_json({"devices": devices,
                                   "defaultDevice": next((item["id"] for item in devices if item["default"]), None)})

        def send_device(self, ident, part):
            phone = self.device_phone(ident)
            if phone is None:
                return
            fleet = runtime.fleet
            if part is None:
                return self.send_json({"device": fleet.entry(phone.id or ident)[1]})
            video = runtime.video if phone.primary else phone.video
            if part in ("stream", "frame") and video is None:
                # A phone plugged in but not set up has no relay yet: never another phone's picture in its place.
                return self.send_json({"error": "This device has no live view yet", "code": "video_unavailable"}, 503)
            if part == "stream":
                return self.send_stream(video)
            if part == "frame":
                return self.send_frame(video)
            if part == "video/status":
                if video is None:
                    return self.send_json({"error": "This device has no live view", "code": "video_unavailable"}, 404)
                return self.send_json({**video.status(), "awakeLease": runtime.awake.status()})
            if part == "info":
                return self.send_json(phone.info())
            try:
                return self.send_json(fleet.setup_state(phone))
            except LookupError as error:
                return self.send_json({"error": str(error), "code": "setup_unmanaged"}, 404)

        def device_action(self, ident, part, action, body):
            phone = self.device_phone(ident)
            if phone is None:
                return
            if part == "control":
                return self.send_json({"ok": True, "action": runtime.control_device(body, phone)})
            fleet = runtime.fleet
            try:
                target = fleet.setup_action(phone, action, body)
                return self.send_json(fleet.setup_state(target))
            except KeyError:
                return self.send_json({"error": "Not found"}, 404)
            except LookupError as error:
                return self.send_json({"error": str(error), "code": "setup_refused"}, 409)

        def setup_action(self, path, body):
            setup = runtime.setup
            if setup is None:
                return self.send_json({"error": "Setup is managed outside this app", "code": "setup_unmanaged"}, 404)
            try:
                if path == "/api/setup/key" and set(body) == {"key"}:
                    setup.save_key(body["key"])
                elif path == "/api/setup/build" and set(body) == {"team"}:
                    setup.manager.start_build(body["team"])
                elif path == "/api/setup/start" and not body:
                    setup.manager.start_runner()
                elif path == "/api/setup/stop" and not body:
                    setup.manager.stop_runner(remember=True)
                elif path == "/api/setup/phone" and set(body) == {"udid"}:
                    runtime.manager.choose(body["udid"])
                elif path == "/api/setup/live" and set(body) == {"enabled"}:
                    setup.set_live(body["enabled"])
                elif path == "/api/setup/refresh" and not body:
                    setup.refresh()
                elif path == "/api/setup/tools" and not body:
                    setup.install_tools()
                elif path == "/api/setup/xcode" and not body:
                    setup.open_xcode()
                else:
                    return self.send_json({"error": "Not found"}, 404)
            except LookupError as error:
                return self.send_json({"error": str(error)}, 409)
            except ValueError as error:
                return self.send_json({"error": str(error)}, 400)
            return self.send_json(setup.state())

        def send_run_frame(self, run_id, frame_id):
            """A screen a run's step, receipt or proof points at: a JPEG at most 480 px on its long side."""
            frames = getattr(runtime, "frames", None)
            data = frames.get(run_id, frame_id) if frames is not None and run_id in runtime.runs else None
            if data is None:
                return self.send_json({"error": "Not found", "code": "frame_not_found"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            # Never in WebKit's disk cache (~/Library/Caches), where deleting the task or the app's
            # data folder would not reach it: the phone's screens stay in the data folder only.
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (OSError, TimeoutError):
                self.close_connection = True

        def do_DELETE(self):
            self.close_connection = True
            if self.refused():
                return
            path = api_path(urlsplit(self.path).path)
            if self.extension("DELETE", path):
                return
            workflow_match = re.fullmatch(r"/api/workflows/([a-f0-9]{12})", path)
            if workflow_match:
                try:
                    return self.send_json({"deleted": runtime.workflows.delete(workflow_match[1])})
                except APIError as exc:
                    return self.send_json({"error": str(exc), "code": exc.code, **exc.details}, exc.status)
                except JournalError:
                    return self.send_json({"error": "Workflows could not be saved. Try again.",
                                           "code": "persistence_unavailable"}, 503)
            device_match = DEVICE_PATH.fullmatch(path)
            if device_match and device_match[2] is None and device_match[3] is None:
                phone = self.device_phone(device_match[1])
                if phone is None:
                    return
                try:
                    return self.send_json({"forgotten": runtime.fleet.forget(phone.id)})
                except LookupError as error:
                    return self.send_json({"error": str(error), "code": "device_refused"}, 409)
            match = re.fullmatch(r"/api/runs/([a-f0-9]{12})", path)
            if not match:
                return self.send_json({"error": "Not found"}, 404)
            try:
                runtime.delete_run(match[1])
            except APIError as exc:
                return self.send_json({"error": str(exc), "code": exc.code, **exc.details}, exc.status)
            except JournalError:
                return self.send_json({"error": "Task history could not be saved. Try again.",
                                       "code": "persistence_unavailable"}, 503)
            return self.send_json({"deleted": match[1]})

        def send_icon(self):
            """An app's icon (see app_icons): 400 for a bad bundle ID, 404 when it has none."""
            query = parse_qs(urlsplit(self.path).query)
            bundle = (query.get("bundle") or [""])[0]
            try:
                icon = runtime.icons.get(bundle)
            except ValueError:
                return self.send_json({"error": "Invalid app bundle identifier", "code": "invalid_bundle"}, 400)
            except IconUnavailable:
                return self.send_json({"error": "The app icon is not available right now", "code": "icon_unavailable"}, 503)
            except OSError:
                return self.send_json({"error": "The app icon could not be saved", "code": "icon_unavailable"}, 503)
            if icon is None:
                return self.send_json({"error": "This app has no icon", "code": "no_icon"}, 404)
            data, mime = icon
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            # Kept out of WebKit's disk cache like run frames: the URL names an app on the phone.
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (OSError, TimeoutError):
                self.close_connection = True

        def send_stream(self, video=PRIMARY_VIDEO):
            """Multipart JPEG (MJPEG): an <img> element plays it directly.

            ``?framing=raw`` sends the same parts as application/octet-stream for
            the dashboard's fetch reader: WebKit (the desktop app's webview) parses
            multipart/x-mixed-replace itself and hands fetch() bodies without the
            part headers, so the reader never saw a frame there.
            """
            raw = parse_qs(urlsplit(self.path).query).get("framing") == ["raw"]
            video = runtime.video if video is PRIMARY_VIDEO else video
            if not isinstance(video, WdaVideo):
                return self.send_json({"error": "Live stream needs a USB iPhone", "code": "video_unavailable"}, 503)
            try:
                viewer = video.subscribe()
            except WdaVideoUnavailable as exc:
                return self.send_json({"error": str(exc), "code": "video_unavailable"}, 503)
            self.close_connection = True
            try:
                self.connection.settimeout(10)  # A stuck viewer releases its slot.
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream" if raw else
                                 f"multipart/x-mixed-replace; boundary={WDA_BOUNDARY}")
                self.send_header("Cache-Control", "no-store, no-transform")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Connection", "close")
                self.end_headers()
                sequence = 0
                while True:
                    frame = video.next_frame(viewer, sequence, timeout=10)
                    if frame is None:
                        if video.closed:
                            break
                        continue
                    sequence, content_type, data = frame[0], frame[1], frame[2]
                    self.wfile.write(f"--{WDA_BOUNDARY}\r\nContent-Type: {content_type}\r\n"
                                     f"Content-Length: {len(data)}\r\n\r\n".encode() + data + b"\r\n")
                    self.wfile.flush()
            except (OSError, TimeoutError):
                pass
            finally:
                video.unsubscribe(viewer)

        def send_frame(self, video=PRIMARY_VIDEO):
            """The newest frame as a plain image (thumbnails, first paint)."""
            video = runtime.video if video is PRIMARY_VIDEO else video
            frame = video.latest() if isinstance(video, WdaVideo) else None
            if frame is None:
                return self.send_json({"error": "No frame yet", "code": "video_unavailable"}, 503)
            self.send_response(200)
            self.send_header("Content-Type", frame[1])
            self.send_header("Content-Length", str(len(frame[2])))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                self.wfile.write(frame[2])
            except (OSError, TimeoutError):
                self.close_connection = True

        def send_video(self):
            if isinstance(runtime.video, WdaVideo):
                return self.send_json({"error": "This iPhone streams at /api/device/stream",
                                       "code": "video_unavailable"}, 503)
            try:
                viewer = runtime.video.subscribe()
            except RuntimeError as exc:  # The stream's own VideoUnavailable
                return self.send_json({"error": str(exc), "code": "video_unavailable"}, 503)
            self.close_connection = True
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/x-mobster-video")
                self.send_header("Cache-Control", "no-store, no-transform")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Connection", "close")
                self.end_headers()
                while True:
                    value = runtime.video.next_record(viewer)
                    if value is None:
                        break
                    if value:
                        self.wfile.write(value)
                        self.wfile.flush()
            except (OSError, TimeoutError):
                pass
            finally:
                runtime.video.unsubscribe(viewer)

        def do_POST(self):
            # Close after POST so unread malformed bodies cannot become a second request.
            self.close_connection = True
            if self.refused():
                return
            if self.extension("POST", api_path(urlsplit(self.path).path)):
                return
            if self.headers.get_content_type() != "application/json":
                return self.send_json({"error": "Use application/json"}, 415)
            try:
                if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                    raise ValueError("Supply one Content-Length and no Transfer-Encoding")
                size = int(self.headers.get("Content-Length", "0"))
                # Exports carry a whole result; every other body is a small command.
                limit = EXPORT_MAX_BYTES + 4096 if api_path(self.path) == "/api/export" else 64000
                if not 0 < size <= limit:
                    return self.send_json({"error": "Invalid body length"}, 400)
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise ValueError("Incomplete request body")
                body = decode_json(raw)
                if not isinstance(body, dict):
                    raise ValueError("JSON body must be an object")
                path = api_path(self.path)
                if path.startswith("/api/setup/"):
                    return self.setup_action(path, body)
                device_match = DEVICE_PATH.fullmatch(path)
                if device_match and (device_match[3] or device_match[2] == "control"):
                    return self.device_action(device_match[1], device_match[2], device_match[3], body)
                if path == "/api/workflows/preview":
                    return self.send_json(schedule_preview(body, runtime.workflows.now()))
                if path == "/api/workflows":
                    return self.send_json({"workflow": runtime.workflows.create(body)}, 201)
                workflow_match = re.fullmatch(r"/api/workflows/([a-f0-9]{12})(/run)?", path)
                if workflow_match:
                    if not workflow_match[2]:
                        return self.send_json({"workflow": runtime.workflows.update(workflow_match[1], body)})
                    if body or len(self.headers.get_all("Idempotency-Key", [])) != 1:
                        raise ValueError("Supply an empty JSON object and one Idempotency-Key")
                    run, replayed = runtime.workflows.run(workflow_match[1], self.headers.get("Idempotency-Key"))
                    return self.send_json({"run": run.public(), "replayed": replayed}, 200 if replayed else 201)
                if path == "/api/usage/estimate":
                    if (set(body) - {"goal", "outputSchema", "outputFormat", "helperModel", "engine"}
                            or not isinstance(body.get("goal"), str) or len(body["goal"].strip()) > 4000
                            or body.get("engine") is not None and body["engine"] not in engines.ENGINES
                            or body.get("outputFormat") not in (None, "auto", *OUTPUT_FORMATS)):
                        raise ValueError("Supply a draft of at most 4000 characters and optional output schema, "
                                         "output format, helper model and engine (smart or fast)")
                    schema = validate_schema(body["outputSchema"]) if body.get("outputSchema") is not None else None
                    goal, helper_model = body["goal"].strip(), resolve_helper_model(body.get("helperModel"))
                    planning = runtime.planning(goal, schema, helper_model)
                    estimates = runtime.engine_estimates(goal, schema, helper_model, planning=planning,
                                                         output_format=body.get("outputFormat"))
                    return self.send_json({**planning, "engine": body.get("engine") or runtime.default_engine(),
                                           "engines": estimates})
                if path == "/api/runs":
                    if body.get("mode", "live") != "live":
                        raise ValueError("Dashboard tasks run on your iPhone. Synthetic replays are not supported here.")
                    if len(self.headers.get_all("Idempotency-Key", [])) > 1:
                        raise ValueError("Supply only one Idempotency-Key")
                    key = self.headers.get("Idempotency-Key")
                    core = {"appId", "goal", "mode", "outputSchema", "outputFormat", "helperModel",
                            "allowedBundles", "dryRun", "engine", "device"}
                    fields = harness_api.run_fields()
                    if set(body) - core - set(fields):
                        raise ValueError("Unsupported task field")
                    if body.get("device") is not None and not isinstance(body["device"], str):
                        raise ValueError("device must be a device's id, UDID or name")
                    extras = {name: body[name] for name in body if name in fields and name not in core}
                    origin = self.origin_label()
                    created = runtime.create(body.get("appId"), body.get("goal"), "live", key,
                                             output_schema=body.get("outputSchema"),
                                             output_format=body.get("outputFormat", "auto"),
                                             helper_model=body.get("helperModel"),
                                             allowed_bundles=body.get("allowedBundles"),
                                             dry_run=body.get("dryRun", False), engine=body.get("engine"),
                                             **({"device": body["device"]} if body.get("device") is not None else {}),
                                             **({"extras": extras} if extras else {}),
                                             **({"origin": origin} if origin != "api" else {}))
                    run, replayed = created if key else (created, False)
                    return self.send_json({"run": run.public(), "replayed": replayed}, 200 if replayed else 201)
                if path == "/api/settings":
                    if {"askBeforeActing", "bypassChecks"} & set(body) and self.needs_app():
                        return
                    return self.send_json(runtime.update_settings(body))
                if path == "/api/keys":
                    return self.send_json(runtime.save_keys(body))
                if path == "/api/keys/test":
                    if set(body) != {"target"}:
                        raise ValueError("Send the target to test: jev, helper or openai")
                    return self.send_json(runtime.check_key(body["target"]))
                if path == "/api/export":
                    return self.send_json(save_export(body))
                if path == "/api/device/control":
                    return self.send_json({"ok": True, "action": runtime.control_device(body)})
                match = re.fullmatch(r"/api/runs/([a-f0-9]{12})/(stop|approval|review|gif)", path)
                run = runtime.runs.get(match[1]) if match else None
                if run and match[2] == "gif":
                    theme, redact, mp4 = body.get("theme", "paper"), body.get("redact", False), body.get("mp4", False)
                    if (set(body) - {"theme", "redact", "mp4"} or theme not in ("paper", "night")
                            or type(redact) is not bool or type(mp4) is not bool):
                        raise ValueError("A GIF takes an optional theme (paper or night), redact and mp4 "
                                         "(true or false)")
                    return self.send_json(runtime.save_run_gif(match[1], theme, redact, mp4))
                if run and match[2] == "approval":
                    if (set(body) - {"choice", "instruction", "answer"} != {"id", "approve"}
                            or not isinstance(body["id"], str)
                            or type(body["approve"]) is not bool
                            or "choice" in body and not isinstance(body["choice"], str)
                            or "instruction" in body and not isinstance(body["instruction"], str)
                            or "answer" in body and not isinstance(body["answer"], str)):
                        raise ValueError("An approval needs its id and approve: true or false (and optionally a "
                                         "choice, an instruction with approve false, or an answer to a question)")
                    pending = run.approval
                    question = (pending is not None and pending.get("id") == body["id"]
                                and pending.get("kind") == "clarify")
                    # Approving or choosing needs the Mac app (S6.6); a decline, a redirect and any answer to a
                    # clarifying question (it never approves a commit) stay token-only.
                    if (body["approve"] or "choice" in body) and not question and self.needs_app():
                        return
                    if not run.answer_approval(body["id"], body["approve"], body.get("choice"), body.get("instruction"),
                                               **({"answer": body["answer"]} if "answer" in body else {})):
                        raise APIError("This approval request is no longer waiting", 409, "approval_not_pending")
                    return self.send_json({"run": run.public()})
                if run and match[2] == "review":
                    if set(body) != {"ok"} or type(body["ok"]) is not bool:
                        raise ValueError("A review needs ok: true or false")
                    return self.send_json({"run": runtime.review_run(run.id, body["ok"]).public()})
                if run:
                    runtime.stop_run(run.id)
                    return self.send_json({"run": run.public()})
                return self.send_json({"error": "Not found"}, 404)
            except (ValueError, TypeError) as exc:
                return self.send_json({"error": str(exc)}, 400)
            except APIError as exc:
                return self.send_json({"error": str(exc), "code": exc.code, **exc.details}, exc.status)
            except WorkflowDispatchUncertain as exc:
                return self.send_json({"error": str(exc), "code": "workflow_dispatch_uncertain", "activeRunId": exc.run_id}, 503)
            except JournalError:
                return self.send_json({"error": "Task history could not be saved. Check task status before retrying; a task may have started.",
                                       "code": "persistence_unavailable"}, 503)
            except TimeoutError:  # the request's socket (5 s, ``setup``)
                return self.send_json({"error": "Request timed out", "code": "request_timeout"}, 408)
            except OSError:
                return self.send_json({"error": "Mobster could not read or write a file for this request.",
                                       "code": "io_error"}, 500)

    return Handler


_ANSWERED = object()  # a route body whose error reply already went out


class BoundedServer(ThreadingHTTPServer):
    """Bound slow clients, preserving capacity beyond the separate SSE limit."""
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, *args, **kwargs):
        self.capacity = threading.BoundedSemaphore(32)
        super().__init__(*args, **kwargs)

    def process_request(self, request, address):
        if not self.capacity.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.capacity.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.capacity.release()


def default_data_dir():
    return user_data_dir()


# Origins the Tauri webview uses for the bundled dashboard.
DESKTOP_ORIGINS = frozenset({"tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"})
TOKEN_FILE = "api-token"


def api_token():
    """This launch's API token: MOBSTER_API_TOKEN from the desktop shell (removed from the
    environment so runner and relay processes don't inherit it), else a random one."""
    token = os.environ.pop("MOBSTER_API_TOKEN", "") or secrets.token_urlsafe(32)
    if not re.fullmatch(r"[A-Za-z0-9_.~-]{32,256}", token):
        raise ValueError("MOBSTER_API_TOKEN must be 32 to 256 URL-safe characters")
    return token


def app_session_secret():
    """The Mac app's session (S6.6): MOBSTER_APP_SESSION from the desktop shell, removed from the environment like
    the token, or None (`mobster serve`). Never written to the token file."""
    value = os.environ.pop("MOBSTER_APP_SESSION", "")
    if not value:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.~-]{32,256}", value):
        raise ValueError("MOBSTER_APP_SESSION must be 32 to 256 URL-safe characters")
    return value


def token_file(config):
    """Where ``serve`` leaves its token for local tools (the Vite proxy, curl): beside the journal."""
    state_db = getattr(config, "state_db", None)
    return Path(state_db).parent / TOKEN_FILE if state_db else None


def write_private(path, text, binary=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "wb" if binary else "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(text)


def watch_parent(parent, interval=1.0):
    """Terminate gracefully once the launching process is gone.

    A frozen one-file sidecar runs as a bootloader plus this Python child;
    killing only the bootloader (or the desktop app crashing) used to orphan
    this process, which kept holding the API port.
    """
    def watch():
        while True:
            time.sleep(interval)
            if os.getppid() != parent:
                os.kill(os.getpid(), signal.SIGTERM)
                return
    threading.Thread(target=watch, name="mobster-parent-watch", daemon=True).start()


def serve(config):
    usd_to_nanodollars(getattr(config, "spend_cap_usd", None))
    hooks = load_extensions()
    runtime_class = (hooks.runtime_for(config) if hooks.runtime_for else None) or Runtime
    token, path = api_token(), token_file(config)
    session = app_session_secret()
    runtime = runtime_class(config)
    # Check once per key, in the background, that the OpenAI key reaches Smart's model.
    runtime.model_reach.enabled = True
    server = None
    try:
        server = BoundedServer(("127.0.0.1", config.port), make_handler(runtime, token, app_session=session))
        # --port 0 lets the system pick: the service line and the allowed origins name the port it chose.
        # (A stand-in server in a test may have no address; it keeps the configured port.)
        bound = getattr(server, "server_address", None)
        if bound:
            config.port = bound[1]
        if path:
            write_private(path, token + "\n")
        runtime.scheduler = WorkflowScheduler(runtime.workflows)
        runtime.scheduler.start()
    except Exception:
        if server is not None:
            server.server_close()
        runtime.close()
        raise
    # The token itself is never printed: stdout reaches logs.
    print(json.dumps({"service": "Mobster API", "url": f"http://127.0.0.1:{config.port}",
                      "tokenFile": str(path) if path else None}), flush=True)
    if getattr(config, "exit_with_parent", False):
        watch_parent(os.getppid())
    previous_term = None
    if threading.current_thread() is threading.main_thread():
        previous_term = signal.getsignal(signal.SIGTERM)
        def terminate(*_):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, terminate)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        runtime.close()
        try:
            if path and path.read_text().strip() == token:
                path.unlink()
        except OSError:
            pass
        if previous_term is not None:
            signal.signal(signal.SIGTERM, previous_term)
