"""The phone guard: whether an iPhone can be driven now, decided by code (never the model).

This build only *detects*. It never types a passcode, never taps the lock screen and never answers a
system sheet: a locked iPhone, a locked app or the App Store's side-button confirmation
stops the run at once with one plain sentence, before a model call is spent on a screen the agent cannot
use. One exception waits instead of stopping ("Look at your iPhone", WOW §4.1): a task a person just started
in the Mac app, the terminal UI or ``mobster chat`` on a locked iPhone waits up to UNLOCK_WAIT_S for that
person to unlock it, reading only ``GET /wda/locked``, and starts the moment it reads unlocked. An attended run
also waits for a Face ID sheet to clear, then reads the app again; only the owner authenticates. Unlocking with a saved
passcode (PLAN §3) waits for the owner's sign-off; ``CAN_UNLOCK`` says so to the other parts (MCP's ``unlock_status``,
the settings), and ``finish`` has nothing to relock.

What it reads, all through WebDriverAgent and all read-only:

- ``GET /wda/locked`` (sessionless, answers in milliseconds even when the UI is wedged), at every check;
- ``GET /wda/activeAppInfo`` (sessionless), to name the app a sheet is over;
- SpringBoard's own accessibility tree, only when the frontier suspects a system sheet: the session's
  ``defaultActiveApplication`` is pointed at SpringBoard for one ``/source`` read and always restored.

Sheet labels are matched as whole labels, never substrings, so a notification banner that happens to say
"locked" is not a locked-app sheet. They come from iOS 18-26's wording and are provisional until the
owner's read-only ``mobster devices lock-probe`` confirms them on his phone (PLAN §6.1); a sheet the
classifier does not know is ``unknown`` and the run goes on as before (the guard never taps anything,
so a miss costs no more than today).
"""

import os
import re
import time

from . import harness_api
from .agent_hooks import READY, GuardVerdict

# This build cannot unlock an iPhone (no passcode is ever stored, read or typed).
CAN_UNLOCK = False
SPRINGBOARD = "com.apple.springboard"
# Each read the guard makes; a locked phone answers /wda/locked in well under a second.
READ_TIMEOUT = 3.0
# How long a USB listing is trusted (``attached``): a pulled cable is known within this many seconds.
ATTACHED_TTL = 2.0
# Look at your iPhone (WOW §4.1, v0): an interactive task that starts on a locked iPhone waits this many seconds for
# the person to unlock it, reading /wda/locked every UNLOCK_POLL_S. Reads only: no button, no tap, no key, ever.
UNLOCK_WAIT_S = 120
UNLOCK_POLL_S = 1.0
# The states a start waits through: the person clears them by unlocking. Never a sheet or an unreadable phone.
WAIT_STATES = ("lock_screen", "passcode_keypad")

STATES = ("unlocked", "lock_screen", "passcode_keypad", "face_id_sheet", "app_locked_sheet", "apple_confirmation",
          "unknown")

# The user-facing sentences (PLAN §3.9-§3.10). None names a passcode, a code or a digit count. The §3.9
# sentence's "or let Mobster unlock it in Settings › iPhones" clause is left out: that setting needs
# passcode unlock, which this build does not have.
PHONE_LOCKED = "Your iPhone is locked. Unlock it and try again. No action was taken."
LOCKED_DURING = "Your iPhone locked during the task, so Mobster stopped. Unlock it and try again."
NOT_RESPONDING = ("Your iPhone isn't responding. Unlock it, close Control Center or Siri if they're open, then "
                  "try again. No action was taken.")
UNPLUGGED = "Your iPhone was unplugged. Plug it back in and try again."
# A task over Wi-Fi whose encrypted link went down.
WIFI_LOST = ("Your iPhone went out of reach over Wi-Fi, so Mobster stopped. Keep it unlocked and on the same "
             "Wi-Fi as this Mac, or plug it in, then try again.")
