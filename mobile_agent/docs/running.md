# Running Mobster

This page covers how to start the agent, how it is configured, how to attach a
USB iPhone or a pool of simulators, and how Mobster's agent (the frontier step
policy) works. Every
command and variable name below is taken from the code. The benchmarks are
described in [benchmarks.md](https://github.com/RadishSoftware/mobster/blob/main/mobile_agent/docs/benchmarks.md).

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
make test-backend    # scripts/ci_backend.py: one process per module, in parallel (the CI gate)
```

## Entry points

`python -m mobile_agent` (`mobile_agent/__main__.py`, program name `mobster`; the
`mobster` console script when installed) has these subcommands. `--version` prints
the package version, which `/api/status` also reports as `version`.

| command | what it does |
| --- | --- |
| (none), `tui` | The terminal UI (`tui/`): a Textual app over `server.Runtime` in process, with its own journal (`terminal.sqlite3`). `--demo` runs Mobster's agent on the scripted phone and model of `tui/demo_phone.py` (`DemoPhone`, `DemoChat`). |
| `doctor` | Checks the setup and prints fixes (`doctor.py`). |
| `history`, `screen`, `version` | Past tasks of the terminal UI and `run` (`history.py`), the phone's screen in the terminal (`terminal_image.py`), the version. |
| `serve` | The local dashboard API. Binds `127.0.0.1` only, port `--port` (default 8765). |
| `run TASK --execute` | Runs one task on Mobster's agent through `server.Runtime` (`smart_run.py`). Prints a line per step on a terminal (`console.py`), and events as JSON lines when piped or with `--json`. Saves the task in the terminal UI's journal (`terminal.sqlite3`, or `--state-db PATH`), and exits 3 without reaching the phone while another Mobster has that journal open. |
| `demo` | `run "Search for coffee" --demo`: Mobster's agent on the scripted phone and model of `tui/demo_phone.py`, offline. No phone and no model API. Not a benchmark. |
| `build-ocr` | Compile the optional Apple Vision OCR helper. Output goes to `mobile_agent/.build/` in a source checkout, else to `~/Library/Application Support/app.mobster.desktop/build/` (`paths.build_dir`). |

Other entry points:

| module | purpose |
| --- | --- |
| `python -m mobile_agent.bench` | MobsterBench-iOS (see [benchmarks.md](https://github.com/RadishSoftware/mobster/blob/main/mobile_agent/docs/benchmarks.md)). |
| `python -m mobile_agent.bench.iosworld` | The iOSWorld harness (see [benchmarks.md](https://github.com/RadishSoftware/mobster/blob/main/mobile_agent/docs/benchmarks.md)). |

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
  device settings). It also sets `MOBSTER_APP_DATA_DIR` for this process and its
  children, so their settings and stores use the same data root. `--state-db`
  chooses the task journal separately.
- `--exit-with-parent`: passed by the desktop app. `--keep-runner` leaves the
  iPhone runner running on exit, for development reloads; the next server adopts it.

The data root (`paths.user_data_dir`) uses `MOBSTER_APP_DATA_DIR` when set, resolved to an
absolute path. Without it, a source checkout uses `mobile_agent/.state/data`; the installed app
uses `~/Library/Application Support/app.mobster.desktop`. With `MOBSTER_DEV_SHELL` set,
a process outside a checkout uses the sibling `app.mobster.desktop.dev` folder. Each dev
window passes its own root. `agent.env` and Keychain service names follow that
root, so a dev copy does not read or overwrite the installed app's credentials. Only a
sibling folder named `app.mobster.desktop.dev` plus digits keeps a readable Keychain
name; any other folder gets a name made from its path.

Every request needs this launch's API token: `Authorization: Bearer <token>`, or
`?token=` on a GET (EventSource and `<img>` cannot send headers). `serve` takes
`MOBSTER_API_TOKEN` from its environment (the desktop app sets it) or makes a
random one, and writes it to `api-token` beside the journal, mode 0600
(`mobile_agent/.state/api-token` in a source checkout; the startup line names
it as `tokenFile`, and it is removed on exit). Local tools read that file (or
`MOBSTER_API_TOKEN`, or `MOBSTER_API_TOKEN_FILE`) and add the header. Beside it,
`api-url` holds the service's address, also removed on exit: the Mac app's agent
uses the next free port up to 8774 when another app holds 8765, and
`mobster chat` and MCP's `phone_task` read it to find the agent.
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
$PY -m mobile_agent run 'Open Settings' --execute --wda-url http://127.0.0.1:8100 --env-file mobile_agent/.env
```

A task needs `--execute`; without it `run` prints why and exits. The run goes
through `server.Runtime`, which takes the same exclusive device lease
(`journal.Lease`) as `serve`, and the WDA driver settles on the MJPEG stream as
well as the tree (`frame_clock.attach_frame_clock`). `--spend-cap-usd` stops it
before further model calls once it has cost that much. `--long`, `--budget` and
`--yes` run a long task.

One Mobster drives a phone at a time on this Mac, however it is reached. Besides
the lease on the WebDriverAgent address, a phone with a UDID has a lease of its
own (`journal.Lease.phone`, `device-leases/phone-<hash>.lock`) that records the
holder's pid and exact command line. A second Mobster that reaches the phone by
another address (a leftover dev relay and the direct link) is refused with
`The Mobster app (pid 123) is driving this iPhone.` A record whose pid is gone,
or whose command line no longer matches, is not named; the refusal then says
`Another Mobster process is driving this iPhone.` The lock is an `flock`, so a
holder that dies frees the phone at once and the next Mobster takes it over.

`--wda-url` defaults to `MOBSTER_WDA_URL`, else `http://127.0.0.1:8100`. The exit
code is 0 when the task is done, 1 when it didn't finish, 2 for a usage error, 3
when it couldn't run (no phone answered, no key) and 130 after `ctrl+c`. The full
flag list is in
[CLI reference](https://docs.mobster.dev/reference/cli).

## Configuration

### How env files load

`config.load_env_file` reads `KEY=VALUE` lines. Blank lines and `#` comments are
skipped, an `export ` prefix and surrounding quotes are removed, and variables
already set in the environment win. A missing file loads nothing (the Setup flow
creates it on first save). A line it can't parse (not `KEY=VALUE`, an invalid name,
bytes that are not UTF-8) is skipped, never fatal. It returns the names it loaded and
one warning per skipped line, by line number; neither holds a value. `mobster` prints
the warnings to stderr, which is `logs/agent.log` in the Mac app.

The dashboard's Setup and Settings pages write the file (mode 0600). Keep keys out
of committed files, shell history and screenshots. The API never returns a key,
only a four-character hint (`keys.hint`).

### Model credentials and selection

| variable | used by | meaning |
| --- | --- | --- |
| `TEXT_MODEL_PROVIDER` | `gemini.configured`, `keys.py` | `vertex` selects Gemini on Vertex AI for the helper; otherwise an OpenAI-compatible endpoint. |
| `TEXT_MODEL` | `gemini.configured`, `keys.py` | The helper model id. With its key, it sets `helper_configured` in the status. |
| `TEXT_MODEL_API_KEY`, `TEXT_MODEL_BASE_URL` | `keys.py` | Key and base URL for an OpenAI-compatible helper. An OpenAI or Anthropic helper's key serves Mobster's agent when `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` is unset. |
| `MOBSTER_HELPER_PROVIDER` | `keys.py` | The provider preset the Settings page saved. |
| `TYPESAFE_API_KEY`, `TYPESAFE_MODEL` | `decision_provider.JevDecisions` | The Jev decider the bench's decision gate compares (`bench/decide_gate.py`). Default model `jev-latest`. |
| `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION` | Vertex clients | Your project id and location (default `global`). Tokens come from `gcloud auth print-access-token`, so an existing `gcloud` login is required. |
| `OPENAI_API_KEY` | `engines.smart_key`, `model_clients.OpenAIChat`, iOSWorld | Mobster's agent on OpenAI (with an OpenAI helper, saved by Settings or set by its base URL, `TEXT_MODEL_API_KEY` serves when this is unset; `keys.openai_key` is the one rule), the frontier policy's OpenAI models, iOSWorld's apps and iOSWorld's judge. |
| `ANTHROPIC_API_KEY` | `engines.smart_key`, `model_clients.AnthropicChat` | Mobster's agent on Claude: with this key, it runs on `claude-sonnet-5-5`, also beside an OpenAI key unless `MOBSTER_SMART_PROVIDER=openai` (an Anthropic helper's `TEXT_MODEL_API_KEY` serves when this is unset; `keys.anthropic_key`). Also the frontier policy's `claude-*` models. |
| `MOBSTER_SMART_MODEL` | `engines.smart_model` | The model Mobster's agent runs on, any OpenAI or Claude id (`claude-sonnet-5-5`, `claude-opus-5-5`, `gpt-5.6-sol`...); its provider's key must be set. Unset: `claude-sonnet-5-5` with an Anthropic key, else `gpt-5.6-sol` with an OpenAI key (`MOBSTER_SMART_PROVIDER=openai` puts OpenAI first). Only the model changes: reasoning and limits stay `SMART_CONFIG`'s. |

