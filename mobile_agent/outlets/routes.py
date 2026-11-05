"""The local API for outlets, under /api/outlets (api_routes, owner "outlets")."""

from ..api_errors import APIError
from ..api_routes import Response, Route, register
from ..service_support import needs_app
from . import hooks as outlet_hooks
from . import service as outlet_service

OFF = "Sending results isn't available right now. Restart Mobster, then try again."


def service_of(request):
    service = outlet_service.service_for(request.runtime)
    if service is None:
        raise APIError(OFF, 503, "unavailable")
    return service


def listed(request):
    service = service_of(request)
    return Response.json({"destinations": service.list(), "kinds": service.kinds()})


def add(request):
    service = service_of(request)
    needs_app(request)
    return Response.json({"destination": service.add(request.body)}, 201)


def update(request):
    service = service_of(request)
    needs_app(request)
    return Response.json({"destination": service.update(request.params["id"], request.body)})


def delete(request):
    service = service_of(request)
    needs_app(request)
    return Response.json({"id": service.delete(request.params["id"])})


def test(request):
    service = service_of(request)
    needs_app(request)
    result = service.test(request.params["id"])
    row = service.store.destination(request.params["id"])
    return Response.json({**result, "destination": service.public(row) if row else None})


def connect(request):
    service = service_of(request)
    needs_app(request)
    return Response.json(service.connect(request.body), 201)


def connect_state(request):
    service = service_of(request)
    needs_app(request)
    return Response.json({"connect": service.connects.get(request.params["id"])})


def connect_cancel(request):
    service = service_of(request)
    needs_app(request)
    return Response.json({"id": service.connects.cancel(request.params["id"])})


def history(request):
    raw = (request.query.get("limit") or ["50"])[0]
    try:
        limit = max(1, min(50, int(raw)))
    except ValueError:
        raise APIError("limit is a number from 1 to 50.", 400, "invalid_request") from None
    return Response.json({"deliveries": service_of(request).history(request.params["id"], limit)})


def deliveries(request):
    return Response.json({"deliveries": service_of(request).deliveries(request.params["id"])})


def resend(request):
    return Response.json({"delivery": service_of(request).resend(request.params["id"])}, 201)


def send_run(request):
    """POST /api/runs/{id}/send {destination}: the finished result to a saved destination, as its delivery."""
    needs_app(request)
    body = request.body if isinstance(request.body, dict) else {}
    if set(body) != {"destination"}:
        raise ValueError('Send {"destination": "<name or id>"}')
    return Response.json(request.runtime.send_run(request.params["id"], body["destination"]), 201)


def hooks_of(request):
    service = outlet_hooks.service_for(request.runtime)
    if service is None:
        raise APIError(OFF, 503, "unavailable")
    return service


def hooks_listed(request):
    workflow = (request.query.get("workflowId") or [None])[0]
    return Response.json({"hooks": hooks_of(request).list(workflow)})


def hook_add(request):
    service = hooks_of(request)
    needs_app(request)
    body = request.body if isinstance(request.body, dict) else {}
    return Response.json({"hook": service.create(body.get("workflowId"))}, 201)


def hook_revoke(request):
    service = hooks_of(request)
    needs_app(request)
    return Response.json({"id": service.revoke(request.params["id"])})


def register_routes():
    for route in (Route("outlets", "GET", "/api/outlets", listed, body="none"),
                  Route("outlets", "POST", "/api/outlets/connects", connect),
                  Route("outlets", "GET", "/api/outlets/connects/{id}", connect_state, body="none"),
                  Route("outlets", "DELETE", "/api/outlets/connects/{id}", connect_cancel, body="none"),
                  Route("outlets", "POST", "/api/outlets/destinations", add),
                  Route("outlets", "POST", "/api/outlets/destinations/{id}", update),
                  Route("outlets", "DELETE", "/api/outlets/destinations/{id}", delete, body="none"),
                  Route("outlets", "POST", "/api/outlets/destinations/{id}/test", test, body="none"),
                  Route("outlets", "GET", "/api/outlets/destinations/{id}/deliveries", history, body="none"),
                  Route("outlets", "GET", "/api/outlets/runs/{id}", deliveries, body="none"),
                  Route("outlets", "POST", "/api/outlets/deliveries/{id}/resend", resend, body="none"),
                  Route("outlets", "POST", "/api/runs/{id}/send", send_run),
                  Route("outlets", "GET", "/api/outlets/hooks", hooks_listed, body="none"),
                  Route("outlets", "POST", "/api/outlets/hooks", hook_add),
                  Route("outlets", "DELETE", "/api/outlets/hooks/(?P<id>[0-9a-f]{32})", hook_revoke, body="none")):
        register(route)
