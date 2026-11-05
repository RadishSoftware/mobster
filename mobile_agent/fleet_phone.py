"""One device as the server drives it (SPEC §3): ``Phone``, and ``PrimaryPhone``, the runtime's own device."""

import threading
import time


class Phone:
    """One device as the server drives it."""

    primary = False

    def __init__(self, fleet, *, id, kind, wda_url=None, mjpeg_url=None, udid=None, name=None, model=None, ios=None,
                 manager=None, setup=None):
        self.fleet, self.runtime = fleet, fleet.runtime
        self.id, self.kind, self.udid = id, kind, udid
        self._wda_url, self.mjpeg_url = (wda_url or "").rstrip("/") or None, mjpeg_url
        self.name, self.model, self.ios = name, model, ios
        self.manager, self.setup = manager, setup
        self.session_id, self.session_lock = None, threading.Lock()
        self.health_at, self.health_ready = 0.0, False
        self.health_lock = threading.Lock()
        self._video = self._control = None
        self.installed_apps = self.device_info = None
        self.objects_lock = threading.Lock()
        # A simulator's start (boot and WebDriverAgent) runs in the background: {"state", "error"}.
        self.start_state = {"state": "idle", "error": None}

    @property
    def key(self):
        """What ``Runtime.active_runs`` records for a run on this device."""
        return self.id

    @property
    def wda_url(self):
        return self._wda_url

    # -- WebDriverAgent ---------------------------------------------------------------------------------

    def wda_session(self):
        from .drivers import resolve_wda_session
        with self.session_lock:
            self.session_id = resolve_wda_session(self.wda_url, preferred=self.session_id)
            return self.session_id

    def current_session(self):
        try:
            return self.wda_session()
        except Exception:
            return None

    def target_status(self):
        """The runtime's target_status for this device: whether WDA answers and reaches the phone's UI."""
        ready, health = False, None
        if self.wda_url:
            with self.health_lock:
                if not self.health_at or time.monotonic() - self.health_at > 5:
                    from .transport import HTTP
                    client = None
                    try:
                        client = HTTP(self.wda_url)
                        response = client.request("GET", "/status", timeout=2)
                        self.health_ready = response.get("value", {}).get("ready") is True
                        if isinstance(response.get("sessionId"), str) and response["sessionId"]:
                            self.session_id = response["sessionId"]
                    except Exception:
                        self.health_ready = False
                    finally:
                        if client:
                            client.close()
                        self.health_at = time.monotonic()
                ready = self.health_ready
            health = self.manager.health_error() if self.manager is not None else None
            if ready and health:
                ready = False
        return {"device": bool(self.wda_url), "ready": ready, "health": health, "can_act": bool(self.wda_url),
                "driver": "wda", "screen_reading": False}

    # -- per-device objects -----------------------------------------------------------------------------

    @property
    def video(self):
        """This device's live view (and the frame clock's stream), made on first use."""
        if self._video is None and self.wda_url:
            from .wda.video import WdaVideo
            with self.objects_lock:
                if self._video is None:
                    self._video = WdaVideo(self.wda_url, mjpeg_url=self.mjpeg_url, session=self.current_session,
                                           quality=self.runtime.video_quality())
        return self._video

    @property
    def control(self):
        if self._control is None and self.wda_url:
            from .manual_control import ManualControl
            with self.objects_lock:
                if self._control is None:
                    self._control = ManualControl(self.wda_url, self.wda_session)
        return self._control

    def apps(self):
        """The apps a task on this device may name: the catalog, then the apps found on the phone."""
        from .catalog import APPS
        from .installed_apps import merge as merge_installed
        inventory = self.installed_apps.snapshot() if self.installed_apps else None
        return merge_installed(APPS, inventory)

    def info(self):
        if self.device_info is not None:
            return self.device_info.snapshot()
        return {"modelIdentifier": self.model if self.kind == "usb" else None,
                "modelName": self.model if self.kind == "simulator" else None, "iosVersion": self.ios,
                "buildVersion": None, "virtual": self.kind != "usb", "source": self.kind,
                "verifiedAt": None, "stale": False}

    def close(self, keep_runner=False):
        for item in (self._video, self._control):
            if item is not None:
                try:
                    item.close()
                except Exception:
                    pass
        if self.manager is not None:
            if keep_runner:
                self.manager.watching.set()
            else:
                self.manager.close()


class PrimaryPhone(Phone):
    """The runtime's own device: everything is the runtime's, looked up when used."""

    primary = True

    def __init__(self, fleet):
        super().__init__(fleet, id=None, kind="usb")

    @property
    def key(self):
        return "@primary"

    @property
    def wda_url(self):
        url = getattr(self.runtime.config, "wda_url", None)
        return url.rstrip("/") if isinstance(url, str) and url else None

    @property
    def manager(self):
        return getattr(self.runtime, "manager", None)

    @manager.setter
    def manager(self, value):
        pass

    @property
    def setup(self):
        return getattr(self.runtime, "setup", None)

    @setup.setter
    def setup(self, value):
        pass

    @property
    def video(self):
        return self.runtime.video

    @property
    def control(self):
        return getattr(self.runtime, "control", None)

    def wda_session(self):
        return self.runtime.wda_session()

    def current_session(self):
        return self.runtime.current_wda_session()

    def target_status(self):
        return self.runtime.target_status()

    def apps(self):
        return self.runtime.apps()

    def info(self):
        return self.runtime.device_info.snapshot()

    def close(self, keep_runner=False):
        pass  # Runtime.close closes the runtime's own objects
