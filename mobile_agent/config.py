"""Typed run budgets. One validated place for every loop bound.

``Agent`` keeps its keyword arguments and its ``ValueError`` contract; it
validates through this module so the CLI, the server, and the agent can share
one definition instead of re-checking magic numbers at each layer.
"""

from dataclasses import dataclass
from pathlib import Path

from .state import finite


@dataclass(frozen=True)
class RunBudgets:
    """Bounds for one agent run. All limits are inclusive upper bounds."""

    max_steps: int = 30
    max_seconds: float = 120
    max_helper_calls: int = 4
    settle_seconds: float = .6
    max_undispatched: int = 4
    max_extraction_retries: int = 1
    spend_cap_usd: float | None = None

    def __post_init__(self):
        if (type(self.max_steps) is not int or not 1 <= self.max_steps <= 10000
                or not finite(self.max_seconds) or self.max_seconds <= 0
                or not finite(self.settle_seconds) or not 0 <= self.settle_seconds <= 5
                or type(self.max_helper_calls) is not int or not 0 <= self.max_helper_calls <= 10000
                or type(self.max_undispatched) is not int or not 0 <= self.max_undispatched <= 100
                or type(self.max_extraction_retries) is not int or not 0 <= self.max_extraction_retries <= 10):
            raise ValueError("Invalid run budget")
        if (self.spend_cap_usd is not None
                and (not finite(self.spend_cap_usd) or self.spend_cap_usd <= 0)):
            raise ValueError("Spend cap must be a positive USD amount")


def load_env_file(path):
    """Load KEY=VALUE lines into the environment. Variables already set win.

    Explicit, never implicit: the desktop app passes its private env file, and
    developers can pass mobile_agent/.env. Blank lines and # comments are
    skipped; surrounding single or double quotes are removed. Returns the
    names loaded (never the values). A missing file loads nothing: the setup
    flow creates it when the first value is saved.
    """
    import os
    import re
    loaded = []
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return loaded
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        key = key.removeprefix("export ").strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"Invalid variable name in env file: {key!r}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded
