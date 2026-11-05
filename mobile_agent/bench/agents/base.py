"""One interface for every agent under test, and one result record for all of them.

An agent receives the task (goal, answer fields, budgets) and a context (the
WDA session, the safety monitor, the absolute deadline). It returns an ``AgentRun``.
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
  approval_denied    the agent stopped at its ask before a commit the task wanted, and the bench declined it
                     (the real-customer benchmark's end for a send or a post)
  error              the agent crashed or a model call failed
"""

import time
from dataclasses import asdict, dataclass, field

STATUSES = ("completed", "abstained", "gave_up", "step_budget", "timeout", "unsafe_stopped",
            "safety_confirmation", "approval_denied", "error")


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
