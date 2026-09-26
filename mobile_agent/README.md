# Mobster CLI: the `mobile_agent` package

An accessibility-first iPhone agent with Jev for typed decisions, an optional small language model for text, extraction and recovery, and a loopback HTTP API for a local UI. Jev receives the first decision directly: there is no mandatory LLM planning pass. It drives a real iPhone over USB through WebDriverAgent. The automation contract is independent of UIKit, SwiftUI, React Native and web content; how much each app exposes is measured, not assumed (`evals/app_survey.py`).

## Run locally

You need Python 3.12 or later (`brew install python@3.12`, or `uv python install 3.12`; the `python3` that ships with macOS is 3.9). From the repository root:

```sh
make venv                                   # python3.12 -m venv mobile_agent/.venv + requirements.txt
mobile_agent/.venv/bin/python -m unittest discover -s mobile_agent -t .
mobile_agent/.venv/bin/python -m mobile_agent demo      # offline fixture replay: no phone, no keys
```

`make test-backend` runs the same suite (the CI gate). Backend tests live in `mobile_agent/tests/`; design notes live in `mobile_agent/docs/`. `python -m mobile_agent --version` prints the version, which `/api/status` also reports.

Start the local API:

```sh
mobile_agent/.venv/bin/python -m mobile_agent serve --manage-device --env-file mobile_agent/.env
```

API tasks are live-only; an unavailable iPhone never falls back to a synthetic run. The separate `demo` command is an offline test fixture only. The service persists up to 100 tasks in a private SQLite journal; it is a single-user runtime, not a multi-user deployment.

## iPhone setup

With `--manage-device` the agent owns the USB iPhone, and the setup API drives it ([setup API](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/setup-api.md)): it finds Xcode and libimobiledevice, the connected and trusted iPhone, and your Apple development teams (from the login keychain); builds WebDriverAgent for that phone into the data folder; and supervises the runner and the USB relay (8100 API, 9100 video), resuming them on the next launch. The Mobster desktop app is one UI for that flow. Without a UI, follow the manual recipe in [usb-wda.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/usb-wda.md) (`scripts/usb-wda.sh` keeps WDA serving).

Keys live in a private env file (`--env-file`, 0600; the setup API writes `TYPESAFE_API_KEY` there). Never put keys in frontend code, command history, screenshots, or committed files. The API never returns model credentials, only a key's last four characters. The Gemini helper uses an existing `gcloud` login with `TEXT_MODEL_PROVIDER=vertex`, an explicit `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION=global`, and `TEXT_MODEL`. Without a helper, Jev can navigate but stops when text generation is required.

Preview one decision without taking an action (the WDA session is adopted automatically):

```sh
mobile_agent/.venv/bin/python -m mobile_agent run 'Open Search' --wda-url http://127.0.0.1:8100 --env-file mobile_agent/.env
```

Add `--execute` only when you want actions, and `--helper` for the text model. `--enable-live` (or the saved setup choice) is the explicit operator gate for API runs.

## Repeated actions (compiled loops)

Requests that repeat an action over a feed ("favourite every photo with a dog", "like every post from @sam", "like anyone who doesn't have blue eyes") are compiled into a small, statically checked loop program (`loops.py`): one helper call writes it, Jev pins its controls from the accessibility tree, and an executor runs it with no model call per item except the item's judgment (the VisionJudge for image content, Jev for text). Every item passes identity and screen guards before and after acting, and is recorded in a per-request ledger before any tap, so no item is acted on twice, including after a crash. The first three items are cross-checked against the step agent. Loops never judge people by race, religion, health, disability or sexual orientation.

When the actions cannot be undone (a swipe deck, likes, follows) and the request does not say what to do with an item Mobster cannot judge or when to stop, the run asks once before starting: the approval states the actions and their bounds (for example "will like up to 20 matching items") and offers "ask me", "pass on it" or "stop" for unsure items. `POST /api/runs/{id}/approval` accepts an optional `choice` with such questions. `POST /api/runs` accepts `dryRun: true`, which judges and logs every item without tapping (scrolling stays allowed); a request that is not a loop only previews its first decision.

