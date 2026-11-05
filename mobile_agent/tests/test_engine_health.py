import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent import device_manager as dm
from mobile_agent import engine_health as eh
from mobile_agent import engines, mac_tools, runner_hints, states
from mobile_agent.run import Run
from mobile_agent.tests.fakes import Clock

UDID = "00008110-00000000ABCD0004"


def reading(**facts):
    base = {"chosen": True, "built": True, "expired": False, "exposed": False, "runner": "running", "holding": None,
            "hint": None, "locked": False, "responsive": True, "usb": True, "wifi": False, "wifi_lost": False}
    return {**base, **facts}


class ClassifyPhoneTests(unittest.TestCase):
    def test_every_cause_has_a_state(self):
        cases = [
            (reading(), None),
            (reading(exposed=True), (eh.HELPER_DOWN, "exposed")),
            (reading(expired=True), (eh.HELPER_DOWN, "signature_expired")),
            (reading(chosen=False), (eh.NO_PHONE, "none_chosen")),
            (reading(usb=False), (eh.WAITING_FOR_PHONE, "unplugged")),
            (reading(wifi=True, usb=None, wifi_lost=True), (eh.WAITING_FOR_PHONE, "wifi_lost")),
            (reading(built=False), (eh.HELPER_DOWN, "not_built")),
            (reading(locked=True), (eh.WAITING_FOR_PHONE, "locked")),
            (reading(responsive=False), (eh.WAITING_FOR_PHONE, "not_responding")),
            (reading(runner="starting", holding="unlock"), (eh.WAITING_FOR_PHONE, "locked")),
            (reading(runner="starting"), (eh.WAITING_FOR_PHONE, "helper_restarting")),
            (reading(runner="starting", hint="developer_mode_off"), (eh.WAITING_FOR_PHONE, "developer_mode_off")),
            (reading(runner="failed", hint="trust_needed"), (eh.WAITING_FOR_PHONE, "trust_needed")),
            (reading(runner="starting", hint="ui_automation_off"), (eh.WAITING_FOR_PHONE, "ui_automation_off")),
            (reading(runner="failed"), (eh.HELPER_DOWN, "failing")),
            (reading(runner="stopped"), (eh.HELPER_DOWN, "stopped")),
        ]
        for facts, expected in cases:
            self.assertEqual(eh.classify_phone(facts), expected, facts)

    def test_unknown_facts_are_never_a_reason(self):
        self.assertIsNone(eh.classify_phone(reading(usb=None, locked=None, responsive=None)))
        self.assertIsNone(eh.classify_phone(reading(wifi=True, usb=False)))

    def test_every_pair_has_words_and_a_next_step(self):
        for row in states.ROWS:
            message, step, _need = eh.words(row.state, row.cause, "Studio iPhone")
            self.assertTrue(message and step, row.cause)
        self.assertEqual(eh.words(eh.WAITING_FOR_PHONE, "locked")[0], "Your iPhone is locked.")
        self.assertEqual(eh.words(eh.WAITING_FOR_PHONE, "locked", "Work iPhone")[1],
                         "Unlock Work iPhone: the task goes on as soon as it's unlocked.")

    def test_the_helper_log_names_only_causes_with_words(self):
        for _needle, cause, _hint in runner_hints.RUNNER_HINTS:
            self.assertTrue(cause is None or cause in eh.PHONE_CAUSES, cause)

    def test_the_model(self):
        self.assertIsNone(eh.classify_model(True, None))
        self.assertEqual(eh.classify_model(False, "Connect your AI account in Setup", has_key=False),
                         (eh.MODEL_UNAVAILABLE, "no_key", "Connect your AI account in Setup"))
        credit = "Your Claude account is out of credit."
        self.assertEqual(eh.classify_model(False, credit, (credit,))[1], "no_credit")
        self.assertEqual(eh.classify_model(False, "Claude didn't accept your key.", (credit,))[1], "key_rejected")


