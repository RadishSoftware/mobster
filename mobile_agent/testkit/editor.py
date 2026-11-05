"""The editor extension's routes on `mobster serve`: a project's checks, a suite started from the editor, and heals.

A suite started here runs on simulators only, never on a real iPhone: that stays `mobster test --device` and
run_tests with a device the user allowed. One suite runs at a time per service.
"""

from pathlib import Path

from .jobs import PATTERN_SUITE, Busy, PrJob, SuiteJob, Suites

SUITE = r"(?P<suite>" + PATTERN_SUITE.strip("^$") + ")"
MAX_COVERING_BYTES = 500_000
NO_CHECKS = ("This project has no saved checks yet. Ask your agent to verify a screen with Mobster and save it "
             "with save_check, or run `mobster verify … --save NAME`.")


def register_routes(add_route, Route):
    add_route(Route("testkit", "GET", "/api/tests/project", project, body="none"))
    add_route(Route("testkit", "POST", "/api/tests/suites", start, max_bytes=16384))
    add_route(Route("testkit", "GET", f"/api/tests/suites/{SUITE}", suite, body="none"))
    add_route(Route("testkit", "POST", f"/api/tests/suites/{SUITE}/stop", stop, max_bytes=1024))
    add_route(Route("testkit", "POST", "/api/tests/heals", heals, max_bytes=16384))


def start_service(runtime):
    runtime.editor_suites = Suites()


def close_service(runtime):
    found = getattr(runtime, "editor_suites", None)
    if found is not None:
        found.close()


# ------------------------------------------------------------------------------------------------- the routes

def project(request):
    """The project's checks with each one's last verdict, the heals waiting, the suite going now, and with ``file``
    (a source file) the checks that cover its screen."""
    from ..api_routes import Response
    from .discover import DiscoveryError, discover
    root, bad = _root((request.query.get("root") or [None])[0])
    if bad is not None:
        return bad
    out = {"root": str(root), "checks": [], "heals": [], "problem": None, "covering": None,
           "suite": _latest(request.runtime, root)}
    if not (root / ".mobster" / "checks").is_dir():
        return Response.json({**out, "problem": NO_CHECKS})
    try:
        entries = discover([], root)
    except DiscoveryError as error:
        return Response.json({**out, "problem": str(error)})
    last = _last_verdicts(root)
    out["checks"] = [_entry_row(entry, last.get(entry.file)) for entry in entries]
    out["heals"], out["problem"] = _waiting(root)
    wanted = (request.query.get("file") or [None])[0]
    if wanted is not None:
        source, bad = _inside(root, wanted, "file")
        if bad is not None:
            return bad
        out["covering"] = covering(root, source, entries)
    return Response.json(out)


def start(request):
    """Start a suite: the checks named (absolute paths in the project), every check, or with ``changed`` the checks
    that cover what changed since the last commit, after building the app."""
    from ..api_routes import Response
    from .api import Options
    from .prrun import PrOptions
    body = request.body if isinstance(request.body, dict) else {}
    if set(body) - {"root", "checks", "changed", "sim", "heal"}:
        return _bad("Send root and, if you like, checks, changed, sim and heal.")
    root, bad = _root(body.get("root"))
    if bad is not None:
        return bad
    checks, changed, sim, heal = body.get("checks"), body.get("changed", False), body.get("sim"), body.get("heal", True)
    if type(changed) is not bool or type(heal) is not bool:
        return _bad("changed and heal are true or false.")
    if sim is not None and not (isinstance(sim, str) and 0 < len(sim) <= 128):
        return _bad("sim is a kind of simulator, such as iPhone 17 Pro.")
    if checks is not None:
        if changed:
            return _bad("Send checks or changed, not both: changed picks the checks itself.")
        if not isinstance(checks, list) or not 0 < len(checks) <= 50:
            return _bad("checks is a list of 1 to 50 check files.")
        paths = []
        for item in checks:
            path, bad = _inside(root, item, "checks")
            if bad is not None:
                return bad
            paths.append(str(path))
    gate = getattr(request.runtime, "plan_gate", None)
    refusal = gate.refusal() if gate is not None else None
    if refusal is not None:
        return Response.error(refusal, 403, "plan_required")
    sims = (sim,) if sim else ()
    if changed:
        def make():
            return PrJob(PrOptions(base="HEAD", drafts="off", github="off", gifs=False),
                         Options(sims=sims, no_heal=not heal, command="editor: test on save"), root)
    else:
        def make():
            return SuiteJob(Options(paths=tuple(paths) if checks else (str(root / ".mobster" / "checks"),),
                                    sims=sims, no_heal=not heal, command="editor"), root=root)
    try:
        job = request.runtime.editor_suites.start(make)
    except Busy as busy:
        return Response.error(f"A suite is still running ({busy.job.public()['message']}). Wait for it, or stop it.",
                              409, "suite_running", suite=busy.job.public())
    return Response.json({"suite": job.public()}, 202)


