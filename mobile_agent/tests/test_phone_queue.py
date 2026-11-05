import os
import unittest
from pathlib import Path
from unittest.mock import patch

from mobile_agent import ownership
from mobile_agent.api_errors import APIError
from mobile_agent.harness import control
from mobile_agent.journal import JournalError
from mobile_agent.server import Runtime, make_handler, may_have_acted
from mobile_agent.steering import SteeringQueue
from mobile_agent.tests.seam_support import request
from mobile_agent.tests.test_fleet import B_URL, DOWN, SIM_A, SIM_B, Harness
from mobile_agent.tests.test_frontier import Driver, Script
from mobile_agent.tests.test_harness_steering import agent, rows


class Base(Harness):
    def setUp(self):
        super().setUp()
        self.now = [10_000.0]
        self.runtime_ = None

    def runtime(self, **config):
        runtime = super().runtime(**config)
        runtime.queues.clock = lambda: self.now[0]
        return runtime

    def phone(self, runtime, device):
        found = runtime.fleet.find(device)
        return found[0] if isinstance(found, tuple) else found

    def end(self, runtime, run, status="completed"):
        run.finish({"event": "result", "status": status})
        if run.lease:
            run.lease.close()
            run.lease = None
        with runtime.lock:
            runtime.active_runs.pop(run.id, None)
        runtime.run_ended(run)

    def call(self, runtime, method, path, body=None, session=None, sent=None):
        status, data, _ = request(make_handler(runtime, app_session=session), method, path, body, headers=sent)
        return status, data


