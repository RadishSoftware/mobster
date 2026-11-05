"""The runtime for the experimental iPhone Mirroring connection."""

import threading
import time

from . import server
from .agent_hooks import READY, GuardVerdict, safe_check
from .compose import build_target_driver
from .runtime_driver import PhoneNotReady

# The status read (the Mirroring window's state) is reused this long.
STATUS_SECONDS = 3.0
CONFIG_URL = "mirroring://iphone"
# Names the icons Vision can't read; a glyph it named is remembered per app.
ICON_MODEL = "claude-haiku-5-5"


def wanted(config=None):
    """Whether this process drives the phone through iPhone Mirroring."""
    from . import mirroring
    return mirroring.enabled() and not getattr(config, "manage_device", False)


def message_for(state):
    """What the Mirroring window's state means for a task, then what to do; None when it shows the phone."""
    from .mirroring import FIXES
    kind = state.get("state")
    if kind == "ready":
        return None
    if kind == "blocked":
        said = (state.get("said") or ["iPhone Mirroring isn't showing your iPhone"])[0]
        return f"{said}. " + FIXES["login" if state.get("login") else "blocked"]
    if kind == "no-window":
        return "iPhone Mirroring has no phone window. " + FIXES["no_window"]
    if kind == "not-running":
        return "iPhone Mirroring isn't open. " + FIXES["not_running"]
    return "Mobster couldn't read iPhone Mirroring. " + (state.get("fix") or FIXES["permission"])


class HelperGuard:
    """A helper connection's phone guard: a task goes on only while the helper says it can, and stops when unsure."""

    def __init__(self, connection, subject, message_for, fix_for):
        self.connection, self.subject = connection, subject
        self.message_for, self.fix_for = message_for, fix_for

    def check(self, driver, *, cause):
        helper = getattr(driver, "helper", None)
        if helper is None:
            return GuardVerdict("stop", self.connection, f"Mobster lost its connection to {self.subject}.")
        try:
            state = helper.request("state", timeout=5)
        except Exception as error:  # noqa: BLE001 -- unsure is a stop
            return GuardVerdict("stop", self.connection,
                                f"Mobster couldn't read {self.subject}. {self.fix_for(error)}".strip())
        problem = self.message_for(state)
        return READY if problem is None else GuardVerdict("stop", self.connection, problem)

    def attached(self):
        return None  # no cable to pull

    def wifi_lost(self):
        return False

    def allows_app(self, bundle_id):
        return True  # the person's app choices wrap this guard (Runtime.make_guard)

    def finish(self, driver):
        pass


