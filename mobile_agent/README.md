# Mobster CLI: the `mobile_agent` package

The engine inside Mobster CLI and Mobster for Mac: an accessibility-first iPhone agent, and a loopback HTTP API for a local UI. Mobster's agent (`frontier/`, called `smart` in statuses) runs on your Claude or OpenAI key and handles long tasks across apps. `engines.py` builds it. It drives a real iPhone over USB, or a simulator, through WebDriverAgent. The automation contract is independent of UIKit, SwiftUI, React Native and web content; how much each app exposes is measured, not assumed (`evals/app_survey.py`). [architecture.md](https://github.com/RadishSoftware/mobster/blob/main/mobile_agent/docs/architecture.md) describes how a run works.

## Run locally

You need Python 3.12 or later (`brew install python@3.12`, or `uv python install 3.12`; the `python3` that ships with macOS is 3.9). From the repository root:

```sh
make venv                                   # python3.12 -m venv mobile_agent/.venv + requirements.txt
make test-backend                           # one process per test module, in parallel
mobile_agent/.venv/bin/python -m mobile_agent demo      # one scripted task on Mobster's agent: no phone, no keys
```

`mobile_agent/.venv/bin/python -m mobile_agent` with no command opens the terminal UI (`--demo` for the scripted phone). `make test-backend` is the CI gate. Backend tests live in `mobile_agent/tests/`; design notes live in `mobile_agent/docs/`. `python -m mobile_agent --version` prints the version, which `/api/status` also reports.

Start the local API:

```sh
mobile_agent/.venv/bin/python -m mobile_agent serve --manage-device --env-file mobile_agent/.env
```

API tasks are live-only; an unavailable iPhone never falls back to a synthetic run. The separate `demo` command runs Mobster's agent offline on a scripted phone and model (`tui/demo_phone.py`). The service persists up to 100 tasks in a private SQLite journal; it is a single-user runtime, not a multi-user deployment.

## iPhone setup

