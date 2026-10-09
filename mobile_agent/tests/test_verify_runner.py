"""verify/runner.py on fakes: the settle loop, every verdict-precedence row (§4), the result JSON (§5.3), frames and
their cap, Smart's event mapping, deny_money, and a key-less run that never touches the network. Offline: no Xcode,
simulator, network or key."""

import base64
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from PIL import Image

from mobile_agent import engines
from mobile_agent.tests.test_verify_fixtures import app, node, paywall
from mobile_agent.transport import TransportError
from mobile_agent.verify import runner
from mobile_agent.verify.assertions import parse_assertion
from mobile_agent.verify.checks import CheckError, check_from_dict
from mobile_agent.verify.runner import VerifyRun, check_approver, deny_commits, deny_money, smart_request, verify
from mobile_agent.verify.tree import EVIDENCE_SOURCE_PATH, RETRY_PAUSE

BUNDLE = "dev.mobster.daybreak"
KEY = "sk-test-0000-never-written"
TARGET = SimpleNamespace(udid="00000000-0000-4000-8000-000000000000", name="Mobster · iPhone 17 Pro · iOS 26.4",
                         device_type="iPhone 17 Pro", runtime="iOS 26.4", wda_url="http://127.0.0.1:1",
                         mjpeg_url="http://127.0.0.1:2", xctestrun="/nonexistent/runner.xctestrun")
APP = SimpleNamespace(bundle_id=BUNDLE, name="Daybreak", version="1.0 (1)", path=None)
PLANS = paywall()
TWO_PLANS = paywall(plans=("Monthly", "Annual"))
LOADING = paywall(loading=True)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(0.0, seconds)


class SimError(RuntimeError):
    """The simulator manager's error, duck-typed by ``kind`` as the spec says."""

    def __init__(self, kind, message, fix=""):
        super().__init__(message)
        self.kind, self.fix = kind, fix


class Lease:
    def __init__(self):
        self.target, self.releases = TARGET, 0

    def release(self):
        self.releases += 1


class Manager:
    """The simulator manager. ``pid`` is the app's process: `simctl launch --terminate-running-process` starts a
    new one."""

    def __init__(self, fail=None, app_info=APP, on=None):
        self.fail, self.calls, self.app, self.leases, self.on = dict(fail or {}), [], app_info, [], dict(on or {})
        self.pid = 1000

    def _step(self, name, *args):
        self.calls.append((name,) + args)
        if name in self.on:
            self.on[name]()
        if name in self.fail:
            raise self.fail[name]

    def names(self):
        return [call[0] for call in self.calls]

    def acquire(self, device=None, runtime=None, *, timeout=600):
        self._step("acquire", device, runtime)
        lease = Lease()
        self.leases.append(lease)
        return lease

    def app_info(self, path):
        self._step("app_info", path)
        return SimpleNamespace(**{**vars(self.app), "path": path})

    def installed_app(self, target, bundle):
        self._step("installed_app", bundle)
        return self.app

    def install(self, target, path):
        self._step("install", path)
        return SimpleNamespace(**{**vars(self.app), "path": path})

    def reset(self, target, bundle, level, app_path=None):
        self._step("reset", bundle, level, app_path)

    def launch(self, target, bundle, args=(), env=None):
        self._step("launch", bundle, tuple(args), dict(env or {}))
        self.pid += 1

    def terminate(self, target, bundle):
        self._step("terminate", bundle)

    def open_url(self, target, url):
        self._step("open_url", url)

    def screenshot(self, target, path, *, max_width=None, quality=75):
        self._step("screenshot", Path(path).name, max_width)
        width = max_width or 804
        Image.new("RGB", (width, round(width * 874 / 402)), (240, 240, 245)).save(path, "JPEG", quality=quality)
        return Path(path)

    def restarter(self, target):
        return None


class Driver:
    """WDA as the run uses it: evidence reads (a script of screens; the last repeats), the foreground app, alerts,
    and deep links (POST /url, which fails with ``url_error``)."""

    def __init__(self, screens, foreground=BUNDLE, alert=None, after_flow=None, url_error=None):
        self.screens, self.reads, self.foreground, self.alert = list(screens), 0, foreground, alert
        self.after_flow, self.closed, self.url_error, self.opened = after_flow, False, url_error, []

    def flow_ran(self):
        """What the flow changed: the screens the reads after it see."""
        if self.after_flow is not None:
            self.screens, self.reads = list(self.after_flow), 0

    def call(self, method, path, body=None, timeout=10):
        if path == EVIDENCE_SOURCE_PATH:
            screen = self.screens[min(self.reads, len(self.screens) - 1)]
            self.reads += 1
            if isinstance(screen, BaseException):
                raise screen
            return screen
        if path == "/alert/text":
            if self.alert:
                return self.alert
            raise TransportError("no alert")
        if (method, path) == ("POST", "/url"):
            self.opened.append(dict(body))
            if self.url_error is not None:
                raise self.url_error
            return None
        raise AssertionError(f"unexpected WDA call {method} {path}")

    def active_app(self, timeout=5):
        return self.foreground() if callable(self.foreground) else self.foreground

    def close(self):
        self.closed = True


def jpeg_data_url(color=(10, 120, 200)):
    out = io.BytesIO()
    Image.new("RGB", (640, 1391), color).save(out, "JPEG", quality=70)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()


# Frontier events in the shapes FrontierAgent emits them (frontier.py: frontier_prompt, frontier_action, result).
def recorded_events():
    return [
        {"event": "frontier_contract", "items": ["REPORT: iOS Version"], "dropped": [], "t": 1.2},
        {"event": "frontier_prompt", "step": 0, "text": "Request: In Daybreak ... SCREEN TEXT", "image": jpeg_data_url(),
         "operations": ["TAP", "DONE"], "targets": ["e1", "e2"], "apps": [BUNDLE],
         "out": {"thought": "Open the paywall", "actions": [{"operation": "TAP", "target": "e2"}]}},
        {"event": "frontier_decision", "step": 0, "chunk": 1, "operation": "TAP", "target_label": "Continue",
         "text": None, "thought": "Open the paywall", "latency_ms": 900, "prep_ms": 5, "usage": {}, "t": 2.4},
        {"event": "frontier_action", "step": 0, "index": 0, "operation": "TAP", "target_label": "Continue",
         "changed": True, "text": None, "field": None, "app_name": "Daybreak", "refresh_ms": 0, "exec_ms": 120,
         "settle_ms": 600, "t": 3.1},
        {"event": "frontier_prompt", "step": 1, "text": "second prompt", "image": jpeg_data_url((200, 40, 40)),
         "operations": ["TYPE", "DONE"], "targets": [], "apps": [BUNDLE], "out": {}},
        {"event": "frontier_action", "step": 1, "index": 0, "operation": "TYPE", "target_label": "Promo code",
         "changed": True, "text": "SPRING", "field": "field", "app_name": "Daybreak", "t": 5.0},
        {"event": "frontier_action", "step": 1, "index": 1, "operation": "SWIPE_UP", "target_label": None,
         "changed": False, "text": None, "field": None, "app_name": "Daybreak", "t": 5.8},
        {"event": "frontier_error", "step": 1, "detail": f"a detail that quotes Bearer {KEY}"},
    ]


# Recorded from a real Smart run of the smoke (scripts/verify_smoke.py, 28 Sep 2026, run 20260928-150212-46d9:
# "Open General, then About" in Settings, passed, $0.0246), as events.jsonl kept them, less the metered inference
# lines. frontier_prompt's text and image, which events.jsonl never keeps, are put back as FrontierAgent emits them,
# and its operations list is shortened.
RECORDED_SETTINGS_RUN = [
    {"event": "frontier_tuned", "settings": {"snapshotMaxChildren": 300, "maxTypingFrequency": 960}, "t": 0.01},
    {"event": "frontier_contract", "items": ["READ (Settings): General", "READ (Settings): About"], "dropped": [],
     "t": 1.62},
    {"event": "frontier_prompt", "step": 0, "text": "Request: In Settings: Open General, then About ...",
     "image": "IMAGE0", "operations": ["TAP", "LONG_PRESS", "TYPE"], "targets": ["e1", "e5"],
     "apps": ["com.apple.Preferences"],
     "out": {"thought": "Open General, then About, and verify the destination.",
             "plan": "Tap General, open About, then verify About is displayed.", "notes_add": [],
             "checklist_updates": [{"id": 1, "status": "done", "value": None}],
             "actions": [{"operation": "TAP", "target": "e5", "target_label": None, "text": None, "app": None},
                         {"operation": "TAP", "target": None, "target_label": "About", "text": None, "app": None}],
             "answer": None}},
    {"event": "frontier_contract_update", "step": 0, "items": ["1:done"], "t": 4.03},
    {"event": "frontier_decision", "step": 0, "chunk": 2, "operation": "TAP", "ops": ["TAP", "TAP"],
     "target_label": "General", "text": None, "thought": "Open General, then About, and verify the destination.",
     "latency_ms": 2059, "prep_ms": 319, "usage": {"prompt_tokens": 2206, "completion_tokens": 144, "cached_tokens": 0,
                                                   "cache_write_tokens": 1496}, "t": 4.03},
    {"event": "frontier_action", "step": 0, "index": 0, "operation": "TAP", "target_label": "General", "changed": True,
     "text": None, "field": None, "app_name": "Settings", "refresh_ms": 168, "exec_ms": 535, "settle_ms": 3332,
     "t": 8.07},
    {"event": "frontier_action", "step": 0, "index": 1, "operation": "TAP", "target_label": "About", "changed": True,
     "text": None, "field": None, "app_name": "Settings", "refresh_ms": 0, "exec_ms": 392, "settle_ms": 3011,
     "t": 11.47},
    {"event": "frontier_prompt", "step": 1, "text": "Request: In Settings: Open General, then About ...",
     "image": "IMAGE1", "operations": ["TAP", "LONG_PRESS", "TYPE"], "targets": [], "apps": ["com.apple.Preferences"],
     "out": {"thought": "About is visibly open under General.", "plan": "Complete.", "notes_add": [],
             "checklist_updates": [{"id": 1, "status": "done", "value": None},
                                   {"id": 2, "status": "done", "value": None}],
             "actions": [{"operation": "DONE", "target": None, "target_label": None, "text": None, "app": None}],
             "answer": "Opened Settings > General > About."}},
    {"event": "frontier_contract_update", "step": 1, "items": ["1:done", "2:done"], "t": 14.15},
    {"event": "frontier_decision", "step": 1, "chunk": 1, "operation": "DONE", "ops": ["DONE"], "target_label": None,
     "text": None, "thought": "About is visibly open under General.", "latency_ms": 2491, "prep_ms": 157,
     "usage": {"prompt_tokens": 2489, "completion_tokens": 103, "cached_tokens": 1496, "cache_write_tokens": 0},
     "t": 14.15},
]
RECORDED_OUTCOME = {"status": "completed", "answer": "Opened Settings > General > About.", "steps": 2}


