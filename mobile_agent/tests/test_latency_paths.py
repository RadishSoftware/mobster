"""Latency trace, hedged model calls, the decision memo, first-screen prediction,
the fresh-observation guard and answer speculation. Offline."""

import copy
import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from mobile_agent import agent as agent_module, hedge, models
from mobile_agent.agent import Agent, FRESH_GUARD_SECONDS, fork_evidence, remember_first_screen
from mobile_agent.decision_memo import DecisionMemo, memo_key, normalized_clock
from mobile_agent.extraction import Evidence
from mobile_agent.http_pool import ConnectionPool, PooledHTTP
from mobile_agent.latency_trace import Trace, summarize
from mobile_agent.models import Decision
from mobile_agent.state import Element, Snapshot
from mobile_agent.inference import ProviderResponseRejected
from mobile_agent.task_policy import ActionSupport, OutputIntent, StopGate
from mobile_agent.transport import TransportError
from mobile_agent.tests.timing import bound


# These tests exercise the model's speed paths on "Open General"; the route
# compiler (routes.py, tested in test_routes.py) would answer those hops itself.
_ROUTES_OFF = None


def setUpModule():
    global _ROUTES_OFF
    import os as _os
    from unittest.mock import patch as _patch
    _ROUTES_OFF = _patch.dict(_os.environ, {"MOBSTER_ROUTES": "0"})
    _ROUTES_OFF.start()


def tearDownModule():
    _ROUTES_OFF.stop()



def screen(title, rows=("General",), bundle="com.apple.Preferences"):
    elements = [Element("0", title, "StaticText", (.3, .05, .4, .04), locator=f"/{title}/t")]
    for index, label in enumerate(rows, 1):
        elements.append(Element(str(index), label, "Cell", (.05, .1 * index + .1, .9, .06),
                                locator=f"/{title}/{index}"))
    return Snapshot(elements, "\n".join((title, *rows)), 393, 852, "wda", bundle_id=bundle)


def decision(operation, target=None, **extra):
    return Decision(operation, target, .97, .95 if operation == "DONE" else .05, .02, "jev-t", 12.0, {},
                    StopGate.CONTINUE, risk_tier="navigation" if operation == "TAP" else None,
                    side_effect_risk=.01, **extra)


class Phone:
    """Root -> General -> DONE. Records reads and taps."""
    can_type = False

    def __init__(self):
        self.screens = {"root": screen("Settings", ("General", "Privacy")),
                        "general": screen("General", ("About", "Keyboard"))}
        self.at, self.reads, self.actions = "root", 0, []

    def observe(self, timeout=10):
        self.reads += 1
        from dataclasses import replace
        return replace(self.screens[self.at], captured_at=time.monotonic())

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        self.actions.append((operation, target.label if target else None))
        if target is not None and target.label == "General":
            self.at = "general"

    def wait_for_change(self, snapshot, timeout=2, wait_seconds=.6, on_settling=None):
        return self.observe()


class Model:
    """A scripted Jev: TAP General on the root, DONE on General."""
    speculative_decisions = True
    stable_completion = True
    model = "jev-t"

    def __init__(self, delay=0.0):
        self.calls, self.delay, self.channels = [], delay, []
        self.lock = threading.Lock()

    def side_channel(self, name="prefetch"):
        self.channels.append(name)
        return name

    def decide(self, snapshot, goal, history, **kwargs):
        with self.lock:
            self.calls.append((snapshot.text.split("\n")[0], kwargs.get("http")))
        if self.delay:
            time.sleep(self.delay)
        if snapshot.text.startswith("Settings"):
            return decision("TAP", "1")
        return decision("DONE")

    def verify_action(self, *args, **kwargs):
        return ActionSupport.ALLOWED


