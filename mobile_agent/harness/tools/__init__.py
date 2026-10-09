"""The tool policy (SPEC §3.1 F7): which registered tools a Smart run offers its model.

Tracks register tools with ``harness_api.register_tool`` (READ_ATTACHMENT, PUT_FILE, RUN_ROUTINE, ASK_USER ...);
``harness_api.tools(ctx)`` builds them in order and drops an op that clashes. ``offered`` then applies the rest of
the policy, in ``engines.build_frontier``, before the frontier sees them:

- the default skills (USE_CODE, READ_NOTIFICATIONS) pass as they are;
- a tool's op is 2-31 capital letters or underscores, new, with ``prepare`` and ``perform`` and a one-line prompt
  that starts with the op ("- OP ..." or "OP ...", at most 400 characters);
- ``interactive_only`` tools only in a run a person watches (the Mac app, the terminal UI, ``mobster chat``);
- ``available(SkillContext)`` must say yes (one that raises says no);
- at most MAX_TOOLS (8) registered tools, in order: the rest are left out and logged.

A tool's prompt line goes into the prompt only when the tool is offered, so a run without tools sends exactly the
prompts it sent before.
"""

import logging
import re

from ...agent_hooks import SkillContext

log = logging.getLogger("mobster.harness")

MAX_TOOLS = 8
OP = re.compile(r"[A-Z][A-Z_]{1,30}")
PROMPT_CHARS = 400


def _default_ops():
    from ...frontier import default_skills
    return {getattr(skill, "op", None) for skill in default_skills()}


def _why_not(skill, taken, run_context, context):
    """None when ``skill`` may be offered, else a short reason for the log."""
    op = getattr(skill, "op", None)
    if not isinstance(op, str) or not OP.fullmatch(op) or op in taken:
        return "its op is invalid or taken"
    if not callable(getattr(skill, "prepare", None)) or not callable(getattr(skill, "perform", None)):
        return "it has no prepare or perform"
    raw = str(getattr(skill, "prompt", "") or "")
    prompt = raw.strip()
    prompt = prompt[2:] if prompt.startswith("- ") else prompt
    if not prompt.startswith(op) or len(prompt) > PROMPT_CHARS or "\n" in raw.strip():
        return "its prompt is not one line that starts with its op"
    if getattr(skill, "interactive_only", False) and not getattr(run_context, "interactive", False):
        return "nobody is watching this run"
    available = getattr(skill, "available", None)
    if callable(available):
        try:
            if not available(context):
                return "it isn't available here"
        except Exception:  # noqa: BLE001 -- a tool that can't say is not offered
            return "its availability check failed"
    return None


def offered(skills, *, run_context=None, driver=None, guard=None, emit=None):
    """``skills`` as the policy offers them (a tuple), the default skills first as given."""
    from ...frontier import OPERATIONS
    defaults = _default_ops()
    context = SkillContext(driver=driver, request="", app_bundle=getattr(run_context, "app_bundle", None),
                           emit=emit or (lambda event: None), guard=guard, run=run_context,
                           clarify=getattr(run_context, "clarify", None))
    out, taken, extra = [], set(OPERATIONS), 0
    for skill in skills or ():
        op = getattr(skill, "op", None)
        if op in defaults and op not in taken:
            taken.add(op)
            out.append(skill)
            continue
        why = _why_not(skill, taken, run_context, context)
        if why is None and extra >= MAX_TOOLS:
            why = f"only {MAX_TOOLS} tools are offered at once"
        if why is not None:
            log.info("tool %r not offered: %s", op, why)
            continue
        taken.add(op)
        extra += 1
        out.append(skill)
    return tuple(out)
