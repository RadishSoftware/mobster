"""The permission gate: one per run. It applies the run's mode to an action and says allow or ask, with the card's
title, and whether the card may offer "Don't ask again".

Like a coding harness's permission modes. The apps a person allowed are the boundary (``PermissionGuard``): in them,
reading and moving around never ask. What asks is decided by today's rules, which the agent passes in as
``existing_act`` (Smart's declared commit, Fast's commit control, a coding agent's tool call):

- ask_important (the default): anything with an effect outside the phone asks: a message, a post, a reaction, a share,
  a follow, a call, an order, a booking, a deletion.
- ask_all: also a change that stays on the phone (``rules.changes``: a switch, Save, Add, Mark as read).
- dont_ask (full access): nothing asks.
- In every mode, typing a one-time code asks first (``rules.code_entry``).

"Don't ask again" is an allow rule for one kind of action in one app ("sending messages in Messages",
``rules.rememberable``), kept in the person's store (``exceptions``). Never near money (an amount, a price, a bank or
payment app), never for a word that undoes something, never on the Home Screen or iOS's sheets, never for a code, and
never for an ask that is forced (``decide(forced=True)``: a step the agent's check isn't sure of, a long run's acts). A
run an agent outside Mobster started never uses one: it asks.

- ``decide(action, existing_act=None)``: the answer for one action.
- ``observe(snapshot, ...)``: called while the model plans the next step. Today's rules need nothing ahead.
- ``remember(decision, app, app_name)``: what the card's "Don't ask again" remembers, or None.
- ``PermissionGuard``: which apps a run may open: the first time a task needs an app, it asks.
"""

from dataclasses import dataclass
import re
import threading
import time

from . import policy as P
from .rules import (RULE_OP, Action, changes, code_entry, norm, outside_act, rememberable, rule_words,
                    screen_context)

TITLES = {"code": "Enter this code?", "setting": "Change this setting?", "save": "Save this?",
          "submit": "Submit this?", "change": "Make this change?", "react": "Add this reaction?",
          "follow": "Follow this account?", "unfollow": "Unfollow this account?", "post": "Repost this?",
          "block": "Block this?", "report": "Report this?"}
# The card's line for an act ``rules.outside_act`` found (at most 12 words).
OUTSIDE_REASONS = {"react": "Others see this reaction", "follow": "Others see this follow",
                   "unfollow": "Others see this unfollow", "post": "Others see this repost",
                   "block": "Changes who can reach you", "report": "Sends a report about someone"}


@dataclass(frozen=True)
class Decision:
    decision: str            # allow | ask | block
    kind: str                # navigate | input | change | commit | floor | secret
    act: str | None          # the card's verb
    reason: str              # for the card, at most 12 words
    tier: int                # 0: today's rules
    ms: float                # time this decision added
    title: str | None = None
    fallback: bool = False
    # The approval card may offer "Don't ask again" (an allow rule for ``rule`` in this app).
    rememberable: bool = False
    rule: str | None = None

    @property
    def asks(self):
        return self.decision == "ask"


def decision_for(kind, mode, existing=False):
    """allow | ask | block for an action of ``kind`` under ``mode``. ``existing``: the agent's own rules ask."""
    if kind == "secret":
        return "block"
    if kind == "floor":
        return "ask"
    if mode == P.DONT_ASK:
        return "allow"
    if existing or kind == "commit":
        return "ask"
    if kind == "change" and mode == P.ASK_ALL:
        return "ask"
    return "allow"


_COMPOSE = re.compile(r"\b(message|imessage|text message|reply|comment|write|chat|say something|add a caption)\b")


def composer_text(elements):
    """The text waiting in the screen's composer (a message box with a value), "" for an empty one, None when the
    screen has no composer."""
    found = None
    for element in elements or ():
        label, placeholder = getattr(element, "label", "") or "", getattr(element, "placeholder", "") or ""
        if getattr(element, "role", None) not in ("TextView", "TextField") or not _COMPOSE.search(norm(label or placeholder)):
            continue
        value = getattr(element, "value", "") or ""
        if placeholder and value == placeholder or value == label:
            value = ""
        if value.strip():
            return value
        found = ""
    return found


def action_from(op, element, snapshot, *, text=None, typed=None, app_name="", goal="", title=None, origins=None):
    """The gate's Action for ``op`` on ``element`` (a state.Element, or None) in ``snapshot``. ``origins``: the rows the
    run has seen that hold the digits typed (rules.code_entry), or None when it can't say."""
    elements = getattr(snapshot, "elements", ()) or ()
    if title is None:
        try:
            from ..contract import screen_title
            title = screen_title(snapshot) or ""
        except Exception:
            title = ""
    return Action(op=op, label=getattr(element, "label", "") or "", role=getattr(element, "role", None),
                  value=getattr(element, "value", None) or None,
                  app=getattr(snapshot, "bundle_id", "") or "", app_name=app_name or "", text=text,
                  typed=typed if typed is not None else composer_text(elements), screen=title or "",
                  context=screen_context(elements), goal=goal or "",
                  origins=tuple(origins) if origins is not None else None)


