"""Spend caps and the priced spend a run has observed: integer nanodollars until the public JSON boundary. Model
prices live with their clients (frontier.cost_usd)."""

import math

NANODOLLARS = 1_000_000_000


def usd_to_nanodollars(value):
    """Operator cap to integer nanodollars. None stays None (unenforced)."""
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError("Spend cap must be a positive USD amount")
    return math.ceil(value * NANODOLLARS)


class SpendLedger:
    """Priced spend observed so far. Unpriced calls are counted, never zero-filled.

    A ledger only knows what inference telemetry reported. Unknown usage stays
    unknown: the guard trips on observed priced spend, and the unpriced count is
    always visible next to it so a cap is never mistaken for a billing record.
    """

    def __init__(self):
        self.cost_nanodollars = 0
        self.priced_calls = 0
        self.unpriced_calls = 0

    def add_event(self, event):
        """Fold one ``inference_finished`` event into the totals. Returns True if counted."""
        if not isinstance(event, dict) or event.get("event") != "inference_finished":
            return False
        cost = event.get("cost_nanodollars")
        if type(cost) is int and 0 <= cost <= 9_007_199_254_740_991:
            self.cost_nanodollars += cost
            self.priced_calls += 1
        else:
            self.unpriced_calls += 1
        return True

    def at_cap(self, cap_nanodollars):
        """True once observed priced spend reaches the cap. Exact integer comparison."""
        return self.cost_nanodollars >= cap_nanodollars
