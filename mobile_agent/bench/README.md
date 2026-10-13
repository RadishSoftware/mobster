# MobsterBench-iOS

A pre-registered benchmark that decides whether Mobster's iPhone agent is state of the art on
accuracy **and** speed. It runs Mobster and strong screenshot baselines on the same real phone,
with the same tasks, budgets, gestures, safety monitor and graders. Nothing here runs by itself:
every command below is explicit.

## Decision rule (pre-registered, hashed with the suite)

For each baseline, per-task success rates are paired: mean(Mobster − baseline) with a 95% bootstrap
CI over tasks. The result is **better** if the lower bound is above 0, **not worse** if it is above
−5 pp, and **worse** otherwise. Speed is compared only on tasks both agents solved at least once:
the median ratio of per-task p50 agent time (Mobster/baseline), with a bootstrap CI. It is
**faster** if the upper bound is below 1. **State of the art** means better or not-worse accuracy,
faster, and zero unsafe attempts, against *every* baseline that ran. The report says where Mobster
loses: each task and category where a baseline is more accurate or faster.

## The suite

`suite.py` defines 68 tasks. They are frozen in `manifest.json` (SHA-256 of tasks, checks, budgets,
fixture labels and image digests, and the decision rule). `run` refuses a suite whose hash differs.
To change a task, bump `SUITE_VERSION` and run `freeze --force`, which shows up in the diff.

| category | n | mirrors | budget |
| --- | ---: | --- | --- |
| navigation | 10 | AndroidLab operation tasks, iOSWorld Settings navigation, MobileAgentBench | 15 steps / 90 s |
| retrieval (incl. 2 abstention) | 10 | AndroidLab query + infeasible tasks, SPA-Bench single-app | 20 / 120 s |
| settings_state | 9 | AndroidWorld System* state tasks (read-only variant) | 20 / 120 s |
| text_entry (search, URL, keypad) | 8 | AndroidWorld typing sub-goals, MobileAgentBench search | 20 / 150 s |
| multi_app | 6 | SPA-Bench cross-app, AndroidWorld information transfer | 35 / 300 s |
| web (incl. 1 abstention) | 10 | WebArena/Mind2Web-style information seeking in mobile Safari | 25 / 180 s |
| scroll (off-screen targets) | 7 | AndroidLens long-horizon scrolling, iOSWorld scroll-to-target | 25 / 180 s |
| visual (dry run) | 8 | VisualWebArena visual predicates, AndroidWorld batch tasks | 80 / 480 s |

Every task has:

- a start state that the harness resets to (relaunch, then pop to root, or open a start URL);
- an **independent oracle**, from one of two sources:
  - the harness's own probe reads the device over its own WDA connection (foreground app,
    navigation title, on-screen text);
  - the answer is compared with ground truth fixed before the run;
- a **safety class**, one of:
  - `read_only`;
  - `read_only_history`: Safari or Maps history only;
  - `reversible_reset`: a search field or the Calculator display, cleared by relaunch;
- step and time budgets.

No task sends, buys, posts, deletes, likes or calls anything, and none changes account, security
or connectivity settings. `test_suite.py` checks every goal for this.

The oracles never read an agent's evidence or citations. Only Mobster produces citations, so
grading them would bias the comparison.

### Ground truth

There are four kinds of ground truth:

- `Literal`: stable public facts written in the task.
- `Captured`: Bluetooth, Wi-Fi and Airplane Mode state, read from the Settings root screen during
  each attempt's reset. They may change between days.
- `Fixture`: records and labelled images that you install.
- `Truth`: per-device values in `truth/<device>.json`.

`Truth` values start from seeds. Some were read on the phone on 2026-09-22 (iOS version, model
number, model name, capacity). Others are public facts. iOS 26 screen titles are marked
**assumed** until `capture-truth` confirms them. `capture-truth` is the harness's read-only
navigator: it relaunches an app, taps rows by their exact accessibility label, and never taps a
switch. `dry-plan` lists every missing or assumed key. A task whose truth is missing is
**ungraded**, never passed.

## Fixtures (set up once, by hand)

```
mobile_agent/.venv/bin/python -m mobile_agent.bench fixtures --out ~/Desktop/mobsterbench-fixtures
```

This writes three things:

- `copy-to-phone/MobsterBench/{dogs12,receipts12,sunglasses12,eyes8}`: 44 CC0 or public-domain
  images from `evals/vision`, renamed so that a filename never reveals its label.
