"""Agents under test. Build them by name; each reports its own availability."""

from .gemini_cu import GeminiComputerUseAgent
from .mobster import MobsterAgent
from .som import SetOfMarksAgent

AGENT_NAMES = ("mobster", "mobster-next", "gemini-cu", "som-pro", "som-flash")
DEFAULT_AGENTS = ("mobster", "mobster-next", "gemini-cu", "som-pro")


def build_agents(names, *, env_file=None, cu_model="gemini-3.8-flash", som_model="gemini-3.1-pro-preview",
                 som_flash_model="gemini-3.8-flash", settle=1.0, vertex=None):
    agents = {}
    for name in names:
        if name == "mobster":
            agents[name] = MobsterAgent(env_file=env_file)
        elif name == "mobster-next":
            agents[name] = MobsterAgent(env_file=env_file, features=True)
        elif name == "gemini-cu":
            agents[name] = GeminiComputerUseAgent(cu_model, vertex=vertex, settle=settle)
        elif name == "som-pro":
            agents[name] = SetOfMarksAgent(som_model, name="som-pro", vertex=vertex, settle=settle)
        elif name == "som-flash":
            agents[name] = SetOfMarksAgent(som_flash_model, name="som-flash", vertex=vertex, settle=settle)
        else:
            raise ValueError(f"unknown agent {name!r}; choose from {AGENT_NAMES}")
        agents[name].name = name
    return agents
