"""Loop routing inside Agent.run, and the step agent's loop-level speed paths. Offline."""

import json
import threading
import unittest
from unittest.mock import Mock

from mobile_agent import agent as agent_module
from mobile_agent.agent import Agent, requested_url
from mobile_agent.models import Decision
from mobile_agent.state import Element, Snapshot
from mobile_agent.task_policy import ActionSupport, OutputSupport, StopGate
from mobile_agent.tests.test_loops import DeckApp, FakeJudge, crop, deck_program
from mobile_agent.transport import TransportError


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


GOAL = "In Hinge: like anyone who doesn't have blue eyes; if you're not sure skip them; stop after 50"


def decision(operation, target=None, *, goal=.05, confidence=.97, risk="navigation", side_effect=.01):
    return Decision(operation, target, confidence, goal, .02, "t", 0, {}, StopGate.CONTINUE,
                    risk_tier=risk if operation == "TAP" else None, side_effect_risk=side_effect)


class Helper:
    """A helper that only compiles: any other use would be a bug in these tests."""

    def __init__(self, program):
        self.program = program
        self.calls = 0
        self.purposes = []

    def complete(self, messages, token_limit, timeout, purpose):
        self.calls += 1
        self.purposes.append(purpose)
        body = {"loop": True, "program": self.program} if self.program else {"loop": False}
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(body)}}]}

    def ask(self, *args, **kwargs):
        raise AssertionError("The loop path never asks the helper for text or recovery")


class Judge(FakeJudge):
    crop = staticmethod(crop)


def shadow_model():
    model = Mock(spec=["decide"])

    def decide(snapshot, goal, history, **kwargs):
        hint = kwargs.get("hint") or ""
        wanted = "Skip" if "not to match" in hint else "Like"
        return decision("TAP", next(e.id for e in snapshot.elements if e.label == wanted), confidence=.99)
    model.decide.side_effect = decide
    return model