class StateMachineTests(unittest.TestCase):
    def setUp(self):
        self.clock, self.events = Clock(), []
        self.health = eh.EngineHealth(self.clock, lambda topic, event: self.events.append((topic, event)), boot="ab12")

    def test_ready_when_nothing_is_wrong(self):
        snapshot = self.health.snapshot()
        self.assertEqual((snapshot["state"], snapshot["cause"], snapshot["issues"]), (eh.READY, None, []))
        self.assertEqual(self.events, [])

    def test_a_cause_has_a_message_a_next_step_and_a_start(self):
        self.health.report("phone:primary", eh.WAITING_FOR_PHONE, "locked", phone="Work iPhone", device="d1")
        snapshot = self.health.snapshot()
        self.assertEqual((snapshot["state"], snapshot["cause"]), (eh.WAITING_FOR_PHONE, "locked"))
        self.assertEqual(snapshot["message"], "Work iPhone is locked.")
        self.assertIn("Unlock Work iPhone", snapshot["next_step"])
        self.assertEqual((snapshot["since"], snapshot["device"], snapshot["need"]), (1000.0, "d1", "unlock"))
        self.assertEqual(snapshot["liveness"]["boot"], "ab12")

    def test_since_is_kept_while_the_state_and_cause_stay_and_restarts_when_they_change(self):
        self.health.report("phone:primary", eh.WAITING_FOR_PHONE, "locked")
        self.clock.now = 1007.0
        self.health.report("phone:primary", eh.WAITING_FOR_PHONE, "locked")
        self.assertEqual(self.health.snapshot()["since"], 1000.0)
        self.health.report("phone:primary", eh.WAITING_FOR_PHONE, "unplugged")
        self.assertEqual(self.health.snapshot()["since"], 1007.0)

    def test_a_change_publishes_one_event_and_a_repeat_publishes_none(self):
        self.health.report("phone:primary", eh.WAITING_FOR_PHONE, "locked")
        self.health.report("phone:primary", eh.WAITING_FOR_PHONE, "locked")
        self.health.report("phone:primary", eh.WAITING_FOR_PHONE, "unplugged")
        self.health.clear("phone:primary")
        self.health.clear("phone:primary")
        self.assertEqual([event["cause"] for _topic, event in self.events], ["locked", "unplugged", None])
        self.assertEqual({topic for topic, _event in self.events}, {"engine"})
        self.assertEqual(self.events[-1][1]["state"], eh.READY)
        self.assertEqual(self.events[0][1]["event"], "engine_health")

    def test_the_most_blocking_state_is_said_first_and_the_rest_are_listed(self):
        self.health.report("phone:primary", eh.WAITING_FOR_PHONE, "locked")
        self.health.report("live", eh.LIVE_OFF, "disabled")
        self.health.report("model", eh.MODEL_UNAVAILABLE, "no_key", message="Connect your AI account in Setup")
        snapshot = self.health.snapshot()
        self.assertEqual(snapshot["state"], eh.MODEL_UNAVAILABLE)
        self.assertEqual(snapshot["message"], "Connect your AI account in Setup")
        self.assertEqual([issue["state"] for issue in snapshot["issues"]],
                         [eh.MODEL_UNAVAILABLE, eh.LIVE_OFF, eh.WAITING_FOR_PHONE])
        self.health.report("engine", eh.STOPPING, "closing")
        self.assertEqual(self.health.snapshot()["state"], eh.STOPPING)
        self.health.clear("engine")
        self.health.clear("model")
        self.assertEqual(self.health.snapshot()["state"], eh.LIVE_OFF)

    def test_the_asker_names_a_silent_process(self):
        self.assertIsNone(eh.from_silence(1.0))
        self.assertEqual(eh.from_silence(eh.SLOW_AFTER_S)["state"], eh.SLOW)
        self.assertEqual(eh.from_silence(eh.FROZEN_AFTER_S)["state"], eh.FROZEN)
        self.assertEqual(eh.from_silence(None, refused=True)["state"], eh.DOWN)
        self.assertEqual(eh.from_silence(0.5, boot="b", seen_boot="a")["state"], eh.RESTARTING)
        for state in (eh.SLOW, eh.FROZEN, eh.DOWN, eh.RESTARTING):
            self.assertIn(state, states.STATES)
        # The state is named before the 10 s bar.
        self.assertLess(eh.FROZEN_AFTER_S, eh.NAMED_WITHIN_S)


