# Mobster

MCP server and CLI that lets Claude Code, Cursor and Codex drive a real iPhone or iOS simulator: read the screen, tap, type, and get pass/fail verdicts from accessibility assertions.

This package launches the standalone `mobster-cli` Python package through [uv](https://docs.astral.sh/uv/). It requires no Mobster desktop app. Your coding agent supplies its own model; the MCP server gives it device tools.

## Install

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and make sure `uvx` is on your `PATH`, then:

```sh
npm install -g @radishsoftware/mobster
mobster version
```

Or run without a global npm install:

```sh
npx -y @radishsoftware/mobster version
```

The launcher runs `uvx --python 3.12 --from mobster-cli==0.4.2 mobster`. On first use, uv downloads the Python package and its dependencies, and Python 3.12 if needed. Later runs reuse uv's cache. Arguments and standard input/output pass through unchanged. Missing `uvx` produces an installation link on stderr and exit status 127.

## Connect your coding agent

Add this local stdio server to your agent's MCP configuration:

```json
{
  "mcpServers": {
    "mobster": {
      "command": "npx",
      "args": ["-y", "@radishsoftware/mobster@0.4.2", "mcp"]
    }
  }
}
```

See [agent setup](https://docs.mobster.dev/mcp-server) and the [CLI reference](https://docs.mobster.dev/cli).

## Requirements

- macOS, npm and uv with `uvx` on `PATH`.
- Xcode and an iOS Simulator runtime for simulator work.
- For a real iPhone: Developer Mode, trust pairing and the device helper described in [device setup](https://docs.mobster.dev/device-setup).

Mobster controls iOS devices. It does not bypass a passcode or Face ID.

## License

MIT. [Source](https://github.com/RadishSoftware/mobster).
