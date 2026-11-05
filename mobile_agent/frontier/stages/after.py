"""The stages that run once an action was sent: the screen after, the record, and whether the chain goes on."""

import time

from ... import contract_asks as proof_asks
from ... import contract_screen as proof_screen
from ...agent_hooks import GuardVerdict
from ..observe import TWO_READ_OPS, UNPROVEN_SETTLES
from ..prompt import row_texts
from ..protocol import HOUSEKEEPING_OPS, SCROLL_OPS, TYPED_OPS
from ..safety import COVERED, PENDING_REASON, SAME_FAILURES_TO_CHECK, GuardStop, consequential, input_shows
from ..screen import names_act, screen_signature, text_status

LOG_LABEL_CHARS, LOG_TEXT_CHARS = 200, 600


def settled_rect(op, element, snapshot):
    """Where the element this step acted on sits on the screen the step ends on, as fractions ``[x, y, w, h]``, for a
    tap or typing step whose element is still there (the same locator, role and label). None when it is unknown."""
    if element is None or not (op in TYPED_OPS or op in ("TAP", "LONG_PRESS")):
        return None
    for other in getattr(snapshot, "elements", None) or ():
        if (other.locator and other.locator == element.locator and other.role == element.role
                and other.label == element.label):
            try:
                x, y, w, h = (float(value) for value in other.rect)
            except (TypeError, ValueError):
                return None
            return [round(x, 4), round(y, 4), round(w, 4), round(h, 4)] if w > 0 and h > 0 else None
    return None


def may_commit(op, element, snapshot):
    """Whether a press may have committed something, by the loop's own test. A test that fails counts as a commit."""
    try:
        return consequential(op, element, snapshot)
    except Exception:
        return True


def action_label(op, element, app):
    if op in ("SWIPE_LEFT", "SWIPE_RIGHT"):
        return element.label[:120] if element is not None and element.label else None
    return element.label[:120] if element else app


def settle(agent, attempt, turn):
    index = turn.index
    op, element, gate, before = attempt.op, attempt.element, attempt.gate, attempt.before
    attempt.exec_ms = agent._spend("exec", attempt.t_exec)
    t_settle = time.monotonic()
    following = turn.chunk[index + 1].op if index + 1 < len(turn.chunk) else None
    last = following in (None, "DONE", "BLOCKED")
    # HOME right before LAUNCH_APP uses nothing on the home screen, so its settle is skipped.
    passing = op == "HOME" and following == "LAUNCH_APP"
    if passing:
        turn.snapshot = before
    elif op in TYPED_OPS and following == "SUBMIT":
        # Return goes to the field just typed into: one read finds it.
        turn.snapshot = agent._read_once()
    else:
        turn.snapshot = agent._settle(before, last and op not in TWO_READ_OPS, rest=last and op == "LAUNCH_APP")
    turn.unproven = last and getattr(agent.driver, "settled_by", None) in UNPROVEN_SETTLES
    attempt.settle_ms = agent._spend("settle", t_settle)
    if agent._contract is not None:
        agent._contract.observe(agent._masked(turn.snapshot), agent._app_name(turn.snapshot), turn.step)
    attempt.changed = passing or turn.snapshot.content_fingerprint != before.content_fingerprint
    if (not attempt.changed and gate is not None and gate.item is not None and proof_asks.asks(gate.item)
            and agent.permissions is not None and gate.item.status != "closed"
            and str(gate.item.count or "1").strip() in ("", "1")):
        # Sent, no receipt yet: the same act again asks first, with the warning, in every mode.
        agent.run_state.held_items[gate.item.id] = PENDING_REASON
    gone = agent._row_gone(op, element, before, turn.snapshot)
    if gone is not None:
        turn.result = {"status": "blocked", "answer": None, "code": "unexpected_change",
                            "reason": gone}
        agent._emit({"event": "frontier_refused", "step": turn.step, "label": "row_gone"})
        return "end"
    if op in SCROLL_OPS and following in SCROLL_OPS and attempt.changed:
        # The next swipe was chosen without this screen: its rows would scroll past unseen (mem-009).
        if turn.turn_rows is None:
            turn.turn_rows = set(row_texts(agent._masked(turn.turn_screen)))
        for row in row_texts(agent._masked(turn.snapshot)):
            if row not in turn.turn_rows:
                turn.turn_rows.add(row)
                agent.run_state.passed.append(row)
    done_key = agent._act_key(op, element, before) if attempt.changed else None
    if agent.run_state.act_seen:
        where, now = agent._app_name(turn.snapshot), None
        for (act_app, _, _), looked in agent.run_state.act_seen.items():
            if act_app == where:
                now = now or screen_signature(turn.snapshot)
                looked.add(now)
    if done_key is not None:
        agent.run_state.acts_done[done_key] = turn.step
        agent.run_state.act_left[done_key] = screen_signature(turn.snapshot)
        agent.run_state.act_seen[done_key] = {agent.run_state.act_left[done_key]}
        agent.run_state.act_refused.pop(done_key, None)


