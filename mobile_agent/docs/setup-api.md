# Setup and device API (local, loopback only)

The dashboard's setup flow drives these endpoints. Every route follows the
server's local-origin rules (Host must be localhost; Origin must be
allowlisted, including the desktop webview). POST bodies are JSON.

## GET /api/setup

```json
{
  "complete": false,
  "steps": [
    {"id": "tools",       "title": "Get your Mac ready", "state": "done",
     "detail": "Xcode 26.4 and the iPhone connection tools are installed.", "commands": []},
    {"id": "api_key",     "title": "Connect your AI account", "state": "todo",
     "detail": "Mobster uses your own AI account, Claude or OpenAI. You pay them directly.", "action": "save_key"},
    {"id": "phone",       "title": "Connect your iPhone", "state": "todo",
     "detail": "Plug your iPhone into this Mac with a cable and unlock it.", "action": null},
    {"id": "wda_build",   "title": "Install Mobster's helper on your iPhone", "state": "todo",
     "detail": "Xcode signs Mobster's helper with your Apple Account and installs it on your iPhone. The first install can take 10 minutes or more.", "action": "build", "blocked_by": null,
     "problem": null},
    {"id": "wda_running", "title": "Start Mobster's helper", "state": "todo",
     "detail": "Mobster's helper keeps the connection to your iPhone open while Mobster runs.", "action": "start"},
    {"id": "live",        "title": "Allow Mobster to tap and type", "state": "todo",
     "detail": "Turn this on so tasks can tap, scroll and type.", "action": "enable_live"},
    {"id": "video",       "title": "See your iPhone live", "state": "todo",
     "detail": "A live picture of your screen confirms everything works.", "action": null}
  ],
  "device": {"udid": "00008130-…", "name": "iPhone", "model": "iPhone16,1", "modelName": "iPhone 15 Pro", "ios": "26.0.1", "trusted": true, "developerMode": true},
  "devices": [{"udid": "00008130-…", "name": "iPhone", "model": "iPhone16,1", "modelName": "iPhone 15 Pro", "ios": "26.0.1", "trusted": true}],
  "chosen": "00008130-…",
  "teams": [{"id": "ABCDE12345", "name": "Jane Appleseed", "personal": true, "label": "Jane Appleseed (Personal Team)"}],
  "tools": {"xcode": {"state": "ok", "version": "Xcode 26.4", "app": "/Applications/Xcode.app"},
            "libimobiledevice": true, "homebrew": true, "bundled": true,
            "install": {"state": "idle", "line": null, "error": null}},
  "build": {"state": "idle", "error": null, "log_tail": [], "started_at": null, "first": false,
            "expires_at": 1790942400000, "expired": false, "renewal": false, "problem": null, "team": "ABCDE12345"},
  "runner": {"state": "stopped", "error": null, "expires_at": 1790942400000,
             "renewal": {"state": "idle", "waiting_for": null, "error": null, "attempted_at": null, "window_hours": 48}},
  "keys": {"openai": false, "anthropic": false, "jev": false},
  "preflight": [
    {"id": "xcode", "title": "Xcode", "state": "ok", "word": "Ready", "detail": "Xcode 26.4"},
    {"id": "cable", "title": "iPhone cable", "state": "ok", "word": "Connected", "detail": "“iPhone” is connected."},
    {"id": "trust", "title": "Trust this Mac", "state": "ok", "word": "Trusted", "detail": "Your iPhone trusts this Mac."},
    {"id": "developer_mode", "title": "Developer Mode", "state": "unknown", "word": "After the first build",
     "detail": "The switch appears on your iPhone once Xcode has seen it. Setup asks when it's time."}
  ]
}
```

- `state` is one of `done`, `todo`, `working`, `blocked` (`blocked`: an earlier
  step must be done first).
- `blocked_by` is the id of the step a `blocked` step waits on (otherwise
  `null`). That step may itself be blocked; the dashboard follows the chain.
- `commands` (optional) are shell commands the user can copy.
- `build.state` is one of `idle`, `running`, `succeeded`, `failed`.
- `runner.state` is one of `stopped`, `starting`, `running`, `failed`.
- `device` is the chosen iPhone while it is connected (else `null`); `devices`
  lists every connected iPhone and `chosen` is the saved choice. The runner only
  ever starts on the chosen phone. With several phones and no choice, the phone
  step's action is `choose_phone`; a missing chosen phone is named in its detail.
- A build belongs to the phone it was made for; after choosing another phone
  the build step asks to build again.
- `runner.external` is `true` when WebDriverAgent answers but was started outside
  Mobster (for example scripts/usb-wda.sh); Mobster cannot stop that runner.
