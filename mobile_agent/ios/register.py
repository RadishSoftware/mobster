"""Track ``ios``'s registrations (tracks.load calls ``register(api)`` once per process, staged)."""


def register(api):
    """``api`` is mobile_agent.harness_api."""
    from ..mcp_server import registry as mcp_registry
    from . import mcp
    mcp_registry.register_provider(mcp.IosDocsProvider())
