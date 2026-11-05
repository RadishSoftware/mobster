"""Track `permissions`' registrations; the gate never depends on them, so a failed one only takes the screens away."""

import os

from ..api_errors import APIError
from ..api_routes import Response, Route
from . import outside
from . import policy as P


def _mode(value, runtime):
    return P.validate_mode(value)


def _store(runtime):
    store = getattr(runtime, "permission_exceptions", None)
    try:
        return store() if callable(store) else None
    except Exception:
        return None


def state(runtime):
    """Settings › Safety's state: the saved mode, each known app's standing, the approved apps and the allow rules."""
    settings = P.from_env()
    store = _store(runtime)
    apps = []
    try:
        listed = runtime.apps() if runtime is not None else []
    except Exception:
        listed = []
    seen = set()
    for app in listed:
        bundle = app.get("bundleId")
        if not isinstance(bundle, str) or bundle in seen:
            continue
        seen.add(bundle)
        apps.append({"bundleId": bundle, "name": app.get("name") or bundle, "installed": app.get("installed"),
                     **settings.apps.status(bundle, store)})
    try:
        remembered = store.public() if store is not None else []
    except Exception:
        remembered = []
    approved = [{"bundleId": item["app"], "name": item.get("appName") or item["app"], "addedAt": item["addedAt"]}
                for item in remembered if item.get("op") == P.APP_OP]
    return {**settings.public(), "modes": [{"id": m, "label": P.MODE_LABELS[m]} for m in P.MODES],
            "apps": apps, "approvedApps": approved,
            "exceptions": [_rule(item) for item in remembered if item.get("op") != P.APP_OP]}


def _rule(item):
    """A "Don't ask again" entry as Settings › Safety lists it: an allow rule names its kind of action in words."""
    from .rules import RULE_OP, RULE_WORDS
    if item.get("op") == RULE_OP:
        words = RULE_WORDS.get(item.get("label") or "", item.get("label") or "")
        return {**item, "words": words[:1].upper() + words[1:]}
    return item


def get_permissions(request):
    return Response.json(state(request.runtime))


def post_permissions(request):
    # api_http.APP_ONLY_ROUTES: with the Mac app running, only its session reaches here.
    runtime = request.runtime
    body = request.body or {}
    if isinstance(body, dict) and set(body) == {"forget"}:
        # A "Don't ask again" exception the person takes back.
        if not isinstance(body["forget"], str) or not callable(getattr(runtime, "permission_exceptions", None)):
            raise ValueError("forget takes an exception's key")
        runtime.permission_exceptions().remove(body["forget"])
        return Response.json(state(runtime))
    if isinstance(body, dict) and len(body) == 1 and set(body) <= {"allow", "revoke"}:
        # An app approved ahead of its first use, or switched off (it asks again the next time a task needs it).
        (key, value), = body.items()
        bundle = P.validate_ids([value], key)[0]
        store = _store(runtime)
        if store is None:
            raise ValueError("Approving apps needs the Mobster service")
        if key == "allow":
            names = {a.get("bundleId"): a.get("name") for a in (runtime.apps() or []) if isinstance(a, dict)}
            store.add(bundle, P.APP_LABEL, P.APP_OP, names.get(bundle) or bundle)
        else:
            from .exceptions import key_of
            store.remove(key_of(bundle, P.APP_LABEL, P.APP_OP))
        return Response.json(state(runtime))
    changes = P.changes_for(body, P.from_env())
    env_file = (runtime.setup.env_file if getattr(runtime, "setup", None) else
                getattr(getattr(runtime, "config", None), "env_file", None))
    if env_file:
        from ..env_secrets import update_env_file
        update_env_file(env_file, changes)
    os.environ.update(changes)
    return Response.json(state(runtime))


def create_approval(request):
    item = outside.REGISTRY.create(request.body or {})
    return Response.json({"approval": outside.public(item)}, 201)


def list_approvals(request):
    since = (request.query.get("since") or [None])[0]
    wait = float((request.query.get("wait") or ["0"])[0] or 0)
    version, items = outside.REGISTRY.pending(int(since) if since and since.isdigit() else None, wait=wait)
    return Response.json({"version": version, "approvals": [outside.public(i) for i in items]})


def get_approval(request):
    wait = float((request.query.get("wait") or ["0"])[0] or 0)
    item = outside.REGISTRY.get(request.params["id"], wait=wait)
    if item is None:
        raise APIError("No such approval", 404, "approval_not_found")
    return Response.json({"approval": outside.public(item)})


def answer_approval(request):
    # api_http.APP_ONLY_PATTERNS: an outside agent can raise the card; only the person in the Mac app answers it.
    body = request.body or {}
    if set(body) - {"approve", "instruction"} or "approve" not in body:
        raise ValueError("An answer is approve: true or false (and optionally an instruction with false)")
    if not outside.REGISTRY.answer(request.params["id"], body["approve"], body.get("instruction")):
        raise APIError("This approval request is no longer waiting", 409, "approval_not_pending")
    return Response.json({"ok": True})


def cancel_approval(request):
    return Response.json({"ok": outside.REGISTRY.cancel(request.params["id"])})


ROUTES = (
    Route("permissions", "GET", r"/api/permissions", get_permissions, body="none"),
    Route("permissions", "POST", r"/api/permissions", post_permissions),
    Route("permissions", "GET", r"/api/approvals", list_approvals, body="none"),
    Route("permissions", "POST", r"/api/approvals", create_approval),
    Route("permissions", "GET", r"/api/approvals/{id}", get_approval, body="none"),
    Route("permissions", "POST", r"/api/approvals/{id}", answer_approval),
    Route("permissions", "DELETE", r"/api/approvals/{id}", cancel_approval, body="none"),
)


def register(api):
    """``api`` is mobile_agent.harness_api."""
    from .. import api_routes
    api.register_run_field("permissionMode", _mode, public=True)
    for route in ROUTES:
        api_routes.register(route)
