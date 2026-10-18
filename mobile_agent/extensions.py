"""Optional extensions to the core agent.

The core drives an iPhone through WebDriverAgent (WDA). A build may add other
device targets in a ``mobile_agent.private`` package, which is not part of the
public distribution. When that package is present, ``load()`` imports it once and
calls its ``register(hooks)``; it adds its drivers (``drivers.register_driver``),
snapshot sources and the hooks below. Without it every
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
        # Command-line additions: objects with ``add_arguments(subparsers, commands)`` and
        # ``run(args, output)``, which returns an exit code or None when the core
        # should handle the command (``__main__``).
        self.cli = []
        # (config) -> a ``server.Runtime`` subclass for this config, or None (``serve.serve``).
        self.runtime_for = None
        # Evaluation harness targets (``evals.harness``): objects with
        # ``add_arguments(target_group)`` and ``probe(args)`` (a probe, or None if not selected).
        self.eval_targets = []


hooks = Hooks()
_lock = threading.Lock()
_loaded = False
_load_error = None


def load():
    """Import the optional extension once. Returns the hooks, filled or empty."""
    global _loaded, _load_error
    with _lock:
        if _load_error is not None:
            raise _load_error
        if _loaded:
            return hooks
        try:
            module = importlib.import_module(PACKAGE)
        except ModuleNotFoundError as error:
            if error.name != PACKAGE:
                _load_error = error
                raise
            _loaded = True
            return hooks
        except Exception as error:
            _load_error = error
            raise
        register = getattr(module, "register", None)
        # A folder with no __init__.py, such as a leftover __pycache__, imports as an empty namespace package.
        if register is not None:
            try:
                register(hooks)
            except Exception as error:
                # Registration may already have changed drivers or hooks. Never retry that partial setup.
                _load_error = error
                raise
        _loaded = True
        return hooks
