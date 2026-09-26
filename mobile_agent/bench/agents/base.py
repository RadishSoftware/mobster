"""One interface for every agent under test, and one result record for all of them.

An agent receives the task (goal, answer fields, budgets) and a context (the
phone, the safety monitor, the absolute deadline). It returns an ``AgentRun``.
The runner owns everything around it (health check, reset, timing, grading),
so no agent can grade itself or skip the reset.

Status vocabulary (identical for every agent):
  completed          the agent says it finished (answer, if any, in ``answer``)
  abstained          the agent explicitly said the requested fact is not available
  gave_up            the agent stopped without claiming success (blocked, no progress)
  step_budget        the task's step budget ran out
  timeout            the task's time budget ran out
  unsafe_stopped     the safety monitor refused an action; the run was stopped
  safety_confirmation  the model's own safety layer asked for human confirmation (never granted)
  error              the agent crashed or a model call failed
"""

from dataclasses import asdict, dataclass, field
import json
import re
import time

STATUSES = ("completed", "abstained", "gave_up", "step_budget", "timeout", "unsafe_stopped",
            "safety_confirmation", "error")


@dataclass
class AgentRun:
    status: str = "error"
    answer: object = None
    abstained: bool = False
    reason: str = ""
    wall_ms: float = 0.0              # agent start -> result, measured by the runner
    agent_ms: float = 0.0             # wall_ms minus harness-only monitor reads
    first_action_ms: float | None = None
    decision_steps: int = 0           # model decision cycles
    action_count: int = 0             # actions dispatched to the phone
    actions: list = field(default_factory=list)
    step_ms: list = field(default_factory=list)
    model_calls: int = 0
    model_ms: float = 0.0
    cost_usd: float | None = 0.0
    unpriced_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    unsafe: list = field(default_factory=list)
    unintended: list = field(default_factory=list)
    monitor_ms: float = 0.0
    config: dict = field(default_factory=dict)
    detail: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


@dataclass
class Context:
    phone: object              # phone.Phone (baselines) — Mobster builds its own driver on the same session
    wda_url: str
    monitor: object            # safety.Monitor
    deadline: float            # time.monotonic() deadline
    started: float = field(default_factory=time.monotonic)
    session: str = ""
    artifacts: str = ""          # a directory for this attempt's files (evidence for offline replay), or ""

    def remaining(self):
        return self.deadline - time.monotonic()

    def elapsed_ms(self):
        return (time.monotonic() - self.started) * 1000


class Recorder:
    """Accumulates model calls and actions into an AgentRun, uniformly across agents."""

    def __init__(self, ctx, run=None):
        self.ctx = ctx
        self.run = run or AgentRun()
        self._last_step = ctx.started

    def model_call(self, info):
        run = self.run
        throttle = info.get("throttle_ms") or 0
        if throttle:
            # Provider throttling is infrastructure: give the time back to the agent's budget.
            run.detail["throttle_ms"] = round(run.detail.get("throttle_ms", 0) + throttle, 1)
            run.detail["throttled_calls"] = run.detail.get("throttled_calls", 0) + 1
            self.ctx.deadline += throttle / 1000
        run.model_calls += 1
        run.model_ms += info.get("latency_ms") or 0
        cost = info.get("cost_usd")
        if cost is None:
            run.unpriced_calls += 1
        elif run.cost_usd is not None:
            run.cost_usd += cost
        run.tokens_in += info.get("prompt_tokens") or 0
        run.tokens_out += info.get("output_tokens") or 0

    def step(self):
        now = time.monotonic()
        self.run.decision_steps += 1
        self.run.step_ms.append(round((now - self._last_step) * 1000, 1))
        self._last_step = now

    def action(self, op, **detail):
        run = self.run
        t = round(self.ctx.elapsed_ms(), 1)
        if run.first_action_ms is None:
            run.first_action_ms = t
        run.action_count += 1
        run.actions.append({"t_ms": t, "op": op, **{k: v for k, v in detail.items() if v not in (None, "")}})


ANSWER_MARKER = re.compile(r"ANSWER_JSON\s*:\s*", re.I)


def extract_json_object(text):
    """The last balanced {...} in text (after ANSWER_JSON: when present), parsed; None if none parses."""
    if not isinstance(text, str):
        return None
    marker = list(ANSWER_MARKER.finditer(text))
    if marker:
        text = text[marker[-1].end():]
    starts = [i for i, c in enumerate(text) if c == "{"]
    for start in (starts[:1] if marker else reversed(starts)):
        depth, in_string, escape = 0, False, False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                escape = (char == "\\" and not escape)
                if char == '"' and not escape:
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start:index + 1])
                    except ValueError:
                        break
                    return value if isinstance(value, dict) else None
    return None


def final_from_object(obj, task):
    """Map a baseline's final {"status": ..., "answer": ...} object onto (status, answer, abstained)."""
    if not isinstance(obj, dict):
        return "gave_up", None, False
    status = str(obj.get("status", "")).strip().casefold()
    answer = obj.get("answer")
    if status in ("infeasible", "not_found", "not found", "unavailable", "cannot_tell", "abstain"):
        return "abstained", None, True
    if status in ("done", "complete", "completed", "success"):
        if task.answer and not isinstance(answer, dict):
            # A bare value for a one-field answer is accepted as that field.
            if len(task.answer) == 1 and answer is not None:
                answer = {task.answer[0][0]: answer}
            else:
                answer = None
        return "completed", answer if task.answer else None, False
    return "gave_up", None, False


def answer_instructions(task):
    """Plain-language answer contract given to every baseline (Mobster gets the same fields as a schema)."""
    if task.answer:
        fields = ", ".join(f'"{name}": {"a list of strings" if kind == "string_list" else "a list of {file, label} objects" if kind == "labels" else "a string"}'
                           for name, kind in task.answer)
        answer = f'"answer": {{{fields}}}'
    else:
        answer = '"answer": null'
    return (f'When the task is finished, reply with status "done" and {answer}. If the requested information '
            f'is not available on the device or page, reply with status "infeasible" and "answer": null instead '
            f'of guessing.')
