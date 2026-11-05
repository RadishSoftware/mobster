"""What a workflow graph should cost before it runs (GET /api/workflows/{id}/estimate): each node's range in dollars,
its model calls and who decides what it does, read from the node's own last runs when it has them, else from Mobster's
agent's estimate for its words. A loop multiplies its steps by its rounds when the run's rounds are known before it
starts; when they aren't (items, until), the estimate is for one round and says so (``partial``)."""

from statistics import median

from . import catalog

HISTORY_RUNS = 5
PHONE = frozenset({"phone.task", "phone.taught", "phone.check"})
CHARS_PER_TOKEN = 4
ASK_OUTPUT_TOKENS = (200, 2000)   # an ai.ask answer, short to long, before it has run


def estimate(graph, *, history, agent, rounds, child=None, ask_cost=None, replays=None):
    """Each node's cost range and model calls, and the total, from its history or the agent's estimate."""
    by_id = {node["id"]: node for node in graph.get("nodes") or ()}
    children = {}
    for node in by_id.values():
        children.setdefault(node.get("parent"), []).append(node["id"])
    out = {}

    def one(node):
        kind, config = node["type"], node.get("config") or {}
        past = (history.get(node["id"]) or [])[:HISTORY_RUNS]
        if kind == "phone.task" and config.get("mode") == "replay":
            return {"lowUsd": 0.0, "highUsd": 0.0, "modelCalls": 0, "basis": "replay", "decides": "replays"}
        if kind in ("phone.task", "phone.taught") and replays is not None and (decides := replays(node)):
            return {"lowUsd": 0.0, "highUsd": 0.0, "modelCalls": 0, "basis": "replay", "decides": decides}
        if kind in PHONE or kind == "ai.ask":
            if past:
                costs, calls = [p["costUsd"] for p in past], [p.get("modelCalls") for p in past]
                return {"lowUsd": round(min(costs), 4), "highUsd": round(max(costs), 4),
                        "modelCalls": None if None in calls else round(median(calls)), "basis": "history",
                        "runs": len(past), **({"decides": _decides(kind, past[0])} if kind in PHONE else {})}
            if kind == "ai.ask":
                return _ask(config, ask_cost)
            words = config.get("text") or config.get("question") or ""
            guess = agent(words, config.get("outputSchema")) if words or kind == "phone.taught" else None
            entry = {"lowUsd": 0.0 if kind == "phone.taught" else (guess or {}).get("lowUsd"),
                     "highUsd": (guess or {}).get("highUsd"), "modelCalls": None, "basis": "estimate",
                     "decides": "taught" if kind == "phone.taught" else "model"}
            if (guess or {}).get("billing") == "plan":
                entry["billing"] = "plan"
            return entry
        if kind == "workflow.call":
            total = child(node) if child is not None else None
            if total is None:
                return {"lowUsd": None, "highUsd": None, "modelCalls": None, "basis": "child"}
            return {**{k: total.get(k) for k in ("lowUsd", "highUsd", "modelCalls")}, "basis": "child",
                    **({"partial": True} if total.get("partial") else {})}
        if kind == "loop":
            return _loop(node, rounds(node))
        return {"lowUsd": 0.0, "highUsd": 0.0, "modelCalls": 0, "basis": "none",
                **({"decides": "code"} if kind == "app.read" else {})}

    def _loop(node, count):
        body = [walk(by_id[ident]) for ident in children.get(node["id"], ())]
        for ident in children.get(node["id"], ()):
            out[ident]["perRound"] = True
        times = count if count is not None else 1
        entry = _sum(body, times)
        entry.update(basis="estimate", rounds=count)
        if count is None:
            entry["partial"] = True
        return entry

    def walk(node):
        if node["id"] not in out:
            out[node["id"]] = one(node)
        return out[node["id"]]

    top = [walk(node) for node in by_id.values() if node.get("parent") is None]
    total = _sum(top, 1)
    limit = (graph.get("limits") or {}).get("maxSpendUsd", catalog.DEFAULT_LIMITS["maxSpendUsd"])
    total["overLimit"] = total["highUsd"] is not None and total["highUsd"] > limit
    total.setdefault("partial", False)
    return {"nodes": out, "total": total}