- `SETUP.txt`: the setup instructions.
- `labels-do-not-copy.json`: the answer key. It stays on the Mac.

To set up the phone:

1. Copy `MobsterBench` into Files › On My iPhone, by AirDrop or through Finder.
2. In Photos, create the album **MobsterBench Dogs** from the dogs12 images, and the album
   **MobsterBench Receipts** from the receipts12 images.
3. Create these records by hand. All of the content is fictional:
   - **Notes:** a note whose lines are `MobsterBench Note`, `Locker code: 4817`, and
     `https://en.m.wikipedia.org/wiki/Alan_Turing`.
   - **Contacts:** Bench Tester, company *Mobster Bench Labs*, mobile `(555) 010-4477`, city Cupertino.
   - **Reminders:** a list named *MobsterBench* with three incomplete reminders: *Water the plants*,
     *Return library books*, and *Pick up dry cleaning*.
   - **Calendar:** an event *MobsterBench Review* on 15 Oct 2026, 14:00–15:00, location *Room 42*.
4. Verify the setup with `dry-plan --probe --verify-fixtures`, which is read-only. With
   `run --verify-fixtures`, any task whose fixture is missing is **skipped** rather than failed.

The visual tasks are dry runs. The agent judges images and must not act on them. The graders are:

- **Exact file sets**, with item precision and recall reported (dogs, receipts, sunglasses).
- **Per-item labels**, where `unsure` is the correct answer for 4 of the 8 eye images.
- **Album counts.**
- **One single-image abstention:** the eyes cannot be seen, so the correct answer is "cannot tell".

## Agents under test

All agents share the same controls:

- **Inputs:** the same goal text and the same answer field names. Mobster gets them as its
  `output_schema`; the baselines get them in the prompt, and they end with
  `{"status": "done" | "infeasible", "answer": …}`.
- **Budgets:** the same step and time budgets, which are hard. A late answer counts as a timeout.
- **Gestures:** the same WDA gestures (`phone.py`), matching Mobster's driver: a W3C tap with a
  40 ms hold, XCTest drag, `/wda/keys`, `pressButton`, and `apps/activate`.
- **Safety:** the same safety monitor (`safety.py`). It checks every action *before* dispatch.
  Refused actions are never sent, the run stops, and the attempt fails with the event recorded.
- **Timing:** agent time excludes time that only the harness spends:
  - the AX read that maps a baseline's coordinate to an element for the safety check (`monitor_ms`);
  - provider throttling. HTTP 429 and 503 responses and dropped connections are retried with
    backoff. The time is recorded as `throttle_ms` and given back to the budget, because it is
    infrastructure rather than the agent.

  Resets and grading are never timed.

| agent | what it is |
| --- | --- |
| `mobster-smart` | Mobster's agent (`frontier.FrontierAgent`), in process, composed by `engines.build_frontier` as the app composes it: device lease, WDA session from `/status`, FrameClock attached in `on` mode, the task's apps, and its answer fields as `output_schema`. The verdict is about this arm. Its other models and arm settings are under "Smart, the held-out tranche, A/Bs and heats" below. |
| `gemini-cu` | Gemini's computer-use tool, `ENVIRONMENT_MOBILE`, on Vertex `generateContent`. `--cu-model auto` picks the first model that answers from 3.8 → 3.7 → 3.6 → 3.5 Flash. |
| `som-pro` / `som-flash` | A frontier VLM with Set-of-Marks over the WDA accessibility boxes, using an M3A-style action space (`--som-model auto`: 3.1 Pro preview first). |

### Baseline fidelity notes

**gemini-cu**, implemented from Google's Vertex "Computer use" guide (read 2026-09-23):

- **Tool and action set:** the mobile predefined action set, with none excluded and no custom
  functions. Coordinates use the guide's 0–999 grid and `v/1000 × screen`.
- **Conversation:**
  - Each model turn is echoed verbatim, so thought signatures round-trip.
  - Every call gets a `functionResponse` that carries the new screenshot, and parallel calls each
    get one.
  - The loop ends on the first turn without a function call.
- **Confirmations:** `require_confirmation` is never granted, because there is no human in the
  loop. The attempt ends as `safety_confirmation`, which counts as a failure but not as an unsafe
  action.
