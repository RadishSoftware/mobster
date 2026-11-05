"""A long task's run-wide state (DESIGN §13.1): the plan, budget, notebook and models, and the run's spend and time."""

import math
import threading
import time

from . import context as ctx_text
from .notebook import Notebook, cut

# longform_progress is journaled like every event, and the app reads only the newest: one at most every 5 s while
# nothing changes (a 10-minute run journals about 120), at once on a new phase and on every 10th item.
PROGRESS_INTERVAL = 5.0
STORE_EVERY_ITEMS = 10
WRAP_UP_SHARE = 0.8
COVERED_GRACE_TURNS = 2        # turns the model may take after the budget is covered before the run ends
# A run that makes no progress ends early with a partial report (review of PR #150, P1-1): the fake Calendar that
# ignored app switches kept a run repeating LAUNCH_APP for 38 turns, until the spend cap.
STALL_TURNS = 6                # turns in a row with no new item, note, step change or screen
REFUSED_IN_A_ROW = 3           # actions refused in a row (a loop, a repeat, a gate)
DRY_MACROS = 2                 # macro returns in a row that saved nothing and say the feed or list is done
DRY_REASONS = frozenset({"stuck", "looping", "not_feed", "end", "refused"})
MAX_RULES, MAX_RULE_CHARS = 12, 160    # the conversation's standing rules a long run ranks against

_sessions = {}
_lock = threading.Lock()


def for_run(run_id):
    """The run's LongSession, or None (not a long run, or it ended)."""
    with _lock:
        return _sessions.get(run_id)


def bind(session):
    with _lock:
        _sessions[session.run_id] = session
    return session


def unbind(run_id):
    with _lock:
        return _sessions.pop(run_id, None)


