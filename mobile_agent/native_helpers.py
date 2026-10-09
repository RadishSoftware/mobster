"""Small native helpers the tracks use (seam S12). Frozen after the seams merge.

- ``find(name)``: the helper ``name`` in the frozen Mac app (Contents/Resources/<name>, vision_judge's rule), else
  the build cache; None when neither has it.
- ``build(name)``: compile ``mobile_agent/helper_<name>.swift`` with ``xcrun swiftc`` into
  ``user_data_dir()/helpers/<name>-<sha8>`` (a source checkout, `mobster` from PyPI). LookupError when Xcode's
  command line tools are missing.
- ``devices_allowed()``: False under ``MOBSTER_NO_DEVICES=1`` (local CI): wireless.devicectl, phone_io.files and
  mobster-wifi-pair raise instead of running then. Each track calls it in its own module.

desktop/scripts/build-sidecar.sh compiles every ``mobile_agent/helper_*.swift`` to
``Contents/Resources/<stem without helper_, _ -> ->`` (helper_doc_text.swift -> doc-text), so ``find("doc-text")``
and ``build("doc_text")`` name the same helper.
"""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Optional

SOURCE_DIR = Path(__file__).resolve().parent
NO_XCODE = "Xcode's command line tools are needed for this"


def devices_allowed() -> bool:
    """Whether this process may talk to real devices (False under MOBSTER_NO_DEVICES=1, as in local CI)."""
    return os.environ.get("MOBSTER_NO_DEVICES", "").strip() not in ("1", "true", "yes")


def require_devices() -> None:
    """Raise RuntimeError under MOBSTER_NO_DEVICES=1 (tests and local CI never reach a phone)."""
    if not devices_allowed():
        raise RuntimeError("Devices are off in this process (MOBSTER_NO_DEVICES=1)")


def _bundle_name(name):
    return name.replace("_", "-")


def _source(name):
    return SOURCE_DIR / f"helper_{name.replace('-', '_')}.swift"


def _cache_dir():
    from .paths import user_data_dir
    return Path(user_data_dir()) / "helpers"


def _cached(name):
    source = _source(name)
    if not source.is_file():
        return None
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:8]
    return _cache_dir() / f"{name.replace('-', '_')}-{digest}"


def find(name: str) -> Optional[Path]:
    """The helper's executable: the frozen app's Contents/Resources/<name>, else the build cache; None when missing."""
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen:
        bundled = _bundle_name(name)
        for candidate in (Path(frozen).parent / "Resources" / bundled,
                          Path(sys.executable).resolve().parent.parent / "Resources" / bundled):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
    cached = _cached(name)
    if cached is not None and cached.is_file() and os.access(cached, os.X_OK):
        return cached
    return None


def build(name: str) -> Path:
    """Compile ``helper_<name>.swift`` into the build cache (once per source version); returns the executable."""
    source = _source(name)
    if not source.is_file():
        raise FileNotFoundError(f"No helper named {name}")
    target = _cached(name)
    if target.is_file():
        return target
    if shutil.which("xcrun") is None:
        raise LookupError(NO_XCODE)
    try:
        subprocess.run(["xcrun", "--find", "swiftc"], check=True, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        raise LookupError(NO_XCODE) from None
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    partial = target.with_suffix(".partial")
    command = ["xcrun", "swiftc", "-O"]
    if "@main" in source.read_text(errors="replace"):
        command.append("-parse-as-library")
    subprocess.run(command + [str(source), "-o", str(partial)], check=True, capture_output=True, timeout=600)
    os.replace(partial, target)
    return target