class TraceTests(unittest.TestCase):
    def test_phases_calls_marks_and_summary(self):
        now = [0.0]
        trace = Trace(clock=lambda: now[0], wall=lambda: 1000.0)
        with trace.span("observe.first"):
            now[0] = .2
            with trace.span("observe.refresh"):  # nested: a mark, never a second phase
                now[0] = .3
        trace.phase("decide.wait_speculation", .3, .35)
        trace.call("decision", .1, .34, lane="speculation", reused=True)
        trace.call("decision", .35, .6, lane="main", hedge=True, won=True)
        now[0] = .6
        event = trace.event()
        self.assertEqual(event["event"], "latency_trace")
        self.assertEqual([p[0] for p in event["phases"]], ["observe.first", "decide.wait_speculation"])
        self.assertEqual(event["phases"][0][2], 300.0)
        self.assertEqual(event["marks"][0][0], "observe.refresh")
        summary = event["summary"]
        self.assertEqual(summary["unattributed_ms"], 250.0)
        self.assertEqual(summary["model_off_lane_ms"], 240.0)
        self.assertEqual(summary["overlap_ms"], 190.0)  # off-lane work minus the main lane's wait for it
        self.assertEqual((summary["hedges"], summary["hedge_wins"]), (1, 1))
        json.dumps(event)

    def test_trace_is_bounded(self):
        trace = Trace()
        for _ in range(2100):
            trace.phase("x", 0, 0)
        self.assertEqual(len(trace.phases), 2000)
        self.assertEqual(trace.event()["dropped"], {"phases": 100})

    def test_agent_emits_one_trace_before_the_result(self):
        events = []
        result = Agent(Phone(), Model(), emit=events.append, settle_seconds=.6).run("Open General", execute=True)
        self.assertEqual(result["status"], "completed_unverified")
        kinds = [event["event"] for event in events]
        self.assertEqual(kinds.count("latency_trace"), 1)
        self.assertEqual(kinds[-2:], ["latency_trace", "result"])
        trace = events[-2]
        names = [phase[0] for phase in trace["phases"]]
        for expected in ("observe.first", "decide", "dispatch.TAP", "settle.TAP", "observe.completion"):
            self.assertIn(expected, names)

    def test_a_caller_owned_trace_is_not_emitted_by_the_agent(self):
        events = []
        trace = Trace()
        Agent(Phone(), Model(), emit=events.append, trace=trace).run("Open General", execute=True)
        self.assertNotIn("latency_trace", [event["event"] for event in events])
        self.assertTrue(trace.phases)


