"""The phases of a run: begin, then each turn admits, observes, decides, absorbs and acts, then close."""

import os
import threading
import time
from collections import deque

from .. import contract as proof
from ..model_clients import ceiling_cost_usd, cost_usd
from .actions import actions_of, parse_reply
from .interrupts import Where, check
from .pipeline import dispatch
from .prompt import prompt_text, system_prompt
from .protocol import FINAL_CALL_SECONDS, MAX_CHUNK, OPERATIONS, WRAP_UP_SHARE, _schema
from .safety import LOOP_WINDOW, SAME_FAILURES_TO_CHECK, note_injection, scrub
from .screen import Provenance, _Verify, context_on, screen_rows, view_of
from .turn import Turn

MAX_ACTIONS = 150
MAX_NOTES = 40


def begin(agent, request):
    turn = Turn(request=request, started=time.monotonic(), recent=deque(maxlen=LOOP_WINDOW), session=agent.longform,
                data_json=agent.output_schema is not None,
                stopped={"status": "stopped", "answer": None, "reason": "Stopped by the user"})
    initial = agent.run_state.initial
    if initial is not None:
        turn.plan, turn.notes, turn.history = initial.plan or "", list(initial.notes), list(initial.history)
        turn.feedback, turn.calls = initial.feedback or "", max(0, int(initial.steps_used or 0))
        agent.run_state.cost_offset = float(initial.cost_usd or 0.0)
    agent.run_state.started = turn.started
    agent._actions = 0
    # A project's secrets the request carries (preset_secrets) are masked from the first turn, as a skill's are.
    agent.run_state.secrets = set(getattr(agent, "preset_secrets", ()) or ())
    agent.run_state.notes_before = [agent._mask(note) for note in turn.notes]
    agent.run_state.provenance = Provenance() if context_on("PROVENANCE") else None
    agent.run_state.readback, agent.run_state.kept = context_on("READBACK"), context_on("KEPT")
    agent.run_state.system = system_prompt(agent.skills)
    turn.max_actions = MAX_ACTIONS
    if turn.session is not None:
        agent.run_state.system = agent.run_state.system + "\n\n" + turn.session.context.LONGFORM_SYSTEM
        turn.session.attach(agent)
        turn.max_actions = max(MAX_ACTIONS, 3 * agent.max_steps)
    agent.run_state.stable_extra = agent._stable_lines(turn.request) + agent._stable_context
    turn.source_reads = getattr(agent.driver, "source_reads", None), getattr(agent.driver, "source_seconds", None)
    # The contract compiles during the first read and the first decision; it is joined before any action.
    turn.compiled = []
    compile_ = (lambda: proof.compile_contract(agent.client, turn.request, list(agent.apps.values())))
    agent._resolve_saved(turn.request)
    saved = agent.saved_program
    if saved is not None and saved.items:
        # The program's contract, validated as a compiled one is: no compile call.
        def compile_(items=saved.items):
            contract = proof.Contract.from_items(items, turn.request)
            return contract if contract.items else proof.compile_contract(agent.client, turn.request,
                                                                          list(agent.apps.values()))
    turn.worker = threading.Thread(target=lambda: turn.compiled.append(compile_()), daemon=True)
    turn.worker.start()
    agent._contract = None
    agent._tune()
    try:
        turn.snapshot = agent._first_screen()
    except BaseException:
        turn.worker.join(60)
        raise
    agent._spend("observe", turn.started)
    # A saved program's first steps act at once, so its contract is joined first.
    turn.pending = agent.early_decision and agent.saved_program is None and not (
        agent.program_parts is not None and agent._parts_may_start(turn.snapshot, turn.request))
    # run() joins it if the loop ends before it does
    agent.run_state.compile_worker = turn.worker if turn.pending else None
    if not turn.pending:
        agent._join_contract(turn.worker, turn.compiled)
    agent._check_app(turn.snapshot)
    return turn


