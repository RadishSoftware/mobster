# Mobster in your own agent

Each folder connects one agent SDK to Mobster's MCP server (`mobster mcp --keyless`) over stdio. `--list` lists Mobster's tools and needs no key. Without it, the example runs an agent that may call only `status`, the tool that reads Mobster's version and setup and changes nothing, so it can't touch a simulator or a phone. Give the agent more tools on purpose, such as `verify_start` and `verify_finish` for the verify loop.

| Folder | SDK | Install | Key for the agent run |
|---|---|---|---|
| `claude_agent_sdk/` | Claude Agent SDK (Python) | `pip install -r requirements.txt` | `ANTHROPIC_API_KEY` |
| `openai_agents/` | OpenAI Agents SDK (Python) | `pip install -r requirements.txt` | `OPENAI_API_KEY` |
| `vercel_ai/` | Vercel AI SDK (TypeScript) | `npm install` | `ANTHROPIC_API_KEY` |
| `langchain/` | LangChain with `langchain-mcp-adapters` | `pip install -r requirements.txt` | `ANTHROPIC_API_KEY` |

Each one finds `mobster` on `PATH`, or at `MOBSTER_BIN`. Use a virtual environment per example; none of these packages is a dependency of Mobster.

```sh
cd examples/agents/claude_agent_sdk
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python main.py --list
```

```
connected status, verify_start, wait, screen, tap, type_text, swipe, alert, open_url, relaunch, wait_for, verify_finish, stop, list_devices, launch_app, home, save_check, use_code, read_notifications, set_clipboard, install_app, unlock_status, unlock
```

On 7 Oct 2026 all four listed these 23 tools, with `claude-agent-sdk` 0.2.164, `openai-agents` 0.23.1, `ai` 7.0.131 with `@ai-sdk/mcp` 2.0.69, and `langchain` 1.4.3 with `langchain-mcp-adapters` 0.3.2. The Claude Agent SDK's agent run called `status` and answered "Mobster version 0.2.0 is running, and Smart is off", for $0.04. The other agent runs weren't made.

A check spans several calls on one server, from `verify_start` to `verify_finish`, so keep one MCP session open for the whole run. That is why the LangChain example loads its tools from `client.session()`: `MultiServerMCPClient.get_tools()` would start a new server for every call. The OpenAI Agents SDK times out a call after 5 seconds by default, and Mobster's `wait` takes up to 50, so the example raises it to 60.
