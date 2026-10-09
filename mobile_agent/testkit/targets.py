"""Where a `mobster test` run's checks run: simulators by device type (``--sim``, a matrix file), one named device
(``--device``), or each check's own simulator.

Rules that keep someone's iPhone out of a repository's reach:
- A matrix file names simulator device types only. Its entries are never looked up as devices, so a phone that
  happens to share a name with a device type is never reached through it.
- A check file may name a simulator, never a real device (verify's rule). A real iPhone is reached only by
  ``--device`` on the command line.
- On a real iPhone a check drives only its own app, and only a build the developer put there: one Mobster installs
  from the check's ``app.path`` in this run (a device build or an .ipa), or one devicectl lists as built by a
  developer. An App Store app, an app that's part of iOS, and the social and dating apps on
  harness_api.UNATTENDED_DENY are couldn't run before anything is tapped.
"""

from dataclasses import dataclass, replace
import json
from pathlib import Path
import plistlib
import re
import subprocess
import tempfile
import threading
from typing import Optional
import zipfile

MATRIX_FILE = "matrix.yaml"
MATRIX_LIMIT = 20
NAME_LIMIT = 100
# devicectl's app list (`xcrun devicectl device info apps --json-output`): result.apps[] with this flag on an app
# built and installed by a developer (Xcode, devicectl). OWNER TEST PLAN item: confirm the field's name on a phone.
DEVELOPER_FIELD = "builtByDeveloper"
DEVICECTL_TIMEOUT = 60
APP_STORE = "Mobster tests your own apps on your iPhone. {app} is from the App Store."
PART_OF_IOS = "Mobster tests your own apps on your iPhone. {app} is part of iOS."
NOT_UNATTENDED = ("Mobster doesn't test social or dating apps on your iPhone. {app} is one, so this check didn't "
                  "run.")
NOT_THIS_APP = "{path} is {built}, not {bundle}, the app the check names."
PLIST_LIMIT = 1_000_000  # an Info.plist read from inside an .ipa


class TargetError(Exception):
    """A usage problem with --sim, --device or the matrix file: one plain sentence."""


class Refused(Exception):
    """A check that can't run on this target: couldn't run, before anything is tapped. ``klass`` names why."""

    def __init__(self, message, klass="device", fix=""):
        super().__init__(message)
        self.message, self.klass, self.fix = message, klass, fix or ""


@dataclass(frozen=True)
class Target:
    key: str                       # "default" | "sim:<type>@<runtime>" | "device:<id>"
    kind: str                      # "default" | "simulator" | "device"
    device_type: Optional[str] = None
    runtime: Optional[str] = None
    record: Optional[dict] = None  # devices.record: a named device (a real one, or a Mobster simulator by name)

    @property
    def label(self):
        if self.record is not None:
            return self.record.get("name") or self.record.get("id") or "device"
        if self.kind == "default":
            return "Each check's simulator"
        return " · ".join(filter(None, [self.device_type or "Default simulator", self.runtime]))

    @property
    def real(self):
        return self.kind == "device" and self.record is not None and self.record.get("kind") != "simulator"

    @property
    def lane(self):
        """Which worker pool runs it: the shared simulator slots, or one lane of its own for a named device (one
        check at a time on it)."""
        return "sims" if self.record is None else self.key

    def public(self):
        out = {"key": self.key, "name": self.label, "kind": "simulator" if self.kind != "device" or (
            self.record or {}).get("kind") == "simulator" else (self.record or {}).get("kind") or "device"}
        if self.device_type:
            out["type"] = self.device_type
        if self.runtime:
            out["runtime"] = self.runtime
        return out


DEFAULT = Target("default", "default")


def parse_sim(text, where="--sim"):
    """``TYPE[@RUNTIME]`` as (type, runtime or None)."""
    text = " ".join(str(text or "").split())
    device, _, runtime = text.partition("@")
    device, runtime = device.strip(), runtime.strip() or None
    if not device:
        raise TargetError(f"{where} {text!r}: name a simulator device type, such as \"iPhone 17 Pro\" or "
                          "\"iPhone 17 Pro@iOS 26.4\"")
    if len(device) > NAME_LIMIT or (runtime and len(runtime) > NAME_LIMIT):
        raise TargetError(f"{where} {text!r} is longer than {NAME_LIMIT} characters")
    return device, runtime


def simulator(device, runtime=None):
    return Target(f"sim:{device}@{runtime or ''}", "simulator", device_type=device, runtime=runtime)


