"""`mobster sim doctor` rows for Android: SDK, platform-tools, emulator, an AVD, acceleration, devices."""

import platform

from ..sim.doctor import FAIL, OK, SKIP, WARN, check
from . import adb as adb_module

SDK_FIX = ("Install Android Studio, open it once, and let it install the SDK (about 3 GB with platform-tools and the "
           "emulator)")
EMULATOR_FIX = "In Android Studio: Settings > SDK Manager > SDK Tools > Android Emulator"
AVD_FIX = "In Android Studio: Device Manager > Create Virtual Device (an arm64 system image on Apple silicon)"


def run_checks(manager):
    """Rows for the SDK in use. ``manager`` is an EmulatorManager."""
    root = manager.root
    if root is None:
        return [check("android_sdk", "Android SDK", FAIL, "not found (no ANDROID_HOME, no ~/Library/Android/sdk)",
                      SDK_FIX)]
    rows = [check("android_sdk", "Android SDK", OK, str(root))]
    adb_path = adb_module.tool("adb", root)
    if not adb_path:
        rows.append(check("android_adb", "adb", FAIL, "platform-tools is missing",
                          "SDK Manager > SDK Tools > Platform-Tools"))
    else:
        version = manager._run([adb_path, "version"], 10, None)
        first = version.text.splitlines()[0] if version.code == 0 and version.text else ""
        rows.append(check("android_adb", "adb", OK if first else FAIL, first or "adb did not run",
                          "" if first else "Reinstall platform-tools from the SDK Manager"))
    emulator = adb_module.tool("emulator", root)
    rows.append(check("android_emulator", "Android emulator", OK if emulator else WARN,
                      emulator or "not installed: USB phones still work", "" if emulator else EMULATOR_FIX))
    if not emulator:
        rows.append(check("android_avd", "Android virtual device", SKIP, "needs the emulator"))
    else:
        avds = manager.avds()
        rows.append(check("android_avd", "Android virtual device", OK if avds else WARN,
                          ", ".join(avds) if avds else "none yet", "" if avds else AVD_FIX))
        machine = platform.machine()
        rows.append(check("android_accel", "Hardware acceleration", OK if machine == "arm64" else WARN,
                          "Apple silicon (Hypervisor.framework)" if machine == "arm64"
                          else f"{machine}: the emulator needs HVF and an x86_64 image",
                          "" if machine == "arm64" else "Use an x86_64 system image"))
    try:
        devices = manager.devices()
    except Exception as error:
        return rows + [check("android_devices", "Android devices", FAIL, f"adb failed: {error}",
                             "Run `adb kill-server`, then `adb devices -l`")]
    blocked = [row for row in devices if row["state"] != "device"]
    detail = "; ".join(f"{row['name']} ({row['kind']}, {row['state']})" for row in devices) or "none connected"
    fix = ("Unlock the phone and accept the USB debugging prompt" if any(r["state"] == "unauthorized" for r in blocked)
           else "Reconnect the device" if blocked else "")
    rows.append(check("android_devices", "Android devices", WARN if blocked else OK if devices else SKIP, detail, fix))
    return rows