### Runtime switches

| variable | default | effect |
| --- | --- | --- |
| `MOBSTER_ENABLE_LIVE` | unset | `1`/`0` saved by Setup; overrides `--enable-live`. |
| `MOBSTER_ASK_BEFORE_ACTING` | `1` | Pause for approval before actions Mobster recognizes as sending, buying, posting, deleting or submitting (a heuristic; see `task_policy.approval_kind`). |
| `MOBSTER_TASK_COST_LIMIT` | `1.00` | Settings › Models: USD a task stops at (status `spend_cap`), or `off`. The lower of this and `--spend-cap-usd` applies. |
| `MOBSTER_MONTHLY_LIMIT` | unset | Settings › Models: USD; new tasks are refused (`monthly_limit_reached`) once this calendar month's spend reaches it, a task stops at what is left of it, and an unreadable month refuses (`usage_unavailable`). |
| `MOBSTER_VIDEO_FPS`, `MOBSTER_VIDEO_DETAIL` | server `DEFAULT_QUALITY` | Live view quality saved by Settings. |
| `MOBSTER_WDA_DEVICES` | unset | Several USB phones, for `mobster devices` (see [Several phones](#several-usb-phones)). |

### Development switches

These exist for A/B tests and debugging, not for everyday use.

| variable | effect |
| --- | --- |
| `MOBSTER_FRAME_CLOCK_LOG`, `MOBSTER_FRAME_CLOCK_TRACE` | The pixel settle's decision log path (`wda/frame_clock.py`), and per-settle tracing in `wda/observe.py`. The pixel settle is on whenever a video stream is attached. |
| `MOBSTER_GLIDE=0` | Use XCTest's drag for every scroll instead of the measured glide. |
| `MOBSTER_TREE_READS=still` | Opt in until its A/B. `observe_ready` takes its first read as the screen at rest, with no agreeing read and no quiet period, when the stream shows not one changed byte from 0.3 s before the read began and its stillness began after the last dispatch (`WDAObserve._at_rest`). It never touches the read before a coordinate tap (`_check_current`) or the agent's refresh before an action (`frontier._refreshed`). `MOBSTER_FRAME_CLOCK_LOG` gets a `ready` row for every `observe_ready` call, with or without the switch (rule, ms, reads, `would_rest` and `first_held`: in a run without the switch, how often a first read the stream showed at rest was the screen the agreeing reads accepted), and a `next_read` row says `stream_still`: whether no byte changed from the accepted read to the next one. `mobile_agent.bench.tuning scoreboard` prints both. Offline on 34 recorded runs (18 Oct): 2.5 calls a task at 1.75 s, 4.3 s a task; at most 4.0 s a task can go on that sample, and how many calls qualify is unmeasured until the A/B. |
| `MOBSTER_SETTLE=launch` | Opt in until its A/B. An app launch (`LAUNCH_APP`) keeps its agreeing reads today. With the switch on, a launch that ends its turn (99% do) settles on one read when the stream already shows the effect and the screen at rest as the launch returns (`wait_for_change(single_read="rest")`: the effect and the rest are asked for with a zero wait, never waited for), which the verify read then proves while the model thinks. `HOME` is unchanged: no recorded run measured it. If the effect isn't seen, the pixels aren't at rest, the read shows the screen before, or the pixels moved during the read, the agreeing reads run as before. With a usable watch, `MOBSTER_FRAME_CLOCK_LOG` records `miss` when a one-read settle was not taken (`not_requested`, `no_effect`, `not_stable`, `read_failed`, `unchanged`, `moved`). Offline on 1,441 recorded launch settles (10 to 14 Oct, simulators): 458 (32%) had the effect seen and the screen at rest for 0.18 s or more when the settle began, a projected saving of 0.3 to 0.7 s and 1.0 to 1.3 reads a task, not a measured effect of the switch. |
| `MOBSTER_EXPERIMENTAL_SETTLE=1` | Opt in until its A/B (`mobile_agent/settle/`). Predictive settle: each settle that ended on agreeing reads teaches `settle/durations.json` (data folder, 0600, numbers only) the app's arrival time for that kind of action: the first read of the screen that stayed, after the dispatch. With 5 samples for an (app, action shape) whose p90 is at most 1.2 s and under 0.5 s above its median, a step whose caller verifies the result (`single_read` or `early`) sleeps that p90, takes one read, and accepts it when it differs from the screen before and the stream, if usable, was still for 0.18 s (`settled_by` is `predicted`, so the verify read proves it while the model thinks). Anything else (no prediction, a read of the old screen, pixels that moved, a failed read) runs the polling loop unchanged. A hit teaches nothing, so the store only learns from polled settles. `MOBSTER_FRAME_CLOCK_LOG` rows say `predicted` (ms), `shape`, `outcome` (`hit`, `none`, `unchanged`, `moved`, `read_failed`, `no_time`) and `arrival_ms`; the run's `frontier_action` event carries them as `settle_prediction`. The stream (`settle/stream.py`) only says whether the pixels were still: the accessibility read stays the observation. Measured offline 26 Oct 2026, by `python -m mobile_agent.settle.replay` on 276 recorded polled settles (73 recorded runs, mostly 12 Oct): AX reads per settle 3.18 to 2.24, mean settle wall 808 to 805 ms (no change), 97% of 151 predictions would have held, 125 settles had no prediction yet. A 90th-percentile wait alone was slower (808 to 913 ms), which is why uneven keys are left to polling. A second log (165 settles, the Mac app's own state folder) gave 2.84 to 2.67 reads and 995 to 1,005 ms. Arrival is estimated from `settle_ms` minus the polling quiet period for rows logged before the switch, so these are projections, not a measured effect. On a virtual-clock fake WDA (0.2 s a read, one recorded screen, busy pixels): 0.70 s and 3 reads polled, 0.35 / 0.50 / 0.60 s and 1 read at a learned wait of 150 / 300 / 400 ms. Not measured: a live simulator or phone, and the pass rate (the paired A/B on 12+ unseen iOSWorld tasks decides the default). |
| `MOBSTER_HEDGE=0`, `MOBSTER_HEDGE_RATIO` | Turn hedged model requests off, or set their budget (default 0.1). See `hedge.py`. |
| `MOBSTER_CTX_MARKS=on` | Opt in until its A/B. The agent's screen rows mark a button that reads as a search or entry prompt `(not editable)`, and a row listed before a bottom bar that covers it `(hidden under the bottom bar)` (`frontier.looks_like_field`, `frontier.under_bars`). Offline on 10 recorded failed turns, 2 samples each (12 Oct): Sonnet 5.5 at effort low chose right 18 of 20 times with the marks, 9 of 20 without. |
| `MOBSTER_CTX_PROVENANCE=on` | Opt in until its A/B. A status text that came with its page, opened by an action that named no act, is marked `(on screen since this page opened, before any action of yours here)` (`frontier.Provenance`): Cinephile shows "Added successfully" on a film's page before any tap. Offline on 2 recorded failed turns, 2 samples each (12 Oct): 4 of 4 right with the mark, 2 of 4 without. |
| `MOBSTER_CTX_READBACK=on` | Opt in until its A/B. After a tap on a control that names an act (Add Food, Save) changes the screen, its history line says which rows changed in a titled page seen before, on screen or behind the sheet it left up: `read back from Today: "Oatmeal, 1 cup cooked, 150" 2 rows (1 before)`. The page behind a sheet comes from the same source read (`Snapshot.behind`), so there's no extra device read. More than 4 changed rows reads as a scroll, and says nothing. Offline on 3 recorded failed turns, 2 samples each (12 Oct): 6 of 6 right with it, 1 of 6 without. |
| `MOBSTER_CTX_KEPT=on` | Opt in until its A/B. A list a turn read and left without acting on a row or taking a note is kept in that turn's block of the turn log: up to 5 rows of 30 characters, at most 3 lists a run. A list is a titled page with 3 or more wide rows stacked alike, or a `READ_LIST`'s rows. Offline (12 Oct): 2 of 2 right on the recorded failed turn, 0 of 2 without; prompts grow 3.9% on the runs that keep the most. |
| `MOBSTER_PROGRAMS` | Saved programs (`learned/programs.py`, [Learned steps from any task](https://docs.mobster.dev/schedules#learned-steps-from-any-task)). A request with the same words as one that ended done, other values in its blanks, repeats that run (`FrontierAgent(saved_program=...)`): code fills the blanks with the request's own words (a model call only when that's ambiguous), checks each step's screen, and the contract's commit check and approvals apply to every step. A new program's first repeat ends through the model's DONE turn; after one proven repeat, a contract proven with nothing to report ends with no DONE call. `learn` only keeps programs. Unset, on for runs from Mobster for Mac, `mobster serve`'s API, `phone_task` through `mobster mcp` and Mobster Remote (origins `app`, `api`, `mcp` and `remote`) and off for the rest; `off` turns it off, `on` turns it on for every origin. Set to `on` explicitly, a request of another shape also runs parts of saved programs (`Parts` in `learned/parts.py`, `FrontierAgent(program_parts=...)`): spans cut at app and screen changes and around blanks, bound by code from the request and the open contract items, each step and the end checked. |

The desktop app's launcher also reads `MOBSTER_WDA_URL`, `MOBSTER_SESSION`,
`MOBSTER_ENV_FILE`, `MOBSTER_ENABLE_LIVE` and `MOBSTER_AGENT_FROM_SOURCE` as
development overrides; the agent itself does not.

## USB iPhone setup

The supported device path is a physical iPhone over USB, driven through
WebDriverAgent. There are two ways to set it up.

**Managed (recommended).** Start `serve --manage-device` and follow the
dashboard's Setup flow ([setup-api.md](https://github.com/RadishSoftware/mobster/blob/main/mobile_agent/docs/setup-api.md)). It finds Xcode and
libimobiledevice, the connected and trusted iPhone, and your Apple development
teams from the login keychain. It then builds WDA for that phone into the data
folder, and supervises the runner and the USB relay: port 8100 for the WDA API
and 9100 for its MJPEG video. They resume on the next launch.

**Manual.** [usb-wda.md](https://github.com/RadishSoftware/mobster/blob/main/mobile_agent/docs/usb-wda.md) has the exact recipe: rebrand WDA's bundle
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
required. `mjpeg` defaults to the WDA port plus 1000 (`pool.default_mjpeg_url`). A WDA port below
8100, or one whose pair would pass 65535, has no default stream: that phone settles by time.

```sh
MOBSTER_WDA_DEVICES='id=a,wda=http://127.0.0.1:8100,truth=iphone15pro;id=b,wda=http://127.0.0.1:8101'
```

`pool.wda_specs_from_env` parses this syntax, and `mobster devices` lists these
phones (`devices.py`, through `pool.wda_specs_from_config`).

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
procedure is in [benchmarks.md](https://github.com/RadishSoftware/mobster/blob/main/mobile_agent/docs/benchmarks.md#iosworld).

## Frontier step policy

`mobile_agent/frontier/` (`FrontierAgent`) lets a frontier model choose each
step, over Mobster's perception and control. The model sees the request, the
allowed apps, the task contract, a log of what each earlier turn did (its actions
and their effects, the notes it took, the items it marked, a changed plan), its
plan, up to 220 screen elements (`MAX_ELEMENTS`) by short ids (`e1`, `e2`, ...),
and a 448-pixel JPEG screenshot (`SCREENSHOT_SIDE`). The screenshot is the MJPEG
stream's newest frame when the pixel settle (`wda/frame_clock.py`) sees a still
screen, else a WDA capture.

The turn log (`FrontierAgent._logged_prompt`) is never rewritten: each finished
turn adds a block, and the prompt cache ends on the newest one, so only the turn's
own lines, the plan, the contract's item states, feedback and the screen are new
text each call. A done action's line keeps up to 200 characters of the row it acted
on and 600 of the text it typed (`LOG_LABEL_CHARS`, `LOG_TEXT_CHARS`). At 60 and 80, a
tapped row lost the neighborhood at its end and a sent reply lost its questions.
A long task keeps its notebook prompt instead. On the IRT 10
iOSWorld tasks (12 Oct 2026, 7 tasks complete in each arm, Claude Sonnet 5.5 low),
the log with 448-pixel screenshots sent 1,630 fresh input tokens a call against
3,001 with the 16-line action window and 640 pixels, cost $0.118 a task against
$0.189 and took 82 s against 108 s at the median.

Each call returns 1 to 6 chained actions (`MAX_CHUNK`) under a strict JSON schema.
Mobster runs them in order and stops the chain when a target is missing or an
action changes nothing. A `SET_TEXT` that changes nothing stops the chain before
its next action that isn't an input, unless its field or wheel already shows the
text (`frontier_chunk_stop` with `reason: "input_not_taken"`). A timer's wheel left at 25 min by `SET_TEXT "12"` no longer
starts a 25-minute timer. When the chain swipes twice in a row, the rows the list
showed between the swipes go into the next prompt once ("Rows your chained swipes
scrolled past"), less the rows on the screen before and after. Without them, a
`SWIPE_UP, SWIPE_UP` chain scrolled rows past that no prompt held. The code
enforces these guards:

- Only listed apps can be launched.
- A commit (send, pay, buy, delete, post, share, follow, call...) that the request
  does not ask for is refused: by the task contract's commit check, or by
  `frontier.guard` (via `task_policy.requested_by`) when the run has no contract.
  The model is told why, and the refusal is in the trace.
- Repeats are refused, judged on the screen: typed text repeated on the screen its
  typing left, a tap repeated on the screen it was made on (it did nothing), and a
  third identical action on the same screen within 12 (a loop). Working down a list
  (another "Pay" as each request is paid) is not a repeat. Swipes are exempt;
  DISMISS tries a different free point each time instead.
- A tap on a control whose first words name an act ("Add Food", "Add to Cart",
  "Save") that already changed this screen in this app is refused until the run
  has looked at another screen of that app it hadn't seen since that tap, such as
  the list the act adds to (`frontier_refused` with `label: "repeat_act"`).
- Limits: 150 actions per run (`MAX_ACTIONS`), plus the caller's `max_steps`,
  `max_seconds` and `max_cost_usd`. The last turn of any of them may only report
  (DONE or BLOCKED), so a run that hits a limit still answers.

Before the first turn, one text-only call (reasoning effort `none`, run while the
first screen is read) compiles the request into a task contract
(`contract.compile_contract`): READ, WRITE, COMMIT, REPORT and FORBID items in the
request's own words. The model sees it every turn and marks items in
`checklist_updates`. A commit the contract doesn't list is refused, and DONE is
accepted only when the screens the run saw show every item, or few turns are left.
A REPORT marked missing, or answered "none", needs a screen that showed it absent:
a search the run typed that came back empty, or the app's own "No results" or
empty list. Without one, DONE sends it back once. When the call fails or finds no items, the run has no contract: `frontier.guard`
refuses unasked commits, and DONE is sent back once for an item-by-item check of
the request.

Replies are compact: an action is `op`, `target` (an element id, a later screen's
label, or the app for LAUNCH_APP) and `text`; the thought is short and the plan is
sent only when it changes. On 12 unseen iOSWorld tasks (25 Sep) output fell from 288 to 161 tokens and
model time from 3.05 to 2.33 s a call, with the same rubric. `LONG_PRESS target`
holds 1.5 s (reaction pickers, context menus). On WDA a horizontal swipe is a fast
flick over the same distance, not XCTest's drag: paging views turn on it, and a list
row's swipe actions are revealed, not triggered.
`SET_TEXT` on a picker wheel (`WDA.set_value`) tries the item as written, then the
wheel's own spellings ("12" on a timer at "25 min" is "12 min"; an alarm's minute
"0" is "00"), then steps the wheel one item at a time, reading its value back after
each try. A wheel that doesn't reach the value fails the action with what it shows
(`WheelNotSet`), so a chained Start or Save doesn't run.

Two macro actions run in code without a model call per step: `READ_LIST` scrolls
the current list to its end and hands every row to the next turn, and
`SCROLL_TO text` scrolls until an item containing the text is on screen.
`READ_LIST` stops after 60 rows or 8 s. A row's own texts inside it (a card's
"Seat" and "12A" under its full label) are not rows, the same as in the prompt's
screen elements. A `READ_LIST` right after one that stopped early goes on from
where it stopped and hands over only rows it hasn't yet. On
12 unseen iOSWorld tasks (25 Sep) READ_LIST raised the research tasks' rubric and
cost about 18 s a use; see [benchmarks.md](https://github.com/RadishSoftware/mobster/blob/main/mobile_agent/docs/benchmarks.md#iterating-on-failures-without-reruns).

### Fixes from the frontier work (October 2026)

A tap within 4 s of a search's Return waits for two agreeing reads with the target in place, at most 3 s, and doesn't
tap while the results keep changing. It is a safety fix with no switch: two runs opened Messages and Settings by
tapping a result that had moved (9 Oct 2026).

`TYPE_SUBMIT` presses Return only when the field reads back exactly what was written; otherwise the receipt says
`submitted: false` and the model is told Return wasn't pressed. A `TYPE_SUBMIT` into text this task didn't write,
or into Safari's address field, replaces that text instead of appending to it. When typing times out, its outcome
is unknown and it is never sent again. If the field then reads back exactly the text written, the task contract
counts it as typed, so a Send after it is bound to it rather than refused as "no text you typed".

With a Claude key that isn't scoped to one workspace, set `ANTHROPIC_WORKSPACE_ID`: every request then carries the
`anthropic-workspace-id` header.

### Where it runs

The frontier policy is Mobster's agent: it runs every task in the Mac app, `mobster serve`,
the terminal UI and `mobster run` (`POST /api/runs` takes `"engine": "smart"`, the only
engine), and in the iOSWorld harness:

```sh
$PY -m mobile_agent.bench.iosworld run ... --policy frontier   # the Smart config: claude-sonnet-5-5, reasoning low
```

Both build the agent from one frozen config, `engines.SMART_CONFIG` (model, reasoning,
step and time limits), and `tests/test_engines.py` proves the two paths build the same
agent. The only deliberate difference is the spend cap: the app stops at the user's
per-task limit, the harness at `task_cost_cap`. The engine's API, events and costs are in [engine-contract.md](https://github.com/RadishSoftware/mobster/blob/main/mobile_agent/docs/engine-contract.md).

- `--policy`: `frontier`, the default and only value.
- `--model`: any model id. `model_clients.chat_client` sends `gemini-*` to Vertex AI
  (`GeminiChat`; needs `GOOGLE_CLOUD_PROJECT` and a `gcloud` login), `claude-*` to the
  Claude API (`AnthropicChat`; needs `ANTHROPIC_API_KEY`), and everything else to the
  OpenAI Responses API (`OpenAIChat`; needs `OPENAI_API_KEY`). The
  prompt's first part (SYSTEM, the schema, the request and the apps) ends in an
  explicit cache breakpoint on GPT-5.6 models, so it is billed at the cached rate
  after a task's first call; the answer is one forced function call.
  The default is `DEFAULT_FRONTIER_MODEL`, `engines.SMART_CONFIG.model` (`claude-sonnet-5-5` since 12 Oct 2026;
  `gpt-5.6-sol` before), so a default run needs `ANTHROPIC_API_KEY`.
- `--reasoning`: default `low` (`engines.SMART_CONFIG.reasoning`). OpenAI models get it as `reasoning.effort`, and
  Gemini maps `minimal`, `low`, `medium` and `high` to a thinking level. Claude gets adaptive
  thinking at that effort (`output_config.effort`); `none` (the task contract and structuring
  calls) is `thinking: between_tools` on Sonnet 5.5, `disabled` on models that take it, and
  effort `low` on Opus 5.5 and Fable, whose thinking is always on (`model_clients.anthropic_thinking`).

**Claude (`AnthropicChat`).** The Messages API, over a kept-alive connection:

- The answer is Claude's structured output (`output_config.format`, JSON Schema), not a forced
  tool call: Sonnet 5.5 and Opus 5.5 refuse `tool_choice` any/tool. `model_clients.anthropic_schema`
  closes every object and leaves out what the grammar can't take (`maxItems`, `pattern`,
  length and number limits); the loop checks those in code.
- Two cache breakpoints (5-minute entries): the SYSTEM prompt, shared by every task, and the
  request with its apps, shared by every turn. The first call of a task reads SYSTEM from the
  cache when another task ran within five minutes.
- The screenshot goes as a base64 image. OpenAI's `detail: low` has no counterpart: Claude
  sees the whole 640-pixel shot, about 1,150 visual tokens.
- Calls are hedged (`hedge.HEDGERS["anthropic"]`, `MOBSTER_HEDGE=0` turns it off): a call still
  running past the rolling p90 of its purpose (at least 4 s, 8 s until 20 calls are known) gets an
  identical twin on a fresh connection within a 10% budget, and one that failed in transit (429,
  5xx, a dropped connection) is sent once more at once. A call still running at three times that
  delay (at least 10 s) gets a rescue twin even when the budget is spent. A decision attempt that
  hasn't answered in 30 s fails as a stall, and when every attempt has failed in transit the call is
  sent once more. A call that runs out its deadline says how many requests were in flight. Inside an attempt a 529 (overloaded) and
  a 429 with `retry-after` are waited out, up to 5 times; a 429 without `retry-after` is a spend
  cap and fails. A losing twin still bills: its tokens are added to the client's usage (and
  counted in `usage["hedges"]`), so the spend cap and the task's cost include it, and in the app
  `costs.meter` records it as a call of its own (purpose `hedge`) in the usage ledger.
- A refusal (`stop_reason: refusal`), a reply cut at `max_tokens` (16,000) or a reply that is
  not JSON ends the turn with an error, as a failed call does.
- In the harness each task runs with at most 50 model calls (`MAX_STEPS`, iOSWorld's
  own limit), 900 s, and a spend cap that grows with the apps the task spans:
  $0.10 plus $0.08 per app, at most $0.60 (`task_cost_cap`, `MAX_TASK_COST_USD`).
  A task that reaches the cap ends with status `budget`. The 24 Sep smokes ran with
  a flat $0.25 cap.

HTTP 429 responses are retried with backoff up to 5 times
(`RATE_LIMIT_RETRIES`). OpenAI's `insufficient_quota` is not retried, because
waiting cannot fix an empty account.

### Costs

`model_clients.PRICES` holds list prices in USD per million tokens. The GPT-5.6 rates
were read from OpenAI's pricing page and model pages on 2026-09-25 (gpt-5.5 on
2026-09-24), the Gemini rates are Vertex AI's standard global rates, read on 2026-09-23, and the
Claude rates are platform.claude.com's pricing page, read on 2026-09-28 (cache write is
the 5-minute rate; Opus 5.5's cache hits are 0.05x input, Fable 5.1's 0.025x).
They are reporting estimates, not invoices.

| model | input | output | cached input | cache write |
| --- | ---: | ---: | ---: | ---: |
| `gpt-5.5` | 5.00 | 30.00 | 0.50 | = input |
| `gpt-5.6-sol` | 4.00 | 20.00 | 0.40 | 5.00 |
| `gpt-5.6-terra` | 2.00 | 12.00 | 0.20 | 2.50 |
| `gpt-5.6-luna` | 0.20 | 1.20 | 0.02 | 0.25 |
| `gpt-5-mini` | 0.25 | 2.00 | 0.025 | = input |
| `claude-sonnet-5-5`, `claude-sonnet-5` | 2.00 | 10.00 | 0.20 | 2.50 |
| `claude-opus-5-5` | 4.00 | 20.00 | 0.20 | 5.00 |
| `claude-opus-5` | 5.00 | 25.00 | 0.50 | 6.25 |
| `claude-fable-5-1` | 10.00 | 50.00 | 0.25 | 12.50 |
| `claude-haiku-4-5` | 1.00 | 5.00 | 0.10 | 1.25 |
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
| frontier `gpt-5.6-terra` (3 runs) | 0.17–0.22 | 126–201 |
| frontier `gpt-5.5` | 0.20 | 65 |
| frontier `gpt-5.6-luna` (2-task check) | 0.011 | 53 |

Before the cap existed, 50-step loops cost about $0.80 each on `gpt-5.5` (the
comment on `MAX_TASK_COST_USD`). The success rates behind these costs are in
[benchmarks.md](https://github.com/RadishSoftware/mobster/blob/main/mobile_agent/docs/benchmarks.md#measured-results-iosworld).
