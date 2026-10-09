"""`mobster mcp`: the MCP server that coding agents use to check iOS screens.

It speaks MCP over stdio (``protocol``), offers Mobster's tools (``tools``) and keeps one run open at a
time (``session``). In key-less mode the host agent drives the app through ``tap``, ``type_text`` and
the other actions, and Mobster judges the run from the assertions declared in ``verify_start``. With an
OpenAI or Anthropic key, ``verify`` runs the steps itself (Smart). The verify and sim packages are imported
inside functions, so this package imports and tests on its own.
"""

__all__ = ["cli", "protocol", "session", "tools"]
