"""Durable workspace usage independent of the bounded task history.

Rows contain only call identity, provider, numeric usage and price snapshots.
The caller writes them in the SAME transaction as the corresponding run event.
Historical uninstrumented calls and external account usage are not invented.

``billing`` says whose bill a call is on: "plan" (a subscription: Claude Code on a Claude plan, Codex on a ChatGPT
plan, a plan from Settings › Models) or "api_key". A plan call costs 0 (``cost_nanodollars``) and carries what the same
tokens would cost at list price in ``api_equivalent_nanodollars``, which is never counted as spend. ``input_tokens``
counts every prompt token, cache reads and writes included; ``cache_read_tokens`` and ``cache_write_tokens`` say how
many of them were.
"""

import json
import time

from datetime import datetime

from .costs import NANODOLLARS

# Who priced a call: Jev, Gemini on Vertex, an OpenAI-compatible helper, OpenAI or Anthropic (the Smart engine), or
# a plan or provider from Settings › Models (harness_api.model_routes; a plan call costs 0).
# The coding agents a task can run on (ai_plans.AGENT_ENGINES) record their runs under their own names.
PROVIDERS = frozenset({"typesafe", "google", "helper", "openai", "anthropic", "claude_code", "codex", "gemini"})
BILLINGS = frozenset({"plan", "api_key"})
# Columns added after the first ledger: added in place to a ledger that predates them (NULL in old rows: unknown).
ADDED_COLUMNS = (("cache_read_tokens", "INTEGER"), ("cache_write_tokens", "INTEGER"), ("billing", "TEXT"),
                 ("api_equivalent_nanodollars", "INTEGER"))
MAX_COUNT = 9_007_199_254_740_991


def known_provider(provider):
    from .harness_api import model_routes
    return provider in PROVIDERS or model_routes().get(provider) is not None


