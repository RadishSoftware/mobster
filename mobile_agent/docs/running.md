# Running Mobster

This page covers how to start the agent, how it is configured, how to attach a
USB iPhone or a pool of simulators, and how to use the frontier step policy. Every
command and variable name below is taken from the code. The benchmarks are
described in [benchmarks.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/benchmarks.md).

Commands assume the repository root as the working directory and the project
virtualenv. It needs Python 3.12 or later; the `python3` that ships with macOS is
3.9 and cannot install the dependencies (`brew install python@3.12`, or
`uv python install 3.12`):

```sh
make venv            # python3.12 -m venv mobile_agent/.venv, then the pinned requirements.txt
PY=mobile_agent/.venv/bin/python
```

An installed copy (`pip install mobster-cli`, once published) provides the same
commands as `mobster ...`; `$PY -m mobile_agent ...` works in either case.

## Tests

The offline suites need no phone, simulator or model key:

```sh
make test-backend    # $PY -m unittest discover -s mobile_agent -t .  (the CI gate)
```

## Entry points

`python -m mobile_agent` (`mobile_agent/__main__.py`, program name `mobster`; the
`mobster` console script when installed) has these subcommands. `--version` prints
the package version, which `/api/status` also reports as `version`.

| command | what it does |
| --- | --- |
| `serve` | The local dashboard API. Binds `127.0.0.1` only, port `--port` (default 8765). |
| `run GOAL` | Previews one decision, or with `--execute` runs a bounded task. Prints events as JSON lines. |
| `demo` | Offline synthetic replay (`demo.py`). No phone and no model API. Not a benchmark. |
| `decide-fixture` | One real Jev decision on a synthetic screen (`--goal`, default "Open Search"). Needs `TYPESAFE_API_KEY`. |
| `build-ocr` | Compile the optional Apple Vision OCR helper. Output goes to `mobile_agent/.build/` in a source checkout, else to `~/Library/Application Support/app.mobster.desktop/build/` (`paths.build_dir`). |

Other entry points:

| module | purpose |
| --- | --- |
| `python -m mobile_agent.bench` | MobsterBench-iOS (see [benchmarks.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/benchmarks.md)). |
| `python -m mobile_agent.bench.iosworld` | The iOSWorld harness (see [benchmarks.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/benchmarks.md)). |
| `python -m mobile_agent.evals.harness` | The older on-device task suite with independent oracles (`evals/tasks.py`). |
| `python -m mobile_agent.eval_gemini` | A small paid helper-model comparison on synthetic fixtures; no phone actions. |

### `serve`

```sh
$PY -m mobile_agent serve --manage-device --env-file mobile_agent/.env
```

Useful flags (all in `build_parser()`):

- `--manage-device`: the agent owns the USB iPhone. It builds, runs and supervises
  WebDriverAgent (WDA) and the USB relay. With no `--wda-url`, the
  WDA URL defaults to `http://127.0.0.1:8100`.
- `--wda-url URL`, `--session ID`: drive an already running WDA. The session WDA is
  serving always wins over `--session`.
