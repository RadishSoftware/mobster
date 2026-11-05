"""``/api/project/env``: a project's secrets for the Mac app's Settings › Project › Secrets and any API client.

GET lists the names in the project's .mobster/env.example and whether each has a value (never the value). POST saves a
value and DELETE removes it. The project is the folder in ``root`` (an absolute path to a folder that exists). Nothing
here returns or logs a value; a refusal says what happened and what to do (project_env.EnvError).
"""

from pathlib import Path

from . import project_env
from .api_errors import APIError
from .api_routes import Response, Route, register

PATH = "/api/project/env"


def _project(root):
    if not isinstance(root, str) or not Path(root).is_absolute() or not Path(root).is_dir():
        raise APIError("root must be the absolute path of a project folder that exists.", 400, "bad_root")
    return project_env.ProjectEnv(root)


def _guard(call):
    try:
        return call()
    except project_env.EnvError as error:
        raise APIError(str(error), 400, "env_refused") from None


def _list(request):
    env = _project((request.query.get("root") or [""])[0])
    return Response.json({"project": str(env.root), "names": _guard(env.status)})


def _set(request):
    body = request.body if isinstance(request.body, dict) else {}
    env = _project(body.get("root"))
    _guard(lambda: env.set(body.get("name"), body.get("value")))
    return Response.json({"name": body["name"], "saved": True})


def _unset(request):
    env = _project((request.query.get("root") or [""])[0])
    name = (request.query.get("name") or [""])[0]
    return Response.json({"name": name, "removed": _guard(lambda: env.unset(name))})


def register_routes() -> None:
    """Register the three routes once per process."""
    from . import api_routes
    if any(r.pattern == PATH for r in api_routes._routes):
        return
    register(Route("projectenv", "GET", PATH, _list, body="none"))
    register(Route("projectenv", "POST", PATH, _set))
    register(Route("projectenv", "DELETE", PATH, _unset, body="none"))
