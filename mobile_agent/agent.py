"""Bounded observe/decide/act loop. Every mutation is journaled and executed at most once."""

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import copy
import inspect
import hashlib
import json
from dataclasses import asdict, replace
import os
import re
import threading
import time
from urllib.parse import quote_plus, urlsplit

from .state import ACTION_OPERATIONS, HOST_KEY_OPERATIONS, TEXT_OPERATIONS, has_trait, same_screen, validate_bundle_id, validate_input_text
from .config import RunBudgets
from .costs import SpendLedger, usd_to_nanodollars
from .errors import Cancelled, SpendCapExceeded
from .transport import TransportError
from .extraction import Evidence, InsufficientEvidence, closed_answers, request_words, validate_extraction
from .hybrid import ESCALATION_CONFIDENCE_FLOOR, EscalationReason, escalation_reason
from .models import Decision, SelectionUnsure, selectable_schema
from . import loops
from . import replay as compiled
from . import milestones, plans, routes
from .output_contract import validate_automatic_schema
from .run_state import RunDeadline, RunState
from .task_policy import (LOADING_WAIT_THRESHOLD, ActionSupport, OutputIntent, OutputSupport, StopGate,
                          COMPLETION_GOAL_FLOOR, agreement_accepts, agreement_reason, claims_calibrated,
                          asks_for_changes, asked_part, has_stop_condition, requested_by,
                          is_keypad_key, is_navigation_shaped_tap, is_plain_navigation_tap, approval_kind,
                          approval_subject, approval_title, commit_act, MESSAGE_ACTS, STATE_CHANGING_LABEL,
                          COMMIT_CONTROL)
from . import fast_proof
from .drivers import DriverRejection, activation_diagnostics
from .latency_trace import NULL as NULL_TRACE, Trace
from . import decision_memo as memo

# Operations allowed past an UNCLEAR stop gate on an EMPTY observation.
# Uncertainty with no evidence means the model hedged for lack of anything to
# judge: a reversible step gathers exactly the evidence that resolves the
# gate. Uncertainty DESPITE observed content is genuine ambiguity and stays
# terminal, as do state-changing operations (TAP, TYPE, adjustments,
# LAUNCH_APP) and BLOCKED, which gates the recovery path.
_UNCLEAR_STOP_PROCEED = frozenset({"DONE", "WAIT", "BACK", "HOME", "VOLUME_UP",
    "VOLUME_DOWN", "SWIPE_UP", "SWIPE_DOWN", "SWIPE_LEFT", "SWIPE_RIGHT"})

# Doubted DONEs on intermediate milestones turned into "keep going" before the run ends.
MILESTONE_DONE_RETRIES = 2

# Operations that only look (or ask for help): an unclear stop gate does not end a question on them.
# Delivering an answer (DONE) stays gated, and so does any request with a conditional stop.
_READ_ONLY_OPERATIONS = frozenset({"WAIT", "BLOCKED", "SWIPE_UP", "SWIPE_DOWN", "SWIPE_LEFT", "SWIPE_RIGHT"})
_CONDITIONAL_STOP = re.compile(r"\b(if|unless|until|when|whenever|once|as soon as|in case|otherwise|"
                               r"stop|halt|abort|quit|cancel)\b", re.I)

# Statuses where the run did what was asked (verified by expected text or not).
COMPLETED_STATUSES = frozenset({"completed_unverified", "expected_text_visible"})

# Unchanged, non-loading WAITs tolerated after recovery before a run ends as a stall.
WAIT_PLATEAU_LIMIT = 6


# Recovery hints per run. They share the helper budget with text entry and
# answer extraction; measured on a web search, three recovery hints left no
# budget to generate the query and the run ended "needs_text_helper".
MAX_RECOVERIES = 2


def recoveries_left(state):
    return getattr(state, "recoveries", 0) < MAX_RECOVERIES


def answer_digest(data):
    """A stable digest of an answer's data for offline labelling."""
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def action_trail(state):
    """What the run did, compactly: operation, target label and observed outcome."""
    return action_trail_of(state.history)


def action_trail_of(history):
    return [{"operation": item.get("operation"), "target": (item.get("label") or "")[:80],
             "outcome": item.get("outcome")} for item in history if isinstance(item, dict)]


def same_observed_element(before, after, element):
    """Never join ephemeral/recycled IDs across unrelated controls or screens."""
    if before.source != after.source or before.bundle_id != after.bundle_id:
        return None
    if not has_trait(before, "stable_ids") and not element.locator:
        return None
    return next((item for item in after.elements
                 if (item.id, item.locator, item.role, item.label) ==
                    (element.id, element.locator, element.role, element.label)), None)


def observed_outcome(before, after, target):
    """What one action observably did, as recorded in its history item."""
    changed = after.content_fingerprint != before.content_fingerprint
    observed_target = same_observed_element(before, after, target) if target else None
    changes = [{"label": element.label, "role": element.role,
                "before": element.value, "after": observed.value}
               for element in before.elements
               if (observed := same_observed_element(before, after, element)) is not None
               and element.value != observed.value]
    return {"changed": changed,
            "target_value_after": observed_target.value if observed_target else None,
            "outcome": "observed_change" if changed else "observed_unchanged",
            "field_changes": changes[:8], "field_changes_truncated": len(changes) > 8}


def post_action_hint(operation):
    """The hint the next decision sees after an action."""
    return ("The text was typed into the field. If the request needs it submitted (search, "
            "go to an address, send), choose SUBMIT on that field; do not TYPE it again."
            if operation == "TYPE" else "")


_IMMUTABLE = (str, int, float, bool, tuple, type(None))


def fork_evidence(evidence):
    """``copy.deepcopy(evidence)`` for Evidence's flat entries, ~10x cheaper.

    ``Evidence.add`` mutates entry dicts in place and their values are
    immutable, so a copy of each entry dict (shared by ``entries`` and
    ``seen``) is a full copy. Anything else falls back to deepcopy.
    Measured: 0.81 -> 0.09 ms p50, 2.2 -> 0.21 ms p90 (it runs inside the
    settle loop's on_settling callback and at every DONE).
    """
    try:
        clone = copy.copy(evidence)
        remap, entries = {}, []
        for entry in evidence.entries:
            if type(entry) is not dict or any(not isinstance(v, _IMMUTABLE) for v in entry.values()):
                return copy.deepcopy(evidence)
            remap[id(entry)] = fresh = dict(entry)
            entries.append(fresh)
        clone.entries = entries
        clone.seen = {key: remap.get(id(entry)) or dict(entry) for key, entry in evidence.seen.items()}
        clone.keys = dict(evidence.keys)
        clone.visual_sources = [dict(item) if type(item) is dict else copy.deepcopy(item)
                                for item in evidence.visual_sources]
        return clone
    except (AttributeError, TypeError):
        return copy.deepcopy(evidence)


# Screens each app showed first after launch, and when a run ended there, per
# process: an app opens on its root screen or resumes where it was left. The
# first decision for a remembered screen starts while the launched app is
# still being observed; the request-identity key decides whether it is used.
FIRST_SCREEN_APPS = 64
_first_screens = OrderedDict()
_first_screens_lock = threading.Lock()


def remember_first_screen(bundle, kind, snapshot):
    if not bundle or snapshot is None or not snapshot.elements or snapshot.bundle_id not in {"", bundle}:
        return
    with _first_screens_lock:
        entry = _first_screens.pop(bundle, {})
        entry[kind] = snapshot
        _first_screens[bundle] = entry
        while len(_first_screens) > FIRST_SCREEN_APPS:
            _first_screens.popitem(last=False)


def first_screen_candidates(bundle):
    with _first_screens_lock:
        entry = dict(_first_screens.get(bundle) or {})
    seen, out = set(), []
    for kind in ("first", "last"):
        snapshot = entry.get(kind)
        if snapshot is not None and snapshot.fingerprint not in seen:
            seen.add(snapshot.fingerprint)
            out.append(snapshot)
    return out


def answer_identity(snapshot, evidence, trail):
    """Identity of one answer prefetch: the screen and every input selection and verification see."""
    return hashlib.sha256(json.dumps({"screen": snapshot.fingerprint, "evidence": evidence.public(),
                                      "trail": trail}, sort_keys=True, default=str).encode()).hexdigest()


def _run_succeeded(state, status, data_status):
    """A completed run that also extracted its answer when one was asked for."""
    return status in COMPLETED_STATUSES and (not state.wants_output or data_status == "extracted")


def _route_ahead(route):
    """The request's compiled route still has named hops to tap."""
    return route is not None and bool(route.hops) and route.next < len(route.hops)


def _on_last_hop(route, snapshot):
    """The screen is the last one the route names (the route must have hops)."""
    return routes.screen_title(snapshot) == routes.norm(route.hops[-1])


def _safe_detail(error):
    """Our own validation/transport messages ("Citation does not match observed evidence");
    anything else is reduced to its type so no model output or payload reaches a log."""
    if type(error) in (ValueError, TransportError, SelectionUnsure, InsufficientEvidence, TimeoutError):
        return str(error)[:120]
    return type(error).__name__


def milestone_goal(state, index):
    """The goal text a decision sees for milestone ``index``."""
    goal = state.goals[index]
    return (goal if goal == state.goal else
            f"Original user request: {state.goal}\nCurrent milestone: {goal}\n"
            "Only act within the original user's authorization.")


def decision_key(snapshot, request):
    """Identity of one decision request: the exact screen plus every input the model sees."""
    def plain(value):
        if isinstance(value, (set, frozenset)):
            return sorted(value)
        raise TypeError(type(value).__name__)
    return hashlib.sha256(json.dumps({"screen": snapshot.fingerprint, **request}, sort_keys=True,
                                     default=plain).encode()).hexdigest()


# Speculative decisions started per dispatched action while its screen settles.
MAX_SPECULATIONS_PER_ACTION = 2

# Screens reached from (app, screen, operation, target) before, process-wide.
# When a dispatched action has a recorded successor, the next decision starts at
# dispatch instead of after two agreeing settle reads: 0.8-1.3 s earlier. The
# request-identity key still decides whether that decision is used, so a wrong
# prediction costs one discarded call, never a different action.
# Measured offline on the 2026-09-22 USB iPhone journals: repeated transitions
# reached exactly the recorded screen 79% of the time (eval-inflated).
TRANSITION_MEMORY = 256
_transitions = OrderedDict()
_transitions_lock = threading.Lock()
PREDICTABLE_OPERATIONS = frozenset({"TAP", "SWIPE_UP", "SWIPE_DOWN", "SWIPE_LEFT", "SWIPE_RIGHT", "BACK"})


def transition_key(snapshot, operation, target):
    return (snapshot.bundle_id, snapshot.content_fingerprint, operation,
            (target.locator, target.label, target.role) if target is not None else None)


def predicted_screen(snapshot, operation, target):
    with _transitions_lock:
        return _transitions.get(transition_key(snapshot, operation, target))


def remember_transition(before, operation, target, after):
    key = transition_key(before, operation, target)
    with _transitions_lock:
        _transitions.pop(key, None)
        _transitions[key] = after
        while len(_transitions) > TRANSITION_MEMORY:
            _transitions.popitem(last=False)


# Targetless, reversible operations dispatch on the decision's own observation.
# Measured (latency breakdown, 23 Sep 2026): the pre-dispatch re-read cost 286 ms
# p50 per swipe and protects nothing: a scroll has no target to go stale and
# commits nothing. BLOCKED-gated swipes still re-read (recovery path).
UNGUARDED_OPERATIONS = frozenset({"SWIPE_UP", "SWIPE_DOWN", "SWIPE_LEFT", "SWIPE_RIGHT", "BACK"})

# An observation whose read began this long before it was captured is covered by
# pixels still since before that; WDA source reads take 130-505 ms (p90).
PIXEL_GUARD_READ_BOUND = .6

# A decision that was ready when its observation returned (replay, memo, a
# finished speculation or prediction) dispatches on that observation without
# the pre-dispatch re-read if it is at most this old: the re-read's own
# residual window (its capture happens mid-request; a WDA source read takes
# 130-500 ms) is at least as long, so it would not shorten the unobserved
# interval. A decision that had to wait for the model is always re-read.
FRESH_GUARD_SECONDS = .15

# "Go to <address>" in the request: the address is opened directly (WDA /url)
# instead of focus, keyboard, typing and submit (~3 s on web.type_url).
URL_REQUEST = re.compile(
    r"\b(?:go to|open|visit|navigate to|load|browse to|head to)\s+(?:the\s+)?(?:(?:web\s*)?(?:page|site|url|address)\s+)?"
    r"(?P<url>(?:https?://)?(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}(?::\d{2,5})?(?:/[^\s\"'<>]*)?)",
    re.I)
# Any address-shaped token; a request naming two is ambiguous and types as before.
ADDRESS_TOKEN = re.compile(r"(?:https?://)?(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}(?:/\S*)?", re.I)
SAFARI = "com.apple.mobilesafari"


def requested_url(goal):
    """The one web address the request says to open, verbatim, as an https URL; else None."""
    found = {match.group("url").rstrip(".,;:!?") for match in URL_REQUEST.finditer(goal or "")}
    addresses = {token.rstrip(".,;:!?") for token in ADDRESS_TOKEN.findall(goal or "")}
    if len(found) != 1 or len(addresses) != 1:
        return None
    address = found.pop()
    while address.endswith(")") and address.count("(") < address.count(")"):
        address = address[:-1].rstrip(".,;:!?")
    url = address if re.match(r"https?://", address, re.I) else "https://" + address
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or len(url) > 2000 or "@" in parts.netloc:
        return None
    return url


WEB_SEARCH_REQUEST = re.compile(r"\b(?:search|look up|google)\s+(?:the web|online|the internet|google)?\s*for\s+"
                                r"(?P<query>[^,.;:!?\n]{2,80}?)(?=\s*(?:[,.;:!?]|\band\b|\bthen\b|$))", re.I)


def requested_search(goal):
    """A web search the request asks for ("Search the web for Ada Lovelace, ..."), as a results URL.

    The query is the request's own words, verbatim. Opening the results page is the
    search itself; the typed route (focus, keyboard, text, Return) took ~4 s and on
    MobsterBench pass 3 typed the query twice into Safari's address bar.
    """
    match = WEB_SEARCH_REQUEST.search(goal or "")
    if not match:
        return None
    query = " ".join(match.group("query").split()).strip(" '\"")
    if not query or len(query) > 80:
        return None
    if re.search(r"\bwikipedia\b", goal, re.I):
        # The request goes on to open the Wikipedia article: search Wikipedia itself, as a
        # results list (fulltext=1: no redirect), so opening the article stays the
        # request's own next step; its results are lighter than a web search engine's.
        return "https://en.m.wikipedia.org/w/index.php?fulltext=1&search=" + quote_plus(query)
    return "https://www.google.com/search?q=" + quote_plus(query)


WIKIPEDIA_ARTICLE_REQUEST = re.compile(
    r"\bWikipedia\s+(?:article|page|entry)\s+(?:for|about|on|of)\s+(?:the\s+)?"
    r"(?P<topic>[^,.;:!?\n]{2,60}?)(?=\s*(?:[,.;:!?]|\band\b|\bthen\b|\bto\b|$))", re.I)
_TOPIC_CONNECTORS = {"of", "the", "and", "de", "la", "von", "van", "du", "da", "del", "der", "&"}


def requested_article(goal):
    """The Wikipedia article a request names by title ("the Wikipedia article about iPhone 15 Pro").

    Opened as Wikipedia's go-search, which lands on the article of that title (or its
    redirect) and falls back to results. Only a name opens this way: every word
    capitalised or a number ("iPhone" counts), so "the article about the engineer the
    tower is named after" stays a link to follow. Measured (MobsterBench pass 5,
    multi.model_release): the typed route spent 21 s and ended needing a text helper.
    """
    match = WIKIPEDIA_ARTICLE_REQUEST.search(goal or "")
    if not match:
        return None
    topic = " ".join(match.group("topic").split()).strip(" '\"")
    words = topic.split()
    if not words or any(not (word[0].isupper() or word[0].isdigit() or any(c.isupper() for c in word[1:3])
                             or word.casefold() in _TOPIC_CONNECTORS) for word in words):
        return None
    if words[0].casefold() in _TOPIC_CONNECTORS:
        return None
    return "https://en.m.wikipedia.org/w/index.php?go=Go&search=" + quote_plus(topic)


def was_ineffective(blocked_pairs, ax_fingerprint, visual_fp, operation, target):
    """Same screen + same action already proved ineffective. Visual refines identity only."""
    return any(op == operation and tgt == target
               and same_screen(ax, vis, ax_fingerprint, visual_fp)
               for ax, vis, op, tgt in blocked_pairs)


def was_rejected(rejected_actions, ax_fingerprint, visual_fp, operation, target, text):
    """Same screen + same action already refused by the action verifier."""
    return any(op == operation and tgt == target and prior_text == text
               and same_screen(ax, vis, ax_fingerprint, visual_fp)
               for ax, vis, op, tgt, prior_text in rejected_actions)


LOOP_STATUS = {
    "count_true": "completed_unverified", "count_items": "completed_unverified",
    "end_of_feed": "completed_unverified", "max_seconds": "completed_unverified",
    "dry_run_preview": "completed_unverified",
    "abstains_in_row": "needs_clarification", "uncertain": "needs_clarification",
    "user_stopped": "stopped", "approval_denied": "approval_denied", "approval_timeout": "approval_timeout",
    "guard_failed": "blocked", "duplicate_blocked": "duplicate_effect_blocked",
}
LOOP_REASONS = {
    "count_true": "reached the number of matches you asked for",
    "count_items": "looked at the most items allowed",
    "end_of_feed": "reached the end of the list",
    "max_seconds": "reached its time limit",
    "dry_run_preview": "dry run: judged the current item without acting",
    "abstains_in_row": "couldn't judge several items in a row",
    "uncertain": "couldn't judge an item and was set to stop",
    "user_stopped": "stopped at your answer",
    "approval_denied": "you declined the loop",
    "approval_timeout": "no answer to the approval request",
    "guard_failed": "the app didn't respond as expected",
    "duplicate_blocked": "refused to repeat an action on an item",
}


def loop_status(summary):
    """(run status, user-facing reason) for a finished loop. Counts only, never item content."""
    counts = summary["counts"]
    reason = summary["reason"]
    what = ("judged" if summary.get("dry_run") else "looked at")
    text = (f"Loop {LOOP_REASONS.get(reason, reason.replace('_', ' '))}: {what} {counts['items']} "
            f"item{'s' if counts['items'] != 1 else ''}, {counts['matched']} matched, "
            f"acted on {counts['acted']}, {counts['unsure']} unsure")
    if summary.get("dry_run"):
        text += "; no actions were taken"
    if summary.get("detail") and reason in {"guard_failed", "duplicate_blocked"}:
        text += f" ({summary['detail'][:120]})"
    return LOOP_STATUS.get(reason, "blocked"), text


