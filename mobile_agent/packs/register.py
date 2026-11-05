"""Track ``packs``'s registrations (tracks.load calls ``register(api)`` once per process, staged)."""

import functools
import time as _time

from . import pins

TOOL_ORDER = 4
CONTEXT_ORDER = 40
SLEEP = _time.sleep                  # the health run's pause after a tap (tests set it to nothing)
# The packs Mobster ships, by module: each module has PACK, an instance of core.Pack.
MODULES = ("instagram", "tiktok", "messages", "settings", "calendar", "notes", "maps", "photos", "safari",
           "x", "youtube", "threads", "linkedin", "reddit")
_extra = []


def all_packs():
    """Every pack this build has, in MODULES order, then any a test added (``add_for_tests``)."""
    import importlib
    out = []
    for name in MODULES:
        try:
            module = importlib.import_module(f"{__package__}.{name}")
        except ModuleNotFoundError as error:
            if error.name != f"{__package__}.{name}":
                raise
            continue
        out.append(module.PACK)
    return out + list(_extra)


def add_for_tests(pack):
    _extra.append(pack)
    return lambda: _extra.remove(pack)


def status_route(request):
    """GET /api/packs: {packs: [health.status ...]}."""
    from pathlib import Path

    from ..api_routes import Response
    from . import core, health
    runtime = request.runtime
    state_db = getattr(getattr(runtime, "config", None), "state_db", None)
    entries = health.recent(Path(state_db).parent if state_db else None)
    return Response.json({"packs": [health.status(pack, core.app_version(runtime, pack.bundle), entries)
                                    for pack in all_packs()]})


class Confirmer:
    def on_finished(self, run, summary, private):
        if (summary or {}).get("status") == "completed":
            from . import heal
            heal.confirm_run(getattr(run, "id", None))


def register(api):
    """``api`` is mobile_agent.harness_api."""
    from .. import api_routes
    from ..mcp_server import registry as mcp_registry
    from . import context, mcp, tools
    packs = all_packs()
    if packs:
        # What a pack read stays in front of the model on later turns (context.py).
        api.register_context_provider(context.factory(packs), order=CONTEXT_ORDER)
    for pack in packs:
        api.register_tool(tools.factory(pack, "OPEN"), order=TOOL_ORDER)
        api.register_tool(tools.factory(pack, "READ"), order=TOOL_ORDER)
    if packs:
        mcp_registry.register_provider(mcp.PacksProvider(packs))
        # A run that finished completed confirms the variants proposed from screens a pack missed in it (heal.py).
        api.register_run_listener(Confirmer())
    api_routes.register(api_routes.Route("packs", "GET", "/api/packs", status_route, body="none"))
    if packs:
        # The watch: a read-only health run per app, as a task with packCheck (a schedule can run it).
        api.register_run_field("packCheck", functools.partial(validate_pack_key, "packCheck"))
        api.register_pre_run(pack_check, order=3)
        # The numbers: a read-only run per app that stores today's profile and Insights numbers (accounts/stats.py).
        api.register_run_field("packStats", functools.partial(validate_pack_key, "packStats"))
        api.register_pre_run(pack_stats, order=3)
        # A workflow's "Read an app" step (graph app.read): one reader's rows, by code, no model.
        api.register_run_field("packRead", validate_pack_read)
        api.register_pre_run(pack_read, order=3)
        # The front door: a request that is exactly a pack reader's question or a Settings page is answered by code,
        # before any model call (front_door.py).
        from . import front_door
        api.register_pre_run(front_door.hook, order=front_door.ORDER)


def validate_pack_key(field, value, runtime):
    """A pack run field (``packCheck``, ``packStats``): a pack's key; the run is that pack's read-only run."""
    if not isinstance(value, str) or value not in {pack.key for pack in all_packs()}:
        raise ValueError(f"{field} is the key of an app pack (GET /api/packs)")
    return value


def validate_pack_read(value, runtime):
    """``packRead``: {"pack", "reader", "n", "arg"}, one of a pack's readers; the run is that read, with no model."""
    if not isinstance(value, dict) or set(value) - {"pack", "reader", "n", "arg"}:
        raise ValueError("packRead is {pack, reader, n, arg}")
    pack = next((p for p in all_packs() if p.key == value.get("pack")), None)
    if pack is None or value.get("reader") not in pack.readers:
        raise ValueError("packRead names an app pack and one of its readers (GET /api/packs)")
    n = value.get("n", 10)
    if type(n) is not int or not 1 <= n <= 50:
        raise ValueError("packRead reads 1 to 50 rows")
    arg = value.get("arg")
    if arg is not None and (not isinstance(arg, str) or not 1 <= len(arg.strip()) <= 200):
        raise ValueError("packRead's arg is 1 to 200 characters")
    return {"pack": pack.key, "reader": value["reader"], "n": n, "arg": arg.strip() if arg else None}


