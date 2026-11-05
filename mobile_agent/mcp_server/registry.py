"""MCP tools the SOTA tracks add (seam S10). Frozen after the seams merge."""

from typing import Optional, Protocol

from .catalog import CORE_TOOL_NAMES

FILE_TOOLS = frozenset({"put_file", "get_file", "list_files"})


# A provider checks toolset.allow_devices for a device it names, never reads the camera roll (only pull_media does,
# with --allow-photos), and starts runs with origin "mcp", which open only apps allowed for outside agents and ask.
class ToolProvider(Protocol):
    name: str                                                    # track key
    # [{"name","description","inputSchema"}]; schema rules as tools.py
    def definitions(self, toolset) -> list: ...
    def annotations(self) -> dict: ...                           # name -> MCP annotations (READ_ONLY / ACTS / LOCAL)
    def instructions(self) -> Optional[str]: ...                 # <= 1 sentence appended to the server instructions
    # -> ToolResult; raise tools.ToolError for plain failures
    def call(self, name: str, arguments: dict, call, toolset): ...


_providers: list = []


def register_provider(provider: ToolProvider) -> None:
    for method in ("definitions", "annotations", "instructions", "call"):
        if not callable(getattr(provider, method, None)):
            raise ValueError(f"An MCP tool provider needs {method}()")
    name = getattr(provider, "name", None)
    if not isinstance(name, str) or not name:
        raise ValueError("An MCP tool provider has a name")
    if any(p.name == name for p in _providers):
        raise ValueError(f"An MCP tool provider named {name} is already registered")
    clashes = sorted(CORE_TOOL_NAMES & set(_static_names(provider)))
    if clashes:
        raise ValueError(f"{', '.join(clashes)} is a core Mobster tool")
    _providers.append(provider)


def _static_names(provider):
    try:
        return list((provider.annotations() or {}).keys())
    except Exception:  # noqa: BLE001
        return []


def providers() -> list:
    from .. import harness_api
    from ..api_routes import TRACK
    owners = {"files": TRACK["files"], "workflows": TRACK["threads"]}
    return [p for p in _providers if not harness_api.disabled(owners.get(p.name, p.name))]


def reset_for_tests() -> None:
    _providers.clear()
