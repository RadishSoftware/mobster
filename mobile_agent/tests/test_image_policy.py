"""When a Smart turn carries the screenshot (image_policy.py) and how the loop uses it. Offline: rows as the loop
prints them and a scripted model."""

import os
import unittest
from unittest.mock import patch

from mobile_agent import image_policy as policy
from mobile_agent.frontier import FrontierAgent, prompt_text
from mobile_agent.state import Element
from mobile_agent.tests.test_frontier import Driver, Script, screen

IMAGE = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA", "detail": "low"}}
ROWS = [f'e{i} StaticText "Row {i}" @4,{10 + i * 5},60,3' for i in range(1, 9)]
NOTHING = policy.Turn()
NEXT = policy.Turn(blind=0)


def rows(*extra, base=ROWS):
    return policy.parse_rows([*base, *extra])


def keeps(*extra, turn=NEXT, base=ROWS):
    return policy.decide(rows(*extra, base=base), turn)


class RowTests(unittest.TestCase):
    def test_rows_are_parsed_as_the_loop_prints_them(self):
        parsed = policy.parse_rows([
            'e1 Button "Save" selected @4,10,20,5',
            'e2 Switch "Wi-Fi" value="on" @80,22,16,3',
            'e3 TextField (no label: find it by position in the screenshot) value="x" editable @26,16,66,3',
            'e4 Icon "Maps" value="Widget" @27,21',
            "not a row"])
        self.assertEqual([r.alias for r in parsed], ["e1", "e2", "e3", "e4"])
        self.assertTrue(parsed[0].selected and parsed[1].has_value)
        self.assertFalse(parsed[2].labeled)
        self.assertEqual((parsed[0].w, parsed[0].h, parsed[3].w), (20, 5, 0))

    def test_unreadable_rows_and_empty_controls_keep_the_picture(self):
        for extra in ('e9 WebView "Article" @-1,10,100,80', 'e9 Button "" @4,80,10,5', 'e9 unrecognized row'):
            with self.subTest(row=extra):
                self.assertTrue(keeps(extra)[0])

    def test_a_quote_in_a_label_is_kept(self):
        self.assertEqual(policy.parse_rows([r'e1 Button "Say \"hi\"" @1,2,3,4'])[0].label, 'Say "hi"')


class DecideTests(unittest.TestCase):
    def test_a_plain_screen_of_labelled_rows_goes_without_a_picture(self):
        self.assertEqual(keeps(), (False, "tree carries the screen"))

    def test_the_turns_that_always_carry_it(self):
        for turn, why in ((policy.Turn(first=True), "first turn"), (policy.Turn(new_app=True), "new app"),
                          (policy.Turn(missed=True), "after a miss"),
                          (policy.Turn(skill_pictures=True), "skill pictures"),
                          (policy.Turn(pixel_read=True), "pixel read"), (policy.Turn(long_task=True), "long task"),
                          (policy.Turn(blind=policy.MAX_BLIND), "refresh"),
                          (policy.Turn(request="What colour is the icon?"), "visual request")):
            with self.subTest(why=why):
                self.assertEqual(keeps(turn=turn), (True, why))
        self.assertEqual(keeps(turn=policy.Turn(blind=policy.MAX_BLIND - 1))[0], False)

    def test_a_screen_the_rows_cannot_describe_keeps_it(self):
        cases = {
            "too few rows": ('e1 Button "OK" @4,10,20,5',),
            "unlabeled control": ('e9 Button (no label: find it by position in the screenshot) @4,80,10,5',),
            "opaque view": ('e9 WebView "Article" @0,10,100,80',),
            "large picture": ('e9 Image "Photo by Sam" @0,10,100,60',),
            "state not in the rows": ('e9 SegmentedControl "Orders" @35,7,31,5',),
        }
        for why, extra in cases.items():
            with self.subTest(why=why):
                self.assertEqual(keeps(*extra, base=ROWS[:2] if why == "too few rows" else ROWS), (True, why))

    def test_state_that_the_rows_do_show_does_not_keep_it(self):
        self.assertFalse(keeps('e9 SegmentedControl "Orders" value="Open" @35,7,31,5')[0])
        self.assertFalse(keeps('e9 Toolbar "Toolbar" @0,90,100,10', 'e10 Button "Home" selected @4,91,20,5')[0])
        self.assertFalse(keeps('e9 Switch "Wi-Fi" value="on" @80,22,16,3')[0])
        self.assertFalse(keeps('e9 Image "AdditionalDimmingOverlay" @0,0,100,100')[0])        # chrome, not content
        self.assertEqual(keeps('e9 Toolbar "Toolbar" @0,90,100,10')[1], "state not in the rows")   # no tab selected

    def test_every_reason_the_policy_gives_is_listed(self):
        seen = {keeps()[1], *(keeps(turn=policy.Turn(**{k: v}))[1] for k, v in (
            ("first", True), ("new_app", True), ("missed", True), ("skill_pictures", True), ("pixel_read", True),
            ("long_task", True), ("blind", 9)))}
        self.assertLessEqual(seen, set(policy.REASONS))

    def test_the_mode_is_always_unless_the_switch_says_tree(self):
        for value, want in ((None, "always"), ("tree", "tree"), (" TREE ", "tree"), ("always", "always"),
                            ("nonsense", "always")):
            with self.subTest(value=value), \
                    patch.dict(os.environ, {} if value is None else {"MOBSTER_IMAGE_POLICY": value}):
                if value is None:
                    os.environ.pop("MOBSTER_IMAGE_POLICY", None)
                self.assertEqual(policy.mode(), want)


