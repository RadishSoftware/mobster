"""Mobster's Smart engine (frontier.FrontierAgent) on the bench phone, composed as the app composes it.

``engines.build_frontier`` with SMART_CONFIG's loop and switches (pinned only where unset, so an arm's settings
win), the device lease, WDA's current session with a FrameClock attached, the task's apps, its answer fields as
``output_schema``, and the task's budgets. The answer goes through ``engines.summarize`` exactly as the app's
result does (the DONE turn's data_json when it fits, else one structuring call).

Agents (``SMART_AGENTS``):
  ``mobster-smart``         Claude Sonnet 5.5 on the API (Smart's default model).
  ``mobster-smart-haiku``   Claude Haiku 5.5 on the API.
  ``mobster-smart-codex``   gpt-6.1-sol on the operator's ChatGPT plan through the Codex CLI's sign-in ($0 a call).
  ``mobster-smart-cc``      Sonnet 5.5 through ``claude -p`` (Claude Code's plan): seconds of process start per
                            call, so it is for success comparisons only.
An arm is any of these plus ``+NAME`` settings (``arm_settings``): ``mobster-smart+stream`` sets
MOBSTER_STREAM_DISPATCH=on for that agent's runs only. Settings are environment switches the loop reads.

Every action Smart sends reaches the phone through ``SmartGuardedDriver``: the bench safety monitor sees it first,
including the paths ``execute`` does not cover (a point tap, a long press, write_text, set_value, a clear).

``as_app`` (the real-customer benchmark, bench/real) composes the run as the Mac app does for a person who typed a
request: Ask before acting (the default permission mode) with an approver that records each card and declines it,
so nothing that reaches outside the phone happens; a long request ("skim my Reels for 3 minutes") as a long run
(mobile_agent/longform) with its report; every app the phone has on offer (``inventory``), the task's apps first;
and a task that starts on the Home Screen (``task.start == "home"``) opens no app first.
"""

import os
import time

from ..safety import ActionIntent, intent_for_element, intent_for_point
from .base import Recorder

SMART_MODELS = {
    "mobster-smart": "claude-sonnet-5-5",
    "mobster-smart-haiku": "claude-haiku-5-5",
    "mobster-smart-opus": "claude-opus-5-5",
    "mobster-smart-codex": "codex:gpt-6.1-sol",
    "mobster-smart-codex-luna": "codex:gpt-6-luna",
    "mobster-smart-luna": "gpt-6-luna",
    "mobster-smart-sol": "gpt-6.1-sol",
    "mobster-smart-cc": "claude-cli:claude-sonnet-5-5",
}
# "+name" suffixes: the environment an arm runs with (on top of SMART_CONFIG's switches).
ARM_SETTINGS = {
    "stream": {"MOBSTER_STREAM_DISPATCH": "on"},
    "diffs": {"MOBSTER_SCREEN_DIFFS": "on"},
    "delegate": {"MOBSTER_DELEGATE": "on"},
    "check": {"MOBSTER_DONE_CHECK": "on"},
    "smallshot": {"MOBSTER_SCREENSHOT_DIET": "on"},
    "cards": {"MOBSTER_APP_CARDS": "on"},
    "route": {"MOBSTER_HAIKU_ROUTE": "on"},
}
# A runaway task stops here (dollars at list price); the plan backends cost 0 a call.
TASK_CAP_USD = 0.60
# A long task reads for minutes: its cap, and the most apps a run is offered (server.MAX_SMART_APPS).
LONG_CAP_USD = 1.00
MAX_OFFERED_APPS = 120


def arm_settings(name):
    """(base agent name, {env: value}) for "mobster-smart+diffs+stream"."""
    base, *flags = name.split("+")
    env = {}
    for flag in flags:
        if flag not in ARM_SETTINGS:
            raise ValueError(f"unknown arm setting {flag!r}; choose from {sorted(ARM_SETTINGS)}")
        env.update(ARM_SETTINGS[flag])
    return base, env