class LoopRoutingTests(unittest.TestCase):
    def test_an_iteration_request_runs_as_a_compiled_loop(self):
        app = DeckApp(["Ana", "Ben", "Cy"])
        events = []
        helper = Helper(deck_program())
        result = Agent(app, shadow_model(), helper, settle_seconds=0, emit=events.append,
                       vision_judge=Judge({"Ana": "no", "Ben": "yes", "Cy": "no"})).run(GOAL, execute=True)
        self.assertEqual(app.taps(), [("Like", "Ana"), ("Skip", "Ben"), ("Like", "Cy")])
        self.assertEqual(result["status"], "completed_unverified")
        self.assertEqual(result["loop"]["counts"]["matched"], 2)
        self.assertEqual(result["loop"]["reason"], "end_of_feed")
        self.assertIn("reached the end of the list", result["reason"])
        self.assertEqual(result["actions"], 3)
        self.assertEqual(helper.purposes, ["loop_compile"])
        kinds = [e["event"] for e in events]
        for kind in ("loop_compiled", "loop_item", "loop_stopped"):
            self.assertIn(kind, kinds)
        loop_events = [e for e in events if e["event"].startswith("loop_")]
        self.assertNotIn("Ana", json.dumps(loop_events))

    def test_a_request_that_is_not_a_loop_runs_the_step_agent_on_the_same_read(self):
        screen = Snapshot([Element("0", "About", "StaticText", (0, .1, 1, .05), locator="/t")], "About",
                          400, 800, "wda")
        reads = []
        driver = Mock(spec=["observe", "execute", "can_type"], can_type=False)
        driver.observe.side_effect = lambda timeout=10: reads.append(1) or screen
        model = Mock(spec=["decide", "stable_completion"], stable_completion=True)
        model.decide.return_value = decision("DONE", goal=.9)
        helper = Helper(None)
        result = Agent(driver, model, helper, settle_seconds=0).run("Like every setting on this page", execute=True)
        self.assertEqual(result["status"], "completed_unverified")
        self.assertEqual(helper.calls, 1)
        self.assertEqual(model.decide.call_count, 1)
        # One read for compiling and deciding, one completion re-read.
        self.assertEqual(len(reads), 3)

    def test_other_requests_never_reach_the_compiler(self):
        driver = DeckApp(["Ana"])
        model = Mock(spec=["decide", "stable_completion"], stable_completion=True)
        model.decide.return_value = decision("DONE", goal=.9)
        helper = Helper(deck_program())
        Agent(driver, model, helper, settle_seconds=0).run("In Hinge: open my profile", execute=True)
        Agent(driver, model, helper, settle_seconds=0, loop_mode="off").run(GOAL, execute=True)
        self.assertEqual(helper.calls, 0)

    def test_a_visual_loop_without_a_judge_does_nothing(self):
        app = DeckApp(["Ana"])
        result = Agent(app, shadow_model(), Helper(deck_program()), settle_seconds=0).run(GOAL, execute=True)
        self.assertEqual((result["status"], app.actions), ("blocked", []))
        self.assertIn("look at images", result["reason"])

    def test_a_protected_characteristic_is_refused(self):
        class Refusing(Helper):
            def complete(self, messages, token_limit, timeout, purpose):
                return {"choices": [{"finish_reason": "stop", "message": {
                    "content": json.dumps({"loop": False, "refused": True})}}]}
        app = DeckApp(["Ana"])
        result = Agent(app, shadow_model(), Refusing(None), settle_seconds=0,
                       vision_judge=Judge({})).run("In Hinge: like everyone who is Muslim", execute=True)
        self.assertEqual((result["status"], app.actions), ("blocked", []))

    def test_dry_run_mode_judges_without_acting(self):
        app = DeckApp(["Ana", "Ben"])
        result = Agent(app, shadow_model(), Helper(deck_program()), settle_seconds=0, loop_mode="dry_run",
                       vision_judge=Judge({"Ana": "no"})).run(GOAL, execute=True)
        self.assertEqual(app.actions, [])
        self.assertTrue(result["loop"]["dry_run"])
        self.assertIn("no actions were taken", result["reason"])

    def test_a_server_dry_run_routes_even_without_execute(self):
        app = DeckApp(["Ana", "Ben"])
        result = Agent(app, shadow_model(), Helper(deck_program()), settle_seconds=0, loop_mode="dry_run",
                       vision_judge=Judge({"Ana": "no"})).run(GOAL, execute=False)
        self.assertEqual(app.actions, [])
        self.assertEqual(result["loop"]["reason"], "dry_run_preview")
        from mobile_agent.server import Run
        run = Run({"id": "h", "name": "Hinge", "bundleId": "co.hinge.app"}, "like anyone", "live", dry_run=True)
        self.assertTrue(run.metadata()["dryRun"])

    def test_the_loop_question_goes_to_the_ask_channel_and_its_wait_is_free(self):
        app = DeckApp(["Ana"])
        asked = []
        goal = "In Hinge: like anyone who doesn't have blue eyes"
        program = deck_program(policy={"unsure_stated": False, "stop_stated": False})
        result = Agent(app, shadow_model(), Helper(program), settle_seconds=0, max_seconds=5,
                       ask=lambda request: asked.append(request) or "choice:skip",
                       vision_judge=Judge({"Ana": "no"})).run(goal, execute=True)
        self.assertEqual([r["kind"] for r in asked], ["loop"])
        self.assertEqual(result["status"], "completed_unverified")
        self.assertEqual(app.taps(), [("Like", "Ana")])


class SwipeGuardTests(unittest.TestCase):
    def test_a_scroll_dispatches_on_the_decisions_own_read(self):
        screen = Snapshot([Element("0", "General", "Cell", (0, .3, 1, .05), locator="/c")], "General", 400, 800, "wda")
        below = Snapshot([Element("0", "Privacy", "Cell", (0, .3, 1, .05), locator="/p")], "Privacy", 400, 800, "wda")
        state = {"screen": screen, "reads": 0, "reads_at_dispatch": None}

        class Driver:
            can_type = False

            def observe(self, timeout=10):
                state["reads"] += 1
                return state["screen"]

            def execute(self, operation, target, snapshot, text=None, timeout=10):
                state["reads_at_dispatch"] = state["reads"]
                state["screen"] = below
        model = Mock(spec=["decide", "stable_completion"], stable_completion=True)
        model.decide.side_effect = [decision("SWIPE_UP", confidence=.9), decision("DONE", goal=.9)]
        Agent(Driver(), model, settle_seconds=0).run("Scroll down", execute=True)
        self.assertEqual(state["reads_at_dispatch"], 1)


