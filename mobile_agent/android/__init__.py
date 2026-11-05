"""Android: emulators and USB phones through adb, behind MOBSTER_EXPERIMENTAL_ANDROID=1 until the owner turns it on."""

import os

FLAG = "MOBSTER_EXPERIMENTAL_ANDROID"


def enabled(environ=None):
    environ = os.environ if environ is None else environ
    return (environ.get(FLAG) or "").strip().casefold() in {"1", "true", "yes", "on"}


def add_test_flag(where):
    """`mobster test --android`, shown only while the flag is on."""
    if enabled():
        where.add_argument("--android", nargs="?", const="", metavar="AVD_OR_SERIAL",
                           help="run on an Android emulator: an AVD name or its adb serial, or none for any idle "
                                "emulator (experimental)")
