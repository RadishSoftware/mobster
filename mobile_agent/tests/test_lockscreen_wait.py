import io
import os
import unittest
from unittest import mock

from mobile_agent import lockscreen, narrate, runtime_driver
from mobile_agent.agent_hooks import READY, GuardVerdict
from mobile_agent.api_errors import APIError
from mobile_agent.tests.test_conversations import FakeClient
from mobile_agent.tests.test_lockscreen import FakeWda
from mobile_agent.tests.test_server_engine import READY as READY_TARGET
from mobile_agent.tests.test_server_engine import Base
from mobile_agent.tests.test_sota_e2e import Service
from mobile_agent.threads import cli as chat
from mobile_agent.threads.term import Term

UDID = "00008130-0011223344556677"


class ScriptedLock(FakeWda):
    def __init__(self, *answers):
        super().__init__()
        self.answers = list(answers)

    def _sessionless(self, method, path, body=None, timeout=None):
        if path != "/wda/locked":
            return super()._sessionless(method, path, body, timeout)
        self.requests.append((method, path, body))
        value = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if value is None:
            raise OSError("WDA did not answer")
        return {"value": value}

    def reads(self):
        return [r for r in self.requests if r == ("GET", "/wda/locked", None)]


class FakeTime:
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def waiting_guard(*, wait_s=lockscreen.UNLOCK_WAIT_S, cancelled=None, attached=None, wifi_lost=False, usb=False):
    time = FakeTime()
    events = []
    guard = lockscreen.DetectGuard(device_id=UDID if usb else None, usb=usb, emit=events.append, wait_s=wait_s,
                                   cancelled=cancelled, sleep=time.sleep, clock=time.clock)
    guard.attached = lambda: attached
    guard.wifi_lost = lambda: wifi_lost
    return guard, events, time


