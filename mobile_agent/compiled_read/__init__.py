"""Compiled reading, dark behind ``MOBSTER_EXPERIMENTAL_COMPILED_READ`` (default off).

A screen is a typed interface, so reading it can be compiled the way acting is (learned/programs.py):

- ``extractors``: a screen the model read into rows once is read again by code when it has the same shape;
- ``delta``: a chained step sends the model the tree changes against a reference screen, not the whole tree;
- ``routes``: a program learned for one app version is only offered for that version.

Nothing here calls a model or imports a UI. The flag off, every hook returns what it was given.
"""

import os

ENV_FLAG = "MOBSTER_EXPERIMENTAL_COMPILED_READ"


def enabled(env=None):
    """Whether compiled reading is switched on (``MOBSTER_EXPERIMENTAL_COMPILED_READ=1``)."""
    env = os.environ if env is None else env
    return (env.get(ENV_FLAG) or "").strip().casefold() in ("1", "true", "yes", "on")

