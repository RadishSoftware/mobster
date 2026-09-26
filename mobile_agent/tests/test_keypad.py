"""Arithmetic in the request compiled to key presses; the result is still read from the display. Offline."""

import unittest

from mobile_agent.keypad import arithmetic, calculator_keys
from mobile_agent.routes import compile_route
from mobile_agent.state import Element, Snapshot

KEYS = ["All Clear", "Divide", "7", "8", "9", "Multiply", "4", "5", "6", "Subtract", "1", "2", "3", "Add", "0",
        "Point", "Equals", "Change Sign"]


def calculator():
    elements = [Element(str(i), label, "Key", (.1 + (i % 4) * .2, .5 + (i // 4) * .08, .18, .07))
                for i, label in enumerate(KEYS)]
    return Snapshot(elements, "0", 393, 852, "synthetic_fixture", bundle_id="com.apple.calculator")


class ArithmeticTests(unittest.TestCase):
    def test_phrasings(self):
        self.assertEqual(arithmetic("compute 5 times 5 using the keypad"), ("5", "×", "5"))
        self.assertEqual(arithmetic("compute 123 plus 456"), ("123", "+", "456"))
        self.assertEqual(arithmetic("Subtract 1800 from 1889."), ("1889", "-", "1800"))
        self.assertEqual(arithmetic("Multiply 3 by 7 and report the product"), ("3", "×", "7"))
        self.assertIsNone(arithmetic("Open Calculator and report the display"))

    def test_only_calculator_requests_compile(self):
        self.assertIsNone(calculator_keys("In Notes, write 5 times 5"))
        self.assertEqual(calculator_keys("In Calculator: compute 12.5 plus 3"),
                         ["1", "2", "Point", "5", "Add", "3", "Equals"])


class KeyRouteTests(unittest.TestCase):
    def test_clears_then_presses_every_key_even_when_nothing_redraws(self):
        route = compile_route("In Calculator: In Calculator, compute 5 times 5 using the keypad and report the result.")
        pressed = []
        screen = calculator()
        while (step := route.step(screen)) is not None:
            operation, key = step
            pressed.append(key.label)
            route.tapped(False)
        self.assertEqual(pressed, ["All Clear", "5", "Multiply", "5", "Equals"])
        self.assertIsNone(route.failed)

    def test_a_missing_key_hands_back(self):
        route = compile_route("In Calculator: compute 5 times 5")
        screen = Snapshot([Element("0", "5", "Key", (.1, .5, .1, .1))], "0", 393, 852, "synthetic_fixture")
        route.step(screen)
        route.tapped(True)
        self.assertIsNone(route.step(screen))
        self.assertEqual(route.failed, "key_missing")


if __name__ == "__main__":
    unittest.main()