class QueueingTests(Base):
    def test_a_queue_listener_failure_removes_only_the_new_waiting_request(self):
        runtime = self.runtime()
        holder = runtime.create("settings", "Open About", "live", device=SIM_B)
        with patch.object(runtime.listeners, "post", side_effect=RuntimeError("no threads")):
            with self.assertRaises(APIError) as failed:
                runtime.create("settings", "Open General", "live", device=SIM_B, queue=True)
        self.assertEqual(failed.exception.code, "start_failed")
        waiting = next(run for run in runtime.runs.values() if run is not holder)
        self.assertEqual(waiting.status, "interrupted")
        self.assertEqual(runtime.queues.total(), 0)
        self.assertEqual(list(runtime.active_runs), [holder.id])

    def test_a_second_request_queues_with_its_number_instead_of_being_refused(self):
        runtime = self.runtime()
        holder = runtime.create("settings", "Open About", "live", device=SIM_B, origin="app")
        waiting = runtime.create("settings", "Open General", "live", device=SIM_B, origin="mcp", queue=True)
        third = runtime.create("settings", "Open Display", "live", device=SIM_B, origin="remote", queue=True)
        self.assertEqual((waiting.status, third.status), ("queued", "queued"))
        info = waiting.public()["queue"]
        self.assertEqual((info["position"], info["behind"]), (1, "a task in Mobster"))
        self.assertEqual(info["message"], "Waiting for a task in Mobster on Mobster · iPhone 17 Pro · iOS 26.4 · 2, "
                                           "you are number 1.")
        self.assertEqual(third.public()["queue"]["position"], 2)
        self.assertEqual(list(runtime.active_runs), [holder.id])
        self.assertEqual(self.leases, [B_URL])
        self.assertIsNone(waiting.lease)
        self.assertIn(waiting.id, runtime.runs)
        self.assertEqual([e["event"] for e in waiting.events], ["queued"])

    def test_the_device_record_names_the_holder_and_the_line(self):
        runtime = self.runtime()
        holder = runtime.create("settings", "Open About", "live", device=SIM_B, origin="mcp")
        waiting = runtime.create("settings", "Open General", "live", device=SIM_B, origin="app", queue=True)
        _, body = self.call(runtime, "GET", "/api/devices")
        record = next(item for item in body["devices"] if item["id"] == SIM_B)
        self.assertEqual(record["holder"]["kind"], "mcp")
        self.assertEqual(record["holder"]["runId"], holder.id)
        self.assertFalse(record["holder"]["stopping"])
        self.assertEqual([(q["id"], q["position"], q["kind"]) for q in record["queue"]], [(waiting.id, 1, "task")])
        other = next(item for item in body["devices"] if item["id"] == SIM_A)
        self.assertEqual((other["holder"], other["queue"]), (None, []))
        status, one = self.call(runtime, "GET", f"/api/devices/{SIM_B}")
        self.assertEqual(one["device"]["holder"]["runId"], holder.id)

    def test_the_holder_finishing_starts_the_next_in_order(self):
        runtime = self.runtime()
        holder = runtime.create("settings", "Open About", "live", device=SIM_B)
        first = runtime.create("settings", "Open General", "live", device=SIM_B, queue=True)
        second = runtime.create("settings", "Open Display", "live", device=SIM_B, queue=True)
        self.end(runtime, holder)
        self.assertEqual(list(runtime.active_runs), [first.id])
        self.assertIsNone(first.queue_info)
        self.assertEqual(self.leases, [B_URL, B_URL])
        self.assertEqual(second.public()["queue"]["position"], 1)
        self.assertEqual(second.public()["queue"]["behind"], "an app using the local API")
        self.assertIn("dequeued", [e["event"] for e in first.events])
        self.end(runtime, first)
        self.assertEqual(list(runtime.active_runs), [second.id])
        self.assertNotIn("queue", second.public())

    def test_a_free_phone_starts_a_queueing_request_at_once(self):
        runtime = self.runtime()
        run = runtime.create("settings", "Open About", "live", device=SIM_B, queue=True)
        self.assertEqual(list(runtime.active_runs), [run.id])
        self.assertNotIn("queue", run.public())

    def test_two_phones_keep_two_lines(self):
        runtime = self.runtime()
        on_b = runtime.create("settings", "Open About", "live", device=SIM_B)
        on_a = runtime.create("settings", "Open General", "live", device=SIM_A)
        behind_b = runtime.create("settings", "Open Display", "live", device=SIM_B, queue=True)
        behind_a = runtime.create("settings", "Open Sounds", "live", device=SIM_A, queue=True)
        self.assertEqual((behind_b.queue_info["position"], behind_a.queue_info["position"]), (1, 1))
        self.end(runtime, on_a)
        self.assertEqual(set(runtime.active_runs), {on_b.id, behind_a.id})
        self.assertEqual(behind_b.status, "queued")
        self.assertIsNotNone(behind_b.queue_info)

    def test_the_first_phone_queues_too(self):
        runtime = self.runtime()
        holder = runtime.create("settings", "Open About", "live", device=SIM_A, origin="mcp")
        waiting = runtime.create("settings", "Open General", "live", device=SIM_A, queue=True)
        self.assertEqual(waiting.queue_info["behind"], "an MCP client")
        self.end(runtime, holder)
        self.assertEqual(list(runtime.active_runs), [waiting.id])

    def test_a_phone_with_no_device_list_queues_on_the_runtime(self):
        runtime = self.runtime()
        holder = runtime.create("settings", "Open About", "live")
        waiting = runtime.create("settings", "Open General", "live", queue=True)
        self.assertEqual(waiting.queue_info["position"], 1)
        self.end(runtime, holder)
        self.assertEqual(list(runtime.active_runs), [waiting.id])


class WhoIsAskingTests(Base):
    def test_an_mcp_client_is_named_as_it_names_itself_and_nothing_more(self):
        runtime = self.runtime()
        body = {"appId": "settings", "goal": "Open About", "device": SIM_B}
        sent = {"X-Mobster-Origin": "mcp", "X-Mobster-Client": "claude-code"}
        status, answer = self.call(runtime, "POST", "/api/runs", body, sent=sent)
        self.assertEqual(status, 201, answer)
        holder = runtime.holder_of(self.phone(runtime, SIM_B))
        self.assertEqual((holder["kind"], holder["label"]), ("mcp", "Claude Code"))
        self.end(runtime, runtime.runs[answer["run"]["id"]])
        status, answer = self.call(runtime, "POST", "/api/runs", {**body, "goal": "Open General"},
                                   sent={"X-Mobster-Origin": "app", "X-Mobster-Client": "claude-code"})
        self.assertEqual(runtime.holder_of(self.phone(runtime, SIM_B))["label"], "a task in Mobster")
        self.end(runtime, runtime.runs[answer["run"]["id"]])
        status, answer = self.call(runtime, "POST", "/api/runs", {**body, "goal": "Open Display"},
                                   sent={"X-Mobster-Origin": "mcp", "X-Mobster-Client": "<script>x</script>"})
        self.assertNotIn("<", runtime.holder_of(self.phone(runtime, SIM_B))["label"])


