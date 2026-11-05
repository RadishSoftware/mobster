"""Where a Network X-ray lives: a private folder (0700) on this Mac. The capture stays only while read, or when kept."""

import time
from pathlib import Path

from .. import paths


def root():
    return Path(paths.user_data_dir()) / "xray" / "network"


def new_folder(now=None, base=None):
    """A new private folder named for the time. Never reuses one: a second in the same second gets -2, -3."""
    base = Path(base) if base else root()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now if now is not None else time.time()))
    for number in range(1, 100):
        folder = base / (stamp if number == 1 else f"{stamp}-{number}")
        try:
            base.mkdir(parents=True, exist_ok=True)
            folder.mkdir(mode=0o700)
        except FileExistsError:
            continue
        folder.chmod(0o700)
        return folder
    raise FileExistsError("Too many X-rays in one second")


def capture_path(folder):
    return Path(folder) / "capture.pcapng"


def discard_capture(folder):
    """Delete the capture (and nothing else) in ``folder``."""
    capture_path(folder).unlink(missing_ok=True)


def conclude(folder, names, *, keep=False, only=None, source=None):
    # The folder's capture goes whatever happens, so a failed analysis leaves no packet metadata behind.
    from . import analyze, report, summary
    try:
        result = analyze.analyze_file(source or capture_path(folder), names=names)
        if only:
            result = summary.only(result, only)
        return result, report.write(result, folder)
    finally:
        if not keep:
            discard_capture(folder)