class PermissionGate:
    def __init__(self, mode=P.DEFAULT_MODE, *, apps=None, emit=None, goal="", origin=None, exceptions=None):
        self.mode = mode if mode in P.MODES else P.DEFAULT_MODE
        self.apps = apps if apps is not None else P.AppPolicy()
        self.emit = emit
        self.goal = goal
        self.origin = origin
        self.exceptions = exceptions
        self.stats = {"decisions": 0, "asks": 0, "blocks": 0, "remembered": 0}

    def observe(self, snapshot, *, app_name="", goal=None, title=None, wait=False):
        """Called while the model plans; today's rules read nothing ahead."""
        return None

    def prefetch(self, action):
        return None

    def warm(self):
        return None

    def reading(self, action, existing_act=None):
        """(kind, act, reason) for ``action`` by today's rules."""
        if code_entry(action):
            return "floor", "code", "Types a one-time code"
        if existing_act:
            return "commit", existing_act, ""
        act = outside_act(action)
        if act is not None:
            return "commit", act, OUTSIDE_REASONS[act]
        act = changes(action)
        if act is not None:
            return "change", act, ""
        op = (action.op or "").upper()
        return ("input" if op in ("TYPE", "SET_TEXT") else "navigate"), None, ""

    def decide(self, action, existing_act=None, forced=False):
        """The answer for ``action``. ``forced``: the run must ask here whatever the mode or a rule says (a step the
        agent's check isn't sure of, a long run's commit): it asks, and no "Don't ask again" rule is offered or used."""
        started = time.perf_counter()
        kind, act, reason = self.reading(action, existing_act)
        verdict = decision_for(kind, self.mode, existing=bool(existing_act))
        if forced and verdict == "allow":
            verdict = "ask"
        outside = self.origin in P.OUTSIDE_ORIGINS
        rule = (rememberable(action, act) if verdict == "ask" and kind in ("commit", "change") and not forced
                else None)
        if rule is not None and not outside and self.exceptions is not None:
            try:
                remembered = self.exceptions.contains(action.app, rule, RULE_OP)
            except Exception:  # noqa: BLE001 -- an unreadable store asks
                remembered = False
            if remembered:
                self.stats["remembered"] += 1
                return self._record(Decision("allow", kind, act, "You said not to ask again for this", 0,
                                             (time.perf_counter() - started) * 1000, rule=rule))
        title = None
        if verdict == "ask":
            title = TITLES.get(act or "") if kind in ("floor", "change") or (
                kind == "commit" and not existing_act) else None
        return self._record(Decision(verdict, kind, act, reason if verdict != "allow" else "", 0,
                                     (time.perf_counter() - started) * 1000, title,
                                     rememberable=rule is not None and not outside, rule=rule))

    @staticmethod
    def remember(decision, app, app_name=""):
        """The approval request's ``remember`` for ``decision`` (server.Run keeps it until the answer): the allow
        rule "Don't ask again" adds, or None when the card doesn't offer it."""
        if decision is None or not decision.rememberable or not decision.rule or not app:
            return None
        return {"app": app, "label": decision.rule, "op": RULE_OP, "app_name": app_name or "",
                "words": rule_words(decision.rule, app_name)}

    def _record(self, decision):
        self.stats["decisions"] += 1
        if decision.decision == "ask":
            self.stats["asks"] += 1
        elif decision.decision == "block":
            self.stats["blocks"] += 1
        if self.emit is not None and decision.decision != "allow":
            try:
                # No label, no text: what the gate said, and why.
                self.emit({"event": "permission_decision", "decision": decision.decision, "kind": decision.kind,
                           "act": decision.act, "tier": decision.tier, "ms": round(decision.ms, 2),
                           "mode": self.mode, "fallback": decision.fallback})
            except Exception:
                pass
        return decision