def stepping_aside(hook):
    """A pack pre-run hook for a run that names its pack (packRead, packCheck, packStats): when the team's pin has the
    pack step aside, the run ends saying so, and never goes on to a model that nobody asked for."""
    @functools.wraps(hook)
    def wrapped(ctx, driver, approve):
        from .. import harness_api
        from . import core
        try:
            return hook(ctx, driver, approve)
        except core.PinnedAside as aside:
            return harness_api.PreRun(result={
                "event": "result", "status": "completion_not_confirmed", "engine": "smart", "outcome": "check",
                "answer": None, "reason": str(aside), "actions": 0, "decisions": 0, "costUsd": 0.0, "model": None,
                "elapsed_ms": 0, "independently_verified": False, "proof": []})
    return wrapped


@stepping_aside
def pack_read(ctx, driver, approve):
    """Pre-run hook: a run with ``packRead`` reads that reader's rows on the phone by code and ends with them as its
    data and a table."""
    spec = (ctx.extras or {}).get("packRead")
    if not spec or driver is None:
        return None
    import time

    from .. import harness_api
    from .. import result_blocks as rb
    from . import core
    pack = next(p for p in all_packs() if p.key == spec["pack"])
    io = _io(ctx, driver, pack)       # a pack the team's pin has step aside ends the run here, before anything acts
    # An acting event, journaled before the first tap: a crash mid-run is never read as a run that didn't act.
    ctx.emit({"event": "skill_started", "op": f"{pack.prefix}_READ"})
    started = time.monotonic()

    def ended(status, outcome, answer, reason=None, data=None, blocks=()):
        return harness_api.PreRun(result={
            "event": "result", "status": status, "engine": "smart", "outcome": outcome, "answer": answer,
            "reason": reason, "data": data, "actions": 1, "decisions": 0, "costUsd": 0.0, "model": None,
            "elapsed_ms": round((time.monotonic() - started) * 1000), "blocks": rb.envelope(list(blocks)),
            "independently_verified": outcome == "done", "proof": []})
    try:
        result = pack.read(io, spec["reader"], spec["n"], spec.get("arg"))
    except core.Lost as lost:
        said = str(lost).rstrip(".")
        return ended("completion_not_confirmed", "check", None,
                     f"Mobster couldn't read {pack.name}: {said}. Check the phone shows {pack.name}, "
                     "then run it again.")
    scalars = (str, int, float, bool, type(None))
    rows = [{k: v for k, v in row.items() if isinstance(v, scalars)} for row in result.rows]
    columns = [{"key": k, "label": label, "type": kind} for k, label, kind in result.columns]
    data = {"rows": rows, "columns": columns, "title": result.title, "stopped": result.stopped or None}
    table = rb.table(columns, rows, title=result.title, id=f"{pack.key}-{spec['reader']}") if rows else None
    return ended("completed", "done", f"{result.title}: {len(rows)} {'row' if len(rows) == 1 else 'rows'}.",
                 data=data, blocks=[table] if table else ())


def _io(ctx, driver, pack):
    from . import core
    pinned = pins.aside(pack)
    if pinned:
        raise core.PinnedAside(pinned)
    return core.PackIO(driver, pack, cancelled=ctx.cancelled, sleep=SLEEP,
                       allows_app=lambda bundle: ctx.runtime is None or getattr(ctx.runtime, "allows_app",
                                                                                lambda *a: True)(ctx.run_id, bundle))


def pack_run(ctx, driver, field, op):
    """What both pack pre-run hooks start from: ``(pack, io, started)`` for the run's ``field``, or None when the run
    doesn't carry it or has no phone."""
    key = (ctx.extras or {}).get(field)
    if not key or driver is None:
        return None
    import time
    pack = next(p for p in all_packs() if p.key == key)
    io = _io(ctx, driver, pack)       # a pack the team's pin has step aside ends the run here, before anything acts
    # An acting event, journaled before the first tap: a crash mid-run is never read as a run that didn't act.
    ctx.emit({"event": "skill_started", "op": f"{pack.prefix}_{op}"})
    return pack, io, time.monotonic()


