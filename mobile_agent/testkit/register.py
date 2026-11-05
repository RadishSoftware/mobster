PAGE_FLAG = "MOBSTER_EXPERIMENTAL_TESTS_PAGE"


def register(api):
    """``api`` is mobile_agent.harness_api."""
    from ..api_routes import Route
    from ..api_routes import register as add_route
    from ..mcp_server.registry import register_provider
    from .mcp import TestTools
    add_route(Route("testkit", "POST", "/api/tests/checks", copy_as_check, max_bytes=4096))
    add_route(Route("testkit", "POST", "/api/tests/notify", notify, max_bytes=16384))
    add_route(Route("testkit", "GET", "/api/tests/recent", recent_suites, body="none"))
    from ..devkit.expo_routes import register_routes as register_expo_routes
    register_expo_routes()
    from ..devkit.links_routes import register_routes as register_link_routes
    register_link_routes()
    from ..devkit.fixtures_routes import register_routes as register_fixture_routes
    register_fixture_routes()
    from .heal_review import register_routes as register_heal_routes
    register_heal_routes()
    add_route(Route("testkit", "GET", "/api/tests/snapshots", snapshots, body="none"))
    add_route(Route("testkit", "GET", "/api/tests/history", check_history, body="none"))
    add_route(Route("testkit", "GET", "/api/tests/schedules", list_schedules, body="none"))
    add_route(Route("testkit", "POST", "/api/tests/schedules/run-now", run_schedule_now, max_bytes=4096))
    add_route(Route("testkit", "POST", "/api/tests/schedules/device", set_schedule_device, max_bytes=4096))
    add_route(Route("testkit", "POST", "/api/tests/schedules/switch", switch_schedule, max_bytes=4096))
    register_provider(TestTools())
    api.register_service(_start_schedules, _close_schedules)
    from . import editor
    editor.register_routes(add_route, Route)
    api.register_service(editor.start_service, editor.close_service)
    from ..devkit import testflight_routes as flight
    add_route(Route("testkit", "GET", "/api/tests/testflight/builds", flight.builds, body="none"))
    add_route(Route("testkit", "POST", "/api/tests/testflight/install", flight.install, max_bytes=4096))
    add_route(Route("testkit", "GET", "/api/tests/testflight/key", flight.key, body="none"))
    add_route(Route("testkit", "POST", "/api/tests/testflight/key", flight.key, max_bytes=8192))
    add_route(Route("testkit", "DELETE", "/api/tests/testflight/key", flight.key, body="none"))


def recent_suites(request):
    from ..api_routes import Response
    from .recent import recent
    return Response.json({"suites": recent()})


def check_history(request):
    from ..api_routes import Response
    from .history import report
    from .recent import known_roots
    roots = known_roots()
    wanted = (request.query.get("project") or [None])[0]
    if wanted is not None:
        roots = [root for root in roots if str(root) == wanted]
        if not roots:
            return Response.error("Mobster hasn't run tests in that project on this Mac.", 404, "project_not_found")
    return Response.json({"projects": [{"project": root.name, "root": str(root), **report(root)} for root in roots]})


def runtime_guard(runtime):
    def guard(spec, device):
        gate = getattr(runtime, "plan_gate", None)
        if gate is not None and gate.refusal() is not None:
            return gate.refusal()
        from ..device_targets import named_device
        try:
            record = named_device(device)
        except Exception:  # noqa: BLE001 -- an unreadable device list: the suite's own check says more
            record = None
        if record is not None and runtime.device_busy(record.get("id")):
            return f"A task is running on {device}, so this scheduled run was skipped, not queued."
        return None
    return guard


def _start_schedules(runtime):
    from .health import TestSchedules
    runtime.test_schedules = TestSchedules(guard=runtime_guard(runtime))


def _close_schedules(runtime):
    found = getattr(runtime, "test_schedules", None)
    if found is not None:
        found.close()


def page_on():
    """A project's testing sections in the Mac app: heals, snapshots, the schedule switch, fixture apply and run options
    (shipped dark)."""
    import os
    return os.environ.get(PAGE_FLAG) == "1"


