"""Finding the Mac's own tools, and asking the Mac short questions."""

import functools
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from .devices import LOOPBACK, UDID_PATTERN

TOOL_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/Applications/Xcode.app/Contents/Developer/usr/bin")
# The iPhone connection tools: libimobiledevice's and libusbmuxd's. Mobster for Mac carries its own build of them
# (desktop/scripts/build-iphone-tools.sh), found before any Homebrew copy, so nobody needs Homebrew or Terminal.
IPHONE_TOOLS = ("idevice_id", "ideviceinfo", "ideviceinstaller", "iproxy", "afcclient", "idevicesyslog",
                "idevicecrashreport")
# /usr/bin/xcodebuild and /usr/bin/git exist on every Mac: they are stubs that forward to the active
# developer directory (and fail, or open an install dialog, without one). They never count as Xcode or
# git: both come from the Xcode app itself, git also from the Command Line Tools or Homebrew.
XCODE_SHIMS = frozenset({"xcodebuild", "git"})
CLT_DIR = "/Library/Developer/CommandLineTools"
XCODE_APP_DIRS = ("/Applications", str(Path.home() / "Applications"))
GIT_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", CLT_DIR + "/usr/bin")
# Xcode's state takes a few short xcodebuild calls: a working Xcode is re-checked rarely, one that
# needs fixing often enough that the fix shows up on its own.
XCODE_OK_TTL, XCODE_RETRY_TTL = 300.0, 10.0
WDA_PORT, VIDEO_PORT = 8100, 9100


def executable(directory, name):
    path = Path(directory) / name
    return str(path) if path.is_file() and os.access(path, os.X_OK) else None


def bundled_tools_dir():
    override = os.environ.get("MOBSTER_IPHONE_TOOLS")
    if override:
        return Path(override)
    frozen = getattr(sys, "_MEIPASS", None)
    if not frozen:
        return None
    for resources in (Path(frozen).parent / "Resources", Path(sys.executable).resolve().parent.parent / "Resources"):
        if (resources / "iphone-tools" / "bin").is_dir():
            return resources / "iphone-tools" / "bin"
    return None


def bundled_tool(name):
    """The app's own copy of an iPhone connection tool, or None."""
    directory = bundled_tools_dir() if name in IPHONE_TOOLS else None
    return executable(directory, name) if directory else None


def tool(name):
    if name in XCODE_SHIMS:
        return xcode_tool(name)
    if path := bundled_tool(name):
        return path
    for directory in TOOL_DIRS:
        if path := executable(directory, name):
            return path
    return shutil.which(name)


def is_xcode(path):
    """A developer directory inside an Xcode app (not the Command Line Tools)."""
    path = Path(path)
    return (path.name == "Developer" and path.parent.name == "Contents" and path.parent.parent.suffix == ".app"
            and executable(path / "usr" / "bin", "xcodebuild") is not None)


def developer_dir():
    selected = (run(["/usr/bin/xcode-select", "-p"], timeout=5) or "").strip().rstrip("/") or None
    if selected and is_xcode(selected):
        return selected, selected
    for root in XCODE_APP_DIRS:
        try:
            apps = sorted(Path(root).glob("Xcode*.app"), key=lambda app: (app.name != "Xcode.app", app.name))
        except OSError:
            continue
        for app in apps:
            if is_xcode(app / "Contents" / "Developer"):
                return str(app / "Contents" / "Developer"), selected
    return None, selected


# A shell's compiler settings that clang and xcodebuild read from the environment. Meant for building for this Mac
# (Homebrew, conda), they point an iPhone or simulator build at the Mac's headers and SDK: on 17 Oct a shell with
# CPATH=/usr/local/include failed the helper's first build ("include location '/usr/local/include' is unsafe for
# cross-compilation", -Werror). Xcode's builds never take them.
HOST_BUILD_ENV = ("CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH", "OBJC_INCLUDE_PATH", "OBJCPLUS_INCLUDE_PATH",
                  "LIBRARY_PATH", "SDKROOT", "CFLAGS", "CXXFLAGS", "OBJCFLAGS", "CPPFLAGS", "LDFLAGS")


def xcode_env(developer=None, base=None):
    env = {key: value for key, value in (os.environ if base is None else base).items() if key not in HOST_BUILD_ENV}
    if developer:
        env["DEVELOPER_DIR"] = developer
    return env


# In place of asking for `sudo xcode-select -s`: only this process's environment changes, never the Mac's setting.
def adopt_xcode(environ=None):
    environ = os.environ if environ is None else environ
    if environ.get("DEVELOPER_DIR"):
        return environ["DEVELOPER_DIR"]
    xcode, selected = developer_dir()
    if xcode and selected != xcode:
        environ["DEVELOPER_DIR"] = xcode
    return xcode


