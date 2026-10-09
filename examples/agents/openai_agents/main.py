"""Mobster's MCP server in the OpenAI Agents SDK.

    python main.py --list    list Mobster's tools (no key needed)
    python main.py           let the agent call Mobster's read-only status tool once (needs OPENAI_API_KEY)

The tool filter gives the agent only `status`, so it can't touch a simulator or a phone. Mobster's calls take up
to 50 seconds (`wait`), so the session timeout is raised from the SDK's 5-second default.
"""

import asyncio
import os
import shutil
import sys

from agents import Agent, Runner
from agents.mcp import MCPServerStdio, create_static_tool_filter


def mobster():
    command = os.environ.get("MOBSTER_BIN") or shutil.which("mobster")
    if not command:
        sys.exit("mobster isn't on PATH. Install it (curl -fsSL https://mobster.dev/install.sh | sh) or set MOBSTER_BIN.")
    return {"command": command, "args": ["mcp", "--keyless"]}


async def main(list_only):
    tool_filter = None if list_only else create_static_tool_filter(allowed_tool_names=["status"])
    async with MCPServerStdio(name="mobster", params=mobster(), cache_tools_list=True,
                              client_session_timeout_seconds=60, tool_filter=tool_filter) as server:
        if list_only:
            print(", ".join(tool.name for tool in await server.list_tools()))
            return
        agent = Agent(name="Mobster status", mcp_servers=[server],
                      instructions="Call Mobster's status tool once, then say which version runs and whether "
                                   "Smart is on.")
        result = await Runner.run(agent, "What does Mobster report?", max_turns=3)
        print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main("--list" in sys.argv))
