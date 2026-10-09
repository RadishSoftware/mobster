"""Seams between the agent loop (frontier), the phone guard (lockscreen) and skills. Frozen for this sprint.

Who does what:

- ``frontier.FrontierAgent(..., guard=None, skills=())`` calls ``PhoneGuard.check`` when a launch fails
  (``launch_failed``), when the screen and the tree disagree or a model tap lands on a SpringBoard passcode keypad
  (``lock_suspected``), after any wait for the user of RESUME_AFTER_SECONDS or more (``resume``), and after an
  App Store Get or Buy (``sheet_suspected``). It emits ``phone_guard`` for each check it makes; the guard itself
  emits ``unlock_started`` / ``unlock_finished`` / ``handoff_waiting`` / ``handoff_finished``.
  ``handoff_waiting {kind, seconds}``: kind is ``lock_screen`` (or ``passcode_keypad``) when a task a person started
  waits for them to unlock the phone at its preflight (lockscreen.UNLOCK_WAIT_S), ``face_id`` or
  ``apple_confirmation`` for a sheet; ``handoff_finished {kind, ok, waited_s}`` ends the wait (ok: the phone reads
  clear). A wait only reads the phone: nothing is pressed, tapped or typed while it lasts.
- A verdict of ``stop`` ends the run (status ``blocked``) with the verdict's sentence as its reason; ``unlocked``
  adds the history line UNLOCKED_LINE and nothing else (the model never sees the keypad); ``ready`` goes on.
- The server (preflight) and ``mcp_server.direct`` call ``check(cause="preflight")`` themselves, and the
  server calls ``finish`` when the run ends.
- A skill's ``prepare`` may navigate but never writes; when its ``Prepared.title`` is set the frontier asks for
  approval (``kind`` = the op in lower case, e.g. ``use_code``) before ``perform``. A denial means no ``perform``,
  and so does a run that cannot ask (no ``approve``): a skill with a title never runs unasked.
  The ``act`` both receive is the model's action with ``"element"``: the resolved target (a ``state.Element`` of
  ``snapshot``) when ``needs_target``, else None. Skill exceptions are reported to the model by type only.
- Every string in ``SkillResult.secrets`` is replaced by MASK in every later screen row, note, history line,
  read list, contract ledger and answer, and ``secret_rects`` are blurred in later screenshots.

No event, approval payload or verdict ever carries a passcode, a code, or a digit count.
"""
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

# What a masked secret reads as, everywhere the model or a person could see it.
MASK = "••••••"
# A wait for the user this long (approvals, hand-offs) or longer is followed by check(cause="resume").
RESUME_AFTER_SECONDS = 20
# The history line the model sees after the guard unlocked the phone (never a frame of the keypad).
UNLOCKED_LINE = "Mobster unlocked the iPhone"

GUARD_CAUSES = ("preflight", "launch_failed", "lock_suspected", "resume", "sheet_suspected")
STOP_CODES = ("phone_locked", "unlock_declined", "passcode_failed", "face_id", "app_locked", "apple_confirmation",
              "unplugged", "keychain_locked", "blocked_app")


@dataclass(frozen=True)
class GuardVerdict:
    state: str                 # "ready" | "unlocked" | "stop"
    code: str = ""             # when stop: phone_locked | unlock_declined | passcode_failed | face_id | app_locked |
                               #            apple_confirmation | unplugged | keychain_locked | blocked_app
    message: str = ""          # one plain sentence for the user; never a passcode, a code, or a digit count


class PhoneGuard(Protocol):
    def check(self, driver, *, cause: str) -> GuardVerdict: ...
        # cause: "preflight" | "launch_failed" | "lock_suspected" | "resume" | "sheet_suspected"
    def attached(self) -> Optional[bool]: ...       # False = USB detached (fail fast); None = unknown
    def allows_app(self, bundle_id: str) -> bool: ... # C7 stretch; implementations without it return True
    def finish(self, driver) -> None: ...           # run ended: relock if this guard unlocked and relock is on