def of_workflow(owner, workflow, graph, depth=0):
    from .. import engines
    from ..api_errors import APIError
    from ..model_clients import cost_usd
    from . import nodes as graph_nodes
    history = {}
    for record in owner.batches.recent(workflow["id"], HISTORY_RUNS * 2):
        for entry in owner.batches.nodes(record["id"]):
            cost = entry.get("costUsd")
            if entry.get("state") != "done" or entry.get("reused") or entry.get("pinned") \
                    or isinstance(cost, bool) or not isinstance(cost, (int, float)):
                continue
            history.setdefault(entry["node"], []).append(
                {"costUsd": float(cost), "modelCalls": entry.get("modelCalls"), "replayed": entry.get("replayed")})
    estimates = getattr(owner.runtime, "engine_estimates", None)

    def agent(text, schema):
        try:
            # With no key for Mobster's agent a model step runs on the person's ready coding agent (Runtime.plan_agent,
            # DR-56): on their plan, so it costs nothing per call, and the label says so instead of API prices (DR-33).
            plan = getattr(owner.runtime, "plan_agent", None)
            chosen = plan() if callable(plan) else None
            if chosen:
                return {"lowUsd": 0.0, "highUsd": 0.0, "billing": "plan", "agent": chosen}
        except Exception:  # noqa: BLE001 -- unknown: priced as Mobster's agent is, below
            pass
        try:
            return estimates(text, schema)[engines.SMART] if callable(estimates) else None
        except Exception:  # noqa: BLE001 -- no estimate is said as unknown (None), never as free
            return None

    def child(node):
        called = (node.get("config") or {}).get("workflowId")
        if depth >= catalog.MAX_CALL_DEPTH or not called:
            return None
        try:
            return owner.estimate(called, _depth=depth + 1)["total"]
        except (APIError, LookupError):
            return None

    def rounds(node):
        config = node.get("config") or {}
        if config.get("over") == "times":
            return config.get("times")
        if config.get("over") == "rows":
            return min(len(config["rows"]), config["max"]) if isinstance(config.get("rows"), list) else None
        if config.get("over") == "phones":
            try:
                return len(graph_nodes.loop_phones(owner.runtime, config.get("phones"))[:config.get("max") or 1000])
            except graph_nodes.NodeFailed:
                return None
        return None                          # items and until: told only as the run goes

    def replays(node):
        return replay_kind(owner, node)

    return estimate(graph, history=history, agent=agent, rounds=rounds, child=child, replays=replays,
                    ask_cost=lambda model, tokens_in, tokens_out: cost_usd(
                        model, {"prompt_tokens": tokens_in, "completion_tokens": tokens_out}))


def replay_kind(owner, node):
    from pathlib import Path

    from ..learned.zero import replay_kind as kind_of
    try:
        state_db = getattr(getattr(owner.runtime, "config", None), "state_db", None)
        if not state_db:
            return None
        config = node.get("config") or {}
        goal = config.get("text")
        if node.get("type") == "phone.taught":
            with owner.journal.lock:
                goal = (owner._get(config.get("workflowId")) or {}).get("goal")
        if config.get("mode") == "agent":
            return None
        return kind_of(Path(state_db).parent, goal, learned=node.get("type") == "phone.task")
    except Exception:  # noqa: BLE001 -- unknown is priced as a model step, never as free
        return None


def _decides(kind, newest):
    if kind == "phone.taught":
        return "taught"
    return "replays" if newest.get("replayed") else "model"


def _ask(config, ask_cost):
    from .nodes import AI_MODELS, AI_SYSTEM, INPUT_CHARS
    model = AI_MODELS.get(config.get("model") or "haiku", AI_MODELS["haiku"])
    base = (len(AI_SYSTEM) + len(str(config.get("instruction") or ""))) // CHARS_PER_TOKEN
    low = ask_cost(model, base, ASK_OUTPUT_TOKENS[0]) if ask_cost is not None else None
    high = ask_cost(model, base + INPUT_CHARS // CHARS_PER_TOKEN, ASK_OUTPUT_TOKENS[1]) if ask_cost is not None \
        else None
    return {"lowUsd": None if low is None else round(low, 4), "highUsd": None if high is None else round(high, 4),
            "modelCalls": 1, "basis": "estimate"}


def _sum(entries, times):
    """The entries' sum, ``times`` over; a part that can't be told (None) makes the whole None."""
    out = {}
    for key, digits in (("lowUsd", 4), ("highUsd", 4), ("modelCalls", None)):
        values = [entry.get(key) for entry in entries]
        out[key] = None if any(v is None for v in values) else \
            (round(sum(values) * times, digits) if digits else sum(values) * times)
    if any(entry.get("partial") for entry in entries):
        out["partial"] = True
    return out