def suite(request):
    from ..api_routes import Response
    job = request.runtime.editor_suites.get(request.params["suite"])
    if job is None:
        return Response.error("Mobster has no suite with that id. It keeps the last ten.", 404, "suite_not_found")
    return Response.json({"suite": job.public()})


def stop(request):
    """Stop the suite: the check going now ends, and the rest don't start."""
    from ..api_routes import Response
    job = request.runtime.editor_suites.get(request.params["suite"])
    if job is None:
        return Response.error("Mobster has no suite with that id. It keeps the last ten.", 404, "suite_not_found")
    job.stop.set()
    return Response.json({"suite": job.public()})


def heals(request):
    """Accept or reject the heals of the checks named, or of every check with heals waiting."""
    from ..api_routes import Response
    from ..replay_core.heal import HealError
    from . import heal_review
    from .discover import DiscoveryError
    body = request.body if isinstance(request.body, dict) else {}
    if "project" in body:  # the Mac app's project page
        return heal_review.settled(request)
    if set(body) - {"root", "action", "checks"} or body.get("action") not in ("accept", "reject"):
        return _bad("Send root, action (accept or reject) and, if you like, checks.")
    root, bad = _root(body.get("root"))
    if bad is not None:
        return bad
    paths = []
    for item in body.get("checks") or ():
        path, bad = _inside(root, item, "checks")
        if bad is not None:
            return bad
        paths.append(str(path))
    if not (root / ".mobster" / "checks").is_dir():
        return Response.error(NO_CHECKS, 404, "no_checks")
    try:
        settled = [{"check": str(entry.path), "steps": count}
                   for entry, count in heal_review.settle(root, heal_review.pending(root, paths), body["action"])]
    except (DiscoveryError, HealError) as error:
        return Response.error(str(error), 409, "heals_unreadable")
    return Response.json({"action": body["action"], "settled": settled, "heals": _waiting(root)[0]})


# ------------------------------------------------------------------------------------------------ the pieces

def covering(root, source, entries):
    """The checks that cover the screen in ``source``, as `mobster test pr` would pick them if the whole file had
    changed: {screen, checks: [{path, reasons}]}; None for a file that shows no screen."""
    from . import pr
    if source.suffix.lower() not in pr.UI_SUFFIXES + pr.STRING_SUFFIXES:
        return None
    try:
        lines = source.read_text(encoding="utf-8", errors="replace")[:MAX_COVERING_BYTES].splitlines()
    except OSError:
        return None
    relative = source.relative_to(root).as_posix()
    changes = pr.Changes(base="", merge_base="", head="", top=root,
                         files=[pr.Change(path=relative, status="M", added=lines)])
    screens = pr.screens_in(changes, root)
    if not screens:
        return None
    picked = pr.pick(entries, screens)
    return {"screen": screens[0].name,
            "checks": [{"path": str(item.entry.path), "name": item.entry.name, "reasons": item.reasons[:4]}
                       for item in picked]}


def _root(value):
    """The project folder ``value`` names, or (None, Response 400)."""
    if not isinstance(value, str) or not value or len(value) > 4096 or not Path(value).is_absolute():
        return None, _bad("root is the project's absolute path.")
    root = Path(value).resolve()
    if not root.is_dir():
        return None, _bad(f"{root} isn't a folder.")
    return root, None


def _inside(root, value, field):
    """``value`` as an absolute path inside ``root``, or (None, Response 400)."""
    if not isinstance(value, str) or not value or len(value) > 4096 or not Path(value).is_absolute():
        return None, _bad(f"{field} takes absolute paths.")
    path = Path(value).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None, _bad(f"{value} isn't a file in {root}.")
    return path, None


def _bad(message):
    from ..api_routes import Response
    return Response.error(message, 400, "bad_request")


def _last_verdicts(root):
    """{check file: its last verdict}, the device that didn't pass first when it ran on several."""
    from .history import report
    last = {}
    for row in report(root)["checks"]:
        known = last.get(row["file"])
        if known is None or (known["status"] == "passed" and row["lastStatus"] != "passed"):
            last[row["file"]] = {"status": row["lastStatus"], "state": row["state"], "device": row["device"],
                                 "runs": row["runs"]}
    return last


def _entry_row(entry, last):
    check = entry.check
    return {"path": str(entry.path), "file": entry.file, "name": entry.name, "error": entry.error,
            "app": getattr(check, "bundle_id", None), "tags": list(entry.tags),
            "steps": len(check.steps) if check is not None else 0, "last": last}


def _waiting(root):
    """(the heals waiting, None) or ([], why they can't be read)."""
    from ..replay_core.heal import HealError
    from . import heal_review
    from .discover import DiscoveryError
    try:
        return heal_review.rows(heal_review.pending(root)), None
    except (DiscoveryError, HealError) as error:
        return [], str(error)


def _latest(runtime, root):
    job = runtime.editor_suites.latest()
    return job.public() if job is not None and job.root is not None and Path(job.root) == root else None