- **History:** screenshots are kept on the last 3 turns only, as in Google's reference agent.
- **iOS mappings the guide leaves to the client:**
  - `go_back` is the left-edge swipe. iOS has no Back key.
  - `press_key` handles enter, home, back and delete.
  - `open_app` resolves the name against Mobster's app catalog.
  - `type` sends keystrokes to the focused field.
  - There is a fixed 1 s settle after each turn, as in the guide's loop.
- **Settings and prompt:**
  - Thinking uses the provider default.
  - Screenshots are downscaled to 1280 px on the long side. Coordinates are normalized, so this
    only bounds the upload.
  - The system instruction states the answer format and the "never send, buy…" rule.
- **Caveats:**
  - Google's guide notes that mobile performance of some models is not fully optimized.
  - On 2026-09-23, `gemini-3.8-flash` returned HTTP 429 on almost every call from this project, so
    `auto` fell back to `gemini-3.7-flash`. That model took about 29 s per call in a 2-call check.
    The run config records which model ran.

**som-pro / som-flash**, following Set-of-Mark prompting (Yang et al. 2023) as used by AndroidWorld's M3A:

- **Marks:** numbered boxes drawn on the screenshot (at most 180), listed as
  `[i] Role "label" (value)`. They come from the **same** WDA accessibility read Mobster uses, so
  the baseline sees what Mobster sees, plus the pixels.
- **Action space:** click, long_press, input_text, scroll, navigate_back (the nav-bar back button,
  else an edge swipe), navigate_home, open_app, keyboard_enter, wait, and status. `status` carries
  the answer.
- **Deviations from M3A:**
  - There is one model call per step. M3A's second "summarise the step" call is dropped.
  - The history holds each step's reason, action and outcome, and whether the screen changed.
  - M3A's `answer` action is folded into `status`.
- **Output and settle:** JSON mode with a response schema, and a fixed 1 s settle.

**Other known asymmetries (reported, not hidden):**

- Mobster's own model calls are not retried on throttling. That is product behaviour.
- The baselines' safety check needs an extra AX read, which is excluded from their time.
- Mobster can fail the Calculator tasks because the display is drawn and not in the AX tree. These
  tasks are tagged `known_gap_mobster_ax_drawn_display`.

## Metrics (`report.py`)

- **Success rate:**
  - A Wilson 95% CI over attempts.
  - A task-level bootstrap CI, because repeats of a task are not independent.
  - pass^1 and pass^3 (as in tau-bench).
  - Flaky tasks: tasks whose repeats disagree.
  - The rate per category.
- **Time:**
  - Wall-clock agent time, p50 and p90 per task.
  - Per step: agent time divided by decision steps.
  - Time to first action.
- **Model calls and cost:**
  - Model calls per task.
  - Dollars per task and in total. Baselines use Vertex standard global rates (`vertex.py`); the
    3.6–3.8 Flash introductory rates are half until 2026-12-31. Mobster uses its own telemetry.
  - Unpriced calls are counted and never treated as zero.
- **Safety:** unsafe or risky attempts that were blocked, and unintended actions (both must be 0).
- **Abstention:** whether abstentions were correct.
- **Visual items:** TP, FP and FN, with precision and recall, and correct "unsure" answers.
- **Accounting:** skipped, infra and ungraded attempts are counted separately and never enter the
  rates.

## Runner hygiene and device rules (`runner.py`, `phone.py`)

Before each attempt, the runner does four things:

- **Health check** (GET only): WDA `/status` must have a session, and `/wda/locked` must be false.
  A locked phone halts the run, and so do 2 consecutive infrastructure failures.
- **Yield:** it waits while the Mobster app on :8765 has a queued or running task. It only sends
  GET requests there and never POSTs to :8765.
- **Lease:** it takes the same exclusive device lease `serve` uses for resets, baselines and grading.
- **Reset:** it resets and verifies the start app.

These rules apply throughout:

- **Timeouts:** short everywhere (health 3 s, screenshot 6 s, AX read 6–8 s), because WDA serves one
  request at a time.
- **Session:** the WDA session always comes from `/status`, never a new one, and the WDA runner is
  never restarted.
- **Order:** tasks are shuffled per repeat, and agents per task, from a seed stored in `plan.json`.
- **Records:** everything goes to `records.jsonl`, one fsynced line per attempt. `--resume DIR`
  continues and retries infrastructure failures. No GUI window is ever opened.

## Commands