def page_off():
    """None while the testing sections are on, else the 404 their routes answer."""
    from ..api_routes import Response
    if page_on():
        return None
    return Response.error(
        f"This Mobster doesn't have a project's testing sections on. Start it with {PAGE_FLAG}=1.",
        404, "tests_page_off")


def app_only(request, message):
    """None, or a 403 when the Mac app configured a private session and this request doesn't carry its proof."""
    from ..api_routes import Response
    local = getattr(request.runtime, "local_api", None) or {}
    if local.get("app_session") and not getattr(request, "app_session", False):
        return Response.error(message, 403, "app_only")
    return None


def _schedule_app_only(request):
    return app_only(request, "Open Mobster to run or change a scheduled check.")


def project_roots(request, body=None):
    """(the project roots `mobster test` ran in on this Mac, or only the one ``project`` names (query, or body when
    given), None), or (None, the 404)."""
    from ..api_routes import Response
    from .recent import known_roots
    wanted = (body or {}).get("project") if body is not None else (request.query.get("project") or [None])[0]
    roots = [root for root in known_roots() if wanted is None or str(root) == wanted]
    if wanted is not None and not roots:
        return None, Response.error("Mobster hasn't run tests in that project on this Mac.", 404, "project_not_found")
    return roots, None


def list_schedules(request):
    import time

    from ..api_routes import Response
    from .health import listing
    roots, bad = project_roots(request)
    if bad is not None:
        return bad
    now = round(time.time() * 1000)
    return Response.json({"projects": [listing(root, now) for root in roots]})


def _named_schedule(request):
    from ..api_routes import Response
    from .health import load
    body = request.body if isinstance(request.body, dict) else {}
    if not isinstance(body.get("project"), str) or not isinstance(body.get("schedule"), str):
        return None, None, None, Response.error("Send project and schedule.", 400, "bad_request")
    roots, bad = project_roots(request, body)
    if bad is not None:
        return None, None, None, bad
    specs, _ = load(roots[0])
    spec = next((item for item in specs if item.name == body["schedule"]), None)
    if spec is None:
        return None, None, None, Response.error("That project has no schedule with that name.", 404,
                                                "schedule_not_found")
    return roots[0], spec, body, None


def run_schedule_now(request):
    from ..api_routes import Response
    denied = _schedule_app_only(request)
    if denied is not None:
        return denied
    root, spec, body, bad = _named_schedule(request)
    if bad is not None:
        return bad
    if set(body) - {"project", "schedule"}:
        return Response.error("Send project and schedule.", 400, "bad_request")
    answer = request.runtime.test_schedules.start(spec, trigger="run-now")
    if answer["outcome"] == "skipped":
        return Response.error(answer["reason"], 409, "schedule_busy")
    return Response.json({"started": True}, 202)


def set_schedule_device(request):
    from ..api_routes import Response
    from .health import ScheduleError, set_device
    denied = _schedule_app_only(request)
    if denied is not None:
        return denied
    root, spec, body, bad = _named_schedule(request)
    if bad is not None:
        return bad
    device = body.get("device")
    if set(body) - {"project", "schedule", "device"} or not (device is None or isinstance(device, str)):
        return Response.error("Send project, schedule and device (text, or null to take it off).", 400, "bad_request")
    try:
        set_device(root, spec.name, device)
    except ScheduleError as error:
        return Response.error(str(error), 400, "bad_request")
    return Response.json({"device": device})


def switch_schedule(request):
    """POST /api/tests/schedules/switch {project, schedule, on}: turn a schedule off or on, on this Mac only."""
    from ..api_routes import Response
    from .health import set_on
    denied = page_off() or _schedule_app_only(request)
    if denied is not None:
        return denied
    root, spec, body, bad = _named_schedule(request)
    if bad is not None:
        return bad
    if set(body) - {"project", "schedule", "on"} or not isinstance(body.get("on"), bool):
        return Response.error("Send project, schedule and on (true or false).", 400, "bad_request")
    set_on(root, spec.name, body["on"])
    return Response.json({"on": body["on"]})


