"""Runtime's limits and permissions: ask before acting, spending caps and a long task's plan."""

import os
from pathlib import Path
from types import SimpleNamespace as NS

from . import engines, harness_api, orchestrate, permissions, usage
from .api_errors import APIError
from .permissions import project_policy
from .wda.video import DEFAULT_QUALITY, VIDEO_DETAIL, VIDEO_FPS

# Where a task's own working folder is the project it belongs to: `mobster run` and the terminal UI.
LOCAL_ORIGINS = frozenset({"cli", "tui"})

DEFAULT_TASK_COST_LIMIT = 1.00

TASK_LIMIT_RANGE = (0.05, 100.0)

MONTH_LIMIT_RANGE = (1.0, 10000.0)

MONTH_LIMIT_REACHED = ("This month's spend has reached your ${limit:,.2f} limit. Raise it in Settings › Models to "
                       "run more tasks.")

MONTH_LIMIT_HELD = ("The task running now could use the rest of this month's ${limit:,.2f} limit. Wait for it to "
                    "finish, or raise the limit in Settings › Models.")

MONTH_LIMIT_STOPPED = ("The task stopped at this month's ${limit:,.2f} spending limit. Raise it in Settings › "
                       "Models to run more tasks.")

WORKFLOW_LIMIT_STOPPED = ("The task stopped at what was left of its workflow's spending limit. Raise the limit in the "
                          "workflow's Repeat settings to run more.")

USAGE_UNKNOWN = ("Mobster couldn't read this month's spending, so it didn't start the task. Try again in a moment, "
                 "or turn off the monthly limit in Settings › Models.")

BULK_CHANGE_LONG = "A long task reads and reports, and this one only changes things. Run it as a normal task."


def run_policy(run, state_db=None):
    """(the project's policy, the profile the task acts as) for ``run``, or (None, profile) while MOBSTER_POLICY is off
    or the task has no project. A task names its project (``extras["project"]``); `mobster run` and the terminal UI use
    the folder they run in. A policy file that can't be read still asks before every act."""
    extras = run.extras if isinstance(run.extras, dict) else {}
    account = extras.get("account") if isinstance(extras.get("account"), dict) else None
    if not project_policy.enabled():
        return None, account
    project = extras.get("project")
    folder = project.get("path") if isinstance(project, dict) else None
    if folder is None and getattr(run, "origin", None) in LOCAL_ORIGINS:
        folder = project_policy.project_folder()
    if not folder:
        return None, account
    return project_policy.load(folder, project_policy.counts_path(state_db)), account


