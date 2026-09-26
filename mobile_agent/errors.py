"""One root for every Mobster domain error. Retry policy lives here, not at call sites.

``MobsterError`` is never raised directly. It exists so callers can catch the
framework's own failures as one family -- transport, journal, pool, API -- while
stdlib errors keep their own meaning: ``ValueError`` is invalid input,
``TimeoutError`` is an exhausted budget, ``Cancelled`` is an operator stop.

No subclass is retryable by default. An interrupted request's outcome is unknown,
and an action that may have run must never be repeated automatically, so the
safe default is to fail closed and let the operator decide.
"""


class MobsterError(RuntimeError):
    """Root of the framework's failures. Never retry without a proven dispatch boundary."""

    retryable = False


class SpendCapExceeded(MobsterError):
    """Observed inference spend reached the operator's cap. A stop signal, not an error to retry."""


class Cancelled(Exception):
    """Operator stop. Control flow, not a failure: never a MobsterError, never retried, never an error status."""
