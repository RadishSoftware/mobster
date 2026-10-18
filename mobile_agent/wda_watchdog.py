"""A watchdog for a wedged WebDriverAgent runner or relay, and the record of actions in flight.

``DeviceManager.probe`` feeds it one result per probe (``GET /wda/locked``, bounded at 3 s). After
FAILURES_TO_WEDGE failures in a row the phone is ``wedged``; once it has stayed wedged for WEDGED_RESTART_SECONDS the
watchdog restarts the runner, subject to the rules below. They are the safety case, and tests/test_wda_watchdog.py
has one test for each:

1. **An action in flight is never re-sent, and never restarted under.** ``ActionTracker`` records every WDA action
   (tap, swipe, key, button, element value, app activate) from just before it is sent until it answers or fails.
   A restart waits while one is in flight (``admit_restart`` checks that, the unknown outcomes and the task, and blocks, in one step). When one ends without an answer its outcome is *unknown*: it is
   recorded, the transport never replays it (transport.HTTP), the effect ledger keeps blocking the same effect
   (effect_ledger.py), and the run ends or goes on from a fresh look. The watchdog sends nothing.
2. **While a restart runs, no new action leaves.** ``admit_restart``, ``run_started`` and ``begin`` share one lock: an action that starts
   after the restart began is refused before it is sent ("nothing sent"), a known outcome.
3. **A restart with a task running happens only between actions** (nothing in flight, no unknown outcome waiting to
   be settled), never on a dead runner: a runner that is not running ends the run with a plain sentence instead
   (``on_dead_runner``), because there is nothing to restart into a task safely.
4. **Backoff, no storm.** The gap between restarts doubles for each restart in a row (MIN_RESTART_GAP, 2x, 4x...
   up to MAX_RESTART_GAP), and resets after HEALTHY_SECONDS of passing probes.
"""

import threading
import time

from .transport import NotSent
from .wda_timeouts import is_action

FAILURES_TO_WEDGE = 3
WEDGED_RESTART_SECONDS = 90.0
MIN_RESTART_GAP = 180.0
MAX_RESTART_GAP = 1800.0
HEALTHY_SECONDS = 120.0

DEAD_RUNNER = ("Mobster's helper on your iPhone stopped, so this task ended. No further action was taken. "
               "Check that your iPhone is connected and unlocked, then run the task again.")


class ActionBlocked(NotSent):
    """An action refused before it was sent, because the runner is restarting. Nothing happened on the phone."""


class ActionTracker:
    """Which WDA actions are in flight per phone, which ended unknown, which run holds each phone, and whether a
    restart blocks new actions. One lock covers all of it, so the restart decision (``admit_restart``) and run
    admission (``run_started``) can't interleave with an action ending or a run starting."""

    UNKNOWN_KEPT = 20

    def __init__(self, clock=time.time):
        self.clock = clock
        self.lock = threading.Lock()
        self.flying = {}      # token -> (url, method, path, started, run_id)
        self.unknown = {}     # url -> [(method, path, at, run_id)]
        self.blocked = set()
        self.runs = {}        # url -> run id of the task holding the phone
        self.last_action = {}  # url -> when the last action was sent (wall clock)
        self.next_token = 0

    @staticmethod
    def _key(url):
        return (url or "").rstrip("/")

    # -- runs ----------------------------------------------------------------------------------------

    def run_started(self, url, run_id):
        """A task takes the phone. Raises ActionBlocked while a restart runs: the start is refused, nothing sent."""
        url = self._key(url)
        with self.lock:
            if url in self.blocked:
                raise ActionBlocked("Mobster's helper is restarting; nothing sent")
            self.runs[url] = run_id

    def run_ended(self, url, run_id):
        """The task is over: its journal holds what it left unknown, so the tracker forgets those outcomes."""
        url = self._key(url)
        with self.lock:
            if url in self.runs and self.runs[url] == run_id:
                del self.runs[url]
            self.unknown.pop(url, None)

    def has_run(self, url):
        with self.lock:
            return self._key(url) in self.runs

    # -- actions -------------------------------------------------------------------------------------

    def begin(self, url, method, path):
        """Record an action about to be sent; None for a read. Raises ActionBlocked while a restart runs."""
        if not is_action(method, path):
            return None
        url = self._key(url)
        with self.lock:
            if url in self.blocked:
                raise ActionBlocked("Mobster's helper is restarting; nothing sent")
            self.next_token += 1
            self.flying[self.next_token] = (url, method, path, self.clock(), self.runs.get(url))
            self.last_action[url] = self.clock()
            return self.next_token

    def end(self, token, outcome="done"):
        """outcome: "done" (answered), "not_sent" (refused before it left) or "unknown" (it left; no answer)."""
        if token is None:
            return
        with self.lock:
            entry = self.flying.pop(token, None)
            if outcome == "unknown" and entry is not None:
                url, method, path, _, run_id = entry
                self.unknown.setdefault(url, []).append((method, path, self.clock(), run_id))
                del self.unknown[url][:-self.UNKNOWN_KEPT]

    def last_action_at(self, url):
        with self.lock:
            return self.last_action.get(self._key(url))

    def in_flight(self, url):
        url = self._key(url)
        with self.lock:
            return [(m, p) for u, m, p, *_ in self.flying.values() if u == url]

    def unknown_outcomes(self, url):
        with self.lock:
            return [(m, p, at) for m, p, at, _ in self.unknown.get(self._key(url), ())]

    # -- restarts ------------------------------------------------------------------------------------

    def admit_restart(self, url):
        """Decide and block in one step: None when new actions are now blocked and the restart may go ahead, else
        the reason it may not ("action_in_flight", "unknown_outcome" while a task holds the phone)."""
        url = self._key(url)
        with self.lock:
            if url in self.blocked:
                return "restarting"
            if any(u == url for u, *_ in self.flying.values()):
                return "action_in_flight"
            if url in self.runs and self.unknown.get(url):
                return "unknown_outcome"
            self.blocked.add(url)
            return None

    def unblock(self, url):
        with self.lock:
            self.blocked.discard(self._key(url))


