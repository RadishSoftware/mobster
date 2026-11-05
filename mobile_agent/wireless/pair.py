"""``mobster-wifi-pair``: turns a trusted iPhone's Wi-Fi reachability on or off over USB (SPEC §3.7 item 1)."""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from .. import native_helpers

NAME = "mobster-wifi-pair"
ACTIONS = ("enable", "disable", "status")
TIMEOUT = 20.0
# Exit codes of the tool -> the problem they mean (words.PROBLEMS).
EXITS = {3: "needs_cable", 4: "not_trusted", 5: "refused", 6: "failed", 2: "usage"}
SOURCE = Path(__file__).resolve().parents[2] / "desktop" / "iphone-tools" / f"{NAME}.c"
LIBRARIES = ("libimobiledevice-1.0", "libplist-2.0")


class PairError(RuntimeError):
    """The tool ran and said no."""

    def __init__(self, code, detail=""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


def command(tool, udid, action):
    """The tool's argv. ValueError for a UDID or an action it doesn't take."""
    from ..devices import UDID_PATTERN
    if action not in ACTIONS:
        raise ValueError(f"The action is one of {', '.join(ACTIONS)}")
    if not isinstance(udid, str) or not UDID_PATTERN.fullmatch(udid):
        raise ValueError("A UDID is 24 to 40 hex digits and dashes")
    return [str(tool), "--udid", udid, f"--{action}"]


def interpret(code, stdout, stderr=""):
    if code == 0:
        try:
            value = json.loads((stdout or "").strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise PairError("failed", "mobster-wifi-pair gave no answer") from None
        if not isinstance(value, dict) or not isinstance(value.get("enabled"), bool):
            raise PairError("failed", "mobster-wifi-pair gave no answer")
        return value["enabled"]
    detail = " ".join((stderr or "").split())[:300]
    raise PairError(EXITS.get(code, "failed"), detail or f"mobster-wifi-pair exited with {code}")


def find():
    """The tool: the Mac app's bundled copy, else one built earlier from this checkout; None when neither exists."""
    from ..mac_tools import bundled_tools_dir, executable
    folder = bundled_tools_dir()
    if folder is not None and (path := executable(folder, NAME)):
        return path
    cached = _cached()
    if cached is not None and cached.is_file() and os.access(cached, os.X_OK):
        return str(cached)
    return None


def _cached():
    if not SOURCE.is_file():
        return None
    from ..paths import machine_data_dir
    digest = hashlib.sha256(SOURCE.read_bytes()).hexdigest()[:8]
    return Path(machine_data_dir()) / "helpers" / f"{NAME}-{digest}"


def build():
    target = _cached()
    if target is None:
        raise LookupError("This Mobster has no mobster-wifi-pair")
    if target.is_file():
        return str(target)
    pkg_config = shutil.which("pkg-config") or next(
        (path for path in ("/opt/homebrew/bin/pkg-config", "/usr/local/bin/pkg-config") if os.access(path, os.X_OK)),
        None)
    if not pkg_config:
        raise LookupError("pkg-config isn't installed (brew install pkg-config libimobiledevice)")
    try:
        flags = subprocess.run([pkg_config, "--cflags", "--libs", *LIBRARIES], capture_output=True, text=True,
                               timeout=30, check=True).stdout.split()
    except (OSError, subprocess.SubprocessError):
        raise LookupError("libimobiledevice isn't installed (brew install libimobiledevice)") from None
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    partial = target.with_suffix(".partial")
    try:
        subprocess.run(["xcrun", "clang", "-O2", str(SOURCE), "-o", str(partial), *flags], capture_output=True,
                       timeout=120, check=True)
    except (OSError, subprocess.SubprocessError):
        partial.unlink(missing_ok=True)
        raise LookupError("mobster-wifi-pair couldn't be built here (Xcode's command line tools are needed)") from None
    os.replace(partial, target)
    return str(target)


def locate():
    """``find()``, else ``build()``; None when this Mac can't have the tool (the Xcode instruction then)."""
    path = find()
    if path:
        return path
    try:
        return build()
    except LookupError:
        return None


def run(udid, action, *, tool=None, timeout=TIMEOUT):
    native_helpers.require_devices()
    tool = tool or locate()
    if not tool:
        raise LookupError("mobster-wifi-pair isn't available on this Mac")
    try:
        completed = subprocess.run(command(tool, udid, action), capture_output=True, text=True, timeout=timeout,
                                   stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise PairError("failed", f"mobster-wifi-pair didn't answer in {timeout:g} s") from None
    except OSError as error:
        raise PairError("failed", f"mobster-wifi-pair couldn't start: {error}") from None
    return interpret(completed.returncode, completed.stdout, completed.stderr)