class RefusalsStayTests(Base):
    def test_the_api_queues_by_default_and_queue_false_refuses(self):
        runtime = self.runtime()
        body = {"appId": "settings", "goal": "Open General", "device": SIM_B}
        first = runtime.create("settings", "Open About", "live", device=SIM_B)
        status, answer = self.call(runtime, "POST", "/api/runs", body)
        self.assertEqual((status, answer["run"]["status"], answer["run"]["queue"]["position"]), (201, "queued", 1))
        status, answer = self.call(runtime, "POST", "/api/runs", {**body, "goal": "Open Display", "queue": False})
        self.assertEqual((status, answer["code"], answer["activeRunId"]), (409, "run_active", first.id))
        status, answer = self.call(runtime, "POST", "/api/runs", {**body, "goal": "Open Sounds", "queue": "no"})
        self.assertEqual(status, 400)

    def test_a_phone_held_by_another_process_is_not_queued(self):
        runtime = self.runtime()
        self.held_elsewhere.add(B_URL)
        with self.assertRaises(APIError) as busy:
            runtime.create("settings", "Open About", "live", device=SIM_B, queue=True)
        self.assertEqual(busy.exception.code, "device_busy")
        self.assertIn("another Mobster process", str(busy.exception))
        self.assertEqual(runtime.queues.total(), 0)

    def test_an_idempotency_key_answers_the_request_already_queued(self):
        runtime = self.runtime()
        runtime.create("settings", "Open About", "live", device=SIM_B)
        waiting, replayed = runtime.create("settings", "Open General", "live", "key-1", device=SIM_B, queue=True)
        again, replayed_again = runtime.create("settings", "Open General", "live", "key-1", device=SIM_B, queue=True)
        self.assertEqual((replayed, replayed_again, again.id), (False, True, waiting.id))
        self.assertEqual(runtime.queues.total(), 1)

    def test_a_queue_that_is_full_refuses_the_next_request_with_a_sentence(self):
        runtime = self.runtime()
        runtime.create("settings", "Open About", "live", device=SIM_B)
        for index in range(ownership.QUEUE_LIMIT):
            runtime.create("settings", f"Open page {index}", "live", device=SIM_B, queue=True)
        with self.assertRaises(APIError) as full:
            runtime.create("settings", "One too many", "live", device=SIM_B, queue=True)
        self.assertEqual((full.exception.status, full.exception.code), (429, "queue_full"))
        self.assertIn("10 requests already wait for", str(full.exception))
        self.assertEqual(runtime.queues.total(), ownership.QUEUE_LIMIT)