# Per-attempt diagnosis: every event's small scalar fields, so a failure is
# explained by its record instead of a re-run. Snapshots and payloads are dropped.
TRACE_EVENTS = 160
QUIET_EVENTS = {"inference_started", "latency_trace"}
HOST_KEYS = {"HOME", "VOLUME_UP", "VOLUME_DOWN", "BACK"}


class SmartGuardedDriver:
    """Delegates everything to the real driver; every way FrontierAgent reaches the phone passes the safety monitor
    first and is recorded."""

    def __init__(self, driver, monitor, recorder):
        self._driver = driver
        self._monitor = monitor
        self._recorder = recorder

    def __getattr__(self, name):
        return getattr(self._driver, name)

    # The agent also reaches the phone outside ``execute`` (opening a requested URL, activating an app).
    # Those go through ``call``/``open_url``; they are classified and recorded the same way.
    RAW_ACTIONS = ("/actions", "/wda/keys", "/wda/pressButton", "/wda/dragfromtoforduration", "/wda/tap",
                   "/wda/touchAndHold", "/wda/doubleTap", "/wda/element")

    def call(self, method, path, body=None, timeout=10):
        from ...drivers import DriverRejection
        if method == "POST" and isinstance(path, str):
            intent = None
            if path == "/url":
                intent = ActionIntent("OPEN_URL", bundle=self._driver._last_bundle or "")
            elif path == "/wda/apps/activate":
                intent = ActionIntent("LAUNCH_APP", app_target=str((body or {}).get("bundleId", "")))
            elif any(path.startswith(prefix) for prefix in self.RAW_ACTIONS):
                intent = ActionIntent("RAW", label=path, bundle=self._driver._last_bundle or "")
            if intent is not None:
                if self._monitor.check(intent).blocked:
                    raise DriverRejection("unsupported_or_stale_target")
                self._recorder.action(intent.operation, app=intent.app_target or None)
        return self._driver.call(method, path, body, timeout)

    def open_url(self, url, timeout=10):
        opener = getattr(self._driver, "open_url", None)
        intent = ActionIntent("OPEN_URL", bundle=getattr(self._driver, "_last_bundle", "") or "")
        if self._monitor.check(intent).blocked:
            from ...drivers import DriverRejection
            raise DriverRejection("unsupported_or_stale_target")
        self._recorder.action("OPEN_URL")
        if callable(opener):
            return opener(url, timeout=timeout)
        # Bound to Safari: a bare /url opens the default browser (Chrome on the bench phone) and every Safari read
        # then fails.
        return self._driver.call("POST", "/url", {"url": url, "bundleId": "com.apple.mobilesafari"}, timeout)

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        from ...drivers import DriverRejection
        if operation == "LAUNCH_APP":
            intent = ActionIntent("LAUNCH_APP", app_target=str(target), bundle=snapshot.bundle_id or "")
        elif operation in HOST_KEYS or target is None:
            intent = ActionIntent(operation, bundle=snapshot.bundle_id or "")
        else:
            intent = intent_for_element(operation, target, snapshot, text=text or "", title=self._title(snapshot))
        verdict = self._monitor.check(intent)
        if verdict.blocked:
            # Refused before dispatch: the agent treats this as "nothing happened" and the cancel callback ends the
            # run at its next boundary.
            raise DriverRejection("unsupported_or_stale_target")
        self._recorder.action(operation, label=intent.label[:80] or None, app=intent.app_target or None)
        return self._driver.execute(operation, target, snapshot, text=text, timeout=timeout)

    def _check(self, intent):
        from ...drivers import DriverRejection
        if self._monitor.check(intent).blocked:
            raise DriverRejection("unsupported_or_stale_target")
        self._recorder.action(intent.operation, label=intent.label[:80] or None)

    def _title(self, snapshot):
        from ...evals.oracles import navigation_title
        try:
            return navigation_title(snapshot)
        except Exception:
            return ""

    def tap_point(self, x, y, snapshot, timeout=10, **kwargs):
        self._check(intent_for_point("TAP", snapshot, x, y, title=self._title(snapshot)))
        try:
            return self._driver.tap_point(x, y, snapshot, timeout=timeout, **kwargs)
        except TypeError:
            return self._driver.tap_point(x, y, snapshot, timeout=timeout)

    def dismiss_at(self, x, y, snapshot, timeout=10):
        """FrontierAgent's DISMISS: a tap on a point meant to be empty, checked as a DISMISS (safety.classify)."""
        self._check(intent_for_point("DISMISS", snapshot, x, y, title=self._title(snapshot)))
        return self._driver.tap_point(x, y, snapshot, timeout=timeout)

    def long_press(self, element, snapshot, timeout=10, **kwargs):
        self._check(intent_for_element("LONG_PRESS", element, snapshot, title=self._title(snapshot)))
        return self._driver.long_press(element, snapshot, timeout=timeout, **kwargs)

    def write_text(self, element, snapshot, text, **kwargs):
        op = "TYPE_SUBMIT" if kwargs.get("submit") else "TYPE"
        self._check(intent_for_element(op, element, snapshot, text=text, title=self._title(snapshot)))
        return self._driver.write_text(element, snapshot, text, **kwargs)

    def set_value(self, element, text, timeout=10, **kwargs):
        self._check(ActionIntent("TAP", label=getattr(element, "label", "") or "",
                                 role=getattr(element, "role", "") or "", bundle=self._driver._last_bundle or ""))
        return self._driver.set_value(element, text, timeout=timeout, **kwargs)

    def clear_text(self, element, timeout=10, **kwargs):
        self._check(ActionIntent("TYPE", label=getattr(element, "label", "") or "",
                                 role=getattr(element, "role", "") or "", bundle=self._driver._last_bundle or ""))
        return self._driver.clear_text(element, timeout=timeout, **kwargs)

    def replace_text(self, element, text, timeout=10, **kwargs):
        self._check(ActionIntent("TYPE", label=getattr(element, "label", "") or "",
                                 role=getattr(element, "role", "") or "", text=text or "",
                                 bundle=self._driver._last_bundle or ""))
        return self._driver.replace_text(element, text, timeout=timeout, **kwargs)