CONTINUATION_HELPER_CALLS = 2
# One device or model request timing out (not the run deadline) re-observes and
# re-decides from fresh evidence, at most this often per run. Nothing is replayed:
# an action whose outcome is unknown is recorded as such before the next step.
STEP_INTERRUPTION_RETRIES = 2
# "How many ...": answered by listing the counted items (see Agent._counted_answer).
COUNT_REQUEST = re.compile(r"\b(how many|count the|count of|number of|total number)\b", re.I)
COUNT_ITEMS_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "string"},
                                                                  "maxItems": 100}},
                      "required": ["items"], "additionalProperties": False}
# Page-read probes per run, the off-screen web nodes that make a page worth one, and how
# long a scroll waits for a probe already in flight (a Safari swipe costs ~2.5 s).
PAGE_PROBES = 3
PAGE_PROBE_MIN_NODES = 20
PAGE_PROBE_SCROLL_WAIT = 2.0
# Request words as extraction.request_words finds them.
_PROBE_WORD = re.compile(r"[a-z0-9]{4,}")
# A confident DONE on a question survives a split re-check on the same screen (see _handle_done).
DONE_RECHECK_TRUST = .8
STEP_RETRY_MIN_SECONDS = 3
# A judge call that says nothing about the picture (no frame, a model error) may be tried once more.
VISUAL_ANSWER_ATTEMPTS = 2


class ContinueTask(Exception):
    """Raised from answer verification when the run should keep going with a hint."""


# Verbs that only say "send a message" ("send the message", "text Sam"), and words that chain another
# action after it: a request with neither is finished once its approved message shows as sent.
MESSAGE_VERBS = frozenset({"send", "message", "text", "reply", "email", "type", "enter", "write", "tap"})
SEQUENCE = re.compile(r"\b(?:then|after(?:wards| that)?|next|also|before|and (?:call|open|share|post|delete|forward))\b", re.I)
# A redirect ("decline and say what to do instead"): helper calls it may add (the rewrite and new text).
REDIRECT_HELPER_CALLS = 2
REVISE_INSTRUCTIONS = (
    'Return JSON {"request":"the revised task"}. A phone automation task ("request") paused before one of its '
    'steps ("declined_step"); the user declined that step and said what to do instead ("change"). Write the '
    'task as it now stands: the request with the change applied, complete and self-contained, in the '
    "request's own words and style. When the change rewords a message, a title or other text, give the exact "
    'new text in full (for "I am running late" and "say ten minutes late": "I am running ten minutes late"). '
    'Keep every other part of the request; add nothing the user did not ask for.')


