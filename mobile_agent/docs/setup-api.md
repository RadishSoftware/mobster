# Setup and device API (local, loopback only)

The dashboard's setup flow drives these endpoints. Every route follows the
server's local-origin rules (Host must be localhost; Origin must be
allowlisted, including the desktop webview). POST bodies are JSON.

## GET /api/setup

```json
{
  "complete": false,
  "steps": [
    {"id": "tools",       "title": "Install the iPhone tools", "state": "done",
     "detail": "Xcode 26.4 and libimobiledevice are installed.", "commands": []},
    {"id": "api_key",     "title": "Add your Jev key", "state": "todo",
     "detail": "Runs are decided by Jev; the key stays on this Mac.", "action": "save_key"},
    {"id": "phone",       "title": "Connect your iPhone", "state": "todo",
     "detail": "Plug in with a cable, unlock it and tap Trust.", "action": null},
    {"id": "wda_build",   "title": "Install the Mobster runner on your iPhone", "state": "todo",
     "detail": "Builds WebDriverAgent with your Apple team.", "action": "build", "blocked_by": null},
    {"id": "wda_running", "title": "Start the runner", "state": "todo",
     "detail": "Keeps the connection to your iPhone open.", "action": "start"},
    {"id": "live",        "title": "Allow Mobster to act on your iPhone", "state": "todo",
     "detail": "Tasks can tap and type once this is on.", "action": "enable_live"},
    {"id": "video",       "title": "See your iPhone live", "state": "todo",
     "detail": "A live picture of your screen confirms everything works.", "action": null}
  ],
  "device": {"udid": "00008130-…", "name": "iPhone", "model": "iPhone16,1", "modelName": "iPhone 15 Pro", "ios": "26.0.1", "trusted": true, "developerMode": true},
  "devices": [{"udid": "00008130-…", "name": "iPhone", "model": "iPhone16,1", "modelName": "iPhone 15 Pro", "ios": "26.0.1", "trusted": true}],
  "chosen": "00008130-…",
  "teams": [{"id": "ABCDE12345", "name": "Jane Appleseed", "personal": true}],
  "tools": {"xcode": {"state": "ok", "version": "Xcode 26.4", "app": "/Applications/Xcode.app"},
            "libimobiledevice": true, "homebrew": true},
  "build": {"state": "idle", "error": null, "log_tail": [], "started_at": null, "first": false,
            "expires_at": 1790942400000, "expired": false},
  "runner": {"state": "stopped", "error": null}
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
  start with "The runner's signature expired. Rebuild it in Setup".

## Actions (each returns the new GET /api/setup body)

| route | body | effect |
| --- | --- | --- |
| `POST /api/setup/key` | `{"key": "apikey_…"}` | Saves `TYPESAFE_API_KEY` to the agent's private env file and applies it now. |
| `POST /api/setup/phone` | `{"udid": "00008130-…"}` | Chooses which connected iPhone to use (stops a runner on another phone). |
| `POST /api/setup/build` | `{"team": "ABCDE12345"}` | Fetches WebDriverAgent (first time) and builds it for the connected iPhone. This takes minutes; poll GET. |
| `POST /api/setup/start` | `{}` | Starts and supervises the runner and the USB relay (ports 8100 API, 9100 video). |
| `POST /api/setup/stop` | `{}` | Stops them. |
| `POST /api/setup/live` | `{"enabled": true}` | Turns live runs on or off (persisted). |
| `POST /api/setup/refresh` | `{}` | Checks the tools, teams and Developer Mode again now ("Check again"). |

Errors: `400 {"error": "…"}` for invalid input, `409` when a prerequisite is
missing (for example, building with no iPhone connected).

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
`{"name", "path"}`. Only .json, .csv, .yaml, .md and .txt; at most 8 MB.
