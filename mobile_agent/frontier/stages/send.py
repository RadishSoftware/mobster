"""The stages that send an action to the phone and answer its failure."""

import time

from ... import contract_asks as proof_asks
from ...model_clients import http_detail
from ...wda.guards import DriverRejection
from ...wda.point_gestures import GESTURE_OPS
from ..interrupts import Where, check
from ..protocol import ELEMENT_OPS, TYPED_OPS
from ..safety import (
    CONNECTION_LOST,
    NOTHING_SENT,
    SAME_FAILURES_TO_CHECK,
    SAME_FAILURES_TO_STOP,
    UNKNOWN_REASON,
    GuardStop,
    consequential,
    failure_feedback,
    outcome_unknown,
    stuck_sentence,
    unknown_advice,
)
from ..screen import names_act, reads_back


def execute(agent, attempt, turn):
    index = turn.index
    op, text, app, point, held, unknown_repeat, gate = (
        attempt.op, attempt.text, attempt.app, attempt.point, attempt.held, attempt.unknown_repeat, attempt.gate)
    attempt.before = turn.snapshot
    attempt.effect = agent._effect(op, attempt.element, app, text, agent._app_name(turn.snapshot), turn.snapshot)
    attempt.before_unknown = (
        agent._before_unknown(attempt.element, turn.snapshot) if attempt.effect is not None else None)
    if unknown_repeat == "retry":
        # A fresh read showed the earlier attempt did nothing: this is its one more attempt.
        agent.run_state.effects.settle(held[0], None, effect=held[1], happened=False)
        agent._emit({"event": "frontier_retry", "step": turn.step, "index": index, "operation": op,
                    "reason": "outcome_unknown_unchanged"})
    attempt.t_exec = time.monotonic()
    agent.run_state.last_receipt = None
    if turn.session is not None:
        turn.session.mark_action()
    if op in GESTURE_OPS:
        # Journaled before it is sent: after a restart the gesture counts as one that may have run.
        if check(agent, turn, Where.WAITED) is not None:
            return "end"
        agent._emit({"event": "action_started", "operation": op, "target": app})
        if check(agent, turn, Where.WAITED) is not None:
            return "end"
    try:
        try:
            attempt.before = agent._execute(op, attempt.element, app, text, turn.snapshot, point)
        except Exception as error:
            # Resent only when the request never ran (refused, a dropped session); never a gesture or a point.
            if op in GESTURE_OPS or op == "TAP_XY" or not agent._recover(error) or not any(
                    mark in str(error) for mark in NOTHING_SENT):
                raise
            turn.snapshot = agent._observe()
            if attempt.element is not None:
                attempt.element = agent._find(
                    turn.snapshot, attempt.element.label, attempt.element.role, attempt.element.locator)
                if attempt.element is None:
                    raise
            attempt.before = agent._execute(op, attempt.element, app, text, turn.snapshot, point)
        if op in GESTURE_OPS:
            agent._emit({"event": "action_acknowledged", "step": turn.step, "operation": op})
        turn.actions += 1
        agent._actions = turn.actions
        if attempt.commit_now:
            agent.run_state.commits += 1
        if unknown_repeat is not None:
            agent.run_state.effects.resolve(held[0], None, effect=held[1])
        turn.acted_since = turn.acted_since or names_act(op, attempt.element, text)
        if attempt.element is not None:
            turn.touched.append(attempt.element)
        if op == "READ_LIST" and agent.run_state.listing:
            turn.read_rows = list(agent.run_state.listing)
        agent.run_state.submitted_at = time.monotonic() if op in ("TYPE_SUBMIT", "SUBMIT") else (
            agent.run_state.submitted_at if op not in ("TAP", "LONG_PRESS", "TAP_XY") else None)
        if turn.session is not None:
            # Opening the app already in front is no progress (P1-1).
            turn.session.on_acted(noop=op == "LAUNCH_APP" and app == getattr(turn.snapshot, "bundle_id", None))
        if agent._contract is not None:
            where = agent._app_name(attempt.before)
            agent._contract.record_action(where, op, getattr(attempt.element, "label", "") or "")
            if op in TYPED_OPS and text:
                agent._contract.record_typed(where, attempt.element, text, turn.step)
                turn.chain_typed.append(text)
            if gate is not None and gate.item is not None:
                agent._contract.committed(gate, attempt.element, attempt.before, where, turn.step)
            if op == "READ_LIST" and agent.run_state.listing:
                agent._contract.saw_lines(where, turn.step, agent.run_state.listing)
        turn.acted = turn.acted or op in ELEMENT_OPS or op == "TAP_XY" or op in GESTURE_OPS
        if agent.run_state.macro_feedback:
            turn.feedback, agent.run_state.macro_feedback = agent.run_state.macro_feedback, None
    except GuardStop:
        raise
    except Exception as error:
        attempt.error = error


