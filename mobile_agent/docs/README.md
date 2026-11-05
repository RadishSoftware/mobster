# Mobster design notes

The design notes for the `mobile_agent` framework: the iPhone agent, its device layer, its local API and its
benchmarks. To use Mobster (install it, connect a phone, run the terminal UI or the command line), start with the user
docs in [docs/](../../docs/). Each note carries the date it was written. When two notes disagree, the later one is closer
to the code.

Start with [architecture.md](architecture.md) for how a run works end to end, and [running.md](running.md) for how to
start one. The [framework README](../README.md) covers installation, the API contract and result formats.

## Framework design

| Note | What it covers | Date |
| --- | --- | --- |
| [architecture.md](architecture.md) | Perception, Mobster's agent, guards, the task contract and the DONE gate, and how a run flows; module and function map; environment variables | 2026-10-13 |
| [running.md](running.md) | How to start `serve` and `run`, env files and variables, USB iPhone setup, several phones, the simulator pool, Mobster's agent and its switches | 2026-10-13 |
| [benchmarks.md](benchmarks.md) | MobsterBench-iOS and iOSWorld: what each measures, commands, and measured results | 2026-09-24 |
| [realbench.md](realbench.md) | MobsterBench Real's pre-registration: the real-customer tasks, keys, graders, metrics, ABBA protocol and the outside-agent comparison | 2026-10-11 |
| [replay-core.md](replay-core.md) | Record a passing run's steps, replay them with no model, heal a changed step as a diff: matching, heals, the circuit breaker, the cache and its keys, measurements | 2026-10-09 |
| [traces.md](traces.md) | Experimental: record a run's tape, export it as a masked bundle, replay it offline | 2026-10-18 |
| [engine-contract.md](engine-contract.md) | What the agent's API and run events say about the engine, for the dashboard and any other client: engines, estimates, outcomes, proof, approvals | undated; current |
| [harness.md](harness.md) | Steering a running task, questions, picking up after an interruption and prewarm, none of which approves an action | undated; current |
| [longform.md](longform.md) | Long tasks: budgets, the plan, the notebook, the report and the routes, and the feed track's reading of lists | 2026-10-08 |

## Features

| Note | What it covers | Date |
| --- | --- | --- |
| [app-api.md](app-api.md) | The App API: an app on the iPhone read into rows by code, as REST routes and MCP tools; its safety and the Mirroring measurement | 2026-10-14 |
| [app-xray.md](app-xray.md) | App X-ray: Mobster explores an app with no model and returns a map of its screens; what it taps and refuses, measured on Settings | shipped 2026-10-15 |
| [corpus.md](corpus.md) | The opt-in run corpus (dark): the setting, exactly what is stored with a sample record and what is not, where it lives | 2026-10-26 |
| [net-xray.md](net-xray.md) | Network X-ray: who each app on the iPhone talks to, from hostnames, sizes and times | shipped 2026-10-15 |
| [attach.md](attach.md) | WebView DOM, Flutter semantics and the per-app automation score, dark behind `MOBSTER_EXPERIMENTAL_ATTACH=1`; the inspector seam, the removed bridge and the simulator proof plan | 2026-11-03 |

## Device and setup

| Note | What it covers | Date |
| --- | --- | --- |
| [usb-wda.md](usb-wda.md) | The supported device path: a USB iPhone through WebDriverAgent; manual setup and measured behaviour | 2026-09-22 |
| [setup-api.md](setup-api.md) | The loopback setup and device API a Setup UI drives: setup actions, live video, settings, approvals, direct control, export | undated; current |
| [workflows.md](workflows.md) | Saved tasks and schedules: API contract, timing and safety | undated; current |

## Elsewhere in the repository

| Document | What it covers |
| --- | --- |
| [../README.md](../README.md) | The framework README: running locally, iPhone setup, result schemas, crash behaviour, how the loop works, deployment boundary |
| [../bench/README.md](../bench/README.md) | MobsterBench-iOS: the pre-registered suite, baselines, metrics, decision rule and commands; `bench/iosworld.py` runs the public iOSWorld suite |
| [../../README.md](../../README.md) | The project overview, requirements and current results |
| [../../research/](../../research/) | Dated research notes behind the engine |

## Measurements

The numbers in these notes come from dated internal run reports. Each quote carries its conditions (date, device, task
count, repeats); read them before you quote a number. Raw run folders hold phone screen text and screenshots, and are
never committed.

## Write a note

- Put a framework design note here, named for its topic, and add it to the right table above.
- Name modules, functions, commands and environment variables exactly as the code does.
- Date every measurement and link the report it comes from.
- Use placeholders for device identifiers, accounts, project IDs, local paths and network names: `<UDID>`,
  `<your-project-id>`, `$HOME/...`.
- Delete a note once its decision lives in the code and the user docs.