# A task a person started waits this long for its phone to come back (DetectGuard.await_return), then stops with these.
RETURN_WAIT_TIMED_OUT = {
    "unplugged": "Your iPhone stayed unplugged, so Mobster stopped. "
                 "Plug it back in and pick the task up where it left off.",
    "wifi_lost": ("Your iPhone stayed out of reach over Wi-Fi, so Mobster stopped. Plug it in or bring it back near "
                  "this Mac, then pick the task up where it left off."),
}
FACE_ID = "{app} is asking for Face ID. Mobster can't use Face ID: open {app} on your iPhone once, then try again."
APP_LOCKED = "{app} is locked on your iPhone. Mobster won't open locked apps; open it yourself or remove the lock."
APPLE_CONFIRMATION = "The App Store wants you to confirm on your iPhone. Mobster never does that for you."
# The stop when a start waited on a locked iPhone (the Mac app's card, `mobster chat`) and nobody unlocked it.
LOCK_WAIT_TIMED_OUT = "Still locked, so the task didn't start. Unlock your iPhone and try again."
# A lock in the middle of a run a person started (DESIGN §12.1) waits the same way, and stops if nobody unlocks.
LOCK_WAIT_MIDRUN_TIMED_OUT = ("Your iPhone stayed locked, so Mobster stopped. Unlock it and pick the task up where it "
                              "left off.")
MIDRUN_CAUSES = ("resume", "lock_suspected")
# The owner is on the phone (owner_presence.py): a lock is theirs to deal with, never a prompt from Mobster. No
# sentence here names a passcode, and no event asks for an unlock.
OWNER_HAS_PHONE = ("You're using your iPhone, so Mobster stayed off it and the task didn't start. "
                   "Start it again when you put the phone down.")

# Whole-label markers (case-insensitive). The keypad needs all ten digit keys besides a title.
_KEYPAD_TITLE = re.compile(r"^(enter( your)? (iphone )?passcode|passcode)$", re.I)
_LOCK_EXTRAS = re.compile(r"^(emergency|cancel|delete)$", re.I)
_FACE_ID = re.compile(r"^(face id|try face id again|use face id( to (unlock|continue))?)$", re.I)
# Only a quoted app name: "Your account is locked" in a banner is not a locked app.
_APP_LOCKED = re.compile(r"^(?:“(?P<a>[^”]{1,60})”|\"(?P<b>[^\"]{1,60})\") is locked\.?$", re.I)
_APP_LOCKED_HINT = re.compile(r"^(?:require face id|face id is required to open .{1,60})$", re.I)
_CONFIRM = re.compile(r"^(double[- ]click to (install|pay|get|confirm|subscribe)|confirm with side button)\.?$", re.I)


def tree_labels(tree, limit=400):
    """The labels in a WDA ``/source?format=json`` tree, in reading order: (type, text) pairs."""
    found, stack = [], [tree] if isinstance(tree, dict) else []
    while stack and len(found) < limit:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        text = next((node[key] for key in ("label", "name", "value")
                     if isinstance(node.get(key), str) and node[key].strip()), "")
        if text:
            found.append((str(node.get("type") or ""), text.strip()[:200]))
        children = node.get("children")
        if isinstance(children, list):
            stack.extend(reversed(children))
    return found


def _keypad(texts):
    digits = {t for t in texts if len(t) == 1 and t.isdigit()}
    return len(digits) == 10 and any(_KEYPAD_TITLE.match(t) for t in texts)


def classify(locked, front=None, labels=None):
    """(state, app or None) from what the guard read; a keypad needs SpringBoard, its title and all ten digits."""
    texts = [text for _, text in (labels or ())]
    if locked is True:
        return ("passcode_keypad" if front in (None, SPRINGBOARD) and _keypad(texts) else "lock_screen"), None
    if locked is None:
        return "unknown", None
    for text in texts:
        if _CONFIRM.match(text):
            return "apple_confirmation", None
    for text in texts:
        match = _APP_LOCKED.match(text)
        if match:
            return "app_locked_sheet", next(name for name in match.groups() if name)
    if any(_APP_LOCKED_HINT.match(text) for text in texts):
        return "app_locked_sheet", None
    if any(_FACE_ID.match(text) for text in texts):
        return "face_id_sheet", None
    if labels is not None and front == SPRINGBOARD and _keypad(texts) and any(_LOCK_EXTRAS.match(t) for t in texts):
        # A passcode keypad over an unlocked phone: iOS asking for the passcode (a locked app whose Face ID
        # failed, a Settings change). Never ours to answer.
        return "app_locked_sheet", None
    return "unlocked", None


