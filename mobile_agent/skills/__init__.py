"""Skills: operations the agent loop hands to code instead of to the model (agent_hooks.Skill)."""

from .codes import UseCode
from .notifications import ReadNotifications
from .open_url import OpenUrl

__all__ = ["UseCode", "ReadNotifications", "OpenUrl", "default_skills"]


def default_skills():
    """The skills every agent run offers, in prompt order."""
    return (UseCode(), ReadNotifications(), OpenUrl())
