"""The stage that decides who asks for an action, and what follows the answer."""

from ... import contract_asks as proof_asks
from ...agent_hooks import RESUME_AFTER_SECONDS
from ..interrupts import Where, check
from ..protocol import ELEMENT_OPS
from ..safety import (
    CAP_REASON,
    CHANGED_WHILE_ASKING,
    INJECTION_REASON,
    MAX_COMMITS_PER_RUN,
    PENDING_REASON,
    SAME_REFUSALS_TO_STOP,
    UNKNOWN_REASON,
    _nobody,
    binding,
    declined_result,
    refused_sentence,
    same_binding,
)


def approval(agent, attempt, turn):
    index = turn.index
    op, text, unknown_repeat, gate = attempt.op, attempt.text, attempt.unknown_repeat, attempt.gate
    asker, force = agent.approve, False
    agent.run_state.ask_reason = None
    attempt.commit_now = agent._is_commit(gate, op, attempt.element, turn.snapshot, text)
    held_reason = (agent.run_state.held_items.get(gate.item.id) if gate is not None and gate.item is not None
                   and gate.item.status != "closed" else None)
    if (held_reason is None and gate is not None and gate.item is not None and gate.item.status != "closed"
            and gate.item.act in agent.run_state.held_acts and proof_asks.asks(gate.item)):
        held_reason = UNKNOWN_REASON  # an earlier run of this task may have done it already
    extra = None
    if held_reason is not None and unknown_repeat is None:
        if (agent.approve or agent.forced_approve or agent.floor_approve) is None and agent.permissions is None:
            if held_reason == UNKNOWN_REASON:
                turn.feedback = (f"Refused: an earlier {gate.item.act} for this request has an unknown outcome: "
                            "it may have happened. Doing it again needs the user's OK, and nobody can "
                            "give it here.")
                turn.history.append(f"{turn.step}.{index}: {op} {attempt.element.label[:60]!r} refused "
                                    "(an earlier attempt's outcome is unknown)")
                agent._emit({"event": "frontier_refused", "step": turn.step, "label": "outcome_unknown"})
                return "stop"
        else:
            extra = held_reason
    elif (attempt.commit_now or agent._out_of_scope(op, attempt.element, turn.request)) and agent.run_state.injected:
        # A screen this run tried to steer Mobster: every commit and every change no request named asks.
        extra = INJECTION_REASON
    elif attempt.commit_now and agent.run_state.commits >= MAX_COMMITS_PER_RUN:
        extra = CAP_REASON.format(agent.run_state.commits)
    if extra is not None and attempt.element is not None:
        force, agent.run_state.ask_reason = True, extra
        asker = asker or agent.forced_approve or agent.floor_approve or (
            _nobody if agent.permissions is not None or extra in (UNKNOWN_REASON, PENDING_REASON) else None)
    if turn.session is not None and attempt.element is not None and agent._long_asks(gate, op, attempt.element):
        # A long run asks before every act that asks, Ask before acting off too (P1-2).
        force = True
    if force and attempt.element is not None:
        if asker is None:
            asker = agent.forced_approve
        if asker is None:
            turn.feedback = (f"Refused: {attempt.element.label[:60]!r} needs the user's OK, and this run can't ask for "
                        "it. Finish with what you found, or BLOCKED if the request needs this.")
            turn.history.append(
                f"{turn.step}.{index}: {op} {attempt.element.label[:60]!r} refused (needs the user's OK)")
            agent._emit({"event": "frontier_refused", "step": turn.step, "label": "needs_approval"})
            return "stop"
    if asker is None and agent.permissions is not None:
        # Full access: what asks in every mode (a one-time code) still reaches the person.
        asker = agent.floor_approve
    if asker is None and agent._asks_mode():
        # A mode that asks with nobody to answer: what would ask is refused, never done.
        asker = _nobody
    cards_before = agent.run_state.cards
    if unknown_repeat == "ask":
        # A consequential action whose first outcome is unknown asks in every mode.
        asker = agent.approve or agent.forced_approve or agent.floor_approve
    if asker is not None:
        verdict, waited = agent._approval(gate, op, attempt.element, turn.snapshot, text, turn.chain_typed, turn.step,
                                          asker=asker, force=force, unknown=unknown_repeat == "ask")
        turn.started += waited  # waiting for the user never spends the task's time
        agent._waited(waited)
        if verdict is None and check(agent, turn, Where.WAITED) is not None:
            # Approved, but Stop came too: a stopped task never acts on an approval.
            return "end"
        if verdict is None and agent.run_state.cards == cards_before and op in ("TAP", "LONG_PRESS") \
                and agent._asks_mode():
            # The two-headed check: the pixels in the control's frame against its label.
            verdict, more = agent._two_heads(asker, op, attempt.element, turn.snapshot, turn.chain_typed, turn.step)
            turn.started += more
            agent._waited(more)
        if verdict is not None and verdict.startswith("refused:"):
            turn.feedback = (f"Refused: {attempt.element.label[:60]!r} needs the user's OK, and nobody can approve it "
                        "here. Finish with what you found, or BLOCKED if the request needs this.")
            turn.history.append(
                f"{turn.step}.{index}: {op} {attempt.element.label[:60]!r} refused (needs the user's OK)")
            agent._emit({"event": "frontier_refused", "step": turn.step, "label": "needs_approval"})
            turn.refusals[turn.feedback] = turn.refusals.get(turn.feedback, 0) + 1
            if turn.refusals[turn.feedback] >= SAME_REFUSALS_TO_STOP:
                turn.result = {"status": "blocked", "answer": None, "code": "stuck",
                          "reason": refused_sentence(turn.feedback, turn.refusals[turn.feedback], turn.actions)}
                return "end"
            return "stop"
        if verdict is not None and verdict.startswith("redirected:"):
            agent.run_state.asked = "redirected"
            instruction = " ".join(verdict.split(":", 1)[1].split())[:500]
            turn.request = f"{turn.request}\n\nThe user's later instruction: {instruction}"
            turn.feedback = (f"The user declined {attempt.element.label[:60]!r} and asked instead: {instruction!r}. "
                        "Do that now; change what you typed if it needs changing.")
            turn.history.append(f"{turn.step}.{index}: {op} {attempt.element.label[:60]!r} declined; user redirected")
            return "stop"
        if verdict is not None:
            turn.result = declined_result(verdict)
            return "end"
        if waited >= RESUME_AFTER_SECONDS:
            # The phone may have locked while the person decided.
            resumed = agent._guard_check("resume", turn.history)
            if resumed is not None and resumed.state == "unlocked" and attempt.element is not None:
                turn.snapshot = agent._observe()
                attempt.element = agent._find(
                    turn.snapshot, attempt.element.label, attempt.element.role, attempt.element.locator)
                if attempt.element is None:
                    turn.feedback = "The screen changed while Mobster waited for your OK: look again."
                    return "stop"
        if agent.run_state.cards != cards_before and attempt.element is not None and op in ELEMENT_OPS:
            # The approval is bound to what the card showed: any change on a fresh read means no action now.
            bound = binding(turn.snapshot, attempt.element)
            fresh = agent._read_once()
            found = agent._find(fresh, attempt.element.label, attempt.element.role, attempt.element.locator)
            if found is None or not same_binding(bound, binding(fresh, found)):
                turn.feedback = CHANGED_WHILE_ASKING
                turn.history.append(f"{turn.step}.{index}: {op} {attempt.element.label[:60]!r} not done "
                                    "(the screen changed while asking)")
                agent._emit({"event": "frontier_refused", "step": turn.step, "label": "changed_while_asking"})
                turn.snapshot, turn.unproven = fresh, False
                return "stop"
            turn.snapshot, attempt.element = fresh, found
