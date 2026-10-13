"""Agents under test. Build them by name; each reports its own availability."""

from .gemini_cu import GeminiComputerUseAgent
from .smart import SMART_MODELS, SmartAgent
from .som import SetOfMarksAgent

AGENT_NAMES = (*SMART_MODELS, "gemini-cu", "som-pro", "som-flash")
DEFAULT_AGENTS = ("mobster-smart", "gemini-cu", "som-pro")


def build_agents(names, *, env_file=None, cu_model="gemini-3.8-flash", som_model="gemini-3.1-pro-preview",
                 som_flash_model="gemini-3.8-flash", settle=1.0, vertex=None):
    agents = {}
    for name in names:
        if name == "gemini-cu":
            agents[name] = GeminiComputerUseAgent(cu_model, vertex=vertex, settle=settle)
        elif name == "som-pro":
            agents[name] = SetOfMarksAgent(som_model, name="som-pro", vertex=vertex, settle=settle)
        elif name.split("+")[0] in SMART_MODELS:
            # Mobster's agent, optionally with arm settings: "mobster-smart+stream".
            agents[name] = SmartAgent(name, env_file=env_file)
        elif name == "som-flash":
            agents[name] = SetOfMarksAgent(som_flash_model, name="som-flash", vertex=vertex, settle=settle)
        else:
            raise ValueError(f"unknown agent {name!r}; choose from {AGENT_NAMES}")
        agents[name].name = name
    return agents