```
PY=mobile_agent/.venv/bin/python

# Offline checks: suite hash, truth coverage, fixtures, agents, runtime/cost estimate
$PY -m mobile_agent.bench dry-plan
$PY -m mobile_agent.bench dry-plan --probe-models              # + one tiny Vertex call per model

# Once, with the phone free and unlocked (read-only navigation by the harness)
$PY -m mobile_agent.bench capture-truth --wda-url http://127.0.0.1:8100
$PY -m mobile_agent.bench dry-plan --probe --verify-fixtures

# Smoke (2 read-only tasks x each agent, 1 repeat)
$PY -m mobile_agent.bench run --only web.heading,nav.accessibility --repeats 1 \
    --out research/bench-runs/smoke-$(date +%F)

# The full benchmark (all agents, 3 repeats), then the report
$PY -m mobile_agent.bench run --repeats 3 --verify-fixtures --out research/bench-runs/full-$(date +%F)
$PY -m mobile_agent.bench run --resume research/bench-runs/full-YYYY-MM-DD      # after any halt
$PY -m mobile_agent.bench report --run research/bench-runs/full-YYYY-MM-DD      # -> research/bench-<date>.md
```

Add `--agents mobster-smart,gemini-cu,som-pro,som-flash` to include the Flash
Set-of-Marks variant.

## Smart, the held-out tranche, A/Bs and heats (October 2026)

Smart runs on the bench as `mobster-smart` (Sonnet 5.5 on the API), `mobster-smart-haiku`, `mobster-smart-luna`
(gpt-6-luna), `mobster-smart-sol` (gpt-6.1-sol), `mobster-smart-codex` (gpt-6.1-sol through the Codex CLI's ChatGPT
sign-in, no per-call cost) and `mobster-smart-cc` (`claude -p`; seconds of process start per call, so not for
timing). `+name` adds an arm's switches: `+jev`, `+oaiimg`, `+watch`, `+bold`, `+fast`, `+esc` (see
`agents/smart.py` and running.md's frontier switches). Every way Smart reaches the phone passes the safety monitor
first, including point taps, long presses and text writes.

| Suite (`--suite`) | What |
|---|---|
| `heldout` | H1: 16 tasks (6 short, 4 retrieval, 4 multi-app, 2 text entry), frozen 2026-10-09 in `heldout-manifest.json` and never tuned on |
| `training` | H1's siblings (same apps and shapes, other values), for anything that learns |
| `combo` | 7 varied tasks for reading engine combinations by hand |
| `hard` | 7 long tasks (four apps chained, web lookups, library counts) with milestones |

```
$PY -m mobile_agent.bench run --suite heldout --agents mobster-smart,mobster-smart-haiku+jev --order abba \
    --repeats 2 --no-yield --secrets-env ~/mobster-work/secrets/bench.env --env-file mobile_agent/.env --out DIR
$PY -m mobile_agent.bench.ab DIR                 # the paired verdict: success CI, short-task time ratio, safety
$PY -m mobile_agent.bench.trace_view DIR         # every step of every attempt, with latency and cost
$PY -m mobile_agent.bench.heat run --agents A,B,C --cap 150 --out DIR   # all variants on the hard set, scored by
                                                 # the milestones reached by the checkpoint
```

`--order abba` runs each task A B B A (the leading arm alternates by task). `--secrets-env` loads a file first, into
the bench process only. A reset clears Calculator, which keeps its last result across a relaunch.

Offline gates on recorded Smart turns: `decide_gate.py` (decision models choosing a mark), `llm_units.py` (each LLM
re-asked a recorded turn: agreement, latency, $).

## MobsterBench Real: the requests customers make (`real/`)

48 tasks paying users ask, on the owner's iPhone, in real apps (Messages, Mail, Calendar, Reminders, Notes, Photos,
Maps, Safari, Settings, Instagram, App Store, Wallet, Weather, Clock, Health), started on the Home Screen. The
pre-registration, with the metrics, the ABBA protocol and the outside-agent comparison, is
[`docs/realbench.md`](../docs/realbench.md); the hash is in `real-manifest.json`.

- **Nothing leaves the phone.** A send, reply, like, follow, comment, call, delete or install ends at Mobster's approval
  card, which the bench records and declines. `real/policy.py` refuses every commit control, money app and setting the
  task doesn't name before it reaches the phone.
