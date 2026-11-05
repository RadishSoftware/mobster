"""The permission gate: one per run; applies the run's mode to an action and says allow or ask."""

import re
import threading
import time
from dataclasses import dataclass, replace

from ..state import icon_glyph, pixel_read
from ..task_policy import approval_title
from . import consequence, project_policy
from . import policy as P
from .rules import RULE_OP, Action, changes, code_entry, norm, outside_act, rememberable, rule_words, screen_context

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
    decision: str            # allow | ask
    kind: str                # navigate | input | change | commit | floor
    act: str | None          # the card's verb
    reason: str              # for the card, at most 12 words
    ms: float                # time this decision added
    title: str | None = None
    # The approval card may offer "Don't ask again" (an allow rule for ``rule`` in this app).
    rememberable: bool = False
    rule: str | None = None
    # The ask is the project policy's (project_policy.py): the mode and the guards would have let it go ahead.
    by_policy: bool = False

    @property
    def asks(self):
        return self.decision == "ask"


def decision_for(kind, mode, existing=False):
    """allow | ask for an action of ``kind`` under ``mode``. ``existing``: the agent's own rules ask."""
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
    """The text waiting in the screen's composer, "" for an empty one, None when the screen has no composer."""
    found = None
    for element in elements or ():
        label, placeholder = getattr(element, "label", "") or "", getattr(element, "placeholder", "") or ""
        if (getattr(element, "role", None) not in ("TextView", "TextField")
                or not _COMPOSE.search(norm(label or placeholder))):
            continue
        value = getattr(element, "value", "") or ""
        if placeholder and value == placeholder or value == label:
            value = ""
        if value.strip():
            return value
        found = ""
    return found


def action_from(op, element, snapshot, *, text=None, typed=None, app_name="", goal="", title=None, origins=None):
    """The gate's Action for ``op`` on ``element`` (a state.Element, or None) in ``snapshot``."""
    elements = getattr(snapshot, "elements", ()) or ()
    if title is None:
        try:
            from ..contract_screen import screen_title
            title = screen_title(snapshot) or ""
        except Exception:
            title = ""
    return Action(op=op, label=getattr(element, "label", "") or "", role=getattr(element, "role", None),
                  value=getattr(element, "value", None) or None,
                  app=getattr(snapshot, "bundle_id", "") or "", app_name=app_name or "", text=text,
                  typed=typed if typed is not None else composer_text(elements), screen=title or "",
                  context=screen_context(elements), goal=goal or "",
                  hint=" ".join(str(getattr(element, key, "") or "") for key in ("placeholder", "identifier")),
                  origins=tuple(origins) if origins is not None else None,
                  rect=tuple(getattr(element, "rect", ()) or ()), around=tuple(elements),
                  pixels=pixel_read(snapshot), glyph=icon_glyph(getattr(element, "locator", "")))


class PermissionGate:
    def __init__(self, mode=P.DEFAULT_MODE, *, apps=None, emit=None, goal="", origin=None, exceptions=None,
                 policy=None, profile=None):
        self.mode = mode if mode in P.MODES else P.DEFAULT_MODE
        self.apps = apps if apps is not None else P.AppPolicy()
        self.emit = emit
        self.goal = goal
        self.origin = origin
        self.exceptions = exceptions
        # The project's policy and the profile (accounts' ``{id, handle...}``) the task acts as: they only add asks.
        self.policy = policy
        self.profile = profile

    @property
    def narrowed(self):
        """Whether the project policy can ask here, so a run asks (or refuses, with nobody to answer) in Full access."""
        return self.policy is not None and self.policy.narrows

    def reading(self, action, existing_act=None):
        """(kind, act, reason) for ``action`` by today's rules."""
        if code_entry(action):
            return "floor", "code", "Types a one-time code"
        if existing_act:
            return "commit", existing_act, ""
        found = consequence.read(action if action.goal or not self.goal else replace(action, goal=self.goal),
                                 action.around)
        if found is not None:
            return "commit", found.act, found.reason
        act = outside_act(action)
        if act is not None:
            return "commit", act, OUTSIDE_REASONS[act]
        act = changes(action)
        if act is not None:
            return "change", act, ""
        op = (action.op or "").upper()
        return ("input" if op in ("TYPE", "SET_TEXT") else "navigate"), None, ""

    def decide(self, action, existing_act=None, forced=False):
        """The answer for ``action``; ``forced`` asks whatever the mode says and offers or uses no allow rule. The
        project policy, when there is one, can turn an allow into an ask and nothing else."""
        decision = self._decide(action, existing_act, forced)
        if self.policy is not None and decision.kind in ("commit", "change"):
            decision = self._narrow(decision, action)
        return self._record(decision)

    def _narrow(self, decision, action):
        """``decision`` or, when the project policy asks where the mode and the guards would not, an ask. An ask the
        mode already made stays as it is, without an offer of "Don't ask again" if the policy asks too."""
        kinds = project_policy.kinds_of(decision.kind, decision.act)
        why = self.policy.asks(kinds, action.app, self.profile)
        if decision.asks:
            return replace(decision, rememberable=False, rule=None) if why and decision.rememberable else decision
        why = why or self.policy.take(kinds, action.app, self.profile)
        if why is None:
            return decision
        act = decision.act
        title = consequence.TITLES.get(act or "") or TITLES.get(act or "") or approval_title(act)
        return Decision("ask", decision.kind, act, why, decision.ms, title, by_policy=True)

    def _decide(self, action, existing_act, forced):
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
                return Decision("allow", kind, act, "You said not to ask again for this",
                                (time.perf_counter() - started) * 1000, rule=rule)
        title = None
        if verdict == "ask":
            title = (consequence.REASON_TITLES.get(reason) or TITLES.get(act or "")
                     or consequence.TITLES.get(act or "")) if kind in ("floor", "change") or (
                kind == "commit" and not existing_act) else None
        return Decision(verdict, kind, act, reason if verdict != "allow" else "",
                        (time.perf_counter() - started) * 1000, title,
                        rememberable=rule is not None and not outside, rule=rule)

    @staticmethod
    def remember(decision, app, app_name=""):
        """The approval request's ``remember`` for ``decision`` (run.Run keeps it until the answer)."""
        if decision is None or not decision.rememberable or not decision.rule or not app:
            return None
        return {"app": app, "label": decision.rule, "op": RULE_OP, "app_name": app_name or "",
                "words": rule_words(decision.rule, app_name)}

    def _record(self, decision):
        if self.emit is not None and decision.decision != "allow":
            try:
                # No label, no text: what the gate said, and why.
                self.emit({"event": "permission_decision", "decision": decision.decision, "kind": decision.kind,
                           "act": decision.act, "ms": round(decision.ms, 2), "mode": self.mode,
                           **({"policy": True} if decision.by_policy else {})})
            except Exception:
                pass
        return decision