class HedgeTests(unittest.TestCase):
    def hedger(self, ratio=1.0):
        return hedge.Hedger(floor={"decision": .05}, default={"decision": .05},
                            budget=hedge.HedgeBudget(ratio=ratio, burst=1))

    def test_a_fast_primary_is_never_hedged(self):
        hedger, seen = self.hedger(), []
        result = hedger.run("decision", lambda index, budget: seen.append(index) or "ok", timeout=2)
        self.assertEqual((result, seen, hedger.budget.hedges), ("ok", [0], 0))

    def test_a_slow_primary_is_hedged_and_the_first_answer_wins(self):
        hedger, reports = self.hedger(), []

        def attempt(index, budget):
            time.sleep(.5 if index == 0 else .01)
            return f"answer-{index}"
        started = time.monotonic()
        result = hedger.run("decision", attempt, timeout=2,
                            on_attempt=lambda *args: reports.append(args))
        self.assertEqual(result, "answer-1")
        # Answered before the 0.5 s primary could: the hedge won.
        self.assertLess(time.monotonic() - started, bound(.3))
        self.assertEqual(hedger.budget.hedges, 1)
        time.sleep(.6)  # the loser finishes in the background and still reports
        self.assertEqual(sorted((r[0], r[4]) for r in reports), [(0, False), (1, True)])

    def test_the_budget_caps_hedges(self):
        hedger = self.hedger(ratio=0.0)
        hedger.budget.tokens = 0
        result = hedger.run("decision", lambda index, budget: time.sleep(.12) or index, timeout=2)
        self.assertEqual((result, hedger.budget.hedges), (0, 0))

    def test_a_fast_failure_raises_like_an_unhedged_call(self):
        def attempt(index, budget):
            raise ValueError("bad response")
        with self.assertRaises(ValueError):
            self.hedger().run("decision", attempt, timeout=2)

    def test_a_fast_failure_in_transit_is_resent_once_without_hedge_budget(self):
        hedger, seen = self.hedger(ratio=0.0), []
        hedger.budget.tokens = 0

        def attempt(index, budget):
            seen.append(index)
            if index == 0:
                raise TransportError("HTTP RemoteDisconnected; request outcome unknown; not retried")
            return "ok"
        self.assertEqual(hedger.run("decision", attempt, timeout=2), "ok")
        self.assertEqual((seen, hedger.resends, hedger.budget.hedges), ([0, 1], 1, 0))

    def test_server_errors_are_resent_and_client_errors_are_not(self):
        for message, resent in (("HTTP 503; request not retried", True), ("HTTP 429; request not retried", True),
                                ("HTTP 400; request not retried", False), ("HTTP 401; request not retried", False)):
            seen = []

            def attempt(index, budget):
                seen.append(index)
                raise TransportError(message)
            with self.assertRaises(TransportError):
                self.hedger().run("decision", attempt, timeout=2)
            self.assertEqual(seen, [0, 1] if resent else [0], message)

    def test_only_failures_in_transit_are_transient(self):
        self.assertTrue(hedge.transient(TransportError("HTTP ConnectionResetError; request outcome unknown; not retried")))
        self.assertTrue(hedge.transient(TransportError("Incomplete HTTP response; outcome unknown; not retried")))
        self.assertFalse(hedge.transient(ProviderResponseRejected("gemini-x", {})))
        self.assertFalse(hedge.transient(TransportError("HTTP ValueError; request outcome unknown; not retried")))
        self.assertFalse(hedge.transient(TransportError("HTTP client already has a request in flight; not retried")))
        self.assertFalse(hedge.transient(TransportError("Existing Google Cloud credentials could not authenticate")))
        self.assertFalse(hedge.transient(ValueError("bad response")))

    def test_no_resend_without_time_left(self):
        hedger, seen = self.hedger(ratio=0.0), []
        hedger.budget.tokens = 0

        def attempt(index, budget):
            seen.append(index)
            time.sleep(.3)
            raise TransportError("HTTP RemoteDisconnected; request outcome unknown; not retried")
        with self.assertRaises(TransportError):
            hedger.run("decision", attempt, timeout=.6)
        self.assertEqual(seen, [0])

    def test_both_failing_raises_the_primary_error(self):
        def attempt(index, budget):
            time.sleep(.1 if index == 0 else .01)
            raise (KeyError if index == 0 else ValueError)("x")
        with self.assertRaises(KeyError):
            self.hedger().run("decision", attempt, timeout=2)

    def test_the_delay_follows_the_rolling_p90_above_the_floor(self):
        hedger = hedge.Hedger(floor={"decision": .8}, default={"decision": .8})
        self.assertEqual(hedger.delay("decision"), .8)
        for value in [.2] * 30 + [1.5] * 10:
            hedger.window.add("decision", value)
        self.assertEqual(hedger.delay("decision"), 1.5)

    def test_jev_sends_the_identical_body_twice_and_keeps_its_own_connection_free(self):
        pool = ConnectionPool(factory=lambda base, key: Mock(connection=Mock(sock=None)))
        model = models.Jev(key="offline")
        self.addCleanup(model.close)
        model.http = PooledHTTP(models.JEV_BASE_URL, "offline", pool=pool)
        bodies, trace = [], Trace()
        model.trace = trace

        def fake(http, path, body, timeout, **kwargs):
            bodies.append((http is model.http, json.dumps(body, sort_keys=True), kwargs["call_id"]))
            time.sleep(.4 if http is model.http else .01)
            return {"answers": {}, "model": "m"}
        saved = hedge.HEDGERS["typesafe"]
        hedge.HEDGERS["typesafe"] = self.hedger()
        try:
            with patch.object(models, "request_inference", fake), \
                    patch.object(models, "pooled_http", lambda base, key: PooledHTTP(base, key, pool=pool)):
                response = model._post({"q": 1}, 5, purpose="decision")
        finally:
            hedge.HEDGERS["typesafe"] = saved
        self.assertEqual(response, {"answers": {}, "model": "m"})
        self.assertEqual(len(bodies), 2)
        self.assertEqual(bodies[0][1], bodies[1][1])
        self.assertEqual({b[0] for b in bodies}, {True, False})
        self.assertEqual({b[2] for b in bodies}, {"jev:1", "jev:1h"})
        time.sleep(.5)
        self.assertTrue(any(call[4].get("hedge") for call in trace.calls))

    def test_side_channel_calls_are_never_hedged(self):
        model = models.Jev(key="offline")
        self.addCleanup(model.close)
        seen = []
        with patch.object(models, "request_inference", lambda http, *a, **k: seen.append(http) or {}):
            model._post({"q": 1}, 5, purpose="decision", http="side")
        self.assertEqual(seen, ["side"])


