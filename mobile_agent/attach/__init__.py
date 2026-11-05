"""Experimental app scoring from the accessibility tree, including WebView and Flutter detection.

Dark behind ``MOBSTER_EXPERIMENTAL_ATTACH=1``. Nothing here runs unless a caller checks ``enabled()``.
"""

import os

ENV_FLAG = "MOBSTER_EXPERIMENTAL_ATTACH"


def enabled():
    """Whether the experimental attach is switched on (``MOBSTER_EXPERIMENTAL_ATTACH=1``)."""
    return os.environ.get(ENV_FLAG, "").strip().casefold() in ("1", "true", "yes", "on")
