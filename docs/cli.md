---
title: "CLI reference"
description: "Every mobster command and flag, with examples."
icon: "book-open"
---

Every command, flag and exit code of `mobster`. `mobster --help` and `mobster <command> --help` print the same information in the terminal. `python -m mobile_agent` is the same program as `mobster`.

| command | what it does |
| --- | --- |
| [`mobster`](#mobster) | open the terminal UI |
| [`mobster run`](#mobster-run) | run one task and print its steps |
| [`mobster verify`](#mobster-verify) | check an iOS app on a simulator and print the verdict |
| [`mobster doctor`](#mobster-doctor) | check the phone, WebDriverAgent, keys and tools |
| [`mobster sim`](#mobster-sim) | create, prepare and clean up Mobster's simulators |
| [`mobster devices`](#mobster-devices) | list the iPhones and simulators Mobster can use |
| [`mobster history`](#mobster-history) | list past tasks from the terminal UI |
| [`mobster export`](#mobster-export) | save a past task as a captioned GIF or MP4 |
| [`mobster screen`](#mobster-screen) | show the phone's screen in the terminal |
| [`mobster serve`](#mobster-serve) | start the local HTTP API |
| [`mobster mcp`](#mobster-mcp) | serve Mobster's checks to coding agents over MCP |
| [`mobster phone`](#mobster-phone) | set the iPhone's clipboard, move files, or install a build on it |
| [`mobster alerts`](#mobster-alerts) | test the webhook that hears about scheduled tasks |
| [`mobster chat`](#mobster-chat) | talk to Mobster's agent in a conversation that remembers |
| [`mobster test`](#mobster-test) | run your app's checks on simulators and iPhones, with reports |
| [`mobster memory`](#mobster-memory) | see and edit what Mobster remembers, and your routines |
| [`mobster wifi`](#mobster-wifi) | use an iPhone without the cable, over the encrypted Wi-Fi link |
| [`mobster demo`](#mobster-demo) | replay a scripted task offline |
| [`mobster version`](#mobster-version) | print the version |
| [`mobster tui`](#mobster-tui) | open the terminal UI (the same as `mobster`) |
| `mobster help [COMMAND]` | print a command's help, the same as `mobster COMMAND --help` (`mobster help sim prepare` too) |
| [`mobster build-ocr`](#developer-commands) | compile the optional Apple Vision OCR helper |
| [`mobster decide-fixture`](#developer-commands) | one Quick mode decision on a synthetic screen |

Two environment variables act as defaults for flags on most commands:

- `MOBSTER_WDA_URL` for `--wda-url` (otherwise `http://127.0.0.1:8100`)
- `MOBSTER_ENV_FILE` for `--env-file` (every command except `serve`)

`--device NAME` names a device from `mobster devices` (its id, UDID or name) on `run`, `screen`, `doctor`, `serve`, `mcp`, `verify` and the terminal UI ([Several phones](/devices)).

Options are spelled in full: `--exec` is not `--execute`. A mistyped command or option is a usage error that suggests the one you meant, such as `Did you mean --execute?`, and names the command whose usage it prints.

`--wda-url` and `--env-file` work before or after the command's name: `mobster --wda-url URL doctor` checks the same phone as `mobster doctor --wda-url URL`. `--demo`, `--resume` and `--app` belong to the terminal UI, and another command refuses them with a usage error. A `~` in a path is expanded, including in `--env-file=~/keys.env`.

## `mobster`

Opens the [terminal UI](/tui). In a pipe or a script, where stdin or stdout is not a terminal, it prints a pointer to `mobster run` and exits with code 2.

| flag | meaning |
| --- | --- |
| `--wda-url URL` | WebDriverAgent's address. Default: `$MOBSTER_WDA_URL`, else `http://127.0.0.1:8100`. |
| `--device NAME` | The device to use, from `mobster devices`: its id, UDID or name. Not with `--wda-url`. |
| `--env-file PATH` | Load `KEY=VALUE` lines (keys, models, settings). Variables already set in the environment win. Default: `$MOBSTER_ENV_FILE`. Settings changed in the UI are saved here. |
| `--demo` | Use a scripted phone and policy: no iPhone, no keys, no model calls. The agent, runtime, approvals and history are real. |
| `--resume [ID]` | Open a past task and put its request in the prompt. Without an ID, the latest. |
| `--app NAME` | Start with this app chosen: a name (`Messages`), catalog id (`messages`) or bundle ID, including an app installed on the phone that is not in the catalog. If the phone has no such app, the UI says so. |
| `--no-bell` | Do not ring the terminal bell when an approval appears. |
| `-V`, `--version` | Print `mobster <version>` and exit. |
| `-h`, `--help` | Show help and exit. |

## `mobster run`

```sh
mobster run TASK [--execute] [--helper] [--json]
                 [--wda-url URL] ...
```

Runs one task. Without `--execute` it reads the screen, shows the first decision and sends nothing to the phone. On a terminal each step prints as a line. When stdout is piped, or with `--json`, every agent event prints as one JSON object per line and the last line is the result ([Scripts and the API](/scripting#json-events)). `run` never asks for approval: with `--execute` it can send, buy, post or delete without a prompt, so review a task with a preview before you add `--execute`.

`run` runs Quick mode, so it needs `TYPESAFE_API_KEY`. It checks the key, and every flag, before it contacts the phone.

| flag | meaning |
| --- | --- |
| `TASK` | What to do, in plain words. Quote it. |
| `--execute` | Act on the phone. Takes the phone's device lock for the run. |
| `--helper` | Use the helper model (`TEXT_MODEL`) for text to type, answers and recovery hints. |
| `--json` | Print raw JSON events, even on a terminal. |
| `--wda-url URL` | WebDriverAgent's address. Default: `$MOBSTER_WDA_URL`, else `http://127.0.0.1:8100`. |
| `--device NAME` | Run on this device, from `mobster devices`: its id, UDID or name. Not with `--wda-url`. A device that isn't there or isn't set up yet exits with code 3. |
| `--session ID` | The WDA session to use. By default Mobster uses the session WDA is serving, else creates one. |
| `--env-file PATH` | Load `KEY=VALUE` lines. Default: `$MOBSTER_ENV_FILE`. |
| `--max-steps N` | Stop after N steps, from 1 to 10000. Default 30. |
| `--max-seconds S` | Stop after S seconds. Default 120. |
| `--spend-cap-usd USD` | Stop before further model calls once observed inference spend reaches USD. The run ends as `spend_cap`. |
| `--expected-text TEXT` | Finish as soon as TEXT is visible on the screen. |
| `--allow-app BUNDLE_ID` | Let the agent switch to this app. Repeat for several. Without it, the agent never switches apps. |
| `--long` | A long task. Not in this build yet: `run --long` says so and exits 3. |

Exit codes:

| code | meaning |
| --- | --- |
| 0 | The task finished (`completed_unverified` or `expected_text_visible`), or a preview was shown. |
| 1 | The task did not finish: it stopped, was blocked, hit a limit, or failed. The result's `status` says which. |
| 2 | A usage error, such as an unknown flag or `--max-steps 0`. |
| 3 | No phone answered at the WebDriverAgent address: the connection was refused, reset (iproxy with no phone behind it) or timed out, the host is unknown, or the server there is not WebDriverAgent. Also when `--device` names no device, or one that isn't set up yet. |
| 130 | Stopped with `ctrl+c`: the first stops the task at its next safe point, and the result's `status` is `stopped`; a second forces it. SIGTERM exits 143 and SIGHUP 129 the same way. In JSON the last line is still the `result`. |

## `mobster verify`

```sh
mobster verify [STEP ...] --bundle ID | --app PATH
               [expectations] [options]
mobster verify --check FILE [--app PATH]
               [--bundle ID] [--device NAME]
               [--runtime NAME] [--reset LEVEL] ...
```

Checks an iOS app on a headless simulator that Mobster creates and manages. Mobster installs and launches the app, runs the flow, and decides the verdict from your expectations, evaluated on the app's accessibility tree. A model never decides the verdict. The check file and the assertion language are described in `docs/checks.md`.

Each `STEP` is one step of the flow in plain English. What runs the flow:

| you give | what runs | model calls |
| --- | --- | --- |
| no steps | Launch-only: Mobster launches the app, opens `--open-url` if given, and checks | none |
| steps, and `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` is set | Mobster's agent (Smart) runs the steps on your key, then Mobster checks | Smart's, capped by `--max-usd` |
| steps, and no key or `--keyless` | nothing: a usage error (exit 3). Let your coding agent drive through `mobster mcp` instead | none |

Smart runs on a Claude model when your only key is Anthropic's, and on an OpenAI model with an OpenAI key. `MOBSTER_SMART_MODEL` picks another Claude or OpenAI model, and then only that provider's key will do.

| flag | meaning |
| --- | --- |
| `STEP` | One step in plain English. Repeat for several. None means a launch-only check. |
| `--check FILE` | Run a saved check (`.yaml`, `.yml` or `.json`). It can't be combined with `STEP`, `--text`, `--no-text`, `--visible`, `--absent`, `--expect`, `--launch-arg`, `--launch-env`, `--open-url` or `--name`. |
| `--app PATH` | The `.app` built for the iOS Simulator. It is installed before the run. |
| `--bundle ID` | The app's bundle ID. Default: read from `--app`. Without `--app`, the app must already be on the simulator. |
| `--device NAME` | A device from `mobster devices` (its id, UDID or name) runs the check on that device; a USB iPhone runs an app already on it (`--bundle`) and its data is never cleared. Any other value is the simulator device type. Default `iPhone 17 Pro`, else the newest available iPhone. |
| `--runtime NAME` | The iOS runtime, such as `iOS 26.4`. Default: the newest installed. |
| `--reset LEVEL` | `none`, `data` or `reinstall`. Default `reinstall` with `--app`, else `data`. Apps whose bundle ID starts with `com.apple.` are never reset. |
| `--launch-arg ARG` | A launch argument. Repeatable. Write `--launch-arg=-Flag` for one that starts with `-`. |
| `--launch-env KEY=VALUE` | A launch environment variable. Repeatable. |
| `--open-url URL` | A deep link opened after launch, such as `daybreak://paywall`. |
| `--text TEXT` | Expect TEXT on screen: some shown element's label or value contains it, ignoring case. Repeatable. |
| `--no-text TEXT` | Expect TEXT not on screen. Repeatable. |
| `--visible SEL` | Expect an element on screen. `SEL` is `key=value[,key=value]` with the keys `id`, `label`, `value`, `role`, `enabled` and `selected`, such as `label=Restore Purchases,role=button`. A value with a comma needs `--expect`. Repeatable. |
| `--absent SEL` | Expect no shown element to match `SEL`. Repeatable. |
| `--expect JSON` | Any expectation as JSON, such as `'{"count": {"id": "/^plan_/"}, "equals": 3}'`. Repeatable. |
| `--name TEXT` | The check's name in reports. Default: the first step, or "Launch check". |
| `--keyless` | Never call a model. A check with steps then can't run. |
| `--max-usd USD` | Smart's spend cap for the run. Default 0.25, at most 1.00. |
| `--max-seconds S` | Smart's time limit for the flow. Default 180, at most 600. |
| `--assert-timeout S` | How long expectations may wait for the screen to settle, from 0 to 60. Default 5. |
| `--out DIR` | Where runs go. Default `./.mobster/runs`. |
| `--save NAME` | Also save the check as `.mobster/checks/NAME.yaml` (`a-z`, `0-9` and `-`). An existing file is replaced, and stderr says so. |
| `--json` | Print the result as one JSON object. The default when stdout isn't a terminal. |
| `--env-file PATH` | Load `KEY=VALUE` lines, such as `OPENAI_API_KEY`. Default: `$MOBSTER_ENV_FILE`. |

Expectations are decided on one settled read: Mobster reads the tree, waits 0.3 s, reads again, and decides once two reads agree and every expectation holds, reading every 0.5 s until `--assert-timeout`. At the timeout the last read decides.

stdout carries one JSON object with `--json` or in a pipe (`schema: "mobster.verify/1"`, the same as the run's `result.json`), else a summary like this:

```text
✗ failed  A new user sees three plans on the paywall  (12.4 s)
  ✓ text "Choose your plan"
  ✗ count id=/^plan_/ == 3: found 2: plan_monthly, plan_annual
  Report  .mobster/runs/20261001-101500-3f9a/report.html
  Rerun   mobster verify --check .mobster/runs/20261001-101500-3f9a/check.yaml
```

Progress lines go to stderr, with `--json` too. Each run writes a folder in `--out`: `result.json`, a self-contained `report.html`, `check.yaml` (the check as a file, which `--check` runs again), `run.log` and `frames/`, including `NN-verdict-ax.jpg`, the verdict screen with every element Mobster read outlined. Smart and key-less runs that take steps also write `events.jsonl`: one line per key-less action, or Smart's events (each model call, decision and action, and the result, without prompt text or images). They also write `frameclock.jsonl`, how long each action's screen took to settle. A launch-only run writes neither. A run writes `.mobster/.gitignore` when it is missing, which leaves `runs/` and `build/` out of git, and each run folder is readable by you only.

Exit codes:

| code | verdict | meaning |
| --- | --- | --- |
| 0 | passed | Every expectation held on one settled read, and the app was in front. |
| 1 | failed | An expectation didn't hold, or the app wasn't in front at the end (it crashed or left). |
| 2 | needs review | Nothing decided the run: the check has no expectations, or every expectation already held before the steps ran. |
| 3 | couldn't run | The check or a flag is invalid (usage errors exit 3, not 2), or the machine couldn't run it: Xcode, the simulator, WebDriverAgent, the install, the launch, the model or the spend cap. `reason.class` says which. |
| 130 | couldn't run | Interrupted with `ctrl+c`: the run stops as `couldnt_run`, class `stopped`, with its folder. SIGTERM exits 143 and SIGHUP 129 the same way. With `--json` or in a pipe, stdout still gets the result, its `exit_code` the process's. |

With `--json` or in a pipe, stdout always gets exactly one result, a usage error (in an option before `verify` too) and an unexpected error included. A `--out` that can't be written is a usage error.

## `mobster test`

```sh
mobster test [PATH ...] [--sim TYPE[@RUNTIME] ...]
             [--device NAME ...] [--parallel N]
             [--retries N] [--repeat K] [options]
mobster test record --run RUN_ID [--name TEXT]
                    [-o PATH]
```

Runs every check in `.mobster/checks` (or the check files and folders given) with `mobster verify`'s runner, on one or more devices, with retries, flake detection and reports. Each check runs as `mobster verify --check` would: launch-only checks need no key, and checks with steps run Smart on your key. [Testing your app](/testing) walks through it, and [CI](/ci) has the recipes.

| flag | meaning |
| --- | --- |
| `PATH` | A check file or a folder of them. Default: `.mobster/checks` in the project (the folder holding `.mobster`, found from the working directory up). |
| `--sim TYPE[@RUNTIME]` | A simulator device type, with an optional runtime, such as `"iPhone 17 Pro@iOS 26.4"`. Repeatable: every check runs on each. Default: each check's own `device`, else `.mobster/matrix.yaml` when there is one. |
| `--device NAME` | A device from `mobster devices`. Repeatable. A real iPhone is reached only through this flag, never from a file in the repository. On it, a check drives only a build you installed: one Mobster installs from the check's `app.path` (an `.ipa` or a device build), or one devicectl lists as built by a developer. An App Store app couldn't run, and the app's data is never cleared. |
| `--matrix FILE` | A YAML file of simulators every check runs on: `simulators: ["iPhone 17 Pro@iOS 26.4", ...]`. Simulators only. |
| `--parallel N` | Simulators running checks at once, from 1 to 8. Default 1. Each run holds its simulator, so two never share one. Each iPhone runs one check at a time, beside them. |
| `--retries N` | Attempts after a failure, from a fresh app (the check's reset), from 0 to 5. Default 1, or 0 with `--repeat`. A check that passes on a retry passed, and is flaky. |
| `--repeat K` | Run each check K times (1 to 20) and report its pass rate. A check passes only if every run passed. |
| `--strict` | A flaky check fails the run. |
| `--quarantine FILE` | Checks that run and are reported but never fail the run. Default `.mobster/quarantine.yaml`. |
| `--tag TAG` | Only the checks with this tag (a check file's `tags`). Repeatable. |
| `--shard I/N` | Run the I-th of N even shards of the checks, such as `2/4`, for several CI machines. |
| `--keyless` | Never call a model: checks with steps couldn't run. |
| `--junit FILE` | Also write the JUnit XML here. |
| `--html DIR` | Also write the HTML report, with a copy of each run's report, here. |
| `--video` | Record each simulator run's screen; the report links each video. |
| `--json` | Print `results.json` on stdout instead of the summary. |
| `--assert-timeout S` | How long expectations may wait for the screen to settle, from 0 to 60. Default 5. |
| `--run RUN_ID` | With `record`: the finished task to make a check from. |
| `--name TEXT` | With `record`: the check's name. Default: the task's request. |
| `-o PATH`, `--output PATH` | With `record`: write the check here instead of printing it. |
| `--env-file PATH` | Load `KEY=VALUE` lines, such as `ANTHROPIC_API_KEY` for Smart steps. Default: `$MOBSTER_ENV_FILE`. |

Every run writes `.mobster/test-results/<suite-id>/`: `results.json` (`schema: "mobster.test/1"`), `junit.xml` (one `<testsuite>` per device, one `<testcase>` per check), `index.html` (the checks × devices matrix, the flaky table and what needs attention) and `runs/`, a copy of each run's own report, so the folder stands on its own as a CI artifact. Each attempt also has its usual folder in `.mobster/runs`.

`mobster test record --run RUN_ID` turns a finished task from the Mobster app or `mobster serve` into a check: its steps in plain words, and what it proved as expectations. It names no device, so it runs on a simulator. It prints the check, or writes it with `-o`.

Exit codes, from the checks that aren't quarantined:

| code | meaning |
| --- | --- |
| 0 | Every check passed (flaky ones too, without `--strict`). |
| 1 | A check failed, or was flaky with `--strict`. |
| 2 | None failed or couldn't run, and one needs review. |
| 3 | None failed and one couldn't run, or `mobster test` was used wrongly (usage errors exit 3, not 2). |
| 130 | Interrupted with `ctrl+c`: the runs in flight stop and the reports are still written. SIGTERM exits 143 and SIGHUP 129. |

## `mobster doctor`

Checks everything a live task needs and prints the fix for each problem. It changes nothing on the phone. Keys are reported as set or not set, never shown.

| flag | meaning |
| --- | --- |
| `--wda-url URL` | The WebDriverAgent to check. Default: `$MOBSTER_WDA_URL`, else `http://127.0.0.1:8100`. |
| `--device NAME` | Check this device, from `mobster devices`. A simulator skips the USB checks, and an iPhone set up besides the first has its own runner checked. Not with `--wda-url`. |
| `--env-file PATH` | Load `KEY=VALUE` lines before checking. Default: `$MOBSTER_ENV_FILE`. |
| `--json` | Print `{"ok": bool, "checks": [{"key", "label", "state", "detail", "fix"}]}`. `state` is `ok`, `warn`, `fail` or `skip`. |
| `--simulator` | Skip the USB iPhone, Xcode and runner checks, for WDA on a simulator. |

The checks, in order: Python, the install, the model key (with only an OpenAI key, a warning that `mobster run` needs `TYPESAFE_API_KEY`), the helper model, Xcode, libimobiledevice, the USB iPhone, the managed WDA runner, WebDriverAgent, the lock screen and the device lock. Exit code 0 when nothing failed (warnings are allowed), 1 otherwise.

## `mobster sim`

```sh
mobster sim <command> [options]
```

Manages the headless simulators that `mobster verify` and `mobster mcp` run on. Mobster creates each one with `xcrun simctl create`, named `Mobster · <device type> · <runtime>` (a second one for the same pair gets ` · 2`), boots it without opening Simulator.app, and starts WebDriverAgent on it, listening on `127.0.0.1` only. A simulator and its WebDriverAgent stay up after a run, so the next run starts warm. `python -m mobile_agent.sim` is the same program.

Every run gets the same simulator setup. The status bar reads 9:41. The keyboard types exactly what it is sent: autocorrection, prediction, smart quotes and smart dashes are off, and the first-keyboard introduction to slide typing is marked as seen. A system alert that an earlier run left on screen is dismissed before the next run starts. On iOS 26, a link opened with `simctl openurl` stops at an "Open in “\<App>”?" prompt until someone presses Open for that URL scheme. When Mobster installs an app, it records Open for every scheme the app declares, so the app's deep links open straight away. For an app Mobster didn't install, Mobster presses Open on the prompt. On a busy Mac, a first boot can outlast its 240 s limit, and so can WebDriverAgent's start. Run the command again; the simulator keeps booting in the meantime.

Mobster changes only the simulators it created. Their UDIDs are kept in `simulators.json` in the data folder: `$MOBSTER_DATA_DIR`, else `~/Library/Application Support/app.mobster.desktop/dev`. A UDID missing from that list is never shut down, erased or deleted, whatever its name.

| command | what it does |
| --- | --- |
| `mobster sim list [--json]` | List Mobster's simulators: name, state, WebDriverAgent address, whether a run is using it, and UDID. |
| `mobster sim prepare [--device NAME] [--runtime NAME] [--json]` | Create the simulator if needed, boot it, build WebDriverAgent the first time, start it, and print how long each phase took. |
| `mobster sim shutdown [UDID]` | Stop WebDriverAgent and shut down every Mobster simulator that isn't running a check, or the one named. |
| `mobster sim erase UDID` | Shut down and erase one Mobster simulator. The next run boots it fresh. |
| `mobster sim delete (UDID \| --all)` | Shut down and delete one Mobster simulator, or all of them. Only UDIDs in the registry whose names start with `Mobster · `. |
| `mobster sim prune [--json]` | Delete Mobster simulators unused for 14 days, WebDriverAgent builds for Xcode versions no longer installed, and registry entries for simulators that are gone. |
| `mobster sim doctor [--fix] [--json]` | Check what `mobster verify` needs and print the fix for each problem. |

| flag | meaning |
| --- | --- |
| `--device NAME` | The simulator device type, such as `iPhone 16 Pro`. Default: `iPhone 17 Pro`, else `iPhone 16 Pro` or `iPhone 15 Pro`, else the newest iPhone the runtime supports. |
| `--runtime NAME` | The iOS runtime, such as `iOS 26.4` or `26.4`. Default: the newest installed. |
| `--all` | With `delete`: every Mobster simulator. |
| `--fix` | With `doctor`: prepare the simulator (create, boot, build and start WebDriverAgent), then check again. It never runs `sudo` and never downloads a runtime; it prints those commands. |
| `--json` | Print JSON. `doctor --json` prints `{"ok": bool, "checks": [{"key", "label", "state", "detail", "fix"}]}`, the same shape as `mobster doctor --json`. |

`doctor` checks, in order: Apple silicon, Xcode (selected, not the Command Line Tools), Xcode's first-launch components, an iOS runtime, the device type, `git`, 5 GB free for the data folder, the WebDriverAgent build, Mobster's simulators and their WebDriverAgent, ports, and Smart's key: your Claude or OpenAI key (`ANTHROPIC_API_KEY` or `OPENAI_API_KEY`). The key row names the model Smart will run on. It warns when `MOBSTER_SMART_MODEL` names a model whose provider's key isn't set while the other provider's is. A key is reported as set or not set, never shown.

WebDriverAgent is built once per Xcode version, unsigned, from the pinned release, into `wda/` in the data folder. Each simulator's WebDriverAgent listens on a port from `MOBSTER_SIM_PORT_BASE` (default 8310) up to 39 above it, and its MJPEG stream on that port plus 1000. Mobster never uses 8100, 8200–8299, 8765, 9100 or 9200–9299. A port another program holds moves the simulator to the next free pair.

| variable | meaning |
| --- | --- |
| `MOBSTER_DATA_DIR` | Where the registry, the WebDriverAgent build and the logs go. |
| `MOBSTER_SIM_PORT_BASE` | The first WebDriverAgent port. Default 8310. |
| `MOBSTER_MAX_SIMS` | How many simulators Mobster keeps per device type and runtime. Default 2. When all of them are running checks, the next run waits for one to finish. |

Exit codes: 0 when the command succeeded (for `doctor`, when nothing failed), 1 when a check or an action failed, 2 for a usage error, 130 when interrupted with `ctrl+c`.

## `mobster devices`

Lists every device Mobster can drive, and changes nothing: USB iPhones (set up, or plugged in and not set up yet), Mobster's simulators, and WebDriverAgent addresses from `MOBSTER_WDA_DEVICES`. Each line shows the device's name, kind, model and iOS version, its state (`ready`, `busy`, `connected`, `needs setup` or `disconnected`) with what to do about it, its id and its WebDriverAgent address. [Several phones](/devices) explains the states and the ports.

| flag | meaning |
| --- | --- |
| `--json` | Print `{"devices": [{"id", "kind", "name", "udid", "model", "modelName", "ios", "state", "reason", "primary", "setUp", "wdaUrl", "mjpegUrl"}]}`. |

## `mobster history`

Lists the terminal UI's past tasks, newest first: id, time, outcome, app and request. It reads the journal without locking it, so it works while the UI is open.

| flag | meaning |
| --- | --- |
| `--json` | One JSON object per task: `id`, `app`, `goal`, `status`, `createdAt`, `finishedAt` (milliseconds) and `reason`. |
| `-n`, `--limit N` | Show N tasks, 1 to 10000. Default 20. |

## `mobster export`

Saves a finished task as a captioned GIF: it opens on the task as you typed it, then shows each step's screen in a phone frame with what Mobster did under it, the approval card if Mobster asked before acting, and the result. Each step holds 1.2 to 2.5 seconds by its caption's length, and the result holds 1.5 seconds longer. The GIF stays under X's 15 MB limit: for a long task it uses fewer colours, drops the cross-fades, then keeps at most 60 steps. It reads the terminal UI's history and the Mac app's, and works while either is open.

```sh
$ mobster export latest --gif
Saved Mobster – Text Alex that I'm running late.gif (1.6 MB, 15 s) to ~/Downloads
It shows every screen of the task. Check it before you post it, or save it again with --redact.
```

| flag | meaning |
| --- | --- |
| `ID` | The task's ID (`mobster history` lists the terminal's; `mobster chat --json` prints one), its first few characters, or `latest` for your newest finished task, from the terminal or the Mac app. |
| `--gif` | Save a GIF. |
| `--mp4` | Save an MP4 too, 936 × 1170 at 30 frames a second. It needs ffmpeg (`brew install ffmpeg`). |
| `--redact` | Blur text fields and message text on every screen, hide quoted text in the captions, and leave out the answer and the approval's wording. A screen saved before Mobster kept where its fields are is blurred all over, except the status bar. |
| `--theme paper\|night` | The ground: `paper` (light, the default) or `night` (dark). |
| `-o`, `--output PATH` | A folder to save in, or a file name to save to (an existing file there is replaced). Default: `~/Downloads`, where a file is never replaced: the second save is `… (1).gif`. |
| `--json` | Print `{"ok", "id", "gif": {"name", "path", "bytes", "frames", "seconds"}, "mp4": {"name", "path", "bytes"}}`. |

The GIF shows every screen the task saw, so look at it before you post it. Screens after a sign-in code are left out, whatever the flags. Nothing is uploaded. Exit code 1 when there's no such task, the task has no screens (a task from before Mobster kept them, or a demo), it's still running, or `--mp4` has no ffmpeg; 2 when neither `--gif` nor `--mp4` is given.

## `mobster screen`

Prints the phone's current screen. kitty and Ghostty get the kitty graphics protocol, iTerm2 and WezTerm get iTerm2 inline images, and other terminals get a drawing in half-block characters. It reads WebDriverAgent's screenshot and taps nothing.

| flag | meaning |
| --- | --- |
| `--wda-url URL` | Default: `$MOBSTER_WDA_URL`, else `http://127.0.0.1:8100`. |
| `--device NAME` | Show this device's screen, from `mobster devices`. Not with `--wda-url`. |
| `--out FILE` | Save the PNG to FILE instead of printing it. Required when stdout is not a terminal. |
| `--width COLUMNS` | Draw it this many columns wide. |

Exit code 3 when WebDriverAgent returns no screen.

## `mobster serve`

Starts the loopback HTTP API (`127.0.0.1` only) that the Mobster Mac app launches as its sidecar. Every request needs the launch's API token. [Scripts and the API](/scripting#the-http-api) shows how to use it.

| flag | meaning |
| --- | --- |
| `--port PORT` | The port on `127.0.0.1`, 0 to 65535. Default 8765. `0` picks a free port, and the service line names it. |
| `--wda-url URL` | Drive this WebDriverAgent. With `--manage-device` the default is `http://127.0.0.1:8100`. |
| `--device NAME` | The device a task that names none runs on, from `mobster devices`. Default: the only device, else the one the last task ran on, else the first iPhone. Without `--wda-url` or `--manage-device`, the server drives that device. |
| `--manage-device` | Own the USB iPhone: build, run and supervise WebDriverAgent ([Device setup](/device-setup#guided-setup)). |
| `--data-dir PATH` | Where the managed device keeps its files. Default `~/Library/Application Support/app.mobster.desktop`, shared with the Mac app. |
| `--env-file PATH` | Load `KEY=VALUE` lines. The setup API saves keys and settings here. `serve` does not read `$MOBSTER_ENV_FILE`. |
| `--session ID` | A preferred WDA session. The session WDA is serving always wins. |
| `--enable-live` | Allow tasks to act on the phone. A saved `MOBSTER_ENABLE_LIVE` of `0` or `1` overrides it. |
| `--spend-cap-usd USD` | Stop each run before further model calls once its spend reaches USD. |
| `--state-db PATH` | The task journal. Default: `mobile_agent/.state/mobster.sqlite3` in a source checkout, else `<data dir>/state/mobster.sqlite3`. |
| `--exit-with-parent` | Shut down when the launching process exits. The Mac app passes it. |
| `--keep-runner` | Leave the iPhone runner running on exit, for development reloads. The next server adopts it. |

## `mobster mcp`

Serves Mobster's tools to a coding agent, such as Claude Code or Codex, over MCP on stdin and stdout. The agent calls `verify_start` with the app and the expectations, drives the app on Mobster's headless simulator with `screen`, `tap`, `type_text`, `swipe`, `alert`, `open_url` and `relaunch`, and gets the verdict from `verify_finish`. The verdict comes from the assertions, checked against the accessibility tree, never from a model. With a Claude or OpenAI key, `verify` runs the steps itself (Smart), on the model Smart picks for that key or the one `MOBSTER_SMART_MODEL` names. The server's instructions, the `verify` tool's description and `status` name that model and whose key pays for it. When Smart is off, `status` and the log say why, such as a `MOBSTER_SMART_MODEL` whose provider has no key set.

```sh
# Add the server to every agent client on this Mac
mobster mcp install --all

# What those clients run: the server, on stdio
mobster mcp
```

Every tool call returns within 45 seconds, and `wait` within 50. Work that takes longer, such as the first simulator boot or a Smart run, continues under a `run_id`, and the agent calls `wait` for it. One run is open at a time per server. A run started right after `stop` waits until the stopped run has released its simulator, so the server never boots a second one for it. A key-less run idle for 15 minutes is stopped, and any run ends after 60 minutes. Stdout carries only the protocol, and the log goes to stderr. The server exits with 0 when the client disconnects, and stops its open run first.

| flag | meaning |
| --- | --- |
| `--env-file PATH` | Load `KEY=VALUE` lines. `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` turns on `verify` (Smart). Variables already set win. Default: `$MOBSTER_ENV_FILE`. |
| `--keyless` | Never offer `verify`, even with a Claude or OpenAI key. The coding agent drives every run. |
| `--device NAME` | The device for runs and phone tools that don't name one: a device from `mobster devices` (its id, UDID or name), or a simulator device type. Default `iPhone 17 Pro`, else the newest available iPhone. |
| `--allow-device NAME` | Drive only this real iPhone (its id, UDID or name), and refuse any other; repeatable. `list_devices` shows the others as `not_allowed`. Without it every set-up device is allowed. Simulators always are. |
| `--allow-files` | Offer the file tools (`put_file`, `get_file`, `list_files`) when this build has them. Off by default. The camera roll is never reachable over MCP. |
| `--runtime NAME` | The iOS runtime for runs that don't name one, such as `iOS 26.4`. Default: the newest installed. |
| `--out DIR` | Where runs go. Default: `$MOBSTER_RUNS_DIR`, else `.mobster/runs` in the folder the client starts the server in. When that folder is `/` or your home folder, runs go to `runs/` in Mobster's data folder. Saved checks go to the sibling `checks/` folder. |
| `--log FILE` | Also append the server's log to FILE. The log never holds keys, and it cuts typed text to 40 characters. |

### `mobster mcp install`

```sh
mobster mcp install [CLIENT ...|--all]
                    [--project DIR]
                    [--allow-device NAME]
                    [--env-file PATH] [--with-skill]
                    [--dry-run] [--remove] [--json]
```

Adds Mobster's server to agent clients' configs, with the absolute path of this `mobster`: Claude Code and Codex through their own `mcp add` commands, the others by editing the file in place, keeping its comments and formatting, with the old file copied to `<file>.mobster-backup`. Running it again changes nothing. It never writes a key. The clients are `claude-code`, `codex`, `cursor`, `vscode`, `claude-desktop`, `opencode`, `gemini`, `windsurf`, `zed`, `goose`, `amp`, `cline` and `omp`; [MCP server](/mcp-server#set-up-your-client) shows the entry each one gets.

| flag | meaning |
| --- | --- |
| `CLIENT` | A client to set up, by name. An unknown name is a usage error that suggests the closest one. A client that isn't installed is skipped and said. |
| `--all` | Every client installed on this Mac. |
| `--project DIR` | Write the project's file in DIR (`.mcp.json`, `.cursor/mcp.json`, `.vscode/mcp.json`, `.codex/config.toml`, `opencode.json`, `.gemini/settings.json`, `.zed/settings.json`, `.amp/settings.json`, `.omp/mcp.json`) instead of yours. Claude Desktop, Windsurf, Goose and Cline have none. |
| `--allow-device NAME` | Add `--allow-device NAME` to the server's arguments: it drives only the real iPhones you name. Repeatable. |
| `--env-file PATH` | Add `--env-file PATH` to the server's arguments, for Smart. The file must exist and be readable by you only (`chmod 600`), or it is a usage error. |
| `--with-skill` | Also copy the mobster skill into each client's skills folder: `~/.claude/skills` for Claude Code, `~/.codeium/windsurf/skills` for Windsurf, `~/.agents/skills` for the others. |
| `--dry-run` | Print what would change, as a diff or the command it would run, and write nothing. |
| `--remove` | Take Mobster's entry (and, with `--with-skill`, the skill) out instead. |
| `--json` | Print `{"ok", "dry_run", "remove", "server": {"command", "args"}, "changes": [{"client", "action", "path", "method"}], "skills": [...]}`. `action` is `added`, `updated`, `unchanged`, `removed`, `absent`, `skipped` or `error`. |

Exit codes: 0 done, 1 a client was skipped or failed (the others still ran), 2 a usage error.

### `mobster mcp clients`

Lists the clients Mobster knows: whether each is installed here, whether its config runs Mobster, and the file. `broken` means the entry runs a `mobster` that no longer exists. `--project DIR` looks at the project's files instead, and `--json` prints `{"clients": [{"id", "name", "detected", "configured", "path", "command", "command_ok", "skills_dir", "skill"}]}`.

### `mobster mcp doctor`

Starts the server as an app opened from the Dock would (the command `install` writes, launchd's `PATH`, your home folder), sends `initialize`, lists the tools and calls `status`, the one tool that only reads, and prints how long each took. Then it checks that every installed client's entry runs a `mobster` that exists. `--env-file PATH` starts the server with that file, and `--json` prints the report. Exit code 0 when everything worked, else 1.

## `mobster demo`

Runs the agent loop once against a scripted screen and model, offline. It prints steps on a terminal and JSON lines when piped. It is a fixture for trying the output and for tests, not a benchmark. For the terminal UI with a scripted phone, run `mobster --demo`.

| flag | meaning |
| --- | --- |
| `--json` | Print raw JSON events, even on a terminal. |

## `mobster version`

Prints `mobster <version>`.

| flag | meaning |
| --- | --- |
| `--json` | Print `{"version", "python", "platform", "tls"}`; `tls` names the CA file HTTPS verifies against (`caFile`) and how many CAs it loads (`caCerts`). |

## `mobster tui`

The same as `mobster` with no command, with the same flags: `--wda-url`, `--device`, `--env-file`, `--demo`, `--resume`, `--app` and `--no-bell`.

## `mobster phone`

```sh
mobster phone clipboard set (TEXT | - | --file FILE)
                            [--device NAME] [--json]
mobster phone clipboard get [--device NAME] [--json]
mobster phone install PATH [--device NAME] [--json]
```

Phone utilities that aren't tasks. Each one takes the device's lock while it works, as a task does, so it waits for nothing and fails at once when a task is using the phone.

| command | what it does |
| --- | --- |
| `mobster phone clipboard set TEXT` | Put TEXT on the phone's clipboard. `-` reads standard input, and `--file FILE` a UTF-8 file. At most 64 KB of text. |
| `mobster phone clipboard get` | Print the phone's clipboard. iOS lets only the app in front read the clipboard, so WebDriverAgent's runner comes to the front for a moment and the app on screen goes to the background and back. Only the CLI reads the clipboard: no MCP tool does, because it may hold a password. |
| `mobster phone install PATH` | Install a signed `.app` or `.ipa` on a USB iPhone with `xcrun devicectl`, or on a Mobster simulator with `xcrun simctl`. An `.ipa` is unpacked first. Progress lines go to stderr. |

| flag | meaning |
| --- | --- |
| `--device NAME` | The device, from `mobster devices`: its id, UDID or name. Default: your iPhone (the primary one), else the only device. |
| `--file FILE` | With `clipboard set`: the file whose text goes on the clipboard. |
| `--json` | Print one JSON object: `{"ok": true, ...}`, or `{"ok": false, "error": {"code", "message", "fix"}}`. `install` prints `{"ok", "device", "name", "path", "bundle_id", "seconds"}`. |

`install` explains the usual failures in plain words: the build isn't signed for this iPhone (`not_signed`), Developer Mode is off (`developer_mode`), the iPhone is locked (`locked`), or the device isn't reachable (`no_device`).

Exit codes: 0 done, 1 it failed, 2 a usage error (a missing or wrong path, text over 64 KB), 3 no such device or no Xcode tools, 130 interrupted with `ctrl+c`.

## `mobster alerts`

```sh
mobster alerts test [--url URL] [--json]
```

Mobster posts to the webhook in `MOBSTER_ALERT_WEBHOOK_URL` when a scheduled task fails, stops before it finishes, or waits a minute or more for your approval. Set it in the Mac app's Settings or in the env file. The address must be https, and never one on your Mac or its local network. Slack, Discord and ntfy addresses get their own format, and any other address gets JSON: `{"workflow", "status", "reason", "runId", "at", "text"}`. The post never holds the task's answer, a screenshot or anything it typed. A webhook that is slow or down never holds up or fails a task.

`mobster alerts test` sends a test post to that address, or to `--url`.

| flag | meaning |
| --- | --- |
| `--url URL` | Send to this https address instead of the saved one. |
| `--json` | Print `{"ok", "message"}`. |

Exit codes: 0 the service took it, 1 it didn't, 2 no usable address.

While a scheduled task is due within 10 minutes, or one is running, Mobster keeps the Mac from idle sleep with `caffeinate`, so the schedule isn't missed. A Mac whose lid is closed on battery still sleeps. `MOBSTER_KEEP_AWAKE=0` turns this off.

## `mobster chat`

Talk to Mobster's agent in a conversation, from Terminal. It hands each message to the Mobster app or `mobster serve`, whichever is running, so the conversation is the same one the Mac app shows and approvals appear where they always do. With neither running, it runs the task itself: `mobster chat "message"` prints the task's steps and asks for approvals in this terminal, and `mobster chat` alone opens the terminal UI on your last conversation. Those conversations are kept in the terminal UI's history, not the Mac app's. With `--url`, it talks only to that address, and when nothing answers there it says "Open Mobster or run `mobster serve` to chat." and exits 3. A follow-up sees what the earlier tasks found ("now reply to her"), and a message sent while a task runs steers it.

```sh
# A conversation in this terminal
mobster chat

# One message, its steps and the answer
mobster chat "What's on my calendar tomorrow?"

mobster chat --thread 3f9c2a1b7d0e \
  "Now text Sam the first one"
mobster chat --new --json \
  "Find my last message from Kate Bell"
```

| flag | meaning |
| --- | --- |
| `MESSAGE` | What to ask. Leave it out for a conversation on a terminal; piped text is one message. |
| `--thread ID` | Continue this conversation. Without it, `mobster chat` continues the one this terminal used last. |
| `--new` | Start a new conversation. |
| `--device NAME` | The iPhone or simulator (id, UDID or name). Default: the conversation's phone. |
| `--json` | Print one JSON object when the task finishes or needs you. Never answers an approval. |
| `--wait SECONDS` | With a message: how long to follow the task (default 300). It goes on after. |
| `--url URL` | Mobster's address on this Mac (default `$MOBSTER_URL`, else `http://127.0.0.1:8765`). |

In a conversation, `/new`, `/threads`, `/open ID`, `/stop`, `/pause`, `/attach PATH`, `/help` and `/quit` do what they say.

Approvals: Mobster asks before it sends, buys, posts or deletes. With the Mac app open you choose Send there, and here `n` declines and `x` stops. With `mobster serve` on a terminal, typing `y` and Return approves the exact text shown above it; typed words never approve anything. `--json`, and any run without a terminal, print `{"status": "waiting_for_approval", ...}` and exit 2. With neither the app nor `serve` running, `y` or `n` answers on the terminal; with `--json` or no terminal, Mobster doesn't do that step, and the task ends with `approval_denied` (exit 1).

A message that only asks Mobster to remember something runs no task: Mobster offers to remember it, `y` on a terminal remembers it, and `--json` prints `{"status": "remember", "proposals": [...]}` (exit 0).

Exit codes: 0 done, 1 the task ended without finishing, 2 it waits for you (an approval or a question), 3 couldn't run, 4 still working when `--wait` ran out.

[Conversations](/conversations) has the HTTP API and the MCP tools.

## `mobster memory`

See and edit what Mobster's agent remembers about you, from Terminal. It reads and writes the same file as Settings › Memory in the Mac app, on this Mac.

```sh
# Pinned things first, then the newest
mobster memory list

mobster memory add "My gym is the one on 5th Street"
mobster memory export > memory.md
```

`list`, `add`, `rm`, `pin`, `unpin`, `clear` and `export` do what they say; `mobster memory <command> --help` has each one's flags. `mobster memory routines` says routines aren't in this build yet.

Exit codes: 0 done, 1 refused or not found, 2 usage, 3 memory can't be opened here (saved by a newer Mobster, or no home folder).

[Memory](/memory) has what goes with a task, suggestions and the HTTP API.

## `mobster wifi`

A preview that's off unless you turn it on with `MOBSTER_WIFI_TRANSPORT=1` in your env file. After you set an iPhone up with a cable once, Mobster can reach it over Wi-Fi, through the encrypted link between this Mac and the phone.

| command | what it does |
| --- | --- |
| `mobster wifi status` | How each iPhone set up here is connected, and whether Wi-Fi is on for it. Reads only |
| `mobster wifi on --device NAME` | Let Mobster reach the iPhone over Wi-Fi when it's unplugged. Plug it in first |
| `mobster wifi off --device NAME` | Use the iPhone only with the cable again |
| `mobster wifi probe --device NAME` | Check the encrypted link to an unplugged iPhone, step by step |

[Wi-Fi](/wifi) has the details, and [CLI options](/reference/cli#mobster-wifi) every flag.

## Developer commands

| command | what it does |
| --- | --- |
| `mobster build-ocr` | Compile the optional Apple Vision OCR helper (`ocr.swift`) with Xcode. It goes to `mobile_agent/.build/` in a checkout, else to the data folder's `build/`. |
| `mobster decide-fixture [--goal TEXT] [--env-file PATH]` | Ask Quick mode's model for one decision on a synthetic screen and print it. Needs `TYPESAFE_API_KEY`. `--goal` defaults to "Open Search". |

Benchmarks and evaluations have their own entry points: `python -m mobile_agent.bench`, `python -m mobile_agent.bench.iosworld` and `python -m mobile_agent.evals.harness` ([Benchmarks](/benchmarks)).