class CancelTests(Base):
    def test_a_cancelled_request_leaves_the_line_and_never_starts(self):
        runtime = self.runtime()
        holder = runtime.create("settings", "Open About", "live", device=SIM_B)
        gone = runtime.create("settings", "Open General", "live", device=SIM_B, queue=True)
        kept = runtime.create("settings", "Open Display", "live", device=SIM_B, queue=True)
        runtime.stop_run(gone.id)
        self.assertEqual(gone.status, "stopped")
        self.assertIn("Nothing was done on the phone", gone.summary["reason"])
        self.assertFalse(gone.summary["actions_may_have_run"])
        self.assertEqual(kept.queue_info["position"], 1)
        self.end(runtime, holder)
        self.assertEqual(list(runtime.active_runs), [kept.id])
        self.assertEqual(self.leases, [B_URL, B_URL])
        self.assertNotIn("dequeued", [e["event"] for e in gone.events])

    def test_cancelling_over_the_api_works_the_same(self):
        runtime = self.runtime()
        runtime.create("settings", "Open About", "live", device=SIM_B)
        waiting = runtime.create("settings", "Open General", "live", device=SIM_B, queue=True)
        status, answer = self.call(runtime, "POST", f"/api/runs/{waiting.id}/stop", {})
        self.assertEqual((status, answer["run"]["status"]), (200, "stopped"))
        self.assertEqual(runtime.queues.total(), 0)

    def test_the_holder_stopped_by_hand_hands_the_phone_on(self):
        runtime = self.runtime()
        holder = runtime.create("settings", "Open About", "live", device=SIM_B)
        waiting = runtime.create("settings", "Open General", "live", device=SIM_B, queue=True)
        runtime.stop_run(holder.id)
        self.assertTrue(ownership.holder_for(holder)["stopping"])
        self.end(runtime, holder, "stopped")
        self.assertEqual(list(runtime.active_runs), [waiting.id])


