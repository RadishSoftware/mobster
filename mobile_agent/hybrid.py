"""Hybrid Jev + LLM decision architecture.

System One (Jev / TypeSafe ``jev-latest``) is the ONLY path for operation,
target, WAIT/DONE/BLOCKED, stop_gate, and action_verification. It answers typed
Choice/Noul/Score questions over semantic AX snapshots.

System Two (a small LLM helper) supplies text generation, constrained
extraction, recovery hints, and an optional once-per-task plan. It NEVER
selects taps, targets, or other device mutations.

Escalation: when Jev confidence is below its risk floor, WAIT loops exceed a
bound, blocked Noul is high, or output intent is unclear, issue at most one
LLM recovery/plan call, then return to Jev. Never LLM-chosen taps.

Speculative fan-out: batch many typed questions into one Jev call (cheap)
rather than multiple round-trips. Device mutations still have one owner;
parallelize read-only reasoning only.

Uncalibrated thresholds below fail closed to WAIT/UNCLEAR and are disclosed as
such. No vector database, extra agent layers, or policy training until measured
limits justify them (docs/adaptive-architecture.md).
"""

from dataclasses import dataclass, field
from enum import StrEnum
import math


class Path(StrEnum):
    """Which side of the hybrid owns a call."""
    SYSTEM_ONE = "system_one"  # Jev decision / verification
    SYSTEM_TWO = "system_two"  # helper text / extraction / recovery / plan


class EscalationReason(StrEnum):
    LOW_CONFIDENCE = "low_confidence"
    WAIT_PLATEAU = "wait_plateau"
    BLOCKED_SIGNAL = "blocked_signal"
    UNCLEAR_OUTPUT_INTENT = "unclear_output_intent"
    MULTI_APP_MILESTONE = "multi_app_milestone"


# Jev owns every action-authorizing method. A helper call on these is a bug.
JEV_ONLY_METHODS = frozenset({"decide", "verify_action", "verify_output"})

# Helper modes that are allowed. None of them may authorize a device mutation.
HELPER_MODES = frozenset({"plan", "text", "recovery", "extract", "extract_auto"})

# Names that would imply helper-side action selection. Refuse even if a future
# Helper method appears under these names.
FORBIDDEN_HELPER_MODES = frozenset({
    "decide", "select", "choose", "action", "tap", "target", "operation",
    "verify_action", "verify_output", "stop_gate", "dispatch",
})

# Uncalibrated System One confidence floor shared with task_policy.NAVIGATION.
# Keep them aligned; hybrid escalates below this instead of only demoting.
ESCALATION_CONFIDENCE_FLOOR = .55

# Uncalibrated WAIT plateau bound (matches Agent recovered_waits trigger).
ESCALATION_WAIT_LOOPS = 3

# Uncalibrated blocked Noul that already prefers recovery in the Agent loop.
ESCALATION_BLOCKED_NOUL = .9

# At most one LLM recovery/plan escalation per run, then Jev again.
ESCALATION_MAX_PER_RUN = 1


def inference_path(provider):
    """Map a telemetry provider to a hybrid path. Unknown stays explicit."""
    if provider == "typesafe":
        return Path.SYSTEM_ONE
    if provider in {"google", "helper"}:
        return Path.SYSTEM_TWO
    return None


def assert_helper_mode(mode):
    """Hard invariant: the helper never performs decision-class work."""
    if not isinstance(mode, str) or mode in FORBIDDEN_HELPER_MODES or mode not in HELPER_MODES:
        raise ValueError("Helper is not authorized for action selection or decision-class work")


def assert_jev_method(name):
    """Hard invariant: decision-class methods exist only on System One."""
    if name not in JEV_ONLY_METHODS:
        raise ValueError("Unknown System One decision method")
    return name


def fanout_stats(questions):
    """Count typed questions batched into one Jev request.

    Prefer one call with many Choice/Noul/Score questions over multiple
    round-trips. Returns a stable, non-content metric shape for run reports.
    """
    if not isinstance(questions, dict) or not questions:
        raise ValueError("Fan-out measurement requires a nonempty question map")
    by_type = {"choice": 0, "noul": 0, "score": 0}
    for name, question in questions.items():
        if not isinstance(name, str) or not isinstance(question, dict):
            raise ValueError("Invalid speculative question entry")
        kind = question.get("type")
        if kind not in by_type:
            raise ValueError("Unsupported question type for fan-out")
        by_type[kind] += 1
    return {"questions": len(questions), "by_type": by_type,
            "round_trips": 1, "speculative_optional": sorted(
                name for name, question in questions.items()
                if name in {"screen_has_loading", "side_effect_risk"})}


