# Benchmarks

Mobster is measured on two benchmarks. They answer different questions.

- **MobsterBench-iOS** (`mobile_agent/bench/`) is this project's pre-registered
  suite. It runs on a real iPhone over USB, with stock Apple apps and a few stable
  web pages, and is graded by oracles that never read the agent's own claims. It
  asks: is Mobster more accurate and faster than strong screenshot baselines on
  the same phone?
- **iOSWorld** (`mobile_agent/bench/iosworld.py`) is a published third-party
  benchmark: 133 tasks over 26 synthetic SwiftUI apps on a simulator, scored by
  its own LLM judge. It asks: how does Mobster compare with published agents on
  unfamiliar apps and multi-step tasks?

Run output is local. `research/bench-runs/*/` and `research/iosworld-runs/` are
git-ignored because they hold screenshots and screen text. The run reports
quoted below are internal and not part of this repository; the numbers are
copied here with their conditions.

All commands assume the repository root and `PY=mobile_agent/.venv/bin/python`.
Setup of the phone and simulators is in [running.md](https://github.com/RadishSoftware/mobster/blob/main/mobile_agent/docs/running.md).

## MobsterBench-iOS

### The suite

`bench/suite.py` defines 68 tasks, `SUITE_NAME = "MobsterBench-iOS"`,
`SUITE_VERSION = "1.0"`. They are frozen in `bench/manifest.json`:

- SHA-256 `7a26c2f2ef7280d55a872c8d960c12240d13927e44caafcf6e799d7d68dd019f`,
  frozen on 2026-09-23.
- The hash covers tasks, checks, budgets, fixture labels, image digests and the
  decision rule.
- `run` refuses a suite whose hash differs, unless you pass `--allow-unfrozen`
  (development only).
- To change a task, bump `SUITE_VERSION` and run `freeze --force`. `freeze`
  refuses to overwrite a different hash without `--force`.

```sh
$PY -m mobile_agent.bench list          # prints the live hash and whether it matches manifest.json
$PY -m mobile_agent.bench list -v       # every task: id, category, safety class, budgets, goal
```

| category | tasks | budget (steps / seconds) |
| --- | ---: | --- |
| navigation | 10 | 15 / 90 |
| retrieval (2 abstention) | 10 | 20 / 120 |
| settings_state | 9 | 20 / 120 |
| text_entry | 8 | 20 / 150 |
| multi_app | 6 | 35 / 300 |
| web (1 abstention) | 10 | 25 / 180 |
| scroll | 7 | 25 / 180 |
| visual (dry run, 1 abstention) | 8 | 80 / 480 |

The safety classes are 41 `read_only`, 19 `read_only_history` and 8
`reversible_reset`. No task sends, buys, posts, deletes, likes or calls anything,
and `bench/tests/test_suite.py` checks every goal for this. Which benchmark each
category mirrors, and the full grading design, are in
[`bench/README.md`](https://github.com/RadishSoftware/mobster/blob/main/mobile_agent/bench/README.md).

### Ground truth and fixtures

Answers are graded against four kinds of truth (`bench/checks.py`,
`bench/truth.py`):

- `Literal`: public facts written in the task.
- `Captured`: Wi-Fi, Bluetooth and Airplane Mode state, read at each reset.
- `Fixture`: records and labelled images you install.
- `Truth`: per-device values in `bench/truth/<device>.json`.

A task whose truth is missing is **ungraded**, never passed.

Fixtures are set up once, by hand:

```sh
$PY -m mobile_agent.bench fixtures --out <folder on the Mac>
```

This writes `copy-to-phone/MobsterBench/` (44 CC0 or public-domain images,
renamed so that a filename never reveals its label), `SETUP.txt`, and
`labels-do-not-copy.json`, the answer key, which stays on the Mac. The records
are all fictional: a note with a locker code, the contact "Bench Tester" at
"Mobster Bench Labs" with a 555-01xx number, a reminders list and a calendar
event. `bench/README.md` has the exact contents. Verify them read-only:

```sh
$PY -m mobile_agent.bench dry-plan --probe --verify-fixtures
```

With `run --verify-fixtures`, a task whose fixture is missing is **skipped**
instead of failed.

### Agents

`bench/agents/__init__.py` defines `AGENT_NAMES`:

| agent | what it is |
| --- | --- |
| `mobster` | The current pipeline, in process, run cold: no replay store, no FrameClock, no VisionJudge. |
| `mobster-next` | The same, with FrameClock in `on` mode and the VisionJudge for compiled visual loops. |
| `gemini-cu` | Gemini computer use (`ENVIRONMENT_MOBILE`) on Vertex. `--cu-model auto` tries 3.8, 3.7, 3.6, then 3.5 Flash. |
| `som-pro`, `som-flash` | A frontier VLM with Set-of-Marks over the same AX boxes, with an M3A-style action space. |

`DEFAULT_AGENTS` is `mobster,mobster-next,gemini-cu,som-pro`. Every agent gets the
same goal text, budgets and safety monitor (`bench/safety.py`), which checks each
action before dispatch. The baselines act through `bench/phone.py`, which uses
the same WDA gesture primitives as Mobster's own driver. The baselines use
Vertex AI, so they need `GOOGLE_CLOUD_PROJECT`, optionally
`GOOGLE_CLOUD_LOCATION`, and a `gcloud` login. The Mobster agents need
`TYPESAFE_API_KEY` and the helper variables (see
[running.md](https://github.com/RadishSoftware/mobster/blob/main/mobile_agent/docs/running.md#configuration)).

### Commands

The bench loads `--env-file`, which defaults to the desktop app's
`$HOME/Library/Application Support/app.mobster.desktop/agent.env`. It never
prints a key, token or project id.

```sh
# Offline: suite hash, truth coverage, fixtures, agent availability, time and cost estimate
$PY -m mobile_agent.bench dry-plan
$PY -m mobile_agent.bench dry-plan --probe-models        # + one tiny Vertex call per baseline model

# Once per phone, phone free and unlocked (read-only navigation by the harness)
$PY -m mobile_agent.bench capture-truth --wda-url http://127.0.0.1:8100 --device iphone15pro
$PY -m mobile_agent.bench dry-plan --probe --verify-fixtures

# Smoke: two read-only tasks, one repeat
$PY -m mobile_agent.bench run --only web.heading,nav.accessibility --repeats 1 \
    --out research/bench-runs/smoke-$(date +%F)

# Full run (default agents, 3 repeats), resume after a halt, then the report
$PY -m mobile_agent.bench run --repeats 3 --verify-fixtures --out research/bench-runs/full-$(date +%F)
$PY -m mobile_agent.bench run --resume research/bench-runs/full-YYYY-MM-DD
$PY -m mobile_agent.bench report --run research/bench-runs/full-YYYY-MM-DD
```

`dry-plan`'s time and cost estimates are priors (`PRIOR_*` in
`bench/__main__.py`), not measurements. Other `run` flags:

- `--agents` selects agents.
- `--only` takes task ids or category names.
- `--seed` fixes the order.
- `--max-attempts` stops early (for smoke tests).
- `--settle` is the baselines' fixed post-action settle (default 1.0 s).
- `--yield-to` (default `http://127.0.0.1:8765`) or `--no-yield` controls
  yielding to a running app.

The runner (`bench/runner.py`) follows these rules:

- Before each attempt it health-checks WDA (GET only). A locked phone halts the
  run, and so do 2 consecutive infrastructure failures.
- It waits while the Mobster app has a queued or running task, and only ever
  sends GET to it.
- It takes the same device lease as `serve`.
- It resets to the task's start state.
- It never creates a WDA session or restarts the runner.

Each run folder holds `plan.json` (seed, order, agent configs) and
`records.jsonl` (one fsynced line per attempt). `report` writes
`research/bench-<today>.md` unless `--out` is given.

### Reading the numbers

- **Attempt accounting.** Every attempt is pass, fail, ungraded, skipped or
  infra. Only pass and fail enter the rates. The others are counted and shown.
- **Success rate.** Passes divided by graded attempts, with a Wilson 95% CI over
  attempts, and a bootstrap CI over tasks, because repeats of one task are not
  independent.
- **pass^k.** tau-bench's pass^k (`metrics.pass_hat_k`) is the probability that k
  attempts at a task all pass, averaged over tasks. pass^3 needs at least 3
  repeats. With 1 repeat, pass^1 equals the success rate and pass^3 is blank.
- **Time.** Agent wall clock from start to result, minus harness-only reads
  (`monitor_ms`) and provider throttling (`throttle_ms`). Resets and grading are
  never timed. Per-step time is agent time divided by decision steps.
- **Cost.** Mobster's cost comes from its own inference telemetry (`costs.py`).
  Baselines use Vertex standard global rates (`bench/vertex.py`, read
  2026-09-23). Unpriced calls are counted, never treated as zero.
- **Safety.** Unsafe or risky actions the monitor blocked, and unintended actions.
  Both must be 0 for a state-of-the-art claim.
- **Verdict (pre-registered).** For each baseline, per-task success is paired, and
  the 95% bootstrap CI of the mean difference decides: "better" if the lower bound
  is above 0, "not worse" if it is above −5 pp. Speed is the median per-task time
  ratio on tasks both agents solved, and counts as "faster" if the CI's upper
  bound is below 1. "State of the art" needs better or not-worse accuracy,
  faster, and zero unsafe attempts against **every** baseline that ran.

The verdict is keyed to the agent named `mobster` (`report.MOBSTER`). A run of
`mobster-next` alone therefore ends with "No graded Mobster attempts: no
verdict." That means no comparison was made. It is not a failure.

### What the "pass" runs are

The `mb-pass1` to `mb-pass6` runs are six successive development passes of
`mobster-next` alone, one repeat each, run on 2026-09-24 on one iPhone 15 Pro
over USB. Code changed between passes (commit `d1c8418` plus uncommitted
changes), and failures from one pass guided the fixes before the next. "Pass N"
is an iteration number, not a pass count. Full reports exist for passes 4 to 6
(internal).

### Measured results (2026-09-24)

| pass | pass / graded | success | Wilson 95% CI | ungraded | infra | task p50 | $ / task |
| --- | ---: | ---: | --- | ---: | ---: | ---: | ---: |
| 4 | 42 / 67 | 63% | 51–73% | 1 | 0 | 6.6 s | $0.0059 |
| 5 | 48 / 68 | 71% | 59–80% | 0 | 2 | 7.1 s | $0.0060 |
| 6 | 54 / 66 | 82% | 71–89% | 2 | 0 | 7.7 s | $0.0057 |

The earlier passes are recorded only in the local, uncommitted run folders,
which show infrastructure trouble: pass 1 halted on two consecutive WDA
failures, pass 2 skipped 16 attempts whose fixtures were not yet in place, and
pass 3 graded 40 passes and 27 fails.

Pass 6 in detail:

- **By category:** navigation 10/10, scroll 7/7, retrieval 9/10, web 9/10,
  visual 6/8, text_entry 6/8, settings_state 5/7, multi_app 2/6.
- **Speed:** task p50 7.7 s, p90 17.1 s. Step p50 1.7 s. First action p50 0.8 s.
  8.4 model calls per task. $0.38 for the whole run.
- **Safety:** one risky tap was blocked ("Clear text" in
  `multi.contact_state`), and 2 unintended actions were recorded. So pass 6
  would not meet the zero-unsafe bar even if baselines had run.
- **Visual dry runs:** item precision 94% and recall 100% (17 TP, 1 FP, 0 FN).
  All 4 "unsure" eye images were correctly abstained, and all 4 abstention
  tasks were correct.
- **Failures:** 4 of the 6 multi-app tasks, both receipt-image tasks,
  `text.calc_add` (the Calculator display is drawn, not in the AX tree; the task
  is tagged `known_gap_mobster_ax_drawn_display`), `text.notes_search`,
  `ret.note_code`, `state.auto_lock`, `state.wifi`, and `web.apollo_landing`.
- **Ungraded:** two of the three `Captured` settings tasks (`state.airplane`,
  `state.bluetooth`).

These numbers need to be read with their limits:

- **No baseline has run yet.** The pre-registered comparison, and so any
  state-of-the-art claim on this suite, has not been made.
- **The passes were not held out.** The same 68 tasks were used to find and fix
  failures between passes. The rise from 63% to 82% is partly fitting to this
  suite. A fresh 3-repeat run on frozen code is the number to quote.
- **One repeat per pass.** Flakiness and pass^3 are unmeasured.
- **Narrow scope.** One phone, one day, stock Apple apps and a few web pages.

## iOSWorld

### What it is

iOSWorld (arXiv 2606.09764, github.com/ljang0/iOSWorld, CC BY 4.0) has 133 tasks
over 26 SwiftUI apps built for one fictional persona. The tasks run on an iPhone
17 Pro simulator and are scored by an LLM judge from each run's trajectory:
per-step screenshots, actions and the final answer, against the task's rubric.
Its default judge model is `gpt-5.4-mini`. A task's score is the fraction of
rubric criteria met, and it passes only when every criterion is met.

`bench/iosworld.py` runs Mobster on iOSWorld's own terms. These parts are
iOSWorld's and are unchanged:

- The tasks, with goals verbatim from `tasks.json`.
- The start state: iOSWorld's own `reset_app_data` and `reseed_apps` from its
  `scripts/appium_agent.py`, then Home, as its runner does per task.
- The 50-step limit (`MAX_STEPS`).
- The trajectory format and the judge (`scripts/judge_trajectories.py`, run
  unmodified).

These parts are Mobster's:

- The agent, driving the simulator through WDA.
- The task's app list becomes Mobster's allowed apps, which is the same list
  iOSWorld gives its own agent. The first app is opened and recorded as a
  `launch_app` step.
- A 900 s time limit per task (`MAX_SECONDS`).

Screenshots exist only for the judge. They are taken with `xcrun simctl io`, never
through WDA and never shown to the agent, and their time is subtracted from the
reported agent seconds.

### Bootstrap

iOSWorld is a separate checkout (the commands take `--repo <path to iOSWorld>`).
Follow its README to create the simulator and run
`./iphone/bootstrap/bootstrap_ios_apps.sh`, which builds and installs the 26 apps
and writes `.app_manifest.json`. The harness reads that file
(`load_manifest`) for bundle ids and app paths, and raises `FileNotFoundError`
if it is missing.

To keep the bootstrap windowless, boot with `xcrun simctl boot <UDID>` instead of
`open -a Simulator`, and pass the bootstrap's `--no-open-simulator` flag. In the
iOSWorld revision checked on 2026-09-24, the bootstrap's final "return to home
screen" step still calls `open -a Simulator` and AppleScript unconditionally
(both are allowed to fail). Its permission-alert clicks also need the Simulator
window. The Mobster harness depends on neither. Before each task it accepts any
pending system alert over WDA (`clear_alerts`, via `/alert/text` and
`/alert/accept`) and presses Home over WDA. Nothing in `iosworld.py` opens a
window.

For a pool, create each simulator fresh (`xcrun simctl create`, same device type
and runtime) and run the bootstrap on each one (`--device "<its name>"`). Do not
use `simctl clone` or iOSWorld's `scripts/create_sim_pool.py`: a clone copies the
source's LaunchServices store (`data/var/db/lsd/*.csstore`), which keeps the
source's absolute container paths. Every clone then runs the apps out of the
source simulator's data containers, and one worker's between-task reset wipes the
data another worker is using mid-task (found 26 Sep 2026: three clones on the
first simulator's containers, so every earlier pooled run is suspect). `run` and
`pool` refuse such a simulator before any task (`check_isolation`: every app's
data container must lie under that simulator's own device folder). Then start one
WDA runner per simulator on its own WDA and MJPEG ports. Starting those runners
is not automated in this repository.

iOSWorld's apps read `OPENAI_API_KEY` for their in-app replies (its runner writes
it into the apps' UserDefaults). Its judge reads it from the environment, so put
it in the env file you pass with `--env-file`.

### Commands

```sh
# One simulator
$PY -m mobile_agent.bench.iosworld run --repo <iOSWorld> --udid <UDID> \
    --wda-url http://127.0.0.1:8200 --mjpeg-url http://127.0.0.1:9200 \
    --out research/iosworld-runs/NAME --env-file <agent env file> \
    [--only caltrack-001,mail-002] [--limit N] [--policy frontier --model gpt-5.6-terra --reasoning low]

# A pool: tasks dealt round-robin, one `run` subprocess per simulator
$PY -m mobile_agent.bench.iosworld pool --repo <iOSWorld> \
    --sims <UDID1>:8200:9200,<UDID2>:8201:9201 --out research/iosworld-runs/NAME --env-file <agent env file>

# iOSWorld's own judge, then the summary
$PY -m mobile_agent.bench.iosworld judge --repo <iOSWorld> --run research/iosworld-runs/NAME --env-file <agent env file>
$PY -m mobile_agent.bench.iosworld report --run research/iosworld-runs/NAME

# Offline, $0: a paired A/B, and the pooled full-mix estimate over any runs
$PY -m mobile_agent.bench.iosworld compare --a research/iosworld-runs/OLD-* --b research/iosworld-runs/NEW-*
$PY -m mobile_agent.bench.iosworld estimate 'research/iosworld-runs/*' --model gpt-5.6-sol
```

- Each task writes `NNN-<task>/trajectory.json`, `task.json` and `screens/`. The
  number follows the task's position in iOSWorld's full `tasks.json`.
- `run` skips a task whose `task.json` exists, unless you pass `--force`, so a
  stopped run can be restarted with the same command.
- `task.json` records the agent status, answer, agent seconds, screenshot
  seconds, reset seconds, cost, model calls, policy, model, and the last 400
  trace events.
- `judge` runs iOSWorld's `scripts/judge_trajectories.py --run-dir` in iOSWorld's
  own `.venv` if one exists (`--force` passes the judge's `--re-judge`). The
  judge's evaluation lands in each `task.json`, which is what `report` reads.
  After the judge's own summary, it prints the official passes and any runs where
  the judge's `success` flag disagrees.
- `report` prints the official passes (see below), the mean rubric score, the
  judge's `success` flag where it disagrees with the official rule, passes by
  category and difficulty, agent seconds (p50 and mean), and dollars per task.

### Scoring, paired A/Bs and pooled estimates

`bench/iosworld_stats.py` does all of this offline from the run folders. It calls
no model and no simulator.

**The official pass rule.** iOSWorld's `judge_trajectories.py` (`_aggregate`)
counts a task as passed only when every rubric criterion is satisfied. It falls
back to the judge's `success` flag only when an evaluation has no rubric results.
The flag is the judge's overall opinion, and it is not the rule. Until 26 Sep
2026, `report` counted the flag. On A/B round 2 (ab6-a2, ab6-b2) the two
disagreed on 4 of 32 runs, in both directions. `report`, `judge`, `compare`,
`estimate` and `bench/diagnose.py` now all count passes by the official rule,
and show the flag separately wherever it disagrees. A test checks the rule
against iOSWorld's own `_aggregate` when an iOSWorld checkout is present
(`IOSWORLD_REPO`, or `../iOSWorld`).

**`compare`** takes two sets of run folders (globs allowed) and pairs them by
task. It prints:

- per task and arm: official passes, mean rubric score, calls, and the mechanism
  counts;
- per arm: official passes, the judge flag, the mean rubric score with its
  standard error (clustered by task), and how many tasks got the same verdict on
  every run of the same build;
- the paired B - A difference in pass rate and in rubric score, averaged over
  tasks, each with its standard error and a 95% bootstrap CI (tasks resampled,
  10,000 draws, seed 20260926);
- tasks that worker logs show were never run (`NOT RUN`, a failed reset) and
  workers that died with a traceback.

`--json` prints everything. `--model` keeps one model's runs only.

The mechanism counts are the primary A/B metrics of the 26 Sep research. Pass rate
is a regression guard. Counts marked ~ in the output are approximations:

| count | source | exact? |
|---|---|---|
| refusals of required commits | `frontier_refused` events from the commit guard, where the task asks for the act | ~ lenient: a noun ("my recent order") counts as asking, so this is an upper bound |
| all guard refusals | the same events, whether or not the act was asked for | exact |
| unrequested commits (must be 0) | executed TAP or LONG_PRESS on a commit-class control (`COMMIT_CONTROL`) the task does not ask for | ~ lower bound, for the same reason; Return-key commits (SUBMIT) are not seen |
| DONE-deferral turns | `frontier_chunk_stop` with reason `verify_before_done` | exact |
| false completion claims | runs that ended DONE ("completed") yet failed the official rule | ~ upper bound: it also counts judge misses and wrong answers, which only a person reading the frames can tell apart |
| runs ended by a WDA crash | status `error` with a transport failure (connection refused or reset) as the reason | exact |
| calls per task | `model_calls` in `task.json`, else the lines of `prompts.jsonl` | exact |
| revisit turns | turns back on a screen seen earlier in the run, other than the turn just before | ~ a screen is the role and label of its first 40 elements, so values are ignored |
| same-screen turns | turns on the same screen as the turn before | ~ as above; the research's `revisit.py` counted these and revisits together |

"The task asks for the act" means the goal or the rubric criteria use the
control's act or a listed synonym (`commit_requested`). A bare "Confirm" counts
as asked for when the request asks for any other commit. `--commit-labels FILE`
(JSON `{task: {control label: true|false}}`) overrides this with hand labels.
The event counts come from the last 400 events that `task.json` keeps. `compare`
warns about any run that reached that cap.

**Reproduced: A/B round 1 and 2 (main against sol-fixes, 16 tasks, 2 runs each,
`gpt-5.6-sol`).**

```sh
$PY -m mobile_agent.bench.iosworld compare --a-name main --b-name fixes \
    --a research/iosworld-runs/ab6-a1 research/iosworld-runs/ab6-a2 \
    --b research/iosworld-runs/ab6-b1 research/iosworld-runs/ab6-b2
```

| 32 runs each | main (a) | sol-fixes (b) |
|---|---|---|
| official passes | **19/32** | **19/32** |
| judge `success` flag (the old count) | 20/32 | 16/32 |
| mean rubric score (SE by task) | 0.863 (0.044) | 0.853 (0.042) |
| same verdict on both runs of a task | 11/16 | 11/16 |
| refusals of required commits ~ | 27 | 36 |
| all guard refusals | 28 | 37 |
| unrequested commits ~ | 0 | 0 |
| DONE-deferral turns | 60 | 35 |
| false completion claims (upper bound) ~ | 8 | 9 |
| runs ended by a WDA crash | 1 | 1 |
| calls per task | 27.1 | 26.0 |
| revisit turns ~ | 124 | 95 |

Paired over the 16 tasks, b - a:

- Pass rate: +0.0 points, SE 9.1, 95% bootstrap CI -18.8 to +18.8.
- Rubric score: -0.011, SE 0.021, 95% bootstrap CI -0.050 to +0.028.

So the builds tie under the official rule, not 20 against 16. The same build got
the same verdict on only 22 of 32 (task, build) pairs across the two rounds. A
16-task, 2-run paired A/B has a pass-rate SE of about 9 points, which catches a
regression but cannot show a 5-point gain. The paired rubric difference is about
four times less noisy (SE 0.02). Of the 65 guard refusals, 63 were required
("Post comment" 52 times in multi-083, the checkout confirm toggle 11 times in
multi-088). The other two, "Reset" and "Delete", were correctly refused.

**`estimate`** pools any set of runs into one official pass rate, reweighted to
iOSWorld's category mix: 27 single-app, 60 multi-app and 46 memory tasks of 133
(or the mix in `--repo`'s `tasks.json`). Within a category it averages each
task's pass rate (`task` weighting, which is right when tasks have different
numbers of runs), or pools runs (`run` weighting, as the 26 Sep report did). It
prints both by default. The 95% CI is a stratified cluster bootstrap: tasks are
resampled within each category, and a drawn task brings all of its runs. It is
wider than a binomial interval over runs, because runs of the same task are not
independent.

All 88 `gpt-5.6-sol` runs so far (sol-a, sol-b, ab6-a1, ab6-a2, ab6-b1, ab6-b2;
28 tasks, 52 official passes):

| weighting | single | multi | memory | full-mix estimate | 95% CI |
|---|---|---|---|---|---|
| run (the report's 58%) | 13/20, 65.0% | 17/36, 47.2% | 22/32, 68.8% | 58.3% | 44.0-72.5% |
| task | 71.4% | 43.2% | 67.5% | 57.3% | 43.0-71.7% |

The SE is 7.3 points. That is wider than the report's "about 10 points" margin,
which treated the 88 runs as independent.

The two policies are `--policy mobster` (the default) and `--policy frontier`.
Their options and costs are in [running.md](https://github.com/RadishSoftware/mobster/blob/main/mobile_agent/docs/running.md#frontier-step-policy).

### Iterating on failures without reruns

A full run costs dollars and tells you only which tasks failed. These tools find
out why, cheaply:

```bash
# $0, seconds: every recorded frontier turn classified by what it bought (chained,
# single, no change, refused, failed, missing, wait); wasted turns ranked by cause
# across tasks, the most-failed judge criteria, and (--tasks) each task's trail of
# wasted turns. --latest keeps only each task's newest run, so fixed failures drop out.
$PY -m mobile_agent.bench.diagnose --latest --tasks research/iosworld-runs/*

# Cents per try: re-ask the model one recorded decision, with the current SYSTEM
# prompt or an edited one (--system FILE), -n times. --list shows each turn's choice
# with its element id resolved; --show prints the turn's full prompt.
$PY -m mobile_agent.bench.replay research/iosworld-runs/NAME/NNN-task --list
$PY -m mobile_agent.bench.replay research/iosworld-runs/NAME/NNN-task --turn 9 -n 3
```

Frontier runs write `prompts.jsonl` (per turn: the prompt text, allowed targets,
operations and apps, and the model's reply) and `screens/prompt-turn-NN.jpg` (the
image the model saw). Trajectory screenshots are named `step-NNN-turn-MM.png`: a
turn can dispatch several actions and a refused turn none, so step numbers alone
do not line up with turns.

Then rerun only the tasks a fix targets (`pool --only a,b,c`); the judge and
report work on any subset.

`bench/sweep.py` replays many recorded turns with another model (or prompt layout)
and compares first actions; it is how a cheaper model was measured and rejected
(luna chose terra's first action on 55% of 529 turns).

#### A/B on unseen tasks (25 Sep)

One run of a task is noisy: the same code scored 1.0 and 0.5 on mem-047. Changes
that claim a general gain were compared on 12 tasks never run before (4 single-app,
4 multi-app, 4 memory; seed 20260925), two runs per build, both builds at the same
time on separate simulator pairs:

| 24 runs each | before (c578d91) | after (checklist, macros, caching, speed paths) |
|---|---|---|
| mean rubric | 0.765 | 0.764 |
| passed (official rule) | 11 | 6 |
| judge `success` flag | 10 | 6 |
| $ per task | 0.218 | 0.183 |
| model calls | 689 | 640 |
| seconds per call | 4.15 | 5.60 |

Mean rubric held (0.765 vs 0.764) and cost fell 16%, but fully passed tasks fell
from 11 to 6 of 24 (10 to 6 by the judge's flag, which this table first quoted); per-call time rose (READ_LIST's swipes, and longer replies). The A/B also found regressions that were fixed before merging (a visibility
probe on every read, silent picker values, appended answer lines) and a guard bug in
both builds (paying a second request was refused as a repeat: multi-086 0.18 -> 0.64-0.73).

### Measured results (iOSWorld)

All iOSWorld runs so far use the same 11-task smoke subset, run on 2026-09-24,
one attempt per task, judged by iOSWorld's unmodified judge. The subset has 5
single-app tasks (all "easy": `caltrack-001`, `clouddocs-004`, `lockedin-003`,
`mail-002`, `splitpay-001`), 3 multi-app tasks (`multi-006`, `multi-031`,
`multi-068`) and 3 memory tasks (`mem-004`, `mem-005`, `mem-041`). The terra and
gpt-5.5 runs had the flat $0.25 per-task cap of the time (their tasks stop at about
$0.25 with status `budget`); the harness now uses `task_cost_cap`, $0.10 plus $0.08
per app, at most $0.60. The two earlier frontier smokes had no cap. Code changed
between runs. No iOSWorld report is published: these figures are computed from the
`task.json` files of the local run folders. "Success" is the same under the
official every-criterion rule: the judge's flag and the rule agree on all of
these runs.

| run (2026-09-24) | policy / model | success | Wilson 95% CI | mean rubric score | agent s p50 | $ / task |
| --- | --- | ---: | --- | ---: | ---: | ---: |
| Jev pipeline, third smoke | `mobster` | 0/11 | 0–26% | 0.19 | 9.9 | 0.011 |
| first frontier smoke | frontier, model not recorded | 5/11 | 21–72% | 0.70 | 163 | not recorded |
| second frontier smoke, no cap | frontier, model not recorded | 3/11 | 10–57% | 0.61 | 142 | 0.56 |
| terra smoke 1 | frontier `gpt-5.6-terra` | 2/11 | 5–48% | 0.44 | 128 | 0.22 |
| terra smoke 2 | frontier `gpt-5.6-terra` | 2/11 | 5–48% | 0.38 | 201 | 0.22 |
| terra smoke 3 | frontier `gpt-5.6-terra` | 4/11 | 15–65% | 0.65 | 126 | 0.17 |
| gpt-5.5 smoke 3 | frontier `gpt-5.5` | 4/11 | 15–65% | 0.62 | 65 | 0.20 |

A 2-task check (`caltrack-001`, `mail-002`) found:

| model | passed | $ / task | seconds p50 |
| --- | ---: | ---: | ---: |
| `gpt-5.5` | 1/2 | $0.23 | 51 |
| `gpt-5.6-luna` | 1/2 | $0.011 | 53 |
| `gpt-5.6-terra` | 2/2 | $0.048 | 21 |

What these runs show:

- **The Jev pipeline does not solve iOSWorld yet.** It passed none of the 11
  tasks, mostly by waiting in apps it did not know (`frontier.py` docstring).
  That result is why the frontier policy exists.
- **Every frontier success but one is a single-app task.** Multi-app tasks are
  0/3 in every run, and memory tasks are 1/3 once and 0/3 otherwise. The failing
  long tasks run to the 50-call limit or the $0.25 cap.
- **The same model varies from run to run.** `gpt-5.6-terra` passed 2, 2 and 4 of
  the same 11 tasks, while the code changed between runs. With n = 11 the
  intervals overlap almost completely: no ranking between models is supported.

#### Smart on Claude: paired A/B (28 Sep)

The frozen Smart loop (`--policy frontier`, reasoning `low`, one code version) on 16 never-run tasks
(seed 20260928, stratified 3 single-app, 7 multi-app, 6 memory), four isolated simulators, pairs
swapped between runs, official every-criterion rule, 95% intervals by task-cluster bootstrap.
`gpt-5.6-sol` ran each task once plus a second run on 5 tasks (a pre-registered order, cut by the
evaluation's OpenAI budget); `claude-sonnet-5-5` twice; `claude-opus-5-5` once.

| | gpt-5.6-sol | claude-sonnet-5-5 | claude-opus-5-5 |
| --- | ---: | ---: | ---: |
| official passes | 10/21 | 16/32 | 9/16 |
| pass rate, task mean (95% CI) | 50% (28–72) | 50% (28–72) | 56% (31–81) |
| mean rubric score (95% CI) | 0.851 (0.77–0.93) | 0.836 (0.75–0.92) | 0.857 (0.77–0.94) |
| $ / task at list price | 0.291 | 0.197 | 0.297 |
| agent s / task, p50 / p90 | 154 / 304 | 137 / 244 | 144 / 271 |
| model s / call, p50 / p90 | 2.25 / 3.51 | 2.42 / 3.71 | 3.76 / 5.95 |
| calls / task | 27.5 | 22.1 | 16.5 |
| runs stopped by the cost cap | 9 of 21 | 5 of 32 | 4 of 16 |
| runs that said DONE and failed (upper bound) | 0.10 / run | 0.28 / run | 0.19 / run |

Paired over the 16 tasks: Sonnet 5.5 against sol, pass rate +0.0 points (95% CI −25 to +25), rubric
−0.015 (−0.108 to +0.086), cost 0.68x (−$0.094 a task, −0.143 to −0.044). Opus 5.5 against Sonnet
5.5: pass rate +6.2 points (−15.6 to +28.1), rubric +0.020 (−0.049 to +0.087), cost 1.51x. No
quality difference is shown. sol is counted at $4/$20, its current promotional rate (OpenAI's pricing
page, 28 Sep; the $2/$10 once read for it is gpt-6-sol's). The dollar cap stopped sol most often: it
now scales with a model's input price (#52), so a rerun gives sol about twice these runs' budget.

GPT-6, on the ab6 sample's 16 tasks (two runs each, 26 Sep): gpt-6-sol 16/32 official (rubric 0.873,
$0.141 a task), gpt-6-astra 20/32 (0.897, $0.605), against gpt-5.6-sol's 19/32 (0.863, $0.268) from
25 Sep. Every gap is inside the noise (about ±12 points), and the sol runs predate the simulator
isolation fix, so neither GPT-6 model replaces gpt-5.6-sol.

### Comparing with published iOSWorld results

The published figures (arXiv 2606.09764) are:

- Best overall: 51.9%, Claude Opus 4.6 with a screenshot plus the XCUITest tree.
- Best vision-only: 28.6%.
- Gemini 3 Flash: 27.8%, averaging 21.2 steps.

Opus with the tree solved 81.5% of single-app tasks and 36.7% of multi-app tasks.

Mobster's numbers are **not yet comparable** to these:

- They cover 11 of the 133 tasks, one attempt each. The subset leans to easy
  single-app tasks, where the published agents also do best.
- The published figure is the full suite, with iOSWorld's own agent loop.
- The judge, task goals, reset and step limit are the same. The agent's inputs
  differ: Mobster's AX tree and a 640-pixel screenshot, instead of iOSWorld's
  XCUITest XML and full screenshot.
- The run output does not record the simulator model, so it is not confirmed
  that it matched iOSWorld's iPhone 17 Pro setting.

A comparable number needs all 133 tasks run with one frozen code version,
ideally with repeats, and judged by the same judge model. A larger run was in
progress when this page was written. Its results are not included here.

## Other measurement tools

- `python -m mobile_agent.evals.harness` is the earlier on-device suite, with
  independent oracles, `--suite` and `--repeats`. On 2026-09-22 its 14-task
  Settings and Safari suite passed 39/42 and 38/42 across two full runs.
- `evals/calibrate.py` calibrates acceptance thresholds on oracle-labelled harness
  runs. `evals/verifier_calibration.py` tests the answer verifier against true
  answers and decoys taken from saved bench evidence.
- `evals/latency_report.py` reports per-phase latency (p50/p90/p99 and the
  critical path) from run journals and eval records.