- **Writes go into bench containers** that the harness restores after each attempt (`real/harness.py`): the "Mobster
  Bench Scratchpad" note, the "Mobster Bench Errands" list, a timer, Dark Mode and Low Power Mode.
- **Keys that change are read by the harness** at the attempt's start or right after it (`real/reads.py`): the latest
  text, the temperature, the battery, the note's text. Code grades every task (`real/checks.py`).

```
PY=mobile_agent/.venv/bin/python
export MOBSTER_BENCH_UDID=<UDID>                                      # the bench iPhone, the only device read
$PY -m mobile_agent.bench.real list -v                                # the tasks, counts and grader coverage
$PY -m mobile_agent.bench.real survey --out ~/mobster-work/realbench/survey   # read-only: screens and keys
$PY -m mobile_agent.bench.real containers [--restore]                 # check or set the bench containers
$PY -m mobile_agent.bench.real run --secrets-env ~/mobster-work/secrets/bench.env \
    --out ~/mobster-work/realbench/runs/NAME                          # each task once, $5 spend cap
$PY -m mobile_agent.bench.real run --agents mobster-smart,mobster-smart+routed --order abba --repeats 2 --out DIR
$PY -m mobile_agent.bench.real report --run DIR
```

`survey` and `run` read the phone's apps from the iPhone `--udid` or `MOBSTER_BENCH_UDID` names, and from no
other: they never list the attached devices. Without a UDID they stop before the phone's apps are read, unless the
output folder already holds `inventory.json`.

Runs, surveys and captured keys hold what the phone showed: keep them under `~/mobster-work/realbench`, never in git.

## The tuning loop on iOSWorld (`tuning.py`, `tuning_analysis.py`)

Every engine change goes through one loop: a hypothesis from measured failures, an offline check, a paired A/B on
the iOSWorld simulators, then a promotion or a recorded loss. iOSWorld's 133 tasks are split once into dev (every
task an earlier build ran), val (never run; promotes a change) and test (never run, never looked at; opened only
for a public claim). The splits file carries its own SHA-256, and `run` refuses a file that no longer matches it
and refuses test tasks outside `--split test`.

```
$PY -m mobile_agent.bench.tuning run --batch DIR --arm A=WORKTREE_MAIN --arm B=WORKTREE_CHANGE --repo ../iOSWorld \
    --sims UDID:8200:9200,UDID:8201:9201,... --splits splits.json --split dev_core --runs 2 \
    --env-file bench.env --model claude-sonnet-5-5 --xctestrun WDA.xctestrun --shutdown --ledger ledger.jsonl
$PY -m mobile_agent.bench.tuning judge --batch DIR --repo ../iOSWorld --env-file bench.env --ledger ledger.jsonl
$PY -m mobile_agent.bench.tuning trace --batch DIR       # trace.jsonl per task: one line per model turn
$PY -m mobile_agent.bench.tuning label --batch DIR --env-file bench.env   # root cause of each failed run
$PY -m mobile_agent.bench.tuning scoreboard --batch DIR --labels DIR/labels.jsonl --ledger ledger.jsonl
$PY -m mobile_agent.bench.tuning counterfactual --runs DIR --variant pkg.mod:fn --n 40 --out OUT
```

- **Runs.** Each task runs A B B A, and its attempts are dealt to the simulators with a rotation, so every
  simulator runs every arm equally often. A batch holds one run folder per arm and run (`A1 B1 B2 A2`), so
  `iosworld compare --a DIR/A* --b DIR/B*` reads it unchanged. An A/A is the same worktree as both arms.
- **No driver outlives its run.** Each task is a child in its own session with a hard timeout (1,200 s; the agent
  stops itself at 900 s). A reaper in its own session watches the runner; when the runner ends, however it ends, the
  reaper kills any child left, stops the batch's WDA runners and, with `--shutdown`, shuts the simulators down.
- **WDA session.** Each task starts a new session the way the app does (no quiescence wait), so the bench measures
  the session users run. `MOBSTER_BENCH_SESSION=legacy` brings back the plain session every run before 10 Oct had.
- **Reset.** `MOBSTER_BENCH_RESET=restore` (with `MOBSTER_BENCH_CHECKPOINTS=DIR`) makes each task's clean slate by
  restoring a checkpoint of the freshly seeded apps (`bench/evals/checkpoint.py`, about 4.5 s, checked by digest)
  instead of iOSWorld's wipe-and-reseed (43-66 s). Each simulator's seed is made once, from iOSWorld's own reset; a
  restore whose digest differs falls back to iOSWorld's reset. The default stays iOSWorld's reset until an A/A shows
  the two equivalent.
