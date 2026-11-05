"""Track ``app_api``'s registrations (tracks.load calls ``register(api)`` once per process, staged): the endpoints' MCP
tools (mcp.AppApiProvider), one per pack reader."""


def register(api):
    """``api`` is mobile_agent.harness_api."""
    from ..mcp_server import registry as mcp_registry
    from ..packs.register import all_packs
    from .mcp import AppApiProvider
    mcp_registry.register_provider(AppApiProvider(all_packs()))