def app_name(bundle, named=None):
    """A display name for a sentence: the sheet's own, else the catalog's, else "This app"."""
    if named:
        return named
    if bundle and bundle != SPRINGBOARD:
        from .catalog import app_label
        label = app_label(bundle)
        if label != bundle:
            return label.rsplit(" (", 1)[0]
    return "This app"


def verdict_for(state, *, cause, app=None):
    """The GuardVerdict a state means for a check made for ``cause``."""
    if state == "unlocked":
        return READY
    if state in ("lock_screen", "passcode_keypad"):
        return GuardVerdict("stop", "phone_locked", PHONE_LOCKED if cause == "preflight" else LOCKED_DURING)
    if state == "face_id_sheet":
        return GuardVerdict("stop", "face_id", FACE_ID.format(app=app or "This app"))
    if state == "app_locked_sheet":
        return GuardVerdict("stop", "app_locked", APP_LOCKED.format(app=app or "This app"))
    if state == "apple_confirmation":
        return GuardVerdict("stop", "apple_confirmation", APPLE_CONFIRMATION)
    # unknown: the phone could not be read. Fail closed: never act on a phone the guard could not read.
    return GuardVerdict("stop", "phone_locked", NOT_RESPONDING if cause == "preflight" else LOCKED_DURING)


def read_locked(driver, timeout=READ_TIMEOUT):
    """/wda/locked: True, False, or None when WDA could not say."""
    try:
        value = driver.http.request("GET", "/wda/locked", None, timeout).get("value")
    except Exception:
        return None
    return value if isinstance(value, bool) else None


def read_front(driver, timeout=READ_TIMEOUT):
    """The foreground bundle, or None; /wda/activeAppInfo is never waited on past READ_TIMEOUT."""
    slow = getattr(driver, "slow_front", None)
    if callable(slow):
        try:
            bundle = slow()
        except Exception:
            bundle = None
        if isinstance(bundle, str) and bundle:
            return bundle
    try:
        value = driver.http.request("GET", "/wda/activeAppInfo", None, min(timeout, READ_TIMEOUT)).get("value")
    except Exception:
        return None
    bundle = value.get("bundleId") if isinstance(value, dict) else None
    return bundle if isinstance(bundle, str) and bundle else None


def read_springboard_tree(driver, timeout=READ_TIMEOUT):
    """SpringBoard's accessibility tree, or None; the session's active application is always put back afterwards."""
    previous = getattr(driver, "_hinted", None) or "auto"
    try:
        driver.call("POST", "/appium/settings", {"settings": {"defaultActiveApplication": SPRINGBOARD}}, timeout)
    except Exception:
        return None
    try:
        return driver.call("GET", "/source?format=json", None, timeout)
    except Exception:
        return None
    finally:
        try:
            driver.call("POST", "/appium/settings", {"settings": {"defaultActiveApplication": previous}}, timeout)
        except Exception:
            pass


def read_springboard(driver, timeout=READ_TIMEOUT):
    """SpringBoard's (type, text) labels, or None."""
    tree = read_springboard_tree(driver, timeout)
    return None if tree is None else tree_labels(tree)


def read_coredevice_locked(udid):
    """The phone's lock as CoreDevice reports it (True, False or None): the second reader for a silent /wda/locked."""
    if not udid:
        return None
    reader = harness_api.hooks().coredevice_locked
    try:
        value = reader(udid)
    except Exception:
        return None
    return value if isinstance(value, bool) else None


def probe(driver, *, springboard=True, timeout=READ_TIMEOUT, udid=None):
    """Everything the guard can read, for ``mobster devices lock-probe``: no tap, no key, no typing."""
    wda = read_locked(driver, timeout)
    core = read_coredevice_locked(udid) if wda is None else None
    locked = True if core is True else wda
    front = read_front(driver, timeout) if wda is not None else None
    labels = read_springboard(driver, timeout) if springboard and wda is not None else None
    state, named = classify(locked, front, labels)
    return {"locked": locked, "front": front, "state": state, "app": named,
            **({"coreDevice": core} if wda is None and udid else {}),
            "labels": [{"type": kind, "label": text} for kind, text in (labels or ())]}


