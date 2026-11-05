"""Heals waiting for review (replay_core/heal.py writes and settles them), for `review_heals` and the Mac app's Tests
page, under /api/tests/heals (api_routes owner "testkit").

| Route | Auth | Answer |
|---|---|---|
| GET /api/tests/heals[?project=/abs/path] | token | {projects: [{project, root, problem, checks}]} |
| POST /api/tests/heals {project, action, checks?} | app | {action, settled: [{name, file, steps}]} |
| GET /api/tests/heals/frame?project=/abs/path&path=.mobster/runs/… | token | the frame, PNG or JPEG |

A check is {name, file, diff, app, ios, device, heals}, and a heal is {step, from, to, why, frames: {before, after},
run, at}. ``action`` is accept (the healed steps stay) or reject (the recorded steps come back and fail when their
screen changes again). ``checks`` names check files relative to the project, default every check with a heal waiting. A
frame is served only when a waiting heal names it. The routes answer 404 tests_page_off until
MOBSTER_EXPERIMENTAL_TESTS_PAGE=1.
"""

from pathlib import Path

from ..replay_core import heal as heals

FRAME_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
FRAME_MAX = 20 * 1024 * 1024
MAX_CHECKS = 50


def pending(root, paths=()):
    """[(Entry, heal document)] for each check under ``paths`` (default the project's checks) with a heal waiting.
    DiscoveryError for a path that isn't a check; HealError for a heal file that can't be read."""
    from .discover import discover
    found = []
    for entry in discover(list(paths), root):
        document = heals.read(entry.path)
        if document is not None:
            found.append((entry, document))
    return found


def settle(root, found, action):
    """Accept or reject each heal in ``found``; [(Entry, steps settled)]."""
    from ..replay_core.checks import cache_dir_for
    cache = cache_dir_for(Path(root) / ".mobster" / "runs")
    settle_one = heals.accept if action == "accept" else heals.reject
    return [(entry, settle_one(entry.path, cache)) for entry, _ in found]


def rows(found):
    """Each check's heals: the step, what it was, what it became, why, and the frames that show it."""
    return [{"check": str(entry.path), "file": entry.file, "name": entry.name, "diff": str(heals.heal_file(entry.path)),
             "heals": [{key: item.get(key) for key in ("step", "from", "to", "why", "frames", "run")}
                       for item in document["heals"] if isinstance(item, dict)]}
            for entry, document in found]


def _steps(document):
    return [{"step": item.get("step"), "from": item.get("from"), "to": item.get("to"), "why": item.get("why"),
             "frames": {name: (item.get("frames") or {}).get(name) for name in ("before", "after")},
             "run": item.get("run"), "at": item.get("at")}
            for item in document["heals"] if isinstance(item, dict)]


def _relative(path, root):
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return str(path)


def project_listing(root):
    """One project's waiting heals as GET /api/tests/heals answers them, and ``problem``, the sentence for a heal file
    that can't be read (then none are listed). A project with no checks has none."""
    from .discover import DiscoveryError
    problem = None
    try:
        found = pending(root)
    except DiscoveryError:
        found = []
    except heals.HealError as error:
        found, problem = [], f"{error}. Run the check again, or delete the file."
    checks = []
    for entry, document in found:
        app = document.get("app") if isinstance(document.get("app"), dict) else {}
        checks.append({"name": entry.name, "file": entry.file, "diff": _relative(heals.heal_file(entry.path), root),
                       "app": app.get("version"), "ios": document.get("ios"), "device": document.get("device"),
                       "heals": _steps(document)})
    return {"project": Path(root).name, "root": str(root), "checks": checks, "problem": problem}


def _frames(found):
    return {frame for _, document in found for item in _steps(document) for frame in item["frames"].values() if frame}


# -- routes ------------------------------------------------------------------------------------------------------------

def listed(request):
    from ..api_routes import Response
    from .register import page_off, project_roots
    off = page_off()
    if off is not None:
        return off
    roots, bad = project_roots(request)
    if bad is not None:
        return bad
    return Response.json({"projects": [project_listing(root) for root in roots]})


def settled(request):
    from ..api_routes import Response
    from .discover import DiscoveryError
    from .register import app_only, page_off, project_roots
    off = page_off() or app_only(request, "Open Mobster to accept or reject a heal.")
    if off is not None:
        return off
    body = request.body if isinstance(request.body, dict) else {}
    checks = body.get("checks", [])
    if set(body) - {"project", "action", "checks"} or not isinstance(body.get("project"), str) \
            or body.get("action") not in ("accept", "reject") or not isinstance(checks, list) \
            or len(checks) > MAX_CHECKS or not all(isinstance(item, str) and item and not Path(item).is_absolute()
                                                     for item in checks):
        return Response.error("Send project, action (accept or reject) and, if you like, checks: the check files "
                              "relative to the project.", 400, "bad_request")
    roots, bad = project_roots(request, body)
    if bad is not None:
        return bad
    root = roots[0]
    try:
        found = pending(root)
    except (DiscoveryError, heals.HealError):
        found = []
    if checks:
        by_file = {entry.file: (entry, document) for entry, document in found}
        missing = [item for item in checks if item not in by_file]
        if missing:
            return Response.error(f"{missing[0]} has no heal waiting for review.", 404, "heal_not_found")
        found = [by_file[item] for item in checks]
    try:
        done = settle(root, found, body["action"])
    except heals.HealError as error:
        return Response.error(f"{error}. Run the check again, or delete the file.", 409, "bad_heal")
    return Response.json({"action": body["action"],
                          "settled": [{"name": entry.name, "file": entry.file, "steps": steps}
                                      for entry, steps in done]})


def frame(request):
    from ..api_routes import Response
    from .discover import DiscoveryError
    from .register import page_off, project_roots
    off = page_off()
    if off is not None:
        return off
    wanted = (request.query.get("path") or [None])[0]
    roots, bad = project_roots(request)
    if bad is not None:
        return bad
    if (request.query.get("project") or [None])[0] is None or not isinstance(wanted, str):
        return Response.error("Send project and path: a frame a waiting heal names.", 400, "bad_request")
    root = roots[0]
    try:
        named = _frames(pending(root))
    except (DiscoveryError, heals.HealError):
        named = set()
    file = (root / wanted).resolve() if wanted in named else None
    runs = (root / ".mobster" / "runs").resolve()
    kind = FRAME_TYPES.get(file.suffix.lower()) if file is not None else None
    if file is None or kind is None or not file.is_relative_to(runs) or not file.is_file() \
            or file.stat().st_size > FRAME_MAX:
        return Response.error("No waiting heal has that frame.", 404, "frame_not_found")
    return Response.bytes(file.read_bytes(), kind)


def register_routes():
    from ..api_routes import Route, register
    # POST /api/tests/heals is the editor's route (editor.heals), which hands the app's bodies to ``settled``.
    for route in (Route("testkit", "GET", "/api/tests/heals", listed, body="none"),
                  Route("testkit", "GET", "/api/tests/heals/frame", frame, body="none")):
        register(route)
