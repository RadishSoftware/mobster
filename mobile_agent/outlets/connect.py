"""Connect a destination with one click, then pick where its results go (docs/webhooks.mdx)."""

import logging
import threading
import time
import uuid

from ..api_errors import APIError
from ..connectors import oauth, shared
from ..connectors import presets as preset_list
from . import net
from .kinds import again
from .webhook import Outcome

log = logging.getLogger("mobster.outlets")

KEEP_SECONDS = 900.0
MAX_CONNECTS = 8
SLACK_OFF = "Mobster's connectors aren't running, and a Slack destination posts with Slack's. Restart Mobster."
GONE = "That sign-in ran out. Connect again."


class Connect:
    def __init__(self, kind, at):
        self.id, self.kind, self.at = uuid.uuid4().hex[:12], kind, at
        self.state, self.message, self.found, self.flow = "waiting", None, None, None
        self.connector = None         # Slack: the connector whose sign-in this waits for

    def public(self):
        found = self.found or {}
        return {"id": self.id, "kind": self.kind.id, "state": self.state, "message": self.message,
                "account": found.get("account"), "targets": found.get("targets")}


class Connects:
    """The connects in flight and finished, for one OutletService."""

    def __init__(self, *, call, connectors, apps=None, sign_in=oauth.SignIn, clock=time.monotonic):
        self.call, self.connectors, self.clock = call, connectors, clock
        self.apps = apps if apps is not None else shared.Apps()
        self.sign_in_class = sign_in
        self._items = {}
        self._lock = threading.Lock()

    def offered(self, kind):
        """Whether Connect can add a destination of ``kind`` now, so the app lists it only then: Slack when a Slack
        connector is signed in already, or when Settings offers the Slack connector (presets.offered: a gated preset
        once Mobster tested it); every other kind always (Connect says why when its service's app isn't ready)."""
        if kind.id != "slack":
            return True
        connectors = self.connectors()
        if connectors is None:
            return False
        if any(row.get("preset") == "slack" and row.get("auth") == "oauth" for row in connectors.list()):
            return True
        return preset_list.offered(preset_list.preset("slack"), connectors.apps)

    def begin(self, kind):
        """Start connecting a destination of ``kind``: {"connect", "authorizeUrl"?}."""
        if kind.id == "slack":
            item, url = self._slack(kind)
            return {"connect": item.public(), **({"authorizeUrl": url} if url else {})}
        if kind.provider is None:
            raise ValueError(f"A {kind.label.lower()} destination doesn't connect: give its fields instead")
        app = self.apps.get(kind.provider)
        if isinstance(app, shared.Unavailable):
            raise APIError(f"{shared.why(kind.label, app.reason)}. Try again later.", 503, "connect_unavailable")
        item = self._add(Connect(kind, self.clock()))
        flow = self.sign_in_class(None, lambda ended, tokens, message: self._signed_in(item, tokens, message),
                                  name=kind.label, app=app, keep=kind.keep)
        try:
            url = flow.begin()
        except oauth.OAuthError as error:
            self._pop(item.id)
            raise APIError(error.message, 502, "sign_in_failed") from None
        item.flow = flow
        return {"connect": item.public(), "authorizeUrl": url}

    def _signed_in(self, item, tokens, message):
        """On the sign-in's thread when the browser came back: what the account offers, then done."""
        if tokens is None:
            item.state, item.message = "failed", message
            return
        try:
            found = item.kind.connected(self.call, tokens)
        except ValueError as error:
            item.state, item.message = "failed", str(error)
            return
        except net.NetError as error:
            item.state, item.message = "failed", f"{error.message} Connect again."
            return
        except Exception:  # noqa: BLE001 -- a connect that broke says so; the app stays up
            log.exception("outlets: a connect failed unexpectedly")
            item.state, item.message = "failed", f"Mobster couldn't finish connecting {item.kind.label}. Connect again."
            return
        item.found, item.at, item.state = found, self.clock(), "done"

    def _slack(self, kind):
        """(connect, authorizeUrl | None): done at once with a signed-in Slack connector; else the Slack connector,
        added when there is none, signs in now, and the connect waits for it (``get``)."""
        connectors = self.connectors()
        if connectors is None:
            raise APIError(SLACK_OFF, 503, "connectors_off")
        rows = [row for row in connectors.list() if row.get("preset") == "slack"]
        item = Connect(kind, self.clock())
        signed = next((row for row in rows if row.get("auth") == "oauth"), None)
        if signed is not None:
            self._slack_found(item, connectors, signed["id"], refuse=True)
            return self._add(item), None
        row = rows[0] if rows else None
        if row is None:
            app = connectors.apps.get("slack")
            if isinstance(app, shared.Unavailable):
                raise APIError(f"{shared.why('Slack', app.reason)}. Connect Slack with your own app under Use in "
                               "tasks, then add the destination.", 409, "slack_own_app")
            row = connectors.add({"preset": "slack"})
        try:
            url = connectors.begin_sign_in(row["id"])["authorizeUrl"]
        except ValueError as error:
            # No app to sign in with: Mobster's isn't ready and you have none of your own (connectors/service.py).
            raise APIError(str(error).rstrip(".") + ".", 409, "slack_own_app") from None
        item.connector = row["id"]
        return self._add(item), url

    def _slack_found(self, item, connectors, connector_id, refuse=False):
        """Done with the channels the Slack connector's sign-in sees."""
        from ..connectors import client as mcp
        try:
            token = connectors.bearer(connector_id)
            item.found = item.kind.connected(self.call, {"access_token": token, "connector": connector_id})
            item.state, item.at = "done", self.clock()
        except (mcp.AuthNeeded, APIError):
            message = "Slack needs you to sign in again under Use in tasks. Then add the destination."
            if refuse:
                raise APIError(message, 409, "slack_own_app") from None
            item.state, item.message = "failed", message
        except (mcp.NotSent, net.NetError, ValueError) as error:
            message = getattr(error, "message", None) or str(error)
            if refuse:
                raise APIError(message, 502, "slack_unreachable") from None
            item.state, item.message = "failed", message

    def _follow(self, item):
        """A Slack connect waiting for the connector's sign-in: done or failed once that is."""
        connectors = self.connectors()
        row = next((row for row in (connectors.list() if connectors is not None else ())
                    if row["id"] == item.connector), None)
        sign_in = (row or {}).get("signIn") or {}
        if row is None:
            item.state, item.message = "failed", "Slack was removed from Use in tasks. Connect again."
        elif sign_in.get("state") == "failed":
            item.state, item.message = "failed", sign_in.get("message") or "Slack didn't connect. Connect again."
        elif row.get("auth") == "oauth" and sign_in.get("state") != "waiting":
            self._slack_found(item, connectors, row["id"])

    def get(self, connect_id):
        item = self._items.get(connect_id) if isinstance(connect_id, str) else None
        if item is None:
            raise APIError(GONE, 404, "not_found")
        if item.connector is not None and item.state == "waiting":
            self._follow(item)
        return item.public()

    def found(self, connect_id, kind_id):
        """What a finished connect of ``kind_id`` found, for saving a destination with it. ValueError otherwise."""
        self._sweep()
        item = self._items.get(connect_id) if isinstance(connect_id, str) else None
        if item is None or item.kind.id != kind_id:
            raise ValueError(GONE)
        if item.state != "done":
            raise ValueError(item.message or f"Finish connecting {item.kind.label} in your browser first.")
        return item.found

    def cancel(self, connect_id):
        item = self._pop(connect_id)
        if item is None:
            raise APIError(GONE, 404, "not_found")
        return connect_id

    def drop(self, connect_id):
        """Forget a connect whose tokens are saved now."""
        self._pop(connect_id)

    def close(self):
        with self._lock:
            items, self._items = list(self._items.values()), {}
        for item in items:
            if item.flow is not None:
                item.flow.cancel()

    def _add(self, item):
        self._sweep()
        with self._lock:
            while len(self._items) >= MAX_CONNECTS:
                oldest = min(self._items.values(), key=lambda value: value.at)
                self._items.pop(oldest.id)
                if oldest.flow is not None:
                    oldest.flow.cancel()
            self._items[item.id] = item
        return item

    def _pop(self, connect_id):
        with self._lock:
            item = self._items.pop(connect_id, None) if isinstance(connect_id, str) else None
        if item is not None and item.flow is not None:
            item.flow.cancel()
        return item

    def _sweep(self):
        """Forget what finished KEEP_SECONDS ago, and a sign-in nobody came back to (its own wait ended it)."""
        now = self.clock()
        with self._lock:
            stale = [key for key, item in self._items.items()
                     if now - item.at > (KEEP_SECONDS if item.state != "waiting" else oauth.WAIT_SECONDS + 60)]
        for key in stale:
            self._pop(key)


