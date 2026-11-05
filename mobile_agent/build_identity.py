"""Distinguish the standalone CLI's entry point from the Mac app's frozen agent."""

import sys
from pathlib import Path

# Only packaging/cli/entry.py selects this, before importing the command. A missing or damaged selection
# keeps the app's checks. Neither an environment variable nor renaming the app's agent selects the CLI.
STANDALONE_CLI = False


def app_bundle():
    """Whether the frozen process must enforce the Mac app's plan and signature checks."""
    if not getattr(sys, "frozen", False):
        return False
    if STANDALONE_CLI is not True:
        return True
    try:
        # The CLI entry point cannot opt an executable inside an app out of the app's checks.
        return any(parent.suffix == ".app" for parent in Path(sys.executable).resolve().parents)
    except (OSError, RuntimeError, TypeError, ValueError):
        return True
