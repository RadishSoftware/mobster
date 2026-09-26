"""Loopback-only dashboard API with SSE, cancellation, and one device owner at a time."""

from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hashlib
import hmac
import os
import re
import secrets
import sys
import signal
import threading
from pathlib import Path
import time
from urllib.parse import parse_qs, urlsplit
import uuid

from . import __version__
from .agent import Agent
from .catalog import APPS
from .drivers import DriverRejection, WDA, resolve_wda_session
from .loops import LoopStore
from .replay import ReplayStore
from .decision_memo import DecisionMemo
from .latency_trace import Trace
from .compose import build_models, build_target_driver, build_vision_judge, build_visual, close_all, warm_clients
from .frame_clock import attach_frame_clock
from .extensions import load as load_extensions
from .transport import HTTP, decode_json
from .pool import NoDeviceAvailable
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
from .costs import SpendLedger, planning_estimate, helper_model_rates, usd_to_nanodollars
from .device_info import DeviceInfo
from .output_contract import validate_output
from .api_errors import APIError
from .paths import user_data_dir
from .workflows import Workflows, WorkflowDispatchUncertain
from .workflow_scheduler import WorkflowScheduler
from .schedule import preview as schedule_preview
from .helper_models import configured_model, model_options, resolve_helper_model, validate_model_request

EXPORT_MAX_BYTES = 8_000_000
EXPORT_TYPES = {"json", "csv", "yaml", "yml", "md", "txt"}


