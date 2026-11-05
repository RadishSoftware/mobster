"""Find (or build) ``mobster-pcap``, Mobster's own pcapd client."""

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

NAME = "mobster-pcap"
SOURCE = Path(__file__).resolve().with_name(f"{NAME}.c")
LIBRARIES = ("libimobiledevice-1.0", "libplist-2.0")


def _cached():
    if not SOURCE.is_file():
        return None
    from ..paths import user_data_dir
    digest = hashlib.sha256(SOURCE.read_bytes()).hexdigest()[:8]
    return Path(user_data_dir()) / "helpers" / f"{NAME}-{digest}"


def find():
    """The app's bundled tool or a previously built copy; None when neither exists."""
    from ..mac_tools import bundled_tools_dir, executable
    folder = bundled_tools_dir()
    if folder is not None and (path := executable(folder, NAME)):
        return str(path)
    cached = _cached()
    if cached is not None and cached.is_file() and os.access(cached, os.X_OK):
        return str(cached)
    return None


def build():
    """Compile the packaged tool source against Homebrew's libimobiledevice."""
    target = _cached()
    if target is None:
        raise LookupError("This Mobster has no mobster-pcap source to build")
    if target.is_file():
        return str(target)
    pkg_config = shutil.which("pkg-config") or next(
        (path for path in ("/opt/homebrew/bin/pkg-config", "/usr/local/bin/pkg-config") if os.access(path, os.X_OK)),
        None)
    if not pkg_config:
        raise LookupError("pkg-config isn't installed. Run `brew install pkg-config libimobiledevice`.")
    try:
        flags = subprocess.run([pkg_config, "--cflags", "--libs", *LIBRARIES], capture_output=True, text=True,
                               timeout=30, check=True).stdout.split()
    except (OSError, subprocess.SubprocessError):
        raise LookupError("libimobiledevice isn't installed. Run `brew install libimobiledevice`.") from None
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    partial = target.with_suffix(".partial")
    try:
        subprocess.run(["xcrun", "clang", "-O2", str(SOURCE), "-o", str(partial), *flags], capture_output=True,
                       timeout=120, check=True)
    except (OSError, subprocess.SubprocessError):
        partial.unlink(missing_ok=True)
        raise LookupError("mobster-pcap couldn't be built here. Install Xcode's command line tools with "
                          "`xcode-select --install`.") from None
    os.replace(partial, target)
    return str(target)


def locate():
    """``find()``, else ``build()``. LookupError says what is missing."""
    return find() or build()
