"""Expo and React Native apps: find the app, build it, open it on a simulator or your iPhone, reload it."""

import base64
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tarfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import DevError

DEFAULT_PORT = 8081
STATE_DIR = ".mobster/expo"
METRO_WAIT = 120
CONFIG_TIMEOUT = 90
BUILD_TIMEOUT = 60 * 60
CONFIG_FILES = ("app.config.ts", "app.config.js", "app.config.mjs", "app.config.cjs")


@dataclass
class ExpoApp:
    root: Path
    name: str
    slug: str
    expo: bool                      # "expo" is a dependency (else a React Native app without Expo)
    sdk: Optional[str] = None       # the expo package's version range, as package.json says
    dev_client: bool = False        # expo-dev-client is a dependency: preview opens your dev client, not Expo Go
    native: bool = False            # an ios folder with an Xcode project or workspace is there
    bundle: Optional[str] = None    # ios.bundleIdentifier
    schemes: tuple = ()             # the app's URL schemes (config "scheme")
    notes: list = field(default_factory=list)

    @property
    def client(self):
        """"dev-client" or "expo-go": what preview opens."""
        return "dev-client" if self.dev_client or not self.expo else "expo-go"

    def public(self):
        return {"name": self.name, "slug": self.slug, "expo": self.expo, "sdk": self.sdk,
                "client": self.client, "native": self.native, "bundle": self.bundle, "schemes": list(self.schemes),
                "notes": list(self.notes)}


def _run(argv, cwd, log=None, timeout=CONFIG_TIMEOUT, env=None):
    """(exit code, output). Output goes to ``log`` when given."""
    try:
        if log is not None:
            Path(log).parent.mkdir(parents=True, exist_ok=True)
            with open(log, "wb") as handle:
                done = subprocess.run(argv, cwd=str(cwd), stdout=handle, stderr=subprocess.STDOUT,
                                      stdin=subprocess.DEVNULL, timeout=timeout, env=env)
            return done.returncode, Path(log).read_text(encoding="utf-8", errors="replace")[-200_000:]
        done = subprocess.run(argv, cwd=str(cwd), capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout,
                              env=env)
        return done.returncode, (done.stdout + done.stderr).decode("utf-8", errors="replace")
    except FileNotFoundError:
        return 127, f"{argv[0]} isn't installed."
    except subprocess.TimeoutExpired:
        return 124, f"{argv[0]} didn't finish in {timeout // 60 or 1} minutes."