tracker = ActionTracker()


def send(url, method, path, send_request):
    """Run ``send_request()`` for a WDA request, recording it when it is an action. Never retries."""
    token = tracker.begin(url, method, path)
    try:
        result = send_request()
    except NotSent:
        tracker.end(token, "not_sent")
        raise
    except Exception:
        tracker.end(token, "unknown")
        raise
    tracker.end(token, "done")
    return result


class Watchdog:
    def __init__(self, restart, *, url=lambda: None, busy=lambda: False, alive=lambda: True, on_dead_runner=None,
                 actions=tracker, wall=time.time):
        """restart(): stop and start the runner and relay. url(): the phone's WDA address. busy(): a task runs on
        it. alive(): the runner process is running. on_dead_runner(message): end the running task."""
        self.restart, self.url, self.busy, self.alive = restart, url, busy, alive
        self.on_dead_runner, self.actions, self.wall = on_dead_runner, actions, wall
        self.state, self.restarts, self.last_restart = "ok", 0, None
        self.failures, self.wedged_since = 0, None
        self.last_restart_at = None
        self.streak = 0            # restarts in a row, for the backoff
        self.healthy_since = None
        self.deferred = None       # why the last restart did not happen: a short id

    # -- input ---------------------------------------------------------------------------------

    def probe_result(self, ok, now):
        """One probe's result at monotonic time ``now``. Returns True when it restarted the runner."""
        if ok:
            self.failures, self.wedged_since, self.deferred = 0, None, None
            if self.state != "restarting":
                self.state = "ok"
            if self.healthy_since is None:
                self.healthy_since = now
            elif now - self.healthy_since >= HEALTHY_SECONDS:
                self.streak = 0
            return False
        self.healthy_since = None
        self.failures += 1
        if self.wedged_since is None:
            self.wedged_since = now
        if self.failures >= FAILURES_TO_WEDGE:
            self.state = "wedged"
        else:
            return False
        if now - self.wedged_since < WEDGED_RESTART_SECONDS:
            return False
        return self._try_restart(now)

    def reset_failures(self):
        """The runner is stopped on purpose or still starting: missed checks count for nothing."""
        self.failures, self.wedged_since = 0, None
        if self.state == "wedged":
            self.state = "ok"

    # -- the rules -----------------------------------------------------------------------------

    def gap(self):
        return min(MAX_RESTART_GAP, MIN_RESTART_GAP * 2 ** max(0, self.streak - 1))

    def admit(self, now):
        """Every automatic restart (this watchdog's, the supervisor's relaunch, autostart) asks here. None: go ahead,
        new actions are blocked until ``done``. Else the reason it must not."""
        url = self.url()
        if self.last_restart_at is not None and now - self.last_restart_at < self.gap():
            return "backoff"
        reason = self.actions.admit_restart(url)
        if reason is not None:
            return reason
        self.state = "restarting"
        self.last_restart_at = now
        self.streak += 1
        self.restarts += 1
        self.last_restart = int(self.wall() * 1000)
        self.failures, self.wedged_since = 0, None
        return None

    def done(self, ok=True):
        self.actions.unblock(self.url())
        self.state = "ok" if ok else "wedged"

    def _try_restart(self, now):
        url = self.url()
        if not self.alive():
            # Nothing to restart into: with a task running it ends the task with a plain sentence. Without one the
            # supervisor's gated relaunch and autostart bring it back.
            if (self.actions.has_run(url) or self.busy()) and callable(self.on_dead_runner):
                self.deferred = "dead_runner_with_task"
                self.on_dead_runner(DEAD_RUNNER)
            return False
        reason = self.admit(now)
        if reason is not None:
            self.deferred = reason
            return False
        self.deferred = None
        try:
            self.restart()
        except Exception:  # noqa: BLE001 -- the next probes decide; backoff holds the next try
            self.done(False)
            return False
        self.done(True)
        return True

    def snapshot(self):
        return {"state": self.state, "restarts": self.restarts, "last_restart": self.last_restart}
