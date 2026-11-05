"""Bounded USB packet traces through Apple's Remote Virtual Interface, without TLS interception."""

import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from . import PhoneIOError, capture_cancellation

# tcpdump holds the phone lock, so it must end by itself even when Mobster is SIGKILLed. Mobster stops it at
# `seconds`; the kernel's SIGALRM, which survives exec, ends it this long after that.
HARD_STOP_GRACE = 15


def die_after(seconds):
    """A preexec_fn that arms SIGALRM in the child; the pending alarm survives exec into tcpdump."""
    return lambda: signal.alarm(seconds)


def binary(name):
    path = shutil.which(name)
    if not path and Path("/Library/Apple/usr/bin", name).is_file():
        path = str(Path("/Library/Apple/usr/bin", name))
    if not path:
        raise PhoneIOError(f"{name} isn't installed. Install Xcode and its device support tools.", "tools")
    return path


def rvi_for(text, udid):
    for line in text.splitlines():
        if re.search(r"(?<![A-Za-z0-9-])" + re.escape(udid) + r"(?![A-Za-z0-9-])", line, re.I):
            found = re.search(r"\b(rvi\d+)\b", line)
            if found:
                return found.group(1)
    return None


def rvi_command(tool, *arguments):
    try:
        result = subprocess.run([tool, *arguments], capture_output=True, text=True, timeout=15,
                                stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise PhoneIOError("rvictl didn't answer. Its outcome is unknown; "
                           "inspect `rvictl -l` before trying again.") from None
    except KeyboardInterrupt:
        raise PhoneIOError("rvictl was interrupted. Its outcome is unknown; "
                           "inspect `rvictl -l` before trying again.") from None
    except OSError:
        raise PhoneIOError("Mobster couldn't start rvictl. Check Xcode's device support tools.", "tools") from None
    if result.returncode:
        raise PhoneIOError("rvictl refused the request. Unlock the USB iPhone and check this Mac's device permissions.")
    return result.stdout + result.stderr


def clear_stale(rvictl, udid, state):
    """With the phone lock held, no capture is alive, so a state file is a dead capture's leftover."""
    if not state.exists():
        return
    if rvi_for(rvi_command(rvictl, "-l"), udid):
        rvi_command(rvictl, "-x", udid)
    state.unlink(missing_ok=True)


def stop(process):
    try:
        if process.poll() is None:
            try:
                process.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass  # The process can exit between poll and signal; wait still reaps it.
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        raise PhoneIOError("Mobster couldn't stop tcpdump. Inspect running capture processes "
                           "before trying again.") from None


def capture(record, output, *, seconds=30, packets=10000, snaplen=128):
    """Capture beside a running task. A separate per-phone lock owns the RVI lifecycle, not the UI lease."""
    from ..env_file import open_private, private_dir
    from ..mac_tools import usb_attached
    from ..native_helpers import require_devices
    from ..paths import user_data_dir
    if (isinstance(seconds, bool) or not isinstance(seconds, int) or not 1 <= seconds <= 300 or
            isinstance(packets, bool) or not isinstance(packets, int) or not 1 <= packets <= 100000 or
            isinstance(snaplen, bool) or not isinstance(snaplen, int) or not 64 <= snaplen <= 65535):
        raise PhoneIOError("Use 1–300 seconds, 1–100000 packets and a snap length of 64–65535 bytes.", "usage")
    require_devices()
    udid = record.get("udid")
    if record.get("kind") != "usb" or not udid or not usb_attached(udid):
        raise PhoneIOError("Packet capture needs a paired iPhone connected by USB.", "needs_cable")
    rvictl, tcpdump = binary("rvictl"), binary("tcpdump")
    folder = user_data_dir() / "network-capture"
    private_dir(folder)
    key = hashlib.sha256(udid.upper().encode()).hexdigest()[:24]
    with open_private(folder / (key + ".lock"), "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PhoneIOError(f"Mobster is already capturing this iPhone. Stop that capture first; an orphaned "
                               f"one ends by itself within {HARD_STOP_GRACE} seconds of its time limit.") from None
        state = folder / (key + ".json")
        with capture_cancellation():
            clear_stale(rvictl, udid, state)
            return _capture(rvictl, tcpdump, udid, output, seconds, packets, snaplen, lock.fileno(), state)


def _capture(rvictl, tcpdump, udid, output, seconds, packets, snaplen, lock_fd, state):
    from ..env_file import open_private
    target = Path(output).expanduser().absolute()
    try:
        stream = open(target, "xb", opener=lambda p, flags: os.open(p, flags, 0o600))
    except FileExistsError:
        raise PhoneIOError("The capture file already exists. Choose another --output path.", "usage") from None
    except OSError:
        raise PhoneIOError("Mobster couldn't create the capture file. Check the output folder.") from None
    owned, process, interface = False, None, None
    cleanup_error = None
    try:
        listing = rvi_command(rvictl, "-l")
        interface = rvi_for(listing, udid)
        if interface is None:
            # Journal the intent first: if Mobster dies before `-x`, the next start removes this interface.
            with open_private(state, "w") as intent:
                json.dump({"owned": True}, intent)
            result = rvi_command(rvictl, "-s", udid)
            interface = rvi_for(result, udid)
            if not interface or "[SUCCEEDED]" not in result:
                raise PhoneIOError("rvictl didn't identify a created interface. Inspect `rvictl -l` before retrying.")
            owned = True
        try:
            # A surviving tcpdump must keep the phone locked if Mobster is killed without cleanup.
            with capture_cancellation(defer=True):
                process = subprocess.Popen([tcpdump, "-i", interface, "-n", "-U", "-s", str(snaplen),
                                            "-c", str(packets), "-w", "-"], stdin=subprocess.DEVNULL,
                                           stdout=stream, stderr=subprocess.DEVNULL, pass_fds=(lock_fd,),
                                           preexec_fn=die_after(seconds + HARD_STOP_GRACE))
            try:
                process.wait(timeout=seconds)
            except subprocess.TimeoutExpired:
                stop(process)
            if process.returncode not in (0, -signal.SIGINT):
                raise PhoneIOError("tcpdump couldn't capture packets. Check BPF capture permissions on this Mac; "
                                   "Apple's packet-capture instructions require administrator access.")
        except OSError:
            raise PhoneIOError("Mobster couldn't start tcpdump. Check this Mac's capture permissions.",
                               "tools") from None
    finally:
        try:
            if process is not None:
                stop(process)
        finally:
            try:
                stream.close()
            finally:
                if owned:
                    try:
                        rvi_command(rvictl, "-x", udid)
                        state.unlink(missing_ok=True)
                    except PhoneIOError:
                        cleanup_error = "The capture's RVI could not be removed. Inspect `rvictl -l`."
                try:
                    if target.stat().st_size == 0:
                        target.unlink()
                except FileNotFoundError:
                    pass  # Removing the output during capture must not hide its original error or cleanup warning.
                failure = sys.exception()
                if cleanup_error and failure is not None:
                    message = str(failure) if isinstance(failure, PhoneIOError) else "Packet capture was interrupted."
                    raise PhoneIOError(f"{message} {cleanup_error}", getattr(failure, "code", "failed")) from None
    try:
        size = target.stat().st_size
    except FileNotFoundError:
        size = 0
    if size < 24:
        message = "No complete capture file remains. Check capture permissions and the output path before trying again."
        raise PhoneIOError(f"{message} {cleanup_error}" if cleanup_error else message)
    return {"ok": True, "path": str(target), "bytes": size, "interface": interface,
            "snaplen": snaplen, "packetLimit": packets, "secondsLimit": seconds, "warning": cleanup_error}
