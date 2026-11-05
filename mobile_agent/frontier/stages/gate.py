"""The stages that ask the task's contract and the signed-in profile before an action."""

from ... import contract_asks as proof_asks
from ... import contract_screen as proof_screen
from ... import harness_api
from ...agent_hooks import GuardVerdict
from ..actions import Action
from ..safety import SAME_REFUSALS_TO_STOP, GuardStop, refused_sentence
from ..screen import screen_image


def contract_gate(agent, attempt, turn):
    index = turn.index
    op, element = attempt.op, attempt.element
    attempt.gate = None
    if agent._contract is not None and element is not None:
        attempt.gate = agent._contract.check(op, element, turn.snapshot, agent._app_name(turn.snapshot),
                                             text=attempt.text, chain_typed=tuple(turn.chain_typed))
        if not attempt.gate.allowed:
            turn.feedback = attempt.gate.reason
            turn.history.append(f"{turn.step}.{index}: {op} {element.label[:60]!r} refused (not declared)")
            agent._emit({"event": "frontier_refused", "step": turn.step, "label": element.label[:120],
                        "reason": attempt.gate.reason[:200]})
            turn.refusals[attempt.gate.reason] = turn.refusals.get(attempt.gate.reason, 0) + 1
            if turn.refusals[attempt.gate.reason] >= SAME_REFUSALS_TO_STOP:
                refused = turn.refusals[attempt.gate.reason]
                agent._emit({"event": "frontier_stuck", "step": turn.step, "refused": refused,
                             "reason": attempt.gate.reason[:200]})
                turn.result = {"status": "blocked", "answer": None, "code": "stuck",
                               "reason": refused_sentence(attempt.gate.reason, refused, turn.actions)}
                return "end"
            return "stop"
    if (attempt.unknown_repeat == "retry" and attempt.gate is not None and attempt.gate.item is not None
            and proof_asks.asks(attempt.gate.item)):
        # The contract calls it a commit, so the person is asked again.
        if (agent.approve or agent.forced_approve or agent.floor_approve) is None:
            turn.feedback = (f"Refused: your earlier {op} {element.label[:60]!r} has an unknown outcome: it may "
                        "have happened. Sending it again needs the user's OK, and nobody can give it here.")
            turn.history.append(f"{turn.step}.{index}: {op} {element.label[:60]!r} refused (its earlier outcome was "
                           "unknown)")
            agent._emit({"event": "frontier_refused", "step": turn.step, "label": "outcome_unknown"})
            return "stop"
        attempt.unknown_repeat = "ask"


def ask_at_send(agent, attempt, turn):
    index = turn.index
    text, element = attempt.text, attempt.element
    can_ask = agent.approve is not None or (turn.session is not None and agent.forced_approve is not None)
    if (attempt.op == "TYPE_SUBMIT" and can_ask and attempt.gate is not None
            and attempt.gate.item is not None and proof_asks.asks(attempt.gate.item)
            and attempt.gate.verbs == frozenset({"send"})):
        # One message, one approval: type now, unasked; the Return asks with the typed text in its composer.
        turn.chunk.insert(index + 1, Action("SUBMIT", target_label=element.label, locator=element.locator,
                                             role=element.role))
        attempt.op, attempt.gate = "TYPE", proof_screen.Gate(True)
        attempt.key = (attempt.op, element.label, text)
        attempt.spot = (attempt.op, element.locator or element.label, text, turn.snapshot.content_fingerprint)
        agent._emit({"event": "frontier_split", "step": turn.step, "index": index, "reason": "ask_at_send"})


def account(agent, attempt, turn):
    index = turn.index
    op, element, gate = attempt.op, attempt.element, attempt.gate
    if agent.commit_check is not None and element is not None:
        commit = (gate.item.act if gate is not None and gate.item is not None and proof_asks.asks(gate.item)
                  else None) if agent._contract is not None else harness_api.commit_act(
            op, element.label, None, getattr(turn.snapshot, "bundle_id", None))
        agent.run_state.acting_as = agent.run_state.media_checked = None
        if commit:
            checked = agent.commit_check(commit, turn.snapshot) or {}
            if checked.get("stop"):
                agent._emit({"event": "frontier_refused", "step": turn.step, "label": element.label[:120],
                            "reason": "account"})
                raise GuardStop(GuardVerdict("stop", checked.get("code") or "account_mismatch",
                                             checked["stop"]))
            if checked.get("refuse"):
                turn.feedback = checked["refuse"]
                turn.history.append(f"{turn.step}.{index}: {op} {element.label[:60]!r} refused (profile not "
                               "confirmed)")
                agent._emit({"event": "frontier_refused", "step": turn.step, "label": element.label[:120],
                            "reason": "account"})
                return "stop"
            agent.run_state.acting_as = checked.get("acting_as")
            if callable(checked.get("preview")):
                # A check of the screen this commit is made on (the queue's media check): no picture, or any failure,
                # is no match.
                image = screen_image(agent.driver, getattr(turn.snapshot, "read_started", None))
                try:
                    agent.run_state.media_checked = bool(image is not None and checked["preview"](image))
                except Exception:  # noqa: BLE001
                    agent.run_state.media_checked = False
