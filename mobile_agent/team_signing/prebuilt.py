"""The unsigned helper runner Mobster signs: where it is, and whether it has the patches this Mobster has.

A Mac without Xcode can't build the runner, so it signs one built elsewhere (once per Mobster release, with the pinned
WebDriverAgent and ``wda_patches``). Three places, in this order: ``MOBSTER_PREBUILT_RUNNER`` (a folder holding
``WebDriverAgentRunner-Runner.app`` and a ``patches.txt`` with its ``wda_patches.patch_digest()``), the Mac app's own
copy in ``Contents/Resources/helper/``, and the runner an Xcode build on this Mac left in its derived-data folder
(whose digest the build recorded). A runner built with other patches is never used: it lacks the edit that keeps the
helper off the phone's network.

Shipping the app's copy is a release step of its own; this module only reads it.
"""

import os
from pathlib import Path

from ..devkit import DevError
from ..wda import patches as wda_patches

NAME = "WebDriverAgentRunner-Runner.app"
ENV = "MOBSTER_PREBUILT_RUNNER"
APP_COPY = Path("/Applications/Mobster.app/Contents/Resources/helper")
DERIVED = Path("Build/Products/Debug-iphoneos") / NAME


def _digest_of(folder):
    try:
        return (Path(folder) / "patches.txt").read_text().strip()
    except OSError:
        return None


def candidates(derived=None, built_patches=None, environ=None, app_copy=APP_COPY):
    environ = os.environ if environ is None else environ
    if environ.get(ENV):
        folder = Path(environ[ENV]).expanduser()
        yield folder / NAME, _digest_of(folder), "MOBSTER_PREBUILT_RUNNER"
    yield Path(app_copy) / NAME, _digest_of(app_copy), "Mobster for Mac"
    if derived is not None:
        yield Path(derived) / DERIVED, built_patches, "this Mac's Xcode build"


def locate(derived=None, built_patches=None, environ=None, app_copy=APP_COPY):
    """(path, where) of a prebuilt runner with this Mobster's patches. DevError, with what to do, when there is none."""
    wanted = wda_patches.patch_digest()
    stale = None
    for path, digest, where in candidates(derived, built_patches, environ, app_copy):
        if not (path / "Info.plist").is_file():
            continue
        if digest == wanted:
            return path, where
        stale = where
    if stale:
        raise DevError(
            f"The helper runner from {stale} was built with older patches than this Mobster has.",
            "Update Mobster for Mac, which carries a matching helper.",
        )
    raise DevError(
        "Mobster has no prebuilt helper to sign.",
        "Update Mobster for Mac, which carries one, or point MOBSTER_PREBUILT_RUNNER at a folder with "
        f"{NAME} and a patches.txt.",
    )
