"""Files from this Mac to an iPhone with no cable: AirDrop to the one recipient named exactly, or iPhone Mirroring."""

import json
import os
import subprocess
from pathlib import Path

from . import PhoneIOError

AIRDROP_WAIT_S = 30
HELPER_SLACK_S = 30

PERMISSION = ("Mobster needs Accessibility to use AirDrop and iPhone Mirroring for you.",
              "Turn it on in System Settings › Privacy & Security › Accessibility, then try again.")


def helper(build=True):
    """The transfer helper's path: the Mac app's copy, else a build from source (Xcode's command line tools)."""
    from .. import native_helpers
    found = native_helpers.find("transfer")
    if found is None and build:
        try:
            found = native_helpers.build("transfer")
        except (LookupError, OSError, subprocess.SubprocessError, RuntimeError):
            found = None
    if found is None:
        raise PhoneIOError("This Mac is missing Mobster's transfer helper.", "tools",
                           "Install Xcode's command line tools (xcode-select --install), or use the Mobster app.")
    return str(found)


def _run(argv, timeout, runner=None):
    from ..native_helpers import require_devices
    require_devices()
    if runner is not None:
        return runner(argv, timeout)
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None
    except OSError as error:
        raise PhoneIOError(f"The transfer helper couldn't run ({type(error).__name__}).", "tools") from None
    return done.stdout


def _answer(output):
    for line in reversed((output or "").strip().splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _files(paths):
    files = [str(Path(p).expanduser().resolve()) for p in paths]
    if not files:
        raise PhoneIOError("Name at least one file.", "usage")
    for file in files:
        if not os.path.isfile(file):
            raise PhoneIOError(f"There is no file at {file}.", "usage")
    return files


def airdrop(paths, to, *, wait=AIRDROP_WAIT_S, transfer_wait=900, runner=None, tool=None):
    """AirDrop ``paths`` to the device named exactly ``to``; ``unknown``: the helper went silent after the press."""
    if not isinstance(to, str) or not to.strip() or len(to) > 120:
        raise PhoneIOError("Name the iPhone AirDrop should send to.", "usage")
    files = _files(paths)
    argv = [tool or helper(), "airdrop", "--to", to, "--timeout", str(int(wait)), "--transfer-timeout",
            str(int(transfer_wait)), *files]
    output = _run(argv, wait + transfer_wait + HELPER_SLACK_S, runner)
    answer = _answer(output)
    if answer is None:
        raise PhoneIOError(f"AirDrop to {to} stopped answering, so the files may or may not have arrived.",
                           "unknown", "Look in Photos on the iPhone before you send them again.")
    if answer.get("ok"):
        return {"sent": int(answer.get("sent") or len(files)), "seconds": float(answer.get("seconds") or 0)}
    code = answer.get("code")
    if code == "permission":
        raise PhoneIOError(PERMISSION[0], "permission", PERMISSION[1])
    if code == "not_found":
        error = PhoneIOError(f"AirDrop doesn't see {to}.", "airdrop_not_found",
                             "Unlock it, keep it near this Mac with Wi-Fi and Bluetooth on, and set AirDrop to "
                             "Contacts Only or Everyone for 10 Minutes.")
        error.seen = [str(name)[:80] for name in answer.get("seen") or ()][:20]
        raise error
    if code == "ambiguous":
        raise PhoneIOError(f"More than one device near this Mac is called {to}, so Mobster sent nothing.",
                           "airdrop_ambiguous", "Give the iPhone its own name in Settings › General › About › Name.")
    if code in ("failed", "cancelled"):
        raise PhoneIOError(f"AirDrop to {to} didn't go through: {answer.get('error') or 'it was declined'}.",
                           "airdrop_declined", "Accept it on the iPhone, then try again.")
    if code == "timeout":
        raise PhoneIOError(f"AirDrop to {to} didn't finish in time, so the files may or may not have arrived.",
                           "unknown", "Look in Photos on the iPhone before you send them again.")
    raise PhoneIOError(f"AirDrop didn't work: {answer.get('error') or code}.", "failed")


def mirroring_drop(paths, x=0.5, y=0.5, *, runner=None, tool=None):
    """Drop ``paths`` on the iPhone Mirroring window at (x, y), fractions of the window. Returns {"operation"}."""
    files = _files(paths)
    if not (0 <= float(x) <= 1 and 0 <= float(y) <= 1):
        raise PhoneIOError("x and y are fractions of the window, 0 to 1.", "usage")
    argv = [tool or helper(), "drop", "--x", f"{float(x):.4f}", "--y", f"{float(y):.4f}", *files]
    answer = _answer(_run(argv, 30, runner))
    if answer is None:
        raise PhoneIOError("The drop on iPhone Mirroring stopped answering, so the files may or may not have arrived.",
                           "unknown", "Look on the iPhone before you send them again.")
    if answer.get("ok"):
        return {"operation": int(answer.get("operation") or 1)}
    code = answer.get("code")
    if code == "permission":
        raise PhoneIOError(PERMISSION[0], "permission", PERMISSION[1])
    if code == "no_window":
        raise PhoneIOError("iPhone Mirroring isn't showing the iPhone.", "no_mirroring",
                           "Open iPhone Mirroring, lock the iPhone and keep it near this Mac, then try again.")
    if code == "refused":
        raise PhoneIOError("iPhone Mirroring didn't take the files: the app in front on the iPhone doesn't take drops.",
                           "mirroring_refused", "Open Photos or Files on the iPhone, then try again.")
    raise PhoneIOError(f"The drop on iPhone Mirroring didn't work: {answer.get('error') or code}.", "failed")
