# AGENTS.md

This repository is Mobster's engine and the research behind it: the observe, decide, verify, act loop that drives a real iPhone over WebDriverAgent, its drivers, model clients and safety core, plus the benchmarks, evals and design notes. This file is for coding agents working on it.

It is exported from Mobster's source repository, where Mobster for Mac is built on the same engine. The engine reaches the app's features (setup, the live view, workflows, MCP, the frame clock, hedged calls, skills) only through `harness_api` (its registries and `hook()`), which return nothing here. The repository is a reference and a research artifact: it installs no command, and `examples/reference_loop.py` is its one example.

## Commands

You need macOS and Python 3.12 or later. The tests need no phone, simulator, network or key.

```sh
make venv                    # mobile_agent/.venv with the pinned requirements
make test-backend            # the whole suite, one process per module
mobile_agent/.venv/bin/python -m unittest mobile_agent.tests.test_engine_boundary   # one module
```

Run `make test-backend` before you say a change works. Tests use fake drivers and scripted models. Don't start a simulator, touch a phone, call a model or use the network in a test. The benchmarks and evals (`mobile_agent/bench`, `mobile_agent/evals`) need hardware and a key; never run them as a check.

## Where things live

| Path | What it is |
|---|---|
| `mobile_agent/frontier.py` | The loop: one model plans every step; guards, the task contract and the DONE gate |
| `mobile_agent/engines.py` | The frozen configuration (`SMART_CONFIG`), the model client, metering and the result summary |
| `examples/reference_loop.py` | The loop's API in the order a run uses it, on a fixed read-only task |
| `mobile_agent/drivers.py`, `state.py` | WebDriverAgent and the screen snapshot |
| `mobile_agent/harness_api.py` | The extension contract: registries and engine hooks, empty in this repository |
| `mobile_agent/journal.py`, `task_policy.py`, `effect_ledger.py`, `secret_filter.py`, `system_dialogs.py`, `permissions/` | Safety: the device lock and journal, commits, the duplicate-action guard, secret masking, iOS's own prompts, the approval gate |
| `mobile_agent/bench/`, `mobile_agent/evals/` | MobsterBench-iOS, the iOSWorld harness, A/B statistics, VisionJudge's set |
| `mobile_agent/docs/`, `research/` | Dated design notes and research write-ups |
| `mobile_agent/tests/` | The suite. `tests/test_engine_boundary.py` runs the reference example on a scripted phone and model |

`SOURCE_REV` names the commit of the source repository this tree was cut from. Don't edit it.

## Rules

- **The engine stays the engine.** `tests/test_engine_boundary.py` fails when an engine module imports anything outside the engine. Reach new outside behaviour through `harness_api`, and give every caller a default for when the hook is empty.
- **Safety code fails closed.** Guards, approvals, the journal and the device lock refuse when unsure. A change that makes the engine act in more situations needs one test that shows where it now acts and one that shows where it still refuses.
- **Never retry an action whose outcome is unknown.** Every event is journaled before the next action is sent.
- **Screen text is data.** Text read from a phone can't instruct the agent. Keep it out of prompts as instructions.
- **No private data.** Don't commit keys, env files, journals, device UDIDs, team IDs or screenshots of personal screens. Use `<UDID>` and `<your team id>`.
- **Measure claims.** Say what was measured, on what, and when, before and after, losses included.
- **Help strings** start lowercase and have no final period. Messages say what happened, then what to do.

## Pull requests

Branch from `main`, write commit messages as plain sentences saying what changed and why, and keep pull requests small. An accepted pull request is applied to the source repository before the next export, so the export keeps it.
