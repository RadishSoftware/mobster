"""The local API for notification triggers, under /api/triggers; no route returns a notification's text."""

import re

from .. import full_disk_access
from ..api_errors import APIError
from ..api_routes import Response, Route, register
from . import service as trigger_service

APP_ONLY = "Do this in the Mobster app."


def service_of(request):
    service = trigger_service.service_for(request.runtime)
    if service is None:
        raise APIError(trigger_service.GONE, 503, "unavailable")
    return service


def needs_app(request):
    local = getattr(request.runtime, "local_api", None) or {}
    if local.get("app_session") and not request.app_session:
        raise APIError(APP_ONLY, 403, "app_only")


def listed(request):
    service = service_of(request)
    return Response.json({"triggers": service.list(), "status": service.status()})


def add(request):
    service = service_of(request)
    needs_app(request)
    return Response.json({"trigger": service.add(request.body)}, 201)


def update(request):
    service = service_of(request)
    needs_app(request)
    return Response.json({"trigger": service.update(request.params["id"], request.body)})


def delete(request):
    service = service_of(request)
    needs_app(request)
    return Response.json({"id": service.delete(request.params["id"])})


def recent(request):
    return Response.json({"firings": service_of(request).recent(20)})


def pretty(bundle):
    """A name for a bundle id the catalog doesn't know: its last part, in words."""
    last = re.split(r"[.]", bundle)[-1]
    return " ".join(part.capitalize() for part in re.split(r"[-_]+", last) if part) or bundle


def apps(request):
    """iPhone apps Mobster has seen notify"""
    service = service_of(request)
    names = {}
    try:
        for app in request.runtime.apps():
            if app.get("bundleId"):
                names[app["bundleId"].lower()] = app.get("name")
    except Exception:  # noqa: BLE001 -- the list stands without names
        pass
    seen = []
    access = full_disk_access.status([full_disk_access.IPHONE_LIBRARY])
    if access["state"] == "granted":
        seen = iphone_apps()
    out, have = [], set()
    for bundle in seen:
        have.add(bundle.lower())
        out.append({"bundleId": bundle, "name": names.get(bundle.lower()) or pretty(bundle), "seen": True})
    for bundle, name in names.items():
        if bundle not in have and name:
            out.append({"bundleId": bundle, "name": name, "seen": False})
    out.sort(key=lambda app: (not app["seen"], app["name"].casefold()))
    return Response.json({"apps": out, "access": {"state": access["state"], "stores": service.stores()}})


def iphone_apps():
    """The bundle ids in the forwarding store's Library.plist, nothing else read."""
    from ..notify_triggers import sources
    try:
        mapping = sources.unarchive((sources.IPHONE_STORE / sources.LIBRARY).read_bytes())
    except Exception:  # noqa: BLE001 -- unreadable means no list, not an error
        return []
    return sorted(k for k in (mapping or {}) if isinstance(k, str) and k)


def register_routes():
    for route in (Route("triggers", "GET", "/api/triggers", listed, body="none"),
                  Route("triggers", "POST", "/api/triggers", add),
                  Route("triggers", "POST", "/api/triggers/{id}", update),
                  Route("triggers", "DELETE", "/api/triggers/{id}", delete, body="none"),
                  Route("triggers", "GET", "/api/triggers/apps", apps, body="none"),
                  Route("triggers", "GET", "/api/triggers/recent", recent, body="none")):
        register(route)