class DecisionMemoTests(unittest.TestCase):
    def request(self, **extra):
        return {"goal": "Open General", "history": [], "can_type": False, "hint": "",
                "original_goal": "Open General", "classify_output": False,
                "evidence_history": {"entries": [], "truncated": False},
                "temporal_context": {"source": "host_request_clock_not_verified_device_clock",
                                     "request_time": "2026-09-24T10:00:00.5-07:00", "timezone_name": "PDT"},
                "host_operations": frozenset(), "allowed_bundles": None, **extra}

    def test_the_key_is_the_whole_request_with_the_clock_at_day_granularity(self):
        root = screen("Settings")
        base = memo_key(root, self.request(), "jev")
        later = self.request()
        later["temporal_context"] = {**later["temporal_context"], "request_time": "2026-09-24T18:30:00-07:00"}
        self.assertEqual(memo_key(root, later, "jev"), base)
        other_day = self.request()
        other_day["temporal_context"] = {**later["temporal_context"], "request_time": "2026-09-25T09:00:00-07:00"}
        self.assertNotEqual(memo_key(root, other_day, "jev"), base)
        for changed in (self.request(hint="x"), self.request(history=[{"operation": "TAP"}]),
                        self.request(goal="Open Privacy")):
            self.assertNotEqual(memo_key(root, changed, "jev"), base)
        self.assertNotEqual(memo_key(screen("Settings", ("General", "Wi-Fi")), self.request(), "jev"), base)
        self.assertNotEqual(memo_key(root, self.request(), "jev-2"), base)

    def test_time_relative_requests_keep_the_full_clock(self):
        context = self.request()["temporal_context"]
        self.assertEqual(normalized_clock(context, "Set an alarm for 7 am"), context)
        self.assertEqual(normalized_clock(context, "What is on my calendar tomorrow"), context)
        self.assertNotIn("request_time", normalized_clock(context, "Find the iOS version"))

    def test_round_trip_ttl_and_forget(self):
        now = [1000.0]
        with tempfile.TemporaryDirectory() as directory:
            memo = DecisionMemo(directory, clock=lambda: now[0])
            key = "a" * 64
            memo.save(key, decision("TAP", "3", output_intent=OutputIntent.ACTION_ONLY))
            fresh = DecisionMemo(directory, clock=lambda: now[0])
            got = fresh.get(key)
            self.assertEqual((got.operation, got.target, got.model, got.latency_ms), ("TAP", "3", "memo", 0.0))
            self.assertIs(got.output_intent, OutputIntent.ACTION_ONLY)
            self.assertEqual(oct(os.stat(os.path.join(directory, key + ".json")).st_mode & 0o777), "0o600")
            now[0] += 25 * 3600
            self.assertIsNone(fresh.get(key))
            memo.save(key, decision("DONE"))
            now[0] = 1000.0 + 25 * 3600
            memo.forget(key)
            self.assertIsNone(DecisionMemo(directory, clock=lambda: now[0]).get(key))

    def test_waits_refusals_demotions_and_typed_text_on_disk_are_never_memoized(self):
        with tempfile.TemporaryDirectory() as directory:
            memo = DecisionMemo(directory)
            memo.save("b" * 64, decision("WAIT"))
            memo.save("c" * 64, decision("BLOCKED"))
            memo.save("d" * 64, Decision("WAIT", None, .3, .05, .02, "j", 1, {}, StopGate.CONTINUE, demoted_from="TAP"))
            memo.save("e" * 64, decision("TYPE", "1", text="Ada Lovelace"))
            self.assertEqual(os.listdir(directory), [])
            self.assertEqual(memo.get("e" * 64).text, "Ada Lovelace")  # in memory only
            self.assertIsNone(memo.get("b" * 64))

    def test_a_successful_run_is_answered_from_the_memo_next_time(self):
        memo = DecisionMemo()
        first = Model()
        Agent(Phone(), first, decision_memo=memo).run("Open General", execute=True)
        self.assertTrue(first.calls)
        second, events = Model(), []
        result = Agent(Phone(), second, decision_memo=memo, emit=events.append).run("Open General", execute=True)
        self.assertEqual(result["status"], "completed_unverified")
        self.assertEqual(second.calls, [])
        self.assertEqual(sum(e["event"] == "decision_memo_hit" for e in events), 2)

    def test_a_failed_run_forgets_the_entries_it_used(self):
        memo = DecisionMemo()
        Agent(Phone(), Model(), decision_memo=memo).run("Open General", execute=True)
        self.assertEqual(len(memo._memory), 2)
        phone = Phone()
        phone.execute = Mock()  # every tap is now ineffective
        result = Agent(phone, Model(), decision_memo=memo, settle_seconds=0).run("Open General", execute=True)
        self.assertNotEqual(result["status"], "completed_unverified")
        # The entry it acted on is gone; the one it never reached is kept.
        self.assertEqual(len(memo._memory), 1)
        self.assertEqual(next(iter(memo._memory.values()))["decision"]["operation"], "DONE")

    def test_disabled_by_environment(self):
        with patch.dict(os.environ, {"MOBSTER_DECISION_MEMO": "0"}):
            self.assertIsNone(Agent(Phone(), Model(), decision_memo=DecisionMemo()).decision_memo)