def failure(agent, attempt, turn):
    error = attempt.error
    if error is None:
        return None
    index = turn.index
    op, text, app, element, gate, before_unknown, effect = (
        attempt.op, attempt.text, attempt.app, attempt.element, attempt.gate, attempt.before_unknown, attempt.effect)
    if op in GESTURE_OPS and not (isinstance(error, DriverRejection) and error.code in error.PRE_DISPATCH):
        kind = GESTURE_OPS[op]
        turn.result = {"status": "blocked", "answer": None, "code": "unknown_outcome",
                        "reason": f"The {kind} outcome is unknown. Inspect the phone before starting another "
                                  "task; it was not retried."}
        agent._emit({"event": "frontier_failed", "step": turn.step, "index": index, "operation": op,
                    "error": f"{kind} outcome unknown", "outcome": "unknown"})
        return "end"
    # A request on a dropped session never ran: its outcome is known.
    lost = agent.run_state.recoveries and any(name in str(error) for name in CONNECTION_LOST if name != "StaleSession")
    # A tap at a point has no control to read back: any failure past dispatch is held as unknown.
    unknown = bool(lost) or outcome_unknown(error, op) or (
        op == "TAP_XY" and not (isinstance(error, DriverRejection) and error.code in error.PRE_DISPATCH))
    if unknown and effect is not None:
        agent.run_state.effects.record_outcome(effect[0], None, outcome="unknown", effect=effect[1],
                                     context=before_unknown)
    if unknown and gate is not None and gate.item is not None and proof_asks.asks(gate.item):
        # Any control of this act, on any screen, now asks with the warning, in every mode.
        agent.run_state.held_items[gate.item.id] = UNKNOWN_REASON
    turn.feedback = (("The connection to the phone dropped during this action and was restored: it may or "
                 "may not have happened. " + unknown_advice(op)) if lost
                else failure_feedback(op, element, text, error))
    detail = http_detail(error)
    what = element.label[:60] if element else (app or "")
    # The line names the target and the status: a bare one repeated a failed launch fifty times (B2).
    turn.history.append(f"{turn.step}.{index}: {op} {what!r} failed ({detail})")
    agent._spend("exec", attempt.t_exec)
    agent._emit({"event": "frontier_failed", "step": turn.step, "index": index, "operation": op,
                "error": f"{type(error).__name__}: {str(error)[:160]}",
                "outcome": "unknown" if unknown else "failed",
                # A picked-up run holds this act too (checkpoints.held_acts).
                **({"held_act": gate.item.act} if unknown and gate is not None and gate.item is not None
                   and proof_asks.asks(gate.item) else {})})
    t_observe = time.monotonic()
    turn.snapshot, turn.unproven = agent._observe(), False
    agent._spend("observe", t_observe)
    if unknown and effect is not None and element is not None:
        # The look after it: a screen that moved on, or a control that changed, says it happened.
        if not agent._unchanged(before_unknown, element, turn.snapshot)[0]:
            agent.run_state.effects.settle(effect[0], None, effect=effect[1], happened=True)
            turn.feedback += (" The screen changed after it, so Mobster counts it as done and won't send it "
                         "again.")
        elif consequential(op, element, turn.snapshot):
            turn.feedback += " The screen looks unchanged; sending it again needs the user's OK."
        else:
            turn.feedback += (" The screen looks unchanged, so it probably didn't happen: if it still looks "
                         "the same, you may send it once more.")
    if op in TYPED_OPS and text and element is not None and agent._contract is not None:
        # Typing that timed out may have landed: a field reading it back holds the agent's text.
        field = agent._find(turn.snapshot, element.label, element.role, element.locator)
        if field is not None and field.editable and reads_back(field.value or "", text):
            agent._contract.record_typed(agent._app_name(turn.snapshot), field, text, turn.step)
            turn.feedback += " The field now reads back exactly the text you wrote: don't type it again."
    same = (op, (element.locator or element.label) if element else app, text)
    turn.failures[same] = turn.failures.get(same, 0) + 1
    if op == "LAUNCH_APP" and detail == "HTTP 400" or turn.failures[same] >= SAME_FAILURES_TO_CHECK:
        # iOS refuses to bring an app forward over the lock screen or a passcode sheet.
        verdict = agent._guard_check("launch_failed" if op == "LAUNCH_APP" else "lock_suspected",
                                    turn.history)
        if verdict is not None and verdict.state == "unlocked":
            turn.failures.clear()
            turn.snapshot = agent._observe()
    if turn.failures.get(same, 0) >= SAME_FAILURES_TO_STOP:
        name = agent.apps.get(app, app) if op == "LAUNCH_APP" else what
        sentence = stuck_sentence(op, name, detail, turn.actions)
        turn.result = {"status": "blocked", "answer": None, "reason": sentence}
        return "end"
    return "stop"