def admit(agent, turn):
    if check(agent, turn, Where.TURN) is not None:
        return True
    tick = getattr(agent.driver, "next_step", None)
    if callable(tick):
        was = getattr(agent.driver, "ax_only_steps", 0)
        left = tick()
        if isinstance(left, int) and left and not was:
            agent._emit({"event": "frontier_ax_only", "step": turn.calls, "steps": left})
    turn.spent = cost_usd(getattr(agent.client, "model", ""), agent.client.usage)
    estimated = None
    if turn.spent is None and agent.max_cost_usd is not None:
        # A cap the run can't measure is never ignored: an unpriced model counts at the highest listed price.
        turn.spent = estimated = ceiling_cost_usd(agent.client.usage)
        if not turn.unpriced and cost_usd(getattr(agent.client, "model", ""), {"prompt_tokens": 1}) is None:
            turn.unpriced = (f" (Mobster has no price for {getattr(agent.client, 'model', '') or 'this model'}, so it "
                        "counted the highest listed price)")
            agent._emit({"event": "frontier_spend_estimate", "model": getattr(agent.client, "model", "") or None,
                        "basis": "highest_listed_price"})
    if turn.spent is not None and agent.run_state.cost_offset:
        turn.spent += agent.run_state.cost_offset
    turn.reserve = 0.0
    if turn.session is not None:
        turn.spent, turn.reserve = turn.session.spent_usd() + (estimated or 0.0), turn.session.reserve_usd()
    if turn.session is not None:
        ended = agent._long_final(turn.session, turn.calls, turn.started, turn.spent, turn.reserve)
        if ended is not None:
            turn.result = ended
            return True
        turn.session.on_turn(turn.calls, agent._app_name(turn.snapshot) or None, turn.history,
                        bundle=getattr(turn.snapshot, "bundle_id", None),
                        screen=getattr(turn.snapshot, "content_fingerprint", None))
    # The last turn of any budget may only report: a run that never answered scored nothing (mem-005).
    turn.final = ("budget"
                  if agent.max_cost_usd is not None and turn.spent is not None and turn.spent >= agent.max_cost_usd
             else "max_steps" if turn.calls >= agent.max_steps - 1
             else "timeout" if time.monotonic() - turn.started > agent.max_seconds - FINAL_CALL_SECONDS else None)
    turn.capped = turn.final is not None
    turn.operations = OPERATIONS + tuple(agent._skill_ops)
    if turn.capped:
        turn.operations = ("DONE", "BLOCKED")
        turn.feedback = ("This is your last turn: the budget is used up. Do not act. Finish now with DONE (or "
                    "BLOCKED): put in answer everything you found and did, and say plainly what is left undone.")
    elif (turn.session is None and agent.max_cost_usd is not None and turn.spent is not None
          and turn.spent >= WRAP_UP_SHARE * agent.max_cost_usd):
        turn.feedback = ((turn.feedback + " ") if turn.feedback else "") + (
            "Budget nearly used: take only the steps that finish the request, then DONE with the full answer.")
    if agent.dialogs and not turn.capped:
        ended, said, turn.snapshot, waited = agent._system_dialog(turn.snapshot, turn.calls, turn.history)
        turn.started += waited
        if ended is not None:
            turn.result = ended
            return True
        if said:
            turn.feedback = said + ((" " + turn.feedback) if turn.feedback else "")
    if agent.run_state.redirect is not None:
        name, instruction = agent.run_state.redirect
        agent.run_state.redirect = None
        turn.request = f"{turn.request}\n\nThe user's later instruction: {instruction}"
        turn.feedback = (f"The user didn't allow {name} and asked instead: {instruction!r}. Do that now."
                    + ((" " + turn.feedback) if turn.feedback else ""))
        turn.snapshot = agent._observe()
    return False


