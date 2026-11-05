"""The ``list_profiles`` MCP tool, and ``list_accounts``, its deprecated alias (provider ``accounts``, seam S10)."""

from ..threads.client import Client, NoService, ServiceError

NO_SERVICE = "Open Mobster or run `mobster serve` so your agent can read your profiles."
EMPTY = "The user hasn't mapped any profiles yet. They add them in Mobster, Settings › Profiles."
SCHEMA = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
TOOL = "list_profiles"
ALIAS = "list_accounts"


class AccountsProvider:
    name = "accounts"

    def __init__(self, connect=None):
        self._connect = connect or (lambda: Client.connect(origin="mcp"))

    def definitions(self, toolset):
        return [{"name": TOOL, "title": "List profiles",
                 "description": "The user's social media profiles mapped in Mobster (handle, app and the iPhone each "
                                "is signed in on). Pass one to phone_task as profile, such as "
                                "\"@getmobster on Instagram\".",
                 "inputSchema": dict(SCHEMA)},
                {"name": ALIAS, "title": "List profiles (old name)",
                 "description": "Deprecated: use list_profiles, which answers the same list. Kept so clients that "
                                "learned this name keep working.",
                 "inputSchema": dict(SCHEMA)}]

    def annotations(self):
        from ..mcp_server.tools import READ_ONLY
        return {TOOL: READ_ONLY, ALIAS: READ_ONLY}

    def instructions(self):
        return ("phone_task takes profile (from list_profiles): the task runs on that profile's iPhone, and Mobster "
                "confirms the profile on screen before it posts or sends.")

    def call(self, name, arguments, call, toolset):
        from ..mcp_server.protocol import ToolResult
        from ..mcp_server.tools import ToolError
        try:
            client = self._connect()
            answer = client.call("GET", "/api/accounts")
        except (NoService, ServiceError) as error:
            raise ToolError(NO_SERVICE if isinstance(error, NoService) else str(error)) from None
        key, field = ("accounts", "account") if name == ALIAS else ("profiles", "profile")
        rows = [{"id": a.get("id"), field: a.get("display"), "handle": "@" + str(a.get("handle")),
                 "app": (a.get("app") or {}).get("name"), "phone": a.get("deviceName") or "the default phone"}
                for a in (answer or {}).get("accounts") or () if isinstance(a, dict)]
        allow = getattr(toolset, "allow_devices", None)
        if allow is not None:
            rows = [row for row in rows if str(row["phone"]).strip().casefold() in allow]
        if not rows:
            return ToolResult(EMPTY, structured={key: []})
        lines = "\n".join(f"- {row[field]}, on {row['phone']}" for row in rows)
        return ToolResult("The user's profiles:\n" + lines, structured={key: rows})
