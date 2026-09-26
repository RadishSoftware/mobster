"""Durable, request-bound ledger of persistent side-effect intents.

The agent loop's local ``blocked_pairs`` / ``typed_fields`` sets guard one
observation or one generated string. They are not a durable record of logical
effects already attempted on this run (precision-guard audit, 19 Sep 2026).
This component is that ledger: the agent loop can call it around every
dispatch of a persistent side-effect action.

Policy: persistent side-effect actions (TAP that committed a form, TYPE
append, INCREMENT/DECREMENT) must not be dispatched twice for the same
logical effect on the same run when the prior outcome is acknowledged or
unknown. Ambiguous outcomes never auto-retry. Only a pre-dispatch refusal
(nothing ran) releases the slot for another attempt.

Not global: construct one ledger per run/request. A fresh instance starts
empty and never shares state with another run.
"""

# Operations that can leave a durable effect on the device.
PERSISTENT_OPERATIONS = frozenset({"TAP", "TYPE", "TYPE_SUBMIT", "INCREMENT", "DECREMENT", "SUBMIT"})

# Outcomes that leave the effect applied, or leave it unknown. A second
# dispatch of the same logical effect is a duplicate.
BLOCKING_OUTCOMES = frozenset({
    "intent",  # recorded, outcome not yet known (in-flight counts as unknown)
    "acknowledged",
    "observed_change",
    "observed_unchanged",
    "unknown",
})

# Pre-dispatch refusals: no app code ran and nothing is ambiguous.
RELEASING_OUTCOMES = frozenset({
    "not_dispatched",
    "failed_pre_dispatch",
})

# "unproven" is deliberately in neither set: the action dispatched and AX was
# silent, and no drawn-state baseline existed to prove change or no-op. It
# never blocks -- the next dispatch carries the now-known drawn identity as
# its before, so that attempt's outcome will be provable -- and it is not a
# clean release, because app code may have run.


class EffectLedger:
    """Per-run durable record of persistent logical side effects.

    A logical effect is ``(operation, target_id, text)`` unless the caller
    passes an explicit ``effect=`` key (for example a coarser "create one
    alarm" identity shared by a commit TAP). Swipes, WAIT, DONE and BLOCKED
    are not persistent and are never duplicate-blocked here.
    """

    def __init__(self):
        self._effects = {}

    @staticmethod
    def _key(operation, target_id, text=None, effect=None):
        if effect is not None:
            return effect
        return (operation, target_id, text)

    def record_intent(self, operation, target_id, text=None, *, effect=None):
        """Record that this logical effect is about to be dispatched.

        Calling this never clears a prior acknowledged/unknown outcome.
        """
        if operation not in PERSISTENT_OPERATIONS:
            return
        key = self._key(operation, target_id, text, effect)
        prior = self._effects.get(key)
        blocked = prior is not None and prior["outcome"] in BLOCKING_OUTCOMES
        self._effects[key] = {
            "operation": operation,
            "target_id": target_id,
            "text": text,
            "outcome": prior["outcome"] if blocked else "intent",
            "attempts": (prior["attempts"] if prior else 0) + 1,
        }

    def would_duplicate(self, operation, target_id, text=None, *, effect=None):
        """True when this logical effect was already attempted on this run
        and the prior outcome is acknowledged/unknown (or still in flight).
        """
        if operation not in PERSISTENT_OPERATIONS:
            return False
        record = self._effects.get(self._key(operation, target_id, text, effect))
        return record is not None and record["outcome"] in BLOCKING_OUTCOMES

    def record_outcome(self, operation, target_id, text=None, outcome="acknowledged", *,
                       effect=None):
        """Record the dispatch outcome for a logical effect.

        Only a pre-dispatch failure (``not_dispatched`` /
        ``failed_pre_dispatch``) releases the slot. Anything else -- including
        ``unknown`` -- keeps the block so an ambiguous outcome is never
        auto-retried. The one exception is ``unproven``: the dispatch
        completed but neither AX nor a drawn baseline could observe its
        effect, so a later attempt is treated as a first dispatch whose
        outcome will be provable (see module notes).
        """
        if operation not in PERSISTENT_OPERATIONS:
            return
        if not isinstance(outcome, str) or not outcome:
            raise ValueError("Effect outcome must be a non-empty string")
        key = self._key(operation, target_id, text, effect)
        record = self._effects.get(key)
        if record is None:
            record = {"operation": operation, "target_id": target_id, "text": text,
                      "attempts": 0}
            self._effects[key] = record
        record["outcome"] = outcome
