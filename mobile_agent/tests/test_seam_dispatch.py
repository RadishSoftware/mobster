import gc
import os
import threading
import time
import unittest
import weakref
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent import harness_api
from mobile_agent.frontier import FrontierAgent
from mobile_agent.harness_api import ContextBlock, ListenerQueue
from mobile_agent.journal import Journal
from mobile_agent.run import Run
from mobile_agent.runtime_work import ContextGather
from mobile_agent.server import Runtime
from mobile_agent.state import Element
from mobile_agent.tests.seam_support import isolate
from mobile_agent.tests.seam_tasks import SmartBase
from mobile_agent.tests.test_frontier import Driver, Script, screen
from mobile_agent.tests.test_server_hardening import APP
from mobile_agent.tests.timing import bound


class Recorder:
    def __init__(self):
        self.calls, self.lock = [], threading.Lock()

    def _add(self, *item):
        with self.lock:
            self.calls.append(item)

    def on_created(self, run, runtime):
        self._add("created", run.id)

    def on_event(self, run, event):
        self._add("event", event["seq"], event["event"])

    def on_step(self, run, record):
        self._add("step", record.step, record.index, record.op)

    def on_checkpoint(self, run, state):
        self._add("checkpoint", state["step"])

    def on_finished(self, run, summary, private):
        self._add("finished", summary["status"], tuple(sorted(private)))


