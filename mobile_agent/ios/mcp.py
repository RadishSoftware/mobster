"""The iOS guide over MCP (``ios_docs``): answers from the local knowledge base, reads no phone and changes nothing."""

from . import kb

TOOL = "ios_docs"


class IosDocsProvider:
    name = "ios"

    def definitions(self, toolset):
        return [{"name": TOOL, "title": "Look up how iOS works",
                 "description": ("How iOS itself works, from Mobster's iPhone guide for iOS 26 and 18: the App "
                                 "Library, Spotlight, where a setting is in Settings, Control Center, the keyboard, "
                                 "iOS's alerts. Local; reads no phone."),
                 "inputSchema": {"type": "object", "additionalProperties": False, "required": ["query"],
                                 "properties": {
                                     "query": {"type": "string", "minLength": 2, "maxLength": 300,
                                               "description": "What to look up, in plain words."},
                                     "ios_version": {"type": "string", "maxLength": 20,
                                                     "description": "The iPhone's iOS version (\"26.0.1\", "
                                                                    "\"18.4\"). Leave it out for the newest."},
                                     "limit": {"type": "integer", "minimum": 1, "maximum": kb.MAX_K,
                                               "description": "How many entries (default 4)."}}}}]

    def annotations(self):
        from ..mcp_server.tools import READ_ONLY
        return {TOOL: dict(READ_ONLY)}

    def instructions(self):
        return "For how iOS itself works (Settings paths, the App Library, system alerts), ask ios_docs first."

    def call(self, name, arguments, call, toolset):
        from ..mcp_server.protocol import ToolResult
        from ..mcp_server.tools import ToolError
        if name != TOOL:
            raise ToolError(f"Mobster has no tool named {name[:80]}.")
        query = " ".join(str(arguments.get("query") or "").split())
        if len(query) < 2:
            raise ToolError("ios_docs needs a query of at least 2 characters.")
        version = arguments.get("ios_version")
        found = kb.query(query, version, arguments.get("limit") or 4)
        return ToolResult(kb.answer(query, version, arguments.get("limit") or 4),
                          structured={"iosVersion": kb.known(version), "entries": [e.public() for e in found]})
