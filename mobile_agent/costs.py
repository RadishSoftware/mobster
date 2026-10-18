"""Spend caps, the priced spend a run has observed (integer nanodollars until the public JSON boundary), the cost
estimate before a Smart task and ``meter``, which puts every model call in the usage ledger. Model prices live with
their clients (model_clients.cost_usd)."""

import math
import re
import time
import uuid

from . import harness_api
from .engines import OPENAI_SMART_MODEL, on_plan, provider_name, smart_model

NANODOLLARS = 1_000_000_000


def usd_to_nanodollars(value):
    """Operator cap to integer nanodollars. None stays None (unenforced)."""
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError("Spend cap must be a positive USD amount")
    return math.ceil(value * NANODOLLARS)


class SpendLedger:
    """Priced spend observed so far. Unpriced calls are counted, never zero-filled.

    A ledger only knows what inference telemetry reported. Unknown usage stays
    unknown, and the unpriced count is always visible next to the priced total
    so it is never mistaken for a billing record.
    """

    def __init__(self):
        self.cost_nanodollars = 0
        self.priced_calls = 0
        self.unpriced_calls = 0

    def add_event(self, event):
        """Fold one ``inference_finished`` event into the totals. Returns True if counted."""
        if not isinstance(event, dict) or event.get("event") != "inference_finished":
            return False
        cost = event.get("cost_nanodollars")
        if type(cost) is int and 0 <= cost <= 9_007_199_254_740_991:
            self.cost_nanodollars += cost
            self.priced_calls += 1
        else:
            self.unpriced_calls += 1
        return True


# -- the estimate and the metering -------------------------------------------------------------------

# What a Smart task costs, fitted on recorded runs of this loop on gpt-5.6-sol at list prices (27 Sep): the 152
# iOSWorld runs of sol-a/b, ab6 and ab7 (research/iosworld-runs) and 32 app runs on simulator 4, split by task,
# 70% to fit (app runs weighted 3x: they are the product) and 30% held out.
# log(cost) = a + b·log(words in the request) + c·(apps it names, up to 6). Held out, the estimate is within 2x
# of the actual cost on 46 of 56 runs (82%; the per-turn model it replaces managed 57% on the iOSWorld part),
# median actual/estimate 0.78; on ab7-b1/b2 30 of 32 (was 11). On app runs it errs high: median 0.67, within
# 2x on 16 of 32 (short tasks cost 1.7-14 cents for the same request). tests/smart_costs.json holds the runs
# and test_engines pins the held-out rate.
SMART_FIT = (-4.48, 0.615, 0.163)
SMART_MAX_APPS = 6
# The range around the estimate: halved to doubled holds about 80% of the recorded runs (10th and 90th
# percentiles of actual/estimate: 0.50 and 1.94).
SMART_SPREAD = 2.0
SMART_STRUCTURE_USD = 0.004  # the answer put into a structure (the user's schema, or an automatic format): one small call
# The fit is sol's (OPENAI_SMART_MODEL), not SMART_CONFIG.model's; another model's tasks cost this multiple of sol's
# on the same tasks, both at list prices (sol at $4/$20). Paired iOSWorld A/B, 16 never-run tasks, 28 Sep: Sonnet 5.5
# $0.197 against sol's $0.291 a task (32 runs and 21; 95% CI of the difference -$0.143 to -$0.044), Opus 5.5 $0.297
# (16 runs).
SMART_COST_RATIO = {OPENAI_SMART_MODEL: 1.0, "claude-sonnet-5-5": 0.68, "claude-opus-5-5": 1.02,
                    # Haiku 5.5 planning with code-checked steps cost 0.059-0.066 of Sonnet 5.5 on the same 16 tasks
                    # (paired A/Bs on the phone, 9 Oct): 0.68 x 0.065. gpt-6.1-sol's list price is half sol's, and a
                    # recorded turn cost 0.46 of sol's (9 Oct).
                    "claude-haiku-5-5": 0.044, "gpt-6.1-sol": 0.46}


def named_apps(goal, app_names=()):
    """How many of ``app_names`` the request names (whole words, any case)."""
    folded = " ".join(str(goal or "").split()).casefold()
    return sum(1 for name in set(app_names) if len(name) > 2 and re.search(rf"\b{re.escape(name.casefold())}\b", folded))