# Causes that read SpringBoard's tree for a sheet. Preflight does not: it presses Home next, and a locked
# phone must stop in well under 2 s.
SHEET_CAUSES = ("launch_failed", "lock_suspected", "sheet_suspected", "resume")


def blocked_apps(env=None):
    """MOBSTER_BLOCKED_APPS: bundle ids Mobster never opens (comma-separated)."""
    raw = (os.environ if env is None else env).get("MOBSTER_BLOCKED_APPS") or ""
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


class DetectGuard:
    """agent_hooks.PhoneGuard that only detects; with ``wait_s`` > 0 a lock waits for the person instead of stopping."""

    def __init__(self, *, device_id=None, usb=False, emit=None, blocked=None, attached_reader=None, wait_s=0.0,
                 cancelled=None, sleep=time.sleep, clock=time.monotonic, owner_active=None):
        self.device_id = device_id
        self.owner_active = owner_active
        self.usb = bool(usb and device_id)
        self.emit = emit
        self.blocked = blocked_apps() if blocked is None else frozenset(blocked)
        self._attached_reader = attached_reader
        self.wait_s = max(0.0, float(wait_s or 0))
        self.cancelled = cancelled
        self.sleep, self.clock = sleep, clock

    def check(self, driver, *, cause):
        locked = read_locked(driver)
        if locked is None:
            attached = self.attached()
            if attached is False:
                return self.await_return(driver, "unplugged", midrun=cause != "preflight") or GuardVerdict(
                    "stop", "unplugged", UNPLUGGED)
            if attached is None and self.wifi_lost():
                return self.await_return(driver, "wifi_lost", midrun=cause != "preflight") or GuardVerdict(
                    "stop", "unplugged", WIFI_LOST)
        if locked is not False or cause not in SHEET_CAUSES:
            state = classify(locked)[0]
            if state in WAIT_STATES and not self._person_waits(cause) and self._owner_has_phone():
                # An unattended check (a schedule, MCP, a script) on a phone its owner is using: "you have it",
                # never "locked, unlock it". A task a person started waits as before: they asked.
                return GuardVerdict("stop", "owner_active", OWNER_HAS_PHONE)
            if cause == "preflight" and self.wait_s > 0 and state in WAIT_STATES:
                return self._await_unlock(driver, state)
            if cause in MIDRUN_CAUSES and self.wait_s > 0 and state in WAIT_STATES:
                return self._await_unlock(driver, state, midrun=True)
            return verdict_for(state, cause=cause)
        front = read_front(driver)
        labels = read_springboard(driver)
        if labels is None:
            return READY  # unlocked, and no sheet could be read: nothing to stop for
        state, named = classify(False, front, labels)
        if state == "face_id_sheet" and self.wait_s > 0:
            return self._await_face_id(driver, app_name(front, named))
        return verdict_for(state, cause=cause, app=app_name(front, named))

    def _await_face_id(self, driver, app):
        """Wait for the owner to clear Face ID. Never treat an unreadable sheet as authentication."""
        started = self.clock()
        verdict = verdict_for("face_id_sheet", cause="resume", app=app)
        self._emit({"event": "handoff_waiting", "kind": "face_id", "seconds": int(self.wait_s)})
        while not self._stopped():
            left = started + self.wait_s - self.clock()
            if left <= 0:
                break
            self.sleep(min(UNLOCK_POLL_S, left))
            if self._stopped():
                break
            locked = read_locked(driver)
            if locked is not False:
                attached = self.attached()
                if attached is False or attached is None and self.wifi_lost():
                    verdict = GuardVerdict("stop", "unplugged", UNPLUGGED if attached is False else WIFI_LOST)
                else:
                    verdict = verdict_for(classify(locked)[0], cause="resume")
                break
            front, labels = read_front(driver), read_springboard(driver)
            state, named = classify(False, front, labels) if front and labels is not None else ("unknown", None)
            if self._stopped():
                break
            verdict = verdict_for(state, cause="resume", app=app_name(front, named))
            if state != "face_id_sheet":
                break
        self._emit({"event": "handoff_finished", "kind": "face_id", "ok": verdict.state == "ready",
                    "waited_s": round(max(0.0, self.clock() - started), 1)})
        return verdict

    def _await_unlock(self, driver, kind, midrun=False):
        """Look at your iPhone: wait up to ``wait_s`` for the person to unlock the phone, then READY, or a stop.

        The only requests are ``GET /wda/locked`` once a second and, when a read fails, the USB listing. Nothing is
        pressed, tapped or typed while the phone is locked: the person unlocks it, never Mobster. A read that fails
        while the cable is known to be in keeps waiting (over Wi-Fi a locked phone may drop its link for a moment)."""
        started = self.clock()
        deadline = started + self.wait_s
        where = {"midrun": True} if midrun else {}
        self._emit({"event": "handoff_waiting", "kind": kind, "seconds": int(self.wait_s), **where})
        while True:
            if self._stopped():
                self._emit({"event": "handoff_finished", "kind": kind, "ok": False, **where})
                # Stop: the run ends as stopped (run.stop is set)
                return verdict_for(kind, cause="resume" if midrun else "preflight")
            left = deadline - self.clock()
            if left <= 0:
                break
            self.sleep(min(UNLOCK_POLL_S, left))
            if self._stopped():
                continue
            locked = read_locked(driver)
            if locked is False:
                self._emit({"event": "handoff_finished", "kind": kind, "ok": True,
                            "waited_s": round(max(0.0, self.clock() - started), 1), **where})
                return READY
            if locked is None:
                attached = self.attached()
                if attached is False or attached is None and self.wifi_lost():
                    self._emit({"event": "handoff_finished", "kind": kind, "ok": False, **where})
                    return GuardVerdict("stop", "unplugged", UNPLUGGED if attached is False else WIFI_LOST)
        self._emit({"event": "handoff_finished", "kind": kind, "ok": False, **where})
        return GuardVerdict("stop", "phone_locked", LOCK_WAIT_MIDRUN_TIMED_OUT if midrun else LOCK_WAIT_TIMED_OUT)

    def await_return(self, driver, kind="unplugged", midrun=True):
        """Wait up to ``wait_s`` for an unplugged or Wi-Fi-lost phone to answer; None when the run doesn't wait."""
        if self.wait_s <= 0:
            return None
        started = self.clock()
        deadline = started + self.wait_s
        where = {"midrun": True} if midrun else {}
        self._emit({"event": "handoff_waiting", "kind": kind, "seconds": int(self.wait_s), **where})
        while not self._stopped():
            left = deadline - self.clock()
            if left <= 0:
                break
            self.sleep(min(UNLOCK_POLL_S, left))
            if self._stopped() or self.attached() is False:
                continue
            locked = read_locked(driver)
            if locked is None:
                continue   # plugged in or on the link again, its runner not answering yet
            self._emit({"event": "handoff_finished", "kind": kind, "ok": True,
                        "waited_s": round(max(0.0, self.clock() - started), 1), **where})
            return self._await_unlock(driver, "lock_screen", midrun=True) if locked else READY
        self._emit({"event": "handoff_finished", "kind": kind, "ok": False, **where})
        if self._stopped():
            return GuardVerdict("stop", "unplugged", UNPLUGGED if kind == "unplugged" else WIFI_LOST)
        return GuardVerdict("stop", "unplugged", RETURN_WAIT_TIMED_OUT[kind])

    def _person_waits(self, cause):
        """Whether a check for ``cause`` waits for a person to unlock instead of stopping (a task they started)."""
        if self.wait_s <= 0:
            return False
        return cause in ("preflight", *MIDRUN_CAUSES)

    def _owner_has_phone(self):
        """Whether the phone's owner is using it now. A reader that fails says no: the lock rules stand."""
        try:
            return bool(self.owner_active()) if callable(self.owner_active) else False
        except Exception:
            return False

    def _stopped(self):
        try:
            return bool(self.cancelled()) if callable(self.cancelled) else False
        except Exception:
            return True  # can't tell whether Stop was pressed: stop, never wait on

    def _emit(self, event):
        if callable(self.emit):
            self.emit(event)

    def attached(self):
        """Whether the phone is connected; None, not False, when it may be on Wi-Fi, so it isn't stopped unplugged."""
        if not self.usb:
            return None
        wifi = _wifi_attached(self.device_id)
        if wifi is not _NOT_WIFI:
            return wifi
        reader = self._attached_reader or harness_api.hooks().usb_attached
        try:
            usb = reader(self.device_id)
        except Exception:
            return None
        if usb is False and _wifi_may_reach(self.device_id):
            return None
        return usb

    def wifi_lost(self):
        """Whether the phone was running over Wi-Fi and its link went down (not a switch Mobster is making)."""
        if not self.usb:
            return False
        lost = harness_api.hooks().wifi_lost
        try:
            return bool(lost(self.device_id))
        except Exception:
            return False

    def allows_app(self, bundle_id):
        return bundle_id not in self.blocked

    def finish(self, driver):
        """Nothing to relock: this guard never unlocks."""


