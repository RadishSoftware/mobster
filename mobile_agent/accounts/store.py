"""accounts.json: the profiles a person runs (Settings › Profiles), each on one app and (optionally) one phone."""

import contextlib
import errno
import fcntl
import json
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path

from .. import secret_filter

STORE = "accounts.json"
STORE_LOCK = "accounts.lock"
LOCK_TIMEOUT = 10.0
MAX_ACCOUNTS = 200
MAX_LINKS = 10
MAX_VARIABLES = 20
NOTES_CHARS = 2000
SIGNATURE_CHARS = 300
VALUE_CHARS = 1000
LINK_LABEL_CHARS = 40
URL_CHARS = 500

ID = re.compile(r"[a-f0-9]{12}")
# Instagram, TikTok, X, Threads and LinkedIn vanity names: letters, digits, dots, underscores and hyphens.
HANDLE = re.compile(r"[A-Za-z0-9._-]{1,64}")
BUNDLE = re.compile(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
VARIABLE = re.compile(r"[a-z][a-z0-9_]{0,31}")
# Names a task's placeholders already mean (context.fill): a variable can't take one.
RESERVED = frozenset({"handle", "account", "app", "signature", "link", "links", "notes"})
FIELDS = frozenset({"handle", "app", "device", "notes", "signature", "links", "variables", "region", "language",
                    "timezone"})
# What the phone should be set to for this profile (fleet_health.py checks it before a task): optional.
REGION = re.compile(r"[A-Za-z]{2}")
LANGUAGE = re.compile(r"[A-Za-z]{2,3}")
TIMEZONE = re.compile(r"[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+){0,2}")

NOT_FOUND = "No profile has that name. Add it in Settings › Profiles."
SECRET = "Profiles don't keep passwords, card numbers or API keys. Leave that value out."


class AccountNotFound(LookupError):
    pass


class AmbiguousAccount(LookupError):
    pass


def handle_of(value):
    """``value`` as a stored handle: no ``@``, no surrounding space. ValueError when it isn't one."""
    if not isinstance(value, str):
        raise ValueError("handle is the profile's name on the app, such as @acme")
    handle = value.strip().lstrip("@").strip()
    if not HANDLE.fullmatch(handle):
        raise ValueError("A handle is 1–64 letters, digits, dots, underscores or hyphens, such as @acme")
    return handle


def display(account):
    """"@acme on Instagram"."""
    return f"@{account['handle']} on {account['app']['name']}"


def _text(value, name, limit):
    """``name`` as a person reads it ("A signature"): the profile form shows the message as it is (P05-18)."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{name} is text of at most {limit:,} characters")
    if len(value) > limit:
        raise ValueError(f"{name} can be at most {limit:,} characters. Shorten it and try again.")
    value = value.strip()
    if value and looks_secret(value):
        raise ValueError(SECRET)
    return value


def looks_secret(text):
    """secret_filter's password words, API keys, card numbers, SSNs, IBANs and an email with a password beside it."""
    f = secret_filter
    return (f.MASK in text or bool(f.KEYWORD.search(text)) or any(p.search(text) for p in (f.API_KEY, f.SSN, f.IBAN))
            or bool(f._cards(text)) or bool(f.EMAIL_PASSWORD.search(text)))


def _app(value):
    if not isinstance(value, dict) or set(value) - {"bundleId", "name"}:
        raise ValueError("app is {bundleId, name}: the app this profile is on")
    bundle, name = value.get("bundleId"), value.get("name")
    if not isinstance(bundle, str) or not BUNDLE.fullmatch(bundle) or len(bundle) > 155:
        raise ValueError("app.bundleId is the app's bundle id, such as com.burbn.instagram")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        raise ValueError("app.name is the app's name, 1–80 characters")
    return {"bundleId": bundle, "name": name.strip()}


def _expectation(value, name, pattern, hint, fold):
    """An expected region, language or time zone: None to clear, else the value in its usual case."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not pattern.fullmatch(value.strip()) or len(value.strip()) > 64:
        raise ValueError(f"{value!r:.40} isn't a {name} Mobster can use. Use {hint}, or leave it empty.")
    return fold(value.strip())


def _timezone(value):
    zone = _expectation(value, "timezone", TIMEZONE, "a time zone name such as America/Chicago", lambda v: v)
    if zone is not None:
        import zoneinfo
        known = zoneinfo.available_timezones()
        if known and zone not in known:
            raise ValueError(f"{zone[:40]!r} isn't a time zone Mobster knows. Use a name such as America/Chicago, "
                             "or leave it empty.")
    return zone


def _links(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_LINKS:
        raise ValueError(f"links is a list of at most {MAX_LINKS} {{label, url}}")
    out, labels = [], set()
    for link in value:
        if not isinstance(link, dict) or set(link) - {"label", "url"}:
            raise ValueError("Each link is {label, url}")
        label = _text(link.get("label"), "A link's label", LINK_LABEL_CHARS)
        url = _text(link.get("url"), "A link's url", URL_CHARS)
        if not re.fullmatch(r"https?://[^\s]{1,490}", url):
            raise ValueError("A link's url starts with https:// or http://")
        if not label:
            raise ValueError("Give each link a label, such as Shop")
        if label.casefold() in labels:
            raise ValueError(f"Two links are called {label}")
        labels.add(label.casefold())
        out.append({"label": label, "url": url})
    return out


def _variables(value):
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > MAX_VARIABLES:
        raise ValueError(f"variables is an object of at most {MAX_VARIABLES} names and values")
    out = {}
    for name, text in value.items():
        if not isinstance(name, str) or not VARIABLE.fullmatch(name):
            raise ValueError("A variable's name is lowercase letters, digits and underscores, such as caption")
        if name in RESERVED:
            raise ValueError(f"{name} is already a profile value. Choose another name.")
        out[name] = _text(text, f"The variable {name}", VALUE_CHARS)
    return out


def clean(body, partial=False):
    """The validated fields of ``body`` (every field for a new profile; only those given for a change)."""
    if not isinstance(body, dict) or set(body) - FIELDS:
        raise ValueError("A profile has handle, app, device, notes, signature, links, variables, "
                         "region, language and timezone")
    if not partial and ("handle" not in body or "app" not in body):
        raise ValueError("Name the profile's handle and its app")
    out = {}
    if "handle" in body:
        out["handle"] = handle_of(body["handle"])
    if "app" in body:
        out["app"] = _app(body["app"])
    if "device" in body:
        device = body["device"]
        if device is not None and (not isinstance(device, str) or not 1 <= len(device.strip()) <= 128):
            raise ValueError("device is the phone's id or name, or null for the default phone")
        out["device"] = device.strip() if isinstance(device, str) else None
    if "notes" in body or not partial:
        out["notes"] = _text(body.get("notes"), "Notes", NOTES_CHARS)
    if "signature" in body or not partial:
        out["signature"] = _text(body.get("signature"), "A signature", SIGNATURE_CHARS)
    if "links" in body or not partial:
        out["links"] = _links(body.get("links"))
    if "variables" in body or not partial:
        out["variables"] = _variables(body.get("variables"))
    if "region" in body:
        out["region"] = _expectation(body["region"], "region", REGION, "a two-letter country code such as US",
                                     str.upper)
    if "language" in body:
        out["language"] = _expectation(body["language"], "language", LANGUAGE, "a language code such as en",
                                       str.lower)
    if "timezone" in body:
        out["timezone"] = _timezone(body["timezone"])
    return out


def _valid(entry):
    try:
        return (isinstance(entry, dict) and ID.fullmatch(entry.get("id") or "") is not None
                and HANDLE.fullmatch(entry.get("handle") or "") is not None and isinstance(entry.get("app"), dict)
                and isinstance(entry["app"].get("bundleId"), str) and isinstance(entry["app"].get("name"), str))
    except TypeError:
        return False


class AccountStore:
    """accounts.json in ``data_dir``."""

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / STORE
        self.lock_path = self.data_dir / STORE_LOCK

    def read(self):
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {"version": 1, "accounts": []}
        items = data.get("accounts") if isinstance(data, dict) else None
        accounts = []
        for entry in items if isinstance(items, list) else ():
            if not _valid(entry):
                continue
            links = entry.get("links") if isinstance(entry.get("links"), list) else []
            accounts.append({"id": entry["id"], "handle": entry["handle"],
                             "app": {"bundleId": entry["app"]["bundleId"], "name": entry["app"]["name"]},
                             "device": entry.get("device") if isinstance(entry.get("device"), str) else None,
                             "deviceName": (entry.get("deviceName") if isinstance(entry.get("deviceName"), str)
                                            else None),
                             "notes": entry.get("notes") if isinstance(entry.get("notes"), str) else "",
                             "signature": entry.get("signature") if isinstance(entry.get("signature"), str) else "",
                             "links": [link for link in links if isinstance(link, dict)
                                       and isinstance(link.get("label"), str) and isinstance(link.get("url"), str)],
                             "variables": {k: v for k, v in (entry.get("variables") or {}).items()
                                           if isinstance(k, str) and isinstance(v, str)}
                             if isinstance(entry.get("variables"), dict) else {},
                             **{key: entry[key] for key in ("region", "language", "timezone")
                                if isinstance(entry.get(key), str) and entry[key]},
                             "createdAt": entry.get("createdAt") if isinstance(entry.get("createdAt"), int) else 0,
                             "updatedAt": entry.get("updatedAt") if isinstance(entry.get("updatedAt"), int) else 0})
        return {"version": 1, "accounts": accounts}

    def all(self):
        return self.read()["accounts"]

    def get(self, account_id):
        """The stored account with this id, else None."""
        return next((a for a in self.all() if a["id"] == account_id), None)

    @contextlib.contextmanager
    def edit(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            end = time.monotonic() + LOCK_TIMEOUT
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN) or time.monotonic() >= end:
                        raise OSError("Another Mobster process is holding the profile list") from None
                    time.sleep(.05)
            data = self.read()
            before = json.dumps(data, sort_keys=True)
            yield data
            if json.dumps(data, sort_keys=True) != before:
                self._write(data)
        finally:
            os.close(fd)

    def _write(self, data):
        fd, temporary = tempfile.mkstemp(prefix=".accounts.", suffix=".json", dir=self.data_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(json.dumps(data, indent=1) + "\n")
            os.replace(temporary, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise

    @staticmethod
    def _taken(accounts, handle, bundle, other=None):
        return any(a["handle"].casefold() == handle.casefold() and a["app"]["bundleId"] == bundle
                   and a["id"] != other for a in accounts)

    def add(self, body, device=None):
        """A new account from ``body`` (store.clean)."""
        fields = clean(body)
        now = round(time.time() * 1000)
        with self.edit() as data:
            if len(data["accounts"]) >= MAX_ACCOUNTS:
                raise ValueError(f"Mobster keeps up to {MAX_ACCOUNTS} profiles. Remove one you no longer run.")
            if self._taken(data["accounts"], fields["handle"], fields["app"]["bundleId"]):
                raise ValueError(f"@{fields['handle']} on {fields['app']['name']} is already in your profiles")
            account = {"id": uuid.uuid4().hex[:12], **fields, "device": device[0] if device else None,
                       "deviceName": device[1] if device else None, "createdAt": now, "updatedAt": now}
            data["accounts"].append(account)
            return dict(account)

    def update(self, account_id, body, device=None):
        """The account with ``body``'s fields changed."""
        fields = clean(body, partial=True)
        with self.edit() as data:
            account = next((a for a in data["accounts"] if a["id"] == account_id), None)
            if account is None:
                raise AccountNotFound(NOT_FOUND)
            merged = {**account, **fields}
            if self._taken(data["accounts"], merged["handle"], merged["app"]["bundleId"], other=account_id):
                raise ValueError(f"@{merged['handle']} on {merged['app']['name']} is already in your profiles")
            if "device" in fields:
                merged["device"], merged["deviceName"] = (device if device else (None, None))
            merged["updatedAt"] = round(time.time() * 1000)
            account.clear()
            account.update(merged)
            return dict(account)

    def remove(self, account_id):
        with self.edit() as data:
            before = len(data["accounts"])
            data["accounts"] = [a for a in data["accounts"] if a["id"] != account_id]
            if len(data["accounts"]) == before:
                raise AccountNotFound(NOT_FOUND)

    def forget_device(self, device_id):
        """Phones Mobster forgets leave their accounts on the default phone."""
        with self.edit() as data:
            for account in data["accounts"]:
                if account.get("device") == device_id:
                    account["device"], account["deviceName"] = None, None


ON = re.compile(r"^\s*@?(?P<handle>[A-Za-z0-9._-]{1,64})\s+on\s+(?P<app>.{1,80}?)\s*$", re.I)


def resolve(accounts, value, bundle=None):
    """The account ``value`` names: its id, "@acme", "acme", or "@acme on Instagram"."""
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 160:
        raise ValueError("account is a profile's id or its handle, such as @acme")
    text = value.strip()
    if ID.fullmatch(text):
        found = next((a for a in accounts if a["id"] == text), None)
        if found is not None:
            return found
    on = ON.match(text)
    app_name = None
    if on:
        text, app_name = on.group("handle"), on.group("app").casefold()
    handle = text.lstrip("@").casefold()
    matches = [a for a in accounts if a["handle"].casefold() == handle]
    if app_name is not None:
        matches = [a for a in matches if a["app"]["name"].casefold() == app_name]
    if len(matches) > 1 and bundle:
        matches = [a for a in matches if a["app"]["bundleId"] == bundle] or matches
    if not matches:
        raise AccountNotFound(NOT_FOUND)
    if len(matches) > 1:
        apps = " and ".join(sorted(a["app"]["name"] for a in matches))
        raise AmbiguousAccount(f"@{matches[0]['handle']} is on {apps}. Say which one, such as "
                               f"\"@{matches[0]['handle']} on {matches[0]['app']['name']}\".")
    return matches[0]


MENTION = re.compile(r"(?<![\w.@/])@([A-Za-z0-9._-]{1,64})")


def mentioned(accounts, text, bundle=None):
    """The one account a message names with an @handle ("Reply to my comments as @acme"), or None when it names
    none of them, or more than one (then the person picks it in the composer)."""
    found = {}
    for match in MENTION.finditer(text or ""):
        handle = match.group(1).rstrip(".").casefold()
        for account in accounts:
            if account["handle"].casefold() == handle and (bundle is None or account["app"]["bundleId"] == bundle):
                found[account["id"]] = account
    return next(iter(found.values())) if len(found) == 1 else None


_stores = {}
_stores_lock = threading.Lock()


def store_for(runtime):
    """The AccountStore beside ``runtime``'s journal (as its replays, loops and frames), else in its data folder
    (one store per folder)."""
    from ..paths import user_data_dir
    config = getattr(runtime, "config", None)
    state_db = getattr(config, "state_db", None)
    folder = Path(state_db).parent if state_db else Path(getattr(config, "data_dir", None) or user_data_dir())
    with _stores_lock:
        store = _stores.get(folder)
        if store is None:
            store = _stores[folder] = AccountStore(folder)
        return store
