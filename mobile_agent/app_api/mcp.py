"""The App API's endpoints as MCP tools (provider ``app_api``, seam S10): ``<app>_<thing>`` for each endpoint
(``instagram_dms``, ``instagram_reels``, ...), with the endpoint's input and output schema, read by the same code as
``GET /v1/<app>/<thing>`` (reading.py)."""

from . import endpoints as endpoints_
from . import reading


class AppApiProvider:
    name = "app_api"

    def __init__(self, packs, profiles=None, sleep=None, client_factory=None):
        self.endpoints = {e.name: e for e in endpoints_.endpoints(packs)}
        self.profiles = profiles or reading.service_profiles("mcp")
        self.sleep = sleep
        self.client_factory = client_factory

    def definitions(self, toolset):
        return [{"name": e.name, "title": f"{e.app_name[:1].upper() + e.app_name[1:]}: {e.title}",
                 "description": e.description,
                 "inputSchema": e.input_schema, "outputSchema": e.output_schema} for e in self.endpoints.values()]

    def annotations(self):
        from ..mcp_server.protocol import ACTS
        # A read taps and swipes to reach its screen, as `tap` and `swipe` do (and opening a DM thread marks it seen),
        # so a client that asks before acting tools asks before these too.
        return {name: dict(ACTS) for name in self.endpoints}

    def instructions(self):
        return ("Tools named <app>_<thing> (such as instagram_dms) read that app on the iPhone into rows by code; "
                "read_screen reads any app's screen into the fields you name. Each is also an App API endpoint.")

    def call(self, name, arguments, call, toolset):
        from .. import harness_api
        from ..mcp_server.protocol import ToolResult
        from ..mcp_server.tools import Busy, ToolError
        endpoint = self.endpoints.get(name)
        if endpoint is None:
            raise ToolError(f"Mobster has no tool named {name[:80]}.")
        options = {"sleep": self.sleep} if self.sleep else {}
        try:
            value = reading.answer(endpoint, arguments, toolset, call=call, profiles=self.profiles,
                                   client_factory=self.client_factory, **options)
        except reading.Failure as failure:
            raise (Busy if failure.status == 409 else ToolError)(str(failure)) from None
        from ..packs import tools as pack_tools
        columns = tuple((c["key"], c["label"], c["type"]) for c in value["columns"])
        required = endpoint.pack.required.get(endpoint.thing, ()) if endpoint.pack else ()   # read_screen has no pack
        lines = "\n".join(pack_tools.row_line(i + 1, row, columns, required) for i, row in enumerate(value["rows"]))
        text = (f"{value['title']} on {value['phone'] or 'the iPhone'}: {len(value['rows'])} of {value['n']} asked"
                + (f" ({value['stopped']})" if value["stopped"] else "") + ".")
        if lines:
            text += "\n" + harness_api.fence(f"{endpoint.app_name} {value['title']}", lines)
        return ToolResult(text, structured=value)
