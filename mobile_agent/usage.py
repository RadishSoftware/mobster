"""Durable workspace usage independent of the bounded task history.

Rows contain only call identity, provider, numeric usage and price snapshots.
The caller writes them in the SAME transaction as the corresponding run event.
Historical uninstrumented calls and external account usage are not invented.
"""

import time

from .costs import NANODOLLARS


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
    """)
    connection.execute("INSERT OR IGNORE INTO usage_tracking VALUES (1,?)", (time.time() * 1000,))


def record_call(connection, run_id, event):
    kind = event.get("event")
    if kind not in {"inference_started", "inference_finished"}:
        return
    call_id, provider, model = (event.get(key) for key in ("call_id", "provider", "model"))
    if (not isinstance(call_id, str) or not 1 <= len(call_id) <= 128
            or provider not in {"typesafe", "google", "helper"}
            or not isinstance(model, str) or not 1 <= len(model) <= 200):
        raise ValueError("Invalid inference usage identity")
    if kind == "inference_started":
        connection.execute("""INSERT INTO usage_calls
            (run_id,call_id,provider,model,started_at) VALUES (?,?,?,?,?)""",
            (run_id, call_id, provider, model, event["timestamp"]))
        return
    usage = event.get("usage", {})
    counts = [usage.get(key) for key in ("input_tokens", "output_tokens", "total_tokens")]
    cost = event.get("cost_nanodollars")
    if any(n is not None and (type(n) is not int or not 0 <= n <= 9_007_199_254_740_991)
           for n in [*counts, cost]):
        raise ValueError("Invalid inference usage counts")
    changed = connection.execute("""UPDATE usage_calls SET model=?,finished_at=?,
        input_tokens=?,output_tokens=?,total_tokens=?,cost_nanodollars=?
        WHERE run_id=? AND call_id=? AND provider=? AND finished_at IS NULL""",
        (model, event["timestamp"], *counts, cost, run_id, call_id, provider)).rowcount
    if changed != 1:
        raise ValueError("Inference completion must match one pending call")


def summary(connection, run_id=None):
    where, params = ("WHERE run_id=?", (run_id,)) if run_id else ("", ())
    rows = connection.execute(f"""SELECT provider,model,count(*),
        count(total_tokens),sum(input_tokens),sum(output_tokens),sum(total_tokens),
        sum(cost_nanodollars),count(cost_nanodollars),
        sum(CASE WHEN finished_at IS NULL THEN 1 ELSE 0 END)
        FROM usage_calls {where} GROUP BY provider,model ORDER BY provider,model""", params).fetchall()
    providers = []
    for provider, model, calls, reported, incoming, outgoing, total, cost, priced, pending in rows:
        providers.append({"provider": provider, "model": model, "calls": calls,
            "reported_calls": reported, "input_tokens": incoming, "output_tokens": outgoing,
            "total_tokens": total, "estimated_usd": cost / NANODOLLARS if cost is not None else None,
            "priced_calls": priced, "unreported_calls": calls - reported,
            "unpriced_calls": calls - priced, "in_flight_calls": pending})
    totals = {key: sum(p[key] for p in providers) for key in (
        "calls", "reported_calls", "priced_calls", "unreported_calls", "unpriced_calls", "in_flight_calls")}
    for key in ("input_tokens", "output_tokens", "total_tokens", "estimated_usd"):
        known = [p[key] for p in providers if p[key] is not None]
        totals[key] = sum(known) if known else None
    return {"tracked_since": connection.execute("SELECT since FROM usage_tracking WHERE id=1").fetchone()[0],
            "updated_at": time.time() * 1000, "scope": "mobster_workspace",
            "cost_basis": "published_rate", "totals": totals, "providers": providers}


def close_pending(connection, run_id, timestamp):
    # Terminal/recovered runs cannot leave a perpetual 'in flight' counter.
    # Unknown usage remains NULL, including interrupted requests that may bill.
    connection.execute("UPDATE usage_calls SET finished_at=? WHERE run_id=? AND finished_at IS NULL",
                       (timestamp, run_id))