- `teams` lists Apple development teams: those of the Apple IDs signed in to Xcode
  (Settings › Accounts; a free account's Personal Team has `personal: true`), then any
  other team with an Apple Development certificate in the keychain. `name` may be `null`.
  The list may be empty.
- `tools.bundled` is true when the iPhone connection tools are the desktop app's own copy
  (`Contents/Resources/iphone-tools/bin`, built by `desktop/scripts/build-iphone-tools.sh`; `MOBSTER_IPHONE_TOOLS`
  overrides the folder). They are found before Homebrew's, so the shipped app never asks for Homebrew or Terminal;
  `commands` then stays empty, and otherwise holds the Terminal commands Help › For developers shows.
- Steps are ordered tools first: the Xcode download takes longest, and the key can be
  added while it runs.
- `tools.xcode.state` is `ok`, `missing`, `not_selected` (the Command Line Tools or nothing
  is the active developer directory), `license`, `first_launch`, `no_ios` or `broken`. The
  `/usr/bin/xcodebuild` and `/usr/bin/git` stubs never count as Xcode or git. The tools
  step's `commands` are every Terminal command that fixes what is missing, in order (for
  example `sudo xcode-select -s /Applications/Xcode.app`).
- `device.developerMode` is `true`/`false` when the trusted phone reports it
  (`ideviceinfo -q com.apple.security.mac.amfi -k DeveloperModeStatus`), else `null`.
- `build.started_at` (ms) and `build.first` describe a running build; the first build also
  fetches WebDriverAgent and prepares the phone, so it takes longest.
- `build.expires_at` (ms) is when the built runner's provisioning profile expires (a free
  Apple account signs for 7 days), or `null` when unknown. Once `build.expired` is true the
  build step is `todo` again, the runner is not relaunched, and `/api/status` limitations
  start with "Mobster's helper needs a refresh: its signature expired. Refresh it in Setup". `runner.expires_at` is
  the same time, for Settings › iPhone.
- `build.team` is the team the runner was last built with (Renew rebuilds with it).
- The api_key step ("Connect your AI account") asks for a Claude (Anthropic) or an OpenAI key
  for Smart. It is `done` with either, or with a Jev key (Fast): any engine can run tasks.
  `keys` says which ones are set; an OpenAI or Anthropic helper's key counts as that
  provider's key. The key itself is saved through `POST /api/keys`. The dashboard groups the
  seven steps into three parts (Your Mac: `tools`; AI account: `api_key`; Your iPhone:
  `phone`, Developer Mode read from `device.developerMode`, `wda_build` with `wda_running`,
  then `live` with `video`); the step ids and order are unchanged.