def build_client(model, reasoning):
    from ...frontier import chat_client
    from .plan_clients import plan_client
    return plan_client(model, reasoning) or chat_client(model, reasoning=reasoning)


class SmartAgent:
    kind = "mobster"

    def __init__(self, name="mobster-smart", *, env_file=None, model=None, settings=None, reasoning=None, as_app=False,
                 inventory=None):
        self.name = name
        self.env_file = env_file
        base, env = arm_settings(name)
        self.model = model or SMART_MODELS.get(base)
        if self.model is None:
            raise ValueError(f"unknown Smart agent {base!r}; choose from {sorted(SMART_MODELS)}")
        self.settings = {**env, **(settings or {})}
        self.reasoning = reasoning
        self.as_app = as_app
        self.inventory = dict(inventory or {})

    def config(self):
        from ...engines import SMART_CONFIG
        config = {"agent": self.name, "engine": "smart", "model": self.model,
                  "reasoning": self.reasoning or SMART_CONFIG.reasoning, "settings": dict(self.settings),
                  "switches": dict(SMART_CONFIG.switches)}
        if self.as_app:
            config.update({"as_app": True, "permission_mode": "ask_important", "approvals": "recorded, declined",
                           "apps_offered": len(self.inventory) or "catalog"})
        return config

    def offered_apps(self, task):
        """{bundle: name} the run may open. Bench tasks: the task's apps. As the app: the task's apps first, then every
        app the phone has (the inventory: user apps and the system apps it kept), else the catalog's, at most
        server.MAX_SMART_APPS, as Runtime.smart_apps."""
        from ...catalog import APPS, app_label
        apps = {bundle: app_label(bundle).split(" (")[0] for bundle in task.bundles}
        if not self.as_app:
            return apps
        names = getattr(task, "app_names", None) or {}
        for bundle in task.bundles:
            if names.get(bundle):
                apps[bundle] = names[bundle]
        # The phone's own apps when its inventory was read (system apps it has included), else the catalog's.
        others = self.inventory.items() if self.inventory else ((app["bundleId"], app["name"]) for app in APPS)
        for bundle, name in others:
            apps.setdefault(bundle, name)
        return dict(list(apps.items())[:MAX_OFFERED_APPS])

    def available(self):
        if self.env_file:
            from ...config import load_env_file
            load_env_file(self.env_file)
        if self.model.startswith("codex:"):
            from .plan_clients import CODEX_AUTH
            return (CODEX_AUTH.exists(), "ok" if CODEX_AUTH.exists() else "the Codex CLI is not signed in")
        if self.model.startswith("claude-cli:"):
            import shutil
            return (bool(shutil.which("claude")), "ok" if shutil.which("claude") else "claude CLI not found")
        if self.model.startswith("claude-") and not os.environ.get("ANTHROPIC_API_KEY"):
            return False, "ANTHROPIC_API_KEY missing"
        if self.model.startswith("gpt-") and not os.environ.get("OPENAI_API_KEY"):
            return False, "OPENAI_API_KEY missing"
        return True, "ok"

    def run(self, task, ctx):
        from ...compose import build_target_driver, close_all
        from ...engines import SMART_CONFIG, build_frontier, meter, summarize
        from ...frame_clock import attach_frame_clock
        from ...journal import Lease

        recorder = Recorder(ctx)
        run = recorder.run
        run.config = self.config()
        events, trace, prompts, full = [], [], [], []
        ledger = _LongLedger()  # a long run's spend, which its own budget reads (LongSession.spent_usd)

        def emit(event):
            kind = event.get("event")
            t = time.monotonic()
            ledger.add(event)
            if kind == "inference_finished":
                usage = event.get("usage") or {}
                recorder.model_call({"latency_ms": event.get("latency_ms"), "prompt_tokens": usage.get("input_tokens"),
                                     "output_tokens": usage.get("output_tokens"),
                                     "cost_usd": event.get("estimated_usd")})
                if event.get("purpose") == "decision" and event.get("success"):
                    run.detail.setdefault("decision_latency_ms", []).append(event.get("latency_ms"))
            elif kind == "frontier_decision":
                recorder.step()
            elif kind == "frontier_prompt":
                prompts.append(event)
                return
            full.append({**{k: v for k, v in event.items() if k not in ("snapshot",)},
                         "t_ms": round((t - ctx.started) * 1000, 1)})
            events.append({"event": kind, "t_ms": round((t - ctx.started) * 1000, 1)})
            if len(trace) < TRACE_EVENTS and kind not in QUIET_EVENTS:
                trace.append(_compact(event, round((t - ctx.started) * 1000)))

        # The arm's own settings: set for this run and put back after it, so nothing one arm chose leaks into the
        # next attempt.
        settings = {**self.settings, "MOBSTER_TRACE_IMAGES": "on", "MOBSTER_TRACE_SCREENS": "on"}
        saved = {key: os.environ.get(key) for key in settings}
        os.environ.update(settings)
        lease = driver = client = agent = session = None
        outcome = None
        cards = []
        try:
            lease = Lease.device(ctx.wda_url)
            driver = build_target_driver(wda_url=ctx.wda_url, session=ctx.session)
            attach_frame_clock(driver, wda_url=ctx.wda_url, session=ctx.session, mode="on")
            if not (self.as_app and getattr(task, "start", "app") == "home"):
                driver.call("POST", "/wda/apps/activate", {"bundleId": task.bundle})
            guarded = SmartGuardedDriver(driver, ctx.monitor, recorder)
            client = meter(build_client(self.model, self.reasoning or SMART_CONFIG.reasoning), emit)
            apps = self.offered_apps(task)
            extras = {}
            if self.as_app:
                extras, session = as_app_extras(task, apps, client, self.model, emit, cards, ledger)
            agent = build_frontier(guarded, client, apps=apps, emit=emit, pin=False,
                                   max_cost_usd=LONG_CAP_USD if session is not None else TASK_CAP_USD,
                                   cancelled=lambda: ctx.monitor.stopped, output_schema=task.answer_schema(),
                                   max_steps=task.max_steps or None, max_seconds=max(1, ctx.remaining()),
                                   **extras)
            # The arm's settings are read when the agent is built and while it runs; keep them set until it ends.
            outcome = agent.run(task.goal)
            report = markdown = None
            if session is not None:
                session.release_phone()
                report, markdown = long_report(session, outcome, task.goal, apps)
            summary = summarize(outcome, agent, None, request=task.goal, output_schema=task.answer_schema(),
                                output_format="json" if task.answer else "auto", report=report,
                                markdown=markdown)
            if session is not None:
                run.detail["longform"] = long_detail(session, report)
                run.detail["report_md"] = (markdown or "")[:8000]
        except Exception as error:
            summary = {"status": "error", "reason": f"{type(error).__name__}: {error}"}
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            if session is not None:
                try:
                    session.release_phone()
                    session.close()
                except Exception:  # noqa: BLE001 -- a long run's bookkeeping never hides the run's result
                    pass
            close_all(driver)
            if client is not None and getattr(client, "connection", None) is not None:
                try:
                    client.connection.close()
                except Exception:
                    pass
            if lease is not None:
                lease.close()
        status, answer, abstained = map_smart(summary, outcome, task, ctx.monitor.stopped, asks=self.as_app)
        run.status, run.answer, run.abstained = status, answer, abstained
        run.reason = str(summary.get("reason") or "")[:300]
        if self.as_app:
            run.detail["approvals"] = cards
            # The whole answer as the person reads it (the report's Markdown for a long task), for text checks.
            text = summary.get("data") if isinstance(summary.get("data"), str) else (outcome or {}).get("answer")
            run.detail["answer_text"] = str(text or "")[:8000]
        if outcome:
            if run.cost_usd is not None and outcome.get("cost_usd") is not None:
                run.detail["loop_cost_usd"] = outcome.get("cost_usd")
            run.detail.update({"smart_status": outcome.get("status"), "outcome": summary.get("outcome"),
                               "data_status": summary.get("data_status"), "timing": outcome.get("timing"),
                               "steps": outcome.get("steps"), "usage": outcome.get("usage")})
            run.detail.setdefault("answer_text", str(outcome.get("answer") or "")[:400])
        run.detail.update({"events": _event_counts(events), "trace": trace})
        _save_prompts(ctx, prompts)
        _save_events(ctx, full, summary)
        return run


