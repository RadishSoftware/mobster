"""Many phones behind one runtime.

The MVP bound one process to one phone, so a second task waited for the first to
finish even when another device sat idle. A mobile automation service is limited
by devices, not by CPU, so the unit of capacity is the phone and the scheduler's
only real job is to hand out phones fairly and never hand the same one to two
tasks at once.

Three properties this has to keep:

* **Exclusivity.** A phone is held by one task at a time, and the cross-process
  ``Lease`` still guards against a second Mobster runtime on the same device.
  In-process fairness alone would not survive someone starting a second server.
* **Fairness.** Waiters are served first-come-first-served. Without a queue, a
  burst of submissions starves whoever asked first, which looks like a hang.
* **Honesty about health.** A phone that stopped answering is withdrawn from
  allocation rather than handed out to fail a task. Health is re-checked, so a
  recovered device returns on its own.
"""

from collections import deque
from dataclasses import dataclass
import itertools
import os
import re
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from .errors import MobsterError
from .journal import JournalError, Lease


class NoDeviceAvailable(MobsterError):
    """Every device is busy or unhealthy; the caller decides whether to wait."""


# A USB phone's UDID as idevice_id prints it (same shape device_manager accepts).
UDID_PATTERN = re.compile(r"[0-9A-Fa-f-]{24,40}")
WDA_BASE_PORT, MJPEG_BASE_PORT = 8100, 9100


def _local_http_url(value, what):
    parts = urlsplit(value or "")
    if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment):
        raise ValueError(f"{what} must be an http(s) URL without credentials")
    return value.rstrip("/")


def default_mjpeg_url(wda_url):
    """The MJPEG URL paired with a WDA port: 8100 -> 9100, 8101 -> 9101 (one iproxy pair per phone), a
    simulator's 8203 -> 9203. None when the port has no pair (below 8100, or a pair past 65535): 9100 there
    would be another device's stream, and settling on it would watch the wrong screen."""
    parts = urlsplit(wda_url or "")
    try:
        port = parts.port  # None without one: http's 80 or https's 443, which have no pair either
    except ValueError:
        return None
    video = MJPEG_BASE_PORT + (port - WDA_BASE_PORT) if port is not None and port >= WDA_BASE_PORT else None
    if not parts.hostname or video is None or video > 65535:
        return None
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname  # an IPv6 address keeps its brackets
    return urlunsplit((parts.scheme, f"{host}:{video}", "", "", ""))


@dataclass(frozen=True)
class WdaDeviceSpec:
    """A USB iPhone served by its own WebDriverAgent runner and iproxy port pair.

    Its route is ``wda_url``. ``truth`` names the ground-truth entry
    (``evals.tasks.DEVICES``) that graders must use for runs on this phone.
    """
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
                 else default_mjpeg_url(self.wda_url) or "")  # "": no stream paired, settle by time
        object.__setattr__(self, "mjpeg_url", mjpeg)
        if self.udid and not UDID_PATTERN.fullmatch(self.udid):
            raise ValueError("Invalid device UDID")

    @property
    def identity(self):
        # The same string server.py and evals/harness.py lease a WDA phone by
        # (``Lease.device(wda_url.rstrip("/"))``), so pooled and direct runs exclude each other.
        return self.wda_url

    def public(self):
        # No URL or UDID: this list is served to the dashboard.
        return {"id": self.id, "label": self.label or self.id, "kind": "wda", "truth": self.truth or None}


@dataclass
class DeviceState:
    spec: object
    healthy: bool = True
    reason: str = ""
    owner: object = None
    lease: object = None
    # Never checked: the first allocation must probe even when the host uptime
    # is shorter than the health interval (checked_at=0 used to skip it).
    checked_at: float = float("-inf")
    used_at: float = 0
    failures: int = 0
    completed: int = 0


def probe_wda(url, timeout=3):
    """Reachability only: WDA's /status answered with a ready runner. Never creates a session."""
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