class QueueTests(unittest.TestCase):
    def setUp(self):
        isolate(self)

    def test_a_failed_dispatcher_start_can_be_retried(self):
        queue = ListenerQueue()
        self.addCleanup(queue.close)
        seen = []
        with patch("mobile_agent.harness_api.threading.Thread.start", side_effect=RuntimeError("thread limit")):
            with self.assertRaisesRegex(RuntimeError, "thread limit"):
                queue.call(lambda: seen.append("failed"))
        self.assertIsNone(queue._dispatcher)
        queue.call(lambda: seen.append("recovered"))
        self.assertTrue(queue.flush())
        self.assertEqual(seen, ["recovered"])

    def test_runtime_shutdown_keeps_services_and_journal_until_required_callbacks_finish(self):
        from mobile_agent.threads import service as threads
        delivered = []
        runtime = object.__new__(Runtime)
        runtime.lock, runtime.closing, runtime.threads = threading.Lock(), False, set()
        runtime.queued, runtime.queue_timers = {}, {}
        runtime.config = SimpleNamespace(keep_runner=True)
        runtime.engine_sampler = SimpleNamespace(close=lambda: None)
        runtime.scheduler = runtime.manager = runtime.control = runtime.fleet = runtime.fanout = runtime.frames = None
        runtime.video = runtime.awake = runtime.power = SimpleNamespace(close=lambda: None)
        runtime.journal, runtime.listeners = Journal(None), ListenerQueue()
        self.addCleanup(runtime.journal.close)
        self.addCleanup(runtime.listeners.close)
        run = Run(dict(APP), "finished", "live", finished_at=1, extras={"threadId": "thread"})
        runtime.runs = {run.id: run}
        service = SimpleNamespace(runtime=runtime, runs={}, closed=False,
                                  on_finished=lambda *_: delivered.append(runtime.journal.connection is not None))
        threads._services.add(runtime, service)
        self.addCleanup(threads.close, runtime)
        runtime._services = [(None, threads.close)]
        harness_api.register_run_listener(threads.Listener())
        entered, release = threading.Event(), threading.Event()
        runtime.listeners.call(lambda: (entered.set(), release.wait()))
        self.assertTrue(entered.wait(1))
        runtime.listeners.post("on_finished", run, {"status": "completed"}, {})
        try:
            self.assertFalse(runtime.close(timeout=0))
            self.assertIs(threads.service_for(runtime), service)
            self.assertIsNotNone(runtime.journal.connection)
        finally:
            release.set()
        self.assertTrue(runtime.close(timeout=1))
        self.assertEqual(delivered, [True])
        self.assertIsNone(threads.service_for(runtime))
        self.assertIsNone(runtime.journal.connection)

    def test_concurrent_emits_reach_listeners_in_their_saved_sequence(self):
        queue, recorder = ListenerQueue(), Recorder()
        self.addCleanup(queue.close)
        harness_api.register_run_listener(recorder)
        run = Run(dict(APP), "g", "live", listeners=queue)
        entered, release, second_posted = threading.Event(), threading.Event(), threading.Event()
        post = queue.post

        def delayed(method, *args):
            if method == "on_event":
                if args[1]["seq"] == 0:
                    entered.set()
                    release.wait(2)
                else:
                    second_posted.set()
            post(method, *args)

        with patch.object(queue, "post", delayed):
            first = threading.Thread(target=run.emit, args=({"event": "first"},))
            second = threading.Thread(target=run.emit, args=({"event": "second"},))
            first.start()
            self.assertTrue(entered.wait(1))
            second.start()
            try:
                second_posted.wait(.1)
            finally:
                release.set()
            first.join(2)
            second.join(2)
            self.assertFalse(first.is_alive() or second.is_alive())
        self.assertTrue(queue.flush())
        self.assertEqual([item[1] for item in recorder.calls], [event["seq"] for event in run.events])

    def test_lifecycle_and_publish_callbacks_keep_order_when_ordinary_queue_is_full(self):
        entered, release = threading.Event(), threading.Event()
        recorder = Recorder()
        harness_api.register_run_listener(recorder)
        queue = ListenerQueue(capacity=1)
        self.addCleanup(queue.close)
        self.addCleanup(release.set)
        run = Run(dict(APP), "g", "live")
        queue.call(lambda: (entered.set(), release.wait(5)))
        self.assertTrue(entered.wait(2))
        queue.post("on_event", run, {"seq": 0, "event": "first"})
        queue.post("on_created", run, None)
        queue.post("on_finished", run, {"status": "completed"}, {})
        queue.call(lambda: recorder.calls.append(("published",)))
        self.assertEqual(recorder.calls, [])
        self.assertEqual(queue.dropped, 0)
        release.set()
        self.assertTrue(queue.flush())
        self.assertEqual([call[0] for call in recorder.calls], ["event", "created", "finished", "published"])

    def test_a_closed_dispatcher_refuses_lifecycle_calls_and_callbacks(self):
        queue = ListenerQueue()
        seen = []
        queue.close()
        queue.post("on_finished", None, {}, {})
        queue.call(lambda: seen.append("late"))
        self.assertIsNone(queue._dispatcher)
        self.assertEqual(seen, [])

    def test_an_idle_dispatcher_releases_delivered_callbacks_and_checkpoints(self):
        class Payload:
            id = "finished-run"

            def __call__(self):
                pass

            def on_checkpoint(self, *_args):
                pass

        harness_api.register_run_listener(Payload())
        queue = ListenerQueue()
        self.addCleanup(queue.close)
        callback, run, state = Payload(), Payload(), Payload()
        references = [weakref.ref(value) for value in (callback, run, state)]
        queue.call(callback)
        del callback
        queue._queue.join()
        gc.collect()
        self.assertIsNone(references[0]())
        queue.post("on_checkpoint", run, state)
        del run, state
        queue._queue.join()
        gc.collect()
        self.assertTrue(all(reference() is None for reference in references))

    def test_order_holds_and_checkpoints_coalesce_latest_wins(self):
        gate = threading.Event()
        seen = []

        class Slow:
            def on_event(self, run, event):
                gate.wait(5)
                seen.append(("event", event["n"]))

            def on_checkpoint(self, run, state):
                seen.append(("checkpoint", state["step"]))
        harness_api.register_run_listener(Slow())
        queue = ListenerQueue()
        self.addCleanup(queue.close)
        run = Run(dict(APP), "g", "live")
        queue.post("on_event", run, {"n": 0})
        for step in range(5):
            queue.post("on_checkpoint", run, {"step": step})
        for n in range(1, 4):
            queue.post("on_event", run, {"n": n})
        gate.set()
        queue.flush()
        self.assertEqual(seen, [("event", 0), ("checkpoint", 4), ("event", 1), ("event", 2), ("event", 3)])

    def test_a_dropped_checkpoint_can_be_queued_again_after_overflow(self):
        entered, release = threading.Event(), threading.Event()
        seen = []

        class Stuck:
            def on_event(self, run, event):
                entered.set()
                release.wait(5)

            def on_checkpoint(self, run, state):
                seen.append(state["step"])

        harness_api.register_run_listener(Stuck())
        queue = ListenerQueue(capacity=1)
        self.addCleanup(queue.close)
        self.addCleanup(release.set)
        run = Run(dict(APP), "g", "live")
        queue.post("on_event", run, {})
        self.assertTrue(entered.wait(2))
        queue.post("on_event", run, {})
        queue.post("on_checkpoint", run, {"step": 1})
        self.assertEqual(queue.dropped, 1)
        release.set()
        self.assertTrue(queue.flush())
        queue.post("on_checkpoint", run, {"step": 2})
        self.assertTrue(queue.flush())
        self.assertEqual(seen, [2])

    def test_close_with_a_full_queue_drains_lifecycle_calls_and_stops_without_a_sentinel(self):
        entered, release = threading.Event(), threading.Event()
        seen = []

        class Listener:
            def on_finished(self, run, summary, private):
                seen.append(summary["status"])

        harness_api.register_run_listener(Listener())
        queue = ListenerQueue(capacity=2)
        self.addCleanup(queue.close)
        self.addCleanup(release.set)
        queue.call(lambda: (entered.set(), release.wait(5)))
        self.assertTrue(entered.wait(2))
        queue.post("on_finished", None, {"status": "completed"}, {})
        queue.call(lambda: seen.append("published"))
        queue.close(timeout=0)
        release.set()
        self.assertTrue(queue.close(timeout=2))  # the dispatcher exited
        self.assertEqual(seen, ["completed", "published"])

    def test_a_run_finishes_while_its_listener_is_still_blocked(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()

        class Blocked:
            def on_event(self, run, event):
                entered.set()
                release.wait()

            def on_step(self, run, record):
                release.wait()

            def on_checkpoint(self, run, state):
                release.wait()

        harness_api.register_run_listener(Blocked())
        db = Journal(None)
        queue = ListenerQueue()
        run = Run(dict(APP), "g", "live", journal=db, listeners=queue)
        db.create(run.metadata())
        rows = [screen(Element("1", f"Row {i}", "Button", (.1, .2, .8, .05))) for i in range(30)]
        steps = [("TAP", f"Row {i}", None) for i in range(20)] + [("DONE", None, None)] * 2
        agent = FrontierAgent(Driver(rows), Script(steps), emit=run.emit, screenshots=False, settle_seconds=0,
                              skills=(), step_observer=lambda r: queue.post("on_step", run, r),
                              checkpoint=lambda s: queue.post("on_checkpoint", run, s))
        results, errors = [], []

        def work():
            try:
                results.append(agent.run("Tap every row"))
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(bound(5)))
            self.assertTrue(finished.wait(bound(5)), "the agent waited for a blocked listener")
            self.assertFalse(release.is_set())
            self.assertEqual(errors, [])
            self.assertTrue(results)
        finally:
            release.set()
            worker.join(bound(5))
            queue.close()
            db.close()