def as_app_extras(task, apps, client, model, emit, cards, ledger=None):
    """(build_frontier keyword arguments, the long run's session or None) for a run composed as the Mac app does
    (server.Runtime.run_smart): the default permission mode, Ask before acting, with ``record_card`` answering every
    card; and a long request as a long run with its tools."""
    from ...permissions import ASK_IMPORTANT, AppPolicy, Settings, gate_for_run
    try:
        from ... import tracks
    except ModuleNotFoundError as error:  # the open-source tree has no tracks, so every run is an ordinary one
        if error.name != "mobile_agent.tracks":
            raise
        tracks = None
    if tracks is not None:
        tracks.load()  # as Runtime.__init__ does: long tasks (longform.enabled) need their track registered

    def ask(request):
        return record_card(cards, request)

    gate = gate_for_run(override=ASK_IMPORTANT, origin="bench", goal=task.goal, emit=emit,
                        settings=Settings(mode=ASK_IMPORTANT, apps=AppPolicy(access="all")))
    extras = {"approve": ask, "dialog_approve": ask, "permissions": gate, "permission_mode": gate.mode}
    session = long_session(task, apps, client, model, emit, ledger if ledger is not None else _LongLedger())
    if session is not None:
        from types import SimpleNamespace
        from ...frontier import default_skills
        from ...longform import feed, items
        tools = tuple(tool for tool in (feed.factory(SimpleNamespace(run_id=session.run_id)),
                                        items.factory(SimpleNamespace(run_id=session.run_id))) if tool is not None)
        extras.update(longform=session, forced_approve=ask, skills=tuple(default_skills()) + tools)
    return extras, session


