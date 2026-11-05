"""``premiumModel``: Mobster is free, and Mobster's model is what you pay for. Off until the owner flips it.

Off, nothing changes. On, the plan gate (plan_gate.py) lets every task through, and approvals, the journal, the device
lock and the secret filter go on as they do on any run: they never read this flag."""

import os
import sys

# The flip, together with ON in desktop/src-tauri/src/premium_model.rs (test_premium_model pins that they agree).
ON = False
DEV_ENV = "MOBSTER_PREMIUM_MODEL"


def enabled(environ=None):
    """``ON``, or ``MOBSTER_PREMIUM_MODEL=1`` in a source checkout. The frozen agent ignores the variable, because there
    it would open the plan gate."""
    if ON:
        return True
    if getattr(sys, "frozen", False):
        return False
    environ = os.environ if environ is None else environ
    return environ.get(DEV_ENV) == "1"