def smart_point(goal, app_names=()):
    """The likeliest cost (USD) of a Smart task for ``goal``: longer requests that name more apps cost more."""
    words = max(1, len(str(goal or "").split()))
    a, b, c = SMART_FIT
    return math.exp(a + b * math.log(words) + c * min(SMART_MAX_APPS, named_apps(goal, app_names)))


def smart_estimate(goal, app_names=(), *, available=True, reason=None, structured=False, model=None):
    model = model or smart_model()
    if on_plan(model):
        # Nothing per call: the run uses the person's plan allowance (``billing`` says so to the UI).
        return {"available": available, "reason": reason, "model": model, "provider": provider_name(model),
                "lowUsd": 0.0, "highUsd": 0.0, "billing": "plan"}
    point = smart_point(goal, app_names) * SMART_COST_RATIO.get(model, 1.0) + (SMART_STRUCTURE_USD if structured else 0)
    return {"available": available, "reason": reason, "model": model, "provider": provider_name(model),
            "lowUsd": round(point / SMART_SPREAD, 4), "highUsd": round(point * SMART_SPREAD, 4)}


def midpoint(estimate):
    """The estimate's single figure: the geometric mean of its range."""
    low, high = estimate["lowUsd"], estimate["highUsd"]
    return round(math.sqrt(low * high), 4) if low > 0 and high > 0 else high


def meter(client, emit, *, provider=None, purpose=None):
    """Wrap ``client.complete`` so every model call emits inference_started / inference_finished with its
    list-price cost (the usage ledger and the spend limits read those). The loop's own accounting
    (client.usage, model_clients.cost_usd) is untouched. ``client.model_failed`` says whether the last call failed.
    A client that hedges (model_clients.AnthropicChat) reports a losing twin's tokens through ``on_hedge_usage``
    when it finishes: it is billed, so it is recorded as a call of its own (purpose "hedge"). ``purpose``: a label
    for every call of this client (a long task's "extract", "digest", "synthesis" or "followup"), so the usage
    ledger and History show where the money went; None reads it from the schema as before."""
    from .model_clients import cost_usd
    inner = client.complete
    client.model_failed = False
    provider = provider or getattr(client, "provider", None) or "openai"

    def complete(messages, schema, **kwargs):
        call_id = uuid.uuid4().hex[:16]
        model = getattr(client, "model", "") or "unknown"
        kind = purpose or ("planning" if "items" in (schema.get("properties") or {}) and "actions" not in (
            schema.get("properties") or {}) else "decision")
        meta = {"provider": provider, "call_id": call_id, "model": model, "purpose": kind}
        if on_plan(model):
            providers = harness_api.model_routes()
            meta["plan"] = providers.plan_name(providers.get(providers.split(model)[0]))  # "On your Kimi Code plan"
        emit({"event": "inference_started", **meta})
        started = time.monotonic()
        try:
            out, usage = inner(messages, schema, **kwargs)
        except Exception as error:
            client.model_failed = True
            emit({"event": "inference_finished", **meta, "success": False,
                  "latency_ms": round((time.monotonic() - started) * 1000, 2), "usage": {},
                  "error_type": type(error).__name__, "estimated_usd": None, "cost_nanodollars": None,
                  "cost_basis": None})
            raise
        if kind != "planning":
            # The task contract compiles alongside the first decision (the early decision): its success, which
            # may land later, never hides that decision's failure.
            client.model_failed = False
        usage = usage or {}
        finished(meta, usage, started)
        return out, usage

    def finished(meta, usage, started):
        prompt, completion = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
        usd = cost_usd(meta["model"], usage)
        basis = "plan" if on_plan(meta["model"]) else "published_rate"
        emit({"event": "inference_finished", **meta, "success": True,
              "latency_ms": round((time.monotonic() - started) * 1000, 2),
              "usage": {"input_tokens": prompt, "output_tokens": completion, "total_tokens": prompt + completion,
                        "cached_input_tokens": int(usage.get("cached_tokens", 0))},
              "estimated_usd": usd, "cost_nanodollars": None if usd is None else round(usd * NANODOLLARS),
              "cost_basis": None if usd is None else basis})

    def hedged(usage):
        meta = {"provider": provider, "call_id": uuid.uuid4().hex[:16],
                "model": getattr(client, "model", "") or "unknown", "purpose": "hedge" if purpose is None
                else f"{purpose}_hedge"}
        emit({"event": "inference_started", **meta})
        finished(meta, usage or {}, time.monotonic())

    client.complete = complete
    if hasattr(client, "on_hedge_usage"):
        client.on_hedge_usage = hedged
    return client
