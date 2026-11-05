"""Runtime's phone driving: the driver a run opens, its guards and app approvals, and what the phone shows."""

import time
from dataclasses import dataclass
from pathlib import Path

from . import engines, harness_api, orchestrate, permissions
from .agent_hooks import safe_allows
from .api_errors import APIError
from .catalog import APPS, register_app_names
from .compose import build_target_driver
from .device_info import DeviceInfo
from .drivers import WDA, resolve_wda_session
from .installed_apps import apps_view
from .installed_apps import merge as merge_installed
from .lockscreen import UNLOCK_WAIT_S
from .transport import HTTP
from .wda.frame_clock import attach_frame_clock
from .wda.guards import DriverRejection
from .wda.video import VideoUnavailable as WdaVideoUnavailable
from .wda.video import WdaVideo


class PhoneNotReady(RuntimeError):
    def __init__(self, message, code="phone_locked"):
        super().__init__(message)
        self.code = code


# Locked phones and overlays (Control Center, Siri's glow) hang WDA's reads 8-20 s (measured).
PREFLIGHT_TIMEOUT = 4

# The app opens after the person answered "Allow Mobster to use …?", and the helper may have restarted meanwhile.
LAUNCH_RECOVER_S = 60

# The helper went away rather than refused the launch (frontier.CONNECTION_LOST, transport.NotSent).
LAUNCH_LOST = ("ConnectionRefusedError", "ConnectionResetError", "RemoteDisconnected", "BrokenPipeError",
               "ConnectionAbortedError", "StaleSession", "nothing sent")


def launch_app(driver, bundle):
    # After a lost request, open the app again only if it isn't in front: the look settles whether the request ran.
    try:
        driver.call("POST", "/wda/apps/activate", {"bundleId": bundle})
        return
    except Exception as error:
        recover = getattr(driver, "recover", None)
        if not callable(recover) or not any(mark in str(error) for mark in LAUNCH_LOST):
            raise
    recover(timeout=LAUNCH_RECOVER_S)
    try:
        front = (driver.call("GET", "/wda/activeAppInfo", None, PREFLIGHT_TIMEOUT) or {}).get("bundleId")
    except Exception:
        front = None
    if front != bundle:
        driver.call("POST", "/wda/apps/activate", {"bundleId": bundle})


def prepare_wda_phone(driver, guard=None):
    # The guard decides in code, before any model call; Home then clears Control Center, Siri and the app switcher.
    from .agent_hooks import safe_check
    from .lockscreen import DetectGuard
    verdict = safe_check(guard if guard is not None else DetectGuard(), driver, "preflight")
    if verdict.state == "stop":
        raise PhoneNotReady(verdict.message, verdict.code or "phone_locked")
    try:
        driver.call("POST", "/wda/pressButton", {"name": "home"}, PREFLIGHT_TIMEOUT)
    except Exception:
        raise PhoneNotReady("Your iPhone isn't responding. Unlock it, close Control Center or Siri if they're "
                            "open, then try again. It may have gone to the Home Screen; "
                            "nothing else was done.") from None


def is_scheduled(idempotency_key):
    from .graph.dispatch import unattended
    key = idempotency_key or ""
    return unattended(key) or key.startswith("workflow:") and ":scheduled:" in key


def person_started(origin, idempotency_key=None):
    return origin in harness_api.INTERACTIVE and not is_scheduled(idempotency_key)


# A person asked for these from elsewhere: their caller sees phoneWait and tells the person what to do.
UNLOCK_WAIT_ORIGINS = frozenset({"mcp", "remote", "console"})


def waits_for_unlock(origin, idempotency_key=None):
    return (person_started(origin, idempotency_key)
            or origin in UNLOCK_WAIT_ORIGINS and not is_scheduled(idempotency_key))


class PhoneAppsGuard:
    def __init__(self, inner, allowed):
        self._inner, self._allowed = inner, frozenset(allowed)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def allows_app(self, bundle_id):
        if bundle_id not in self._allowed:
            return False
        inner = getattr(self._inner, "allows_app", None)
        return bool(inner(bundle_id)) if callable(inner) else True