def load_matrix(path):
    """The simulators a matrix file lists: ``simulators:`` (or a top-level list) of "TYPE[@RUNTIME]" or
    {device: TYPE, runtime: RUNTIME}. Simulators only: a ``devices:`` key is refused."""
    path = Path(path)
    import yaml
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise TargetError(f"{path} doesn't exist.") from None
    except (OSError, UnicodeDecodeError) as error:
        raise TargetError(f"{path} can't be read ({type(error).__name__}).") from None
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        line = f" line {mark.line + 1}" if mark is not None else ""
        raise TargetError(f"{path}{line}: {getattr(error, 'problem', None) or 'not valid YAML'}") from None
    if isinstance(data, dict):
        if "devices" in data:
            raise TargetError(f"{path} names devices. A matrix file names simulators only; pass a real iPhone with "
                              "--device on the command line.")
        unknown = [key for key in data if key != "simulators"]
        if unknown:
            raise TargetError(f"{path} has an unknown key {unknown[0]!r}; list them under simulators:")
        data = data.get("simulators")
    if not isinstance(data, list) or not data:
        raise TargetError(f"{path} must list simulators, such as simulators: [\"iPhone 17 Pro@iOS 26.4\"]")
    if len(data) > MATRIX_LIMIT:
        raise TargetError(f"{path} lists more than {MATRIX_LIMIT} simulators")
    out = []
    for index, item in enumerate(data):
        where = f"{path.name} item {index + 1}"
        if isinstance(item, dict):
            unknown = [key for key in item if key not in ("device", "runtime")]
            if unknown or not isinstance(item.get("device"), str) or not isinstance(item.get("runtime", ""), str):
                raise TargetError(f"{where}: write \"TYPE@RUNTIME\" or {{device: TYPE, runtime: RUNTIME}}")
            text = item["device"] + ("@" + item["runtime"] if item.get("runtime") else "")
        elif isinstance(item, str):
            text = item
        else:
            raise TargetError(f"{where}: write \"TYPE@RUNTIME\", such as \"iPhone 17 Pro@iOS 26.4\"")
        out.append(simulator(*parse_sim(text, where)))
    return out


def resolve(*, sims=(), devices=(), matrix=None, root=None, named_device=None):
    """The targets, in order, without duplicates: ``--sim`` and the matrix file's simulators, then each ``--device``.
    With none of them, the project's .mobster/matrix.yaml when there is one, else each check's own simulator."""
    targets = [simulator(*parse_sim(text)) for text in sims or ()]
    if matrix is not None:
        targets += load_matrix(matrix)
    for name in devices or ():
        name = " ".join(str(name or "").split())
        if not name or len(name) > 128:
            raise TargetError("--device needs a device's name or id, as `mobster devices` lists them")
        record = _named(name, named_device)
        if record is None:
            raise TargetError(f"No device is named {name!r}. `mobster devices` lists them; for a simulator device "
                              "type, use --sim.")
        targets.append(Target(f"device:{record.get('id') or name}", "device", record=record))
    if not targets and root is not None and (Path(root) / ".mobster" / MATRIX_FILE).is_file():
        targets = load_matrix(Path(root) / ".mobster" / MATRIX_FILE)
    return list({target.key: target for target in targets}.values()) or [DEFAULT]


def _named(name, named_device):
    from ..devices import AmbiguousDevice
    if named_device is None:
        from ..device_targets import named_device
    try:
        return named_device(name)
    except AmbiguousDevice as error:
        raise TargetError(str(error)) from None


# -- a check on a target -----------------------------------------------------------------------------------------

def for_target(check, target, *, named_device=None, gate=None):
    """(the check as it runs on ``target``, notes, the device record it runs on or None). Raises Refused.

    A simulator target replaces the check's own device and runtime. The default target keeps them, after verify's
    rule: a check file names a simulator (a device type, or a Mobster simulator by name), never a real device. A
    real device goes through ``gate`` (DeviceGate)."""
    from ..harness_api import unattended_allowed
    notes = []
    # The app the check names and the app its build is (an .ipa's too): neither may be a social or dating app.
    for bundle in (check.bundle_id, bundle_of(check.app_path)):
        if bundle and not unattended_allowed(bundle):
            raise Refused(NOT_UNATTENDED.format(app=bundle), "not_unattended")
    if target.kind == "simulator" and target.record is None:
        return replace(check, device=target.device_type, runtime=target.runtime), notes, None
    if target.kind == "default":
        if not check.device:
            return check, notes, None
        record = _named(check.device, named_device) if check.device else None
        if record is None:
            return check, notes, None  # a simulator device type, as verify reads it
        if record.get("kind") != "simulator":
            name = record.get("name") or check.device
            raise Refused(f"The check names “{name}”, a real device, and a check file may name only a simulator. To "
                          f"run it there, pass --device \"{name}\".", "usage")
        return check, notes, record
    record = target.record
    if record.get("kind") == "simulator":
        return replace(check, device=None, runtime=None), notes, record
    if gate is None:
        raise Refused("Mobster can't tell whether the app is your own build on this device.", "device")
    return gate.admit(check, record)