def observe(agent, turn):
    if agent._contract is not None:
        agent._contract.observe(agent._masked(turn.snapshot), agent._app_name(turn.snapshot), turn.calls)
        ended = agent._screen_goal_finish(turn.snapshot, turn.actions, turn.capped, turn.calls)
        if ended is None:
            ended, said = agent._saved_finish(turn.actions, turn.capped, turn.calls, turn.snapshot)
            if said:
                turn.feedback = ((turn.feedback + " ") if turn.feedback else "") + said
        if ended is not None:
            turn.result = ended
            return True
    if agent.program_parts is not None and not turn.capped and not turn.pending and agent._contract_ready():
        ended, said, fresh = agent._parts_point(turn.snapshot, turn.request, turn.calls, turn.actions)
        if said:
            turn.feedback = ((turn.feedback + " ") if turn.feedback else "") + said
        if ended is not None:
            turn.result = ended
            return True
        if fresh is not turn.snapshot:
            turn.snapshot, turn.unproven = fresh, False
    if not turn.capped and agent._saved_rejoin(turn.snapshot):
        turn.feedback = ((turn.feedback + " ") if turn.feedback else "") + (
            "Mobster goes on with the steps the person taught: the screen fits one of them again.")
    if agent._saved_live() and not turn.capped:
        ready = agent._saved_ready(turn.snapshot)
        if ready is not turn.snapshot:
            turn.snapshot, turn.unproven = ready, False
    t_prep = time.monotonic()
    row_marks = None
    if agent.run_state.provenance is not None:
        agent.run_state.provenance.observe(
            view_of(agent._app_name(turn.snapshot), turn.snapshot), turn.snapshot.elements, turn.acted_since)
        row_marks = agent.run_state.provenance.marks(turn.snapshot.elements)
    turn.acted_since = False
    if agent.run_state.readback:
        agent._remember_lists(turn.snapshot)
    turn.aliases, turn.lines = screen_rows(turn.snapshot, agent.run_state.receipts, row_marks)
    note_injection(agent, turn.snapshot, turn.calls)
    if agent.region_reader is not None and agent._asks_mode():
        try:
            prefetch = getattr(agent.region_reader, "prefetch", None)
            if callable(prefetch):
                prefetch(agent.driver, turn.snapshot)  # read while the model decides
        except Exception:
            pass
    send = agent.screenshots and agent._wants_image(turn, turn.lines)
    turn.image = agent._screenshot(turn.snapshot) if send else None
    if turn.image is not None:
        agent.run_state.imaged_app = getattr(turn.snapshot, "bundle_id", "") or ""
    agent.run_state.blind = 0 if turn.image is not None or not agent.screenshots else agent.run_state.blind + 1
    turn.prep_ms = agent._spend("prep", t_prep)
    if os.environ.get("MOBSTER_TRACE_SCREENS") == "on":
        # Only when a harness asks, never the app; masked like the prompt.
        agent._emit({"event": "frontier_screen", "step": turn.calls, "app": agent._app_name(turn.snapshot),
                    "rows": agent._mask("\n".join(turn.lines))[:12000]})
    return False


def decide(agent, turn):
    t0 = time.monotonic()
    agent.run_state.calls = turn.calls
    executed = None
    if agent._saved_live() and not turn.capped and not turn.pending and agent._contract_ready():
        executed, why = agent._saved_turn(turn.snapshot, turn.aliases, turn.calls)
        if why:
            turn.feedback = ((turn.feedback + " ") if turn.feedback else "") + why
    if agent.link_first is not None:
        agent.link_first.settle(turn.snapshot)
        if executed is None and "OPEN_URL" in agent._skill_ops:
            executed = agent.link_first.turn(turn.request, agent.apps)
    turn.programmed = executed is not None
    turn.unrun = None
    if executed is not None:
        turn.out, turn.proposed, turn.usage, turn.messages, turn.verify = executed, actions_of(executed), {}, None, None
    else:
        if agent.turn_context is not None or agent.run_state.steered or agent._first_turn_context:
            agent.run_state.turn_extra = agent._turn_sections(
                turn.calls, turn.snapshot, turn.plan, turn.notes, turn.started, turn.spent)
        turn.messages = agent._prompt(
            turn.request, turn.snapshot, turn.lines, turn.plan, turn.history, turn.feedback, turn.image)
        agent.run_state.listing, agent.run_state.passed = None, []  # shown once
        turn.verify = _Verify(agent.driver) if turn.unproven and not turn.capped else None
        try:
            reply, turn.usage = agent._decide(turn.messages, _schema(agent.apps, turn.operations, data=turn.data_json,
                                                      step=turn.session is not None), turn.calls, client=agent.client)
            turn.out, turn.proposed, turn.unrun = parse_reply(reply, turn.operations)
        except Exception as error:
            if turn.verify is not None:
                turn.verify.result()
            if check(agent, turn, Where.WAITED) is not None:
                return True  # Stop during the call or its retry wait: stopped, not an error
            agent._emit({"event": "frontier_error", "step": turn.calls, "detail": str(error)[:300]})
            turn.result = {"status": "error", "answer": None, "reason": str(error)[:300]}
            return True
    turn.latency_ms = agent._spend("model" if executed is None else "program", t0)
    if turn.pending:
        # The early decision is back: the contract is joined before anything acts.
        turn.pending, agent.run_state.compile_worker = False, None
        agent._join_contract(turn.worker, turn.compiled)
        if agent._contract is not None:
            agent._contract.observe(agent._masked(turn.snapshot), agent._app_name(turn.snapshot), turn.calls)
    if check(agent, turn, Where.WAITED) is not None:
        if turn.verify is not None:
            turn.verify.result()
        return True
    if turn.messages is not None:
        agent.emit({"event": "frontier_prompt", "step": turn.calls, "text": prompt_text(turn.messages),
                   "image": turn.image["image_url"]["url"] if turn.image else None,
                   "image_why": agent.run_state.image_why, "operations": list(turn.operations),
                   "targets": list(turn.aliases), "apps": list(agent.apps), "out": agent._mask_reply(turn.out)})
    return False


