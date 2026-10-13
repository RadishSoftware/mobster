# Mobster engine

The agent loop behind [Mobster for Mac](https://mobster.dev), and the research we measured it with. MIT.

An agent that drives your iPhone can read your messages and tap Send. This repository is the code that decides when: the observe, decide, verify, act loop, the WebDriverAgent driver it reads and acts through, and the safety core in front of every action. Next to it are the benchmarks, the evals and the results we published, with the losses printed next to the wins.

Mobster for Mac is the product built on this loop. It sets up your iPhone, shows it live, asks for your OK on a card, runs workflows on a schedule, adds itself to Claude Code, Cursor and Codex, and runs the loop with the speed and quality work that lives in the app. [Get Mobster for Mac at mobster.dev](https://mobster.dev).

## Read the loop

| Path | What it is |
|---|---|
| `mobile_agent/frontier.py` | The loop: one model plans every step; the guards, the task contract and the DONE gate |
| `mobile_agent/contract.py` | What the task asked for, and the proof on screen that each part was done |
| `mobile_agent/engines.py` | The configuration the app and the benchmarks share (model, reasoning, limits), and the result summary |
| `mobile_agent/drivers.py`, `state.py` | WebDriverAgent's HTTP API: the accessibility tree as a snapshot, then taps, typing and swipes |
| `mobile_agent/transport.py`, `gemini.py` | The transport under the model calls. The Claude, OpenAI and Gemini clients are in `frontier.py` |
| `mobile_agent/journal.py`, `task_policy.py`, `effect_ledger.py`, `secret_filter.py`, `system_dialogs.py`, `permissions/` | The safety core: the device lock and run journal, what counts as a send, buy, post or delete, the duplicate-action guard, secret masking, iOS's own prompts, the approval gate |
| `mobile_agent/harness_api.py` | The seam to Mobster for Mac: registries and hooks, empty in this repository |
| `examples/reference_loop.py` | The loop's API in the order a run uses it |
| `mobile_agent/bench/`, `mobile_agent/evals/` | MobsterBench-iOS, the iOSWorld harness, the baselines, the paired A/B tools, VisionJudge's labelled set |
| `mobile_agent/docs/`, `research/` | Design notes and dated research write-ups |

## How one step works

1. **Observe.** The driver reads the app's accessibility tree from WebDriverAgent and turns it into a snapshot: each element's role, label, value and frame.
2. **Decide.** The model gets the task, the plan so far and the screen as numbered rows, and answers with the next 1 to 6 actions.
3. **Verify.** Before an action goes out, the loop checks it against the screen it was chosen on. After it, the loop reads the screen again and checks the action took effect.
4. **Act.** The driver taps, types or swipes, and the loop goes back to step 1 until the contract's proof is on screen or a limit stops it.

Around every step, the safety core holds:

- The loop asks before a send, a buy, a post or a delete (`task_policy.py`, `permissions/`), and an action whose outcome is unknown is never sent again (`effect_ledger.py`).
- Every event is emitted before the next action is sent. Mobster for Mac writes each one to its journal (`journal.py`), and one run holds a phone's lock at a time.
- Passwords, one-time codes, card numbers and keys are masked before anything is stored (`secret_filter.py`).
- Text read from the phone is data. It never reaches the model as an instruction.

`examples/reference_loop.py` shows the API end to end on a fixed, read-only task: reading the iOS version in Settings. You bring a WebDriverAgent runner that serves your iPhone and a Claude or OpenAI key. It answers no to every approval.

## Results

Every number names what was measured, on what, and when. The method and confidence intervals are in [`mobile_agent/docs/benchmarks.md`](mobile_agent/docs/benchmarks.md) and [`research/`](research/README.md).

| Measured | Result | Losses and gaps |
|---|---|---|
| iOSWorld `dev_core` split, this loop on Claude Sonnet 5.5 at low reasoning, simulators, 10 to 11 Oct 2026 | 53.8% official pass; agent time p50 103 s; about $0.18 a task | Multi-app tasks pass 31%. The best published result, 51.9%, is on all 133 tasks, so the two aren't directly comparable |
| iOSWorld, 16 unseen tasks, paired A/B, 28 Sep 2026 | Sonnet 5.5 50% (95% CI 28 to 72), $0.197 a task; Opus 5.5 56% (31 to 81); gpt-5.6-sol 50% | No difference between the models is significant at this size |
| MobsterBench-iOS, 68 tasks, iPhone 15 Pro over USB, 24 Sep 2026 | 54 of 66 passed (82%, CI 71 to 89); p50 7.7 s a task; $0.0057 a task | Multi-app 2 of 6. One risky tap blocked and 2 unintended actions recorded. Not held out: fixes went in between passes (63%, 71%, 82%). No baseline ran |
| Where time goes, 346 runs, 22 Sep 2026 | The device side is about 61% of wall time, the model about 36% | At least 17% is waiting for the screen to settle |
| FrameClock (the MJPEG stream as a settle clock), iPhone 15 Pro, 23 Sep 2026 | Mean navigation settle 1,122 to 996 ms with 0 premature settles in 48 | 0 of 32 premature still allows up to about 9%; Safari's pixel settle isn't measured |
| VisionJudge on 135 labelled public-domain images, 23 Sep 2026 | 1 error in 117 answers (99.1% precision) | One rater; thresholds fitted on the same set; not phone screenshots |
| The action verifier, 34 attempts, 19 Sep 2026 | Correct actions allowed 14 of 14; wrong actions blocked 12 of 12 | It let through 6 actions it should have refused; repeating an Add is still unsolved |

The September results predate 13 Oct 2026, when this loop became Mobster's only engine. The October iOSWorld numbers are this loop. The research notes describe modules that ship in Mobster for Mac (the frame clock, hedged calls, skills); their code isn't in this repository.

## Tests

The tests need no phone, simulator, network or key:

```sh
make venv
make test-backend
```

`mobile_agent/tests/test_engine_boundary.py` checks that the engine imports nothing outside its own modules, and runs the reference example on a scripted phone and model.

## Third-party software

The engine installs jsonschema and referencing (MIT) and Pillow (MIT-CMU) from PyPI. It talks to [WebDriverAgent](https://github.com/appium/WebDriverAgent) (BSD-3-Clause, Facebook, Inc.) over HTTP; this repository holds none of its code. VisionJudge's images are CC0 or public domain, credited in `mobile_agent/evals/vision/manifest.json`.

## License

MIT, copyright Radish Retail, LLC. See [LICENSE](LICENSE). Report a vulnerability as [SECURITY.md](SECURITY.md) says.
