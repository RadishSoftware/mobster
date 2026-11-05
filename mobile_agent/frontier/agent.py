"""FrontierAgent: a frontier model chooses each step, over Mobster's perception and control layer."""

import logging
import os
import time

from .. import contract_screen as proof_screen
from .. import harness_api
from ..harness import checkpoints as harness_checkpoints
from ..model_clients import cost_usd
from .act import ActMixin
from .approvals import ApprovalsMixin
from .hooks import HooksMixin
from .observe import ObserveMixin
from .phases import absorb, act, admit, begin, close, decide, observe
from .prompt import PromptMixin, image_part
from .safety import PHONE_LOCKED, GuardStop, SafetyMixin, scrub
from .saved import SavedMixin
from .skills import SkillsMixin, default_skills, usable_skills
from .turn import RunState

log = logging.getLogger("mobster.frontier")


# Screen finish: a request that only opens a screen ends once code sees it titled so and the DONE gate accepts.
def screen_goal(contract):
    if contract is None or not contract.items:
        return None
    if any(item.kind not in ("READ", "FORBID") for item in contract.items):
        return None
    reads = [item for item in contract.items if item.kind == "READ"]
    if not reads:
        return None
    names = proof_screen.quoted(reads[-1].what) or [reads[-1].what]
    return [proof_screen.plain(name) for name in names if proof_screen.plain(name)]


def titled_with(snapshot, names):
    title = proof_screen.plain(proof_screen.screen_title(snapshot) or "")
    return bool(title) and any(name == title or name.startswith(title + " screen") for name in names or ())


