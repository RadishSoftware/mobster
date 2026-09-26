"""Arithmetic a request spells out, compiled to the key presses that compute it.

"In Calculator, compute 123 plus 456", "Multiply 3 by 7", "subtract 1800 from 1889":
the operands and the operation are in the request, so which keys to press is not a
judgment. Measured (MobsterBench pass 4, 24 Sep): Jev pressed 14 keys for "multiply
3 by 7" and never read a product. The compiled sequence clears the calculator, types
each operand digit by digit, the operator and Equals; the answer is then read from the
display like any other literal, so the result is still observed, never computed here.
"""

import re

_NUMBER = r"(-?\d[\d,]*(?:\.\d+)?)"
OPERATOR_KEYS = {"+": "Add", "-": "Subtract", "×": "Multiply", "÷": "Divide"}
_INFIX = re.compile(_NUMBER + r"\s*(plus|\+|minus|−|-(?=\s)|times|x|×|\*|multiplied by|divided by|/|÷)\s*" + _NUMBER,
                    re.I)
_VERB = re.compile(r"\b(multiply|divide)\s+" + _NUMBER + r"\s+by\s+" + _NUMBER
                   + r"|\b(add)\s+" + _NUMBER + r"\s+to\s+" + _NUMBER
                   + r"|\b(subtract)\s+" + _NUMBER + r"\s+from\s+" + _NUMBER, re.I)
_SYMBOL = {"plus": "+", "+": "+", "minus": "-", "−": "-", "-": "-", "times": "×", "x": "×", "×": "×", "*": "×",
           "multiplied by": "×", "divided by": "÷", "/": "÷", "÷": "÷"}
DIGIT_KEYS = {**{str(d): str(d) for d in range(10)}, ".": "Point"}
CLEAR_KEYS = ("All Clear", "Clear")


def arithmetic(goal):
    """(a, op, b) with op in + - × ÷ when the request states one binary calculation; else None."""
    goal = goal or ""
    match = _VERB.search(goal)
    if match:
        groups = match.groups()
        if groups[0]:
            return groups[1], "×" if groups[0].casefold() == "multiply" else "÷", groups[2]
        if groups[3]:
            return groups[4], "+", groups[5]
        return groups[8], "-", groups[7]  # subtract A from B is B - A
    match = _INFIX.search(goal)
    if match:
        return match.group(1), _SYMBOL[match.group(2).casefold()], match.group(3)
    return None


def key_sequence(a, op, b):
    """The keys to press after clearing: digits of a, the operator, digits of b, Equals."""
    keys = []
    for operand, after in ((a, OPERATOR_KEYS[op]), (b, "Equals")):
        text = operand.replace(",", "")
        negative = text.startswith("-")
        for char in text.lstrip("-"):
            if char not in DIGIT_KEYS:
                return None
            keys.append(DIGIT_KEYS[char])
        if negative:
            keys.append("Change Sign")
        keys.append(after)
    return keys


def calculator_keys(goal, bundle_hint=""):
    """The compiled press sequence for a Calculator request, or None."""
    if "calculator" not in (goal or "").casefold() and "calculator" not in (bundle_hint or "").casefold():
        return None
    parsed = arithmetic(goal)
    return key_sequence(*parsed) if parsed else None
