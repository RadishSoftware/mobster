"""A check or an MCP session on one named device (SPEC §3): the simulator manager's interface, for one device."""

import base64
import io
import json
import threading
import urllib.request
from urllib.parse import quote

KIND_WORDS = {"usb": "iPhone", "simulator": "simulator", "wda": "device"}


def _sim_api():
    from .sim.models import AppInfo, SimError, SimLease, SimTarget
    return AppInfo, SimError, SimLease, SimTarget


class PinnedSimulator:
    """The simulator manager, handing out the one simulator ``udid`` names. Everything else is the manager's."""

    def __init__(self, manager, udid):
        self._manager, self.udid = manager, udid

    def acquire(self, device=None, runtime=None, *, timeout=600, cancelled=None):
        return self._manager.acquire_udid(self.udid, timeout=timeout, cancelled=cancelled)

    def __getattr__(self, name):
        return getattr(self._manager, name)


class WdaDevice:
    """A device driven only through WebDriverAgent, as the simulator manager's interface for VerifyRun and MCP."""

    # Someone's own phone, not a simulator Mobster made: a Smart check here refuses every declared commit
    # (verify.smart.check_approver). A WDA address may be a phone too, so it counts as one.
    real_device = True

    def __init__(self, record, lease=None, opener=urllib.request.urlopen):
        self.record = record
        self.url = (record.get("wdaUrl") or "").rstrip("/")
        self.word = KIND_WORDS.get(record.get("kind"), "device")
        self._lease = lease
        self._open = opener
        self._session = None
        self._lock = threading.Lock()

    # -- plumbing -----------------------------------------------------------------------------------------

    def _request(self, method, path, body=None, timeout=30):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.url + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"} if data is not None else {})
        with self._open(request, timeout=timeout) as response:
            return json.load(response)

    def session(self):
        """WDA's current session, or a new one."""
        with self._lock:
            status = self._request("GET", "/status", timeout=10)
            if status.get("sessionId"):
                self._session = status["sessionId"]
            elif not self._session:
                created = self._request("POST", "/session", {"capabilities": {"alwaysMatch": {}}})
                self._session = created.get("sessionId") or (created.get("value") or {}).get("sessionId")
            return self._session

    def _in_session(self, method, path, body=None, timeout=30):
        return self._request(method, f"/session/{quote(self.session(), safe='')}{path}", body, timeout)

    def _error(self, kind, message, fix=""):
        _, SimError, _, _ = _sim_api()
        return SimError(kind, message, fix)

    # -- the simulator manager's interface ---------------------------------------------------------------

    def acquire(self, device=None, runtime=None, *, timeout=600, cancelled=None):
        """The device's lease (one Mobster process at a time) once its WebDriverAgent answers."""
        _, SimError, SimLease, SimTarget = _sim_api()
        from .verify.actions import ActionNotDispatched
        name = self.record.get("name") or self.record.get("id")

        def refuse(kind, message, fix=""):
            error = SimError(kind, message, fix)
            raise ActionNotDispatched(error) from error

        def check_cancelled():
            if cancelled is not None and cancelled():
                refuse("stopped", "The suite was stopped before this device ran.")

        check_cancelled()
        if not self.url:
            refuse("wda", f"“{name}” isn't set up yet. {self.record.get('reason') or ''}".strip(),
                   "Set it up in the Mobster app, or with `mobster serve --manage-device`.")
        from .journal import JournalError, Lease, LeaseHeld
        try:
            lease = (self._lease or (lambda url: Lease.device(url, udid=self.record.get("udid"))))(self.url)
        except LeaseHeld as held:
            check_cancelled()
            refuse("busy", (f"{held.owner} is driving “{name}”." if held.owner else
                            f"“{name}” is in use by another Mobster app or command."),
                   "Wait for its task to finish, or stop it, then try again.")
        except JournalError as error:
            refuse("environment", f"Mobster couldn't take the device's lock: {error}")
        try:
            check_cancelled()
            try:
                status = self._request("GET", "/status", timeout=5)
                if (status.get("value") or {}).get("ready") is not True:
                    raise ValueError
            except Exception:
                refuse("wda", f"Nothing answers at “{name}”'s WebDriverAgent.",
                       "Keep the iPhone unlocked and connected, and start its runner in the Mobster app "
                       "or with `mobster serve --manage-device`." if self.record.get("kind") == "usb" else
                       "Start its WebDriverAgent, then try again.")
            check_cancelled()
            runtime_name = f"iOS {self.record['ios']}" if self.record.get("ios") else ""
            target = SimTarget(udid=self.record.get("udid") or self.record.get("id") or "", name=name,
                               device_type=self.record.get("modelName") or self.record.get("model") or "iPhone",
                               runtime=runtime_name, wda_url=self.url, mjpeg_url=self.record.get("mjpegUrl") or "",
                               xctestrun="")
            return SimLease(target, [lease])
        except BaseException:
            lease.close()
            raise

    def app_info(self, app_path):
        raise self._error("install", f"A {self.word} can't install a simulator build ({app_path}).",
                          "Pass bundle_id for an app already on it.")

    def install(self, target, app_path):
        return self.app_info(app_path)

    def installed_app(self, target, bundle_id):
        AppInfo, _, _, _ = _sim_api()
        return AppInfo(bundle_id=bundle_id, name=bundle_id, version="", path=None)

    def reset(self, target, bundle_id, level, app_path=None):
        if level != "none":
            raise self._error("install", f"Mobster never clears an app's data on a {self.word}.",
                              "Pass reset: none (the app keeps its data).")

    def launch(self, target, bundle_id, args=(), env=None):
        # WDA only brings a running app forward, and VerifyRun waits 15 s for a new process: start a fresh one
        self.terminate(target, bundle_id)
        try:
            self._in_session("POST", "/wda/apps/launch",
                             {"bundleId": bundle_id, "arguments": list(args or ()), "environment": dict(env or {})},
                             timeout=60)
        except Exception as error:
            raise self._error("launch", f"{bundle_id} didn't launch on the {self.word} ({type(error).__name__}).",
                              "Check that the app is installed on it, and that it is unlocked.") from None

    def appearance(self, name=None):
        if name is None:
            info = self._in_session("GET", "/wda/device/info", timeout=10)
            style = (info.get("value") or {}).get("userInterfaceStyle")
            return style if style in ("light", "dark") else None
        self._request("POST", "/wda/device/appearance", {"name": name}, timeout=15)

    def terminate(self, target, bundle_id):
        self._in_session("POST", "/wda/apps/terminate", {"bundleId": bundle_id}, timeout=30)

    def open_url(self, target, url):
        try:
            self._in_session("POST", "/url", {"url": url}, timeout=20)
        except Exception as error:
            raise self._error("launch", f"The {self.word} didn't open the link ({type(error).__name__}).") from None

    def screenshot(self, target, path, *, max_width=None, quality=75):
        from pathlib import Path

        from PIL import Image
        data = self._request("GET", "/screenshot", timeout=20).get("value")
        image = Image.open(io.BytesIO(base64.b64decode(data))).convert("RGB")
        if str(path).lower().endswith(".png"):   # a baseline compares pixels: lossless, at full size
            image.save(path, "PNG")
            return Path(path)
        if max_width and image.width > max_width:
            image = image.resize((max_width, round(image.height * max_width / image.width)))
        image.save(path, "JPEG", quality=quality)
        return Path(path)

    def restarter(self, target):
        return None  # its runner is kept up by whoever started it

    def evidence_tools(self, target):
        # a WebDriverAgent address has none: Mobster can't tell which phone it is
        if self.record.get("kind") != "usb" or not self.record.get("udid"):
            return None
        from .native_helpers import devices_allowed
        if not devices_allowed():
            return None
        from .verify.evidence import PhoneTools
        return PhoneTools(self.record["udid"], network=self.record.get("transport") == "wifi")


def manager_for(record, simulators=None, progress=None):
    if record.get("kind") == "android":
        from .android.manager import AndroidManager
        from .paths import dev_data_dir
        return AndroidManager(dev_data_dir(), name=record["serial"], phones=True)
    if record.get("kind") == "simulator":
        if simulators is None:
            from .sim import SimulatorManager
            simulators = SimulatorManager(progress=progress)
        return PinnedSimulator(simulators, record["udid"])
    return WdaDevice(record)


def named_device(name, records=None):
    from . import devices
    if not name:
        return None
    try:
        return devices.resolve(devices.discover(probe=False) if records is None else records, name)
    except devices.DeviceNotFound:
        return None
