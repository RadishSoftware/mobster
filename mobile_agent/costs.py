"""Published-rate estimates, not billing records. Rates verified 2026-09-19.

Sources: https://typesafe.ai/blog/introducing-system-one-models-and-jev
https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing
All arithmetic uses integer nanodollars until the public JSON boundary.
"""

import json
import math
from datetime import date, datetime, timezone

RATE_DATE = "2026-09-19"
NANODOLLARS = 1_000_000_000
GOOGLE_RATE_SOURCE = "https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing"
# Integer nanodollars per token: input, output (including reasoning), cached input.
# Standard global on-demand rates, before promotional credits. These are not
# Priority/Flex/Batch rates and cannot be applied to an arbitrary provider/region.
GOOGLE_RATES = {
    "gemini-3.5-flash-lite": (300, 2500, 30),
    "gemini-3.7-flash": (1500, 7500, 150),
    "gemini-3.1-pro-preview": (2000, 12000, 200),
}
PRO_LONG_CONTEXT_RATES = (4000, 18000, 400)
# Jev bills input only: 42 nanodollars ($0.042 per million) per input token.
JEV_MODELS = frozenset({"jev-latest", "jev-1.13.0"})
JEV_INPUT_NANODOLLARS = 42


def _public_rates(rates):
    return dict(zip(("input_usd_per_million", "output_usd_per_million",
                     "cached_input_usd_per_million"), (value / 1000 for value in rates)))


def google_model_rate(model, region, *, today=None):
    """Display the base context tier without simulating a million-token request.

    The 3.7 promotion is delivered as credits back on net spend. Show it, but
    never presume an account has received those credits in spend estimates.
    """
    if region != "global" or model not in GOOGLE_RATES:
        return None
    result = {"model": model, **_public_rates(GOOGLE_RATES[model]),
              "rate_date": RATE_DATE, "source": GOOGLE_RATE_SOURCE}
    if model == "gemini-3.1-pro-preview":
        result["context_tiers"] = [{"above_input_tokens": 200_000,
                                    **_public_rates(PRO_LONG_CONTEXT_RATES)}]
    if (model == "gemini-3.7-flash"
            and (today or datetime.now(timezone.utc).date()) <= date(2026, 12, 31)):
        result["promotion"] = {"through": "2026-12-31", **_public_rates((750, 3750, 75)),
                               "description": "50% credits back on net spend; estimates exclude promotional credits."}
    return result


def helper_model_rates(models, region):
    return {model: rate for model in models if (rate := google_model_rate(model, region)) is not None}


def estimate_cost(provider, model, usage, region=None):
    unknown = {"estimated_usd": None, "cost_basis": None, "cost_nanodollars": None}
    if not isinstance(usage, dict) or any(type(n) is not int or n < 0 for n in usage.values()):
        return unknown
    incoming = usage.get("input_tokens")
    if incoming is None:
        return unknown
    if provider == "typesafe" and model in JEV_MODELS:
        cost = incoming * JEV_INPUT_NANODOLLARS
    elif provider == "google" and model in GOOGLE_RATES and region == "global":
        outgoing = usage.get("output_tokens")
        total = usage.get("total_tokens")
        reasoning = usage.get("reasoning_tokens", 0)
        cached = usage.get("cached_input_tokens", 0)
        # Do not price a response with an unexplained token remainder, missing
        # output counts, or tool-use input under a text-only rate calculation.
        if (outgoing is None or total != incoming + outgoing + reasoning
                or cached > incoming or usage.get("tool_input_tokens", 0)):
            return unknown
        # The context tier applies to the entire request, including output and
        # cached input, not just tokens beyond the 200K threshold.
        input_rate, output_rate, cached_rate = (PRO_LONG_CONTEXT_RATES
            if model == "gemini-3.1-pro-preview" and incoming > 200_000 else GOOGLE_RATES[model])
        cost = (incoming - cached) * input_rate + cached * cached_rate + (outgoing + reasoning) * output_rate
    else:
        return unknown
    if cost > 9_007_199_254_740_991:
        return unknown
    return {"estimated_usd": cost / NANODOLLARS, "cost_nanodollars": cost,
            "cost_basis": "published_rate", "rate_date": RATE_DATE}


