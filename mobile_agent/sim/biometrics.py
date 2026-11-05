"""Simulator biometric events through the notifyutil backend used by Appium."""

import re

from ..devices import wda_url
from .models import SimError

# Appium's names: https://github.com/appium/appium-ios-simulator/blob/f104016afea238b138054d7c9b868dfbec77be72/lib/extensions/biometric.ts
ENROLLMENT = "com.apple.BiometricKit.enrollmentChanged"
BIOMETRICS = {"face-id": "pearl", "touch-id": "fingerTouch"}
ACTIONS = ("status", "enroll", "unenroll", "match", "nonmatch")


def perform(manager, udid, action, biometric_type="face-id"):
    # no acquire_udid: it dismisses the authentication prompt this command exercises
    if action not in ACTIONS or biometric_type not in BIOMETRICS:
        raise SimError("simulator", "Unknown biometric action or type.",
                       "See `mobster sim biometric --help`.")
    # Claim the current port under the registry lock: acquire() may move a runner
    # to a different port while preparing it. Never lock a stale address.
    lease = None
    try:
        with manager.registry.edit() as entries:
            entry = dict(manager._entry(udid, entries))
            lease = manager._try_lease(wda_url(entry["wda_port"]))
            if lease is None:
                raise SimError("busy", f"{entry['name']} is running a check.",
                               "Wait for it to finish, or stop it, before sending biometric events.")
        current = manager._guard_owned(entry, manager._devices())
        if current is None or current.state != "Booted":
            raise SimError("simulator", "Biometric testing needs a booted Mobster simulator.",
                           "Run `mobster sim prepare`, then use the UDID from `mobster sim list`.")
        return _perform(manager, entry["udid"], action, biometric_type)
    finally:
        if lease is not None:
            lease.close()


def _notify(manager, udid, *args):
    result = manager.simctl("spawn", udid, "notifyutil", *args, timeout=30)
    if not result.ok:
        raise SimError("simulator", f"The simulator biometric command did not complete: "
                       f"{result.last_line() or 'notifyutil failed'}",
                       "Inspect the app and `mobster sim biometric <UDID> status` before sending another event.")
    return result.out


def deliver(manager, udid, action, biometric_type="face-id"):
    """``action`` on simulator ``udid`` without the lease check ``perform`` makes: for a check run that holds the
    simulator itself (a Face ID match while its app waits for one). The caller owns the lease."""
    if action not in ACTIONS or biometric_type not in BIOMETRICS:
        raise SimError("simulator", "Unknown biometric action or type.", "See `mobster sim biometric --help`.")
    return _perform(manager, udid, action, biometric_type)


def _enrolled(manager, udid):
    output = _notify(manager, udid, "-g", ENROLLMENT)
    match = re.fullmatch(re.escape(ENROLLMENT) + r"\s+([01])", output.strip())
    if match is None:
        raise SimError("simulator", "The simulator did not report a recognized biometric enrollment state.",
                       "Use Simulator's Features menu to inspect enrollment; this runtime may be unsupported.")
    return match[1] == "1"


def _perform(manager, udid, action, biometric_type):
    if action in ("enroll", "unenroll"):
        enabled = action == "enroll"
        _notify(manager, udid, "-s", ENROLLMENT, "1" if enabled else "0")
        _notify(manager, udid, "-p", ENROLLMENT)
        enrolled = _enrolled(manager, udid)
        if enrolled != enabled:
            raise SimError("simulator", "The simulator did not confirm the requested biometric enrollment.",
                           "Inspect enrollment in Simulator's Features menu before trying again.")
    else:
        enrolled = _enrolled(manager, udid)
        if action in ("match", "nonmatch"):
            if not enrolled:
                raise SimError("simulator", "The simulator has no enrolled biometric.",
                               f"Run `mobster sim biometric {udid} enroll` before sending a match event.")
            outcome = "match" if action == "match" else "nomatch"
            notification = f"com.apple.BiometricKit_Sim.{BIOMETRICS[biometric_type]}.{outcome}"
            _notify(manager, udid, "-p", notification)
    return {"udid": udid, "action": action, "type": biometric_type, "enrolled": enrolled,
            "event_sent": action in ("match", "nonmatch")}