- `preflight` is Setup's first screen: Xcode, the cable, trust and Developer Mode, read at
  the same time (Xcode's state, the USB phones and System Information run side by side).
  `state` is `ok`, `todo` (the user has something to do), `waiting` (an earlier check comes
  first) or `unknown` (nothing can tell yet); `word` is its one-word reading. The cable is
  seen through System Information even before libimobiledevice is installed.

### Translated failures (`problem`)

A failed build (`build.problem`, and the wda_build step's `problem`) and a failing runner
(`runner.problem`, and the wda_running step's `problem`) carry
`{"kind", "fix", "action", "raw"[, "command"]}` from `mobile_agent/setup_errors.py`: `fix`
is one line to act on, `action` the button to offer, `raw` the tool's own words for
"Details". It covers the twelve failures people hit most:

| kind | fix (abridged) | action |
| --- | --- | --- |
| `no_team` | Pick your Apple team | `choose_team` |
| `xcode_signin` | Sign in again in Xcode › Settings › Accounts (a team is chosen, but its sign-in lapsed) | `check_again` |
| `developer_mode` | Turn on Developer Mode | `check_again` |
| `not_trusted` | Unlock your iPhone and tap Trust | `check_again` |
| `locked` | Unlock your iPhone and keep it unlocked | `retry` |
| `xcode_components` | Let Xcode finish installing its components (`command` to copy) | `copy_command` |
| `disk_full` | Free up space on your Mac | `retry` |
| `bundle_conflict` | The runner's app ID is taken by another team | `retry` |
| `app_id_limit` | A free Apple ID can make 10 app IDs a week | `choose_team` |
| `free_app_limit` | Delete an app you installed with Xcode; a free Apple ID allows 3 on the phone | `retry` |
| `untrusted_developer` | Trust your developer app (VPN & Device Management) | `retry` |
| `not_paired` | Unplug your iPhone, plug it back in and tap Trust | `check_again` |

Text nothing recognizes has `kind: "unknown"` and `fix: null`: show `build.error` and keep
`raw` under Details. An expired runner's problem is `{"kind": "expired", "action": "renew"}`.

### Runner renewal

The runner's bundle id carries a suffix derived from the team: `app.mobster.wda.runner.<first
10 hex characters of the SHA-256 of the team id>` (`signing.runner_bundle_id`; a digest, so
the id on the phone doesn't spell out the team). App IDs are unique across all Apple teams,
so a free account could not register the plain id once another team had it. After the first
build with the suffix, Mobster uninstalls exactly `app.mobster.wda.runner.xctrunner` (the
plain-id runner earlier versions installed) from that phone with `xcrun devicectl device
uninstall app`, once per phone, and notes it in the build log.

`runner.renewal` reports `mobile_agent/runner_renewal.py`. While Mobster is open within 48
hours of `expires_at`, with the chosen iPhone connected and unlocked (as WebDriverAgent
reports it) and no task or build running, the watch loop rebuilds the runner once per
signature through the same path as `POST /api/setup/build`, then restarts a running runner
between tasks. A build inside that window first removes Xcode's cached profiles for the
runner that expire within it, so the new signature starts a fresh 7 days. `state` is `idle`,
`due` (inside the window; `waiting_for` is `disconnected`, `locked`, `runner_stopped`, `busy`,
`building` or `retry_later`), `renewing` or `failed` (`error` says why; a failed renewal is retried 6 hours
later). `build.renewal` is true while the build it started runs. `window_hours` is the window
in use.

To prove a renewal on a real phone in one sitting, add `MOBSTER_RENEW_WINDOW_HOURS=170` to
the agent's env file (the Mac app's is `~/Library/Application Support/app.mobster.desktop/agent.env`)
and reopen Mobster. Any signature (7 days at most) is then inside the window, so with the
iPhone connected and unlocked and the runner running, it renews within a few seconds, drops
the cached profile first, and `runner.expires_at` should move forward to about 7 days from
now. Remove the line afterwards: with it set the runner is always due, and is rebuilt every
6 hours. Values outside 1 to 170 are ignored.

## Actions (each returns the new GET /api/setup body)

| route | body | effect |
| --- | --- | --- |
| `POST /api/setup/key` | `{"key": "apikey_…"}` | Saves `TYPESAFE_API_KEY` (Fast) to the agent's private env file and applies it now. The OpenAI key is saved with `POST /api/keys` `{"openai": {"key": "sk-…"}}`. |
| `POST /api/setup/phone` | `{"udid": "00008130-…"}` | Chooses which connected iPhone to use (stops a runner on another phone). |
| `POST /api/setup/build` | `{"team": "ABCDE12345"}` | Fetches WebDriverAgent (first time) and builds it for the connected iPhone. This takes minutes; poll GET. It is also Renew (with `build.team`). |
| `POST /api/setup/start` | `{}` | Starts and supervises the runner and the USB relay (ports 8100 API, 9100 video). |
| `POST /api/setup/stop` | `{}` | Stops them. |
| `POST /api/setup/live` | `{"enabled": true}` | Turns live runs on or off (persisted). |
| `POST /api/setup/refresh` | `{}` | Checks the tools, teams and Developer Mode again now ("Check again"). |
| `POST /api/setup/xcode` | `{}` | Opens Xcode (`open <tools.xcode.app>`), so it can finish installing its components; 409 when it isn't installed. |
| `POST /api/setup/tools` | `{}` | Installs the iPhone connection tools with Homebrew in the background (only a build that doesn't carry them; 409 without Homebrew). `tools.install` reports `state` (`idle`, `running`, `succeeded`, `failed`), the current `line` in words ("Downloading libplist") and an `error`. |

Errors: `400 {"error": "…"}` for invalid input, `409` when a prerequisite is
missing (for example, building with no iPhone connected).

## Keys

`GET /api/keys` returns whether each key is set and its last four characters, never the key:

```json
{"openai": {"configured": true, "keyHint": "…e3d1", "fromHelper": false, "model": "gpt-5.6-sol"},
 "anthropic": {"configured": true, "keyHint": "…a7f2", "fromHelper": false, "model": "claude-sonnet-5-5"},
 "smart": {"provider": "anthropic", "preference": "anthropic"},
 "jev": {"configured": false, "keyHint": null},
 "helper": {"configured": false, "provider": null, "model": null, "baseUrl": null, "keyHint": null, "project": null, "location": null},
 "providers": [...], "persisted": true}
```

`POST /api/keys` takes `{"openai": {"key": "sk-…"}}` or `{"openai": null}` (saved as
`OPENAI_API_KEY`), the same for `anthropic` (`ANTHROPIC_API_KEY`), `jev` and `helper`, and
`{"smartProvider": "anthropic" | "openai" | null}` (`MOBSTER_SMART_PROVIDER`): which saved key
Smart uses when both are saved. Setup's AI account step sends it with the key the person
chose; left unset, Smart keeps its earlier rule (OpenAI's key first), and a choice whose key is
removed falls back to the key that is there (`engines.smart_model`). `smart.provider` is whose
account Smart runs on now (null with no key), and `smart.preference` the saved choice.
`openai.fromHelper` is true when no OpenAI key of its own is saved but the helper's provider is
OpenAI: Smart uses that key.
`POST /api/keys/test` `{"target": "openai"}` makes one free request (`GET
https://api.openai.com/v1/models/gpt-5.6-sol`) and returns `{"ok", "message"}`; a key that
works but can't use the model reads "OpenAI accepted the key, but it can't use gpt-5.6-sol."
`{"target": "anthropic"}` does the same on the Claude API. A failed test carries `problem`:
`rejected` (the provider refused the key), `no_credit` (with `link` to its billing page),
`no_model`, `offline` or `provider_error`, so Setup words the fix itself.

## Live video

`GET /api/device/video/status` returns `{"kind": "mjpeg", "status":
"idle"|"starting"|"live", "sourceKind": "wda_mjpeg"|"wda_screenshot"|null,
"fps": 12.5, "capturedAt": 1790000000000, "stream": "/api/device/stream"}`
for a USB iPhone. `GET /api/device/stream` is `multipart/x-mixed-replace`
JPEG; an `<img>` element plays it. `GET /api/device/frame` is the newest single
frame (503 until one exists).

## Settings

`GET /api/settings` returns `{"askBeforeActing": true, "bypassChecks": false}`.
`POST /api/settings` with `{"askBeforeActing": false}` or `{"bypassChecks": true}`
changes one (persisted as `MOBSTER_ASK_BEFORE_ACTING` / `MOBSTER_BYPASS_CHECKS` in
the agent's env file). Ask before acting is on by default; bypass is off.

## Ask before acting (approvals)

With `askBeforeActing` on, a task pauses before an action that can commit
something (task_policy.needs_approval: a tap on Send/Buy/Post/Delete/…, a
submit in a side-effect task, or a high Jev side-effect score). While it waits,
the run (`GET /api/runs/{id}` and the run list) carries

```json
"approval": {"id": "a1b2c3d4e5f6", "operation": "TYPE_SUBMIT", "label": "Message", "role": "TextField",
             "text": "On my way!", "app": "Messages", "requestedAt": 1790000000000, "expiresAt": 1790000600000}
```

and the event stream has `approval_requested` (without the text; typed text is
never journaled) and later `approval_resolved` with `decision` of `approved`,
`denied`, `timeout` or `stopped`. Answer with `POST /api/runs/{id}/approval`
`{"id": "a1b2c3d4e5f6", "approve": true}` (409 `approval_not_pending` if it was
already answered or expired). A declined action ends the task as
`approval_denied`; no answer within 10 minutes ends it as `approval_timeout`.
Waiting never counts against the task's time limit.

## Direct control

`POST /api/device/control` performs one gesture on the USB iPhone. Coordinates
are fractions of the screen (0..1):

| body | effect |
| --- | --- |
| `{"action": "tap", "x": 0.5, "y": 0.3, "holdMs": 40}` | tap; `holdMs` up to 3000 is a long press |
| `{"action": "swipe", "x1": 0.5, "y1": 0.8, "x2": 0.5, "y2": 0.2, "durationMs": 250}` | drag at that speed (40–3000 ms) |
| `{"action": "type", "text": "coffee", "submit": false}` | type into the focused field; `submit` presses Return |
| `{"action": "button", "name": "home"}` | `home`, `volume_up`, `volume_down` |

Refused with 409 `device_busy` while a task runs and 409 `live_disabled` while
live actions are off; 503 `control_failed` when the phone does not accept it.

## Export (desktop)

`POST /api/export` `{"filename": "mobster-result-abc.json", "text": "…"}` saves
the text to `~/Downloads` without overwriting (`name (1).json`) and returns
`{"name", "path"}`. Only .json, .csv, .yaml, .md and .txt; at most 8 MB. When
the agent may not write to Downloads (in the Mac app, macOS asks the first time), it
answers 403 `downloads_denied` with where to allow it in System Settings. A finished
task saves as a GIF the same way: `POST /api/runs/{id}/gif` (engine-contract.md).
