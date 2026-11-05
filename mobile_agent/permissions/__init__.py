"""Permission modes and app approvals: the gate, its rules and the allow rules a person keeps."""

from .exceptions import remember_in
from .gate import Decision, PermissionGate, PermissionGuard, action_from, decision_for
from .policy import (
    ASK_ALL,
    ASK_IMPORTANT,
    DONT_ASK,
    MODES,
    AppPolicy,
    Settings,
    effective_mode,
    from_env,
    from_file,
    loosens,
)
from .rules import Action


def gate_for_run(*, override=None, origin=None, goal="", emit=None, settings=None, exceptions=None, policy=None,
                 profile=None):
    """The gate for one run: the saved settings (or ``settings``), its override and origin, the allow rules, and the
    project's policy and the profile the task acts as (they only add asks)."""
    settings = settings or from_env()
    mode = effective_mode(settings.mode, override, origin)
    return PermissionGate(mode, apps=settings.apps, emit=emit, goal=goal, origin=origin, exceptions=exceptions,
                          policy=policy, profile=profile)


__all__ = ["ASK_ALL", "ASK_IMPORTANT", "DONT_ASK", "MODES", "Action", "AppPolicy", "Decision", "PermissionGate",
           "PermissionGuard", "Settings", "action_from", "decision_for", "effective_mode", "from_env", "from_file",
           "gate_for_run", "loosens", "remember_in"]
