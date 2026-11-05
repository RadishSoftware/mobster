import json
import os
import tempfile
import threading
import time
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mobile_agent import hedge
from mobile_agent.drivers import WDA
from mobile_agent.evals.latency_report import analyze
from mobile_agent.journal import Lease
from mobile_agent.latency_overlap import temporal_overlap_ms
from mobile_agent.latency_trace import Trace, summarize
from mobile_agent.server import Runtime
from mobile_agent.state import Element, Snapshot, from_wda
from mobile_agent.tests.test_engines import CLEAN, Script
from mobile_agent.tests.test_server_hardening import APP, ParkedWorker
from mobile_agent.tests.timing import bound
from mobile_agent.transport import TransportError


def screen(title, rows=("General",), bundle="com.apple.Preferences"):
    elements = [Element("0", title, "StaticText", (.3, .05, .4, .04), locator=f"/{title}/t")]
    for index, label in enumerate(rows, 1):
        elements.append(Element(str(index), label, "Cell", (.05, .1 * index + .1, .9, .06),
                                locator=f"/{title}/{index}"))
    return Snapshot(elements, "\n".join((title, *rows)), 393, 852, "wda", bundle_id=bundle)


class TraceTests(unittest.TestCase):
    def test_phases_calls_marks_and_summary(self):
        now = [0.0]
        trace = Trace(clock=lambda: now[0], wall=lambda: 1000.0)
        with trace.span("observe.first"):
            now[0] = .2
            with trace.span("observe.refresh"):
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
        self.assertEqual(summary["overlap_ms"], 200.0)
        self.assertTrue(summary["overlap_complete"])
        self.assertEqual((summary["hedges"], summary["hedge_wins"]), (1, 1))
        json.dumps(event)

    def test_trace_is_bounded(self):
        trace = Trace()
        for _ in range(2100):
            trace.phase("x", 0, 0)
        self.assertEqual(len(trace.phases), 2000)
        self.assertEqual(trace.event()["dropped"], {"phases": 100})
        self.assertFalse(trace.event()["summary"]["overlap_complete"])

    def test_temporal_overlap_counts_concurrent_calls_once_and_only_during_work(self):
        phases = [["work", 0, 20], ["wait.backoff", 20, 20], ["work", 40, 60]]
        calls = [["decision", "speculation", 60, 40, {}],
                 ["decision", "prediction", 60, 40, {}],
                 ["decision", "main", 0, 100, {}]]
        self.assertEqual(temporal_overlap_ms(phases, calls, 100), 40)
        self.assertEqual(summarize(phases, calls, 100)["model_off_lane_ms"], 80)
        self.assertEqual(summarize(phases, calls, 100)["overlap_ms"], 40)
        self.assertEqual(temporal_overlap_ms(phases, [["x", "speculation", 105, 20, {}]], 100), 0)
        self.assertEqual(temporal_overlap_ms(phases, [["x", "speculation", 90, 20, {}]], 100), 10)
        self.assertEqual(temporal_overlap_ms(phases, calls, 0), 0)
        report = analyze([{"id": "overlap", "trace": {"wall_ms": 100, "phases": phases,
                                                    "calls": calls, "marks": [], "dropped": {"calls": 1}}}])
        self.assertEqual(report["overlap"]["temporal_ms_per_run"]["mean"], 40)
        self.assertEqual(report["overlap"]["incomplete_trace_runs"], 1)


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
        self.assertLess(time.monotonic() - started, bound(.3))
        self.assertEqual(hedger.budget.hedges, 1)
        time.sleep(.6)
        self.assertEqual(sorted((r[0], r[4]) for r in reports), [(0, False), (1, True)])

    def test_the_budget_caps_hedges(self):
        hedger = self.hedger(ratio=0.0)
        hedger.budget.tokens = 0
        result = hedger.run("decision", lambda index, budget: time.sleep(.12) or index, timeout=2)
        self.assertEqual((result, hedger.budget.hedges), (0, 0))

    def test_a_stalled_primary_gets_a_rescue_twin_without_budget(self):
        hedger = hedge.Hedger(floor={"decision": .05}, default={"decision": .05}, stall_min=.2,
                              budget=hedge.HedgeBudget(ratio=0.0, burst=0))

        def attempt(index, budget):
            time.sleep(3 if index == 0 else .01)
            return f"answer-{index}"
        started = time.monotonic()
        self.assertEqual(hedger.run("decision", attempt, timeout=5), "answer-1")
        self.assertLess(time.monotonic() - started, bound(1.0))
        self.assertEqual((hedger.rescues, hedger.budget.hedges), (1, 0))

    def test_a_hedged_call_whose_attempts_all_stall_is_resent_once(self):
        seen = []
        in_flight = threading.Barrier(2, timeout=2)

        def attempt(index, budget):
            seen.append(index)
            if index < 2:
                in_flight.wait()
                raise TransportError("HTTP TimeoutError; Anthropic request outcome unknown")
            return "answer"
        hedger = self.hedger()
        self.assertEqual(hedger.run("decision", attempt, timeout=3), "answer")
        self.assertEqual(sorted(seen), [0, 1, 2])
        self.assertEqual((hedger.budget.hedges, hedger.resends), (1, 1))

    def test_a_slow_primary_short_of_a_stall_is_not_rescued(self):
        hedger = hedge.Hedger(floor={"decision": .05}, default={"decision": .05}, stall_min=1.0,
                              budget=hedge.HedgeBudget(ratio=0.0, burst=0))
        seen = []

        def attempt(index, budget):
            seen.append(index)
            time.sleep(.3)
            return index
        self.assertEqual(hedger.run("decision", attempt, timeout=5), 0)
        self.assertEqual((seen, hedger.rescues), ([0], 0))

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
        self.assertTrue(hedge.transient(TransportError("HTTP ConnectionResetError; "
                                                       "request outcome unknown; not retried")))
        self.assertTrue(hedge.transient(TransportError("Incomplete HTTP response; outcome unknown; not retried")))
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


