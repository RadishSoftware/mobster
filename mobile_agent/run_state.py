"""Explicit per-run state for the observe/decide/act loop.

``Agent.run`` used to close over two dozen mutable locals shared by nested
helpers. Every field below is one of those locals, named and initialized in one
place, so each loop phase can take the state it needs instead of capturing the
whole frame. Construction validates exactly what ``run`` validated.

The one deliberate exception is ``visual_request``: ``Agent._visual_answer``
caches it on first use and tests ``hasattr``, because None is a valid value.
Some helpers still read fields with ``getattr`` defaults since tests drive them
with lightweight stand-in states; the defaults here match those.
"""

import time
from datetime import datetime

from .effect_ledger import EffectLedger
from .errors import Cancelled, SpendCapExceeded
from .extraction import Evidence, focus_terms, validate_schema
from .output_contract import validate_output


class RunDeadline(TimeoutError):
    """The run's own deadline passed. Other TimeoutErrors are one request timing out."""


class RunState:
    """Mutable state for one ``Agent.run`` call. Never shared across runs."""

    def __init__(self, goal, *, execute, expected_text, subgoals, output_schema, output_format,
                 max_seconds, cancelled, spend_ledger, spend_cap_nanos):
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 12000:
            raise ValueError("Goal must be bounded nonempty text")
        if expected_text is not None and (not isinstance(expected_text, str)
                or not expected_text.strip() or len(expected_text) > 12000):
            raise ValueError("Expected text must be bounded nonempty text")
        if subgoals is not None and (not isinstance(subgoals, (list, tuple)) or not subgoals
                or len(subgoals) > 10):
            raise ValueError("Subgoals must be a bounded nonempty list")
        schema = validate_schema(output_schema) if output_schema is not None else None
        if output_format is not None:
            validate_output(output_format, schema)
        goals = list(subgoals or [goal])
        if not goals or any(not isinstance(g, str) or not g.strip() or len(g) > 12000 for g in goals):
            raise ValueError("Subgoals must be nonempty strings")
        if goals[-1] != goal:
            goals.append(goal)

        self.goal = goal
        self.execute = execute
        self.expected_text = expected_text
        self.schema = schema
        self.classify_output = output_format == "auto"
        self.output_intent = None
        self.resolved_output_format = (None if output_format == "auto" else
                                       output_format or ("json" if schema is not None else None))
        self.automatic_output = schema is None and self.resolved_output_format is not None
        self.wants_output = schema is not None or self.automatic_output

        self.evidence = Evidence()
        self.evidence.focus = focus_terms(goal)
        self.started = time.monotonic()
        request_time = datetime.now().astimezone()
        self.temporal_context = {"source": "host_request_clock_not_verified_device_clock",
                                 "request_time": request_time.isoformat(),
                                 "timezone_name": request_time.tzname() or ""}
        self.deadline = self.started + max_seconds
        self.cancelled = cancelled
        self.spend_ledger = spend_ledger
        self.spend_cap_nanos = spend_cap_nanos

        self.history, self.metrics = [], []
        self.hint = ""
        self.goals = goals
        self.goal_index = 0
        self.snapshot = None
        self.blocked_pairs = set()
        self.rejected_actions = set()
        # Durable logical side effects for this run (precision-guard-audit:50).
        # Strengthens blocked_pairs/typed_fields; never a global across runs.
        self.effects = EffectLedger()
        self.in_flight_effect = None
        # Last known drawn-state identity. None means unknown / not configured;
        # missing visual never claims two AX-identical screens differ.
        self.visual_fp = None
        # Speculative answer work started at DONE: (screen fingerprint, future).
        self.prefetch = None
        self.retarget = None
        self.prefetched = None
        # Speculative next decision started while an action's screen settled:
        # (request identity, future). Used only by an identical request.
        self.speculation = None
        self.speculations = 0
        self.current_goal = goal
        # The recovery hint the latest decision saw.
        self.decided_hint = ""
        self.wait_fingerprint, self.unchanged_waits = None, 0
        self.undispatched = 0
        self.extraction_retries = 0
        self.recovered_waits = set()
        self.typed_fields = set()
        self.attempted_actions, self.action_outcome = 0, "none"
        # Hybrid System Two escalation: at most one recovery/plan call for
        # low-confidence / blocked / plateau signals, then Jev again.
        self.escalations_used = 0
        # Compiled loops: the read taken to compile a request that turned out
        # not to be a loop (reused as the first observation), and a finished
        # loop's counts for the result.
        self.loop_fallback_snapshot = None
        self.loop_summary = None
        # The request's web address was opened directly (at most once per run).
        self.url_opened = False

        # Set by Agent.run before the first step (replays, decision memo, compiled route).
        self.trace, self.replay, self.replay_used, self.replay_key = [], [], False, None
        self.memo_pending, self.memo_used, self.memo_key = [], [], None
        self.route, self.route_pending = None, None
        self.latency = None
        # Step loop bookkeeping.
        self.decision_ready = False
        self.previous_outcome = "none"
        self.step_interruptions = 0
        self.continued = False
        self.done_blocked = False
        self.no_wait_fingerprint = None
        self.milestone_rejections = 0
        self.duplicate_refusals = 0
        self.approved_actions = set()
        # The next action goes to the user whatever needs_approval says: the model or its
        # checker was unsure and ask before acting lets the user decide instead.
        self.force_approval = False
        self.recoveries = 0
        # Answer paths: page-read probes, deferred collection surveys, the visual judge
        # and answer speculation started before DONE.
        self.page_probe, self.page_probes, self.probed_screens = None, 0, set()
        self.deferred_survey = None
        self.survey_answer = None
        self.visual_attempts, self.visual_answer, self.visual_evidence = 0, None, None
        self.answer_speculation = None

    def budget(self):
        """Remaining call budget. Raises on stop, deadline, or spend cap."""
        if self.cancelled():
            raise Cancelled()
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RunDeadline("Run budget exhausted")
        if self.spend_cap_nanos is not None and self.spend_ledger.at_cap(self.spend_cap_nanos):
            raise SpendCapExceeded("Observed inference spend reached the run's spend cap")
        return min(20, remaining)
