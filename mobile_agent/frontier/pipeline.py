"""One action of the chosen chain, run through the stages in order until one decides."""

import dataclasses

from ..contract_screen import Gate
from ..state import Element, Snapshot
from ..wda.point_gestures import PointGesture
from .actions import Action
from .interrupts import interrupted
from .stages.after import chain, record, settle
from .stages.approve import approval
from .stages.controls import shape, sideways_swipe, target
from .stages.ends import active, done_deferred, done_gate, finish, input_not_taken, wait
from .stages.gate import account, ask_at_send, contract_gate
from .stages.guards import loops, refresh, requested, results_still_moving, unknown_outcome
from .stages.points import gesture_points, keypad, point_approval, skill
from .stages.send import execute, failure


# One action of a chain, and what the stages have learned about it on its way through them.
@dataclasses.dataclass(slots=True)
class Attempt:
    act: Action
    op: str
    text: str | None
    app: str | None
    element: Element | None = None
    point: tuple[float, float] | PointGesture | None = None
    held: tuple | None = None
    unknown_repeat: str | None = None
    key: tuple = ()
    spot: tuple = ()
    gate: Gate | None = None
    commit_now: bool = False
    before: Snapshot | None = None
    effect: tuple | None = None
    before_unknown: dict | None = None
    t_exec: float = 0.0
    refresh_ms: int = 0
    exec_ms: int = 0
    settle_ms: int = 0
    changed: bool = False
    error: Exception | None = None


# A stage returns "next" to go on with the chunk, "stop" to look again, "end" with turn.result set, or None to
# pass the action on. DONE, BLOCKED and WAIT end in a stage of their own, so the stages that act never see them.
STAGES = (interrupted, input_not_taken, active, keypad, skill, gesture_points, point_approval, target,
          sideways_swipe, shape, unknown_outcome, loops, requested, refresh, results_still_moving, contract_gate,
          ask_at_send, account, approval, execute, failure, settle, record, chain)
ROUTES = {"DONE": (interrupted, input_not_taken, done_gate, done_deferred, finish),
          "BLOCKED": (interrupted, input_not_taken, finish),
          "WAIT": (interrupted, input_not_taken, wait)}


def dispatch(agent, act, turn):
    attempt = Attempt(act, act.op, act.text, act.app)
    for stage in ROUTES.get(act.op, STAGES):
        flow = stage(agent, attempt, turn)
        if flow is not None:
            return flow
