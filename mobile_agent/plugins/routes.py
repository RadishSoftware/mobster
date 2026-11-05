"""The local API for plugins, under /api/plugins (api_routes, owner "plugins")."""

from ..api_errors import APIError
from ..api_routes import Response, Route, register
from ..service_support import needs_app
from . import install as sources
from . import manifest
from . import service as plugin_service

OFF = "Plugins aren't available right now. Restart Mobster, then try again."
NAME = r"(?P<name>[a-z0-9][a-z0-9-]{0,47})"


def service_of(request):
    service = plugin_service.service_for(request.runtime)
    if service is None:
        raise APIError(OFF, 503, "unavailable")
    return service


def refused(error):
    return APIError(str(error), 400, "plugin_invalid")


def listed(request):
    return Response.json({"plugins": service_of(request).list()})


def preview(request):
    service = service_of(request)
    needs_app(request)
    try:
        return Response.json(service.preview(request.body))
    except manifest.PluginError as error:
        raise refused(error) from None


def preview_zip(request):
    service = service_of(request)
    needs_app(request)
    try:
        return Response.json(service.preview(zip_data=request.body or b""))
    except manifest.PluginError as error:
        raise refused(error) from None


def install(request):
    service = service_of(request)
    needs_app(request)
    try:
        return Response.json({"plugin": service.install(request.body)}, 201)
    except manifest.PluginError as error:
        raise refused(error) from None


def update(request):
    service = service_of(request)
    needs_app(request)
    body = request.body
    if not isinstance(body, dict) or set(body) != {"enabled"}:
        raise ValueError("A plugin's enabled can change")
    try:
        return Response.json({"plugin": service.set_enabled(request.params["name"], body["enabled"])})
    except manifest.PluginError as error:
        raise refused(error) from None


def delete(request):
    service = service_of(request)
    needs_app(request)
    return Response.json({"name": service.uninstall(request.params["name"])})


def register_routes():
    for route in (Route("plugins", "GET", "/api/plugins", listed, body="none"),
                  Route("plugins", "POST", "/api/plugins/preview", preview),
                  Route("plugins", "POST", "/api/plugins/preview-zip", preview_zip, body="raw",
                        max_bytes=sources.MAX_ARCHIVE),
                  Route("plugins", "POST", "/api/plugins/install", install),
                  Route("plugins", "POST", f"/api/plugins/{NAME}", update),
                  Route("plugins", "DELETE", f"/api/plugins/{NAME}", delete, body="none")):
        register(route)