class TaskWaitTests(unittest.TestCase):
    def run_with(self, *events):
        run = Run({"id": "any", "name": "Any app"}, "Open Notes", "live", engine="smart", device_id="d1",
                  device_name="Work iPhone")
        for event in events:
            run.emit(dict(event))
        return run

    DECISION = {"event": "frontier_decision", "step": 3, "chunk": 1, "operation": "TAP", "ops": ["TAP"]}
    ACTION = {"event": "frontier_action", "step": 3, "index": 0, "operation": "TAP"}
    WAIT = {"event": "handoff_waiting", "kind": "lock_screen", "seconds": 120, "midrun": True}

    def test_a_wait_mid_action_is_an_unknown_action_and_says_it_is_not_resent(self):
        run = self.run_with(self.DECISION, self.WAIT)
        self.assertTrue(eh.action_in_flight(run.events))
        runtime = fake_runtime(runs={run.id: run})
        eh.Sampler(runtime).sample()
        issue = runtime.engine_health.snapshot()["issues"][0]
        self.assertEqual((issue["state"], issue["cause"], issue["task"], issue["midrun"]),
                         (eh.WAITING_FOR_PHONE, "locked", run.id, True))
        self.assertTrue(issue["action_unknown"])
        self.assertIn("doesn't send that action again", issue["next_step"])
        self.assertEqual(issue["device_name"], "Work iPhone")

    def test_a_wait_between_actions_has_no_unknown_action(self):
        run = self.run_with(self.DECISION, self.ACTION, self.WAIT)
        self.assertFalse(eh.action_in_flight(run.events))
        runtime = fake_runtime(runs={run.id: run})
        eh.Sampler(runtime).sample()
        self.assertNotIn("action_unknown", runtime.engine_health.snapshot()["issues"][0])

    def test_an_unreadable_decision_counts_as_in_flight(self):
        self.assertTrue(eh.action_in_flight([{"event": "frontier_decision", "ops": "TAP"}]))
        self.assertFalse(eh.action_in_flight([{"event": "frontier_decision", "ops": ["DONE"]}]))

    def test_the_wait_ends_with_the_task_and_never_resends(self):
        run = self.run_with(self.DECISION, self.WAIT)
        runtime = fake_runtime(runs={run.id: run})
        sampler = eh.Sampler(runtime)
        sampler.sample()
        self.assertEqual(runtime.engine_health.snapshot()["state"], eh.WAITING_FOR_PHONE)
        run.emit({"event": "handoff_finished", "kind": "lock_screen", "ok": True})
        sampler.sample()
        self.assertEqual(runtime.engine_health.snapshot()["state"], eh.READY)
        self.assertEqual([e["event"] for e in run.events], ["frontier_decision", "handoff_waiting", "handoff_finished"])

    def test_the_task_words_are_the_ones_mcp_passes_on(self):
        self.assertEqual(eh.task_wait("unplugged", "Work iPhone"),
                         ("plug_in", "Plug Work iPhone back into the Mac: the task goes on when it's back."))
        self.assertIsNone(eh.task_wait("face_id"))


def fake_runtime(manager=None, runs=None, enable_live=True, model=(True, None), closing=False,
                 wda_url="http://127.0.0.1:8100"):
    runtime = SimpleNamespace(
        runs=runs or {}, manager=manager, pool=None, fleet=None, closing=closing, scripted_models=lambda: False,
        config=SimpleNamespace(enable_live=enable_live, wda_url=wda_url),
        engines_status=lambda: {"smart": {"available": model[0], "reason": model[1]}},
        target_status=lambda: {"ready": True})
    runtime.engine_health = eh.EngineHealth(Clock())
    return runtime


class SamplerTests(unittest.TestCase):
    def test_the_detectors_feed_the_state_and_the_duplicates_agree(self):
        runtime = fake_runtime(model=(False, engines.NO_ANTHROPIC_CREDIT), enable_live=False, wda_url=None)
        with patch.object(engines, "smart_key", return_value="sk-test"):
            eh.Sampler(runtime).sample()
        snapshot = runtime.engine_health.snapshot()
        self.assertEqual([(i["state"], i["cause"]) for i in snapshot["issues"]],
                         [(eh.MODEL_UNAVAILABLE, "no_credit"), (eh.LIVE_OFF, "disabled"),
                          (eh.NO_PHONE, "none_attached")])
        self.assertIn("out of credit", snapshot["message"])

    def test_a_detector_that_raises_never_says_ready_by_clearing_what_stood(self):
        runtime = fake_runtime()
        runtime.engine_health.report("phone:primary", eh.WAITING_FOR_PHONE, "locked")
        runtime.config.wda_url = "http://127.0.0.1:8100"
        runtime.target_status = lambda: 1 / 0
        with self.assertLogs("mobster", level="ERROR"):
            eh.Sampler(runtime).sample()
        self.assertEqual(runtime.engine_health.snapshot()["cause"], "locked")

    def test_closing_is_named(self):
        runtime = fake_runtime(closing=True)
        eh.Sampler(runtime).sample()
        self.assertEqual(runtime.engine_health.snapshot()["state"], eh.STOPPING)