class MirroringRuntime(server.Runtime):
    """``server.Runtime`` over iPhone Mirroring. One phone: the one this Mac mirrors."""

    one_target = True
    # The driver build_target_driver makes (cable_bt_runtime.py runs the Cable + Bluetooth one through this class).
    connection = "mirroring"
    # What guard messages call the connection.
    subject = "iPhone Mirroring"

    def __init__(self, config):
        super().__init__(config)
        # No WebDriverAgent: the live view and the person's taps there come from this connection (make_video,
        # make_control), not from the WebDriverAgent that Runtime builds for any wda_url.
        for made in (self.video, self.control):
            if made is not None:
                made.close()
        self.video = self.make_video(config)
        self.control = self.make_control()
        self._status_lock = threading.Lock()
        self._status_helper = None
        self._status, self._status_at = {"state": "unknown"}, 0.0

    def make_video(self, config):
        """The live view: ScreenCaptureKit frames of the Mirroring window, also while it is behind other windows."""
        from .mirroring import live_video
        return live_video(on_activity=self.video_activity, quality=self.video_quality())

    def make_control(self):
        """The person's taps on the live view, through the Mirroring helper."""
        from .mirroring import MirroringControl
        return MirroringControl()

    def message_for(self, state):
        return message_for(state)

    def fix_for(self, error):
        from .mirroring import fix_for
        return fix_for(error)

    def mirroring_state(self):
        """The Mirroring window's state, read at most every STATUS_SECONDS by a helper of its own."""
        with self._status_lock:
            if time.monotonic() - self._status_at < STATUS_SECONDS:
                return self._status
            try:
                if self._status_helper is None:
                    from .mirroring import Helper
                    self._status_helper = Helper()
                self._status = self._status_helper.request("state", timeout=5)
            except Exception as error:  # noqa: BLE001 -- reported as not ready, with what to do
                from .mirroring import fix_for
                self._status = {"state": "unreadable", "fix": fix_for(error)}
                helper, self._status_helper = self._status_helper, None
                if helper is not None:
                    helper.close()
            self._status_at = time.monotonic()
            return self._status

    def target_status(self):
        problem = self.message_for(self.mirroring_state())
        return {"device": True, "ready": problem is None, "health": problem, "can_act": True, "driver": self.connection,
                "screen_reading": True}

    def not_ready_message(self, status):
        problem = self.message_for(self.mirroring_state())
        return problem or super().not_ready_message(status)

    def icon_labeller(self):
        """The model that names icons Vision can't read, or None: icons then stay unlisted, and a tap on one asks."""
        import os
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return None
        from .model_clients import chat_client
        try:
            return chat_client(ICON_MODEL, reasoning="low")
        except Exception:  # noqa: BLE001 -- no labeller is a working, more careful mode
            return None

    def icon_memory(self):
        """Icon names learned per app, kept in the data folder across runs (perception.IconMemory)."""
        if getattr(self, "_icon_memory", None) is None:
            from .paths import user_data_dir
            from .perception import IconMemory
            folder = user_data_dir()
            try:
                folder.mkdir(parents=True, exist_ok=True)
                self._icon_memory = IconMemory(str(folder / "mirroring-icons.json"))
            except OSError:
                self._icon_memory = IconMemory()
        return self._icon_memory

    def check_app(self, app):
        pass  # apps open by name (ios.system); Mobster can't list the phone's apps here

    def route_memory(self):
        """Where the routes that opened apps on this phone are kept (learned.routes), or None."""
        from pathlib import Path

        from .learned.routes import for_data_dir
        state_db = getattr(self.config, "state_db", None)
        return for_data_dir(Path(state_db).parent) if state_db else None

    def app_killer(self, phone, primary):
        return None

    def phone_guard(self, run, phone):
        return HelperGuard(self.connection, self.subject, self.message_for, self.fix_for)

    def build_driver(self, names):
        return build_target_driver(connection=self.connection, app_names=names, labeller=self.icon_labeller(),
                                   icon_memory=self.icon_memory())

    def open_driver(self, run, trace, setup):
        """As ``Runtime.open_driver``: the driver, the guard's check, Home, then the task's app by name (ios.system)."""
        phone = self.phone_for(run)
        names = {app["bundleId"]: app["name"] for app in self.apps() if app.get("bundleId") and app.get("name")}
        with trace.span("setup.driver"):
            driver = setup.hold(self.build_driver(names))
        driver.routes = self.route_memory()
        if run.stop.is_set():
            return None
        setup.phase("preflight")
        run.guard = self.make_guard(run, phone)
        with trace.span("setup.preflight"):
            verdict = safe_check(run.guard, driver, "preflight")
            if verdict is not None and verdict.state == "stop":
                raise PhoneNotReady(verdict.message, verdict.code or "mirroring")
            driver.execute("HOME", None, None)
        if run.stop.is_set():
            return None
        if not self.approve_start_apps(run, phone):
            return None
        if not run.app.get("bundleId") or getattr(run, "start_on_home", False):
            setup.phase("reading_app")
            return driver
        run.emit({"event": "app_launch_started", "app": run.app["name"]})
        setup.phase("launching")
        with trace.span("launch.activate"):
            driver.execute("LAUNCH_APP", run.app["bundleId"], None)
        run.emit({"event": "app_launch_acknowledged", "app": run.app["name"]})
        setup.phase("reading_app")
        return driver

    def close(self, *args, **kwargs):
        try:
            return super().close(*args, **kwargs)
        finally:
            helper, self._status_helper = self._status_helper, None
            if helper is not None:
                helper.close()