class FirstScreenPredictionTests(unittest.TestCase):
    def setUp(self):
        with agent_module._first_screens_lock:
            agent_module._first_screens.clear()

    def test_the_remembered_first_screen_is_decided_while_the_launch_is_observed(self):
        remember_first_screen("com.apple.Preferences", "first", screen("Settings", ("General", "Privacy")))
        model, events = Model(delay=.05), []
        result = Agent(Phone(), model, emit=events.append, launch_bundle="com.apple.Preferences").run(
            "Open General", execute=True)
        self.assertEqual(result["status"], "completed_unverified")
        kinds = [event["event"] for event in events]
        self.assertIn("decision_first_screen_predicted", kinds)
        self.assertEqual(model.calls[0], ("Settings", "prediction"))
        used = [event for event in events if event["event"] == "decision_speculation_used"]
        self.assertTrue(used and used[0]["predicted"] is True)

    def test_a_different_first_screen_discards_the_prediction(self):
        remember_first_screen("com.apple.Preferences", "first", screen("Settings", ("Wi-Fi",)))
        events = []
        Agent(Phone(), Model(), emit=events.append, launch_bundle="com.apple.Preferences").run(
            "Open General", execute=True)
        self.assertIn("decision_speculation_discarded", [event["event"] for event in events])

    def test_the_first_screen_is_remembered_per_app(self):
        Agent(Phone(), Model(), launch_bundle="com.apple.Preferences").run("Open General", execute=True)
        candidates = agent_module.first_screen_candidates("com.apple.Preferences")
        self.assertEqual([c.text.split("\n")[0] for c in candidates], ["Settings", "General"])
        self.assertEqual(agent_module.first_screen_candidates("com.apple.mobilesafari"), [])


class AnswerPhone(Phone):
    reports_settling = True

    def wait_for_change(self, snapshot, timeout=2, wait_seconds=.6, on_settling=None):
        if on_settling is not None:
            on_settling(self.observe())
            time.sleep(.1)  # the settle proof, while the speculation runs
        return self.observe()

    def __init__(self):
        super().__init__()
        self.screens["about"] = Snapshot([Element("0", "About", "StaticText", (.3, .05, .4, .04), locator="/a/t"),
                                          Element("1", "iOS Version", "StaticText", (.05, .2, .9, .06),
                                                  locator="/a/1", value="26.0.1")],
                                         "About\niOS Version 26.0.1", 393, 852, "wda",
                                         bundle_id="com.apple.Preferences")

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        super().execute(operation, target, snapshot, text, timeout)
        if target is not None and target.label == "About":
            self.at = "about"


class AnswerModel(Model):
    """Root -> General -> About -> DONE, then select the cited literal and verify it."""

    def decide(self, snapshot, goal, history, **kwargs):
        with self.lock:
            self.calls.append((snapshot.text.split("\n")[0], kwargs.get("http")))
        time.sleep(.02)
        title = snapshot.text.split("\n")[0]
        if title == "Settings":
            return decision("TAP", "1")
        if title == "General":
            return decision("TAP", "1")
        return decision("DONE")

    def select_fields(self, goal, schema, evidence, timeout=20, *, http=None):
        entry = next(e for e in evidence["entries"] if e["text"] == "26.0.1")
        return {"data": {"ios_version": "26.0.1"},
                "citations": [{"path": "/ios_version", "evidence_id": entry["id"], "quote": "26.0.1"}]}

    def verify_output(self, *args, **kwargs):
        from mobile_agent.task_policy import OutputSupport
        return OutputSupport.SUPPORTED


ANSWER_SCHEMA = {"type": "object", "properties": {"ios_version": {"type": "string"}},
                 "required": ["ios_version"], "additionalProperties": False}


