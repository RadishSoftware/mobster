"""Add a device ends wireless: the Wi-Fi step of a new iPhone's setup, on the cable it was plugged in with. Mobster
asks first: it turns on the phone's Connect via network switch only after the person said yes for this phone, in
`mobster setup` or with the phone's own Wi-Fi switch (`mobster wifi on`, Use over Wi-Fi in the app)."""

import threading
import time
from dataclasses import dataclass
from typing import Optional

from . import devicectl, pair, store, switch, transport_on

STATES = ("off", "skipped", "after_trust", "asking", "pairing", "paired", "waiting_reboot", "waiting_tunnel",
          "ready", "cable_only")
# Why a phone stays on the cable: the phone said no, the pair tool isn't on this Mac (tick Connect via network in
# Xcode instead), its answer couldn't be read, it didn't come back after a restart, or no Wi-Fi link came in time
# (old_ios: iOS before 17.4, where the link often doesn't come up).
REASONS = ("refused", "needs_xcode", "unknown", "no_return", "no_tunnel", "old_ios")
REBOOT_WAIT = 300.0   # seconds a phone that left the cable mid-setup is waited for
TUNNEL_WAIT = 90.0    # seconds Mobster waits for the Wi-Fi link once the helper runs
POLL = 3.0            # devicectl at most this often while it waits
TUNNEL_FROM = (17, 4)
HUB_FROM = 27         # iOS 27 and later: Device Hub's Pair Nearby Device, said in words only


def version(text):
    """(major, minor, patch) of "17.3.1", or None for anything that isn't a version."""
    if not isinstance(text, str):
        return None
    parts = text.strip().split(".")
    if not 1 <= len(parts) <= 3 or not all(part.isdigit() and len(part) <= 3 for part in parts):
        return None
    numbers = [int(part) for part in parts]
    return tuple(numbers + [0] * (3 - len(numbers)))


@dataclass(frozen=True)
class Facts:
    """What a Setup read saw of the phone: on the cable now, trusting this Mac, its helper installed, running."""
    attached: bool
    trusted: bool
    built: bool
    running: bool
    ios: Optional[str] = None


class Flow:
    """One phone's Wi-Fi step."""

    def __init__(self, udid, data_dir, *, pair_run=None, lister=None, clock=time.monotonic):
        self.udid = udid
        self.data_dir = data_dir
        self.pair_run = pair_run or (lambda: pair.run(udid, "enable"))
        self.lister = lister or devicectl.list_devices
        self.clock = clock
        self.lock = threading.Lock()
        self.state = None          # None until the first read decides skipped or after_trust
        self.reason = None
        self.os = None             # devicectl's osVersionNumber, else lockdown's ProductVersion
        self.cable = False
        self.since = None          # when waiting_reboot or waiting_tunnel began
        self.stored = False        # Wi-Fi turned on in Mobster's own list (store.py)
        self.consent = None        # the person's answer to "use it over Wi-Fi?": None until asked and answered
        self.before = None         # the phone's record before the pairing noted it (switch.noted), for a no
        self.rearm = False         # the tool said the phone wasn't on the cable or trusted: wait for a new plug-in
        self.polled_at = None
        self.worker = None

    # -- the reads -----------------------------------------------------------------------------------------------

    def observe(self, facts, now=None):
        """Move on from what a Setup read saw. True when there is slow work for ``work``."""
        now = self.clock() if now is None else now
        with self.lock:
            self.cable = facts.attached
            if facts.ios and self.os is None:
                self.os = facts.ios
            if self.state is None:
                self.state = "skipped" if facts.built else "after_trust"
            if self.state == "after_trust":
                if not (facts.attached and facts.trusted):
                    self.rearm = False
                elif not self.rearm:
                    if not self.consent:
                        self.state = "asking"
                    elif self._note():
                        self.state = "pairing"
            if self.state == "asking" and self._turned_on():
                # its own Wi-Fi switch turned the phone's switch on and put it in Mobster's list; once it's off the
                # cable, the Wi-Fi monitor has it (wireless/monitor.py)
                self.state, self.stored = "paired" if facts.attached else "skipped", True
            if self.state == "paired":
                if not facts.attached:
                    self.state, self.since = "waiting_reboot", now
                else:
                    self._store()
                    if self.stored and facts.built and facts.running:
                        self.state, self.since = "waiting_tunnel", now
            elif self.state == "waiting_reboot":
                if facts.attached:
                    self.state, self.since = "paired", None
                elif now - self.since >= REBOOT_WAIT:
                    self._end("no_return")
            if self.state == "waiting_tunnel" and now - self.since >= TUNNEL_WAIT:
                self._end_tunnel_wait()
            return self.state in ("pairing", "waiting_tunnel")

    def _end_tunnel_wait(self):
        """No Wi-Fi link in time: say why. Callers hold ``self.lock``."""
        old = version(self.os)
        self._end("old_ios" if old is not None and old[:2] < TUNNEL_FROM else "no_tunnel")

    def answer(self, yes):
        """The person's answer for this phone: yes goes on to the pairing (over the cable, once it trusts this Mac),
        no keeps it on the cable. LookupError when Mobster isn't asking now."""
        with self.lock:
            if self.state != "asking":
                raise LookupError("Mobster isn't asking about Wi-Fi for this iPhone now.")
            self.consent = yes is True
            self.state = "after_trust" if self.consent else "skipped"

    def _note(self):
        """The pairing is noted in Mobster's list before it is sent (switch.noted); until the phone is set up here
        there is no list to note it in, and nothing is sent. Callers hold ``self.lock``."""
        try:
            self.before = switch.noted(self.data_dir, self.udid)
            return True
        except Exception:  # noqa: BLE001 -- not set up here yet, or the list can't be written: send nothing
            return False

    def _turned_on(self):
        """Wi-Fi turned on for the phone with its own switch, which is a yes. Callers hold ``self.lock``."""
        try:
            return store.enabled(self.data_dir, self.udid)
        except Exception:  # noqa: BLE001 -- a list that can't be read is no yes
            return False

    def _store(self):
        """Mobster's own list says Wi-Fi is on for the phone, once it's set up here (a local write: tried each read)."""
        if self.stored:
            return
        try:
            store.put(self.data_dir, self.udid, True, "mobster")
            self.stored = True
        except LookupError:
            pass   # not set up here yet: the helper's install adopts it

    def _end(self, reason):
        self.state, self.reason, self.since = "cable_only", reason, None

    # -- the slow part -------------------------------------------------------------------------------------------

    def work(self, now=None):
        """The pairing, once; or one look at CoreDevice while the Wi-Fi link is awaited. True while more is to do."""
        with self.lock:
            state = self.state
        if state == "pairing":
            self._pair()
            return False
        if state != "waiting_tunnel":
            return False
        now = self.clock() if now is None else now
        if self.polled_at is not None and now - self.polled_at < POLL:
            return True
        self.polled_at = now
        try:
            peer = self.lister().get(self.udid.upper())
        except devicectl.DevicectlError:
            peer = None   # a hang or no answer: the deadline decides
        with self.lock:
            if self.state != "waiting_tunnel":
                return False
            if peer is not None and peer.os_version:
                self.os = peer.os_version
            if peer is not None and peer.network and peer.tunnel == "up":
                self.state, self.since = "ready", None
                return False
            if now - self.since >= TUNNEL_WAIT:
                self._end_tunnel_wait()
                return False
            return True

    def _pair(self):
        said_no = True   # the phone or this Mac said no and nothing changed on the phone: the noted record goes back
        try:
            on = self.pair_run()
            outcome = ("paired", None) if on is True else ("cable_only", "refused")
            said_no = on is not True
        except pair.PairError as error:
            outcome = ("after_trust", None) if error.code in ("needs_cable", "not_trusted") else \
                ("cable_only", "refused" if error.code == "refused" else "unknown")
            said_no = error.code in switch.SAID_NO
        except LookupError:
            outcome = ("cable_only", "needs_xcode")
        except RuntimeError:
            outcome = ("off", None)   # devices are off in this process: nothing was sent
        except Exception:  # noqa: BLE001 -- whatever happened, its outcome is unknown: never tried again
            outcome = ("cable_only", "unknown")
            said_no = False
        with self.lock:
            if said_no and self.before is not None:
                try:
                    switch.put_back(self.data_dir, self.udid, self.before)
                except Exception:  # noqa: BLE001 -- the noted record stays: an off clears it, which is safe
                    pass
            if self.state != "pairing":
                return
            state, reason = outcome
            if state == "cable_only":
                self._end(reason)
            else:
                self.state = state
                self.rearm = state == "after_trust"
                if state == "paired":
                    self._store()   # the yes holds at once, even when nothing reads setup again

    # -- what the setup body says --------------------------------------------------------------------------------

    def public(self):
        with self.lock:
            return view(self.state or "after_trust", self.reason, self.os, self.cable)


