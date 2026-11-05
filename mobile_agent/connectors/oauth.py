"""Sign in to a remote MCP server with OAuth (the MCP authorization spec): authorization code with PKCE, a loopback
redirect on this Mac, and a client the server registers (dynamic client registration) or, when it registers none,
Mobster's client ID metadata document (CLIENT_METADATA_URL, served from site/public)."""

import base64
import enum
import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import NamedTuple
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request

from .. import __version__, pinning
from . import callback_page

FIXED_PORT = 47613
FIXED_REDIRECT = f"http://127.0.0.1:{FIXED_PORT}/callback"
# Mobster's client ID for a server that registers no clients but reads a client's metadata from its URL
# (draft-ietf-oauth-client-id-metadata-document): site/public/oauth/client-metadata.json,
# whose redirect is FIXED_REDIRECT.
CLIENT_METADATA_URL = "https://mobster.dev/oauth/client-metadata.json"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")   # what a redirect URI may name; the listener is on 127.0.0.1 either way
WAIT_SECONDS = 300.0            # how long Mobster waits for the browser to come back
TIMEOUT = 15.0
MAX_BYTES = 1_000_000
def page(ok, name, message, *, denied=False):
    """The page the browser shows when the sign-in comes back to this Mac (``name``: the connector's, whose mark it
    shows)."""
    return callback_page.render("ok" if ok else "denied" if denied else "error", name, message)


@dataclass(frozen=True)
class OwnApp:
    """An OAuth app the person created at the vendor. Its redirect URL is FIXED_REDIRECT."""
    client_id: str
    client_secret: str | None = None


@dataclass(frozen=True)
class MobsterApp:
    """Mobster's own OAuth app at a vendor, as the license API's registry describes it (shared.py): no discovery."""
    client_id: str
    authorize_url: str
    token_endpoint: str
    scope: str
    params: tuple = ()
    redirect_uri: str | None = None
    picker_key: str | None = None


class Ended(enum.Enum):
    """How a sign-in ended, as ``on_done`` hears it."""
    SIGNED_IN = "signed_in"
    REFUSED = "refused"          # the service or its token endpoint said no
    UNANSWERED = "unanswered"    # the browser never came back within WAIT_SECONDS
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Registration:
    """A client a server registered for Mobster (RFC 7591), kept so a vendor whose admin approves each client
    (monday.com) gets the approved one again on the next sign-in."""
    endpoint: str
    client_id: str
    client_secret: str | None
    redirect_uri: str

    @classmethod
    def parse(cls, value):
        """One from the Keychain's JSON, or None when it isn't one."""
        if not isinstance(value, dict) or not all(isinstance(value.get(k), str) and value[k]
                                                  for k in ("endpoint", "client_id", "redirect_uri")):
            return None
        secret = value.get("client_secret")
        return cls(value["endpoint"], value["client_id"], secret if isinstance(secret, str) and secret else None,
                   value["redirect_uri"])


class OAuthError(Exception):
    """Sign-in failed. ``message`` is safe to show: never a code, a token or a secret."""

    def __init__(self, message, retry=False):
        super().__init__(message)
        self.message, self.retry = message, retry


def safe_url(url):
    # Plain http only for 127.0.0.1 and localhost (the tests' fake server); api.mobster.dev is pinned (pinning.py).
    parts = urlsplit(url or "")
    if parts.scheme == "https" and parts.hostname:
        return url
    if parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost"):
        return url
    raise OAuthError("The server's sign-in address isn't https, so Mobster won't use it.")


