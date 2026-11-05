"""`mobster wifi probe`: the owner's probe (SPEC §3.7, owner test plan items 1-4), whose result decides the release."""

import os
import signal
import socket
import subprocess
import time
from functools import partial
from pathlib import Path

from ..devices import wda_url
from ..transport import request_once
from . import devicectl, exposure, network
from .relay import Relay, check_target

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
START_WAIT = 120.0


def http_status(port, timeout=3.0):
    """WebDriverAgent's /status through 127.0.0.1:``port``: its JSON value, or None."""
    try:
        value = request_once(wda_url(port), "GET", "/status", timeout=timeout).get("value")
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def stream_answers(port, timeout=3.0):
    """Whether the MJPEG port behind 127.0.0.1:``port`` sends anything back to a request."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout) as sock:
            sock.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            return bool(sock.recv(1))
    except OSError:
        return False


def usbmux_network(udid, runner=None):
    """Whether ``idevice_id -n`` (read-only) lists the phone over the network: True, False, or None (no tool)."""
    from ..mac_tools import tool
    listing = tool("idevice_id")
    if not listing:
        return None
    output = (runner or partial(network.read_output, timeout=5))([listing, "-n"])
    if output is None:
        return None
    return udid.upper() in {line.strip().upper() for line in output.splitlines() if line.strip()}


# It binds only 127.0.0.1, never enters a passcode, and changes nothing on the phone but --start's own helper.
class Probe:
    """One probe of one phone. ``steps``: [{"key", "state", "title", "detail"}]; ``fields``: what CoreDevice said."""

    def __init__(self, *, udid, name, slot_port=None, data_dir=None, start=False, manager=None, out=None,
                 network_check=None):
        self.udid, self.name, self.slot_port = udid, name, slot_port
        self.network_check = network_check
        self.data_dir, self.start, self.manager = data_dir, start, manager
        self.out = out
        self.steps, self.fields, self.peer, self.address = [], {}, None, None

    def step(self, key, state, title, detail=""):
        item = {"key": key, "state": state, "title": title, "detail": detail}
        self.steps.append(item)
        if self.out is not None:
            self.out(item)
        return item

    @property
    def ok(self):
        needed = {"coredevice", "tunnel", "wda", "lan"}
        states = {item["key"]: item["state"] for item in self.steps}
        return needed <= set(states) and all(states[key] == OK for key in needed)

    def public(self):
        return {"device": {"udid": self.udid, "name": self.name}, "ok": self.ok, "steps": self.steps,
                "fields": self.fields, "tunnelAddress": self.address}

    # -- the steps --------------------------------------------------------------------------------------------------

    def run(self, usb_attached):
        usb = usb_attached(self.udid)
        self.step("usb", INFO, "USB", {True: "plugged in: unplug it to probe Wi-Fi", False: "not plugged in",
                                       None: "couldn't tell"}[usb])
        self._network()
        if not self._coredevice():
            return self
        if not self._tunnel():
            return self
        self._readers()
        wda = self._wda()
        if not wda and self.start:
            if self._launch():
                wda = self._wda()
        self._app_relay()
        self._lan()
        self._usbmux()
        return self

    def _network(self):
        from .words import NETWORK_PROBLEMS, problem
        try:
            result = (self.network_check or network.peer_check)()
        except Exception:  # noqa: BLE001
            result = {"state": "unknown"}
        state = result.get("state")
        code = NETWORK_PROBLEMS.get(state)
        if code:
            found = problem(code, self.name)
            self.step("network", WARN, "This Mac's network", f"{found['message']} {found['fix']}")
        elif state == "ok":
            self.step("network", OK, "This Mac's network", f"devices on it can see each other "
                                                           f"({result.get('interface')})")
        else:
            self.step("network", INFO, "This Mac's network", {"offline": "this Mac isn't on a network"}.get(
                state, "couldn't tell whether devices on it can see each other"))

    def _coredevice(self):
        try:
            peers = devicectl.list_devices()
        except devicectl.DevicectlError as error:
            self.step("coredevice", FAIL, "CoreDevice", str(error))
            return False
        self.peer = peers.get(self.udid.upper())
        if self.peer is None:
            self.step("coredevice", FAIL, "CoreDevice", "doesn't list this iPhone at all. Pair it with the cable in "
                                                        "Setup, then try again.")
            return False
        self.fields = dict(self.peer.fields)
        seen = ", ".join(f"{key}={value}" for key, value in sorted(self.fields.items())
                         if not isinstance(value, list)) or "no connection fields"
        self.step("fields", INFO, "Fields CoreDevice reports", seen)
        if self.peer.wired:
            self.step("coredevice", WARN, "CoreDevice", "sees it on USB (transportType wired). Unplug it and probe "
                                                        "again.")
            return False
        if not self.peer.network:
            self.step("coredevice", FAIL, "CoreDevice", f"doesn't see it over the network (transportType "
                                                        f"{self.peer.transport or 'missing'}). Turn on Wi-Fi with "
                                                        "`mobster wifi on` with the cable, keep the phone unlocked "
                                                        "and on the same Wi-Fi as this Mac.")
            return False
        self.step("coredevice", OK, "CoreDevice", "sees it over the network (transportType localNetwork"
                  + (f", iOS {self.peer.os_version}" if self.peer.os_version else "") + ")")
        return True

    def _tunnel(self):
        peer = self.peer
        if peer.tunnel != "up":
            try:
                found = devicectl.bring_up(self.udid)
                peer = found.get(self.udid.upper()) or peer
                if peer.tunnel != "up":
                    peer = devicectl.list_devices().get(self.udid.upper()) or peer
            except devicectl.DevicectlError as error:
                self.step("tunnel", FAIL, "Encrypted tunnel", f"couldn't be brought up: {error}")
                return False
        self.peer = peer
        self.fields = dict(peer.fields) or self.fields
        if peer.tunnel != "up":
            self.step("tunnel", FAIL, "Encrypted tunnel", f"not up (tunnelState {peer.tunnel_state or 'missing'})")
            return False
        try:
            address = check_target(peer.address)
        except ValueError:
            self.step("tunnel", FAIL, "Encrypted tunnel", f"up, but its address ({peer.address or 'missing'}) isn't "
                                                          "an fd00::/8 tunnel address, so Mobster won't use it")
            return False
        interface, problem = network.tunnel_route(address)
        if problem:
            self.step("tunnel", FAIL, "Encrypted tunnel", f"up, but {problem}, so Mobster won't use it")
            return False
        self.address = address
        self.step("tunnel", OK, "Encrypted tunnel", f"up, the phone's end is {address} (through {interface})")
        return True

    def _readers(self):
        lines, clear = [], True
        for name, understood in (("lockState", self._lock_line), ("apps", self._apps_line)):
            try:
                document = devicectl.run_json(["device", "info", name, "--device", self.udid], devicectl.APPS_TIMEOUT)
                result = document.get("result") if isinstance(document, dict) else None
                keys = ", ".join(sorted(result)) if isinstance(result, dict) else "no result"
                line = understood(document)
            except (devicectl.DevicectlError, RuntimeError) as error:
                line, keys = str(error), "none"
            if line is None:
                clear = False
                line = "unreadable"
            lines.append(f"{name}: {line} (fields: {keys})")
        self.step("readers", INFO if clear else WARN, "Reads through CoreDevice", "; ".join(lines))

    @staticmethod
    def _lock_line(document):
        locked = devicectl.lock_state(document)
        return None if locked is None else "locked" if locked else "unlocked"

    @staticmethod
    def _apps_line(document):
        try:
            return (
                f"{len(devicectl.parse_apps(document))} user apps, {len(devicectl.parse_apps(document, 'system'))} "
                "Apple apps")
        except devicectl.DevicectlError:
            return None

    def _wda(self):
        """WebDriverAgent on the tunnel address, through a relay on 127.0.0.1 (any free port)."""
        relay = Relay(self.address, [(0, 8100), (0, 9100)], name="wifi-probe")
        try:
            relay.start()
        except OSError as error:
            self.step("wda", FAIL, "WebDriverAgent over the tunnel", f"the probe's relay couldn't start: {error}")
            return False
        try:
            api, video = relay.ports
            status = http_status(api)
            if status is None:
                self.step("wda", WARN, "WebDriverAgent over the tunnel",
                          "doesn't answer on [tunnel]:8100 (yet). Start Mobster's helper over Wi-Fi: the Mac app "
                          "with Wi-Fi for iPhones on, or this probe with --start.")
                return False
            ready = status.get("ready") is True
            streams = stream_answers(video)
            self.step("wda", OK if ready else WARN, "WebDriverAgent over the tunnel",
                      f"/status answers through 127.0.0.1 ({'ready' if ready else 'not ready'}); video port "
                      f"{'answers' if streams else 'does not answer'}")
            return ready
        finally:
            relay.stop()

    def _launch(self):
        """--start: launch Mobster's helper over Wi-Fi for this probe, and wait for it."""
        from ..device_manager import wifi_runner_command
        from ..mac_tools import tool
        from . import xctestrun
        manager = self.manager
        if manager is None or not manager.built():
            self.step("runner", FAIL, "Mobster's helper over Wi-Fi", "isn't built for this iPhone. Set it up with "
                                                                     "the cable first.")
            return False
        xcodebuild = tool("xcodebuild")
        if not xcodebuild:
            self.step("runner", FAIL, "Mobster's helper over Wi-Fi", "Xcode isn't installed or set up")
            return False
        try:
            plan = xctestrun.write(manager.derived / "Build" / "Products", manager.derived / xctestrun.FOLDER,
                                   self.address)
        except (LookupError, ValueError) as error:
            self.step("runner", FAIL, "Mobster's helper over Wi-Fi", str(error))
            return False
        log_path = Path(manager.logs) / "wifi-probe.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "ab") as log:
            process = subprocess.Popen(wifi_runner_command(xcodebuild, plan, self.udid), stdout=log,
                                       stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
                                       env=manager.runner_env())
        self._process = process
        relay = Relay(self.address, [(0, 8100)], name="wifi-probe")
        relay.start()
        try:
            deadline = time.monotonic() + START_WAIT
            while time.monotonic() < deadline and process.poll() is None:
                if (http_status(relay.ports[0], timeout=2) or {}).get("ready") is True:
                    self.step("runner", OK, "Mobster's helper over Wi-Fi",
                              f"started with USE_IP={self.address} (log: {log_path.name})")
                    return True
                time.sleep(2)
        finally:
            relay.stop()
        self.step("runner", FAIL, "Mobster's helper over Wi-Fi",
                  f"didn't answer within {int(START_WAIT)} s. See {log_path} for xcodebuild's output.")
        return False

    def stop_launched(self):
        process = getattr(self, "_process", None)
        if process is None or process.poll() is not None:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
                process.wait(timeout=5)
                return
            except (ProcessLookupError, PermissionError):
                return
            except subprocess.TimeoutExpired:
                continue

    def _app_relay(self):
        if not self.slot_port:
            return
        status = http_status(self.slot_port, timeout=2)
        self.step("app_relay", INFO, "The Mac app's relay",
                  f"127.0.0.1:{self.slot_port} " + ("answers" if status is not None else "doesn't answer (fine when "
                                                                                       "the app isn't running it)"))

    def _lan(self):
        names = list(self.peer.hostnames) if self.peer is not None else []
        addresses, hits = exposure.exposed(names)
        if hits:
            self.step("lan", FAIL, "The phone's Wi-Fi address",
                      "WebDriverAgent ANSWERS on " + ", ".join(hits) + ". Mobster stops a helper that does this "
                      "when it starts it. Turn Wi-Fi off for this phone (`mobster wifi off`) and report this.")
        elif addresses:
            self.step("lan", OK, "The phone's Wi-Fi address",
                      f"{', '.join(addresses)}: 8100 and 9100 don't answer (also check from another machine)")
        else:
            self.step("lan", WARN, "The phone's Wi-Fi address",
                      "couldn't be found. From another machine on the same Wi-Fi, check that `nc -vz <the phone's "
                      "Wi-Fi address> 8100` and 9100 fail.")

    def _usbmux(self):
        listed = usbmux_network(self.udid)
        self.step("usbmux", INFO, "usbmuxd over the network",
                  {True: "lists the phone (evidence only; Mobster never uses it for WebDriverAgent)",
                   False: "doesn't list the phone", None: "couldn't tell (idevice_id is missing)"}[listed])


def describe(item):
    glyph = {OK: "✓", WARN: "!", FAIL: "✗", INFO: "·"}[item["state"]]
    return f"  {glyph} {item['title']:<32} {item['detail']}"
