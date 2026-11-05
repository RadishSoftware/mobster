"""Power while tasks run (Runtime.power): the Mac kept awake, its battery watched, a phone's battery watched."""

import os
import threading
import time

from . import device_status, engines, phone_ready
from .harness import control

INTERVAL = 30.0
MAC_WARN_DEFAULT = 20
PHONE_PAUSE_DEFAULT = 10
PAUSE_RANGE = (1, 50)
# A battery pause longer than this ends the task (status "stopped"): nobody came to plug the phone in.
BATTERY_PAUSE_LIMIT = 30 * 60
PHONE_BATTERY = "phone_battery"


def _percent(name, default, low=1, high=99):
    """MOBSTER_* percent: the default when unset or unreadable, None when ``off`` (or 0)."""
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("off", "0", "false", "no"):
        return None
    try:
        value = int(float(raw))
    except (ValueError, OverflowError):
        return default
    return value if low <= value <= high else default


def mac_warn_threshold():
    return _percent("MOBSTER_MAC_BATTERY_WARN", MAC_WARN_DEFAULT)


def phone_pause_threshold():
    return _percent("MOBSTER_PHONE_BATTERY_PAUSE", PHONE_PAUSE_DEFAULT, *PAUSE_RANGE)


class AwakeHolds:
    """One keep-awake (workflow_scheduler.KeepAwake) shared by owners, held while any owner holds it."""

    def __init__(self, keep_awake=None):
        self.keep_awake = keep_awake
        self.owners = set()
        self.lock = threading.Lock()

    def attach(self, keep_awake):
        with self.lock:
            self.keep_awake = keep_awake
            if self.owners and keep_awake is not None:
                keep_awake.hold()

    def hold(self, owner):
        with self.lock:
            self.owners.add(owner)
            if self.keep_awake is not None:
                self.keep_awake.hold()

    def release(self, owner):
        with self.lock:
            self.owners.discard(owner)
            if not self.owners and self.keep_awake is not None:
                self.keep_awake.release()

    def close(self):
        with self.lock:
            self.owners.clear()
            if self.keep_awake is not None:
                self.keep_awake.release()

    @property
    def held(self):
        return bool(self.keep_awake is not None and self.keep_awake.held)

    def handle(self, owner):
        """The scheduler's keep-awake: hold() and release() as ``owner``."""
        return _Handle(self, owner)

    def status(self):
        with self.lock:
            owners = sorted("schedule" if owner == "schedule" else "task" for owner in self.owners)
        enabled = bool(self.keep_awake is not None and getattr(self.keep_awake, "enabled", False))
        return {"keptAwake": self.held, "keepAwake": enabled, "for": sorted(set(owners))}


class _Handle:
    def __init__(self, holds, owner):
        self.holds, self.owner = holds, owner

    @property
    def held(self):
        return self.owner in self.holds.owners and self.holds.held

    def hold(self):
        self.holds.hold(self.owner)

    def release(self):
        self.holds.release(self.owner)


