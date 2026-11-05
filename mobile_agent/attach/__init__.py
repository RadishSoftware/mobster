"""Attach to what an app draws with: a WebView's DOM, a Flutter semantics tree, and a score for every app.

Dark behind ``MOBSTER_EXPERIMENTAL_ATTACH=1``. Nothing here runs unless a caller checks ``enabled()``.
"""

import os

ENV_FLAG = "MOBSTER_EXPERIMENTAL_ATTACH"


def enabled():
    """Whether the experimental attach is switched on (``MOBSTER_EXPERIMENTAL_ATTACH=1``)."""
    return os.environ.get(ENV_FLAG, "").strip().casefold() in ("1", "true", "yes", "on")