class FrontierAgent(ApprovalsMixin, ActMixin, ObserveMixin, PromptMixin, SavedMixin, SkillsMixin,
                    HooksMixin, SafetyMixin):
    def __init__(self, driver, client, *, apps=(), emit=None, max_steps=50, max_seconds=900, screenshots=True,
                 settle_seconds=.6, max_cost_usd=None, approve=None, cancelled=None,
                 guard=None, skills=None, output_schema=None, early_decision=True, context_blocks=(),
                 turn_context=None, steering=None, clarify=None, step_observer=None, checkpoint=None,
                 run_context=None, commit_check=None, dialogs=None, permission_mode=None, dialog_approve=None,
                 longform=None, forced_approve=None, permissions=None, floor_approve=None,
                 saved_program=None, program_parts=None, region_reader=None, dialog_policy=None):
        self.driver, self.client = driver, client
        # region_reader(driver, element, snapshot): the text the pixels show in a control, "" for none, None unknown.
        if region_reader is None and permissions is not None:
            # The harness's reader; the open-source engine has none, and the tree decides.
            load = harness_api.hooks().screen_text
            try:
                region_reader = load()
            except Exception:
                region_reader = None
        self.region_reader = region_reader
        self.run_state = RunState(started=time.monotonic())
        # saved_program: a Matched, or a function (client, request) -> Matched | None called once at the run's start.
        self.saved_program = saved_program
        self.program_parts = program_parts
        # commit_check(act, snapshot) -> None, {"acting_as"}, {"refuse": feedback} or {"stop": reason, "code"}.
        self.commit_check = commit_check
        # longform=None keeps every prompt and reply schema byte for byte as without long tasks.
        self.longform = longform
        # forced_approve asks in a long run when Ask before acting is off.
        self.forced_approve = forced_approve
        self.permissions = permissions
        # floor_approve asks, in Full access, only what asks in every mode (a one-time code).
        self.floor_approve = floor_approve
        # Seam S7 hooks (harness_api): with none of them set, the prompts are byte for byte what they were.
        self.context_blocks = tuple(context_blocks or ())
        self.turn_context, self.steering, self.clarify = turn_context, steering, clarify
        self.step_observer, self.checkpoint, self.run_context = step_observer, checkpoint, run_context
        self._stable_context = "".join(harness_api.render_block(b) for b in self.context_blocks if b.stable)
        self._first_turn_context = "".join(harness_api.render_block(b) for b in self.context_blocks if not b.stable)
        self._stable_images = tuple(image_part(mime, data) for b in self.context_blocks if b.stable
                                    for mime, data in (b.images or ()))[:4]
        self.link_first = None  # intents.plan.LinkFirst, set by a caller with MOBSTER_EXPERIMENTAL_INTENTS on
        self.guard = guard
        self.skills = usable_skills(default_skills() if skills is None else skills)
        self._skill_ops = {skill.op: skill for skill in self.skills}
        self.output_schema = output_schema
        # The first decision runs while the contract compiles; the contract is joined before any action.
        self.early_decision = early_decision
        # cancelled() is checked each turn, after each model call, before each action and between macro swipes.
        self.cancelled = cancelled
        # approve(request) blocks until the person answers: approved, denied, timeout, stopped or "redirected:<text>".
        self.approve = approve
        # Without a contract, the regex guard refuses commits the request doesn't ask for.
        self._contract = None
        self.max_cost_usd = max_cost_usd
        self.apps = dict(apps)  # bundle -> name
        self._sink = emit or (lambda event: None)
        self.max_steps, self.max_seconds = max_steps, max_seconds
        self.screenshots = screenshots and getattr(client, "vision", True) is not False
        # Which turns carry the picture (image_policy.py): every turn unless MOBSTER_IMAGE_POLICY=tree.
        self._image_policy = harness_api.hooks().image_policy()
        self._image_mode = self._image_policy.mode() if self._image_policy else "always"
        self.settle_seconds = settle_seconds
        self._actions = 0
        # The last action's settled screen: the step list keeps where its fields sit, never their text.
        self.last_screen = None
        # iOS's own dialogs are answered under the permission mode in a Mobster run, unless MOBSTER_SYSTEM_DIALOGS=off.
        flag = os.environ.get("MOBSTER_SYSTEM_DIALOGS", "").strip().casefold()
        self.dialogs = (flag == "on" or (flag not in ("off", "0", "false") and run_context is not None)
                        if dialogs is None else bool(dialogs))
        self.permission_mode = permission_mode
        self.dialog_approve = dialog_approve if dialog_approve is not None else approve
        self.dialog_policy = dialog_policy   # a check's answers to iOS's dialogs (verify/dialogs.py); None in a task

    def _spend(self, bucket, since):
        seconds = time.monotonic() - since
        self.run_state.timing[bucket] = self.run_state.timing.get(bucket, 0.0) + seconds
        return round(seconds * 1000)

    def _stopping(self):
        try:
            return self.cancelled is not None and bool(self.cancelled())
        except Exception:
            return False

    def _app_name(self, snapshot):
        return self.apps.get(getattr(snapshot, "bundle_id", ""), getattr(snapshot, "bundle_id", "") or "")

    # The journal and the Mac app see events, never a secret: every string is masked.
    def emit(self, event):
        secrets = self.run_state.secrets
        self._sink(scrub(event, secrets) if secrets else event)

    def _emit(self, event):
        self.emit({**event, "t": round(time.monotonic() - self.run_state.started, 2)})
        if event.get("event") == "frontier_refused" and self.longform is not None:
            # A long run whose actions keep being refused is going nowhere.
            self.longform.on_refused()

    # A phone-guard stop ends the run blocked with the guard's sentence; a dead device still reports from the notes.
    def run(self, request, initial=None):
        # Picked up: acts the earlier run may already have done stay held.
        self.run_state = RunState(started=time.monotonic(), initial=initial,
                                  held_acts=frozenset(getattr(initial, "held_acts", ()) or ()),
                                  notes=list(initial.notes) if initial is not None else [],
                                  plan=initial.plan if initial is not None else "")
        self._compiled_delta = None  # a run's own reference screen (compiled_read/delta.py)
        result = None
        # Stop reaches the model client too: a call waiting out a rate limit ends at once.
        held = getattr(self.client, "cancelled", None)
        if self.cancelled is not None:
            try:
                self.client.cancelled = self._stopping
            except AttributeError:
                pass
        try:
            result = self._run(request)
            if self.saved_program is not None and isinstance(result, dict):
                result["program"] = self._saved_report()
            if self.program_parts is not None and isinstance(result, dict):
                result["parts"] = self.program_parts.report()
            if getattr(self, "_compiled_delta", None) is not None and isinstance(result, dict):
                result["compiled_read"] = {"delta": dict(self._compiled_delta.stats)}
            return result
        except GuardStop as stop:
            result = self._guard_result(stop.verdict)
            return result
        except Exception as error:
            self.emit({"event": "frontier_error", "step": self.run_state.calls,
                       "detail": f"{type(error).__name__}: {error}"[:300]})
            stopped = self._stopping()
            answer = None if stopped or self.longform is not None else self._report_from_notes(request)
            result = {"status": "stopped" if stopped else "error", "answer": answer,
                      "reason": f"{type(error).__name__}: {error}"[:300],
                      "steps": self.run_state.calls, "actions": 0, "notes": self.run_state.notes,
                      "plan": self.run_state.plan,
                      "elapsed": None, "usage": dict(self.client.usage),
                      "timing": {bucket: round(seconds, 2) for bucket, seconds in self.run_state.timing.items()},
                      "cost_usd": cost_usd(getattr(self.client, "model", ""), self.client.usage)}
            self.emit({"event": "result", **{k: v for k, v in result.items() if k not in ("notes", "usage")}})
            return result
        finally:
            if self.cancelled is not None:
                try:
                    self.client.cancelled = held
                except AttributeError:
                    pass
            worker, self.run_state.compile_worker = self.run_state.compile_worker, None
            if worker is not None:
                worker.join(60)  # the contract call still in flight bills and reports before the run ends
            if self.run_context is not None and isinstance(result, dict):
                try:
                    # A run that can't be picked up keeps no checkpoint.
                    harness_checkpoints.ended(self.run_context, result.get("status"), result.get("code"))
                except Exception:
                    log.exception("checkpoint cleanup failed")

    def _guard_result(self, verdict):
        elapsed = time.monotonic() - self.run_state.started
        timing = {bucket: round(seconds, 2) for bucket, seconds in self.run_state.timing.items()}
        result = {"status": "blocked", "answer": None, "reason": verdict.message or PHONE_LOCKED,
                  "code": verdict.code or None, "steps": self.run_state.calls, "actions": self._actions,
                  "notes": [self._mask(n) for n in self.run_state.notes], "plan": self._mask(self.run_state.plan),
                  "elapsed": round(elapsed, 2), "usage": dict(self.client.usage), "timing": timing,
                  "cost_usd": cost_usd(getattr(self.client, "model", ""), self.client.usage)}
        if self._contract is not None:
            result["contract"] = self._contract.summary()
        self.emit({"event": "result", **{k: v for k, v in result.items() if k not in ("notes", "usage")}})
        return result

    def _join_contract(self, worker, compiled):
        t_contract = time.monotonic()
        worker.join(90)
        self._contract = (compiled or [None])[0]
        if self._contract is not None and self.longform is not None:
            # A long task's REPORT items are proven from its notebook.
            self._contract.notebook = self.longform.notebook
        self._spend("model", t_contract)
        if self._contract is not None:
            self._emit({"event": "frontier_contract", "items": [i.label() for i in self._contract.items],
                        "dropped": [i.label() for i in self._contract.dropped]})
        else:
            # No contract: the event keeps its name; the app's plan and recorded traces wait for it.
            self._emit({"event": "frontier_checklist", "items": []})

    def _screen_goal_finish(self, snapshot, actions, capped, calls):
        if actions < 1 or capped or self.longform is not None:
            return None
        names = screen_goal(self._contract)
        if not names or not titled_with(snapshot, names):
            return None
        saved = self.saved_program
        if saved is not None and saved.stopped is None and saved.steps:
            if saved.at < len(saved.steps):
                return None  # a saved program takes all its steps first; a screen on the way proves nothing
            self._saved_check()
        import copy
        title = " ".join((proof_screen.screen_title(snapshot) or "").split())
        # The gate is tried on a copy first: a refusal here must not count toward its reopen limit.
        if not copy.deepcopy(self._contract).done_gate(f"Opened {title}.", self.max_steps - calls)[0]:
            return None
        accepted, _, answer = self._contract.done_gate(f"Opened {title}.", self.max_steps - calls)
        if not accepted:
            return None
        self._emit({"event": "frontier_screen_finish", "step": calls, "title": self._mask(title)[:120]})
        if saved is not None and saved.stopped is None and saved.steps:
            saved.stopped = "done"
        return {"status": "completed", "answer": answer}

    def _run(self, request):
        turn = begin(self, request)
        while turn.calls < self.max_steps and turn.actions < turn.max_actions:
            if admit(self, turn) or observe(self, turn) or decide(self, turn) or absorb(self, turn) or act(self, turn):
                break
        return close(self, turn)