class Agent:
    # Answer work starts at the DONE decision, in parallel with the completion
    # re-read (latency breakdown of 23 Sep 2026, item 6). A switch for same-day A/B runs.
    early_answer = True
    # The run's latency trace; a no-op until run() attaches one.
    trace = None
    _trace = NULL_TRACE
    # Guards the speculative-answer table, written from speculation workers.
    _answer_lock = threading.Lock()
    # The server sets this after construction when it stores frames: step events then carry
    # ``_frame`` (JPEG bytes), which it swaps for a frame id before the event is journaled.
    # Never set without such a server: the journal cannot hold bytes.
    capture_frames = False

    def __init__(self, driver, model, helper=None, max_steps=30, max_seconds=120,
                 max_helper_calls=4, settle_seconds=.6, emit=None, cancelled=None, visual=None,
                 max_undispatched=4, max_extraction_retries=1,
                 spend_cap_usd=None, spend_ledger=None, allowed_bundles=None, replay_store=None,
                 approve=None, ask=None, loop_mode="auto", vision_judge=None, loop_store=None,
                 trace=None, decision_memo=None, launch_bundle=None, bypass=False):
        # "Ask before acting": approve(request) blocks until the user answers and
        # returns "approved", "denied", "timeout" or "stopped". None never asks.
        if approve is not None and not callable(approve):
            raise ValueError("Approval handler must be callable")
        self.approve = approve
        # Bypass (the user's explicit opt-in): an action the model is fairly sure of is
        # taken even when a check would stop it. A side-effect tap needs only navigation
        # confidence and an UNCLEAR action check proceeds. A MISMATCH, the duplicate and
        # replay guards, and approvals (when ask before acting is on) still apply.
        self.bypass = bool(bypass)
        # A question channel that works even with "ask before acting" off: a
        # compiled loop on an irreversible feed asks once how to treat items
        # it cannot judge. Defaults to the approval channel.
        if ask is not None and not callable(ask):
            raise ValueError("Question handler must be callable")
        self.ask = ask or approve
        # Compiled loops (loops.py): "auto" routes iteration requests to the
        # loop runner, "dry_run" judges and logs without acting, "off" never.
        if loop_mode not in {"auto", "dry_run", "off"}:
            raise ValueError("Loop mode must be auto, dry_run or off")
        self.loop_mode = loop_mode
        self.vision_judge = vision_judge
        self.loop_store = loop_store
        # Compiled runs: reuse a completed run's actions on byte-identical screens.
        self.replay_store = replay_store
        self.budgets = RunBudgets(max_steps=max_steps, max_seconds=max_seconds,
                                  max_helper_calls=max_helper_calls, settle_seconds=settle_seconds,
                                  max_undispatched=max_undispatched,
                                  max_extraction_retries=max_extraction_retries,
                                  spend_cap_usd=spend_cap_usd)
        if spend_ledger is not None and not isinstance(spend_ledger, SpendLedger):
            raise ValueError("Spend ledger must be a SpendLedger")
        if spend_cap_usd is not None and spend_ledger is None:
            # A cap that observes nothing would enforce nothing. Fail closed.
            raise ValueError("A spend cap needs a ledger observing inference telemetry")
        self.spend_ledger = spend_ledger
        self.spend_cap_nanos = usd_to_nanodollars(spend_cap_usd)
        if allowed_bundles is None:
            self.allowed_bundles = None
        else:
            if (not isinstance(allowed_bundles, (list, tuple, set, frozenset))
                    or not 1 <= len(allowed_bundles) <= 32):
                raise ValueError("Allowed bundles must be 1-32 bundle identifiers")
            try:
                self.allowed_bundles = frozenset(validate_bundle_id(bundle) for bundle in allowed_bundles)
            except ValueError:
                raise ValueError("Allowed bundles must be valid bundle identifiers") from None
        if visual is not None and not callable(visual):
            raise ValueError("Visual reader must be callable")
        # Opt-in supplemental screen reading. Configured by an operator for a
        # public-reading task, never inferred from an empty accessibility tree.
        self.visual = visual
        self.driver, self.model, self.helper = driver, model, helper
        self.max_steps, self.max_seconds = self.budgets.max_steps, self.budgets.max_seconds
        self.max_helper_calls, self.settle_seconds = self.budgets.max_helper_calls, self.budgets.settle_seconds
        # A bounded allowance for refusals that provably dispatched nothing. Without
        # a bound, a screen that never settles would spin until the run deadline.
        self.max_undispatched = self.budgets.max_undispatched
        # A helper that returns a malformed envelope has not established that the
        # answer is absent, only that this response was unusable.
        self.max_extraction_retries = self.budgets.max_extraction_retries
        self.emit = emit or (lambda event: None)
        self.cancelled = cancelled or (lambda: False)
        # Latency trace (latency_trace.Trace). A caller that owns the run's
        # trace (the server, the eval harness) passes it in and emits it; else
        # the agent records its own and emits it with the result.
        self.trace = trace
        self._trace = NULL_TRACE
        # Exact decision memo (decision_memo.py); None disables it.
        self.decision_memo = decision_memo if memo.enabled() else None
        # The app the caller launched for this run: its remembered first screen
        # lets the first decision start while the launch is still being observed.
        self.launch_bundle = launch_bundle
        # Plain-words steps shown in the run view; a plan's sub-agents share the count.
        self._step_count = [0]

    def _span(self, name, **attrs):
        return self._trace.span(name, **attrs)

    def _attach_trace(self):
        trace = self.trace if self.trace is not None else Trace()
        self._trace = trace
        for client in (self.model, self.helper):
            # Only real clients declare the slot; test doubles are left alone.
            if client is not None and hasattr(type(client), "trace"):
                try:
                    client.trace = trace
                except Exception:
                    pass
        return trace

    def run(self, goal, *, execute=False, expected_text=None, subgoals=None, output_schema=None, output_format=None):
        state = RunState(goal, execute=execute, expected_text=expected_text, subgoals=subgoals,
                         output_schema=output_schema, output_format=output_format,
                         max_seconds=self.max_seconds, cancelled=self.cancelled,
                         spend_ledger=self.spend_ledger, spend_cap_nanos=self.spend_cap_nanos)
        # "Answer 'on' or 'off'": the request's own answer set (see extraction.closed_answers).
        state.schema = closed_answers(goal, state.schema)
        observe_ready = getattr(self.driver, "observe_ready", self.driver.observe)
        state.latency = self._attach_trace()
        state.trace, state.replay_used = [], False
        state.replay_key = compiled.request_key(goal, output_schema, output_format, self.allowed_bundles)
        # Auto output needs the first live decision to classify the request, and
        # milestones re-scope the goal per step; both always run live.
        steps = (self.replay_store.load(state.replay_key)
                 if self.replay_store is not None and len(state.goals) == 1 else None)
        if steps and state.classify_output and steps[0].get("output_intent") not in {
                intent.value for intent in OutputIntent if intent is not OutputIntent.UNCLEAR}:
            steps = None
        state.replay = list(steps) if steps else []
        state.memo_pending, state.memo_used, state.memo_key = [], [], None
        state.route, state.route_pending = routes.compile_route(goal) if len(state.goals) == 1 else None, None
        state.bulk_request = loops.bulk_wording(goal)
        try:
            if loops.dating_loop(goal):
                # A request to repeat an action over people in a dating app ends before the phone is read or touched.
                return self._refuse_loop(state, None, "dating_app")
            planned = self._run_plan(state) if self._plan_candidate(state) else None
            if planned is not None:
                return planned
            if not state.replay and not self._loop_candidate(state):
                self._compile_milestones(state)
                self._predict_first_decision(state)
            if self._loop_candidate(state) and not self._survey_after_route(state):
                looped = self._run_loop(state, observe_ready)
                if looped is not None:
                    return looped
            for step in range(self.max_steps):
                try:
                    terminal = self._step(state, observe_ready, step)
                except ContinueTask as hint:
                    state.hint = str(hint)
                    state.done_blocked = True
                    # The remaining steps may need text (a better search) and a second
                    # extraction; the first attempt already spent helper calls on its answer.
                    # This Agent serves one run, so the allowance ends with it.
                    self.max_helper_calls += CONTINUATION_HELPER_CALLS
                    self.emit({"event": "completion_continued", "step": step, "reason": "route_unfinished"})
                    continue
                except (TimeoutError, TransportError) as exc:
                    if not self._retry_interrupted_step(state, step, exc):
                        raise
                    continue
                if terminal is not None:
                    return terminal
            return self._finish(state, "max_steps", state.snapshot)
        except Cancelled:
            return self._finish(state, "stopped", state.snapshot, "Stopped at the next operation boundary")
        except TimeoutError as exc:
            if isinstance(exc, RunDeadline) or state.deadline <= time.monotonic():
                return self._finish(state, "timeout", state.snapshot,
                                    "Run deadline reached; in-flight actions are never replayed")
            self._record_unknown_outcome(state)
            self.emit({"event": "error", "type": "TimeoutError", "native_code": None, "retry": False})
            return self._finish(state, "error", state.snapshot,
                                "A device or model request timed out repeatedly before the run deadline")
        except SpendCapExceeded:
            return self._finish(state, "spend_cap", state.snapshot,
                                "Observed inference spend reached the run's spend cap; no further model calls were dispatched")
        except Exception as exc:
            # No request payload, generated text, credentials, or provider response body in logs.
            native_code = exc.code if isinstance(exc, DriverRejection) else None
            diagnostics = exc.diagnostics if isinstance(exc, DriverRejection) else {}
            self._record_unknown_outcome(state)
            self.emit({"event": "error", "type": type(exc).__name__, "native_code": native_code,
                       "retry": False, **diagnostics,
                       **({"detail": str(exc)[:120]} if isinstance(exc, (TransportError, TimeoutError)) else {})})
            detail = "; an in-flight action may have run" if state.action_outcome == "unknown" else "; no automatic retry"
            category = native_code or type(exc).__name__
            return self._finish(state, "error", state.snapshot, f"{category}{detail}")

    @staticmethod
    def _record_unknown_outcome(state):
        if state.in_flight_effect is not None and state.action_outcome == "unknown":
            state.effects.record_outcome(state.in_flight_effect[0], state.in_flight_effect[1],
                                         state.in_flight_effect[2], outcome="unknown")
            state.in_flight_effect = None

    def _retry_interrupted_step(self, state, step, exc):
        """True when one timed-out request should cost a re-observation, not the run.

        Measured on MobsterBench (24 Sep): a single slow WDA read ended runs as
        'timeout' 8.8 s into a 300 s budget. The run deadline itself never retries.
        """
        if isinstance(exc, RunDeadline) or isinstance(exc, DriverRejection):
            return False
        retries = getattr(state, "step_interruptions", 0)
        if retries >= STEP_INTERRUPTION_RETRIES or state.deadline - time.monotonic() < STEP_RETRY_MIN_SECONDS:
            return False
        state.step_interruptions = retries + 1
        self._flush_step(state)
        self._record_unknown_outcome(state)
        state.loop_fallback_snapshot = None
        state.hint = ("The previous step was interrupted by a device or network timeout. Re-check the current "
                      "screen before acting; an action may or may not have taken effect.")
        # Our own transport/timeout messages ("HTTP 500; request not retried") carry no payload.
        self.emit({"event": "step_interrupted", "step": step, "type": type(exc).__name__,
                   "detail": str(exc)[:120], "phase": getattr(self._trace, "last_span", None),
                   "retry": state.step_interruptions})
        return True

    def _step(self, state, observe_ready, step):
        """One observe/decide/route iteration. A result dict terminates the run; None continues."""
        t = time.monotonic()
        snapshot = getattr(state, "loop_fallback_snapshot", None)
        state.loop_fallback_snapshot = None
        if snapshot is None:
            with self._span("observe.first" if step == 0 else "observe.ready"):
                snapshot = observe_ready(timeout=state.budget())
        state.budget()
        state.evidence.add(snapshot, step)
        observe_ms = (time.monotonic() - t) * 1000
        self.emit({"event": "observation", "step": step, **snapshot.public()})
        state.snapshot = snapshot
        if getattr(state, "bulk_request", False) and loops.in_dating_app(snapshot.bundle_id):
            # Whatever way the run got here (a route, a plan, a loop the compiler never saw): not one tap in bulk.
            return self._refuse_loop(state, snapshot, "dating_app")
        if step == 0 and state.execute and not state.replay:
            opened = self._open_requested_url(state, snapshot, step)
            if opened:
                return None
        # Jev owns the hot path. A helper is used only for required text,
        # explicit output extraction, or recovery after a blocked decision.
        # Operator-supplied milestones remain supported without an LLM gate.
        current_goal = milestone_goal(state, state.goal_index)
        state.current_goal = current_goal
        if step == 0:
            remember_first_screen(self.launch_bundle, "first", snapshot)
        route_choice = self._route_choice(state, snapshot, step)
        # A question over a collection that was not on the start screen runs once its items
        # show; after the route's update, so arriving on the named screen counts at once.
        surveyed = self._deferred_survey(state, snapshot)
        if surveyed is not None:
            return surveyed
        answered = self._page_probe(state, snapshot, step)
        if answered is not None:
            return answered
        answered = self._visual_answer(state, snapshot, step)
        if answered is not None:
            return answered
        if route_choice is not None and not state.classify_output:
            # The request names this screen's next hop: no model call. See routes.py.
            decision, state.speculation = route_choice, None
        else:
            decision = self._replayed_decision(state, snapshot)
        state.memo_key = None
        # True when this step's decision was already available when its
        # observation returned (replay, memo, or a finished speculation).
        state.decision_ready = decision is not None
        if decision is None:
            request = self._decision_request(state, snapshot, current_goal)
            key = self._memo_key(snapshot, request)
            decision = self._memo_decision(state, key)
            state.decision_ready = decision is not None
            if decision is None:
                decision = self._speculated_decision(state, snapshot, request)
                if decision is None:
                    with self._span("decide"):
                        decision = self._decide(snapshot, request, timeout=state.budget())
                if key is not None and memo.memoizable(decision):
                    state.memo_pending.append((key, decision))
            else:
                state.speculation = None
                state.memo_key = key
        else:
            state.speculation = None
            self._trace.mark("decision.replay")
        if route_choice is not None and decision is not route_choice:
            # First step of an auto-output request: Jev classified the request and cleared
            # its stop gate on this screen; the route supplies the action.
            if decision.stop_gate is StopGate.CONTINUE and decision.operation != "BLOCKED":
                decision = replace(route_choice, output_intent=decision.output_intent)
            else:
                state.route.failed = "jev_stop_gate"
        # The hint this decision saw; a DONE decided under a helper's hint is re-checked.
        state.decided_hint = state.hint
        state.budget()
        event = {"event": "decision", "step": step, "goal_index": state.goal_index,
                 "snapshot": snapshot.fingerprint, "source": snapshot.source,
                 "observe_ms": round(observe_ms, 2), **asdict(decision)}
        state.metrics.append(event)
        chosen = next((e for e in snapshot.elements if e.id == decision.target), None) if decision.target else None
        # The chosen element's label, for traces (observation events already carry screen text).
        self.emit({**event, "target_label": chosen.label[:120]} if chosen is not None else event)
        if state.classify_output:
            # The first answer is bound to the original request, not evolving UI or hints.
            # Never ask again: later observations cannot silently change the output contract.
            state.output_intent = decision.output_intent or OutputIntent.UNCLEAR
            state.classify_output = False
            state.resolved_output_format = (None if state.output_intent in {OutputIntent.ACTION_ONLY, OutputIntent.UNCLEAR}
                                            else state.output_intent.value)
            state.automatic_output = state.resolved_output_format is not None
            state.wants_output = state.automatic_output
        stopped = self._stop_if_required(state, decision, snapshot, step)
        if stopped is not None:
            return stopped
        if state.output_intent == OutputIntent.UNCLEAR:
            return self._finish(state, "needs_clarification", snapshot, "Please clarify the requested action or answer")
        if not state.execute:
            return self._finish(state, "preview", snapshot, "No device actions executed")
        state.force_approval = False
        # The message the person approved already shows as sent: that is the task, whatever the model
        # decided (it waited, or said DONE and its goal watcher disagreed, live on sim 4).
        sent = self._approved_message_sent(state, snapshot)
        if sent is not None:
            return sent
        if decision.approvable_target is not None and (self.approve is not None or self.bypass):
            # A tap the side-effect floor gated to WAIT although the model is fairly sure of it
            # (APPROVAL_CONFIDENCE_FLOOR). Waiting cannot change the screen, so WAIT only ran into
            # no_progress. With ask before acting, a commit-labelled control is put to the user
            # (approval_kind) and anything else goes on to the action check, which still refuses a
            # mismatch and puts an unclear effect to the user; bypass takes it.
            self.emit({"event": "demotion_lifted", "step": step, "operation": decision.demoted_from,
                       "target": decision.approvable_target,
                       "by": "approval" if self.approve is not None else "bypass"})
            decision = replace(decision, operation=decision.demoted_from, target=decision.approvable_target,
                               demoted_from=None, approvable_target=None)
        if decision.operation == "DONE":
            return self._handle_done(state, observe_ready, decision, current_goal, step)
        if decision.operation == "WAIT" and decision.blocked_probability < .9:
            return self._handle_wait(state, snapshot, decision, current_goal, step)
        if decision.operation in {"SWIPE_UP", "SWIPE_DOWN"} and getattr(state, "page_probe", None) is not None:
            # A scroll here is ~2.5 s in Safari; a page probe in flight usually lands sooner.
            answered = self._page_probe(state, snapshot, step, wait=PAGE_PROBE_SCROLL_WAIT)
            if answered is not None:
                return answered
        return self._handle_action(state, snapshot, decision, current_goal, step)

    # -- counted answers ----------------------------------------------------------------

    def _count_request(self, state):
        properties = (state.schema or {}).get("properties") if isinstance(state.schema, dict) else None
        return (not state.automatic_output and isinstance(properties, dict) and len(properties) == 1
                and next(iter(properties.values())).get("type") == "string"
                and bool(COUNT_REQUEST.search(state.goal)) and self.helper is not None
                and callable(getattr(self.helper, "extract", None)) and self.helper.calls < self.max_helper_calls)

    def _counted_answer(self, state, snapshot):
        """(data, citations, support) for "how many ...", or None to extract as usual.

        A count is never a literal on screen ("3" incomplete reminders is three
        rows), so literal extraction cannot ground it. The helper lists the
        counted items instead, each a cited literal held to the usual contract,
        and the count is the number of distinct cited observations, computed
        here. The answer verifier cannot judge counts (replayed 24 Sep: it gave a
        right and a wrong count the same p_supported .06), so the guard is
        self-consistency: a second, differently worded listing must cite exactly
        the same rows, all on the screen the run ended on. Otherwise no count.
        """
        name = next(iter(state.schema["properties"]))
        asks = (
            "\nList every item this request counts, one entry per item, each exactly as shown on screen. "
            "Do not count items the request excludes. The number is computed from your list.",
            "\nWhich items on the current screen does this request ask to count? Give each one exactly as "
            "shown, once. Leave out anything the request excludes; the count is taken from your list.")
        observed = state.evidence.public()
        listings = []
        try:
            for ask in asks:
                if self.helper.calls >= self.max_helper_calls + 1:
                    return None
                with self._span("extract.count"):
                    produced = self.helper.extract(observed, state.goal + ask, COUNT_ITEMS_SCHEMA,
                                                   timeout=state.budget())
                listings.append(validate_extraction(produced, COUNT_ITEMS_SCHEMA, observed))
        except (Cancelled, TimeoutError, SpendCapExceeded):
            raise
        except Exception as error:
            self.emit({"event": "count_failed", "reason": type(error).__name__, "detail": _safe_detail(error)})
            return None
        entries = {entry["id"]: entry for entry in observed["entries"]}
        sets = [{citation["evidence_id"] for citation in listing["citations"]} for listing in listings]
        current = all(entries[i]["last_seen_step"] == observed["latest_step"] for i in sets[0])
        agree = sets[0] == sets[1] and bool(sets[0])
        self.emit({"event": "count_signals", "items": len(sets[0]), "agree": agree, "current": current})
        if not (agree and current):
            return None
        return {name: str(len(sets[0]))}, listings[0]["citations"], OutputSupport.SUPPORTED

    # -- dataflow plans -----------------------------------------------------------------

    def _compile_milestones(self, state):
        """A request of several actions runs as ordered milestones (milestones.py); else unchanged."""
        if not (state.execute and len(state.goals) == 1 and self.helper is not None
                and callable(getattr(self.helper, "complete", None)) and os.environ.get("MOBSTER_MILESTONES") != "0"
                and milestones.milestone_candidate(state.goal) and not loops.looks_iterative(state.goal)):
            return
        started = time.monotonic()
        try:
            with self._span("milestones.compile"):
                items = milestones.compile_milestones(self.helper, state.goal, timeout=min(15, state.budget()))
        except (Cancelled, SpendCapExceeded, TimeoutError):
            raise
        except Exception as error:
            self.emit({"event": "milestones_not_compiled", "reason": type(error).__name__})
            return
        if not items:
            state.single_step = True
            self.emit({"event": "milestones_not_compiled", "reason": "single_step"})
            return
        state.goals = items + [state.goal]
        state.goal_index = 0
        self.emit({"event": "milestones_compiled", "count": len(items),
                   "compile_ms": round((time.monotonic() - started) * 1000, 1)})

    def _plan_candidate(self, state):
        fields = list((state.schema or {}).get("properties") or {}) if isinstance(state.schema, dict) else []
        return (state.execute and not state.replay and len(state.goals) == 1 and not state.automatic_output
                and self.helper is not None and callable(getattr(self.helper, "complete", None))
                and os.environ.get("MOBSTER_PLANS") != "0"
                and plans.plan_candidate(state.goal, self.allowed_bundles, fields))

    def _run_plan(self, state):
        """Run a multi-app request as a compiled dataflow plan (plans.py); None runs it step by step."""
        from .catalog import APPS
        names = {app["bundleId"]: app["name"] for app in APPS}
        apps = [{"bundle_id": bundle, "name": names.get(bundle, bundle)} for bundle in sorted(self.allowed_bundles)]
        fields = list(state.schema["properties"])
        started = time.monotonic()
        try:
            with self._span("plan.compile"):
                plan = plans.compile_plan(self.helper, state.goal, apps, fields, timeout=state.budget())
        except (Cancelled, SpendCapExceeded, TimeoutError):
            raise
        except Exception as error:
            self.emit({"event": "plan_not_compiled", "reason": type(error).__name__, "detail": str(error)[:160]})
            return None
        if plan is None:
            self.emit({"event": "plan_not_compiled", "reason": "not_a_plan"})
            return None
        self.emit({"event": "plan_compiled", "steps": len(plan["steps"]),
                   "apps": [step["app"] for step in plan["steps"]],
                   "compile_ms": round((time.monotonic() - started) * 1000, 1)})
        values, results = {}, []
        launch = getattr(self.driver, "launch", None)
        for index, step in enumerate(plan["steps"]):
            try:
                request = plans.step_request(step, values, state.goal)
            except plans.PlanError as error:
                self.emit({"event": "plan_step_failed", "index": index, "status": "transform"})
                return self._plan_result(state, results, None, f"Step {index + 1}: {error}") if results else None
            self.emit({"event": "plan_step", "index": index, "app": step["app"], "finds": sorted(step["finds"]),
                       "request": request[:240]})
            if callable(launch):
                try:
                    launch(step["app"], timeout=min(20, state.budget()))
                except (Cancelled, SpendCapExceeded):
                    raise
                except Exception as error:
                    self.emit({"event": "plan_launch_failed", "index": index, "reason": type(error).__name__})
            sub = Agent(self.driver, self.model, self.helper, max_steps=self.max_steps,
                        max_seconds=max(1, state.deadline - time.monotonic()),
                        max_helper_calls=(self.helper.calls if self.helper else 0) + self.budgets.max_helper_calls,
                        settle_seconds=self.settle_seconds, emit=self.emit, cancelled=self.cancelled,
                        visual=self.visual, spend_ledger=self.spend_ledger,
                        allowed_bundles=[step["app"]], approve=self.approve, ask=self.ask, loop_mode="off",
                        trace=self.trace, launch_bundle=step["app"], bypass=self.bypass)
            sub.capture_frames, sub._step_count = self.capture_frames, self._step_count
            result = sub.run(f"In {names.get(step['app'], step['app'])}: {request}", execute=True,
                             output_schema=plans.step_schema(step), output_format="json")
            results.append(result)
            data = result.get("data")
            if not isinstance(data, dict) or any(not isinstance(data.get(name), str) or not data[name]
                                                 for name in step["finds"]):
                self.emit({"event": "plan_step_failed", "index": index, "status": result.get("status")})
                return self._plan_result(state, results, None,
                                         f"Step {index + 1} of {len(plan['steps'])} ({names.get(step['app'])}) "
                                         f"ended without its value: {result.get('reason') or result.get('status')}")
            values.update({name: data[name] for name in step["finds"]})
        answer = {field: values[name] for field, name in plan["answer"].items()}
        return self._plan_result(state, results, answer, f"Answered in {len(plan['steps'])} verified steps")

    def _plan_result(self, state, results, answer, reason):
        """One result for the whole plan: the last step's, with the plan's answer and totals."""
        last = dict(results[-1]) if results else {}
        last["actions"] = sum(result.get("actions") or 0 for result in results)
        last["decisions"] = sum(result.get("decisions") or 0 for result in results)
        last["plan_steps"] = len(results)
        last["reason"] = reason
        if answer is not None:
            last["data"] = answer
        elif last.get("status") in COMPLETED_STATUSES:
            last["status"], last["data"] = "completion_not_confirmed", None
        # Every step's proof backs the plan's answer; done needs all of it (fast_proof.outcome).
        last["proof"] = [item for result in results for item in result.get("proof") or ()][:fast_proof.PROOF_LIMIT]
        last["outcome"] = fast_proof.outcome(last.get("status"), last["proof"])
        last["answer"] = fast_proof.answer_sentence(answer, state.resolved_output_format) if answer is not None else None
        last["elapsed_ms"] = round((time.monotonic() - state.started) * 1000, 2)
        self.emit({"event": "result", **{key: value for key, value in last.items()
                                         if key in ("status", "reason", "actions", "decisions", "plan_steps",
                                                    "outcome", "answer", "proof")}})
        return last

    # -- visual answers -------------------------------------------------------------------

    def _visual_answer(self, state, snapshot, step):
        """Answer a question about the picture on screen with the VisionJudge, or None.

        The accessibility tree has no image content, so "what colour are the
        person's eyes in eyes8-06" ended in WAIT/BLOCKED (MobsterBench diag-5,
        visual.eye_answer/eye_abstain). Once the requested picture is open
        (visual_answer.image_on_screen), the judge answers from the request's
        own answer set; the answer is a judgment, recorded with its image hash
        and tier, never a cited literal. An unsure judge is an honest abstention.
        Pure code until the picture is on screen; at most VISUAL_ANSWER_ATTEMPTS
        judge calls per run, and only one that says something about the picture.
        """
        if (self.vision_judge is None or not state.wants_output or state.automatic_output or not state.execute
                or state.goal_index + 1 != len(state.goals)
                or getattr(state, "visual_attempts", 0) >= VISUAL_ANSWER_ATTEMPTS):
            return None
        from . import visual_answer
        if not hasattr(state, "visual_request"):
            state.visual_request = visual_answer.visual_request(state.goal, state.schema)
        request = state.visual_request
        element = visual_answer.image_on_screen(snapshot, request) if request is not None else None
        if element is None:
            return None
        state.visual_attempts = getattr(state, "visual_attempts", 0) + 1
        crop = self._judge_crop()
        try:
            with self._span("visual_answer"):
                outcome = visual_answer.judge_image(
                    self.vision_judge, request, element, self.driver, crop=crop,
                    timeout=min(visual_answer.JUDGE_TIMEOUT, state.budget()))
        except (Cancelled, SpendCapExceeded, TimeoutError):
            raise
        except Exception as error:
            self.emit({"event": "visual_answer_failed", "step": step, "reason": type(error).__name__})
            return None
        state.budget()
        evidence = outcome["evidence"]
        self.emit({"event": "visual_answer", "step": step, "answered": outcome["answer"] is not None,
                   "transient": outcome["transient"], "reason": outcome["reason"],
                   **{key: evidence.get(key) for key in ("image_sha", "tier", "score")}})
        if outcome["transient"]:
            return None  # Nothing was learned about the picture; the step agent carries on.
        state.visual_attempts = VISUAL_ANSWER_ATTEMPTS
        state.visual_evidence = {**evidence, "kind": "visual_judgment", "field": request["field"],
                                 "allowed": list(request["choices"].values()),
                                 "abstention_allowed": request["abstain_allowed"]}
        if outcome["answer"] is None:
            state.visual_answer = (None, [], "insufficient_evidence")
            return self._finish(state, "completion_not_confirmed", snapshot,
                                "The picture does not show this clearly enough to tell; no answer was given "
                                "rather than a guess")
        state.visual_answer = ({request["field"]: outcome["answer"]}, [], "extracted")
        return self._finish(state, "completed_unverified", snapshot,
                            "Answered by looking at the picture on screen (a visual judgment, not quoted text)")

    # -- page-read probe -----------------------------------------------------------------

    def _page_probe(self, state, snapshot, step, wait=0.0):
        """Answer from a page's whole text before scrolling to it, when the verifier agrees.

        WebKit publishes the whole page; its off-screen text is evidence (see
        Evidence._add_offscreen). On a page whose off-screen text mentions the
        request, answer selection and verification start on a side connection
        while navigation carries on. A SUPPORTED answer ends the run on the
        screen it was selected on; anything else is dropped and the run goes on.
        At most PAGE_PROBES per run.
        """
        if not self._answer_prefetchable(state) or os.environ.get("MOBSTER_PAGE_PROBE") == "0":
            return None
        probe = getattr(state, "page_probe", None)
        if probe is not None:
            fingerprint, future, probed = probe
            if not future.done() and wait > 0:
                try:
                    future.result(timeout=min(wait, max(0.0, state.budget())))
                except Exception:
                    pass
            if not future.done():
                return None
            state.page_probe = None
            try:
                _extracted, support, _signals = future.result()
            except (Cancelled, SpendCapExceeded):
                raise
            except Exception as error:
                self.emit({"event": "page_probe_failed", "step": step, "reason": type(error).__name__,
                           "detail": _safe_detail(error)})
                return None
            if support is not OutputSupport.SUPPORTED:
                self.emit({"event": "page_probe_unsupported", "step": step})
                return None
            self.emit({"event": "page_probe_answered", "step": step})
            state.prefetch = (fingerprint, future)
            return self._finish(state, "completed_unverified", probed,
                                "Answered from the page's text, including text below the visible screen; "
                                "verified against it")
        if getattr(state, "page_probes", 0) >= PAGE_PROBES:
            return None
        probed_screens = getattr(state, "probed_screens", set())
        if snapshot.fingerprint in probed_screens:
            return None
        reason = self._probe_reason(state, snapshot)
        if reason is None:
            return None
        started = self._prefetch_answer(state, snapshot, channel="prefetch_page")
        if started is not None:
            state.page_probes = getattr(state, "page_probes", 0) + 1
            state.probed_screens = probed_screens | {snapshot.fingerprint}
            state.page_probe = (started[0], started[1], snapshot)
            self.emit({"event": "page_probe_started", "step": step, "reason": reason,
                       "offscreen": len(getattr(snapshot, "offscreen", ()))})
        return None

    def _probe_reason(self, state, snapshot):
        """Why this screen may already answer the request, or None.

        "page": a web page whose off-screen text mentions the request. "row": the
        row the question asks about ("is Bluetooth on or off?") is on screen and
        the request's named screens are behind it. Measured (24 Sep): with the
        answer visible, Jev chose BLOCKED or its completion re-check split, ending
        state.bluetooth and state.wifi without an answer.
        """
        route = getattr(state, "route", None)
        if (route is not None and route.reveal and route.next >= len(route.hops)
                and any(routes.target_visible(snapshot, name) for name in route.reveal)):
            return "row"
        offscreen = [node for node in getattr(snapshot, "offscreen", ()) if node.web]
        words = request_words(state.goal)
        # Precompiled and short-circuiting: 1500 web nodes, 1.03 -> 0.79 ms with no match
        # (re.findall's pattern-cache lookup per node was the difference); early matches unchanged.
        if len(offscreen) >= PAGE_PROBE_MIN_NODES and any(
                not words.isdisjoint(_PROBE_WORD.findall((node.label + " " + node.value).casefold()))
                for node in offscreen):
            return "page"
        return None

    # -- compiled routes ----------------------------------------------------------------

    @staticmethod
    def _route_arrived(state):
        route = getattr(state, "route", None)
        snapshot = getattr(state, "snapshot", None)
        return (route is not None and bool(route.hops) and route.failed is None and route.next >= len(route.hops)
                and snapshot is not None and _on_last_hop(route, snapshot))

    def _route_choice(self, state, snapshot, step):
        """The request's next named hop on this screen, as a decision; None leaves it to Jev."""
        route = getattr(state, "route", None)
        if route is None or not state.execute or state.replay or os.environ.get("MOBSTER_ROUTES") == "0":
            return None
        pending, state.route_pending = getattr(state, "route_pending", None) or (None, 0), None
        pending, attempts = pending
        if pending is not None and state.attempted_actions == attempts:
            # Refused before dispatch (diag-15, multi.note_link: a stale screen refused the
            # route's search, and the route then gave up on a search it never sent).
            if pending == "TYPE_SUBMIT":
                route.not_sent()
        elif pending in ("TAP", "SWIPE_UP") and state.history and state.history[-1].get("operation") == pending:
            changed = bool(state.history[-1].get("changed"))
            (route.tapped if pending == "TAP" else route.swiped)(changed)
        was_active = route.active
        choice = route.step(snapshot)
        if choice is None:
            if was_active:
                self.emit({"event": "route_" + ("failed" if route.failed else "finished"), "step": step,
                           "reason": route.failed, "hops": len(route.hops), "reached": route.next})
            return None
        operation, target, *text = choice
        state.route_pending = (operation, state.attempted_actions)
        self.emit({"event": "route_decision", "step": step, "operation": operation,
                   "hop": route.next, "hops": len(route.hops)})
        return Decision(operation=operation, target=target.id if target is not None else None, confidence=1.0,
                        goal_probability=0.0, blocked_probability=0.0, model="route", latency_ms=0.0, usage={},
                        stop_gate=StopGate.CONTINUE,
                        risk_tier={"TAP": "navigation", "TYPE_SUBMIT": "type"}.get(operation, "scroll"),
                        side_effect_risk=0.0, text=text[0] if text else None)

    # -- compiled loops -----------------------------------------------------------------

    def _loop_candidate(self, state):
        """Cheap rules only; the helper classifies (and compiles) when they fire."""
        # A dry run is how a loop is previewed: it routes even when nothing may execute.
        return (self.loop_mode != "off" and (state.execute or self.loop_mode == "dry_run") and self.helper is not None
                and len(state.goals) == 1 and not state.replay
                and callable(getattr(self.helper, "complete", None)) and loops.looks_iterative(state.goal))

    def _run_loop(self, state, observe_ready, *, deferred=False):
        """Compile and run a loop program. None hands the request to the step agent unchanged.

        ``deferred``: a survey resumed by ``_deferred_survey`` on the screen that shows its collection.
        """
        with self._span("observe.first"):
            snapshot = observe_ready(timeout=state.budget())
        state.budget()
        state.evidence.add(snapshot, len(state.metrics) if deferred else 0)
        state.snapshot = snapshot
        # The step agent reuses this read as its first observation if the request is not a loop.
        state.loop_fallback_snapshot = snapshot
        store = self.loop_store
        key = loops.program_key(snapshot.bundle_id, state.goal)
        tools = loops.jev_tools(self.model)
        compiler = loops.HelperCompiler(self.helper)
        started = time.monotonic()
        program = store.load_program(key, state.goal) if store is not None and not deferred else None
        resumed = program is not None
        if resumed and program["report"] is not None and not loops.match_items(snapshot, program["feed"]["item"]):
            return self._defer_survey(state, program)
        if program is None:
            try:
                with self._span("loop.compile"):
                    program, why = loops.compile_loop(state.goal, snapshot, compiler=compiler, pinner=tools,
                                                      timeout=state.budget())
            except (Cancelled, SpendCapExceeded, TimeoutError):
                raise
            except loops.NoItemsOnScreen as error:
                if error.program["report"] is not None and not deferred:
                    return self._defer_survey(state, error.program)
                self.emit({"event": "loop_not_compiled", "reason": "invalid_program", "detail": str(error)[:200]})
                return None
            except loops.ProgramError as error:
                self.emit({"event": "loop_not_compiled", "reason": "invalid_program", "detail": str(error)[:200]})
                self._defer_uncompiled_survey(state, snapshot, deferred)
                return None
            except Exception as error:
                self.emit({"event": "loop_not_compiled", "reason": type(error).__name__})
                self._defer_uncompiled_survey(state, snapshot, deferred)
                return None
            if program is None:
                if why in loops.REFUSALS:
                    return self._refuse_loop(state, snapshot, why)
                self.emit({"event": "loop_not_compiled", "reason": why})
                if why == "not_a_loop":
                    self._defer_uncompiled_survey(state, snapshot, deferred)
                return None
        if program["report"] is not None and not self._survey_runnable(state, program):
            return None
        route = getattr(state, "route", None)
        if (program["report"] is not None and not deferred and _route_ahead(route)
                and not _on_last_hop(route, snapshot)):
            # The request names the collection's screen ("open On My iPhone > MobsterBench >
            # receipts12. Which of the 12 images ..."): judge nothing before arriving there.
            # Live (24 Sep) the start screen's rows matched the selector and a survey of
            # Files' Browse screen answered "none".
            return self._defer_survey(state, program)
        state.loop_fallback_snapshot = None
        if program["evidence"]["source"] == "vision" and self.vision_judge is None:
            self.emit({"event": "loop_not_compiled", "reason": "vision_unavailable"})
            return self._finish(state, "blocked", snapshot,
                                "This task needs Mobster to look at images, which isn't available here yet. "
                                "Nothing was done")
        if store is not None and not resumed:
            try:
                store.save_program(key, program)
            except OSError:
                pass
        dry_run = self.loop_mode == "dry_run"
        # A dry run keeps its judgments to itself: a later real run must still act on those items.
        # A survey re-reads every item each time it is asked, so it never resumes a ledger.
        ledger = (store.ledger(key) if store is not None and not dry_run and program["report"] is None
                  else loops.LoopLedger())
        self.emit({"event": "loop_compiled", "feed": program["feed"]["kind"],
                   "advance": program["feed"]["advance"]["by"], "source": program["evidence"]["source"],
                   "targets": sorted(program["targets"]), "unsure": program["branches"]["unsure"],
                   "count_true": program["stop"]["count_true"], "count_items": program["stop"]["count_items"],
                   "max_seconds": program["stop"]["max_seconds"], "irreversible": program["policy"]["irreversible"],
                   "resumed": resumed, "already_handled": len(ledger.items), "dry_run": dry_run,
                   "compile_ms": round((time.monotonic() - started) * 1000, 1)})
        # The loop's own, user-visible time limit governs it (bounded by loops.MAX_SECONDS),
        # not the per-task default sized for a handful of screens.
        state.deadline = max(state.deadline, time.monotonic() + program["stop"]["max_seconds"] + 30)

        def timed(channel):
            if channel is None:
                return None

            def call(request):
                waited = time.monotonic()
                try:
                    return channel(request)
                finally:
                    # Time spent waiting for the user never counts against the run deadline.
                    state.deadline += time.monotonic() - waited
            return call
        crop = self._judge_crop() if self.vision_judge is not None else None
        try:
            runner = loops.LoopRunner(
                program, driver=self.driver, request=state.goal, emit=self.emit, budget=state.budget,
                ledger=ledger, effects=state.effects, judge=self.vision_judge, tools=tools, compiler=compiler,
                shadow=lambda screen, hint: self._loop_shadow(state, screen, hint),
                approve=timed(self.approve), ask=timed(self.ask),
                frame_clock=getattr(self.driver, "frame_clock", None), crop=crop, dry_run=dry_run,
                settle_seconds=self.settle_seconds, on_action=lambda item: self._loop_action(state, item))
            with self._span("loop"):
                summary = runner.run(snapshot)
        except loops.LoopRefused as refusal:
            return self._refuse_loop(state, snapshot, refusal.reason)
        state.loop_summary = {key: summary[key] for key in ("reason", "counts", "elapsed_ms", "dry_run")}
        if program["report"] is not None:
            return self._finish_survey(state, program, summary, runner.snapshot or snapshot, runner.survey_screens)
        status, reason = loop_status(summary)
        return self._finish(state, status, runner.snapshot or snapshot, reason)

    def _refuse_loop(self, state, snapshot, reason):
        """End the run before any tap: a loop Mobster will not run (``loops.REFUSALS``), with its plain sentence."""
        self.emit({"event": "loop_not_compiled", "reason": reason})
        return self._finish(state, "blocked", snapshot, loops.REFUSALS[reason])

    def _defer_uncompiled_survey(self, state, snapshot, deferred):
        """Ask again on arrival when a question over a named collection did not compile at the start.

        Asked before the collection is on screen, the helper can call it "not a loop" or
        return an unusable program (visual.files_dogs, visual.album_dogs, 24 Sep).
        """
        route = getattr(state, "route", None)
        if not deferred and state.wants_output and _route_ahead(route) and loops.looks_iterative(state.goal):
            state.deferred_survey = {"item": None, "app": snapshot.bundle_id or "", "min_items": 2}
            self.emit({"event": "loop_deferred", "report": "unknown"})

    def _survey_after_route(self, state):
        """Defer compiling a question over a collection the request's route has yet to reach.

        Compiled on the start screen, the helper cannot see the collection: it called
        surveys "not a loop" or wrote selectors for the wrong rows, and its call held up
        the first tap (diag-14, 24 Sep: 26 s of two compiles before navigating). The
        route's taps are read-only, and the survey is compiled once, on arrival.
        """
        route = getattr(state, "route", None)
        if not (state.wants_output and _route_ahead(route)):
            return False
        state.deferred_survey = {"item": None, "app": "", "min_items": 2}
        self.emit({"event": "loop_deferred", "report": "route"})
        return True

    def _defer_survey(self, state, program):
        """Keep a survey whose collection is not on screen yet; the step agent navigates to it.

        MobsterBench's visual tasks start at the app's root ("open On My iPhone >
        MobsterBench > dogs12. Which of the 12 images ..."), so the collection
        only appears after a few step-agent hops. Returns None (run the step agent).
        """
        if not self._survey_runnable(state, program):
            return None
        state.deferred_survey = {"item": program["feed"]["item"], "app": program["app"],
                                 "min_items": min(2, program["stop"]["count_items"])}
        self.emit({"event": "loop_deferred", "report": program["report"]["kind"]})
        return None

    def _survey_runnable(self, state, program):
        """Whether a survey can answer this request; if not, the step agent takes it (a survey only reads)."""
        if program["evidence"]["source"] == "vision" and self.vision_judge is None:
            reason = "vision_unavailable"
        elif state.schema is not None and loops.survey_field(state.schema, program["report"]["kind"]) is None:
            # "What colour are the eyes in eyes8-06?" compiled as a survey could only abstain.
            reason = "survey_schema_mismatch"
        else:
            return True
        self.emit({"event": "loop_not_compiled", "reason": reason})
        return False

    def _deferred_survey(self, state, snapshot):
        """Run a deferred survey once the step agent's screen shows its collection, else None.

        Called with each step's observation: a selector match is pure code
        (microseconds), so steps that do not reach the collection pay nothing.
        The program is compiled again against this screen (one helper call) so
        its item selector is grounded in what is shown, not in the start screen.
        """
        pending = getattr(state, "deferred_survey", None)
        if pending is None or (pending["app"] and snapshot.bundle_id and snapshot.bundle_id != pending["app"]):
            return None
        route = getattr(state, "route", None)
        if _route_ahead(route) and route.failed is None and not _on_last_hop(route, snapshot):
            return None  # still on the way to the collection the request names
        arrived = (route is not None and bool(route.hops) and route.failed is None
                   and (route.next >= len(route.hops) or _on_last_hop(route, snapshot)))
        # On the screen the request names, compile against it whatever the start screen's
        # selector says (it was written for other rows: visual.album_dogs, 24 Sep).
        if not arrived and (pending.get("item") is None or len(loops.survey_anchors(
                loops.match_items(snapshot, pending["item"]))) < pending["min_items"]):
            return None
        state.deferred_survey = None  # One attempt: a second compile is not a second chance.
        result = self._run_loop(state, lambda timeout=None: snapshot, deferred=True)
        # Not a survey after all: the step agent continues on its own read of this screen.
        state.loop_fallback_snapshot = None
        return result

    def _finish_survey(self, state, program, summary, snapshot, screens):
        """Finish a survey with its answer computed in code (loops.survey_answer), never by a model.

        Names are labels the loop observed, cited from the screens it judged
        them on; a count is the number of judged items. Anything short of every
        item judged is an honest abstention ("insufficient_evidence"), not a guess.
        """
        survey = summary.get("survey") or {}
        state.loop_summary["survey"] = {"kind": survey.get("kind"), "items": [
            {"name": r["name"], "outcome": r["outcome"], "answer": r["answer"]} for r in survey.get("items") or ()]}
        status, reason = loop_status(summary)
        if not state.wants_output or state.schema is None:
            return self._finish(state, status, snapshot, reason)
        for screen in screens:
            state.evidence.add(screen, len(state.metrics))
        answer = loops.survey_answer(summary, program, state.schema, state.goal)
        if answer["abstain"] is not None:
            state.survey_answer = (None, [], "insufficient_evidence")
            self.emit({"event": "survey_abstained", "reason": summary["reason"]})
            return self._finish(state, "stopped" if status == "stopped" else "completion_not_confirmed", snapshot,
                                answer["abstain"])
        observed = {entry["text"]: entry["id"] for entry in state.evidence.entries if entry.get("field") == "label"}
        citations = [{"path": path, "evidence_id": observed[label], "quote": quote}
                     for path, label, quote in answer["cited"] if label in observed]
        state.survey_answer = (answer["data"], citations, "extracted")
        return self._finish(state, "completed_unverified", snapshot, reason)

    def _loop_action(self, state, item):
        """One dispatched loop tap or scroll, in the run's history (control labels only)."""
        if item.get("phase") == "dispatching":
            state.attempted_actions += 1
            state.previous_outcome, state.action_outcome = state.action_outcome, "unknown"
            return
        if item.get("phase") == "not_dispatched":
            state.attempted_actions -= 1
            state.action_outcome = getattr(state, "previous_outcome", "none")
            return
        state.action_outcome = "acknowledged"
        state.done_blocked = False  # an action happened: DONE is available again
        state.history.append({"operation": item.get("operation"), "label": (item.get("label") or "")[:80],
                              "target_role": item.get("role") or "", "outcome": item.get("outcome"),
                              "loop": True})

    def _loop_shadow(self, state, snapshot, hint):
        """The step agent's own pick for the current loop item (shadow phase)."""
        request = self._decision_request(state, snapshot, state.goal, hint=hint)
        decision = self._decide(snapshot, request, timeout=state.budget())
        return decision.operation, decision.target

    # -- direct navigation -----------------------------------------------------------------

    def _open_requested_url(self, state, snapshot, step):
        """Open the address the request names, in Safari, with one WDA call. True when opened.

        Replaces focus, keyboard wait, typing, submit and their verification
        (~3 s measured on web.type_url). The address is verbatim from the
        request and the loaded page is still observed and judged by Jev; any
        failure leaves the ordinary typing path untouched.
        """
        if snapshot.bundle_id != SAFARI or snapshot.source != "wda" or getattr(state, "url_opened", False):
            return False
        # MOBSTER_DIRECT_URL=0 turns this off (typing the address instead).
        if os.environ.get("MOBSTER_DIRECT_URL") == "0":
            return False
        url = requested_url(state.goal) or requested_search(state.goal) or requested_article(state.goal)
        opener = getattr(self.driver, "open_url", None)
        if url is None or not (callable(opener) or callable(getattr(self.driver, "call", None))):
            return False
        state.url_opened = True
        self.emit({"event": "action_started", "step": step, "operation": "OPEN_URL", "target": None,
                   "snapshot": snapshot.fingerprint})
        started = time.monotonic()
        try:
            with self._span("dispatch.OPEN_URL"):
                if callable(opener):
                    opener(url, timeout=state.budget())
                else:
                    # Bound to Safari: a bare /url opens the default browser (Chrome on the
                    # measured phone), which left the Safari session unreadable (0/3 on 23 Sep).
                    self.driver.call("POST", "/url", {"url": url, "bundleId": SAFARI},
                                     timeout=min(5, state.budget()))
        except (Cancelled, SpendCapExceeded, TimeoutError):
            raise
        except Exception as error:
            # Refused or unsupported: nothing is assumed to have happened; type as before.
            self.emit({"event": "action_not_dispatched", "step": step, "operation": "OPEN_URL",
                       "native_code": type(error).__name__, "dispatched": False})
            return False
        state.attempted_actions += 1
        act_ms = (time.monotonic() - started) * 1000
        item = {"operation": "OPEN_URL", "label": "the address in the request", "target_role": "",
                "target_value_before": "", "target_value_after": None, "outcome": "acknowledged",
                "changed": None}
        state.history.append(item)
        state.action_outcome = "acknowledged"
        state.done_blocked = False  # an action happened: DONE is available again
        self.emit({"event": "action_acknowledged", "step": step, "act_ms": round(act_ms, 2)})
        t = time.monotonic()
        native_wait = getattr(self.driver, "wait_for_change", None)
        with self._span("settle.OPEN_URL"):
            after = (native_wait(snapshot, wait_seconds=self.settle_seconds,
                                 timeout=min(self.settle_seconds + 1, state.budget()))
                     if callable(native_wait) and self.settle_seconds > 0 else self.driver.observe(timeout=state.budget()))
        state.budget()
        state.snapshot = after
        state.evidence.add(after, step)
        state.action_outcome = "observed"
        item.update(observed_outcome(snapshot, after, None))
        self.emit({"event": "observation_after_action", "step": step, "changed": item["changed"],
                   "settle_ms": round((time.monotonic() - t) * 1000, 2), "image_unchanged": False,
                   "visual_identity": None})
        return True

    # -- decision memo and first-screen prediction ---------------------------------------

    def _memo_key(self, snapshot, request):
        if self.decision_memo is None:
            return None
        try:
            return memo.memo_key(snapshot, request, getattr(self.model, "model", type(self.model).__name__))
        except (TypeError, ValueError):
            return None

    def _memo_decision(self, state, key):
        """The memoized decision for exactly this request, or None."""
        if key is None:
            return None
        try:
            decision = self.decision_memo.get(key)
        except Exception:
            return None
        if decision is None:
            return None
        state.memo_used.append(key)
        self.emit({"event": "decision_memo_hit", "operation": decision.operation})
        self._trace.mark("memo.hit", operation=decision.operation)
        return decision

    def _memo_forget(self, state):
        """The memoized decision just acted on proved wrong for this screen: never reuse it."""
        key, state.memo_key = getattr(state, "memo_key", None), None
        if key is not None and self.decision_memo is not None:
            self.decision_memo.forget(key)
            self._trace.mark("memo.forgotten")

    def _predict_first_decision(self, state):
        """Start step 0's decision for each remembered first screen of the launched app.

        Runs while the launched app is observed (observe_ready: one read plus
        the settle proof). Used only by an identical request (same screen,
        same inputs), so a wrong prediction costs a discarded call.
        """
        if not (self.launch_bundle and state.execute
                and getattr(self.model, "speculative_decisions", False) is True
                and callable(getattr(self.model, "side_channel", None))):
            return
        if self.launch_bundle == SAFARI and requested_url(state.goal):
            return  # Step 0 opens the address instead of deciding.
        channels = ("prediction", "speculation")
        pending = {}
        for channel, candidate in zip(channels, first_screen_candidates(self.launch_bundle)):
            evidence = Evidence()
            evidence.add(candidate, 0)
            request = self._decision_request(state, candidate, milestone_goal(state, 0), evidence=evidence)
            key = self._memo_key(candidate, request)
            memoized = self.decision_memo.get(key) if key is not None else None
            if memoized is not None:
                # The memo answers this screen without a call (and starts its answer if DONE).
                if memoized.operation == "DONE":
                    self._speculate_answer(state, candidate, evidence=evidence, trail=[])
                continue
            http, budget = self.model.side_channel(channel), state.budget()
            pending[channel] = (decision_key(candidate, request), self._speculation_pool().submit(
                self._decide_then_prefetch, state, candidate, request, budget, http,
                {"evidence": evidence, "trail": []}))
        if pending:
            state.speculation = pending
            self.emit({"event": "decision_first_screen_predicted", "candidates": len(pending)})
            self._trace.mark("first_screen.predicted", candidates=len(pending))

    def _decision_request(self, state, snapshot, current_goal, *, history=None, hint=None, evidence=None):
        """Every input of one decision except the screen itself and the timeout."""
        evidence = state.evidence if evidence is None else evidence
        return {"goal": current_goal, "history": state.history if history is None else history,
                "can_type": self.driver.can_type, "hint": state.hint if hint is None else hint,
                "original_goal": state.goal, "classify_output": state.classify_output,
                "evidence_history": evidence.decision_context(snapshot),
                "temporal_context": state.temporal_context,
                "host_operations": getattr(self.driver, "host_operations", frozenset()),
                "allowed_bundles": self.allowed_bundles,
                **({"exclude_operations": excluded} if (excluded := self._excluded_operations(state, snapshot))
                   else {})}

    @staticmethod
    def _excluded_operations(state, snapshot):
        """After a route-unfinished continuation, DONE waits for one more action; after an idle
        WAIT on this same still screen, WAIT is not offered again (see _handle_wait)."""
        excluded = []
        if getattr(state, "done_blocked", False):
            excluded.append("DONE")
        if getattr(state, "no_wait_fingerprint", None) == snapshot.content_fingerprint:
            excluded.append("WAIT")
        return tuple(excluded)

    def _decide(self, snapshot, request, *, timeout, **extra):
        request = dict(request)
        goal, history = request.pop("goal"), request.pop("history")
        try:
            return self.model.decide(snapshot, goal, history, timeout=timeout, **request, **extra)
        except TransportError as error:
            # Jev answers an over-long input with HTTP 400 (max_tokens_exceeded); nothing was
            # decided, so the same question with fewer targets and less text is not a replay.
            if not str(error).startswith("HTTP 400") or "tight" not in inspect.signature(self.model.decide).parameters:
                raise
            self.emit({"event": "decision_input_tightened"})
            return self.model.decide(snapshot, goal, history, timeout=timeout, **request, **extra, tight=True)

    def _speculation_supported(self, state):
        return (state.execute and not state.replay and not state.classify_output
                and getattr(self.model, "speculative_decisions", False) is True
                and callable(getattr(self.model, "side_channel", None)))

    def _speculate(self, state, before, after, target, item, operation, step, *, predicted=False):
        """Start the next step's decision for ``after`` while the driver proves it settled.

        The request is built exactly as the next step will build it if the
        screen settles on ``after``; that step uses the result only when its
        own request has the same identity, so a speculation can never answer a
        different question than the sequential loop would have asked.
        """
        pending = getattr(state, "speculation", None) or {}
        if not self._speculation_supported(state):
            return
        if not predicted and (state.speculations >= MAX_SPECULATIONS_PER_ACTION
                              or any(not future.done() for name, (_, future) in pending.items()
                                     if name == "speculation")):
            return
        simulated = {**item, **observed_outcome(before, after, target)}
        evidence = fork_evidence(state.evidence)
        evidence.add(after, step)
        evidence.add(after, step + 1)
        request = self._decision_request(state, after, state.current_goal,
                                         history=[*state.history[:-1], simulated],
                                         hint=post_action_hint(operation), evidence=evidence)
        key = decision_key(after, request)
        if any(existing == key for existing, _ in pending.values()):
            return  # Already deciding exactly this request.
        memo_key = self._memo_key(after, request)
        memoized = self.decision_memo.get(memo_key) if memo_key is not None else None
        if memoized is not None:
            # The memo already answers exactly this request; an answer task can
            # still start its answer now if that decision is DONE.
            if memoized.operation == "DONE":
                self._speculate_answer(state, after, evidence=evidence, trail=action_trail_of(request["history"]))
            return
        channel = "prediction" if predicted else "speculation"
        if not predicted:
            state.speculations += 1
        http, budget = self.model.side_channel(channel), state.budget()
        state.speculation = {**pending, channel: (key, self._speculation_pool().submit(
            self._decide_then_prefetch, state, after, request, budget, http,
            {"evidence": evidence, "trail": action_trail_of(request["history"])}))}
        self.emit({"event": "decision_predicted" if predicted else "decision_speculated", "step": step + 1})

    def _speculation_pool(self):
        """The shared speculation executor, created on first use."""
        if getattr(self, "_speculator", None) is None:
            # One worker per channel: a dispatch-time prediction and a settle-time
            # speculation for a different screen run side by side.
            self._speculator = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mobster-speculate")
        return self._speculator

    def _judge_crop(self):
        """The vision judge's own crop, else the stock element crop (None if unavailable)."""
        crop = getattr(self.vision_judge, "crop", None)
        if not callable(crop):
            try:
                from .vision_judge import crop_for_element as crop
            except ImportError:
                crop = None
        return crop

    def _decide_then_prefetch(self, state, snapshot, request, budget, http, answer_inputs):
        """A speculative decision; if it says DONE on an answer task, start the answer too.

        The answer prefetch is keyed by the screen fingerprint and uses the
        evidence and action trail the run will have on that screen, so it is
        the request the DONE step would send; it is used only if the run
        finishes on that exact screen.
        """
        decision = self._decide(snapshot, request, timeout=budget, http=http)
        if decision.operation == "DONE":
            self._speculate_answer(state, snapshot, **answer_inputs)
        return decision

    def _speculate_answer(self, state, snapshot, *, evidence, trail):
        """Start the answer for ``snapshot`` now, keyed by every input it depends on."""
        if not (self.early_answer and self._answer_prefetchable(state)):
            return
        identity = answer_identity(snapshot, evidence, trail)
        with self._answer_lock:
            if identity in (getattr(state, "answer_speculation", None) or {}):
                return
            state.answer_speculation = {**(getattr(state, "answer_speculation", None) or {}), identity: None}
        try:
            started = self._prefetch_answer(state, snapshot, channel="prefetch_speculative",
                                            evidence=evidence, trail=trail)
        except Exception:
            started = None
        with self._answer_lock:
            if started is None:
                state.answer_speculation.pop(identity, None)
            else:
                state.answer_speculation[identity] = started[1]

    def _speculated_decision(self, state, snapshot, request):
        """The speculative decision for exactly this request, or None to decide live."""
        speculation, state.speculation = getattr(state, "speculation", None), None
        if not speculation:
            return None
        wanted = decision_key(snapshot, request)
        match = next(((name, future) for name, (key, future) in speculation.items() if key == wanted), None)
        if match is None:
            self.emit({"event": "decision_speculation_discarded"})
            self._trace.mark("speculation.miss", channels=len(speculation))
            return None
        channel, future = match
        ready = future.done()
        state.decision_ready = ready
        waited = time.monotonic()
        try:
            with self._span("decide.wait_" + channel):
                decision = future.result(timeout=state.budget())
        except (Cancelled, SpendCapExceeded):
            raise
        except Exception:
            # An unusable speculation is not an answer; the live call decides.
            self._trace.mark("speculation.failed", channel=channel)
            return None
        self._trace.mark("speculation.hit", channel=channel, ready=ready)
        self.emit({"event": "decision_speculation_used", "predicted": channel == "prediction",
                   "waited_ms": round((time.monotonic() - waited) * 1000, 2)})
        return decision

    def _replayed_decision(self, state, snapshot):
        """The recorded decision for this exact screen, or None to decide live.

        Any mismatch -- a different screen, a missing or ambiguous target --
        abandons the rest of the recording for this run.
        """
        if not state.replay:
            return None
        step = state.replay[0]
        target = compiled.resolve(step, snapshot)
        if target is False:
            state.replay = []
            self.emit({"event": "replay_abandoned", "remaining": 0})
            return None
        state.replay.pop(0)
        state.replay_used = True
        # The first live decision classified an Auto request; the recording
        # carries that classification for the identical request.
        intent = OutputIntent(step["output_intent"]) if state.classify_output else None
        return Decision(step["operation"], target, step.get("confidence") or 1.0, 0.0, 0.0, "replay", 0, {},
                        StopGate.CONTINUE, output_intent=intent, risk_tier=step.get("risk_tier"),
                        side_effect_risk=step.get("side_effect_risk"))

    def _handle_done(self, state, observe_ready, decision, current_goal, step):
        # A requested answer has its own terminal literal/schema and semantic
        # acceptance gates. Do not prevent those gates from even seeing an
        # answer because an uncalibrated navigation score is below .9.
        # Action-only tasks and intermediate milestones keep the navigation
        # gate; no answer can stand in for an unperformed action.
        navigation_gate = not state.wants_output or state.goal_index + 1 < len(state.goals)
        if navigation_gate and self._route_arrived(state):
            # The request's last named screen is the one on show (its title, not a model's
            # estimate): nav.files_on_my_iphone arrived and was vetoed at goal .45 (24 Sep).
            self.emit({"event": "completion_by_route", "step": step})
            navigation_gate = False
        if (navigation_gate and decision.goal_probability < COMPLETION_GOAL_FLOOR
                and decision.blocked_probability < .9 and state.goal_index + 1 < len(state.goals)
                and getattr(state, "milestone_rejections", 0) < MILESTONE_DONE_RETRIES):
            # An intermediate milestone is not the request: a doubted DONE there is "not yet",
            # not the end of the run (iOSWorld caltrack-001, 24 Sep: the search sheet showed
            # oatmeal, DONE at goal .41 ended the task before adding it).
            state.milestone_rejections = getattr(state, "milestone_rejections", 0) + 1
            self.emit({"event": "milestone_done_doubted", "step": step, "goal_index": state.goal_index})
            state.hint = ("DONE was not accepted: the current milestone is not visibly complete on this screen. "
                          "Take the next action it needs, or BLOCKED if it cannot be done.")
            return None
        if (navigation_gate and decision.goal_probability < COMPLETION_GOAL_FLOOR
                or decision.blocked_probability >= .9):
            return self._finish(state, "inconsistent_completion", state.snapshot, "DONE and goal watcher disagree")
        # Completion is re-evaluated on a fresh observation. This is model agreement,
        # not an independent task oracle; status and traces preserve that distinction.
        decided = state.snapshot
        # Answer selection and verification start at the DONE decision, in
        # parallel with the completion re-read (and re-ask, if the screen
        # moved). Used only if the run ends on this exact screen.
        if self.early_answer:
            state.prefetch = self._prefetch_answer(state, decided)
        stable = (getattr(self.model, "stable_completion", False) is True
                  and not getattr(state, "decided_hint", "") and decided is not None)
        if stable and self._pixels_still_since(decided):
            # The MJPEG clock shows no pixel change since before the decided
            # read began: that read is the fresh observation (the same rule as
            # the pixel-guarded refresh before a tap). Saves one WDA source
            # round trip (0.35 s in Settings to 1.3 s in Safari, measured).
            fresh = decided
            self.emit({"event": "completion_reread_skipped_pixels_still", "step": step})
            self._trace.mark("completion.skipped_pixels")
        else:
            with self._span("observe.completion"):
                fresh = observe_ready(timeout=state.budget())
        # Same screen: a DONE made without a helper's hint on a screen that a
        # new read proves unchanged is not asked again. Measured on the USB
        # iPhone journals (2026-09-22): in 219 of 219 such checks the re-asked
        # decision agreed, at 0.3-1.1 s each. Any change asks again.
        same_screen = stable and fresh.fingerprint == decided.fingerprint
        if same_screen and fresh is decided and not self._pixels_still_since(decided):
            # observe_ready may hand back the very read the decision used.
            with self._span("observe.completion"):
                fresh = self.driver.observe(timeout=state.budget())
            same_screen = fresh.fingerprint == decided.fingerprint
        state.evidence.add(fresh, step)
        if not self.early_answer:
            state.prefetch = self._prefetch_answer(state, fresh)
        elif fresh.fingerprint != decided.fingerprint:
            # The screen moved: an answer for the old screen can never be used.
            state.prefetch = self._prefetch_answer(state, fresh, channel="prefetch_fresh")
        if same_screen:
            verification = decision
            self.emit({"event": "completion_same_screen", "goal_index": state.goal_index})
        else:
            with self._span("decide.completion"):
                verification = self.model.decide(fresh, current_goal, state.history,
                    can_type=self.driver.can_type, timeout=state.budget(), original_goal=state.goal,
                    classify_output=False, evidence_history=state.evidence.decision_context(fresh),
                    temporal_context=state.temporal_context, allowed_bundles=self.allowed_bundles)
        self.emit({"event": "completion_check", "goal_index": state.goal_index,
                   "same_screen": same_screen, **asdict(verification)})
        state.metrics.append(asdict(verification))
        state.budget()
        stopped = self._stop_if_required(state, verification, fresh, step)
        if stopped is not None:
            return stopped
        answer_gated = (not navigation_gate and decision.operation == "DONE"
                        and decision.goal_probability >= DONE_RECHECK_TRUST
                        and verification.blocked_probability < .9
                        and fresh.bundle_id == getattr(decided, "bundle_id", None)
                        and routes.screen_title(fresh) == routes.screen_title(decided))
        if answer_gated and verification.operation == "BLOCKED":  # WAIT means still changing: gate stands
            # A question answered on a screen that only redrew (Settings' Wi-Fi row
            # refreshing its network): the re-check split (DONE .84 then BLOCKED .42,
            # state.wifi, 24 Sep). The answer still has to pass extraction and the
            # answer verifier, which are the gates that matter for a question.
            self.emit({"event": "completion_recheck_overridden", "step": step,
                       "recheck": verification.operation})
        elif (verification.operation != "DONE"
                or navigation_gate and verification.goal_probability < COMPLETION_GOAL_FLOOR
                or verification.blocked_probability >= .9):
            return self._finish(state, "completion_not_confirmed", fresh)
        if not same_screen:
            fresh = self._refresh_unchanged(state, fresh, step)
            if fresh is None:
                return None
        if state.goal_index + 1 < len(state.goals):
            state.goal_index += 1
            state.hint = ""
            return None
        if state.expected_text:
            if state.expected_text.casefold() not in fresh.text.casefold():
                return self._finish(state, "expected_text_missing", fresh)
            return self._finish(state, "expected_text_visible", fresh,
                                "Text assertion passed; it does not prove the entire task")
        return self._finish(state, "completed_unverified", fresh,
                            "Model agreement only; no independent task oracle supplied")

    def _handle_wait(self, state, snapshot, decision, current_goal, step):
        fingerprint = snapshot.content_fingerprint
        state.unchanged_waits = state.unchanged_waits + 1 if fingerprint == state.wait_fingerprint else 1
        state.wait_fingerprint = fingerprint
        loading = decision.screen_has_loading
        if (decision.demoted_from is None and loading is not None and loading < LOADING_WAIT_THRESHOLD
                and (asks_for_changes(state.goal) or len(state.goals) > 1)):
            # WAIT on a still, non-loading screen is "I don't know what to do": on iOSWorld (24 Sep)
            # splitpay-001 waited six times on the payment search it needed. The next decision on
            # this same screen must act, say BLOCKED (recovery) or DONE; every guard still applies.
            state.no_wait_fingerprint = fingerprint
        # Low-confidence demotion (an action gated to WAIT) is a hybrid
        # escalation signal: one recovery hint, then Jev owns the next
        # decision. Never a tap. Intentional WAIT and loading demotion
        # do not spend this escalation.
        helper_navigation_limit = self._helper_navigation_limit(state)
        demoted_low_conf = (decision.demoted_from is not None
                            and decision.confidence < ESCALATION_CONFIDENCE_FLOOR)
        low_conf = escalation_reason(
            demoted_from=decision.demoted_from if demoted_low_conf else None,
            escalations_used=state.escalations_used)
        if (low_conf is EscalationReason.LOW_CONFIDENCE and self.helper
                and self.helper.calls < helper_navigation_limit and recoveries_left(state)):
            self._recovery_hint(state, snapshot, current_goal, reason="low_confidence", escalate=True)
            return None
        if state.unchanged_waits >= 3 and fingerprint not in state.recovered_waits:
            fresh = self._refresh_unchanged(state, snapshot, step)
            if fresh is None:
                return None
            state.recovered_waits.add(fingerprint)
            # WAIT used to bypass recovery entirely: a missing AX value could
            # consume the whole decision budget without ever consulting a helper.
            # A plateau is not success, and we do not infer an answer from actions.
            if self.helper and self.helper.calls < helper_navigation_limit and recoveries_left(state):
                self._recovery_hint(state, fresh, current_goal, reason="unchanged_wait")
            else:
                state.hint = ("Repeated WAIT decisions observed the same unchanged screen. Reassess whether "
                              "there is evidence of an in-progress transition. A missing/blank result is not "
                              "evidence of completion; do not compute or invent an unobserved answer. Use an "
                              "authorized observation/navigation path if available, otherwise BLOCKED.")
            return None
        # An unchanged screen that is not loading, after its recovery was spent,
        # is a stall rather than a wait: measured on a USB iPhone, one such run
        # burned all 30 steps (84 s) re-deciding an identical screen.
        if (state.unchanged_waits >= WAIT_PLATEAU_LIMIT and fingerprint in state.recovered_waits
                and loading is not None and loading < LOADING_WAIT_THRESHOLD):
            return self._finish(state, "no_progress", snapshot,
                                "The screen stayed unchanged with no load in progress and no confident action")
        # Repeated identical observations should not hammer the provider. Keep
        # waiting possible for real asynchronous loads; the existing run budget
        # remains the terminal bound rather than an arbitrary short loading cap.
        with self._span("wait.backoff"):
            time.sleep(min(.15 * 2 ** min(state.unchanged_waits - 1, 4), 2.0, state.budget()))
        return None

    def _helper_navigation_limit(self, state):
        """Helper calls navigation may spend. Reserve extraction only after Auto resolves to a requested answer."""
        return max(0, self.max_helper_calls - int(state.wants_output))

    def _recovery_hint(self, state, snapshot, current_goal, *, reason=None, escalate=False):
        """Ask the helper for one recovery hint and log it. Callers own the guards."""
        state.recoveries = getattr(state, "recoveries", 0) + 1
        with self._span("helper.recovery"):
            state.hint = self.helper.ask("recovery", snapshot, current_goal, state.history, timeout=state.budget())
        state.budget()
        if not isinstance(state.hint, str) or not state.hint.strip() or len(state.hint) > 1000:
            raise ValueError("Helper returned an invalid recovery hint")
        event = {"event": "helper", "purpose": "recovery"}
        if reason is not None:
            event["reason"] = reason
        event["calls"] = self.helper.calls
        if escalate:
            state.escalations_used += 1
            event["escalations"] = state.escalations_used
        self.emit(event)

    def _handle_action(self, state, snapshot, decision, current_goal, step):
        state.wait_fingerprint, state.unchanged_waits = None, 0
        # A TAP re-reads after its action verification and compares against
        # this decision's observation, which covers the decision and
        # verification windows together; a read here too cost ~270 ms per tap.
        # TYPE keeps it: its text helper must never run on a stale screen.
        guarded = decision.operation == "TAP" and decision.blocked_probability < .9
        # A scroll or Back has no target to go stale and commits nothing.
        unguarded = decision.operation in UNGUARDED_OPERATIONS and decision.blocked_probability < .9
        fresh = snapshot if guarded or unguarded else self._refresh_unchanged(state, snapshot, step)
        if fresh is None:
            return None
        snapshot = fresh
        state.snapshot = fresh
        if decision.operation == "BLOCKED" or decision.blocked_probability >= .9:
            if (self.helper and self.helper.calls < self._helper_navigation_limit(state) and not state.hint
                    and recoveries_left(state)):
                self._recovery_hint(state, snapshot, current_goal)
                return None
            return self._finish(state, "blocked", snapshot, "No supported progress")

        target = next((e for e in snapshot.elements if e.id == decision.target), None)
        if decision.operation not in ACTION_OPERATIONS:
            return self._finish(state, "invalid_action", snapshot)
        if decision.operation in HOST_KEY_OPERATIONS:
            if (not has_trait(snapshot, "host_keys") or decision.target is not None
                    or decision.operation not in getattr(self.driver, "host_operations", ())):
                return self._finish(state, "invalid_action", snapshot)
        elif decision.operation == "LAUNCH_APP":
            bundle = decision.target
            if (self.allowed_bundles is None or bundle not in self.allowed_bundles):
                return self._finish(state, "invalid_action", snapshot)
            if snapshot.bundle_id and bundle == snapshot.bundle_id:
                state.hint = "Already in the requested app; continue the task there instead of relaunching."
                return None
            target = None
        elif (decision.target is not None and target is None) or (
                decision.operation in {"TAP", "TYPE", "TYPE_SUBMIT", "SUBMIT", "INCREMENT", "DECREMENT"} and target is None):
            return self._finish(state, "invalid_target", snapshot)
        elif (has_trait(snapshot, "exact_actions") and (target is None or decision.operation not in target.actions)
                or target is not None and decision.operation != "TYPE" and decision.operation not in target.actions
                or decision.operation in TEXT_OPERATIONS and (not self.driver.can_type or not target.editable)):
            return self._finish(state, "invalid_action", snapshot)
        if was_ineffective(state.blocked_pairs, snapshot.content_fingerprint, state.visual_fp,
                           decision.operation, decision.target):
            return self._finish(state, "no_progress", snapshot, "Refusing to replay an ineffective action")
        # Position paths repeat across screens (Files' first folder cell is Cell[1] in every
        # folder): with the label, opening "eyes8" is not a replay of opening "MobsterBench".
        effect_target = ((f"{target.locator}\u241f{target.label}" if target.locator else target.label)
                         or decision.target) if target else decision.target
        if decision.operation == "SUBMIT":
            # Submitting the same text twice is a duplicate; new text is a new effect.
            effect_target = f"{effect_target}\u241f{target.value}"
        text, field_key = None, None
        if decision.operation in TEXT_OPERATIONS:
            # Text Jev selected verbatim from the request in the decision call
            # needs no helper round trip; the helper generates everything else.
            selected = decision.text if (decision.text is not None and not (
                target.value and decision.text.startswith(target.value))) else None
            if selected is not None:
                text = selected
                self.emit({"event": "text_selected", "step": step})
            elif not self.helper or self.helper.calls >= self._helper_navigation_limit(state):
                return self._finish(state, "needs_text_helper", snapshot)
            else:
                with self._span("helper.text"):
                    text = self.helper.ask("text", snapshot, current_goal, state.history,
                                           target=decision.target, timeout=state.budget())
                state.budget()
            validate_input_text(text)
            # A semantic field identity, not an ephemeral observation index.
            field_key = (snapshot.bundle_id, target.locator, target.label, text)
            if (decision.operation == "TYPE_SUBMIT" and text.strip() == target.value.strip()
                    and "SUBMIT" in target.actions):
                # The field already holds exactly this text; the remaining effect
                # is the submit. Typing it again would duplicate it.
                self.emit({"event": "type_submit_as_submit", "step": step})
                decision = replace(decision, operation="SUBMIT", text=None)
                text, field_key = None, None
                effect_target = f"{effect_target}\u241f{target.value}"
            elif field_key in state.typed_fields or text in target.value:
                # Never type the same text twice. The first refusal re-decides
                # with the reason (measured: after typing a Safari search, Jev
                # chose TYPE again where SUBMIT was needed); a repeat ends the run.
                state.duplicate_refusals = getattr(state, "duplicate_refusals", 0) + 1
                if state.duplicate_refusals > 1:
                    return self._finish(state, "duplicate_text_blocked", snapshot)
                self.emit({"event": "duplicate_text_refused", "step": step})
                state.hint = ("That text is already in the field; it was NOT typed again. If it should be "
                              "submitted (search, go to an address, send), choose SUBMIT on that field. "
                              "Otherwise choose a different action.")
                return None
            # Text generation is another network boundary. Never type into a changed screen.
            # Selected text crossed no boundary since this step's refresh.
            if selected is None:
                fresh = self._refresh_unchanged(state, snapshot, step)
                if fresh is None:
                    return None
        if target is not None and is_keypad_key(decision.operation, target.role, target.label):
            # Each key press is its own input: the same key twice is two presses, not a replay.
            effect_target = f"{effect_target}\u241fpress{len(state.history)}"
        if state.effects.would_duplicate(decision.operation, effect_target, text):
            self.emit({"event": "effect_duplicate_refused", "step": step,
                       "operation": decision.operation, "target": decision.target,
                       "text_present": text is not None})
            return self._finish(state, "duplicate_effect_blocked", snapshot,
                                "Refusing to dispatch a duplicate persistent side effect")
        if decision.operation in {"TAP", "TYPE", "TYPE_SUBMIT", "SUBMIT"}:
            rejected_key = (fresh.content_fingerprint, state.visual_fp, decision.operation,
                            decision.target, text)
            if was_rejected(state.rejected_actions, fresh.content_fingerprint, state.visual_fp,
                            decision.operation, decision.target, text):
                return self._finish(state, "blocked", fresh,
                                    "Refusing a repeated action whose effect does not match the request")
            verifier = getattr(self.model, "verify_action", None)
            skipped = is_plain_navigation_tap(
                decision.operation, target.role, target.label, risk_tier=decision.risk_tier,
                confidence=decision.confidence, side_effect_risk=decision.side_effect_risk)
            keypad = not skipped and is_keypad_key(decision.operation, target.role, target.label)
            if skipped or keypad:
                support = ActionSupport.ALLOWED
            else:
                with self._span("verify.action"):
                    support = (verifier(fresh, state.goal, decision.operation, target, state.history, text=text,
                                        temporal_context=state.temporal_context, timeout=state.budget())
                               if callable(verifier) else ActionSupport.UNCLEAR)
            state.budget()
            support = support if isinstance(support, ActionSupport) else ActionSupport.UNCLEAR
            self.emit({"event": "action_check", "step": step, "operation": decision.operation,
                       "target": decision.target, "snapshot": fresh.fingerprint,
                       "support": support.value,
                       "skipped": "plain_navigation" if skipped else "keypad" if keypad else None})
            # Every verifier result is bound to exactly one observation, including
            # denied results. A changed form discards the proposed action and guard.
            # A plain navigation tap has no verifier result to bind, so only its
            # own target must be unchanged: lazily loading pages (measured on
            # Wikipedia) otherwise invalidated two decisions per task.
            if self._pixels_still_since(fresh):
                # The MJPEG clock shows no pixel change since before this
                # observation's read began: the verified screen is the screen.
                self.emit({"event": "refresh_skipped_pixels_still", "step": step})
                self._trace.mark("refresh.skipped_pixels")
            elif (getattr(state, "decision_ready", False) and fresh.captured_at > 0
                  and time.monotonic() - fresh.captured_at <= FRESH_GUARD_SECONDS):
                # Proven at rest and read moments ago (see FRESH_GUARD_SECONDS).
                self.emit({"event": "refresh_skipped_fresh", "step": step})
                self._trace.mark("refresh.skipped_fresh")
            else:
                fresh = self._refresh_unchanged(state, fresh, step, target=target if skipped else None)
                if fresh is None:
                    return None
            if support is ActionSupport.UNCLEAR and is_navigation_shaped_tap(
                    decision.operation, target.role, target.label, confidence=decision.confidence,
                    side_effect_risk=decision.side_effect_risk):
                # Measured (MobsterBench nav.files_on_my_iphone): "only look; do not open,
                # share or delete anything" made opening a location UNCLEAR, ending the run.
                self.emit({"event": "unclear_navigation_allowed", "step": step})
                support = ActionSupport.ALLOWED
            elif support is ActionSupport.UNCLEAR and requested_by(
                    state.goal, decision.operation, target.role, target.label,
                    side_effect_risk=decision.side_effect_risk):
                # The request names this act ("archive a receipt" -> "Archive"); an UNCLEAR is
                # no evidence of a wrong effect (task_policy.requested_by). MISMATCH still blocks.
                self.emit({"event": "unclear_requested_allowed", "step": step})
                support = ActionSupport.ALLOWED
            if support is ActionSupport.UNCLEAR and self.approve is not None:
                # The user settles what the checker could not: the approval names this exact
                # action, and declining ends the task. Before, an UNCLEAR ended it unasked
                # (walking directions in Maps and Send in Messages, 25 Sep).
                self.emit({"event": "unclear_put_to_user", "step": step})
                support, state.force_approval = ActionSupport.ALLOWED, True
            elif support is ActionSupport.UNCLEAR and self.bypass:
                self.emit({"event": "unclear_bypassed", "step": step})
                support = ActionSupport.ALLOWED
            if support is ActionSupport.UNCLEAR:
                return self._finish(state, "needs_clarification", fresh,
                                    "This action was not dispatched because its exact effect could not be established; earlier actions may have completed")
            if support is ActionSupport.MISMATCH and self.approve is not None and (
                    decision.operation == "TYPE" and getattr(state, "redirect_field", None) == (target.locator, target.label)
                    or getattr(state, "redirected_commit", None) == (decision.operation, target.label)):
                # After a redirect, the user's own change: typing it into the field the redirect emptied,
                # or the declined commit proposed again with it. Live, the check read the thread (the
                # same words already sent) as a reason to refuse either one. Typing without Return
                # commits nothing and the commit is put to the user with its exact text anyway, so the
                # user decides instead of the run ending "couldn't finish" right after they said what
                # to do. Once each per redirect.
                self.emit({"event": "redirect_mismatch_put_to_user", "step": step, "operation": decision.operation})
                if decision.operation == "TYPE":
                    state.redirect_field = None
                else:
                    state.redirected_commit = None
                support, state.force_approval = ActionSupport.ALLOWED, True
            if support is ActionSupport.MISMATCH:
                self._memo_forget(state)
                state.rejected_actions.add(rejected_key)
                state.hint = ("The proposed action was NOT executed: its prospective effect did not match the "
                              "original request. Inspect every current field value, units, identity and timing. "
                              "Correct only authorized values before committing; never create a duplicate to fix "
                              "an earlier attempt. Choose a different supported action or BLOCKED.")
                return None
        state.budget()
        forced, state.force_approval = state.force_approval, False
        kind = subject = None
        if self.approve is not None:
            # A tap on text reads as the control it lands on (WebKit's checkout Button holds a StaticText of its label).
            subject = approval_subject(fresh.elements, target) if decision.operation == "TAP" else target
            kind = approval_kind(decision.operation, subject.label if subject else "", risk_tier=decision.risk_tier,
                                 uncertain=forced, role=subject.role if subject else None)
        if kind is not None:
            asked = self._ask_approval(state, fresh, target, text, decision, step, kind, subject)
            if asked is not True:
                return asked
            fresh = self._refresh_unchanged(state, fresh, step, target=target)
            if fresh is None:
                return None
        retarget, state.retarget = getattr(state, "retarget", None), None
        if retarget is not None:
            # Same element, same frame, re-identified in the fresh observation.
            target = retarget
        return self._dispatch(state, fresh, target, text, field_key, effect_target, decision, step)

    def _flush_step(self, state):
        """Show an acknowledged action whose settle was cut short (a timeout, an error) as a step."""
        pending, state.pending_step = getattr(state, "pending_step", None), None
        if pending is not None:
            step, text, operation, target = pending
            self._emit_step(state, step, text, operation, target, changed=True)

    def _emit_step(self, state, step, text, operation=None, target=None, changed=False, frame=None):
        """One plain-words step for the run view, with the settled screen when frames are on."""
        try:
            if operation is not None:
                fast_proof.track_path(state, step, operation, target, changed)
            self._step_count[0] += 1
            event = {"event": "step", "n": self._step_count[0], "step": step, "text": text}
            if self.capture_frames:
                frame = frame if frame is not None else fast_proof.frame_jpeg(self.driver)
                if frame:
                    event["_frame"] = frame
        except Exception:
            return  # The step list is presentation; it never changes a run.
        self.emit(event)

    def _proof(self, state, status, snapshot, data, citations):
        """What the screen showed for this result (fast_proof); a proof on the final screen is a step."""
        if not state.execute or status not in COMPLETED_STATUSES:
            return []
        final = len(state.metrics)
        if data is not None:
            proof = fast_proof.cited_proof(state, state.evidence.public()["entries"], citations)
        elif not state.wants_output:
            proof = fast_proof.screen_proof(state, state.goal, getattr(state, "typed_texts", ()), snapshot, final)
        else:
            proof = []
        on_final = [item for item in proof if item["step"] >= final]
        frame = (fast_proof.frame_jpeg(self.driver, allow_capture=True)
                 if on_final and self.capture_frames else None)
        for item in proof:
            if item["step"] >= final:
                item["step"] = final
                self._emit_step(state, final, f"{'Read' if data is not None else 'Found'} {item['quote']}",
                                frame=frame)
            else:
                # An earlier screen: the step whose settled frame shows it (the last action before it).
                item["step"] = max((s for s in getattr(state, "step_paths", {}) if s < item["step"]), default=None)
        return proof

    def _approved_message_sent(self, state, snapshot):
        """Finish a one-part message task once the message the person approved shows as sent, or None.

        Live on sim 4 (27 Sep), Jev never proposed DONE after an approved Send: the words sat in the
        thread, the composer was empty, and the run wandered until no_progress, reading "Couldn't
        finish" for a message that went out. Only a single-goal action task counts, and only the
        exact approved text reading back outside its composer (fast_proof.screen_proof).
        """
        approved = getattr(state, "approved_message", None)
        if approved is None or not state.execute or state.wants_output or len(state.goals) != 1:
            return None
        # A request that may go on after the message ("text Sam, then call him") is not finished by it. The
        # message's own words are left out of that test: "I am running late" is no second action.
        rest = asked_part(re.sub(re.escape(" ".join(approved["text"].split())), " ", " ".join(state.goal.split()),
                                 flags=re.I))
        others = {m.group(1).casefold() for m in milestones._ACTION.finditer(rest)} - MESSAGE_VERBS
        if (others or SEQUENCE.search(rest)) and not getattr(state, "single_step", False):
            return None
        proof = fast_proof.screen_proof(state, state.goal, [(approved["locator"], approved["label"], approved["text"])],
                                        snapshot, len(state.metrics))
        if not proof:
            return None
        self.emit({"event": "approved_message_sent"})
        return self._finish(state, "completed_unverified", snapshot, "The message you approved shows as sent")

    @staticmethod
    def _app_name(snapshot):
        """The app's display name for a sentence ("Reminders"), or "" when the catalog does not know it."""
        bundle = getattr(snapshot, "bundle_id", None)
        if not bundle:
            return ""
        try:
            from .catalog import APPS
            return next((app["name"] for app in APPS if app.get("bundleId") == bundle), "")
        except Exception:
            return ""

    def _pixels_still_since(self, snapshot):
        """Whether a healthy FrameClock ("on") saw no change since before ``snapshot``'s read began.

        Stricter than AX equality for everything drawn, blind to AX-only
        changes; the WDA driver still re-reads any snapshot older than
        WDA_FRESH_SECONDS before a coordinate tap.
        """
        clock = getattr(self.driver, "frame_clock", None)
        if clock is None or getattr(self.driver, "frame_clock_mode", "off") != "on":
            return False
        try:
            still = clock.still_for()
        except Exception:
            return False
        return (still is not None and snapshot.captured_at > 0
                and time.monotonic() - still <= snapshot.captured_at - PIXEL_GUARD_READ_BOUND)

    def _ask_approval(self, state, fresh, target, text, decision, step, kind="commit", subject=None):
        """True to dispatch, None to decide again, or the run's terminal result.

        ``kind`` is "commit" (the act puts something out: the card names it and shows the exact
        text) or "unsure" (a step the model or its check could not establish: Continue or Stop).
        A commit's card names ``subject`` (approval_subject): the control a tap on its text lands on.
        An action the user already approved is not asked again when the screen
        moved underneath it and Jev chose the same action; any other change asks.
        A redirect ("redirected:<instruction>") declines this action and continues the run
        with the instruction added to the request. Time spent waiting for the user never
        counts against the run deadline.
        """
        key = (decision.operation, target.label if target else "", text)
        approved = getattr(state, "approved_actions", set())
        if key in approved:
            return True
        # A commit is named by the control the tap lands on (``subject``, from approval_subject).
        named = subject if subject is not None and kind == "commit" else target
        label = (named.label if named else "")[:200]
        act = (commit_act(label, state.goal, named.role if named else None, decision.operation)
               if kind == "commit" else None)
        to = fast_proof.recipient(fresh, named) if act in MESSAGE_ACTS else None
        # A tap on Send types nothing: the words going out are the ones in the composer.
        shown = text if text is not None or act not in MESSAGE_ACTS else fast_proof.composed_text(fresh, named)
        request = {"step": step, "operation": decision.operation, "label": label,
                   "role": named.role if named else "", "text": shown, "app": fresh.bundle_id,
                   "kind": kind, "act": act, "title": approval_title(act, unsure=kind == "unsure", recipient=to),
                   "target": fast_proof.target_rect(target)}
        waited = time.monotonic()
        with self._span("approval.wait"):
            answer = self.approve(request)
        state.deadline += time.monotonic() - waited
        if answer == "approved":
            state.approved_actions = approved | {key}
            if kind == "commit" and act in MESSAGE_ACTS and shown:
                # What the person approved going out, and from which field: once it shows as sent,
                # the run is done (_approved_message_sent).
                field = fast_proof.composer(fresh, named, shown)
                if field is not None:
                    state.approved_message = {"label": field.label, "locator": field.locator, "text": shown, "to": to}
            return True
        if answer == "stopped":
            raise Cancelled()
        if answer == "timeout":
            return self._finish(state, "approval_timeout", fresh,
                                "No answer to the approval request, so the action was not taken")
        if isinstance(answer, str) and answer.startswith("redirected:"):
            instruction = " ".join(answer[len("redirected:"):].split())[:500]
            if instruction:
                return self._redirect(state, fresh, target, text, decision, step, instruction)
        if kind == "unsure":
            return self._finish(state, "stopped", fresh,
                                "You stopped at a step Mobster was unsure of, so it was not taken")
        return self._finish(state, "approval_denied", fresh, "You declined the action, so it was not taken")

    def _redirect(self, state, fresh, target, text, decision, step, instruction):
        """Decline this action and keep going with the user's instruction as the current request.

        Live on sim 4 (26 Sep, runs 73cb2ffc33e1 and fcec1c83390d) a redirect at Send failed twice:
        the composer still held the declined text, so the next TYPE would have appended to it, the
        action check refused it, Jev proposed it again and the run ended blocked. Now the text the
        declined commit would have sent is emptied first (``_clear_declined_text``), and the request
        the model, the text helper and the action check read says the instruction wins.
        """
        # The declined action is not refused for good: if the model proposes it again, the user is
        # asked again (with the text it would send then) and can redirect or decline once more.
        self._memo_forget(state)
        what = f"“{target.label}”" if target is not None and target.label else "that action"
        original = state.goal
        # The user's change is new work: the rewrite and the text it asks for get their own helper calls.
        self.max_helper_calls += REDIRECT_HELPER_CALLS
        state.goal = self._revised_request(state, original, what, instruction) or (
            f"{original}\nThe user changed this request when asked to approve {what}: {instruction}\n"
            "Where the two differ, the user's change wins: do what it says, then finish the task.")
        state.goals = [state.goal if goal == original else goal for goal in state.goals]
        # A redirected run is not the request it started as: never replay or memoize it under that key.
        state.replay_key, state.memo_pending = None, []
        cleared = self._clear_declined_text(state, fresh, target, step)
        state.redirected_commit = (decision.operation, target.label if target is not None else "")
        state.hint = (f"The user declined {what} and said: {instruction}. Nothing was dispatched for the declined "
                      "action. " + (f"The text in “{cleared}” was removed; write there what the user asked for "
                                    "now, then continue." if cleared else
                                    "If a field holds text that no longer matches the request, change it first."))
        state.redirects = getattr(state, "redirects", 0) + 1
        self.emit({"event": "approval_redirected", "step": step, "operation": decision.operation,
                   "redirects": state.redirects, "cleared": bool(cleared)})
        return None

    def _revised_request(self, state, original, what, instruction):
        """The request as it stands after the user's change, as one self-contained task, or None.

        Live, "say ten minutes late" appended to the request made the text helper type exactly
        " ten minutes late" and left Jev unsure the task was done once it was sent. One helper call
        rewrites the request instead ("Send the message I am running ten minutes late in this
        conversation"), so the model, the text helper and the action check all read the change as
        the request. Any failure keeps the appended form.
        """
        helper = self.helper
        if helper is None or not callable(getattr(helper, "complete", None)):
            return None
        try:
            from .models import AUTHORITY_RULES
            from .transport import decode_json
            with self._span("helper.revise"):
                result = helper.complete(
                    [{"role": "system", "content": REVISE_INSTRUCTIONS + " " + AUTHORITY_RULES},
                     {"role": "user", "content": json.dumps({"request": original, "declined_step": what,
                                                             "change": instruction}, ensure_ascii=False)}],
                    300, min(10., state.budget()), "request_revision")
            choice = result["choices"][0]
            if choice.get("finish_reason") not in {None, "stop"}:
                return None
            data = decode_json(choice["message"]["content"])
            revised = " ".join(str(data["request"]).split()) if isinstance(data, dict) and set(data) == {"request"} else ""
        except (Cancelled, SpendCapExceeded, RunDeadline):
            raise
        except Exception:
            return None
        return revised if 3 <= len(revised) <= 2000 else None

    def _clear_declined_text(self, state, fresh, target, step):
        """Empty the field holding text this run typed that the declined commit would have sent (the
        composer beside Send), so the next TYPE writes the user's version instead of appending to the
        old one. Returns the field's label, or None when there was nothing to empty or it could not be."""
        clear = getattr(self.driver, "clear_text", None)
        typed = [(label, " ".join(text.split())) for _locator, label, text in getattr(state, "typed_texts", ())
                 if text and text.strip()]
        if not callable(clear) or not typed:
            return None
        # Only a field this run typed into, still holding what it typed (not a sent bubble with the same words).
        fields = [e for e in fresh.elements if e.editable and e.value.strip() and e.value != (e.placeholder or None)
                  and any(label == e.label and words in " ".join(e.value.split()) for label, words in typed)]
        if not fields:
            return None
        if target is not None:
            fields.sort(key=lambda e: abs(e.center[1] - target.center[1]))
        field = fields[0]
        try:
            with self._span("redirect.clear"):
                clear(field, timeout=max(1., min(10., state.deadline - time.monotonic())))
        except Exception as error:
            self.emit({"event": "redirect_clear_failed", "step": step, "error": type(error).__name__})
            return None
        # The field's old text is gone: typing the new text there is no duplicate of it.
        state.typed_fields = {key for key in state.typed_fields
                              if not (isinstance(key, tuple) and len(key) == 4 and key[1:3] == (field.locator, field.label))}
        state.typed_texts = [item for item in state.typed_texts if item[1] != field.label]
        state.redirect_field = (field.locator, field.label)
        # The action history the model and the action check read says the field was emptied: without it,
        # the earlier TYPE there read as text still in the field, and typing the new version was refused
        # as a mismatch (live, sim 4, 27 Sep).
        state.history.append({"operation": "CLEAR_TEXT", "label": field.label, "target_role": field.role,
                              "target_value_before": field.value, "target_value_after": "",
                              "outcome": "observed", "changed": True,
                              "note": "Emptied because the user declined sending this text and changed the request"})
        self._emit_step(state, step, f"Cleared {fast_proof.step_text('TYPE', field.label).removeprefix('Typed into ')}")
        return field.label or "the field"

    def _dispatch(self, state, fresh, target, text, field_key, effect_target, decision, step):
        before = fresh.fingerprint
        before_content = fresh.content_fingerprint
        effect_key = (decision.operation, effect_target, text)
        state.effects.record_intent(effect_key[0], effect_key[1], effect_key[2])
        state.in_flight_effect = effect_key
        self.emit({"event": "action_started", "step": step, "operation": decision.operation,
                   "target": decision.target, "snapshot": before})
        t = time.monotonic()
        action_timeout = state.budget()
        state.attempted_actions += 1
        previous_outcome = state.action_outcome
        state.action_outcome = "unknown"
        try:
            # LAUNCH_APP carries its bundle in the effect target; element ops carry an element.
            dispatch_target = effect_target if decision.operation == "LAUNCH_APP" else target
            with self._span("dispatch." + decision.operation):
                diagnostics = activation_diagnostics(self.driver.execute(
                    decision.operation, dispatch_target, fresh, text=text, timeout=action_timeout))
        except DriverRejection as rejection:
            if rejection.code not in DriverRejection.PRE_DISPATCH:
                raise
            # The bridge refused this request before its dispatch boundary, so no
            # app code ran and nothing is ambiguous. A settling scroll is the
            # common cause: the revision moves between the pre-dispatch refresh
            # and the bridge's own check. Observing and deciding again is an
            # ordinary loop iteration, not a retry of an unknown outcome.
            state.attempted_actions -= 1
            state.action_outcome = previous_outcome
            state.in_flight_effect = None
            state.effects.record_outcome(effect_key[0], effect_key[1], effect_key[2],
                                         outcome="failed_pre_dispatch")
            state.undispatched += 1
            self.emit({"event": "action_not_dispatched", "step": step,
                       "operation": decision.operation, "target": decision.target,
                       "native_code": rejection.code, "dispatched": False,
                       "undispatched": state.undispatched, **rejection.diagnostics})
            if state.undispatched > self.max_undispatched:
                return self._finish(state, "error", fresh, f"{rejection.code}; no automatic retry")
            state.hint = ("The previous operation was NOT dispatched: the screen changed while it was "
                          "being prepared, so no app code ran. Read the current screen and choose again.")
            return None
        act_ms = (time.monotonic() - t) * 1000
        if decision.operation in TEXT_OPERATIONS:
            state.typed_fields.add(field_key)
            # In order, with the field: proof reads the last, and whether that field still holds it.
            state.typed_texts = [*getattr(state, "typed_texts", ()), (target.locator, target.label, text)]
        # Record execution BEFORE observation, so a failed observe cannot invite replay.
        item = {"operation": decision.operation, "label": target.label if target else "",
                "target_role": target.role if target else "",
                "target_value_before": target.value if target else "",
                "target_value_after": None, "outcome": "acknowledged",
                "changed": None, **diagnostics}
        state.history.append(item)
        state.action_outcome = "acknowledged"
        state.done_blocked = False  # an action happened: DONE is available again
        self.emit({"event": "action_acknowledged", "step": step, "act_ms": round(act_ms, 2), **diagnostics})
        # The step is shown once the screen settles; if the settle never finishes, _flush_step shows it.
        state.pending_step = (step, fast_proof.step_text(decision.operation, target.label if target else ""),
                              decision.operation, target)
        settle_end = min(state.deadline, time.monotonic() + self.settle_seconds)
        t = time.monotonic()
        settle_started = self._trace.now()
        native_wait = getattr(self.driver, "wait_for_change", None)
        state.speculations = 0
        predictable = decision.operation in PREDICTABLE_OPERATIONS
        if predictable and self._speculation_supported(state):
            predicted = predicted_screen(fresh, decision.operation, target)
            if predicted is not None:
                # Seen this exact transition before: decide its successor now,
                # while the screen is still moving.
                self._speculate(state, fresh, predicted, target, item, decision.operation, step, predicted=True)
        settling = {}
        if getattr(self.driver, "reports_settling", False) and self._speculation_supported(state):
            # While the driver proves the new screen settled, decide it speculatively.
            settling["on_settling"] = lambda candidate: self._speculate(
                state, fresh, candidate, target, item, decision.operation, step)
        # The observation still needs transport/main-thread time after its wait.
        # Reserve it inside the run deadline, never extend the caller's budget.
        after = (native_wait(fresh, wait_seconds=self.settle_seconds,
                             timeout=min(self.settle_seconds + 1, state.budget()), **settling)
                 if native_wait and self.settle_seconds > 0
                 else self.driver.observe(timeout=state.budget()))
        state.budget()
        while after.content_fingerprint == before_content and time.monotonic() < settle_end:
            time.sleep(min(.08, state.budget()))
            after = self.driver.observe(timeout=state.budget())
            state.budget()
        self._trace.phase("settle." + decision.operation, settle_started)
        changed = after.content_fingerprint != before_content
        state.snapshot = after
        state.evidence.add(after, step)
        state.action_outcome = "observed"
        item.update(observed_outcome(fresh, after, target))
        state.effects.record_outcome(effect_key[0], effect_key[1], effect_key[2],
                                     outcome=item["outcome"])
        # Suspected no-op on AX identity (apps whose state is drawn, not
        # published). One cost-aware capture of the drawn screen decides
        # whether pixels actually moved. Missing/refused/stale/blank
        # capture fails closed: behavior stays as today. The fingerprint
        # is change-detection identity only -- never an action target
        # and never a prior fact in decision context.
        unproven = False
        if not changed and self.visual:
            had_identity = state.visual_fp is not None
            _added, after_visual = self._read_visual(state, after)
            if after_visual is not None:
                if had_identity and after_visual != state.visual_fp:
                    changed = True
                    item["changed"] = True
                    item["outcome"] = "observed_change"
                    item["visual_changed"] = True
                if not had_identity:
                    # No before identity, so this action's effect is
                    # unknown. Do not record a blocked pair: that would
                    # claim a no-op we never observed and would refuse a
                    # later distinct drawn transition that shares this
                    # after identity (digit append). The next attempt
                    # carries this identity as its before, and a proven
                    # no-op is recorded then. The ledger gets the honest
                    # "unproven": AX was silent and no drawn baseline
                    # existed, so nothing was observed either way. It is
                    # not a committed duplicate (blocking) and not a
                    # proven no-op; the next dispatch of this effect is
                    # a first dispatch whose outcome will be provable.
                    unproven = True
                    state.effects.record_outcome(effect_key[0], effect_key[1], effect_key[2],
                                                 outcome="unproven")
                    state.in_flight_effect = None
                state.visual_fp = after_visual
            else:
                # No proven identity; fall back to AX-only matching.
                state.visual_fp = None
        elif changed:
            # AX moved; the drawn identity of the new screen is unknown.
            state.visual_fp = None
        self.emit({"event": "observation_after_action", "step": step, "changed": changed,
            "settle_ms": round((time.monotonic() - t) * 1000, 2),
            "image_unchanged": bool(after.image_hash and after.image_hash == fresh.image_hash),
            "visual_identity": state.visual_fp})
        if not changed and not unproven:
            self._memo_forget(state)
            # Record with the identity of the screen where this action was
            # ineffective: the after-capture when one proved the drawn
            # state, else the prior/unknown identity (None fails closed).
            state.blocked_pairs.add((before_content, state.visual_fp, decision.operation, decision.target))
        if state.in_flight_effect is not None:
            # Visual refine may have upgraded observed_unchanged to
            # observed_change; keep the ledger on the final outcome.
            state.effects.record_outcome(effect_key[0], effect_key[1], effect_key[2],
                                         outcome=item["outcome"])
            state.in_flight_effect = None
        state.pending_step = None
        self._emit_step(state, step, fast_proof.step_text(decision.operation, target.label if target else ""),
                        decision.operation, target, changed)
        if changed and predictable:
            remember_transition(fresh, decision.operation, target, after)
        if changed and (entry := compiled.record(before_content, decision, target, fresh)) is not None:
            state.trace.append(entry)
        state.hint = post_action_hint(decision.operation)
        return None

    def _read_visual(self, state, snapshot):
        """One bounded, explicitly authorized screen read. Failure adds nothing.

        Returns ``(added_entries, visual_fingerprint_or_None)``. The
        fingerprint is change-detection identity only: never an action
        target, never a prior fact in decision context, never content
        authority for a private field.
        """
        if snapshot is None or not self.visual:
            return 0, None
        try:
            with self._span("observe.visual"):
                observation = self.visual(snapshot, timeout=state.budget())
        except (Cancelled, SpendCapExceeded):
            raise
        except Exception:
            # A refused, stale, blank or unavailable capture is an absence of
            # evidence, never a reason to relax the answer's requirements.
            self.emit({"event": "visual_evidence", "step": len(state.metrics),
                       "entries": 0, "available": False})
            return 0, None
        state.budget()
        added = state.evidence.add_visual(observation.public())
        identity = observation.fingerprint()
        self.emit({"event": "visual_evidence", "step": len(state.metrics), "available": True,
                   "entries": added, "frame_source": observation.frame_source,
                   "image_hash": observation.image_hash, "actionable": False,
                   "visibility_verified": False})
        return added, identity

    def _answer_prefetchable(self, state):
        model = self.model
        return (state.wants_output and state.execute and state.goal_index + 1 == len(state.goals)
                and not state.automatic_output and selectable_schema(state.schema)
                and callable(getattr(model, "select_fields", None))
                and callable(getattr(model, "verify_output", None))
                and callable(getattr(model, "side_channel", None)))

    def _prefetch_answer(self, state, snapshot, channel="prefetch", *, evidence=None, trail=None):
        """Start answer selection and verification for this screen on a side connection.

        Runs while the completion check and its re-read are in flight; used only
        if the run finishes on a byte-identical screen (same fingerprint), so it
        answers exactly what the sequential path would have been asked.
        ``evidence`` and ``trail`` default to the run's current ones; a
        speculative prefetch passes the ones the run will have on ``snapshot``.
        """
        model = self.model
        if not self._answer_prefetchable(state):
            return None
        if channel == "prefetch" and getattr(state, "answer_speculation", None):
            with self._answer_lock:
                ready = state.answer_speculation.get(
                    answer_identity(snapshot, state.evidence, action_trail(state)))
            if ready is not None:
                # Already started when a speculative decision on this exact screen said DONE.
                self._trace.mark("prefetch.speculated")
                return snapshot.fingerprint, ready
        evidence = fork_evidence(state.evidence if evidence is None else evidence)
        observed = evidence.public()
        goal, schema, budget = state.goal, state.schema, state.budget()
        http = model.side_channel() if channel == "prefetch" else model.side_channel(channel)
        trail = action_trail(state) if trail is None else trail

        def work():
            extracted = validate_extraction(model.select_fields(goal, schema, observed, timeout=budget, http=http),
                                            schema, observed)
            support, signals = self._verify_answer(None, extracted, snapshot, evidence=evidence, goal=goal,
                                                   timeout=budget, http=http, actions=trail)
            return extracted, support, signals
        if getattr(self, "_pool", None) is None:
            # Two workers: a prefetch for a screen that moved must not queue
            # behind the one for the screen it replaced.
            self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mobster-prefetch")
        return snapshot.fingerprint, self._pool.submit(work)

    def _verify_answer(self, state, extracted, snapshot, *, evidence=None, goal=None, timeout=None,
                       http=None, actions=None):
        """(support, signals) for one candidate. Signals are numbers only."""
        evidence = evidence if evidence is not None else state.evidence
        goal = goal if goal is not None else state.goal
        kwargs = {"current_screen": snapshot.public(),
                  "actions": actions if actions is not None else action_trail(state)}
        if http is not None:
            kwargs["http"] = http
        compact = evidence.for_verification(extracted["citations"], goal=goal)
        timeout = timeout if timeout is not None else state.budget()
        # A class attribute, so mocks that only define verify_output keep working.
        if callable(getattr(type(self.model), "verify_output_signals", None)):
            return self.model.verify_output_signals(goal, extracted, compact, timeout=timeout, **kwargs)
        return self.model.verify_output(goal, extracted, compact, timeout=timeout, **kwargs), {}

    def _take_prefetch(self, state, snapshot):
        """The prefetched (extracted, support) for this exact screen, or None.

        A prefetched abstention or unusable pick is re-raised so the caller
        treats it exactly like the same outcome from a live call.
        """
        prefetch, state.prefetch = state.prefetch, None
        if prefetch is None or snapshot is None or prefetch[0] != snapshot.fingerprint:
            if prefetch is not None:
                self._trace.mark("prefetch.miss")
            return None
        self._trace.mark("prefetch.hit", ready=prefetch[1].done())
        with self._span("extract.wait_prefetch"):
            return prefetch[1].result(timeout=state.budget())

    def _continue_if_route_unfinished(self, state, signals):
        """Keep going, once, when the answer looks right but the request's route is not done.

        Measured 2026-09-24 (web.search): "search for Ada Lovelace, open her Wikipedia
        article, and report the year she was born" stopped on the results page, which
        already shows 1815; the verifier correctly refused (the article was never
        opened) and the run ended without an answer. The per-claim verifier signals
        say which part failed: a low final-state claim with a supported field claim
        means the remaining steps were skipped, not that the answer is wrong.
        """
        final_state, field = signals.get("claim_final_state"), signals.get("claim_field")
        if (getattr(state, "continued", False) or not isinstance(final_state, float) or final_state >= .5
                or not isinstance(field, float) or field < .5):
            return
        remaining = state.deadline - time.monotonic()
        if (remaining < .3 * self.max_seconds or len(state.metrics) >= self.max_steps - 3
                or (self.helper and self.helper.calls >= self.max_helper_calls - 1)):
            return
        state.continued = True
        raise ContinueTask("Not finished yet: the answer is not accepted because the screen and actions so far "
                           "do not complete everything the request asks for (for example an article or page it "
                           "says to open). Do the remaining steps of the request first, then finish.")

    def _continue_if_answer_elsewhere(self, state):
        """Keep going, once, when nothing on the observed screens answers and the finish was a guess.

        Measured 2026-09-24 (state.low_power, iOS 26): Settings > Battery showed an "Enable
        Low Power Mode" card; the switch itself is one screen deeper (Power Mode). Jev
        finished there at 0.31 confidence and the run ended with no answer. A low-confidence
        DONE with no citable answer is a navigation miss, not an absent fact.
        """
        last = state.metrics[-1] if state.metrics else {}
        if (getattr(state, "continued", False) or last.get("operation") != "DONE"
                or not isinstance(last.get("confidence"), (int, float)) or last["confidence"] >= DONE_RECHECK_TRUST):
            return
        remaining = state.deadline - time.monotonic()
        if remaining < .3 * self.max_seconds or len(state.metrics) >= self.max_steps - 3:
            return
        state.continued = True
        raise ContinueTask("Not finished: nothing on the screens so far answers the request. The value is "
                           "probably one level deeper; open the row on this screen that leads to it, then finish.")

    def _finish(self, state, status, snapshot=None, reason=""):
        self._flush_step(state)
        data, citations, schema_validated = None, [], False
        output_support = None
        resolved_schema = state.schema
        data_status = "not_requested"
        if snapshot is not None:
            state.evidence.add(snapshot, len(state.metrics))
            remember_first_screen(self.launch_bundle, "last", snapshot)
        # A flat string schema is answered by Jev choosing observed literals;
        # the generative helper remains the fallback when that pick is unsure.
        selectable = (not state.automatic_output and selectable_schema(state.schema)
                      and callable(getattr(self.model, "select_fields", None)))
        if state.wants_output:
            if (survey := getattr(state, "survey_answer", None)) is not None:
                # A compiled survey's answer, computed in code from judged items (_finish_survey).
                data, citations, data_status = survey
                schema_validated = data is not None
            elif (visual := getattr(state, "visual_answer", None)) is not None:
                # A judged answer about the picture on screen (_visual_answer): no literal, no citation.
                data, citations, data_status = visual
                schema_validated = data is not None
            elif not state.execute:
                data_status = "preview"
            elif status not in COMPLETED_STATUSES:
                # Do not turn a failed completion check into a schema-valid
                # answer. Raw observed evidence stays available for review.
                data_status = "not_extracted"
            elif not selectable and (not self.helper or not callable(
                    getattr(self.helper, "extract_auto" if state.automatic_output else "extract", None))):
                data_status = "helper_unavailable"
            elif not selectable and self.helper.calls >= self.max_helper_calls:
                data_status = "helper_budget_exhausted"
            elif not state.evidence.entries:
                data_status = "no_observed_evidence"
            elif self._count_request(state) and (counted := self._counted_answer(state, snapshot)) is not None:
                data, citations, output_support = counted
                schema_validated, data_status = True, "extracted"
            else:
                (status, reason, data, citations, output_support, schema_validated, data_status,
                 resolved_schema) = self._extract_answer(state, snapshot, selectable, status, reason)
        if data_status != "extracted":
            resolved_schema = state.schema  # Do not publish a rejected candidate's inferred shape.
            if state.execute and state.wants_output and status in COMPLETED_STATUSES:
                status = "completion_not_confirmed"
                reason = {
                    "helper_unavailable": "The requested answer could not be produced because the helper is unavailable",
                    "helper_budget_exhausted": "The helper budget ended before the requested answer was produced",
                    "no_observed_evidence": "No observed evidence supports the requested answer",
                    "extraction_timeout": "Result extraction timed out; no answer was returned",
                    "insufficient_evidence": "The observed evidence does not contain all requested facts",
                    "extraction_failed": "Result extraction failed validation; no answer was returned",
                }.get(data_status, "The requested answer was not produced")
        result = {"event": "result", "status": status, "reason": reason,
            "actions": len(state.history), "decisions": len(state.metrics),
            "attempted_actions": state.attempted_actions, "last_action_outcome": state.action_outcome,
            "helper_calls": self.helper.calls if self.helper else 0,
            "elapsed_ms": round((time.monotonic() - state.started) * 1000, 2),
            "goal_index": state.goal_index, "subgoals": len(state.goals),
            "model_claimed_complete": status in COMPLETED_STATUSES,
            "independently_verified": False,
            "final_source": snapshot.source if snapshot else None,
            "data": data, "data_status": data_status, "schema_validated": schema_validated,
            "output_schema": resolved_schema,
            "resolved_output_format": state.resolved_output_format,
            "output_support": output_support.value if output_support is not None else None,
            "output_intent": state.output_intent.value if state.output_intent is not None else None,
            "schema_source": "automatic" if state.automatic_output else "custom" if state.schema is not None else None,
            "citations": citations, "evidence": state.evidence.public(),
            "replayed": getattr(state, "replay_used", False)}
        try:
            proof = self._proof(state, status, snapshot, data, citations)
        except Exception:
            proof = []  # Proof is presentation: without it the run reads "check the result", never done.
        ledger = self.spend_ledger
        if ledger is not None and ledger.priced_calls:
            # What the run's model calls cost at published rates, so History can show it.
            result["costUsd"] = round(ledger.cost_nanodollars / 1e9, 6)
        answer = (fast_proof.answer_sentence(data, state.resolved_output_format) if data_status == "extracted"
                  else fast_proof.done_sentence(proof, self._app_name(snapshot), getattr(state, "approved_message", None))
                  if data is None else None)
        result.update(engine="fast", outcome=fast_proof.outcome(status, proof), proof=proof, answer=answer)
        if getattr(state, "redirects", 0):
            result["redirects"] = state.redirects  # History reads it without the run's events
        if getattr(state, "loop_summary", None) is not None:
            result["loop"] = state.loop_summary
        if getattr(state, "visual_evidence", None) is not None:
            result["visual_evidence"] = state.visual_evidence
        self._store_compiled(state, status, data_status)
        if self.trace is None and getattr(state, "latency", None) is not None:
            # This agent owns the run's trace: publish it with the result.
            try:
                self.emit(state.latency.event())
            except Exception:
                pass  # Telemetry never changes a run's outcome.
        self.emit(result)
        return result

    def _extract_answer(self, state, snapshot, selectable, status, reason):
        """Extract the requested answer, verify it, and give a rejected pick one second opinion.

        The terminal answer gates of ``_finish``. Returns (status, reason, data, citations,
        output_support, schema_validated, data_status, resolved_schema); ContinueTask from
        ``_continue_if_*`` propagates to the step loop as before.
        """
        data, citations, schema_validated = None, [], False
        output_support = None
        resolved_schema = state.schema
        data_status = "not_requested"
        observed_evidence = state.evidence.public()

        answer_source = ["helper"]

        def attempt_extraction():
            nonlocal resolved_schema, selectable
            if selectable:
                selectable = False  # One pick per run; a retry goes to the helper.
                try:
                    prefetched = self._take_prefetch(state, snapshot)
                    if prefetched is not None:
                        state.prefetched = prefetched
                        self.emit({"event": "extraction_selected", "step": len(state.metrics),
                                   "prefetched": True})
                        answer_source[0] = "selection"
                        return validate_extraction(prefetched[0], resolved_schema, observed_evidence)
                    with self._span("extract.select"):
                        produced = self.model.select_fields(state.goal, state.schema, observed_evidence,
                                                            timeout=state.budget())
                    state.budget()
                    self.emit({"event": "extraction_selected", "step": len(state.metrics)})
                    answer_source[0] = "selection"
                    return validate_extraction(produced, resolved_schema, observed_evidence)
                except (Cancelled, TimeoutError, InsufficientEvidence, SpendCapExceeded):
                    raise
                except Exception as error:
                    self.emit({"event": "extraction_selection_unsure", "step": len(state.metrics),
                               "reason": type(error).__name__, "detail": _safe_detail(error)})
                    if not self.helper or self.helper.calls >= self.max_helper_calls:
                        raise
            if state.automatic_output:
                with self._span("extract.helper"):
                    produced = self.helper.extract_auto(observed_evidence, state.goal,
                                                        state.resolved_output_format, timeout=state.budget())
                resolved_schema = validate_automatic_schema(state.resolved_output_format, produced["schema"])
                produced = {"data": produced["data"], "citations": produced["citations"]}
            else:
                with self._span("extract.helper"):
                    produced = self.helper.extract(observed_evidence, state.goal, state.schema,
                                                   timeout=state.budget())
            state.budget()
            return validate_extraction(produced, resolved_schema, observed_evidence)

        def extract_with_retry():
            """Retry a malformed helper response, never an abstention.

            A response that fails schema or literal-citation validation is a
            formatting failure; the same evidence may extract cleanly on a
            second call. An explicit abstention is a real answer about the
            evidence and repeating it would only spend budget to hear it again.
            """
            while True:
                try:
                    return attempt_extraction()
                except (Cancelled, TimeoutError, InsufficientEvidence, SpendCapExceeded):
                    raise
                except Exception as error:
                    if (state.extraction_retries >= self.max_extraction_retries
                            or not self.helper or self.helper.calls >= self.max_helper_calls):
                        raise
                    state.extraction_retries += 1
                    self.emit({"event": "extraction_retry", "step": len(state.metrics),
                               "attempt": state.extraction_retries, "reason": "unusable_response",
                               "detail": _safe_detail(error)})

        try:
            try:
                extracted = extract_with_retry()
            except InsufficientEvidence:
                # Accessibility legitimately omits some on-screen text.
                # Read the pixels once, then hold the result to the same
                # literal-citation contract; abstention is still allowed.
                # A second extraction needs its own helper budget, and a
                # screen is never read when that budget cannot pay for it.
                if (not self.helper or self.helper.calls >= self.max_helper_calls
                        or not self._read_visual(state, snapshot)[0]):
                    raise
                observed_evidence = state.evidence.public()
                extracted = extract_with_retry()
        except SpendCapExceeded:
            raise
        except Cancelled:
            status, data_status, reason = "stopped", "not_extracted", "Stopped during result extraction"
        except TimeoutError:
            data_status = "extraction_timeout"
        except InsufficientEvidence:
            data_status = "insufficient_evidence"
            self._continue_if_answer_elsewhere(state)
        except Exception:
            data_status = "extraction_failed"
            self._continue_if_answer_elsewhere(state)
        else:
            # A quoted label or placeholder can pass literal validation without
            # answering the request. Keep the candidate private until Jev agrees.
            try:
                verifier = getattr(self.model, "verify_output", None)
                prefetched, state.prefetched = state.prefetched, None
                if prefetched is not None and prefetched[0] == extracted:
                    support, signals = prefetched[1], prefetched[2]
                elif callable(verifier):
                    with self._span("extract.verify"):
                        support, signals = self._verify_answer(state, extracted, snapshot)
                else:
                    support, signals = OutputSupport.UNCLEAR, {}
                state.budget()
                output_support = support if isinstance(support, OutputSupport) else OutputSupport.UNCLEAR
                accepted_by = "verifier" if output_support is OutputSupport.SUPPORTED else None
                if accepted_by is None and claims_calibrated(signals):
                    # Calibrated per-claim acceptance (task_policy.CALIBRATED_CLAIM_FLOORS).
                    output_support, accepted_by = OutputSupport.SUPPORTED, "claims_calibrated"
                agree = None
                if (output_support is not OutputSupport.SUPPORTED and answer_source[0] == "selection"
                        and self.helper and self.helper.calls < self.max_helper_calls):
                    # A rejected pick is one opinion about which literal answers;
                    # the generative extractor gets one independent attempt,
                    # held to the same literal and verification contract.
                    answer_source[0] = "helper"
                    self.emit({"event": "extraction_second_opinion", "step": len(state.metrics)})
                    first = extracted
                    try:
                        extracted = extract_with_retry()
                    except (Cancelled, TimeoutError, SpendCapExceeded):
                        raise
                    except Exception as error:
                        self.emit({"event": "second_opinion_failed", "step": len(state.metrics),
                                   "reason": type(error).__name__})
                        extracted = first
                    else:
                        agree = extracted["data"] == first["data"]
                        with self._span("extract.verify"):
                            support, signals = self._verify_answer(state, extracted, snapshot)
                        state.budget()
                        output_support = (support if isinstance(support, OutputSupport)
                                          else OutputSupport.UNCLEAR)
                        if output_support is OutputSupport.SUPPORTED:
                            accepted_by = "verifier"
                        elif agree and agreement_accepts(signals):
                            # Two independent extractors chose the same literal and
                            # every per-claim check is high: accept despite a split
                            # overall verdict (thresholds: task_policy, calibrated
                            # by evals/calibrate.py).
                            output_support = OutputSupport.SUPPORTED
                            accepted_by = agreement_reason(signals)
                self.emit({"event": "answer_signals", "step": len(state.metrics), "source": answer_source[0],
                           "agree": agree, "accepted_by": accepted_by,
                           # A digest, never the answer: lets evals/calibrate.py label this
                           # candidate against ground truth it already holds.
                           "candidate_sha": answer_digest(extracted["data"]),
                           **{k: round(v, 4) for k, v in signals.items() if isinstance(v, float)}})
            except Cancelled:
                status, data_status, reason = "stopped", "not_extracted", "Stopped during answer verification"
            except TimeoutError:
                status, data_status, reason = ("completion_not_confirmed", "verification_timeout",
                                              "Answer verification timed out; no answer was returned")
            except Exception:
                status, data_status, reason = ("completion_not_confirmed", "verification_failed",
                                              "Answer verification failed; no answer was returned")
            else:
                if output_support is OutputSupport.SUPPORTED:
                    data, citations = extracted["data"], extracted["citations"]
                    schema_validated, data_status = True, "extracted"
                else:
                    self._continue_if_route_unfinished(state, signals)
                    status, data_status, reason = ("completion_not_confirmed", "unsupported_answer",
                        "The proposed answer could not be supported as the requested facts from observed app evidence")
        return (status, reason, data, citations, output_support, schema_validated, data_status,
                resolved_schema)


    def _store_compiled(self, state, status, data_status):
        """Record a run that passed every gate; drop a recording that led a run astray."""
        self._store_memo(state, status, data_status)
        store, key = self.replay_store, getattr(state, "replay_key", None)
        if store is None or key is None:
            return
        succeeded = _run_succeeded(state, status, data_status)
        try:
            if succeeded and state.trace and len(state.goals) == 1:
                trace = [dict(step) for step in state.trace]
                if state.output_intent is not None:
                    trace[0]["output_intent"] = state.output_intent.value
                store.save(key, trace)
            elif not succeeded and state.replay_used:
                store.forget(key)
        except OSError:
            pass

    def _store_memo(self, state, status, data_status):
        """Memoize a successful run's live decisions; forget entries a failed run used."""
        if self.decision_memo is None:
            return
        succeeded = _run_succeeded(state, status, data_status)
        try:
            if succeeded:
                for key, decision in getattr(state, "memo_pending", ()):
                    self.decision_memo.save(key, decision)
            else:
                for key in getattr(state, "memo_used", ()):
                    self.decision_memo.forget(key)
        except Exception:
            pass  # The memo is an optimization; it never changes a run's outcome.

    def _stop_if_required(self, state, decision, observed, step):
        if decision.stop_gate == StopGate.CONTINUE:
            return None
        if decision.stop_gate == StopGate.STOP and not has_stop_condition(state.goal):
            self.emit({"event": "stop_gate_unfounded", "step": step, "operation": decision.operation})
            return None
        if decision.stop_gate == StopGate.STOP:
            if decision.operation == "DONE" and not _CONDITIONAL_STOP.search(state.goal):
                # Nothing further would be done either way; a prohibition read as a stop
                # condition ("only look; do not open anything") must not turn a finished
                # navigation into "stopped" (MobsterBench nav.files_on_my_iphone, 24 Sep),
                # nor keep a question from reading the answer on screen (ret.note_code,
                # diag-15: the open note showed "Locker code: 4817"). Reading acts on nothing.
                # The completion check still re-reads and re-decides.
                self.emit({"event": "stop_gate_done", "step": step})
                return None
            return self._finish(state, "user_condition_met", observed,
                                "Stopped because an explicit condition in the original request applies")
        if decision.operation in _UNCLEAR_STOP_PROCEED and not observed.elements:
            self.emit({"event": "stop_gate_unclear", "step": step, "operation": decision.operation})
            return None
        target = next((e for e in observed.elements if e.id == decision.target), None)
        if not has_stop_condition(state.goal) and (
                decision.operation in _READ_ONLY_OPERATIONS
                or target is not None and requested_by(
                    state.goal, decision.operation, target.role, target.label,
                    side_effect_risk=decision.side_effect_risk)):
            # No conditional or prohibitive wording: no condition to be unclear about, for a look
            # or an act the request names (iOSWorld multi-006, 24 Sep: "Book a table for 8 ..."
            # ended at step 1 on a WAIT). Anything else still asks.
            self.emit({"event": "stop_gate_unfounded", "step": step, "operation": decision.operation})
            return None
        searching = (decision.operation in TEXT_OPERATIONS and target is not None and target.role == "SearchField")
        # A plain navigation tap (a row, a cell; no state-changing words, low side-effect
        # score) only opens a screen: for a question it is looking, like a scroll.
        opening = (decision.operation == "TAP" and target is not None
                   and target.role in ("Cell", "Button", "Link", "StaticText")
                   and decision.side_effect_risk is not None and decision.side_effect_risk < .15
                   and not STATE_CHANGING_LABEL.search(target.label or "")
                   and not COMMIT_CONTROL.search(target.label or ""))
        searching = searching or opening
        if (state.wants_output and (decision.operation in _READ_ONLY_OPERATIONS or searching)
                and not _CONDITIONAL_STOP.search(state.goal)):
            # A question ("is Bluetooth on or off?") read as "not actionable": measured on
            # MobsterBench (24 Sep) ending runs at step 0. Looking cannot break a stop
            # condition; every action that could is gated again at its own step.
            self.emit({"event": "stop_gate_unclear_read_only", "step": step, "operation": decision.operation})
            return None
        return self._finish(state, "needs_clarification", observed,
                            "Cannot safely determine whether the original request permits continuing")

    def _refresh_unchanged(self, state, observed, step, target=None):
        # A guard and its action share one observation. Changed evidence discards both,
        # including before any recovery/text helper or terminal extraction is called.
        with self._span("observe.refresh"):
            fresh = self.driver.observe(timeout=state.budget())
        state.budget()
        state.evidence.add(fresh, step)
        changed = fresh.fingerprint != observed.fingerprint
        if changed and target is not None and fresh.bundle_id == observed.bundle_id:
            key = (target.locator, target.label, target.role, target.rect)
            match = next((e for e in fresh.elements if (e.locator, e.label, e.role, e.rect) == key), None)
            if match is not None:
                # The same element is still exactly where the tap will land.
                self.emit({"event": "refresh_target_stable", "step": step})
                state.retarget = match
                return fresh
        if changed:
            self.emit({"event": "stale_decision", "step": step})
            return None
        return fresh
