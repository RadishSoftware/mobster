"""Device-code sign-ins for plans whose provider allows them (OAuth 2.0 device grant, RFC 8628)."""

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import providers

GRANT = "urn:ietf:params:oauth:grant-type:device_code"
FLOWS = {
    "grok": {"client_id": "b1a00492-073a-47ea-816f-4c329264a828",
             "device_url": "https://auth.x.ai/oauth2/device/code", "token_url": "https://auth.x.ai/oauth2/token",
             "scope": "openid profile email offline_access grok-cli:access api:access"},
    "copilot": {"client_env": "MOBSTER_GITHUB_CLIENT_ID",
                "device_url": "https://github.com/login/device/code",
                "token_url": "https://github.com/login/oauth/access_token", "scope": "read:user",
                "exchange_url": "https://api.github.com/copilot_internal/v2/token"},
}
REFRESH_MARGIN = 300
_lock = threading.Lock()
# Route state locks precede this lock; it serializes credential changes, never provider requests.
credential_lock = threading.Lock()


class DeviceError(RuntimeError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def client_id(provider_id, env=None):
    env = os.environ if env is None else env
    flow = FLOWS[provider_id]
    return flow.get("client_id") or (env.get(flow.get("client_env", "")) or "").strip() or None


def available(provider_id, env=None):
    return provider_id in FLOWS and bool(client_id(provider_id, env))


def _post(url, form, opener=urllib.request.urlopen):
    request = urllib.request.Request(url, urllib.parse.urlencode(form).encode(), method="POST", headers={
        "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded", "User-Agent": "Mobster"})
    try:
        with opener(request, timeout=30) as response:
            reply = json.loads(response.read(1 << 20) or b"{}")
        if not isinstance(reply, dict):
            raise DeviceError("The provider returned an invalid sign-in response.", "invalid_response")
        return reply
    except urllib.error.HTTPError as error:
        try:
            reply = json.loads(error.read(65536) or b"{}")
            return reply if isinstance(reply, dict) and reply else {"error": f"http_{error.code}"}
        except ValueError:
            return {"error": f"http_{error.code}"}


def start(provider_id, opener=urllib.request.urlopen, env=None):
    """Ask for a device code: {"user_code", "url", "device_code", "interval", "expires_at"}."""
    cid = client_id(provider_id, env)
    if not cid:
        raise DeviceError("GitHub Copilot needs Mobster's own GitHub app, which this build doesn't have yet.",
                          "unavailable")
    flow = FLOWS[provider_id]
    reply = _post(flow["device_url"], {"client_id": cid, "scope": flow["scope"]}, opener)
    if not reply.get("device_code") or not reply.get("user_code"):
        raise DeviceError(f"The sign-in didn't start ({reply.get('error') or 'no code came back'}).", "device_failed")
    return {"user_code": reply["user_code"], "device_code": reply["device_code"],
            "url": reply.get("verification_uri_complete") or reply.get("verification_uri")
            or reply.get("verification_url"),
            "interval": max(2, int(reply.get("interval") or 5)),
            "expires_at": time.time() + int(reply.get("expires_in") or 900)}


def wait(provider_id, attempt, opener=urllib.request.urlopen, pause=time.sleep, cancelled=lambda: False, env=None,
         save=None):
    """Poll until the person finishes; saves and returns the record."""
    flow, cid = FLOWS[provider_id], client_id(provider_id, env)
    interval = attempt["interval"]
    while time.time() < attempt["expires_at"]:
        pause(interval)
        if cancelled():
            raise DeviceError("You cancelled the sign-in. Nothing was saved.", "cancelled")
        reply = _post(flow["token_url"], {"client_id": cid, "device_code": attempt["device_code"], "grant_type": GRANT},
                      opener)
        error = reply.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error in ("access_denied", "expired_token"):
            raise DeviceError("The sign-in was cancelled or ran out of time. Nothing was saved.", error)
        if error or not reply.get("access_token"):
            raise DeviceError(f"The sign-in failed ({error or 'no token'}).", error or "no_token")
        record = {"access_token": reply["access_token"], "refresh_token": reply.get("refresh_token"),
                  "expires_at": time.time() + int(reply.get("expires_in") or 3600), "saved_at": time.time()}
        if provider_id == "copilot":
            record = {"github_token": reply["access_token"], "saved_at": time.time()}
            record.update(_exchange(record["github_token"], opener))
        if cancelled():
            raise DeviceError("You cancelled the sign-in. Nothing was saved.", "cancelled")
        if save is not None:
            save(provider_id, record)
        else:
            with credential_lock:
                providers.save_record(provider_id, record)
        return record
    raise DeviceError("The sign-in code expired. Try again.", "expired_token")


def _exchange(github_token, opener=urllib.request.urlopen):
    """GitHub's Copilot token for a GitHub token: {access_token, expires_at, baseUrl}."""
    request = urllib.request.Request(FLOWS["copilot"]["exchange_url"], headers={
        "Authorization": f"token {github_token}", "Accept": "application/json", "User-Agent": "Mobster"})
    try:
        with opener(request, timeout=20) as response:
            data = json.loads(response.read(1 << 20) or b"{}")
    except urllib.error.HTTPError as error:
        raise DeviceError("This GitHub account has no Copilot plan Mobster can use." if error.code in (401, 403, 404)
                          else f"GitHub answered {error.code}.", "not_eligible") from None
    api = ((data.get("endpoints") or {}).get("api") or "").rstrip("/")
    if not api.startswith("https://"):  # the Copilot token only ever goes over https
        api = "https://api.individual.githubcopilot.com"
    return {"access_token": data.get("token"), "expires_at": float(data.get("expires_at") or time.time() + 1500),
            "baseUrl": api}


def access_token(provider_id, opener=urllib.request.urlopen, now=None):
    """A bearer for the chat client, refreshed (Grok) or exchanged again (Copilot) when it expires."""
    now = time.time() if now is None else now
    with _lock:
        with credential_lock:
            record = providers.load_record(provider_id)
            original = record.copy()
        if not record.get("access_token"):
            raise DeviceError(f"Sign in to {providers.get(provider_id).name} first.", "signed_out")
        if float(record.get("expires_at") or 0) - now > REFRESH_MARGIN:
            return record["access_token"]
        if provider_id == "copilot":
            record.update(_exchange(record["github_token"], opener))
        else:
            if not record.get("refresh_token"):
                raise DeviceError("Your Grok sign-in ended. Sign in again.", "signed_out")
            reply = _post(FLOWS[provider_id]["token_url"], {"grant_type": "refresh_token",
                                                            "client_id": client_id(provider_id),
                                                            "refresh_token": record["refresh_token"]},
                          opener)
            if reply.get("access_token"):
                record.update(access_token=reply["access_token"],
                              refresh_token=reply.get("refresh_token") or record["refresh_token"],
                              expires_at=now + int(reply.get("expires_in") or 3600))
        with credential_lock:
            if providers.load_record(provider_id) != original:
                raise DeviceError("Your sign-in changed. Try again.", "sign_in_changed")
            if provider_id != "copilot" and not reply.get("access_token"):
                providers.forget(provider_id)
                raise DeviceError("Your Grok sign-in ended. Sign in again.", "signed_out")
            providers.save_record(provider_id, record)
        return record["access_token"]
