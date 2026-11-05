"""The stages that end or hold an action: an input that changed nothing, DONE, BLOCKED and WAIT."""

import time

from ..prompt import DONE_CHECK
from ..protocol import TYPED_OPS, WRAP_UP_SHARE

IDLE_WAIT_LIMIT = 2


def input_not_taken(agent, attempt, turn):
    index = turn.index
    op = attempt.op
    if turn.untaken is not None and op not in TYPED_OPS:
        # An input that changed nothing: what follows was planned on a value that isn't there.
        turn.feedback = (f"Your {turn.untaken[0]} {agent._mask(turn.untaken[1])[:60]!r} changed nothing on screen, so "
                    f"{op} did not run. Check the values on screen and set them before going on.")
        turn.history.append(f"{turn.step}.{index}: {op} not run: the {turn.untaken[0]} before it changed nothing")
        agent._emit({"event": "frontier_chunk_stop", "step": turn.step, "index": index,
                    "reason": "input_not_taken"})
        return "stop"


def _said(attempt, turn):
    return turn.out.get("answer") or attempt.text


def done_gate(agent, attempt, turn):
    if agent._contract is None:
        return None
    index = turn.index
    wrap = (agent.max_cost_usd is not None and turn.spent is not None
            and turn.spent + turn.reserve >= WRAP_UP_SHARE * agent.max_cost_usd)
    accepted, reason, answer = agent._contract.done_gate(
        _said(attempt, turn), 0 if wrap else agent.max_steps - turn.calls)
    if not accepted:
        turn.feedback = reason
        turn.history.append(f"{turn.step}.{index}: DONE refused: items not shown on screen yet")
        agent._emit({"event": "frontier_chunk_stop", "step": turn.step, "index": index,
                     "reason": "proof_gate", "open": [i.id for i in agent._contract.items
                                                      if i.reason and not i.unproven]})
        return "stop"
    agent._reveal_found(turn)
    turn.result = {"status": "completed", "answer": answer}
    if turn.data_json:
        turn.result["data_json"] = turn.out.get("data_json")
    return "end"


# No contract: a DONE right after a change, or before the request was walked item by item, is deferred once.
def done_deferred(agent, attempt, turn):
    if turn.calls >= agent.max_steps - 1 or (
            agent.max_cost_usd is not None and turn.spent is not None
            and turn.spent >= WRAP_UP_SHARE * agent.max_cost_usd):
        return None
    index = turn.index
    reasons = ["You finished right after changing things: check on this screen that every "
               "change took effect as intended."] if turn.acted else []
    if not turn.request_checked:
        reasons.append(DONE_CHECK)
    turn.request_checked = True
    if reasons:
        turn.feedback = " ".join(reasons)
        turn.history.append(f"{turn.step}.{index}: DONE deferred: check the result and the request first")
        agent._emit({"event": "frontier_chunk_stop", "step": turn.step, "index": index,
                     "reason": "verify_before_done"})
        return "stop"


def finish(agent, attempt, turn):
    said = _said(attempt, turn)
    if attempt.op == "BLOCKED":
        sentence = said or (turn.out.get("thought") or "").strip() or None
        turn.result = {"status": "blocked", "answer": sentence, "reason": sentence}
    else:
        turn.result = {"status": "completed", "answer": said}
        if turn.data_json:
            turn.result["data_json"] = turn.out.get("data_json")
    return "end"


def wait(agent, attempt, turn):
    index = turn.index
    if turn.idle_waits >= IDLE_WAIT_LIMIT:
        turn.feedback = ("Refused: you have waited twice and nothing on screen changed; nothing is "
                         "loading. Act instead: the thing you wait for may not exist (check that "
                         "earlier steps were really saved), or try another route.")
        turn.history.append(f"{turn.step}.{index}: WAIT refused (screen idle)")
        agent._emit({"event": "frontier_refused", "step": turn.step, "label": "idle_wait"})
        return "stop"
    turn.history.append(f"{turn.step}.{index}: WAIT")
    t_wait = time.monotonic()
    time.sleep(1.0)
    fresh = agent._observe()
    agent._spend("wait", t_wait)
    turn.idle_waits = turn.idle_waits + 1 if fresh.content_fingerprint == turn.snapshot.content_fingerprint else 0
    turn.snapshot, turn.unproven = fresh, False
    return "stop"


def active(agent, attempt, turn):
    turn.idle_waits = 0
