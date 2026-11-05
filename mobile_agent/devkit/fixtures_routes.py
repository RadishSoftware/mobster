"""A project's fixtures over the local API, under /api/tests/fixtures (api_routes owner "testkit"), for the Mac app's
project page.

| Route | Auth | Answer |
|---|---|---|
| GET /api/tests/fixtures?project=/abs/path | token | {fixtures: [fixture]} |
| POST /api/tests/fixtures/apply {project, names, bundle?, sim?} | app | {applied: a sentence} |

A fixture is {name, names, reset, args, env, steps, files, keychain, pasteboard, destructive}. Each fixture is flattened
with what it extends (``names`` lists them in order). ``destructive`` is true when it resets the app, seeds files,
resets the keychain or sets the pasteboard, which a run refuses on a real iPhone. ``pasteboard`` says whether one is
set, never its text. 400 bad_project without an absolute path to a folder; 400 bad_fixture with the sentence for a
fixture that can't be read.

Apply is `mobster fixtures apply` on a simulator: Mobster's own, or ``sim``. It never takes an iPhone. ``bundle``
defaults to the project's app. It answers when the simulator is ready, which can take a minute; one apply runs at a
time, and a simulator a check holds is never waited for (409 busy, 409 apply_failed with the sentence). It answers 404
tests_page_off until MOBSTER_EXPERIMENTAL_TESTS_PAGE=1. Its answer reads like "Applied … on iPhone 17 Pro: reset data, 2
files."
"""

import threading
from pathlib import Path

from ..api_errors import APIError
from ..api_routes import Response, Route, register


def listed(request):
    value = (request.query.get("project") or [None])[0]
    path = Path(value) if isinstance(value, str) else None
    if path is None or not path.is_absolute() or not path.is_dir():
        raise APIError("Send project: the absolute path of the project's folder.", 400, "bad_project")
    from ..verify import fixtures
    from ..verify.checks import project_root
    from .fixtures_cli import listing
    try:
        return Response.json({"fixtures": listing(project_root(path))})
    except fixtures.FixtureError as error:
        raise APIError(str(error), 400, "bad_fixture") from None


_applying = threading.Lock()


def applied(request):
    from ..testkit.register import app_only, page_off
    from . import DevError
    off = page_off() or app_only(request, "Open Mobster to apply a fixture.")
    if off is not None:
        return off
    body = request.body if isinstance(request.body, dict) else {}
    names, bundle, sim = body.get("names"), body.get("bundle"), body.get("sim")
    if set(body) - {"project", "names", "bundle", "sim"} or not isinstance(names, list) or not 1 <= len(names) <= 10 \
            or not all(isinstance(name, str) and 0 < len(name) <= 100 for name in names) \
            or not all(value is None or isinstance(value, str) and 0 < len(value) <= 155 for value in (bundle, sim)):
        raise APIError("Send project, names (the fixtures, in order) and, if you like, bundle and sim.", 400,
                       "bad_request")
    path = Path(body["project"]) if isinstance(body.get("project"), str) else None
    if path is None or not path.is_absolute() or not path.is_dir():
        raise APIError("Send project: the absolute path of the project's folder.", 400, "bad_project")
    from ..verify.checks import project_root
    from .fixtures_cli import apply
    from .links import bundle_id
    root = project_root(path)
    bundle = bundle or bundle_id(root)
    if not bundle:
        raise APIError("Mobster can't tell which app this project builds. Send bundle, the app's bundle ID.", 400,
                       "no_bundle")
    if not _applying.acquire(blocking=False):
        raise APIError("Another fixture is being applied. Try again when it's done.", 409, "busy")
    try:
        return Response.json({"applied": apply(root, names, bundle, sim=sim, wait=0)})
    except DevError as error:
        raise APIError(error.line(), 409, "apply_failed") from None
    finally:
        _applying.release()


def register_routes():
    register(Route("testkit", "GET", "/api/tests/fixtures", listed, body="none"))
    register(Route("testkit", "POST", "/api/tests/fixtures/apply", applied, max_bytes=8192))
