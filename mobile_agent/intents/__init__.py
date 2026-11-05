"""Deterministic path first: before tapping, ask whether the goal has a link that gets there (dark).

``atlas`` reads what the apps on a phone declare (URL schemes, associated domains) as "discovered" links, a layer
under ``deeplinks.LINKS``. ``callback`` builds and reads x-callback-url returns. ``plan`` proposes a link for a goal.
Everything is off unless ``MOBSTER_EXPERIMENTAL_INTENTS=1``. ``deeplinks.decide`` still governs every link; a
discovered link is never "verified" (that word is for links opened on a real iPhone, with a date).
"""

import os

ENV_FLAG = "MOBSTER_EXPERIMENTAL_INTENTS"


def enabled():
    """Whether the experimental deterministic-path layer is on (``MOBSTER_EXPERIMENTAL_INTENTS=1``)."""
    return os.environ.get(ENV_FLAG, "").strip().casefold() in ("1", "true", "yes", "on")

