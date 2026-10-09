"""The app under test: its Info.plist, and emptying its containers on a reset.

Every folder a reset empties is checked first: its real path (symlinks resolved) must sit inside the
simulator's own folder, ~/Library/Developer/CoreSimulator/Devices/<UDID>. A path that resolves anywhere else
(a cloned simulator's shared containers, a symlink planted in a container) is refused, and nothing is removed.
"""

import os
from pathlib import Path
import plistlib
import shutil

from .simctl import sim_error

SIMULATOR_PLATFORM = "iPhoneSimulator"
BUILD_FOR_SIMULATOR = ("xcodebuild -scheme <Scheme> -destination 'generic/platform=iOS Simulator' "
                       "-derivedDataPath .mobster/build CODE_SIGNING_ALLOWED=NO build")
EMPTIED = ("Documents", "Library", "tmp")


def devices_root():
    return Path.home() / "Library" / "Developer" / "CoreSimulator" / "Devices"


def device_folder(udid):
    return devices_root() / udid


def read_info(app_path):
    """The .app's Info.plist as a dict. SimError("install") when it isn't a readable app bundle."""
    path = Path(app_path).expanduser()
    if not path.is_dir():
        raise sim_error("install", f"{path} isn't an .app folder.",
                        "Pass the .app that xcodebuild built for the iOS Simulator (a folder ending in .app).")
    try:
        with open(path / "Info.plist", "rb") as stream:
            info = plistlib.load(stream)
    except FileNotFoundError:
        raise sim_error("install", f"{path} has no Info.plist, so it isn't an app bundle.",
                        "Pass the .app that xcodebuild built for the iOS Simulator.") from None
    except (OSError, ValueError, plistlib.InvalidFileException):
        raise sim_error("install", f"{path}/Info.plist can't be read.", "Build the app again.") from None
    if not isinstance(info, dict):
        raise sim_error("install", f"{path}/Info.plist isn't a dictionary.", "Build the app again.")
    return info


def version_text(info):
    """ "1.0 (1)" from CFBundleShortVersionString and CFBundleVersion; either alone when that is all there is."""
    short = str(info.get("CFBundleShortVersionString") or "").strip()
    build = str(info.get("CFBundleVersion") or "").strip()
    if short and build and short != build:
        return f"{short} ({build})"
    return short or build


def display_name(info, bundle_id):
    return str(info.get("CFBundleDisplayName") or info.get("CFBundleName") or bundle_id)


def url_schemes(info):
    """The URL schemes the app declares (CFBundleURLTypes) as written, in order, without duplicates."""
    schemes = []
    for kind in info.get("CFBundleURLTypes") or ():
        for scheme in (kind.get("CFBundleURLSchemes") or ()) if isinstance(kind, dict) else ():
            scheme = str(scheme).strip()
            if scheme and scheme not in schemes:
                schemes.append(scheme)
    return schemes


def scheme_approvals(udid):
    """The simulator's remembered answers to "Open in “<App>”?" ({"<opener>--><scheme>": bundle id}), as last
    written to disk, or {}."""
    path = device_folder(udid) / "data" / "Library" / "Preferences" / "com.apple.launchservices.schemeapproval.plist"
    try:
        with open(path, "rb") as stream:
            data = plistlib.load(stream)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return {}
    return data if isinstance(data, dict) else {}


def built_for_simulator(info):
    """CFBundleSupportedPlatforms names the iOS Simulator (Xcode always writes it; DTPlatformName is the
    fallback for a hand-made bundle without it)."""
    platforms = info.get("CFBundleSupportedPlatforms")
    if isinstance(platforms, list) and platforms:
        return SIMULATOR_PLATFORM in platforms
    return str(info.get("DTPlatformName") or "").lower() == "iphonesimulator"


def device_build_error(path):
    return sim_error("install", "This .app was built for a device. Build it for the iOS Simulator: "
                                f"{BUILD_FOR_SIMULATOR}",
                     f"Rebuild {Path(path).name} with -destination 'generic/platform=iOS Simulator'.")


def inside(path, root):
    """Whether ``path`` resolves (symlinks followed) to a place strictly inside ``root``."""
    real, base = os.path.realpath(path), os.path.realpath(root)
    return real != base and real.startswith(base.rstrip(os.sep) + os.sep)


def guard_container(container, udid):
    """The container's real path, or SimError("simulator") when it isn't inside this simulator's folder."""
    folder = device_folder(udid)
    if not folder.is_dir():
        raise sim_error("simulator", f"The simulator's folder {folder} is missing.",
                        "Delete it with `mobster sim delete` and run again.")
    if not container or not inside(container, folder):
        raise sim_error("simulator", f"The container {container} resolves outside this simulator's folder "
                                     f"({os.path.realpath(container) if container else 'no path'}), so Mobster "
                                     "didn't empty it.",
                        "Use a simulator Mobster created (`mobster sim prepare`); a cloned one shares containers.")
    return Path(os.path.realpath(container))


def empty_container(container, udid):
    """Empty Documents, Library and tmp of one data or app-group container, keeping the folders. Returns the
    number of entries removed. Symlinks inside are removed as links, never followed."""
    root = guard_container(container, udid)
    removed = 0
    for name in EMPTIED:
        folder = root / name
        if folder.is_symlink():
            raise sim_error("simulator", f"{folder} is a symbolic link, so Mobster didn't empty it.",
                            "Erase the simulator with `mobster sim erase`.")
        if not folder.is_dir():
            continue
        for entry in list(os.scandir(folder)):
            path = Path(entry.path)
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(path)
            else:
                path.unlink()
            removed += 1
    return removed