class FailClosedTests(Base):
    def test_a_queued_worker_construction_failure_releases_its_phone(self):
        runtime = self.runtime()
        holder = runtime.create("settings", "Open About", "live", device=SIM_B)
        waiting = runtime.create("settings", "Open General", "live", device=SIM_B, queue=True)
        with patch("mobile_agent.server.threading.Thread", side_effect=RuntimeError("no threads")):
            self.end(runtime, holder)
        self.assertEqual(waiting.status, "error")
        self.assertEqual(waiting.summary.get("stop_code"), "start_failed")
        self.assertIsNone(waiting.lease)
        self.assertEqual(runtime.active_runs, {})
        self.assertEqual(runtime.queues.total(), 0)
        replacement = runtime.create("settings", "Open Display", "live", device=SIM_B)
        self.assertEqual(list(runtime.active_runs), [replacement.id])

    def test_a_journal_that_cannot_save_the_dequeued_event_starts_no_worker(self):
        runtime = self.runtime()
        holder = runtime.create("settings", "Open About", "live", device=SIM_B)
        waiting = runtime.create("settings", "Open General", "live", device=SIM_B, queue=True)
        emit = waiting.emit

        def emit_or_fail(event, *args, **kwargs):
            if event.get("event") == "dequeued":
                raise JournalError("The journal is full.")
            return emit(event, *args, **kwargs)

        with patch.object(waiting, "emit", side_effect=emit_or_fail), \
                patch("mobile_agent.server.threading.Thread") as thread, \
                patch.object(runtime, "finished") as finished:
            self.end(runtime, holder)
        thread.assert_not_called()
        self.assertEqual(waiting.status, "error")
        self.assertIn("could not save", waiting.summary["reason"])
        self.assertIn("Nothing was done on the phone", waiting.summary["reason"])
        self.assertIsNone(waiting.lease)
        self.assertEqual(runtime.active_runs, {})
        self.assertEqual(self.leases, [B_URL])
        self.assertEqual([call.args[0] for call in finished.call_args_list if call.args[0] is waiting], [waiting])

    def test_a_phone_that_is_not_ready_when_its_turn_comes_ends_the_request_without_acting(self):
        runtime = self.runtime()
        holder = runtime.create("settings", "Open About", "live", device=SIM_B)
        waiting = runtime.create("settings", "Open General", "live", device=SIM_B, queue=True)
        behind = runtime.create("settings", "Open Display", "live", device=SIM_B, queue=True)
        self.targets[SIM_B] = DOWN
        self.end(runtime, holder)
        for run in (waiting, behind):
            self.assertEqual(run.status, "error", run.id)
            self.assertIn("is not ready for live tasks", run.summary["reason"])
            self.assertFalse(run.summary["actions_may_have_run"])
            self.assertEqual(run.summary["actions"], 0)
            self.assertNotIn("run_started", [e["event"] for e in run.events])
        self.assertEqual(runtime.active_runs, {})
        self.assertEqual(self.leases, [B_URL])
        self.assertEqual(runtime.queues.total(), 0)

    def test_a_lease_taken_by_another_process_while_it_waited_ends_the_request(self):
        runtime = self.runtime()
        holder = runtime.create("settings", "Open About", "live", device=SIM_B)
        waiting = runtime.create("settings", "Open General", "live", device=SIM_B, queue=True)
        self.held_elsewhere.add(B_URL)
        self.end(runtime, holder)
        self.assertEqual(waiting.status, "error")
        self.assertIn("another Mobster process", waiting.summary["reason"])
        self.assertIn("Nothing was done on the phone", waiting.summary["reason"])
        self.assertEqual(runtime.active_runs, {})

    def test_the_first_phone_not_ready_ends_the_request_too(self):
        runtime = self.runtime()
        holder = runtime.create("settings", "Open About", "live", device=SIM_A)
        waiting = runtime.create("settings", "Open General", "live", device=SIM_A, queue=True)
        runtime.target_status.return_value = DOWN
        self.targets[SIM_A] = DOWN
        self.end(runtime, holder)
        self.assertEqual(waiting.status, "error")
        self.assertIn("not ready", waiting.summary["reason"])
        self.assertEqual(runtime.active_runs, {})

    def test_a_disconnected_phone_ends_the_request(self):
        runtime = self.runtime()
        holder = runtime.create("settings", "Open About", "live", device=SIM_B)
        waiting = runtime.create("settings", "Open General", "live", device=SIM_B, queue=True)
        runtime.fleet._set_disconnected(SIM_B, True)
        self.end(runtime, holder)
        self.assertEqual(waiting.status, "error")
        self.assertIn("disconnected in Mobster", waiting.summary["reason"])

    def test_an_app_taken_off_the_phones_list_while_it_waited_ends_the_request_without_acting(self):
        runtime = self.runtime()
        phone = self.phone(runtime, SIM_B)
        runtime.fleet.set_prefs(phone, apps=["com.apple.Preferences", "com.apple.mobilesafari"])
        holder = runtime.create("settings", "Open About", "live", device=SIM_B)
        waiting = runtime.create("settings", "Open General", "live", device=SIM_B, queue=True)
        runtime.fleet.set_prefs(phone, apps=["com.apple.mobilesafari"])
        self.end(runtime, holder)
        self.assertEqual(waiting.status, "error")
        self.assertIn("isn't one of the apps Mobster may use", waiting.summary["reason"])
        self.assertIn("did nothing on it", waiting.summary["reason"])
        self.assertEqual(waiting.summary["actions"], 0)
        self.assertEqual(runtime.active_runs, {})
        self.assertEqual(self.leases, [B_URL])

    def test_an_app_still_on_the_phones_list_starts_when_its_turn_comes(self):
        runtime = self.runtime()
        runtime.fleet.set_prefs(self.phone(runtime, SIM_B), apps=["com.apple.Preferences"])
        holder = runtime.create("settings", "Open About", "live", device=SIM_B)
        waiting = runtime.create("settings", "Open General", "live", device=SIM_B, queue=True)
        self.end(runtime, holder)
        self.assertEqual(list(runtime.active_runs), [waiting.id])

    def test_an_app_never_opened_while_it_waited_ends_the_request_without_acting(self):
        runtime = self.runtime()
        holder = runtime.create("settings", "Open About", "live", device=SIM_B)
        waiting = runtime.create("settings", "Open General", "live", device=SIM_B, queue=True)
        with patch.dict(os.environ, {"MOBSTER_BLOCKED_APPS": "com.apple.Preferences"}):
            self.end(runtime, holder)
        self.assertEqual(waiting.status, "error")
        self.assertEqual(waiting.summary["stop_code"], "app_not_allowed")
        self.assertIn("never opens", waiting.summary["reason"])
        self.assertEqual(runtime.active_runs, {})

    def test_an_app_turned_off_for_outside_agents_while_an_mcp_request_waited_ends_it(self):
        runtime = self.runtime()
        holder = runtime.create("settings", "Open About", "live", device=SIM_B)
        waiting = runtime.create("settings", "Open General", "live", device=SIM_B, origin="mcp", queue=True)
        mine = runtime.create("settings", "Open Display", "live", device=SIM_B, origin="app", queue=True)
        with patch.dict(os.environ, {"MOBSTER_OUTSIDE_BLOCKED_APPS": "com.apple.Preferences"}):
            self.end(runtime, holder)
        self.assertEqual(waiting.status, "error")
        self.assertIn("off for agents outside Mobster", waiting.summary["reason"])
        self.assertEqual(list(runtime.active_runs), [mine.id])

    def test_ask_before_acting_turned_on_while_it_waited_applies_when_it_starts(self):
        runtime = self.runtime()
        phone = self.phone(runtime, SIM_B)
        runtime.fleet.set_prefs(phone, askBeforeActing=False)
        holder = runtime.create("settings", "Open About", "live", device=SIM_B)
        waiting = runtime.create("settings", "Open General", "live", device=SIM_B, queue=True)
        self.assertIs(waiting.ask_override, False)
        runtime.fleet.set_prefs(phone, askBeforeActing=True)
        self.end(runtime, holder)
        self.assertEqual(list(runtime.active_runs), [waiting.id])
        self.assertIs(waiting.ask_override, True)
        self.assertTrue(runtime.asks_before_acting(waiting))

    def test_a_request_that_fails_to_start_for_an_unexpected_reason_ends_instead_of_staying_queued(self):
        runtime = self.runtime()
        holder = runtime.create("settings", "Open About", "live", device=SIM_B)
        waiting = runtime.create("settings", "Open General", "live", device=SIM_B, queue=True)
        behind = runtime.create("settings", "Open Display", "live", device=SIM_B, queue=True)
        real = Runtime.start_queued

        def broken(this, item):
            if item["run"] is waiting:
                raise RuntimeError("boom")
            return real(this, item)

        with patch.object(Runtime, "start_queued", broken), self.assertLogs("mobster.harness_api", "ERROR"):
            self.end(runtime, holder)
        self.assertEqual(waiting.status, "error")
        self.assertIn("Nothing was done on the phone", waiting.summary["reason"])
        self.assertEqual(waiting.summary["actions"], 0)
        self.assertEqual(list(runtime.active_runs), [behind.id])
        self.assertEqual(runtime.queues.total(), 0)

    def test_a_request_that_is_not_started_by_a_closing_runtime_acts_on_nothing(self):
        runtime = self.runtime()
        holder = runtime.create("settings", "Open About", "live", device=SIM_B)
        waiting = runtime.create("settings", "Open General", "live", device=SIM_B, queue=True)
        runtime.close()
        self.assertEqual(waiting.status, "interrupted")
        self.assertEqual(waiting.summary["reason"], ownership.QUEUE_RESTART)
        self.assertEqual(waiting.summary["actions"], 0)
        self.assertNotEqual(holder.id, waiting.id)

    def test_a_restart_ends_a_request_that_never_started_and_says_it_did_nothing(self):
        runtime = self.runtime()
        runtime.create("settings", "Open About", "live", device=SIM_B)
        waiting = runtime.create("settings", "Open General", "live", device=SIM_B, queue=True)
        state = runtime.config.state_db
        with patch.object(Runtime, "drop_queues", lambda self, *a, **k: []):
            runtime.close()
        self.assertIsNone(waiting.finished_at)
        again = self.runtime(state=Path(state).parent)
        saved = again.runs[waiting.id]
        self.assertEqual((saved.status, saved.summary["reason"]), ("interrupted", ownership.QUEUE_RESTART))
        self.assertFalse(saved.summary["actions_may_have_run"])
        self.assertEqual(again.queues.total(), 0)
        self.assertEqual(again.active_runs, {})


