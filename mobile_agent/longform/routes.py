"""The long-form routes (DESIGN §13.5), owner ``longform``."""

from ..api_errors import APIError
from ..api_routes import Response, Route
from . import plan as P

GOAL_CHARS = 4000


def _app(runtime, app_id):
    if app_id in (None, "", "any"):
        return None
    if not isinstance(app_id, str):
        raise ValueError("appId is an app's id")
    app = next((a for a in P.runtime_apps(runtime) if a.get("id") == app_id), None)
    if app is None:
        raise ValueError("Choose an app from your catalog")
    return app


def plan_route(request):
    body = request.body if isinstance(request.body, dict) else {}
    if set(body) - {"goal", "appId", "threadId", "device", "force"}:
        raise ValueError("A plan takes goal, and optionally appId, threadId, device and force")
    goal = body.get("goal")
    if not isinstance(goal, str) or not 1 <= len(goal.strip()) <= GOAL_CHARS:
        raise ValueError("Write the task first (1 to 4,000 characters)")
    force = body.get("force", False)
    if type(force) is not bool:
        raise ValueError("force is true or false")
    runtime = request.runtime
    _app(runtime, body.get("appId"))     # an appId the catalog doesn't have is a 400, as at start
    from . import enabled
    if not enabled():
        return Response.json({"longform": False})
    if force and P.bulk_change(goal):
        from ..runtime_limits import BULK_CHANGE_LONG
        return Response.json({"longform": False, "refused": {"code": "bulk_change", "message": BULK_CHANGE_LONG}})
    apps = P.runtime_apps(runtime)
    card = P.make_plan(goal, apps=apps, limit_usd=runtime.task_cap(), nav_model=P.nav_model(),
                       kind="feed" if force and not P.detect(goal, apps) else None)
    if card is None:
        return Response.json({"longform": False})
    P.store_for(runtime).put(card)
    return Response.json({"longform": True, "plan": P.public(card)})


def _run(runtime, run_id):
    with runtime.lock:
        run = runtime.runs.get(run_id)
    if run is None:
        raise APIError("Task not found", 404, "not_found")
    return run


def _state(runtime, run_id):
    """(notebook dict or None, plan, report, markdown) of a run, live or from the journal."""
    from .notebook import Notebook
    from .session import for_run
    from .store import load_notebook, load_report
    session = for_run(run_id)
    if session is not None:
        book = session.notebook
        report = session.report
        markdown = None
        if report is not None:
            from .report import markdown as render
            markdown = render(report, session.budget)
        return book, book.plan or session.plan, report, markdown
    data = load_notebook(getattr(runtime, "journal", None), run_id)
    report, markdown = load_report(getattr(runtime, "journal", None), run_id)
    if data is None:
        return None, None, report, markdown
    book = Notebook.from_dict(data)
    return book, data.get("plan"), report, markdown


def longform_route(request):
    run = _run(request.runtime, request.params["id"])
    book, plan, report, markdown = _state(request.runtime, run.id)
    if book is None:
        raise APIError("This task isn't a long task.", 404, "not_longform")
    items = (request.query.get("items") or ["1"])[0] not in ("0", "false")
    return Response.json({"plan": P.public(plan) if plan else None, "notebook": book.public(items=items),
                          "report": report, "markdown": markdown})


def report_route(request):
    run = _run(request.runtime, request.params["id"])
    book, _plan, report, markdown = _state(request.runtime, run.id)
    if book is None and report is None:
        raise APIError("This task isn't a long task.", 404, "not_longform")
    if report is None or not markdown:
        raise APIError("The report isn't written yet.", 404, "no_report")
    title = " ".join(str(report.get("title") or "Report").split())[:120]
    safe = "".join(ch for ch in title if ch not in '/\\:*?"<>|').strip() or "Report"
    return Response.bytes(markdown.encode(), "text/markdown; charset=utf-8", filename=f"Mobster – {safe}.md")


ROUTES = (Route("longform", "POST", "/api/longform/plan", plan_route),
          Route("longform", "GET", "/api/runs/{id}/longform", longform_route, body="none"),
          Route("longform", "GET", "/api/runs/{id}/longform/report.md", report_route, body="none"))
