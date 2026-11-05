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

    def test_a_run_calls_no_model_only_with_a_field_registered_so(self):
        harness_api.register_run_field("threadId", lambda value, runtime: str(value))
        harness_api.register_run_field("mapIt", lambda value, runtime: value, model=False)
        self.assertTrue(harness_api.calls_no_model({"mapIt": {}}))
        self.assertTrue(harness_api.calls_no_model({"threadId": "7", "mapIt": {"screens": 3}}))
        # An ordinary field, an unset one and no fields at all still call a model.
        for extras in ({"threadId": "7"}, {"mapIt": None}, {}, None):
            with self.subTest(extras=extras):
                self.assertFalse(harness_api.calls_no_model(extras))

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
    def test_a_hook_that_acts_then_raises_never_hands_the_task_to_the_model(self):
        def hook(ctx, driver, approve):
            driver.execute("HOME", None, driver.observe())
            raise OSError("the action response was lost")
        harness_api.register_pre_run(hook, order=0)
        harness_api.register_pre_run(lambda *args: self.fail("a later hook ran"), order=10)
        runtime = self.runtime()
        run = runtime.create("messages", "Read Sam's message", "live")
        script = self.script()
        phone = self.work(runtime, run, script)
        self.assertEqual(script.prompts, [])
        self.assertEqual([action[0] for action in phone.actions], ["HOME"])
        self.assertEqual((run.summary["status"], run.summary["outcome"]),
                         ("completion_not_confirmed", "check"))
        self.assertTrue(run.summary["actions_may_have_run"])
        self.assertEqual(run.summary["code"], "outcome_unknown")
        self.assertIn("Inspect", run.summary["reason"])

    def test_a_before_phone_hook_failure_still_allows_the_model_to_start(self):
        def hook(ctx, driver, approve):
            self.assertIsNone(driver)
            self.assertIsNone(approve)
            raise OSError("context unavailable")
        hook.before_phone = True
        harness_api.register_pre_run(hook, order=0)
        runtime = self.runtime()
        run = runtime.create("messages", "Read Sam's message", "live")
        script = self.script()
        self.work(runtime, run, script)
        self.assertTrue(script.prompts)
        self.assertEqual(run.status, "completed")
        self.assertTrue(any(event.get("event") == "context_error" for event in run.events))

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
        self.assertIn("T0: TAP 'Sam' -> screen changed", first)
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
