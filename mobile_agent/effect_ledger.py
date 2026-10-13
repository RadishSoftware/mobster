"""Durable, request-bound ledger of persistent side-effect intents.

The agent loop's local ``blocked_pairs`` / ``typed_fields`` sets guard one
observation or one generated string. They are not a durable record of logical
effects already attempted on this run (precision-guard audit, 19 Sep 2026).
This component is that ledger: the agent loop can call it around every
dispatch of a persistent side-effect action.

Policy: persistent side-effect actions (TAP that committed a form, TYPE
append, INCREMENT/DECREMENT) must not be dispatched twice for the same
logical effect on the same run when the prior outcome is acknowledged or
unknown. Ambiguous outcomes never auto-retry. A pre-dispatch refusal
(nothing ran) releases the slot for another attempt. An unknown outcome
stays blocking until a fresh observation settles it (``settle``): the action
showed its effect (it is done, still blocking), or the screen is as it was
before (one more attempt, ``RETRY_LIMIT``). A later dispatch of the same
effect that comes back answered (``resolve``) leaves nothing unknown.

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
    "unproven",  # dispatched without a drawn baseline: its effect is still unknown
})

# Pre-dispatch refusals: no app code ran and nothing is ambiguous.
RELEASING_OUTCOMES = frozenset({
    "not_dispatched",
    "failed_pre_dispatch",
    "observed_absent",  # a fresh look after an unknown outcome showed the action did not happen (settle)
    "resolved",  # the same effect, sent again after that look or with the person's OK, came back answered
})

# Times one unknown outcome can be released by a look showing nothing happened. A retry whose own outcome is unknown
# again stays blocked.
RETRY_LIMIT = 1


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
                       effect=None, context=None):
        """Record the dispatch outcome for a logical effect.

        Only a pre-dispatch failure (``not_dispatched`` /
        ``failed_pre_dispatch``) releases the slot. Anything else -- including
        ``unknown`` -- keeps the block so an ambiguous outcome is never
        auto-retried. ``unproven`` also blocks: without a drawn baseline,
        the dispatched action's effect is still unknown. ``context``: what the
        caller needs to settle an unknown outcome later (the screen before the
        action), kept on the record.
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
        if context is not None:
            record["context"] = context

    def lookup(self, operation, target_id, text=None, *, effect=None):
        """A copy of this logical effect's record (operation, outcome, attempts, context...), or None."""
        record = self._effects.get(self._key(operation, target_id, text, effect))
        return dict(record) if record is not None else None

    def held(self, match):
        """Keys of the effects still held back (a blocking outcome) whose record ``match(key, record)`` accepts: a
        caller whose effect key carries the screen finds the same control's effect from a screen still showing."""
        return [key for key, record in self._effects.items()
                if record["outcome"] in BLOCKING_OUTCOMES and match(key, dict(record))]

    def settle(self, operation, target_id, text=None, *, effect=None, happened):
        """Settle an ``unknown`` outcome from a fresh observation; the outcome after.

        ``happened`` True: the action showed its effect, so it is done
        (``observed_change``, still blocking). False: the screen is as it was
        before the action, so it did not happen (``observed_absent``, released
        for one more attempt, at most ``RETRY_LIMIT`` times for one record).
        Any other record is left as it is: only an unknown outcome is settled.
        """
        record = self._effects.get(self._key(operation, target_id, text, effect))
        if record is None or record["outcome"] != "unknown":
            return record["outcome"] if record is not None else None
        if happened:
            record["outcome"] = "observed_change"
        elif record.get("retries", 0) < RETRY_LIMIT:
            record["outcome"] = "observed_absent"
            record["retries"] = record.get("retries", 0) + 1
        return record["outcome"]

    def resolve(self, operation, target_id, text=None, *, effect=None):
        """The same effect was sent again (after a look that settled it, or with the person's OK) and the send came
        back answered: nothing about it is unknown any more, and its retry allowance starts over."""
        record = self._effects.get(self._key(operation, target_id, text, effect))
        if record is not None and record["outcome"] in ("unknown", "observed_absent"):
            record["outcome"], record["retries"] = "resolved", 0