def path_cost_totals(events):
    """Per-run System One vs System Two USD/token/latency totals from telemetry.

    Missing usage or price stays missing (None), never a synthetic zero. This
    is published-rate accounting, not billing. ``events`` are inference
    started/finished rows emitted by ``inference.request_inference``.
    """
    from .hybrid import Path, hybrid_totals

    totals = hybrid_totals(events)
    one, two = totals[Path.SYSTEM_ONE], totals[Path.SYSTEM_TWO]
    costs = [row["cost_nanodollars"] for row in (one, two) if row["cost_nanodollars"] is not None]
    tokens = [row["total_tokens"] for row in (one, two) if row["total_tokens"] is not None]
    return {
        "system_one": one,
        "system_two": two,
        "combined": {
            "calls": one["calls"] + two["calls"],
            "cost_nanodollars": sum(costs) if costs else None,
            "estimated_usd": (sum(costs) / NANODOLLARS) if costs else None,
            "total_tokens": sum(tokens) if tokens else None,
        },
        "cost_basis": "published_rate",
    }


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
    unknown: the guard trips on observed priced spend, and the unpriced count is
    always visible next to it so a cap is never mistaken for a billing record.
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

    def at_cap(self, cap_nanodollars):
        """True once observed priced spend reaches the cap. Exact integer comparison."""
        return self.cost_nanodollars >= cap_nanodollars


def planning_estimate(jev_model, helper_model=None, helper_region=None, *, goal="", output_schema=None):
    """An explicitly assumed planning range, never a measured or capped quote.

    Token counts use a disclosed rough byte ratio, not provider tokenization.
    Unseen app state and future step count remain explicit scenario assumptions.
    """
    prompt_tokens = math.ceil(len(goal.encode("utf-8")) / 4)
    schema_tokens = math.ceil(len(json.dumps(output_schema, separators=(",", ":")).encode("utf-8")) / 4) if output_schema else 0
    decision_input = 5000 + prompt_tokens
    helper_input = 2000 + prompt_tokens + schema_tokens
    jev = estimate_cost("typesafe", jev_model, {"input_tokens": decision_input})
    helper = estimate_cost("google", helper_model, {
        "input_tokens": helper_input, "output_tokens": 300, "total_tokens": helper_input + 300}, helper_region) if helper_model else None
    known = jev["cost_nanodollars"] is not None and (helper is None or helper["cost_nanodollars"] is not None)
    low = high = None
    if known:
        low = (5 * jev["cost_nanodollars"] + (2 * helper["cost_nanodollars"] if helper else 0)) / NANODOLLARS
        high = (20 * jev["cost_nanodollars"] + (6 * helper["cost_nanodollars"] if helper else 0)) / NANODOLLARS
    rates = {}
    if jev["cost_nanodollars"] is not None:
        rates["typesafe"] = {"model": jev_model, "input_usd_per_million": JEV_INPUT_NANODOLLARS / 1000,
                             "output_usd_per_million": 0}
    if rate := google_model_rate(helper_model, helper_region):
        rates["google"] = rate
    return {"basis": "planning_scenario", "helperModel": helper_model,
            "estimated_min_usd": low, "estimated_max_usd": high, "rates": rates,
            "rate_date": RATE_DATE, "confidence": "low", "is_spending_cap": False,
            "assumptions": {"decision_calls": [5, 20], "decision_input_tokens_per_call": decision_input,
                "helper_calls": [2, 6] if helper else [0, 0],
                "helper_input_tokens_per_call": helper_input, "helper_output_tokens_per_call": 300,
                "estimated_prompt_tokens": prompt_tokens, "estimated_schema_tokens": schema_tokens,
                "token_estimation": "Approximately one token per four UTF-8 bytes; not provider tokenization."},
            "description": "Low-confidence planning range using prompt size and assumed steps. Excludes promotional credits. Actual steps and spend can exceed it; this is not a spending cap."}