class ExpiryTests(Base):
    def test_a_request_that_waits_too_long_ends_with_a_plain_message(self):
        runtime = self.runtime()
        runtime.create("settings", "Open About", "live", device=SIM_B)
        old = runtime.create("settings", "Open General", "live", device=SIM_B, queue=True)
        self.now[0] += 20 * 60
        young = runtime.create("settings", "Open Display", "live", device=SIM_B, queue=True)
        self.assertEqual(runtime.sweep_queues(), 0)
        self.now[0] += 11 * 60
        self.assertEqual(runtime.sweep_queues(), 1)
        self.assertEqual((old.status, old.summary["stop_code"]), ("error", "queue_expired"))
        self.assertIn("waited 30 minutes", old.summary["reason"])
        self.assertIn("Nothing was done on the phone", old.summary["reason"])
        self.assertEqual(young.queue_info["position"], 1)

    def test_the_wait_is_set_in_minutes(self):
        with patch.dict("os.environ", {ownership.QUEUE_TTL_ENV: "5"}):
            runtime = self.runtime()
        self.assertEqual(runtime.queues.ttl, 300)

    def test_an_expired_request_does_not_start_when_the_phone_frees(self):
        runtime = self.runtime()
        holder = runtime.create("settings", "Open About", "live", device=SIM_B)
        old = runtime.create("settings", "Open General", "live", device=SIM_B, queue=True)
        self.now[0] += 31 * 60
        runtime.sweep_queues()
        self.end(runtime, holder)
        self.assertEqual(old.status, "error")
        self.assertEqual(runtime.active_runs, {})


