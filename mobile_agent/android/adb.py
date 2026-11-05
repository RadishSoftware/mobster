"""adb: find the SDK tools, list devices, and run the few commands the driver needs. One argv per call, never a shell
string built on this side; the runner is injectable so the tests use a fake adb."""

import base64
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

SERIAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\-]{0,63}")
PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+")
DEFAULT_ROOTS = ("~/Library/Android/sdk", "~/Android/Sdk")
OUTPUT_LIMIT = 64_000_000
quote = shlex.quote


class AdbError(RuntimeError):
    """An adb call failed or its answer was not understood. ``fix`` is one plain sentence (may be "")."""

    def __init__(self, message, fix=""):
        super().__init__(message)
        self.fix = fix


@dataclass(frozen=True)
class Result:
    code: int
    out: bytes = b""
    err: bytes = b""

    @property
    def text(self):
        return self.out.decode("utf-8", "replace")

    @property
    def message(self):
        lines = (self.err or self.out).decode("utf-8", "replace").strip().splitlines()
        return lines[-1] if lines else ""


def run_process(argv, timeout=30, stdin=None):
    """The default runner. A timeout kills the child (subprocess.run does) and is reported as code 124."""
    try:
        done = subprocess.run(argv, input=stdin, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return Result(124, b"", b"timed out")
    except OSError as error:
        return Result(127, b"", str(error).encode())
    return Result(done.returncode, done.stdout[:OUTPUT_LIMIT], done.stderr[:4096])


def sdk_root(environ=None):
    """The Android SDK folder: ANDROID_HOME, ANDROID_SDK_ROOT, else the Android Studio default; None when absent."""
    environ = os.environ if environ is None else environ
    for name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if environ.get(name):
            path = Path(environ[name]).expanduser()
            if path.is_dir():
                return path
    for default in DEFAULT_ROOTS:
        path = Path(default).expanduser()
        if path.is_dir():
            return path
    return None


def tool(name, root=None):
    """The path of an SDK tool (adb, emulator, avdmanager, ...), or None."""
    root = root if root is not None else sdk_root()
    if root is None:
        return None
    for relative in {"adb": ("platform-tools/adb",), "emulator": ("emulator/emulator",),
                     "avdmanager": ("cmdline-tools/latest/bin/avdmanager", "cmdline-tools/bin/avdmanager")
                     }.get(name, (name,)):
        path = Path(root) / relative
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def tool_in_build_tools(name, root=None):
    """A tool from the newest build-tools folder (aapt2), or None."""
    root = root if root is not None else sdk_root()
    if root is None:
        return None
    for folder in sorted((Path(root) / "build-tools").glob("*"), reverse=True):
        path = folder / name
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def valid_serial(serial):
    if not isinstance(serial, str) or not SERIAL.fullmatch(serial):
        raise AdbError(f"{serial!r} is not an adb serial.")
    return serial


def valid_package(package):
    if not isinstance(package, str) or len(package) > 255 or not PACKAGE.fullmatch(package):
        raise AdbError(f"{package!r} is not an Android package name.")
    return package


@dataclass(frozen=True)
class AndroidDevice:
    serial: str
    state: str        # device, offline, unauthorized
    kind: str         # emulator or usb
    model: str = ""
    product: str = ""

    @property
    def ready(self):
        return self.state == "device"


def parse_devices(text):
    """`adb devices -l` as AndroidDevice rows. Lines that are not a serial and a state are ignored."""
    devices = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2 or not SERIAL.fullmatch(parts[0]) or parts[0] == "List":
            continue
        extra = dict(item.split(":", 1) for item in parts[2:] if ":" in item)
        devices.append(AndroidDevice(parts[0], parts[1], "emulator" if parts[0].startswith("emulator-") else "usb",
                                     extra.get("model", ""), extra.get("product", "")))
    return devices


class Adb:
    """adb for one device (``serial``) or, with none, for listing. ``run`` is (argv, timeout, stdin) -> Result."""

    def __init__(self, serial=None, run=run_process, binary=None):
        self.serial = valid_serial(serial) if serial else None
        self._run = run
        self.binary = binary or tool("adb") or "adb"

    def adb(self, *args, timeout=30, stdin=None, check=True):
        argv = [self.binary, *(["-s", self.serial] if self.serial else []), *map(str, args)]
        result = self._run(argv, timeout, stdin)
        if check and result.code != 0:
            raise AdbError(f"adb {args[0]} failed: {result.message or 'exit ' + str(result.code)}",
                           "Check that the device is online (`adb devices -l`) and unlocked.")
        return result

    def shell(self, *args, timeout=30, check=True):
        return self.adb("shell", *args, timeout=timeout, check=check)

    # -- devices

    def devices(self):
        return parse_devices(self.adb("devices", "-l", timeout=10).text)

    def prop(self, name):
        return self.shell("getprop", name, timeout=10).text.strip()

    def booted(self):
        return self.shell("getprop", "sys.boot_completed", timeout=10, check=False).text.strip() == "1"

    # -- screen

    def screen_px(self):
        """(width, height, density dpi) of the display in pixels, from `wm size` and `wm density`."""
        size = re.findall(r"(\d+)x(\d+)", self.shell("wm", "size", timeout=10).text)
        density = re.findall(r"(\d+)", self.shell("wm", "density", timeout=10).text)
        if not size or not density:
            raise AdbError("The device did not report its screen size.")
        width, height = map(int, size[-1])
        return width, height, int(density[-1])

    def dump_ui(self, timeout=15):
        """The accessibility tree as uiautomator's XML, with the trailer line removed."""
        text = self.adb("exec-out", "uiautomator", "dump", "/dev/tty", timeout=timeout).text
        start, end = text.find("<?xml"), text.rfind("</hierarchy>")
        if start < 0 or end < 0:
            raise AdbError("uiautomator returned no accessibility tree.",
                           "Wait for the screen to settle, or restart the device's UI automation.")
        return text[start:end + len("</hierarchy>")]

    def screenshot_png(self, timeout=15):
        data = self.adb("exec-out", "screencap", "-p", timeout=timeout).out
        if not data.startswith(b"\x89PNG"):
            raise AdbError("The device returned no screenshot.")
        return data

    def screenshot_data_uri(self, timeout=15):
        return "data:image/png;base64," + base64.b64encode(self.screenshot_png(timeout)).decode()

    def front_package(self):
        """The package in front, from the focused window; "" when the device does not say."""
        text = self.shell("dumpsys", "window", timeout=10, check=False).text
        match = re.search(r"mCurrentFocus=Window\{\S+ \S+ ([A-Za-z0-9_.]+)/", text)
        return match.group(1) if match else ""

    # -- input

    def tap(self, x, y):
        self.shell("input", "tap", int(round(x)), int(round(y)), timeout=10)

    def swipe(self, x1, y1, x2, y2, millis=300):
        self.shell("input", "swipe", *(int(round(n)) for n in (x1, y1, x2, y2)), int(millis), timeout=10)

    def long_press(self, x, y, millis=900):
        self.swipe(x, y, x, y, millis)

    def key(self, code):
        self.shell("input", "keyevent", int(code), timeout=10)

    def text(self, value):
        """Type ASCII text at the focus. adb joins its arguments into one device shell line, so the text is single
        quoted. `input text` cannot send other characters, and reads "%s" as a space, so those are refused."""
        if not value.isascii() or "%" in value or any(ord(c) < 32 for c in value):
            raise AdbError("Android can type plain ASCII text without % only.",
                           "Use text without accents, emoji or % until the keyboard helper ships.")
        self.shell("input", "text", quote(value.replace(" ", "%s")), timeout=15)

    # -- apps

    def install(self, apk, timeout=180):
        path = Path(apk)
        if path.suffix != ".apk" or not path.is_file():
            raise AdbError(f"{apk} is not an .apk file.")
        result = self.adb("install", "-r", str(path), timeout=timeout, check=False)
        if result.code != 0 or b"Success" not in result.out:
            raise AdbError(f"adb install failed: {result.message or result.text.strip()[-200:]}")

    def launch(self, package):
        valid_package(package)
        result = self.shell("monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1", timeout=20)
        if b"No activities found" in result.out + result.err:
            raise AdbError(f"{package} has no launchable activity or is not installed.")

    def stop(self, package):
        self.shell("am", "force-stop", valid_package(package), timeout=10)

    def installed_version(self, package):
        """versionName of an installed package, or None when it is not installed."""
        valid_package(package)
        if "package:" not in self.shell("pm", "path", package, timeout=10, check=False).text:
            return None
        match = re.search(r"versionName=(\S+)", self.shell("dumpsys", "package", package, timeout=15).text)
        return match.group(1) if match else ""

    def clear_data(self, package):
        result = self.shell("pm", "clear", valid_package(package), timeout=20, check=False)
        if b"Success" not in result.out:
            raise AdbError(f"Clearing {package}'s data failed: {result.message or result.text.strip()[-120:]}")

    def uninstall(self, package):
        self.adb("uninstall", valid_package(package), timeout=60, check=False)

    def permission(self, package, permission, grant):
        if not re.fullmatch(r"[A-Za-z0-9_.]+", permission):
            raise AdbError(f"{permission!r} is not a permission name.")
        result = self.shell("pm", "grant" if grant else "revoke", valid_package(package), permission, timeout=15,
                            check=False)
        if result.code != 0 or b"Exception" in result.out + result.err or b"Error" in result.err:
            raise AdbError(f"{'Granting' if grant else 'Revoking'} {permission} failed: "
                           f"{result.message or result.text.strip()[-120:]}")

    def open_url(self, url, package=None):
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9+.\-]*:[^\s'\"`\\]+", url):
            raise AdbError(f"{url!r} is not a link adb can open.")
        self.shell("am", "start", "-a", "android.intent.action.VIEW", "-d", quote(url),
                   *(["-p", valid_package(package)] if package else []), timeout=15)
