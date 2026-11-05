"""What the connectors that answer MCP calls with a service's own API share (graph_api.py, slack_api.py): the tool
descriptor, the argument checks, ``Refusal``, and the session surface the connector service drives (client.Session's:
initialize, ping, list_tools, call_tool, close)."""

import json
import time

from ..outlets import net
from . import client as mcp


class Refusal:
    """The service (or the arguments) said no before anything happened: the tool's error result."""

    def __init__(self, message):
        self.message = message


def tool_spec(name, title, description, properties, required=(), read_only=True):
    return {"name": name, "title": title, "description": description,
            "inputSchema": {"type": "object", "properties": properties, "required": list(required),
                            "additionalProperties": False},
            "annotations": {"title": title, "readOnlyHint": read_only, "destructiveHint": False,
                            "openWorldHint": not read_only}}


def count_schema(top, default):
    return {"type": "integer", "minimum": 1, "maximum": top, "description": f"At most {top}; {default} if left out."}


def short(value, limit):
    return str(value)[:limit] if value is not None else None


def required_text(value, field, limit):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    if len(value) > limit:
        raise ValueError(f"{field} is longer than {limit} characters")
    return value.strip()


class ApiSession:
    """A session over a service's API."""

    NAME = TOOLS = None

    def __init__(self, token, name=None, call=None):
        self.token, self.name = token, name or self.NAME
        self.call = call or net.call
        self.ready, self.tools_changed = False, False
        self.server_info, self.instructions = {}, ""

    def list_tools(self, timeout=mcp.LIST_TIMEOUT):
        return [json.loads(json.dumps(spec)) for spec in self.TOOLS]

    def close(self):
        self.ready = False

    def call_tool(self, name, arguments, timeout=mcp.CALL_TIMEOUT):
        """The tool's result ({"content", "structuredContent"?, "isError"?}), as client.Session.call_tool."""
        if not self.ready:
            raise mcp.NotSent(f"{self.name} isn't connected.")
        method = getattr(self, f"_{name}", None) if name in {t["name"] for t in self.TOOLS} else None
        if method is None:
            raise mcp.ServerError(f"{self.name} has no tool called {name}.")
        arguments = arguments if isinstance(arguments, dict) else {}
        deadline = time.monotonic() + timeout
        try:
            found = method(arguments, deadline)
        except ValueError as error:
            found = Refusal(str(error))
        if isinstance(found, Refusal):
            return {"content": [{"type": "text", "text": found.message}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(found, ensure_ascii=False)}],
                "structuredContent": found}