class AnswerSpeculationTests(unittest.TestCase):
    def setUp(self):
        with agent_module._first_screens_lock:
            agent_module._first_screens.clear()

    def run_answer(self, **kwargs):
        events = []
        result = Agent(AnswerPhone(), AnswerModel(), emit=events.append, **kwargs).run(
            "Report the iOS version", execute=True, output_schema=ANSWER_SCHEMA)
        marks = [mark[0] for mark in next(e for e in events if e["event"] == "latency_trace")["marks"]]
        return result, marks

    def test_a_speculative_done_starts_the_answer_before_the_done_step(self):
        result, marks = self.run_answer()
        self.assertEqual(result["data"], {"ios_version": "26.0.1"})
        self.assertIn("prefetch.speculated", marks)

    def test_an_answer_speculated_with_other_evidence_is_never_used(self):
        # A remembered final screen (About) predicted as the first screen says DONE with
        # evidence of that screen alone; the real run reaches About with more evidence.
        self.run_answer(launch_bundle="com.apple.Preferences")
        for _ in range(2):
            result, marks = self.run_answer(launch_bundle="com.apple.Preferences")
            self.assertEqual(result["status"], "completed_unverified")
            self.assertEqual(result["data"], {"ios_version": "26.0.1"})


class GuardTests(unittest.TestCase):
    def test_fresh_guard_skips_the_reread_only_for_a_decision_ready_at_observation(self):
        remember_first_screen("com.apple.Preferences", "first", screen("Settings", ("General", "Privacy")))
        phone, events = Phone(), []
        Agent(phone, Model(), emit=events.append, launch_bundle="com.apple.Preferences").run(
            "Open General", execute=True)
        self.assertIn("refresh_skipped_fresh", [event["event"] for event in events])
        phone, events = Phone(), []
        Agent(phone, Model(), emit=events.append).run("Open General", execute=True)
        self.assertNotIn("refresh_skipped_fresh", [event["event"] for event in events])
        self.assertGreater(FRESH_GUARD_SECONDS, 0)
        self.assertLessEqual(FRESH_GUARD_SECONDS, .15)

    def test_fork_evidence_equals_a_deep_copy(self):
        evidence = Evidence()
        evidence.add(screen("Settings", ("General", "Privacy")), 0)
        evidence.add(screen("General", ("About",)), 1)
        forked, deep = fork_evidence(evidence), copy.deepcopy(evidence)
        for copy_ in (forked, deep):
            copy_.add(screen("Settings", ("General", "Privacy")), 2)
        self.assertEqual(forked.public(), deep.public())
        self.assertEqual(forked.decision_context(screen("General")), deep.decision_context(screen("General")))
        self.assertNotEqual(evidence.public(), forked.public())  # the original is untouched


if __name__ == "__main__":
    unittest.main()


class ServerWiringTests(unittest.TestCase):
    def test_work_warms_models_first_traces_every_phase_and_emits_the_trace(self):
        from pathlib import Path
        from types import SimpleNamespace
        from mobile_agent.drivers import WDA
        from mobile_agent.journal import Lease
        from mobile_agent.server import Runtime
        from mobile_agent.tests.test_server_hardening import APP, ParkedWorker

        order, seen = [], {}

        class FakeAgent:
            def __init__(self, *args, **kwargs):
                seen.update(kwargs)

            def run(self, goal, **kwargs):
                with seen["trace"].span("decide"):
                    pass
                return {"event": "result", "status": "completed_unverified", "independently_verified": False}

        with tempfile.TemporaryDirectory() as root, \
                patch("mobile_agent.server.threading.Thread", ParkedWorker), \
                patch("mobile_agent.server.Lease.device", side_effect=lambda _: Lease(Path(root) / "d.lock")):
            config = SimpleNamespace(wda_url="http://127.0.0.1:8100", session=None,
                                     ocr=False, enable_live=True, port=8765, state_db=str(Path(root) / "r.sqlite3"))
            runtime = Runtime(config)
            self.addCleanup(runtime.close)
            runtime.apps = Mock(return_value=[dict(APP)])
            runtime.status = Mock(return_value={"live_enabled": True, "helper_configured": False})
            runtime.wda_session = Mock(return_value="s")
            run, _ = runtime.create("settings", "Open About", "live", "wiring", engine="fast")
            driver = Mock(spec=WDA)
            driver.last_image = None
            with patch("mobile_agent.server.build_models", side_effect=lambda **k: order.append("models") or (Mock(), None)), \
                    patch("mobile_agent.server.warm_clients", side_effect=lambda *a: order.append("warm")), \
                    patch("mobile_agent.server.build_target_driver", side_effect=lambda **k: order.append("driver") or driver), \
                    patch("mobile_agent.server.prepare_wda_phone"), patch("mobile_agent.server.attach_frame_clock"), \
                    patch("mobile_agent.server.Agent", FakeAgent):
                runtime.work(run)
            if run.lease:
                run.lease.close()
                run.lease = None
        self.assertEqual(order[:3], ["models", "warm", "driver"])
        self.assertEqual(seen["launch_bundle"], "com.apple.Preferences")
        self.assertIs(seen["decision_memo"], runtime.decision_memo)
        trace = next(event for event in run.events if event["event"] == "latency_trace")
        names = [phase[0] for phase in trace["phases"]]
        for expected in ("setup.models", "setup.driver", "setup.preflight", "launch.activate", "decide",
                         "finish.close"):
            self.assertIn(expected, names)
        self.assertEqual(run.events[-1]["event"], "run_finished")


