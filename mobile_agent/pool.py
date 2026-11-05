"""Many phones behind one runtime: one task per phone, first come first served, unhealthy phones withdrawn."""

import itertools
import os
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from .devices import UDID_PATTERN
from .errors import MobsterError
from .journal import JournalError, Lease
from .wda.video import default_mjpeg_url


class NoDeviceAvailable(MobsterError):
    pass


def _local_http_url(value, what):
    parts = urlsplit(value or "")
    if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment):
        raise ValueError(f"{what} must be an http(s) URL without credentials")
    return value.rstrip("/")


@dataclass(frozen=True)
class WdaDeviceSpec:
    id: str
    wda_url: str
    mjpeg_url: str = ""
    udid: str = ""
    label: str = ""
    truth: str = ""

    kind = "wda"

    def __post_init__(self):
        if not isinstance(self.id, str) or not self.id or len(self.id) > 64:
            raise ValueError("A device needs a short id")
        object.__setattr__(self, "wda_url", _local_http_url(self.wda_url, "wda_url"))
        mjpeg = (_local_http_url(self.mjpeg_url, "mjpeg_url") if self.mjpeg_url
                 else default_mjpeg_url(self.wda_url) or "")
        object.__setattr__(self, "mjpeg_url", mjpeg)
        if self.udid and not UDID_PATTERN.fullmatch(self.udid):
            raise ValueError("Invalid device UDID")

    @property
    def identity(self):
        # The string direct runs lease a WDA phone by, so pooled and direct runs exclude each other.
        return self.wda_url

    def public(self):
        # No URL or UDID: the dashboard reads this.
        return {"id": self.id, "label": self.label or self.id, "kind": "wda", "truth": self.truth or None}


@dataclass
class DeviceState:
    spec: object
    healthy: bool = True
    reason: str = ""
    owner: object = None
    lease: object = None
    # -inf: the first allocation probes even when the host uptime is shorter than the health interval.
    checked_at: float = float("-inf")
    used_at: float = 0
    failures: int = 0
    completed: int = 0


def probe_wda(url, timeout=3):
    from .transport import HTTP
    client = HTTP(url)
    try:
        status = client.request("GET", "/status", timeout=timeout)
    finally:
        client.close()
    value = status.get("value") if isinstance(status, dict) else None
    if not isinstance(value, dict) or value.get("ready") is False:
        raise RuntimeError("WebDriverAgent is not ready")
    return value


# Probes for an extension's device kinds.
PROBES = {}