_NOT_WIFI = object()


def _wifi_attached(udid):
    attached = harness_api.hooks().wifi_attached
    try:
        wifi, value = attached(udid)
    except Exception:
        return _NOT_WIFI
    return value if wifi else _NOT_WIFI


def _wifi_may_reach(udid):
    """Whether an unplugged phone may run over the Mac app's Wi-Fi relay, in a process that doesn't run its link."""
    may_reach = harness_api.hooks().wifi_may_reach
    try:
        return bool(may_reach(udid))
    except Exception:
        return False


def guard_for(*, device_id, wda_url, mode, approve=None, emit=None, video=None, usb=True, wait_s=0.0, cancelled=None):
    """The phone guard for one run (agent_hooks.PhoneGuard). ``mode``: "interactive" | "schedule" | "scripts".
    ``usb``: whether ``device_id`` is a USB iPhone's UDID (a simulator or a bare WDA address is never
    "unplugged"). ``approve`` and ``video`` are for unlocking, which this build does not do. ``wait_s``: how long a
    start on a locked phone, or a run the phone locks under, waits for the person to unlock it (0, the default, stops
    at once: ``mobster run``, schedules, MCP); ``cancelled``: whether Stop was pressed."""
    if mode not in ("interactive", "schedule", "scripts"):
        raise ValueError(f"unknown guard mode {mode!r}")
    from . import owner_presence
    return DetectGuard(device_id=device_id, usb=usb, emit=emit, wait_s=wait_s if mode == "interactive" else 0.0,
                       cancelled=cancelled,
                       owner_active=lambda: owner_presence.is_owner_active(wda_url))