# A card that only opens another app by a link (the OPEN_URL skill: "Open this link in Settings?") is what a person
# allows; one into Phone or FaceTime could start a call, so it is declined like every commit.
NAVIGATION_CARDS = frozenset({"open_url"})
CALL_APPS = frozenset({"phone", "facetime"})


def record_card(cards, request):
    """The bench's answer to every approval card: write down what it shows, then decline it; a link that only opens
    another app is allowed."""
    request = request if isinstance(request, dict) else {}
    card = {key: request.get(key) for key in ("kind", "step", "operation", "label", "role", "text", "title", "act",
                                               "app_name", "reason")}
    allow = (card.get("kind") in NAVIGATION_CARDS
             and str(card.get("app_name") or "").strip().casefold() not in CALL_APPS)
    card["answer"] = "approved" if allow else "denied"
    cards.append(card)
    return card["answer"]


def long_session(task, apps, client, model, emit, ledger):
    """A LongSession when the request is a long one (longform.plan.detect over the offered apps), else None."""
    import uuid
    from ...engines import anthropic_key, openai_smart_key
    try:
        from ...longform import enabled, plan as long_plan, session as long_sessions
    except ModuleNotFoundError as error:  # long tasks are Mobster for Mac's (the open-source tree leaves them out)
        if error.name != "mobile_agent.longform":
            raise
        return None
    if not enabled():
        return None
    offered = [{"bundleId": bundle, "name": name} for bundle, name in apps.items()]
    kind = long_plan.detect(task.goal, offered)
    if kind is None:
        return None
    card = long_plan.make_plan(task.goal, apps=offered, limit_usd=LONG_CAP_USD,
                               nav_model=getattr(client, "model", model), origin="bench", kind=kind)
    if card is None:
        return None
    key = anthropic_key() if str(model).startswith("claude-") else openai_smart_key()
    return long_sessions.start(run_id="bench-" + uuid.uuid4().hex[:10], goal=task.goal, plan=card,
                               budget=long_plan.budget_of(card), origin="bench", nav_client=client, key=key,
                               inference=emit, ledger=ledger, cap_usd=LONG_CAP_USD, emit=emit)