class Client:
    model = "gpt-5.6-sol"

    def __init__(self):
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}

    def complete(self, messages, schema, **kwargs):
        usage = {"prompt_tokens": 10_000, "completion_tokens": 500}
        for key, value in usage.items():
            self.usage[key] += value
        self.usage["calls"] += 1
        return {"actions": []}, usage


class Agent:
    """FrontierAgent's surface: run(request) emits the recorded events and returns an outcome. ``front``: a bundle
    the flow brings to the front, asked of the phone guard as FrontierAgent._check_app does."""

    def __init__(self, driver, client, emit, approve, cancelled, outcome, events, front=None):
        self.driver, self.client, self.emit, self.approve, self.cancelled = driver, client, emit, approve, cancelled
        self.outcome, self.events, self.requests, self.max_seconds = outcome, events, [], 900
        self.front, self.guard = front, None

    def run(self, request):
        self.requests.append(request)
        if self.front is not None and self.guard is not None and not self.guard.allows_app(self.front):
            name = self.front
            result = {"status": "blocked", "answer": None, "code": "blocked_app", "steps": 1, "actions": 1,
                      "reason": f"{name} is on your list of apps Mobster never opens, so it stopped.",
                      "usage": dict(self.client.usage)}
            self.emit({"event": "result", **{k: v for k, v in result.items() if k != "usage"}})
            return result
        self.driver.flow_ran()
        for _ in range(3):
            self.client.complete([], {"properties": {"actions": {}}})
        for event in self.events:
            self.emit(dict(event))
        result = {"steps": 2, "actions": 3, "usage": dict(self.client.usage), **self.outcome}
        self.emit({"event": "result", **{k: v for k, v in result.items() if k != "usage"}})
        return result


class RunnerCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.runs = self.root / ".mobster" / "runs"
        self.clock = Clock()
        self.front = self.watched = None  # () -> (bundle, pid): what GET /wda/activeAppInfo answers
        for patch in (mock.patch.object(runner, "_now", self.clock.time),
                      mock.patch.object(runner, "_pause", self.clock.sleep),
                      mock.patch.object(runner, "front_app", self.front_app)):
            patch.start()
            self.addCleanup(patch.stop)

    def front_app(self, wda_url, timeout=5):
        """runner.front_app: ``self.front`` when a test sets one, else the fakes' (``watch``)."""
        front = self.front or self.watched
        if front is None:
            raise ConnectionRefusedError("no WDA in this test")
        return front()

    def watch(self, driver, manager):
        """The fakes' foreground app: the driver's, in the manager's latest process."""
        self.watched = lambda: (driver.active_app(), manager.pid)

    def check(self, **data):
        data.setdefault("bundle_id", BUNDLE)
        return check_from_dict(data)

    def verify(self, check, driver, *, mode="auto", manager=None, key=None, agent=None, cancelled=None):
        manager = manager or Manager()
        self.watch(driver, manager)
        self.agents = []

        def build_frontier(driver_, client, *, apps, emit, max_cost_usd=None, approve=None, cancelled=None,
                           guard=None, **_):
            made = Agent(driver_, client, emit, approve, cancelled, **(agent or {"outcome": {"status": "completed",
                                                                                   "answer": "Done."},
                                                                       "events": recorded_events()}))
            made.apps, made.max_cost_usd, made.guard = apps, max_cost_usd, guard
            self.agents.append(made)
            return made
        with mock.patch.object(runner, "build_driver", lambda target, m, run_dir, mode_: driver), \
                mock.patch.object(engines, "build_client", lambda key=None: Client()), \
                mock.patch.object(engines, "build_frontier", build_frontier):
            result = verify(check, mode=mode, runs_dir=self.runs, manager=manager, key=key, cancelled=cancelled)
        return result, manager

    def keyless(self, check, driver, manager=None):
        manager = manager or Manager()
        self.watch(driver, manager)
        run = VerifyRun(check, mode="keyless", runs_dir=self.runs, manager=manager)
        with mock.patch.object(runner, "build_driver", lambda target, m, run_dir, mode_: driver):
            run.prepare()
        return run, manager


# -- the settle loop -----------------------------------------------------------------------------------------------

class SettleTests(RunnerCase):
    def settle(self, screens, expect, timeout=5):
        run = VerifyRun(self.check(expect=expect), mode="launch", runs_dir=self.runs, manager=Manager())
        run.driver = Driver(screens)
        results, tree, stable = run._settle(run.check.expect, timeout)
        return [r.ok for r in results], stable, run.driver.reads

    def test_two_agreeing_reads_decide(self):
        self.assertEqual(self.settle([PLANS], [{"text": "Choose your plan"}]), ([True], True, 2))

    def test_a_changing_screen_is_read_again(self):
        oks, stable, reads = self.settle([LOADING, PLANS, PLANS], [{"no_text": "Loading"}])
        self.assertEqual((oks, stable, reads), ([True], True, 3))

    def test_a_screen_that_never_settles_is_decided_at_the_timeout(self):
        ticking = [app(node("StaticText", f"Tick {i}"), node("StaticText", "Plans")) for i in range(40)]
        started = self.clock.now
        oks, stable, reads = self.settle(ticking, [{"text": "Plans"}], timeout=5)
        self.assertEqual((oks, stable), ([True], False))
        self.assertGreaterEqual(self.clock.now - started, 5)
        self.assertEqual(reads, 12)  # 0.3 s, then every 0.5 s to 5 s

    def test_a_failing_assertion_waits_for_the_timeout(self):
        oks, stable, reads = self.settle([TWO_PLANS], [{"count": {"id": "/^plan_/"}, "equals": 3}], timeout=2)
        self.assertEqual((oks, stable), ([False], True))
        self.assertEqual(reads, 6)

    def test_every_assertion_holds_on_one_read(self):
        """no_text Loading holds on one read and text Plans on another; never both on the same read."""
        both = app(node("StaticText", "Plans"), node("StaticText", "Loading"))
        neither = app(node("StaticText", "Nothing yet"))
        oks, stable, _ = self.settle([both, neither] * 20, [{"no_text": "Loading"}, {"text": "Plans"}], timeout=3)
        self.assertNotEqual(oks, [True, True])
        self.assertFalse(stable)

    def test_timeout_zero_is_one_pair(self):
        self.assertEqual(self.settle([PLANS], [{"text": "Nope"}], timeout=0), ([False], True, 2))

    def test_evaluate_never_touches_the_verdict(self):
        run, _ = self.keyless(self.check(expect=[{"text": "Choose your plan"}]), Driver([PLANS]))
        waited = run.evaluate([parse_assertion({"text": "Something else"})], timeout=0)
        self.assertFalse(waited[0].ok)
        self.assertEqual(run.finish()["verdict"], "passed")


# -- verdict precedence (§4) ---------------------------------------------------------------------------------------

