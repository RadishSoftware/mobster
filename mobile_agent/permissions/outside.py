"""Approvals for outside agents over MCP: the same gate applies, and only the Mac app's session can answer one."""

import hashlib
import json
import threading
import time
import uuid
from dataclasses import asdict

from . import consequence
from . import policy as P
from .gate import PermissionGate

EXPIRES_S = 600
WAIT_S = 38.0
KEEP_S = 120          # a decided approval stays readable this long, for the agent that asked
MAX_PENDING = 20
POLL_S = 20.0


class Refused(Exception):
    """The action may not go ahead now; the message says why and what the agent can do."""

    def __init__(self, message="", state=None, minutes=None):
        super().__init__(message)
        self.state, self.minutes = state, minutes


def _clean(value, limit):
    return " ".join(str(value or "").split())[:limit] or None


class Registry:
    def __init__(self, clock=time.time):
        self.clock = clock
        self._items = {}
        self._version = 0
        self._cond = threading.Condition()

    def _expire(self, now):
        for item in list(self._items.values()):
            if item["state"] == "pending" and now * 1000 >= item["expiresAt"]:
                item["state"], item["decidedAt"] = "timeout", now * 1000
                self._version += 1
            if item["state"] != "pending" and now * 1000 - (item.get("decidedAt") or 0) > KEEP_S * 1000:
                del self._items[item["id"]]

    def create(self, body):
        if not isinstance(body, dict) or set(body) - {"key", "request", "device", "source"}:
            raise ValueError("An approval takes key, request, device and source")
        request = body.get("request")
        if not isinstance(request, dict) or not isinstance(request.get("label"), str):
            raise ValueError("request needs at least the control's label")
        key = _clean(body.get("key"), 128)
        source = body.get("source") if isinstance(body.get("source"), dict) else {}
        fingerprint = hashlib.sha256(json.dumps((body.get("device"), source, request), sort_keys=True,
                                               ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        now = self.clock()
        with self._cond:
            self._expire(now)
            for item in self._items.values():
                if key and item["key"] == key and item["state"] == "pending":
                    if item["fingerprint"] != fingerprint:
                        raise ValueError("This approval key already names a different waiting action")
                    return dict(item)
            if sum(item["state"] == "pending" for item in self._items.values()) >= MAX_PENDING:
                raise ValueError("Too many approvals are waiting")
            item = {"id": uuid.uuid4().hex[:12], "key": key, "fingerprint": fingerprint, "state": "pending",
                    "kind": "commit", "outside": True,
                    "source": {"kind": "mcp", "client": _clean(source.get("client"), 60)},
                    "device": _clean(body.get("device"), 128),
                    "operation": _clean(request.get("operation"), 20) or "TAP",
                    "label": _clean(request.get("label"), 200) or "", "role": _clean(request.get("role"), 40) or "",
                    "text": str(request["text"])[:4000] if isinstance(request.get("text"), str) else None,
                    "title": _clean(request.get("title"), 160), "act": _clean(request.get("act"), 40),
                    "reason": _clean(request.get("reason"), 120), "app": _clean(request.get("app_name"), 80) or "",
                    "target": None, "requestedAt": now * 1000, "expiresAt": (now + EXPIRES_S) * 1000,
                    "instruction": None}
            if isinstance(request.get("apps"), list):
                item["apps"] = [name for name in (_clean(n, 80) for n in request["apps"][:6]) if name]
            self._items[item["id"]] = item
            self._version += 1
            self._cond.notify_all()
            return dict(item)

    def get(self, approval_id, wait=0.0):
        """The approval, once decided or after ``wait`` seconds; None when unknown."""
        deadline = time.monotonic() + max(0.0, min(wait, POLL_S))
        with self._cond:
            while True:
                self._expire(self.clock())
                item = self._items.get(approval_id)
                if item is None or item["state"] != "pending":
                    return dict(item) if item else None
                left = deadline - time.monotonic()
                if left <= 0:
                    return dict(item)
                self._cond.wait(min(left, 1.0))

    def pending(self, since=None, wait=0.0):
        """(version, the approvals waiting): returns at once, or when the list changes after ``since``."""
        deadline = time.monotonic() + max(0.0, min(wait, POLL_S))
        with self._cond:
            while True:
                self._expire(self.clock())
                if since is None or since != self._version or time.monotonic() >= deadline:
                    items = [dict(i) for i in self._items.values() if i["state"] == "pending"]
                    return self._version, sorted(items, key=lambda i: i["requestedAt"])
                self._cond.wait(min(deadline - time.monotonic(), 1.0))

    def answer(self, approval_id, approve, instruction=None):
        if type(approve) is not bool:
            raise ValueError("approve must be true or false")
        if instruction is not None:
            instruction = " ".join(str(instruction).split())
            if approve or not 1 <= len(instruction) <= 500:
                raise ValueError("A redirect declines the action and says in 1–500 characters what to do instead")
        with self._cond:
            self._expire(self.clock())
            item = self._items.get(approval_id)
            if item is None or item["state"] != "pending":
                return False
            item["state"] = "approved" if approve else "redirected" if instruction else "denied"
            item["instruction"], item["decidedAt"] = instruction, self.clock() * 1000
            self._version += 1
            self._cond.notify_all()
            return True

    def cancel(self, approval_id):
        with self._cond:
            item = self._items.get(approval_id)
            if item is None or item["state"] != "pending":
                return False
            item["state"], item["decidedAt"] = "cancelled", self.clock() * 1000
            self._version += 1
            self._cond.notify_all()
            return True


# In memory only: an approval can hold the exact text to be sent, which is never journaled.
REGISTRY = Registry()


def public(item):
    """An approval as the Mac app reads it (the exact text included: it never leaves this Mac's memory)."""
    return {k: v for k, v in item.items() if k not in {"key", "fingerprint"}}


def key_for(device, action):
    raw = json.dumps((device, asdict(action)), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def app_name(bundle, device=None, folder=None):
    """The name the person knows the app by ("Chrome")."""
    try:
        from ..catalog import app_name as catalog_name
        from ..installed_apps import InventoryCache
        from ..paths import user_data_dir
        inventory = InventoryCache(folder or user_data_dir() / "installed-apps").load(device) if device else None
        for app in (inventory.user if inventory is not None else ()):
            if app.get("bundleId") == bundle and app.get("name") and app["name"] != bundle:
                return app["name"]
        return catalog_name(bundle) or bundle
    except Exception:  # noqa: BLE001 -- a name is for reading: the bundle id stands in
        return bundle


def check_app(bundle, name=None, *, device, client_name=None, settings=None, grants=None, connect=None,
              wait_s=WAIT_S):
    """An outside agent opening or acting in ``bundle``: returns once approved, else Refused."""
    settings = settings or P.from_file()
    name = name or app_name(bundle, device)
    if not settings.apps.allows(bundle, outside=True):
        raise Refused(settings.apps.blocked_message(name, outside=settings.apps.allows(bundle)) +
                      " Nothing was done.")
    if grants is None:
        from .exceptions import Exceptions, default_path
        grants = Exceptions(default_path())
    if settings.apps.approved(bundle, grants):
        return
    request = {"operation": "OPEN_APP", "label": name, "role": "App", "text": None,
               "title": f"Allow Mobster to use {name}?", "act": "use app", "app_name": name, "apps": [name],
               "reason": P.app_card_reason()}
    answer = ask_app(request, key=hashlib.sha256(f"{device}␟app␟{bundle}".encode()).hexdigest()[:32],
                     device=device, client_name=client_name, connect=connect, wait_s=wait_s)
    if answer.get("state") == "approved":
        try:
            grants.add(bundle, P.APP_LABEL, P.APP_OP, name)
        except Exception:  # noqa: BLE001 -- approved now even if the store can't keep it
            pass
        return
    _refuse(answer, f"Allow Mobster to use {name}")


def _refuse(answer, what):
    """Refused with what the person's answer (not approved) means for the agent."""
    state = answer.get("state")
    if state == "redirected":
        raise Refused(f"The user declined this in the Mobster app and said: {answer.get('instruction')!r}. Nothing "
                      "was done.", state)
    if state == "denied":
        raise Refused("The user declined this in the Mobster app. Nothing was done.", state)
    if state == "timeout":
        raise Refused("Nobody answered the approval in the Mobster app in 10 minutes, so nothing was done.", state)
    if state == "pending":
        left = max(0, int((answer.get("expiresAt", 0) / 1000 - time.time()) // 60))
        raise Refused(f"This needs the user's OK in the Mobster app ({what}). They haven't answered yet, so nothing "
                      f"was done. Tell the user, then make the same call again to keep waiting (the request stays "
                      f"open about {left} more minutes).", state, left)
    raise Refused("The approval was withdrawn, so nothing was done.", "withdrawn")


def _project_policy():
    """The policy of the project this server runs in (its working folder), when MOBSTER_POLICY is on."""
    from ..paths import default_history_db
    from . import project_policy
    folder = project_policy.project_folder() if project_policy.enabled() else None
    return project_policy.load(folder, project_policy.counts_path(default_history_db())) if folder else None


def check_action(action, *, device, client_name=None, settings=None, connect=None, wait_s=WAIT_S, forced=False):
    """An outside agent's action: whether it asked (True), or Refused; never below ask_important."""
    settings = settings or P.from_file()
    if action.app:
        check_app(action.app, action.app_name, device=device, client_name=client_name, settings=settings,
                  connect=connect, wait_s=wait_s)
    from ..harness_api import commit_act
    gate = PermissionGate(P.effective_mode(settings.mode, None, "mcp"), apps=settings.apps, origin="mcp",
                          policy=_project_policy())
    op = (action.op or "").upper()
    typing = op in ("TYPE", "SET_TEXT")
    # A row's swipe is read by the gate's own signals (permissions.consequence): its words are the row's content.
    existing = None if typing or op.startswith("SWIPE_") else commit_act(action.op, action.label, None,
                                                                        action.app or None)
    if existing == "other" and op in consequence.RETURN_OPS and consequence.query_return(action) and commit_act(
            "TYPE", action.label, None, action.app or None) is None:
        # commit_act calls every Return a commit; only a field told for certain to be a search or address bar drops it.
        existing = None
    decision = gate.decide(action, existing_act=existing, forced=forced)
    if decision.decision == "allow":
        return False
    title = decision.title or approval_title(decision.act)
    text = action.text
    if text is None and decision.act in {"send_message", "reply", "comment", "post", "share"}:
        text = action.typed
    request = {"operation": action.op, "label": action.label, "role": action.role or "", "text": text,
               "title": title, "act": decision.act, "reason": decision.reason or None,
               "app_name": action.app_name or action.app}
    answer = ask_app(request, key=key_for(device, action), device=device, client_name=client_name, connect=connect,
                     wait_s=wait_s)
    if answer.get("state") == "approved":
        return True
    _refuse(answer, title)


def check_code(bundle, field_name, *, device, client_name=None, connect=None, wait_s=WAIT_S):
    """An outside agent entering a one-time code (use_code): it asks in the Mac app in every mode, never remembered."""
    request = {"operation": "USE_CODE", "label": str(field_name or "the code field")[:200], "role": "TextField",
               "text": None, "title": "Enter this code?", "act": "code", "reason": "Types a one-time code",
               "app_name": bundle or ""}
    answer = ask_app(request, key=hashlib.sha256(f"{device}␟code␟{bundle}␟{field_name}".encode()).hexdigest()[:32],
                     device=device, client_name=client_name, connect=connect, wait_s=wait_s)
    if answer.get("state") == "approved":
        return
    _refuse(answer, "Enter this code?")


def check_link(url, title, app, app_name, *, device, client_name=None, connect=None, wait_s=WAIT_S):
    """An outside agent opening a link that asks (deeplinks.decide): the Mac app's card every time, never remembered."""
    request = {"operation": "OPEN_URL", "label": f"Open {str(url)[:180]}", "role": "Link", "text": str(url)[:500],
               "title": title or "Open this link?", "act": "open link", "reason": None,
               "app_name": app_name or app or ""}
    answer = ask_app(request, key=hashlib.sha256(f"{device}␟link␟{url}".encode()).hexdigest()[:32], device=device,
                     client_name=client_name, connect=connect, wait_s=wait_s)
    if answer.get("state") == "approved":
        return
    _refuse(answer, title or "Open this link?")


_TITLES = {"send_message": "Send this message?", "reply": "Send this reply?", "comment": "Post this comment?",
           "post": "Post this?", "react": "Send this reaction?", "pay": "Make this payment?",
           "request_money": "Send this request?", "transfer": "Make this transfer?", "order": "Place this order?",
           "book": "Make this booking?", "follow": "Follow this account?", "unfollow": "Unfollow this account?",
           "share": "Share this?", "call": "Place this call?", "delete": "Delete this?", "cancel": "Cancel this?",
           "install": "Get this app?"}


def approval_title(act):
    """The card's question for an act of today's commit test ("Send this message?")."""
    return _TITLES.get(act or "", "Do this now?")


def ask_app(request, *, key, device=None, client_name=None, connect=None, wait_s=WAIT_S):
    """Raise the approval in the Mac app and wait up to ``wait_s`` for its answer: the approval as the app has it."""
    from ..threads.client import Client, NoService, ServiceError
    try:
        client = (connect or (lambda: Client.connect(origin="mcp")))()
        made = client.call("POST", "/api/approvals", {"key": key, "request": request, "device": device,
                                                      "source": {"client": client_name}})
    except (NoService, ServiceError, OSError):
        raise Refused("This needs the user's OK, and the Mobster app isn't open to ask them. Ask the user to open "
                      "Mobster, then try again. Nothing was done.") from None
    approval = made.get("approval") or made
    deadline = time.monotonic() + wait_s
    while approval.get("state") == "pending":
        left = deadline - time.monotonic()
        if left <= 0:
            break
        try:
            got = client.call("GET", f"/api/approvals/{approval['id']}?wait={min(POLL_S, max(1, int(left)))}",
                              timeout=min(POLL_S, left) + 5)
        except (ServiceError, OSError):
            break
        approval = got.get("approval") or got
    return approval
