"""A project's deep links over the local API, under /api/tests/links (api_routes owner "testkit"), for the Mac app's
project page. Logic is links.py's; these routes only name the project and shape the answer.

| Route | Auth | Body | Answer |
|---|---|---|---|
| GET /api/tests/links?project=/abs/path | token | | {links: [{name, url, source, title, screen, last}]} |
| POST /api/tests/links | app | {project, name, url, title?} | 201 {link}: saved in .mobster/links.yaml |
| DELETE /api/tests/links?project=/abs/path&name=… | app | | {removed}: the name taken out of .mobster/links.yaml |

``source`` is links.yaml, expo, xcode or learned. ``screen`` is false for a scheme or a domain alone, which opens the
app and no screen in it. ``last`` is the newest check of the link (`mobster links check`, or Check links on the
project's page): {status, device, at}, or null when it was never checked. 400 bad_project without an absolute path to a
folder; 400 bad_link for a name or address that can't be saved. Checking links opens each on a simulator: `mobster links
check`, or POST /api/projects/{id}/runs {links: true} for a project Mobster knows (projects/runs.py). Saving rewrites
.mobster/links.yaml as the `links:` list, so a comment in the file is lost.
"""

from pathlib import Path

from ..api_errors import APIError
from ..api_routes import Response, Route, register
from . import links


def _root(request, body):
    value = body.get("project") or (request.query.get("project") or [None])[0]
    path = Path(value) if isinstance(value, str) else None
    if path is None or not path.is_absolute() or not path.is_dir():
        raise APIError("Send project: the absolute path of the project's folder.", 400, "bad_project")
    from ..verify.checks import project_root
    return project_root(path)


def _needs_app(request):
    local = getattr(request.runtime, "local_api", None) or {}
    if local.get("app_session") and not request.app_session:
        raise APIError("Do this in the Mobster app.", 403, "app_only")


def _guard(call):
    try:
        return call()
    except links.LinksError as error:
        raise APIError(str(error), 400, "bad_link") from None


def _last_checks(root):
    """{link name: {status, device, at}}: the newest result of each link's check, from the project's history."""
    from ..testkit.history import report
    prefix = f".mobster/{links.FILE}#"
    newest = {}
    for row in report(root)["checks"]:
        file = row.get("file") or ""
        if not file.startswith(prefix) or row.get("lastStatus") is None:
            continue
        name, at = file[len(prefix):], row.get("lastAt") or 0
        if name not in newest or at > (newest[name]["at"] or 0):
            newest[name] = {"status": row["lastStatus"], "device": row.get("device") or None, "at": row.get("lastAt")}
    return newest


def listed(request):
    root = _root(request, {})
    found = _guard(lambda: links.discover(root, deep=False))
    last = _last_checks(root)
    return Response.json({"links": [{**link.public(), "last": last.get(link.name)} for link in found]})


def saved(request):
    _needs_app(request)
    body = request.body if isinstance(request.body, dict) else {}
    root = _root(request, body)
    link = _guard(lambda: links.save(root, body.get("name"), body.get("url"), body.get("title") or ""))
    return Response.json({"link": link.public()}, 201)


def removed(request):
    _needs_app(request)
    root = _root(request, {})
    name = (request.query.get("name") or [None])[0]
    return Response.json({"removed": _guard(lambda: links.remove(root, name))})


def register_routes():
    for route in (Route("testkit", "GET", "/api/tests/links", listed, body="none"),
                  Route("testkit", "POST", "/api/tests/links", saved),
                  Route("testkit", "DELETE", "/api/tests/links", removed, body="none")):
        register(route)
