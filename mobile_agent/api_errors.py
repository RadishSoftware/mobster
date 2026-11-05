"""HTTP-facing domain errors shared by runs and saved workflows."""

from .errors import MobsterError


class APIError(MobsterError):
    def __init__(self, message, status=409, code="conflict", **details):
        super().__init__(message)
        self.status, self.code, self.details = status, code, details


def nothing_started(error):
    """``error`` as a new APIError that also says ``"started": false``: Mobster recorded and ran nothing for the
    request. Only a layer that has just found no task under the request's Idempotency-Key may say it, since a task is
    recorded under that key before anything is sent to a phone. Without the field, a client must treat a 5xx as an
    outcome it can't know."""
    return APIError(str(error), error.status, error.code, **{**error.details, "started": False})