class WaitForUnlock(unittest.TestCase):
    def test_a_locked_start_waits_then_runs_the_moment_it_reads_unlocked(self):
        phone = ScriptedLock(True, True, False)
        guard, events, time = waiting_guard()
        self.assertEqual(guard.check(phone, cause="preflight"), READY)
        self.assertEqual([e["event"] for e in events], ["handoff_waiting", "handoff_finished"])
        self.assertEqual(events[0], {"event": "handoff_waiting", "kind": "lock_screen", "seconds": 120})
        self.assertEqual(events[1], {"event": "handoff_finished", "kind": "lock_screen", "ok": True, "waited_s": 2.0})
        self.assertEqual(time.slept, [lockscreen.UNLOCK_POLL_S] * 2)
        self.assertEqual(phone.requests, [("GET", "/wda/locked", None)] * 3)
        self.assertEqual(phone.writes(), [])
        self.assertFalse(lockscreen.CAN_UNLOCK)

    def test_a_phone_that_stays_locked_stops_at_the_timeout_with_the_plain_sentence(self):
        phone = ScriptedLock(True)
        guard, events, time = waiting_guard()
        verdict = guard.check(phone, cause="preflight")
        self.assertEqual(verdict, GuardVerdict("stop", "phone_locked", lockscreen.LOCK_WAIT_TIMED_OUT))
        self.assertEqual(lockscreen.LOCK_WAIT_TIMED_OUT,
                         "Still locked, so the task didn't start. Unlock your iPhone and try again.")
        self.assertEqual(events[-1], {"event": "handoff_finished", "kind": "lock_screen", "ok": False})
        self.assertEqual(time.now - 1000.0, 120.0)
        self.assertEqual(len(phone.reads()), 1 + 120)
        self.assertEqual(phone.writes(), [])

    def test_stop_ends_the_wait_with_no_further_reads(self):
        calls = iter([False, True])
        phone = ScriptedLock(True)
        guard, events, _ = waiting_guard(cancelled=lambda: next(calls, True))
        verdict = guard.check(phone, cause="preflight")
        self.assertEqual(verdict, GuardVerdict("stop", "phone_locked", lockscreen.PHONE_LOCKED))
        self.assertEqual(len(phone.reads()), 1)
        self.assertEqual(events[-1], {"event": "handoff_finished", "kind": "lock_screen", "ok": False})

    def test_a_pulled_cable_stops_at_once_as_unplugged(self):
        phone = ScriptedLock(True, None)
        guard, events, time = waiting_guard(attached=False, usb=True)
        self.assertEqual(guard.check(phone, cause="preflight"),
                         GuardVerdict("stop", "unplugged", lockscreen.UNPLUGGED))
        self.assertEqual(time.slept, [lockscreen.UNLOCK_POLL_S])
        self.assertFalse(events[-1]["ok"])

    def returning_guard(self, plugged, wait_s=lockscreen.UNLOCK_WAIT_S, wifi=False):
        guard, events, time = waiting_guard(wait_s=wait_s, usb=True)
        listing = iter([False] * plugged)
        if wifi:
            guard.attached = lambda: None
            guard.wifi_lost = lambda: True
        else:
            guard.attached = lambda: next(listing, True)
        return guard, events, time

    def test_a_pulled_cable_mid_task_waits_and_carries_on_once_the_helper_answers_again(self):
        phone = ScriptedLock(None, None, None, None, False)
        guard, events, _ = self.returning_guard(plugged=3)
        self.assertEqual(guard.check(phone, cause="resume"), READY)
        self.assertEqual([(e["event"], e["kind"], e.get("ok"), e.get("midrun")) for e in events],
                         [("handoff_waiting", "unplugged", None, True), ("handoff_finished", "unplugged", True, True)])
        self.assertEqual(phone.writes(), [])

    def test_a_phone_back_on_wifi_carries_on_too(self):
        phone = ScriptedLock(None, None, False)
        guard, events, _ = self.returning_guard(plugged=0, wifi=True)
        self.assertEqual(guard.check(phone, cause="lock_suspected"), READY)
        self.assertEqual([(e["kind"], e.get("ok")) for e in events], [("wifi_lost", None), ("wifi_lost", True)])

    def test_a_phone_plugged_back_in_locked_waits_for_the_unlock_as_well(self):
        phone = ScriptedLock(None, True, True, False)
        guard, events, _ = self.returning_guard(plugged=0)
        guard.attached = iter([False, True, True, True, True]).__next__
        self.assertEqual(guard.check(phone, cause="resume"), READY)
        self.assertEqual([(e["event"], e["kind"]) for e in events],
                         [("handoff_waiting", "unplugged"), ("handoff_finished", "unplugged"),
                          ("handoff_waiting", "lock_screen"), ("handoff_finished", "lock_screen")])

    def test_a_cable_left_out_stops_ready_to_pick_up(self):
        phone = ScriptedLock(None)
        guard, events, time = self.returning_guard(plugged=10_000)
        self.assertEqual(guard.check(phone, cause="resume"),
                         GuardVerdict("stop", "unplugged", lockscreen.RETURN_WAIT_TIMED_OUT["unplugged"]))
        self.assertEqual(time.now - 1000.0, 120.0)
        self.assertEqual(events[-1]["ok"], False)

    def test_with_no_wait_a_pulled_cable_still_stops_at_once(self):
        phone = ScriptedLock(None)
        guard, events, time = self.returning_guard(plugged=10_000, wait_s=0)
        self.assertEqual(guard.check(phone, cause="resume"), GuardVerdict("stop", "unplugged", lockscreen.UNPLUGGED))
        self.assertEqual((events, time.slept), ([], []))
        guard, events, _ = self.returning_guard(plugged=10_000, wait_s=0, wifi=True)
        self.assertEqual(guard.check(phone, cause="resume"), GuardVerdict("stop", "unplugged", lockscreen.WIFI_LOST))

    def test_stop_ends_the_wait_for_the_cable(self):
        phone = ScriptedLock(None)
        calls = iter([False, False, True])
        guard, events, _ = waiting_guard(usb=True, cancelled=lambda: next(calls, True))
        guard.attached = lambda: False
        self.assertEqual(guard.check(phone, cause="resume"), GuardVerdict("stop", "unplugged", lockscreen.UNPLUGGED))
        self.assertEqual(events[-1]["ok"], False)

    def test_a_dropped_read_with_the_link_up_keeps_waiting(self):
        phone = ScriptedLock(True, None, None, False)
        guard, events, _ = waiting_guard(attached=None)
        self.assertEqual(guard.check(phone, cause="preflight"), READY)
        self.assertTrue(events[-1]["ok"])

    def test_with_no_wait_a_locked_phone_still_stops_at_once_with_todays_sentence(self):
        phone = ScriptedLock(True)
        guard, events, time = waiting_guard(wait_s=0)
        self.assertEqual(guard.check(phone, cause="preflight"),
                         GuardVerdict("stop", "phone_locked", lockscreen.PHONE_LOCKED))
        self.assertEqual((events, time.slept, len(phone.reads())), ([], [], 1))
        for mode in ("schedule", "scripts"):
            guard = lockscreen.guard_for(device_id=None, wda_url="http://127.0.0.1:8100", mode=mode, usb=False,
                                         wait_s=120)
            self.assertEqual(guard.wait_s, 0)

    def test_an_unreadable_phone_never_waits_it_fails_closed(self):
        phone = ScriptedLock(None)
        guard, events, time = waiting_guard(attached=None)
        self.assertEqual(guard.check(phone, cause="preflight"),
                         GuardVerdict("stop", "phone_locked", lockscreen.NOT_RESPONDING))
        self.assertEqual((events, time.slept), ([], []))

    def test_a_lock_during_the_task_waits_then_carries_on_from_there(self):
        for cause in lockscreen.MIDRUN_CAUSES:
            with self.subTest(cause=cause):
                phone = ScriptedLock(True, True, False)
                guard, events, _ = waiting_guard()
                self.assertEqual(guard.check(phone, cause=cause), READY)
                self.assertEqual(events, [
                    {"event": "handoff_waiting", "kind": "lock_screen", "seconds": 120, "midrun": True},
                    {"event": "handoff_finished", "kind": "lock_screen", "ok": True, "waited_s": 2.0, "midrun": True}])
                self.assertEqual(phone.writes(), [])

    def test_a_lock_during_the_task_that_nobody_unlocks_stops_ready_to_pick_up(self):
        phone = ScriptedLock(True)
        guard, events, _ = waiting_guard()
        self.assertEqual(guard.check(phone, cause="resume"),
                         GuardVerdict("stop", "phone_locked", lockscreen.LOCK_WAIT_MIDRUN_TIMED_OUT))
        self.assertEqual(events[-1], {"event": "handoff_finished", "kind": "lock_screen", "ok": False, "midrun": True})
        self.assertEqual(phone.writes(), [])

    def test_with_no_wait_a_lock_during_the_task_still_stops_at_once(self):
        phone = ScriptedLock(True)
        guard, events, time = waiting_guard(wait_s=0)
        self.assertEqual(guard.check(phone, cause="resume"),
                         GuardVerdict("stop", "phone_locked", lockscreen.LOCKED_DURING))
        self.assertEqual((events, time.slept), ([], []))

    def test_the_wait_and_its_end_read_as_one_plain_line_with_no_digits(self):
        narrator = narrate.Narrator()
        lines = [item.text for event in ({"event": "handoff_waiting", "kind": "lock_screen", "seconds": 120},
                                         {"event": "handoff_finished", "kind": "lock_screen", "ok": True,
                                          "waited_s": 4.2},
                                         {"event": "handoff_finished", "kind": "lock_screen", "ok": False})
                 for item in narrator.feed(event)]
        self.assertEqual(lines, ["Look at your iPhone: unlock it and Mobster starts",
                                 "Your iPhone is unlocked: starting"])
        self.assertFalse(any(ch.isdigit() for line in lines for ch in line))
        midrun = [item.text for event in ({"event": "handoff_waiting", "kind": "lock_screen", "seconds": 120,
                                           "midrun": True},
                                          {"event": "handoff_finished", "kind": "lock_screen", "ok": True,
                                           "waited_s": 4.2, "midrun": True})
                  for item in narrate.Narrator().feed(event)]
        self.assertEqual(midrun, ["Your iPhone locked: unlock it and Mobster carries on",
                                  "Your iPhone is unlocked: carrying on"])


