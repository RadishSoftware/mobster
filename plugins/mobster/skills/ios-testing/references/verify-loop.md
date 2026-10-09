# The verify loop in detail

## verify_start

| Field | Meaning |
|---|---|
| `app_path` | Absolute path of the `.app` built for the iOS Simulator. A relative path is refused |
| `bundle_id` | The app's bundle ID, when the app is already installed (instead of `app_path`) |
| `steps` | The flow in plain English, for the report. May be empty |
| `expect` | The assertions the verdict is decided by. Fixed at start |
| `launch_args`, `launch_env` | Launch arguments and environment, such as `["-SkipOnboarding", "YES"]` |
| `open_url` | A deep link opened after launch, such as `myapp://settings` |
| `reset` | `none`, `data` or `reinstall` |
| `device` | A device from `list_devices`. Default: Mobster's simulator |

It returns `{run_id, status: "ready", screen}`, or `status: "preparing"`: then call `wait` with the `run_id`.

## Assertions

Each assertion is an object with one kind key, checked against the accessibility tree on a settled screen.

| Kind | Form | Holds when |
|---|---|---|
| Text present | `{"text": "Choose your plan"}` | A shown element's label or value contains it (case-insensitive) |
| Text absent | `{"no_text": "Loading"}` | No shown element's label or value contains it |
| Element shown | `{"visible": {selector}}` | At least one shown element matches |
| Element absent | `{"absent": {selector}}` | No shown element matches |
| Value | `{"value": {selector}, "equals": X}` | Exactly one element matches and its value equals X (`true`/`false` for switches) |
| Count | `{"count": {selector}, "equals": N}` | Also `at_least` or `at_most` |

A selector has one or more of `id`, `label`, `value`, `role`, `enabled` and `selected`. `label` and `value` are exact unless written `/regex/` or `/regex/i`. Roles: `button`, `text`, `field`, `switch`, `cell`, `image`, `link`, `tab`, `slider`, `stepper`, `picker`, `segment`, `navbar`, `alert`.

Write expectations that hold only after the steps. If they already hold on the first screen, the verdict is `needs_review`. Content scrolled out of view isn't shown: use `swipe` with `until` to bring it in.

## Example: a setting survives a relaunch

```json
{"tool": "verify_start", "arguments": {
  "app_path": "/abs/path/build/Build/Products/Debug-iphonesimulator/Daybreak.app",
  "launch_args": ["-DaybreakSkipOnboarding", "YES"], "open_url": "daybreak://settings",
  "steps": ["Turn on Daily reminder.", "Relaunch the app and open Settings again."],
  "expect": [{"value": {"id": "daily_reminder"}, "equals": true}]}}
{"tool": "tap", "arguments": {"run_id": "…", "target": {"id": "daily_reminder"}}}
{"tool": "relaunch", "arguments": {"run_id": "…"}}
{"tool": "open_url", "arguments": {"run_id": "…", "url": "daybreak://settings"}}
{"tool": "verify_finish", "arguments": {"run_id": "…"}}
```

## Verdicts

| Verdict | Means | What you do |
|---|---|---|
| `passed` | Every assertion held on one settled screen | Report it, with the report path |
| `failed` | An assertion didn't hold, or the app left the front | Read the failing assertion, fix the code, rebuild, verify again |
| `needs_review` | Nothing decided it: no assertions, or they held before the steps | Add assertions that only hold after the steps |
| `couldnt_run` | The check or this Mac couldn't run it | Read `reason`; `status` and `mobster sim doctor` help |

## Other tools in a run

| Tool | Use |
|---|---|
| `screen` | The outline and a screenshot, without acting |
| `wait_for` | Wait until assertions hold (up to 30 s). It never changes the verdict |
| `alert` | Accept or dismiss a system alert, by button name if needed |
| `save_check` | Save the run's check as `.mobster/checks/<name>.yaml`, so `mobster verify --check` reruns it in CI |
| `stop` | Abandon the run |
| `verify` | Smart: Mobster runs the steps itself on the user's own key. Listed only when the server has one |

One run is open at a time. Every call returns within 45 seconds, and `wait` within 50.

Full reference: https://docs.mobster.dev/mcp-server and https://docs.mobster.dev/checks
