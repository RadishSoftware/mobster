"""HTTP-facing domain errors shared by runs and saved workflows."""

from .errors import MobsterError


class APIError(MobsterError):
    def __init__(self, message, status=409, code="conflict", **details):
        super().__init__(message)
        self.status, self.code, self.details = status, code, details