def record(agent, attempt, turn):
    index = turn.index
    op, text, app, element, gate, before, changed = (
        attempt.op, attempt.text, attempt.app, attempt.element, attempt.gate, attempt.before, attempt.changed)
    turn.after = op if changed or op in TYPED_OPS else None
    turn.last_done = (attempt.key, before.content_fingerprint, turn.snapshot.content_fingerprint)
    turn.recent.append(attempt.spot)
    agent.last_screen = turn.snapshot
    agent._emit({"event": "frontier_action", "step": turn.step, "index": index, "operation": op,
               "target_label": action_label(op, element, app), "changed": changed,
               "text": agent._mask(text[:200]) if text is not None and (op in TYPED_OPS or op == "SCROLL_TO") else None,
               "field": ("search" if proof_screen.is_search_field(element) else "composer"
                         if proof_screen.COMPOSER.search(element.label or "") else "field")
               if element is not None and op in TYPED_OPS else None,
               "app_name": agent._app_name(before) or None,
               # Whether a press may have committed something: the step's commit.
               **({"consequential": may_commit(op, element, before)} if op in ("TAP", "LONG_PRESS") else {}),
               **({"element_rect": rect} if (rect := settled_rect(op, element, turn.snapshot)) else {}),
               "refresh_ms": attempt.refresh_ms, "exec_ms": attempt.exec_ms, "settle_ms": attempt.settle_ms,
               **({"settle_prediction": ps} if (ps := getattr(agent.driver, "predicted_settle", None)) else {})})
    what = (element.label[:LOG_LABEL_CHARS] if element else app or "")
    receipt = agent.run_state.last_receipt
    if receipt is not None:
        agent._emit({"event": "frontier_text", "step": turn.step, "index": index, "method": receipt.get("method"),
                    "chars": receipt.get("chars"), "matches": receipt.get("matches"),
                    "repaired": receipt.get("repaired"), "flattened": receipt.get("flattened")})
    turn.history.append(f"{turn.step}.{index}: {op} {what!r}" + (f" text={text[:LOG_TEXT_CHARS]!r}" if text else "")
                   + (" -> screen changed" if changed else " -> NO visible change")
                   + (f"; field: {text_status(receipt)[:160]}" if receipt is not None else "")
                   + ("; Return NOT pressed" if receipt is not None and op == "TYPE_SUBMIT"
                      and receipt.get("submitted") is False else ""))
    if agent.run_state.readback:
        if changed and op in ("TAP", "LONG_PRESS", "TAP_XY") and names_act(op, element, text):
            said = agent._read_back(turn.snapshot)
            if said:
                turn.history[-1] += "; " + said
        agent._remember_lists(turn.snapshot)
    if op == "SET_TEXT" and text and not changed and not input_shows(element, text):
        # Only an unchanged SET_TEXT stops what follows: a TYPE's text is checked by its receipt.
        turn.untaken = (op, text)
    if agent.step_observer is not None:
        agent._observe_step(turn.step, index, op, before, element, text, changed, gate)
    else:
        agent.run_state.asked = None
    agent._check_app(turn.snapshot)
    if gate is not None and gate.item is not None and gate.item.act == "install":
        # The App Store's own confirmation is the person's: the guard hands it off or stops (C3).
        verdict = agent._guard_check("sheet_suspected", turn.history)
        if verdict is not None and verdict.state == "unlocked":
            turn.snapshot = agent._observe()
    if changed:
        turn.covered = 0
        turn.failures.clear()  # only failures in a row stop the run
    if (not changed and op in ("TAP", "TAP_XY", "LONG_PRESS") and not turn.checked_front
            and agent._front_app_differs(turn.snapshot)):
        # The tree is the app's, but SpringBoard is in front: the lock screen or a system sheet.
        turn.checked_front = True
        verdict = agent._guard_check("lock_suspected", turn.history)
        if verdict is not None and verdict.state == "unlocked":
            turn.snapshot = agent._observe()
        elif verdict is None:
            # No guard to look: taps land on a screen the tree doesn't show, and the second time the run stops (B2).
            turn.covered += 1
            name = agent._app_name(turn.snapshot) or "the app"
            if turn.covered >= SAME_FAILURES_TO_CHECK:
                raise GuardStop(GuardVerdict("stop", "phone_locked", COVERED.format(app=name)))
            turn.feedback = (f"The iPhone's lock screen, Home Screen or a system sheet is in front of {name}: "
                        "what is listed may not be what the screen shows. If it is the lock screen or a "
                        "passcode screen, finish with BLOCKED; never tap it.")
        return "stop"


def chain(agent, attempt, turn):
    index = turn.index
    op, element, changed = attempt.op, attempt.element, attempt.changed
    if attempt.commit_now and index + 1 < len(turn.chunk) and turn.chunk[index + 1].op not in ("DONE", "BLOCKED"):
        # A commit ends its chain: the next action is chosen on the screen it left.
        agent._emit({"event": "frontier_chunk_stop", "step": turn.step, "index": index, "reason": "commit"})
        return "stop"
    if not changed and op in HOUSEKEEPING_OPS and [a.op for a in turn.chunk[index + 1:]] == ["DONE"]:
        # Closing or scrolling had nothing to do; the DONE after it stands (mem-004).
        return "next"
    if (not changed and op == "TAP" and element is not None and element.editable
            and index + 1 < len(turn.chunk) and turn.chunk[index + 1].op in TYPED_OPS):
        # Focusing a field often shows nothing; the typing after it is the point (multi-010).
        return "next"
    if not changed and op not in TYPED_OPS:
        if index + 1 < len(turn.chunk):
            agent._emit({"event": "frontier_chunk_stop", "step": turn.step, "index": index,
                       "reason": "no_change"})
        return "stop"  # an action with no effect: look again before going on
    return "next"
