
import atexit

from ..api_errors import APIError
from ..api_routes import Response, Route, register
from ..service_support import needs_app
from . import DevError, expo, expo_dev

_started = {}   # project root -> the Metro this process started for it


def _stop_all():
    for root, pid in list(_started.items()):
        expo_dev.stop(root, expected_pid=pid)
        _started.pop(root, None)


atexit.register(_stop_all)


def _app(project):
    try:
        return expo.resolve_project(project)
    except DevError as error:
        raise APIError(error.line(), 400, "bad_project") from None


def _project(request):
    body = request.body if isinstance(request.body, dict) else {}
    value = body.get("project") or (request.query.get("project") or [None])[0]
    if not isinstance(value, str) or not value:
        raise APIError("Send project: the absolute path of the Expo app's folder.", 400, "bad_project")
    return _app(value)


def _port(request):
    port = (request.body if isinstance(request.body, dict) else {}).get("port")
    if port is None:
        return None
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise APIError("port is a number from 1024 to 65535.", 400, "bad_port")
    return port


def status_route(request):
    app = _project(request)
    bundle = (request.query.get("bundle") or [""])[0] == "1"
    return Response.json({"app": app.public(), "server": expo_dev.status(app, check_bundle=bundle)})


def dev_route(request):
    needs_app(request)
    app = _project(request)
    try:
        server, started = expo_dev.ensure(app, _port(request))
    except DevError as error:
        starting = "still starting" in error.message
        raise APIError(error.line(), 409 if starting else 502, "starting" if starting else "metro_failed") from None
    if started:
        _started[str(app.root)] = server["pid"]
    return Response.json({"app": app.public(), "server": server, "started": started})


def reload_route(request):
    needs_app(request)
    app = _project(request)
    server = expo_dev.status(app)
    if server["state"] != "running":
        raise APIError("Metro isn't running for this project.", 409, "not_running")
    try:
        expo.reload(server["port"])
    except DevError as error:
        raise APIError(error.line(), 502, "metro_failed") from None
    return Response.json({"app": app.public(), "server": expo_dev.status(app, check_bundle=True)})


def stop_route(request):
    needs_app(request)
    app = _project(request)
    pid = expo_dev.stop(app.root)
    _started.pop(str(app.root), None)
    return Response.json({"stopped": pid})


def register_routes():
    for route in (Route("testkit", "GET", "/api/tests/expo", status_route, body="none"),
                  Route("testkit", "POST", "/api/tests/expo/dev", dev_route),
                  Route("testkit", "POST", "/api/tests/expo/reload", reload_route),
                  Route("testkit", "POST", "/api/tests/expo/stop", stop_route)):
        register(route)
