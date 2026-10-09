"""MCP tools the SOTA tracks add (seam S10). Frozen after the seams merge.

A track registers a ``ToolProvider`` from its ``register(api)``; ``ToolSet.list_tools`` appends each provider's
definitions (annotations merged) and ``ToolSet.call`` sends a name it doesn't own to the provider that lists it,
after the same validation and under the same 45 s rule as a core tool.

Rules a provider follows (the ToolSet enforces what it can):
- ``toolset`` is the ``ToolSet``. A tool that names a device checks ``toolset.allow_devices`` (``mobster mcp
  --allow-device``), and its result passes through ``toolset._mask`` (the ToolSet does this for every result).
- Files' ``put_file``, ``get_file`` and ``list_files`` need ``toolset.allow_files`` (``--allow-files``): without it
  they are not listed. The camera roll (DCIM) is never reachable from MCP.
- A run an MCP tool starts is origin "mcp" (Runtime.create(origin="mcp")): it can't start in or open an app on
  harness_api.UNATTENDED_DENY.
"""

from typing import Optional, Protocol

FILE_TOOLS = frozenset({"put_file", "get_file", "list_files"})


class ToolProvider(Protocol):
    name: str                                                    # track key
    def definitions(self, toolset) -> list: ...                  # [{"name","description","inputSchema"}]; schema rules as tools.py
    def annotations(self) -> dict: ...                           # name -> MCP annotations (READ_ONLY / ACTS / LOCAL)
    def instructions(self) -> Optional[str]: ...                 # <= 1 sentence appended to the server instructions
    def call(self, name: str, arguments: dict, call, toolset): ...  # -> ToolResult; raise tools.ToolError for plain failures


_providers: list = []


def register_provider(provider: ToolProvider) -> None:
    """Add ``provider``. ValueError when it lacks the protocol's methods or its name is taken. A tool name that
    clashes with a core tool is refused when the ToolSet lists it (``definitions`` depends on the toolset)."""
    for method in ("definitions", "annotations", "instructions", "call"):
        if not callable(getattr(provider, method, None)):
            raise ValueError(f"An MCP tool provider needs {method}()")
    name = getattr(provider, "name", None)
    if not isinstance(name, str) or not name:
        raise ValueError("An MCP tool provider has a name")
    if any(p.name == name for p in _providers):
        raise ValueError(f"An MCP tool provider named {name} is already registered")
    from .tools import CORE_TOOL_NAMES
    clashes = sorted(CORE_TOOL_NAMES & set(_static_names(provider)))
    if clashes:
        raise ValueError(f"{', '.join(clashes)} is a core Mobster tool")
    _providers.append(provider)


def _static_names(provider):
    """Tool names a provider declares without a toolset (its annotations' keys)."""
    try:
        return list((provider.annotations() or {}).keys())
    except Exception:  # noqa: BLE001
        return []


def providers() -> list:
    from .. import harness_api
    from ..api_routes import TRACK
    owners = {"files": TRACK["files"]}
    return [p for p in _providers if not harness_api.disabled(owners.get(p.name, p.name))]


def reset_for_tests() -> None:
    _providers.clear()
