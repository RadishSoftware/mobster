# Mobster CLI documentation

This is the index of the documents that describe the `mobile_agent` framework: the iPhone
agent, its device layer, its local API and its benchmarks. Dates are the dates written in
each document. When two documents disagree, the later one describes the code more
accurately.

Start with [architecture.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/architecture.md) for how a run works end to end, and
[running.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/running.md) for how to start it. The [framework README](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/README.md) covers
installation, the API contract and result formats.

## Framework design

| document | what it covers | date |
| --- | --- | --- |
| [architecture.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/architecture.md) | Perception, decision layers, guards, extraction and verification, and how a run flows; module and function map; environment variables | 2026-09-24 |
| [running.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/running.md) | How to start `serve` and `run`, env files and variables, USB iPhone setup, several phones, the simulator pool, the frontier policy | 2026-09-24 |
| [benchmarks.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/benchmarks.md) | MobsterBench-iOS and iOSWorld: what each measures, commands, and measured results | 2026-09-24 |
| [compiled-intent.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/compiled-intent.md) | What runs before, instead of and after the step model: routes, answer probes, direct URL opens, dataflow plans, counts, surveys, visual answers | 2026-09-24 |
| [adaptive-architecture.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/adaptive-architecture.md) | Research-backed plan for app adaptation and release gates; the hybrid Jev + LLM control plane (appendix of 2026-09-21) | 2026-09-19 |
| [gemini-eval.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/gemini-eval.md) | How the helper model was selected; method and safeguards | 2026-09-19 |

## Device and setup

| document | what it covers | date |
| --- | --- | --- |
| [usb-wda.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/usb-wda.md) | The supported device path: a USB iPhone through WebDriverAgent; manual setup and measured behaviour | 2026-09-22 |
| [setup-api.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/setup-api.md) | The loopback setup and device API a Setup UI drives: setup actions, live video, settings, approvals, direct control, export | undated; current |
| [workflows.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/workflows.md) | Saved tasks and schedules: API contract, timing and safety | undated; current |

## Elsewhere in the repository

| document | what it covers |
| --- | --- |
| [../README.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/README.md) | Framework README: running locally, iPhone setup, compiled loops, result schemas, crash behaviour, deployment boundary |
| [../bench/README.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/bench/README.md) | MobsterBench-iOS: the pre-registered suite, baselines, metrics, decision rule and commands; `bench/iosworld.py` runs the public iOSWorld suite |
| [../../README.md](https://github.com/uninstantiated/mobster-cli/blob/main/README.md) | Project overview, requirements and current results |

## Measurements

The numbers quoted in these documents come from dated internal run reports. Each quote carries
its conditions (date, device, task count, repeats); read them before quoting a number. Raw run
folders hold phone screen text and screenshots, and are never committed.

## Writing a new document

- Put framework design notes here, and add them to the right table above.
- Name modules, functions, commands and environment variables exactly as they appear in the
  code.
- Date every measurement and link the report it comes from.
- Use placeholders for device identifiers, accounts, project IDs, local paths and network
  names: `<UDID>`, `<your-project-id>`, `$HOME/...`.
