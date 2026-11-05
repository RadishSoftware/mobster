"""The opt-in screen-to-action corpus, dark behind ``MOBSTER_EXPERIMENTAL_CORPUS`` (default off).

Every completed run is a list of (screen, action) pairs on real iOS. With the person's opt-in (``consent``), the
run listener writes one record per step to a local JSONL file (``store``): the screen as roles and hashed labels
(``skeleton``), the action without its text, the app and version, the outcome. Nothing leaves the Mac: ``mobster
corpus export`` writes a masked bundle for the person to read, and sharing is a later decision.

With the flag off, or the opt-in off, the hooks return what they were given and no file is made.
"""

import os

ENV_FLAG = "MOBSTER_EXPERIMENTAL_CORPUS"


def enabled(env=None):
    """Whether the corpus is switched on at all (``MOBSTER_EXPERIMENTAL_CORPUS=1``)."""
    env = os.environ if env is None else env
    return (env.get(ENV_FLAG) or "").strip().casefold() in ("1", "true", "yes", "on")


def install(hooks):
    """Add ``mobster corpus`` to the engine's commands, only with the flag on (harness_api._load calls this)."""
    if enabled():
        from . import cli
        hooks.cli.append(cli)