@dataclass(frozen=True)
class RunSetup:
    phase: object
    hold: object
    finish_phone: object


class NoAwakeLease:
    def acquire(self, owner):
        pass

    def release(self, owner):
        pass

    def close(self, timeout=5.0):
        pass

    def status(self):
        return {"status": "unsupported", "owners": 0, "bundleId": None, "error": None}


class NoVideo:
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


# Restart, cancellation and failure paths rely on one closable handle per run, pooled or not.
class PooledLease:
    def __init__(self, pool, owner):
        self.pool, self.owner = pool, owner

    def close(self):
        pool, self.pool = self.pool, None
        if pool is not None:
            pool.release(self.owner)


def runner_restart(manager):
    if manager is None or not callable(getattr(manager, "start_runner", None)):
        return None

    def restart():
        if getattr(manager, "transport", None) != "usb" or getattr(manager, "runner", None) is None:
            raise LookupError("Mobster doesn't run this phone's runner over USB now")
        manager.start_runner()
        return manager.runner_announced
    return restart


def planned_apps(run):
    try:
        plan = getattr(run, "plan", None)
        if not plan and getattr(run, "longform", None):
            plan = orchestrate.long_plan(run.longform["plan"])
        return [app["bundleId"] for app in (plan or {}).get("apps") or ()
                if isinstance(app, dict) and isinstance(app.get("bundleId"), str) and app["bundleId"]]
    except Exception:  # noqa: BLE001 -- the plan's apps ask when they open instead
        return []


