"""HTTP-facing domain errors shared by runs and saved workflows."""

from .errors import MobsterError


class APIError(MobsterError):
    def __init__(self, message, status=409, code="conflict", **details):
        super().__init__(message)
        self.status, self.code, self.details = status, code, details


# Why the phone guard stopped a run before it acted (agent_hooks.STOP_CODES): a run result's ``stop_code``,
# a refused start's ``stop`` detail, and ``mobster run``'s ``error_type`` name these. Each comes with one plain
# sentence (lockscreen.py) and none ever carries a passcode, a code or a digit count.
PHONE_STOP_CODES = ("phone_locked", "unlock_declined", "passcode_failed", "face_id", "app_locked",
                    "apple_confirmation", "unplugged", "keychain_locked", "blocked_app")