class TakeBackTests(Base):
    def test_take_back_asks_for_a_stop_at_the_next_step_and_marks_the_holder(self):
        runtime = self.runtime()
        holder = runtime.create("settings", "Open About", "live", device=SIM_B, origin="mcp")
        phone = self.phone(runtime, SIM_B)
        answer = runtime.take_back(phone)
        self.assertTrue(control.stop_after_requested(holder.steering))
        self.assertFalse(holder.stop.is_set())
        self.assertTrue(answer["holder"]["stopping"])
        self.assertEqual(holder.public()["takingBack"], True)
        self.assertIn("take_back_requested", [e["event"] for e in holder.events])
        self.assertTrue(runtime.queues.of(runtime.queue_key(phone)).held())

    def test_a_run_waiting_for_the_person_has_nothing_in_flight_and_stops_at_once(self):
        runtime = self.runtime()
        holder = runtime.create("settings", "Open About", "live", device=SIM_B)
        holder.approval = {"id": "x", "kind": "act"}
        runtime.take_back(self.phone(runtime, SIM_B))
        self.assertTrue(holder.stop.is_set())

    def test_after_take_back_the_line_waits_for_the_grace_then_resumes(self):
        runtime = self.runtime()
        phone = self.phone(runtime, SIM_B)
        holder = runtime.create("settings", "Open About", "live", device=SIM_B)
        waiting = runtime.create("settings", "Open General", "live", device=SIM_B, queue=True)
        runtime.take_back(phone)
        self.end(runtime, holder, "stopped")
        self.assertEqual(runtime.active_runs, {})
        self.assertEqual(waiting.status, "queued")
        self.assertIn("held for its owner", waiting.queue_info["message"])
        self.assertTrue(waiting.queue_info["paused"])
        record = runtime.fleet.entry(SIM_B)[1]
        self.assertIsNotNone(record["queuePausedUntil"])
        self.now[0] += ownership.TAKE_BACK_GRACE_SECONDS + 1
        runtime.dispatch_queue(runtime.queue_key(phone))
        self.assertEqual(list(runtime.active_runs), [waiting.id])

    def test_resume_queue_ends_the_hold_at_once(self):
        runtime = self.runtime()
        phone = self.phone(runtime, SIM_B)
        holder = runtime.create("settings", "Open About", "live", device=SIM_B)
        waiting = runtime.create("settings", "Open General", "live", device=SIM_B, queue=True)
        runtime.take_back(phone)
        self.end(runtime, holder, "stopped")
        status, answer = self.call(runtime, "POST", f"/api/devices/{SIM_B}/resume-queue", {})
        self.assertEqual(status, 200)
        self.assertEqual(list(runtime.active_runs), [waiting.id])
        self.assertIsNone(answer["device"]["queuePausedUntil"])

    def test_a_request_that_arrives_during_the_hold_queues_and_a_script_is_refused(self):
        runtime = self.runtime()
        phone = self.phone(runtime, SIM_B)
        holder = runtime.create("settings", "Open About", "live", device=SIM_B)
        runtime.take_back(phone)
        self.end(runtime, holder, "stopped")
        late = runtime.create("settings", "Open General", "live", device=SIM_B, queue=True)
        self.assertEqual((late.status, late.queue_info["position"]), ("queued", 1))
        with self.assertRaises(APIError) as refused:
            runtime.create("settings", "Open Display", "live", device=SIM_B)
        self.assertEqual(refused.exception.code, "run_active")
        self.assertIn("Take it back", str(refused.exception))

    def test_the_route_ends_the_holders_run_and_answers_the_device(self):
        runtime = self.runtime()
        status, answer = self.call(runtime, "POST", f"/api/devices/{SIM_B}/take-back", {})
        self.assertEqual((status, answer["code"]), (409, "not_held"))
        holder = runtime.create("settings", "Open About", "live", device=SIM_B, origin="mcp")
        self.assertEqual(self.call(runtime, "POST", f"/api/devices/{SIM_B}/take-back", {"x": 1})[0], 400)
        status, answer = self.call(runtime, "POST", f"/api/devices/{SIM_B}/take-back", {})
        self.assertEqual(status, 200)
        self.assertTrue(answer["device"]["holder"]["stopping"])
        self.assertTrue(control.stop_after_requested(holder.steering))

    def test_only_the_mac_app_can_take_a_phone_back(self):
        runtime = self.runtime()
        holder = runtime.create("settings", "Open About", "live", device=SIM_B, origin="mcp")
        for path in ("take-back", "resume-queue"):
            status, answer = self.call(runtime, "POST", f"/api/devices/{SIM_B}/{path}", {}, session="app-secret")
            self.assertEqual((status, answer["code"]), (403, "app_only"), path)
            status, answer = self.call(runtime, "POST", f"/api/devices/{SIM_B}/{path}", {}, session="app-secret",
                                       sent={"X-Mobster-App-Session": "guess", "X-Mobster-Origin": "mcp"})
            self.assertEqual(status, 403, path)
        self.assertFalse(control.stop_after_requested(holder.steering))
        status, _ = self.call(runtime, "POST", f"/api/devices/{SIM_B}/take-back", {}, session="app-secret",
                              sent={"X-Mobster-App-Session": "app-secret"})
        self.assertEqual(status, 200)
        self.assertTrue(control.stop_after_requested(holder.steering))

class BoundaryTests(unittest.TestCase):
    def test_an_action_whose_outcome_is_unknown_is_journaled_and_never_sent_again(self):
        queue = SteeringQueue()

        class Dropped(Driver):
            attempts = 0

            def execute(self, operation, target, snapshot, text=None, timeout=10):
                Dropped.attempts += 1
                control.request_stop_after(queue)
                raise TimeoutError("the phone did not answer")

        driver = Dropped(rows())
        script = Script([("TAP", "Row 0", None), ("TAP", "Row 0", None), ("DONE", None, None)])
        events = []
        result = agent(driver, script, queue, events).run("Tap the first row")
        self.assertEqual(Dropped.attempts, 1)
        self.assertEqual(result["status"], "stopped")
        decisions = [e for e in events if e["event"] == "frontier_decision"]
        self.assertEqual(len(decisions), 1)
        self.assertTrue(may_have_acted(decisions[0]))
        self.assertIn("frontier_failed", [e["event"] for e in events])
        self.assertNotIn("action_acknowledged", [e["event"] for e in events])


if __name__ == "__main__":
    unittest.main()