def absorb(agent, turn):
    turn.step, turn.calls, turn.feedback = turn.calls, turn.calls + 1, turn.unrun or ""
    if turn.unrun:
        turn.history.append(f"{turn.step}: part of the reply was not run (not a listed action)")
        agent._emit({"event": "frontier_refused", "step": turn.step, "label": "unknown_action"})
        turn.unrunnable = turn.unrunnable + 1 if not turn.proposed else 0
        if turn.unrunnable >= SAME_FAILURES_TO_CHECK:
            # A model whose replies name no listed action twice in a row won't start to.
            turn.result = {"status": "error", "answer": None,
                      "reason": "The model's replies named no action Mobster can run, twice in a row. Choose "
                                "another model in Settings › Models, or try again."}
            return True
    elif not turn.programmed:
        turn.unrunnable = 0
    before_plan, before_notes = turn.plan, len(turn.notes)
    turn.plan = agent._mask((turn.out.get("plan") or turn.plan)[:600])
    if turn.plan != before_plan:
        agent.run_state.turn_plans[turn.step] = turn.plan
    known = {" ".join(n.casefold().split()) for n in turn.notes}
    if turn.session is not None:
        turn.session.on_reply(turn.out.get("step"), turn.step)
    for note in turn.out.get("notes_add") or ():
        if (isinstance(note, str) and note.strip() and (len(turn.notes) < MAX_NOTES or turn.session is not None)
                and " ".join(note.casefold().split()) not in known):
            turn.notes.append(agent._mask(note.strip()[:300]))
            known.add(" ".join(note.casefold().split()))
            if turn.session is not None:
                turn.session.add_fact(turn.notes[-1])
    if len(turn.notes) > before_notes:
        agent.run_state.turn_notes[turn.step] = turn.notes[before_notes:]
    if turn.session is not None and len(turn.notes) > MAX_NOTES:
        del turn.notes[:len(turn.notes) - MAX_NOTES]
    agent.run_state.notes, agent.run_state.plan = turn.notes, turn.plan
    if agent._contract is not None:
        marked = agent._contract.update(turn.out.get("checklist_updates"), turn.step)
        if marked:
            agent.run_state.turn_marks[turn.step] = [f"{i.id} {i.status if i.kind == 'REPORT' else i.claim}"
                                      + (f"={agent._mask(i.value)[:120]}" if i.value else "") for i in marked]
            agent._emit({"event": "frontier_contract_update", "step": turn.step,
                        "items": [f"{i.id}:{i.status if i.kind == 'REPORT' else i.claim}"
                                  + (f"={agent._mask(i.value)[:60]}" if i.value else "") for i in marked]})
    turn.chunk = turn.proposed[:MAX_CHUNK]
    agent._emit({"event": "frontier_decision", "step": turn.step, "chunk": len(turn.chunk),
               "operation": turn.chunk[0].op if turn.chunk else None,
               "ops": [a.op for a in turn.chunk],
               "target_label": (turn.aliases[turn.chunk[0].target].label[:120]
                                if turn.chunk and turn.chunk[0].target in turn.aliases else None),
               "text": agent._mask(turn.chunk[0].text) if turn.chunk else None,
               "thought": agent._mask(turn.out.get("thought") or "")[:300],
               "latency_ms": turn.latency_ms, "prep_ms": turn.prep_ms, "usage": turn.usage,
               "source": "program" if turn.programmed else "planner"})
    turn.finished = False
    if turn.capped:
        answer = (agent._contract.done_gate(turn.out.get("answer"), 0)[2] if agent._contract is not None
                  else (turn.out.get("answer") or "").strip())
        turn.result = {"status": turn.final, "answer": answer,
                  "reason": {"budget": f"spend cap ${agent.max_cost_usd} reached{turn.unpriced}",
                             "max_steps": f"{agent.max_steps} turns used",
                             "timeout": f"{agent.max_seconds} s used"}[turn.final]}
        return True
    if turn.verify is not None:
        t_verify = time.monotonic()
        checked, turn.unproven = turn.verify.result(), False
        agent._spend("verify", t_verify)  # only the wait past the model's answer
        if checked is not None:
            turn.counts["verified"] += 1
            if checked.content_fingerprint != turn.snapshot.content_fingerprint:
                # The prompt's read was not the screen at rest: act on the newer one.
                turn.counts["verify_changed"] += 1
                agent._emit({"event": "frontier_verify", "step": turn.step, "changed": True})
            turn.snapshot = checked
    return False