## Results and optional schemas

`POST /api/runs` accepts `appId`, `goal`, optional `outputSchema`, and `outputFormat` (`auto`, `text`, `json`, `yaml`, `csv`, or `markdown`, default `auto`). Mode defaults to `live`; the API rejects synthetic mode. Send a unique `Idempotency-Key` for each new task. Retrying that exact key and payload returns the existing task rather than dispatching again. A key reused with a different task, schema, or format returns a conflict. Existing JSON request keys remain compatible. Every `/api/*` route is also served under `/v1/*`; `/api/status` reports `api_version`.

Result format is selected before execution and persisted with the run. The API and extraction pipeline retain canonical JSON data; the dashboard deterministically presents and downloads it in the requested format. Schema authoring syntax (JSON or YAML) is a separate editor choice, parsed before submission. CSV requires an explicit flat-object or bounded array-of-flat-objects schema with named scalar columns. Nested data belongs in JSON, YAML, or Markdown; CSV quoting and spreadsheet-formula guards are presentation protections, not transformations of canonical evidence. Markdown does not generate a new narrative or additional facts.

All agent results include observed AX evidence. With a schema and configured text helper, `summary.data` contains extracted JSON, `data_status` reports extraction state, and `schema_validated` reports local JSON Schema validation. Each non-null scalar must cite an exact observed literal. Schema validation and literal provenance do not prove semantic correctness or task completeness. Missing helper credentials return `data: null` with `helper_unavailable`, never fabricated values.

The supported Draft 2020-12 subset includes typed objects, arrays, scalar values, nullable types, enums, and basic bounds. Every object requires `additionalProperties: false`; arrays require `items` and `maxItems` of at most 100. Remote/local references, regular expressions, and composition keywords are rejected. Hidden content, inferred counts, transformed metrics such as `1.2K` into `1200`, and generated prose summaries are not literal extraction. Without a schema, `auto` output asks the helper to choose a structure; every value must still cite observed text.

Gemini uses native Vertex `generateContent`, JSON-object responses, minimal thinking, local schema validation, and no automatic retries. A helper's explicit `{data: null, citations: []}` response is an abstention (`insufficient_evidence`), not successful schema validation. Malformed or ungrounded responses remain extraction failures. `python -m mobile_agent.eval_gemini --project YOUR_PROJECT --repeats 2` runs a small paid comparison with synthetic text, append-only typing, planning, extraction, missing-data, and instruction-injection fixtures; it takes no phone actions. Other JSON-object chat providers remain opt-in via `TEXT_MODEL_API_KEY`, `TEXT_MODEL_BASE_URL`, and `TEXT_MODEL`.

The local corrected evaluation selected `gemini-3.5-flash-lite`: 16/16 fixtures passed, with 592 ms successful median and 747 ms p95. It was faster than the other model that passed every fixture; this is not a universal ranking. See [evaluation details](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/gemini-eval.md). Set `TEXT_MODEL` to choose it.

## Crash and failure behavior

The journal defaults to `mobile_agent/.state/mobster.sqlite3` in a source checkout and to `~/Library/Application Support/app.mobster.desktop/state/mobster.sqlite3` in an installed copy, never inside site-packages (override with `--state-db`). It uses WAL with synchronous commits, and saves action intent before dispatch. The database and lock files are private to the local user. Journals contain task goals and observed app text; they are not encrypted at rest. Do not publish or commit them.

Interrupted tasks are marked `interrupted` on restart and are never automatically resumed. Idempotency tombstones survive history eviction; replaying an expired task returns 410 instead of starting another. Process leases prevent two Mobster runtimes from controlling the same iPhone. These are local protections, not tenant authorization. Stop takes effect at operation boundaries; a synchronous UIKit action already underway cannot be undone, and a remote relay command already queued may still execute. Ambiguous actions are never retried automatically.

The API reserves a bounded terminal record outside the ordinary event budget, so an exhausted trace cannot prevent crash recovery. Final status, summary, and the terminal event commit together. Actual storage failures remain explicit and recovery fails closed. These durability guarantees apply to API/dashboard tasks; the diagnostic CLI prints events to stdout and does not persist its own run journal.