class SnapshotCacheTests(unittest.TestCase):
    def fresh(self, snapshot):
        from dataclasses import replace
        clone = replace(snapshot)
        clone.__dict__.pop("_fingerprints", None)
        return clone

    def test_cached_fingerprints_follow_every_mutation(self):
        snapshot = screen("Settings", ("General", "Privacy"))
        first, content = snapshot.fingerprint, snapshot.content_fingerprint
        self.assertEqual(snapshot.fingerprint, first)
        mutations = [lambda s: setattr(s, "bundle_id", "com.other.app"),
                     lambda s: setattr(s, "revision", "r2"),
                     lambda s: setattr(s, "text", s.text + "!"),
                     lambda s: s.elements.__setitem__(0, Element("0", "Changed", "StaticText", (.3, .05, .4, .04))),
                     lambda s: s.elements.append(Element("9", "New", "Cell", (.1, .9, .5, .05)))]
        for mutate in mutations:
            snapshot = screen("Settings", ("General", "Privacy"))
            snapshot.fingerprint, snapshot.content_fingerprint  # warm the cache
            mutate(snapshot)
            self.assertEqual(snapshot.fingerprint, self.fresh(snapshot).fingerprint)
            self.assertEqual(snapshot.content_fingerprint, self.fresh(snapshot).content_fingerprint)
        revised = screen("Settings", ("General", "Privacy"))
        revised.content_fingerprint
        revised.revision = "r3"
        self.assertEqual(revised.content_fingerprint, content)  # revision is not content
        self.assertNotEqual(revised.fingerprint, first)

    def test_public_is_unchanged_and_the_guard_still_refuses_doctype(self):
        from dataclasses import asdict
        from mobile_agent.state import from_wda
        snapshot = screen("Settings", ("General",))
        # enabled / selected / placeholder come only from rich sources (the frontier) and stay out of public().
        legacy = [{**{k: v for k, v in asdict(e).items()
                      if k not in ("locator", "hit", "enabled", "selected", "placeholder")},
                   "rect": list(e.rect), "actions": list(e.actions)} for e in snapshot.elements]
        self.assertEqual(json.dumps(snapshot.public()["elements"]), json.dumps(legacy))
        for bad in ('<!DOCTYPE x><XCUIElementTypeApplication/>', '<!doctype x><a/>', '<!EnTiTy x>'):
            with self.assertRaises(ValueError):
                from_wda(bad)


class CompletionPixelTests(unittest.TestCase):
    def run_done(self, still_since):
        phone = Phone()
        phone.at = "general"
        phone.frame_clock_mode = "on"
        phone.frame_clock = Mock(still_for=Mock(return_value=still_since))
        events = []
        result = Agent(phone, Model(), emit=events.append).run("Open General", execute=True)
        return result, phone.reads, [event["event"] for event in events]

    def test_a_pixel_still_screen_is_not_read_again_to_confirm_done(self):
        result, reads, kinds = self.run_done(60.0)  # still for the last minute
        self.assertEqual(result["status"], "completed_unverified")
        self.assertIn("completion_reread_skipped_pixels_still", kinds)
        self.assertEqual(reads, 1)

    def test_moving_or_unknown_pixels_keep_the_reread(self):
        for still in (None, 0.0):  # unknown, or moved just now
            result, reads, kinds = self.run_done(still)
            self.assertEqual(result["status"], "completed_unverified")
            self.assertNotIn("completion_reread_skipped_pixels_still", kinds)
            self.assertEqual(reads, 2)