def snapshots(request):
    """GET /api/tests/snapshots[?project=]: each snapshot that differs from its baseline or has none yet, as
    {check, file, name, device, appearance, textSize, locale, state, changed, baseline, current, diff}. Empty until
    `snapshot:` assertions write them."""
    from ..api_routes import Response
    off = page_off()
    if off is not None:
        return off
    roots, bad = project_roots(request)
    if bad is not None:
        return bad
    return Response.json({"projects": [{"project": root.name, "root": str(root), "snapshots": []} for root in roots]})


def copy_as_check(request):
    from ..api_routes import Response
    from ..verify.checks import LIMITS
    from .record import RUN_ID, RecordError, check_data, check_yaml
    body = request.body if isinstance(request.body, dict) else {}
    unknown = [key for key in body if key not in ("runId", "name")]
    if unknown:
        return Response.error(f"Unknown field {unknown[0]}: send runId and, if you like, name.", 400, "bad_request")
    run_id, name = body.get("runId"), body.get("name")
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        return Response.error("runId must be a task's 12-character id.", 400, "bad_request")
    if name is not None and (not isinstance(name, str) or len(name) > LIMITS["name"]):
        return Response.error(f"name must be text of at most {LIMITS['name']} characters.", 400, "bad_request")
    runtime = request.runtime
    with runtime.lock:
        run = runtime.runs.get(run_id)
    if run is None:
        return Response.error("That task isn't in Mobster's history.", 404, "run_not_found")
    record = {**run.public(), "app": dict(run.app or {})}  # public() leaves out the app's bundle ID
    try:
        data = check_data(record, name=name)
        text = check_yaml(record, name=name)
    except RecordError as error:
        return Response.error(error.message, error.status, error.code)
    return Response.json({"yaml": text, "name": data["name"], "app": data["app"]["bundle"],
                          "steps": len(data.get("steps") or ()), "expect": len(data["expect"])})


def notify(request):
    """One try to post a suite's message to a saved Slack destination. 4xx before any try; 200 after one."""
    from .. import secret_filter
    from ..api_errors import APIError
    from ..api_routes import Response
    from ..outlets import service as outlet_service
    body = request.body if isinstance(request.body, dict) else {}
    report = body.get("report")
    lines = report.get("lines") if isinstance(report, dict) else None
    link = report.get("link") if isinstance(report, dict) else None
    if set(body) - {"destination", "report"} or not isinstance(report, dict) \
            or set(report) - {"title", "lines", "link"} \
            or not isinstance(report.get("title"), str) or not 1 <= len(report["title"]) <= 300 \
            or not isinstance(lines, list) or not 1 <= len(lines) <= 20 \
            or not all(isinstance(line, str) and len(line) <= 1000 for line in lines) \
            or not (link is None or isinstance(link, str) and len(link) <= 600):
        return Response.error("Send destination and report: {title, lines, link?}.", 400, "bad_request")
    service = outlet_service.service_for(request.runtime)
    if service is None:
        return Response.error("Destinations aren't running. Restart Mobster.", 503, "unavailable")
    try:
        row = service.find(body.get("destination"))
    except ValueError as error:
        return Response.error(str(error), 404, "destination_not_found")
    if row["kind"] != "slack":
        return Response.error(f"{row['name']} isn't a Slack destination. Name one in .mobster/notifications.yaml.",
                              409, "not_slack")
    message = {"type": "tests.finished", "text": secret_filter.redact(report["title"]),
               "report": {"title": secret_filter.redact(report["title"]),
                          "lines": [secret_filter.redact(line) for line in lines], "link": link}}
    try:
        outcome = service.send_event(row["id"], message, event="tests.finished")
    except APIError as error:
        if error.status in (404, 409, 503):
            return Response.json({"ok": False, "unsent": True, "message": str(error)})
        return Response.json({"ok": False, "unsent": False, "message": str(error)})
    except Exception:  # noqa: BLE001 -- the try began; whether it arrived is unknown
        return Response.json({"ok": False, "unsent": False, "message": "Slack didn't answer."})
    return Response.json({"ok": bool(outcome.ok), "unsent": bool(outcome.unsent), "message": outcome.message or ""})