- **Per-turn timing.** With `MOBSTER_BENCH_TTFT=on` (the runner's default), `bench/stream_timing.py` sends each Claude
  call's identical request streamed and rebuilds the same reply, so each turn records its time to first token
  beside its tokens. It is a measurement for the benchmark; the app keeps the unstreamed call. Both arms run it alike.
- **The trace** has per turn: the screen's and the screenshot's hashes, the app, the model and route, input, output
  and cached tokens, call latency, time to first token and generation time, the actions with refresh, exec and settle
  times, refusals, chunk stops and the contract items' states. Per task: the official pass, the rubric and each
  criterion, status, answer, $, calls and the time buckets.
- **The labeller** reads each failed trace and returns one root cause from a fixed taxonomy (perception or grounding,
  wrong plan, lost cross-app state, premature DONE, step budget, loop or stall, gave up, infra, judge miss), the first
  wrong step and a one-line counterfactual. Spot-check its labels by hand before trusting the map.
- **Counterfactuals** re-ask recorded turns with a variant (`fn(messages, schema, turn) -> (messages, schema)`) and
  report agreement with the recorded first action (also on passing runs only), output tokens, latency and time to
  first token. Kill an idea here when it is cheap to kill.
- **The scoreboard** shows per arm the official pass (and reweighted to the 133-task mix), rubric, s/task p50 and
  p90, calls, output tokens per turn, turn latency, time to first token, $/task, false DONEs and WDA crashes; the
  same by category; the paired difference with bootstrap CIs and their half-widths per category; the failure map;
  and the trend across builds from the ledger.

## Cheap evals (`evals/`)

Most engine ideas should live or die before a paired A/B, which costs about $20 and an hour. `evals/` holds the
cheaper levels, each run on recorded data. None of them touches a device.

L0 is trace analytics, $0 and under a minute over every recorded run:

```
$PY -m mobile_agent.bench.evals.l0 --runs ~/mobster-work/tuning/batches research/iosworld-runs \
    --suite iosworld --app-groups groups.json --labels labels.jsonl --out DIR
```

It writes `DIR/l0.md` and `DIR/l0.json`:

- the mechanisms ranked by seconds: round trips, repeated actions, unchanged actions, refusals, DONE deferrals,
  DONE-only last turns, re-read screens, typing and settle time, and turns beyond the shortest passing run of the
  same task;
- where the seconds go, without overlap;
- time sinks by step type and by app group;
- where each failing run first leaves the passing path.

The divergence is the first milestone the failing run never reached. A milestone is an action every passing run of
the task took; scrolls and dismissals don't count. `groups.json` maps app names to groups and stays outside the
repository, with the runs.

The same table now carries a time-to-first-token breakdown when the runs have streamed timing:
- how often the cache hit;
- how TTFT scales with uncached tokens;
- the turns whose output tokens show hidden thinking.

L1 is component and turn evals on frozen sets, cents a run. A set is built once from recorded runs and frozen as
`<root>/<name>/<version>/`: `items.jsonl`, the screenshots it uses, and a `MANIFEST.json` whose sha256 covers both. The
loader refuses a set whose files no longer match it. Tasks are split into a tune half and a report half by one
seeded map shared by every set. A task in val or test never enters a set. The sets hold task text and screens, so
they stay outside the repository.

```
$PY -m mobile_agent.bench.evals.build done --runs BATCHES --batches aa1,ac1 --splits splits.json \
    --labels labels.jsonl --root SETS --version v2
$PY -m mobile_agent.bench.evals.l1 SETS/done/v2 --system llm:claude-haiku-5-5 --half report \
    --keys bench.env --ledger spend.jsonl --cap 15 --out DIR
```

| Set | Item | Label |
|---|---|---|
| `done` | a run's ending at its DONE: contract, notes, answer, each step's new rows, list reads and effects | the judge's verdict, the unsatisfied criteria, the verdict with labelled judge misses corrected |
| `value` | a screen's rows and screenshot, and a REPORT question | the value the agent found on that screen in a passing run, or none for another screen |
| `golden` | a recorded prompt and screenshot | the next action, plus other passing runs' actions on that screen; at a failing run's divergence, also its wrong action |
| `contract` | a request and its apps | the judge's rubric criteria, each with how often judged runs failed it |
| `commit` | a commit-class control the guard decided on (refused, tapped, on a read screen), with the contracts compiled for its task | whether the request asks for that act (`commit_requested`), and whether a run failed because the guard refused it |
| `grounding` | a screen's rows and screenshot, and an intent made from the tree (a label, the third row of a list, the button beside a label, a long label's first words) or the planner's own words for a tap | the element |

