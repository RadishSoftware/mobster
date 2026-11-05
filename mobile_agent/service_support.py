"""What the per-runtime services (connectors, outlets, plugins) share: their registry, ``ms`` and the app-only check."""

import threading

from .api_errors import APIError

APP_ONLY = "Do this in the Mobster app."


class ServiceRegistry:
    """One service per Runtime, keyed by the runtime object."""

    def __init__(self):
        self._items = {}
        self._lock = threading.Lock()

    def get(self, runtime):
        with self._lock:
            return self._items.get(id(runtime))

    def add(self, runtime, service):
        with self._lock:
            self._items[id(runtime)] = service

    def pop(self, runtime):
        with self._lock:
            return self._items.pop(id(runtime), None)

    def all(self):
        with self._lock:
            return list(self._items.values())


def ms(seconds):
    return round(seconds * 1000) if isinstance(seconds, (int, float)) else None


def needs_app(request, message=APP_ONLY):
    """403 app_only when this agent has a Mac app session and the request lacks it."""
    local = getattr(request.runtime, "local_api", None) or {}
    if local.get("app_session") and not request.app_session:
        raise APIError(message, 403, "app_only")