def shelf(count=6):
    return screen(*(Element(str(i), f"Item {i}", "Button", (.05, .05 + i * .06, .5, .04)) for i in range(count)),
                  bundle="com.example.shop")


class Spy(Script):
    def __init__(self, steps):
        super().__init__(steps)
        self.sent = []

    def complete(self, messages, schema, timeout=60):
        if "items" not in schema["properties"]:
            self.sent.append((any(p.get("type") == "image_url" for p in messages[1]["content"]), prompt_text(messages)))
        return super().complete(messages, schema, timeout)


class LoopTests(unittest.TestCase):
    def run_loop(self, steps, screens, mode="tree", request="Open the shop tab and add nothing."):
        script, driver, events = Spy(steps), Driver(screens), []
        with patch.dict(os.environ, {"MOBSTER_IMAGE_POLICY": mode}), \
                patch("mobile_agent.frontier.safety.screenshot_part", return_value=IMAGE):
            FrontierAgent(driver, script, emit=events.append, screenshots=True, settle_seconds=0).run(request)
        return script, driver, events

    def test_with_the_policy_on_the_picture_goes_on_the_first_turn_and_every_few_after(self):
        same = shelf()
        steps = [("SWIPE_UP", None, None)] * 6 + [("DONE", None, None), ("DONE", None, None)]
        script, _, events = self.run_loop(steps, [same] * 12)
        sent = [image for image, _ in script.sent]
        self.assertEqual(sent[:5], [True, False, False, False, True])           # first turn, MAX_BLIND turns without
        whys = [e["image_why"] for e in events if e["event"] == "frontier_prompt"]
        self.assertEqual(whys[:5], ["first turn", "tree carries the screen", "tree carries the screen",
                                    "tree carries the screen", "refresh"])
        self.assertIn("No screenshot this turn", script.sent[1][1])
        self.assertNotIn("No screenshot this turn", script.sent[0][1])

    def test_a_failed_capture_is_retried_before_the_policy_can_omit_a_picture(self):
        captures = iter([None, IMAGE])
        with patch.object(FrontierAgent, "_screenshot", side_effect=lambda *_: next(captures, IMAGE)):
            script, _, events = self.run_loop([("SWIPE_UP", None, None)] * 3 + [("DONE", None, None)] * 2,
                                              [shelf()] * 12)
        self.assertEqual([event["image_why"] for event in events if event["event"] == "frontier_prompt"][:2],
                         ["first turn", "first turn"])
        self.assertEqual([image for image, _ in script.sent][:3], [False, True, False])

    def test_a_miss_brings_the_picture_back_on_the_next_turn(self):
        screens = [shelf()] * 12
        steps = [("SWIPE_UP", None, None), ("TAP", "Item that is not there", None), ("SWIPE_UP", None, None),
                 ("DONE", None, None), ("DONE", None, None)]
        script, _, events = self.run_loop(steps, screens)
        whys = [e["image_why"] for e in events if e["event"] == "frontier_prompt"]
        self.assertEqual(whys[:3], ["first turn", "tree carries the screen", "after a miss"])

    def test_point_taps_require_the_picture_and_resume_with_one_after_refusal(self):
        class PointModel(Spy):
            def complete(self, messages, schema, timeout=60):
                out, usage = super().complete(messages, schema, timeout)
                for action in out.get("actions", []):
                    if action["operation"] == "TAP_XY":
                        action.update(target="50,90", text="the unlisted control")
                return out, usage

        for blind in (False, True):
            with self.subTest(without_picture=blind):
                steps = ([("SWIPE_UP", None, None)] if blind
                         else []) + [("TAP_XY", None, None)] + [("DONE", None, None)] * 4
                model, driver, events = PointModel(steps), Driver([shelf()] * 12), []
                with patch.dict(os.environ, {"MOBSTER_IMAGE_POLICY": "tree"}), \
                        patch("mobile_agent.frontier.safety.screenshot_part", return_value=IMAGE), \
                        patch.object(Driver, "tap_point", create=True) as tap, \
                        patch.object(FrontierAgent, "_point_approval", return_value=(None, 0, False)):
                    FrontierAgent(driver, model, emit=events.append, screenshots=True,
                                  settle_seconds=0).run("Open the shop tab.")
                self.assertEqual(tap.call_count, 0 if blind else 1)
                if blind:
                    prompts = [event for event in events if event["event"] == "frontier_prompt"]
                    self.assertEqual(prompts[2]["image_why"], "after a miss")
                    self.assertIn("Read a screenshot before choosing screen points.", model.sent[2][1])

    def test_the_default_sends_a_picture_every_turn_with_the_prompts_as_before(self):
        steps = [("SWIPE_UP", None, None)] * 3 + [("DONE", None, None), ("DONE", None, None)]
        script, _, events = self.run_loop(steps, [shelf()] * 10, mode="always")
        self.assertTrue(all(image for image, _ in script.sent))
        self.assertFalse(any("No screenshot this turn" in text for _, text in script.sent))
        self.assertEqual({e["image_why"] for e in events if e["event"] == "frontier_prompt"}, {"off"})

    def test_a_failed_capture_in_the_default_mode_is_not_announced_to_the_model(self):
        script, driver, events = Spy([("DONE", None, None), ("DONE", None, None)]), Driver([shelf()] * 4), []
        with patch.dict(os.environ, {"MOBSTER_IMAGE_POLICY": "always"}), \
                patch("mobile_agent.frontier.safety.screenshot_part", return_value=None):
            FrontierAgent(driver, script, emit=events.append, screenshots=True, settle_seconds=0).run("Look at it.")
        self.assertFalse(any("No screenshot this turn" in text for _, text in script.sent))