def view(state, reason=None, os=None, cable=False):
    """The setup body's ``wifi``. ValueError for a combination that can't happen."""
    if state not in STATES:
        raise ValueError(f"unknown Wi-Fi step state {state}")
    if (state == "cable_only") != (reason in REASONS):
        raise ValueError("a reason goes with cable_only, and only with it")
    parsed = version(os)
    return {"state": state, "reason": reason, "os": os if parsed else None,
            "hub": bool(parsed and parsed[0] >= HUB_FROM), "cable": bool(cable)}


OFF = view("off")

_flows = {}
_flows_lock = threading.Lock()


def flow(udid, data_dir, **kwargs):
    """The phone's step, made on its first read."""
    key = udid.upper()
    with _flows_lock:
        found = _flows.get(key)
        if found is None:
            found = _flows[key] = Flow(udid, data_dir, **kwargs)
        return found


def answer(udid, yes):
    """The person's answer for the phone whose step is asking (Flow.answer). LookupError when none is."""
    with _flows_lock:
        found = _flows.get(str(udid or "").upper())
    if found is None:
        raise LookupError("Mobster isn't asking about Wi-Fi for this iPhone now.")
    found.answer(yes)


def forget(udid=None):
    """Drop one phone's step, or every phone's (tests)."""
    with _flows_lock:
        if udid is None:
            _flows.clear()
        else:
            _flows.pop(udid.upper(), None)


def _run(item):
    """The worker: ``work`` until nothing is left, looking again every POLL seconds."""
    try:
        while item.work():
            time.sleep(POLL)
    finally:
        with item.lock:
            item.worker = None


def step(udid, data_dir, facts, *, spawn=None, env=None):
    if not transport_on(env) or not udid or data_dir is None:
        return dict(OFF)
    item = flow(udid, data_dir)
    if item.observe(facts):
        with item.lock:
            start = item.worker is None
            if start:
                item.worker = True
        if start:
            (spawn or _thread)(item)
    return item.public()


def _thread(item):
    threading.Thread(target=_run, args=(item,), name=f"mobster-wifi-setup-{item.udid[-6:]}", daemon=True).start()
