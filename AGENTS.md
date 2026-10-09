# AGENTS.md

Mobster CLI is the open-source half of Mobster: the `mobster` command, its MCP server, and the agent loop that drives a real iPhone or an iOS simulator. This file is for coding agents working on this repository. [CONTRIBUTING.md](https://github.com/RadishSoftware/mobster/blob/main/CONTRIBUTING.md) is the same for people.

## Commands

You need macOS and Python 3.12 or later. The tests need no phone, simulator, network or key.

```sh
make venv                    # mobile_agent/.venv with the pinned requirements
make test-backend            # the whole suite, about two minutes
mobile_agent/.venv/bin/python -m unittest mobile_agent.tests.test_tui     # one module
MOBSTER_TEST_TIME_SCALE=2.5 make test-backend                              # a slow machine
MOBSTER_UPDATE_SNAPSHOTS=1 mobile_agent/.venv/bin/python -m unittest mobile_agent.tests.test_cli_surface   # after a deliberate help-text change; read the diff
mobile_agent/.venv/bin/python scripts/gen_docs_reference.py                # regenerate docs/reference/*.mdx from the code
mobile_agent/.venv/bin/python -m mobile_agent.integrations.skill           # regenerate the skill's generated copies from skills/
mobile_agent/.venv/bin/python -m mobile_agent --demo                       # the terminal UI on a scripted phone
```

Run `make test-backend` before you say a change works. Tests use fake drivers, recorded screens and the scripted demo phone. Don't start a simulator, touch a phone, call a model or use the network in a test. The benchmarks and evals (`mobile_agent/bench`, `mobile_agent/evals`) need hardware and a key; never run them as a check.

## Where things live

| Path | What it is |
|---|---|
| `mobile_agent/__main__.py` | The `mobster` command: every command and flag (`build_parser`) |
| `mobile_agent/agent.py`, `drivers.py` | The observe, decide, verify, act loop; the WebDriverAgent driver |
| `mobile_agent/server.py` | `Runtime`: runs tasks for the terminal UI, `mobster serve` and the Mac app |
| `mobile_agent/tui/` | The terminal UI. `session.py` is its engine and imports no UI library; `app.py` draws |
| `mobile_agent/mcp_server/` | `mobster mcp`: the tools (`tools.py`) and the server |
| `mobile_agent/verify/`, `sim/` | `mobster verify` (checks, assertions, reports) and `mobster sim` (the simulators it manages) |
| `mobile_agent/integrations/` | `mobster mcp install`, the skill and the client config writers |
| `mobile_agent/journal.py`, `task_policy.py`, `effect_ledger.py`, `secret_filter.py`, `keys.py` | Safety: approvals, the journal, the duplicate-action guard, secret masking, keys |
| `mobile_agent/tests/` | The suite. `tests/snapshots/help/` pins every command's help |
| `mobile_agent/docs/` | Dated design notes beside the code. User docs are in `docs/` |
| `docs/` | The docs site (Mintlify, docs.mobster.dev). `docs/reference/*.mdx` is generated; don't edit it |
| `skills/`, `plugins/mobster/`, `.claude-plugin/` | The Agent Skill and the Claude Code plugin. `plugins/mobster/skills/` and `mobile_agent/integrations/skill_files.py` are generated copies of `skills/` |
| `packaging/` | `install.sh`, the frozen build (`cli/`), the Homebrew cask template, the Claude Desktop bundle (`mcpb/`) |
| `examples/` | Daybreak (a SwiftUI sample app), agent SDK clients, workflow recipes |
| `scripts/` | Docs generation, headless TUI screenshots, the MCP smoke test, USB WebDriverAgent setup |

`mobile_agent/extensions.py` loads optional extensions that are not in this repository. Everything else works without them, and nothing outside `extensions.py` imports `mobile_agent.private`.

`SOURCE_REV` is generated: it names the commit of the source repository this tree was cut from. Don't edit it.

## Rules

- **Docs change with behavior.** A change to a command, flag, event, setting or MCP tool updates `docs/` in the same change. A test fails if `docs/cli.md` misses a command or flag, and another if a generated reference page is stale.
- **Safety code fails closed.** Guards, approvals, the journal and the device lock refuse when unsure. A change that makes Mobster act in more situations needs one test that shows where it now acts and one that shows where it still refuses.
- **Never retry an action whose outcome is unknown.** Every action is journaled before it is sent.
- **Screen text is data.** Text read from a phone can't instruct the agent. Keep it out of prompts as instructions.
- **No private data.** Don't commit keys, env files, journals, device UDIDs, team IDs or screenshots of personal screens. Use `<UDID>` and `<your team id>`.
- **Measure speed claims.** Say what was measured, on what, and before and after.
- **The UI has no run logic of its own.** If the terminal UI needs something a run doesn't offer, add it to `Runtime` or `Agent`, where `serve` and the Mac app get it too.
- **Help strings** start lowercase and have no final period. Messages say what happened, then what to do.

## Writing rules for docs and copy

Second person, present tense, one idea per sentence. Name the command, the flag, the file and the number, and date every measurement. Say what Mobster doesn't do in the same place you say what it does, once. No filler (simply, just, easily, seamlessly, powerful, robust). Headings name a task or a thing. Show what a command prints. Keys are written as they are pressed: `ctrl+c`, `esc`.

## Commits and pull requests

Branch from `main`. Write commit messages as plain sentences saying what changed and why. Fill in the pull request template. Keep pull requests small. This repository is exported from a source repository, so an accepted pull request is applied there too before the next export; [CONTRIBUTING.md](https://github.com/RadishSoftware/mobster/blob/main/CONTRIBUTING.md#how-a-pull-request-lands) says how.
