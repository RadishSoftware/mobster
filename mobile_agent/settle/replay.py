"""Offline replay of recorded settles: what predictive settle would have done on frameclock.jsonl logs.

    python -m mobile_agent.settle.replay LOG [LOG ...]

Reads ``settle`` rows (``MOBSTER_FRAME_CLOCK_LOG`` files) in time order, learns each key's arrival the way a run does,
and for every settle that polled (path ax, frame or frame_fallback) asks whether the learned p90 would have been
enough. A row records no arrival unless it was logged with the switch on (``arrival_ms``), so the replay estimates it:
``stable_ms`` when the row has it, else ``settle_ms`` minus the polling loop's quiet period (0.5 s on AX alone, 0.3 s
with the pixels). The wall time of a hit is the wait plus one read; of a miss, the wait, one read and then the
whole polled settle (an upper bound). These are projections from recordings, not a measured effect of the switch.
"""

import json
import sys
import tempfile
from pathlib import Path

from .store import SettleStore, shape_of

POLLED = {"ax", "frame", "frame_fallback"}
QUIET_AX_MS, QUIET_PIXELS_MS, READ_MS = 500.0, 300.0, 200.0


def estimate_arrival(row):
    if row.get("arrival_ms") is not None:
        return float(row["arrival_ms"])
    if row.get("stable_ms") is not None:
        return float(row["stable_ms"])
    quiet = QUIET_PIXELS_MS if row.get("path") == "frame" else QUIET_AX_MS
    return max(0.0, float(row["settle_ms"]) - quiet)


def load(paths):
    rows = []
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("kind") == "settle" and row.get("changed") and "settle_ms" in row:
                    rows.append(row)
    return sorted(rows, key=lambda row: row.get("at", 0))


def replay(rows):
    with tempfile.TemporaryDirectory() as folder:
        store = SettleStore(Path(folder) / "durations.json")
        totals = {"settles": 0, "predicted": 0, "hits": 0, "misses": 0, "polled_ms": 0.0, "with_ms": 0.0,
                  "polled_reads": 0, "with_reads": 0, "no_prediction": 0}
        for row in rows:
            if row.get("path") not in POLLED:
                continue
            app, shape = row.get("bundle") or "", shape_of(row.get("op"))
            arrival, wall, reads = estimate_arrival(row), float(row["settle_ms"]), int(row.get("reads", 1))
            totals["settles"] += 1
            totals["polled_ms"] += wall
            totals["polled_reads"] += reads
            wait = store.p90(app, shape)
            if wait is None:
                totals["no_prediction"] += 1
                totals["with_ms"], totals["with_reads"] = totals["with_ms"] + wall, totals["with_reads"] + reads
                store.add(app, shape, arrival)
            elif wait >= arrival:
                totals["predicted"] += 1
                totals["hits"] += 1
                totals["with_ms"] += wait + READ_MS
                totals["with_reads"] += 1
            else:
                totals["predicted"] += 1
                totals["misses"] += 1
                totals["with_ms"] += wait + READ_MS + wall
                totals["with_reads"] += 1 + reads
                store.add(app, shape, arrival)
    return totals


def report(totals):
    n = totals["settles"] or 1
    lines = [f"{totals['settles']} polled settles; {totals['no_prediction']} had no prediction yet, "
             f"{totals['hits']} hits, {totals['misses']} misses ({totals['predicted']} predicted)",
             f"step settle wall: {totals['polled_ms'] / n:.0f} ms polled -> {totals['with_ms'] / n:.0f} ms predicted "
             f"(mean per settle)",
             f"AX reads per settle: {totals['polled_reads'] / n:.2f} -> {totals['with_reads'] / n:.2f}"]
    if totals["predicted"]:
        lines.append(f"hit rate when predicted: {totals['hits'] / totals['predicted']:.0%}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(report(replay(load(sys.argv[1:]))))
