"""Which requests cost nothing to run, so an estimate can say $0 and mean it: a trusted taught program or a learned path
that fits the words (the same code match the run makes first), and what the front door answers by code."""

from .programs import TRUSTED, Programs, code_fills, normalized, programs_dir
from .workflows import Learned, learned_dir, match_template


def replay_kind(data_dir, goal, *, learned=True):
    """"taught" or "replays" when a trusted no-model program fits ``goal``, else None: unknown is never free."""
    goal = normalized(goal)
    if not goal or data_dir is None:
        return None
    try:
        for path in Programs(programs_dir(data_dir)).all():
            template = (path.get("identity") or {}).get("template") or ""
            if path.get("status") == TRUSTED and path.get("taught") and len(code_fills(template, goal)) == 1 \
                    and not any(step.get("live") or step.get("decide") for step in path.get("steps") or ()):
                return "taught"
        if learned:
            for path in Learned(learned_dir(data_dir)).all():
                template = (path.get("identity") or {}).get("template") or ""
                if (match_template(template, goal) is not None or template == goal) and not path.get("needs_answer") \
                        and not any(step.get("live") for step in path.get("steps") or ()):
                    return "replays"
    except Exception:  # noqa: BLE001
        return None
    return None


def front_door(goal):
    """True when the front door answers ``goal`` by code before any model call (packs/front_door.parse)."""
    try:
        from ..packs import front_door as door
        return door.enabled() and door.parse(goal) is not None
    except Exception:  # noqa: BLE001
        return False


def free_kind(data_dir, goal):
    """"taught", "replays" or "front_door" when ``goal`` costs no model call, else None."""
    return replay_kind(data_dir, goal) or ("front_door" if front_door(goal) else None)
