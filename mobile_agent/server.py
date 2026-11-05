"""Loopback-only dashboard API with SSE, cancellation, and one device owner at a time."""

import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from . import engine_health, engines, eventbus, harness_api, ownership, plan_gate, tracks
from .api_devices import DeviceRoutesMixin
from .api_get import GetRoutesMixin
from .api_http import ApiBase
from .api_post import WriteRoutesMixin
from .app_icons import AppIcons
from .cli_constants import DEFAULT_WDA_URL
from .costs import NANODOLLARS
from .device_info import DeviceInfo
from .device_manager import DeviceManager
from .fanout import GROUP_ID, Fanout
from .fleet import Fleet
from .installed_apps import InstalledApps, InventoryCache
from .journal import Journal, JournalError
from .manual_control import ManualControl
from .paths import user_data_dir
from .pool import default_mjpeg_url
from .power_watch import PowerWatch
from .run import Run
from .runtime_create import TaskStartMixin
from .runtime_driver import PhoneDriverMixin
from .runtime_limits import LimitsMixin
from .runtime_queue import PhoneQueueMixin
from .runtime_runs import RunActionsMixin
from .runtime_status import StatusMixin
from .runtime_teach import LiveViewMixin
from .runtime_work import TaskWorkMixin
from .setup_service import SetupService
from .wda.frame_store import FrameStore
from .wda.video import WdaVideo
from .workflows.service import Workflows

DONE_ACTION_EVENTS = frozenset({"action_acknowledged", "frontier_action"})
ACTING_EVENTS = frozenset({"action_started", "app_launch_started", "frontier_action", "frontier_failed",
                           "skill_started",
                           "frontier_app_relaunched", "frontier_cleanup"})
IDLE_OPERATIONS = frozenset({None, "DONE", "BLOCKED", "WAIT"})


def may_have_acted(event):
    # Fails closed: a decision is journaled before it is sent, and one whose ops can't be read counts as acting.
    kind = event.get("event")
    if kind in ACTING_EVENTS:
        return True
    if kind != "frontier_decision":
        return False
    ops = event.get("ops")
    return not isinstance(ops, list) or any(op not in IDLE_OPERATIONS for op in ops)