class FrontierHookTests(unittest.TestCase):
    def test_step_records_and_checkpoints(self):
        body = Element("1", "Message", "TextField", (.1, .8, .6, .05), editable=True,
                       actions=("TAP", "TYPE", "TYPE_SUBMIT"))
        send = Element("2", "Send", "Button", (.8, .8, .1, .05))
        rows = [screen(body, send, bundle="com.apple.MobileSMS")] * 8
        records, states, asked = [], [], []
        script = Script([[("TYPE", "Message", "My PIN is 4821")], [("TAP", "Send", None)], ("DONE", None, None),
                         ("DONE", None, None)])

        def approve(request):
            asked.append(request["label"])
            return "approved"
        FrontierAgent(Driver(rows), script, screenshots=False, settle_seconds=0, skills=(), approve=approve,
                      apps={"com.apple.MobileSMS": "Messages"}, step_observer=records.append,
                      checkpoint=states.append).run("Send Sam a message with my PIN")
        self.assertEqual(asked, ["Send"])
        typed, tapped = records[0], records[1]
        self.assertEqual((typed.op, typed.text, typed.approval, typed.commit), ("TYPE", "My PIN is 4821", None, None))
        self.assertEqual((tapped.op, tapped.app, tapped.approval, tapped.commit),
                         ("TAP", "com.apple.MobileSMS", "approved", "send_message"))
        self.assertEqual(tapped.target["label"], "Send")
        self.assertEqual(tapped.target["frame"], [.8, .8, .1, .05])
        self.assertRegex(tapped.screen, r"^[0-9a-f]{16}$")
        self.assertEqual(tapped.screen, typed.screen)
        self.assertGreaterEqual(len(states), 2)
        self.assertEqual(set(states[0]), {"step", "plan", "notes", "history", "app", "cost_usd", "elapsed_s",
                                          "contract"})
        self.assertNotIn("4821", repr(states))