class AnswerAtDoneTests(unittest.TestCase):
    def test_answer_work_starts_before_the_completion_re_read(self):
        screen = Snapshot([Element("0", "Model Number, MTQM3LL/A", "Cell", (0, .3, 1, .05))], "", 400, 800, "wda")
        started = threading.Event()
        seen = []

        class Driver:
            can_type = False
            reads = 0

            def observe(self, timeout=10):
                Driver.reads += 1
                if Driver.reads == 2:
                    # The completion re-read: the answer is already being selected.
                    seen.append(started.wait(2))
                return screen

            execute = Mock()
        model = Mock(spec=["decide", "select_fields", "verify_output", "side_channel"])
        model.decide.return_value = Decision("DONE", None, .99, .99, 0, "offline", 0, {}, StopGate.CONTINUE)
        model.side_channel.return_value = object()

        def select(*args, **kwargs):
            started.set()
            return {"data": {"model_number": "MTQM3LL/A"}, "citations": [
                {"path": "/model_number", "evidence_id": "e0", "quote": "Model Number, MTQM3LL/A"}]}
        model.select_fields.side_effect = select
        model.verify_output.return_value = OutputSupport.SUPPORTED
        schema = {"type": "object", "properties": {"model_number": {"type": "string"}},
                  "required": ["model_number"], "additionalProperties": False}
        result = Agent(Driver(), model).run("Report the Model Number", execute=True, output_schema=schema,
                                            output_format="json")
        self.assertEqual(seen, [True])
        self.assertEqual(result["data"], {"model_number": "MTQM3LL/A"})
        self.assertEqual(model.select_fields.call_count, 1)


SAFARI_START = Snapshot([Element("0", "Address", "TextField", (0, .9, 1, .05), True, "/f", "",
                                 ("TAP", "TYPE", "TYPE_SUBMIT"))], "Address", 400, 800, "wda",
                        bundle_id="com.apple.mobilesafari")
ARTICLE = Snapshot([Element("0", "Guido van Rossum", "StaticText", (0, .1, 1, .05), locator="/h")],
                   "Guido van Rossum", 400, 800, "wda", bundle_id="com.apple.mobilesafari")


class OpenUrlTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch
        flag = patch.dict("os.environ", {"MOBSTER_DIRECT_URL": "1"})  # off by default until validated live
        flag.start()
        self.addCleanup(flag.stop)

    def driver(self, *, fail=False):
        state = {"screen": SAFARI_START, "calls": []}

        class Driver:
            can_type = True

            def observe(self, timeout=10):
                return state["screen"]

            def call(self, method, path, body=None, timeout=10):
                state["calls"].append((method, path, body))
                if fail:
                    raise TransportError("unsupported")
                state["screen"] = ARTICLE

            execute = Mock()
        return Driver(), state

    def test_the_requested_address_is_opened_directly(self):
        driver, state = self.driver()
        model = Mock(spec=["decide", "stable_completion"], stable_completion=True)
        model.decide.return_value = decision("DONE", goal=.9)
        events = []
        result = Agent(driver, model, settle_seconds=0, emit=events.append).run(
            "In Safari: go to en.m.wikipedia.org/wiki/Guido_van_Rossum and report the year he was born",
            execute=True)
        self.assertEqual(state["calls"], [("POST", "/url", {"url": "https://en.m.wikipedia.org/wiki/Guido_van_Rossum", "bundleId": "com.apple.mobilesafari"})])
        driver.execute.assert_not_called()
        self.assertEqual(result["actions"], 1)
        self.assertEqual(result["status"], "completed_unverified")
        started = [e for e in events if e["event"] == "action_started"]
        self.assertEqual(started[0]["operation"], "OPEN_URL")
        # Jev decided on the loaded article, not the empty address bar.
        self.assertEqual(model.decide.call_args.args[0].text, "Guido van Rossum")

    def test_a_refused_open_falls_back_to_the_ordinary_path(self):
        driver, state = self.driver(fail=True)
        model = Mock(spec=["decide", "stable_completion"], stable_completion=True)
        model.decide.return_value = decision("DONE", goal=.9)
        events = []
        Agent(driver, model, settle_seconds=0, emit=events.append).run(
            "In Safari: go to example.com and report the heading", execute=True)
        self.assertEqual(len(state["calls"]), 1)
        self.assertIn("action_not_dispatched", [e["event"] for e in events])
        self.assertEqual(model.decide.call_args.args[0].text, "Address")

    def test_only_one_named_address_in_safari_qualifies(self):
        self.assertIsNone(requested_url("go to a.com and b.com"))
        self.assertIsNone(requested_url("Search the web for Ada Lovelace"))
        driver, state = self.driver()
        state["screen"] = Snapshot(SAFARI_START.elements, "Address", 400, 800, "wda", bundle_id="com.apple.Preferences")
        model = Mock(spec=["decide", "stable_completion"], stable_completion=True)
        model.decide.return_value = decision("DONE", goal=.9)
        Agent(driver, model, settle_seconds=0).run("go to example.com", execute=True)
        self.assertEqual(state["calls"], [])