def act(agent, turn):
    turn.after = None
    seen, turn.touched, turn.read_rows = turn.snapshot, [], None
    turn.acted = False
    turn.chain_typed = []  # text typed earlier in this chunk: a Send after it is bound to it
    turn.checked_front = False
    turn.untaken = None
    turn.turn_rows = None
    turn.turn_screen = turn.snapshot
    for turn.index, action in enumerate(turn.chunk):
        flow = dispatch(agent, action, turn)
        if flow == "next":
            continue
        if flow == "end":
            turn.finished = True
        break
    if agent.run_state.kept and turn.messages is not None and not agent.run_state.turn_notes.get(turn.step):
        kept = agent._kept_read(seen, turn.snapshot, turn.touched, turn.read_rows)
        if kept:
            agent.run_state.turn_reads[turn.step] = agent._mask(kept)
    if agent.checkpoint is not None or agent.run_context is not None:
        agent._send_checkpoint(turn.calls, turn.snapshot, turn.plan, turn.notes, turn.history, turn.started, turn.spent)
    return turn.finished


def close(agent, turn):
    if turn.result.get("status") == "approval_denied":
        agent._close_declined_sheet()
    if turn.result.get("status") == "max_steps" and not turn.result.get("reason"):
        turn.result["reason"] = (f"{MAX_ACTIONS} actions used" if turn.actions >= MAX_ACTIONS
                            else f"{agent.max_steps} turns used")
    elapsed = time.monotonic() - turn.started
    timing = {bucket: round(seconds, 2) for bucket, seconds in agent.run_state.timing.items()}
    timing["other"] = round(elapsed - sum(agent.run_state.timing.values()), 2)
    timing.update(turn.counts)
    if agent.link_first is not None:
        timing.update(agent.link_first.counts())
    if turn.source_reads[0] is not None:
        timing["source_reads"] = agent.driver.source_reads - turn.source_reads[0]
        timing["source_seconds"] = round(agent.driver.source_seconds - turn.source_reads[1], 2)
    if agent._contract is not None:
        turn.result["contract"] = agent._contract.summary()
    if agent.run_state.secrets:
        turn.result = scrub(turn.result, agent.run_state.secrets)  # the answer, the reason, data_json: never a secret
    if agent.run_state.unlocks:
        timing["unlocks"] = agent.run_state.unlocks
    turn.result.update({"steps": turn.calls, "actions": turn.actions, "notes": turn.notes, "plan": turn.plan,
                   "elapsed": round(elapsed, 2), "usage": dict(agent.client.usage), "timing": timing,
                   "cost_usd": cost_usd(getattr(agent.client, "model", ""), agent.client.usage)})
    agent.emit({"event": "result", **{k: v for k, v in turn.result.items() if k not in ("notes", "usage")}})
    return turn.result