def save_export(body, directory=None):
    """Save a result the dashboard exports into ~/Downloads, never overwriting.

    The desktop webview cannot be relied on to download blob links, so the
    agent writes the file. The name is reduced to a safe basename with a known
    extension; the directory is fixed.
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
    folder = Path(directory) if directory else Path.home() / "Downloads"
    folder.mkdir(parents=True, exist_ok=True)
    for index in range(1000):
        target = folder / (f"{stem}.{extension.lower()}" if index == 0 else f"{stem} ({index}).{extension.lower()}")
        try:
            with open(target, "x", encoding="utf-8") as stream:
                stream.write(text)
            return {"name": target.name, "path": str(target)}
        except FileExistsError:
            continue
    raise ValueError("Too many files with that name in Downloads")


class PhoneNotReady(RuntimeError):
    """The phone cannot be driven right now; the message says what the user can do."""


# Seconds a preflight check may take. Locked phones and system overlays (Control
# Center, the iOS 26 Siri glow) block WDA's accessibility calls; measured: reads
# then hang 8-20 s and a task failed only with "TransportError".
PREFLIGHT_TIMEOUT = 4


def prepare_wda_phone(driver):
    """Before a task: refuse a locked or unresponsive phone plainly, then clear overlays.

    Home dismisses Control Center, Notification Center, Siri and the app switcher;
    the task opens its own app next, so leaving whatever was in front is harmless.
    """
    try:
        locked = WDA.response_value(driver.http.request("GET", "/wda/locked", None, PREFLIGHT_TIMEOUT))
    except Exception:
        raise PhoneNotReady("Your iPhone isn't responding. Unlock it, close Control Center or Siri if they're "
                            "open, then try again. No action was taken.") from None
    if locked is True:
        raise PhoneNotReady("Your iPhone is locked. Unlock it, then try again. No action was taken.")
    try:
        driver.call("POST", "/wda/pressButton", {"name": "home"}, PREFLIGHT_TIMEOUT)
    except Exception:
        raise PhoneNotReady("Your iPhone isn't responding. Unlock it, close Control Center or Siri if they're "
                            "open, then try again. It may have gone to the Home Screen; nothing else was done.") from None


# How long a paused action waits for the user before the task ends without it.
APPROVAL_TIMEOUT_SECONDS = 600


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
    stop: threading.Event = field(default_factory=threading.Event)
    condition: threading.Condition = field(default_factory=threading.Condition)
    # The action waiting for the user's answer. Held in memory only: it can
    # carry the exact text to be sent, which is never written to the journal.
    approval: dict | None = None
    approval_answer: str | None = None

    def request_approval(self, request, timeout=None):
        """Block until the user answers. Returns approved, denied, timeout or stopped.

        A question (``kind`` "loop" or "question", from a compiled loop) also
        carries ``choices``; picking one returns "choice:<id>", and a plain
        approval means its ``default_choice``. The label states what will
        happen and its limits, so the plain approve/decline prompt reads right.
        """
        timeout = APPROVAL_TIMEOUT_SECONDS if timeout is None else timeout
        now = time.time()
        pending = {"id": uuid.uuid4().hex[:12], "operation": request["operation"], "label": request["label"],
                   "role": request.get("role") or "", "text": request.get("text"), "app": self.app["name"],
                   "requestedAt": now * 1000, "expiresAt": (now + timeout) * 1000}
        kind = request.get("kind") or "action"
        if kind != "action":
            choices = [{"id": str(c["id"])[:40], "label": str(c["label"])[:120]}
                       for c in request.get("choices") or () if isinstance(c, dict) and c.get("id")][:6]
            default = request.get("default_choice")
            pending.update(kind=kind, question=str(request.get("question") or "")[:300], choices=choices,
                           defaultChoice=default if any(c["id"] == default for c in choices) else None,
                           limits=request.get("limits"))
        with self.condition:
            self.approval, self.approval_answer = pending, None
        self.emit({"event": "approval_requested", "approval_id": pending["id"], "step": request.get("step"),
                   "operation": pending["operation"], "label": pending["label"],
                   "text_present": pending["text"] is not None, "kind": kind,
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
        self.emit({"event": "approval_resolved", "approval_id": pending["id"], "decision": answer})
        return answer

    def answer_approval(self, approval_id, approve, choice=None):
        with self.condition:
            if self.approval is None or self.approval["id"] != approval_id or self.approval_answer is not None:
                return False
            if choice is not None:
                if not approve or choice not in {c["id"] for c in self.approval.get("choices", ())}:
                    raise ValueError("That choice is not one of this question's answers")
                self.approval_answer = f"choice:{choice}"
            else:
                self.approval_answer = "approved" if approve else "denied"
            self.condition.notify_all()
            return True

    def emit(self, event):
        with self.condition:
            entry = {**event, "seq": len(self.events), "timestamp": time.time() * 1000}
            if self.journal:
                self.journal.append(self.metadata(), entry)
            self.events.append(entry)
            self.condition.notify_all()

    def finish(self, summary):
        """Publish terminal status, summary and event as one durable transition."""
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
                "dryRun": self.dry_run}

    def public(self, include_events=True):
        with self.condition:
            return {**{k: v for k, v in self.metadata().items() if k != "app"},
                    "approval": dict(self.approval) if self.approval else None,
                    "events": list(self.events) if include_events else [], "eventCount": len(self.events)}


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
    def __init__(self, config):
        self.config = config
        self.runs = {}
        self.lock = threading.Lock()
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
        # A USB iPhone streams through WDA's MJPEG server; nothing else to configure.
        self.video = (WdaVideo(config.wda_url, session=self.current_wda_session, on_activity=self.video_activity,
                               quality=self.video_quality())
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
        self.scheduler = None
        # The USB iPhone lifecycle, when this server owns it (desktop app).
        self.manager = self.setup = None
        if getattr(config, "manage_device", False):
            data_dir = Path(getattr(config, "data_dir", None) or default_data_dir())
            env_file = getattr(config, "env_file", None) or data_dir / "agent.env"
            self.manager = DeviceManager(data_dir, config.wda_url or "http://127.0.0.1:8100")
            self.setup = SetupService(self, self.manager, env_file)
            self.device_info = DeviceInfo(manager=self.manager)
            threading.Thread(target=self.manager.watch, args=(self.manager.watching,), name="mobster-device-watch",
                             daemon=True).start()
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
                    journal=self.journal)
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
        """How many more live runs this runtime can admit right now."""
        if self.pool is None:
            return 0 if self.active_runs else 1
        free = self.pool.position()
        return max(0, free["healthy"] - free["busy"])

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

    def check_app(self, app):
        """Raise ValueError when a live run cannot use ``app`` on this target."""

    def open_driver(self, run, trace, setup):
        """Build this run's driver, check the phone and bring the app forward.

        ``setup.hold(driver)`` hands the driver to the caller, which closes it however
        this ends; ``setup.phase(name)`` names the step a failure happened in. Returns
        the driver to run with, or None when the run was stopped meanwhile.
        """
        with trace.span("setup.driver"):
            driver = setup.hold(build_target_driver(wda_url=self.config.wda_url,
                                                    session=self.wda_session() if self.config.wda_url else None,
                                                    expected_bundle=run.app["bundleId"]))
        state_db = getattr(self.config, "state_db", None)
        if isinstance(driver, WDA) and state_db and WDA.overlay_hint_path is None:
            # Apps that needed WDA's foreground hint, kept across restarts: the
            # first read otherwise fails for ~9 s under the Siri overlay.
            WDA.load_overlay_hints(str(Path(state_db).parent / "wda-overlay-hints.json"))
        if isinstance(driver, WDA) and isinstance(self.video, WdaVideo):
            # Pixel settle for this run (MOBSTER_FRAME_CLOCK); subscribing keeps the
            # relay's MJPEG stream up, and closing the driver releases it.
            attach_frame_clock(driver, self.video, log_dir=Path(state_db).parent if state_db else None)
        if run.stop.is_set():
            return None
        setup.phase("preflight")
        with trace.span("setup.preflight"):
            prepare_wda_phone(driver)
        run.emit({"event": "app_launch_started", "app": run.app["name"]})
        setup.phase("launching")
        with trace.span("launch.activate"):
            driver.call("POST", "/wda/apps/activate", {"bundleId": run.app["bundleId"]})
        run.emit({"event": "app_launch_acknowledged", "app": run.app["name"]})
        setup.phase("reading_app")
        return driver

    def visual_options(self, run):
        """Where this run's opt-in screen reader reads from (``compose.build_visual``)."""
        return {}

    def apps(self):
        return [dict(app) for app in APPS]

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

    def limitations(self, *, key, helper, device, ready, wda, can_act=None, notes=(), health=None):
        """Why live runs are limited, most actionable first (the dashboard shows the first)."""
        reasons = []
        if not key:
            reasons.append("Add your Jev key in Setup" if getattr(self, "setup", None)
                           else "Add TYPESAFE_API_KEY to the agent's env file")
        if not self.config.enable_live:
            reasons.append("Live runs are off; enable them to act on the phone")
        manager = getattr(self, "manager", None)
        if manager is not None and manager.expired():
            reasons.append("The runner's signature expired. Rebuild it in Setup")
        if device and not ready:
            reasons.append(health or (f"WebDriverAgent is not answering at {self.config.wda_url}" if wda
                                      else "Your iPhone is currently unreachable"))
        if notes:
            reasons += list(notes)
        elif not (wda or can_act):
            reasons.append("No iPhone attached: start the agent with --manage-device or --wda-url")
        if not helper:
            reasons.append("Text/recovery helper is not configured")
        return reasons

    def control_device(self, body):
        """One gesture from the live view. Never while a task holds the phone."""
        if self.control is None:
            raise APIError("Direct control needs a USB iPhone", 404, "control_unavailable")
        if not self.config.enable_live:
            raise APIError("Turn on “Allow actions on your iPhone” in Settings to control it", 409, "live_disabled")
        with self.lock:
            busy = bool(self.active_runs)
        if busy:
            raise APIError("A task is using your iPhone. Stop it to take over.", 409, "device_busy")
        try:
            return self.control.perform(body)
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

    def settings(self):
        value = {"askBeforeActing": self.ask_before_acting(), "bypassChecks": self.bypass_checks()}
        if isinstance(getattr(self, "video", None), WdaVideo):
            value["videoFps"], value["videoDetail"] = self.video_quality()
        return value

    @property
    def keys(self):
        """Bring-your-own keys, saved to the same private env file as other settings."""
        env_file = self.setup.env_file if getattr(self, "setup", None) else getattr(self.config, "env_file", None)
        return Keys(env_file)

    def update_settings(self, changes):
        allowed = {"askBeforeActing", "bypassChecks", "videoFps", "videoDetail"}
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
        return self.settings()

    def status(self):
        key = bool(os.environ.get("TYPESAFE_API_KEY"))
        helper = helper_configured()
        target = self.target_status()
        device, ready, health = target["device"], target["ready"], target["health"]
        wda = bool(self.config.wda_url)
        return {"api_version": "v1", "version": __version__, "jev_configured": key, "helper_configured": helper,
                "helper_model": configured_model(), "helper_models": model_options(),
                "helper_model_rates": helper_model_rates(model_options(), os.environ.get("GOOGLE_CLOUD_LOCATION", "global"))
                    if os.environ.get("TEXT_MODEL_PROVIDER") == "vertex" else {},
                "live_enabled": bool(not self.closing and self.config.enable_live and key and ready and target["can_act"]),
                "device_ready": ready, "durable_history": getattr(self.config, "state_db", None) is not None,
                "screen_reading": target["screen_reading"], "ask_before_acting": self.ask_before_acting(),
                "bypass_checks": self.bypass_checks(),
                "spend_cap_usd": getattr(self.config, "spend_cap_usd", None),
                "devices": self.pool.public() if self.pool else [],
                "concurrency": {"capacity": self.pool.size if self.pool else 1,
                                "running": len(self.active_runs),
                                "available": self.capacity()},
                "device": "Your iPhone" if device else None,
                "driver": target["driver"],
                "limitations": self.limitations(key=key, helper=helper, device=device, ready=ready, health=health,
                                                wda=wda, can_act=target["can_act"], notes=target["notes"])}

    def create(self, app_id, goal, mode, idempotency_key=None, output_schema=None, output_format="auto", helper_model=None,
                 allowed_bundles=None, dry_run=False):
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
            if not self.capacity():
                raise APIError("Every assigned phone is busy; stop a run or wait for one to finish",
                               code="run_active", activeRunId=self.active)
            app = next((a for a in self.apps() if a["id"] == app_id), None)
            if not app:
                raise ValueError("Choose an app from your catalog")
            allowed = None
            if extra_bundles is not None:
                known = {a["bundleId"] for a in self.apps() if isinstance(a.get("bundleId"), str)}
                unknown = sorted(extra_bundles - known)
                if unknown:
                    raise ValueError("Unknown app bundle: " + ", ".join(unknown[:5]))
                allowed = frozenset({app["bundleId"]} | extra_bundles)
            if mode == "live" and not self.status()["live_enabled"]:
                raise APIError("Your iPhone is not ready for live tasks. Try again when it is available.", 503, "device_unavailable")
            if mode == "live":
                self.check_app(app)
            lease = None
            if mode == "live" and self.pool is None:
                # No pool means a WDA target, which still needs its own exclusive lease.
                try:
                    lease = Lease.device(self.config.wda_url.rstrip("/"))
                except JournalError:
                    raise APIError("Your iPhone is busy in another Mobster process", code="device_busy") from None
            run = Run(app, goal.strip(), mode, idempotency_key=idempotency_key, output_schema=output_schema,
                      output_format=output_format, helper_model=selected_helper_model, allowed_bundles=allowed,
                      journal=self.journal, lease=lease, dry_run=dry_run)
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
            self.runs[run.id] = run
            self.active_runs[run.id] = device.id if device else None
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
        return (run, False) if idempotency_key else run

    def work(self, run):
        driver = model = helper = judge = None
        trace = Trace()
        phase = "preparing"
        result = {"status": "stopped", "independently_verified": False,
                  "reason": "The task stopped before executing agent actions."}
        try:
            if run.stop.is_set():
                return
            run.status = "running"
            run.emit({"event": "run_started", "mode": run.mode, "app": run.app["name"]})
            if run.mode == "demo":
                result = self.demo(run)
                return
            # Model clients first: their connections (and the gcloud token) open
            # while the driver is configured, the phone is checked and the app
            # launches, instead of during the first observation.
            ledger = SpendLedger()

            def track(event):
                ledger.add_event(event)
                run.emit(event)

            with trace.span("setup.models"):
                model, helper = build_models(
                    helper=run.helper_model is not None and self.status()["helper_configured"],
                    helper_model=run.helper_model, on_inference=track)
                judge = build_vision_judge(enabled=helper is not None, on_inference=track)
                warm_clients(model, helper, judge)

            def set_phase(value):
                nonlocal phase
                phase = value

            def hold(value):
                nonlocal driver
                driver = value
                return value

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
                          spend_cap_usd=getattr(self.config, "spend_cap_usd", None), spend_ledger=ledger,
                          allowed_bundles=run.allowed_bundles, replay_store=self.replay_store,
                          approve=run.request_approval if self.ask_before_acting() else None,
                          ask=run.request_approval, vision_judge=judge, loop_store=self.loop_store,
                          loop_mode="dry_run" if run.dry_run else "auto", trace=trace,
                          decision_memo=self.decision_memo, launch_bundle=run.app["bundleId"],
                          bypass=self.bypass_checks())
            phase = "running"
            result = agent.run(f"In {run.app['name']}: {run.goal}", execute=not run.dry_run,
                               output_schema=run.output_schema, output_format=run.output_format)
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
        finally:
            self.awake.release(run.id)
            if run.mode == "live" and driver and not run.stop.is_set() and not self.closing:
                with trace.span("finish.preview"):
                    self.preview(run, final=True)
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

    def close(self, timeout=25):
        with self.lock:
            self.closing = True
        if self.scheduler and not self.scheduler.close(timeout):
            return False  # Keep journal/device ownership while a scheduler dispatch may still be in flight.
        self.video.close()
        if getattr(self, "control", None) is not None:
            self.control.close()
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


