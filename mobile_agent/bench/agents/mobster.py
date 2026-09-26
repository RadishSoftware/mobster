"""Mobster, in process, exactly as ``evals/harness.py --in-process`` runs it.

Same composition as ``serve``: the exclusive device lease, WDA's current
session, a configured WDA driver, Jev plus the configured Gemini helper, and
the "In <App>: <goal>" framing. The task's answer fields go in as Mobster's
``output_schema``; step and time budgets are the task's.

Two agents:
  ``mobster``       the current pipeline exactly as ``evals/harness.py --in-process``
                    builds it (cold: no replay store, no FrameClock attached, no
                    VisionJudge, Agent defaults).
  ``mobster-next``  the same plus the new features forced on: FrameClock attached
                    in ``on`` mode (settle from the MJPEG stream) and the
                    VisionJudge that compiled visual loops need. Detected, never
                    assumed: a feature not wired in this checkout is reported and,
                    if none is, the agent is skipped with the reason.

Dry-run tasks run Mobster in its own ``loop_mode="dry_run"`` (judge, never act)
when the Agent supports it; the baselines get the same instruction in words.

The only wrapper is the safety monitor around ``driver.execute`` (the single
place Mobster's actions reach the phone). Mobster's targets are AX elements,
so the monitor needs no extra read and adds no measurable time.
"""

import importlib.util
import inspect
import os
import time

from ..safety import ActionIntent, intent_for_element
from .base import Recorder

COMPLETED = ("completed_unverified", "expected_text_visible")
# Mobster's explicit "the fact is not there" outcomes (same set the eval harness grades as abstention,
# minus configuration failures, which are errors rather than decisions).
ABSTAIN_DATA = {"insufficient_evidence", "no_observed_evidence", "unsupported_answer"}
# user_condition_met: the request's own "if it is not shown, say so" applied and the run
# stopped without an answer, which is that request's abstention.
DECLINED = {"blocked", "completion_not_confirmed", "user_condition_met"}
HOST_KEYS = {"HOME", "VOLUME_UP", "VOLUME_DOWN", "BACK"}
LOOP_MODULES = ("mobile_agent.loop_program", "mobile_agent.loop_programs", "mobile_agent.loops",
                "mobile_agent.programs")


def feature_status():
    """Which new features are actually wired in this checkout (never inferred from flags alone)."""
    status = {}
    try:
        from ... import drivers, frame_clock
        source = inspect.getsource(drivers.WDA)
        wired = "frame_clock" in source
        status["frame_clock"] = {"available": bool(wired and frame_clock.available()),
                                 "reason": "wired into WDA driver" if wired else
                                 "frame_clock.py exists but WDA driver does not consume it yet"}
    except Exception as error:
        status["frame_clock"] = {"available": False, "reason": f"{type(error).__name__}"}
    found = next((name for name in LOOP_MODULES if importlib.util.find_spec(name) is not None), None)
    judge = importlib.util.find_spec("mobile_agent.vision_judge") is not None
    # Loop routing itself is on by default in Agent; what the flag adds is the VisionJudge that
    # visual loops need (the server builds one; the in-process harness does not).
    status["loop_programs"] = {"available": bool(found and judge), "module": found,
                               "reason": (f"{found} + vision_judge" if found and judge else
                                          "no loop program / vision judge module in this checkout")}
    return status


