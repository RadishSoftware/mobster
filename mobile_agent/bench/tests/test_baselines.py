"""Both screenshot baselines against a fake phone and scripted Vertex responses."""

import json
import unittest

from mobile_agent.bench.agents.gemini_cu import GeminiComputerUseAgent, prune_screenshots
from mobile_agent.bench.agents.som import SetOfMarksAgent, annotate, describe
from mobile_agent.bench.suite import build_suite
from mobile_agent.bench.tests.fakes import (FakePhone, FakeVertex, call, context, element, model_turn, png,
                                            snapshot, text)

TASKS = {task.id: task for task in build_suite()}
VERSION, NAV, ABSENT = TASKS["ret.ios_version"], TASKS["nav.accessibility"], TASKS["ret.absent.home_address"]


def cu(responses, phone=None, task=VERSION):
    vertex = FakeVertex(responses)
    agent = GeminiComputerUseAgent("gemini-3.8-flash", vertex=vertex, sleep=lambda s: None)
    phone = phone or FakePhone([snapshot([element("General", rect=(0, .4, 1, .06))])])
    return agent.run(task, context(phone, task)), vertex, phone


class ComputerUseTests(unittest.TestCase):
    def test_request_uses_the_mobile_environment_and_sends_a_screenshot(self):
        run, vertex, _ = cu([model_turn(text('ANSWER_JSON: {"status": "done", "answer": {"ios_version": "26.0.1"}}'))])
        body = vertex.requests[0]
        self.assertEqual(body["tools"], [{"computer_use": {"environment": "ENVIRONMENT_MOBILE"}}])
        parts = body["contents"][0]["parts"]
        self.assertIn("ios_version", parts[0]["text"])
        self.assertEqual(parts[1]["inlineData"]["mimeType"], "image/png")
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.answer, {"ios_version": "26.0.1"})
        self.assertEqual(run.model_calls, 1)
        self.assertIsNone(run.first_action_ms)

    def test_click_maps_the_0_999_grid_to_points_and_returns_a_screenshot(self):
        run, vertex, phone = cu([model_turn(text("tap General"), call("click", x=500, y=430)),
                                 model_turn(text('ANSWER_JSON: {"status": "done", "answer": {"ios_version": "x"}}'))])
        self.assertIn(("tap", round(.5 * 393), round(.43 * 852)), phone.calls)
        response = vertex.requests[1]["contents"][-1]["parts"][0]["functionResponse"]
        self.assertEqual(response["name"], "click")
        self.assertEqual(response["id"], "id-click")
        self.assertEqual(response["response"], {"status": "ok"})
        self.assertIn("inlineData", response["parts"][0])
        # The model's own turn is echoed back verbatim (thought signatures round-trip).
        self.assertEqual(vertex.requests[1]["contents"][1]["role"], "model")
        self.assertEqual(run.action_count, 1)
        self.assertIsNotNone(run.first_action_ms)

    def test_type_open_app_back_and_keys(self):
        search = TASKS["text.settings_search"]
        phone = FakePhone([snapshot([element("Search", role="SearchField", editable=True,
                                             actions=("TAP", "TYPE", "TYPE_SUBMIT"))])])
        run, _, phone = cu([model_turn(call("open_app", app_name="Settings")),
                            model_turn(call("type", text="Auto-Lock", press_enter=True)),
                            model_turn(call("go_back")), model_turn(call("press_key", key="enter")),
                            model_turn(text('ANSWER_JSON: {"status": "done", "answer": null}'))], phone, search)
        self.assertIn(("activate", "com.apple.Preferences"), phone.calls)
        self.assertIn(("type", "Auto-Lock", True), phone.calls)
        self.assertIn(("back",), phone.calls)
        self.assertIn(("key", "\n"), phone.calls)
        self.assertEqual(run.status, "completed")

    def test_safety_confirmation_is_never_granted(self):
        run, _, phone = cu([model_turn(call("click", x=10, y=10, safety_decision={
            "decision": "require_confirmation", "explanation": "captcha"}))])
        self.assertEqual(run.status, "safety_confirmation")
        self.assertFalse([c for c in phone.calls if c[0] == "tap"])

    def test_tap_on_a_switch_is_refused_and_stops_the_run(self):
        phone = FakePhone([snapshot([element("Airplane Mode", role="Switch", rect=(.8, .2, .15, .05))])])
        run, _, phone = cu([model_turn(call("click", x=870, y=220))], phone, NAV)
        self.assertEqual(run.status, "unsafe_stopped")
        self.assertFalse([c for c in phone.calls if c[0] == "tap"])

    def test_infeasible_is_an_abstention(self):
        run, _, _ = cu([model_turn(text('Not shown. ANSWER_JSON: {"status": "infeasible", "answer": null}'))],
                       task=ABSENT)
        self.assertEqual(run.status, "abstained")
        self.assertTrue(run.abstained)

    def test_step_budget(self):
        responses = [model_turn(call("wait", seconds=1)) for _ in range(VERSION.max_steps + 2)]
        run, _, _ = cu(responses)
        self.assertEqual(run.status, "step_budget")
        self.assertEqual(run.decision_steps, VERSION.max_steps)

    def test_only_recent_screenshots_are_kept(self):
        contents = [{"role": "user", "parts": [{"text": "t"}, {"inlineData": {"data": "0"}}]}]
        for i in range(5):
            contents.append({"role": "model", "parts": [call("wait")]})
            contents.append({"role": "user", "parts": [{"functionResponse": {"name": "wait", "response": {},
                                                                             "parts": [{"inlineData": {"data": str(i)}}]}}]})
        prune_screenshots(contents, 3)
        kept = [c for c in contents if c["role"] == "user" and any(
            "inlineData" in p or (p.get("functionResponse") or {}).get("parts") for p in c["parts"])]
        self.assertEqual(len(kept), 3)
        self.assertEqual(contents[-1]["parts"][0]["functionResponse"]["parts"][0]["inlineData"]["data"], "4")