def api_path(path):
    """Route /v1/* to the same handlers as /api/*. Unversioned paths keep working."""
    return "/api/" + path[len("/v1/"):] if path.startswith("/v1/") else path


def make_handler(runtime, token=None):
    """The API's request handler. ``serve`` always passes the per-launch ``token``;
    ``None`` (tests that build a handler directly) leaves requests unauthenticated."""
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
            self.send_header("Access-Control-Allow-Methods", "GET, POST")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Idempotency-Key")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            if self.refused():
                return
            path = api_path(urlsplit(self.path).path)
            if path == "/api/status":
                return self.send_json(runtime.status())
            if path == "/api/apps":
                return self.send_json({"apps": runtime.apps()})
            if path == "/api/workflows":
                try:
                    return self.send_json({"workflows": runtime.workflows.list(), "schedulerError": runtime.workflows.scheduler_error})
                except JournalError:
                    return self.send_json({"error": "Saved tasks are temporarily unavailable", "code": "persistence_unavailable"}, 503)
            if path == "/api/usage":
                return self.send_json(runtime.journal.usage())
            if path == "/api/usage/estimate":
                return self.send_json(planning_estimate(os.environ.get("TYPESAFE_MODEL", "jev-latest"),
                    configured_model(),
                    os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
                    if os.environ.get("TEXT_MODEL_PROVIDER") == "vertex" else None))
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
                return self.send_json({"runs": [r.public(include_events=False) for r in reversed(runs)]})
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
                else:
                    return self.send_json({"error": "Not found"}, 404)
            except LookupError as error:
                return self.send_json({"error": str(error)}, 409)
            except ValueError as error:
                return self.send_json({"error": str(error)}, 400)
            return self.send_json(setup.state())

        def send_stream(self):
            """Multipart JPEG (MJPEG): an <img> element plays it directly.

            ``?framing=raw`` sends the same parts as application/octet-stream for
            the dashboard's fetch reader: WebKit (the desktop app's webview) parses
            multipart/x-mixed-replace itself and hands fetch() bodies without the
            part headers, so the reader never saw a frame there.
            """
            raw = parse_qs(urlsplit(self.path).query).get("framing") == ["raw"]
            if not isinstance(runtime.video, WdaVideo):
                return self.send_json({"error": "Live stream needs a USB iPhone", "code": "video_unavailable"}, 503)
            try:
                viewer = runtime.video.subscribe()
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
                    frame = runtime.video.next_frame(viewer, sequence, timeout=10)
                    if frame is None:
                        if runtime.video.closed:
                            break
                        continue
                    sequence, content_type, data = frame[0], frame[1], frame[2]
                    self.wfile.write(f"--{WDA_BOUNDARY}\r\nContent-Type: {content_type}\r\n"
                                     f"Content-Length: {len(data)}\r\n\r\n".encode() + data + b"\r\n")
                    self.wfile.flush()
            except (OSError, TimeoutError):
                pass
            finally:
                runtime.video.unsubscribe(viewer)

        def send_frame(self):
            """The newest frame as a plain image (thumbnails, first paint)."""
            frame = runtime.video.latest() if isinstance(runtime.video, WdaVideo) else None
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
                    if (set(body) - {"goal", "outputSchema", "helperModel"} or not isinstance(body.get("goal"), str)
                            or len(body["goal"].strip()) > 4000):
                        raise ValueError("Supply a draft of at most 4000 characters and optional output schema/helper model")
                    schema = validate_schema(body["outputSchema"]) if body.get("outputSchema") is not None else None
                    return self.send_json(planning_estimate(os.environ.get("TYPESAFE_MODEL", "jev-latest"),
                        resolve_helper_model(body.get("helperModel")),
                        os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
                        if os.environ.get("TEXT_MODEL_PROVIDER") == "vertex" else None,
                        goal=body["goal"].strip(), output_schema=schema))
                if path == "/api/runs":
                    if body.get("mode", "live") != "live":
                        raise ValueError("Dashboard tasks run on your iPhone. Synthetic replays are not supported here.")
                    if len(self.headers.get_all("Idempotency-Key", [])) > 1:
                        raise ValueError("Supply only one Idempotency-Key")
                    key = self.headers.get("Idempotency-Key")
                    if set(body) - {"appId", "goal", "mode", "outputSchema", "outputFormat", "helperModel",
                                         "allowedBundles", "dryRun"}:
                        raise ValueError("Unsupported task field")
                    created = runtime.create(body.get("appId"), body.get("goal"), "live", key,
                                             output_schema=body.get("outputSchema"),
                                             output_format=body.get("outputFormat", "auto"),
                                             helper_model=body.get("helperModel"),
                                             allowed_bundles=body.get("allowedBundles"),
                                             dry_run=body.get("dryRun", False))
                    run, replayed = created if key else (created, False)
                    return self.send_json({"run": run.public(), "replayed": replayed}, 200 if replayed else 201)
                if path == "/api/settings":
                    return self.send_json(runtime.update_settings(body))
                if path == "/api/keys":
                    return self.send_json(runtime.keys.update(body))
                if path == "/api/keys/test":
                    if set(body) != {"target"}:
                        raise ValueError("Send the target to test: jev or helper")
                    return self.send_json(runtime.keys.test(body["target"]))
                if path == "/api/export":
                    return self.send_json(save_export(body))
                if path == "/api/device/control":
                    return self.send_json({"ok": True, "action": runtime.control_device(body)})
                match = re.fullmatch(r"/api/runs/([a-f0-9]{12})/(stop|approval)", path)
                run = runtime.runs.get(match[1]) if match else None
                if run and match[2] == "approval":
                    if (set(body) - {"choice"} != {"id", "approve"} or not isinstance(body["id"], str)
                            or type(body["approve"]) is not bool
                            or "choice" in body and not isinstance(body["choice"], str)):
                        raise ValueError("An approval needs its id and approve: true or false (and optionally a choice)")
                    if not run.answer_approval(body["id"], body["approve"], body.get("choice")):
                        raise APIError("This approval request is no longer waiting", 409, "approval_not_pending")
                    return self.send_json({"run": run.public()})
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
            except (OSError, TimeoutError):
                return self.send_json({"error": "Request timed out", "code": "request_timeout"}, 408)

    return Handler


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


def token_file(config):
    """Where ``serve`` leaves its token for local tools (the Vite proxy, curl): beside the journal."""
    state_db = getattr(config, "state_db", None)
    return Path(state_db).parent / TOKEN_FILE if state_db else None


def write_private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w") as stream:
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
    runtime = runtime_class(config)
    server = None
    try:
        server = BoundedServer(("127.0.0.1", config.port), make_handler(runtime, token))
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