def escalation_reason(*, confidence=None, demoted_from=None, operation=None,
                      blocked_probability=None, output_intent=None,
                      wait_loops=0, escalations_used=0, multi_app_hint=False):
    """Return the single allowed recovery/plan trigger, or None.

    Order is deliberate: low confidence first (an unsafe action must not be
    retried), then blocked, then WAIT plateau, then unclear output intent.
    ``multi_app_hint`` is an operator/plan signal only; it never expands user
    authority and never chooses a tap.
    """
    if type(escalations_used) is not int or escalations_used < 0 or type(wait_loops) is not int or wait_loops < 0:
        raise ValueError("Escalation counters must be nonnegative integers")
    if escalations_used >= ESCALATION_MAX_PER_RUN:
        return None
    low = demoted_from is not None or (
        confidence is not None and confidence < ESCALATION_CONFIDENCE_FLOOR)
    if low:
        return EscalationReason.LOW_CONFIDENCE
    if operation == "BLOCKED" or (
            blocked_probability is not None and blocked_probability >= ESCALATION_BLOCKED_NOUL):
        return EscalationReason.BLOCKED_SIGNAL
    if wait_loops >= ESCALATION_WAIT_LOOPS:
        return EscalationReason.WAIT_PLATEAU
    if output_intent is not None and getattr(output_intent, "value", output_intent) == "unclear":
        return EscalationReason.UNCLEAR_OUTPUT_INTENT
    if multi_app_hint:
        return EscalationReason.MULTI_APP_MILESTONE
    return None


def escalation_mode(reason):
    """Recovery hint is the default; multi-app asks for a one-shot plan."""
    if not isinstance(reason, EscalationReason):
        raise ValueError("Escalation requires a typed reason")
    return "plan" if reason is EscalationReason.MULTI_APP_MILESTONE else "recovery"


@dataclass
class HybridRouter:
    """Explicit System One / System Two facade with budget isolation.

    Decision methods delegate only to ``model`` (Jev). Helper access is
    mode-checked and never used for decide/verify. Escalation spends at most
    one helper call and then returns control to Jev.
    """
    model: object
    helper: object | None = None
    max_helper_calls: int = 4
    reserved_extraction: int = 0
    jev_calls: int = 0
    helper_calls: int = 0
    escalations: int = 0
    fanouts: list = field(default_factory=list)

    def __post_init__(self):
        if (type(self.max_helper_calls) is not int or self.max_helper_calls < 0
                or type(self.reserved_extraction) not in (int, bool)
                or not 0 <= int(self.reserved_extraction) <= 1):
            raise ValueError("Invalid hybrid budgets")

    def decide(self, *args, **kwargs):
        assert_jev_method("decide")
        self.jev_calls += 1
        return self.model.decide(*args, **kwargs)

    def verify_action(self, *args, **kwargs):
        assert_jev_method("verify_action")
        verifier = getattr(self.model, "verify_action", None)
        if not callable(verifier):
            raise ValueError("System One action verification is required")
        self.jev_calls += 1
        return verifier(*args, **kwargs)

    def verify_output(self, *args, **kwargs):
        assert_jev_method("verify_output")
        verifier = getattr(self.model, "verify_output", None)
        if not callable(verifier):
            raise ValueError("System One output verification is required")
        self.jev_calls += 1
        return verifier(*args, **kwargs)

    def helper_ask(self, mode, *args, **kwargs):
        assert_helper_mode(mode)
        if self.helper is None:
            raise ValueError("Helper is unavailable")
        ask = getattr(self.helper, "ask", None)
        if not callable(ask):
            raise ValueError("Helper cannot answer this mode")
        self.helper_calls += 1
        return ask(mode, *args, **kwargs)

    def helper_extract(self, *args, automatic=False, **kwargs):
        assert_helper_mode("extract_auto" if automatic else "extract")
        if self.helper is None:
            raise ValueError("Helper is unavailable")
        method = getattr(self.helper, "extract_auto" if automatic else "extract", None)
        if not callable(method):
            raise ValueError("Helper cannot extract")
        self.helper_calls += 1
        return method(*args, **kwargs)

    def navigation_budget(self):
        """Helper calls available for navigation (text/recovery/plan).

        Extraction is optionally reserved so a requested answer is not starved
        by earlier recovery hints. Jev has its own call path and is not capped
        by this helper budget. Spent calls match the live helper counter when
        present so budget isolation matches the Agent loop.
        """
        spent = self.helper_calls
        if self.helper is not None:
            spent = max(spent, getattr(self.helper, "calls", 0))
        return max(0, self.max_helper_calls - int(self.reserved_extraction) - spent)

    def escalate(self, *, snapshot, goal, history, reason, **kwargs):
        """One System Two recovery/plan call under a typed reason, then Jev."""
        if not isinstance(reason, EscalationReason):
            raise ValueError("Escalation requires a typed reason")
        if self.escalations >= ESCALATION_MAX_PER_RUN or self.navigation_budget() <= 0:
            return None
        mode = escalation_mode(reason)
        hint = self.helper_ask(mode, snapshot, goal, history, **kwargs)
        self.escalations += 1
        return hint

    def record_fanout(self, questions):
        stats = fanout_stats(questions)
        self.fanouts.append(stats)
        return stats


