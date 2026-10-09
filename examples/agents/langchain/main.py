"""Mobster's MCP server in LangChain, through langchain-mcp-adapters.

    python main.py --list    list Mobster's tools (no key needed)
    python main.py           let a LangChain agent call Mobster's read-only status tool once (needs ANTHROPIC_API_KEY)

The tools are loaded from one session that stays open while the agent runs. `MultiServerMCPClient.get_tools()`
opens a new server for every call instead, which breaks a check: verify_start and verify_finish must reach the
same server. The agent gets only `status`, so it can't touch a simulator or a phone.
"""

import asyncio
import os
import shutil
import sys

from langchain.agents import create_agent
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools


def mobster():
    command = os.environ.get("MOBSTER_BIN") or shutil.which("mobster")
    if not command:
        sys.exit("mobster isn't on PATH. Install it (curl -fsSL https://mobster.dev/install.sh | sh) or set MOBSTER_BIN.")
    return {"mobster": {"command": command, "args": ["mcp", "--keyless"], "transport": "stdio"}}


async def main(list_only):
    client = MultiServerMCPClient(mobster())
    async with client.session("mobster") as session:
        tools = await load_mcp_tools(session)
        if list_only:
            print(", ".join(tool.name for tool in tools))
            return
        agent = create_agent("anthropic:claude-sonnet-5-5", [tool for tool in tools if tool.name == "status"])
        prompt = "Call Mobster's status tool once. Say which Mobster version runs and whether Smart is on."
        result = await agent.ainvoke({"messages": [{"role": "user", "content": prompt}]}, {"recursion_limit": 6})
        print(result["messages"][-1].content)


if __name__ == "__main__":
    asyncio.run(main("--list" in sys.argv))
