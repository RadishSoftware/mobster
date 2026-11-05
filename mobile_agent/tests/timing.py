"""Wall-clock bounds for tests, scaled by MOBSTER_TEST_TIME_SCALE where the machine is slow."""

import os


def bound(seconds):
    return seconds * float(os.environ.get("MOBSTER_TEST_TIME_SCALE", "1") or "1")


def slow_machine():
    """True where timing bounds are scaled (CI): real-time race tests are skipped there."""
    return float(os.environ.get("MOBSTER_TEST_TIME_SCALE", "1") or "1") > 1
