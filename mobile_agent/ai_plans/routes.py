"""The app's API for Settings › Models and Setup's AI step: the providers in providers.py."""

import threading
import urllib.error
import urllib.request

from . import compat_chat, device_login, providers

_signins = {}
_lock = threading.Lock()


class RouteError(ValueError):
    def __init__(self, message, status=400, code="bad_request"):
        super().__init__(message)
        self.status, self.code = status, code


def check_key(provider, key, base=None, opener=urllib.request.urlopen):
    """(True, None) when ``key`` opens the provider's model list, (False, why) when refused, else (None, None)."""
    url = (base or providers.base_url(provider)).rstrip("/")
    url = url + ("/v1/models" if provider.wire == providers.ANTH else "/models")
    headers = {"Accept": "application/json", "User-Agent": "Mobster"}
    if key:
        if provider.wire == providers.ANTH:
            headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        else:
            headers["Authorization"] = "Bearer " + key
    try:
        with opener(urllib.request.Request(url, headers=headers), timeout=10) as response:
            response.read(1 << 16)
        return True, None
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            return False, f"{provider.name} didn't accept that key."
        return None, None
    except (urllib.error.URLError, TimeoutError, OSError):
        if provider.mechanism == "local":
            return False, f"Nothing answers at {url.rsplit('/', 1)[0]}. Start {provider.name.split(' (')[0]} first."
        return None, None


def handle(keys, provider_id, action, body, opener=urllib.request.urlopen):
    provider = providers.get(provider_id)
    if provider is None:
        raise RouteError("Mobster doesn't know that provider.", 404, "not_found")
    body = body if isinstance(body, dict) else {}
    if action == "key":
        with device_login.credential_lock:
            record = providers.load_record(provider.id)
            original = record.copy()
        for name, field in (("key", "key"), ("model", "model"), ("baseUrl", "baseUrl")):
            value = body.get(name)
            if value is not None:
                if not isinstance(value, str) or len(value) > 4000:
                    raise RouteError(f"{name} is text")
                record[field] = value.strip()
        if "vision" in body:
            if not isinstance(body["vision"], bool):
                raise RouteError("vision is true or false")
            record["vision"] = body["vision"]
        if provider.mechanism == "local":
            record["enabled"] = True
        if provider.mechanism == "custom" and not record.get("baseUrl", "").startswith(("http://", "https://")):
            raise RouteError("Enter the endpoint's base URL, such as https://example.com/v1.")
        base = record.get("baseUrl")
        if base and not base.startswith("https://") and not compat_chat.plain_http_ok(base):
            # Checked before the key is tried: check_key would send it to that address.
            raise RouteError(compat_chat.PLAIN_HTTP.format(name=provider.name))
        if provider.mechanism in ("key", "custom") and not record.get("key"):
            raise RouteError("Paste the key.")
        ok, why = check_key(provider, record.get("key"), record.get("baseUrl"), opener)
        if ok is False:
            raise RouteError(why, 400, "key_rejected")
        with device_login.credential_lock:
            if providers.load_record(provider.id) != original:
                raise RouteError("Your saved provider changed. Try again.", 409, "credentials_changed")
            providers.save_record(provider.id, record)
        return {"saved": True, "checked": ok is True, "provider": public_one(provider.id)}
    if action == "use":
        if not providers.configured(provider):
            raise RouteError(f"Add {provider.name} first.", 409, "not_configured")
        keys.update({"smartProvider": provider.id})
        return {"using": provider.id}
    if action == "forget":
        with _lock:
            if provider.id in _signins:
                _signins[provider.id].update(cancel=True, state="cancelled", error=None)
            with device_login.credential_lock:
                providers.forget(provider.id)
        if (keys.state().get("smart") or {}).get("preference") == provider.id:
            keys.update({"smartProvider": None})
        return {"forgotten": provider.id}
    if action == "sign-in":
        if provider.mechanism != "device":
            raise RouteError(f"{provider.name} takes a key, not a sign-in.")
        with _lock:
            previous = _signins.get(provider.id)
            if previous is not None:
                previous["cancel"] = True
            current = {"state": "waiting", "error": None, "cancel": False}
            _signins[provider.id] = current
        try:
            attempt = device_login.start(provider.id, opener)
        except Exception as failure:
            with _lock:
                if _signins.get(provider.id) is current and not current["cancel"]:
                    current.update(state="failed", error="The provider didn't start the sign-in. Try again.")
            if isinstance(failure, device_login.DeviceError):
                raise RouteError(str(failure), 409, failure.code or "sign_in_failed") from None
            raise RouteError("The provider didn't start the sign-in. Try again.", 502, "sign_in_failed") from None
        with _lock:
            if _signins.get(provider.id) is not current or current["cancel"]:
                raise RouteError("You cancelled the sign-in. Nothing was saved.", 409, "cancelled")

        def save(provider_id, record):
            # Cancel, replace and remove share this lock, so a late token reply cannot restore a forgotten account.
            with _lock:
                if _signins.get(provider.id) is not current or current["cancel"]:
                    raise device_login.DeviceError("You cancelled the sign-in. Nothing was saved.", "cancelled")
                with device_login.credential_lock:
                    providers.save_record(provider_id, record)
                current.update(state="signed_in", error=None)

        def finish():
            try:
                device_login.wait(provider.id, attempt, opener,
                                  cancelled=lambda: current["cancel"], save=save)
                state, error = "signed_in", None
            except device_login.DeviceError as failure:
                state, error = ("cancelled" if failure.code == "cancelled" else "failed"), str(failure)
            except Exception:
                state, error = "failed", "The provider didn't finish the sign-in. Try again."
            with _lock:
                if _signins.get(provider.id) is current and not current["cancel"]:
                    current.update(state=state, error=error)
        threading.Thread(target=finish, daemon=True, name=f"mobster-sign-in-{provider.id}").start()
        return {"userCode": attempt["user_code"], "url": attempt["url"], "state": "waiting"}
    if action == "cancel":
        with _lock:
            current = _signins.get(provider.id)
            if current is not None:
                if current["state"] == "waiting":
                    current.update(cancel=True, state="cancelled", error=None)
            return {"state": current["state"] if current is not None else "cancelled"}
    raise RouteError("Unknown action.", 404, "not_found")


def public_one(provider_id):
    return next(p for p in public() if p["id"] == provider_id)


def public():
    rows = providers.public()
    with _lock:
        for row in rows:
            if row["id"] in _signins:
                row["signIn"] = {k: v for k, v in _signins[row["id"]].items() if k != "cancel"}
    return rows

