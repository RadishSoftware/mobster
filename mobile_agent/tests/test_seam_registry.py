"""Seam S2/S6: the harness registries. Provider and tool order, clashing ops, run fields, pre-run results and
hand-offs, listener isolation, and the caps on frontier options. Offline."""

import unittest

from mobile_agent import harness_api
from mobile_agent.agent_hooks import Prepared, SkillResult
from mobile_agent.harness_api import ContextBlock, InitialState, PreRun
from mobile_agent.tests.seam_support import isolate
from mobile_agent.tests.seam_tasks import SmartBase


def ctx(**extras):
    return harness_api.RunContext(run_id="a" * 12, goal="g", engine="smart", origin="app", app_bundle=None,
                                  device_id=None, device_kind=None, extras=harness_api.frozen_mapping(extras),
                                  data_dir=None, emit=lambda e: None, clarify=lambda r: "", cancelled=lambda: False,
                                  runtime=None)


class Provider:
    def __init__(self, key, text="", turn=""):
        self.key, self.max_chars, self.text, self.turn = key, 2000, text, turn

    def blocks(self, ctx):
        return [ContextBlock(self.key, self.key.title(), self.text)] if self.text else []

    def turn_blocks(self, ctx, turn):
        return [ContextBlock(self.key, f"{self.key} now", self.turn, stable=False)] if self.turn else []


class Tool:
    prompt = "- X: x"
    needs_target = False

    def __init__(self, op):
        self.op = op

    def available(self, ctx):
        return True

    def prepare(self, ctx, act, snapshot):
        return Prepared(None)

    def perform(self, ctx, prepared, act, snapshot):
        return SkillResult("ok")