class PermissionGuard:
    """A phone guard for the person's app choices; the rest is the inner guard's. Fails closed: no answer, no app."""

    def __init__(self, inner, apps, names=None, outside=False, ask=None, grants=None, conversation=None):
        self.inner, self.names, self.outside = inner, dict(names or {}), outside
        self._apps = apps
        self.ask, self.grants = ask, grants
        self.declined = set()  # apps the person said Not now to in this run: not asked again in it
        # Allow covers this run, or its conversation's runs; never an app that is refused or switched off.
        self._conversation = conversation is not None
        self._granted = conversation if conversation is not None else set()
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
        """Whether every app in ``bundles`` may be used now: False if one is refused, declined, or nobody answers."""
        apps = self.apps
        wanted = [b for b in dict.fromkeys(bundles) if b and b != P.SPRINGBOARD]
        if any(not apps.allows(b, outside=self.outside) for b in wanted):
            return False
        with self._lock:
            missing = [b for b in wanted if not self._approved(apps, b)]
            if not missing:
                return True
            if any(b in self.declined for b in missing) or self.ask is None:
                return False
            names = [self._name(b) for b in missing]
            listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
            card = {"kind": "commit", "operation": "OPEN_APP", "label": listed[:200], "role": "App", "text": None,
                    "title": f"Allow Mobster to use {listed}?", "act": "use app", "app_name": names[0], "apps": names,
                    "bundles": missing, "reason": P.task_card_reason(len(names), self._conversation)}
            if self.grants is not None:
                card["remember"] = {"app": missing[0], "label": P.APP_LABEL, "op": P.APP_OP, "app_name": names[0],
                                    "words": P.ALWAYS_ALLOW,
                                    "apps": [{"app": b, "app_name": n} for b, n in zip(missing, names)]}
            try:
                answer = self.ask(card)
            except Exception:  # noqa: BLE001 -- no answer: the app stays shut
                answer = None
            if answer != "approved":
                self.declined.update(missing)
                if isinstance(answer, str) and answer.startswith("redirected:"):
                    self.instruction = " ".join(answer.split(":", 1)[1].split())[:500] or None
                return False
            # Allow: this run only; Always allow is read from the store at each check, so it can be off mid-run.
            self._granted.update(b for b in missing if not apps.approved(b, self.grants))
            return True

    def allows_app(self, bundle_id):
        try:
            if bundle_id and bundle_id != P.SPRINGBOARD and not self.request([bundle_id]):
                return False
            inner = getattr(self.inner, "allows_app", None)
            return bool(inner(bundle_id)) if callable(inner) else True
        except Exception:  # noqa: BLE001 -- fails closed
            return False

    def refusal(self, bundles):
        """Why ``request(bundles)`` said no, without asking again."""
        apps = self.apps
        wanted = [b for b in dict.fromkeys(bundles) if b and b != P.SPRINGBOARD]
        refused = [b for b in wanted if not apps.allows(b, outside=self.outside)]
        waiting = [b for b in wanted if not self._approved(apps, b)]
        return next((self.blocked_message(b) for b in refused + waiting), None)

    def blocked_message(self, bundle_id, name=None):
        name = name or self._name(bundle_id)
        apps = self.apps
        if not apps.allows(bundle_id, outside=self.outside):
            return apps.blocked_message(name, outside=self.outside)
        if not self._approved(apps, bundle_id):
            return apps.unapproved_message(name)
        inner = getattr(self.inner, "blocked_message", None)
        return inner(bundle_id, name) if callable(inner) else None
