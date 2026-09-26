"""Wall-clock bounds for tests, scaled on slow machines.

Timing assertions back up structural ones (which settle path ran, how many reads): the
absolute numbers are machine-dependent. Shared CI runners exceeded several bounds by a few
percent (24 Sep); CI sets MOBSTER_TEST_TIME_SCALE, local runs keep the strict bound.
"""

import os


def bound(seconds):
    return seconds * float(os.environ.get("MOBSTER_TEST_TIME_SCALE", "1") or "1")


def slow_machine():
    """True where timing bounds are scaled (CI): real-time race tests are skipped there."""
    return float(os.environ.get("MOBSTER_TEST_TIME_SCALE", "1") or "1") > 1
