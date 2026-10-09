"""What CoreDevice sees: ``xcrun devicectl``, always with a timeout and a kill (SPEC §3.7 item 2).

``devicectl list devices`` can hang while it sets up a tunnel (Apple FB15184273), so every call runs in its own
process group and is killed when it overruns (8 s for a listing). It is the only supported way to read devicectl
(its ``--json-output`` file). No pymobiledevice3 is used (GPL-3.0).

The JSON field names are Apple's and not documented: ``connectionProperties.transportType`` ("wired" |
"localNetwork"), ``tunnelState`` ("connected" | "connecting" | "disconnected" | "unavailable") and the tunnel address
(``tunnelIPAddress``; ``tunnelIPAddressString`` also read). ``mobster wifi probe`` prints what the phone really
reports, so the owner's probe confirms them.

Under MOBSTER_NO_DEVICES=1 (tests, local CI) every call raises instead of running.
"""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time
from typing import Mapping, Optional

from .. import native_helpers

LIST_TIMEOUT = 8.0
BRING_UP_TIMEOUT = 20.0
DEVELOPER_TTL = 300.0
TUNNEL = {"connected": "up", "connecting": "connecting", "disconnected": "down", "disconnecting": "down",
          "unavailable": "down"}
ADDRESS_KEYS = ("tunnelIPAddress", "tunnelIPAddressString", "tunnelAddress")


class DevicectlError(RuntimeError):
    """devicectl couldn't answer. ``code``: "no_xcode" | "hang" | "failed"."""

    def __init__(self, message, code="failed"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Peer:
    """One phone as CoreDevice reports it."""
    udid: str
    name: Optional[str] = None
    transport: Optional[str] = None        # raw transportType: "wired", "localNetwork", ...
    tunnel: str = "unknown"                # "up" | "connecting" | "down" | "unknown"
    tunnel_state: Optional[str] = None     # raw tunnelState
    address: Optional[str] = None          # the tunnel address as reported (checked again before any use)
    pairing: Optional[str] = None
    developer_mode: Optional[str] = None
    boot: Optional[str] = None
    hostnames: tuple = ()
    fields: Mapping = field(default_factory=dict)   # connectionProperties as read, for the probe

    @property
    def network(self):
        return self.transport == "localNetwork"

    @property
    def wired(self):
        return self.transport == "wired"


def _text(value):
    return value if isinstance(value, str) and value else None


def parse(document):
    """{UDID (upper case): Peer} from devicectl's JSON (``list devices`` or ``device info details``). Entries without
    a UDID are skipped; anything malformed reads as no device."""
    result = document.get("result") if isinstance(document, dict) else None
    if not isinstance(result, dict):
        return {}
    devices = result.get("devices")
    if devices is None and isinstance(result.get("device"), dict):
        devices = [result["device"]]
    peers = {}
    for device in devices if isinstance(devices, list) else ():
        if not isinstance(device, dict):
            continue
        hardware = device.get("hardwareProperties") if isinstance(device.get("hardwareProperties"), dict) else {}
        properties = device.get("deviceProperties") if isinstance(device.get("deviceProperties"), dict) else {}
        connection = device.get("connectionProperties") if isinstance(device.get("connectionProperties"), dict) else {}
        udid = _text(hardware.get("udid")) or _text(device.get("udid"))
        if not udid:
            continue
        state = _text(connection.get("tunnelState"))
        address = next((_text(connection.get(key)) for key in ADDRESS_KEYS if _text(connection.get(key))), None)
        names = []
        for key in ("localHostnames", "potentialHostnames"):
            value = connection.get(key)
            names += [item for item in value if isinstance(item, str) and item] if isinstance(value, list) else []
        peers[udid.upper()] = Peer(
            udid=udid, name=_text(properties.get("name")), transport=_text(connection.get("transportType")),
            tunnel=TUNNEL.get(state, "unknown"), tunnel_state=state, address=address,
            pairing=_text(connection.get("pairingState")), developer_mode=_text(properties.get("developerModeStatus")),
            boot=_text(properties.get("bootState")), hostnames=tuple(dict.fromkeys(names)),
            fields={key: value for key, value in connection.items() if isinstance(value, (str, int, float, bool, list))})
    return peers


_developer = {"at": None, "dir": None}
_developer_lock = threading.Lock()


def developer_dir():
    """Xcode's developer folder (device_manager.developer_dir), looked up at most every 5 minutes; None without Xcode."""
    with _developer_lock:
        if _developer["at"] is None or time.monotonic() - _developer["at"] > DEVELOPER_TTL:
            from ..device_manager import developer_dir as lookup
            try:
                _developer["dir"] = lookup()[0]
            except Exception:
                _developer["dir"] = None
            _developer["at"] = time.monotonic()
        return _developer["dir"]


def command(args, developer):
    """The argv for ``xcrun devicectl <args>`` with Xcode's own xcrun."""
    xcrun = Path(developer) / "usr" / "bin" / "xcrun"
    return [str(xcrun) if xcrun.is_file() else "/usr/bin/xcrun", "devicectl", *args]


def run_json(args, timeout, *, developer=None, runner=None):
    """devicectl's JSON document for ``args``. Raises DevicectlError: "no_xcode", "hang" (killed after ``timeout``
    seconds) or "failed". ``runner(argv, env, timeout)`` replaces the process for tests."""
    native_helpers.require_devices()
    developer = developer or developer_dir()
    if not developer:
        raise DevicectlError("Xcode isn't installed or set up on this Mac", "no_xcode")
    with tempfile.TemporaryDirectory(prefix="mobster-devicectl-") as folder:
        output = Path(folder) / "result.json"
        argv = command([*args, "--json-output", str(output)], developer)
        env = {**os.environ, "DEVELOPER_DIR": developer}
        code, error = (runner or _run)(argv, env, timeout)
        try:
            document = json.loads(output.read_text())
        except (OSError, ValueError):
            document = None
    if document is None:
        raise DevicectlError(f"devicectl gave no answer (exit {code}): {error[-200:]}".strip(), "failed")
    outcome = (document.get("info") or {}).get("outcome") if isinstance(document, dict) else None
    if code != 0 and outcome != "success" and not parse(document):
        raise DevicectlError(f"devicectl failed (exit {code}): {error[-200:]}".strip(), "failed")
    return document


def _run(argv, env, timeout):
    """(exit code, the end of stderr). Its own process group, killed on overrun: DevicectlError "hang"."""
    with tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors,
                                       env=env, start_new_session=True)
        except OSError as error:
            raise DevicectlError(f"devicectl couldn't start: {error}", "no_xcode") from None
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill(process)
            raise DevicectlError(f"devicectl didn't answer in {timeout:g} s and was stopped", "hang") from None
        errors.seek(0)
        return code, errors.read()[-2000:].decode("utf-8", "replace")


def kill(process):
    """Kill a hung devicectl and everything it started."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def list_devices(timeout=LIST_TIMEOUT, **kwargs):
    """{UDID: Peer} for every device CoreDevice knows (on USB, over the network, or neither now)."""
    return parse(run_json(["list", "devices"], timeout, **kwargs))


def bring_up(udid, timeout=BRING_UP_TIMEOUT, **kwargs):
    """Ask CoreDevice for the phone's details, which brings its tunnel up when it can be; {UDID: Peer} as read."""
    from ..device_manager import UDID_PATTERN
    if not isinstance(udid, str) or not UDID_PATTERN.fullmatch(udid):
        raise ValueError("A UDID is 24 to 40 hex digits and dashes")
    return parse(run_json(["device", "info", "details", "--device", udid], timeout, **kwargs))