class Runtime(PhoneQueueMixin, PhoneDriverMixin, LiveViewMixin, LimitsMixin, StatusMixin, TaskStartMixin, TaskWorkMixin,
              RunActionsMixin):
    # `mobster run` and the terminal UI: a task that names no device runs on the one target, without the device list.
    one_target = False
    plan_gate = None
    # The demo (a scripted phone and model) never acts on a phone, so it needs no plan.
    needs_plan = True
    # The sentence a yes to an outside agent's approval is refused with, or None: the hosted review demo
    # (remote/review.py) sets it, and Run.answer_approval then takes no yes from the Mac window, a link or the console.
    approvals_closed = None
    # The device list's class (fleet.Fleet); a scripted world of several phones gives its own (tests/fleet_world.py).
    fleet_class = Fleet

    def __init__(self, config):
        # The tracks register first: their schemas migrate when the journal opens below.
        tracks.load()
        self.config = config
        self.runs = {}
        self.lock = threading.Lock()
        self._conversation_apps = {}
        self.listeners = harness_api.ListenerQueue()
        self.bus = eventbus.bus
        # run id -> device id: one live run per device.
        self.active_runs = {}
        self.queues = ownership.Queues(ttl=ownership.ttl_seconds())
        self.queued = {}
        self.queue_timers = {}
        self.pool = self.make_pool(config)
        self.inventory = {}
        self.inventory_at = 0
        self.inventory_lock = threading.Lock()
        self.inventory_ok = False
        self.wda_health_at, self.wda_ready = 0, False
        # Re-resolved from WDA itself: a runner restart replaces the session, so a fixed --session would fail.
        self.wda_session_id, self.wda_session_lock = getattr(config, "session", None), threading.Lock()
        self.closing = False
        self.threads = set()
        self.streams = threading.BoundedSemaphore(8)
        # Set by ``serve`` once it listens; Mobster Remote's bridge waits on ``serving``.
        self.local_api = None
        self.serving = threading.Event()
        self.device_info = self.make_device_info(config)
        self.awake = self.make_awake_lease()
        self.power = PowerWatch(self)
        # WDA's MJPEG port is paired with its own (8100 -> 9100, 8203 -> 9203), never another device's stream.
        self.video = (WdaVideo(config.wda_url, mjpeg_url=default_mjpeg_url(config.wda_url),
                               session=self.current_wda_session,
                               on_activity=self.video_activity, quality=self.video_quality())
                      if getattr(config, "wda_url", None) else self.make_video(config))
        self.control = ManualControl(config.wda_url, self.wda_session) if getattr(config, "wda_url", None) else None
        self.controlled_at = {}
        self.lessons, self.lessons_lock = {}, threading.Lock()
        self.journal = Journal(getattr(config, "state_db", None))
        state_db = getattr(config, "state_db", None)
        self.frames = FrameStore(Path(state_db).parent / "frames" if state_db else None)
        self.model_reach = engines.ModelReach()
        self.scheduler = None
        self.manager = self.setup = None
        self.installed_apps = None
        data_dir = Path(getattr(config, "data_dir", None) or user_data_dir())
        self.plan_gate = plan_gate.for_runtime(config, data_dir) if self.needs_plan else None
        self.icons = AppIcons(data_dir / "app-icons", allowed=self.icon_bundles)
        if getattr(config, "manage_device", False):
            env_file = getattr(config, "env_file", None) or data_dir / "agent.env"
            self.manager = DeviceManager(data_dir, config.wda_url or DEFAULT_WDA_URL)
            self.setup = SetupService(self, self.manager, env_file)
            self.device_info = DeviceInfo(manager=self.manager)
            threading.Thread(target=self.manager.watch, args=(self.manager.watching,), name="mobster-device-watch",
                             daemon=True).start()
            # --socket exists only with the optional extension, which the signed app doesn't ship.
            if not getattr(config, "socket", None):
                self.installed_apps = InstalledApps(self.manager.device,
                                                    cache=InventoryCache(data_dir / "installed-apps"),
                                                    over_wifi=self.manager.wifi_udid)
        self.fleet = self.fleet_class(self)
        self.engine_health = engine_health.EngineHealth(publish=self.publish, boot=self.bus.boot)
        self.engine_sampler = engine_health.Sampler(self)
        try:
            self.workflows = Workflows(self)
            for item in self.journal.load():
                run = Run(item["app"], item["goal"], item["mode"], id=item["id"],
                    status=item["status"], created_at=item["createdAt"] / 1000,
                    finished_at=item["finishedAt"] / 1000 if item["finishedAt"] is not None else None,
                    events=item["events"], summary=item["summary"],
                    idempotency_key=item.get("idempotencyKey"), output_schema=item.get("outputSchema"),
                    output_format=item.get("outputFormat", "json"),
                    allowed_bundles=(frozenset(item["allowedBundles"]) if item.get("allowedBundles") else None),
                    journal=self.journal, engine=item.get("engine"), estimate_usd=item.get("estimateUsd"),
                    reviewed_ok=item.get("reviewedOk") is True, frames=self.frames,
                    device_id=item.get("device"), device_name=item.get("deviceName"),
                    group_id=item["groupId"] if isinstance(item.get("groupId"), str)
                    and GROUP_ID.fullmatch(item["groupId"]) else None,
                    extras=dict(item.get("extras") or {}),
                    origin=item.get("origin") if item.get("origin") in harness_api.ORIGINS else "api",
                    pinned_apps=item.get("pinnedApps") if isinstance(item.get("pinnedApps"), list) else None,
                    block_state=dict(item["blockState"]) if isinstance(item.get("blockState"), dict) else {},
                    shares=[dict(x) for x in item["shares"] if isinstance(x, dict)][:50]
                    if isinstance(item.get("shares"), list) else [],
                    issues=[dict(x) for x in item["issues"] if isinstance(x, dict)][:50]
                    if isinstance(item.get("issues"), list) else [])
                if isinstance(item.get("costUsd"), (int, float)):
                    run.cost_nanodollars, run.cost_known = round(item["costUsd"] * NANODOLLARS), True
                self.runs[run.id] = run
                if run.finished_at is None:
                    # A task that waited for a held phone never started a worker: it acted on nothing.
                    waited = not any(e.get("event") == "run_started" or may_have_acted(e) for e in run.events)
                    run.finish({"event": "result", "status": "interrupted", "independently_verified": False,
                        "reason": ownership.QUEUE_RESTART if waited else
                        "The service restarted. An in-flight action may have run; this task was not resumed.",
                        "actions": sum(e["event"] in DONE_ACTION_EVENTS for e in run.events),
                        "actions_may_have_run": any(may_have_acted(e) for e in run.events)})
                    if run.summary.get("persistence_failed"):
                        raise JournalError("Interrupted task recovery could not be saved")
        except Exception:
            self.journal.close()
            raise
        self.fanout = Fanout(self)
        self.start_services()

    def start_services(self):
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

    def scripted_models(self):
        return type(self).smart_client is not Runtime.smart_client

    def device_busy(self, device_id):
        with self.lock:
            return any(value == device_id for value in self.active_runs.values()) or any(
                getattr(run, "device_id", None) == device_id and run.id in self.active_runs
                for run in self.runs.values())

    def run_context(self, run):
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
            runtime=self, account_context=run.account_context, workflow_id=run.workflow_id)

    def current_wda_session(self):
        # Without a session the stream stays at full size and framerate (measured: 1178x2556 at ~9.5 fps on an
        # iPhone 15 Pro). WDA keeps MJPEG settings server-wide, so making one here changes nothing a run relies on.
        try:
            return self.wda_session()
        except Exception:
            return None

    def video_activity(self, active):
        self.awake.acquire("video") if active else self.awake.release("video")

    @property
    def active(self):
        return next(iter(self.active_runs), None)

    def capacity(self):
        if self.pool is None:
            fleet = getattr(self, "fleet", None)
            if fleet is None:
                return 0 if self.active_runs else 1
            return 0 if self.run_on(fleet.primary) else 1
        free = self.pool.position()
        return max(0, free["healthy"] - free["busy"])

    def publish(self, topic, event):
        try:
            (getattr(self, "bus", None) or eventbus.bus).publish(topic, event)
        except Exception:  # noqa: BLE001
            harness_api.log.exception("event bus publish failed")

    def close(self, timeout=25):
        deadline = time.monotonic() + timeout
        self.engine_sampler.close()
        # Stop every task before the runners and relays go: quitting the Mac app gives this process only SIDECAR_GRACE
        # (desktop/src-tauri/src/sidecar.rs).
        with self.lock:
            self.closing = True
            waiting = self.drop_queues()
            for run in self.runs.values():
                if run.finished_at is None:
                    with run.condition:
                        if not run.stop.is_set():
                            run.quitting = True
                        run.stop.set()
                        run.condition.notify_all()
        scheduled_checks = getattr(self, "test_schedules", None)
        if scheduled_checks is not None and not scheduled_checks.close(max(0.0, deadline - time.monotonic())):
            return False  # Its suite may still dispatch: keep the phone and journal owned.
        from .projects import service as project_service
        projects = project_service.service_for(self)
        if projects is not None:
            if projects.begin_close() is False:
                return False
            if not projects.join(max(0.0, deadline - time.monotonic())):
                return False  # An accepted project check may still dispatch or write its report.
        for timer in list(self.queue_timers.values()):
            timer.cancel()
        for run in waiting:
            try:
                self.finished(run)
            except Exception:  # noqa: BLE001 -- closing goes on
                harness_api.log.exception("run_finished listeners failed")
        if not getattr(self.config, "keep_runner", False):
            phones = list(getattr(getattr(self, "fleet", None), "phones", {}).values())
            for manager in [self.manager] + [getattr(phone, "manager", None) for phone in phones]:
                begin = getattr(manager, "begin_close", None)
                if callable(begin):
                    try:
                        begin()
                    except Exception:  # noqa: BLE001 -- closing goes on; close() below stops it the slow way
                        pass
        workflows = getattr(self, "workflows", None)
        if workflows is not None and hasattr(workflows, "close"):
            if workflows.close() is False:
                return False  # a batch may still write its ending to the journal
        if self.scheduler and not self.scheduler.close(max(0.0, deadline - time.monotonic())):
            return False  # a scheduler dispatch may still be in flight
        self.video.close()
        if getattr(self, "control", None) is not None:
            self.control.close()
        if getattr(self, "fleet", None) is not None:
            self.fleet.close(keep_runner=getattr(self.config, "keep_runner", False))
        if not getattr(self.config, "keep_runner", False):
            from .sim import wda as sim_wda
            sim_wda.stop_started()  # a simulator's runner this process started goes with it
        if self.manager is not None:
            if getattr(self.config, "keep_runner", False):
                # A development reload: the runner and relay outlive this process and the next server adopts them.
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
        self.power.close()
        for thread in threads:
            thread.join(max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in threads):
            # Keep the leases and journal until exit: an old worker might still dispatch.
            return False
        if getattr(self, "fanout", None) is not None:
            self.fanout.close()
        if getattr(self, "listeners", None) is not None:
            if not self.listeners.close(timeout=max(0.0, deadline - time.monotonic())):
                return False
        self.close_services()
        if getattr(self, "frames", None) is not None:
            self.frames.close()
        self.journal.close()
        return True


class ApiHandler(GetRoutesMixin, WriteRoutesMixin, DeviceRoutesMixin, ApiBase):
    pass


def make_handler(runtime, token=None, app_session=None, setup_token=None):
    # token None (tests only) leaves requests unauthenticated.
    return type("Handler", (ApiHandler,), {"runtime": runtime, "token": token, "app_session": app_session,
                                           "setup_token": setup_token})


class BoundedServer(ThreadingHTTPServer):
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
