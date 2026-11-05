"""Stop, stop after this step, pause, a message and the deadline: where a running task honours each."""

import enum
import time

from ..agent_hooks import RESUME_AFTER_SECONDS
from .hooks import BATTERY_PAUSED_FEEDBACK, PAUSED_FEEDBACK, STEER_FEEDBACK, STOPPED_AFTER
from .safety import note_injection


class Interrupt(enum.Enum):
    STOP = "stop"
    STOP_AFTER = "stop_after"
    PAUSE = "pause"
    DEADLINE = "deadline"
    STEER = "steer"


class Where(enum.Enum):
    TURN = "turn"
    ACTION = "action"
    WAITED = "waited"


# The order each boundary looks in. After a wait only Stop counts: an approved action is the step in progress.
HONOURED = {Where.TURN: (Interrupt.STOP, Interrupt.STOP_AFTER, Interrupt.PAUSE, Interrupt.DEADLINE, Interrupt.STEER),
            Where.ACTION: (Interrupt.STOP, Interrupt.STEER, Interrupt.DEADLINE, Interrupt.STOP_AFTER, Interrupt.PAUSE),
            Where.WAITED: (Interrupt.STOP,)}


# "end" with turn.result set, "stop" to decide again on a fresh look, or None to go on.
def check(agent, turn, where):
    for interrupt in HONOURED[where]:
        outcome = RESPONSES[interrupt](agent, turn, where)
        if outcome is not None:
            return outcome
    return None


def interrupted(agent, attempt, turn):
    return check(agent, turn, Where.ACTION)


# DONE and BLOCKED only report, so a deadline, a stop-after or a pause never holds them back.
def _finishing(turn):
    return turn.chunk[turn.index].op in ("DONE", "BLOCKED")


def _chunk_stop(agent, turn, interrupt):
    agent._emit({"event": "frontier_chunk_stop", "step": turn.step, "index": turn.index, "reason": interrupt.value})


def _stop(agent, turn, where):
    if agent._stopping():
        turn.result = dict(turn.stopped)
        return "end"
    return None


def _stop_after(agent, turn, where):
    if where is Where.ACTION and _finishing(turn):
        return None
    if agent._stop_after_asked():
        turn.result = dict(STOPPED_AFTER)
        return "end"
    return None


def _pause(agent, turn, where):
    if where is Where.ACTION and _finishing(turn):
        return None
    if not agent._pause_asked():
        return None
    if where is Where.ACTION:
        _chunk_stop(agent, turn, Interrupt.PAUSE)
        turn.history.append(f"{turn.step}.{turn.index}: paused before {turn.chunk[turn.index].op}; it did not run")
        return "stop"
    # Held at the turn's top: nothing is read or done until Continue, and the screen is read afresh after.
    waited, ended = agent._hold(turn.calls, turn.history)
    turn.started += waited
    agent._waited(waited)
    if ended is not None:
        turn.result = ended
        return "end"
    if waited >= RESUME_AFTER_SECONDS:
        # The phone may have locked while paused: the guard looks first.
        agent._guard_check("resume", turn.history)
    turn.snapshot, turn.unproven = agent._observe(), False
    agent._check_app(turn.snapshot)
    turn.feedback = (BATTERY_PAUSED_FEEDBACK if agent.run_state.battery_pause else PAUSED_FEEDBACK) + (
        (" " + turn.feedback) if turn.feedback else "")
    return None


def _deadline(agent, turn, where):
    if where is Where.ACTION:
        if _finishing(turn) or time.monotonic() - turn.started <= agent.max_seconds:
            return None
        # A decision back after the task's time ran out never acts.
        _chunk_stop(agent, turn, Interrupt.DEADLINE)
        turn.result = {"status": "timeout", "reason": f"{agent.max_seconds} s used",
                       "answer": agent._report_from_notes(turn.request) if turn.session is None else None}
        return "end"
    if time.monotonic() - turn.started > agent.max_seconds:
        turn.result = {"status": "timeout",
                       "answer": agent._report_from_notes(turn.request) if turn.session is None else None}
        return "end"
    return None


def _steer(agent, turn, where):
    if where is Where.ACTION:
        note_injection(agent, turn.snapshot, turn.step)
        if turn.index > 0 and agent.steering is not None and agent.steering.pending():
            # The user wrote while the chain ran: look at the message before the next action.
            _chunk_stop(agent, turn, Interrupt.STEER)
            return "stop"
        return None
    if agent._take_steering(turn.calls, turn.history):
        turn.feedback = STEER_FEEDBACK + ((" " + turn.feedback) if turn.feedback else "")
    return None


RESPONSES = {Interrupt.STOP: _stop, Interrupt.STOP_AFTER: _stop_after, Interrupt.PAUSE: _pause,
             Interrupt.DEADLINE: _deadline, Interrupt.STEER: _steer}
