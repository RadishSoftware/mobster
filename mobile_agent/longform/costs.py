"""The cost and phone-time model of a long task (DESIGN §10)."""

import math

from .budget import ITEMS_RANGE

# Tokens per unit: (fresh input, cached input, output).
NAV_TOKENS = (3500, 4000, 180)
EXTRACT_TOKENS = (700, 700, 120)
DIGEST_TOKENS = (3000, 0, 300)
CONTRACT_USD = 0.005
# Synthesis: base + per kept item (Opus 5.5 at medium thinking; Sonnet 5.5 at low).
SYNTH_BASE = {"claude-opus-5-5": 0.04, "claude-sonnet-5-5": 0.012, "gpt-5.6-sol": 0.03}
SYNTH_PER_ITEM = {"claude-opus-5-5": 0.0014, "claude-sonnet-5-5": 0.0004, "gpt-5.6-sol": 0.0010}
# Measured on the scripted world's notebooks, 8 Oct 2026 (core's synthesis A/B): Sonnet 10 to 12 s, Opus 19 to 26 s.
REPORT_SECONDS = {"claude-opus-5-5": 25, "claude-sonnet-5-5": 11, "gpt-5.6-sol": 20}
# Phone seconds: a navigation turn (model call, act, settle) and one item of each kind.
NAV_TURN_S = 3.6
ITEM_S = {"reel": 2.0, "tiktok": 2.0, "short": 2.0, "post": 2.0, "profile": 5.5, "chat": 4.0, "row": 3.0,
          "result": 3.0}
SKIP_S = 0.8                 # an ad or a repeat, swiped past without frames
SKIPPED_SHARE = {"reel": 0.16, "tiktok": 0.16, "short": 0.16, "post": 0.1}   # 5 ads and 3 repeats per 50 kept
LOW, HIGH = 0.6, 1.8
EXTRACT_MODEL = "claude-haiku-5-5"


def unit_usd(model, tokens):
    """List price of one call of ``tokens`` (fresh, cached, out) on ``model``; Sonnet's rates for an unpriced one."""
    from ..model_clients import PRICES
    price = PRICES.get(model) or PRICES["claude-sonnet-5-5"]
    fresh, cached, out = tokens
    cached_rate = price[0] if price[2] is None else price[2]
    return (fresh * price[0] + cached * cached_rate + out * price[1]) / 1e6


def nav_usd(model):
    return unit_usd(model, NAV_TOKENS)


EXTRACT_USD = unit_usd(EXTRACT_MODEL, EXTRACT_TOKENS)
DIGEST_USD = unit_usd(EXTRACT_MODEL, DIGEST_TOKENS)


def synth_usd(model, kept):
    base = SYNTH_BASE.get(model, SYNTH_BASE["claude-sonnet-5-5"])
    per = SYNTH_PER_ITEM.get(model, SYNTH_PER_ITEM["claude-sonnet-5-5"])
    if kept > 120:
        # Failed or partial map chunks leave their items in the report prompt. Reserve the largest raw prompt the
        # synthesis accepts, plus all chunk calls, even when the usual successful prompt needs only 40 raw items.
        chunks = math.ceil(kept / 60)
        return round(base + min(kept, ITEMS_RANGE[1]) * per + chunks * DIGEST_USD * 4, 5)
    return round(base + kept * per, 5)


def synth_estimate(items, model):
    """The synthesis reserve: what writing the report from ``items`` kept items costs on ``model``, plus 25%."""
    return round(synth_usd(model, max(0, int(items or 0))) * 1.25, 5)


def nav_turns(kind, items=0, steps=1):
    """Navigation turns a plan takes."""
    items = max(0, int(items or 0))
    return {"feed": 8.0, "survey": 8 + 0.1 * items, "chain": 4.0 + 3.0 * max(1, steps),
            "browse": 10.0}.get(kind, 10.0)


def estimate(kind, *, items, item_kind="reel", steps=1, nav_model="claude-sonnet-5-5",
             synth_model="claude-opus-5-5", phone_seconds_cap=None):
    """{"phoneSeconds", "reportSeconds", "usd", "lowUsd", "highUsd", "items"} for a plan."""
    turns = nav_turns(kind, items or 0, steps)
    per_item = ITEM_S.get(item_kind, 3.0)
    skipped_share = SKIPPED_SHARE.get(item_kind, 0.0) if kind in ("feed", "browse") else 0.0
    if items is None and kind == "chain":
        items = 0
    if items is None:
        room = max(0.0, (phone_seconds_cap or 600.0) - turns * NAV_TURN_S)
        kept = int(room / (per_item + skipped_share * SKIP_S))
        turns = nav_turns(kind, kept, steps) + kept / 60   # a macro slice returns every 60 items
    else:
        kept = int(items)
    skipped = round(kept * skipped_share)
    usd = (turns * nav_usd(nav_model) + kept * EXTRACT_USD + math.ceil(kept / 15) * DIGEST_USD
           + synth_usd(synth_model, kept) + CONTRACT_USD)
    phone = turns * NAV_TURN_S + kept * per_item + skipped * SKIP_S
    if phone_seconds_cap:
        phone = min(phone, phone_seconds_cap)
    return {"phoneSeconds": round(phone), "reportSeconds": REPORT_SECONDS.get(synth_model, 15),
            "usd": round(usd, 2), "lowUsd": round(usd * LOW, 2), "highUsd": round(usd * HIGH, 2), "items": kept}


def ceil_half(usd):
    """Up to the next $0.50."""
    return math.ceil(round(usd * 2, 6)) / 2
