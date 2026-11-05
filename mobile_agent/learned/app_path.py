"""The app path: a run listener and a pre-run hook (registered by learned/register.py)."""

from pathlib import Path

from ..replay_core.recorder import typed_plainly
from .parts import Parts, parts_on
from .programs import (
    ITEM_FIELDS,
    ORIGINS,
    Programs,
    accept,
    code_fills,
    holds_secret,
    learn,
    match,
    mode,
    programs_dir,
    settle,
    taught_on,
)
from .taught import apply_corrections
from .workflows import Runs

_RUNS = Runs(lambda: {"data_dir": None, "recorder": None, "matched": None, "parts": None})


def _route_version(runtime, bundle):
    """The app's version for the route corpus (compiled_read/routes.py), or None with the flag off."""
    from ..compiled_read import enabled, routes
    return routes.version_of(runtime, bundle) if enabled() else None


def _eligible(run):
    return (mode(origin=getattr(run, "origin", None)) != "off" and getattr(run, "origin", None) in ORIGINS
            and (getattr(run, "engine", None) or "smart") == "smart" and getattr(run, "mode", "live") == "live")


def _settle_run(store, matched, done, run_id):
    """Settle a replayed program after its run; a run the model helped with never trusts it."""
    if apply_corrections(store, matched) is not None:
        return None
    replayed_all = matched.stopped == "done" and not getattr(matched, "helped", False)
    return settle(store, matched=matched, done=done, replayed_all=replayed_all, run_id=run_id)


def _settles(run):
    """A run whose taught program is settled though it learns nothing (a saved workflow's run, its schedule's)."""
    return (taught_on(origin=getattr(run, "origin", None)) and (getattr(run, "engine", None) or "smart") == "smart"
            and getattr(run, "mode", "live") == "live")


class ProgramListener:

    def on_created(self, run, runtime):
        learns = _eligible(run)
        if not learns and not _settles(run):
            return
        state_db = getattr(getattr(runtime, "config", None), "state_db", None)
        data_dir = getattr(run, "data_dir", None) or (Path(state_db).parent if state_db else None)
        if data_dir is not None:
            from ..replay_core.recorder import Recorder
            entry = _RUNS.get(run.id)
            entry["data_dir"] = Path(data_dir)
            entry["version"] = _route_version(runtime, (getattr(run, "app", None) or {}).get("bundleId"))
            if learns:
                entry["recorder"] = Recorder(text_policy=lambda text: ("literal", text) if typed_plainly(text)
                                             else ("live", None))

    def on_step(self, run, record):
        entry = _RUNS.get(getattr(run, "id", None), create=False)
        if entry is not None and entry.get("recorder") is not None:
            entry["recorder"].observe_step(record)

    def on_finished(self, run, summary, private):
        entry = _RUNS.pop(getattr(run, "id", None))
        if entry is None or entry.get("data_dir") is None:
            return
        if not _eligible(run):
            # A saved workflow's run that replayed a taught program: settle it; nothing is learned from the run.
            if _settles(run) and entry.get("matched") is not None:
                done = (summary or {}).get("status") == "completed" and (summary or {}).get("outcome") == "done"
                try:
                    _settle_run(Programs(programs_dir(entry["data_dir"])), entry["matched"], done,
                                getattr(run, "id", None))
                except (OSError, ValueError):
                    pass
            return
        from .workflows import device_key, first_of_run, pin_launches
        store = Programs(programs_dir(entry["data_dir"]), version=entry.get("version"))
        done = (summary or {}).get("status") == "completed" and (summary or {}).get("outcome") == "done"
        matched = entry.get("matched")
        parts = entry.get("parts")
        try:
            proven = _settle_run(store, matched, done, run.id) if matched is not None else None
            if parts is not None:
                parts.settle(done)
            if done:
                steps = pin_launches(list(entry["recorder"].steps)) if entry.get("recorder") else []
                for step in steps:
                    screen = step.get("screen") or {}
                    if parts is not None and not screen.get("signature") and screen.get("hash") in parts.pairs:
                        step["screen"] = {**screen, "signature": parts.pairs[screen["hash"]]}
                items = list((private or {}).get("contract") or ())
                if any(holds_secret(str(item.get(name) or "")) for item in items if isinstance(item, dict)
                       for name in ITEM_FIELDS):
                    items = []  # a secret in the contract: no contract is kept
                learn(store, app=(getattr(run, "app", None) or {}).get("bundleId"), goal=getattr(run, "goal", ""),
                      device=device_key(run), steps=steps, items=items, done=True, run_id=run.id, trusted=proven,
                      origin=getattr(run, "origin", None), final_pairs=getattr(parts, "last_pairs", None),
                      first=None if (summary or {}).get("program") else first_of_run(run, summary))
        except (OSError, ValueError):
            pass


def pre_run(ctx, driver, approve):
    """harness_api pre-run hook: hand the live loop a saved program for this request."""
    from .. import harness_api
    from .workflows import device_key
    if ctx.engine != "smart" or ctx.data_dir is None:
        return None
    if (getattr(ctx, "extras", None) or {}).get("replay") in ("off", "only"):
        return None              # a workflow step's mode: the model every time, or its own learned steps only
    on = mode(origin=ctx.origin) == "on" and ctx.origin in ORIGINS
    if not on and not taught_on(origin=ctx.origin):
        return None
    store = Programs(programs_dir(ctx.data_dir), version=_route_version(getattr(ctx, "runtime", None), ctx.app_bundle))
    goal, app, device = ctx.goal, ctx.app_bundle, device_key(ctx)
    entry = _RUNS.get(ctx.run_id)
    entry["data_dir"] = entry.get("data_dir") or Path(ctx.data_dir)
    practice = (getattr(ctx, "extras", None) or {}).get("practice")
    if practice:
        # Try it now: that taught program, step by step, the person answering each step (Run.request_approval).
        path = store.by_id(practice)
        fills = code_fills((path or {}).get("identity", {}).get("template") or "", goal) if path else []
        run = getattr(getattr(ctx, "runtime", None), "runs", {}).get(ctx.run_id) if ctx.runtime is not None else None
        if not path or not path.get("taught") or not fills or run is None:
            return None  # fails closed: no practice, the task runs as any task does
        matched = accept(path, fills[0], goal, secret=holds_secret)
        if matched is None:
            return None
        matched.practice, matched.ask = True, run.request_approval
        entry["matched"] = matched
        return harness_api.PreRun(program=matched)
    parts = entry["parts"] = Parts(store, device=device, run_id=ctx.run_id) if on and parts_on() else None
    fillable = [path for path in store.candidates(app, device, goal, taught_only=not on)
                if code_fills((path.get("identity") or {}).get("template") or "", goal)]
    if not fillable:
        return harness_api.PreRun(parts=parts) if parts is not None else None

    def find(client, request):
        matched, _ = match(client, store, goal=goal, app=app, device=device, secret=holds_secret, taught_only=not on)
        entry["matched"] = matched
        return matched
    if len(fillable) == 1 and len(code_fills((fillable[0].get("identity") or {}).get("template") or "", goal)) == 1:
        matched = find(None, goal)
        return harness_api.PreRun(program=matched, parts=parts) if matched is not None or parts is not None else None
    return harness_api.PreRun(program=find, parts=parts)


__all__ = ["ProgramListener", "pre_run"]
