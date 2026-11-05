"""In-memory simctl, WebDriverAgent and device lease fakes for the simulator manager's tests."""

import contextlib
import json
import plistlib
import tempfile
import uuid
from pathlib import Path
from unittest import mock

from mobile_agent.journal import LeaseHeld
from mobile_agent.sim import api, apps
from mobile_agent.sim.governor import Governor, Pressure
from mobile_agent.sim.simctl import Result
from mobile_agent.sim.wda import Listener, WebDriverAgent
from mobile_agent.sim.xcode import Tools
from mobile_agent.tests.fakes import Clock

RUNTIME_ID = "com.apple.CoreSimulator.SimRuntime.iOS-26-4"
OLD_RUNTIME_ID = "com.apple.CoreSimulator.SimRuntime.iOS-18-6"
DEVICE_TYPES = [
    {"name": "iPhone 17 Pro", "identifier": "com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro",
     "productFamily": "iPhone", "minRuntimeVersion": 1703936},
    {"name": "iPhone Air", "identifier": "com.apple.CoreSimulator.SimDeviceType.iPhone-Air",
     "productFamily": "iPhone", "minRuntimeVersion": 1703936},
    {"name": "iPhone 16 Pro", "identifier": "com.apple.CoreSimulator.SimDeviceType.iPhone-16-Pro",
     "productFamily": "iPhone", "minRuntimeVersion": 1179648},
    {"name": "iPhone 15 Pro", "identifier": "com.apple.CoreSimulator.SimDeviceType.iPhone-15-Pro",
     "productFamily": "iPhone", "minRuntimeVersion": 1114112},
    {"name": "iPad Pro 13-inch (M5)", "identifier": "com.apple.CoreSimulator.SimDeviceType.iPad-Pro-13-M5",
     "productFamily": "iPad", "minRuntimeVersion": 1703936},
]
RUNTIMES = [
    {"name": "iOS 26.4", "version": "26.4", "identifier": RUNTIME_ID, "isAvailable": True, "platform": "iOS",
     "supportedDeviceTypes": [{"name": item["name"]} for item in DEVICE_TYPES]},
    {"name": "iOS 18.6", "version": "18.6", "identifier": OLD_RUNTIME_ID, "isAvailable": True, "platform": "iOS",
     "supportedDeviceTypes": [{"name": "iPhone 16 Pro"}, {"name": "iPhone 15 Pro"}]},
    {"name": "iOS 27.0", "version": "27.0", "identifier": "com.apple.CoreSimulator.SimRuntime.iOS-27-0",
     "isAvailable": False, "platform": "iOS", "supportedDeviceTypes": []},
    {"name": "watchOS 26.4", "version": "26.4", "identifier": "com.apple.CoreSimulator.SimRuntime.watchOS-26-4",
     "isAvailable": True, "platform": "watchOS"},
]
TOOLS = Tools(xcodebuild="/Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild", git="/usr/bin/git",
              version="26.4", build="17E192", env={"PATH": "/usr/bin:/bin"})


def udid():
    return str(uuid.uuid4()).upper()


class FakeSimctl:
    def __init__(self, devices_root):
        self.root = Path(devices_root)
        self.devices = {}
        self.calls = []
        self.envs = []
        self.fail = {}
        self.hooks = {}
        self.containers = {}
        self.device_types = [dict(item) for item in DEVICE_TYPES]
        self.runtimes = [dict(item) for item in RUNTIMES]

    def add(self, name, state="Shutdown", runtime=RUNTIME_ID,
            device_type="com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro", identifier=None):
        identifier = identifier or udid()
        self.devices[identifier] = {"udid": identifier, "name": name, "state": state,
                                    "deviceTypeIdentifier": device_type, "isAvailable": True, "runtime": runtime,
                                    "dataPath": str(self.root / identifier / "data")}
        (self.root / identifier / "data").mkdir(parents=True, exist_ok=True)
        return identifier

    def simctl_calls(self, command):
        return [call[3:] for call in self.calls if call[:3] == [api.XCRUN, "simctl", command]]

    def listing(self):
        grouped = {}
        for device in self.devices.values():
            item = {key: value for key, value in device.items() if key != "runtime"}
            grouped.setdefault(device["runtime"], []).append(item)
        return {"devicetypes": self.device_types, "runtimes": self.runtimes, "devices": grouped}

    def __call__(self, argv, timeout=60, env=None, done=None):
        argv = [str(part) for part in argv]
        self.calls.append(argv)
        self.envs.append(env)
        if argv[:2] != [api.XCRUN, "simctl"]:
            return Result(0, "", "")
        command, args = argv[2], argv[3:]
        queued = self.fail.get(command)
        if queued:
            return queued.pop(0)
        if command == "list":
            data = self.listing()
            if "devices" in args:
                data = {"devices": data["devices"]}
            return Result(0, json.dumps(data), "")
        if command == "create":
            name, kind, runtime = args
            identifier = self.add(name, device_type=kind, runtime=runtime)
            return Result(0, identifier + "\n", "")
        device = self.devices.get(args[0]) if args else None
        if command in {"boot", "shutdown", "erase", "delete", "bootstatus"} and device is None:
            return Result(148, "", "Invalid device: " + (args[0] if args else ""))
        if command == "boot":
            if device["state"] == "Booted":
                return Result(149, "", "Unable to boot device in current state: Booted")
            device["state"] = "Booted"
        elif command == "shutdown":
            if device["state"] == "Shutdown":
                return Result(149, "", "Unable to shutdown device in current state: Shutdown")
            device["state"] = "Shutdown"
        elif command == "delete":
            del self.devices[args[0]]
        elif command == "get_app_container":
            key = (args[0], args[1], args[2] if len(args) > 2 else "app")
            if key not in self.containers:
                return Result(2, "", "No such file or directory")
            return Result(0, self.containers[key] + "\n", "")
        elif command == "io":
            from PIL import Image
            Image.new("RGB", (1206, 2622), (20, 40, 60)).save(args[-1], "PNG")
        if command in self.hooks:
            self.hooks[command](args)
        return Result(0, "", "")


