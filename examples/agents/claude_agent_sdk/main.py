"""Mobster's MCP server in the Claude Agent SDK.

    python main.py --list    list Mobster's tools (no key needed)
    python main.py           let Claude call Mobster's read-only status tool once (needs ANTHROPIC_API_KEY)

The agent gets no built-in tools and may call only `status`, so it can't touch a simulator or a phone. To let it
verify an app, add the tools you want to `allowed_tools`, such as mcp__mobster__verify_start.
"""

import asyncio
import os
import shutil
import sys

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, query


def mobster():
    command = os.environ.get("MOBSTER_BIN") or shutil.which("mobster")
    if not command:
        sys.exit("mobster isn't on PATH. Install it (curl -fsSL https://mobster.dev/install.sh | sh) or set MOBSTER_BIN.")
    return {"mobster": {"type": "stdio", "command": command, "args": ["mcp", "--keyless"]}}


async def list_tools():
    options = ClaudeAgentOptions(mcp_servers=mobster(), strict_mcp_config=True, setting_sources=[])
    async with ClaudeSDKClient(options=options) as client:
        for _ in range(60):  # stdio servers connect in the background
            (server,) = (await client.get_mcp_status())["mcpServers"]
            if server["status"] != "pending":
                break
            await asyncio.sleep(0.25)
        print(server["status"], ", ".join(tool["name"] for tool in server.get("tools", [])))


async def ask_status():
    options = ClaudeAgentOptions(
        mcp_servers=mobster(),
        tools=[],                                   # no built-in tools: no shell, no file edits
        allowed_tools=["mcp__mobster__status"],     # the one tool it may call
        permission_mode="dontAsk",                  # anything else is denied, not prompted
        strict_mcp_config=True, setting_sources=[],  # ignore your own Claude Code settings and servers
        max_turns=3, model="claude-sonnet-5-5",
        env={"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"},
    )
    prompt = "Call Mobster's status tool once. Say which Mobster version runs and whether Smart is on."
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            print(message.result)
            print(f"(cost ${message.total_cost_usd or 0:.4f})")


if __name__ == "__main__":
    asyncio.run(list_tools() if "--list" in sys.argv else ask_status())
