"""Whether the person who owns an iPhone is using it, so Mobster never grabs it from them."""

import threading
import time

POLL_INTERVAL_S = 15.0     # between read-only polls of one phone
READ_TIMEOUT_S = 3.0
ACTION_QUIET_S = 30.0      # a change this soon after a Mobster action is Mobster's own
OWNER_QUIET_S = 120.0      # owner_active ends after this long with no change
IDLE_AFTER_S = 300.0       # a phone first seen, or unchanged, is idle after this long
IDLE_RELEASE_S = 120.0     # idle for this long: stop live view, prewarm and the awake hold

OWNER_ACTIVE_REFUSAL = ("You're using your iPhone right now, so Mobster didn't start this task. "
                        "No action was taken. It can start once you put the phone down.")


class OwnerPresence:
    """One phone's presence. ``observe`` is the only input besides ``note_mobster_action``/``run_started``."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self.lock = threading.Lock()
        self.state, self.since = "unknown", self._ms(clock())
        self.baseline = None          # (locked, front) at the last read
        self.first_seen = None
        self.last_change = None
        self.last_action = None
        self.holding = False
        self.idle_since = None
        self.released = False         # idle release already done for this idle stretch

    @staticmethod
    def _ms(seconds):
        return int(seconds * 1000)

    def _set(self, state, now):
        if state != self.state:
            self.state, self.since = state, self._ms(now)
            self.released = False
            self.idle_since = now if state == "idle" else None

    # -- inputs -----------------------------------------------------------------------------------

    def note_mobster_action(self, now=None):
        """Mobster acted on the phone (a run's step, an MCP tap, manual control): a change soon after is its own."""
        with self.lock:
            self.last_action = self.clock() if now is None else now

    def run_started(self, now=None):
        with self.lock:
            now = self.clock() if now is None else now
            self.holding, self.last_action = True, now
            self._set("mobster", now)

    def run_finished(self, locked=None, front=None, now=None):
        """The run ended. What the phone shows now is Mobster's doing, not the owner's: it becomes the baseline."""
        with self.lock:
            now = self.clock() if now is None else now
            self.holding, self.last_action = False, now
            if locked is not None or front is not None:
                self.baseline = (locked, front)
            self.last_change = now
            self._set("idle", now)

    def observe(self, *, held=False, ok=True, locked=None, front=None, now=None):
        with self.lock:
            now = self.clock() if now is None else now
            if held:
                self.holding, self.last_action = True, now
                self._set("mobster", now)
                return self.state
            if self.holding:
                # A hold just ended without run_finished (another process's lease): treat it as a finished run.
                self.holding, self.last_action = False, now
                if ok:
                    self.baseline, self.last_change = (locked, front), now
                self._set("idle", now)
                return self.state
            if not ok or not isinstance(locked, bool):
                self._set("unknown", now)
                return self.state
            reading = (locked, front)
            if self.first_seen is None:
                self.first_seen = now
            if self.baseline is None:
                self.baseline, self.last_change = reading, now
            elif reading != self.baseline:
                self.baseline = reading
                # Only a person unlocks a phone or switches its app. The phone locking itself (auto-lock) is not use.
                if locked is False:
                    self.last_change = now
                    own = self.last_action is not None and now - self.last_action <= ACTION_QUIET_S
                    if not own:
                        self._set("owner_active", now)
                        return self.state
            quiet = now - (self.last_change if self.last_change is not None else self.first_seen)
            if self.state == "owner_active":
                if quiet >= OWNER_QUIET_S:
                    self._set("idle", now)
            elif quiet >= IDLE_AFTER_S or self.state == "idle":
                self._set("idle", now)
            else:
                self._set("unknown", now)
            return self.state

    # -- outputs ----------------------------------------------------------------------------------

    def is_owner_active(self):
        with self.lock:
            return self.state == "owner_active"

    def should_release(self, now=None):
        with self.lock:
            now = self.clock() if now is None else now
            if self.state != "idle" or self.released or self.idle_since is None:
                return False
            if now - self.idle_since < IDLE_RELEASE_S:
                return False
            self.released = True
            return True

    def snapshot(self):
        with self.lock:
            return {"state": self.state, "since": self.since}


# -- the reads ---------------------------------------------------------------------------------------


def read_phone(wda_url, timeout=READ_TIMEOUT_S):
    """(ok, locked, front): the two read-only sessionless reads. Never raises."""
    from .transport import read_kept  # the helper's kept connection: a connection a poll ran WDA out of descriptors
    try:
        locked = read_kept(wda_url, "/wda/locked", timeout=timeout).get("value")
        front = None
        if locked is False:
            value = read_kept(wda_url, "/wda/activeAppInfo", timeout=timeout).get("value")
            bundle = value.get("bundleId") if isinstance(value, dict) else None
            front = bundle if isinstance(bundle, str) and bundle else None
        return isinstance(locked, bool), locked, front
    except Exception:  # noqa: BLE001 -- an unanswered phone is "unknown", never an error
        return False, None, None


def lease_held(wda_url):
    """Whether a Mobster process holds this phone's lease now (taken and let go at once). Fails to True."""
    from .journal import Lease
    try:
        lease = Lease.device(wda_url.rstrip("/"), wait=0)
    except Exception:  # noqa: BLE001 -- can't tell: treat it as held, never as free
        return True
    lease.close()
    return False


class Poller:
    """Polls one phone's presence lazily: at most every POLL_INTERVAL_S, and never reads while a run holds it."""

    def __init__(self, presence, wda_url, busy=lambda: False, reader=read_phone, lease=lease_held,
                 monotonic=time.monotonic):
        self.presence, self.wda_url, self.busy = presence, wda_url, busy
        self.reader, self.lease, self.monotonic = reader, lease, monotonic
        self.polled_at = None

    def poll(self, force=False):
        now = self.monotonic()
        if not force and self.polled_at is not None and now - self.polled_at < POLL_INTERVAL_S:
            return self.presence.snapshot()
        self.polled_at = now
        try:
            held = bool(self.busy()) or self.lease(self.wda_url)
        except Exception:  # noqa: BLE001
            held = True
        from .wda.watchdog import tracker
        # an MCP tap or a manual-control gesture is Mobster's, not the owner's
        mine = tracker.last_action_at(self.wda_url)
        if mine is not None:
            self.presence.note_mobster_action(mine)
        if held:
            self.presence.observe(held=True)
        else:
            ok, locked, front = self.reader(self.wda_url)
            self.presence.observe(ok=ok, locked=locked, front=front)
        return self.presence.snapshot()


# -- a registry, so a guard or an MCP tool finds a phone's presence from its WDA address --------------

_registry = {}
_registry_lock = threading.Lock()


def register(wda_url, presence):
    with _registry_lock:
        _registry[(wda_url or "").rstrip("/")] = presence


def presence_for(wda_url):
    with _registry_lock:
        return _registry.get((wda_url or "").rstrip("/"))


def is_owner_active(wda_url):
    """Whether the owner is using the phone at ``wda_url``. False when nothing tracks it."""
    presence = presence_for(wda_url)
    return bool(presence is not None and presence.is_owner_active())


def forget(wda_url):
    with _registry_lock:
        _registry.pop((wda_url or "").rstrip("/"), None)