@stepping_aside
def pack_check(ctx, driver, approve):
    """Pre-run hook: a run with ``packCheck`` visits the pack's tab screens on the phone, records each in the health
    log,
    and ends with a checklist."""
    run = pack_run(ctx, driver, "packCheck", "CHECK")
    if run is None:
        return None
    import time

    from . import core, health
    pack, io, started = run
    rows = health.check(pack, io)
    version = core.app_version(ctx.runtime, pack.bundle)
    for row in rows:
        health.record(ctx.data_dir, {"pack": pack.key, "op": "CHECK", "step": row["route"], "ok": row["landed"],
                                     "screen": row["screen"], "confidence": row["confidence"], "version": version,
                                     **({"unsure_screen": pack.routes[row["route"]].lands[0]}
                                        if not row["landed"] and not row["screen"] else {})})
    ctx.emit({"event": "app_pack_check", "pack": pack.key, "routes": len(rows),
              "landed": sum(1 for row in rows if row["landed"]), "version": version})
    from .. import harness_api
    return harness_api.PreRun(result=health.check_result(pack, rows, version, time.monotonic() - started))


@stepping_aside
def pack_stats(ctx, driver, approve):
    """Pre-run hook: a run with ``packStats`` reads the signed-in profile's numbers and its Insights overview and stores
    them as today's row (accounts/stats.py), no model."""
    run = pack_run(ctx, driver, "packStats", "STATS")
    if run is None:
        return None
    import time

    from .. import harness_api
    from .. import result_blocks as rb
    from ..accounts import stats
    from . import core
    pack, io, started = run
    account = (ctx.extras or {}).get("account")
    wanted = account.get("handle") if isinstance(account, dict) else None

    def done(answer, rows=(), ok=True):
        blocks = [rb.table([{"label": label, "key": k, "type": kind} for k, label, kind in pack_columns],
                           list(rows), title=f"{pack.name} numbers", id=f"{pack.key}-stats")] if rows else []
        return harness_api.PreRun(result={
            "event": "result", "status": "completed", "engine": "smart", "outcome": "done" if ok else "check",
            "answer": answer, "actions": 1, "decisions": 0, "elapsed_ms": round((time.monotonic() - started) * 1000),
            "costUsd": 0.0, "model": None, "blocks": rb.envelope(blocks)})

    pack_columns = (("handle", "Profile", "text"), ("followers", "Followers", "number"),
                    ("following", "Following", "number"), ("posts", "Posts", "number"), ("likes", "Likes", "number"),
                    ("period", "Period", "text"), ("views", "Views", "number"), ("reach", "Reached", "number"),
                    ("profile_visits", "Profile visits", "number"), ("follower_change", "Follower change", "number"))
    try:
        profile = pack.read(io, "profile", 1, None)
        mine = profile.rows[0] if profile.rows else {}
        handle = mine.get("handle")
        if mine.get("state") != "yours" or not handle:
            return done(f"Mobster didn't store {pack.name} numbers: the Profile tab doesn't show your own profile. "
                        "Open your profile in the app, then run this again.", ok=False)
        if wanted and wanted.lstrip("@").casefold() != handle.lstrip("@").casefold():
            return done(f"Mobster didn't store {pack.name} numbers: the app shows @{handle}, "
                        f"not @{wanted.lstrip('@')}. "
                        "Switch to that account, then run this again.", ok=False)
        values = {k: mine.get(k) for k in ("followers", "following", "posts", "likes")}
        try:
            overview = pack.read(io, "insights", 1, None).rows[0]
            values.update({k: overview.get(k) for k in ("period", "views", "reach", "profile_visits",
                                                        "follower_change")})
            note = ""
        except core.Lost as lost:
            note = f" Insights weren't read: {str(lost).rstrip('.')}."
        folder = ctx.data_dir
        if ctx.runtime is not None:
            from ..accounts.store import store_for
            folder = store_for(ctx.runtime).data_dir
        stats.record(folder, pack.key, handle, values)
    except core.Lost as lost:
        return done(f"Mobster couldn't read {pack.name}: {str(lost).rstrip('.')}. Check the phone shows "
                    f"{pack.name}, then run this again.", ok=False)
    ctx.emit({"event": "app_pack_stats", "pack": pack.key})
    return done(f"Stored @{handle}'s {pack.name} numbers for today.{note}", [{"handle": handle, **values}])
