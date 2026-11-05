"""The transport monitor: the service that keeps each phone on the cable or on Wi-Fi (SPEC §3.7 items 2, 5-7)."""

import logging
import threading
import time

from . import devicectl, store, transport
from .relay import check_target

log = logging.getLogger("mobster.wireless")

TICK = 10.0
SLOW = 60.0               # devicectl at most this often while every phone with Wi-Fi on is on the cable
START_TIMEOUT = 120.0     # a runner started over Wi-Fi answers within this, or it is stopped and retried
LOST_AFTER = 30.0         # a Wi-Fi runner that stops answering for this long (and no task) is restarted
NETWORK_EVERY = 60.0      # the network check, at most this often while a phone with Wi-Fi on can't be found


def task_on(runtime, phone, udid):
    # when it can't be told, it counts as busy
    try:
        keys = dict.fromkeys(key for key in (udid, getattr(phone, "key", None)) if key)
        if any(runtime.device_busy(key) for key in keys):
            return True
    except Exception:  # noqa: BLE001
        return True
    in_use = getattr(getattr(runtime, "fleet", None), "in_use", None)
    if not callable(in_use):
        return False
    try:
        return bool(in_use(phone))
    except Exception:  # noqa: BLE001
        return True


class Monitor:
    def __init__(self, runtime, *, lister=None, bring_up=None, attached=None, nwi=None, clock=None, publish=None,
                 through=None, network_check=None):
        from ..mac_tools import usb_attached
        from . import network
        self.runtime = runtime
        self.through = through or network.through_tunnel
        self.network_check = network_check or network.peer_check
        self.network = None        # the last network.peer_check result, or None before one ran
        self.network_at = None
        self.lister = lister or devicectl.list_devices
        self.bring_up = bring_up or devicectl.bring_up
        self.attached = attached or usb_attached
        self.nwi = nwi or (lambda: network.signature(network.nwi()))
        self.clock = clock or time.monotonic
        self.publish = publish or getattr(runtime, "publish", None) or (lambda topic, event: None)
        self.backoffs = {}         # UDID -> transport.Backoff
        self.starting = {}         # UDID -> when a Wi-Fi start began
        self.silent = {}           # UDID -> since when a Wi-Fi runner stopped answering
        self.peers = {}            # UDID -> devicectl.Peer
        self.peers_at = None
        self.devicectl_problem = None
        self.net = None
        self.known = set()
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.kicked = threading.Event()
        self.thread = None

    # -- the service ----------------------------------------------------------------------------------------------

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self):
        if self.running:
            return
        self.stopping.clear()
        self.thread = threading.Thread(target=self._loop, name="mobster-wifi-monitor", daemon=True)
        self.thread.start()

    def stop(self, timeout=2.0):
        self.stopping.set()
        self.kicked.set()
        if self.thread is not None:
            self.thread.join(timeout)

    def kick(self, udid=None):
        """Look again now; with ``udid``, forget its backoff first (Try again, Wi-Fi just turned on)."""
        if udid:
            self.backoff(udid).reset()
            self.peers_at = None
        self.kicked.set()

    def _loop(self):
        while not self.stopping.is_set() and not getattr(self.runtime, "closing", False):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 -- the monitor never stops Mobster
                log.exception("the Wi-Fi monitor's check failed")
            self.kicked.wait(self.pause())
            self.kicked.clear()

    def pause(self):
        now = self.clock()
        due = [backoff.next_at - now for backoff in list(self.backoffs.values()) if backoff.failures]
        return max(0.5, min([TICK, *due]))

    def backoff(self, udid):
        return self.backoffs.setdefault(str(udid).upper(), transport.Backoff())

    # -- one look at every phone ----------------------------------------------------------------------------------

    def tick(self):
        with self.lock:
            if getattr(self.runtime, "closing", False):
                return
            fleet = getattr(self.runtime, "fleet", None)
            if fleet is None or fleet.data_dir is None:
                return
            now = self.clock()
            phones = fleet.usb_managers()
            enabled = store.enabled_udids(fleet.data_dir)
            signature = self.nwi()
            if signature is not None and self.net is not None and signature != self.net:
                # Another Wi-Fi, a new address, a VPN: the tunnel is set up again, so look again at once.
                for backoff in list(self.backoffs.values()):
                    backoff.reset()
                self.peers_at = None
                self.network_at = None
            if signature is not None:
                self.net = signature
            usb = {udid.upper(): self._usb(udid) for _, udid, _ in phones}
            off_cable = [udid for _, udid, _ in phones if udid.upper() in enabled and usb[udid.upper()] is not True]
            due = SLOW if not off_cable else TICK * 0.9
            if enabled and (self.peers_at is None or now - self.peers_at >= due):
                self._refresh(now)
            away = [udid for udid in off_cable if not self.peers.get(udid.upper()) or
                    not self.peers[udid.upper()].network]
            if away and (self.network_at is None or now - self.network_at >= NETWORK_EVERY):
                self._check_network(now)
            managed = set()
            for phone, udid, manager in phones:
                managed.add(udid.upper())
                try:
                    self._step(phone, udid, manager, udid.upper() in enabled, usb[udid.upper()], now)
                except Exception:  # noqa: BLE001 -- one phone never stops the others
                    log.exception("the Wi-Fi monitor's check of one phone failed")
            for gone in self.known - managed:
                transport.forget(gone)
            self.known = managed

    def busy(self, phone, udid):
        return task_on(self.runtime, phone, udid)

    def _usb(self, udid):
        try:
            return self.attached(udid)
        except Exception:
            return None

    def _refresh(self, now):
        self.peers_at = now
        try:
            self.peers = self.lister()
            self.devicectl_problem = None
        except devicectl.DevicectlError as error:
            self.peers = {}
            if error.code == "hang":
                self.devicectl_problem = "devicectl_hang"
            else:
                self.devicectl_problem = "no_xcode" if error.code == "no_xcode" else "not_seen"
        except RuntimeError:
            self.peers, self.devicectl_problem = {}, "not_seen"   # MOBSTER_NO_DEVICES=1

    def _check_network(self, now):
        self.network_at = now
        try:
            self.network = self.network_check()
        except Exception:  # noqa: BLE001 -- a check that fails says nothing
            log.exception("the Wi-Fi monitor's network check failed")
            self.network = None

    def network_problem(self):
        from .words import NETWORK_PROBLEMS
        return NETWORK_PROBLEMS.get((self.network or {}).get("state"))

    def _tunnel(self, udid):
        peer = self.peers.get(udid.upper())
        if peer is None or not peer.network:
            return ("unknown" if peer is None else "down"), None, False
        address = None
        if peer.tunnel == "up" and peer.address:
            try:
                address = check_target(peer.address)
            except ValueError:
                return "up", None, True
            # only an address this Mac reaches through its CoreDevice tunnel, never through Wi-Fi, Ethernet or a VPN
            if not self.through(address):
                return "up", None, True
        return peer.tunnel, address, True

    @staticmethod
    def _now(manager):
        """(what the phone's runner uses now, or None when it isn't running)."""
        running = manager.runner is not None and manager.runner.running
        return getattr(manager, "transport", None) if running else None

    def _step(self, phone, udid, manager, enabled, usb, now):
        key = udid.upper()
        current = self._now(manager)
        tunnel, address, seen = self._tunnel(udid) if enabled else ("unknown", None, False)
        reachable = bool(current == "wifi" and address is None and manager.wda_ready(timeout=1.5))
        route_lost = False
        if current == "wifi" and enabled and usb is not True and reachable and address is None:
            # Answering over Wi-Fi: devicectl not answering (or not listing it this time) doesn't end a working link,
            # while this Mac still reaches its address through the tunnel.
            route_lost = not self.through(manager.wifi_address)
            if not route_lost:
                address, tunnel = manager.wifi_address, "up"
        desired = transport.choose(usb is True, enabled, address is not None)
        busy = self.busy(phone, udid)
        step = transport.next_step(current, desired, busy)
        if step == "stay" and current == "wifi" and address and manager.wifi_address != address and not busy:
            step = "switch"   # the tunnel came back with another address: start again on it
        backoff = self.backoff(udid)
        problem, waiting = None, step == "wait"

        if step == "switch" and desired == "wifi":
            problem, _started = self._start_wifi(udid, manager, address, backoff, now)
        elif step == "switch" and current == "wifi":
            manager.stop_runner()
            self.starting.pop(key, None)
            self.silent.pop(key, None)
            if desired == "usb":
                manager.autostart()          # the cable's runner, as at launch
            else:
                backoff.fail(now)
                problem = "route" if route_lost else "tunnel_down" if seen else (self.devicectl_problem or "not_seen")
        elif desired is None and enabled and usb is not True and current != "wifi" and not busy:
            problem = self._bring_up(udid, tunnel, seen, backoff, now)

        current = self._now(manager)
        reachable = bool(current is not None and manager.wda_ready(timeout=1.5))
        if current == "wifi":
            problem = self._watch_wifi(udid, manager, reachable, busy, backoff, now) or problem
            current = self._now(manager)
            reachable = reachable and current == "wifi"
        # Switching: a change waits for a task, or a start over Wi-Fi hasn't answered yet.
        switching = waiting or (current == "wifi" and key in self.starting and not reachable)
        if not enabled:
            problem = None
        elif problem == "not_seen":
            problem = self.network_problem() or problem
        before, after = transport.update(udid, transport=current, tunnel=tunnel if enabled else "unknown",
                                         reachable=reachable, problem=problem, switching=bool(switching))
        if before is None or (before.transport, before.reachable) != (after.transport, after.reachable):
            self.publish("devices", {"event": "transport_changed", "device": udid, "transport": after.transport,
                                     "reachable": after.reachable})

    def _start_wifi(self, udid, manager, address, backoff, now):
        if not manager.settings().get("autostart"):
            return None, False   # stopped in Setup: stays stopped on Wi-Fi too
        if not backoff.ready(now):
            return ("runner" if backoff.failures else None), False
        try:
            manager.start_wifi_runner(address, self.peers[udid.upper()].udid)
        except LookupError as error:
            backoff.fail(now)
            text = str(error)
            return ("identity" if "different iPhone" in text else "address" if "isn't available" in text else
                    "port_busy" if "port" in text else "not_set_up" if "Build" in text or "Setup" in text else
                    "runner"), False
        self.starting[udid.upper()] = now
        self.silent.pop(udid.upper(), None)
        return None, True

    def _watch_wifi(self, udid, manager, reachable, busy, backoff, now):
        key = udid.upper()
        if reachable:
            self.starting.pop(key, None)
            self.silent.pop(key, None)
            backoff.reset()
            return None
        if busy:
            return None
        started = self.starting.get(key)
        if started is not None:
            if now - started < START_TIMEOUT:
                return None
        else:
            since = self.silent.setdefault(key, now)
            if now - since < LOST_AFTER:
                return None
        manager.stop_runner()
        self.starting.pop(key, None)
        self.silent.pop(key, None)
        backoff.fail(now)
        return "runner"

    def _bring_up(self, udid, tunnel, seen, backoff, now):
        """Unplugged with Wi-Fi on and no usable tunnel: ask CoreDevice to bring it up, at the backoff's pace."""
        if self.devicectl_problem in ("no_xcode", "devicectl_hang"):
            return self.devicectl_problem
        if not seen:
            return "not_seen"
        if tunnel == "up":
            return "address"   # up, but its address isn't a tunnel address: never used
        if not backoff.ready(now):
            return "tunnel_down"
        try:
            self.peers.update(self.bring_up(udid))
            self.peers_at = None   # read the list again next time
        except devicectl.DevicectlError as error:
            backoff.fail(now)
            if error.code == "hang":
                return "devicectl_hang"
            return "tunnel_down"
        except (RuntimeError, ValueError):
            backoff.fail(now)
            return "tunnel_down"
        self.kicked.set()   # look again soon: the tunnel may be up now
        return "tunnel_down"

    def retire(self):
        with self.lock:
            fleet = getattr(self.runtime, "fleet", None)
            for phone, udid, manager in (fleet.usb_managers() if fleet is not None else ()):
                if getattr(manager, "transport", None) == "wifi" and not self.busy(phone, udid):
                    try:
                        manager.stop_runner()
                        manager.autostart()
                    except Exception:  # noqa: BLE001
                        log.exception("couldn't stop a runner on Wi-Fi")
                transport.forget(udid)
            self.known = set()
