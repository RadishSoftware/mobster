"""The values the simulator manager hands out and raises; ``api`` and the package re-export them."""

import threading
from dataclasses import dataclass
from typing import Optional

from ..errors import SimError as SimError

SIM_KINDS = ("environment", "simulator", "wda", "install", "launch", "busy")


@dataclass(frozen=True)
class AppInfo:
    bundle_id: str
    name: str             # CFBundleDisplayName, else CFBundleName, else the bundle id
    version: str          # "1.0 (1)"
    path: Optional[str]   # the .app, or None for an app looked up by bundle id
    icon: Optional[bytes] = None  # its home screen icon as a PNG (apps.icon_png), when its bundle has one to read
    executable: Optional[str] = None  # CFBundleExecutable: the process name its log and crash reports carry


@dataclass(frozen=True)
class SimTarget:
    udid: str
    name: str             # "Mobster · iPhone 17 Pro · iOS 26.4"
    device_type: str      # "iPhone 17 Pro"
    runtime: str          # "iOS 26.4"
    wda_url: str          # "http://127.0.0.1:8310"
    mjpeg_url: str        # "http://127.0.0.1:9310"
    xctestrun: str        # the built runner plan (for the restarter)


class SimLease:
    """Holds one simulator's device lease; ``release()`` is idempotent."""
    target: SimTarget

    def __init__(self, target: SimTarget, leases=(), timing: Optional[dict] = None, on_release=None):
        self.target = target
        self._on_release = on_release
        self.timing = dict(timing or {})
        self._leases = list(leases)
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            leases, self._leases = self._leases, []
            on_release, self._on_release = self._on_release, None
        try:
            if on_release is not None:
                on_release()
        finally:
            for lease in leases:
                try:
                    lease.close()
                except OSError:
                    pass

    def __enter__(self) -> SimTarget:
        return self.target

    def __exit__(self, *exc) -> None:
        self.release()
