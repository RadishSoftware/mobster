"""Permission modes and app approvals, like a coding harness's permission modes.

- ``policy``: the modes (ask_all, ask_important, dont_ask), the settings in agent.env, and which apps Mobster may use
  (approved one by one, the first time a task needs each). An allowed app is the boundary: in it, reading and moving
  around never ask.
- ``rules``: today's rules as the gate reads them: a typed one-time code (asks in every mode), a change that stays on
  the phone (asks in ask_all), and the kind of action a "Don't ask again" remembers per app.
- ``gate``: one per run; allow or ask for each action, with the card's title. The agent's own commit test says what
  has an effect outside the phone.
- ``exceptions``: the "Don't ask again" store: allowed apps, allow rules per app and kind of action, and links.
- ``outside``: approvals an outside agent (MCP) asks for, shown in the Mac app.

The agent loops call the gate at one place each (frontier ``_approval``, agent ``approval_kind``, the coding agents'
``PhoneTools._approval``); a run without a gate (benchmarks, ``mobster verify``) behaves exactly as before.
"""

from .gate import Decision, PermissionGate, PermissionGuard, action_from, decision_for
from .policy import (ASK_ALL, ASK_IMPORTANT, DONT_ASK, MODES, AppPolicy, Settings, effective_mode, from_env,
                     from_file, loosens)
from .rules import Action


def gate_for_run(*, override=None, origin=None, goal="", emit=None, settings=None, exceptions=None):
    """The gate for one run: the saved settings (or ``settings``), the run's override and origin, and the person's
    "Don't ask again" rules."""
    settings = settings or from_env()
    mode = effective_mode(settings.mode, override, origin)
    return PermissionGate(mode, apps=settings.apps, emit=emit, goal=goal, origin=origin, exceptions=exceptions)


__all__ = ["ASK_ALL", "ASK_IMPORTANT", "DONT_ASK", "MODES", "Action", "AppPolicy", "Decision", "PermissionGate",
           "PermissionGuard", "Settings", "action_from", "decision_for", "effective_mode", "from_env", "from_file",
           "gate_for_run", "loosens"]
