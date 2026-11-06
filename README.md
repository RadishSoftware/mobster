# Mobster

MCP server and CLI that lets Claude Code, Cursor and Codex drive a real iPhone or iOS simulator — read the screen, tap, type, and get pass/fail verdicts from accessibility assertions.

Mobster connects your coding agent to the app you are building. Run saved checks, inspect failures and keep the verdict tied to the accessibility tree. The shared runtime powers the command line and Mobster for Mac; this repository owns the CLI, MCP server, simulator tools and verification engine.

On macOS, with Python 3.12 or later:

```sh
python3.12 -m pip install mobster-cli
```

Running a device requires macOS and Xcode. The Python package runs independently of Mobster for Mac. With [uv](https://docs.astral.sh/uv/getting-started/installation/) installed, `npx @radishsoftware/mobster mcp` runs the same CLI through the npm launcher. Tests use synthetic fixtures.

| Workflow | Mobster CLI | Mobster for Mac | XCTest |
| --- | --- | --- | --- |
| Claude Code, Cursor or Codex controls a device through MCP | `mobster mcp` | Agent integration in the app | Requires an adapter |
| Inspect the screen, tap and type | CLI and MCP tools | Live view and agent tasks | Test code |
| Accessibility assertions with pass/fail reports | `mobster verify`, `mobster test` | Shared verification runtime | XCTest assertions |
| Desktop workspace and team features | Separate app extensions | Included with the app | Xcode workspace |

## Connect your agent

```sh
mobster mcp --help
mobster mcp install --help
mobster sim --help
mobster verify --help
```

Follow the [agent setup guide](https://docs.mobster.dev/agents/setup) and [testing guide](https://docs.mobster.dev/testing).

## Terminal screenshots

These are scripted demo sessions, not captures of a personal phone.

![A completed scripted task](https://raw.githubusercontent.com/RadishSoftware/mobster/cli-v0.4.1/docs/images/tui-finished.png)

![Scripted task history](https://raw.githubusercontent.com/RadishSoftware/mobster/cli-v0.4.1/docs/images/tui-history.png)

## Develop

From this source checkout:

```sh
make venv
make test-backend
```

Read [CONTRIBUTING.md](https://github.com/RadishSoftware/mobster/blob/cli-v0.4.1/CONTRIBUTING.md). Training, model weights, datasets and experiment outputs are maintained separately from the product runtime.