class PredictedSpeculationTests(unittest.TestCase):
    def setUp(self):
        agent_module._transitions.clear()
        self.addCleanup(agent_module._transitions.clear)

    def run_once(self):
        home = Snapshot([Element("0", "General", "Cell", (0, .3, 1, .05), locator="/c")], "General", 400, 800, "wda")
        about = Snapshot([Element("0", "About", "StaticText", (0, .1, 1, .05), locator="/t")], "About", 400, 800, "wda")
        state = {"screen": home}

        class Driver:
            can_type = False

            def observe(self, timeout=10):
                return state["screen"]

            def execute(self, operation, target, snapshot, text=None, timeout=10):
                state["screen"] = about
        model = Mock(spec=["decide", "side_channel", "speculative_decisions", "stable_completion"])
        model.speculative_decisions, model.stable_completion = True, True
        model.side_channel.side_effect = lambda name="prefetch": f"{name}-http"
        calls = []

        def decide(snapshot, goal, history, **kwargs):
            calls.append(kwargs.get("http"))
            return decision("TAP", "0", confidence=.99) if snapshot.text == "General" else decision("DONE", goal=.9)
        model.decide.side_effect = decide
        events = []
        result = Agent(Driver(), model, settle_seconds=0, emit=events.append).run("Open General", execute=True)
        return result, calls, [e["event"] for e in events]

    def test_a_known_transition_decides_its_successor_at_dispatch(self):
        _, first_calls, first_events = self.run_once()
        self.assertNotIn("decision_predicted", first_events)
        result, calls, kinds = self.run_once()
        self.assertEqual(result["status"], "completed_unverified")
        self.assertIn("decision_predicted", kinds)
        self.assertIn("decision_speculation_used", kinds)
        self.assertEqual(calls, [None, "prediction-http"])

    def test_a_wrong_prediction_is_discarded(self):
        self.run_once()
        # Poison the memory: the same tap is predicted to reach a different screen.
        key = next(iter(agent_module._transitions))
        agent_module._transitions[key] = Snapshot(
            [Element("0", "Wi-Fi", "Cell", (0, .1, 1, .05), locator="/w")], "Wi-Fi", 400, 800, "wda")
        result, calls, kinds = self.run_once()
        self.assertIn("decision_speculation_discarded", kinds)
        self.assertEqual(calls, [None, "prediction-http", None])
        self.assertEqual(result["status"], "completed_unverified")