The dashboard shows the iPhone's live screen (WebDriverAgent's MJPEG stream, relayed by the agent) or an explicit unavailable state; it never shows a stale frame as live. Device health checks detect reachability, not successful sign-in or unrestricted app functionality.

## How the loop works

Each step reads the WebDriverAgent accessibility tree (about 130–210 ms: the expensive `visible` attribute is excluded and visibility is rebuilt geometrically, including occlusion by bars, keyboards and modal alerts), and Jev makes one typed decision over operation, target, completion and blockage. It does not generate text or inspect screenshots. The helper supplies field text, falls back for extraction, and gives at most two recovery hints per run.

Before dispatch the agent re-reads the screen and refuses a stale decision; a plain navigation tap only needs its own target to be unchanged, so lazily loading pages do not invalidate it. Taps land on the observed element's coordinates. A screen counts as settled only after it stops changing for 0.5 s (or matches a screen already proven at rest). Every action is journaled before sending, and ambiguous failures are never retried automatically. Answers are extracted by Jev selecting observed literals (with a generative second opinion) and accepted only after verification: a verdict plus per-claim checks, or two independent extractors agreeing with high per-claim signals. Completed runs are recorded and later replayed step by step when each step's preconditions hold.

The supported primitive set is tap, type, type-and-submit, submit (Return/Go), four-direction scrolling, host keys (Home, volume up/down) and allowlisted app switching. Secure fields, hidden or occluded elements and stale observations fail closed. Completion remains explicitly unverified unless an external task oracle exists.

Observed inference spend is folded into a per-run ledger from model telemetry. `--spend-cap-usd` (on `run` and `serve`) stops the run before further model calls once priced spend reaches the cap, with terminal status `spend_cap`.

## Live video

The agent relays WebDriverAgent's MJPEG stream (device port 9100, forwarded over USB) at `GET /api/device/stream`: one upstream connection whatever the number of viewers, newest-frame delivery so a slow viewer skips frames, and nothing running while nobody watches. When the video port is unreachable it falls back to screenshots. `GET /api/device/video/status` reports the source and frame rate.

## Deployment boundary

A client of the API (such as the desktop app's dashboard) selects apps and tasks, observes run events and the live screen, cancels at operation boundaries, and retains history. Mobster is a single-user, loopback-only runtime for your own iPhone, not a hosted service: it has no multi-user authentication, its traces are not encrypted at rest, and it has no rate limits. "Ask before acting" (`MOBSTER_ASK_BEFORE_ACTING`, on by default) pauses before actions Mobster recognizes as sending, buying, posting, deleting or submitting; it is a heuristic (a label pattern plus Jev's side-effect score), not a guarantee, and typing without Return never asks. Camera, cellular, Apple Pay, biometrics, account permissions and app-specific anti-automation restrictions cannot be assumed merely because an app is installed.

## Verification status

On a USB iPhone 15 Pro (iOS 26): the 14-task suite (Settings and Safari: reading, scrolling, following links, typing a URL, web search, abstention) passed 39/42 and 38/42 attempts across two full runs, graded by an independent oracle; the Settings suite passed 35/35 (22 Sep 2026, internal run reports). That is the only device measured; other iPhone models and iOS versions are untested. Known limits: drawn surfaces and text in images are invisible to the tree, Chrome's hidden tab switcher leaks into the tree (web tasks use Safari), and answer-acceptance thresholds still need calibration data with wrong answers.

## Sources

[TypeSafe Jev API](https://docs.typesafe.ai/api), [Jev model introduction](https://docs.typesafe.ai/introduction), [WebDriverAgent](https://github.com/appium/WebDriverAgent), [libimobiledevice](https://libimobiledevice.org/), [UIKit accessibility](https://developer.apple.com/documentation/uikit/accessibility), [OpenRouter API](https://openrouter.ai/docs/api-reference/overview), [shadcn/ui](https://ui.shadcn.com/).