class PrecedenceTests(RunnerCase):
    def test_1_usage_steps_with_keyless(self):
        manager = Manager()
        result, _ = self.verify(self.check(steps=["Reach the paywall."]), Driver([PLANS]), mode="launch",
                                manager=manager)
        self.assertEqual((result["verdict"], result["exit_code"], result["reason"]["class"]),
                         ("couldnt_run", 3, "usage"))
        self.assertEqual(manager.calls, [])  # nothing touched the simulator
        self.assertTrue(Path(result["run_dir"], "result.json").is_file())

    def test_1_usage_steps_with_no_key(self):
        with mock.patch.object(engines, "smart_key", lambda env=None: None):
            result, manager = self.verify(self.check(steps=["Reach the paywall."]), Driver([PLANS]))
        self.assertEqual(result["reason"]["class"], "usage")
        self.assertEqual(result["reason"]["fix"], "Set OPENAI_API_KEY or ANTHROPIC_API_KEY for Smart, or let your "
                                                  "coding agent drive through `mobster mcp`.")
        self.assertEqual(manager.calls, [])

    KEY_VARIABLES = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "MOBSTER_SMART_MODEL", "MOBSTER_HELPER_PROVIDER",
                     "TEXT_MODEL", "TEXT_MODEL_API_KEY", "TEXT_MODEL_BASE_URL", "TEXT_MODEL_PROVIDER")

    def test_1_smart_runs_on_either_key_and_says_which_one_is_missing(self):
        # Since #44, Smart runs on gpt-5.6-sol with an OpenAI key or on claude-sonnet-5-5 with only an Anthropic
        # key; MOBSTER_SMART_MODEL naming a model makes its provider's key the one that counts.
        either = ("usage", "These steps need Smart, and no OpenAI or Anthropic key is set.", runner.NO_KEY_FIX)
        cases = [({}, either),
                 ({"OPENAI_API_KEY": KEY}, None),
                 ({"ANTHROPIC_API_KEY": KEY}, None),
                 ({"OPENAI_API_KEY": KEY, "MOBSTER_SMART_MODEL": "claude-sonnet-5-5"},
                  ("usage", "These steps need Smart, and MOBSTER_SMART_MODEL is claude-sonnet-5-5, which needs "
                            "ANTHROPIC_API_KEY, and it isn't set.",
                   "Set ANTHROPIC_API_KEY for Smart on claude-sonnet-5-5, or let your coding agent drive through "
                   "`mobster mcp`.")),
                 ({"ANTHROPIC_API_KEY": KEY, "MOBSTER_SMART_MODEL": "gpt-5.6-sol"},
                  ("usage", "These steps need Smart, and MOBSTER_SMART_MODEL is gpt-5.6-sol, which needs "
                            "OPENAI_API_KEY, and it isn't set.",
                   "Set OPENAI_API_KEY for Smart on gpt-5.6-sol, or let your coding agent drive through "
                   "`mobster mcp`."))]
        for environ, refused in cases:
            with self.subTest(environ=sorted(environ)), mock.patch.dict("os.environ"):
                for name in self.KEY_VARIABLES:
                    os.environ.pop(name, None)
                os.environ.update(environ)
                result, manager = self.verify(self.check(steps=["x"], expect=[{"text": "Choose your plan"}]),
                                              Driver([PLANS]))
                if refused is None:
                    self.assertNotEqual(result["verdict"], "couldnt_run", result["reason"])
                    self.assertEqual(len(self.agents), 1)
                else:
                    reason = result["reason"]
                    self.assertEqual((reason["class"], reason["message"], reason["fix"]), refused)
                    self.assertEqual(manager.calls, [])
                self.assertNotIn(KEY, json.dumps(result))

    def test_1_usage_steps_in_a_keyless_run_without_an_agent(self):
        result, _ = self.verify(self.check(steps=["x"]), Driver([PLANS]), mode="keyless")
        self.assertEqual(result["reason"]["class"], "usage")

    def test_2_model_when_smart_has_no_key(self):
        run = VerifyRun(self.check(steps=["x"], expect=[{"text": "Plans"}]), mode="smart", runs_dir=self.runs,
                        manager=Manager())
        with mock.patch.object(runner, "build_driver", lambda *a: Driver([PLANS])), \
                mock.patch.object(engines, "smart_key", lambda env=None: None):
            run.prepare()
            with self.assertRaises(runner.CouldntRun):
                run.run_smart()
        result = run.finish()
        self.assertEqual((result["verdict"], result["reason"]["class"]), ("couldnt_run", "model"))

    def test_3_the_machine(self):
        for kind in ("environment", "simulator", "wda", "install", "launch", "busy"):
            for step in ("acquire", "installed_app", "launch"):
                with self.subTest(kind=kind, step=step):
                    manager = Manager(fail={step: SimError(kind, f"{kind} broke", f"Fix {kind}.")})
                    result, _ = self.verify(self.check(expect=[{"text": "Plans"}]), Driver([PLANS]),
                                            manager=manager)
                    self.assertEqual((result["verdict"], result["exit_code"]), ("couldnt_run", 3))
                    self.assertEqual(result["reason"], {"class": kind, "message": f"{kind} broke",
                                                        "fix": f"Fix {kind}."})
                    self.assertTrue(all(lease.releases == 1 for lease in manager.leases))

    def test_3_wda_when_the_driver_or_the_reads_fail(self):
        def broken(*args):
            raise ConnectionRefusedError("refused")
        with mock.patch.object(runner, "build_driver", broken):
            result = verify(self.check(expect=[{"text": "Plans"}]), runs_dir=self.runs, manager=Manager())
        self.assertEqual(result["reason"]["class"], "wda")
        dead = Driver([TransportError("reset"), TransportError("reset again")])
        result, _ = self.verify(self.check(expect=[{"text": "Plans"}]), dead)
        self.assertEqual((result["verdict"], result["reason"]["class"]), ("couldnt_run", "wda"))

    def test_3_an_app_the_check_does_not_name(self):
        result, _ = self.verify(self.check(app_path="/x/Other.app", bundle_id="dev.mobster.other"), Driver([PLANS]))
        self.assertEqual(result["reason"]["class"], "install")

    def test_4_smart_errors_name_the_provider_that_failed(self):
        for reason, fix in (("OpenAI HTTP 429 insufficient_quota", "Check your OpenAI key and credit, then run again."),
                            ("Anthropic HTTP 402 billing_error",
                             "Check your Anthropic key and credit, then run again.")):
            with self.subTest(reason=reason):
                result, _ = self.verify(self.check(steps=["x"], expect=[{"text": "Choose your plan"}]),
                                        Driver([PLANS]), key=KEY,
                                        agent={"outcome": {"status": "error", "reason": reason}, "events": []})
                self.assertEqual((result["reason"]["class"], result["reason"]["fix"]), ("model", fix))
        result, _ = self.verify(self.check(steps=["x"], expect=[{"text": "Choose your plan"}]), Driver([PLANS]),
                                key=KEY, agent={"outcome": {"status": "error", "reason": "Anthropic HTTP 503"},
                                                "events": []})
        self.assertEqual(result["reason"]["class"], "model")
        # The agent's own message names the account the way the app does (engines.account_name: Claude).
        self.assertIn("Claude", result["reason"]["message"])

    def test_4_smart_errors(self):
        for reason, klass in (("OpenAI HTTP 401: invalid key", "model"),
                              ("OpenAI HTTP 429 insufficient_quota", "model"),
                              ("ConnectionRefusedError: [Errno 61] Connection refused", "wda"),
                              ("KeyError: 'x'", "internal")):
            with self.subTest(reason=reason):
                result, _ = self.verify(self.check(steps=["x"], expect=[{"text": "Choose your plan"}]),
                                        Driver([PLANS]), key=KEY,
                                        agent={"outcome": {"status": "error", "reason": reason}, "events": []})
                self.assertEqual((result["verdict"], result["reason"]["class"]), ("couldnt_run", klass))

    def test_4_budget_with_a_failure_and_without(self):
        spent = {"outcome": {"status": "budget", "answer": "Ran out."}, "events": []}
        result, _ = self.verify(self.check(steps=["x"], expect=[{"count": {"id": "/^plan_/"}, "equals": 3}]),
                                Driver([TWO_PLANS]), key=KEY, agent=spent)
        self.assertEqual((result["verdict"], result["reason"]["class"]), ("couldnt_run", "budget"))
        self.assertIn("--max-usd", result["reason"]["fix"])
        self.assertEqual(result["assertions"][0]["ok"], False)
        result, _ = self.verify(self.check(steps=["x"], expect=[{"count": {"id": "/^plan_/"}, "equals": 3}]),
                                Driver([TWO_PLANS], after_flow=[PLANS]), key=KEY, agent=spent)
        self.assertEqual(result["verdict"], "passed")

    def test_4_stopped(self):
        run, manager = self.keyless(self.check(expect=[{"text": "Plans"}]), Driver([PLANS]))
        result = run.abort("stopped", "Idle for 15 minutes.")
        self.assertEqual((result["verdict"], result["reason"]["class"]), ("couldnt_run", "stopped"))
        self.assertEqual(manager.leases[0].releases, 1)
        self.assertIs(run.finish(), result)
        stopped = {"outcome": {"status": "stopped", "reason": "Stopped by the user"}, "events": []}
        result, _ = self.verify(self.check(steps=["x"], expect=[{"text": "Plans"}]), Driver([PLANS]), key=KEY,
                                agent=stopped, cancelled=lambda: True)
        self.assertEqual(result["reason"]["class"], "stopped")
        # A "stopped" nobody asked for is the run's own time limit: a timeout, so a failure is class blocked.
        result, _ = self.verify(self.check(steps=["x"], expect=[{"text": "Nope"}]), Driver([PLANS]), key=KEY,
                                agent=stopped)
        self.assertEqual((result["verdict"], result["reason"]["class"]), ("failed", "blocked"))

    def test_4_internal_keeps_the_traceback_in_run_log(self):
        manager = Manager(fail={"launch": ValueError("boom in launch")})
        result, _ = self.verify(self.check(expect=[{"text": "Plans"}]), Driver([PLANS]), manager=manager)
        self.assertEqual(result["reason"]["class"], "internal")
        self.assertNotIn("Traceback", json.dumps(result))
        self.assertNotIn("boom in launch", result["summary"])
        self.assertIn("Traceback", Path(result["run_dir"], "run.log").read_text())

    def test_5_the_app_is_not_in_front(self):
        alert = "Allow “Daybreak” to send you notifications?"
        driver = Driver([PLANS], foreground=lambda: BUNDLE if driver.reads == 0 else "com.apple.springboard",
                        alert=alert)
        result, _ = self.verify(self.check(expect=[{"text": "Choose your plan"}]), driver)
        self.assertEqual((result["verdict"], result["exit_code"], result["reason"]["class"]),
                         ("failed", 1, "app_not_running"))
        self.assertIn("com.apple.springboard", result["reason"]["message"])
        self.assertIn(alert, result["reason"]["message"])
        self.assertEqual(result["alert"], alert)

    def test_5_comes_before_a_failed_assertion_and_before_no_assertions(self):
        for expect in ([{"text": "Nope"}], []):
            with self.subTest(expect=expect):
                result, _ = self.verify(self.check(expect=expect), Driver([PLANS], foreground=lambda: "other.app"))
                self.assertEqual(result["reason"]["class"], "app_not_running")

    def test_6_blocked_and_assertion(self):
        blocked = {"outcome": {"status": "blocked", "answer": "A login wall."}, "events": []}
        result, _ = self.verify(self.check(steps=["x"], expect=[{"text": "Nope"}]), Driver([PLANS]), key=KEY,
                                agent=blocked)
        self.assertEqual((result["verdict"], result["reason"]["class"]), ("failed", "blocked"))
        self.assertIn("A login wall.", result["reason"]["message"])
        for status in ("timeout", "max_steps", "approval_denied"):
            with self.subTest(status=status):
                result, _ = self.verify(self.check(steps=["x"], expect=[{"text": "Nope"}]), Driver([PLANS]), key=KEY,
                                        agent={"outcome": {"status": status}, "events": []})
                self.assertEqual(result["reason"]["class"], "blocked")
        result, _ = self.verify(self.check(expect=[{"count": {"id": "/^plan_/"}, "equals": 3}]), Driver([TWO_PLANS]))
        self.assertEqual((result["verdict"], result["exit_code"], result["reason"]["class"]), ("failed", 1, "assertion"))
        self.assertEqual(result["summary"], "count id=/^plan_/ == 3 failed: found 2: plan_monthly, plan_annual")
        self.assertEqual(result["reason"]["message"], "1 of 1 expectations failed")

    def test_7_no_assertions(self):
        result, _ = self.verify(self.check(), Driver([PLANS]))
        self.assertEqual((result["verdict"], result["exit_code"], result["reason"]["class"]),
                         ("needs_review", 2, "no_assertions"))
        self.assertTrue(result["frames"][-1].endswith("-verdict-ax.jpg"))

    def test_8_held_before_the_flow(self):
        run, _ = self.keyless(self.check(steps=["Open the paywall."], expect=[{"text": "Choose your plan"}]),
                              Driver([PLANS]))
        self.assertEqual(run.baseline, {"taken": True, "held": [True]})
        result = run.finish()
        self.assertEqual((result["verdict"], result["reason"]["class"]), ("needs_review", "held_before_flow"))
        self.assertIn("Add an expectation that only holds after it.", result["reason"]["message"])

    def test_8_does_not_apply_when_something_changed(self):
        driver = Driver([LOADING])
        run, _ = self.keyless(self.check(steps=["Wait for plans."], expect=[{"no_text": "Loading"},
                                                                            {"text": "Choose your plan"}]), driver)
        self.assertEqual(run.baseline["held"], [False, True])
        driver.screens = [PLANS]
        self.assertEqual(run.finish()["verdict"], "passed")

    def test_9_passed(self):
        result, _ = self.verify(self.check(expect=[{"text": "Choose your plan"},
                                                   {"count": {"id": "/^plan_/"}, "equals": 3},
                                                   {"value": {"id": "plan_annual"}, "equals": "$39.99 / year"},
                                                   {"visible": {"label": "Restore Purchases", "role": "button"}},
                                                   {"no_text": "Loading"}]), Driver([PLANS]))
        self.assertEqual((result["verdict"], result["exit_code"], result["reason"]["class"]), ("passed", 0, None))
        self.assertEqual(result["summary"], "5 of 5 expectations held")