class GuardedDriver:
    """Delegates everything to the real driver; ``execute`` passes the safety monitor first."""

    def __init__(self, driver, monitor, recorder):
        self._driver = driver
        self._monitor = monitor
        self._recorder = recorder

    def __getattr__(self, name):
        return getattr(self._driver, name)

    # Mobster also reaches the phone outside ``execute`` (opening a requested URL, activating an app).
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
        # Bound to Safari exactly as Agent._open_requested_url does: a bare /url opens the
        # default browser (Chrome on the bench phone) and every Safari read then fails.
        return self._driver.call("POST", "/url", {"url": url, "bundleId": "com.apple.mobilesafari"}, timeout)

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        from ...drivers import DriverRejection
        from ...evals.oracles import navigation_title
        if operation == "LAUNCH_APP":
            intent = ActionIntent("LAUNCH_APP", app_target=str(target), bundle=snapshot.bundle_id or "")
        elif operation in HOST_KEYS or target is None:
            intent = ActionIntent(operation, bundle=snapshot.bundle_id or "")
        else:
            title = ""
            try:
                title = navigation_title(snapshot)
            except Exception:
                pass
            intent = intent_for_element(operation, target, snapshot, text=text or "", title=title)
        verdict = self._monitor.check(intent)
        if verdict.blocked:
            # Refused before dispatch: Mobster treats this as "nothing happened" and the
            # cancel callback ends the run at its next boundary.
            raise DriverRejection("unsupported_or_stale_target")
        self._recorder.action(operation, label=intent.label[:80] or None, app=intent.app_target or None)
        return self._driver.execute(operation, target, snapshot, text=text, timeout=timeout)


class MobsterAgent:
    name = "mobster"
    kind = "mobster"

    def __init__(self, *, env_file=None, features=False, replay_dir=None, helper=True):
        self.env_file = env_file
        self.features = features
        self.replay_dir = replay_dir
        self.helper = helper
        if features:
            self.name = "mobster-next"

    def config(self):
        from ...frame_clock import frame_clock_mode
        return {"agent": self.name, "features": feature_status() if self.features else None,
                "frame_clock": "on (attached)" if self.features else "not attached (as evals/harness.py)",
                "env_frame_clock_default": frame_clock_mode(),
                "replay": bool(self.replay_dir), "helper_model": os.environ.get("TEXT_MODEL"),
                "jev_model": os.environ.get("TYPESAFE_MODEL")}

    def available(self):
        if self.env_file:
            from ...config import load_env_file
            load_env_file(self.env_file)
        if not os.environ.get("TYPESAFE_API_KEY"):
            return False, "Jev is not configured (TYPESAFE_API_KEY missing from the env file)"
        if self.features:
            status = feature_status()
            live = [name for name, item in status.items() if item["available"]]
            if not live:
                return False, "no new feature is wired yet: " + "; ".join(
                    f"{name}: {item['reason']}" for name, item in status.items())
        return True, "ok"

    def run(self, task, ctx):
        from ...agent import Agent
        from ...catalog import APPS
        from ...compose import build_models, build_target_driver, close_all
        from ...gemini import configured as helper_configured
        from ...journal import Lease
        from ...replay import ReplayStore

        recorder = Recorder(ctx)
        run = recorder.run
        run.config = self.config()
        events = []
        trace = []

        def emit(event):
            event = dict(event)
            t = time.monotonic()
            kind = event.get("event")
            if kind == "inference_finished":
                recorder.model_call({"latency_ms": event.get("latency_ms"),
                                     "prompt_tokens": (event.get("usage") or {}).get("input_tokens"),
                                     "output_tokens": (event.get("usage") or {}).get("output_tokens"),
                                     "cost_usd": (event["cost_nanodollars"] / 1e9
                                                  if isinstance(event.get("cost_nanodollars"), int) else None)})
            elif kind == "decision":
                recorder.step()
            events.append({"event": kind, "t_ms": round((t - ctx.started) * 1000, 1)})
            if len(trace) < TRACE_EVENTS and kind not in QUIET_EVENTS:
                trace.append(_compact(event, round((t - ctx.started) * 1000)))

        app = next(a for a in APPS if a["bundleId"] == task.bundle)
        saved_env = os.environ.get("MOBSTER_FRAME_CLOCK")
        if getattr(ctx, "artifacts", ""):
            os.environ["MOBSTER_DEBUG_CROPS"] = os.path.join(ctx.artifacts, "crops")
        lease = driver = model = helper = None
        try:
            if self.features:
                os.environ["MOBSTER_FRAME_CLOCK"] = "on"
            lease = Lease.device(ctx.wda_url)
            driver = build_target_driver(wda_url=ctx.wda_url, session=ctx.session)
            if self.features:
                from ...frame_clock import attach_frame_clock
                attach_frame_clock(driver, wda_url=ctx.wda_url, session=ctx.session, mode="on")
            driver.call("POST", "/wda/apps/activate", {"bundleId": app["bundleId"]})
            guarded = GuardedDriver(driver, ctx.monitor, recorder)
            model, helper = build_models(helper=self.helper and helper_configured(), on_inference=emit)
            options = {}
            accepted = inspect.signature(Agent.__init__).parameters
            if task.dry_run and "loop_mode" in accepted:
                # The product's own dry-run mode: judge and log, never act.
                options["loop_mode"] = "dry_run"
            if len(task.bundles) > 1 and "allowed_bundles" in accepted:
                # What the product does when a user adds apps to a task: the other apps are
                # launchable (LAUNCH_APP) instead of reachable only through the Home Screen.
                options["allowed_bundles"] = frozenset(task.bundles)
            if self.features and "vision_judge" in accepted:
                from ...compose import build_vision_judge
                options["vision_judge"] = build_vision_judge(enabled=helper is not None, on_inference=emit)
            run.config["agent_options"] = sorted(options)
            agent = Agent(guarded, model, helper, emit=emit, max_steps=task.max_steps,
                          max_seconds=max(1, ctx.remaining()), cancelled=lambda: ctx.monitor.stopped,
                          replay_store=ReplayStore(self.replay_dir) if self.replay_dir else None, **options)
            result = agent.run(f"In {app['name']}: {task.goal}", execute=True,
                               output_schema=task.answer_schema(),
                               output_format="json" if task.answer else "auto")
        except Exception as error:
            result = {"status": "error", "reason": f"{type(error).__name__}: {error}"}
        finally:
            close_all(model, helper, driver)
            if lease is not None:
                lease.close()
            if self.features:
                if saved_env is None:
                    os.environ.pop("MOBSTER_FRAME_CLOCK", None)
                else:
                    os.environ["MOBSTER_FRAME_CLOCK"] = saved_env
        _save_replay(ctx, task, result, run.config)
        status, answer, abstained = map_result(result, task, ctx.monitor.stopped)
        run.status, run.answer, run.abstained = status, answer, abstained
        run.reason = str(result.get("reason") or "")[:300]
        run.detail = {"mobster_status": result.get("status"), "data_status": result.get("data_status"),
                      "mobster_actions": result.get("actions"), "mobster_decisions": result.get("decisions"),
                      "events": _event_counts(events), "trace": trace}
        return run