def hybrid_totals(events):
    """Sum provider telemetry into System One vs System Two totals.

    ``events`` are inference_started/finished rows (or any dict with provider,
    latency_ms, usage, cost_nanodollars). Missing usage stays missing: totals
    are None when no finished call reported a field, never synthesized zero.
    """
    def empty():
        return {"calls": 0, "finished": 0, "failed": 0, "latencies": [],
                "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                "cost_nanodollars": 0, "reported_usage": 0, "priced": 0}

    paths = {Path.SYSTEM_ONE: empty(), Path.SYSTEM_TWO: empty()}
    started, finished = set(), set()
    for event in events or ():
        if not isinstance(event, dict):
            raise ValueError("Inference events must be objects")
        name = event.get("event")
        if name not in {"inference_started", "inference_finished"}:
            continue
        path = inference_path(event.get("provider"))
        if path is None:
            continue
        bucket = paths[path]
        call_id = event.get("call_id")
        if name == "inference_started":
            if call_id in started:
                continue
            started.add(call_id)
            bucket["calls"] += 1
            continue
        if call_id in finished:
            continue
        finished.add(call_id)
        bucket["finished"] += 1
        if event.get("success") is False:
            bucket["failed"] += 1
        latency = event.get("latency_ms")
        if type(latency) in (int, float) and latency >= 0:
            bucket["latencies"].append(float(latency))
        usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
        if usage:
            bucket["reported_usage"] += 1
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            value = usage.get(key)
            if type(value) is int and value >= 0:
                bucket[key] += value
        cost = event.get("cost_nanodollars")
        if type(cost) is int and cost >= 0:
            bucket["cost_nanodollars"] += cost
            bucket["priced"] += 1

    def finish(bucket):
        latencies = sorted(bucket.pop("latencies"))
        out = {
            "calls": bucket["calls"], "finished": bucket["finished"], "failed": bucket["failed"],
            "input_tokens": bucket["input_tokens"] if bucket["reported_usage"] else None,
            "output_tokens": bucket["output_tokens"] if bucket["reported_usage"] else None,
            "total_tokens": bucket["total_tokens"] if bucket["reported_usage"] else None,
            "cost_nanodollars": bucket["cost_nanodollars"] if bucket["priced"] else None,
            "estimated_usd": (bucket["cost_nanodollars"] / 1_000_000_000) if bucket["priced"] else None,
            "priced_calls": bucket["priced"], "unpriced_calls": bucket["finished"] - bucket["priced"],
        }
        if latencies:
            mid = len(latencies) // 2
            median = latencies[mid] if len(latencies) % 2 else (latencies[mid - 1] + latencies[mid]) / 2
            out["latency_ms"] = {"n": len(latencies), "median_ms": round(median, 2),
                                 "p95_ms": round(latencies[math.ceil(.95 * len(latencies)) - 1], 2),
                                 "min_ms": round(latencies[0], 2), "max_ms": round(latencies[-1], 2)}
        else:
            out["latency_ms"] = None
        return out

    return {Path.SYSTEM_ONE: finish(paths[Path.SYSTEM_ONE]),
            Path.SYSTEM_TWO: finish(paths[Path.SYSTEM_TWO])}