def fetch_json(url, data=None, headers=None, form=False, timeout=TIMEOUT):
    """GET (or POST ``data``) ``url``; the JSON object it answers, or None for a 404."""
    url = safe_url(url)
    body = None
    head = {"Accept": "application/json", "User-Agent": f"Mobster/{__version__}", **(headers or {})}
    if data is not None:
        if form:
            body = urlencode(data).encode()
            head["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            head["Content-Type"] = "application/json"
    try:
        with pinning.opener().open(Request(url, data=body, headers=head, method="POST" if body is not None else "GET"),
                                   timeout=timeout) as response:
            raw = response.read(MAX_BYTES + 1)
    except HTTPError as error:
        if error.code == 404 and data is None:
            return None
        detail, answer = "", None
        try:
            answer = json.loads(error.read(20_000))
            detail = str(answer.get("error_description") or answer.get("message") or answer.get("error") or "")[:200]
        except (ValueError, OSError, AttributeError):
            pass
        refused = isinstance(answer, dict) and answer.get("error") == "sign_in" and answer.get("message")
        if error.code == 401 and refused:
            # The license API's own sentence for a missing Mobster sign-in, whole and without the status code.
            raise OAuthError(str(answer["message"])[:400]) from None
        raise OAuthError(f"The sign-in server answered HTTP {error.code}" + (f": {detail}" if detail else "") + ".",
                         retry=error.code == 429 or error.code >= 500) from None
    except (URLError, OSError, ValueError):
        raise OAuthError("Mobster couldn't reach the sign-in server.", retry=True) from None
    if len(raw) > MAX_BYTES:
        raise OAuthError("The sign-in server's answer was too long.")
    try:
        value = json.loads(raw)
    except ValueError:
        if data is None:
            return None
        raise OAuthError("The sign-in server didn't answer with JSON.") from None
    return value if isinstance(value, dict) else None


def _well_known(base, name):
    """``/.well-known/<name>`` for ``base``, with its path after (RFC 8414), then at the root."""
    parts = urlsplit(base)
    root = f"{parts.scheme}://{parts.netloc}"
    path = parts.path.rstrip("/")
    urls = [f"{root}/.well-known/{name}{path}"] if path else []
    urls.append(f"{root}/.well-known/{name}")
    return urls


def _revocation(metadata):
    """The server's token revocation endpoint (RFC 7009), or None. Only one on the token endpoint's own origin: a token
    goes nowhere a refresh wouldn't already send it."""
    endpoint = metadata.get("revocation_endpoint")
    try:
        endpoint = safe_url(endpoint) if isinstance(endpoint, str) else None
    except OAuthError:
        return None
    token = urlsplit(metadata["token_endpoint"])
    return endpoint if endpoint and urlsplit(endpoint)[:2] == token[:2] else None


def discover(url, resource_metadata=None):
    """{"authorization_endpoint", "token_endpoint", "registration_endpoint"?, "client_metadata", "scopes"?, "resource"}
    for the MCP server at ``url``."""
    resource = None
    for candidate in ([resource_metadata] if resource_metadata else []) + _well_known(url, "oauth-protected-resource"):
        resource = fetch_json(candidate)
        if resource:
            break
    issuer = None
    if resource and isinstance(resource.get("authorization_servers"), list) and resource["authorization_servers"]:
        issuer = str(resource["authorization_servers"][0])
    if not issuer:
        parts = urlsplit(url)
        issuer = f"{parts.scheme}://{parts.netloc}"   # servers before protected-resource metadata
    metadata = None
    for candidate in _well_known(issuer, "oauth-authorization-server") + _well_known(issuer, "openid-configuration"):
        metadata = fetch_json(candidate)
        if metadata and metadata.get("authorization_endpoint") and metadata.get("token_endpoint"):
            break
        metadata = None
    if not metadata:
        raise OAuthError("This server doesn't say how to sign in. Use a token instead.")
    scopes = resource.get("scopes_supported") if resource else None
    return {"authorization_endpoint": safe_url(metadata["authorization_endpoint"]),
            "token_endpoint": safe_url(metadata["token_endpoint"]),
            "registration_endpoint": metadata.get("registration_endpoint"),
            "client_metadata": metadata.get("client_id_metadata_document_supported") is True,
            "revocation_endpoint": _revocation(metadata),
            "scopes": " ".join(str(s) for s in scopes) if isinstance(scopes, list) else None,
            "resource": (resource or {}).get("resource") or url}


def register_client(endpoint, redirect_uri):
    """Dynamic client registration (RFC 7591): a Registration."""
    answer = fetch_json(safe_url(endpoint), {
        "client_name": "Mobster", "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "token_endpoint_auth_method": "none", "client_uri": "https://mobster.dev"})
    registration = Registration.parse({**(answer or {}), "endpoint": endpoint, "redirect_uri": redirect_uri})
    if registration is None:
        raise OAuthError("The server didn't register Mobster as a client.")
    return registration


def _challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def _tokens(answer, client, endpoint, resource, now, keep=(), revocation=None):
    """The tokens to keep, and ``keep``'s fields of the answer under ``kept`` (Discord's ``webhook``)."""
    if not answer or not isinstance(answer.get("access_token"), str) or not answer["access_token"]:
        raise OAuthError("The sign-in server didn't give Mobster a token.")
    expires = answer.get("expires_in")
    tokens = {"access_token": answer["access_token"], "refresh_token": answer.get("refresh_token") or None,
              "expires_at": now + float(expires) if isinstance(expires, (int, float)) and expires > 0 else None,
              "client_id": client["client_id"], "client_secret": client.get("client_secret") or None,
              "token_endpoint": endpoint, "resource": resource}
    if keep:
        tokens["kept"] = {key: answer[key] for key in keep if key in answer}
    if revocation:
        tokens["revocation_endpoint"] = revocation
    return tokens


def _account_headers(endpoint):
    """The account's token for Mobster's own token route (shared.account_headers), {} for any other endpoint."""
    from .shared import account_headers  # shared imports this module
    return account_headers(endpoint)


def refresh(saved, clock=time.time):
    """New tokens from ``saved`` (vault's ``oauth``) with its refresh token. OAuthError without one."""
    if not saved or not saved.get("refresh_token"):
        raise OAuthError("Sign in again.")
    data = {"grant_type": "refresh_token", "refresh_token": saved["refresh_token"], "client_id": saved["client_id"]}
    if saved.get("client_secret"):
        data["client_secret"] = saved["client_secret"]
    if saved.get("resource"):
        data["resource"] = saved["resource"]
    answer = fetch_json(saved["token_endpoint"], data, headers=_account_headers(saved["token_endpoint"]), form=True)
    tokens = _tokens(answer, {"client_id": saved["client_id"], "client_secret": saved.get("client_secret")},
                     saved["token_endpoint"], saved.get("resource"), clock(),
                     revocation=saved.get("revocation_endpoint"))
    tokens["refresh_token"] = tokens["refresh_token"] or saved["refresh_token"]
    return tokens


REVOKE_TIMEOUT = 5.0


def revoke(saved, timeout=REVOKE_TIMEOUT):
    """Ask the server that issued ``saved`` (vault's ``oauth``) to revoke it (RFC 7009): its refresh token, then its
    access token. Never raises: Disconnect already happened on this Mac whatever the server says."""
    endpoint = (saved or {}).get("revocation_endpoint")
    if not endpoint or not saved.get("client_id"):
        return None
    sent = False
    for hint in ("refresh_token", "access_token"):
        token = saved.get(hint)
        if not token:
            continue
        data = {"token": token, "token_type_hint": hint, "client_id": saved["client_id"]}
        if saved.get("client_secret"):
            data["client_secret"] = saved["client_secret"]
        head = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
                "User-Agent": f"Mobster/{__version__}"}
        try:
            request = Request(safe_url(endpoint), data=urlencode(data).encode(), headers=head, method="POST")
            with pinning.opener().open(request, timeout=timeout) as response:
                response.read(MAX_BYTES)
        except (HTTPError, URLError, OSError, ValueError, OAuthError):
            return False
        sent = True
    return sent


def expiring(saved, clock=time.time, margin=60.0):
    expires = (saved or {}).get("expires_at")
    return isinstance(expires, (int, float)) and expires - margin <= clock()


class Plan(NamedTuple):
    """How one sign-in goes: its client ({"client_id", "client_secret"?}), the authorize and token endpoints, the
    redirect URI, the MCP resource (None for a MobsterApp) and the authorize URL's own parameters."""
    client: dict
    authorize: str
    token: str
    redirect: str
    resource: str | None
    query: dict
    revocation: str | None = None


class SignIn:
    """One sign-in in flight."""

    def __init__(self, url, on_done, *, name="the service", app=None, scopes=None, resource_metadata=None,
                 registration=None, redirect_host="127.0.0.1", wait=WAIT_SECONDS, clock=time.time, keep=(), then=None):
        if redirect_host not in LOOPBACK_HOSTS:
            raise ValueError(f"A sign-in comes back to {' or '.join(LOOPBACK_HOSTS)}, not {redirect_host}")
        self.url, self.on_done, self.name, self.app, self.keep = url, on_done, name, app, tuple(keep)
        self.then, self.onward = then, None   # onward: the page ``then`` answered, once signed in
        self.registration, self.redirect_host = registration, redirect_host
        self.on_fixed_port = False   # whether the listener got FIXED_PORT, which CLIENT_METADATA_URL's redirect needs
        self.scopes, self.resource_metadata, self.wait, self.clock = scopes, resource_metadata, wait, clock
        self.state = None             # "<port>.<nonce>", once the listener has its port
        self.verifier = secrets.token_urlsafe(48)
        self.server = None
        self._done = threading.Event()
        self._finished = threading.Lock()
        self._claimed = False         # the state was used once: a second callback never trades a code
        self._claim = threading.Lock()

    def _listen(self):
        handler = self._handler()
        for port in (FIXED_PORT, 0):
            try:
                server = ThreadingHTTPServer(("127.0.0.1", port), handler)
                self.on_fixed_port = port == FIXED_PORT
                return server
            except OSError:
                if isinstance(self.app, OwnApp) and port == FIXED_PORT:
                    raise OAuthError(f"Port {FIXED_PORT} on this Mac is in use, and your OAuth app's redirect URL "
                                     "needs it. Quit what uses it, then sign in again.") from None
        raise OAuthError("Mobster couldn't listen for the sign-in on this Mac.")

    def _plan(self, listener):
        """How this sign-in goes: its client, endpoints, redirect URI and the authorize URL's own parameters."""
        if isinstance(self.app, MobsterApp):
            app = self.app
            return Plan({"client_id": app.client_id}, app.authorize_url, app.token_endpoint,
                        app.redirect_uri or listener, None, {**dict(app.params), "scope": app.scope})
        meta = discover(self.url, self.resource_metadata)
        match self.app:
            case OwnApp(client_id=client_id, client_secret=secret):
                client = {"client_id": client_id, "client_secret": secret}
            case None if meta.get("registration_endpoint"):
                saved = self.registration
                if not (saved and saved.endpoint == meta["registration_endpoint"] and saved.redirect_uri == listener):
                    self.registration = register_client(meta["registration_endpoint"], listener)
                client = {"client_id": self.registration.client_id, "client_secret": self.registration.client_secret}
            case None if meta["client_metadata"]:
                if not self.on_fixed_port:
                    raise OAuthError(f"Port {FIXED_PORT} on this Mac is in use, and this sign-in needs it. Quit what "
                                     "uses it, then sign in again.")
                client = {"client_id": CLIENT_METADATA_URL, "client_secret": None}
            case _:
                raise OAuthError("This server needs an OAuth app of your own. Add its client ID and secret, then sign "
                                 "in again.")
        scope = self.scopes or meta.get("scopes")
        return Plan(client, meta["authorization_endpoint"], meta["token_endpoint"], listener, meta["resource"],
                    {"resource": meta["resource"], **({"scope": scope} if scope else {})}, meta["revocation_endpoint"])

    def begin(self):
        self.server = self._listen()
        port = self.server.server_address[1]
        try:
            self.plan = self._plan(f"http://{self.redirect_host}:{port}/callback")
        except BaseException:
            self.server.server_close()
            self.server = None
            raise
        # The license API's https callback finds this listener by the port at the start of state.
        self.state = f"{port}.{secrets.token_urlsafe(24)}"
        query = {"response_type": "code", "client_id": self.plan.client["client_id"],
                 "redirect_uri": self.plan.redirect,
                 "code_challenge": _challenge(self.verifier), "code_challenge_method": "S256", "state": self.state,
                 **self.plan.query}
        joiner = "&" if "?" in self.plan.authorize else "?"
        threading.Thread(target=self._serve, name="mobster-connector-signin", daemon=True).start()
        return self.plan.authorize + joiner + urlencode(query)

    def _serve(self):
        self.server.timeout = 0.5
        deadline = time.monotonic() + self.wait
        while not self._done.is_set() and time.monotonic() < deadline:
            self.server.handle_request()
        self.server.server_close()
        self._finish(Ended.UNANSWERED, None, "Nobody finished signing in, so Mobster stopped waiting. Sign in again.")

    def cancel(self):
        self._finish(Ended.CANCELLED, None, "Sign-in was cancelled.")

    def _finish(self, ended, tokens, message):
        with self._finished:
            if self._done.is_set():
                return  # reported once: the browser's answer, a cancel or the wait running out
            self._done.set()
        try:
            self.on_done(ended, tokens, message)
        except Exception:  # noqa: BLE001 -- a failing callback never reaches the browser's page
            pass

    def _complete(self, query):
        supplied = query.get("state")
        # Constant time, and once: the listener answers each callback on its own thread, so two at once must not both
        # trade a code. A wrong state claims nothing, so a page that guesses can't spend the real one.
        if self.state is None or not isinstance(supplied, str) or not secrets.compare_digest(
                supplied.encode(), self.state.encode()):
            return False, "This sign-in link is out of date. Start again in Mobster."
        if not query.get("error") and not query.get("code"):
            return False, "The service didn't send a sign-in code. Start again in Mobster."
        with self._claim:
            claimed, self._claimed = self._claimed, True
        if claimed:
            return False, "This sign-in already came back. Check Mobster for how it went."
        if query.get("error"):
            message = "You didn't allow Mobster to connect." if query["error"] == "access_denied" else \
                f"The service refused the sign-in ({str(query['error'])[:60]})."
            self._finish(Ended.REFUSED, None, message)
            return False, message
        code = query.get("code")
        plan = self.plan
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": plan.redirect,
                "client_id": plan.client["client_id"], "code_verifier": self.verifier,
                **({"resource": plan.resource} if plan.resource else {})}
        if plan.client.get("client_secret"):
            data["client_secret"] = plan.client["client_secret"]
        try:
            headers = _account_headers(plan.token) if isinstance(self.app, MobsterApp) else None
            answer = fetch_json(plan.token, data, headers=headers, form=True)
            tokens = _tokens(answer, plan.client, plan.token, plan.resource, self.clock(), self.keep, plan.revocation)
        except OAuthError as error:
            self._finish(Ended.REFUSED, None, error.message)
            return False, error.message
        self._finish(Ended.SIGNED_IN, tokens, None)
        if self.then is not None:
            try:
                onward = self.then(tokens)
            except Exception:  # noqa: BLE001 -- the sign-in worked: the done page shows whatever the next page did
                onward = None
            self.onward = onward if isinstance(onward, str) and onward.startswith("http://127.0.0.1:") else None
        return True, "You can close this tab."

    def _handler(self):
        sign_in = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                parts = urlsplit(self.path)
                if parts.path != "/callback":
                    self.send_response(404)
                    self.end_headers()
                    return
                query = {k: v[0] for k, v in parse_qs(parts.query).items() if v}
                ok, message = sign_in._complete(query)
                if ok and sign_in.onward:
                    self.send_response(303)
                    self.send_header("Location", sign_in.onward)
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = page(ok, sign_in.name, message, denied=query.get("error") == "access_denied").encode()
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Security-Policy", callback_page.csp())
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

        return Handler
