"""One root for every Mobster domain error."""


class MobsterError(RuntimeError):
    """Root of the framework's failures. Never retry without a proven dispatch boundary."""

    retryable = False


class SimError(RuntimeError):
    """A simulator problem; ``kind`` and ``fix`` are its stable public details."""

    def __init__(self, kind: str, message: str, fix: str = ""):
        super().__init__(message)
        self.kind, self.fix = kind, fix
