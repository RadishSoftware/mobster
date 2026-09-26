"""Optional extensions to the core agent.

The core drives an iPhone through WebDriverAgent (WDA). A build may add other
device targets in a ``mobile_agent.private`` package, which is not part of the
public distribution. When that package is present, ``load()`` imports it once and
calls its ``register(hooks)``; it adds its drivers (``drivers.register_driver``),
snapshot sources (``state.SOURCE_TRAITS``) and the hooks below. Without it every
hook stays empty and the core behaves exactly the same.
"""

import importlib
import threading

PACKAGE = __package__ + ".private"


class Hooks:
    """Extension points. Each one is optional; the core checks before using it."""

    def __init__(self):
        # (**options) -> Driver, for a run target that is not WDA (compose.build_target_driver).
        self.target_driver = None
        # (driver, options, cancelled) -> screen reader callable, or None (compose.build_visual).
        self.screen_reader = None
        # Command-line additions: objects with ``add_arguments(subparsers, commands)`` and
        # ``run(args, output, run_task)``, which returns an exit code or None when the core
        # should handle the command (``__main__``).
        self.cli = []
        # (config) -> a ``server.Runtime`` subclass for this config, or None (``server.serve``).
        self.runtime_for = None
        # Evaluation harness targets (``evals.harness``): objects with
        # ``add_arguments(target_group)`` and ``probe(args)`` (a probe, or None if not selected).
        self.eval_targets = []


hooks = Hooks()
_lock = threading.Lock()
_loaded = False


def load():
    """Import the optional extension once. Returns the hooks, filled or empty."""
    global _loaded
    with _lock:
        if _loaded:
            return hooks
        _loaded = True
        try:
            module = importlib.import_module(PACKAGE)
        except ModuleNotFoundError as error:
            if error.name != PACKAGE:
                raise
            return hooks
        module.register(hooks)
        return hooks
