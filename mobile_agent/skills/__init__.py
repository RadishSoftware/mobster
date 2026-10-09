"""Skills: operations the agent loop hands to code instead of to the model (agent_hooks.Skill).

A skill does something the model must not do itself, or can't do well: ``USE_CODE`` finds a verification code
on the phone and types it into the code field without the model ever seeing it (codes.py), and
``READ_NOTIFICATIONS`` reads Notification Center read-only, with codes masked (notifications.py).

``default_skills()`` is what frontier loads when it is given no skills (agent_hooks: a build without this
package uses none). Each skill's ``op`` is new to frontier.OPERATIONS and its ``prompt`` is one action line.
"""

from .codes import UseCode
from .notifications import ReadNotifications

__all__ = ["UseCode", "ReadNotifications", "default_skills"]


def default_skills():
    """The skills every agent run offers, in prompt order."""
    return (UseCode(), ReadNotifications())