class PowerWatch:
    def __init__(self, runtime, *, read_mac=None, read_phone=None, clock=time.monotonic, interval=INTERVAL):
        self.runtime = runtime
        self.awake = AwakeHolds()
        self.read_mac = read_mac or device_status.read_mac
        self.read_phone = read_phone or phone_reader
        self.clock, self.interval = clock, interval
        self.lock = threading.Lock()
        self._mac = (None, None)            # (read at, value)
        self._phones = {}                   # phone key -> (read at, value)
        self.mac_low = None                 # the percent while the Mac's battery warning stands, else None
        self.warned = set()                 # runs told the Mac's battery is low
        self.paused = {}                    # run id -> the level it paused at (a battery pause this watch made)
        self.kept_going = set()             # runs a person continued from a battery pause
        self.stop = threading.Event()
        self.thread = None

    # -- runs ------------------------------------------------------------------------------------------------------

    def run_started(self, run):
        self.awake.hold(run.id)
        self._ensure_thread()
        if getattr(run, "origin", None) == "schedule" and phone_ready.enabled():
            threading.Thread(target=self._low_power, args=(run,), name="mobster-low-power", daemon=True).start()

    def _low_power(self, run):
        """A scheduled task's one ``phone_low_power`` warning, as it starts: Low Power Mode makes its phone lock after
        30 seconds. A read that fails says nothing (phone_ready.py); it never touches the task."""
        try:
            phone = self.runtime.phone_for(run)
            warning = phone_ready.low_power_warning(phone.wda_url, phone.name or phone.id, "the next scheduled task")
            if warning:
                run.emit({"event": "phone_low_power", "message": warning})
        except Exception:  # noqa: BLE001 -- a warning never stops a task
            pass

    def run_finished(self, run):
        self.awake.release(run.id)
        with self.lock:
            self.warned.discard(run.id)
            self.paused.pop(run.id, None)
            self.kept_going.discard(run.id)

    def close(self):
        self.stop.set()
        self.awake.close()

    def _ensure_thread(self):
        with self.lock:
            if self.thread is not None or self.stop.is_set():
                return
            thread = threading.Thread(target=self._work, name="mobster-power", daemon=True)
            thread.start()
            self.thread = thread

    def _work(self):
        while not self.stop.wait(self.interval):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 -- the watch never stops a task
                pass

    # -- readings --------------------------------------------------------------------------------------------------

    def mac(self, fresh=False):
        at, value = self._mac
        if fresh or at is None or self.clock() - at > device_status.MAC_TTL:
            try:
                value = self.read_mac()
            except Exception:  # noqa: BLE001
                value = {"power": None}
            self._mac = (self.clock(), value)
        return dict(value)

    def phone(self, phone, fresh=False):
        key = getattr(phone, "key", None)
        at, value = self._phones.get(key, (None, None))
        if fresh or at is None or self.clock() - at > device_status.PHONE_TTL:
            try:
                value = self.read_phone(phone)
            except Exception:  # noqa: BLE001
                value = {}
            self._phones[key] = (self.clock(), value)
        return dict(value)

    def warnings(self):
        if self.mac_low is None or not self.runtime.active_runs:
            return []
        return [{"code": "mac_battery_low", "percent": self.mac_low,
                 "message": f"This Mac is on battery at {self.mac_low}%. Plug it in: a Mac that runs out of battery "
                            "sleeps and stops its tasks."}]

    # -- one look --------------------------------------------------------------------------------------------------

    def tick(self):
        runs = self._running()
        if not runs:
            return
        self._mac_battery(runs)
        threshold = phone_pause_threshold()
        for run in runs:
            try:
                self._phone_battery(run, threshold)
            except Exception:  # noqa: BLE001 -- one phone's reading never stops the others'
                pass

    def _running(self):
        runtime = self.runtime
        with runtime.lock:
            ids = list(runtime.active_runs)
            return [run for run in (runtime.runs.get(i) for i in ids) if run is not None and run.finished_at is None]

    def _mac_battery(self, runs):
        threshold = mac_warn_threshold()
        mac = self.mac(fresh=True)
        level = mac.get("battery")
        low = (threshold is not None and mac.get("power") == "battery" and type(level) is int and level <= threshold)
        if not low:
            self.mac_low = None
            self.warned.clear()
            return
        first = self.mac_low is None
        self.mac_low = level
        event = {"event": "mac_battery_low", "percent": level, "threshold": threshold}
        for run in runs:
            if run.id not in self.warned:
                self.warned.add(run.id)
                run.emit(dict(event))
        if first:
            self.runtime.publish("power", {**event, "runs": len(runs)})

    def _phone_battery(self, run, threshold):
        queue = run.steering
        if run.id in self.paused:
            why = control.reason(queue)
            if not control.paused(queue) or not why or why.get("code") != PHONE_BATTERY:
                # A person continued it (or paused it themselves): theirs from now on, and its battery doesn't pause
                # this task again.
                self.paused.pop(run.id, None)
                self.kept_going.add(run.id)
                return
            battery = self.phone(self.runtime.phone_for(run), fresh=True).get("battery") or {}
            if threshold is None or battery.get("state") in device_status.CHARGING:
                self.paused.pop(run.id, None)
                control.set_paused(queue, False)
            return
        if threshold is None or run.id in self.kept_going or not eligible(run) or control.paused(queue):
            return  # off, kept going by a person, not a long or scheduled task, or a person's own pause stands
        battery = self.phone(self.runtime.phone_for(run), fresh=True).get("battery") or {}
        level, state = battery.get("level"), battery.get("state")
        if type(level) is int and level <= threshold and state in device_status.DRAINING:
            self.paused[run.id] = level
            control.set_paused(queue, True, reason={"code": PHONE_BATTERY, "level": level,
                                                     "limit": BATTERY_PAUSE_LIMIT})


def eligible(run):
    """A long task or a scheduled task on Mobster's agent, live (the only ones the battery may pause)."""
    return (getattr(run, "engine", None) == engines.SMART and getattr(run, "mode", None) == "live"
            and (getattr(run, "longform", None) is not None or getattr(run, "origin", None) == "schedule"))


def phone_reader(phone):
    """device_status.read_phone for a fleet.Phone (the primary's UDID is its device manager's)."""
    kind = getattr(phone, "kind", None)
    udid = getattr(phone, "udid", None)
    manager = getattr(phone, "manager", None)
    if kind == "usb" and not udid and manager is not None:
        try:
            udid = getattr(manager, "pinned", None) or manager.settings().get("udid")
        except Exception:  # noqa: BLE001
            udid = None
    return device_status.read_phone(kind, udid=udid, wda_url=getattr(phone, "wda_url", None))