class Admission(Base):
    def setUp(self):
        super().setUp()
        session = mock.patch("mobile_agent.runtime_driver.resolve_wda_session", return_value="s")
        session.start()
        self.addCleanup(session.stop)
        os.environ["OPENAI_API_KEY"] = "sk-test-000000000000"

    def locked_runtime(self):
        runtime = self.runtime()
        runtime.target_status = mock.Mock(return_value={**READY_TARGET, "ready": False,
                                                        "health": "Your iPhone is locked."})
        runtime.manager = mock.Mock(health={"locked": True},
                                    engine_reading=lambda: {"chosen": True, "built": True, "runner": "running",
                                                            "responsive": True, "usb": True, "locked": True})
        return runtime

    def test_who_waits(self):
        for origin in ("app", "tui", "cli", "mcp", "remote"):
            self.assertTrue(runtime_driver.waits_for_unlock(origin), origin)
        for origin in ("workflow", "schedule", "api", "hook", None):
            self.assertFalse(runtime_driver.waits_for_unlock(origin), origin)
        for origin in ("app", "mcp", "remote"):
            self.assertFalse(runtime_driver.waits_for_unlock(origin, "workflow:w1:scheduled:2026-10-08T09:00"), origin)
        self.assertTrue(runtime_driver.waits_for_unlock("app", "workflow:w1:manual:abc"))
        self.assertFalse(runtime_driver.person_started("mcp") or runtime_driver.person_started("remote"))
        runtime = self.runtime()
        with mock.patch("mobile_agent.owner_presence.is_owner_active", return_value=True):
            self.assertTrue(runtime.owner_blocks_start(None, "mcp"))
            self.assertTrue(runtime.owner_blocks_start(None, "remote"))
            self.assertFalse(runtime.owner_blocks_start(None, "app"))

    def test_the_mac_app_starts_a_task_on_a_locked_phone_and_it_runs_after_the_unlock(self):
        runtime = self.locked_runtime()
        run = runtime.create("messages", "Text Sam hi", "live", origin="app")
        self.assertEqual(run.origin, "app")
        guard = runtime.make_guard(run, None)
        self.assertEqual(guard.wait_s, lockscreen.UNLOCK_WAIT_S)
        time = FakeTime()
        guard.sleep, guard.clock = time.sleep, time.clock
        phone = ScriptedLock(True, True, False)
        phone.call = mock.Mock()
        runtime_driver.prepare_wda_phone(phone, guard)
        events = [e["event"] for e in run.events]
        self.assertEqual(events[-2:], ["handoff_waiting", "handoff_finished"])
        self.assertTrue(run.events[-1]["ok"])
        self.assertEqual(phone.writes(), [])
        phone.call.assert_called_once_with("POST", "/wda/pressButton", {"name": "home"},
                                           runtime_driver.PREFLIGHT_TIMEOUT)

    def test_a_coding_agent_over_mcp_and_remote_start_on_a_locked_phone_and_wait(self):
        runtime = self.locked_runtime()
        run = runtime.create(None, "What iOS version is this iPhone on?", "live", origin="mcp")
        guard = runtime.make_guard(run, None)
        self.assertEqual(getattr(guard, "inner", guard).wait_s, lockscreen.UNLOCK_WAIT_S)
        run.stop.set()
        remote = runtime.make_guard(mock.Mock(idempotency_key=None, origin="remote"), None)
        self.assertEqual(getattr(remote, "inner", remote).wait_s, lockscreen.UNLOCK_WAIT_S)

    def test_a_schedule_and_the_api_are_still_refused_at_once(self):
        runtime = self.locked_runtime()
        for origin, key in (("api", None), ("hook", None), ("app", "workflow:w1:scheduled:2026-10-08T09:00"),
                            ("mcp", "workflow:w1:scheduled:2026-10-08T09:00")):
            with self.assertRaises(APIError) as refused:
                runtime.create(None, "What iOS version is this iPhone on?", "live", idempotency_key=key,
                               origin=origin)
            error = refused.exception
            self.assertEqual((str(error), error.status, error.details),
                             (lockscreen.PHONE_LOCKED, 503, {"stop": "phone_locked"}))

    def test_a_phone_that_is_not_ready_for_another_reason_is_still_refused(self):
        runtime = self.locked_runtime()
        runtime.manager = mock.Mock(health={"locked": False},
                                    engine_reading=lambda: {"chosen": True, "built": True, "runner": "running",
                                                            "responsive": True, "usb": True, "locked": False})
        with self.assertRaises(APIError) as refused:
            runtime.create(None, "What iOS version is this iPhone on?", "live", origin="app")
        self.assertIn("not ready for live tasks", str(refused.exception))

    def test_a_guard_for_a_schedule_or_the_api_never_waits(self):
        runtime = self.runtime()
        for origin, key in (("app", "workflow:w1:scheduled:2026-10-08T09:00"), ("api", None), ("hook", None)):
            run = mock.Mock(idempotency_key=key, origin=origin)
            guard = runtime.make_guard(run, None)
            self.assertEqual(getattr(guard, "inner", guard).wait_s, 0)


