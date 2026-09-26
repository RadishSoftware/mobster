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
| `mobster` | The current pipeline, in process, exactly as `evals/harness.py --in-process` builds it: device lease, WDA session from `/status`, Jev + configured Gemini helper, "In <App>: <goal>" framing. It runs cold: no replay store, no FrameClock attached, no VisionJudge. Dry-run tasks use Mobster's own `loop_mode="dry_run"` when the Agent supports it. |
| `mobster-next` | The same, with the new features forced on: FrameClock attached in `on` mode, and the VisionJudge that compiled visual loops need. Availability is detected from the code (`feature_status()`). When nothing is wired, the agent is skipped with the reason. |
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
- Mobster runs cold, so replay across repeats is off.
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

Add `--agents mobster,mobster-next,gemini-cu,som-pro,som-flash` to include the Flash
Set-of-Marks variant.
