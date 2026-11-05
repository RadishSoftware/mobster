# Setup and troubleshooting

Mobster runs on a Mac with Apple silicon, with Xcode and an iOS Simulator runtime. The MCP server comes from Mobster for Mac: its own agent serves it, so there is nothing else to install.

## Add the server to your agent

1. Install Mobster for Mac from https://mobster.dev/download and move it to Applications.
2. Open it, then Settings › Connectors › **Use Mobster from…**.
3. Press **Add** beside Claude Code or Codex. They load the server in their next session.

**Add** writes this entry into the agent's MCP settings:

```json
{"mcpServers": {"mobster": {"command": "/Applications/Mobster.app/Contents/MacOS/mobster-agent", "args": ["mcp"]}}}
```

When the app holds a model key, it adds `"--env-file", "~/Library/Application Support/app.mobster.desktop/agent.env"` to `args`, so Smart (`verify_start` with `smart`) uses the key the app uses. The key itself never goes into the config. For another agent that runs local MCP servers, Settings › Connectors › Use Mobster from… › **Another agent** shows the same command with Copy.

## Common problems

| Symptom | Fix |
|---|---|
| No mobster tools in the client | Open Mobster for Mac, Settings › Connectors › Use Mobster from…: the row says whether that agent is added. Press **Add**, then restart the client |
| The server fails to start | Run `/Applications/Mobster.app/Contents/MacOS/mobster-agent mcp doctor`; it shows where it fails |
| Mobster refuses to add itself | macOS runs the app from a temporary copy (opened from Downloads or the disk image). Move it to Applications, open it from there, then add it again |
| `verify_start` stays `preparing` | The first run boots the simulator and builds WebDriverAgent (about a minute). Keep calling `wait` |
| `couldnt_run`, class `environment` | Read `reason`; `status` says what to fix |
| A phone is `not_allowed` | The user kept that phone off limits. Ask them |
| The phone is locked | Ask the user to unlock it. Mobster never enters a passcode |
| `verify_start` refuses `smart` | Smart needs the user's own model key: add it in Mobster for Mac, Settings › Models, then press **Add** again |

Docs: https://docs.mobster.dev/mcp-server