class PhoneDriverMixin:
    def phone_for(self, run):
        phone = getattr(run, "phone", None)
        fleet = getattr(self, "fleet", None)
        return phone if phone is not None else fleet.primary if fleet is not None else None

    def apps_for(self, phone):
        return self.apps() if phone is None or phone.primary else phone.apps()

    # Target hooks: the private package's runtime (harness_api.EngineHooks.runtime_for) overrides these.
    def make_pool(self, config):
        return None

    def make_device_info(self, config):
        return DeviceInfo()

    def make_awake_lease(self):
        return NoAwakeLease()

    def make_video(self, config):
        return NoVideo()

    def target_status(self):
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
                "screen_reading": False}

    def primary_udid(self):
        if self.manager is None:
            return None
        try:
            return getattr(self.manager, "pinned", None) or self.manager.settings().get("udid")
        except Exception:
            return None

    def app_killer(self, phone, primary):
        from . import app_killer
        if primary:
            udid = self.primary_udid()
            return app_killer.device(udid) if udid else None
        udid = getattr(phone, "udid", None)
        if not udid:
            return None
        return {"usb": app_killer.device, "simulator": app_killer.simulator}.get(getattr(phone, "kind", None),
                                                                                   lambda _: None)(udid)

    def phone_guard(self, run, phone):
        from .lockscreen import guard_for
        primary = phone is None or phone.primary
        udid = None
        if primary:
            udid = self.primary_udid()
        elif getattr(phone, "kind", None) == "usb":
            udid = phone.udid
        key = run.idempotency_key or ""
        mode = "schedule" if is_scheduled(key) else "interactive"
        wait_s = UNLOCK_WAIT_S if waits_for_unlock(getattr(run, "origin", None), run.idempotency_key) else 0
        return guard_for(device_id=udid, wda_url=self.config.wda_url if primary else phone.wda_url, mode=mode,
                         approve=run.request_approval, emit=run.emit, usb=bool(udid), wait_s=wait_s,
                         cancelled=run.stop.is_set)

    def not_ready_message(self, status):
        return "Your iPhone is not ready for live tasks. Try again when it is available."

    def conversation_allowed(self, run):
        # In memory, newest 256 conversations: a restart asks again, which is the safe direction.
        thread_id = (run.extras or {}).get("threadId") if isinstance(getattr(run, "extras", None), dict) else None
        if not isinstance(thread_id, str) or not thread_id:
            return None
        with self.lock:
            allowed = self._conversation_apps.pop(thread_id, None) or set()
            self._conversation_apps[thread_id] = allowed
            while len(self._conversation_apps) > 256:
                del self._conversation_apps[next(iter(self._conversation_apps))]
            return allowed

    def make_guard(self, run, phone):
        guard = self.phone_guard(run, phone)
        origin = getattr(run, "origin", None)
        # Read live: switching an app off in Settings › Safety takes effect mid-run.
        guard = permissions.PermissionGuard(guard, lambda: permissions.from_env().apps,
                                            {a["bundleId"]: a["name"] for a in self.apps() if a.get("bundleId")},
                                            outside=origin in permissions.policy.OUTSIDE_ORIGINS,
                                            ask=run.request_approval, grants=self.permission_exceptions(),
                                            conversation=self.conversation_allowed(run))
        if isinstance(getattr(run, "phone_apps", None), frozenset):
            guard = PhoneAppsGuard(guard, run.phone_apps)
        return guard

    def approve_start_apps(self, run, phone=None):
        # One card before any app opens (owner, 14 Oct); None when Stop came, PhoneNotReady when one is declined.
        try:
            named = orchestrate.named_apps(run.goal,
                                           [a for a in self.apps_for(phone) if a.get("installed") is not False])
        except Exception:  # noqa: BLE001 -- the named apps ask when they open instead
            named = []
        starting = ([run.app.get("bundleId")] + [pin.get("bundleId") for pin in run.pinned_apps or ()]
                    + [app.get("bundleId") for app in named] + planned_apps(run) + sorted(run.allowed_bundles or ()))
        request = getattr(run.guard, "request", None)
        if not callable(request) or request([b for b in starting if b]):
            return True
        if run.stop.is_set():
            return None
        take = getattr(run.guard, "take_instruction", None)
        instruction = take() if callable(take) else None
        if instruction and run.engine == engines.SMART:
            # "Say what to do instead": the task starts without those apps and the words reach the agent.
            message = run.steering.put(instruction, source="app")
            run.emit({"event": "user_message", "id": message.id, "text": message.text, "source": message.source})
            declined = {b for b in starting if b and not safe_allows(run.guard, b)}
            if run.app.get("bundleId") in declined:
                run.start_on_home = True
            return True
        # Say why without a second card: the task can't start anyway.
        refusal = getattr(run.guard, "refusal", None)
        said = refusal([b for b in starting if b]) if callable(refusal) else None
        raise PhoneNotReady(said or f"Mobster didn't open {run.app['name']}.", code="blocked_app")

    def allows_app(self, run_id, bundle):
        # Fails closed for an unknown run.
        run = self.runs.get(run_id)
        guard = getattr(run, "guard", None) if run is not None else None
        if guard is None:
            return bundle in (None, "", "com.apple.springboard")
        return safe_allows(guard, bundle)

    def check_app(self, app):
        if app.get("installed") is False:
            raise ValueError(f"{app['name']} isn't installed on your iPhone")

    def verify_phone(self, phone, wda_url):
        from .wda import identity as wda_identity
        manager = self.manager if phone is None or phone.primary else getattr(phone, "manager", None)
        saved = {}
        try:
            saved = manager.settings() if manager is not None else {}
        except Exception:  # noqa: BLE001 -- nothing saved: nothing to compare
            pass
        name = (getattr(phone, "name", None) if phone is not None else None) or saved.get("device_name")
        try:
            wda_identity.verify(wda_url, name=name, model=getattr(phone, "model", None) if phone else None)
        except wda_identity.WrongDevice as error:
            raise APIError(str(error), 409, "wrong_device") from None

    def open_driver(self, run, trace, setup):
        # setup.hold hands the driver to the caller, which closes it; None when the run was stopped meanwhile.
        phone = self.phone_for(run)
        primary = phone is None or phone.primary
        wda_url = self.config.wda_url if primary else phone.wda_url
        if wda_url and (phone is None or getattr(phone, "kind", "usb") == "usb"):
            self.verify_phone(phone, wda_url)
        with trace.span("setup.driver"):
            session = (self.wda_session() if primary else phone.wda_session()) if wda_url else None
            driver = setup.hold(build_target_driver(wda_url=wda_url, session=session,
                                                    expected_bundle=run.app["bundleId"]))
        if isinstance(driver, WDA):
            driver.killer = self.app_killer(phone, primary)
            driver.runner_restart = runner_restart(self.manager if primary else getattr(phone, "manager", None))
        state_db = getattr(self.config, "state_db", None)
        if isinstance(driver, WDA) and state_db and WDA.overlay_hint_path is None:
            # Kept across restarts: the first read otherwise fails for ~9 s under the Siri overlay.
            WDA.load_overlay_hints(str(Path(state_db).parent / "wda-overlay-hints.json"))
        video = self.video if primary else phone.video
        if isinstance(driver, WDA) and isinstance(video, WdaVideo):
            # Subscribing keeps the relay's MJPEG stream up; closing the driver releases it.
            attach_frame_clock(driver, video, log_dir=Path(state_db).parent if state_db else None)
        if run.stop.is_set():
            return None
        setup.phase("preflight")
        run.guard = self.make_guard(run, phone)
        with trace.span("setup.preflight"):
            prepare_wda_phone(driver, run.guard)
        if run.stop.is_set():
            return None  # Stop pressed during the check: the app stays shut.
        if not self.approve_start_apps(run, phone):
            return None
        if not run.app.get("bundleId") or getattr(run, "start_on_home", False):
            setup.phase("reading_app")
            return driver
        run.emit({"event": "app_launch_started", "app": run.app["name"]})
        setup.phase("launching")
        with trace.span("launch.activate"):
            launch_app(driver, run.app["bundleId"])
        run.emit({"event": "app_launch_acknowledged", "app": run.app["name"]})
        setup.phase("reading_app")
        return driver

    def smart_client(self):
        return engines.acquire_client(engines.smart_key())

    def apps(self):
        inventory = self.installed_apps.snapshot() if self.installed_apps else None
        apps = merge_installed(APPS, inventory)
        if inventory is not None:
            register_app_names({app["bundleId"]: app["name"] for app in apps if app.get("foundOnPhone")})
        return apps

    def icon_bundles(self):
        return {app["bundleId"] for app in self.apps() if app.get("foundOnPhone") or app.get("installed") is True}

    def apps_payload(self, phone=None):
        if phone is not None and not phone.primary:
            installed = getattr(phone, "installed_apps", None)
            if installed is None:
                return {"apps": phone.apps(), "inventory": {"state": "unknown", "at": None}}
            return apps_view(APPS, installed)
        view = apps_view(APPS, self.installed_apps)
        if view["inventory"]["state"] == "live":
            register_app_names({app["bundleId"]: app["name"] for app in view["apps"] if app.get("foundOnPhone")})
        return view

    def preview(self, run, final=False):
        # Operator-only: never fed to the agent, captured off the accessibility hot path.
        if (not run.phone_finished and run.mode == "live" and callable(run.capture)
                and (final or run.status in {"running", "queued"})):
            if not run.image_lock.acquire(blocking=False):
                return {"image": run.image, "source": run.mode, "capturedAt": run.image_captured_at}
            try:
                if run.phone_finished:
                    return {"image": run.image, "source": run.mode, "capturedAt": run.image_captured_at}
                if final or time.monotonic() - run.image_at > 1:
                    try:
                        captured = run.capture(timeout=3)
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
        with self.wda_session_lock:
            self.wda_session_id = resolve_wda_session(self.config.wda_url, preferred=self.wda_session_id)
            return self.wda_session_id

    def limitations(self, *, key, device, ready, wda, can_act=None, health=None, engine_reason=None):
        reasons = []
        managed = getattr(self, "setup", None) is not None
        if not key:
            reasons.append(engine_reason if engine_reason and engine_reason != engines.NO_SMART_KEY
                           else engines.no_key_reason(managed))
        if not self.config.enable_live:
            reasons.append("Live runs are off; enable them to act on the phone")
        manager = getattr(self, "manager", None)
        if manager is not None and manager.expired():
            reasons.append("Mobster's helper needs a refresh: its signature expired. Refresh it in Setup")
        if device and not ready:
            reasons.append(health or (f"WebDriverAgent is not answering at {self.config.wda_url}" if wda
                                      else "Your iPhone is currently unreachable"))
        if not (wda or can_act):
            reasons.append("No iPhone attached: start the agent with --manage-device or --wda-url")
        return reasons