def after_error(guard, driver):
    """Why a task's step failed when the phone itself says so (cable, Wi-Fi, lock), else None; it only reads."""
    if guard is None:
        return None
    from .agent_hooks import safe_attached
    attached = safe_attached(guard)
    if attached is False:
        return GuardVerdict("stop", "unplugged", UNPLUGGED)
    if attached is None:
        try:
            lost = bool(guard.wifi_lost())
        except Exception:
            lost = False
        if lost:
            return GuardVerdict("stop", "unplugged", WIFI_LOST)
    if driver is not None and read_locked(driver) is True:
        return GuardVerdict("stop", "phone_locked", LOCKED_DURING)
    return None


def unlock_settings(record=None):
    """The phone's unlock settings, as booleans: off, since this build cannot unlock."""
    return {"enabled": False, "askBeforeUnlocking": True, "onSchedule": "never", "scripts": False,
            "relockAfter": True, "needsCheck": False, "supported": CAN_UNLOCK}


def describe(probe_result):
    """Plain lines for ``mobster devices lock-probe``."""
    words = {"unlocked": "unlocked, no system sheet", "lock_screen": "locked (lock screen)",
             "passcode_keypad": "locked (passcode keypad showing)", "face_id_sheet": "asking for Face ID",
             "app_locked_sheet": "a locked app's sheet", "apple_confirmation": "asking to confirm with the side button",
             "unknown": "not readable (WebDriverAgent didn't answer)"}
    lines = [f"State: {words.get(probe_result['state'], probe_result['state'])}"]
    if "coreDevice" in probe_result:
        lines.append("CoreDevice lock state: " + {True: "locked", False: "unlocked", None: "not readable"}[
            probe_result["coreDevice"]])
    if probe_result.get("front"):
        lines.append(f"Front app: {probe_result['front']}")
    labels = probe_result.get("labels") or []
    if labels:
        lines.append(f"SpringBoard shows {len(labels)} labels:")
        lines.extend(f"  {item['type'] or '?'}: {item['label']}" for item in labels)
    lines.append("Nothing was tapped or typed.")
    return lines

