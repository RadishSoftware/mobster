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
- **Help strings** start lowercase and have no final period. Messages say what happened, then what to do.

## Code quality

This is the bar for every change and every review. It distills Cursor's thermo-nuclear code-quality review and the principles in [pstack](https://github.com/cursor/plugins/tree/main/pstack/skills). A reviewer blocks a change that misses it unless the PR says why.

**Shape**
- **Subtract first.** Delete dead code, stale docs, redundant guards and unused flags before you add. Leave the code smaller or simpler than you found it. Build for observed use, not speculative cases.
- **Smallest diff, one source of truth.** Make each decision in one place and pass its result along. Three similar lines beat a premature abstraction.
- **Redesign, don't bolt on.** Write the change as if the requirement had been there from the start, and carry it through types, docs and examples. Look for the move that makes branches, modes or layers disappear instead of rearranging them.
- **Model the domain.** Use a state machine, typed model, registry or tagged union instead of scattered booleans and if/else chains. One more branch on an existing chain, or a second flag that must stay in sync with the first, means you skipped this. Group code by the knowledge it owns, not by execution order (load, validate, save).
- **Boundaries.** Parse and validate where data enters (arguments, config, env, files, network, model output, screen text) into typed values, then trust the types. Keep logic in pure functions and the shell thin. Don't leak wire, storage or framework types through an interface.
- **Types.** Make illegal states unrepresentable. Give ids that share a primitive their own types. Match exhaustively (`assert_never`, `const _: never`). Derive types from the schema that owns the shape. No `any`, no unchecked `as` or `cast`, no optional that hides an invariant.
- **Reader load.** Inline wrappers with one caller, pass-through layers and adapters with one implementation. Prefer returns over mutation and locals over module state, and derive values instead of syncing them. A new reader answers "where does X come from?" and "what can change X?" in 30 seconds.
- **One of each.** One way to do a task, one owner for each piece of state, one list (derive the others, or fail the build when copies disagree). Reuse the canonical helper and put logic in the layer that owns the concept. Feature checks stay out of shared paths.
- **Replace, don't wrap.** A new internal API moves every caller and deletes the old one in the same change. No compatibility shims. A flag becomes the default or is deleted.
- **Files stay under 1,000 lines.** A change that would push one past splits it first.
- **Concurrency.** Give each actor its own file, key or directory before you reach for a lock. Where one writer is a real invariant, enforce it in code (a lock, a single owner, an atomic rename), not by convention. Run independent work in parallel and apply related updates atomically.
- **Idempotent operations.** A command that changes state converges when it runs twice or after a crash at any point. Read the current state and apply the difference, write to a temporary path and rename, and leave nothing (locks, temp files, claims) the next run can't recognize as stale. Phone actions are the exception: one with an unknown outcome is never retried.
- **Experience first.** Every feature, control and option earns its place. Ship fewer features, finished, with their empty, loading and error states. The caller who imports your code and the next maintainer are users too. For a novel interaction or architecture, prototype two or three options side by side before you commit to one.
- **Comments say why, never what.** A workaround that needs a paragraph of justification is wrong code; fix the code. No commented-out code, no stray prints or `console.log`.

**Debugging**
- Reproduce first. Fix the first wrong decision, not the place it surfaced. A guard, retry, swallowed error or clamp is a fix only when the state it handles is legitimate. Fix every instance of the pattern.
- After two failed fixes on one premise, write the premise down and test it before a third.
- A failure only after a restart points at persisted state: config, caches, locks.
- If the deciding fact took digging, add the cheap signal that would have shown it at first look.

**Proof**
- Test behavior, not implementation. Assert what callers observe: returns, errors, effects on the phone, disk or network. A test fails when the behavior breaks and survives a refactor. Derive the expected value from the requirement, not from a run of the code. Delete tests that pin internals.
- A bug fix starts with a test that fails for the right reason, when a cheap one exists. No test beats a bad one (mock-heavy or timing-dependent).
- Prove it on the real artifact: run the command, read the actual value, look at the real UI. "It compiles" and an agent's report are not proof. When a check fails, suspect the observation first.
- Explain every number: give the run count, the spread and what bounds it ("why not twice as fast?"), and rule out errors, cached or skipped work and code that never ran. Say what was measured, on what, and when, before and after, losses included.
- Build the lever. For non-trivial edits or checks, write the script, codemod or generator, and commit it if the work outlives the change. Its rerun is the proof.
- A rule that keeps breaking becomes a test, lint or type, not more prose.
- Sequence verifiable units: small commits that each pass, the failing test before the fix, the deletion before the reshape. A planned migration may break only in declared places, and ends fully verified.

**Working**
- Don't wait on a person for reversible work. Decide, do it, and say why. Ask first before anything irreversible or outward: a force-push, deleting data, sending a message, or a send, post or buy on a real phone. Product direction comes from the maintainers.
- Guard the context window. Send bulk reading to subagents and keep summaries.

**Review.** Correct behavior is not enough to approve. These block unless the author justifies them: a visible simplification left on the table, a file pushed past 1,000 lines, ad-hoc branches tangling an existing flow, feature checks scattered through shared code, a wrapper, cast or optional that obscures the design, a duplicated helper or logic in the wrong layer, and complexity moved instead of deleted. Lead with structural findings. Leave a few high-conviction comments, not a list of nits. Be direct.

## Pull requests

Branch from `main`, write commit messages as plain sentences saying what changed and why, and keep pull requests small. An accepted pull request is applied to the source repository before the next export, so the export keeps it.