The systems `l1` runs:

- `done`: `accept_all` (the engine as it is), `file:` (an offline gate's results), `llm:<model>` (a reference completion check), and `completion_check:<model>`.
- `value`: `llm:<model>` and `jev`.
- `golden`: `llm:<model>` with one observation arm:
  - `+noimage`: rows only;
  - no suffix: rows and the screenshot;
  - `+marks`: rows and the screenshot with each row's box and id drawn;
  - `+marksonly`: the marked screenshot alone.

  Add `+nothink` for any arm. `decide:jev` and `decide:openai` pick the next tap with a decision model.
- `contract`: `recorded`, and `compile:<model>` with `+itemize` or `+selfcheck`. A matcher model maps the items onto the criteria.
- `grounding`: `llm:<model>` (rows only, `+image`, `+marks`, `+marksonly`), `decide:jev`, and `decide:openai` with the same arms.
- `commit`: `family:current` (this checkout's guard rule) and `family:old` (the rule before #224), with no model call.

Each run prints accuracy, latency p50 and p90, and $. It writes its rows, and `--rescore ROWS` scores old rows
against a set again with no calls. The ledger refuses a call once the spend recorded in it reaches `--cap`.

The pairwise step judge (`evals/pairwise.py`) turns a golden run into a preference win rate. It keeps only the
turns where the candidate's first action differs from every acceptable recorded action. On each one, a judge sees the
request, the progress and the screen, and is asked twice with the actions swapped. A verdict counts only when both
orders agree; otherwise it's a tie. `--calibrate synthetic` asks the judge about pairs whose answer is
known: the recorded action against a tap on a random other element, or against DONE mid-task.

```
$PY -m mobile_agent.bench.evals.pairwise SETS/golden/v2 --rows DIR/golden-v2-report-llm_claude-haiku-5-5.jsonl \
    --judge claude-sonnet-5-5 --keys bench.env --ledger spend.jsonl --cap 15 --out DIR
```

L2 starts offline with `evals/irt.py`, a 2PL item response model fitted over every judged iOSWorld run. Each engine
configuration is a respondent and each task an item. It ranks tasks by Fisher information at the tuned engine's
ability, and the top k are the informative subset for a fast directional A/B. `--pairs` backtests the subset on
past paired experiments: for each one it refits without that experiment, then compares the subset's delta with the
full delta, and random subsets give the baseline.

```
$PY -m mobile_agent.bench.evals.irt --runs BATCHES research/iosworld-runs --anchor ac1:A --tasks DEV_CORE \
    --pairs pairs.json --fraction 0.5 --out DIR
```

The finishing-turn set (`finish`) re-asks each DONE ending's final turn. It scores a model or effort level by how
often it finishes when the run was done and when it wasn't.

`evals/proxy.py` checks whether the cheap evals predict the expensive one. Keep one JSONL line per hypothesis that
reached a paired A/B or was stopped before one: its deltas on each proxy, and its L3 result. With four or more
L3 results, it prints Spearman's rank correlation between each proxy and the outcome, and how often their signs
agree.

```
$PY -m mobile_agent.bench.evals.proxy validity.jsonl --outcome pass_delta
```

L2 checkpoints (`evals/checkpoint.py`) snapshot the iOSWorld apps' data on a simulator. A snapshot covers each app's
data-container folders, the ones iOSWorld's reset wipes, and the shared app-group containers. Copies are APFS
clones (`cp -c`), and a digest of the persistent folders checks every restore against its save. Restoring the
freshly seeded checkpoint is a reset. Restoring one taken just before a task's hard part starts a run at the crux.
It refuses any simulator whose name doesn't start with "Eval Lab", so the tuning loop's simulators are never touched.

```
$PY -m mobile_agent.bench.evals.checkpoint save --udid UDID --repo BOOTSTRAP_DIR --name seed
$PY -m mobile_agent.bench.evals.checkpoint restore --udid UDID --repo BOOTSTRAP_DIR --name seed
```
