"""A vertical swipe never starts on a slider or switch (it would drag the control, not the page). Offline."""

import unittest
from types import SimpleNamespace

from mobile_agent.drivers import clear_stroke_start


def screen(*controls):
    return SimpleNamespace(elements=[SimpleNamespace(role=role, rect=rect) for role, rect in controls])


class SwipeStartTests(unittest.TestCase):
    def test_a_swipe_up_starting_on_the_brightness_slider_starts_above_it(self):
        brightness = screen(("Slider", (.08, .77, .83, .04)), ("StaticText", (.08, .6, .5, .03)))
        y = clear_stroke_start(brightness, "SWIPE_UP", 196, .80 * 852, 393, 852)
        self.assertLess(y / 852, .77 - .01)
        self.assertGreater(y / 852, .7)

    def test_a_clear_start_is_unchanged(self):
        self.assertEqual(clear_stroke_start(screen(("Slider", (.08, .3, .83, .04))), "SWIPE_UP", 196, 681.6, 393, 852),
                         681.6)
        self.assertEqual(clear_stroke_start(screen(("Button", (0, 0, 1, 1))), "SWIPE_UP", 196, 681.6, 393, 852), 681.6)

    def test_a_swipe_down_moves_its_start_down_and_sideways_swipes_are_untouched(self):
        y = clear_stroke_start(screen(("Switch", (.08, .18, .83, .04))), "SWIPE_DOWN", 196, .2 * 852, 393, 852)
        self.assertGreater(y / 852, .23)
        self.assertEqual(clear_stroke_start(screen(("Slider", (0, 0, 1, 1))), "SWIPE_LEFT", 196, 400, 393, 852), 400)

    def test_a_screen_covered_in_controls_keeps_the_original_start(self):
        self.assertEqual(clear_stroke_start(screen(("Picker", (0, 0, 1, 1))), "SWIPE_UP", 196, 681.6, 393, 852), 681.6)


if __name__ == "__main__":
    unittest.main()