@dataclass(frozen=True)
class Prepared:
    title: Optional[str]       # approval question, or None when no approval is needed; never contains a secret
    state: object = None       # skill-private (e.g. the code, held in memory only)


@dataclass(frozen=True)
class SkillResult:
    feedback: str              # what the model is told next turn (no secrets)
    changed: bool = False
    secrets: tuple = ()        # strings frontier masks ("••••••") in every later row, note, history line and answer
    secret_rects: tuple = ()   # (x, y, w, h) screen fractions frontier blurs in screenshots until that field changes
    stop: Optional[GuardVerdict] = None
    # Track harness: seconds the skill waited for the person (a question), which never count against the task's time,
    # and a status that ends the run with ``feedback`` as its reason: "approval_timeout" (nobody answered) or
    # "stopped" (Stop was pressed meanwhile).
    waited: float = 0.0
    status: Optional[str] = None


@dataclass
class SkillContext:
    driver: object
    request: str
    app_bundle: Optional[str]
    emit: Callable[[dict], None]
    guard: Optional[PhoneGuard] = None
    # Seam S7 (optional; the defaults keep every caller working):
    approve: Optional[Callable[[dict], str]] = None   # the frontier's approver, or None; RUN_ROUTINE commit steps only
    clarify: Optional[Callable[[dict], str]] = None   # run.request_approval limited to kind "clarify"; others raise
    run: Optional[object] = None                      # the run's harness_api.RunContext, or None (benchmarks, tests)
    # A skill that needs the user's OK returns Prepared(title=...) from prepare(): the frontier's existing path asks
    # (SmartEvents.approving flushes steps and records declined text), and with no approver the skill never runs.


class Skill(Protocol):
    op: str                    # e.g. "USE_CODE", "READ_NOTIFICATIONS"; must not clash with frontier.OPERATIONS
    prompt: str                # one "- OP ...: ..." line appended to frontier.SYSTEM's action list
    needs_target: bool         # True: the first action targets an element id (USE_CODE: the code field)
    # Optional, for tools other tracks register (harness/tools: the policy): interactive_only = True offers the tool
    # only in a run a person watches (ASK_USER).
    def available(self, ctx: SkillContext) -> bool: ...
    def prepare(self, ctx: SkillContext, act: dict, snapshot) -> Prepared: ...   # may navigate; no writes
    def perform(self, ctx: SkillContext, prepared: Prepared, act: dict, snapshot) -> SkillResult: ...


READY = GuardVerdict("ready")


def mask(text, secrets):
    """``text`` with every secret replaced by MASK (longest first, so a code inside a longer one goes too)."""
    if not text or not secrets or not isinstance(text, str):
        return text
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            text = text.replace(secret, MASK)
    return text


def safe_check(guard, driver, cause):
    """``guard.check(driver, cause=cause)``, or None without a guard. A guard that raises is a stop that names
    nothing secret: a failing guard must never let a run act on a phone it could not read."""
    if guard is None:
        return None
    try:
        verdict = guard.check(driver, cause=cause)
    except Exception:
        return GuardVerdict("stop", "phone_locked", "Mobster couldn't check whether your iPhone is unlocked, so it "
                                                    "stopped.")
    return verdict if isinstance(verdict, GuardVerdict) else READY


def safe_attached(guard):
    """``guard.attached()``: False only when the guard knows the phone was unplugged."""
    if guard is None:
        return None
    try:
        return guard.attached()
    except Exception:
        return None


def safe_allows(guard, bundle_id):
    """``guard.allows_app(bundle_id)``; True without a guard or for a guard without the C7 list."""
    if guard is None or not bundle_id:
        return True
    allows = getattr(guard, "allows_app", None)
    if not callable(allows):
        return True
    try:
        return bool(allows(bundle_id))
    except Exception:
        return True
