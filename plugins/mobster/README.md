# Mobster for Claude Code

This plugin gives Claude Code Mobster's MCP server, the `ios-testing` skill, and the `/mobster:verify` and `/mobster:test-pr` commands. With them, Claude Code builds your iOS app, runs it on a simulator Mobster manages or on the iPhone you set up in Mobster for Mac, taps through it and gets a verdict from Mobster. Mobster checks declared assertions. No model grades the outcome.

The MCP server comes from [Mobster for Mac](https://mobster.dev/download). The plugin holds no server: it starts the agent inside `Mobster.app`.

## Install

In Claude Code:

```text
/plugin marketplace add RadishSoftware/mobster-plugin
/plugin install mobster@mobster
```

The skill on its own, for Claude Code, Codex, Cursor and other agents that read Agent Skills:

```sh
npx skills add RadishSoftware/mobster-plugin
```

The skill needs the `mobster` MCP server in your agent. In Mobster for Mac, open Settings › Connectors and, under **Use Mobster from…**, press **Add** beside Claude Code or Codex.

## What it runs

`scripts/mobster-mcp` starts the same command that Settings › Connectors › Use Mobster from… › **Add** writes:

```sh
/Applications/Mobster.app/Contents/MacOS/mobster-agent mcp
```

It looks for `Mobster.app` in `MOBSTER_APP`, `/Applications`, `~/Applications`, then wherever Spotlight finds it. `MOBSTER_APP_PATHS`, a list of `Mobster.app` paths separated by colons, replaces that search. When the app's settings file `~/Library/Application Support/app.mobster.desktop/agent.env` exists and only you can read it, the script adds `--env-file` with that path, so Smart (`verify`) uses the model key the app uses. The key never goes into a config. Without the app, the script prints where to download it and exits 127.

`scripts/mobster-mcp --check` runs the server's own check instead: it starts the server, lists its tools and calls `status`.

## Requirements

- Mobster for Mac 0.4.0 or later, in Applications.
- A Mac with Apple silicon, Xcode and an iOS Simulator runtime.
- For a real iPhone: the phone set up in Mobster for Mac.

## What it sends

The plugin sends nothing to Mobster's servers. With Smart on, `verify_start` with `smart` sends the steps you give it and screenshots of the simulator or iPhone to your model provider, under your account with them. [Privacy](https://mobster.dev/privacy) lists every place Mobster sends data.

## Docs and support

- [Add Mobster to your agent](https://docs.mobster.dev/agents)
- [MCP server reference](https://docs.mobster.dev/mcp-server)
- [Support](https://mobster.dev/support)