def _package(root):
    try:
        data = json.loads((Path(root) / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _deps(package):
    out = {}
    for key in ("dependencies", "devDependencies"):
        if isinstance(package.get(key), dict):
            out.update(package[key])
    return out


def _app_json_is_expo(root):
    """app.json with an ``expo`` object: the file `npx create-expo-app` writes."""
    try:
        data = json.loads((Path(root) / "app.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and isinstance(data.get("expo"), dict)


def read_config(root, *, runner=None):
    root = Path(root)
    if any((root / name).is_file() for name in CONFIG_FILES):
        code, out = (runner or _run)(["npx", "--no-install", "expo", "config", "--json", "--type", "public"], root)
        start = out.find("{")
        if code == 0 and start >= 0:
            try:
                value = json.loads(out[start:])
                if isinstance(value, dict):
                    return value
            except ValueError:
                pass
    try:
        data = json.loads((root / "app.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if isinstance(data, dict) and isinstance(data.get("expo"), dict):
        return data["expo"]
    return data if isinstance(data, dict) else {}


def _slug(text):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]+", "-", str(text).lower())).strip("-") or "app"


def detect(root, *, runner=None):
    """The Expo or React Native app at ``root``, or None."""
    root = Path(root)
    package = _package(root)
    if package is None:
        return None
    deps = _deps(package)
    configured = any((root / name).is_file() for name in CONFIG_FILES) or _app_json_is_expo(root)
    if "expo" not in deps and "react-native" not in deps and not configured:
        return None
    expo = "expo" in deps or (configured and "react-native" not in deps)
    config = read_config(root, runner=runner) if expo else {}
    name = str(config.get("name") or package.get("name") or root.name)
    slug = _slug(config.get("slug") or package.get("name") or name)
    schemes = config.get("scheme")
    schemes = tuple(item for item in ([schemes] if isinstance(schemes, str) else schemes or ())
                    if isinstance(item, str) and item)
    ios = config.get("ios") if isinstance(config.get("ios"), dict) else {}
    native = any((root / "ios").glob("*.xcworkspace")) or any((root / "ios").glob("*.xcodeproj"))
    app = ExpoApp(root=root, name=name, slug=slug, expo=expo, sdk=deps.get("expo"),
                  dev_client="expo-dev-client" in deps, native=native,
                  bundle=ios.get("bundleIdentifier") if isinstance(ios.get("bundleIdentifier"), str) else None,
                  schemes=schemes)
    if not expo and not native:
        app.notes.append("This React Native app has no ios folder, so Mobster can't build it.")
    return app


def project_root(start):
    """The nearest folder from ``start`` up that holds package.json."""
    folder = Path(start).resolve()
    for candidate in (folder, *folder.parents):
        if (candidate / "package.json").is_file():
            return candidate
    return folder


def find_app(cwd, runner=None):
    """The Expo or React Native app in ``cwd``'s project, or a DevError saying to run it from the app's folder."""
    app = detect(project_root(cwd), runner=runner)
    if app is None:
        raise DevError("This folder isn't an Expo or React Native app: no package.json names expo or react-native.",
                       "Run it from your app's folder.")
    return app


# -- Metro -------------------------------------------------------------------------------------------------------

def metro_running(port=DEFAULT_PORT, *, opener=None, root=None):
    try:
        with (opener or urllib.request.urlopen)(f"http://127.0.0.1:{int(port)}/status", timeout=2) as response:
            if b"packager-status:running" not in response.read(200):
                return False
            headers = getattr(response, "headers", None) or {}
            # Metro names its project in this header, so another app's Metro on the port is never reused
            served = headers.get("X-React-Native-Project-Root") if root is not None else None
    except Exception:
        return False
    return root is None or isinstance(served, str) and os.path.realpath(served) == os.path.realpath(root)


def lan_ip():
    """This Mac's address on the local network (no packet is sent), or None."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        address = probe.getsockname()[0]
        return None if address.startswith("127.") or address == "0.0.0.0" else address
    except OSError:
        return None
    finally:
        probe.close()


def host_for(record):
    """The address the device reaches Metro at: loopback for a simulator, this Mac's LAN address for a phone."""
    if record.get("kind") == "simulator":
        return "127.0.0.1"
    host = lan_ip()
    if not host:
        raise DevError("This Mac has no address on the local network, so the iPhone can't reach Metro.",
                       "Join the same Wi-Fi as the iPhone, then try again.")
    return host


def start_metro(app, port=DEFAULT_PORT, *, popen=None, wait=METRO_WAIT, opener=None, sleep=time.sleep,
                clock=time.monotonic, stop_on_fail=False, kill=os.kill):
    folder = Path(app.root) / STATE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / "metro.log"
    argv = ["npx", "expo", "start", "--port", str(port), "--lan"]
    if app.expo:
        argv.append("--dev-client" if app.client == "dev-client" else "--go")
    else:
        argv = ["npx", "react-native", "start", "--port", str(port)]
    handle = open(log, "wb")
    try:
        process = (popen or subprocess.Popen)(argv, cwd=str(app.root), stdin=subprocess.DEVNULL, stdout=handle,
                                              stderr=subprocess.STDOUT, start_new_session=True,
                                              env={**os.environ, "EXPO_NO_TELEMETRY": "1"})
    except FileNotFoundError:
        raise DevError("npx isn't installed, so Mobster couldn't start Metro.",
                       "Install Node.js (https://nodejs.org), then try again.") from None
    finally:
        handle.close()
    (folder / "metro.pid").write_text(f"{process.pid}\n")
    (folder / "metro.port").write_text(f"{int(port)}\n")
    deadline = clock() + wait
    while clock() < deadline:
        if metro_running(port, opener=opener, root=app.root):
            return process.pid
        if process.poll() is not None:
            break
        sleep(1)
    if process.poll() is not None:
        for name in ("metro.pid", "metro.port"):
            (folder / name).unlink(missing_ok=True)
    elif stop_on_fail:
        from .expo_dev import _stop_owned
        _stop_owned(app.root, kill=kill, sleep=sleep, clock=clock, expected_pid=process.pid)
    raise DevError(f"Metro didn't start on port {port}. Its log is {STATE_DIR}/metro.log.",
                   "Run `npx expo start` yourself to see why, then try again.")


def owns_metro(root, pid):
    """Whether this process group is a Metro for this project, rather than a stale or reused pid."""
    from ..supervised import command_of
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return False
    try:
        if os.getpgid(pid) != pid or not re.search(r"(?:expo|react-native).*\bstart\b", command_of(pid) or ""):
            return False
        result = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                                capture_output=True, text=True, timeout=5)
        return any(line == "n" + str(Path(root).resolve()) for line in result.stdout.splitlines())
    except (OSError, subprocess.SubprocessError):
        return False


def metro_starting(root, *, kill=os.kill):
    """Whether the Metro ``start_metro`` started for this app is still a live process (it may not answer yet)."""
    try:
        pid = int((Path(root) / STATE_DIR / "metro.pid").read_text().strip())
        return pid > 1 and owns_metro(root, pid)
    except (OSError, ValueError):
        return False


def stop_metro(root, *, kill=os.kill, expected_pid=None):
    """Stop the Metro that ``start_metro`` started for this app. Returns its pid, or None when there was none."""
    pidfile = Path(root) / STATE_DIR / "metro.pid"
    try:
        pid = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        return None
    if pid <= 1 or (expected_pid is not None and pid != expected_pid) or not owns_metro(root, pid):
        return None
    try:
        kill(-pid, signal.SIGTERM)  # the process group: npx and the node it started
    except (ProcessLookupError, PermissionError):
        pass
    pidfile.unlink(missing_ok=True)
    (Path(root) / STATE_DIR / "metro.port").unlink(missing_ok=True)
    return pid


def preview_url(app, host, port=DEFAULT_PORT, path=None):
    server = f"http://{host}:{int(port)}"
    route = str(path or "").lstrip("/")
    if app.client == "dev-client":
        url = f"exp+{app.slug}://expo-development-client/?url={urllib.parse.quote(server, safe='')}"
        return url
    return f"exp://{host}:{int(port)}" + (f"/--/{route}" if route else "")


def app_link(app, path):
    """``path`` as the app's own deep link (its first scheme), or None when it has none."""
    route = str(path or "").lstrip("/")
    if not app.schemes:
        return None
    return f"{app.schemes[0]}://{route}"


# A WebSocket text frame to Metro's message socket, in the standard library: the same broadcast `r` sends in
# `npx expo start` ({"version": 2, "method": "reload"}), which every connected app answers by reloading.

def _ws_frame(payload, mask=None):
    data = payload.encode()
    mask = mask or os.urandom(4)
    head = bytearray([0x81])
    if len(data) < 126:
        head.append(0x80 | len(data))
    else:
        head.append(0x80 | 126)
        head += len(data).to_bytes(2, "big")
    return bytes(head) + mask + bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))


def reload(port=DEFAULT_PORT, *, connect=None, timeout=5):
    """Ask every app connected to Metro on ``port`` to reload its JavaScript. DevError when Metro doesn't answer."""
    key = base64.b64encode(os.urandom(16)).decode()
    request = (f"GET /message HTTP/1.1\r\nHost: 127.0.0.1:{int(port)}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode()
    try:
        sock = (connect or socket.create_connection)(("127.0.0.1", int(port)), timeout)
    except OSError:
        raise DevError(f"Metro isn't running on port {port}, so there's nothing to reload.",
                       "Start it with `mobster expo preview` or `npx expo start`.") from None
    try:
        sock.sendall(request)
        answer = b""
        while b"\r\n\r\n" not in answer and len(answer) < 4096:
            chunk = sock.recv(1024)
            if not chunk:
                break
            answer += chunk
        if b" 101 " not in answer.split(b"\r\n", 1)[0]:
            raise DevError(f"Metro on port {port} didn't accept the reload.", "Press r in `npx expo start`.")
        sock.sendall(_ws_frame(json.dumps({"version": 2, "method": "reload"})))
    except OSError:
        raise DevError(f"Metro on port {port} closed the connection.", "Press r in `npx expo start`.") from None
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return True


# -- builds ------------------------------------------------------------------------------------------------------

def prebuild(app, *, runner=None, progress=None):
    """Make the ios folder with ``npx expo prebuild --platform ios`` when the app has none."""
    if app.native:
        return False
    if not app.expo:
        raise DevError("This React Native app has no ios folder, so Mobster can't build it.",
                       "Run `npx react-native init` again, or add the ios folder from your template.")
    (progress or (lambda line: None))("Making the ios folder: npx expo prebuild --platform ios")
    log = Path(app.root) / STATE_DIR / "prebuild.log"
    code, out = (runner or _run)(["npx", "expo", "prebuild", "--platform", "ios"], app.root, log,
                                 timeout=BUILD_TIMEOUT)
    if code != 0:
        raise DevError(f"npx expo prebuild failed. Its log is {STATE_DIR}/prebuild.log.",
                       "Fix what it says (often CocoaPods: `brew install cocoapods`), then try again.")
    app.native = True
    return True


# Release by default: a Debug React Native build loads its JavaScript from Metro and can't run without it.
def build_sim(app, *, configuration="Release", platform="sim", runner=None, progress=None, bundle=None):
    from ..testkit import prbuild
    prebuild(app, runner=runner, progress=progress)
    found = prbuild.find_project(Path(app.root) / "ios")
    if found is None:
        raise DevError("The ios folder has no Xcode workspace or project.", "Run `npx expo prebuild --platform ios`.")
    try:
        scheme = prbuild.pick_scheme(found, runner=runner, cwd=app.root)
        return prbuild.build(app.root, scheme=scheme, project=found, platform=platform, configuration=configuration,
                             runner=runner, progress=progress, bundle=bundle or app.bundle)
    except prbuild.BuildError as error:
        raise DevError(error.message, error.fix) from None


def build_device(app, device_udid, *, configuration="Debug", runner=None, progress=None):
    """Your dev client, signed with your team and installed on the iPhone, by ``npx expo run:ios --device``."""
    if not app.expo:
        raise DevError("This is a React Native app without Expo.", "Build and run it on the iPhone from Xcode.")
    (progress or (lambda line: None))(f"Building {app.name} for the iPhone and installing it: npx expo run:ios")
    log = Path(app.root) / STATE_DIR / "run-ios.log"
    code, out = (runner or _run)(["npx", "expo", "run:ios", "--device", device_udid, "--configuration",
                                  configuration, "--no-bundler"], app.root, log, timeout=BUILD_TIMEOUT)
    if code != 0:
        fix = "Open ios/*.xcworkspace in Xcode and pick a team under Signing & Capabilities, then try again." \
            if re.search(r"sign|provision|team", out, re.I) else f"Its log is {STATE_DIR}/run-ios.log."
        raise DevError("npx expo run:ios failed.", fix)
    return {"bundle": app.bundle, "path": None, "platform": "device", "log": str(log)}


def build_eas(app, profile, *, simulator, runner=None, progress=None):
    folder = Path(app.root) / STATE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / ("eas-build.tar.gz" if simulator else "eas-build.ipa")
    output.unlink(missing_ok=True)
    (progress or (lambda line: None))(f"Building the EAS profile {profile} on this Mac: eas build --local")
    log = folder / "eas.log"
    code, out = (runner or _run)(["npx", "--yes", "eas-cli", "build", "--platform", "ios", "--profile", profile,
                                  "--local", "--non-interactive", "--output", str(output)], app.root, log,
                                 timeout=BUILD_TIMEOUT)
    if code != 0 or not output.is_file():
        raise DevError(f"eas build --local failed. Its log is {STATE_DIR}/eas.log.",
                       "It needs Xcode, CocoaPods and fastlane on this Mac, and `eas login`.")
    if not simulator:
        return {"bundle": app.bundle, "path": str(output), "platform": "device", "log": str(log)}
    target = folder / "eas"
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    with tarfile.open(output) as archive:
        members = [member for member in archive.getmembers()
                   if not member.name.startswith(("/", "..")) and ".." not in Path(member.name).parts
                   and (member.isfile() or member.isdir())]
        archive.extractall(target, members=members)
    apps = sorted(target.rglob("*.app"))
    if not apps:
        raise DevError("The EAS build has no .app inside.", f"Check the profile {profile} has ios.simulator: true.")
    return {"bundle": app.bundle, "path": str(apps[0]), "platform": "sim", "log": str(log)}
