"""Builds Mobster's network shim (netshim.m) for the iOS Simulator with Xcode's clang, once per source version."""

import hashlib
import os
import subprocess
from pathlib import Path

SOURCE = Path(__file__).with_name("netshim.m")
MIN_IOS = "15.0"
TIMEOUT = 120


class ShimError(Exception):
    """The shim couldn't be built: what happened, then what to do."""

    def __init__(self, message, fix="Install Xcode's command-line tools (`xcode-select --install`), then try again."):
        super().__init__(message)
        self.message, self.fix = message, fix


def cache_dir():
    return Path(os.environ.get("MOBSTER_CACHE_DIR") or Path.home() / "Library" / "Caches" / "Mobster")


def ensure_built(*, cache=None, run=subprocess.run):
    """The path of the compiled, ad-hoc signed shim; built on first use and kept by the hash of its source."""
    source = SOURCE.read_bytes()
    folder = Path(cache or cache_dir()) / "netshim" / hashlib.sha256(source).hexdigest()[:12]
    library = folder / "netshim.dylib"
    if library.is_file():
        return str(library)
    folder.mkdir(parents=True, exist_ok=True)
    partial = folder / f"netshim.{os.getpid()}.tmp"
    try:
        for command in (["xcrun", "--sdk", "iphonesimulator", "clang", "-dynamiclib", "-arch", "arm64",
                         "-arch", "x86_64", f"-mios-simulator-version-min={MIN_IOS}", "-fobjc-arc",
                         "-framework", "Foundation", "-o", str(partial), str(SOURCE)],
                        ["codesign", "-s", "-", "--force", str(partial)]):
            done = run(command, capture_output=True, text=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL)
            if done.returncode != 0:
                last = next((line for line in reversed((done.stderr or "").splitlines()) if line.strip()), "")
                raise ShimError(f"Mobster couldn't build its network shim: {last[:200] or command[0] + ' failed'}.")
        partial.replace(library)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ShimError(f"Mobster couldn't build its network shim ({type(error).__name__}).") from None
    finally:
        partial.unlink(missing_ok=True)
    return str(library)
