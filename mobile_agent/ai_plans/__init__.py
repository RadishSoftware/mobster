"""Bring your own plan: Mobster's tasks on the person's coding agent and its subscription."""

AGENT_ENGINES = ("codex", "claude_code", "gemini")


def is_agent(engine):
    return engine in AGENT_ENGINES
