"""`xcrun simctl`: running it, and pure parsers for its JSON (`simctl list -j`).

Parsers take the decoded JSON and return plain records, so the choice of device type, runtime and name is
tested without Xcode. Every function here is read-only; the manager (api.py) decides what to change.
"""

from dataclasses import dataclass
import re
import subprocess
from typing import NamedTuple, Optional

NAME_PREFIX = "Mobster · "
DEFAULT_DEVICE_TYPES = ("iPhone 17 Pro", "iPhone 16 Pro", "iPhone 15 Pro")
DOWNLOAD_RUNTIME = "xcodebuild -downloadPlatform iOS"


def sim_error(kind, message, fix=""):
    """A SimError (api.py), imported late: api.py imports this module."""
    from .api import SimError
    return SimError(kind, message, fix)


class Result(NamedTuple):
    code: Optional[int]   # None: the command could not run, or timed out
    out: str
    err: str

    @property
    def ok(self):
        return self.code == 0

    @property
    def timed_out(self):
        return self.code is None and "did not finish in" in (self.err or "")

    def last_line(self):
        """The last non-empty line of stderr, else stdout: what a person needs from a failed command."""
        for text in (self.err, self.out):
            lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
            if lines:
                return lines[-1][:300]
        return ""


def run(argv, timeout=60, env=None):
    """Run a short command without a terminal. Never raises for the command's own failure."""
    try:
        completed = subprocess.run([str(part) for part in argv], capture_output=True, text=True, timeout=timeout,
                                   env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return Result(None, "", f"{argv[0]} did not finish in {timeout:g} s")
    except (OSError, subprocess.SubprocessError) as exc:
        return Result(None, "", f"{argv[0]} could not run: {exc}")
    return Result(completed.returncode, completed.stdout or "", completed.stderr or "")


@dataclass(frozen=True)
class Runtime:
    identifier: str
    name: str              # "iOS 26.4"
    version: tuple         # (26, 4)
    available: bool
    device_types: tuple    # names of the device types it supports; empty when simctl does not say


@dataclass(frozen=True)
class DeviceType:
    identifier: str
    name: str              # "iPhone 17 Pro"
    family: str            # "iPhone"
    min_runtime: int       # simctl's packed minimum runtime version


@dataclass(frozen=True)
class Device:
    udid: str
    name: str
    state: str             # "Shutdown", "Booted", "Booting", "Shutting Down", "Creating"
    runtime: str           # the runtime identifier it belongs to
    device_type: str       # the device type identifier
    available: bool
    data_path: str


def version_tuple(text):
    """ "26.4" -> (26, 4); anything unreadable sorts first."""
    parts = re.findall(r"\d+", str(text or ""))
    return tuple(int(part) for part in parts) or (0,)


def parse_runtimes(data):
    """iOS runtimes from `simctl list runtimes -j` (or the whole `simctl list -j`)."""
    runtimes = []
    for item in (data or {}).get("runtimes") or ():
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        platform = str(item.get("platform") or name.split(" ")[0])
        if platform != "iOS":
            continue
        supported = tuple(str(entry.get("name")) for entry in item.get("supportedDeviceTypes") or ()
                          if isinstance(entry, dict) and entry.get("name"))
        runtimes.append(Runtime(identifier=str(item.get("identifier") or ""), name=name,
                                version=version_tuple(item.get("version") or name),
                                available=bool(item.get("isAvailable")), device_types=supported))
    return runtimes


def parse_device_types(data):
    types = []
    for item in (data or {}).get("devicetypes") or ():
        if not isinstance(item, dict) or not item.get("name"):
            continue
        family = str(item.get("productFamily") or str(item["name"]).split(" ")[0])
        try:
            minimum = int(item.get("minRuntimeVersion") or 0)
        except (TypeError, ValueError):
            minimum = 0
        types.append(DeviceType(identifier=str(item.get("identifier") or ""), name=str(item["name"]),
                                family=family, min_runtime=minimum))
    return types


def parse_devices(data):
    """Every simulator in the default device set, from `simctl list devices -j`."""
    devices = []
    for runtime, items in ((data or {}).get("devices") or {}).items():
        for item in items or ():
            if not isinstance(item, dict) or not item.get("udid"):
                continue
            devices.append(Device(udid=str(item["udid"]), name=str(item.get("name") or ""),
                                  state=str(item.get("state") or ""), runtime=str(runtime),
                                  device_type=str(item.get("deviceTypeIdentifier") or ""),
                                  available=item.get("isAvailable", True) is not False,
                                  data_path=str(item.get("dataPath") or "")))
    return devices


def _same(a, b):
    return " ".join(str(a).split()).casefold() == " ".join(str(b).split()).casefold()


def choose_runtime(runtimes, wanted=None):
    """The newest available iOS runtime, or the one asked for ("iOS 26.4", "26.4", "iOS 26" or an identifier).

    Raises SimError("environment") with the fix when there is none."""
    available = sorted((runtime for runtime in runtimes if runtime.available), key=lambda runtime: runtime.version)
    if not available:
        raise sim_error("environment", "No iOS Simulator runtime is installed.", DOWNLOAD_RUNTIME)
    if not wanted:
        return available[-1]
    text = str(wanted).strip()
    for runtime in reversed(available):
        if _same(runtime.name, text) or runtime.identifier == text or _same("iOS " + text, runtime.name):
            return runtime
    number = re.sub(r"(?i)^ios\s*", "", text)
    if re.fullmatch(r"\d+(\.\d+)*", number):
        prefix = version_tuple(number)
        for runtime in reversed(available):
            if runtime.version[:len(prefix)] == prefix:
                return runtime
    names = ", ".join(runtime.name for runtime in reversed(available))
    raise sim_error("environment", f"{text} isn't installed. Installed: {names}.", DOWNLOAD_RUNTIME)


def supported(device_type, runtime):
    return not runtime.device_types or device_type.name in runtime.device_types


def choose_device_type(device_types, runtime, wanted=None):
    """The device type asked for, else iPhone 17 Pro, else iPhone 16 Pro or 15 Pro, else the newest iPhone
    the runtime supports (the highest minimum runtime; on a tie, the one simctl lists first).

    Raises SimError("environment") with the fix when there is none."""
    if wanted:
        text = str(wanted).strip()
        match = next((kind for kind in device_types if _same(kind.name, text) or kind.identifier == text), None)
        if match is None:
            iphones = [kind.name for kind in device_types
                       if kind.name.startswith("iPhone") and supported(kind, runtime)]
            raise sim_error("environment", f"{text} isn't a simulator device type in this Xcode. Try one of: "
                            f"{', '.join(iphones[:6]) or 'none'}.",
                            "See every type with: xcrun simctl list devicetypes")
        if not supported(match, runtime):
            raise sim_error("environment", f"{match.name} doesn't run {runtime.name}.",
                            f"Pick another device, or install its runtime: {DOWNLOAD_RUNTIME}")
        return match
    usable = [kind for kind in device_types if kind.name.startswith("iPhone") and supported(kind, runtime)]
    for name in DEFAULT_DEVICE_TYPES:
        match = next((kind for kind in usable if kind.name == name), None)
        if match:
            return match
    if not usable:
        raise sim_error("environment", f"No iPhone device type runs {runtime.name}.", DOWNLOAD_RUNTIME)
    newest = max(kind.min_runtime for kind in usable)
    return next(kind for kind in usable if kind.min_runtime == newest)


def sim_name(device_type, runtime, taken=()):
    """ "Mobster · iPhone 17 Pro · iOS 26.4", then " · 2", " · 3" … for more of the same pair."""
    base = f"{NAME_PREFIX}{device_type} · {runtime}"
    taken = set(taken)
    if base not in taken:
        return base
    number = 2
    while f"{base} · {number}" in taken:
        number += 1
    return f"{base} · {number}"


def is_mobster_name(name):
    return str(name or "").startswith(NAME_PREFIX)


UDID = re.compile(r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$")


def parse_created(out):
    """The UDID `simctl create` prints, or None."""
    text = (out or "").strip().upper()
    return text if UDID.match(text) else None


def parse_groups(out):
    """[(group id, path)] from `simctl get_app_container <udid> <bundle> groups`."""
    groups = []
    for line in (out or "").splitlines():
        line = line.strip()
        if not line:
            continue
        slash = line.find("/")
        if slash < 0:
            continue
        groups.append((line[:slash].strip(), line[slash:].strip()))
    return groups