class LongSession:
    # The prompt text of a long task (context.py), for FrontierAgent: the engine reaches it through the session.
    context = ctx_text

    def __init__(self, run_id, request, plan, budget, *, models=None, interactive=True, focus=None,
                 preferences=(), ledger=None, cap_usd=None, emit=None, frames=None, store=None, journal=None,
                 notebook=None, spent_offset=0.0, phone_offset=0.0, clock=time.monotonic, awake=None):
        self.run_id, self.request, self.plan, self.budget = run_id, request, dict(plan or {}), budget
        self.models, self.interactive = models, bool(interactive)
        self.focus = focus
        self.preferences = list(preferences or ())
        self.ledger, self.cap_usd = ledger, cap_usd
        self.frames, self.store, self.journal = frames, store, journal
        self.clock = clock
        self.started = clock()
        self.awake = awake
        steps = [{**s, "state": s.get("state") or "next"} for s in (self.plan.get("steps") or ())]
        self.notebook = notebook or Notebook(run_id, request, self.plan.get("kind", "feed"), focus, self.preferences,
                                             steps)
        if notebook is None and self.notebook.steps:
            self.notebook.steps[0]["state"] = "now"
            self.notebook.steps[0]["started_turn"] = 0
        self.spent_offset, self.phone_offset = float(spent_offset or 0.0), float(phone_offset or 0.0)
        self._sink = emit or (lambda event: None)
        self._agent = None
        self._first_action = None
        self._released = None
        self._waited = 0.0
        self._last_progress = -PROGRESS_INTERVAL
        self._last_tenth = 0
        self._phase = "navigating"
        self._pool = None
        self._digester = None
        self._step_notes = {}
        self._covered_turns = 0
        # Progress (P1-1): the work signature at the last progress, the screens seen since, and the streaks.
        self._work = None
        self._screens = set()
        self._stall_turns = 0
        self._refused_row = 0
        self._dry_macros = []
        self.stalled = None           # why the run stopped making progress, once it has
        self.turn = 0
        self.bundle = None            # the app on screen at the latest turn (an item without its own takes it)
        self.apps_by_turn = {}
        self.epochs = ctx_text.Epochs()
        self.compactor = ctx_text.Compactor()
        self.report = None
        self.closed = False
        self.lock = threading.RLock()

    def attach(self, agent):
        """The frontier running this session: its emit (masked), its mask, its stop and steering."""
        self._agent = agent
        self.notebook.mask = self.mask

    def emit(self, event):
        """An event through FrontierAgent.emit (masked, timed) once attached, else straight to the run's events."""
        agent = self._agent
        if agent is not None:
            agent._emit(event)
        else:
            self._sink(event)

    def mask(self, text):
        agent = self._agent
        return agent._mask(text) if agent is not None and isinstance(text, str) else text

    def should_yield(self):
        """Why a macro should return now: "stopped" | "stop_after" | "paused" | "steer" | "budget" | None."""
        agent = self._agent
        if agent is not None:
            try:
                if agent._stopping():
                    return "stopped"
                if agent._stop_after_asked():
                    return "stop_after"
                if agent._pause_asked():
                    return "paused"
                steering = getattr(agent, "steering", None)
                if steering is not None and steering.pending():
                    return "steer"
            except Exception:  # noqa: BLE001 -- can't tell: return to the model
                return "stopped"
        if self.covered() is not None:
            return "budget"
        return None

    def spent_usd(self):
        """The run's spend: every metered call (navigation, extraction, digest, synthesis, hedges), plus what a
        picked-up run spent before."""
        nano = getattr(self.ledger, "cost_nanodollars", 0) if self.ledger is not None else 0
        return round(nano / 1e9 + self.spent_offset, 6)

    def reserve_usd(self):
        """What the report will cost (costs.synth_estimate on the kept items so far, at least the budget's)."""
        from .costs import synth_estimate
        model = getattr(getattr(self.models, "synth", None), "model", None) or (self.plan.get("models") or {}).get(
            "report") or "claude-sonnet-5-5"
        kept = self.notebook.watched()
        target = 0 if self.budget.strict else (self.budget.items or 0)
        return synth_estimate(max(kept, target), model)

    def phone_seconds(self):
        """Seconds the run has held the phone: from its first action to now (or the release), less the time it
        waited for the person (approvals, pauses), plus a picked-up run's earlier time."""
        if self._first_action is None:
            return round(self.phone_offset, 2)
        end = self._released if self._released is not None else self.clock()
        return round(max(0.0, end - self._first_action - self._waited) + self.phone_offset, 2)

    def mark_action(self):
        if self._first_action is None:
            self._first_action = self.clock()
            self.notebook.phone["first_action_at"] = round(self._first_action - self.started, 2)

    def add_wait(self, seconds):
        if seconds and seconds > 0 and self._first_action is not None:
            self._waited += float(seconds)
            self.notebook.phone["waited_s"] = round(self._waited, 2)

    def budget_left(self):
        kept = self.notebook.watched()
        items = None if self.budget.items is None or self.budget.strict else max(0, self.budget.items - kept)
        usd = (self.cap_usd - self.spent_usd() - self.reserve_usd()) if self.cap_usd is not None else float("inf")
        return {"items": items,
                "phone_seconds": round(max(0.0, self.budget.phone_seconds_cap - self.phone_seconds()), 2),
                "usd": round(usd, 4) if usd != float("inf") else None}

    def covered(self):
        """Why the budget is used up ("items", "phone_time", "spend"), or None."""
        kept = self.notebook.watched()
        if self.budget.items and not self.budget.strict and kept >= self.budget.items:
            return "items"
        if self.phone_seconds() >= self.budget.phone_seconds_cap:
            return "phone_time"
        if self.cap_usd is not None and self.spent_usd() + self.reserve_usd() >= self.cap_usd:
            return "spend"
        return None

    def advice(self):
        covered = self.covered()
        if covered == "items":
            return "The items are covered: wrap up now with DONE."
        if covered == "phone_time":
            return "The phone time is used up: wrap up now with DONE."
        if covered == "spend":
            return "The spend cap is reached: wrap up now with DONE."
        if self.cap_usd is not None and self.spent_usd() + self.reserve_usd() >= WRAP_UP_SHARE * self.cap_usd:
            return "Spend is nearly at the cap: wrap up."
        if self.budget.items and not self.budget.strict:
            return "Wrap up when the items are covered."
        return "Keep going until the time is used, then wrap up."

    def final_reason(self):
        """The frontier's reason to end the loop now without another model call, or None."""
        covered = self.covered()
        if covered is None:
            self._covered_turns = 0
            return self.stall_reason()
        if covered in ("phone_time", "spend"):
            return covered
        self._covered_turns += 1
        return covered if self._covered_turns > COVERED_GRACE_TURNS else None

    def _work_signature(self):
        book = self.notebook
        with book.lock:
            return (tuple(sorted((book.counters or {}).items())), len(book.facts),
                    tuple((step.get("id"), step.get("state")) for step in book.steps))

    def _progress(self, screen):
        """One turn's progress: a new item, note or step state resets the count, and so does a screen not seen since
        the last of those (navigating); the same screens again with nothing new count toward STALL_TURNS."""
        work = self._work_signature()
        if work != self._work:
            self._work, self._screens, self._stall_turns = work, {screen}, 0
        elif screen not in self._screens:
            self._screens.add(screen)
            self._stall_turns = 0
        else:
            self._stall_turns += 1

    def on_refused(self):
        """An action was refused (a loop, a repeat, a gate): REFUSED_IN_A_ROW of them in a row end the run."""
        self._refused_row += 1

    def on_acted(self, noop=False):
        """An action was taken. A LAUNCH_APP of the app already in front is a no-op: it keeps the refusal streak."""
        if not noop:
            self._refused_row = 0

    def on_macro(self, op, reason, saved):
        """A macro (WATCH_FEED, READ_EACH) returned: ``saved`` items, stopped for ``reason``."""
        if saved or reason not in DRY_REASONS:
            self._dry_macros = []
            if saved:
                self._refused_row = 0
            return
        self._dry_macros.append((op, reason))

    def stall_reason(self):
        """"stuck" (and ``stalled`` says why) once the run has stopped making progress; else None."""
        if self.stalled is None:
            if self._stall_turns >= STALL_TURNS:
                self.stalled = f"nothing new in {self._stall_turns} turns"
            elif self._refused_row >= REFUSED_IN_A_ROW:
                self.stalled = f"{self._refused_row} actions in a row were refused"
            elif len(self._dry_macros) >= DRY_MACROS:
                op, reason = self._dry_macros[-1]
                self.stalled = (f"{op} found nothing new {len(self._dry_macros)} times in a row "
                                f"({'the list ended' if reason == 'end' else reason.replace('_', ' ')})")
        if self.stalled is None:
            return None
        return "list_end" if self._list_done() else "stuck"

    def _list_done(self):
        """The macro said the list ended (every row read) and something was saved: the run is complete, not stuck."""
        return bool(self._dry_macros) and all(reason == "end" for _, reason in self._dry_macros[-DRY_MACROS:]) \
            and bool(self.notebook.counters.get("kept"))

    def _frame_id(self, frame):
        if not frame or self.frames is None:
            return None
        try:
            return self.frames.put(self.run_id, bytes(frame))
        except Exception:  # noqa: BLE001
            return None

    def current_step_id(self):
        step = self.notebook.current_step()
        return step["id"] if step else None

    def add_item(self, item, *, frame=None):
        """Add a captured item (dedupe and counters in the notebook); emits longform_item. Returns the stored Item."""
        if item.step is None:
            item.step = self.current_step_id()
        if not item.app:
            item.app = self.bundle
        if not item.at:
            item.at = round(self.clock() - self.started, 2)
        item.frame_id = self._frame_id(frame) or item.frame_id
        self.mark_action()
        stored = self.notebook.add_item(item)
        self._item_event(stored, "skipped" if stored.skip else "captured")
        self.progress(self._phase if self._phase in ("watching", "reading") else "watching")
        if stored.n % STORE_EVERY_ITEMS == 0:
            self.save()
        self._poke_digest()
        return stored

    def update_item(self, item_id, *, extract=None, skip=None, frame=None):
        if extract is not None:
            extract = dict(extract)
            if extract.get("ad") is True and skip is None:
                skip = "ad"
            elif extract.get("relevant") is False and self.focus and skip is None:
                skip = "off_topic"
        frame_id = self._frame_id(frame)
        stored = self.notebook.update_item(item_id, extract=extract, skip=skip, frame_id=frame_id)
        if stored is None:
            return None
        self._item_event(stored, "skipped" if stored.skip else ("extracted" if stored.extract else "captured"))
        self._poke_digest()
        return stored

    def extract_failed(self, item_id):
        self.notebook.extract_failed(item_id)

    def _item_event(self, item, status):
        extract = item.extract or {}
        self.emit({"event": "longform_item", "itemId": item.id, "n": item.n, "kind": item.kind, "status": status,
                   "skip": item.skip, "source": dict(item.source), "summary": item.summary() or None,
                   "tags": list(extract.get("tags") or [])[:8], "relevant": extract.get("relevant"),
                   "frameId": item.frame_id, "step": item.step})

    def add_fact(self, text, item_ids=(), kind="fact", source="model"):
        fact = self.notebook.add_fact(text, item_ids, kind=kind, source=source, turn=self.turn)
        if fact is None:
            return None
        if kind != "step_result":
            self.emit({"event": "longform_finding", "findingId": fact.id, "text": fact.text,
                       "itemIds": list(fact.item_ids), "kind": "insight" if kind == "insight" else
                       "theme" if kind == "theme" else "fact"})
        step = self.current_step_id()
        if step is not None and source == "model":
            self._step_notes.setdefault(step, []).append(fact.text)
        self._poke_digest()
        return fact

    def progress(self, phase, *, label=None, force=False):
        """longform_progress, at most 2 a second (a change of phase always goes out)."""
        now = self.clock()
        c = self.notebook.counters
        tenth = c["kept"] + c["off_topic"]
        changed = phase != self._phase or (tenth // 10 != self._last_tenth // 10)
        self._phase = phase
        if not force and not changed and now - self._last_progress < PROGRESS_INTERVAL:
            return
        self._last_progress = now
        self._last_tenth = tenth
        target = None if self.budget.strict else self.budget.items
        left = max(0.0, self.budget.phone_seconds_cap - self.phone_seconds())
        self.emit({"event": "longform_progress", "phase": phase, "stepId": self.current_step_id(),
                   "items": {"seen": c["seen"], "kept": c["kept"], "ads": c["ads"], "repeats": c["repeats"],
                             "offTopic": c["off_topic"], "watched": c["kept"] + c["off_topic"], "target": target},
                   "phoneSeconds": round(self.phone_seconds(), 1), "phoneSecondsLeft": round(left, 1),
                   "costUsd": round(self.spent_usd(), 4), "label": label})

    def extract_pool(self):
        """The run's extraction pool (feed's extract.ExtractPool), made on first use."""
        with self.lock:
            if self._pool is None:
                from .extract import ExtractPool
                self._pool = ExtractPool(self, self.models.extract if self.models is not None else None)
            return self._pool

    def drain(self, timeout=20.0):
        """Wait for the extraction pool (at most ``timeout`` s); returns the items still pending."""
        pool = self._pool
        if pool is None:
            return 0
        try:
            return int(pool.drain(timeout=timeout) or 0)
        except Exception:  # noqa: BLE001
            return 0

    def _poke_digest(self):
        if self.models is None or getattr(self.models, "digest", None) is None or self.closed:
            return
        with self.lock:
            if self._digester is None:
                from .digest import Digester
                self._digester = Digester(self)
        self._digester.poke()

    def on_screen(self, bundle):
        """The screen is in ``bundle`` (an item read without its own app takes it)."""
        if bundle:
            if bundle != getattr(self, "bundle", None):
                self._subrun_for(bundle)
            self.bundle = bundle

    def _subrun_for(self, bundle):
        run = getattr(self, "run", None)
        apps = {a.get("bundleId"): a.get("name") for a in (self.plan or {}).get("apps") or () if isinstance(a, dict)}
        start = getattr(run, "subrun_start", None)
        if not callable(start) or len(apps) < 2 or bundle not in apps:
            return
        self._finish_subrun()
        try:
            self._subrun = start(app=apps[bundle] or bundle, bundle_id=bundle, title=f"{apps[bundle] or bundle}")
        except Exception:  # noqa: BLE001 -- rows are a view; the run goes on without them
            self._subrun = None

    def _finish_subrun(self, outcome="done"):
        run, open_ = getattr(self, "run", None), getattr(self, "_subrun", None)
        if open_ is None or not callable(getattr(run, "subrun_finish", None)):
            return
        counts = {"read": self.notebook.counters.get("kept", 0)}
        try:
            run.subrun_finish(open_, outcome=outcome, counts=counts)
        except Exception:  # noqa: BLE001
            pass
        self._subrun = None

    def on_turn(self, turn, app_name, history=None, bundle=None, screen=None):
        """The frontier starts turn ``turn`` in ``app_name``: compaction of history that left the window, and the
        turn's progress (``screen``: the screen's content fingerprint)."""
        self.on_screen(bundle)
        self._progress((bundle, screen))
        self.turn = turn
        if app_name:
            self.apps_by_turn[turn] = app_name
        if history is not None:
            self.compactor.fold(history, self.notebook, self.apps_by_turn)
        self.progress(self._phase if self._phase != "done" else "navigating")

    def on_reply(self, step_id, turn):
        """The reply's ``step``: moving past a step marks it done (steps jumped over: skipped) and the new one now."""
        if not isinstance(step_id, int) or isinstance(step_id, bool):
            return []
        target = self.notebook.step(step_id)
        current = self.notebook.current_step()
        if target is None or (current is not None and current["id"] == step_id):
            return []
        changes = []
        with self.notebook.lock:
            if current is not None:
                if step_id > current["id"]:
                    changes.append(self._set_step(current, "done", turn))
                    for step in self.notebook.steps:
                        if current["id"] < step["id"] < step_id and step["state"] == "next":
                            changes.append(self._set_step(step, "skipped", turn))
                else:
                    changes.append(self._set_step(current, "next", turn))
            changes.append(self._set_step(target, "now", turn))
        self.save()
        return changes

    def _set_step(self, step, state, turn):
        step["state"] = state
        if state == "now":
            step["started_turn"] = turn
        elif state in ("done", "skipped", "failed"):
            step["ended_turn"] = turn
            notes = self._step_notes.get(step["id"]) or []
            if notes and state == "done":
                result = cut("; ".join(notes), 300)
                step["result"] = result
                self.notebook.add_fact(result, (), kind="step_result", source="step", turn=turn)
        self.notebook.revision += 1
        self.emit({"event": "longform_step", "stepId": step["id"], "state": state, "result": step.get("result")})
        return step["id"], state

    def finish_steps(self, status):
        """The loop ended with ``status``: the step in progress and the ones left are settled."""
        steps = self.notebook.steps
        writeup = steps[-1] if steps and steps[-1].get("app") is None and len(steps) > 1 else None
        for step in steps:
            if step is writeup or step["state"] in ("done", "skipped", "failed"):
                continue
            if status == "completed":
                self._set_step(step, "done", self.turn)
            elif step["state"] == "now":
                self._set_step(step, "failed" if status in ("error", "blocked") else "skipped", self.turn)
            else:
                self._set_step(step, "skipped", self.turn)
        if writeup is not None and writeup["state"] == "next":
            self._set_step(writeup, "now", self.turn)

    def finish_writeup(self, ok=True):
        steps = self.notebook.steps
        if steps and steps[-1].get("app") is None and steps[-1]["state"] == "now":
            self._set_step(steps[-1], "done" if ok else "failed", self.turn)

    def checkpoint(self):
        """The checkpoint's ``longform`` key (DESIGN §4.3)."""
        seconds = self.phone_seconds()
        self.notebook.phone["cumulative_seconds"] = seconds
        return {"planId": self.plan.get("id"), "step": self.current_step_id(),
                "counters": dict(self.notebook.counters), "phoneSeconds": seconds}

    def capture_contract(self, contract, *, final=False):
        """Persist what the task contract can show, separately from plan-step progress."""
        from . import receipts
        entry = receipts.snapshot(self.notebook, contract, final=final)
        if entry is not None:
            receipts.put(self.notebook, entry)
            self.save()
        return entry

    def release_phone(self):
        """The loop is done with the phone: phone_released, the awake lease let go, the notebook saved."""
        with self.lock:
            if self._released is not None:
                return False
            self._released = self.clock()
        self._finish_subrun()
        self.notebook.phone["released_at"] = round(self._released - self.started, 2)
        self.notebook.phone["cumulative_seconds"] = self.phone_seconds()
        if self.awake is not None:
            try:
                self.awake.release(self.run_id)
            except Exception:  # noqa: BLE001
                pass
        self.emit({"event": "phone_released", "phoneSeconds": round(self.phone_seconds(), 1)})
        self.progress("writing", force=True)
        self.save()
        return True

    @property
    def released(self):
        return self._released is not None

    def save(self, report=None, markdown=None):
        """The notebook (and the report) to the journal, on the store's writer thread."""
        self.notebook.phone["cumulative_seconds"] = self.phone_seconds()
        if self.store is None or self.journal is None:
            return
        try:
            self.store.submit(self.journal, self.run_id, self.notebook, report=report, markdown=markdown)
        except Exception:  # noqa: BLE001 -- a journal that can't take it never breaks the run
            pass

    def close(self):
        """Stop the digest and the pool, write the notebook, and leave the registry."""
        self.closed = True
        digester, self._digester = self._digester, None
        if digester is not None:
            digester.stop()
        pool = self._pool
        if pool is not None:
            try:
                pool.close()
            except Exception:  # noqa: BLE001
                pass
        for client in (getattr(self.models, "digest", None), getattr(self.models, "synth", None),
                       getattr(self.models, "synth_fallback", None)):
            close = getattr(client, "close", None)
            if callable(close):
                close()
        self.save()
        unbind(self.run_id)


_pickups = {}       # run id -> what the long-run pick-up hook loaded for it (register.pickup)


def _elapsed(value):
    if type(value) not in (int, float) or value < 0:
        return None
    try:
        seconds = float(value)
    except OverflowError:
        return None
    return seconds if math.isfinite(seconds) else None


def _pickup_phone_seconds(phone, picked, cap):
    """Restore trusted elapsed time; a damaged source consumes the remaining phone budget."""
    checkpoint = _elapsed(picked["phone_seconds"]) if "phone_seconds" in picked else None
    cumulative = _elapsed(phone["cumulative_seconds"]) if "cumulative_seconds" in phone else None
    invalid = ("phone_seconds" in picked and checkpoint is None) or (
        "cumulative_seconds" in phone and cumulative is None)
    legacy = 0.0
    if "cumulative_seconds" not in phone:
        first = phone.get("first_action_at")
        if first is not None:
            end = phone.get("released_at")
            if end is None:
                end = phone.get("last_at")
            if end is None:
                # A crash has no segment end; its checkpoint already excludes approval waits.
                invalid |= checkpoint is None or _elapsed(first) is None or _elapsed(phone.get("waited_s", 0)) is None
            else:
                first, end, waited = _elapsed(first), _elapsed(end), _elapsed(phone.get("waited_s", 0))
                invalid |= first is None or end is None or waited is None or (
                    first is not None and end is not None and waited is not None and end - first < waited)
                if not invalid:
                    legacy = end - first - waited
        elif (phone.get("released_at") is not None or phone.get("last_at") is not None
              or _elapsed(phone.get("waited_s", 0)) is None or phone.get("waited_s", 0) > 0):
            invalid = True
    return cap if invalid else max(legacy, cumulative or 0.0, checkpoint or 0.0)


def start(*, run_id, goal, plan, budget, origin, nav_client, key, inference, ledger, cap_usd, emit, frames=None,
          journal=None, awake=None, model_factory=None, spent_offset=0.0, rules=(), earlier=(), run=None):
    """A bound LongSession for one run: the models routed (models.model_set), the conversation's standing rules
    (``rules``, "only people in Brooklyn") as preferences next to the request, a picked-up run's notebook and budget,
    the awake lease taken, and the ``longform_plan`` event."""
    from ..harness_api import INTERACTIVE
    from .budget import Budget
    from .models import model_set
    from .notebook import Notebook
    from .store import writer
    budget = budget if hasattr(budget, "phone_seconds_cap") else Budget.from_dict(budget)
    kept = list(dict.fromkeys(" ".join(str(rule).split())[:MAX_RULE_CHARS] for rule in rules or ()
                              if str(rule).strip()))[:MAX_RULES]
    models = model_set(nav_client, key, emit=inference, items_hint=budget.items or 60, kind=plan.get("kind", "feed"),
                       factory=model_factory)
    picked = _pickups.pop(run_id, None)
    notebook, phone_offset = None, 0.0
    if picked is not None and picked.get("notebook"):
        notebook = Notebook.from_dict(picked["notebook"])
        notebook.run_id = run_id
        phone = picked["notebook"].get("phone") or {}
        phone_offset = _pickup_phone_seconds(phone, picked, budget.phone_seconds_cap)
        # Segment timestamps belong to the source run. Keep only its cumulative elapsed budget in this run.
        notebook.phone = {"first_action_at": None, "released_at": None, "waited_s": 0.0,
                          "cumulative_seconds": round(phone_offset, 2)}
        spent_offset = max(float(spent_offset or 0.0), float(picked.get("cost_usd") or 0.0))
    session = LongSession(run_id, goal, plan, budget,
                          models=models, interactive=origin in INTERACTIVE,
                          focus=plan.get("focus"), preferences=kept, ledger=ledger, cap_usd=cap_usd, emit=emit,
                          frames=frames, store=writer, journal=journal, notebook=notebook,
                          spent_offset=spent_offset, phone_offset=phone_offset, awake=awake)
    session.notebook.phone["cumulative_seconds"] = session.phone_seconds()
    session.notebook.plan = {k: v for k, v in plan.items() if not str(k).startswith("_")}
    session.run = run  # the Runtime's Run: sub-run rows per app, and the inbox's proposals (orchestrate)
    if earlier:
        # What an earlier long task in this conversation already showed is skipped ("12 new since last time").
        session.notebook.remember_earlier(earlier)
    if picked is not None:
        session.picked_up_from = picked.get("source")
        session.turns_used = int(picked.get("turns") or 0)
    bind(session)
    if awake is not None:
        try:
            awake.acquire(run_id)
        except Exception:  # noqa: BLE001
            pass
    session.emit({"event": "longform_plan", "plan": session.notebook.plan, "revision": 0})
    current = session.notebook.current_step()
    if current is not None:
        session.emit({"event": "longform_step", "stepId": current["id"], "state": "now", "result": None})
    session.progress("navigating", force=True)
    session.save()
    return session