def bundle_of(app_path):
    """The bundle ID in an .app's Info.plist, or in the one app an .ipa holds (Payload/X.app/Info.plist); else
    None."""
    info = _info(app_path)
    value = info.get("CFBundleIdentifier") if info else None
    return value if isinstance(value, str) else None


def _info(app_path):
    if not app_path:
        return None
    text = str(app_path)
    try:
        if text.lower().endswith(".ipa"):
            return _ipa_info(text)
        if not text.endswith(".app"):
            return None
        with open(Path(app_path) / "Info.plist", "rb") as handle:
            data = plistlib.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, plistlib.InvalidFileException, ValueError, zipfile.BadZipFile):
        return None


def _ipa_info(path):
    """The Info.plist of the one app in an .ipa, read without unpacking it; None when there isn't exactly one."""
    with zipfile.ZipFile(path) as archive:
        plists = [item for item in archive.infolist()
                  if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", item.filename)]
        if len(plists) != 1 or plists[0].file_size > PLIST_LIMIT:
            return None
        data = plistlib.loads(archive.read(plists[0]))
    return data if isinstance(data, dict) else None


def device_build(app_path):
    """True for a build a real iPhone can install (an .ipa, or an .app built for iphoneos), False for a simulator
    build, None when the path says neither."""
    if not app_path:
        return None
    if str(app_path).lower().endswith(".ipa"):
        return True
    info = _info(app_path)
    if not info:
        return None
    platform = str(info.get("DTPlatformName") or "").lower()
    supported = [str(item).lower() for item in info.get("CFBundleSupportedPlatforms") or ()]
    if platform == "iphonesimulator" or "iphonesimulator" in supported:
        return False
    if platform == "iphoneos" or "iphoneos" in supported:
        return True
    return None


class DeviceGate:
    """Which apps a check may drive on a real iPhone, for one suite: installs a check's device build once, and
    reads each phone's app list once (devicectl). ``runner(argv) -> (code, output)`` and ``installer(record, path)``
    are fakes in tests."""

    def __init__(self, *, runner=None, installer=None, progress=None, allowed=None):
        self.runner = runner or _run
        self.installer = installer
        self.progress = progress or (lambda line: None)
        self.allowed = allowed
        self._apps = {}         # device id -> {bundle: app dict}
        self._installed = {}    # (device id, app path) -> bundle
        self._lock = threading.Lock()

    def admit(self, check, record):
        """(the check as it runs on the phone, notes, record). Raises Refused."""
        notes = []
        name = record.get("name") or record.get("id") or "the iPhone"
        built = bundle_of(check.app_path)
        if check.bundle_id and built and built != check.bundle_id:
            raise Refused(NOT_THIS_APP.format(path=Path(check.app_path).name, built=built, bundle=check.bundle_id),
                          "usage", "Point app.path at the build of the app the check names.")
        bundle = check.bundle_id or built
        if bundle and bundle.startswith("com.apple."):
            raise Refused(PART_OF_IOS.format(app=_apple_name(bundle)), "not_developer_build")
        if record.get("kind") != "usb":
            raise Refused(f"Mobster can't tell which apps on “{name}” are your own builds: it can check an iPhone "
                          "connected to this Mac, or a simulator.", "device",
                          "Plug the iPhone in, or run the check on a simulator with --sim.")
        build = device_build(check.app_path)
        if check.app_path and build is not False:
            installed = self._install(record, check.app_path, bundle)
            # What landed on the phone is the app the check drives: the same rules, read from the install.
            self._admit_installed(installed, check)
            bundle = installed
            notes.append(f"Installed {Path(check.app_path).name} on {name} for this run.")
        else:
            if check.app_path:
                notes.append(f"{Path(check.app_path).name} is a simulator build, so the check used the build of "
                             f"{bundle or 'the app'} already on {name}.")
            if not bundle:
                raise Refused("The check doesn't say which app to test: add app.bundle.", "usage")
            app = self._apps_on(record).get(bundle)
            if app is None:
                raise Refused(f"{bundle} isn't on “{name}”. Install your build there from Xcode, or give the check an "
                              ".ipa or a device build in app.path.", "install")
            if app.get(DEVELOPER_FIELD) is not True:
                label = app.get("name") or bundle
                raise Refused(APP_STORE.format(app=label), "not_developer_build",
                              "Install your own build of it from Xcode, then run the check again.")
        reset = check.reset
        if reset in ("data", "reinstall"):
            notes.append(f"Mobster never clears an app's data on an iPhone, so this ran with reset: none instead of "
                         f"{reset}.")
        return replace(check, app_path=None, bundle_id=bundle, reset="none", device=None, runtime=None), notes, record

    @staticmethod
    def _admit_installed(installed, check):
        from ..harness_api import unattended_allowed
        if not unattended_allowed(installed):
            raise Refused(NOT_UNATTENDED.format(app=installed), "not_unattended")
        if installed.startswith("com.apple."):
            raise Refused(PART_OF_IOS.format(app=_apple_name(installed)), "not_developer_build")
        if check.bundle_id and installed != check.bundle_id:
            raise Refused(NOT_THIS_APP.format(path=Path(check.app_path).name, built=installed, bundle=check.bundle_id),
                          "usage", "Point app.path at the build of the app the check names.")

    # -- devicectl ---------------------------------------------------------------------------------------------

    def _devices_allowed(self):
        if self.allowed is not None:
            return self.allowed()
        from ..native_helpers import devices_allowed
        return devices_allowed()

    def _apps_on(self, record):
        key = record.get("id") or record.get("udid")
        with self._lock:
            if key in self._apps:
                return self._apps[key]
        if not self._devices_allowed():
            raise Refused("Devices are off in this process (MOBSTER_NO_DEVICES=1).", "environment")
        udid = record.get("udid") or record.get("id")
        with tempfile.TemporaryDirectory(prefix="mobster-test-apps-") as folder:
            out = Path(folder) / "apps.json"
            code, output = self.runner(["xcrun", "devicectl", "device", "info", "apps", "--device", str(udid),
                                        "--json-output", str(out)])
            try:
                data = json.loads(out.read_text()) if out.is_file() else None
            except (OSError, ValueError):
                data = None
        if code != 0 or not isinstance(data, dict):
            last = next((line.strip() for line in reversed((output or "").splitlines()) if line.strip()), "")
            raise Refused(f"Mobster couldn't list the apps on “{record.get('name') or udid}”"
                          + (f": {last[:200]}" if last else "") + ".", "device",
                          "Keep the iPhone unlocked and connected, then try again.")
        apps = (data.get("result") or {}).get("apps") if isinstance(data.get("result"), dict) else None
        found = {}
        for app in apps if isinstance(apps, list) else ():
            if isinstance(app, dict) and isinstance(app.get("bundleIdentifier"), str):
                found[app["bundleIdentifier"]] = app
        with self._lock:
            self._apps[key] = found
        return found

    def _install(self, record, app_path, bundle):
        key = (record.get("id"), str(app_path))
        with self._lock:
            if key in self._installed:
                return self._installed[key]
        if not self._devices_allowed():
            raise Refused("Devices are off in this process (MOBSTER_NO_DEVICES=1).", "environment")
        installer = self.installer
        if installer is None:
            from ..phone_io.install import install as installer
        self.progress(f"Installing {Path(app_path).name} on {record.get('name') or record.get('id')}")
        try:
            result = installer(record, app_path)
        except Exception as error:  # PhoneIOError and the like: one sentence, and its fix
            raise Refused(str(error) or f"The install failed ({type(error).__name__}).", "install",
                          getattr(error, "fix", "") or "") from None
        installed = (result or {}).get("bundle_id") or bundle
        if not installed:
            raise Refused(f"Mobster installed {Path(app_path).name} but couldn't read its bundle ID.", "install")
        with self._lock:
            self._installed[key] = installed
            # Installed by Mobster for this run: a developer's build, whatever the phone's list says.
            self._apps.setdefault(record.get("id") or record.get("udid"), {})[installed] = {
                "bundleIdentifier": installed, DEVELOPER_FIELD: True}
        return installed


def _apple_name(bundle):
    tail = bundle.rsplit(".", 1)[-1]
    return {"Preferences": "Settings", "mobilesafari": "Safari", "MobileSMS": "Messages",
            "mobilenotes": "Notes", "mobilemail": "Mail"}.get(tail, re.sub(r"^mobile", "", tail) or bundle)


def _run(argv):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=DEVICECTL_TIMEOUT,
                                stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return 127, "Xcode's command-line tools aren't installed."
    except subprocess.TimeoutExpired:
        return 124, f"devicectl didn't answer in {DEVICECTL_TIMEOUT} s."
    return result.returncode, (result.stdout or "") + (result.stderr or "")