class NoToken(Exception):
    """No access token now; ``outcome`` says why. Nothing was sent."""

    def __init__(self, outcome):
        super().__init__(outcome.message)
        self.outcome = outcome


def access(ctx, label, refresh=False):
    """The access token a connected destination sends, renewed through the license API first when it's expiring or
    ``refresh`` (after a 401), and saved (``ctx.keep``)."""
    saved = ctx.saved
    # A Notion destination saved before 15 Oct 2026 holds an internal integration's token, which never expires.
    tokens = saved.get("oauth") or ({"access_token": saved["token"]} if saved.get("token") else None)
    if not tokens or not tokens.get("access_token"):
        raise NoToken(Outcome(False, False, None, f"Mobster has no {label} sign-in for this. " + again(label),
                              unsent=True))
    if not (refresh or oauth.expiring(tokens, ctx.wall)):
        return tokens["access_token"]
    if not tokens.get("refresh_token"):
        raise NoToken(Outcome(False, False, None, f"{label} signed Mobster out. " + again(label), unsent=True))
    try:
        tokens = ctx.refresh(tokens)
    except oauth.OAuthError as error:
        if error.retry:
            raise NoToken(Outcome(False, True, None, f"Mobster couldn't renew its {label} sign-in. {error.message}",
                                  unsent=True)) from None
        raise NoToken(Outcome(False, False, None, f"{label} signed Mobster out. " + again(label), unsent=True)) \
            from None
    ctx.keep(tokens)
    return tokens["access_token"]