class ParallelHarnessTests(unittest.TestCase):
    def test_jobs_run_concurrently_one_worker_per_phone_graded_by_that_phones_truth(self):
        from types import SimpleNamespace
        from mobile_agent.evals import harness

        tasks = [SimpleNamespace(id=f"t{i}", category="nav") for i in range(4)]
        seen, active, peak = [], [0], [0]
        lock = threading.Lock()

        def fake_attempt(task, client, probe, index, yield_url=None):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
                seen.append((client.wda_url, task.id))
            time.sleep(.05)
            with lock:
                active[0] -= 1
            return {"task": task.id, "category": "nav", "attempt": index, "passed": True, "oracles": [],
                    "elapsed_ms": 50}

        args = SimpleNamespace(devices="id=a,wda=http://127.0.0.1:8100,truth=iphone15pro;"
                                       "id=b,wda=http://127.0.0.1:8101,truth=iphone15pro",
                               repeats=1, env_file=None, replay_dir=None, memo_dir=None, yield_to=None)
        with tempfile.TemporaryDirectory() as root, \
                patch.object(harness, "attempt", fake_attempt), \
                patch("mobile_agent.evals.oracles.WDAProbe", lambda url: SimpleNamespace(url=url)), \
                patch("mobile_agent.pool.probe_wda", lambda *a, **k: True), \
                patch("mobile_agent.journal.Lease.device",
                      side_effect=lambda key: __import__("mobile_agent.journal", fromlist=["Lease"]).Lease(
                          os.path.join(root, key.rsplit(":", 1)[-1] + ".lock"))):
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                records = harness.run_parallel(args, lambda truth: tasks, [])
        self.assertEqual(sorted(r["task"] for r in records), ["t0", "t1", "t2", "t3"])
        self.assertEqual({r["device"] for r in records}, {"a", "b"})
        self.assertEqual(peak[0], 2)


class LatencyReportTests(unittest.TestCase):
    def test_traced_and_legacy_runs_are_attributed_to_the_same_phases(self):
        from mobile_agent.evals.latency_report import analyze
        events = []
        Agent(Phone(), Model(), emit=events.append).run("Open General", execute=True)
        traced = {"id": "t", "goal": "g", "events": events, "created_ms": 0}
        t0 = 1000.0
        legacy = {"id": "l", "goal": "g", "created_ms": 0, "events": [
            {"event": "run_started", "timestamp": t0}, {"event": "app_launch_started", "timestamp": t0 + 200},
            {"event": "app_launch_acknowledged", "timestamp": t0 + 230},
            {"event": "observation", "timestamp": t0 + 800},
            {"event": "inference_started", "call_id": "jev:1", "purpose": "decision", "timestamp": t0 + 801},
            {"event": "inference_finished", "call_id": "jev:1", "purpose": "decision", "latency_ms": 300,
             "timestamp": t0 + 1101},
            {"event": "decision", "operation": "TAP", "timestamp": t0 + 1102},
            {"event": "action_started", "operation": "TAP", "timestamp": t0 + 1350},
            {"event": "action_acknowledged", "timestamp": t0 + 1830},
            {"event": "observation_after_action", "timestamp": t0 + 3000},
            {"event": "result", "timestamp": t0 + 3100}, {"event": "run_finished", "timestamp": t0 + 3270}]}
        report = analyze([traced, legacy])
        self.assertEqual((report["runs"], report["traced_runs"]), (2, 1))
        phases = report["phases"]
        for name in ("observe.first", "decide", "dispatch.TAP", "settle.TAP", "observe.refresh", "finish"):
            self.assertIn(name, phases)
        self.assertEqual(phases["settle.TAP"]["n"], 2)
        self.assertIn("decision@main", report["calls"])
        shares = sum(row["share"] for row in report["critical_path"])
        self.assertAlmostEqual(shares, 100, delta=1.0)


class HelperHedgeTests(unittest.TestCase):
    def test_a_slow_helper_call_is_hedged_without_spending_helper_budget(self):
        env = {"TEXT_MODEL_API_KEY": "offline", "TEXT_MODEL": "some-model", "TEXT_MODEL_PROVIDER": "openai"}
        with patch.dict(os.environ, env):
            helper = models.Helper()
        self.addCleanup(helper.close)
        sent = []

        def fake(http, path, body, timeout, **kwargs):
            sent.append((http is helper.http, kwargs["call_id"]))
            time.sleep(.4 if http is helper.http else .01)
            return {"choices": [], "usage": {}}
        saved = hedge.HEDGERS["helper"]
        hedge.HEDGERS["helper"] = hedge.Hedger(any_floor=.05, any_default=.05,
                                               budget=hedge.HedgeBudget(ratio=1.0, burst=1))
        try:
            with patch.object(models, "request_inference", fake):
                helper.complete([{"role": "user", "content": "x"}], 10, 5, "text")
        finally:
            hedge.HEDGERS["helper"] = saved
        self.assertEqual(helper.calls, 1)  # the twin is not a second helper call against the budget
        self.assertEqual(sorted(call_id for _, call_id in sent), ["helper:1", "helper:1h"])
