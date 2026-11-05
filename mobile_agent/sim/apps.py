"""The app under test: its Info.plist, and emptying its containers on a reset."""

import os
import plistlib
import re
import shutil
from io import BytesIO
from pathlib import Path

from .models import SimError

SIMULATOR_PLATFORM = "iPhoneSimulator"
BUILD_FOR_SIMULATOR = ("xcodebuild -scheme <Scheme> -destination 'generic/platform=iOS Simulator' "
                       "-derivedDataPath .mobster/build CODE_SIGNING_ALLOWED=NO build")
EMPTIED = ("Documents", "Library", "tmp")
# The icon a report shows beside the app's name: 2 times its 16 px, with room to spare for a 3x screen.
ICON_SIZE = 120
# An icon file name the asset catalog writes (AppIcon60x60, AppIcon76x76~ipad), never a path.
ICON_NAME = re.compile(r"[A-Za-z0-9 ._~-]{1,100}")
ICON_FILE_LIMIT = 2_000_000


def devices_root():
    return Path.home() / "Library" / "Developer" / "CoreSimulator" / "Devices"


def device_folder(udid):
    return devices_root() / udid


def read_info(app_path):
    """The .app's Info.plist as a dict. SimError("install") when it isn't a readable app bundle."""
    path = Path(app_path).expanduser()
    if not path.is_dir():
        raise SimError("install", f"{path} isn't an .app folder.",
                       "Pass the .app that xcodebuild built for the iOS Simulator (a folder ending in .app).")
    try:
        with open(path / "Info.plist", "rb") as stream:
            info = plistlib.load(stream)
    except FileNotFoundError:
        raise SimError("install", f"{path} has no Info.plist, so it isn't an app bundle.",
                       "Pass the .app that xcodebuild built for the iOS Simulator.") from None
    except (OSError, ValueError, plistlib.InvalidFileException):
        raise SimError("install", f"{path}/Info.plist can't be read.", "Build the app again.") from None
    if not isinstance(info, dict):
        raise SimError("install", f"{path}/Info.plist isn't a dictionary.", "Build the app again.")
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


# A device build's PNGs are Apple's CgBI variant, which Pillow can't read, so a device build has no icon here.
def icon_png(app_path, info, size=ICON_SIZE):
    from PIL import Image

    folder = Path(app_path).expanduser()
    names = list(info.get("CFBundleIconFiles") or ())
    for key in ("CFBundleIcons", "CFBundleIcons~ipad"):
        icons = info.get(key)
        primary = icons.get("CFBundlePrimaryIcon") if isinstance(icons, dict) else None
        if isinstance(primary, dict):
            names += list(primary.get("CFBundleIconFiles") or ()) + [primary.get("CFBundleIconName")]
    best = None
    for name in {name for name in names if isinstance(name, str) and ICON_NAME.fullmatch(name) and ".." not in name}:
        for file in folder.glob(f"{name}*.png"):
            if file.parent != folder or not file.is_file() or file.stat().st_size > ICON_FILE_LIMIT:
                continue
            try:
                with Image.open(file) as image:
                    image.verify()
                with Image.open(file) as image:
                    area = image.width * image.height
                    if image.width == image.height and image.width <= 1024 and (best is None or area > best[0]):
                        best = (area, file)
            except (OSError, ValueError, SyntaxError):
                continue
    if best is None:
        return None
    with Image.open(best[1]) as image:
        icon = image.convert("RGBA").resize((size, size), Image.LANCZOS)
    out = BytesIO()
    icon.save(out, "PNG", optimize=True)
    return out.getvalue()


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
    path = device_folder(udid) / "data" / "Library" / "Preferences" / "com.apple.launchservices.schemeapproval.plist"
    try:
        with open(path, "rb") as stream:
            data = plistlib.load(stream)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return {}
    return data if isinstance(data, dict) else {}


def built_for_simulator(info):
    platforms = info.get("CFBundleSupportedPlatforms")
    if isinstance(platforms, list) and platforms:
        return SIMULATOR_PLATFORM in platforms
    return str(info.get("DTPlatformName") or "").lower() == "iphonesimulator"


def device_build_error(path):
    return SimError("install", "This .app was built for a device. Build it for the iOS Simulator: "
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
        raise SimError("simulator", f"The simulator's folder {folder} is missing.",
                       "Delete it with `mobster sim delete` and run again.")
    if not container or not inside(container, folder):
        raise SimError("simulator", f"The container {container} resolves outside this simulator's folder "
                                    f"({os.path.realpath(container) if container else 'no path'}), so Mobster "
                                    "didn't empty it.",
                       "Use a simulator Mobster created (`mobster sim prepare`); a cloned one shares containers.")
    root = Path(os.path.realpath(container))
    for name in EMPTIED:
        folder = root / name
        if folder.is_symlink():
            raise SimError("simulator", f"{folder} is a symbolic link, so Mobster didn't empty it.",
                           "Erase the simulator with `mobster sim erase`.")
    return root


def empty_container(container, udid):
    root = guard_container(container, udid)
    removed = 0
    for name in EMPTIED:
        folder = root / name
        if not folder.is_dir():
            continue
        for entry in list(os.scandir(folder)):
            # a symlink is removed as a link, never followed
            path = Path(entry.path)
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(path)
            else:
                path.unlink()
            removed += 1
    return removed
