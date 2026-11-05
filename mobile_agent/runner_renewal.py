"""Renews the runner before its signature runs out, and rebuilds it after an update changes its patches."""

import threading
import time

from .signing import renew_window

# After a renewal that failed, try again this much later (Xcode or Apple may be down).
RETRY_AFTER = 6 * 3600
# With less than this left a failed renewal is tried again sooner: a quarter of the time left, at least this often.
RETRY_FLOOR = 15 * 60
# While the runner is stopped, CoreDevice's lock reading is reused for this long: the watch loop ticks every 5 s.
LOCK_READ_EVERY = 30


def coredevice_locked(udid):
    """The phone's lock as CoreDevice reports it: True, False, or None when it can't say."""
    from .wireless import devicectl
    return devicectl.read_lock_state(udid) if udid else None


class RunnerRenewal:
    def __init__(self, manager, busy=lambda: False, clock=time.time, builder=None, restarter=None,
                 lock_reader=coredevice_locked):
        self.manager, self.busy, self.clock = manager, busy, clock
        self.builder = builder or (lambda team: manager.start_build(team, renewal=True))
        self.restarter = restarter or manager.start_runner
        self.lock_reader = lock_reader
        self.lock_read = (None, None)   # (when, reading) of the newest CoreDevice lock read
        self.lock = threading.Lock()
        # What a renewal was started for (the signature's expiry, or ("patches", digest) for an out-of-date runner),
        # when, and whether it is still building.
        self.renewed_for = None
        self.attempted_at = None
        self.building = False
        self.restart_pending = False
        self.error = None

    # -- when ----------------------------------------------------------------------

    def expires_at(self):
        return self.manager.signature_expiry()

    def out_of_date(self):
        """A runner built with other WDA patches than this Mobster has: an update changed them."""
        check = getattr(self.manager, "out_of_date", None)
        try:
            return bool(check()) if callable(check) else False
        except Exception:  # noqa: BLE001
            return False

    def target(self):
        """What a renewal now would be for: ("patches", digest) for an out-of-date runner,
        else the signature's expiry."""
        if self.out_of_date():
            from .wda import patches as wda_patches
            return ("patches", wda_patches.patch_digest())
        return self.expires_at()

    def _unlocked_by_coredevice(self, udid, now):
        """CoreDevice's lock reading while the runner can't give one, reused for LOCK_READ_EVERY seconds."""
        at, reading = self.lock_read
        if at is None or now - at >= LOCK_READ_EVERY or now < at:
            try:
                reading = self.lock_reader(udid)
            except Exception:  # noqa: BLE001
                reading = None
            reading = reading if isinstance(reading, bool) else None
            self.lock_read = (now, reading)
        return None if reading is None else not reading

    def reason_to_wait(self, now=None):
        """Why a renewal can't start now (a short id), or None when it can."""
        now = self.clock() if now is None else now
        manager = self.manager
        settings = manager.settings()
        expires = self.expires_at()
        stale = self.out_of_date()
        if not stale:
            if expires is None:
                return "unknown_expiry"
            if expires - now > renew_window():
                return "not_due"
        if not settings.get("team") or not settings.get("udid"):
            return "not_set_up"
        if settings.get("built_for", settings.get("udid")) != settings.get("udid"):
            return "not_set_up"
        # never for a locked or disconnected phone: xcodebuild stalls on it, with nobody there to see why
        device = manager.device()
        if device is None or not device.get("trusted"):
            return "disconnected"
        health = manager.health
        if health.get("responsive") is None:
            # Nothing answers for the phone (the runner is stopped, expired or out of date): CoreDevice says.
            unlocked = self._unlocked_by_coredevice(device.get("udid") or settings.get("udid"), now)
            if unlocked is None:
                return "runner_stopped"
            if not unlocked:
                return "locked"
        elif health.get("locked") is not False or health.get("responsive") is not True:
            return "locked"
        presence = getattr(manager, "presence", None)
        if presence is not None and presence.is_owner_active():
            return "owner_active"  # the owner is on the phone: a build and install would reach into their hands
        if self.busy():
            return "busy"
        if manager.build.get("state") == "running":
            return "building"
        if self.renewed_for == self.target() and (self.building or self.error is None):
            return "done"
        if self.attempted_at is not None:
            # An out-of-date runner has no deadline of its own: a failed rebuild is tried again RETRY_AFTER later.
            deadline = expires if expires is not None and not stale else self.attempted_at + 4 * RETRY_AFTER
            if now - self.attempted_at < self.retry_after(deadline, self.attempted_at):
                return "retry_later"
        return None

    @staticmethod
    def retry_after(expires, now):
        left = max(0.0, expires - now)
        return min(RETRY_AFTER, max(RETRY_FLOOR, left / 4))

    # -- the watch loop's tick -------------------------------------------------------

    def tick(self):
        """Called every few seconds by DeviceManager.watch. Returns True when it started a build."""
        with self.lock:
            self._follow_build()
            if self.signed_here():
                return False
            if self.reason_to_wait() is not None:
                return False
            team = self.manager.settings()["team"]
            self.renewed_for, self.attempted_at, self.error = self.target(), self.clock(), None
            self.restart_pending = self.manager.runner is not None and self.manager.runner.running
            try:
                self.builder(team)
            except Exception as error:  # a build refused up front (no phone, Xcode broke) is retried later
                self.error = str(error)[:300]
                return False
            self.building = True
            return True

    def signed_here(self):
        """A helper signed on this Mac with the team's profile (team_signing) isn't renewed by a build: its profile
        lasts a year, and Setup shows the card to sign it again when it ends. A build here would need Xcode."""
        driver = (self.manager.settings() or {}).get("driver") if hasattr(self.manager, "settings") else None
        return isinstance(driver, str) and driver != "xcodebuild"

    def _follow_build(self):
        """After our build: remember a failure, and restart a running runner between tasks."""
        if not self.building:
            if self.restart_pending and not self.busy():
                self.restart_pending = False
                try:
                    self.restarter()
                except Exception as error:
                    self.error = str(error)[:300]
            return
        state = self.manager.build.get("state")
        if state == "running":
            return
        self.building = False
        if state == "failed":
            self.error = self.manager.build.get("error") or "The renewal build failed."
            self.restart_pending = False
        elif state == "succeeded":
            if not isinstance(self.renewed_for, tuple) and self.expires_at() == self.renewed_for:
                # Xcode signed with the same profile again: say so rather than claim a renewal.
                self.error = "Xcode reused the old signature. Refresh Mobster's helper again from Settings."
            if self.restart_pending and not self.busy():
                self.restart_pending = False
                try:
                    self.restarter()
                except Exception as error:
                    self.error = str(error)[:300]

    # -- what Setup shows --------------------------------------------------------------

    def status(self):
        """{"state", "error", "attempted_at"}: idle, due (waiting for the phone), renewing or failed."""
        now = self.clock()
        window = renew_window()
        expires = self.expires_at()
        reason = self.reason_to_wait(now)
        if self.building:
            state = "renewing"
        elif self.error:
            state = "failed"
        elif (self.out_of_date() or expires is not None and expires - now <= window) and reason in {
                "disconnected", "locked", "runner_stopped", "owner_active", "busy", "building",
                                                  "retry_later"}:
            state = "due"
        else:
            state = "idle"
        return {"state": state, "waiting_for": reason if state == "due" else None, "error": self.error,
                "attempted_at": round(self.attempted_at * 1000) if self.attempted_at else None,
                "window_hours": window // 3600,
                # When the signature runs out (epoch ms) and how long is left: the fleet view and the Mac app's
                # notification say "renewal due" with this deadline while the phone is away, locked or in use.
                "deadline": round(expires * 1000) if expires is not None else None,
                "hours_left": round((expires - now) / 3600, 1) if expires is not None else None,
                "overdue": bool(expires is not None and expires <= now)}

    public = status
