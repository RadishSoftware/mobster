"""Renew the runner before its signature runs out, so the phone keeps working past day 7.

A free Apple ID signs the runner for 7 days; after that iOS refuses to open it and every
task fails until someone rebuilds it. When Mobster is open within renew_window() (48 hours)
of the expiry, with the chosen iPhone connected and unlocked and no task or build
running, this rebuilds it through the same path as Setup's Rebuild button
(DeviceManager.start_build), once per signature. When that build succeeds and the
runner was running, it restarts the runner (between tasks) so the phone gets the newly
signed copy.

It never builds for a locked or disconnected phone: xcodebuild would stall on it, and
the user is not there to see why. "Unlocked" is what WebDriverAgent reports
(DeviceManager.health), so an already expired runner is left to the Renew button.
"""

import threading
import time

from .device_manager import renew_window

# After a renewal that failed, try again this much later (Xcode or Apple may be down).
RETRY_AFTER = 6 * 3600


class RunnerRenewal:
    def __init__(self, manager, busy=lambda: False, clock=time.time, builder=None, restarter=None):
        """busy: whether a task is running now. builder(team) starts a build and restarter() restarts
        the runner; both default to the manager's own (the fakes in tests replace them)."""
        self.manager, self.busy, self.clock = manager, busy, clock
        self.builder = builder or (lambda team: manager.start_build(team, renewal=True))
        self.restarter = restarter or manager.start_runner
        self.lock = threading.Lock()
        # The signature (its expiry) a renewal was started for, when, and whether it is still building.
        self.renewed_for = None
        self.attempted_at = None
        self.building = False
        self.restart_pending = False
        self.error = None

    # -- when ----------------------------------------------------------------------

    def expires_at(self):
        return self.manager.signature_expiry()

    def reason_to_wait(self, now=None):
        """Why a renewal can't start now (a short id), or None when it can."""
        now = self.clock() if now is None else now
        manager = self.manager
        settings = manager.settings()
        expires = self.expires_at()
        if expires is None:
            return "unknown_expiry"
        if expires - now > renew_window():
            return "not_due"
        if not settings.get("team") or not settings.get("udid"):
            return "not_set_up"
        if settings.get("built_for", settings.get("udid")) != settings.get("udid"):
            return "not_set_up"
        device = manager.device()
        if device is None or not device.get("trusted"):
            return "disconnected"
        health = manager.health
        if health.get("responsive") is None:
            # Nothing answers for the phone, so whether it's unlocked is unknown: the runner is stopped.
            return "runner_stopped"
        if health.get("locked") is not False or health.get("responsive") is not True:
            return "locked"
        if self.busy():
            return "busy"
        if manager.build.get("state") == "running":
            return "building"
        if self.renewed_for == expires and (self.building or self.error is None):
            return "done"
        if self.attempted_at is not None and now - self.attempted_at < RETRY_AFTER:
            return "retry_later"
        return None

    # -- the watch loop's tick -------------------------------------------------------

    def tick(self):
        """Called every few seconds by DeviceManager.watch. Returns True when it started a build."""
        with self.lock:
            self._follow_build()
            if self.reason_to_wait() is not None:
                return False
            expires = self.expires_at()
            team = self.manager.settings()["team"]
            self.renewed_for, self.attempted_at, self.error = expires, self.clock(), None
            self.restart_pending = self.manager.runner is not None and self.manager.runner.running
            try:
                self.builder(team)
            except Exception as error:  # a build refused up front (no phone, Xcode broke) is retried later
                self.error = str(error)[:300]
                return False
            self.building = True
            return True

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
            if self.expires_at() == self.renewed_for:
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
        elif expires is not None and expires - now <= window and reason in {
                "disconnected", "locked", "runner_stopped", "busy", "building", "retry_later"}:
            state = "due"
        else:
            state = "idle"
        return {"state": state, "waiting_for": reason if state == "due" else None, "error": self.error,
                "attempted_at": round(self.attempted_at * 1000) if self.attempted_at else None,
                "window_hours": window // 3600}