class LimitsMixin:
    # Elsewhere nobody is watching to answer, so the plan starts as made.
    PLAN_ORIGINS = frozenset({"app", "tui"})

    def apply_caps(self, run, month_cap, cap_usd):
        # Neither raises the per-task limit.
        if month_cap is not None:
            run.cap_usd, run.cap_from_month = month_cap
        if cap_usd is not None and run.mode == "live":
            current = run.cap_usd if run.cap_usd is not None else self.task_cap(run)
            if current is None or cap_usd < current:
                run.cap_usd, run.cap_from_month, run.cap_from_workflow = float(cap_usd), False, True

    def ask_before_acting(self):
        return permissions.from_env().mode != permissions.DONT_ASK

    def permission_gate(self, run):
        override = run.extras.get("permissionMode") if isinstance(run.extras, dict) else None
        phone = getattr(run, "ask_override", None)
        if override is None and isinstance(phone, bool):
            # The phone's own Ask before acting: off is full access, though a one-time code still asks.
            saved = permissions.from_env().mode
            override = (permissions.DONT_ASK if not phone
                        else permissions.ASK_IMPORTANT if saved == permissions.DONT_ASK else saved)
        self.remembering(run)
        # A queued post never rides a "Don't ask again": its Post asks, or the queue's batch approval answers it.
        queued = isinstance(run.extras, dict) and run.extras.get("queueItem") is not None
        policy, profile = run_policy(run, getattr(getattr(self, "config", None), "state_db", None))
        return permissions.gate_for_run(override=override, origin=getattr(run, "origin", None), goal=run.goal,
                                        emit=run.emit, exceptions=None if queued else self.permission_exceptions(),
                                        policy=policy, profile=profile)

    def dry_run(self, body):
        """POST /api/runs with dryRun: the plan for ``body["goal"]`` (dry_run.plan). No run is made, nothing is
        journaled and the phone is never reached."""
        from . import dry_run
        goal = body.get("goal")
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 4000:
            raise ValueError("A dry run takes a goal of at most 4000 characters")
        fields = harness_api.run_fields()
        extras = {name: fields[name][0](body[name], self) for name in ("project", "account")
                  if body.get(name) is not None and name in fields}
        origin = None
        policy, profile = run_policy(NS(extras=extras, origin=origin), getattr(self.config, "state_db", None))
        return dry_run.plan(goal, apps=self.apps(), mode=permissions.from_env().mode, policy=policy, profile=profile,
                            origin=origin, estimator=lambda g, names: self.engine_estimates(g)[engines.SMART])

    def permission_exceptions(self):
        store = getattr(self, "_permission_exceptions", None)
        if store is None:
            from .permissions.exceptions import Exceptions, default_path
            state_db = getattr(self.config, "state_db", None)
            path = Path(state_db).parent / "permission-exceptions.json" if state_db else default_path()
            store = self._permission_exceptions = Exceptions(path)
        return store

    def remembering(self, run):
        exceptions = self.permission_exceptions()
        run.on_remember = lambda spec: permissions.remember_in(exceptions, spec)
        return run

    def asks_before_acting(self, run):
        # Always for a Mobster Remote run, whatever the setting says.
        if getattr(run, "origin", None) in harness_api.FORCED_ASK_ORIGINS:
            return True
        override = getattr(run, "ask_override", None)
        return override if isinstance(override, bool) else self.ask_before_acting()

    @staticmethod
    def video_quality():
        try:
            fps = int(os.environ.get("MOBSTER_VIDEO_FPS", DEFAULT_QUALITY[0]))
        except ValueError:
            fps = DEFAULT_QUALITY[0]
        detail = os.environ.get("MOBSTER_VIDEO_DETAIL", DEFAULT_QUALITY[1])
        return (fps, detail) if fps in VIDEO_FPS and detail in VIDEO_DETAIL else DEFAULT_QUALITY

    @staticmethod
    def task_cost_limit():
        raw = os.environ.get("MOBSTER_TASK_COST_LIMIT", "").strip()
        if raw.casefold() == "off":
            return None
        try:
            value = float(raw) if raw else DEFAULT_TASK_COST_LIMIT
        except ValueError:
            return DEFAULT_TASK_COST_LIMIT
        return value if TASK_LIMIT_RANGE[0] <= value <= TASK_LIMIT_RANGE[1] else DEFAULT_TASK_COST_LIMIT

    @staticmethod
    def monthly_limit():
        raw = os.environ.get("MOBSTER_MONTHLY_LIMIT", "").strip()
        try:
            value = float(raw) if raw else None
        except ValueError:
            return None
        return value if value is not None and MONTH_LIMIT_RANGE[0] <= value <= MONTH_LIMIT_RANGE[1] else None

    def month_usd(self, strict=False):
        # A monthly limit is never checked against a number Mobster couldn't read.
        journal = getattr(self, "journal", None)
        try:
            return round(journal.spend_since(usage.month_start()), 6) if journal is not None else 0.0
        except Exception:
            if strict:
                raise
            return 0.0

    def monthly_cap(self, limit, raised=None):
        try:
            spent = self.month_usd(strict=True)
        except Exception:
            raise APIError(USAGE_UNKNOWN, 503, "usage_unavailable") from None
        if spent >= limit:
            raise APIError(MONTH_LIMIT_REACHED.format(limit=limit), 409, "monthly_limit_reached",
                           monthUsd=spent, monthlyLimitUsd=limit)
        reserved = 0.0
        # A report-only long run still reserves its remaining model spend after releasing the phone.
        for other in self.runs.values():
            if other.finished_at is not None or (other.id not in self.active_runs and not other.phone_finished):
                continue
            cap = getattr(other, "cap_usd", None)
            if cap is not None:
                reserved += max(0.0, cap - (other.cost_usd or 0.0))
        left = limit - spent - reserved
        if left <= 0:
            raise APIError(MONTH_LIMIT_HELD.format(limit=limit), 409, "monthly_limit_reached",
                           monthUsd=spent, monthlyLimitUsd=limit)
        task = self.task_cap(raised=raised)
        return (left, True) if task is None or left < task else (task, False)

    def run_cap(self, run):
        cap = getattr(run, "cap_usd", None)
        return cap if cap is not None else self.task_cap(run)

    def note_limit(self, run, result):
        if (getattr(run, "cap_from_workflow", False) and isinstance(result, dict)
                and result.get("status") == "spend_cap"):
            result["spend_limit"] = "workflow"
            result["reason"] = WORKFLOW_LIMIT_STOPPED
        if (getattr(run, "cap_from_month", False) and isinstance(result, dict)
                and result.get("status") == "spend_cap"):
            limit = self.monthly_limit()
            result["spend_limit"] = "monthly"
            if limit is not None:
                result["reason"] = MONTH_LIMIT_STOPPED.format(limit=limit)
        return result

    def task_cap(self, run=None, *, raised=None):
        limit = self.task_cost_limit()
        if raised is None and run is not None:
            raised = (getattr(run, "longform", None) or {}).get("capUsd")
        if raised is not None:
            limit = raised if limit is None else max(limit, raised)
        caps = [cap for cap in (limit, getattr(self.config, "spend_cap_usd", None)) if cap is not None]
        return min(caps) if caps else None

    def long_task(self, goal, app, extras, origin, pins=None):
        from .longform import enabled
        from .longform import plan as P
        requested = extras.get("longform")
        forced = requested is not None
        if not enabled():
            if forced:
                raise ValueError("Long tasks are turned off on this Mac")
            return None
        if forced and P.bulk_change(goal):
            # "--long 'delete every email'" never becomes a 600-turn run: a long task reads and reports.
            raise ValueError(BULK_CHANGE_LONG)
        apps = P.runtime_apps(self)
        if not forced and pins and len(pins) >= 2:
            return None  # several apps pinned: the orchestrator's plan (plan_for)
        kind = P.detect(goal, apps)
        if kind is None and not forced:
            return None
        requested = requested or {}
        limit = self.task_cap()
        card = P.store_for(self).get(requested["planId"]) if requested.get("planId") else None
        if card is None or requested.get("budget") or requested.get("alternative"):
            card = P.make_plan(goal, apps=apps, limit_usd=limit, nav_model=P.nav_model(),
                               kind=kind or ("feed" if P.FEED_NOUN.search(goal) else "browse"),
                               change=requested.get("budget"), alternative=requested.get("alternative"))
        raise_usd = requested.get("capUsd")
        honoured = None
        if raise_usd is not None:
            proposed = (card.get("cap") or {}).get("proposedUsd") or 0.0
            explicit = getattr(self.config, "spend_cap_usd", None) if origin == "cli" else None
            ceiling = max(proposed, explicit or 0.0)
            if origin not in harness_api.INTERACTIVE:
                raise ValueError("A long task's spending limit can be raised only when you start it yourself, in "
                                 "the Mobster app or the terminal")
            if limit is not None and raise_usd < limit:
                raise ValueError("longform.capUsd can only raise this task's limit")
            if raise_usd > ceiling + 1e-9:
                raise ValueError(f"longform.capUsd can be at most ${ceiling:.2f}, the amount this task's plan proposed")
            honoured = raise_usd
        return {"plan": card, "capUsd": honoured}

    def plan_for(self, run, goal=None):
        goal = run.goal if goal is None else goal
        if getattr(run, "longform", None):
            return orchestrate.long_plan(run.longform["plan"])
        pins = [{"name": pin["name"], "bundleId": pin.get("bundleId")} for pin in run.pinned_apps or ()]
        apps = pins if len(pins) >= 2 else orchestrate.named_apps(
            goal, [app for app in self.apps_for(self.phone_for(run)) if app.get("installed") is not False])
        if len(apps) < 2:
            return None
        return orchestrate.apps_plan(goal, apps, self.asks_before_acting(run))

    def confirm_plan(self, run):
        # None to go on; Skip and no answer end the task with nothing done.
        if run.origin not in self.PLAN_ORIGINS or (run.extras or {}).get("longform") is not None \
                or harness_api.calls_no_model(run.extras):
            # Nobody to ask, a long task started from its own card, or a run that calls no model.
            return None
        goal, said = run.goal, []
        for attempt in range(3):
            plan = self.plan_for(run, goal)
            if plan is None:
                return None
            answer = run.propose_plan(plan)
            if answer.startswith("redirected:") and attempt < 2:
                said.append(answer.split(":", 1)[1])
                goal = f"{run.goal.rstrip('.')}. {' '.join(said)}"
                if getattr(run, "longform", None):
                    # The newest words first: "only 20 Reels" sets the budget over the goal's "40 Reels".
                    self.replan_long(run, f"{' '.join(reversed(said))}. {run.goal}")
                continue
            if answer == "approved" or answer.startswith("choice:"):
                if run.longform:
                    choice = answer.split(":", 1)[1] if answer.startswith("choice:") else None
                    self.replan_long(run, f"{' '.join(reversed(said))}. {run.goal}" if said else run.goal,
                                     alternative=None if choice in (None, "default") else choice,
                                     raise_cost=bool(run.plan_terms.get("allowCost")))
                run.plan = self.plan_for(run, goal) or plan
                for words in said:
                    try:
                        run.steer(words, source="app" if "app" in harness_api.steer_sources(run.origin) else run.origin)
                    except (APIError, ValueError):
                        pass
                return None
            if answer == "stopped":
                return {"event": "result", "status": "stopped", "outcome": "stopped", "independently_verified": False,
                        "reason": "You stopped the task before its plan started. Nothing ran on your iPhone.",
                        "costUsd": run.cost_usd}
            timeout = answer == "timeout"
            return {"event": "result", "status": "approval_timeout" if timeout else "approval_denied",
                    "outcome": "declined", "independently_verified": False, "costUsd": run.cost_usd,
                    "reason": "Nobody started the plan in 10 minutes, so nothing ran on your iPhone." if timeout
                    else "The plan wasn't started. Start a new task with your updated request."
                    if answer.startswith("redirected:")
                    else "You skipped the plan, so nothing ran on your iPhone."}
        return None

    def replan_long(self, run, goal, alternative=None, raise_cost=False):
        from .longform import plan as P
        card = P.make_plan(goal, apps=P.runtime_apps(self), limit_usd=self.task_cap(), nav_model=P.nav_model(),
                           alternative=alternative) or run.longform["plan"]
        cap = run.longform.get("capUsd")
        if raise_cost:
            chosen = next((item for item in card.get("alternatives") or () if item.get("id") == alternative), None)
            proposed = (chosen or {}).get("proposedUsd") or (card.get("cap") or {}).get("proposedUsd")
            if proposed and run.origin in harness_api.INTERACTIVE:
                cap = float(proposed)
        run.longform = {**run.longform, "plan": card, "capUsd": cap}
