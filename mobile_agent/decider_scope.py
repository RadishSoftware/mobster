"""What a decision model may decide, as code (owner rule, 2026-10-09)."""

from .task_policy import COMMIT_CONTROL, GUARD_WORDS, SHEET_OPENERS

TARGET, CHECK, BRANCH = "target", "check", "branch"
KINDS = (TARGET, CHECK, BRANCH)
# Text, long presses (context menus hold commits), point taps, text macros and the run's endings are the planner's.
NAVIGATION_OPS = frozenset({"TAP", "SWIPE_UP", "SWIPE_DOWN", "SWIPE_LEFT", "SWIPE_RIGHT", "DISMISS", "HOME",
                            "LAUNCH_APP", "WAIT"})
CHECK_TOPICS = frozenset({"settled", "toast", "popup", "screen_is", "item_visible", "evidence", "item_relevant",
                          "item_ad"})
# Roles that change a setting when tapped are never a decider's target.
STATEFUL_ROLES = frozenset({"Switch", "Toggle", "Slider", "Stepper", "PickerWheel", "Picker"})


class OutOfScope(ValueError):
    """The decision model was asked, or answered, outside navigation and classification."""


def commits(element):
    """True when tapping ``element`` may commit something (or its label can't be read)."""
    if element is None:
        return True
    label = " ".join((getattr(element, "label", "") or "").split()[:GUARD_WORDS])
    if getattr(element, "role", "") in STATEFUL_ROLES or getattr(element, "editable", False):
        return True
    return bool(COMMIT_CONTROL.search(label)) and label.casefold() not in SHEET_OPENERS


def admit(kind, *, op=None, topic=None):
    """Raise OutOfScope unless a question of ``kind`` may go to a decision model."""
    if kind not in KINDS:
        raise OutOfScope(f"unknown decision kind {kind!r}")
    if kind == TARGET:
        if op not in NAVIGATION_OPS:
            raise OutOfScope(f"{op} is the planner's: a decision model only navigates")
    elif kind == CHECK:
        if topic not in CHECK_TOPICS:
            raise OutOfScope(f"check {topic!r} is not a state check a decision model may answer")

