"""A task's record: its event log, its approval handshake and what it says outside. It knows no phone and no HTTP."""

import itertools
import re
import threading
import time
import uuid
from dataclasses import dataclass, field

from . import failure_line, harness_api, orchestrate, result_blocks, secret_filter, traces
from .api_errors import APIError
from .costs import NANODOLLARS
from .explain import explain
from .journal import JournalError
from .run_executor import CancelToken
from .steering import SteeringQueue

APPROVAL_TIMEOUT_SECONDS = 600
INSTRUCTION_MAX_CHARS = 500
ANSWER_MAX_CHARS = 500
STEER_MAX_CHARS = 2000
APPROVAL_PENDING = "Choose Send or Don't send above, or say what to do instead there."


QUIT_REASON = "Mobster quit while this task was running. Run it again to finish."


def quit_summary(summary):
    out = {**summary, "status": "interrupted", "reason": QUIT_REASON, "interrupted_by": "quit"}
    if out.get("outcome") == "stopped":
        out["outcome"] = "couldnt_finish"
    return out


def preapproved_matches(spec, request):
    if isinstance(spec, dict) and spec.get("kind") == "queue_post":
        from .content_queue.runner import preapproval_matches
        return preapproval_matches(spec, request)
    return result_blocks.preapproval_matches(spec, request)


PHONE_WAIT_KINDS = frozenset({"lock_screen", "passcode_keypad", "unplugged", "wifi_lost"})
PHONE_WAIT_SCAN = 400


def phone_wait(events):
    for event in reversed(events[-PHONE_WAIT_SCAN:]):
        name = event.get("event")
        if name == "handoff_finished":
            return None
        if name != "handoff_waiting":
            continue
        if event.get("kind") not in PHONE_WAIT_KINDS:
            return None
        seconds = event.get("seconds") if isinstance(event.get("seconds"), int) and event["seconds"] > 0 else 120
        return {"kind": event["kind"], "seconds": seconds, "since": event.get("timestamp"),
                "midrun": event.get("midrun") is True}
    return None


CHOICE_DETAIL = " \u2014 "


def clarify_choice(choice):
    # The detail after an em dash tells two people with one first name apart (CR-25).
    label, _, after = str(choice.get("label") or choice["id"]).partition(CHOICE_DETAIL)
    detail = " ".join(str(choice.get("detail") or after).split())[:120]
    return {"id": str(choice["id"])[:40], "label": " ".join(label.split())[:120] or str(choice["id"])[:120],
            **({"detail": detail} if detail else {})}