- `--env-file PATH`: load `KEY=VALUE` lines (see [Configuration](#configuration)).
- `--enable-live`: the operator gate for live dashboard runs. A saved
  `MOBSTER_ENABLE_LIVE` of `0` or `1` overrides the flag.
- `--spend-cap-usd N`: stop a run before further model calls once its observed
  inference spend reaches N dollars. The run ends with status `spend_cap`.
- `--state-db PATH`: the private SQLite task journal. The default is
  `mobile_agent/.state/mobster.sqlite3` in a source checkout, and
  `<data dir>/state/mobster.sqlite3` otherwise (a pip install or the frozen desktop
  sidecar), never inside site-packages. Journals hold task goals and observed
  screen text: never commit or publish them.
- `--data-dir PATH`: where the managed device keeps its files (the WDA build, logs,
  device settings). Default `~/Library/Application Support/app.mobster.desktop`,
  the folder the desktop app uses, so one iPhone setup serves both.
- `--exit-with-parent`: passed by the desktop app. `--keep-runner` leaves the
  iPhone runner running on exit, for development reloads; the next server adopts it.

Every request needs this launch's API token: `Authorization: Bearer <token>`, or
`?token=` on a GET (EventSource and `<img>` cannot send headers). `serve` takes
`MOBSTER_API_TOKEN` from its environment (the desktop app sets it) or makes a
random one, and writes it to `api-token` beside the journal, mode 0600
(`mobile_agent/.state/api-token` in a source checkout; the startup line names
it as `tokenFile`, and it is removed on exit). Local tools read that file (or
`MOBSTER_API_TOKEN`, or `MOBSTER_API_TOKEN_FILE`) and add the header.
From a shell:

```sh
curl -H "Authorization: Bearer $(cat mobile_agent/.state/api-token)" http://127.0.0.1:8765/api/status
```

Any HTTP client can drive the API; the Mobster Mac app's dashboard is one.

Mobster, the Mac app (a separate, closed-source product), launches the same
`serve` as a sidecar with `--manage-device --exit-with-parent --data-dir <app data dir>`
and its private `agent.env`.

### `run`

```sh
$PY -m mobile_agent run 'Open Settings' --wda-url http://127.0.0.1:8100 --env-file mobile_agent/.env
```

Without `--execute` this prints one decision and takes no action. Add:

- `--execute` to act. It takes the same exclusive device lease (`journal.Lease`)
  as `serve`, and attaches the FrameClock pixel settle to the WDA driver.
- `--helper` to enable the text helper model (typing, extraction, recovery).
- `--allow-app BUNDLE_ID` (repeatable) to offer app switching. Without it,
  `LAUNCH_APP` is never offered.
- `--max-steps` (default 30), `--max-seconds` (default 120), `--spend-cap-usd`,
  `--expected-text`.

The exit code is 0 for `preview`, `expected_text_visible` and
`completed_unverified`, and 1 otherwise.

## Configuration

### How env files load

`config.load_env_file` reads `KEY=VALUE` lines. Blank lines and `#` comments are
skipped, an `export ` prefix and surrounding quotes are removed, and variables
already set in the environment win. A missing file loads nothing (the Setup flow
creates it on first save). It returns only the names it loaded, never values.

The dashboard's Setup and Settings pages write the file (mode 0600). Keep keys out
of committed files, shell history and screenshots. The API never returns a key,
only a four-character hint (`keys.hint`).

### Model credentials and selection

| variable | used by | meaning |
| --- | --- | --- |
| `TYPESAFE_API_KEY` | `models.Jev` | Jev decision model key. Required for any live run. |
| `TYPESAFE_MODEL` | `models.Jev` | Jev model id. Default `jev-latest`. |
| `TEXT_MODEL_PROVIDER` | `gemini.configured`, `models` | `vertex` selects Gemini on Vertex AI; otherwise an OpenAI-compatible endpoint. |
| `TEXT_MODEL` | helper | Helper model id. The measured default is `gemini-3.5-flash-lite` (see `gemini-eval.md`). |
| `TEXT_MODEL_API_KEY`, `TEXT_MODEL_BASE_URL` | helper | Key and base URL for an OpenAI-compatible helper. |
| `TEXT_MODEL_REASONING_EFFORT` | helper | `none`, `low`, `medium` or `high`. |
| `MOBSTER_HELPER_PROVIDER` | `keys.py` | The provider preset the Settings page saved. |
| `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION` | Vertex clients | Your project id and location (default `global`). Tokens come from `gcloud auth print-access-token`, so an existing `gcloud` login is required. |
| `OPENAI_API_KEY` | `frontier.OpenAIChat`, iOSWorld | The frontier policy's OpenAI models, iOSWorld's apps and iOSWorld's judge. |

Without a helper, Jev can navigate but stops when a step needs generated text.

### Runtime switches

| variable | default | effect |
| --- | --- | --- |
| `MOBSTER_ENABLE_LIVE` | unset | `1`/`0` saved by Setup; overrides `--enable-live`. |
| `MOBSTER_ASK_BEFORE_ACTING` | `1` | Pause for approval before actions Mobster recognizes as sending, buying, posting, deleting or submitting (a heuristic; see `task_policy.needs_approval`). |
| `MOBSTER_BYPASS_CHECKS` | `0` | `1`: act on a commit the model is fairly sure of and on an unclear action check; MISMATCH and duplicate guards still stop it. |
| `MOBSTER_VIDEO_FPS`, `MOBSTER_VIDEO_DETAIL` | server `DEFAULT_QUALITY` | Live view quality saved by Settings. |
| `MOBSTER_WDA_DEVICES` | unset | Several USB phones; not yet read by `serve` (see [Several phones](#several-usb-phones)). |
| `MOBSTER_VISION_T0` | bundled or built helper | Path override for the local Apple Vision helper used by `vision_judge`. |

### Speed paths and feature switches

The on/off switches are on by default unless noted. They exist for A/B tests
and bisecting, not for everyday use.

| variable | effect |
| --- | --- |
| `MOBSTER_FRAME_CLOCK` | `off`, `shadow` or `on` (default `on` since the 2026-09-23 live validation). Settles on the MJPEG stream as well as the AX tree. |
| `MOBSTER_FRAME_CLOCK_LOG`, `MOBSTER_FRAME_CLOCK_TRACE` | FrameClock decision log path, and per-settle tracing in `drivers.py`. |
| `MOBSTER_GLIDE=0` | Use XCTest's drag for every scroll instead of the measured glide. |
| `MOBSTER_GLIDE_BUNDLES` | Comma list of extra bundle ids allowed to glide (`*` = all). |
| `MOBSTER_HEDGE=0`, `MOBSTER_HEDGE_RATIO` | Turn hedged model requests off, or set their budget (default 0.1). See `hedge.py`. |
| `MOBSTER_DECISION_MEMO=0` | Turn off the content-addressed decision memo (`decision_memo.py`). |
| `MOBSTER_ROUTES=0` | Turn off compiled navigation routes (`routes.py`). |
| `MOBSTER_PLANS=0` | Turn off multi-app dataflow plans (`plans.py`). |
| `MOBSTER_MILESTONES=0` | Turn off milestone compilation for multi-action requests (`milestones.py`). |
| `MOBSTER_PAGE_PROBE=0` | Turn off answer prefetch from off-screen page text (`agent.py`). |
| `MOBSTER_DIRECT_URL=0` | Type Safari addresses instead of opening the URL directly. |
| `MOBSTER_TEXT_SELECTION=1` | Opt in: let Jev pick typed text as a span of the request. Off by default after a 0/3 live result on 2026-09-24 (`models.py`). |
| `MOBSTER_DEBUG_CROPS=<dir>` | Save the image crops shown to the vision judge. The benchmark sets it per attempt. |

The desktop app's launcher also reads `MOBSTER_WDA_URL`, `MOBSTER_SESSION`,
`MOBSTER_ENV_FILE`, `MOBSTER_ENABLE_LIVE` and `MOBSTER_AGENT_FROM_SOURCE` as
development overrides; the agent itself does not.

## USB iPhone setup

The supported device path is a physical iPhone over USB, driven through
WebDriverAgent. There are two ways to set it up.

**Managed (recommended).** Start `serve --manage-device` and follow the
dashboard's Setup flow ([setup-api.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/setup-api.md)). It finds Xcode and
libimobiledevice, the connected and trusted iPhone, and your Apple development
teams from the login keychain. It then builds WDA for that phone into the data
folder, and supervises the runner and the USB relay: port 8100 for the WDA API
and 9100 for its MJPEG video. They resume on the next launch.

**Manual.** [usb-wda.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/usb-wda.md) has the exact recipe: rebrand WDA's bundle
ids for your team, `build-for-testing`, then run `test-without-building` and
`iproxy`. `scripts/usb-wda.sh` keeps both alive and restarts either one if it
exits:

```sh
TEAM=<your team id> UDID=<device udid> WDA_DIR=<path to WebDriverAgent> scripts/usb-wda.sh
```

The script also reads `WDA_DERIVED_DATA`, `WDA_PORT` (default 8100) and
`WDA_LOG_DIR`. Without `UDID` it picks the first physical device `idevice_id -l`
lists.

Rules that hold on either path:

- The agent adopts the session WDA is serving (`drivers.resolve_wda_session`); it
  never needs a fixed session id.
- Keep the phone unlocked. AX reads hang or fail while it is locked, and the
  benchmark halts on a locked phone.
- Turn on a Focus mode before evaluations: a notification can take the foreground
  mid-run.
- Only one Mobster process can control a phone at a time. `journal.Lease` is a
  cross-process, non-blocking lock per device: a live task in a second `serve`,
  a `run --execute`, or a benchmark attempt is refused while another holds it.
  (The benchmark also waits, GET only, while the app has a task running.)

### Several USB phones

`MOBSTER_WDA_DEVICES` lists phones as `;`-separated entries of `key=value` pairs.
The keys are `id`, `udid`, `wda`, `mjpeg`, `label` and `truth`, and `wda` is
required. `mjpeg` defaults to the WDA port plus 1000 (`pool.default_mjpeg_url`).

```sh
MOBSTER_WDA_DEVICES='id=a,wda=http://127.0.0.1:8100,truth=iphone15pro;id=b,wda=http://127.0.0.1:8101'
```

`pool.wda_specs_from_env` parses this syntax. Today its only caller is the
evaluation harness, whose `--devices` flag takes a string in this syntax and
needs `--in-process` (one worker per phone). Nothing reads the environment
variable itself yet: `pool.wda_specs_from_config` would, but `serve` does not
call it.

## Simulator pool (iOSWorld only)

Simulators are used only by the iOSWorld harness. Each simulator needs its own
WDA runner and MJPEG port. The harness takes them as `UDID:WDA_PORT:MJPEG_PORT`
triples. The module docstring's example uses WDA on 8200 and MJPEG on 9200, one
port up per simulator:

```sh
$PY -m mobile_agent.bench.iosworld pool --repo <path to iOSWorld> \
    --sims <UDID1>:8200:9200,<UDID2>:8201:9201,<UDID3>:8202:9202,<UDID4>:8203:9203 \
    --out research/iosworld-runs/NAME --env-file <agent env file>
```

`cmd_pool` deals tasks round-robin, starts one `iosworld run` subprocess per
simulator, and writes each worker's log to `NAME.worker<i>.log` beside the output
folder. Creating the simulators (iOSWorld's `scripts/create_sim_pool.py` clones a
bootstrapped one) and starting a WDA runner on each is outside this repository.
The harness only connects to the ports you give it. Screenshots for the judge go
through `simctl io`, never WDA, and nothing opens a Simulator window. The full
procedure is in [benchmarks.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/benchmarks.md#iosworld).

## Frontier step policy

`mobile_agent/frontier.py` (`FrontierAgent`) lets a frontier model choose each
step, over Mobster's perception and control. The model sees the request, the
allowed apps, its own plan and notes, the recent actions and their effects, up to
220 screen elements (`MAX_ELEMENTS`) by short ids (`e1`, `e2`, ...), and a
640-pixel JPEG screenshot (`SCREENSHOT_SIDE`). The screenshot is the MJPEG
stream's newest frame when FrameClock sees a still screen, else a WDA capture.

Each call returns 1 to 6 chained actions (`MAX_CHUNK`) under a strict JSON schema.
Mobster runs them in order and stops the chain when a target is missing or an
action changes nothing. The code enforces these guards:

- Only listed apps can be launched.
- A tap on a commit control (send, pay, buy, delete, post, share, follow, call...)
  that the request does not ask for is refused (`frontier.guard`, via
  `task_policy.requested_by`). The model is told why, and the refusal is in the
  trace.
- Repeats are refused, judged on the screen: typed text repeated on the screen its
  typing left, a tap repeated on the screen it was made on (it did nothing), and a
  third identical action on the same screen within 12 (a loop). Working down a list
  (another "Pay" as each request is paid) is not a repeat. Swipes are exempt;
  DISMISS tries a different free point each time instead.
- Limits: 150 actions per run (`MAX_ACTIONS`), plus the caller's `max_steps`,
  `max_seconds` and `max_cost_usd`. The last turn of any of them may only report
  (DONE or BLOCKED), so a run that hits a limit still answers.

Before the first turn, one text-only call (reasoning effort `none`, run while the
first screen is read) splits the request into a checklist of "do" and "report"
items (`compile_checklist`). The model sees it every turn and keeps it current
(`checklist_updates`: done, found with its value, missing). DONE is sent back once
for an item-by-item check of the request, and again (at most twice) while items
are open or the answer leaves out a found value. Only a last turn, which cannot be
sent back, gets found values appended.

Replies are compact by default (`MOBSTER_FRONTIER_REPLY=full` restores the old shape): an
action is `op`, `target` (an element id, a later screen's label, or the app for
LAUNCH_APP) and `text`; the thought is short and the plan is sent only when it
changes. On 12 unseen iOSWorld tasks (25 Sep) output fell from 288 to 161 tokens and
model time from 3.05 to 2.33 s a call, with the same rubric. `LONG_PRESS target`
holds 1.5 s (reaction pickers, context menus). On WDA a horizontal swipe is a fast
flick over the same distance (`MOBSTER_FLICK=off` for XCTest's drag): paging views
turn on it, and a list row's swipe actions are revealed, not triggered.

Two macro actions run in code without a model call per step: `READ_LIST` scrolls
the current list to its end and hands every row to the next turn, and
`SCROLL_TO text` scrolls until an item containing the text is on screen.
`MOBSTER_FRONTIER_MACROS=off` (or `FrontierAgent(macros=False)`) removes both. On
12 unseen iOSWorld tasks (25 Sep) READ_LIST raised the research tasks' rubric and
cost about 18 s a use; see [benchmarks.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/benchmarks.md#iterating-on-failures-without-reruns).

### Where it runs

Today the frontier policy is wired only into the iOSWorld harness:

```sh
$PY -m mobile_agent.bench.iosworld run ... --policy frontier --model gpt-5.6-terra --reasoning low
# --model defaults to gpt-5.6-luna; terra is the costlier model used in most smokes
```

- `--policy`: `mobster` (default: Jev with FrameClock, plus the helper and
  VisionJudge when a helper is configured, composed like the benchmark's
  `mobster-next`) or `frontier`.
- `--model`: any model id. `frontier.chat_client` sends `gemini-*` to Vertex AI
  (`GeminiChat`; needs `GOOGLE_CLOUD_PROJECT` and a `gcloud` login) and everything
  else to the OpenAI Responses API (`OpenAIChat`; needs `OPENAI_API_KEY`). The
  prompt's first part (SYSTEM, the schema, the request and the apps) ends in an
  explicit cache breakpoint on GPT-5.6 models, so it is billed at the cached rate
  after a task's first call; the answer is one forced function call.
  The default is `DEFAULT_FRONTIER_MODEL = "gpt-5.6-luna"`.
- `--reasoning`: default `low`. OpenAI models get it as `reasoning.effort`, and
  Gemini maps `minimal`, `low`, `medium` and `high` to a thinking level.
- In the harness each task runs with at most 50 model calls (`MAX_STEPS`, iOSWorld's
  own limit), 900 s, and a spend cap that grows with the apps the task spans:
  $0.10 plus $0.08 per app, at most $0.60 (`task_cost_cap`, `MAX_TASK_COST_USD`).
  A task that reaches the cap ends with status `budget`. The 24 Sep smokes ran with
  a flat $0.25 cap.

HTTP 429 responses are retried with backoff up to 5 times
(`RATE_LIMIT_RETRIES`). OpenAI's `insufficient_quota` is not retried, because
waiting cannot fix an empty account.

### Costs

`frontier.PRICES` holds list prices in USD per million tokens. The GPT-5.6 rates
were read from OpenAI's pricing page and model pages on 2026-09-25 (gpt-5.5 on
2026-09-24), and the Gemini rates are `bench/vertex.py`'s, read on 2026-09-23.
They are reporting estimates, not invoices.

| model | input | output | cached input | cache write |
| --- | ---: | ---: | ---: | ---: |
| `gpt-5.5` | 5.00 | 30.00 | 0.50 | = input |
| `gpt-5.6-sol` | 4.00 | 20.00 | 0.40 | 5.00 |
| `gpt-5.6-terra` | 2.00 | 12.00 | 0.20 | 2.50 |
| `gpt-5.6-luna` | 0.20 | 1.20 | 0.02 | 0.25 |
| `gpt-5-mini` | 0.25 | 2.00 | 0.025 | = input |
| `gemini-3.8/3.7/3.6-flash` | 1.50 | 7.50 | = input | = input |
| `gemini-3.5-flash` | 1.50 | 9.00 | = input | = input |
| `gemini-3.1-pro-preview` | 2.00 | 12.00 | = input | = input |

The pricing table listed `gpt-5.6-sol` at $2 / $10 on the same day its model page
said $4 / $20; the higher rates are used.

Gemini 3.6–3.8 Flash are half price until 2026-12-31. They are listed here at the
standard rate so that numbers stay comparable. For a model not in the table,
`cost_usd` returns `None`. The iOSWorld harness then records the task's cost as
0, so a $0 cost for an unpriced model means unknown, not free. The spend cap
cannot stop an unpriced model, because its spend is unknown.

Measured per-task cost on the 11-task iOSWorld smoke set (2026-09-24; the
frontier runs had the $0.25 cap, so their failing long tasks cost about $0.25
each). These come from the local, uncommitted run folders:

| policy / model | $ per task (mean) | agent seconds p50 |
| --- | ---: | ---: |
| `mobster` (Jev + helper) | 0.011 | 9.9 |
| frontier `gpt-5.6-terra` (3 runs) | 0.17–0.22 | 126–201 |
| frontier `gpt-5.5` | 0.20 | 65 |
| frontier `gpt-5.6-luna` (2-task check) | 0.011 | 53 |

Before the cap existed, 50-step loops cost about $0.80 each on `gpt-5.5` (the
comment on `MAX_TASK_COST_USD`). The success rates behind these costs are in
[benchmarks.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/benchmarks.md#measured-results-iosworld).

On MobsterBench-iOS the Jev pipeline costs $0.0057 per task at 7.7 s p50
(2026-09-24, pass 6). The frontier policy has not been run on MobsterBench.