# -- the result, the folder and frames ------------------------------------------------------------------------------

class ResultTests(RunnerCase):
    KEYS = ["schema", "run_id", "verdict", "exit_code", "summary", "reason", "mode", "check", "app", "device",
            "assertions", "baseline", "stable", "steps", "agent", "frames", "proof", "alert", "report",
            "draft_check", "run_dir", "repro", "seconds", "timing", "cost_usd", "mobster"]

    def test_the_result_json_field_for_field(self):
        check = self.check(app_path=str(self.root / "Build" / "Daybreak.app"), name="Paywall",
                           expect=[{"text": "Choose your plan"}], open_url="daybreak://paywall")
        driver = Driver([PLANS])
        result, manager = self.verify(check, driver)
        self.assertEqual(list(result), self.KEYS)
        on_disk = json.loads(Path(result["run_dir"], "result.json").read_text())
        self.assertEqual(on_disk, result)
        self.assertEqual(result["schema"], "mobster.verify/1")
        self.assertRegex(result["run_id"], r"^\d{8}-\d{6}-[0-9a-f]{4}$")
        self.assertEqual(result["mode"], "launch")
        self.assertEqual(result["check"], {"name": "Paywall", "steps": [], "expect": [{"text": "Choose your plan"}],
                                           "source": None})
        self.assertEqual(result["app"], {"bundle_id": BUNDLE, "name": "Daybreak", "version": "1.0 (1)",
                                         "path": str(self.root / "Build" / "Daybreak.app")})
        self.assertEqual(result["device"], {"name": TARGET.name, "udid": TARGET.udid, "type": "iPhone 17 Pro",
                                            "runtime": "iOS 26.4"})
        item = result["assertions"][0]
        self.assertEqual(list(item), ["index", "assertion", "text", "ok", "observed", "matches", "frame"])
        self.assertEqual(item["frame"], "frames/02-verdict.jpg")
        self.assertEqual(list(item["matches"][0]), ["id", "label", "value", "role", "rect"])
        self.assertEqual(result["baseline"], {"taken": False, "held": []})
        self.assertIs(result["stable"], True)
        self.assertIsNone(result["agent"])
        self.assertEqual(result["cost_usd"], 0)
        self.assertEqual(result["frames"], ["frames/01-launch.jpg", "frames/02-verdict.jpg",
                                            "frames/03-verdict-ax.jpg"])
        run_dir = Path(result["run_dir"])
        self.assertTrue(run_dir.is_absolute())
        self.assertEqual(result["proof"], [str(run_dir / "frames/03-verdict-ax.jpg"),
                                           str(run_dir / "frames/02-verdict.jpg")])
        for key in ("report", "draft_check"):
            self.assertTrue(Path(result[key]).is_absolute() and Path(result[key]).is_file(), key)
        self.assertEqual(result["repro"], f"mobster verify --check {result['draft_check']}")
        self.assertEqual(set(result["timing"]) - {"simulator", "install", "launch", "flow", "assert", "report"}, set())
        self.assertEqual(result["mobster"], {"version": __import__("mobile_agent").__version__})
        for name in ("frames/01-launch.jpg", "frames/02-verdict.jpg", "frames/03-verdict-ax.jpg", "check.yaml",
                     "report.html", "run.log"):
            self.assertTrue((run_dir / name).is_file(), name)
        # A launch-only run takes no step: no events.jsonl (docs/cli.md).
        self.assertEqual(sorted(p.name for p in run_dir.iterdir()),
                         ["check.yaml", "frames", "report.html", "result.json", "run.log"])
        # The install and reset (reinstall with an .app) and the launch, in order; the deep link goes through WDA
        # with the app's bundle ID (SPEC §6.4), never simctl openurl, which stops at "Open in “Daybreak”?".
        self.assertEqual(manager.names()[:5], ["acquire", "app_info", "reset", "launch", "screenshot"])
        self.assertNotIn("open_url", manager.names())
        self.assertEqual(driver.opened, [{"url": "daybreak://paywall", "bundleId": BUNDLE}])

    def test_resets(self):
        for data, calls in (({"bundle_id": BUNDLE}, [("reset", BUNDLE, "data", None)]),
                            ({"bundle_id": BUNDLE, "reset": "none"}, []),
                            ({"bundle_id": "com.apple.Preferences"}, []),
                            ({"app_path": "/a/Daybreak.app", "reset": "data"},
                             [("install", "/a/Daybreak.app"), ("reset", BUNDLE, "data", None)]),
                            ({"app_path": "/a/Daybreak.app", "reset": "none"}, [("install", "/a/Daybreak.app")])):
            with self.subTest(data=data):
                _, manager = self.verify(check_from_dict({**data, "expect": [{"text": "Choose"}]}), Driver([PLANS]))
                self.assertEqual([c for c in manager.calls if c[0] in ("install", "reset")], calls)

    def test_the_launch_arguments_and_environment(self):
        check = self.check(launch_args=["-DaybreakSkipOnboarding", "YES"], launch_env={"DAYBREAK_SEED": "3"},
                           expect=[{"text": "Choose"}])
        _, manager = self.verify(check, Driver([PLANS]))
        self.assertIn(("launch", BUNDLE, ("-DaybreakSkipOnboarding", "YES"), {"DAYBREAK_SEED": "3"}), manager.calls)

    def test_couldnt_run_writes_every_artifact_it_can(self):
        result, _ = self.verify(self.check(expect=[{"text": "x"}]), Driver([PLANS]),
                                manager=Manager(fail={"acquire": SimError("busy", "All busy.", "Wait.")}))
        run_dir = Path(result["run_dir"])
        self.assertEqual(sorted(p.name for p in run_dir.iterdir()),
                         ["check.yaml", "frames", "report.html", "result.json", "run.log"])
        self.assertEqual(result["frames"], [])
        self.assertEqual(result["device"]["udid"], None)

    def test_ctrl_c_during_the_verdict_read_still_writes_every_artifact(self):
        # Review round 3: a SIGINT in finish() (the settle loop waits the whole --assert-timeout when an
        # expectation fails) left a run folder with no result.json, report.html or check.yaml.
        driver = Driver([PLANS, KeyboardInterrupt()])
        with self.assertRaises(KeyboardInterrupt):
            self.verify(self.check(expect=[{"text": "Nope"}]), driver)
        run_dir, = (self.runs).iterdir()
        self.assertTrue({"check.yaml", "report.html", "result.json", "run.log"} <= {p.name for p in run_dir.iterdir()})
        result = json.loads((run_dir / "result.json").read_text())
        self.assertEqual((result["verdict"], result["reason"]["class"]), ("couldnt_run", "stopped"))

    def test_the_gitignore_is_written_once(self):
        self.verify(self.check(expect=[{"text": "Choose"}]), Driver([PLANS]))
        ignore = self.root / ".mobster" / ".gitignore"
        self.assertEqual(ignore.read_text(), "runs/\nbuild/\ncache/\ntest-results/\n")
        ignore.write_text("mine\n")
        self.verify(self.check(expect=[{"text": "Choose"}]), Driver([PLANS]))
        self.assertEqual(ignore.read_text(), "mine\n")
        elsewhere = self.root / "out"
        VerifyRun(self.check(), mode="launch", runs_dir=elsewhere, manager=Manager())
        self.assertFalse((self.root / ".gitignore").exists())

    def test_the_gitignore_is_written_even_after_runs_exist(self):
        """A run that made .mobster/runs first (a phone driven directly, an older version) never left the repo
        without the .gitignore for good."""
        (self.root / ".mobster" / "runs" / "older-run").mkdir(parents=True)
        VerifyRun(self.check(), mode="launch", runs_dir=self.runs, manager=Manager())
        self.assertEqual((self.root / ".mobster" / ".gitignore").read_text(), "runs/\nbuild/\ncache/\ntest-results/\n")

    def test_the_gitignore_an_earlier_version_wrote_is_brought_up_to_date(self):
        """`mobster test` adds cache/ and test-results/; a file an earlier Mobster wrote, word for word, gains them,
        and the user's own file is never touched."""
        ignore = self.root / ".mobster" / ".gitignore"
        ignore.parent.mkdir(parents=True)
        ignore.write_text("runs/\nbuild/\n")
        VerifyRun(self.check(), mode="launch", runs_dir=self.runs, manager=Manager())
        self.assertEqual(ignore.read_text(), "runs/\nbuild/\ncache/\ntest-results/\n")
        ignore.write_text("runs/\nbuild/\nsecrets.env\n")
        VerifyRun(self.check(), mode="launch", runs_dir=self.runs, manager=Manager())
        self.assertEqual(ignore.read_text(), "runs/\nbuild/\nsecrets.env\n")

    def test_run_folders_are_private(self):
        run = VerifyRun(self.check(), mode="launch", runs_dir=self.runs, manager=Manager())
        self.assertEqual(run.run_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual((run.run_dir / "frames").stat().st_mode & 0o777, 0o700)
        other = VerifyRun(self.check(), mode="launch", runs_dir=self.runs, manager=Manager())
        self.assertNotEqual(other.run_dir, run.run_dir)

    def test_the_draft_check_and_save(self):
        check = self.check(app_path=str(self.root / "Build" / "Daybreak.app"), expect=[{"text": "Choose"}])
        run, _ = self.keyless(check, Driver([PLANS]))
        result = run.finish()
        draft = Path(result["draft_check"]).read_text()
        self.assertIn("path: Build/Daybreak.app", draft)  # relative to the project root
        saved = run.save_check("paywall")
        self.assertEqual(saved, self.root / ".mobster" / "checks" / "paywall.yaml")
        from mobile_agent.verify.checks import load_check
        self.assertEqual(load_check(saved).app_path, check.app_path)
        with self.assertRaises(CheckError):
            run.save_check("Bad Name")

    def test_finish_is_idempotent_and_releases_once(self):
        driver = Driver([PLANS])
        run, manager = self.keyless(self.check(expect=[{"text": "Choose"}]), driver)
        first = run.finish()
        self.assertIs(run.finish(), first)
        self.assertEqual(manager.leases[0].releases, 1)
        self.assertTrue(driver.closed)
        self.assertEqual(run.state, "finished")

    def test_an_abort_while_preparing(self):
        holder = {}
        manager = Manager(on={"launch": lambda: holder.update(result=holder["run"].abort("stopped", "Stopped."))})
        run = VerifyRun(self.check(expect=[{"text": "x"}]), mode="keyless", runs_dir=self.runs, manager=manager)
        holder["run"] = run
        with mock.patch.object(runner, "build_driver", lambda *a: Driver([PLANS])):
            with self.assertRaises(runner.CouldntRun):
                run.prepare()
        self.assertEqual(holder["result"]["reason"]["class"], "stopped")
        self.assertIs(run.finish(), holder["result"])
        self.assertEqual(manager.leases[0].releases, 1)
        self.assertTrue(Path(holder["result"]["run_dir"], "result.json").is_file())

    def test_an_abort_while_preparing_stops_the_webdriveragent_build(self):
        """The first build runs in its own process group for up to 20 minutes: a run stopped while it waits on
        the build kills it (MCP's stop and shutdown abort from another thread)."""
        manager = Manager()
        manager.wda = mock.Mock()
        run = VerifyRun(self.check(expect=[{"text": "x"}]), mode="keyless", runs_dir=self.runs, manager=manager)
        run.state = "preparing"
        run.abort("stopped", "Stopped.")
        manager.wda.cancel.assert_called_once_with()
        ready = VerifyRun(self.check(expect=[{"text": "x"}]), mode="keyless", runs_dir=self.runs,
                          manager=Manager())
        ready.abort("stopped", "Stopped.")  # a manager without a build to stop is fine


class BuildDriverTests(unittest.TestCase):
    def test_only_runs_that_act_get_the_frame_clock(self):
        """The frame clock times each action's settle on the MJPEG stream; a launch-only run takes no action."""
        from mobile_agent import compose, frame_clock
        for mode, attached in (("launch", False), ("keyless", True), ("smart", True)):
            with self.subTest(mode=mode):
                built = SimpleNamespace(tune=lambda **kwargs: None)
                with mock.patch.object(runner, "wda_session", lambda url: "session-1"), \
                        mock.patch.object(compose, "build_target_driver", lambda **kwargs: built), \
                        mock.patch.object(frame_clock, "attach_frame_clock") as attach, \
                        mock.patch.object(engines, "apply_switches"):
                    self.assertIs(runner.build_driver(TARGET, Manager(), "/runs/x", mode), built)
                self.assertEqual(attach.called, attached)
                if attached:
                    self.assertEqual(attach.call_args.kwargs["log_path"], "/runs/x/frameclock.jsonl")
                    self.assertEqual(attach.call_args.kwargs["mjpeg_url"], TARGET.mjpeg_url)


class FrontAppTests(unittest.TestCase):
    def test_the_bundle_and_process_from_active_app_info(self):
        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False
        answers = [{"value": {"bundleId": "com.apple.Preferences", "pid": 4242, "name": ""}, "sessionId": None},
                   {"value": {"bundleId": "com.apple.springboard"}},
                   {"value": {"bundleId": "", "pid": True}}, {"value": None}]
        urls = []

        def urlopen(url, timeout=None):
            urls.append((url, timeout))
            return Response(json.dumps(answers.pop(0)).encode())
        with mock.patch.object(runner.urllib.request, "urlopen", urlopen):
            self.assertEqual(runner.front_app("http://127.0.0.1:8310", timeout=2), ("com.apple.Preferences", 4242))
            self.assertEqual(runner.front_app("http://127.0.0.1:8310"), ("com.apple.springboard", None))
            self.assertEqual(runner.front_app("http://127.0.0.1:8310"), (None, None))
            self.assertEqual(runner.front_app("http://127.0.0.1:8310"), (None, None))
        self.assertEqual(urls[0], ("http://127.0.0.1:8310/wda/activeAppInfo", 2))


class DeepLinkTests(RunnerCase):
    """SPEC §6.4 (amended 28 Sep): `simctl openurl` stops at SpringBoard's "Open in “<App>”?" on iOS 26.4, so a
    run opens links through WDA's POST /url with the app's bundle ID, and uses the manager only when WDA fails."""

    def test_the_check_link_goes_to_wda_with_the_bundle_id(self):
        driver = Driver([PLANS])
        result, manager = self.verify(self.check(open_url="daybreak://paywall", expect=[{"text": "Choose"}]), driver)
        self.assertEqual(result["verdict"], "passed")
        self.assertEqual(driver.opened, [{"url": "daybreak://paywall", "bundleId": BUNDLE}])
        self.assertNotIn("open_url", manager.names())

    def test_the_mcp_link_goes_to_wda_with_the_bundle_id(self):
        driver = Driver([PLANS])
        run, manager = self.keyless(self.check(expect=[{"text": "Choose"}]), driver)
        run.open_url("daybreak://paywall?plan=annual")
        self.assertEqual(driver.opened, [{"url": "daybreak://paywall?plan=annual", "bundleId": BUNDLE}])
        self.assertNotIn("open_url", manager.names())
        with self.assertRaises(CheckError):
            run.open_url("paywall")
        self.assertEqual(len(driver.opened), 1)

    def test_a_wda_failure_falls_back_to_the_manager(self):
        refused = TransportError("WDA rejected command; not retried")
        driver = Driver([PLANS], url_error=refused)
        result, manager = self.verify(self.check(open_url="daybreak://paywall", expect=[{"text": "Choose"}]), driver)
        self.assertEqual(result["verdict"], "passed")
        self.assertEqual(len(driver.opened), 1)
        self.assertIn(("open_url", "daybreak://paywall"), manager.calls)
        self.assertIn("WDA didn't open the link (TransportError); opening it with simctl",
                      Path(result["run_dir"], "run.log").read_text())
        run, manager = self.keyless(self.check(expect=[{"text": "Choose"}]), Driver([PLANS], url_error=refused))
        run.open_url("daybreak://today")
        self.assertEqual(manager.calls[-1], ("open_url", "daybreak://today"))

    def test_a_manager_failure_after_the_fallback_is_the_machines(self):
        manager = Manager(fail={"open_url": SimError("launch", "daybreak://x didn't open", "Check the scheme.")})
        result, _ = self.verify(self.check(open_url="daybreak://x", expect=[{"text": "Choose"}]),
                                Driver([PLANS], url_error=TransportError("no session")), manager=manager)
        self.assertEqual((result["verdict"], result["reason"]["class"]), ("couldnt_run", "launch"))


class RelaunchTests(RunnerCase):
    """`simctl launch --terminate-running-process` replaces an app already in front (a com.apple.* app, --reset
    none), and WDA can name the old process for a moment. Run 20260928-154329-4354 took it for the new one, read
    at once, got "Application com.apple.Preferences is not running" twice and ended couldnt_run (wda)."""

    SETTINGS = SimpleNamespace(bundle_id="com.apple.Preferences", name="Settings", version="1 (1)", path=None)
    NOT_RUNNING = "Failed to get matching snapshot: Application com.apple.Preferences is not running"

    def relaunching(self, old_for, reads_fail_for):
        """A manager and driver where, after the launch, WDA names the old process for ``old_for`` seconds and
        evidence reads fail with "not running" for ``reads_fail_for`` seconds. Returns (manager, driver, log)."""
        from mobile_agent.tests.test_verify_fixtures import SETTINGS_ROOT
        clock, log = self.clock, {"launched": None, "failed": 0, "first_read": None}
        manager = Manager(app_info=self.SETTINGS, on={"launch": lambda: log.update(launched=clock.now)})

        def front():
            launched = log["launched"]
            if launched is None or clock.now < launched + old_for:
                return "com.apple.Preferences", 1000  # in front before the launch, then still named
            return "com.apple.Preferences", manager.pid

        class Relaunching(Driver):
            def call(self, method, path, body=None, timeout=10):
                if path == EVIDENCE_SOURCE_PATH:
                    if log["first_read"] is None:
                        log["first_read"] = clock.now
                    if clock.now < log["launched"] + reads_fail_for:
                        log["failed"] += 1
                        raise TransportError(RelaunchTests.NOT_RUNNING)
                return super().call(method, path, body, timeout)
        self.front = front
        return manager, Relaunching([SETTINGS_ROOT], foreground="com.apple.Preferences"), log

    def test_the_run_waits_for_the_new_process(self):
        manager, driver, log = self.relaunching(old_for=.6, reads_fail_for=.6)
        result, _ = self.verify(check_from_dict({"bundle_id": "com.apple.Preferences", "expect": [{"text": "General"}]}),
                                driver, manager=manager)
        self.assertEqual(result["verdict"], "passed")
        self.assertEqual(log["failed"], 0)
        self.assertGreaterEqual(log["first_read"] - log["launched"], .6)
        self.assertRegex(Path(result["run_dir"], "run.log").read_text(),
                         r"com\.apple\.Preferences in front in process 1001 \(was 1000\) after 0\.[6-9]\d s")

    def test_a_read_in_the_relaunch_window_is_retried_after_a_pause(self):
        """WDA names the new process at once, and the first read still lands before the app answers."""
        manager, driver, log = self.relaunching(old_for=0, reads_fail_for=.5)
        result, _ = self.verify(check_from_dict({"bundle_id": "com.apple.Preferences", "expect": [{"text": "General"}]}),
                                driver, manager=manager)
        self.assertEqual(result["verdict"], "passed")
        self.assertEqual(log["failed"], 1)
        self.assertLess(.5, RETRY_PAUSE)

    def test_the_app_named_with_no_process_is_not_the_new_one(self):
        """Measured: 3 of 21 Settings reruns got activeAppInfo with the app and no process 0.03-0.07 s after the
        launch."""
        manager, driver, log = self.relaunching(old_for=0, reads_fail_for=.5)
        self.front = lambda: (("com.apple.Preferences", None) if log["launched"] is not None
                              and self.clock.now < log["launched"] + .5 else
                              ("com.apple.Preferences", 1000 if log["launched"] is None else manager.pid))
        result, _ = self.verify(check_from_dict({"bundle_id": "com.apple.Preferences", "expect": [{"text": "General"}]}),
                                driver, manager=manager)
        self.assertEqual((result["verdict"], log["failed"]), ("passed", 0))
        self.assertGreaterEqual(log["first_read"] - log["launched"], .5)

    def test_a_process_that_never_changes_waits_out_the_limit(self):
        manager, driver, log = self.relaunching(old_for=1e9, reads_fail_for=0)
        result, _ = self.verify(check_from_dict({"bundle_id": "com.apple.Preferences", "expect": [{"text": "General"}]}),
                                driver, manager=manager)
        self.assertGreaterEqual(log["first_read"] - log["launched"], runner.FOREGROUND_WAIT_SECONDS)
        self.assertEqual(result["verdict"], "passed")
        self.assertIn("was not in front 15 s after launch (still process 1000)",
                      Path(result["run_dir"], "run.log").read_text())

    def test_the_mcp_relaunch_waits_for_the_new_process_too(self):
        manager, driver, log = self.relaunching(old_for=.4, reads_fail_for=0)
        run, _ = self.keyless(check_from_dict({"bundle_id": "com.apple.Preferences", "expect": [{"text": "General"}]}),
                              driver, manager)
        self.front = lambda: (("com.apple.Preferences", 1001) if self.clock.now < log["launched"] + .4
                              else ("com.apple.Preferences", manager.pid))
        run.relaunch()
        self.assertEqual(manager.names()[-2:], ["terminate", "launch"])
        self.assertEqual(manager.pid, 1002)
        self.assertGreaterEqual(self.clock.now - log["launched"], .4)

    def test_an_app_not_in_front_before_the_launch_takes_any_process(self):
        manager = Manager()
        # SpringBoard is in front before the launch, so there is no process to replace and any one will do.
        self.front = lambda: (BUNDLE, 1000) if "launch" in manager.names() else ("com.apple.springboard", 77)
        started = self.clock.now
        run, _ = self.keyless(self.check(expect=[{"text": "Choose"}]), Driver([PLANS]), manager)
        self.assertLess(self.clock.now - started, 1)
        self.assertEqual(run.finish()["verdict"], "passed")


class LockOrderTests(RunnerCase):
    """finish() and abort() take run.lock, then _finish_lock. The MCP server's reaper, stop and shutdown call
    abort() holding run.lock while its Smart worker calls finish() on another thread: in the opposite order the
    two deadlocked, with no verdict written and the simulator never released."""

    def test_abort_holding_the_lock_and_finish(self):
        run, manager = self.keyless(self.check(expect=[{"text": "Choose your plan"}]), Driver([PLANS]))
        inside, finishing = threading.Event(), threading.Event()
        real_fail = run._fail

        def slow_fail(error):  # abort() calls it holding run.lock
            inside.set()
            finishing.wait(2)
            time.sleep(.2)  # finish() is now waiting on a lock
            return real_fail(error)
        run._fail = slow_fail
        results = {}
        aborting = threading.Thread(target=lambda: results.update(abort=run.abort("stopped", "Idle.")), daemon=True)
        aborting.start()
        self.assertTrue(inside.wait(2))

        def finish():
            finishing.set()
            results.update(finish=run.finish())
        worker = threading.Thread(target=finish, daemon=True)
        worker.start()
        worker.join(5)
        aborting.join(5)
        self.assertFalse(worker.is_alive() or aborting.is_alive(), "finish() and abort() deadlocked")
        self.assertIs(results["finish"], results["abort"])
        self.assertEqual(results["abort"]["reason"]["class"], "stopped")
        self.assertEqual(manager.leases[0].releases, 1)
        self.assertTrue(Path(results["abort"]["run_dir"], "result.json").is_file())

    def test_the_mcp_server_shape(self):
        """Thread B holds run.lock (the reaper) and aborts once thread A (the Smart worker) is inside finish()."""
        run, manager = self.keyless(self.check(expect=[{"text": "Choose your plan"}]), Driver([PLANS]))
        held, finishing, results = threading.Event(), threading.Event(), {}

        def reaper():
            with run.lock:
                held.set()
                finishing.wait(2)
                time.sleep(.2)
                results["abort"] = run.abort("stopped", "The session was idle for 15 minutes.")

        def worker():
            held.wait(2)
            finishing.set()
            results["finish"] = run.finish()
        threads = [threading.Thread(target=reaper, daemon=True), threading.Thread(target=worker, daemon=True)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertFalse(any(thread.is_alive() for thread in threads), "finish() and abort() deadlocked")
        self.assertIs(results["finish"], results["abort"])
        self.assertEqual(results["finish"]["reason"]["class"], "stopped")
        self.assertEqual(manager.leases[0].releases, 1)


class AbortWhilePreparingTests(RunnerCase):
    """abort() decides under _finish_lock who releases the simulator: the preparing thread while the state is
    "preparing", finish() once it is "ready". Whichever side of prepare()'s last stop check an abort lands on, the
    run ends stopped with the lease released once and the driver closed."""

    def test_an_abort_between_the_last_stop_check_and_ready(self):
        """Review round 2: an abort landing after prepare()'s last stop check and before the move to "ready" wrote
        its stopped result, prepare() went on to "ready", and finish() returned that result early: the lease was
        never released and the driver never closed."""
        manager, driver, aborted = Manager(), Driver([PLANS]), {}
        run = VerifyRun(self.check(expect=[{"text": "Choose your plan"}]), mode="keyless", runs_dir=self.runs,
                        manager=manager)
        self.watch(driver, manager)
        run._stop = LastCheckRace(run, lambda: aborted.update(result=run.abort("stopped", "The session was idle.")))
        with mock.patch.object(runner, "build_driver", lambda *a: driver):
            try:
                run.prepare()
            except runner.CouldntRun as error:
                self.assertEqual(error.klass, "stopped")
        run._stop.aborting.join(5)
        self.assertFalse(run._stop.aborting.is_alive(), "abort() never returned")
        self.assertEqual(run._stop.triggered, 1)
        self.assertEqual(aborted["result"]["reason"]["class"], "stopped")
        self.assertIs(run.finish(), aborted["result"])
        self.assertEqual(run.state, "finished")
        self.assertEqual([lease.releases for lease in manager.leases], [1])
        self.assertTrue(driver.closed)
        on_disk = json.loads(Path(aborted["result"]["run_dir"], "result.json").read_text())
        self.assertEqual(on_disk["reason"]["class"], "stopped")

    def test_an_abort_after_the_launch_frame(self):
        """The other side of the check: the abort lands on the 01-launch frame, after _prepare's last check."""
        holder = {}
        manager = Manager(on={"screenshot": lambda: holder.setdefault(
            "result", holder["run"].abort("stopped", "The session was idle for 15 minutes."))})
        driver = Driver([PLANS])
        run = holder["run"] = VerifyRun(self.check(expect=[{"text": "x"}]), mode="keyless", runs_dir=self.runs,
                                        manager=manager)
        self.watch(driver, manager)
        with mock.patch.object(runner, "build_driver", lambda *a: driver):
            with self.assertRaises(runner.CouldntRun):
                run.prepare()
        self.assertEqual(run.state, "finished")
        self.assertEqual(run.error["message"], "The session was idle for 15 minutes.")  # the abort's reason stands
        self.assertIs(run.finish(), holder["result"])
        self.assertEqual([lease.releases for lease in manager.leases], [1])
        self.assertTrue(driver.closed)

    def test_prepare_after_an_abort_takes_no_simulator(self):
        manager = Manager()
        run = VerifyRun(self.check(expect=[{"text": "x"}]), mode="keyless", runs_dir=self.runs, manager=manager)
        result = run.abort("stopped", "Stopped.")
        with self.assertRaises(runner.CouldntRun):
            run.prepare()
        self.assertEqual(manager.calls, [])
        self.assertIs(run.finish(), result)
        self.assertEqual(run.state, "finished")

    def test_finish_frees_a_simulator_whatever_wrote_the_result(self):
        """A result written while preparing leaves the release to the preparing thread; once no thread is
        preparing, finish() releases whatever is still held."""
        run = VerifyRun(self.check(expect=[{"text": "x"}]), mode="keyless", runs_dir=self.runs, manager=Manager())
        lease, driver = Lease(), Driver([PLANS])
        run.lease, run.driver, run.state = lease, driver, "preparing"
        run._preparing = True
        result = run.abort("stopped", "Stopped.")
        self.assertIs(run.finish(), result)
        self.assertEqual((lease.releases, driver.closed), (0, False))  # the preparing thread's to release
        run._preparing = False
        self.assertIs(run.finish(), result)
        self.assertIs(run.finish(), result)
        self.assertEqual((lease.releases, driver.closed), (1, True))

    def test_release_is_once_across_threads(self):
        run = VerifyRun(self.check(expect=[{"text": "x"}]), mode="keyless", runs_dir=self.runs, manager=Manager())
        lease, closes = Lease(), []
        run.lease = lease
        run.driver = SimpleNamespace(close=lambda: (closes.append(1), time.sleep(.05)))
        threads = [threading.Thread(target=run._release, daemon=True) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual((lease.releases, len(closes)), (1, 1))


class LastCheckRace(threading.Event):
    """The run's stop flag. When prepare() makes its last stop check (the driver is built by then), another thread
    calls ``abort`` and the check answers what it saw before. With prepare() holding _finish_lock for the check,
    that abort waits for the lock (it set the flag first); without, it runs to the end inside the window."""

    def __init__(self, run, abort):
        super().__init__()
        self.run, self.abort, self.triggered, self.aborting = run, abort, 0, None

    def is_set(self):
        before = super().is_set()
        caller = sys._getframe(1).f_code.co_name
        if caller != "prepare" or self.run.driver is None or self.triggered:
            return before
        self.triggered += 1
        held = self.run._finish_lock.locked()
        self.aborting = threading.Thread(target=self.abort, daemon=True)
        self.aborting.start()
        if held:  # abort() can't take the lock before prepare() lets it go: wait only for the flag
            self.wait(5)
        else:
            self.aborting.join(5)
        return before

class FrameTests(RunnerCase):
    def test_naming_and_the_step_cap(self):
        run, _ = self.keyless(self.check(expect=[{"text": "Choose"}]), Driver([PLANS]))
        for index in range(75):
            path = run.frame("step")
            run.record_step(op="TAP", text=f"Tapped {index}", target={"id": "x", "label": "X", "role": "Button"},
                            changed=True, frame=path)
        self.assertEqual(run.frames[0], "frames/01-launch.jpg")
        self.assertEqual(run.frames[1], "frames/02-step.jpg")
        result = run.finish()
        steps = [f for f in result["frames"] if f.endswith("-step.jpg")]
        # 75 step frames: the first 10, every other one of the 55 between (28), the last 10.
        self.assertEqual(len(steps), 48)
        self.assertEqual(steps[:10], [f"frames/{n:02d}-step.jpg" for n in range(2, 12)])
        self.assertEqual(steps[10:14], [f"frames/{n:02d}-step.jpg" for n in (12, 14, 16, 18)])
        self.assertEqual(steps[-10:], [f"frames/{n:02d}-step.jpg" for n in range(67, 77)])
        on_disk = sorted(p.name for p in Path(result["run_dir"], "frames").iterdir())
        self.assertEqual(len([n for n in on_disk if n.endswith("-step.jpg")]), 48)
        self.assertEqual([s["frame"] for s in result["steps"]][:10], steps[:10])
        self.assertIsNone(result["steps"][11]["frame"])
        self.assertEqual(result["steps"][10]["frame"], "frames/12-step.jpg")
        self.assertTrue(result["frames"][-1].endswith("-verdict-ax.jpg"))
        self.assertEqual(result["proof"][2], str(Path(result["run_dir"]) / steps[-1]))

    def test_the_cap_holds_for_long_runs(self):
        run, _ = self.keyless(self.check(expect=[{"text": "Choose"}]), Driver([PLANS]))
        for _ in range(200):
            run.frame("step")
        result = run.finish()
        self.assertLessEqual(len([f for f in result["frames"] if f.endswith("-step.jpg")]), 60)

    def test_frames_are_half_the_simulators_pixel_width(self):
        run, manager = self.keyless(self.check(expect=[{"text": "Choose"}]), Driver([PLANS]))
        with Image.open(Path(run.run_dir) / run.frames[0]) as image:
            self.assertEqual(image.width, 402)
        run.frame("step")
        self.assertEqual(manager.calls[-1], ("screenshot", "02-step.jpg", 402))

    def test_a_step_record(self):
        run, _ = self.keyless(self.check(expect=[{"text": "Choose"}]), Driver([PLANS]))
        path = run.frame("step")
        step = run.record_step(op="TYPE", text="Typed the password", target={"id": "password", "label": "Password",
                                                                             "role": "SecureTextField"},
                               typed="••••", changed=True, frame=path)
        self.assertEqual(step, {"index": 1, "op": "TYPE", "text": "Typed the password",
                                "target": {"id": "password", "label": "Password", "role": "SecureTextField"},
                                "typed": "••••", "changed": True, "frame": "frames/02-step.jpg",
                                "at_ms": step["at_ms"]})
        events = Path(run.run_dir, "events.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(events[-1])["event"], "step")
        self.assertEqual(run.record_step(op="TAP", text="t", frame="frames/02-step.jpg")["frame"], "frames/02-step.jpg")
        self.assertIsNone(run.record_step(op="TAP", text="t")["frame"])


class NoNetworkTests(RunnerCase):
    def test_a_keyless_run_and_its_verdict_never_touch_the_network(self):
        # The runner's alert, foreground and frame reads catch Exception, so the refusal alone could be swallowed
        # there: every attempt is counted as well, and the count must be zero.
        attempts = []

        def refuse(*args, **kwargs):
            attempts.append(args[1:] or kwargs)
            raise AssertionError("a key-less run opened a connection")
        with mock.patch.object(socket.socket, "connect", refuse), \
                mock.patch.object(socket, "create_connection", refuse):
            run, _ = self.keyless(self.check(steps=["Open the paywall."], expect=[{"text": "Choose your plan"}]),
                                  Driver([LOADING, PLANS]))
            run.frame("step")
            result = run.finish()
        self.assertIn(result["verdict"], ("passed", "needs_review"))
        self.assertIsNone(result["agent"])
        self.assertEqual(result["cost_usd"], 0)
        self.assertEqual(attempts, [])

    def test_a_swallowed_attempt_still_fails_it(self):
        """The guard above, on a driver that opens a socket when the verdict reads /alert/text (review round 2):
        VerifyRun._alert catches the refusal, and the attempt is still counted."""
        attempts = []

        class Noisy(Driver):
            def call(self, method, path, body=None, timeout=10):
                if path == "/alert/text":
                    socket.create_connection(("127.0.0.1", 9), timeout=1)
                return super().call(method, path, body, timeout)

        def refuse(*args, **kwargs):
            attempts.append(args[1:] or kwargs)
            raise AssertionError("a key-less run opened a connection")
        with mock.patch.object(socket.socket, "connect", refuse), \
                mock.patch.object(socket, "create_connection", refuse):
            run, _ = self.keyless(self.check(expect=[{"text": "Choose your plan"}]), Noisy([PLANS]))
            result = run.finish()
        self.assertEqual(result["verdict"], "passed")  # the refusal was swallowed...
        self.assertEqual(len(attempts), 1)             # ...and counted


# -- Smart ----------------------------------------------------------------------------------------------------------

class SmartTests(RunnerCase):
    def test_a_smart_check_on_a_real_iphone_refuses_every_commit(self):
        # A USB iPhone (device_targets.WdaDevice) is someone's own phone: a send, delete or post there is real.
        check = self.check(steps=["Open the paywall"], expect=[{"text": "Choose your plan"}])
        phone = Manager()
        phone.real_device = True
        self.verify(check, Driver([app(node("StaticText", "Welcome"))], after_flow=[PLANS]), key=KEY, manager=phone)
        approve = self.agents[0].approve
        self.assertIs(approve, deny_commits)
        for act, label in (("send", "Send"), ("delete", "Delete"), ("post", "Post"), ("other", "Send"),
                           ("other", "Sign up"), ("purchase", "Buy")):
            self.assertEqual(approve({"act": act, "label": label}), "denied", act)

    def test_a_smart_check_on_a_real_iphone_stays_in_its_app(self):
        """SPEC §3.8: on someone's phone a check drives only the app under test. The frontier asks the guard about
        every app in front (C7); a simulator, Mobster's own, has no such guard."""
        check = self.check(steps=["Open the paywall"], expect=[{"text": "Choose your plan"}])
        phone = Manager()
        phone.real_device = True
        phone.record = {"id": "00008020-000A1B2C3D4E5F60", "udid": "00008020-000A1B2C3D4E5F60", "kind": "usb",
                        "name": "Sam's iPhone", "wdaUrl": "http://127.0.0.1:1"}
        result, _ = self.verify(check, Driver([app(node("StaticText", "Welcome"))], after_flow=[PLANS]), key=KEY,
                                manager=phone)
        self.assertEqual(result["verdict"], "passed")
        guard = self.agents[0].guard
        self.assertIsInstance(guard, runner.StayInApp)
        self.assertTrue(guard.allows_app(BUNDLE))
        self.assertTrue(guard.allows_app("com.apple.SafariViewService"))  # SFSafariViewController, inside the app
        for other in ("com.apple.MobileSMS", "com.apple.mobileslideshow", "com.apple.shortcuts", "com.example.other"):
            self.assertFalse(guard.allows_app(other), other)
        self.assertIsNotNone(guard.inner)  # lockscreen's detection: a locked or unplugged phone stops the run
        self.verify(check, Driver([app(node("StaticText", "Welcome"))], after_flow=[PLANS]), key=KEY)
        self.assertIsNone(self.agents[0].guard)  # a simulator

    def test_a_smart_check_that_leaves_its_app_on_a_real_iphone_couldnt_run_with_no_frame_of_the_other_app(self):
        check = self.check(steps=["Open Messages and read the latest message"], expect=[{"text": "Choose your plan"}])
        phone = Manager()
        phone.real_device = True
        phone.record = {"id": "00008020-000A1B2C3D4E5F60", "udid": "00008020-000A1B2C3D4E5F60", "kind": "usb",
                        "name": "Sam's iPhone", "wdaUrl": "http://127.0.0.1:1"}
        result, manager = self.verify(check, Driver([app(node("StaticText", "Welcome"))], after_flow=[PLANS]),
                                      key=KEY, manager=phone,
                                      agent={"outcome": {"status": "completed", "answer": "Done."}, "events": [],
                                             "front": "com.apple.MobileSMS"})
        self.assertEqual(result["verdict"], "couldnt_run")
        self.assertEqual(result["reason"]["class"], "usage")
        self.assertIn("a check stays in Daybreak, and Smart opened com.apple.MobileSMS", result["reason"]["message"])
        self.assertEqual(result["reason"]["fix"], runner.LEFT_APP_FIX)
        self.assertFalse([frame for frame in result["frames"] if "verdict" in frame])
        self.assertEqual(manager.names().count("screenshot"), 1)  # the launch frame, before the flow

    def test_on_a_real_iphone_a_link_opens_in_the_app_or_not_at_all(self):
        """WDA opens a check's link in the app under test. Its fallback opens the link system-wide, in whichever app
        takes it (shortcuts://run-shortcut runs a Shortcut): on someone's phone that fallback never runs."""
        phone = Manager()
        phone.real_device = True
        driver = Driver([PLANS], url_error=TransportError("WDA rejected command; not retried"))
        result, manager = self.verify(self.check(open_url="daybreak://paywall", expect=[{"text": "Choose"}]), driver,
                                      manager=phone)
        self.assertEqual(result["verdict"], "couldnt_run")
        self.assertEqual(result["reason"]["class"], "launch")
        self.assertEqual(driver.opened, [{"url": "daybreak://paywall", "bundleId": BUNDLE}])
        self.assertNotIn("open_url", manager.names())

    def test_the_approver_follows_the_device(self):
        from mobile_agent.device_targets import PinnedSimulator, WdaDevice
        from mobile_agent.devices import record
        iphone = WdaDevice(record(id="00008020-000A1B2C3D4E5F60", kind="usb", name="Work iPhone",
                                  wda_url_value="http://127.0.0.1:8101", state="ready"))
        address = WdaDevice(record(id="wda-8400", kind="wda", name="Lab", wda_url_value="http://127.0.0.1:8400",
                                   state="ready"))
        self.assertIs(check_approver(iphone), deny_commits)
        self.assertIs(check_approver(address), deny_commits)
        self.assertIs(check_approver(PinnedSimulator(Manager(), "6F1C0E52-0000-4000-8000-00000000000A")), deny_money)
        self.assertIs(check_approver(Manager()), deny_money)
        self.assertIs(check_approver(None), deny_money)

    def test_recorded_frontier_events_become_steps_and_frames(self):
        check = self.check(steps=["Open the paywall", "Enter the promo code"],
                           expect=[{"text": "Choose your plan"}], max_usd=0.15)
        result, _ = self.verify(check, Driver([app(node("StaticText", "Welcome"))], after_flow=[PLANS]), key=KEY)
        agent = self.agents[0]
        self.assertEqual(agent.requests, ["In Daybreak: 1. Open the paywall 2. Enter the promo code"])
        self.assertEqual(agent.apps, {BUNDLE: "Daybreak"})
        self.assertEqual(agent.max_cost_usd, 0.15)
        self.assertEqual(agent.max_seconds, 180.0)
        self.assertIs(agent.approve, deny_money)
        self.assertEqual(result["verdict"], "passed")
        self.assertEqual(result["mode"], "smart")
        # Each step's frame is the screen after it: the image Smart sees next turn, and the verdict frame after
        # the last turn.
        self.assertEqual([(s["op"], s["text"], s["typed"], s["changed"], s["frame"]) for s in result["steps"]], [
            ("TAP", "Tapped Continue", None, True, "frames/03-step.jpg"),
            ("TYPE", "Typed “SPRING” in Promo code", "SPRING", True, "frames/04-verdict.jpg"),
            ("SWIPE_UP", "Scrolled down", None, False, "frames/04-verdict.jpg")])
        self.assertEqual(result["frames"], ["frames/01-launch.jpg", "frames/02-step.jpg", "frames/03-step.jpg",
                                            "frames/04-verdict.jpg", "frames/05-verdict-ax.jpg"])
        self.assertEqual(result["steps"][0]["target"], {"id": None, "label": "Continue", "role": None})
        with Image.open(Path(result["run_dir"], "frames/03-step.jpg")) as image:
            self.assertGreater(image.getpixel((5, 5))[0], 150)  # the second turn's (red) image
            self.assertEqual(image.width, 402)
        self.assertEqual(result["agent"], {"status": "completed", "note": "Done.", "model": "gpt-5.6-sol",
                                           "turns": 2})

    def test_events_keep_no_prompts_images_or_key(self):
        result, _ = self.verify(self.check(steps=["x"], expect=[{"text": "Choose"}]), Driver([PLANS]), key=KEY)
        run_dir = Path(result["run_dir"])
        events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
        prompts = [e for e in events if e["event"] == "frontier_prompt"]
        self.assertEqual(len(prompts), 2)
        self.assertTrue(all("text" not in e and "image" not in e for e in prompts))
        self.assertIn("frontier_action", {e["event"] for e in events})
        self.assertIn("inference_finished", {e["event"] for e in events})
        for path in run_dir.rglob("*"):
            if path.is_file() and path.suffix in (".json", ".jsonl", ".log", ".html", ".yaml"):
                with self.subTest(file=path.name):
                    self.assertNotIn(KEY, path.read_text())
        self.assertIn("[key]", (run_dir / "events.jsonl").read_text())

    def test_the_cost_is_the_larger_of_the_meter_and_the_loop(self):
        result, _ = self.verify(self.check(steps=["x"], expect=[{"text": "Choose"}]), Driver([PLANS]), key=KEY)
        # Three metered calls of 10,000 in and 500 out at $4 and $20 per million: $0.05 each, $0.15 either way.
        self.assertAlmostEqual(result["cost_usd"], 0.15, places=5)

    def test_a_recorded_settings_run(self):
        from mobile_agent.tests.test_verify_fixtures import SETTINGS_ABOUT, SETTINGS_ROOT
        images = {"IMAGE0": jpeg_data_url((30, 30, 30)), "IMAGE1": jpeg_data_url((220, 220, 220))}
        events = [dict(e, image=images[e["image"]]) if e["event"] == "frontier_prompt" else e
                  for e in RECORDED_SETTINGS_RUN]
        settings = SimpleNamespace(bundle_id="com.apple.Preferences", name="Settings", version="1 (1353.4.9)",
                                   path=None)
        check = check_from_dict({"bundle_id": "com.apple.Preferences", "steps": ["Open General, then About"],
                                 "expect": [{"text": "iOS Version"}], "max_usd": 0.15})
        result, manager = self.verify(check, Driver([SETTINGS_ROOT], after_flow=[SETTINGS_ABOUT],
                                                    foreground="com.apple.Preferences"),
                                      manager=Manager(app_info=settings), key=KEY,
                                      agent={"outcome": RECORDED_OUTCOME, "events": events})
        self.assertEqual(self.agents[0].requests, ["In Settings: Open General, then About"])
        self.assertEqual((result["verdict"], result["baseline"]), ("passed", {"taken": True, "held": [False]}))
        # One turn, two taps: Smart saw no image between them, so both show the screen after the turn (About,
        # 03-step), not the Settings root it started from (02-step, run 20260928-154032-da3a).
        self.assertEqual([(s["op"], s["text"], s["changed"], s["frame"]) for s in result["steps"]],
                         [("TAP", "Tapped General", True, "frames/03-step.jpg"),
                          ("TAP", "Tapped About", True, "frames/03-step.jpg")])
        self.assertEqual(result["frames"], ["frames/01-launch.jpg", "frames/02-step.jpg", "frames/03-step.jpg",
                                            "frames/04-verdict.jpg", "frames/05-verdict-ax.jpg"])
        self.assertEqual(result["agent"], {"status": "completed", "note": "Opened Settings > General > About.",
                                           "model": "gpt-5.6-sol", "turns": 2})
        self.assertEqual([c[0] for c in manager.calls if c[0] in ("reset", "install")], [])  # a system app
        self.assertIn('found in cell "iOS Version, 26.4"', result["assertions"][0]["observed"])

    def test_the_request(self):
        self.assertEqual(smart_request("Settings", ["Open General, then About"]),
                         "In Settings: Open General, then About")

    def test_a_turn_without_an_image_leaves_its_steps_waiting(self):
        events = [dict(e, image=None) if e["event"] == "frontier_prompt" and e["step"] == 1 else e
                  for e in recorded_events()]
        result, _ = self.verify(self.check(steps=["x"], expect=[{"text": "Choose your plan"}]),
                                Driver([PLANS]), key=KEY, agent={"outcome": {"status": "completed"}, "events": events})
        self.assertEqual([s["frame"] for s in result["steps"]], ["frames/03-verdict.jpg"] * 3)

    def test_the_frame_cap_keeps_a_verdict_after_frame(self):
        run, _ = self.keyless(self.check(expect=[{"text": "Choose"}]), Driver([PLANS]))
        for _ in range(75):
            run.frame("step")
        step = run.record_step(op="TAP", text="Tapped Continue")
        run._awaiting_frame.append(step)
        result = run.finish()
        self.assertTrue(result["steps"][0]["frame"].endswith("-verdict.jpg"))
        self.assertTrue(Path(result["run_dir"], result["steps"][0]["frame"]).is_file())


class DenyMoneyTests(unittest.TestCase):
    def test_money_is_always_denied(self):
        for request in ({"act": "pay"}, {"act": "transfer"}, {"act": "order"}, {"act": "request_money"},
                        {"act": "other", "label": "Buy now"}, {"act": "other", "title": "Tap 'Subscribe'?"},
                        {"act": "other", "label": "Place Order"}, {"act": "other", "label": "Pay $4.99"}):
            with self.subTest(request=request):
                self.assertEqual(deny_money(request), "denied")

    def test_other_commits_go_through(self):
        for request in ({"act": "send_message", "label": "Send"}, {"act": "other", "label": "Sign up"},
                        {"act": "follow", "label": "Follow"}, {"act": "other", "label": "Payment history"}, {}):
            with self.subTest(request=request):
                self.assertEqual(deny_money(request), "approved")


if __name__ == "__main__":
    unittest.main()
