"""Mobster's own OAuth apps, for the connectors that sign in with one click though their vendors let no app register
itself: Slack, Google Drive and GitHub (the presets with ``auth: "mobster_app"``)."""

import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from .. import plan_gate
from ..share import api_base
from . import oauth

TTL, FAIL_TTL, TIMEOUT = 600.0, 60.0, 5.0
REASONS = ("awaiting_approval", "unconfigured", "unreachable")
CLIENT_ID = re.compile(r"[A-Za-z0-9._-]{8,200}")
PICKER_KEY = re.compile(r"[A-Za-z0-9_-]{30,100}")   # a Google API key: the Picker's browser key (picker.py)


@dataclass(frozen=True)
class Unavailable:
    """Mobster's own app can't be used now."""
    reason: str


def why(name, reason):
    """What happened, for a sentence that then says what to do."""
    return {"awaiting_approval": f"Mobster's own {name} app is waiting for {name}'s approval",
            "unconfigured": f"Mobster's own {name} app isn't set up yet",
            "unreachable": "Mobster couldn't reach its sign-in service"}[reason]


KEY = re.compile(r"[a-z0-9-]{1,40}")
TOKEN_ROUTE = re.compile(r"/api/connectors/[a-z0-9-]{1,40}/token")


def account_file():
    """The Mac app's account.json, which holds the account's entitlement token (plan_gate.py)."""
    return plan_gate.app_account_file()


def account_headers(endpoint):
    """``{"Authorization": "Bearer <entitlement token>"}`` when ``endpoint`` is the license API's token route and this
    Mac's account has a token; else {}: the entitlement token never goes to a vendor."""
    base, parts = api_base(), urlsplit(endpoint or "")
    prefix = urlsplit(base).path.rstrip("/")
    path = parts.path[len(prefix):] if parts.path.startswith(prefix + "/") else ""
    if origin(endpoint or "") != origin(base) or parts.query or parts.fragment or not TOKEN_ROUTE.fullmatch(path):
        return {}
    stored = plan_gate.stored_account(account_file())
    return {"Authorization": f"Bearer {stored['entitlement']['token']}"} if stored else {}


def has_account():
    """Whether this Mac's Mobster account is signed in: Mobster's own apps trade a sign-in through it."""
    return bool(plan_gate.stored_account(account_file()))


def parse(answer, base):
    """{key: MobsterApp | Unavailable} from GET /api/connectors. Anything malformed is left out (unconfigured)."""
    providers = answer.get("providers") if isinstance(answer, dict) else None
    apps = {}
    for key, item in (providers.items() if isinstance(providers, dict) else ()):
        if not isinstance(item, dict) or not KEY.fullmatch(str(key)):
            continue
        if item.get("state") in ("awaiting_approval", "unconfigured"):
            apps[key] = Unavailable(item["state"])
        elif item.get("state") == "ready":
            try:
                apps[key] = _app(item, base)
            except (ValueError, oauth.OAuthError):
                continue
    return apps


def _app(item, base):
    client_id, scope, params, relay = item.get("clientId"), item.get("scope"), item.get("params"), item.get(
        "redirectUri")
    if not isinstance(client_id, str) or not CLIENT_ID.fullmatch(client_id) or not isinstance(scope, str) \
            or not isinstance(params, dict) or not all(isinstance(v, str) for v in params.values()):
        raise ValueError("malformed")
    if relay is not None and (not isinstance(relay, str) or origin(relay) != origin(base)):
        raise ValueError("a callback on another origin")
    picker = item.get("pickerKey")
    return oauth.MobsterApp(client_id, oauth.safe_url(item.get("authorizeUrl")), oauth.safe_url(item.get("tokenUrl")),
                            scope, tuple(params.items()), relay,
                            picker if isinstance(picker, str) and PICKER_KEY.fullmatch(picker) else None)


def origin(url):
    parts = urlsplit(url)
    return (parts.scheme, parts.netloc)


def tested(answer):
    """The gated presets' keys the answer names as tested; anything malformed is left out."""
    keys = answer.get("tested") if isinstance(answer, dict) else None
    return frozenset(key for key in keys if isinstance(key, str) and KEY.fullmatch(key)) if isinstance(keys, list) \
        else frozenset()


def ask(base):
    """The license API's answer, parsed: ({key: MobsterApp | Unavailable}, tested keys)."""
    answer = oauth.fetch_json(f"{base}/api/connectors", timeout=TIMEOUT)
    return parse(answer, base), tested(answer)


class Apps:
    """Which of Mobster's own apps are ready, and which gated presets are tested, cached."""

    def __init__(self):
        self._lock = threading.Lock()
        self._apps, self._missing, self._tested, self._at, self._ttl = ({}, Unavailable("unreachable"), frozenset(),
                                                                        None, 0.0)

    def _fresh(self):
        now = time.monotonic()
        if self._at is None or now - self._at > self._ttl:
            try:
                (self._apps, self._tested), self._missing, self._ttl = ask(api_base()), Unavailable("unconfigured"), TTL
            except oauth.OAuthError:
                self._apps, self._tested, self._missing, self._ttl = ({}, frozenset(), Unavailable("unreachable"),
                                                                      FAIL_TTL)
            self._at = now

    def get(self, key):
        """An oauth.MobsterApp, or Unavailable."""
        with self._lock:
            self._fresh()
            return self._apps.get(key, self._missing)

    def tested(self, key):
        """Whether the gated preset ``key`` passed Mobster's test; False when Mobster couldn't ask."""
        with self._lock:
            self._fresh()
            return key in self._tested