class PixelGuardTests(unittest.TestCase):
    def run_with(self, mode, still):
        button = Element("0", "Continue", "Button", (0, .3, 1, .05), locator="/b")
        screen = Snapshot([button], "Continue", 400, 800, "wda")
        after = Snapshot([Element("0", "Done", "StaticText", (0, .1, 1, .05), locator="/d")], "Done", 400, 800, "wda")
        state = {"screen": screen, "reads": 0}

        class Clock:
            def still_for(self):
                return still

        class Driver:
            can_type = False
            frame_clock = Clock()
            frame_clock_mode = mode

            def observe(self, timeout=10):
                state["reads"] += 1
                return state["screen"]

            def execute(self, operation, target, snapshot, text=None, timeout=10):
                state["reads_at_dispatch"] = state["reads"]
                state["screen"] = after
        model = Mock(spec=["decide", "verify_action", "stable_completion"], stable_completion=True)
        # A verified tap (not plain navigation): its post-verification re-read is the guard in question.
        model.decide.side_effect = [decision("TAP", "0", confidence=.8), decision("DONE", goal=.9)]
        model.verify_action.return_value = ActionSupport.ALLOWED
        events = []
        Agent(Driver(), model, settle_seconds=0, emit=events.append).run("Tap Continue", execute=True)
        return state["reads_at_dispatch"], [e["event"] for e in events]

    def test_still_pixels_replace_the_re_read_only_when_the_clock_is_on(self):
        reads, kinds = self.run_with("on", 30.0)
        self.assertEqual(reads, 1)
        self.assertIn("refresh_skipped_pixels_still", kinds)
        self.assertEqual(self.run_with("shadow", 30.0)[0], 2)
        self.assertEqual(self.run_with("on", None)[0], 2)  # Unhealthy stream: read as before.
        self.assertEqual(self.run_with("on", .1)[0], 2)  # Moved during the read: read again.


if __name__ == "__main__":
    unittest.main()


class RealVisionJudgeTests(unittest.TestCase):
    """The loop drives the real VisionJudge (eye-colour decomposition, crops from a WDA still)."""

    def test_eye_colour_loop_likes_only_non_blue_eyes(self):
        import base64
        import io
        from PIL import Image
        from mobile_agent.vision_judge import JudgeConfig, VisionJudge
        colours = {"Ana": (30, 60, 220), "Ben": (130, 80, 30), "Cy": (140, 90, 40)}

        class App(DeckApp):
            def capture_preview(self, timeout=3):
                name = self.names[min(self.index, len(self.names) - 1)]
                buffer = io.BytesIO()
                Image.new("RGB", (400, 800), colours[name]).save(buffer, "PNG")
                return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()

        class VisionHelper:
            requests = []

            def complete(self, messages, token_limit, timeout, purpose, **options):
                steps = options["response_format"]["json_schema"]["schema"]["properties"]["results"]["items"]
                keys = [key for key in steps["required"] if key != "image" and not key.endswith("_confidence")]
                rows = []
                images = [part for part in messages[1]["content"] if part.get("type") == "image_url"]
                for index, part in enumerate(images, 1):
                    raw = base64.b64decode(part["image_url"]["url"].split(",", 1)[1])
                    r, g, b = Image.open(io.BytesIO(raw)).convert("RGB").getpixel((4, 4))
                    row = {"image": index}
                    for key in keys:
                        # The last step is the colour question; the gates before it answer yes.
                        row[key] = ("blue" if b > r else "brown") if key == keys[-1] else "yes"
                        row[key + "_confidence"] = .96
                    rows.append(row)
                VisionHelper.requests.append(keys)
                return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"results": rows})}}]}

        program = deck_program(predicate={"question": "Does this person have blue eyes?", "decompose": "eye_colour",
                                          "match_values": ["blue"], "positive": "yes", "true_choices": ["no"],
                                          "false_choices": ["yes"], "aggregate": "any"})
        judge = VisionJudge(helper_factory=lambda model=None: VisionHelper(), config=JudgeConfig(t2_enabled=False))
        self.addCleanup(judge.close)
        app = App(["Ana", "Ben", "Cy"])
        result = Agent(app, shadow_model(), Helper(program), settle_seconds=0, vision_judge=judge).run(
            GOAL, execute=True)
        self.assertEqual(app.taps(), [("Skip", "Ana"), ("Like", "Ben"), ("Like", "Cy")])
        self.assertEqual(result["loop"]["counts"]["matched"], 2)
        self.assertEqual(len(VisionHelper.requests[0]), 4)  # Face, eyes, colour photo, then the iris.


class WarmClientsTests(unittest.TestCase):
    def test_every_client_is_warmed_off_thread_and_failures_are_ignored(self):
        from mobile_agent.compose import warm_clients
        calls = []

        class Jev:
            def warm(self, connections=1):
                calls.append(("jev", connections))

        class Helper:
            def warm(self):
                calls.append(("helper",))

        class Broken:
            def warm(self):
                raise RuntimeError("no network")
        warm_clients(Jev(), Broken(), Helper()).join(2)
        self.assertEqual(calls, [("jev", 3), ("helper",)])
        warm_clients(None, None, None).join(2)