class PermissionGuard:
    """A phone guard that applies the person's app choices (agent_hooks.safe_allows): an app on the never-open list
    (or turned off for agents outside Mobster) is never opened; an app not approved yet asks the first time, "Allow
    Mobster to use Things?", and Allow is remembered in ``grants`` (the "Don't ask again" store) until the person
    switches it off. With nobody to answer (``ask`` None), or after Not now, Smart never launches the app, and
    finding itself in one it goes Home and stops. Everything else is the inner guard's. Fails closed.

    Read live: ``apps`` may be a callable returning the current AppPolicy, and approvals are read from ``grants`` at
    every check, so switching an app off in Settings takes effect mid-run. An answer that says what to do instead
    ("Say what to do instead") is kept in ``instruction`` for the run (``take_instruction``)."""

    def __init__(self, inner, apps, names=None, outside=False, ask=None, grants=None):
        self.inner, self.names, self.outside = inner, dict(names or {}), outside
        self._apps = apps
        self.ask, self.grants = ask, grants
        self.declined = set()  # apps the person said Not now to in this run: not asked again in it
        self._granted = set()  # approved in this run when the store couldn't keep it
        self.instruction = None
        self._lock = threading.Lock()

    @property
    def apps(self):
        apps = self._apps
        try:
            return apps() if callable(apps) else apps
        except Exception:  # noqa: BLE001 -- unreadable settings: nothing counts as approved, the never-open list holds
            return P.AppPolicy(access="ask")

    @apps.setter
    def apps(self, value):
        self._apps = value

    def _approved(self, apps, bundle):
        return bundle in self._granted or apps.approved(bundle, self.grants)

    def take_instruction(self):
        """What the person said to do instead on the app card, once; None when they didn't."""
        with self._lock:
            instruction, self.instruction = self.instruction, None
        return instruction

    def __getattr__(self, name):
        if self.inner is None:
            raise AttributeError(name)
        return getattr(self.inner, name)

    def check(self, driver, *, cause):
        if self.inner is None:
            from ..agent_hooks import READY
            return READY
        return self.inner.check(driver, cause=cause)

    def attached(self):
        return self.inner.attached() if self.inner is not None else None

    def finish(self, driver):
        if self.inner is not None:
            self.inner.finish(driver)

    def _name(self, bundle):
        return self.names.get(bundle) or bundle

    def request(self, bundles):
        """Whether every app in ``bundles`` may be used now: those not approved yet are asked about together, once
        ("Allow Mobster to use Things and Notes?"). False when one is refused, declined, or nobody can answer."""
        apps = self.apps
        wanted = [b for b in dict.fromkeys(bundles) if b and b != P.SPRINGBOARD]
        if any(not apps.allows(b, self._name(b), outside=self.outside) for b in wanted):
            return False
        with self._lock:
            missing = [b for b in wanted if not self._approved(apps, b)]
            if not missing:
                return True
            if any(b in self.declined for b in missing) or self.ask is None:
                return False
            names = [self._name(b) for b in missing]
            listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
            try:
                answer = self.ask({
                    "kind": "commit", "operation": "OPEN_APP", "label": listed[:200], "role": "App", "text": None,
                    "title": f"Allow Mobster to use {listed}?", "act": "use app", "app_name": names[0], "apps": names,
                    "reason": P.app_card_reason(len(names))})
            except Exception:  # noqa: BLE001 -- no answer: the app stays shut
                answer = None
            if answer != "approved":
                self.declined.update(missing)
                if isinstance(answer, str) and answer.startswith("redirected:"):
                    self.instruction = " ".join(answer.split(":", 1)[1].split())[:500] or None
                return False
            for bundle in missing:
                try:
                    self.grants.add(bundle, P.APP_LABEL, P.APP_OP, self._name(bundle))
                except Exception:  # noqa: BLE001 -- approved for this run even if the store can't keep it
                    self._granted.add(bundle)
            return True

    def allows_app(self, bundle_id):
        try:
            if bundle_id and bundle_id != P.SPRINGBOARD and not self.request([bundle_id]):
                return False
            inner = getattr(self.inner, "allows_app", None)
            return bool(inner(bundle_id)) if callable(inner) else True
        except Exception:  # noqa: BLE001 -- fails closed
            return False

    def blocked_message(self, bundle_id, name=None):
        name = name or self._name(bundle_id)
        apps = self.apps
        if not apps.allows(bundle_id, name, outside=self.outside):
            return apps.blocked_message(name, outside=self.outside)
        if not self._approved(apps, bundle_id):
            return apps.unapproved_message(name)
        inner = getattr(self.inner, "blocked_message", None)
        return inner(bundle_id, name) if callable(inner) else None


class AppCheck:
    """``app_check`` for an agent without a phone guard of its own (Fast, a replay): whether ``bundle`` may be used
    now, through ``guard`` (agent_hooks.safe_allows: the person's app choices, asking the first time), and why not."""

    def __init__(self, guard):
        self.guard = guard

    def __call__(self, bundle):
        from ..agent_hooks import safe_allows
        return safe_allows(self.guard, bundle)

    def message(self, bundle):
        said = getattr(self.guard, "blocked_message", None)
        try:
            return said(bundle) if callable(said) else None
        except Exception:  # noqa: BLE001
            return None
