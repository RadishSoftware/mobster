"""Track `learned`'s registrations (tracks.load calls ``register(api)`` once per process, staged).

- The run listener that learns a saved workflow's path, and the pre-run hook that replays it (workflows.py).
- Saved programs (app_path.py): the listener that keeps a done Smart run whole, and the pre-run hook that hands the
  live loop a program for a request with the same words and other values (``MOBSTER_PROGRAMS``: on by default for
  Mobster for Mac, ``mobster serve``, ``phone_task`` through ``mobster mcp`` and Mobster Remote, ``off`` turns it off;
  the store and matching are programs.py).
- ``GET /api/replay/workflows`` → ``{"learned": {workflowId: {...}}}``: which saved workflows run without the model,
  for the Mac app's Workflows rows.
- ``POST /api/replay/workflows/{id}/forget`` → ``{"forgotten": n}``: the next run of that workflow uses the model
  and learns again.
- ``GET /api/replay/programs`` → ``{"programs": [...]}``: the saved and taught programs (programs.public), newest
  first, for the Programs list under Workflows. A taught program saved as a workflow names it in ``workflowIds``: a
  workflow's "repeat the task you taught" step references that workflow.
- Run field ``practice`` (a taught program's id): Try it now. The run replays that program and asks about each step
  first (approval kind ``practice``); the person's corrections change the program after the run.
- ``POST /api/replay/programs/{program}/rename`` ``{"name"}`` and ``POST /api/replay/programs/{program}/forget``
  (``{program}``: the id ``GET`` gives, 16 hex, a dash, 8 hex). Forgetting a taught program also forgets what its
  workflow learned, so its next run uses the model.

The check front end (replay_core) needs no registration: `mobster test` and its MCP tool call it through
verify/runner.py.
"""

from ..replay_core.store import PROGRAM_ID

# After harness pick-up (0) and the packs and long-form hooks (1 to 5): a run being picked up goes first. A saved
# workflow's own learned path goes before a saved program.
PRE_RUN_ORDER = 20
PROGRAM_ORDER = 24


def register(api):
    """``api`` is mobile_agent.harness_api."""
    from ..api_routes import Route
    from ..api_routes import register as add_route
    from .workflows import LearnListener, pre_run
    api.register_run_listener(LearnListener())
    api.register_pre_run(pre_run, order=PRE_RUN_ORDER)
    from .app_path import ProgramListener
    from .app_path import pre_run as program_pre_run
    api.register_run_listener(ProgramListener())
    api.register_pre_run(program_pre_run, order=PROGRAM_ORDER)
    add_route(Route("learned", "GET", "/api/replay/workflows", learned_workflows, body="none"))
    add_route(Route("learned", "POST", "/api/replay/workflows/{id}/forget", forget_workflow, body="none"))
    api.register_run_field("practice", validate_practice, public=True)
    add_route(Route("learned", "GET", "/api/replay/programs", list_programs, body="none"))
    add_route(Route("learned", "POST", "/api/replay/programs/" + PROGRAM + "/rename", rename_program))
    add_route(Route("learned", "POST", "/api/replay/programs/" + PROGRAM + "/forget", forget_program, body="none"))


PROGRAM = f"(?P<program>{PROGRAM_ID.pattern})"


def validate_practice(value, runtime):
    """``practice`` on POST /api/runs: a taught program's id."""
    if not isinstance(value, str) or not PROGRAM_ID.fullmatch(value):
        raise ValueError("practice is a taught program's id")
    return value


def _context(runtime):
    from pathlib import Path

    from ..catalog import APPS
    state_db = getattr(getattr(runtime, "config", None), "state_db", None)
    data_dir = Path(state_db).parent if state_db else None
    bundles = {app["id"]: app.get("bundleId") for app in APPS}
    apps = getattr(runtime, "apps", None)
    if callable(apps):
        try:
            bundles.update({app["id"]: app.get("bundleId") for app in apps()})
        except Exception:
            pass
    return data_dir, bundles


def _workflows(runtime):
    try:
        return runtime.workflows.list() if getattr(runtime, "workflows", None) is not None else []
    except Exception:  # noqa: BLE001 -- no saved workflows to link: the programs still list
        return []


def learned_workflows(request):
    from ..api_routes import Response
    from .workflows import learned_for
    runtime = request.runtime
    data_dir, bundles = _context(runtime)
    workflows = _workflows(runtime)
    learned = learned_for(workflows, data_dir, bundles)
    # A workflow saved from a lesson runs its taught program before it learns anything of its own.
    for program in _programs(data_dir, workflows):
        for workflow_id in program["workflowIds"]:
            if workflow_id not in learned and program["taught"]:
                learned[workflow_id] = {"steps": program["steps"], "learnedAt": (program["createdAt"] or 0) / 1000,
                                        "relearnedAt": None, "replays": program["replays"],
                                        "needsModelToAnswer": False, "liveSteps": 0, "approvals": program["asks"],
                                        "last": None, "device": program["device"], "taught": True,
                                        "programId": program["id"]}
    return Response.json({"learned": learned})


def _programs(data_dir, workflows):
    from . import programs
    if data_dir is None:
        return []
    store = programs.Programs(programs.programs_dir(data_dir))
    out = []
    for path in store.all():
        listed = programs.public(path, workflows)
        stored = path.get("workflowId")
        if stored and stored not in listed["workflowIds"] and any(w.get("id") == stored for w in workflows):
            listed["workflowIds"].insert(0, stored)
        out.append(listed)
    return out


def list_programs(request):
    from ..api_routes import Response
    data_dir, _ = _context(request.runtime)
    return Response.json({"programs": _programs(data_dir, _workflows(request.runtime))})


def _program(request):
    from ..api_errors import APIError
    from . import programs
    data_dir, _ = _context(request.runtime)
    if data_dir is None:
        raise APIError("Mobster has no data folder with programs", 404, "program_not_found")
    store = programs.Programs(programs.programs_dir(data_dir))
    path = store.by_id(request.params["program"])
    if path is None:
        raise APIError("No saved program has that id. It may have been forgotten.", 404, "program_not_found")
    return store, path, data_dir


def rename_program(request):
    from ..api_routes import Response
    from . import programs
    body = request.body if isinstance(request.body, dict) else {}
    if set(body) != {"name"} or not isinstance(body["name"], str):
        raise ValueError("Rename takes a name")
    store, path, data_dir = _program(request)
    renamed = store.rename(request.params["program"], body["name"])
    return Response.json({"program": programs.public(renamed, _workflows(request.runtime))})


def forget_program(request):
    from ..api_routes import Response
    from . import programs
    from .workflows import forget_for
    store, path, data_dir = _program(request)
    workflows = _workflows(request.runtime)
    _, bundles = _context(request.runtime)
    linked = set(programs.public(path, workflows)["workflowIds"]) | (
        {path["workflowId"]} if path.get("workflowId") else set())
    forgotten = bool(store.forget(path))
    for workflow in workflows:
        if workflow.get("id") in linked:
            forget_for(workflow, data_dir, bundles)
    return Response.json({"forgotten": forgotten})


def forget_workflow(request):
    from ..api_routes import Response
    from .workflows import forget_for
    runtime = request.runtime
    data_dir, bundles = _context(runtime)
    workflow = runtime.workflows.get(request.params["id"])
    return Response.json({"forgotten": forget_for(workflow, data_dir, bundles) if data_dir else 0})