class _LongLedger:
    """The long run's spend, as the Runtime's ledger keeps it (the bench's own cost comes from the run's events)."""

    def __init__(self):
        self.cost_nanodollars = 0

    def add(self, event):
        if isinstance(event, dict) and event.get("event") == "inference_finished":
            nano = event.get("cost_nanodollars")
            self.cost_nanodollars += int(nano if nano is not None else round((event.get("estimated_usd") or 0) * 1e9))


def long_report(session, outcome, goal, apps):
    """(report, markdown) of a long run, as the Runtime writes them (synthesis, and an inbox sweep's drafts)."""
    from ...longform import inbox
    from ...longform.synthesis import synthesize
    report, markdown = synthesize(session, (outcome or {}).get("status") or "error", answer=(outcome or {}).get("answer"))
    if inbox.wants_drafts(goal):
        section = inbox.drafts_markdown(session, app_names=apps)
        if section:
            markdown = (markdown or "").rstrip() + "\n\n" + section
    return report, markdown


def long_detail(session, report):
    """What the bench keeps of a long run: phone time, the notebook's counts, spend, and the report's code-scored
    grounding, hygiene and structure (vague_rubric). The notebook itself (what the phone showed) is not kept."""
    from .. import vague_rubric
    notebook = session.notebook.to_dict() if hasattr(session.notebook, "to_dict") else {}
    items = {item["id"]: item for item in notebook.get("items") or () if isinstance(item, dict) and item.get("id")}
    report = report if isinstance(report, dict) else {}
    grounding, _ = vague_rubric.grounding(report, items)
    hygiene, _ = vague_rubric.hygiene(report, items)
    structure, _ = vague_rubric.structure(report)
    try:
        spent = float(session.spent_usd() or 0.0)
    except Exception:  # noqa: BLE001
        spent = None
    budget = session.budget.public() if hasattr(session.budget, "public") else {}
    return {"phone_seconds": round(float(session.phone_seconds() or 0.0), 1), "budget": budget,
            "counters": dict(getattr(session.notebook, "counters", {}) or {}), "usd": spent,
            "rubric": {"grounding": round(grounding, 3), "hygiene": hygiene, "structure": round(structure, 3)},
            "items": len(items), "partial": bool(report.get("partial"))}


