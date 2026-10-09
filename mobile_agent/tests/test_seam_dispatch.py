"""Seam S6.2/S6.4: listeners get the run's events off the agent thread, in order, with checkpoints coalesced; a slow
listener never slows a turn; context providers gather while the phone gets ready and time out at 2 s; pre-run hooks
get the frontier's approver. Offline."""

import os
import statistics
import threading
import time
import unittest
from unittest.mock import patch

from mobile_agent import harness_api
from mobile_agent.frontier import FrontierAgent
from mobile_agent.harness_api import ContextBlock, ListenerQueue
from mobile_agent.journal import Journal
from mobile_agent.server import ContextGather, Run, Runtime
from mobile_agent.state import Element
from mobile_agent.tests.seam_support import isolate
from mobile_agent.tests.seam_tasks import SmartBase
from mobile_agent.tests.test_frontier import Driver, Script, screen
from mobile_agent.tests.test_server_hardening import APP
from mobile_agent.tests.timing import bound, slow_machine


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

    def test_emit_posts_outside_the_run_condition(self):
        recorder = Recorder()
        harness_api.register_run_listener(recorder)
        db = Journal(None)
        self.addCleanup(db.close)
        queue = ListenerQueue()
        self.addCleanup(queue.close)
        run = Run(dict(APP), "g", "live", journal=db, listeners=queue)
        db.create(run.metadata())
        held = []
        post = queue.post
        with patch.object(queue, "post", side_effect=lambda *a: (held.append(run.condition._is_owned()), post(*a))):
            run.emit({"event": "one"})
            run.emit({"event": "two"})
        self.assertEqual(held, [False, False])
        queue.flush()
        self.assertEqual(recorder.calls, [("event", 0, "one"), ("event", 1, "two")])

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
        queue.post("on_event", run, {"n": 0})          # the dispatcher waits on this one
        for step in range(5):
            queue.post("on_checkpoint", run, {"step": step})
        for n in range(1, 4):
            queue.post("on_event", run, {"n": n})
        gate.set()
        queue.flush()
        self.assertEqual(seen, [("event", 0), ("checkpoint", 4), ("event", 1), ("event", 2), ("event", 3)])

    def test_an_overflow_drops_and_counts(self):
        gate = threading.Event()

        class Stuck:
            def on_event(self, run, event):
                gate.wait(5)
        harness_api.register_run_listener(Stuck())
        queue = ListenerQueue(capacity=3)
        self.addCleanup(queue.close)
        for n in range(10):
            queue.post("on_event", None, {"n": n})
        self.assertGreaterEqual(queue.dropped, 6)
        gate.set()
        self.assertTrue(queue.flush())

    @unittest.skipIf(slow_machine(), "a timing comparison: skipped where timing bounds are scaled")
    def test_a_slow_listener_does_not_slow_a_turn(self):
        def turns(listener):
            harness_api._listeners.clear()
            if listener is not None:
                harness_api.register_run_listener(listener)
            db = Journal(None)
            queue = ListenerQueue()
            run = Run(dict(APP), "g", "live", journal=db, listeners=queue)
            db.create(run.metadata())
            rows = [screen(Element("1", f"Row {i}", "Button", (.1, .2, .8, .05))) for i in range(30)]
            steps = [("TAP", f"Row {i}", None) for i in range(20)] + [("DONE", None, None)] * 2
            started = time.perf_counter()
            agent = FrontierAgent(Driver(rows), Script(steps), emit=run.emit, screenshots=False, settle_seconds=0,
                                  skills=(), step_observer=lambda r: queue.post("on_step", run, r),
                                  checkpoint=lambda s: queue.post("on_checkpoint", run, s))
            result = agent.run("Tap every row")
            elapsed = (time.perf_counter() - started) / max(1, result["steps"])
            queue.close(0)
            db.close()
            return elapsed

        class Sleepy:
            def on_event(self, run, event):
                time.sleep(.02)

            def on_step(self, run, record):
                time.sleep(.02)

            def on_checkpoint(self, run, state):
                time.sleep(.02)
        quiet = statistics.median(turns(None) for _ in range(3))
        loud = statistics.median(turns(Sleepy()) for _ in range(3))
        self.assertLess(loud - quiet, bound(.005))


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
        with patch.dict(os.environ, {"MOBSTER_FRONTIER_CONTRACT": "off"}):
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
        self.assertEqual(tapped.screen, typed.screen)  # the same screen structurally
        self.assertGreaterEqual(len(states), 2)
        self.assertEqual(set(states[0]), {"step", "plan", "notes", "history", "app", "cost_usd", "elapsed_s",
                                          "contract"})
        self.assertNotIn("4821", repr(states))  # checkpoints pass through the secret filter


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
        self.assertEqual(ContextGather.BUDGET, 2.0)
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
        self.assertEqual(len(blocks[0].text), 30)  # capped at the provider's max_chars
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
            # The provider runs on its own thread meanwhile: this waits for it, and would time out otherwise.
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
        self.assertEqual(recorder.calls[-1], ("finished", "completed", ("notes", "plan")))
        published = runtime.bus.since({"runs"}, bus_start)
        self.assertEqual([e["event"] for e in published], ["run_created", "run_finished"])
        self.assertEqual(published[-1]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