# Health probes for device kinds other than "wda" (``spec.kind``), added by an extension.
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
        """Health is checked lazily, on the allocation path, under no lock."""
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
            # Taken while the probe was in flight: another task owns this phone,
            # so a stale probe result must not rewrite its health accounting.
            return state.healthy
        state.checked_at = time.monotonic()
        if healthy != state.healthy:
            self._emit("device_health", device=state.spec.id, healthy=healthy, reason=reason)
        state.healthy, state.reason = healthy, reason
        return healthy

    def _take(self, owner, device_id=None):
        """Least-recently-used healthy free device, so load spreads across the pool."""
        candidates = [state for state in self.states.values()
                      if state.owner is None and (device_id is None or state.spec.id == device_id)
                      and self._refresh(state)]
        # Every _refresh releases the lock while probing, so a candidate taken by
        # another waiter after its own probe must be filtered again here, where the
        # lock is held continuously until ownership below is assigned.
        candidates = [state for state in candidates if state.owner is None]
        if not candidates:
            return None
        state = min(candidates, key=lambda item: (item.used_at, item.spec.id))
        try:
            # The in-process pool is not the only possible owner of this phone.
            state.lease = Lease.device(state.spec.identity)
        except JournalError:
            state.healthy = False
            state.reason = "held by another Mobster process"
            state.checked_at = time.monotonic()
            self._emit("device_health", device=state.spec.id, healthy=False, reason=state.reason)
            return None
        state.owner = owner
        state.used_at = time.monotonic()
        self._emit("device_acquired", device=state.spec.id, owner=str(owner))
        return state.spec

    def acquire(self, owner, timeout=0, cancelled=None):
        """Wait in line for a phone. Returns a DeviceSpec or raises NoDeviceAvailable."""
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
                    # Strict FIFO: only the head of the queue may take a device.
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

    def acquire_device(self, owner, device_id, timeout=0, cancelled=None):
        """Wait for one particular phone (a per-device worker, per-device ground truth).

        Not queued behind general ``acquire`` waiters: a waiter for another
        phone must never hold this one up. General waiters can still take this
        phone first when it frees; whoever holds the lock first wins. A phone
        that is free but fails its health probe raises at once.
        """
        if owner is None:
            raise ValueError("A device lease needs an owner")
        if device_id not in self.states:
            raise ValueError("Unknown device")
        deadline = time.monotonic() + max(0, timeout)
        with self.condition:
            if any(state.owner == owner for state in self.states.values()):
                raise ValueError("This owner already holds a device")
            while True:
                if cancelled and cancelled():
                    raise NoDeviceAvailable("Cancelled while waiting for a device")
                if self.closed:
                    raise NoDeviceAvailable("The device pool is shutting down")
                spec = self._take(owner, device_id)
                if spec is not None:
                    return spec
                state = self.states[device_id]
                if state.owner is None and not state.healthy:
                    # Waiting out a dead phone would stall its worker for the whole timeout.
                    raise NoDeviceAvailable(f"That device is not reachable: {state.reason}"[:160])
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise NoDeviceAvailable("That device is busy")
                self.condition.wait(min(remaining, 1.0))

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

    def position(self, owner=None):
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
    """USB phones from ``MOBSTER_WDA_DEVICES``; [] when unset.

    One phone per ``;``-separated entry of ``key=value`` pairs::

        id=15pro,udid=00008130-...,wda=http://127.0.0.1:8100,truth=iphone15pro;
        id=xr,udid=00008020-...,wda=http://127.0.0.1:8101,mjpeg=http://127.0.0.1:9101

    ``mjpeg`` defaults to the WDA port + 1000 (the iproxy pair convention).
    """
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
    """USB phones from ``config.devices`` items carrying ``wdaUrl``, else from the environment.

    Deliberately separate from ``specs_from_config``: until ``Runtime.work``
    binds each run to ``run.device.wda_url`` (see pool design notes), a WDA
    pool would hold one phone while the run drove ``config.wda_url``.
    """
    items = [item for item in getattr(config, "devices", None) or () if isinstance(item, dict) and item.get("wdaUrl")]
    if items:
        return [WdaDeviceSpec(id=item.get("id") or f"phone-{index + 1}", wda_url=item["wdaUrl"],
                              mjpeg_url=item.get("mjpegUrl", ""), udid=item.get("udid", ""),
                              label=item.get("label", ""), truth=item.get("truth", ""))
                for index, item in enumerate(items)]
    return wda_specs_from_env()


def run_on_devices(pool, jobs, work, *, eligible=None, timeout=3600, cancelled=None, on_result=None):
    """Run independent jobs concurrently, one worker per phone, until the queue drains.

    ``work(spec, job)`` runs with ``spec``'s phone exclusively held (pool
    reservation plus the cross-process lease) and is re-acquired per job, so
    another Mobster server can interleave between jobs as it can with the
    single-phone harness. ``eligible(spec, job)`` restricts a job to phones
    it can run on (for example, phones whose ground truth is recorded).
    A job no phone is eligible for is returned as skipped, never run.
    Returns ``[(job, spec_or_None, result_or_exception)]`` in completion order.
    """
    eligible = eligible or (lambda spec, job: True)
    specs = [state.spec for state in pool.states.values()]
    queue, lock, results = deque(), threading.RLock(), []
    for job in jobs:
        if any(eligible(spec, job) for spec in specs):
            queue.append(job)
        else:
            results.append((job, None, NoDeviceAvailable("No device is eligible for this job")))

    def record(entry):
        with lock:
            results.append(entry)
        if on_result is not None:
            try:
                on_result(*entry)
            except Exception:
                pass

    busy = [0]
    alive = {spec.id for spec in specs}
    changed = threading.Condition(lock)

    def worker(spec):
        owner = f"run-on-devices:{spec.id}:{id(queue)}"
        while not (cancelled and cancelled()):
            with changed:
                while True:
                    job = next((item for item in queue if eligible(spec, item)), None)
                    # Idle only while another phone's job might still be handed back.
                    if job is not None or busy[0] == 0:
                        break
                    changed.wait(.5)
                if job is None:
                    alive.discard(spec.id)
                    changed.notify_all()
                    return
                queue.remove(job)
                busy[0] += 1
            try:
                try:
                    held = pool.acquire_device(owner, spec.id, timeout=timeout, cancelled=cancelled)
                except NoDeviceAvailable as error:
                    # This phone is gone: hand the job back to another eligible
                    # live phone, and fail what only this phone could run.
                    with changed:
                        alive.discard(spec.id)
                        queue.appendleft(job)
                        stranded = [item for item in queue if not any(
                            eligible(other, item) for other in specs if other.id in alive)]
                        for item in stranded:
                            queue.remove(item)
                        changed.notify_all()
                    for item in stranded:
                        record((item, spec, error))
                    return
                failed = False
                try:
                    outcome = work(held, job)
                except Exception as error:
                    failed, outcome = True, error
                finally:
                    pool.release(owner, failed=failed)
                record((job, held, outcome))
            finally:
                with changed:
                    busy[0] -= 1
                    changed.notify_all()

    threads = [threading.Thread(target=worker, args=(spec,), name=f"mobster-device-{spec.id}", daemon=True)
               for spec in specs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    with lock:
        leftover = list(queue)
        queue.clear()
    for job in leftover:
        record((job, None, NoDeviceAvailable("Stopped before this job ran")))
    return results