With `--manage-device` the agent owns the USB iPhone, and the setup API drives it ([setup API](https://github.com/RadishSoftware/mobster/blob/main/mobile_agent/docs/setup-api.md)): it finds Xcode and libimobiledevice, the connected and trusted iPhone, and your Apple development teams (from the login keychain); builds WebDriverAgent for that phone into the data folder; and supervises the runner and the USB relay (8100 API, 9100 video), resuming them on the next launch. The Mobster Mac app is one UI for that flow. Without a UI, follow the manual recipe in [usb-wda.md](https://github.com/RadishSoftware/mobster/blob/main/mobile_agent/docs/usb-wda.md) (`scripts/usb-wda.sh` keeps WDA serving).

Keys live in a private env file (`--env-file`, 0600; `POST /api/keys` writes `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` there). Never put keys in frontend code, command history, screenshots, or committed files. The API never returns model credentials, only a key's last four characters.

Run one task (the WDA session is adopted automatically). The agent asks on the terminal before it sends, buys, posts or deletes:

```sh
mobile_agent/.venv/bin/python -m mobile_agent run 'Open Search' --execute --wda-url http://127.0.0.1:8100 --env-file mobile_agent/.env
```

`--enable-live` (or the saved setup choice) is the explicit operator gate for API runs.

## Results and optional schemas

`POST /api/runs` accepts `appId`, `goal`, optional `outputSchema`, and `outputFormat` (`auto`, `text`, `json`, `yaml`, `csv`, or `markdown`, default `auto`). Mode defaults to `live`; the API rejects synthetic mode. Send a unique `Idempotency-Key` for each new task. Retrying that exact key and payload returns the existing task rather than dispatching again. A key reused with a different task, schema, or format returns a conflict. Existing JSON request keys remain compatible. Every `/api/*` route is also served under `/v1/*`; `/api/status` reports `api_version`.

Result format is selected before execution and persisted with the run. The API and extraction pipeline retain canonical JSON data; the dashboard deterministically presents and downloads it in the requested format. Schema authoring syntax (JSON or YAML) is a separate editor choice, parsed before submission. CSV requires an explicit flat-object or bounded array-of-flat-objects schema with named scalar columns. Nested data belongs in JSON, YAML, or Markdown; CSV quoting and spreadsheet-formula guards are presentation protections, not transformations of canonical evidence. Markdown does not generate a new narrative or additional facts.

All agent results include observed AX evidence. With a schema, `summary.data` contains the agent's JSON answer, `data_status` reports extraction state, and `schema_validated` reports local JSON Schema validation. Each non-null scalar must cite an exact observed literal. Schema validation and literal provenance do not prove semantic correctness or task completeness.

The supported Draft 2020-12 subset includes typed objects, arrays, scalar values, nullable types, enums, and basic bounds. Every object requires `additionalProperties: false`; arrays require `items` and `maxItems` of at most 100. Remote/local references, regular expressions, and composition keywords are rejected. Hidden content, inferred counts, transformed metrics such as `1.2K` into `1200`, and generated prose summaries are not literal extraction. Without a schema, `auto` output asks the model to choose a structure; every value must still cite observed text.

## Crash and failure behavior

The journal defaults to `mobile_agent/.state/mobster.sqlite3` in a source checkout and to `~/Library/Application Support/app.mobster.desktop/state/mobster.sqlite3` in an installed copy, never inside site-packages (override with `--state-db`). It uses WAL with synchronous commits, and saves action intent before dispatch. The database and lock files are private to the local user. Journals contain task goals and observed app text; they are not encrypted at rest. Do not publish or commit them.

Interrupted tasks are marked `interrupted` on restart and are never automatically resumed. Idempotency tombstones survive history eviction; replaying an expired task returns 410 instead of starting another. Process leases prevent two Mobster runtimes from controlling the same iPhone. These are local protections, not tenant authorization. Stop takes effect at operation boundaries; a synchronous UIKit action already underway cannot be undone, and a remote relay command already queued may still execute. Ambiguous actions are never retried automatically.

The API reserves a bounded terminal record outside the ordinary event budget, so an exhausted trace cannot prevent crash recovery. Final status, summary, and the terminal event commit together. Actual storage failures remain explicit and recovery fails closed. `mobster run` and the terminal UI keep the same guarantees in the terminal's journal, `terminal.sqlite3` in the same folder (`run --state-db` names another file), and a `mobster run` killed mid-task is marked `interrupted` the next time either starts. `mobster run --demo` keeps its scripted task in memory.

The dashboard shows the iPhone's live screen (WebDriverAgent's MJPEG stream, relayed by the agent) or an explicit unavailable state; it never shows a stale frame as live. Device health checks detect reachability, not successful sign-in or unrestricted app functionality.

## Several devices

A runtime drives every device in its fleet (`fleet.py`; ports, states and naming in `devices.py`): the primary phone (the one `--wda-url` or `--manage-device` names, served by the runtime's own `video`, `control` and `wda_session()`), each other USB iPhone set up here (its own pinned `DeviceManager`, relay on 8100+N, build folder and Setup), Mobster's simulators, and `MOBSTER_WDA_DEVICES` addresses. One task per device, tasks on different devices at once; each live task holds its device's `Lease.device(wda_url)`.

| request | answer |
| --- | --- |
| `GET /api/devices` | `{"devices": [Device], "defaultDevice": id or null}` |
| `GET /api/devices/{id}` | `{"device": Device}`; `{id}` is an id, UDID or URL-encoded name; 404 `device_not_found`, 400 `device_ambiguous` |
| `DELETE /api/devices/{id}` | forgets an extra USB iPhone (stops its runner, frees its ports): `{"forgotten": id}`; 409 `device_refused` for the primary or a busy device |
| `GET /api/devices/{id}/stream`, `/frame`, `/video/status`, `/info` | that device's live view and identity, as `/api/device/*` for the primary |
| `POST /api/devices/{id}/control` | a gesture on that device, as `/api/device/control`; 409 `device_busy` only while a task runs on that device; 403 `app_only` without the Mac app's session when the Mac app runs the agent |
| `GET /api/devices/{id}/setup` | that device's Setup checklist (as `/api/setup`), or `{"kind": "simulator", "complete", "device", "runner": {"state", "error"}}` |
| `POST /api/devices/{id}/setup/build` `{"team"}` | builds the runner for that iPhone; the first iPhone set up becomes the primary, any other gets the next port pair |
| `POST /api/devices/{id}/setup/start`, `/stop`, `/refresh` | starts or stops its runner (a simulator: boots it and starts WebDriverAgent, or shuts it down) |
| `POST /api/runs` `{..., "device"}` | runs there; without it, on the default device. 409 `run_active` (with `activeRunId`) while that device has a task; 503 `device_unavailable` (with `device`) when it isn't ready |
| `GET /api/runs?device={id}` | that device's history; runs saved before devices belong to the primary |
| `GET /api/apps?device={id}` | the apps a task on that device can start in |

`Device` is `{id, kind ("usb" | "simulator" | "wda"), name, udid, model, modelName, ios, state ("ready" | "busy" | "connected" | "needs_setup" | "disconnected"), reason, primary, setUp, default, activeRunId, approval, lastUsedAt}`. A run carries `device` and `deviceName`; a saved task carries `device` (null: `serve --device`, else the only device, else the primary; never the one used last). A workflow whose device is gone skips as `skipped_offline`.

## Deployment boundary

A client of the API (such as the desktop app's dashboard) selects apps and tasks, observes run events and the live screen, cancels at operation boundaries, and retains history. Mobster is a single-user, loopback-only runtime for your own iPhone, not a hosted service: it has no multi-user authentication, its traces are not encrypted at rest, and it has no rate limits. "Ask before acting" (`MOBSTER_ASK_BEFORE_ACTING`, on by default) pauses before actions Mobster recognizes as sending, buying, posting, deleting or submitting; it is a heuristic, not a guarantee, and typing without Return never asks. Camera, cellular, Apple Pay, biometrics, account permissions and app-specific anti-automation restrictions cannot be assumed merely because an app is installed.

## Verification status

Device runs use a USB iPhone 15 Pro (iOS 26). That is the only device measured; other iPhone models and iOS versions are untested. Known limits: drawn surfaces and text in images are invisible to the tree, and Chrome's hidden tab switcher leaks into the tree (web tasks use Safari).

## Sources

[WebDriverAgent](https://github.com/appium/WebDriverAgent), [libimobiledevice](https://libimobiledevice.org/), [UIKit accessibility](https://developer.apple.com/documentation/uikit/accessibility), [shadcn/ui](https://ui.shadcn.com/).
