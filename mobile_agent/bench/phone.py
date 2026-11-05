"""The phone's health before every attempt: WDA answers, has a session and is unlocked."""

from dataclasses import dataclass

from ..transport import HTTP

# WDA serves one request at a time, so a stuck read could block it for minutes.
HEALTH_TIMEOUT = 3


@dataclass
class Health:
    ok: bool
    locked: bool | None = None
    session: str | None = None
    reason: str = ""


def health(url, timeout=HEALTH_TIMEOUT):
    """GET-only: WDA status, session and lock state. Never unlocks, never creates a session."""
    client = HTTP(url)
    try:
        try:
            status = client.request("GET", "/status", timeout=timeout)
        except Exception as error:
            return Health(False, reason=f"WDA unreachable: {type(error).__name__}")
        session = status.get("sessionId") if isinstance(status, dict) else None
        value = status.get("value") if isinstance(status, dict) else None
        if isinstance(value, dict) and value.get("ready") is False:
            return Health(False, session=session, reason="WDA reports not ready")
        try:
            locked = client.request("GET", "/wda/locked", timeout=timeout).get("value")
        except Exception as error:
            return Health(False, session=session, reason=f"lock state unavailable: {type(error).__name__}")
        if locked is True:
            return Health(False, locked=True, session=session, reason="phone is locked")
        if locked is not False:
            return Health(False, session=session, reason="lock state unavailable: expected a boolean")
        if not session:
            return Health(False, locked=False, reason="WDA has no active session")
        return Health(True, locked=False, session=session)
    finally:
        client.close()

