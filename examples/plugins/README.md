# Plugins for Mobster for Mac

A Mobster plugin is a folder in the Claude Code plugin layout, so one folder works in both places
([Plugins](https://docs.mobster.dev/plugins)).

`standup-notes/` is a small one:

| File | What it adds in Mobster |
|---|---|
| `.claude-plugin/plugin.json` | Its name, version and description |
| `.mcp.json` | A connector, `notes`: the MCP reference memory server, started with `npx` (needs Node.js) |
| `skills/standup/SKILL.md` | Instructions a task gets when it asks for a standup |
| `mobster/workflows/morning-standup.json` | A workflow, saved paused |

Install it in **Settings › Plugins › Install a plugin**: choose **Folder or .zip** and give the path of
`examples/plugins/standup-notes`. Mobster shows what it adds before **Install**. A plugin in its own GitHub repository
installs with **GitHub** and `owner/repo`.