class FakeLease:
    def __init__(self, leases, url):
        self.leases, self.url = leases, url
        self.closed = False

    def close(self):
        if not self.closed:
            self.closed = True
            self.leases.held.discard(self.url)


class FakeLeases:
    def __init__(self):
        self.held = set()

    def __call__(self, url):
        if url in self.held:
            raise LeaseHeld("Another Mobster process already owns this resource")
        self.held.add(url)
        return FakeLease(self, url)


class FakeAlertHTTP:
    NO_ALERT = {"error": "no such alert", "message": "An attempt was made to operate on a modal dialog when one "
                                                     "was not open"}

    def __init__(self):
        self.alerts = []
        self.requests = []
        self.pressed = []

    def show(self, text, buttons=("Cancel", "OK"), sticky=False):
        self.alerts.insert(0, {"text": text, "buttons": list(buttons), "sticky": sticky})

    def paths(self, method=None):
        return [path for verb, path, _ in self.requests if method in (None, verb)]

    def __call__(self, method, url, body=None, timeout=10):
        path = "/" + url.split("//", 1)[-1].split("/", 1)[-1]
        self.requests.append((method, path, body))
        if method == "GET" and path == "/alert/text":
            return (200, self.alerts[0]["text"]) if self.alerts else (404, self.NO_ALERT)
        if method == "POST" and path in ("/alert/accept", "/alert/dismiss"):
            if not self.alerts:
                return 404, self.NO_ALERT
            alert, name = self.alerts[0], (body or {}).get("name")
            if name is not None and name not in alert["buttons"]:
                return 500, {"error": "unknown error", "message": f"Failed to find button with label '{name}'"}
            button = name or (alert["buttons"][-1] if path == "/alert/accept" else alert["buttons"][0])
            self.pressed.append((alert["text"], button))
            if not alert["sticky"]:
                self.alerts.pop(0)
            return 200, None
        raise AssertionError(f"unexpected WebDriverAgent request {method} {path}")


class FakeWDA:
    read_alert = WebDriverAgent.read_alert
    alert_text = WebDriverAgent.alert_text
    alert_action = WebDriverAgent.alert_action

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.plans = []
        self.started, self.stopped = [], []
        self.built = 0
        self.loopback = True
        self.mjpeg_listening = True
        self.busy = set()
        self.http = FakeAlertHTTP()
        self.xctestrun = self.data_dir / "Products" / "WebDriverAgentRunner_iphonesimulator26.4-arm64.xctestrun"

    def ensure_build(self, tools):
        self.built += 1
        self.xctestrun.parent.mkdir(parents=True, exist_ok=True)
        self.xctestrun.write_text("plan")
        return self.xctestrun

    def plan(self, udid, port, mjpeg, xctestrun):
        return self.plans.pop(0) if self.plans else "start"

    def start(self, udid, port, mjpeg, xctestrun, tools):
        self.started.append((udid, port, mjpeg))

    def wait_ready(self, udid, port, process=None, timeout=120):
        return None

    def check_loopback(self, port, mjpeg):
        host = "127.0.0.1" if self.loopback else "*"
        found = {port: [Listener("WebDriver", 1, host, port)],
                 mjpeg: [Listener("WebDriver", 1, host, mjpeg)] if self.mjpeg_listening else []}
        return self.loopback and self.mjpeg_listening, found

    def stop(self, udid):
        self.stopped.append(udid)
        return []

    def cancel(self):
        self.cancelled = getattr(self, "cancelled", 0) + 1

    def listening(self, port):
        return port in self.busy

    def owner(self, port):
        return None

    def ready(self, url):
        return True

    def ps(self):
        return ""

    def runner_log(self, identifier):
        return self.data_dir / "logs" / f"wda-{identifier}.log"


@contextlib.contextmanager
def fake_manager(environ=None):
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder) / "Devices"
        root.mkdir()
        data = Path(folder) / "data"
        env = {"MOBSTER_SIM_PORT_BASE": "8310", **(environ or {})}
        with mock.patch.dict("os.environ", env), mock.patch.object(apps, "devices_root", return_value=root):
            manager = api.SimulatorManager(data_dir=data, progress=None)
            simctl = FakeSimctl(root)
            wda = FakeWDA(data)
            leases = FakeLeases()
            clock = Clock()
            manager.run, manager.wda, manager.lease = simctl, wda, leases
            manager.clock, manager.sleep = clock, clock.sleep
            manager._tools = TOOLS
            manager.governor = Governor(sample=lambda: Pressure(load=0.0, cores=8, free_gb=64.0), sleep=clock.sleep,
                                        clock=clock)
            yield manager, simctl, wda, leases, root


def make_app(folder, name="Firefox", bundle="org.mozilla.ios.Firefox", platforms=("iPhoneSimulator",), **extra):
    app = Path(folder) / f"{name}.app"
    app.mkdir(parents=True, exist_ok=True)
    info = {"CFBundleIdentifier": bundle, "CFBundleName": name, "CFBundleShortVersionString": "1.0",
            "CFBundleVersion": "1", **extra}
    if platforms is not None:
        info["CFBundleSupportedPlatforms"] = list(platforms)
    with open(app / "Info.plist", "wb") as stream:
        plistlib.dump(info, stream)
    return app
