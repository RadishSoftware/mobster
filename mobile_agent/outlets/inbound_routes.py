"""The local API for commands from Slack and Discord, under /api/outlets/inbound (api_routes, owner "outlets").

| Route | Auth | Body | Answer |
|---|---|---|---|
| GET /api/outlets/inbound | token | | {on, channels: [{channel, name, senders, workflows}]} |
| POST /api/outlets/inbound/codes | app | {channel} | 201 {code, expiresAt}: the code to type in Slack or Discord |
| POST /api/outlets/inbound/senders/{channel}/{sender} | app | {public} | {sender} |
| DELETE /api/outlets/inbound/senders/{channel}/{sender} | app | | {sender} |
| POST /api/outlets/inbound/workflows | app | {channel, workflowId} | 201 {workflowId} |
| DELETE /api/outlets/inbound/workflows/{channel}/{id} | app | | {workflowId} |

``channel`` is slack or discord. A sender is {channel, sender, label, public, createdAt, lastAt, uses}; ``public`` is
whether the run's result is posted for the whole channel to read. 409 off while ``MOBSTER_EXPERIMENTAL_INBOUND``
isn't 1, and 409 remote_off while Mobster Remote isn't set up here.
"""

from ..api_errors import APIError
from ..api_routes import Response, Route, register
from . import inbound
from .routes import OFF, needs_app


def service_of(request):
    service = inbound.service_for(request.runtime)
    if service is None:
        raise APIError(OFF, 503, "unavailable")
    return service


def body_of(request):
    return request.body if isinstance(request.body, dict) else {}


def state(request):
    return Response.json(service_of(request).state())


def code(request):
    service = service_of(request)
    needs_app(request)
    return Response.json(service.new_code(body_of(request).get("channel")), 201)


def sender_update(request):
    service = service_of(request)
    needs_app(request)
    row = service.set_public(request.params["channel"], request.params["sender"], body_of(request).get("public"))
    return Response.json({"sender": row})


def sender_remove(request):
    service = service_of(request)
    needs_app(request)
    return Response.json({"sender": service.forget(request.params["channel"], request.params["sender"])})


def workflow_add(request):
    service = service_of(request)
    needs_app(request)
    body = body_of(request)
    return Response.json({"workflowId": service.enable(body.get("channel"), body.get("workflowId"))}, 201)


def workflow_remove(request):
    service = service_of(request)
    needs_app(request)
    return Response.json({"workflowId": service.disable(request.params["channel"], request.params["id"])})


def register_routes():
    where = r"(?P<channel>slack|discord)"
    for route in (Route("outlets", "GET", "/api/outlets/inbound", state, body="none"),
                  Route("outlets", "POST", "/api/outlets/inbound/codes", code),
                  Route("outlets", "POST", f"/api/outlets/inbound/senders/{where}/(?P<sender>[A-Za-z0-9:]{{3,60}})",
                        sender_update),
                  Route("outlets", "DELETE", f"/api/outlets/inbound/senders/{where}/(?P<sender>[A-Za-z0-9:]{{3,60}})",
                        sender_remove, body="none"),
                  Route("outlets", "POST", "/api/outlets/inbound/workflows", workflow_add),
                  Route("outlets", "DELETE", f"/api/outlets/inbound/workflows/{where}/{{id}}", workflow_remove,
                        body="none")):
        register(route)