def _save_replay(ctx, task, result, config):
    """The answer stage's inputs, for offline replay of extraction and verification
    (calibrating the answer verifier needs its evidence, not only its scores)."""
    if not getattr(ctx, "artifacts", "") or not task.answer or not isinstance(result.get("evidence"), dict):
        return
    try:
        import json
        from pathlib import Path
        folder = Path(ctx.artifacts)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "answer-stage.json").write_text(json.dumps({
            "task": task.id, "goal": task.goal, "schema": task.answer_schema(),
            "status": result.get("status"), "data": result.get("data"), "citations": result.get("citations"),
            "data_status": result.get("data_status"), "evidence": result["evidence"],
            "jev_model": config.get("jev_model")}, ensure_ascii=False))
    except (OSError, TypeError, ValueError):
        pass  # Diagnostics never change an attempt.


def map_result(result, task, blocked=False):
    status = result.get("status")
    data = result.get("data")
    data_status = result.get("data_status")
    if blocked:
        return "unsafe_stopped", None, False
    if data is None and task.answer and status not in ("error", "timeout", "stopped") and (
            data_status in ABSTAIN_DATA or (status in DECLINED and data_status == "not_extracted")):
        return "abstained", None, True
    if status in COMPLETED:
        return "completed", data if isinstance(data, dict) else None, False
    if status == "timeout":
        return "timeout", None, False
    if status == "max_steps":
        return "step_budget", None, False
    if status == "error":
        return "error", None, False
    return "gave_up", None, False


# Per-attempt diagnosis: every event's small scalar fields, so a failure is
# explained by its record instead of a re-run. Snapshots and payloads are dropped.
TRACE_EVENTS = 160
QUIET_EVENTS = {"inference_started", "latency_trace"}


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
