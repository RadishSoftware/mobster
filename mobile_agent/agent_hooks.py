"""Seams between the agent loop (frontier), the phone guard (lockscreen) and skills."""
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

SKILL_IMAGES = 2
MASK = "••••••"
# A wait for the user this long (approvals, hand-offs) or longer is followed by check(cause="resume").
RESUME_AFTER_SECONDS = 20
# The history line the model sees after the guard unlocked the phone (never a frame of the keypad).
UNLOCKED_LINE = "Mobster unlocked the iPhone"

# Why the phone guard stopped a run; none carries a passcode, a code or a digit count.
STOP_CODES = ("phone_locked", "unlock_declined", "passcode_failed", "face_id", "app_locked", "apple_confirmation",
              "unplugged", "keychain_locked", "blocked_app", "system_dialog")


@dataclass(frozen=True)
class GuardVerdict:
    state: str                 # "ready" | "unlocked" | "stop"
    code: str = ""             # when stop: one of STOP_CODES
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
    remember: Optional[dict] = None  # the approval's "Don't ask again" (run.Run.request_approval), or None


@dataclass(frozen=True)
class SkillResult:
    feedback: str              # what the model is told next turn (no secrets)
    changed: bool = False
    secrets: tuple = ()        # strings frontier masks ("••••••") in every later row, note, history line and answer
    secret_rects: tuple = ()   # (x, y, w, h) screen fractions frontier blurs in screenshots until that field changes
    stop: Optional[GuardVerdict] = None
    # Seconds the skill waited for the person, which never count against the task's time.
    waited: float = 0.0
    status: Optional[str] = None
    # Shown to the model once, on its next turn, before the screen; only a run that sends screenshots sends them.
    images: tuple = ()


@dataclass
class SkillContext:
    driver: object
    request: str
    app_bundle: Optional[str]
    emit: Callable[[dict], None]
    guard: Optional[PhoneGuard] = None
    approve: Optional[Callable[[dict], str]] = None   # the frontier's approver, or None; replayed commit steps only
    clarify: Optional[Callable[[dict], str]] = None   # run.request_approval limited to kind "clarify"; others raise
    run: Optional[object] = None                      # the run's harness_api.RunContext, or None (benchmarks, tests)
    # A skill that needs the user's OK returns Prepared from prepare(); with no approver it never runs.


class Skill(Protocol):
    op: str                    # e.g. "USE_CODE", "READ_NOTIFICATIONS"; must not clash with frontier.OPERATIONS
    prompt: str                # one "- OP ...: ..." line appended to frontier.SYSTEM's action list
    needs_target: bool         # True: the first action targets an element id (USE_CODE: the code field)
    # interactive_only offers the tool only in a run a person watches.
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
    """``guard.check(driver, cause=cause)``, or None without a guard; a guard that raises is a stop, never a go."""
    if guard is None:
        return None
    try:
        verdict = guard.check(driver, cause=cause)
        if isinstance(verdict, GuardVerdict) and verdict.state in ("ready", "unlocked", "stop"):
            return verdict
    except Exception:
        pass
    return GuardVerdict("stop", "phone_locked", "Mobster couldn't check whether your iPhone is unlocked, so it "
                                                "stopped.")


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
        return allows(bundle_id) is True
    except Exception:
        return False
