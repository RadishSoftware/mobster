"""Long-form tasks (track ``longform``, DESIGN.md)."""

TRACK = "longform"


def enabled():
    """Whether long tasks run: the track is loaded and on (its run field is registered), and ``MOBSTER_LONGFORM``
    isn't "off" (the switch the benchmark's "before" runs can use; every task then runs as it did before long
    tasks)."""
    import os
    if os.environ.get("MOBSTER_LONGFORM", "").strip().casefold() in ("off", "0", "false", "no"):
        return False
    from .. import harness_api
    return not harness_api.disabled(TRACK) and TRACK in harness_api.run_fields()