class RegistryTests(unittest.TestCase):
    def setUp(self):
        isolate(self)

    def test_providers_and_tools_come_in_order_and_clashing_ops_are_skipped(self):
        harness_api.register_context_provider(lambda c: Provider("memory"), order=20)
        harness_api.register_context_provider(lambda c: Provider("thread"), order=10)
        harness_api.register_context_provider(lambda c: None, order=5)  # not for this run
        harness_api.register_context_provider(lambda c: 1 / 0, order=1)  # a broken factory is skipped
        self.assertEqual([p.key for p in harness_api.context_providers(ctx())], ["thread", "memory"])
        harness_api.register_tool(lambda c: Tool("READ_ATTACHMENT"), order=30)
        harness_api.register_tool(lambda c: Tool("ASK_USER"), order=10)
        harness_api.register_tool(lambda c: Tool("ASK_USER"), order=11)   # a duplicate
        harness_api.register_tool(lambda c: Tool("TAP"), order=12)        # a frontier operation
        harness_api.register_tool(lambda c: Tool("USE_CODE"), order=13)   # a default skill
        self.assertEqual([t.op for t in harness_api.tools(ctx())], ["ASK_USER", "READ_ATTACHMENT"])
        with self.assertRaises(ValueError):
            harness_api.register_tool(lambda c: None, order="first")

    def test_run_fields(self):
        harness_api.register_run_field("threadId", lambda value, runtime: str(value))
        harness_api.register_run_field("secretNote", lambda value, runtime: value, public=False)
        for name in ("threadId", "goal", "appId", "ThreadId", "x", "thread_id"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                harness_api.register_run_field(name, lambda value, runtime: value)

        def positive(value, runtime):
            if not isinstance(value, int) or value < 1:
                raise ValueError("budget must be a whole number of minutes")
            return value
        harness_api.register_run_field("budget", positive)
        self.assertEqual(harness_api.clean_extras({"threadId": 7, "budget": 3}, None), {"threadId": "7", "budget": 3})
        with self.assertRaisesRegex(ValueError, "whole number"):
            harness_api.clean_extras({"budget": 0}, None)
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            harness_api.clean_extras({"other": 1}, None)
        self.assertEqual(harness_api.public_extras({"threadId": "7", "secretNote": "x"}), {"threadId": "7"})

    def test_frontier_options_are_capped(self):
        harness_api.register_frontier_options(lambda c: {"max_steps": 120})
        harness_api.register_frontier_options(lambda c: {"max_seconds": 1800})
        self.assertEqual(harness_api.frontier_options(ctx()), {"max_steps": 120, "max_seconds": 1800.0})
        for bad in ({"max_steps": 201}, {"max_seconds": 3601}, {"max_steps": 0}, {"temperature": 1},
                    {"max_steps": True}):
            with self.subTest(options=bad):
                harness_api._frontier_options.clear()
                harness_api.register_frontier_options(lambda c, bad=bad: bad)
                with self.assertRaises(ValueError):
                    harness_api.frontier_options(ctx())

    def test_one_sink_and_one_replayer(self):
        items = []
        harness_api.register_thread_sink(lambda thread, item: items.append((thread, item)) or {"id": "i1", **item},
                                         lambda thread, item_id, changes: {"id": item_id, **changes})
        self.assertEqual(harness_api.post_thread_item("3f9c2a1b7d0e", {"kind": "note"}), {"id": "i1", "kind": "note"})
        self.assertEqual(harness_api.update_thread_item("3f9c2a1b7d0e", "i1", {"done": True}), {"id": "i1", "done": True})
        with self.assertRaises(ValueError):
            harness_api.register_thread_sink(lambda *a: None, lambda *a: None)

        class Replayer:
            def replay(self, trajectory, **kwargs):
                return harness_api.ReplayOutcome("done", 0, "")
        self.assertIsNone(harness_api.replayer())
        harness_api.register_replayer(Replayer())
        self.assertIsInstance(harness_api.replayer(), Replayer)
        with self.assertRaises(ValueError):
            harness_api.register_replayer(Replayer())

    def test_a_raising_listener_is_isolated(self):
        calls = []

        class Broken:
            def on_event(self, run, event):
                raise RuntimeError("listener bug")

        class Good:
            def on_event(self, run, event):
                calls.append(event)
        harness_api.register_run_listener(Broken())
        harness_api.register_run_listener(Good())
        harness_api.dispatch("on_event", None, {"event": "x"})
        harness_api.dispatch("on_step", None, None)  # neither has it: nothing happens
        self.assertEqual(calls, [{"event": "x"}])


class PreRunTests(SmartBase):
    def test_a_pre_run_result_finishes_the_run_without_a_model_call(self):
        harness_api.register_pre_run(lambda c, driver, approve: None, order=0)
        harness_api.register_pre_run(lambda c, driver, approve: PreRun(result={
            "event": "result", "status": "completed", "outcome": "done", "answer": "Replayed your routine.",
            "engine": "smart"}), order=10)
        harness_api.register_pre_run(lambda c, driver, approve: self.fail("a later hook ran"), order=20)
        runtime = self.runtime()
        run = runtime.create("messages", "Order my usual", "live")
        script = self.script()
        self.work(runtime, run, script)
        self.assertEqual((run.status, run.summary["answer"]), ("completed", "Replayed your routine."))
        self.assertEqual(script.prompts, [])

    def test_a_handoff_starts_the_agent_from_its_state(self):
        state = InitialState(plan="Open the thread with Sam, then send", notes=("Sam's thread is pinned",),
                             history=("0.0: TAP 'Sam' -> screen changed",), feedback="Go on from Sam's thread.",
                             steps_used=3, reason="a routine handed off")
        harness_api.register_pre_run(lambda c, driver, approve: PreRun(handoff=state), order=10)
        runtime = self.runtime()
        run = runtime.create("messages", "Text Sam", "live")
        script = self.script()
        self.work(runtime, run, script)
        first = script.prompts[0]
        self.assertIn("Turns used: 3 of 50", first)
        self.assertIn("Plan so far: Open the thread with Sam, then send", first)
        self.assertIn("- Sam's thread is pinned", first)
        self.assertIn("0.0: TAP 'Sam' -> screen changed", first)
        self.assertIn("Feedback on your last action: Go on from Sam's thread.", first)

    def test_a_raising_listener_and_bad_options_never_break_a_run(self):
        class Broken:
            def on_event(self, run, event):
                raise RuntimeError("listener bug")

            def on_finished(self, run, summary, private):
                raise RuntimeError("listener bug")
        harness_api.register_run_listener(Broken())
        harness_api.register_frontier_options(lambda c: {"temperature": 2})
        runtime = self.runtime()
        run = runtime.create("messages", "Read Sam's last message", "live")
        self.work(runtime, run, self.script())
        self.assertEqual(run.status, "completed")
        errors = [e for e in run.events if e["event"] == "context_error"]
        self.assertEqual([(e["key"], e["error"]) for e in errors], [("frontier_options", "ValueError")])


if __name__ == "__main__":
    unittest.main()