class NamedWithinTenSecondsTests(unittest.TestCase):
    def world(self, onset, cause):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        manager.runner = SimpleNamespace(running=True, alive=True, restarts=0, last_exit=None, held=None)
        clock = Clock(0.0)

        def wda_get(path, timeout):
            if clock.now < onset:
                return {"value": False if path == "/wda/locked" else {"ready": True}}
            if cause == "locked" and path == "/wda/locked":
                return {"value": True}
            if cause == "not_responding" and path == "/wda/locked":
                raise TimeoutError()
            return {"value": {"ready": True}}
        runtime = fake_runtime(manager)
        runtime.engine_health = eh.EngineHealth(clock)
        return manager, clock, runtime, wda_get

    def named_after(self, onset, cause, phase):
        manager, clock, runtime, wda_get = self.world(onset, cause)
        sampler = eh.Sampler(runtime)
        with patch.object(manager, "_wda_get", wda_get), patch.object(manager, "settings",
                                                                      return_value={"udid": UDID}), \
                patch.object(manager, "built", return_value=True), patch.object(manager, "attached",
                                                                                return_value=True), \
                patch.object(manager, "wda_ready", return_value=True):
            step = 0.05
            ticks = int(30 / step)
            for i in range(ticks):
                clock.now = round(i * step, 3)
                if abs((clock.now % dm.PROBE_TICK)) < 1e-6:
                    manager.probe(now=clock.now)
                if abs(((clock.now - phase) % eh.SAMPLE_S)) < 1e-6:
                    sampler.sample()
                    if runtime.engine_health.snapshot()["cause"] == cause:
                        return clock.now - onset
        self.fail(f"{cause} was not named in 30 s")

    def test_locked_is_named_inside_the_bar_whenever_it_begins(self):
        worst = max(self.named_after(onset, "locked", phase) for onset in (0.1, 1.3, 2.9, 4.45, 7.0)
                    for phase in (0.0, 0.5, 0.95))
        self.assertLessEqual(worst, eh.NAMED_WITHIN_S)

    def test_a_phone_that_does_not_answer_is_named_inside_the_bar(self):
        worst = max(self.named_after(onset, "not_responding", 0.25) for onset in (0.1, 2.2, 5.7))
        self.assertLessEqual(worst + dm.PROBE_TIMEOUT, eh.NAMED_WITHIN_S)

    def test_the_detection_budget_adds_up(self):
        probe = dm.PROBE_TICK + dm.PROBE_INTERVAL + 1.5 + dm.PROBE_TIMEOUT
        self.assertLessEqual(probe + eh.SAMPLE_S, eh.NAMED_WITHIN_S)
        self.assertLessEqual(mac_tools.ATTACHED_TTL + eh.SAMPLE_S, eh.NAMED_WITHIN_S)
        self.assertLessEqual(eh.FROZEN_AFTER_S, eh.NAMED_WITHIN_S)
        self.assertLess(eh.SLOW_AFTER_S, eh.FROZEN_AFTER_S)

    def test_a_pulled_cable_and_a_wifi_helper_that_goes_silent(self):
        manager, clock, runtime, wda_get = self.world(0, "locked")
        with patch.object(manager, "settings", return_value={"udid": UDID}), patch.object(manager, "built",
                                                                                          return_value=True), \
                patch.object(manager, "attached", return_value=False):
            self.assertEqual(manager.engine_reading()["usb"], False)
            eh.Sampler(runtime).sample()
            self.assertEqual(runtime.engine_health.snapshot()["cause"], "unplugged")
        manager.transport = "wifi"
        manager.health["ready"] = False
        with patch.object(manager, "settings", return_value={"udid": UDID}), patch.object(manager, "built",
                                                                                          return_value=True), \
                patch.object(manager, "runner_state", return_value={"state": "starting", "error": None}):
            self.assertEqual(eh.classify_phone(manager.engine_reading()), (eh.WAITING_FOR_PHONE, "wifi_lost"))


class HelperLogTests(unittest.TestCase):
    def test_the_log_line_names_the_cause_and_a_later_success_clears_it(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = dm.DeviceManager(directory)
            manager.logs.mkdir(parents=True, exist_ok=True)
            log = manager.logs / "wda-runner.log"
            log.write_text("error: Developer Mode is disabled on this device\n")
            self.assertIn("Developer Mode", manager.runner_hint())
            self.assertEqual(manager.runner_cause, "developer_mode_off")
            log.write_text("error: Developer Mode is disabled\nServerURLHere->http://x<-ServerURLHere\n")
            self.assertIsNone(manager.runner_hint())
            self.assertIsNone(manager.runner_cause)


if __name__ == "__main__":
    unittest.main()