class ChatLine(unittest.TestCase):
    def test_mobster_chat_prints_the_line_the_mac_app_shows(self):
        out = io.StringIO()
        talk = chat.Chat(FakeClient(), Term(io.StringIO(""), out), color=False)
        talk.handle("event", "a" * 12, {"event": "handoff_waiting", "kind": "lock_screen", "seconds": 120})
        talk.handle("event", "a" * 12,
                    {"event": "handoff_finished", "kind": "lock_screen", "ok": True, "waited_s": 3.1})
        self.assertEqual(out.getvalue().splitlines(), ["  ! Look at your iPhone: unlock it and Mobster starts",
                                                       "  – Your iPhone is unlocked: starting"])


class ScriptedWorld(Service):
    def setUp(self):
        super().setUp()
        poll = mock.patch.object(lockscreen, "UNLOCK_POLL_S", 0.02)
        poll.start()
        self.addCleanup(poll.stop)
        self.phone["locked"] = True

    def events(self, run_id):
        return [e["event"] for e in self.run_of(run_id)["events"]]

    def test_a_task_from_the_mac_app_waits_for_the_unlock_then_runs(self):
        _, run_id = self.start("What iOS version is this iPhone on?")
        self.until(lambda: "handoff_waiting" in self.events(run_id), "the wait for the unlock")
        self.assertNotIn("handoff_finished", self.events(run_id))
        self.assertEqual(self.calls, [])
        self.phone["locked"] = False
        run = self.finished(run_id)
        events = [e["event"] for e in run["events"]]
        finished = next(e for e in run["events"] if e["event"] == "handoff_finished")
        self.assertTrue(finished["ok"])
        self.assertTrue(self.calls)
        self.assertNotIn("stop_code", run["summary"] or {})
        self.assertEqual(run["status"], "completed")
        self.assertLess(events.index("handoff_finished"), events.index("inference_started"))
        self.assertLess(events.index("handoff_finished"), events.index("frontier_action"))

    def test_a_task_from_a_script_on_the_api_still_stops_at_once(self):
        status, data = self.call("POST", "/api/runs", {"goal": "What iOS version is this iPhone on?", "mode": "live"},
                                 origin=None)
        self.assertEqual(status, 201, data)
        run = self.finished(data["run"]["id"])
        self.assertEqual((run["summary"]["stop_code"], run["summary"]["reason"]),
                         ("phone_locked", lockscreen.PHONE_LOCKED))
        self.assertNotIn("handoff_waiting", [e["event"] for e in run["events"]])
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
