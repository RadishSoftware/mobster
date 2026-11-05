"""The local API for connectors, under /api/connectors (api_routes, owner "connectors")."""

import threading
import time

from ..api_errors import APIError
from ..api_routes import Response, Route, register
from ..service_support import needs_app
from . import github_app, picker
from . import service as connector_service

OFF = "Connectors aren't available right now. Restart Mobster, then try again."


def service_of(request):
    service = connector_service.service_for(request.runtime)
    if service is None:
        raise APIError(OFF, 503, "unavailable")
    return service


def listed(request):
    service = service_of(request)
    return Response.json({"connectors": service.list(), "presets": service.presets(),
                          "notYet": service.presets_not_yet()})


def add(request):
    service = service_of(request)
    needs_app(request)
    return Response.json({"connector": service.add(request.body)}, 201)


def update(request):
    service = service_of(request)
    needs_app(request)
    return Response.json({"connector": service.update(request.params["id"], request.body)})


def delete(request):
    service = service_of(request)
    needs_app(request)
    return Response.json({"id": service.remove(request.params["id"])})


def test(request):
    service = service_of(request)
    needs_app(request)
    return Response.json(service.test(request.params["id"]))


def sign_in(request):
    service = service_of(request)
    needs_app(request)
    return Response.json(service.begin_sign_in(request.params["id"]))


def sign_out(request):
    service = service_of(request)
    needs_app(request)
    connector, revoked = service.sign_out(request.params["id"])
    return Response.json({"connector": connector, "revoked": revoked})


def reconnect(request):
    service = service_of(request)
    needs_app(request)
    return Response.json(service.reconnect(request.params["id"]))


def files(request):
    service = service_of(request)
    needs_app(request)
    return Response.json(picker.begin(service, request.params["id"]))


def imports_listed(request):
    service = service_of(request)
    needs_app(request)
    return Response.json({"servers": service.importable()})


def imports_add(request):
    service = service_of(request)
    needs_app(request)
    return Response.json({"connector": service.add_import(request.body)}, 201)


# Repositories the person allowed in the Mac app while this agent runs: a token for one of them needs no second card.
_checks_allowed = set()
_checks_lock = threading.Lock()
CHECKS_WAIT_S = 25.0  # `mobster test pr` waits 30 s for the answer
CHECKS_DECLINED = "Mobster Checks didn't get a token for {repo}: {why} Allow it in Mobster, then run the checks again."


def confirm_checks(request, repo, wait_s=None):
    """In the Mac app's agent, a caller without the app's session gets a repository's GitHub token only after the person
    allows it on a card in the Mac app (S6.6), once per repository, so another process with the API token can't mint
    one unseen: the token writes check runs and comments for an hour."""
    local = getattr(request.runtime, "local_api", None) or {}
    if not local.get("app_session") or request.app_session:
        return
    with _checks_lock:
        if repo in _checks_allowed:
            return
    from ..permissions import outside
    item = outside.REGISTRY.create({
        "key": f"github-checks:{repo}", "source": {"client": "mobster test pr"},
        "request": {"operation": "GITHUB_TOKEN", "label": f"Post checks and comments on {repo}", "role": "Link",
                    "title": "Let Mobster Checks post on GitHub?", "act": "post", "app_name": "GitHub",
                    "reason": "An hour's token that writes this repository's check runs and pull request comments"}})
    deadline = time.monotonic() + (CHECKS_WAIT_S if wait_s is None else wait_s)
    while item and item["state"] == "pending" and time.monotonic() < deadline:
        item = outside.REGISTRY.get(item["id"], wait=deadline - time.monotonic())
    state = (item or {}).get("state")
    if state == "approved":
        with _checks_lock:
            _checks_allowed.add(repo)
        return
    if state == "pending":
        outside.REGISTRY.cancel(item["id"])
    why = "nobody answered the card in time." if state in ("pending", "timeout", None) else "the card was declined."
    raise APIError(CHECKS_DECLINED.format(repo=repo, why=why), 403, "app_only")


def github_checks_token(request):
    service = service_of(request)
    repo = request.body.get("repo") if isinstance(request.body, dict) else None
    return Response.json(github_app.token(service, repo, github_app.account_file(request.runtime),
                                          confirm=lambda checked: confirm_checks(request, checked)))


def file_issue(request):
    """POST /api/runs/{id}/issue {tracker?, target?, again?}: the finished result as an issue in a connected tracker."""
    needs_app(request)
    return Response.json(request.runtime.file_run_issue(request.params["id"], request.body), 201)


def register_routes():
    for route in (Route("connectors", "GET", "/api/connectors", listed, body="none"),
                  Route("connectors", "POST", "/api/connectors", add),
                  Route("connectors", "POST", "/api/connectors/{id}", update),
                  Route("connectors", "DELETE", "/api/connectors/{id}", delete, body="none"),
                  Route("connectors", "POST", "/api/connectors/{id}/test", test, body="none"),
                  Route("connectors", "POST", "/api/connectors/{id}/sign-in", sign_in, body="none"),
                  Route("connectors", "POST", "/api/connectors/{id}/sign-out", sign_out, body="none"),
                  Route("connectors", "POST", "/api/connectors/{id}/reconnect", reconnect, body="none"),
                  Route("connectors", "POST", "/api/connectors/{id}/files", files, body="none"),
                  Route("connectors", "GET", "/api/connectors/imports", imports_listed, body="none"),
                  Route("connectors", "POST", "/api/connectors/imports", imports_add),
                  Route("connectors", "POST", "/api/connectors/github-checks/token", github_checks_token),
                  Route("connectors", "POST", "/api/runs/{id}/issue", file_issue)):
        register(route)
