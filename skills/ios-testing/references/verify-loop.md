# The verify loop in detail

## verify_start

| Field | Meaning |
|---|---|
| `app_path` | Absolute path of the `.app` built for the iOS Simulator. A relative path is refused |
| `bundle_id` | The app's bundle ID, when the app is already installed (instead of `app_path`) |
| `steps` | The flow in plain English, for the report. May be empty |
| `expect` | Accessibility-tree assertions checked at `verify_finish`. Fixed at start |
| `launch_args`, `launch_env` | Launch arguments and environment, such as `["-SkipOnboarding", "YES"]` |
| `open_url` | A deep link opened after launch, such as `myapp://settings` |
| `reset` | `none`, `data` or `reinstall` |
| `device` | A device from `list_devices`. Default: Mobster's simulator |

It returns `{run_id, status: "ready", screen}`, or `status: "preparing"`: then call `wait` with the `run_id`.

## Assertions

Each assertion below is an object with one kind key. Mobster evaluates these accessibility-tree assertions together against one read.

| Kind | Form | Holds when |
|---|---|---|
| Text present | `{"text": "Choose your plan"}` | A shown element's label or value contains it (case-insensitive) |
| Text absent | `{"no_text": "Loading"}` | No shown element's label or value contains it |
| Element shown | `{"visible": {selector}}` | At least one shown element matches |
| Element absent | `{"absent": {selector}}` | No shown element matches |
| Value | `{"value": {selector}, "equals": X}` | Exactly one element matches and its value equals X (`true`/`false` for switches) |
| Count | `{"count": {selector}, "equals": N}` | Also `at_least` or `at_most` |

A selector has one or more of `id`, `label`, `value`, `role`, `enabled` and `selected`. `label` and `value` are exact unless written `/regex/` or `/regex/i`. Roles: `button`, `text`, `field`, `switch`, `cell`, `image`, `link`, `tab`, `slider`, `stepper`, `picker`, `segment`, `navbar`, `alert`.

Write expectations that hold only after the steps. If they already hold on the first screen, the run may end `needs_review`. Content scrolled out of view isn't shown: use `swipe` with `until` to bring it in.

## Example: a setting survives a relaunch

```json
{"tool": "verify_start", "arguments": {
  "app_path": "/abs/path/build/Build/Products/Debug-iphonesimulator/YourApp.app",
  "launch_args": ["-SkipOnboarding", "YES"], "open_url": "yourapp://settings",
  "steps": ["Turn on Daily reminder.", "Relaunch the app and open Settings again."],
  "expect": [{"value": {"id": "daily_reminder"}, "equals": true}]}}
{"tool": "tap", "arguments": {"run_id": "…", "target": {"id": "daily_reminder"}}}
{"tool": "launch_app", "arguments": {"run_id": "…", "restart": true}}
{"tool": "open_url", "arguments": {"run_id": "…", "url": "yourapp://settings"}}
{"tool": "verify_finish", "arguments": {"run_id": "…"}}
```

## Verdicts

| Verdict | Means | What you do |
|---|---|---|
| `passed` | The assertions and execution checks passed | Report it, with the report path |
| `failed` | An assertion or another check failed, such as crash detection | Read the failure and report, fix the cause, rebuild and verify again |
| `needs_review` | The run needs review, for example because evidence or required measurements are missing | Read `reason` and the report before rerunning |
| `couldnt_run` | The run could not finish, for example because of an invalid check, an execution error or a resource limit | Read `reason` and fix the reported problem |

## Other tools in a run

| Tool | Use |
|---|---|
| `screen` | The outline and a screenshot, without acting |
| `wait_for` | Wait until assertions hold (up to 30 s). It never changes the verdict |
| `verify_start` with `smart` | Mobster runs the steps itself on the user's own key; `wait` for the verdict. Refused unless Mobster for Mac has a key |
| `alert` | Accept or dismiss a system alert, by button name if needed |
| `save_check` | Save the run's check, or a task from the Mobster app, as `.mobster/checks/<name>.yaml`, so `run_tests` runs it again |
| `stop` | Abandon the run |

One run is open at a time. Every call returns within 45 seconds, and `wait` within 50.

Full reference: https://docs.mobster.dev/mcp-server and https://docs.mobster.dev/checks