def initialize(connection):
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS usage_tracking (
            id INTEGER PRIMARY KEY CHECK(id=1), since REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS usage_calls (
            run_id TEXT NOT NULL, call_id TEXT NOT NULL,
            provider TEXT NOT NULL, model TEXT NOT NULL,
            started_at REAL NOT NULL, finished_at REAL,
            input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER,
            cost_nanodollars INTEGER, PRIMARY KEY(run_id,call_id));
        CREATE TABLE IF NOT EXISTS usage_limits (
            provider TEXT PRIMARY KEY, data TEXT NOT NULL, at REAL NOT NULL);
    """)
    present = {row[1] for row in connection.execute("PRAGMA table_info(usage_calls)")}
    for name, kind in ADDED_COLUMNS:
        if name not in present:
            connection.execute(f"ALTER TABLE usage_calls ADD COLUMN {name} {kind}")
    connection.execute("INSERT OR IGNORE INTO usage_tracking VALUES (1,?)", (time.time() * 1000,))


def _count(value):
    return value is None or (type(value) is int and 0 <= value <= MAX_COUNT)


def record_call(connection, run_id, event):
    kind = event.get("event")
    if kind == "agent_limits":
        record_limits(connection, event)
        return
    if kind not in {"inference_started", "inference_finished"}:
        return
    call_id, provider, model = (event.get(key) for key in ("call_id", "provider", "model"))
    if (not isinstance(call_id, str) or not 1 <= len(call_id) <= 128
            or not known_provider(provider)
            or not isinstance(model, str) or not 1 <= len(model) <= 200):
        raise ValueError("Invalid inference usage identity")
    if kind == "inference_started":
        connection.execute("""INSERT INTO usage_calls
            (run_id,call_id,provider,model,started_at) VALUES (?,?,?,?,?)""",
            (run_id, call_id, provider, model, event["timestamp"]))
        return
    usage = event.get("usage", {})
    counts = [usage.get(key) for key in ("input_tokens", "output_tokens", "total_tokens")]
    # Smart's calls say cached_input_tokens; the coding agents' say cache_read_tokens.
    cache = [usage.get("cache_read_tokens", usage.get("cached_input_tokens")), usage.get("cache_write_tokens")]
    cost, equivalent = event.get("cost_nanodollars"), event.get("api_equivalent_nanodollars")
    if not all(_count(n) for n in [*counts, *cache, cost, equivalent]):
        raise ValueError("Invalid inference usage counts")
    billing = event.get("billing")
    if billing is None and event.get("cost_basis") == "plan":
        billing = "plan"
    if billing is not None and billing not in BILLINGS:
        raise ValueError("Invalid inference billing")
    changed = connection.execute("""UPDATE usage_calls SET model=?,finished_at=?,
        input_tokens=?,output_tokens=?,total_tokens=?,cost_nanodollars=?,
        cache_read_tokens=?,cache_write_tokens=?,billing=?,api_equivalent_nanodollars=?
        WHERE run_id=? AND call_id=? AND provider=? AND finished_at IS NULL""",
        (model, event["timestamp"], *counts, cost, *cache, billing, equivalent, run_id, call_id, provider)).rowcount
    if changed != 1:
        raise ValueError("Inference completion must match one pending call")


# What a coding agent says of its plan's usage limits (Claude Code's rate_limit_event): kept per agent, newest only.
LIMIT_STATUSES = frozenset({"allowed", "allowed_warning", "rejected"})


def record_limits(connection, event):
    """Keep the newest limit signal an agent's run reported (ai_plans.cli.limit_signal's shape). Anything malformed
    is dropped: a limit reading is a hint, never a reason to stop a run."""
    provider, limits = event.get("agent"), event.get("limits")
    if provider not in PROVIDERS or not isinstance(limits, dict) or limits.get("status") not in LIMIT_STATUSES:
        return
    data = json.dumps(limits, sort_keys=True)
    if len(data) > 2000:
        return
    connection.execute("INSERT INTO usage_limits VALUES (?,?,?) ON CONFLICT(provider) DO UPDATE SET data=excluded.data,"
                       "at=excluded.at", (provider, data, event["timestamp"]))


def limits(connection):
    """{agent: {...the limit signal, "at": ms}} for every agent that reported one."""
    out = {}
    for provider, data, at in connection.execute("SELECT provider,data,at FROM usage_limits ORDER BY provider"):
        try:
            out[provider] = {**json.loads(data), "at": at}
        except ValueError:
            continue
    return out


def summary(connection, run_id=None):
    where, params = ("WHERE run_id=?", (run_id,)) if run_id else ("", ())
    rows = connection.execute(f"""SELECT provider,model,billing,count(*),
        count(total_tokens),sum(input_tokens),sum(output_tokens),sum(total_tokens),
        sum(cost_nanodollars),count(cost_nanodollars),
        sum(CASE WHEN finished_at IS NULL THEN 1 ELSE 0 END),
        sum(cache_read_tokens),sum(cache_write_tokens),sum(api_equivalent_nanodollars)
        FROM usage_calls {where} GROUP BY provider,model,billing ORDER BY provider,model,billing""", params).fetchall()
    providers = []
    for (provider, model, billing, calls, reported, incoming, outgoing, total, cost, priced, pending, cache_read,
         cache_write, equivalent) in rows:
        providers.append({"provider": provider, "model": model, "billing": billing, "calls": calls,
            "reported_calls": reported, "input_tokens": incoming, "output_tokens": outgoing,
            "total_tokens": total, "estimated_usd": cost / NANODOLLARS if cost is not None else None,
            "priced_calls": priced, "unreported_calls": calls - reported,
            "unpriced_calls": calls - priced, "in_flight_calls": pending,
            "cache_read_tokens": cache_read, "cache_write_tokens": cache_write,
            "api_equivalent_usd": equivalent / NANODOLLARS if equivalent is not None else None})
    totals = {key: sum(p[key] for p in providers) for key in (
        "calls", "reported_calls", "priced_calls", "unreported_calls", "unpriced_calls", "in_flight_calls")}
    for key in ("input_tokens", "output_tokens", "total_tokens", "estimated_usd", "cache_read_tokens",
                "cache_write_tokens", "api_equivalent_usd"):
        known = [p[key] for p in providers if p[key] is not None]
        totals[key] = sum(known) if known else None
    plan = [p for p in providers if p["billing"] == "plan" and p["total_tokens"] is not None]
    totals["plan_tokens"] = sum(p["total_tokens"] for p in plan) if plan else None
    return {"tracked_since": connection.execute("SELECT since FROM usage_tracking WHERE id=1").fetchone()[0],
            "updated_at": time.time() * 1000, "scope": "mobster_workspace",
            "cost_basis": "published_rate", "totals": totals, "providers": providers,
            "limits": limits(connection) if not run_id else {}}


def close_pending(connection, run_id, timestamp):
    # Terminal/recovered runs cannot leave a perpetual 'in flight' counter.
    # Unknown usage remains NULL, including interrupted requests that may bill.
    connection.execute("UPDATE usage_calls SET finished_at=? WHERE run_id=? AND finished_at IS NULL",
                       (timestamp, run_id))


def month_start(now=None):
    """Milliseconds at the start of this calendar month in the Mac's local time zone."""
    # Keep this local and naive so timestamp() uses the offset at the month's start,
    # which can differ from today's offset across a daylight-saving transition.
    now = datetime.fromtimestamp(time.time() if now is None else now)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000


def week_start(now=None):
    """Milliseconds at the start of this week (Monday, 00:00) in the Mac's local time zone."""
    from datetime import timedelta
    local = datetime.fromtimestamp(time.time() if now is None else now)  # naive local time: DST-safe below
    monday = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return monday.timestamp() * 1000


def spend_since(connection, since_ms):
    """Published-rate spend of the calls started at or after ``since_ms`` (unpriced calls add nothing)."""
    cost = connection.execute("SELECT sum(cost_nanodollars) FROM usage_calls WHERE started_at>=?",
                              (since_ms,)).fetchone()[0]
    return (cost or 0) / NANODOLLARS


def plan_since(connection, since_ms):
    """Tokens on the person's plans since ``since_ms`` and what they'd cost at list price: {"tokens", "apiEquivalentUsd"}
    (None where nothing was reported). Included in the plan, so never spend."""
    tokens, equivalent = connection.execute("""SELECT sum(total_tokens),sum(api_equivalent_nanodollars)
        FROM usage_calls WHERE started_at>=? AND billing='plan'""", (since_ms,)).fetchone()
    return {"tokens": tokens, "apiEquivalentUsd": equivalent / NANODOLLARS if equivalent is not None else None}
