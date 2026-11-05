"""Track ``longform``'s registrations (tracks.load calls ``register(api)`` once per process, staged)."""

import math

from . import TRACK


def validate_field(value, runtime):
    """``longform``: an object with planId (a plan card's id), alternative ("full"), budget and capUsd."""
    if not isinstance(value, dict) or set(value) - {"planId", "alternative", "budget", "capUsd"}:
        raise ValueError("longform takes planId, alternative, budget and capUsd")
    clean = {}
    if value.get("planId") is not None:
        if not isinstance(value["planId"], str) or not value["planId"].startswith("p_") or len(value["planId"]) > 20:
            raise ValueError("longform.planId is a plan's id")
        clean["planId"] = value["planId"]
    if value.get("alternative") is not None:
        if value["alternative"] not in ("full", "more"):
            raise ValueError("longform.alternative is full or more")
        clean["alternative"] = value["alternative"]
    if value.get("budget") is not None:
        from .budget import ALL_CAP, Budget, override
        override(Budget("items", None, ALL_CAP, False, 600.0, "items", "", "row"), value["budget"])  # shape check
        clean["budget"] = dict(value["budget"])
    if value.get("capUsd") is not None:
        from ..runtime_limits import TASK_LIMIT_RANGE
        cap = value["capUsd"]
        if isinstance(cap, bool) or not isinstance(cap, (int, float)) or not math.isfinite(cap) \
                or not TASK_LIMIT_RANGE[0] <= cap <= TASK_LIMIT_RANGE[1]:
            raise ValueError("longform.capUsd is between $0.05 and $100")
        clean["capUsd"] = round(float(cap), 2)
    return clean


def frontier_options(ctx):
    """A long run's caps: 60 + 12 a step + 1 per 10 items turns (at most 600), the phone-time cap plus 300 s (at
    most 7,200)."""
    from .budget import turn_caps
    from .session import for_run
    session = for_run(ctx.run_id)
    if session is None:
        return {}
    steps = len(session.plan.get("steps") or ()) or 1
    max_steps, max_seconds = turn_caps(session.budget, steps)
    used = getattr(session, "turns_used", 0) or 0
    return {"max_steps": int(min(600, max_steps + used)), "max_seconds": float(min(7200.0, max_seconds))}


def feed_factory(ctx):
    from .feed import factory
    return factory(ctx)


def items_factory(ctx):
    from .items import factory
    return factory(ctx)


def recall_factory(ctx):
    from .recall import factory
    return factory(ctx)


def pickup(ctx, driver, approve):
    """Pre-run hook (order 1, after harness's pick-up): a picked-up long run keeps its notebook, its counters, its
    step states and its remaining budget."""
    from .. import harness_api
    source = (ctx.extras or {}).get("resumeFrom")
    if not source or harness_api.disabled(TRACK):
        return None
    from .store import load_notebook
    runtime = ctx.runtime
    data = load_notebook(getattr(runtime, "journal", None), source)
    if data is None:
        return None
    from .session import _pickups
    turns = 0
    checkpoint_phone = {}
    try:
        from ..harness.checkpoints import load
        state = load(runtime.journal, source) or {}
        turns = int(state.get("step") or 0)
        longform = state.get("longform")
        if isinstance(longform, dict) and "phoneSeconds" in longform:
            checkpoint_phone["phone_seconds"] = longform["phoneSeconds"]
        elif longform is not None and not isinstance(longform, dict):
            checkpoint_phone["phone_seconds"] = None  # damaged checkpoint cannot grant more phone time
    except Exception:  # noqa: BLE001 -- a damaged checkpoint may not grant a fresh phone budget
        turns = 0
        checkpoint_phone["phone_seconds"] = None
    original = (getattr(runtime, "runs", None) or {}).get(source)
    _pickups[ctx.run_id] = {"notebook": data, "source": source, "turns": turns,
                            "cost_usd": getattr(original, "cost_usd", None) or 0.0, **checkpoint_phone}
    ctx.emit({"event": "longform_picked_up", "from": source, "items": len(data.get("items") or ()),
              "kept": (data.get("counters") or {}).get("kept", 0)})
    return None


pickup.after_handoff = True


def followup_hook(ctx, driver, approve):
    from .followup import pre_run
    return pre_run(ctx, driver, approve)


followup_hook.before_phone = True


def register(api):
    """``api`` is mobile_agent.harness_api."""
    from .. import api_routes, journal
    from . import routes, store
    journal.register_schema(store.SCHEMA, store.VERSION, store.migrate)
    api.register_run_field("longform", validate_field)
    api.register_frontier_options(frontier_options)
    api.register_tool(feed_factory, order=5)
    api.register_tool(items_factory, order=5)
    api.register_tool(recall_factory, order=5)
    api.register_pre_run(pickup, order=1)
    api.register_pre_run(followup_hook, order=5)
    api.register_service(lambda runtime: None, store.close)
    for route in routes.ROUTES:
        api_routes.register(route)