def xcode_tool(name, developer=...):
    developer = developer_dir()[0] if developer is ... else developer
    directories = (([str(Path(developer) / "usr" / "bin")] if developer else [])
                   + (list(GIT_DIRS) if name == "git" else []))
    return next((path for directory in directories if (path := executable(directory, name))), None)


def execute(command, env=None, timeout=20):
    """(exit code, stdout and stderr) of a short command; (None, "") if it could not run."""
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=env,
                                   stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None, ""
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


# The Mac's xcode-select choice is never a problem: Mobster runs Xcode's tools with DEVELOPER_DIR (adopt_xcode).
def xcode_status():
    xcode, selected = developer_dir()
    status = {"state": "ok", "app": str(Path(xcode).parent.parent) if xcode else None, "developer_dir": xcode,
              "selected": selected, "version": None, "problems": [], "fixes": []}
    problems, fixes = status["problems"], status["fixes"]
    if not xcode:
        problems.append("missing")
    else:
        xcodebuild, env = str(Path(xcode) / "usr" / "bin" / "xcodebuild"), {**os.environ, "DEVELOPER_DIR": xcode}
        code, output = execute([xcodebuild, "-version"], env)
        if code == 0:
            status["version"] = (output.strip().splitlines() or [None])[0]
            if execute([xcodebuild, "-checkFirstLaunchStatus"], env)[0] not in (0, None):
                problems.append("first_launch")
                fixes.append("sudo xcodebuild -runFirstLaunch")
            else:
                code, sdks = execute([xcodebuild, "-showsdks"], env, timeout=30)
                if code == 0 and "iphoneos" not in sdks:
                    problems.append("no_ios")
                    fixes.append("xcodebuild -downloadPlatform iOS")
        elif "license" in output.lower():
            problems.append("license")
            fixes.append("sudo xcodebuild -license accept")
        else:
            problems.append("broken")
    status["state"] = problems[0] if problems else "ok"
    return status


def count_iphones(items, simulated=False):
    """iPhones in a system_profiler USB tree; a virtual one on a simulated bus doesn't count."""
    total = 0
    for item in items if isinstance(items, list) else ():
        if not isinstance(item, dict):
            continue
        on_simulated = simulated or item.get("USBKeyHardwareType") == "Simulated"
        name = str(item.get("_name") or "")
        vendor = str(item.get("USBDeviceKeyVendorID") or item.get("vendor_id") or "").lower()
        if name.startswith("iPhone") and "virtual" not in name.lower() and "0x05ac" in vendor and not on_simulated:
            total += 1
        total += count_iphones(item.get("_items"), on_simulated)
    return total


def run(command, timeout=10):
    """stdout of a short command, or None if it failed."""
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


# How long one USB listing answers ``usb_attached``: a pulled cable is known within this many seconds.
ATTACHED_TTL = 2.0
_attached_cache = {"at": None, "udids": None}
_attached_lock = threading.Lock()


def usb_attached(udid, now=None, ttl=ATTACHED_TTL):
    if not isinstance(udid, str) or not UDID_PATTERN.fullmatch(udid):
        return None
    now = time.monotonic() if now is None else now
    with _attached_lock:
        at, udids = _attached_cache["at"], _attached_cache["udids"]
        if at is None or now - at >= ttl:
            listing = tool("idevice_id")
            output = run([listing, "-l"], timeout=3) if listing else None
            udids = None if output is None else {line.strip().upper() for line in output.splitlines() if line.strip()}
            _attached_cache.update(at=now, udids=udids)
    return None if udids is None else udid.upper() in udids


@functools.lru_cache(maxsize=4)
def iproxy_binds_loopback(path):
    """Whether this iproxy takes -s; an older one cannot be kept off the LAN."""
    try:
        completed = subprocess.run([path, "-h"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return "--source" in completed.stdout + completed.stderr


def relay_command(udid, wda_port=WDA_PORT, video_port=VIDEO_PORT):
    iproxy = tool("iproxy")
    if not iproxy:
        raise LookupError("The iPhone connection tools aren't installed. Open Setup › Get your Mac ready.")
    if not iproxy_binds_loopback(iproxy):
        raise LookupError("This iproxy can't be limited to this Mac. Update it: "
                          "brew upgrade libimobiledevice libusbmuxd")
    return [iproxy, "-s", LOOPBACK, "-u", udid, f"{wda_port}:{WDA_PORT}", f"{video_port}:{VIDEO_PORT}"]