class DevicePool:
    def __init__(self, specs, health_interval=20, probe=None, on_event=None):
        specs = list(specs)
        if not specs:
            raise ValueError("A device pool needs at least one device")
        if len({spec.id for spec in specs}) != len(specs):
            raise ValueError("Device ids must be unique")
        if len({spec.identity for spec in specs}) != len(specs):
            raise ValueError("Two devices cannot share one route")
        udids = [spec.udid for spec in specs if getattr(spec, "udid", "")]
        if len(set(udids)) != len(udids):
            raise ValueError("Two devices cannot share one UDID")
        self.states = {spec.id: DeviceState(spec) for spec in specs}
        self.health_interval = health_interval
        self.probe = probe
        self.on_event = on_event or (lambda event: None)
        self.condition = threading.Condition(threading.RLock())
        self.closed = False
        self._tickets = itertools.count()
        self._waiting = []

    @property
    def size(self):
        return len(self.states)

    def _emit(self, event, **fields):
        try:
            self.on_event({"event": event, **fields})
        except Exception:
            pass

    def _refresh(self, state, force=False):
        if state.owner is not None:
            return state.healthy
        if not force and time.monotonic() - state.checked_at < self.health_interval:
            return state.healthy
        identity = state.spec.identity
        probe = self.probe or (probe_wda if state.spec.kind == "wda" else PROBES[state.spec.kind])
        self.condition.release()
        try:
            probe(identity)
        except Exception as error:
            healthy, reason = False, str(error)[:120]
        else:
            healthy, reason = True, ""
        finally:
            self.condition.acquire()
        if state.owner is not None:
            # Taken while the probe was in flight: a stale result must not rewrite the owner's health.
            return state.healthy
        state.checked_at = time.monotonic()
        if healthy != state.healthy:
            self._emit("device_health", device=state.spec.id, healthy=healthy, reason=reason)
        state.healthy, state.reason = healthy, reason
        return healthy

    def _take(self, owner):
        candidates = [state for state in self.states.values()
                      if state.owner is None and self._refresh(state)]
        # _refresh releases the lock while probing: filter again now that it is held until ownership is assigned.
        if self.closed:
            raise NoDeviceAvailable("The device pool is shutting down")
        if any(state.owner == owner for state in self.states.values()):
            raise ValueError("This owner already holds a device")
        candidates = [state for state in candidates if state.owner is None and state.healthy]
        for state in sorted(candidates, key=lambda item: (item.used_at, item.spec.id)):
            try:
                # Another Mobster process may own this phone.
                state.lease = Lease.device(state.spec.identity)
            except JournalError:
                state.healthy = False
                state.reason = "held by another Mobster process"
                state.checked_at = time.monotonic()
                self._emit("device_health", device=state.spec.id, healthy=False, reason=state.reason)
                continue
            state.owner = owner
            state.used_at = time.monotonic()
            self._emit("device_acquired", device=state.spec.id, owner=str(owner))
            return state.spec
        return None

    def acquire(self, owner, timeout=0, cancelled=None):
        if owner is None:
            raise ValueError("A device lease needs an owner")
        deadline = time.monotonic() + max(0, timeout)
        ticket = next(self._tickets)
        with self.condition:
            if self.closed:
                raise NoDeviceAvailable("The device pool is shutting down")
            if any(state.owner == owner for state in self.states.values()):
                raise ValueError("This owner already holds a device")
            self._waiting.append(ticket)
            try:
                while True:
                    if cancelled and cancelled():
                        raise NoDeviceAvailable("Cancelled while waiting for a device")
                    if self.closed:
                        raise NoDeviceAvailable("The device pool is shutting down")
                    # Strict FIFO.
                    if self._waiting[0] == ticket:
                        spec = self._take(owner)
                        if spec is not None:
                            return spec
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise NoDeviceAvailable(
                            "Every device is busy" if any(s.owner is not None for s in self.states.values())
                            else "No device is currently reachable")
                    self.condition.wait(min(remaining, 1.0))
            finally:
                self._waiting.remove(ticket)
                self.condition.notify_all()

    def release(self, owner, failed=False):
        with self.condition:
            for state in self.states.values():
                if state.owner != owner:
                    continue
                if state.lease is not None:
                    try:
                        state.lease.close()
                    except Exception:
                        pass
                state.lease = None
                state.owner = None
                state.used_at = time.monotonic()
                if failed:
                    state.failures += 1
                    # Re-probe before this device is offered again.
                    state.checked_at = float("-inf")
                else:
                    state.completed += 1
                self._emit("device_released", device=state.spec.id, owner=str(owner), failed=bool(failed))
                self.condition.notify_all()
                return state.spec
            return None

    def position(self):
        with self.condition:
            return {"waiting": len(self._waiting),
                    "busy": sum(1 for state in self.states.values() if state.owner is not None),
                    "healthy": sum(1 for state in self.states.values() if state.healthy),
                    "total": len(self.states)}

    def public(self):
        with self.condition:
            return [{**state.spec.public(), "healthy": state.healthy,
                     "status": "busy" if state.owner is not None else
                               "ready" if state.healthy else "unavailable",
                     "reason": state.reason, "completed": state.completed,
                     "failures": state.failures}
                    for state in sorted(self.states.values(), key=lambda item: item.spec.id)]

    def close(self):
        with self.condition:
            self.closed = True
            for state in self.states.values():
                if state.lease is not None:
                    try:
                        state.lease.close()
                    except Exception:
                        pass
                state.lease = None
            self.condition.notify_all()


def wda_specs_from_env(value=None):
    value = os.environ.get("MOBSTER_WDA_DEVICES", "") if value is None else value
    specs = []
    for index, entry in enumerate(part for part in value.split(";") if part.strip()):
        fields = {}
        for pair in entry.split(","):
            if not pair.strip():
                continue
            key, sep, item = pair.partition("=")
            key = key.strip().lower()
            if not sep or key not in {"id", "udid", "wda", "mjpeg", "label", "truth"} or key in fields:
                raise ValueError(f"MOBSTER_WDA_DEVICES entry {index + 1}: expected unique key=value pairs "
                                 "(id, udid, wda, mjpeg, label, truth)")
            fields[key] = item.strip()
        if not fields.get("wda"):
            raise ValueError(f"MOBSTER_WDA_DEVICES entry {index + 1} needs wda=<url>")
        specs.append(WdaDeviceSpec(id=fields.get("id") or f"phone-{index + 1}", wda_url=fields["wda"],
                                   mjpeg_url=fields.get("mjpeg", ""), udid=fields.get("udid", ""),
                                   label=fields.get("label", ""), truth=fields.get("truth", "")))
    return specs


def wda_specs_from_config(config):
    items = [item for item in getattr(config, "devices", None) or () if isinstance(item, dict) and item.get("wdaUrl")]
    if items:
        return [WdaDeviceSpec(id=item.get("id") or f"phone-{index + 1}", wda_url=item["wdaUrl"],
                              mjpeg_url=item.get("mjpegUrl", ""), udid=item.get("udid", ""),
                              label=item.get("label", ""), truth=item.get("truth", ""))
                for index, item in enumerate(items)]
    return wda_specs_from_env()
