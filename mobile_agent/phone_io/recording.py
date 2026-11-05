"""Bounded USB screen recordings from the iPhone's QuickTime capture device, without WDA."""

import fcntl
import hashlib
import math
import os
import re
import subprocess
import tempfile
from pathlib import Path

from .. import native_helpers
from ..env_file import open_private, private_dir
from ..listen import ListenError, _answer, capture_hints, helper_path
from . import PhoneIOError, capture_cancellation


def record_video(udid: str, *, seconds: float = 10, output: str | Path) -> dict:
    """Save a completed MOV without overwriting anything; errors and cancellation leave no partial output."""
    if not isinstance(udid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", udid):
        raise PhoneIOError("Name the USB iPhone to record with --device.", "usage")
    if (isinstance(seconds, bool) or not isinstance(seconds, (float, int))
            or not 1 <= seconds <= 300):
        raise PhoneIOError("Use a recording duration from 1 to 300 seconds.", "usage")
    if not native_helpers.devices_allowed():
        raise PhoneIOError("Devices are off in this process (MOBSTER_NO_DEVICES=1).", "devices_off")
    target = Path(output).expanduser().absolute()
    if target.suffix.lower() != ".mov":
        raise PhoneIOError("Use an --output path ending in .mov.", "usage")
    if os.path.lexists(target):
        raise PhoneIOError("The recording file already exists. Choose another --output path.", "usage")
    try:
        executable = helper_path()
    except ListenError as error:
        raise PhoneIOError(str(error), "tools") from None

    from ..paths import user_data_dir
    locks = user_data_dir() / "video-capture"
    key = hashlib.sha256(udid.replace("-", "").upper().encode()).hexdigest()[:24]
    try:
        private_dir(locks)
        with open_private(locks / (key + ".lock"), "a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PhoneIOError("Mobster is already recording this iPhone. Stop that recording first.",
                                   "capture_busy") from None
            return _record(executable, udid, seconds, target, capture_hints(udid), lock.fileno())
    except OSError:
        raise PhoneIOError("Mobster couldn't save the recording. "
                           "Check the output folder and its permissions.") from None


def _stop(process):
    try:
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=7)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            except ProcessLookupError:
                process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        raise PhoneIOError("Mobster couldn't stop video capture. Check running phone-audio processes before "
                           "recording again.", "capture_failed") from None


def _record(executable, udid, seconds, target, hints, lock_fd):
    # Same filesystem permits atomic publication with link(): an existing target always wins.
    with capture_cancellation(), tempfile.TemporaryDirectory(prefix=".mobster-record-", dir=target.parent) as work:
        os.chmod(work, 0o700)
        argv = [str(executable), "record-video", "--udid", udid, "--seconds", str(seconds), "--work", work]
        argv += hints
        process = None
        try:
            with capture_cancellation(defer=True):
                process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE,
                                           text=True, start_new_session=True, pass_fds=(lock_fd,))
            try:
                stdout, _ = process.communicate(timeout=seconds + 20)
            except subprocess.TimeoutExpired:
                raise PhoneIOError("The video helper didn't finish in time. Check the USB connection.",
                                   "capture_failed") from None
        finally:
            if process is not None:
                try:
                    _stop(process)
                finally:
                    # Popen.__exit__ would wait without a timeout if the bounded stop failed.
                    process.stdout.close()
                    process.stderr.close()
        if process.returncode == 2:
            raise PhoneIOError("This Mobster native helper doesn't support video recording. Update Mobster and its "
                               "native helpers together.", "tools")
        answer = _answer(stdout)
        if not answer.get("ok"):
            raise PhoneIOError(str(answer.get("message") or "The iPhone couldn't record video."),
                               str(answer.get("error") or "capture_failed"))
        movie = Path(work) / "video.mov"
        if process.returncode or not movie.is_file() or movie.is_symlink() or movie.stat().st_size == 0:
            raise PhoneIOError("The video helper produced no completed movie.", "capture_failed")
        duration = answer.get("durationSeconds")
        if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
            raise PhoneIOError("The video helper produced no readable movie duration.", "capture_failed")
        os.chmod(movie, 0o600)
        try:
            os.link(movie, target)
        except FileExistsError:
            raise PhoneIOError("The recording file already exists. Choose another --output path.", "usage") from None
        return {"ok": True, "path": str(target), "seconds": seconds, "bytes": movie.stat().st_size,
                "audio": answer.get("audio") is True, "transport": "usb", "format": "mov",
                "match": answer.get("match", "udid"), "durationSeconds": duration}
