"""Cable or Wi-Fi: choosing the transport, the reconnect backoff, and each phone's link state (SPEC §3.7 items 5-6).

The rule: a phone on USB uses the cable. Else, when Wi-Fi is on for it and the encrypted tunnel is up, it uses
Wi-Fi. Else it is unplugged. A change waits until no task is running on the phone (``runtime.device_busy``), so a
cable plugged in mid-task keeps Wi-Fi until the task ends, then switches.

The link states live in this process only (the monitor writes them; lockscreen.py reads them for a running task's
guard). Nothing here touches a phone.
"""

from dataclasses import asdict, dataclass, replace
import threading
import time
from typing import Optional

TRANSPORTS = ("usb", "wifi")
TUNNEL_STATES = ("up", "connecting", "down", "unknown")
# Seconds before each reconnect attempt after a failure: 1, 2, 4, 8, ... at most 60.
BACKOFF = (1, 2, 4, 8, 16, 32, 60)


def choose(usb_attached, wifi_enabled, tunnel_up):
    """The transport a phone should use now: "usb", "wifi" or None (unplugged and not reachable)."""
    if usb_attached:
        return "usb"
    if wifi_enabled and tunnel_up:
        return "wifi"
    return None


def next_step(current, desired, busy):
    """What to do about a phone whose runner uses ``current`` when it should use ``desired``:
    "stay" (nothing to change), "wait" (a task is running: change after it) or "switch"."""
    if current == desired:
        return "stay"
    return "wait" if busy else "switch"


class Backoff:
    """Reconnect delays: after the n-th failure in a row, wait BACKOFF[n-1] seconds (60 s from then on)."""

    def __init__(self, schedule=BACKOFF):
        self.schedule = tuple(schedule)
        self.failures = 0
        self.next_at = 0.0

    def delay(self, failures=None):
        failures = self.failures if failures is None else failures
        if failures <= 0:
            return 0
        return self.schedule[min(failures, len(self.schedule)) - 1]

    def ready(self, now):
        return now >= self.next_at

    def fail(self, now):
        """Count a failure; returns the seconds until the next attempt."""
        self.failures += 1
        wait = self.delay()
        self.next_at = now + wait
        return wait

    def reset(self):
        self.failures = 0
        self.next_at = 0.0


@dataclass(frozen=True)
class Link:
    """One phone's connection as Mobster sees it now."""
    udid: str
    transport: Optional[str] = None      # "usb" | "wifi" | None: what its runner uses now
    tunnel: str = "unknown"              # "up" | "connecting" | "down" | "unknown"
    reachable: bool = False              # its WebDriverAgent answers through this Mac's relay or iproxy
    last_seen_at: Optional[int] = None   # ms: the last time it answered
    problem: Optional[str] = None        # a words.PROBLEMS code, or None
    switching: bool = False              # a change of transport is pending or under way

    def public(self):
        value = asdict(self)
        value["lastSeenAt"] = value.pop("last_seen_at")
        return value


_links = {}
_lock = threading.Lock()


def link(udid) -> Optional[Link]:
    with _lock:
        return _links.get(_key(udid))


def update(udid, **changes):
    """Set fields of ``udid``'s link; returns (before, after). ``reachable`` True stamps ``last_seen_at``."""
    key = _key(udid)
    with _lock:
        before = _links.get(key)
        current = before or Link(udid=udid)
        if changes.get("reachable"):
            changes.setdefault("last_seen_at", round(time.time() * 1000))
        after = replace(current, **changes)
        _links[key] = after
        return before, after


def forget(udid=None):
    """Drop one phone's link state, or every phone's (tests, a monitor that stops)."""
    with _lock:
        if udid is None:
            _links.clear()
        else:
            _links.pop(_key(udid), None)


def _key(udid):
    return str(udid or "").upper()


# What lockscreen.DetectGuard.attached() returns when Wi-Fi has nothing to say about a phone: it then asks USB.
NOT_WIFI = object()


def attached(udid):
    """Whether a running task's phone is connected, as far as Wi-Fi knows: True on a live Wi-Fi link, None while
    switching or while the Wi-Fi link is down (unknown: the task's own checks decide), NOT_WIFI when the phone
    isn't on Wi-Fi in this process (then the USB listing answers, as it always did)."""
    state = link(udid)
    if state is None or state.transport != "wifi":
        return NOT_WIFI   # on the cable (a task started there still stops as unplugged when it's pulled)
    if state.switching:
        return None
    return True if state.reachable else None


def lost(udid):
    """Whether ``udid`` was running over Wi-Fi and its link went down (not a switch Mobster is making)."""
    state = link(udid)
    return state is not None and state.transport == "wifi" and not state.reachable and not state.switching