class Slow:
    key, max_chars = "slow", 100

    def __init__(self, seconds):
        self.seconds = seconds

    def blocks(self, ctx):
        time.sleep(self.seconds)
        return [ContextBlock("slow", "Slow", "late")]

    def turn_blocks(self, ctx, turn):
        return ()


class Fast:
    key, max_chars = "memory", 30

    def blocks(self, ctx):
        return [ContextBlock("memory", "What you know about the user", "Kate Bell is my sister; " * 4)]

    def turn_blocks(self, ctx, turn):
        return [ContextBlock("memory", "Now", f"turn {turn.step}", stable=False)]


class Broken:
    key, max_chars = "broken", 100

    def blocks(self, ctx):
        raise KeyError("nope")


class GatherTests(unittest.TestCase):
    def setUp(self):
        isolate(self)

    def test_providers_time_out_and_raise_without_holding_the_run(self):
        harness_api.register_context_provider(lambda c: Slow(1.0), order=10)
        harness_api.register_context_provider(lambda c: Fast(), order=20)
        harness_api.register_context_provider(lambda c: Broken(), order=30)
        events = []
        gather = ContextGather(None, events.append, budget=.2)
        started = time.monotonic()
        blocks, ready = gather.join()
        self.assertLess(time.monotonic() - started, bound(.6))
        self.assertEqual([p.key for p in ready], ["memory"])
        self.assertEqual(len(blocks), 1)
        self.assertEqual(len(blocks[0].text), 30)
        self.assertEqual([(e["event"], e.get("key"), e.get("error")) for e in events],
                         [("context_error", "slow", "timeout"), ("context_error", "broken", "KeyError"),
                          ("context_used", None, None)])
        self.assertEqual((events[-1]["keys"], events[-1]["chars"]), (["memory"], 30))
        turn = gather.turn_context(ready)
        self.assertEqual(turn(harness_api.TurnState(2, None, "", (), 0.0, None))[0].text, "turn 2")


class RunWiringTests(SmartBase):
    def test_providers_gather_while_the_phone_gets_ready(self):
        started = threading.Event()

        class Watch(Fast):
            def blocks(self, ctx):
                started.set()
                return super().blocks(ctx)
        harness_api.register_context_provider(lambda c: Watch(), order=10)
        original = Runtime.open_driver

        def open_driver(runtime, run, trace, setup):
            self.assertTrue(started.wait(2))
            return original(runtime, run, trace, setup)
        runtime = self.runtime()
        run = runtime.create("messages", "Text Kate", "live")
        script = self.script()
        self.work(runtime, run, script, open_driver=open_driver)
        self.assertIn("What you know about the user:\nKate Bell is my sister", script.prompts[0])
        self.assertIn("Now:\nturn 0", script.prompts[0])
        used = next(e for e in run.events if e["event"] == "context_used")
        self.assertEqual(used["keys"], ["memory"])
        self.assertNotIn("text", used)

    def test_pre_runs_get_the_frontiers_approver(self):
        seen = []
        harness_api.register_pre_run(lambda c, driver, approve: seen.append((c.run_id, approve)), order=10)
        runtime = self.runtime()
        run = runtime.create("messages", "Read Sam's message", "live")
        self.work(runtime, run, self.script())
        self.assertEqual(seen[0][0], run.id)
        self.assertTrue(callable(seen[0][1]))
        self.finish(runtime, run)
        with patch.dict(os.environ, {"MOBSTER_ASK_BEFORE_ACTING": "0"}):
            again = runtime.create("messages", "Read Sam's message again", "live")
            self.work(runtime, again, self.script())
        self.assertIsNone(seen[1][1])

    def test_listeners_see_the_run_in_order_and_the_bus_says_it_finished(self):
        recorder = Recorder()
        harness_api.register_run_listener(recorder)
        runtime = self.runtime()
        bus_start = runtime.bus.position()
        run = runtime.create("messages", "Read Sam's message", "live")
        self.work(runtime, run, self.script())
        kinds = [c[0] for c in recorder.calls]
        self.assertEqual(kinds[0], "created")
        self.assertEqual(kinds[-1], "finished")
        self.assertIn("checkpoint", kinds)
        seqs = [c[1] for c in recorder.calls if c[0] == "event"]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(recorder.calls[-1], ("finished", "completed", ("contract", "notes", "plan", "screen")))
        published = runtime.bus.since({"runs"}, bus_start)
        self.assertEqual([e["event"] for e in published], ["run_created", "run_finished"])
        self.assertEqual(published[-1]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