def som(actions, phone, task=VERSION):
    responses = [model_turn(text(json.dumps(action))) for action in actions]
    vertex = FakeVertex(responses)
    agent = SetOfMarksAgent("gemini-3.1-pro-preview", vertex=vertex, sleep=lambda s: None)
    return agent.run(task, context(phone, task)), vertex, phone


class SetOfMarksTests(unittest.TestCase):
    def setUp(self):
        self.general = element("General", rect=(0, .4, 1, .06))
        self.switch = element("Airplane Mode", role="Switch", rect=(.8, .2, .15, .05))
        self.phone = FakePhone([snapshot([self.general, self.switch], title="Settings")])

    def test_click_by_index_taps_the_element_centre_and_answers(self):
        run, vertex, phone = som([{"reason": "open", "action_type": "click", "index": 0},
                                  {"reason": "done", "action_type": "status", "goal_status": "complete",
                                   "answer_json": '{"status": "done", "answer": {"ios_version": "26.0.1"}}'}],
                                 self.phone)
        self.assertIn(("tap", round(.5 * 393), round(.43 * 852)), phone.calls)
        prompt = vertex.requests[0]["contents"][0]["parts"][0]["text"]
        self.assertIn('[0] Cell "General"', prompt)
        self.assertEqual(vertex.requests[0]["generationConfig"]["responseMimeType"], "application/json")
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.answer, {"ios_version": "26.0.1"})
        self.assertIn("click", vertex.requests[1]["contents"][0]["parts"][0]["text"])  # history carried

    def test_switch_is_refused(self):
        run, _, phone = som([{"reason": "x", "action_type": "click", "index": 1}], self.phone, NAV)
        self.assertEqual(run.status, "unsafe_stopped")
        self.assertFalse([c for c in phone.calls if c[0] == "tap"])

    def test_scroll_input_and_infeasible(self):
        field = element("Search", role="SearchField", editable=True, actions=("TAP", "TYPE", "TYPE_SUBMIT"))
        phone = FakePhone([snapshot([field])])
        run, _, phone = som([{"reason": "s", "action_type": "scroll", "direction": "down"},
                             {"reason": "t", "action_type": "input_text", "index": 0, "text": "Auto"},
                             {"reason": "n", "action_type": "status", "goal_status": "infeasible"}],
                            phone, TASKS["text.settings_search"])
        self.assertIn(("swipe", "SWIPE_UP", None), phone.calls)
        self.assertIn(("type", "Auto", False), phone.calls)
        self.assertEqual(run.status, "abstained")

    def test_marks_are_drawn_and_described(self):
        image = annotate(png(393, 852), [self.general, self.switch])
        self.assertTrue(image.startswith(b"\x89PNG"))
        self.assertIn('[1] Switch "Airplane Mode"', describe([self.general, self.switch]))


if __name__ == "__main__":
    unittest.main()
