import unittest

from mobile_agent.frontier import CHANGED_WHILE_ASKING
from mobile_agent.permissions import ASK_IMPORTANT, PermissionGate, policy
from mobile_agent.state import Element
from mobile_agent.tests import test_frontier as F
from mobile_agent.tests.test_turn_exits import Model, refused, run

TAP = [("TAP", "Help", None)]
DONE = [("DONE", None, None)]
HELP = Element("1", "Help", "Button", (.4, .9, .2, .05))
EMAIL = Element("1", "Email", "TextField", (.1, .2, .8, .05), True, actions=("TAP", "TYPE", "TYPE_SUBMIT"))
SWIPE = [("SWIPE_LEFT", "Mail from Ann", None)]
ROW = Element("1", "Mail from Ann", "Cell", (.05, .3, .7, .06))
DELETE = Element("2", "Delete", "Button", (.8, .3, .15, .06))
BLOCK = Element("0", "Block this Caller", "Cell", (0, .6, .995, .05))


def go(screens, model, request="Use the app.", **kwargs):
    driver = F.Driver(screens)
    result, events = run(driver, model, request, **kwargs)
    return driver, result, events


# One refuse test and one act test for each stage that had neither: the loop and repeat rules, a disabled control,
# a swipe on a row whose actions show, and a screen that moved while the person decided.
class LoopStageTests(unittest.TestCase):
    def test_the_third_identical_tap_on_an_unchanged_screen_is_refused_as_a_loop(self):
        driver, _, events = go([F.screen(HELP)] * 8, Model(TAP, TAP, TAP, DONE), "Tap Help three times.")
        self.assertEqual(driver.actions, [("TAP", "Help", None)] * 2)
        self.assertEqual(refused(events), ["loop"])

    def test_the_second_identical_tap_is_sent(self):
        driver, _, events = go([F.screen(HELP)] * 8, Model(TAP, TAP, DONE), "Tap Help twice.")
        self.assertEqual(driver.actions, [("TAP", "Help", None)] * 2)
        self.assertEqual(refused(events), [])

    def test_the_same_text_typed_again_into_the_same_field_is_refused_as_a_repeat(self):
        driver, _, events = go([F.screen(EMAIL)] * 8, Model([("TYPE", "Email", "ann@example.com")],
                                                          [("TYPE", "Email", "ann@example.com")], DONE),
                               "Type ann@example.com into Email.")
        self.assertEqual(driver.actions, [("TYPE", "Email", "ann@example.com")])
        self.assertEqual(refused(events), ["repeat"])

    def test_other_text_typed_into_the_same_field_is_sent(self):
        driver, _, events = go([F.screen(EMAIL)] * 8, Model([("TYPE", "Email", "ann@example.com")],
                                                          [("TYPE", "Email", "bob@example.com")], DONE),
                               "Type an email into Email.")
        self.assertEqual([a[2] for a in driver.actions], ["ann@example.com", "bob@example.com"])
        self.assertEqual(refused(events), [])


class ShapeStageTests(unittest.TestCase):
    def test_a_disabled_control_is_refused_and_tells_the_model_what_enables_it(self):
        off = Element("1", "Continue", "Button", (.4, .9, .2, .05), enabled=False)
        model = Model([("TAP", "Continue", None)], DONE)
        driver, _, events = go([F.screen(off)] * 4, model, "Tap Continue.")
        self.assertEqual((driver.actions, refused(events)), ([], ["disabled"]))
        self.assertIn("'Continue' is disabled: it does nothing yet. Do what enables it first", model.prompts[1])

    def test_an_enabled_control_is_tapped(self):
        on = Element("1", "Continue", "Button", (.4, .9, .2, .05))
        driver, _, events = go([F.screen(on)] * 4, Model([("TAP", "Continue", None)], DONE), "Tap Continue.")
        self.assertEqual((driver.actions, refused(events)), ([("TAP", "Continue", None)], []))


class SwipeStageTests(unittest.TestCase):
    def test_a_swipe_on_a_row_whose_actions_already_show_is_refused(self):
        model = Model(SWIPE, DONE)
        driver, _, events = go([F.screen(ROW, DELETE)] * 4, model, "Show the delete button on Mail from Ann.")
        self.assertEqual((driver.actions, refused(events)), ([], ["swipe_again"]))
        self.assertIn("'Mail from Ann' already shows its actions. TAP the one the request needs, or leave it.",
                      model.prompts[1])

    def test_a_swipe_on_a_row_whose_actions_are_hidden_is_sent(self):
        driver, _, events = go([F.screen(ROW)] * 4, Model(SWIPE, DONE), "Show the delete button on Mail from Ann.")
        self.assertEqual((driver.actions, refused(events)), ([("SWIPE_LEFT", "Mail from Ann", None)], []))


class AskingStageTests(unittest.TestCase):
    def ask(self, moves):
        phone = {"com.apple.mobilephone": "Phone"}
        driver = F.Driver([F.screen(BLOCK, bundle="com.apple.mobilephone")] * 10)
        asked = []

        def asker(request_):
            asked.append(request_)
            if moves:
                driver.screens = [F.screen(Element("0", "Block this Caller", "Cell", (0, .7, .995, .05)),
                                           bundle="com.apple.mobilephone")] * 10
            return "approved"
        model = Model([("TAP", "Block this Caller", None)], DONE)
        _, events = run(driver, model, "Block this caller", approve=asker, apps=phone,
                        permissions=PermissionGate(ASK_IMPORTANT, apps=policy.ALLOW_ALL))
        return driver, asked, events, model

    def test_a_control_that_moved_while_the_person_decided_is_not_tapped(self):
        driver, asked, events, model = self.ask(moves=True)
        self.assertEqual(([r["label"] for r in asked], driver.actions), (["Block this Caller"], []))
        self.assertEqual(refused(events), ["changed_while_asking"])
        self.assertIn(CHANGED_WHILE_ASKING, model.prompts[1])

    def test_a_control_that_stayed_where_it_was_is_tapped_after_the_answer(self):
        driver, asked, events, _ = self.ask(moves=False)
        self.assertEqual(([r["label"] for r in asked], driver.actions),
                         (["Block this Caller"], [("TAP", "Block this Caller", None)]))
        self.assertEqual(refused(events), [])


if __name__ == "__main__":
    unittest.main()
