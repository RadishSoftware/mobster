"""The manager VerifyRun drives for an Android check: the simulator manager's interface over adb and one emulator or
phone. One manager serves one run; its lease is the emulator manager's."""

import re
from dataclasses import dataclass
from pathlib import Path

from ..sim.models import AppInfo, SimError
from . import adb as adb_module
from .adb import Adb, AdbError
from .conditions import AndroidConditions
from .driver import AndroidDriver
from .emulator import EmulatorManager

# Android release -> API level, for `runtime: Android 16`.
API_LEVELS = {"12": 31, "12L": 32, "13": 33, "14": 34, "15": 35, "16": 36}
BADGING_PACKAGE = re.compile(r"package: name='([^']+)'[^\n]*?versionName='([^']*)'")
BADGING_LABEL = re.compile(r"application-label:'([^']*)'")


@dataclass(frozen=True)
class AndroidRunTarget:
    """What VerifyRun reads off a leased device. ``udid`` and the two addresses exist because the iOS side's log line
    and report read them; Android has no WebDriverAgent."""
    serial: str
    name: str
    kind: str
    avd: str = ""
    runtime: str = ""

    @property
    def udid(self):
        return self.serial

    @property
    def device_type(self):
        return self.avd or self.name

    wda_url = ""
    mjpeg_url = ""


def _normal(text):
    return re.sub(r"[^a-z0-9]", "", str(text).casefold())


def avd_for(avds, device=None, runtime=None, api_of=lambda name: None):
    """The AVD a check's ``device`` and ``runtime`` name: an exact name, else the first whose name starts with the
    device, then the first on that Android release. None when nothing was asked for."""
    chosen = list(avds)
    if device:
        wanted = _normal(device)
        exact = [name for name in chosen if _normal(name) == wanted]
        chosen = exact or [name for name in chosen if _normal(name).startswith(wanted)]
    if runtime:
        level = API_LEVELS.get(re.sub(r"(?i)^android\s*", "", runtime).strip())
        if level is None:
            raise SimError("environment", f"“{runtime}” is not an Android release Mobster knows "
                                          f"({', '.join('Android ' + k for k in API_LEVELS)}).",
                           "Name the release, such as runtime: Android 16.")
        chosen = [name for name in chosen if api_of(name) == level]
    if (device or runtime) and not chosen:
        raise SimError("environment", f"No Android virtual device matches {device or ''} {runtime or ''}".strip() + ".",
                       "`mobster sim android list` shows what exists; create one in Android Studio's Device Manager.")
    return chosen[0] if (device or runtime) else None