class SnapshotCacheTests(unittest.TestCase):
    def fresh(self, snapshot):
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
            snapshot.fingerprint, snapshot.content_fingerprint
            mutate(snapshot)
            self.assertEqual(snapshot.fingerprint, self.fresh(snapshot).fingerprint)
            self.assertEqual(snapshot.content_fingerprint, self.fresh(snapshot).content_fingerprint)
        revised = screen("Settings", ("General", "Privacy"))
        revised.content_fingerprint
        revised.revision = "r3"
        self.assertEqual(revised.content_fingerprint, content)
        self.assertNotEqual(revised.fingerprint, first)

    def test_public_is_unchanged_and_the_guard_still_refuses_doctype(self):
        snapshot = screen("Settings", ("General",))
        legacy = [{**{k: v for k, v in asdict(e).items()
                      if k not in ("locator", "hit", "enabled", "selected", "placeholder")},
                   "rect": list(e.rect), "actions": list(e.actions)} for e in snapshot.elements]
        self.assertEqual(json.dumps(snapshot.public()["elements"]), json.dumps(legacy))
        for bad in ('<!DOCTYPE x><XCUIElementTypeApplication/>', '<!doctype x><a/>', '<!EnTiTy x>'):
            with self.assertRaises(ValueError):
                from_wda(bad)


class ServerWiringTests(unittest.TestCase):
    def test_work_builds_the_client_first_traces_every_phase_and_emits_the_trace(self):
        order, seen = [], {}

        class FakeAgent:
            def run(self, goal, **kwargs):
                seen["goal"] = goal
                return {"status": "completed", "answer": "About is open.", "steps": 1, "actions": 1, "elapsed": 1.0}

        def build_frontier(driver, client, **kwargs):
            order.append("agent")
            seen.update(kwargs)
            return FakeAgent()

        with tempfile.TemporaryDirectory() as root, \
                patch("mobile_agent.server.threading.Thread", ParkedWorker), \
                patch("mobile_agent.journal.Lease.device", side_effect=lambda _: Lease(Path(root) / "d.lock")), \
                patch.dict(os.environ, {**CLEAN, "ANTHROPIC_API_KEY": "sk-ant-test-000000000000"}, clear=True):
            config = SimpleNamespace(wda_url="http://127.0.0.1:8100", session=None,
                                     ocr=False, enable_live=True, port=8765, state_db=str(Path(root) / "r.sqlite3"))
            runtime = Runtime(config)
            self.addCleanup(runtime.close)
            runtime.apps = Mock(return_value=[dict(APP)])
            runtime.status = Mock(return_value={"live_enabled": True, "helper_configured": False})
            runtime.wda_session = Mock(return_value="s")
            run, _ = runtime.create("settings", "Open About", "live", "wiring")
            driver = Mock(spec=WDA)
            driver.last_image = None
            with patch.object(Runtime, "smart_client",
                              lambda _runtime: order.append("client") or Script([[("DONE", None, None)]], [])), \
                    patch("mobile_agent.runtime_driver.build_target_driver",
                          side_effect=lambda **k: order.append("driver") or driver), \
                    patch("mobile_agent.runtime_driver.prepare_wda_phone"), \
                    patch("mobile_agent.runtime_driver.attach_frame_clock"), \
                    patch("mobile_agent.engines.build_frontier", build_frontier):
                runtime.work(run)
            if run.lease:
                run.lease.close()
                run.lease = None
        self.assertEqual(order, ["client", "driver", "agent"])
        self.assertEqual(seen["goal"], "In Settings: Open About")
        driver.call.assert_any_call("POST", "/wda/apps/activate", {"bundleId": "com.apple.Preferences"})
        trace = next(event for event in run.events if event["event"] == "latency_trace")
        names = [phase[0] for phase in trace["phases"]]
        for expected in ("setup.models", "setup.driver", "setup.preflight", "launch.activate", "smart.run",
                         "finish.close"):
            self.assertIn(expected, names)
        self.assertEqual(run.events[-1]["event"], "run_finished")


class LatencyReportTests(unittest.TestCase):
    def test_traced_and_legacy_runs_are_attributed_to_the_same_phases(self):
        now = [0.0]
        trace = Trace(clock=lambda: now[0], wall=lambda: 1000.0)
        for name, seconds in (("observe.first", .6), ("decide", .3), ("dispatch.TAP", .25), ("settle.TAP", .7),
                              ("observe.refresh", .1), ("finish", .05)):
            with trace.span(name):
                now[0] += seconds
        trace.call("decision", .6, .9, lane="main")
        traced = {"id": "t", "goal": "g", "events": [trace.event(), {"event": "result"}], "created_ms": 0}
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


if __name__ == "__main__":
    unittest.main()