def map_smart(summary, outcome, task, blocked=False, asks=False):
    if blocked:
        return "unsafe_stopped", None, False
    raw = (outcome or {}).get("status") or summary.get("status")
    if asks and raw in ("approval_denied", "approval_timeout"):
        # The run stopped at an approval card, which the bench declines: the end a commit task asks for.
        return "approval_denied", None, False
    if raw == "completed":
        data = summary.get("data") if isinstance(summary.get("data"), dict) else None
        return "completed", data if task.answer else None, False
    if raw == "blocked":
        return "gave_up", None, False
    if raw == "max_steps":
        return "step_budget", None, False
    if raw == "timeout":
        return "timeout", None, False
    if raw in ("error", "stopped"):
        return "error", None, False
    return "gave_up", None, False


def _save_events(ctx, events, summary):
    """Every loop event in order (executor steps, refusals, settles, the result), for reading a run by hand."""
    if not getattr(ctx, "artifacts", ""):
        return
    import json
    from pathlib import Path
    try:
        folder = Path(ctx.artifacts)
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / "events.jsonl", "w", encoding="utf-8") as handle:
            for index, event in enumerate(events):
                image = event.get("image")
                if isinstance(image, str) and image.startswith("data:image/jpeg;base64,"):
                    import base64
                    (folder / "screens").mkdir(parents=True, exist_ok=True)
                    name = f"screens/event-{index:03d}.jpg"
                    (folder / name).write_bytes(base64.b64decode(image.split(",", 1)[1]))
                    event = {**event, "image": name}
                handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        (folder / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, default=str, indent=1))
    except (OSError, TypeError, ValueError):
        pass


def _save_prompts(ctx, prompts):
    """Every turn's prompt, screenshot and reply, for offline gates (bench/replay.py, the DELEGATE gate)."""
    if not getattr(ctx, "artifacts", "") or not prompts:
        return
    import base64
    import json
    from pathlib import Path
    try:
        folder = Path(ctx.artifacts)
        (folder / "screens").mkdir(parents=True, exist_ok=True)
        with open(folder / "prompts.jsonl", "w", encoding="utf-8") as handle:
            for event in prompts:
                event = dict(event)
                image = event.pop("image", None)
                if image and image.startswith("data:image/jpeg;base64,"):
                    name = f"prompt-turn-{event.get('step', 0):02d}.jpg"
                    (folder / "screens" / name).write_bytes(base64.b64decode(image.split(",", 1)[1]))
                    event["image"] = f"screens/{name}"
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def _compact(event, t_ms):
    out = {"t": t_ms}
    for key, value in event.items():
        if key in ("snapshot", "usage", "screen", "elements", "data", "citations"):
            continue
        if isinstance(value, str):
            out[key] = value[:160]
        elif isinstance(value, (int, float, bool)) or value is None:
            out[key] = value
        elif isinstance(value, (list, tuple)) and len(value) <= 8 and all(
                isinstance(v, (str, int, float, bool)) for v in value):
            out[key] = [v[:60] if isinstance(v, str) else v for v in value]
    return out


def _event_counts(events):
    counts = {}
    for event in events:
        counts[event["event"]] = counts.get(event["event"], 0) + 1
    return counts