class AndroidManager:
    # Fail closed: a manager is a phone's until it holds an emulator (verify.smart.is_real_device).
    real_device = True
    platform = "android"
    system_prefixes = ("com.android.", "com.google.android.", "android")

    def __init__(self, data_dir, *, name=None, emulators=None, adb_factory=None, phones=False):
        self.emulators = emulators or EmulatorManager(data_dir)
        self.name = name or ""   # an AVD name or an adb serial, from --android
        # A check runs on an emulator only: Mobster can't yet tell someone's own builds from the apps on a phone. The
        # device tools pass True; their own allowance decides which phone they may drive.
        self.phones = phones
        self._adb = adb_factory or (lambda serial: Adb(serial, self.emulators._run, self.emulators.adb.binary))

    # -- the device

    def _api_of(self, avd):
        ini = Path.home() / ".android" / "avd" / f"{avd}.ini"
        try:
            match = re.search(r"target=android-(\d+)", ini.read_text())
        except OSError:
            return None
        return int(match.group(1)) if match else None

    def acquire(self, device=None, runtime=None, *, timeout=600, cancelled=None):
        # A refusal here sends nothing, so the run's journal records it as rejected (verify/actions.py). ``cancelled``
        # (the runner's stop flag) is read before a device is held; an emulator boot isn't interrupted.
        from ..verify.actions import ActionNotDispatched
        if cancelled is not None and cancelled():
            raise ActionNotDispatched(SimError("environment", "The run was stopped before an Android device was held.",
                                               ""))
        serials = {row["serial"] for row in self.emulators.devices()}
        serial = self.name if self.name in serials else None
        avd = None if serial else (self.name or avd_for(self.emulators.avds(), device, runtime, self._api_of))
        lease = self.emulators.acquire(avd=avd, serial=serial, timeout=timeout)
        target = lease.target
        if target.kind != "emulator" and not self.phones:
            lease.release()
            raise ActionNotDispatched(SimError(
                "environment", f"“{target.name}” is an Android phone, and Mobster runs checks only on Android "
                "emulators for now: it can't yet tell your own builds from the other apps on a phone.",
                "Run the check on an emulator, such as mobster test --android Pixel_8."))
        adb = self._adb(target.serial)
        try:
            release = adb.prop("ro.build.version.release")
        except AdbError:
            release = ""
        lease.target = AndroidRunTarget(target.serial, target.name, target.kind, target.avd,
                                        f"Android {release}" if release else "")
        self.real_device = target.kind != "emulator"
        self.adb = adb
        return lease

    def build_driver(self, target, run_dir, mode):
        return AndroidDriver(target.serial, self._adb(target.serial))

    def front_app(self, target):
        return self._adb(target.serial).front_package() or None, None

    def conditions(self, spec, target, bundle, actions=None):
        return AndroidConditions(spec, self._adb(target.serial), bundle=bundle, emulator=target.kind == "emulator",
                                 actions=actions)

    # -- apps

    def app_info(self, app_path):
        path = Path(app_path)
        if path.suffix.lower() != ".apk" or not path.is_file():
            raise SimError("install", f"{app_path} is not an .apk file.", "Pass the .apk of the app the check names.")
        aapt = adb_module.tool_in_build_tools("aapt2", self.emulators.root)
        if aapt is None:
            raise SimError("install", "Mobster can't read the package name of an .apk without aapt2.",
                           "Install Android SDK Build-Tools from the SDK Manager, or name the app with bundle: and "
                           "install it yourself.")
        result = self.emulators._run([aapt, "dump", "badging", str(path)], 30, None)
        text = result.text
        match = BADGING_PACKAGE.search(text)
        if result.code != 0 or not match:
            raise SimError("install", f"aapt2 could not read {path.name}.", "Check that the file is a built APK.")
        label = BADGING_LABEL.search(text)
        return AppInfo(bundle_id=match.group(1), name=label.group(1) if label else match.group(1),
                       version=match.group(2), path=str(path))

    def installed_app(self, target, bundle_id):
        try:
            version = self._adb(target.serial).installed_version(bundle_id)
        except AdbError as error:
            raise SimError("install", str(error)) from None
        if version is None:
            raise SimError("install", f"{bundle_id} is not installed on {target.name}.",
                           "Pass app.path with the .apk, or install the app first.")
        return AppInfo(bundle_id=bundle_id, name=bundle_id, version=version, path=None)

    def install(self, target, app_path):
        info = self.app_info(app_path)
        try:
            self._adb(target.serial).install(app_path)
        except AdbError as error:
            raise SimError("install", str(error)) from None
        return info

    def reset(self, target, bundle_id, level, app_path=None):
        if self.real_device and level != "none":
            raise SimError("install", f"Mobster never clears or reinstalls an app on a phone, so it didn't reset "
                           f"{bundle_id} ({level}).", "Use reset: none on a phone, or run the check on an emulator.")
        adb = self._adb(target.serial)
        try:
            if level == "reinstall":
                adb.uninstall(bundle_id)
                adb.install(app_path)
            elif level == "data":
                adb.clear_data(bundle_id)
            elif level != "none":
                raise SimError("install", f"Mobster can't reset {bundle_id} at level {level!r} on Android.",
                               "Use reset: none, data or reinstall.")
        except AdbError as error:
            raise SimError("install", str(error)) from None

    def launch(self, target, bundle_id, args=(), env=None):
        if args or env:
            raise SimError("launch", "Launch arguments and environment aren't supported on Android.",
                           "Remove launch.args and launch.env from the check.")
        adb = self._adb(target.serial)
        try:
            adb.stop(bundle_id)
            adb.launch(bundle_id)
        except AdbError as error:
            raise SimError("launch", str(error)) from None

    def terminate(self, target, bundle_id):
        try:
            self._adb(target.serial).stop(bundle_id)
        except AdbError as error:
            raise SimError("launch", str(error)) from None

    def open_url(self, target, url):
        try:
            self._adb(target.serial).open_url(url)
        except AdbError as error:
            raise SimError("launch", str(error)) from None

    def screenshot(self, target, path, *, max_width=None, quality=75):
        from io import BytesIO

        from PIL import Image
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = self._adb(target.serial).screenshot_png(30)
        except AdbError as error:
            raise SimError("simulator", f"The screenshot failed: {error}") from None
        with Image.open(BytesIO(data)) as image:
            image = image.convert("RGB")
            if max_width and image.width > max_width:
                image = image.resize((int(max_width), max(1, round(image.height * max_width / image.width))),
                                     Image.LANCZOS)
            image.save(path, "JPEG", quality=int(quality), optimize=True)
        return path
