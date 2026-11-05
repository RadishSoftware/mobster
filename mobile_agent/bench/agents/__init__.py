"""Agents under test, built by name; each reports its own availability."""

from .smart import SMART_MODELS, SmartAgent

AGENT_NAMES = tuple(SMART_MODELS)


def build_agents(names, *, env_file=None):
    return {name: SmartAgent(name, env_file=env_file) for name in names}
