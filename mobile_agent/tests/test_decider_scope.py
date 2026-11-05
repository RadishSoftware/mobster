import unittest
from types import SimpleNamespace

from mobile_agent import decider_scope as scope
from mobile_agent.decision_provider import parse_choice
from mobile_agent.marks import element_marks, mark_lines, options


def el(label, role="Button", rect=(.1, .1, .3, .05), editable=False, value=""):
    return SimpleNamespace(label=label, role=role, rect=rect, editable=editable, value=value)


class Scope(unittest.TestCase):
    def test_navigation_targets_are_admitted(self):
        for op in ("TAP", "SWIPE_UP", "DISMISS", "HOME", "LAUNCH_APP", "WAIT"):
            scope.admit(scope.TARGET, op=op)
        scope.admit(scope.CHECK, topic="toast")
        scope.admit(scope.BRANCH)

    def test_text_reading_done_and_commits_are_refused(self):
        for op in ("TYPE", "TYPE_SUBMIT", "SET_TEXT", "SUBMIT", "DONE", "BLOCKED", "READ_LIST", "LONG_PRESS", "TAP_XY"):
            with self.assertRaises(scope.OutOfScope):
                scope.admit(scope.TARGET, op=op)
        with self.assertRaises(scope.OutOfScope):
            scope.admit(scope.CHECK, topic="extract_amount")
        with self.assertRaises(scope.OutOfScope):
            scope.admit("summarize")


    def test_an_unreadable_target_fails_closed(self):
        self.assertTrue(scope.commits(None))


class Seam(unittest.TestCase):
    def test_a_choice_outside_the_offer_or_not_the_argmax_is_no_answer(self):
        offered = {"1": "a", "2": "b"}
        self.assertIsNone(parse_choice({"choice": "7", "probabilities": {"1": .5, "2": .5}}, offered))
        self.assertIsNone(parse_choice({"choice": "1", "probabilities": {"1": .2, "2": .8}}, offered))
        self.assertIsNone(parse_choice(None, offered))


class Marks(unittest.TestCase):
    def test_marks_are_numbered_and_resolve_to_their_element(self):
        elements = [el("General"), el("Clock", "StaticText"), el("", "Other", rect=(0, 0, 0, 0))]
        marks = element_marks(elements)
        self.assertEqual([m.id for m in marks], ["1", "2"])
        self.assertIs(marks[0].element, elements[0])
        self.assertAlmostEqual(marks[0].center[0], .25)
        self.assertIn("none", options(marks))
        self.assertTrue(mark_lines(marks)[0].startswith('1 Button "General" @10,10,30,5'))


if __name__ == "__main__":
    unittest.main()
