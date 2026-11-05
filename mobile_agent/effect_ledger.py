"""One run's ledger of side effects: one whose outcome is acknowledged or unknown is never sent again."""

# Operations that can leave a durable effect on the device.
PERSISTENT_OPERATIONS = frozenset({"TAP", "TYPE", "TYPE_SUBMIT", "SET_TEXT", "INCREMENT", "DECREMENT", "SUBMIT"})

# Proven refusals and outcomes settle/resolve reconciled; any other outcome, unfamiliar ones too, blocks a replay.
RELEASING_OUTCOMES = frozenset({
    "not_dispatched",
    "failed_pre_dispatch",
    "observed_absent",  # a fresh look after an unknown outcome showed the action did not happen (settle)
    "resolved",  # the same effect, sent again after that look or with the person's OK, came back answered
})

# A refusal before anything was sent. It releases a slot nothing was sent through, never one an earlier send holds.
PRE_DISPATCH_REFUSALS = frozenset({"not_dispatched", "failed_pre_dispatch"})

# Times a look showing nothing happened can release one unknown outcome; a retry unknown again stays held.
RETRY_LIMIT = 1


class EffectLedger:
    def __init__(self):
        self._effects = {}

    @staticmethod
    def _key(operation, target_id, text=None, effect=None):
        if effect is not None:
            return effect
        return (operation, target_id, text)

    def would_duplicate(self, operation, target_id, text=None, *, effect=None):
        """Whether this effect was already attempted on this run with an acknowledged or unknown outcome."""
        if operation not in PERSISTENT_OPERATIONS:
            return False
        record = self._effects.get(self._key(operation, target_id, text, effect))
        return record is not None and record["outcome"] not in RELEASING_OUTCOMES

    def record_outcome(self, operation, target_id, text=None, outcome="acknowledged", *,
                       effect=None, context=None):
        """Record a dispatch outcome: only a pre-dispatch refusal releases the slot; unknown is never retried."""
        if operation not in PERSISTENT_OPERATIONS:
            return
        if not isinstance(outcome, str) or not outcome:
            raise ValueError("Effect outcome must be a non-empty string")
        key = self._key(operation, target_id, text, effect)
        record = self._effects.get(key)
        if (record is not None and outcome in PRE_DISPATCH_REFUSALS
                and record["outcome"] not in RELEASING_OUTCOMES):
            return  # a later refusal says nothing about the earlier attempt, which may have run
        if record is None:
            record = {"operation": operation, "target_id": target_id, "text": text}
            self._effects[key] = record
        record["outcome"] = outcome
        if context is not None:
            record["context"] = context

    def lookup(self, operation, target_id, text=None, *, effect=None):
        """A copy of this logical effect's record (operation, outcome, context...), or None."""
        record = self._effects.get(self._key(operation, target_id, text, effect))
        return dict(record) if record is not None else None

    def held(self, match):
        """Keys of the effects still held back (a blocking outcome) whose record ``match(key, record)`` accepts."""
        return [key for key, record in self._effects.items()
                if record["outcome"] not in RELEASING_OUTCOMES and match(key, dict(record))]

    def settle(self, operation, target_id, text=None, *, effect=None, happened):
        """Settle an unknown outcome from a look: happened holds it, not happened releases it RETRY_LIMIT times."""
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
        """The same effect was sent again and came back answered: nothing is unknown, and its retries start over."""
        record = self._effects.get(self._key(operation, target_id, text, effect))
        if record is not None and record["outcome"] in ("unknown", "observed_absent"):
            record["outcome"], record["retries"] = "resolved", 0