@dataclass
class Run:
    app: dict
    goal: str
    mode: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    events: list = field(default_factory=list)
    summary: dict | None = None
    image: str | None = None
    image_at: float = 0
    image_lock: threading.Lock = field(default_factory=threading.Lock)
    capture: object = None
    image_captured_at: float | None = None
    idempotency_key: str | None = None
    output_schema: dict | None = None
    output_format: str = "auto"
    allowed_bundles: frozenset | None = None
    journal: object = None
    lease: object = None
    device: object = None
    guard: object = None
    device_id: str | None = None
    device_name: str | None = None
    phone: object = None
    group_id: str | None = None
    # The phone's own rules, kept with the run: a later change never reaches a running task.
    phone_apps: frozenset | None = None
    ask_override: bool | None = None
    stop: CancelToken = field(default_factory=CancelToken)  # a token, so RunExecutor(run.stop) stops with it
    condition: threading.Condition = field(default_factory=threading.Condition)
    # In memory only: it can carry the exact text to be sent, which is never written to the journal.
    approval: dict | None = None
    approval_answer: str | None = None
    approval_deadline: float | None = None
    answered: dict = field(default_factory=dict)
    engine: str | None = None
    cost_nanodollars: int = 0
    cost_known: bool = False
    estimate_usd: float | None = None
    cap_usd: float | None = None
    cap_from_month: bool = False
    cap_from_workflow: bool = False
    reviewed_ok: bool = False
    frames: object = None
    extras: dict = field(default_factory=dict)
    # Detached writing context; never part of public metadata, journal or repr.
    account_context: object = field(default=None, repr=False)
    workflow_id: str | None = field(default=None, repr=False)
    phone_finished: bool = field(default=False, repr=False)
    origin: str = "api"
    # The hosted review demo (remote/review.py) takes no yes to an outside agent's approval: the sentence a yes is
    # refused with. None everywhere else.
    approvals_closed: str | None = None
    steering: SteeringQueue = field(default_factory=SteeringQueue)
    # The agent's masked notes and plan for listeners: in memory only, never journaled.
    private: dict = field(default_factory=dict)
    listeners: object = None
    # A workflow loop waits for the listeners' on_finished, so round two finds what round one taught.
    settled: threading.Event = field(default_factory=threading.Event)
    longform: dict | None = None
    start_on_home: bool = False
    remember_spec: dict | None = None
    on_remember: object = None
    pinned_apps: list | None = None
    plan: dict | None = None
    plan_terms: dict = field(default_factory=dict)
    subrun_open: str | None = None
    subrun_said: dict = field(default_factory=dict)
    subruns_seen: set = field(default_factory=set)
    quitting: bool = False
    # In memory only; a send that doesn't match one exactly asks as usual.
    preapproved: list = field(default_factory=list)
    # Journaled, so an approved item is never approved, and so never sent, twice.
    block_state: dict = field(default_factory=dict)
    # Never the page itself, never a token.
    shares: list = field(default_factory=list)
    # Issues filed from the result, noted before each is sent: "filing" or "unknown" is never sent again.
    issues: list = field(default_factory=list)
    # The MCP client's own name for itself: a label only, never an identity.
    client: str | None = None
    started_at: float | None = None
    taking_back: bool = False
    queue_info: dict | None = None

    def request_approval(self, request, timeout=None, pending_out=None):
        # approved, denied, timeout, stopped, decided, choice:<id>, redirected:<text> or answer:<text>.
        timeout = APPROVAL_TIMEOUT_SECONDS if timeout is None else timeout
        now = time.time()
        pending = {"id": uuid.uuid4().hex[:12], "operation": request["operation"], "label": request["label"],
                   "role": request.get("role") or "", "text": request.get("text"),
                   "app": str(request.get("app_name") or self.app["name"])[:80],
                   "requestedAt": now * 1000, "expiresAt": (now + timeout) * 1000,
                   "actingAs": str(request["acting_as"])[:120] if request.get("acting_as") else None,
                   "recipient": str(request["recipient"])[:120] if request.get("recipient") else None}
        kind = request.get("kind") or "action"
        if kind == "commit":
            with self.condition:
                spec = next((one for one in self.preapproved if preapproved_matches(one, request)), None)
                if spec is not None and not self.stop.is_set() and not self.approvals_closed:
                    spec["used"] = True
                else:
                    spec = None
            if spec is not None:
                # Approved on its review queue item: exactly these words, to this person, once.
                self.emit({"event": "approval_requested", "approval_id": pending["id"], "step": request.get("step"),
                           "operation": pending["operation"], "label": pending["label"],
                           "text_present": pending["text"] is not None, "kind": kind, "title": request.get("title"),
                           "act": request.get("act"), "reason": None, "target": None, "choices": [],
                           "preapproved": spec.get("via") or "review_queue"})
                with self.condition:
                    self._remember(pending["id"], "approved")
                self.emit({"event": "approval_resolved", "approval_id": pending["id"], "decision": "approved",
                           "via": spec.get("via") or "review_queue", "source": dict(spec.get("source") or {})})
                return "approved"
        target = request.get("target")
        pending.update(kind=kind, title=str(request["title"])[:160] if request.get("title") else None,
                       act=str(request["act"])[:40] if request.get("act") else None,
                       reason=" ".join(str(request["reason"]).split())[:120] if request.get("reason") else None,
                       target=({key: float(target[key]) for key in ("x", "y", "w", "h")}
                               if isinstance(target, dict) and all(isinstance(target.get(k), (int, float))
                                                                   for k in ("x", "y", "w", "h")) else None))
        crop = request.get("crop")
        if isinstance(crop, (bytes, bytearray)) and crop and self.frames is not None:
            frame_id = self.frames.put(self.id, crop)
            pending["crop"] = f"/api/runs/{self.id}/frames/{frame_id}" if frame_id else None
        if isinstance(request.get("apps"), list):
            named = [(name, bundle) for name, bundle in itertools.zip_longest(
                request["apps"], request.get("bundles") if isinstance(request.get("bundles"), list) else ())
                if isinstance(name, str) and name]
            pending["apps"] = [name for name, _ in named]
            if isinstance(request.get("bundles"), list):
                pending["appIds"] = [bundle[:200] if isinstance(bundle, str) else None for _, bundle in named]
        if kind in ("loop", "question"):
            choices = [{"id": str(c["id"])[:40], "label": str(c["label"])[:120]}
                       for c in request.get("choices") or () if isinstance(c, dict) and c.get("id")][:6]
            default = request.get("default_choice")
            pending.update(kind=kind, question=str(request.get("question") or "")[:300], choices=choices,
                           defaultChoice=default if any(c["id"] == default for c in choices) else None,
                           limits=request.get("limits"))
        elif kind == "clarify":
            choices = [clarify_choice(c) for c in request.get("choices") or () if isinstance(c, dict)
                       and c.get("id")][:6]
            pending.update(label=str(request["label"])[:80],
                           question=str(request.get("question") or request["label"])[:300], choices=choices,
                           allowText=bool(request.get("allow_text", not choices)))
        elif kind == "practice":
            known = {"right": "That’s right", "skip": "Skip this step", "model": "Let Mobster do it"}
            choices = [{"id": c["id"], "label": known[c["id"]]} for c in request.get("choices") or ()
                       if isinstance(c, dict) and c.get("id") in known][:3]
            practice = request.get("practice") if isinstance(request.get("practice"), dict) else {}
            pending.update(label=str(request["label"])[:80], question=str(request.get("question") or "")[:300],
                           choices=choices, defaultChoice="right" if any(c["id"] == "right" for c in choices) else None,
                           allowTap=True, practice={"step": int(practice.get("step") or 0),
                                                    "of": int(practice.get("of") or 0),
                                                    "purpose": str(practice.get("purpose") or "")[:160],
                                                    "differs": bool(practice.get("differs"))})
        elif kind == "plan":
            shaped = orchestrate.plan(request.get("plan"))
            pending.update(operation="PLAN", label=shaped["summary"][:80], title=shaped["summary"][:160], text=None,
                           plan=shaped, limits=" ".join(shaped["policy"]) or None)
            if shaped.get("long"):
                pending.update(choices=orchestrate.long_choices(shaped["long"]), defaultChoice="default")
            with self.condition:
                self.plan_terms = {}
        elif kind == "proposals":
            items = orchestrate.proposals(request.get("items"))
            count = len(items)
            title = str(request.get("title") or "").strip()[:120] or (
                items[0]["title"] if count == 1 else f"Review {count} drafts")
            pending.update(operation="PROPOSE", label=title[:80], title=title, text=None,
                           act=items[0]["act"] if len({item["act"] for item in items}) == 1 else None,
                           app=items[0]["app"] if len({item["app"] for item in items}) == 1 else "Several apps",
                           items=items)
        pending["explain"] = explain(request)
        spec = request.get("remember") if isinstance(request.get("remember"), dict) else None
        pending["rememberable"] = spec is not None
        pending["rememberLabel"] = str(spec["words"])[:80] if spec and spec.get("words") else None
        if isinstance(pending_out, dict):
            pending_out.clear()
            pending_out.update(pending)
        deadline = time.monotonic() + timeout
        with self.condition:
            self.approval, self.approval_answer, self.remember_spec = pending, None, spec
            self.approval_deadline = deadline
        self.emit({"event": "approval_requested", "approval_id": pending["id"], "step": request.get("step"),
                   "operation": pending["operation"], "label": pending["label"],
                   "text_present": pending["text"] is not None, "kind": kind,
                   "title": pending["title"], "act": pending["act"], "reason": pending["reason"],
                   "target": pending["target"], "explain": explain({**request, "recipient": None}),
                   **({"crop": pending["crop"]} if pending.get("crop") else {}),
                   "choices": [c["id"] for c in pending.get("choices", ())],
                   **({"acting_as": pending["actingAs"]} if pending["actingAs"] else {}),
                   **({"plan": pending["plan"]} if kind == "plan" else {}),
                   **({"items": [orchestrate.public_proposal(item) for item in pending["items"]],
                       "count": len(pending["items"])} if kind == "proposals" else {})})
        with self.condition:
            while self.approval_answer is None and not self.stop.is_set():
                # The monotonic clock stands still while the Mac sleeps: the wall clock the card counts down to wins.
                remaining = min(deadline - time.monotonic(), now + timeout - time.time())
                if remaining <= 0:
                    break
                self.condition.wait(timeout=min(remaining, 1))
            # Stop wins over an answer it raced with: a stopped task never acts, whichever reached it first.
            answer = "stopped" if self.stop.is_set() else self.approval_answer or "timeout"
            self.approval, self.approval_answer = None, None
            self.approval_deadline = None
            tally = {}
            if kind == "proposals":
                # Fail closed: nothing nobody chose goes out, and after Stop nothing does.
                for item in pending["items"]:
                    if item["decision"] is None or answer == "stopped":
                        item["decision"] = "skip"
                tally = {"sent": sum(item["decision"] == "send" for item in pending["items"]),
                         "skipped": sum(item["decision"] == "skip" for item in pending["items"])}
            self._remember(pending["id"], "redirected" if answer.startswith("redirected:") else
                           "answered" if answer.startswith("answer:") else
                           "choice" if answer.startswith("choice:") else answer)
        # A redirect's instruction and a typed answer go to the agent, never into the journal.
        self.emit({"event": "approval_resolved", "approval_id": pending["id"],
                   "decision": "redirected" if answer.startswith("redirected:") else
                   "answered" if answer.startswith("answer:") else answer, **tally})
        return answer

    def _remember(self, approval_id, decision):
        self.answered.pop(approval_id, None)
        self.answered[approval_id] = decision
        while len(self.answered) > 20:
            self.answered.pop(next(iter(self.answered)))

    def approval_decision(self, approval_id):
        with self.condition:
            if self.approval is not None and self.approval.get("id") == approval_id:
                if self.stop.is_set():
                    return "stopped"
                answer = self.approval_answer or ""
                return ("redirected" if answer.startswith("redirected:") else
                        "answered" if answer.startswith("answer:") else
                        "choice" if answer.startswith("choice:") else answer or None)
            return self.answered.get(approval_id)

    def _approval_waiting(self, approval_id):
        if (self.stop.is_set() or self.approval is None or self.approval["id"] != approval_id
                or self.approval_answer is not None):
            return False
        if (self.approval_deadline is None or time.monotonic() >= self.approval_deadline
                or time.time() * 1000 >= self.approval["expiresAt"]):
            self.approval_answer = "timeout"
            self.condition.notify_all()
            return False
        return True

    def _refuse_yes(self, approval_id, approve):
        """A run that ``approvals_closed`` takes no yes (an approval, a choice, a send): the refusal is journaled and
        raised, before anything is decided. A no, a redirect and any answer to a clarifying question go through."""
        if not self.approvals_closed or approve is not True:
            return
        with self.condition:
            pending = self.approval
            if pending is None or pending["id"] != approval_id or pending.get("kind") == "clarify":
                return
        self.emit({"event": "approval_refused", "approval_id": approval_id, "reason": "approvals_closed"})
        raise ValueError(self.approvals_closed)

    def answer_approval(self, approval_id, approve, choice=None, instruction=None, answer=None, remember=False,
                        allow_cost=False):
        """Answer the pending request. ``instruction`` with approve false is a redirect: the task goes on
        with it ("Decline and say what to do instead"), and request_approval returns "redirected:<text>".
        ``answer`` (a clarifying question only, with approve true): the user's typed answer, 1–500 characters;
        request_approval returns "answer:<text>"."""
        self._refuse_yes(approval_id, approve)
        with self.condition:
            if not self._approval_waiting(approval_id):
                return False
            proposals = self.approval is not None and self.approval.get("kind") == "proposals"
            if allow_cost:
                if (self.approval is None or self.approval.get("kind") != "plan" or not approve
                        or not (self.approval.get("plan") or {}).get("long")):
                    raise ValueError("allowCost goes with Start on a long task's plan")
        if proposals:
            if choice is not None or instruction is not None or answer is not None:
                raise ValueError("Proposals take Send or Skip, for some items or for all")
            return self.decide_proposals(approval_id, approve)
        with self.condition:
            # After Stop nothing is answered, though the card shows until the agent's thread wakes.
            if not self._approval_waiting(approval_id):
                return False
            if answer is not None and self.approval.get("kind") == "practice":
                # Where the person tapped on the live view: never sent to the phone by itself.
                found = re.fullmatch(r"\s*(0(?:\.\d+)?|1(?:\.0+)?|\.\d+)\s*,\s*(0(?:\.\d+)?|1(?:\.0+)?|\.\d+)\s*",
                                     str(answer))
                if not approve or choice is not None or instruction is not None or not found:
                    raise ValueError("A practice answer is a tap: x,y in screen fractions from 0 to 1, "
                                     "with approve true")
                self.approval_answer = f"answer:{float(found.group(1)):.4f},{float(found.group(2)):.4f}"
            elif answer is not None:
                text = " ".join(str(answer).split())
                if self.approval.get("kind") != "clarify":
                    raise ValueError("Only a question from Mobster's agent takes a typed answer")
                if not approve or choice is not None or instruction is not None \
                        or not 1 <= len(text) <= ANSWER_MAX_CHARS:
                    raise ValueError("An answer says in 1–500 characters what Mobster's agent asked, with approve true")
                self.approval_answer = f"answer:{text}"
            elif instruction is not None:
                text = " ".join(str(instruction).split())
                if approve or choice is not None or not 1 <= len(text) <= INSTRUCTION_MAX_CHARS:
                    raise ValueError("A redirect declines the action and says in 1–500 characters what to do instead")
                self.approval_answer = f"redirected:{text}"
            elif choice is not None:
                if not approve or choice not in {c["id"] for c in self.approval.get("choices", ())}:
                    raise ValueError("That choice is not one of this question's answers")
                self.approval_answer = f"choice:{choice}"
            else:
                self.approval_answer = "approved" if approve else "denied"
                spec = self.remember_spec if approve and remember and self.approval.get("rememberable") else None
                if spec is not None and callable(self.on_remember):
                    try:
                        self.on_remember(spec)
                    except Exception:
                        pass  # the answer stands; only the remembering failed
            if allow_cost:
                self.plan_terms = {"allowCost": True}
            self.condition.notify_all()
            return True

    def decide_proposals(self, approval_id, approve, items=None, text=None):
        if type(approve) is not bool:
            raise ValueError("approve is true or false")
        self._refuse_yes(approval_id, approve)
        if text is not None:
            text = orchestrate._draft(text, "An edited draft")
            if not text:
                raise ValueError("An edited draft has text")
        with self.condition:
            if not self._approval_waiting(approval_id):
                return False
            if self.approval.get("kind") != "proposals":
                raise ValueError("Only proposals are decided item by item")
            pool = self.approval["items"]
            if items is None:
                chosen = [item for item in pool if item["decision"] is None]
            else:
                by_id = {item["id"]: item for item in pool}
                if (not isinstance(items, list) or not items or len(set(map(str, items))) != len(items)
                        or not all(isinstance(key, str) and key in by_id for key in items)):
                    raise ValueError("items are ids of this batch's proposals, each once")
                chosen = [by_id[key] for key in items]
                if any(item["decision"] is not None for item in chosen):
                    return False
            if not chosen:
                return False
            if text is not None and (not approve or len(chosen) != 1):
                raise ValueError("An edited draft sends exactly one item")
            for item in chosen:
                item["decision"] = "send" if approve else "skip"
                if text is not None and text != item["text"]:
                    item["text"], item["edited"] = text, True
            decided = [(item["id"], item["decision"], item["edited"]) for item in chosen]
        for item_id, decision, edited in decided:
            self.emit({"event": "proposal_decided", "approval_id": approval_id, "item_id": item_id,
                       "decision": decision, "edited": edited})
        with self.condition:
            if (self.approval is not None and self.approval["id"] == approval_id and self.approval_answer is None
                    and all(item["decision"] for item in self.approval["items"])):
                self.approval_answer = "decided"
                self.condition.notify_all()
        return True

    def interpret(self, text, apps=(), read_only=True):
        self.emit(orchestrate.interpretation(text, apps, read_only))

    def propose_plan(self, plan, timeout=None):
        shaped = orchestrate.plan(plan)
        return self.request_approval({"kind": "plan", "operation": "PLAN", "label": shaped["summary"][:80],
                                      "plan": shaped}, timeout=timeout)

    def propose(self, items, title=None, timeout=None):
        pending = {}
        decision = self.request_approval({"kind": "proposals", "operation": "PROPOSE", "label": "Proposals",
                                          "items": items, "title": title}, timeout=timeout, pending_out=pending)
        return {"approvalId": pending["id"], "decision": decision,
                "items": {item["id"]: {"decision": item["decision"], "text": item["text"], "edited": item["edited"]}
                          for item in pending["items"]}}

    def proposal_result(self, approval_id, item_id, status, line=None):
        if status not in orchestrate.PROPOSAL_STATUS:
            raise ValueError("A proposal's result is sent, failed or skipped")
        self.emit({"event": "proposal_result", "approval_id": str(approval_id)[:40], "item_id": str(item_id)[:40],
                   "status": status, "line": orchestrate._text(line, 200, "A result's line", required=False)})

    def subruns_planned(self, subruns):
        if not isinstance(subruns, (list, tuple)) or not 1 <= len(subruns) <= orchestrate.PLAN_APPS:
            raise ValueError(f"Plan 1-{orchestrate.PLAN_APPS} sub-runs")
        rows = []
        for row in subruns:
            if not isinstance(row, dict):
                raise ValueError("A sub-run is a dict")
            rows.append(orchestrate.subrun(row.get("app"), row.get("bundleId"), row.get("title"),
                                           row.get("id") or uuid.uuid4().hex[:8]))
        self.emit({"event": "subruns_planned", "subruns": rows})
        return [row["subrun"] for row in rows]

    def subrun_start(self, app, bundle_id=None, title=None, subrun_id=None, run_id=None):
        row = orchestrate.subrun(app, bundle_id, title, subrun_id or uuid.uuid4().hex[:8], run_id)
        with self.condition:
            if self.subrun_open is not None:
                raise ValueError("Finish the open sub-run first")
            if row["subrun"] in self.subruns_seen and row["subrun"] != subrun_id:
                raise ValueError("Each sub-run has its own id")
            self.subrun_open = row["subrun"]
            self.subruns_seen.add(row["subrun"])
        self.emit({"event": "subrun_started", **row})
        return row["subrun"]

    def subrun_progress(self, subrun_id, line, counts=None):
        shaped = {"line": orchestrate._text(line, 160, "A progress line"), "counts": orchestrate.counts(counts)}
        now = time.monotonic()
        with self.condition:
            if subrun_id not in self.subruns_seen:
                raise ValueError("No sub-run has that id")
            if now - self.subrun_said.get(subrun_id, 0) < 0.5:
                return False
            self.subrun_said[subrun_id] = now
        self.emit({"event": "subrun_progress", "subrun": subrun_id, **shaped})
        return True

    def subrun_finish(self, subrun_id, outcome="done", line=None, counts=None):
        if outcome not in orchestrate.SUBRUN_OUTCOMES:
            raise ValueError("A sub-run ends done, check, couldnt_finish, stopped or skipped")
        shaped = {"line": orchestrate._text(line, 200, "A sub-run's line", required=False),
                  "counts": orchestrate.counts(counts)}
        with self.condition:
            if subrun_id not in self.subruns_seen:
                raise ValueError("No sub-run has that id")
            if self.subrun_open == subrun_id:
                self.subrun_open = None
        self.emit({"event": "subrun_finished", "subrun": subrun_id, "outcome": outcome, **shaped})

    def emit(self, event):
        with self.condition:
            entry = self._append(event)
            if self.listeners is not None:
                # Posting under the lock keeps publication in saved sequence order when two threads emit.
                self.listeners.post("on_event", self, entry)

    def _append(self, event):
        if "_frame" in event:
            # Screens are stored beside the run and referenced by id, never inlined; the caller's dict gets the id too.
            data = event.pop("_frame")
            event["frameId"] = self.frames.put(self.id, data) if self.frames is not None and data else None
        # A call on the person's plan is no spend: History and Home don't read "0¢" for "On your Claude plan".
        if (event.get("event") == "inference_finished" and type(event.get("cost_nanodollars")) is int
                and event.get("billing") != "plan"):
            with self.condition:
                self.cost_nanodollars += event["cost_nanodollars"]
                self.cost_known = True
        with self.condition:
            # The one gate for what a run stores and serves: secrets are masked in the journal and every reader.
            entry = {**secret_filter.redact_deep(event), "seq": len(self.events), "timestamp": time.time() * 1000}
            if self.subrun_open is not None and "subrun" not in entry:
                entry["subrun"] = self.subrun_open
            if self.journal:
                self.journal.append(self.metadata(), entry)
            self.events.append(entry)
            self.condition.notify_all()
        return entry

    def steer(self, text, source="app"):
        if source not in ("app", "tui", "cli", "mcp", "voice", "remote"):
            raise ValueError("Unknown message source")
        allowed = harness_api.steer_sources(self.origin)
        if source not in allowed:
            raise APIError("This task takes messages only where it was started.", 403, "steer_not_allowed")
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= STEER_MAX_CHARS:
            raise ValueError("A message is 1 to 2,000 characters")
        with self.condition:
            if self.finished_at is not None or self.steering.closed:
                raise APIError("This task has finished.", 409, "run_not_active")
            if self.approval is not None and self.approval.get("kind") != "clarify":
                raise APIError(APPROVAL_PENDING, 409, "approval_pending", approvalId=self.approval["id"])
            message = self.steering.put(text, source=source)
        self.emit({"event": "user_message", "id": message.id, "text": message.text, "source": message.source})
        return {"id": message.id, "text": message.text, "source": message.source, "at": message.at * 1000}

    def finish(self, summary):
        if self.finished_at is None:
            unread = self.steering.close()
            if unread:
                try:
                    self.emit({"event": "steer_unread", "ids": [message.id for message in unread]})
                except Exception:
                    pass  # the terminal record below still goes in
        with self.condition:
            if self.finished_at is not None:
                return
            finished_at = time.time()
            if summary.get("status") == "stopped" and self.quitting:
                summary = quit_summary(summary)
            if "deviceKind" not in summary:
                kind = getattr(self.phone, "kind", None) or "usb"
                summary = {**summary, "deviceKind": kind,
                           **({"deviceName": self.device_name} if self.device_name else {})}
            if "blocks" in summary:
                # The one gate for result blocks: bounded and redacted, else the block falls back to its text.
                blocks, problems = result_blocks.validate(summary.get("blocks"))
                summary = {**summary, "blocks": blocks}
                if blocks is None:
                    summary.pop("blocks")
                if problems:
                    summary["blocks_problems"] = [str(p)[:160] for p in problems[:8]]
            status = summary["status"]
            if status in {"queued", "running"}:
                raise ValueError("A terminal run cannot remain active")
            if traces.enabled() and "failure" not in summary:
                failure = failure_line.failure_line(summary, self.events)
                if failure is not None:
                    summary = {**summary, "failure": failure}
            summary = secret_filter.redact_deep(summary)
            status = summary["status"]
            entry = {"event": "run_finished", "status": status, "summary": summary,
                     "seq": len(self.events), "timestamp": finished_at * 1000}
            metadata = {**self.metadata(), "status": status, "summary": summary,
                        "finishedAt": finished_at * 1000}
            try:
                if self.journal:
                    self.journal.finish(metadata, entry)
            except JournalError:
                # Visible, never presented as durable; failed events were not appended, so seq ids stay contiguous.
                summary = {**summary, "persistence_failed": True}
                entry.update(summary=summary, persistence_failed=True)
            self.status, self.summary, self.finished_at = status, summary, finished_at
            self.events.append(entry)
            self.condition.notify_all()

    def metadata(self):
        return {"id": self.id, "app": self.app, "appId": self.app["id"], "appName": self.app["name"],
                "goal": self.goal, "mode": self.mode, "status": self.status,
                "createdAt": self.created_at * 1000,
                "finishedAt": self.finished_at * 1000 if self.finished_at is not None else None,
                "summary": self.summary, "idempotencyKey": self.idempotency_key,
                "outputSchema": self.output_schema, "outputFormat": self.output_format,
                "allowedBundles": sorted(self.allowed_bundles) if self.allowed_bundles is not None else None,
                "engine": self.engine, "costUsd": self.cost_usd,
                "estimateUsd": self.estimate_usd, "reviewedOk": self.reviewed_ok,
                "device": self.device_id, "deviceName": self.device_name,
                "extras": harness_api.public_extras(self.extras), "origin": self.origin,
                "pinnedApps": self.pinned_apps, **({"groupId": self.group_id} if self.group_id else {}),
                **({"blockState": dict(self.block_state)} if self.block_state else {}),
                **({"shares": [dict(item) for item in self.shares]} if self.shares else {}),
                **({"issues": [dict(item) for item in self.issues]} if self.issues else {})}

    @property
    def steerable(self):
        return self.engine == "smart" and self.mode == "live"

    @property
    def cost_usd(self):
        return round(self.cost_nanodollars / NANODOLLARS, 6) if self.cost_known else None

    def public(self, include_events=True):
        with self.condition:
            waiting = phone_wait(self.events) if self.finished_at is None else None
            return {**{k: v for k, v in self.metadata().items() if k != "app"},
                    "approval": dict(self.approval) if self.approval else None,
                    **({"phoneWait": waiting} if waiting else {}),
                    **({"queue": dict(self.queue_info)} if self.queue_info else {}),
                    **({"takingBack": True} if self.taking_back and self.finished_at is None else {}),
                    "steerable": self.steerable,
                    "events": list(self.events) if include_events else [], "eventCount": len(self.events)}

    def row(self):
        value = self.public(include_events=False)
        if isinstance(value["summary"], dict) and "timing" in value["summary"]:
            value["summary"] = {k: v for k, v in value["summary"].items() if k != "timing"}
        return value
